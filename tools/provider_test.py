"""(17 Nov) The same products described through several OpenRouter endpoints of one model: real cost, speed,
accepted share and the texts side by side, to pick a cheaper endpoint without losing Persian quality.
Run in the app container: python tools/provider_test.py [run] [n]
Out: /data/runs/provider-test/compare.csv (texts) and summary.csv (cost per endpoint)."""
import sys, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/app")
import pandas as pd
import app
from crawler.config import run_dir
from crawler.io import FILES, read_csv
from crawler.describe import Writer

M = "deepseek/deepseek-v4.1-flash"
MODELS = [M, M + "@baidu/fp8", M + "@alibaba", M + "@novita/fp8", M + "@deepinfra/fp8", M + "@river"]
# (18 Nov) 'cap': the default model with describe.routing = a price cap on output and two endpoints left out
CAP = {"max_price": {"completion": 0.5}, "ignore": ["novita", "baidu"]}
if len(sys.argv) > 3:
    MODELS = sys.argv[3].split(",")
cfg = app.build_cfg()
n = int(sys.argv[2]) if len(sys.argv) > 2 else 20
runs = sorted((p for p in Path("/data/runs").iterdir() if (p / FILES["new"]).exists()), key=lambda p: p.stat().st_mtime)
run = sys.argv[1] if len(sys.argv) > 1 else runs[-1].name
new = read_csv(run_dir(cfg, run) / FILES["new"])
has = new[new["description"].fillna("") != ""]
sample = (has if len(has) >= n else new).sample(min(n, len(new)), random_state=7).to_dict("records")
print("RUN", run, "sample", len(sample), flush=True)
rows, summ = [], []
for m in MODELS:
    if m == "cap":
        c2 = {**cfg, "describe": {**cfg.get("describe", {}), "routing": CAP}}
        w = Writer(c2)
    else:
        w = Writer(cfg, m)
    t0 = time.time()
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(w.write, sample))
    secs = time.time() - t0
    ok = sum(1 for x in res if x)
    for r, x in zip(sample, res):
        rows.append({"endpoint": m.split("@")[1] if "@" in m else ("cap" if m == "cap" else "default"), "name": r.get("name_fa") or r.get("name"),
                     "url": r["url"], "ok": bool(x), "short": (x or {}).get("short", ""), "html": (x or {}).get("html", "")})
    u = w.usage
    summ.append({"endpoint": m.split("@")[1] if "@" in m else ("cap" if m == "cap" else "default"),
                 "served_by": " ".join(f"{k}:{v}" for k, v in w.served.most_common()), "ok": ok, "of": len(sample),
                 "calls": u["calls"], "failed": u["failed"], "tokens_in": u["in"], "tokens_out": u["out"],
                 "usd": round(u["usd"], 5), "usd_per_ok": round(u["usd"] / ok, 6) if ok else "", "seconds": round(secs)})
    print("EP", summ[-1], flush=True)
O = Path("/data/runs/provider-test"); O.mkdir(parents=True, exist_ok=True)
tag = sys.argv[4] if len(sys.argv) > 4 else ""
pd.DataFrame(rows).to_csv(O / f"compare{tag}.csv", index=False, encoding="utf-8-sig")
pd.DataFrame(summ).to_csv(O / f"summary{tag}.csv", index=False, encoding="utf-8-sig")
print("DONE", flush=True)
