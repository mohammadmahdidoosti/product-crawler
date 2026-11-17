"""Keyword discovery from seller sites: for each taxonomy sub-group, search 2 business-intent seeds (Serper),
take the top specialised sellers (marketplaces, news, social skipped) and read the category links on their home page.
In:  /data/tmp/seeds.tsv  (id, group, class, n_keywords, seed1, seed2)  -- kept on the server, not in the repo
Out: /data/runs/disc-all/disc.jsonl (one line per sub-group, resumable) and hosts.jsonl (category cache per site)
Run in the app container: python tools/kwdisc/disc_all.py [workers]"""
import sys, re, json, html, threading
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/app")
import app
from crawler.suppliers import _serper
from crawler.pipeline import _fetchers

f = _fetchers(app.build_cfg())["iran"]
OUT = Path("/data/runs/disc-all"); OUT.mkdir(parents=True, exist_ok=True)
SKIP = re.compile(r"digikala|torob|divar|emalls|basalam|sheypoor|aparat|wikipedia|wikiwand|youtube|instagram|technolife|snappshop|"
                  r"mrtakhfif|zoomit|namnak|beytoote|mihanblog|blogfa|virgool|tebyan|hamshahri|isna|irna|eghtesad|donya-e-eqtesad|"
                  r"ninisite|filimo|google|telegram|t\.me|eitaa|rubika|bale|amazon|alibaba|aliexpress|ebay|linkedin|facebook|"
                  r"pinterest|behtarino|neshan|balad|safarmarket|digistyle|okala|tapsi|snapp|bama\.ir|ketabrah|fidibo|"
                  r"tasnimnews|farsnews|mehrnews|khabaronline|yjc|entekhab|asriran|tabnak", re.I)
CAT = re.compile(r"product-category|/category/|/cat/|/categories/|/product-cat|/shop/|/products/|/collection|/c/|/pc/|/brand/", re.I)
TAG = re.compile(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', re.S | re.I)
LOCK = threading.Lock()
hosts_cache = {}
hp = OUT / "hosts.jsonl"
if hp.exists():
    for l in open(hp, encoding="utf-8"):
        try: d = json.loads(l); hosts_cache[d["h"]] = d["cats"]
        except Exception: pass
done = set()
dp = OUT / "disc.jsonl"
if dp.exists():
    for l in open(dp, encoding="utf-8"):
        try: done.add(json.loads(l)["id"])
        except Exception: pass


def cats_of(h):
    if h in hosts_cache:
        return hosts_cache[h]
    r = f.get("https://" + h + "/") or {}
    t = r.get("text") or ""
    cats = []
    for href, txt in TAG.findall(t):
        u = urljoin("https://" + h + "/", href)
        if urlparse(u).netloc.lower().removeprefix("www.") != h or not CAT.search(unquote(u)):
            continue
        name = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", txt))).strip()
        if 2 <= len(name) <= 50 and name not in cats:
            cats.append(name)
    cats = cats[:150]
    with LOCK:
        hosts_cache[h] = cats
        with open(hp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"h": h, "cats": cats}, ensure_ascii=False) + "\n")
    return cats


def one(row):
    sid, grp, cls, n, q1, q2 = row
    if int(sid) in done:
        return
    hosts, titles = [], []
    for q in (q1, q2):
        j = _serper(f, "search", q, 10)
        for o in j.get("organic") or []:
            h = urlparse(o.get("link", "")).netloc.lower().removeprefix("www.")
            if h and not SKIP.search(h) and h not in hosts:
                hosts.append(h); titles.append(o.get("title", ""))
        for r in (j.get("relatedSearches") or []):
            titles.append("REL:" + r.get("query", ""))
    sites = {}
    for h in hosts[:4]:
        try:
            sites[h] = cats_of(h)
        except Exception as e:
            sites[h] = []
    rec = {"id": int(sid), "group": grp, "class": cls, "n": int(n), "hosts": hosts[:10], "sites": sites,
           "related": [t[4:] for t in titles if t.startswith("REL:")]}
    with LOCK:
        with open(dp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


rows = [l.rstrip("\n").split("\t") for l in open("/data/tmp/seeds.tsv", encoding="utf-8") if l.strip()]
W = int(sys.argv[1]) if len(sys.argv) > 1 else 6
with ThreadPoolExecutor(W) as ex:
    for k, _ in enumerate(ex.map(one, rows), 1):
        if k % 50 == 0:
            print("DISC", k, len(rows), flush=True)
print("DONE", len(rows), flush=True)
