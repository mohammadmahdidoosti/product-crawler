"""Language pilot: Google competition check per language via Serper (top-10 organic + shopping block).
Run in the app container: python tools/lang/lang_serp.py  -> /data/runs/lang-test/serp.csv"""
import csv, json, sys, time
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/app")
import app
from crawler.config import secret
from crawler.pipeline import _fetchers

GL = {"_fa": ("ir", "fa"), "pl": ("pl", "pl"), "ro": ("ro", "ro"), "cs": ("cz", "cs"), "hu": ("hu", "hu"),
      "el": ("gr", "el"), "bg": ("bg", "bg"), "uk": ("ua", "uk"), "tr": ("tr", "tr"), "ar": ("sa", "ar"),
      "ru": ("ru", "ru"), "pt": ("br", "pt-br"), "es": ("mx", "es-419"), "id": ("id", "id"),
      "vi": ("vn", "vi"), "uz": ("uz", "uz")}
PICK = [0, 1, 4, 5, 6, 8, 9, 11, 12, 15, 18, 21, 23, 27, 29, 32, 37, 40, 48, 52]   # 20 of the 60 terms

terms = json.load(open(Path(__file__).with_name("terms.json"), encoding="utf-8"))
key = secret("SERPER_API_KEY")
f = _fetchers(app.build_cfg())["iran"]
out = Path("/data/runs/lang-test"); out.mkdir(parents=True, exist_ok=True)


def one(job):
    lang, i = job
    gl, hl = GL[lang]
    q = terms[lang][i]
    for _ in range(3):
        res = f.post_json("https://google.serper.dev/search", {"q": q, "gl": gl, "hl": hl, "num": 10},
                          {"X-API-KEY": key, "Content-Type": "application/json"})
        j = (res or {}).get("json") or {}
        if j.get("organic") is not None:
            break
        time.sleep(3)
    org = j.get("organic") or []
    hosts = [urlparse(o.get("link", "")).netloc.lower().removeprefix("www.") for o in org]
    return {"lang": lang, "idx": i, "en": terms["_en"][i], "q": q, "n": len(org), "hosts": "|".join(hosts),
            "shopping": len(j.get("shopping") or []), "related": " | ".join(r.get("query", "") for r in (j.get("relatedSearches") or [])[:5]),
            "titles": " || ".join(o.get("title", "") for o in org[:5])}


jobs = [(l, i) for l in GL for i in PICK]
with ThreadPoolExecutor(6) as ex:
    rows = list(ex.map(one, jobs))
with open(out / "serp.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
    w.writeheader(); w.writerows(rows)
print("DONE", len(rows), "empty", sum(1 for r in rows if not r["n"]))
