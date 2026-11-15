"""(15 Nov) Make the spec-table rows of prepared batches ahead, on the crawler, while bulk_send.py sends the
batches before them: the send then finds every list in specs_cache.json and goes straight to the products
(the turning took 2-3 minutes at the start of each batch). Same texts and same cache as publish(), so the
cost is what the send would have spent anyway; it is written to each batch's cost log.

    python tools/spec_prefetch.py kw2 --skip 114 115      # every ready batch except the ones being sent
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("queue")
    ap.add_argument("--skip", nargs="*", default=[], help="batch numbers to leave (being sent now)")
    a = ap.parse_args()
    import app
    from crawler import specs as SP
    from crawler.config import run_dir, secret
    from crawler.costs import add as add_cost
    from crawler.pipeline import export_rows
    from batch_lanes import qdir
    cfg = app.build_cfg()
    q = qdir(cfg, a.queue)
    names = sorted((p.name for p in (q / "ready").iterdir()), key=lambda n: (len(n), n))
    total = 0.0
    for n in names:
        if n in a.skip:
            continue
        run = f"batch-{n}" if n.isdigit() else n
        t0 = time.time()
        items = export_rows(cfg, run)
        _, usd, calls = SP.prefetch(cfg["_data_dir"], [(r["Name"], r.get("Description") or "") for _, r in items],
                                    secret("OPENROUTER_API_KEY"))
        if calls:
            add_cost(run_dir(cfg, run), "spec_table", calls, usd, f"feature lists turned into spec rows: {calls}")
        total += usd
        print(time.strftime("%H:%M"), run, f"lists={calls} usd={usd:.3f} s={time.time() - t0:.0f}", flush=True)
    print("DONE usd", round(total, 3), flush=True)


if __name__ == "__main__":
    main()
