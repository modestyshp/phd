"""PhD collector: no AI, no tokens. Reads sources.json, finds matching
opportunities, writes docs/data.json and docs/index.html."""
import json, re, datetime as dt, os, time
from urllib.parse import urljoin
import requests, feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dp

TODAY = dt.date.today()
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
     "Accept": "text/html,application/xhtml+xml,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9,fr;q=0.8"}

def fetch(url):
    last = None
    for i in range(3):
        try:
            r = requests.get(url, headers=H, timeout=30)
            if r.status_code in (429, 412, 503): time.sleep(5 * (i + 1)); last = r; continue
            r.raise_for_status(); return r
        except requests.exceptions.ConnectionError as e:
            last = e; time.sleep(4)
    if hasattr(last, "raise_for_status"): last.raise_for_status()
    raise last
FIELD = re.compile(r"linguistic|tesol|tefl|\belt\b|\befl\b|\bcall\b|language (education|teaching|learning|acquisition)|applied lang|"
                   r"educational technolog|digital (learning|education|humanities)|e-learning|didactic|didactique|"
                   r"sciences? de l.[ée]ducation|education|pedagog|teacher", re.I)
FUND = re.compile(r"phd|ph\.d|doctor|doctoral|doctorat|scholarship|bourse|stipend|assistantship|fellowship|promotion", re.I)

def deadline(text):
    m = re.search(r"(deadline|closing date|apply (before|by)|application (deadline|date)|date limite|closes?)\W{0,5}(.{0,40})", text, re.I)
    if not m: return ""
    try:
        d = dp.parse(m.group(4), fuzzy=True, dayfirst=True, default=dt.datetime(TODAY.year, 1, 1)).date()
        return d.isoformat() if d >= TODAY else ""
    except Exception: return ""

def fund_type(t):
    t = t.lower()
    if "assistant" in t: return "Assistantship"
    if re.search(r"scholarship|bourse|fellowship|stipend", t): return "Scholarship"
    return "PhD job / position"

def from_feed(src):
    out = []
    for e in feedparser.parse(src["url"], request_headers=H).entries:
        txt = f"{e.get('title','')} {e.get('summary','')}"
        out.append((e.get("title", ""), e.get("link", ""), txt))
    return out

def from_html(src):
    r = fetch(src["url"])
    soup = BeautifulSoup(r.text, "html.parser"); out = []
    for a in soup.find_all("a", href=True):
        t = " ".join(a.get_text().split())
        if len(t) < 15: continue
        ctx = " ".join((a.find_parent(["li", "article", "div", "tr"]) or a).get_text().split())[:400]
        out.append((t, urljoin(src["url"], a["href"]), ctx))
    return out


# ---------- READING EACH PAGE (the part that replaces manual checking) ----------
KEY = os.environ.get("GEMINI_API_KEY", "")
MAX_PAGES = int(os.environ.get("MAX_PAGES", "80"))
BAD = re.compile(r"youtube\.com|youtu\.be|twitter\.com|//x\.com|facebook\.com|instagram\.com|linkedin\.com|admin\.ch/opc|\.pdf(\?|$)", re.I)
DOCS = {"CV": r"\bCV\b|curriculum vitae|resume", "Motivation letter": r"motivation|cover letter|statement of purpose|lettre de motivation",
        "Research proposal": r"research proposal|project proposal|research plan|projet de recherche",
        "Transcripts": r"transcript|relev[ée]s de notes", "Recommendation letters": r"recommendation|reference letter|referee|lettre de recommandation",
        "Passport / ID": r"passport|passeport", "Language test": r"IELTS|TOEFL|DELF|DALF|TCF|TestDaF|DSH|Cambridge|language (proficiency|certificate)"}

class LLMError(Exception): pass

