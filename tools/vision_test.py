"""(18 Nov) The photo check through other OpenRouter endpoints, on photos the current setup already judged:
n photos it flagged (overlay text or another shop's logo) and n it passed, each checked again by every
model/endpoint given. Agreement with the earlier verdict, errors and real cost per endpoint.
Run in the app container: python tools/vision_test.py [n] [model@endpoint,...]
Out: /data/runs/vision-test/compare.csv and summary.csv."""
import os, sys, glob, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/app")
import pandas as pd
import app
from crawler.config import secret
from crawler.vision import check

M = "deepseek/deepseek-v4.1-flash"
n = int(sys.argv[1]) if len(sys.argv) > 1 else 30
models = sys.argv[2].split(",") if len(sys.argv) > 2 else [M, M + "@deepinfra/fp8"]
cfg = app.build_cfg()
ic = cfg.get("images", {})
base = (ic.get("public_base") or os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
key = secret("OPENROUTER_API_KEY")
frames = []
for p in sorted(glob.glob("/data/runs/*/05_vision.csv"), key=os.path.getmtime)[-25:]:
    try:
        v = pd.read_csv(p, dtype=str).fillna("")
    except Exception:
        continue
    v = v[(v.get("error", "") == "") & (v["file"] != "")]
    frames.append(v)
v = pd.concat(frames).drop_duplicates("file")
bad = v[(v.overlay_text == "True") | (v.shop_logo == "True")]
good = v[(v.overlay_text == "False") & (v.shop_logo == "False")]
jobs = pd.concat([bad.sample(min(n, len(bad)), random_state=3).assign(was="bad"),
                  good.sample(min(n, len(good)), random_state=3).assign(was="ok")]).to_dict("records")
print("PHOTOS", len(jobs), "base", base, flush=True)
rows, summ = [], []
for m in models:
    t0 = time.time()
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(lambda j: check(m, f"{base}/img/{j['file']}", j.get("url", ""), key), jobs))
    agree = errors = cost = 0
    tp = fp = fn = 0
    for j, x in zip(jobs, res):
        isbad = x.get("overlay_text") is True or x.get("shop_logo") is True
        if x.get("_error"):
            errors += 1
        else:
            agree += int(isbad == (j["was"] == "bad"))
            tp += int(isbad and j["was"] == "bad"); fn += int(not isbad and j["was"] == "bad")
            fp += int(isbad and j["was"] == "ok")
        try: cost += float(x.get("_cost") or 0)
        except ValueError: pass
        rows.append({"model": m, "file": j["file"], "was": j["was"], "now_bad": isbad,
                     "overlay_text": x.get("overlay_text"), "shop_logo": x.get("shop_logo"),
                     "note": x.get("note", ""), "error": x.get("_error", "")})
    summ.append({"model": m, "photos": len(jobs), "errors": errors, "agree": agree, "bad_found": tp,
                 "bad_missed": fn, "ok_called_bad": fp, "usd": round(cost, 5),
                 "usd_per_photo": round(cost / max(1, len(jobs) - errors), 6), "seconds": round(time.time() - t0)})
    print("VM", summ[-1], flush=True)
O = Path("/data/runs/vision-test"); O.mkdir(parents=True, exist_ok=True)
pd.DataFrame(rows).to_csv(O / "compare.csv", index=False, encoding="utf-8-sig")
pd.DataFrame(summ).to_csv(O / "summary.csv", index=False, encoding="utf-8-sig")
print("DONE", flush=True)
