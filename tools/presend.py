"""(25 Nov, owner's OK) Get ready batches ready for the senders ahead of time, on the crawler.

Measured on 25 Nov: a sender spent ~26 of its ~54 minutes per batch on the photo work at publishing
(images.defer_check: the check again, the second look, the free fixes, the search) and ~6 on the site
categories - while its share of the shop sat idle. This tool does the same two steps for the batches waiting
in ready/, one batch at a time per worker, and leaves a mark (presend.ok in the run folder). A sender that
finds the mark goes straight to the products (publish(..., photos_done=True)) and takes marked batches first.
What is done is the same code as at publishing, only earlier: the products and photos come out the same.

    WC_BRIDGE=unix:/data/tmp/bridge.sock python tools/presend.py kw100 --worker 1     # a loop; more workers side by side

A batch being worked on sits in presend/<worker>/ (a rename: neither a sender nor another worker can take it)
and goes back to ready/ when done or when it failed. A worker that starts puts its own leftovers back first.
A file STOPPRESEND in /data/lanes/<queue>/ stops a worker after its current batch.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MARK = "presend.ok"


def say(*a) -> None:
    print(time.strftime("%m-%d %H:%M"), "PRESEND", *a, flush=True)


def order(names) -> list:
    return sorted(names, key=lambda n: (len(n), n))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("queue")
    ap.add_argument("--worker", default="1")
    ap.add_argument("--once", action="store_true", help="one batch, then exit (a test)")
    a = ap.parse_args()
    if not os.environ.get("WC_BRIDGE"):
        raise SystemExit("WC_BRIDGE is not set: the site categories go through the SSH bridge")
    import app
    from crawler.config import run_dir
    from crawler.publish import presend
    from batch_lanes import qdir, site_categories
    cfg = app.build_cfg()
    q = qdir(cfg, a.queue)
    mine = q / "presend" / str(a.worker)
    mine.mkdir(parents=True, exist_ok=True)
    for f in mine.iterdir():                               # a worker killed earlier: its batch goes back
        os.rename(f, q / "ready" / f.name)
        say("back to ready/", f.name)
    while not (q / "STOPPRESEND").exists():
        item = None
        for name in order(p.name for p in (q / "ready").iterdir()):
            run = f"batch-{name}" if name.isdigit() else name
            d = run_dir(cfg, run)
            if (d / MARK).exists() or not (d / "prepared.ok").exists():
                continue
            try:
                os.rename(q / "ready" / name, mine / name)     # a sender or another worker may have it
            except OSError:
                continue
            item = name
            break
        if item is None:
            if a.once:
                break
            time.sleep(60)
            continue
        run = f"batch-{item}" if item.isdigit() else item
        t0 = time.time()
        try:
            say(run, "site categories")
            site_categories(app, run)
            say(run, "photos")
            res = presend(app.build_cfg(), run)
            (run_dir(cfg, run) / MARK).write_text(time.strftime("%Y-%m-%d %H:%M"), encoding="utf-8")
            say(run, "DONE", res, f"min={(time.time() - t0) / 60:.0f}")
        except Exception as e:                             # noqa: BLE001 - the sender does it as before
            say(run, "FAILED", f"{type(e).__name__}: {e}"[:400])
            failed = True
        else:
            failed = False
        finally:
            os.rename(mine / item, q / "ready" / item)
        if failed and not a.once:
            time.sleep(300)    # (25 Nov) no credit: 4,400 failures in a minute without this pause
        if a.once:
            break


if __name__ == "__main__":
    main()