_model = None
def pick_model():
    """Ask Google which models this key can use and pick a current Flash model."""
    global _model
    if _model: return _model
    if os.environ.get("GEMINI_MODEL"):
        _model = os.environ["GEMINI_MODEL"]; return _model
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", headers={"x-goog-api-key": KEY}, timeout=30)
    if r.status_code != 200: raise LLMError(f"model list {r.status_code}: {r.text[:200]}")
    names = [m["name"].split("/")[-1] for m in r.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
    if "gemini-flash-latest" in names:
        _model = "gemini-flash-latest"; return _model
    ver = lambda n: float(re.search(r"gemini-(\d+(?:\.\d+)?)", n).group(1)) if re.search(r"gemini-(\d+(?:\.\d+)?)", n) else 0
    flash = [n for n in names if "flash" in n and not re.search(r"image|tts|live|audio|thinking|embed|8b|robotics", n)]
    stable = [n for n in flash if not re.search(r"lite|preview|exp", n)]
    pool = stable or flash or names
    if not pool: raise LLMError("no usable model for this key")
    _model = sorted(pool, key=ver, reverse=True)[0]
    return _model

def page_text(url):
    r = fetch(url)
    if "pdf" in r.headers.get("content-type", "").lower(): return ""
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "noscript"]): t.decompose()
    return " ".join(soup.get_text(" ").split())[:7000]

def quick_facts(text):
    docs = [k for k, rx in DOCS.items() if re.search(rx, text, re.I)]
    amt = re.search(r"(?:€|\$|£|EUR|USD|GBP|CAD|HK\$)\s?[\d][\d,\. ]{2,}(?:\s?(?:per|/|a)\s?(?:month|year|annum))?", text)
    return {"deadline": deadline(text), "requirements": docs, "funding_details": amt.group(0).strip() if amt else ""}

PROMPT = """You read a web page about a PhD / doctoral funding opportunity for a Moroccan student holding a Master in ELT (English Language Teaching) and Digital Technology in education.
Use ONLY the page text below. If something is not stated, write "unknown". Never guess dates or amounts. Today is %s.
Return ONLY JSON with these keys:
is_phd_funding (true/false: is this a PhD/doctoral position, scholarship, assistantship or fellowship, not a menu, news or master's page),
country, institution,
funding_type (one of: Scholarship, Assistantship, PhD job (paid), Other, unknown),
funding_details (amount, duration, what is covered),
deadline (YYYY-MM-DD or unknown),
eligible_fields (subjects accepted),
who_can_apply (nationality, degree, age or other limits),
requirements (list of documents and language tests needed),
fit_elt_dt (high, medium, low or unknown: would an applied linguistics / ELT / educational technology master's graduate be eligible),
fit_reason (one short sentence),
how_to_apply (one short sentence),
summary (one short sentence).
PAGE TEXT:
%s"""

