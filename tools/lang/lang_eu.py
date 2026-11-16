"""Europe language map: Serper top-10 for all 60 terms + Google Suggest (depth 1 + top-3 depth 2) per language.
Run in the app container: python tools/lang/lang_eu.py [serp|sug|sum]  ->  /data/runs/lang-eu/*.csv"""
import csv, json, re, sys, time, random, threading, urllib.parse, urllib.request
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/app")

HERE = Path(__file__).parent
T = json.load(open(HERE / "terms.json", encoding="utf-8"))
T.update(json.load(open(HERE / "terms_eu.json", encoding="utf-8")))
GL = {"de": ("de", "de"), "fr": ("fr", "fr"), "it": ("it", "it"), "es_es": ("es", "es"), "pt_pt": ("pt", "pt-PT"),
      "nl": ("nl", "nl"), "sv": ("se", "sv"), "en_gb": ("uk", "en"), "da": ("dk", "da"), "no": ("no", "no"),
      "fi": ("fi", "fi"), "sk": ("sk", "sk"), "sq": ("al", "sq"), "hr": ("hr", "hr"), "sr": ("rs", "sr"),
      "bs": ("ba", "bs"), "sl": ("si", "sl"), "mk": ("mk", "mk"), "lt": ("lt", "lt"), "lv": ("lv", "lv"),
      "et": ("ee", "et"), "ca": ("es", "ca"),
      "pl": ("pl", "pl"), "ro": ("ro", "ro"), "cs": ("cz", "cs"), "hu": ("hu", "hu"), "el": ("gr", "el"),
      "bg": ("bg", "bg"), "uk": ("ua", "uk"), "tr": ("tr", "tr"), "ru": ("ru", "ru")}
NEW = [l for l in GL if l in json.load(open(HERE / "terms_eu.json", encoding="utf-8"))]
CAT = {"ابزار برقی": [0, 1, 2, 3, 51, 52, 55, 56], "جوش و برش": [4, 45, 50, 54], "پمپ و کمپرسور": [5, 9, 10],
       "بالابر و حمل": [6, 7, 8], "برق صنعتی": [11, 12, 13, 14, 37, 38, 39, 59], "اندازه‌گیری": [15, 16, 17, 18, 19, 20, 40],
       "ابزار دستی": [21, 22, 46, 58], "انبار و کارگاه": [41, 42, 57], "ایمنی": [23, 24, 25, 26],
       "انتقال قدرت": [27, 28, 29, 35, 36], "هیدرولیک و پنوماتیک": [30, 31, 32, 33, 34, 53],
       "مصرفی و اتصالات": [43, 44, 47, 48, 49]}
MARKET = re.compile(r"amazon|ebay|allegro|olx|kaufland|otto\.|idealo|manomano|leroymerlin|cdiscount|bol\.com|coolblue|"
                    r"mediamarkt|obi\.|hornbach|bauhaus|toom\.|castorama|bricodepot|bricomart|praktiker|emag|alza|heureka|ceneo|"
                    r"skroutz|bestprice|temu|aliexpress|alibaba|toolstation|screwfix|wickes|diy\.com|argos|jula|biltema|"
                    r"clasohlson|k-rauta|tokmanni|motonet|verkkokauppa|prisma|power\.|elgiganten|komplett|finn\.no|tori\.fi|"
                    r"blocket|dba\.dk|guloggratis|jemogfix|silvan|harald-?nyborg|xxl\.|vseinstrumenti|ozon|wildberries|avito|"
                    r"yandex|rozetka|epicentr|prom\.ua|trendyol|hepsiburada|n11|akakce|cimri|sahibinden|pazaruvaj|technopolis|"
                    r"mall\.|zbozi|arukereso|worten|fnac|ikea|dedeman|bricostore|mr-bricolage|pigu|varle|220\.lv|kaup24|"
                    r"hinnavaatlus|ss\.lv|ss\.com|osta\.ee|merkury|bauhof|k-rauta|ksenukai|depo|senukai|links\.hr|sancta-domenica|"
                    r"njuskalo|kupujemprodajem|olx\.ba|mimovrste|bolha|merkur\.si|pazar3|reptil|neptun|shpresa|kaufmann|"
                    r"amazon|bigbang|mercadolivre|mercadolibre|wish\.|lidl|aldi|coppel|tesco|wayfair|vidaxl|banggood", re.I)
B2B = re.compile(r"wurth|würth|rs-online|rs-components|conrad|hoffmann-group|contorion|mercateo|zoro|manutan|kaiserkraft|"
                 r"raja|cromwell|misumi|mouser|farnell|distrelec|festo|smc\.|grainger|rubix|brammer|hahn-kolb|"
                 r"orbis|boels|skf\.|schaeffler|bosch-professional|makita|dewalt|milwaukee|hilti|metabo", re.I)
INFO = re.compile(r"wikipedia|youtube|facebook|instagram|pinterest|reddit|tiktok|linkedin|wiktionary|quora", re.I)
OUT = Path("/data/runs/lang-eu"); OUT.mkdir(parents=True, exist_ok=True)


