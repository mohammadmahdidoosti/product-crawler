"""Add the shop's IDs of the bulk-made categories to /data/wp_categories.csv (rows already there are kept).
Safe to run again: an old-code lane that rewrote the file from its own copy is put right again."""
import sys, shutil, time
sys.path.insert(0, "/app")
import pandas as pd
from crawler.publish import _ProcLock
with _ProcLock("categories"):
    from pathlib import Path
    from crawler.io import read_csv
    w = read_csv(Path("/data/wp_categories.csv"))
    b = pd.read_csv("/data/tmp/bulkcats_out.csv", dtype=str).fillna("")
    add = b[~b["ID"].isin(set(w["ID"]))]
    if len(add):
        shutil.copy("/data/wp_categories.csv", f"/data/wp_categories.csv.bak-bulk-{time.strftime('%H%M%S')}")
        from pathlib import Path
        from crawler.io import write_csv
        write_csv(pd.concat([w, add], ignore_index=True), Path("/data/wp_categories.csv"))
    print("MERGE added", len(add), "total", len(w) + len(add), flush=True)
