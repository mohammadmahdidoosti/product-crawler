"""Keyword discovery, keyword-first (same method as the industrial keyword research):
for each real keyword, search it exactly as written (Serper, gl=ir hl=fa), keep the top 10 results and Google's related
searches, skip marketplaces and content-only pages, and read the category menu of up to 5 specialised shops.
In:  /data/tmp/<name>.tsv  (id, keyword, taxonomy group, class)  -- kept on the server, not in the repo
Out: /data/runs/disc-kw/<name>.jsonl (one line per keyword, resumable); menus cached in /data/runs/disc-kw/hosts.jsonl
Run in the app container: python tools/kwdisc/disc_kw.py <name> [workers]"""
import sys, re, json, html, threading
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/app")
import app
from crawler.suppliers import _serper
from crawler.pipeline import _fetchers

NAME = sys.argv[1] if len(sys.argv) > 1 else "kw100"
W = int(sys.argv[2]) if len(sys.argv) > 2 else 6
f = _fetchers(app.build_cfg())["iran"]
OUT = Path("/data/runs/disc-kw"); OUT.mkdir(parents=True, exist_ok=True)
MARKET = re.compile(r"digikala|torob|divar|emalls|basalam|sheypoor|technolife|snappshop|mrtakhfif|digistyle|okala|"
                    r"amazon|alibaba|aliexpress|ebay|timcheh|banimode|modiseh|sesbaz|kalaoma|dmrco|irantejarat|"
                    r"hamsanat|factoryland|iranmarkets|eavar|kilid|esam", re.I)
SOCIAL = re.compile(r"aparat|wikipedia|wikiwand|youtube|instagram|telegram|t\.me|eitaa|rubika|bale\.ai|linkedin|facebook|"
                    r"pinterest|google|filimo|namasha|twitter|x\.com", re.I)
NEWS = re.compile(r"zoomit|namnak|beytoote|mihanblog|blogfa|virgool|tebyan|hamshahri|isna|irna|eghtesad|ninisite|"
                  r"tasnimnews|farsnews|mehrnews|khabaronline|yjc|entekhab|asriran|tabnak|digiato|chetor|bartarinha|"
                  r"setare|rokna|khabarfoori|honarmrooz|parsine|fararu|ana\.press|irib|varzesh3|car\.ir|bama\.ir", re.I)
CONTENT_PATH = re.compile(r"/(blog|mag|magazine|article|articles|news|post|wiki|learn|amoozesh|maghale|maqale)/", re.I)
CAT = re.compile(r"product-category|/category/|/cat/|/categories/|/product-cat|/shop/|/products/|/collection|/c/|/pc/|"
                 r"/brand/|/product-tag/|/دسته", re.I)
BLOCK = re.compile(r'<(nav|header)\b.*?</\1>|<(ul|div)\b[^>]*class="[^"]*(?:menu|cat|nav|mega)[^"]*"[^>]*>.*?</\2>',
                   re.S | re.I)
TAG = re.compile(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', re.S | re.I)
LOCK = threading.Lock()
hosts_cache = {}
hp = OUT / "hosts.jsonl"
if hp.exists():
    for l in open(hp, encoding="utf-8"):
        try: d = json.loads(l); hosts_cache[d["h"]] = d
        except Exception: pass
dp = OUT / f"{NAME}.jsonl"
done = set()
if dp.exists():
    for l in open(dp, encoding="utf-8"):
        try: done.add(json.loads(l)["id"])
        except Exception: pass


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def menu_of(h):
    if h in hosts_cache:
        return hosts_cache[h]
    r = f.get("https://" + h + "/") or {}
    t = r.get("text") or ""
    title = clean((re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I) or [None, ""])[1])[:120]
    menu, cats = [], []
    for b in BLOCK.finditer(t):
        for href, txt in TAG.findall(b.group(0)):
            u = urljoin("https://" + h + "/", href)
            if urlparse(u).netloc.lower().removeprefix("www.") != h:
                continue
            n = clean(txt)
            if 2 <= len(n) <= 50 and n not in menu:
                menu.append(n)
    for href, txt in TAG.findall(t):
        u = urljoin("https://" + h + "/", href)
        if urlparse(u).netloc.lower().removeprefix("www.") != h or not CAT.search(unquote(u)):
            continue
        n = clean(txt)
        if 2 <= len(n) <= 50 and n not in cats:
            cats.append(n)
    d = {"h": h, "title": title, "ok": bool(t), "cats": cats[:150], "menu": menu[:200]}
    with LOCK:
        hosts_cache[h] = d
        with open(hp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    return d


def kind(h, link):
    if MARKET.search(h): return "market"
    if SOCIAL.search(h): return "social"
    if NEWS.search(h): return "news"
    if CONTENT_PATH.search(unquote(urlparse(link).path)): return "content"
    return "site"


def one(row):
    sid, kw, grp, cls = (row + ["", "", ""])[:4]
    if int(sid) in done:
        return
    j = _serper(f, "search", kw, 10) or {}
    org, shops = [], []
    for o in j.get("organic") or []:
        link = o.get("link", "")
        h = urlparse(link).netloc.lower().removeprefix("www.")
        k = kind(h, link)
        org.append({"h": h, "k": k, "t": o.get("title", "")[:120], "u": unquote(link)[:200], "s": o.get("snippet", "")[:160]})
        if k == "site" and h and h not in shops:
            shops.append(h)
    sites = {}
    for h in shops[:5]:
        try:
            sites[h] = menu_of(h)
        except Exception:
            sites[h] = {"h": h, "ok": False, "cats": [], "menu": []}
    rec = {"id": int(sid), "kw": kw, "group": grp, "class": cls, "organic": org,
           "related": [r.get("query", "") for r in (j.get("relatedSearches") or [])],
           "paa": [r.get("question", "") for r in (j.get("peopleAlsoAsk") or [])], "sites": sites}
    with LOCK:
        with open(dp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


rows = [l.rstrip("\n").split("\t") for l in open(f"/data/tmp/{NAME}.tsv", encoding="utf-8") if l.strip()]
with ThreadPoolExecutor(W) as ex:
    for k, _ in enumerate(ex.map(one, rows), 1):
        if k % 25 == 0:
            print("DISC", k, len(rows), flush=True)
print("DONE", len(rows), flush=True)
