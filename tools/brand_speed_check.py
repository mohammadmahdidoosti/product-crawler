"""(10 Nov) The faster brands_fa (remembered keys, plain test before the regex, one site index) against the
running one: same answers, and the time each takes. Inside the app:
python tools/brand_speed_check.py /path/to/new/brands_fa.py batch-38"""
import importlib.util
import sys
import time

import pandas as pd

import app  # noqa: F401 - the app's config and data dir
from crawler import brands_fa as OLD
from crawler.config import run_dir

spec = importlib.util.spec_from_file_location("crawler.brands_fa_new", sys.argv[1])
NEW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(NEW)

cfg = app.build_cfg()
t = OLD.table(cfg)
names = []
for run in sys.argv[2:]:
    p = run_dir(cfg, run) / "06_woo_import.csv"
    if p.exists():
        names += list(pd.read_csv(p, dtype=str).fillna("")["Name"])
names = names[:150]
print("brands", len(t), "names", len(names), flush=True)
t0 = time.time()
b = [NEW.from_name(n, t) for n in names]
t1 = time.time()
print(f"new from_name: {t1 - t0:.1f}s", flush=True)
a = [OLD.from_name(n, t) for n in names]
t2 = time.time()
print(f"old from_name: {t2 - t1:.1f}s; found {sum(1 for x in a if x)}; differences {sum(1 for x, y in zip(a, b) if x != y)}")
