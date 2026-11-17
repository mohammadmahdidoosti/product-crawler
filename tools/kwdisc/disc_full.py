"""Full keyword discovery over the "missing" keywords (keyword-first, the method tested on 100 keywords).
For each keyword: search it exactly as written (Serper, gl=ir hl=fa); when the first page is mostly marketplaces
or has few specialised shops, search "<kw> عمده" too; when few result titles contain the keyword (another meaning),
search "<kw> <taxonomy leaf>" instead. The category menus of the specialised shops found are read once per site.
Nothing is classified here: everything is kept, labelling is done afterwards from the saved results.
In:  /data/tmp/nd.csv (one keyword per line, header 'kw') and /data/tmp/leaf.txt (leaf names, '#', one leaf id per
     keyword) -- both kept on the server, not in the repo.
Out: /data/runs/disc-full/disc.jsonl (one line per keyword, resumable) and hosts.jsonl (menu cache per site)
Run in the app container: python tools/kwdisc/disc_full.py [workers]
Stops by itself (exit 3) when Serper keeps failing (e.g. no credit left); re-run to resume."""
import sys, re, json, html, threading, shutil
from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/app")
import app
from crawler.suppliers import _serper
from crawler.pipeline import _fetchers

W = int(sys.argv[1]) if len(sys.argv) > 1 else 32
f = _fetchers(app.build_cfg())["iran"]
f.timeout, f.retries = 8.0, 0          # a slow shop is skipped rather than waited for (time, not quality)
OUT = Path("/data/runs/disc-full"); OUT.mkdir(parents=True, exist_ok=True)
hp, dp = OUT / "hosts.jsonl", OUT / "disc.jsonl"
if not hp.exists() and Path("/data/runs/disc-kw/hosts.jsonl").exists():
    shutil.copy("/data/runs/disc-kw/hosts.jsonl", hp)

MARKET = re.compile(r"digikala|torob|divar|emalls|basalam|sheypoor|technolife|snappshop|mrtakhfif|digistyle|okala|"
                    r"amazon|alibaba|aliexpress|ebay|timcheh|banimode|modiseh|sesbaz|kalaoma|esam|cafebazaar|myket|"
                    r"iranmarket\.app|grocery\.snapp|snapp\.ir|khanoumi|digikalamag|zarebin", re.I)
SOCIAL = re.compile(r"aparat|wikipedia|wikiwand|wikifeqh|wikishia|wiki\.|youtube|instagram|telegram|t\.me|eitaa|rubika|"
                    r"bale\.ai|linkedin|facebook|pinterest|google|filimo|namava|namasha|twitter|x\.com|reddit|neshan|"
                    r"balad\.ir|abadis|vajehyab|dehkhoda", re.I)
NEWS = re.compile(r"zoomit|namnak|beytoote|mihanblog|blogfa|virgool|tebyan|hamshahri|isna|irna|eghtesad|ninisite|"
                  r"tasnimnews|farsnews|mehrnews|khabaronline|yjc|entekhab|asriran|tabnak|digiato|chetor|bartarinha|"
                  r"setare|rokna|khabarfoori|honarmrooz|parsine|fararu|ana\.press|irib|varzesh3|faradars|"
                  r"tamin\.ir|my\.tamin", re.I)
CONTENT_PATH = re.compile(r"/(blog|mag|magazine|article|articles|news|post|wiki|learn|amoozesh|maghale|maqale)/", re.I)
CONTENT_HOST = re.compile(r"^(mag|blog|news|learn)\.", re.I)
CAT = re.compile(r"product-category|/category/|/cat/|/categories/|/product-cat|/shop/|/products/|/collection|/c/|/pc/|"
                 r"/brand/|/product-tag/|/دسته", re.I)
BLOCK = re.compile(r'<(nav|header)\b.*?</\1>|<(ul|div)\b[^>]*class="[^"]*(?:menu|cat|nav|mega)[^"]*"[^>]*>.*?</\2>',
                   re.S | re.I)
