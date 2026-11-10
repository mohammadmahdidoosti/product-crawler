"""(10 Nov) Check that the quick pre-test in pricelist.list_price gives the same answers as the plain loop,
and how long each takes, on the names of real runs. Run inside the app: python tools/lp_speed_check.py batch-36 ..."""
import re
import sys
import time

import pandas as pd

from crawler import pricelist as PL
from crawler.config import run_dir

SEP = re.compile(r"[\s\-_/.]")


def old(cfg, name, sku, idx):
    best, flat = None, PL._flat(name)
    for k, v in idx.items():
        if not PL.contains_model(f"{name} {sku}", k):
            continue
        bn = v.get("brand_names") or []
        if bn and not any(b in flat for b in bn):
            continue
        if PL.weak_key(k) and not (bn or PL.kind_in(name, v.get("name", ""))):
            continue
        if best is None or len(k) > len(best[0]):
            best = (k, v)
    return best[1] if best else None


def new(cfg, name, sku, idx, stripped):
    best, flat = None, PL._flat(name)
    text = SEP.sub("", f"{name} {sku}".translate(PL.FA_DIGITS).upper())
    for k, v in idx.items():
        if stripped[k] not in text or not PL.contains_model(f"{name} {sku}", k):
            continue
        bn = v.get("brand_names") or []
        if bn and not any(b in flat for b in bn):
            continue
        if PL.weak_key(k) and not (bn or PL.kind_in(name, v.get("name", ""))):
            continue
        if best is None or len(k) > len(best[0]):
            best = (k, v)
    return best[1] if best else None


import app  # noqa: E402 - the app's own config (data dir)
cfg = app.build_cfg()
idx = PL.load_index(cfg)
stripped = {k: SEP.sub("", k) for k in idx}
rows = []
for run in sys.argv[1:]:
    if run.startswith("idx"):                      # 'idx300': names of price-list rows themselves (they match)
        ks = list(idx)[:: max(1, len(idx) // int(run[3:] or 300))]
        rows += [(f"{idx[k].get('name', '')} {idx[k].get('brand', '')}", "") for k in ks]
        continue
    p = run_dir(cfg, run) / "06_woo_import.csv"
    if p.exists():
        d = pd.read_csv(p, dtype=str).fillna("")
        rows += list(zip(d["Name"], d["SKU"]))
print("keys", len(idx), "products", len(rows), flush=True)
t0 = time.time()
b = [new(cfg, n, s, idx, stripped) for n, s in rows]
t1 = time.time()
print(f"new: all {len(rows)} in {t1 - t0:.1f}s, {sum(1 for x in b if x)} with a list price", flush=True)
step = max(1, len(rows) // 200)                       # the plain loop is slow: every n-th product, about 200
sample = sorted(set(range(0, len(rows), step)) | {i for i, x in enumerate(b) if x})   # + every match
t0 = time.time()
a = {i: old(cfg, *rows[i], idx) for i in sample}
t1 = time.time()
diff = [rows[i] for i in sample if a[i] != b[i]]
print(f"old: {len(sample)} in {t1 - t0:.1f}s; matches {sum(1 for i in sample if a[i])}; differences {len(diff)} {diff[:3]}")
