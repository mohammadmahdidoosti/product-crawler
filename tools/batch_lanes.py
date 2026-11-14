"""Batch lanes: several processes take batches from one shared queue and do, per batch, photos + texts ->
prepare -> send to the site as drafts. (14 Nov, owner) What the 11-13 Nov one-off lane scripts learned, in one
place:

* a shared queue (a file per batch moved between todo/doing/done/failed): no batch is done twice and a lane
  that is free takes the next one, whatever its size;
* prepare and send run in this process, not through the app's HTTP API: a busy app answered late, the request
  timed out, the lane's sender thread died and three batches were never sent, with nothing on the screen;
* every step is tried again (3 times) and a batch that still fails goes to failed/ with the reason, loudly;
* at most --slots lanes write to the shop at once (a lock file per slot), not one at a time;
* a batch already crawled starts after the crawl, and its top-up crawl is not run again (it read hundreds of
  pages and found nothing);
* the cost printed is what THIS run of the batch cost, not the batch's lifetime total.

Usage (in the container, from /app):
    python tools/batch_lanes.py fill NAME 101 102 103 ...     # queue the batches
    python tools/batch_lanes.py lane NAME 1 --slots 3          # one lane; start several (1, 2, 3 ...)
    python tools/batch_lanes.py status NAME
A file STOP in the queue directory stops every lane after its current batch.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TRIES = 3


def qdir(cfg: dict, name: str) -> Path:
    d = Path(cfg["_data_dir"]) / "lanes" / name
    for sub in ("todo", "doing", "done", "failed"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def say(lane: str, *a) -> None:
    print(time.strftime("%H:%M"), f"LANE{lane}", *a, flush=True)


def claim(q: Path) -> str | None:
    """The next batch, moved to doing/ (a rename is atomic: two lanes never get the same one)."""
    for f in sorted((q / "todo").iterdir(), key=lambda p: (len(p.name), p.name)):
        try:
            os.rename(f, q / "doing" / f.name)
            return f.name
        except OSError:
            continue
    return None


def finish(q: Path, name: str, where: str, note: str = "") -> None:
    src = q / "doing" / name
    dst = q / where / name
    try:
        os.rename(src, dst)
    except OSError:
        dst.touch()
    if note:
        dst.write_text(note, encoding="utf-8")


def retry(lane: str, what: str, fn):
    last = None
    for k in range(1, TRIES + 1):
        try:
            return fn()
        except Exception as e:                          # noqa: BLE001 - tried again, then reported
            last = e
            if "OpenRouter" in str(e) or "اعتبار" in str(e):
                raise                                   # no credit: trying again only fails again
            say(lane, what, f"try {k} failed:", str(e)[:200])
            time.sleep(30 * k)
    raise RuntimeError(f"{what}: {last}")


class Slot:
    """One of n lock files: at most n lanes write to the shop at the same time."""

    def __init__(self, q: Path, n: int):
        self.q, self.n, self.f = q, max(1, n), None

    def __enter__(self):
        while True:
            for i in range(self.n):
                f = open(self.q / f"sendslot-{i}", "w")
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.f = f
                    return self
                except OSError:
                    f.close()
            time.sleep(15)

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()
        return False


def route_batch(app, run: str) -> None:
    """(14 Nov) A batch of the keyword taxonomy has no queue yet: the market check (one search per group), the
    route as the check decides (the owner's review step counts every row as right, as for batches 51-100), then
    the queue. A batch with a queue is left as it is."""
    from crawler import routing_state as RS
    from crawler.config import run_dir
    from crawler.io import FILES
    cfg = app.build_cfg()
    if (run_dir(cfg, run) / FILES["queue"]).exists() or not run.startswith("batch-"):
        return
    n = int(run.split("-")[1])
    with _routing_lock():
        RS.probe_batch(cfg, n)
        RS.review_batch(cfg, n, [])
        RS.make_queue(cfg, n, run)


def _routing_lock():
    from crawler.publish import _ProcLock
    return _ProcLock("routing")


def site_categories(app, run: str) -> None:
    """(14 Nov) The new keyword categories are made on the site only when a product goes there."""
    from crawler.catmake import ensure_wp
    from crawler.config import run_dir
    from crawler.io import read_csv
    d = run_dir(app.build_cfg(), run)
    p = d / "05_new_products.csv"
    if not p.exists():
        return
    df = read_csv(p)
    ids = list(df.get("taxonomy_id", []))
    for v in df.get("extra_taxonomy_ids", []):
        ids += [x.strip() for x in str(v or "").split("|") if x.strip()]
    ensure_wp(app.build_cfg(), ids)


def images_and_texts(app, lane: str, run: str) -> None:
    """The pipeline from the first step not yet done (a crawled batch is not crawled again)."""
    from crawler.config import run_dir
    d = run_dir(app.build_cfg(), run)
    opts = {"keep_queue": True}
    if (d / "03_crawled.csv").exists():
        opts["resume_from"] = "filter"
    steps = list(app.STEPS)
    if (d / "02_topup_done.csv").exists() and "topup" in app.STEPS:
        app.STEPS.remove("topup")                       # this process only
    try:
        job = app.Job(f"lane{lane}")
        job._run("all", run, opts)
        if not job.state.get("ok"):
            raise RuntimeError(job.state.get("error") or "images/texts failed")
    finally:
        app.STEPS[:] = steps


def run_lane(name: str, lane: str, slots: int, max_usd: float, skip_prices: bool) -> None:
    import app
    from crawler.config import run_dir
    from crawler.costs import summary
    from crawler.publish import prepare, publish
    if skip_prices and "prices" in app.STEPS:
        app.STEPS.remove("prices")
    cfg = app.build_cfg()
    q = qdir(cfg, name)
    while not (q / "STOP").exists():
        item = claim(q)
        if item is None:
            break
        run = item if not item.isdigit() else f"batch-{item}"
        since = time.strftime("%Y-%m-%d %H:%M")
        t0 = time.time()
        try:
            say(lane, run, "route")
            retry(lane, f"{run} route", lambda: route_batch(app, run))
            say(lane, run, "photos+texts")
            retry(lane, f"{run} photos+texts", lambda: images_and_texts(app, lane, run))
            say(lane, run, "prepare")
            retry(lane, f"{run} prepare", lambda: prepare(app.build_cfg(), run))
            usd = summary(run_dir(cfg, run), since=since).get("usd", 0.0)
            if max_usd and usd > max_usd:
                raise RuntimeError(f"cost ${usd:.2f} over the ${max_usd:.2f} limit: not sent, look at it")
            retry(lane, f"{run} site categories", lambda: site_categories(app, run))
            with Slot(q, slots):
                say(lane, run, "send")
                res = retry(lane, f"{run} send", lambda: publish(app.build_cfg(), run, limit=3000, keep_draft=True))
            usd = summary(run_dir(cfg, run), since=since).get("usd", 0.0)
            note = f"{res.get('counts')} usd={usd:.3f} min={(time.time() - t0) / 60:.0f}"
            say(lane, run, "SENT", note)
            finish(q, item, "done", note)
        except Exception as e:                          # noqa: BLE001 - one batch never stops the lane
            note = f"{type(e).__name__}: {e}"[:500]
            say(lane, run, "FAILED", note)
            traceback.print_exc()
            finish(q, item, "failed", note)
            if "OpenRouter" in note or "اعتبار" in note:
                (q / "STOP").write_text("no credit", encoding="utf-8")
    say(lane, "ALLDONE")


def status(cfg: dict, name: str) -> None:
    q = qdir(cfg, name)
    for sub in ("todo", "doing", "done", "failed"):
        files = sorted(p.name for p in (q / sub).iterdir())
        print(sub, len(files), " ".join(files)[:300])
    for p in sorted((q / "failed").iterdir()):
        print("  failed", p.name, p.read_text(encoding="utf-8")[:200])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fill", "lane", "status"])
    ap.add_argument("name")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--slots", type=int, default=3, help="lanes that may write to the shop at once")
    ap.add_argument("--max-usd", type=float, default=2.0, help="a batch costing more is not sent")
    ap.add_argument("--with-prices", action="store_true", help="also run the price detective step")
    a = ap.parse_args()
    import app
    cfg = app.build_cfg()
    if a.cmd == "fill":
        q = qdir(cfg, a.name)
        for b in a.args:
            (q / "todo" / str(b)).touch()
        status(cfg, a.name)
    elif a.cmd == "status":
        status(cfg, a.name)
    else:
        run_lane(a.name, a.args[0] if a.args else "1", a.slots, a.max_usd, not a.with_prices)


if __name__ == "__main__":
    main()