def ask_llm(text):
    body = {"contents": [{"parts": [{"text": PROMPT % (TODAY.isoformat(), text)}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{pick_model()}:generateContent"
    for i in range(3):
        r = requests.post(url, headers={"x-goog-api-key": KEY, "Content-Type": "application/json"}, json=body, timeout=90)
        if r.status_code == 429: time.sleep(20 * (i + 1)); continue
        if r.status_code != 200: raise LLMError(f"{r.status_code} {r.text[:200]}")
        out = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(re.sub(r"^```(?:json)?|```$", "", out.strip()).strip())
    raise LLMError("rate limited (free quota used up for now)")

def add_my_links(items, old):
    if not os.path.exists("my_links.txt"): return
    for line in open("my_links.txt", encoding="utf-8"):
        u = line.strip()
        if u.startswith("http") and u not in items:
            items[u] = {"title": u, "link": u, "country": "", "source": "My links", "fund": "unknown", "deadline": "",
                        "first_seen": old.get(u, {}).get("first_seen", TODAY.isoformat())}

def enrich(items, old, log):
    for link in list(items):
        if not link.startswith("http") or BAD.search(link): del items[link]
    use_ai = bool(KEY)
    if use_ai:
        try: log.append("MODEL " + pick_model())
        except Exception as ex: use_ai = False; log.append(f"MODEL ERROR {str(ex)[:200]}")
    def prio(l):
        o = items[l]; t = o["title"]
        if o["source"] == "My links": return (-1, -1)
        return (0 if FIELD.search(t) else 1, 0 if re.search(r"phd|doctor|studentship|fellowship|scholarship", t, re.I) else 1)
    done = n_ok = n_fail = ai_fail = 0
    for link in sorted(items, key=prio):
        o = items[link]
        inf = old.get(link, {}).get("info")
        if inf and (inf.get("ai") or not use_ai or (inf.get("retry") and inf["retry"] > TODAY.isoformat())):
            o["info"] = inf; continue
        if done >= MAX_PAGES or ai_fail >= 3: continue
        done += 1
        try: text = page_text(link)
        except Exception as ex:
            n_fail += 1; log.append(f"READFAIL {link[:70]}: {str(ex)[:70]}")
            o["info"] = {"summary": "The script could not open this page. Open it and check.", "ai": False,
                         "retry": (TODAY + dt.timedelta(days=7)).isoformat()}
            continue
        info = quick_facts(text); info["ai"] = False
        if len(text) < 300:
            info["summary"] = "Page needs JavaScript, so it could not be read automatically. Open it and check."
            info["retry"] = (TODAY + dt.timedelta(days=14)).isoformat()
        elif use_ai:
            try:
                got = ask_llm(text)
                info.update({k: v for k, v in got.items() if v not in (None, "", [], "unknown")})
                info["ai"] = True; ai_fail = 0; time.sleep(5)
            except Exception as ex:
                ai_fail += 1; log.append(f"AIFAIL {link[:60]}: {str(ex)[:160]}")
                if ai_fail >= 3: log.append("AI STOPPED after 3 errors in a row, see AIFAIL lines above")
                continue
        o["info"] = info; n_ok += 1
    for link in list(items):
        inf = items[link].get("info")
        if not inf: continue
        if inf.get("is_phd_funding") is False: del items[link]; continue
        o = items[link]
        if inf.get("country") and not o.get("country"): o["country"] = inf["country"]
        if inf.get("funding_type") in ("Scholarship", "Assistantship", "PhD job (paid)", "Other"): o["fund"] = inf["funding_type"]
        d = inf.get("deadline", "")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(d)): o["deadline"] = d
    log.append(f"READ  {n_ok} pages read this run, {n_fail} could not be opened, AI {'ON' if use_ai else 'OFF'}")

def main():
    srcs = json.load(open("sources.json", encoding="utf-8"))
    old = {}
    if os.path.exists("docs/data.json"):
        old = {o["link"]: o for o in json.load(open("docs/data.json", encoding="utf-8"))["items"]}
    items, log = {}, []
    for s in srcs:
        time.sleep(1.5)
        try:
            rows = from_feed(s) if s["type"] == "rss" else from_html(s)
            n = 0
            for title, link, txt in rows:
                blob = f"{title} {txt}"
                ok = FIELD.search(blob) and FUND.search(blob) if s.get("match", "field") == "field" else FUND.search(title)
                if not ok or not link or link in items: continue
                items[link] = {"title": title[:160], "link": link, "country": s["country"], "source": s["name"],
                               "fund": fund_type(blob), "deadline": deadline(blob),
                               "first_seen": old.get(link, {}).get("first_seen", TODAY.isoformat())}
                n += 1
            log.append(f"OK    {s['name']}: {n} matches (of {len(rows)} links)")
        except Exception as ex:
            log.append(f"FAIL  {s['name']}: {ex}")
    add_my_links(items, old)
    enrich(items, old, log)
    keep = [o for o in items.values() if not o["deadline"] or o["deadline"] >= TODAY.isoformat()]
    os.makedirs("docs", exist_ok=True)
    data = {"updated": TODAY.isoformat(), "items": keep, "log": log,
            "programmes": json.load(open("programmes.json", encoding="utf-8")), "watch": json.load(open("watch.json", encoding="utf-8"))}
    json.dump(data, open("docs/data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    page = open("template.html", encoding="utf-8").read().replace("/*DATA*/null", json.dumps(data, ensure_ascii=False))
    open("docs/index.html", "w", encoding="utf-8").write(page)
    print("\n".join(log), f"\nTOTAL {len(keep)}")

if __name__ == "__main__": main()
