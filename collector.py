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
    keep = [o for o in items.values() if not o["deadline"] or o["deadline"] >= TODAY.isoformat()]
    os.makedirs("docs", exist_ok=True)
    data = {"updated": TODAY.isoformat(), "items": keep, "log": log,
            "programmes": json.load(open("programmes.json", encoding="utf-8")), "watch": json.load(open("watch.json", encoding="utf-8"))}
    json.dump(data, open("docs/data.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    page = open("template.html", encoding="utf-8").read().replace("/*DATA*/null", json.dumps(data, ensure_ascii=False))
    open("docs/index.html", "w", encoding="utf-8").write(page)
    print("\n".join(log), f"\nTOTAL {len(keep)}")

if __name__ == "__main__": main()
