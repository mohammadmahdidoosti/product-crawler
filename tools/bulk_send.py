"""(15 Nov) Send prepared batches to the shop through the SSH bridge (crawler/wcbridge.py), one batch after
the other: site categories, then the products as drafts - the same code as a lane's send, only the way in
is different (WC_BRIDGE must be set). A batch that went through moves from the queue's ready/ to done/.

    WC_BRIDGE=unix:/data/tmp/bridge.sock python tools/bulk_send.py kw2 --workers 2              # every ready batch
    WC_BRIDGE=unix:/data/tmp/bridge.sock python tools/bulk_send.py kw2 --run batch-111 --limit 10 # a test, no move

A file STOP in /data/lanes/<queue>/ stops after the current batch.

(24 Nov) Several senders may run side by side (--workers split between them): a sender first moves the batch
from ready/ to sending/ (a rename: only one sender gets it), and it goes to done/ when sent or back to ready/
when it failed. A sender that is killed leaves its batch in sending/: move it back to ready/ by hand.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def say(*a) -> None:
    print(time.strftime("%m-%d %H:%M"), "BULK", *a, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("queue")
    ap.add_argument("--run", help="one batch only (batch-N); it stays in ready/")
    ap.add_argument("--limit", type=int, default=3000, help="products per batch (a test: 10)")
    ap.add_argument("--workers", type=int, default=2, help="products at the same time (WordPress processes)")
    ap.add_argument("--max", type=int, default=0, help="stop after this many batches")
    a = ap.parse_args()
    if not os.environ.get("WC_BRIDGE"):
        raise SystemExit("WC_BRIDGE is not set: this tool only sends through the SSH bridge")
    import app
    from crawler.config import run_dir
    from crawler.publish import publish
    from batch_lanes import qdir, site_categories
    cfg = app.build_cfg()
    q = qdir(cfg, a.queue)
    runs = [a.run] if a.run else [f"batch-{p.name}" if p.name.isdigit() else p.name
                                  for p in sorted((q / "ready").iterdir(), key=lambda p: (len(p.name), p.name))]
    sending = q / "sending"
    sending.mkdir(exist_ok=True)
    n = 0
    for run in runs:
        if (q / "STOP").exists():
            say("STOP file: stopped"); break
        if a.max and n >= a.max:
            break
        item = run.replace("batch-", "")
        whole = not a.run and a.limit >= 3000
        if whole:
            try:
                os.rename(q / "ready" / item, sending / item)     # only one sender gets the batch
            except OSError:
                continue                                        # another sender has it, or it is gone
        t0 = time.time()
        if not (run_dir(cfg, run) / "prepared.ok").exists():
            say(run, "not prepared: skipped")
            if whole:
                os.rename(sending / item, q / "ready" / item)
            continue
        try:
            say(run, "site categories")
            site_categories(app, run)
            say(run, "send", f"limit={a.limit} workers={a.workers}")
            res = publish(app.build_cfg(), run, limit=a.limit, keep_draft=True, workers=a.workers)
        except Exception as e:                          # noqa: BLE001 - the next batch still goes
            say(run, "FAILED", f"{type(e).__name__}: {e}"[:400])
            if whole:
                os.rename(sending / item, q / "ready" / item)
            continue
        n += 1
        note = f"{res.get('counts')} min={(time.time() - t0) / 60:.0f} (bridge)"
        say(run, "SENT", note)
        if whole:
            os.rename(sending / item, q / "done" / item)
            (q / "done" / item).write_text(note, encoding="utf-8")
    say("ALLDONE", n, "batch(es)")


if __name__ == "__main__":
    main()