def serp():
    import app
    from crawler.config import secret
    from crawler.pipeline import _fetchers
    key = secret("SERPER_API_KEY"); f = _fetchers(app.build_cfg())["iran"]

    def one(job):
        l, i = job; gl, hl = GL[l]; q = T[l][i]; j = {}
        for _ in range(3):
            res = f.post_json("https://google.serper.dev/search", {"q": q, "gl": gl, "hl": hl, "num": 10},
                              {"X-API-KEY": key, "Content-Type": "application/json"})
            j = (res or {}).get("json") or {}
            if j.get("organic") is not None:
                break
            time.sleep(3)
        hosts = [urlparse(o.get("link", "")).netloc.lower().removeprefix("www.") for o in j.get("organic") or []]
        return {"lang": l, "idx": i, "q": q, "hosts": "|".join(hosts)}
    jobs = [(l, i) for l in GL for i in range(60)]
    with ThreadPoolExecutor(6) as ex:
        rows = list(ex.map(one, jobs))
    with open(OUT / "serp_all.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print("SERP DONE", len(rows), "empty", sum(1 for r in rows if not r["hosts"]), flush=True)


LOCK = threading.Lock()


def sug():
    path = OUT / "sug.jsonl"; done = {}
    if path.exists():
        for l in open(path, encoding="utf-8"):
            try: d = json.loads(l); done[(d["lang"], d["q"])] = d
            except Exception: pass
    out = open(path, "a", encoding="utf-8"); state = {"delay": 0.8}

    def fetch(l, q):
        gl, hl = GL[l]
        url = ("https://suggestqueries.google.com/complete/search?client=firefox&ie=utf-8&oe=utf-8&hl="
               + urllib.parse.quote(hl) + "&gl=" + gl + "&q=" + urllib.parse.quote(q))
        for _ in range(6):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/128.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read().decode("utf-8", "replace"))
                state["delay"] = max(0.8, state["delay"] * 0.95); return data[1]
            except Exception as e:
                state["delay"] = min(60, state["delay"] * 2); print("ERR", l, e, flush=True); time.sleep(state["delay"] * 3)
        return None

    def run(l, q, depth, root):
        if (l, q) in done: return done[(l, q)]["s"]
        s = fetch(l, q)
        if s is None: return []
        d = {"lang": l, "q": q, "depth": depth, "root": root, "s": s}
        with LOCK:
            done[(l, q)] = d; out.write(json.dumps(d, ensure_ascii=False) + "\n"); out.flush()
        time.sleep(state["delay"] + random.random() * 0.4); return s

    def job(lq):
        l, q = lq; s = run(l, q, 1, q)
        for c in [x for x in s if x.strip() != q.strip()][:3]:
            run(l, c, 2, q)
    jobs = [(l, T[l][i]) for l in NEW for i in range(60)]
    with ThreadPoolExecutor(8) as ex:
        for n, _ in enumerate(ex.map(job, jobs), 1):
            if n % 100 == 0: print("SUG", n, len(jobs), flush=True)
    print("SUG DONE", len(done), flush=True)


def summary():
    rows = list(csv.DictReader(open(OUT / "serp_all.csv", encoding="utf-8")))
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        i = int(r["idx"])
        cat = next(c for c, ix in CAT.items() if i in ix)
        by[r["lang"]][cat] += [h for h in r["hosts"].split("|") if h]
    with open(OUT / "cats.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(["lang", "cat", "n", "market", "b2b", "info", "free"])
        for l, cs in by.items():
            for c, hs in cs.items():
                n = len(hs) or 1; m = sum(bool(MARKET.search(h)) for h in hs)
                b = sum(bool(B2B.search(h)) and not MARKET.search(h) for h in hs)
                i = sum(bool(INFO.search(h)) and not MARKET.search(h) and not B2B.search(h) for h in hs)
                w.writerow([l, c, len(hs), round(100 * m / n), round(100 * b / n), round(100 * i / n), round(100 * (n - m - b - i) / n)])
    sug = defaultdict(lambda: {"d1": [], "d2": [], "u": set()})
    p = OUT / "sug.jsonl"
    if p.exists():
        for line in open(p, encoding="utf-8"):
            d = json.loads(line); s = sug[d["lang"]]
            (s["d1"] if d["depth"] == 1 else s["d2"]).append(len(d["s"])); s["u"].update(d["s"])
    with open(OUT / "langs.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(["lang", "hosts", "distinct", "top3", "market", "b2b", "info", "free", "d1avg", "d1zero", "d2avg", "uniq"])
        for l, cs in by.items():
            hs = [h for v in cs.values() for h in v]; n = len(hs) or 1
            cnt = defaultdict(int)
            for h in hs: cnt[h] += 1
            top3 = sum(sorted(cnt.values(), reverse=True)[:3])
            m = sum(bool(MARKET.search(h)) for h in hs); b = sum(bool(B2B.search(h)) and not MARKET.search(h) for h in hs)
            i = sum(bool(INFO.search(h)) and not MARKET.search(h) and not B2B.search(h) for h in hs)
            s = sug.get(l)
            w.writerow([l, len(hs), len(cnt), round(100 * top3 / n), round(100 * m / n), round(100 * b / n), round(100 * i / n),
                        round(100 * (n - m - b - i) / n),
                        round(sum(s["d1"]) / len(s["d1"]), 1) if s and s["d1"] else "", sum(x == 0 for x in s["d1"]) if s else "",
                        round(sum(s["d2"]) / len(s["d2"]), 1) if s and s["d2"] else "", len(s["u"]) if s else ""])
    print("SUM DONE", flush=True)


if __name__ == "__main__":
    what = sys.argv[1:] or ["serp", "sug", "sum"]
    if "serp" in what: serp()
    if "sug" in what: sug()
    if "sum" in what: summary()