TAG = re.compile(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>', re.S | re.I)
GEN = {"صنعتی", "تجهیزات", "لوازم", "انواع", "خرید", "فروش", "قیمت", "دستگاه", "ابزار", "قطعات", "محصولات", "سیستم",
       "عمده", "جانبی", "مخصوص", "برای", "کامل"}
LOCK = threading.Lock()
hosts_cache = {}
if hp.exists():
    for l in open(hp, encoding="utf-8"):
        try: d = json.loads(l); hosts_cache[d["h"]] = d
        except Exception: pass
done = set()
if dp.exists():
    for l in open(dp, encoding="utf-8"):
        try: done.add(json.loads(l)["id"])
        except Exception: pass
fails = [0]


def norm(s):
    s = s.replace("ي", "ی").replace("ك", "ک").replace("‌", " ")
    return re.sub(r"\s+", " ", s).strip()


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def menu_of(h):
    with LOCK:
        if h in hosts_cache:
            return
        hosts_cache[h] = {"h": h, "pending": True}
    try:
        r = f.get("https://" + h + "/") or {}
    except Exception:
        r = {}
    t = r.get("text") or ""
    title = clean((re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I) or [None, ""])[1])[:120]
    menu, cats = [], []
    for b in BLOCK.finditer(t):
        for href, txt in TAG.findall(b.group(0)):
            try:
                u = urljoin("https://" + h + "/", href)
                if urlparse(u).netloc.lower().removeprefix("www.") != h:
                    continue
            except ValueError:                      # a malformed link on the page
                continue
            n = clean(txt)
            if 2 <= len(n) <= 50 and n not in menu:
                menu.append(n)
    for href, txt in TAG.findall(t):
        try:
            u = urljoin("https://" + h + "/", href)
            if urlparse(u).netloc.lower().removeprefix("www.") != h or not CAT.search(unquote(u)):
                continue
        except ValueError:
            continue
        n = clean(txt)
        if 2 <= len(n) <= 50 and n not in cats:
            cats.append(n)
    d = {"h": h, "title": title, "ok": bool(t), "cats": cats[:150], "menu": menu[:200]}
    with LOCK:
        hosts_cache[h] = d
        with open(hp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")


def kind(h, link, title):
    if MARKET.search(h): return "market"
    if SOCIAL.search(h): return "social"
    if NEWS.search(h): return "news"
    if CONTENT_HOST.search(h) or CONTENT_PATH.search(unquote(urlparse(link).path)): return "content"
    return "site"


def search(q):
    j = _serper(f, "search", q, 10) or {}
    if "searchParameters" not in j:
        return None
    org = []
    for o in j.get("organic") or []:
        link = o.get("link", "")
        h = urlparse(link).netloc.lower().removeprefix("www.")
        org.append({"h": h, "k": kind(h, link, o.get("title", "")), "t": o.get("title", "")[:110]})
    return org


def shops_of(org, n):
    out = []
    for o in org:
        if o["k"] == "site" and o["h"] and o["h"] not in out:
            out.append(o["h"])
    return out[:n]


def one(item):
    try:
        _one(item)
    except Exception as e:                          # one bad keyword or page must not stop the whole run
        print("ERR", item[0], type(e).__name__, str(e)[:120], flush=True)


def _one(item):
    i, kw, leaf = item
    if i in done or fails[0] >= 25:
        return
    org = search(kw)
    if org is None:
        with LOCK: fails[0] += 1
        return
    k = norm(kw)
    toks = [w for w in k.split() if len(w) > 2 and w not in GEN] or k.split()
    match = sum(1 for o in org if all(w in norm(o["t"]) for w in toks)) / max(1, len(org))
    mk = sum(1 for o in org if o["k"] == "market")
    s1 = shops_of(org, 5)
    q2 = None
    if match < 0.3 and leaf and norm(leaf) not in k:
        q2 = ("ctx", kw + " " + leaf)
    elif mk >= 3 or len(s1) <= 2:
        q2 = ("omde", kw + " عمده")
    org2, s2 = None, []
    if q2:
        org2 = search(q2[1])
        if org2 is None:
            with LOCK: fails[0] += 1
            return
        s2 = [h for h in shops_of(org2, 4) if h not in s1]
    for h in s1 + s2:
        try:
            menu_of(h)
        except Exception as e:
            print("ERR host", h, type(e).__name__, flush=True)
    rec = {"id": i, "kw": kw, "leaf": leaf, "match": round(match, 2), "org": org, "shops": s1}
    if q2:
        rec.update({"q2k": q2[0], "q2": q2[1], "org2": org2, "shops2": s2})
    with LOCK:
        fails[0] = 0
        with open(dp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


kws = [l.strip() for l in open("/data/tmp/nd.csv", encoding="utf-8") if l.strip()][1:]
lt = open("/data/tmp/leaf.txt", encoding="utf-8").read().split("\n#\n")
leaves, ids = lt[0].split("\n"), [int(x) for x in lt[1].split()]
assert len(ids) == len(kws), (len(ids), len(kws))
items = [(i, kws[i], leaves[ids[i]]) for i in range(len(kws))]
import os
if os.environ.get("SHARD"):                         # SHARD=k/n: this process takes keywords i % n == k (parallel runs)
    k_, n_ = map(int, os.environ["SHARD"].split("/"))
    items = [it for it in items if it[0] % n_ == k_ and it[0] not in done]
print("START", len(items), "done", len(done), "hosts", len(hosts_cache), flush=True)
with ThreadPoolExecutor(W) as ex:
    for n, _ in enumerate(ex.map(one, items), 1):
        if n % 250 == 0:
            print("DISC", n, len(items), flush=True)
if fails[0] >= 25:
    print("STOP serper failing (credit?)", flush=True)
    sys.exit(3)
print("DONE", len(items), flush=True)
