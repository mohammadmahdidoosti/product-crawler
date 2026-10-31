"""Night crawls without a person (31 Oct): batches of the routing table run one after another, CRAWL ONLY.

The owner is not at the chat at night and his computer may be off, so the server walks the list itself:
for each batch, the market probe (when rows still wait for it), the routing accepted as proposed (the
person's review of batches 8-10 corrected 0-1 row in 100), the queue and the crawl. Publishing - the paid
photo fixes, the model descriptions and the site itself - is never done here: it waits for the morning
and the owner's yes. A batch whose crawl costs more than the cap, or that fails, stops the list.

State: DATA_DIR/autopilot.json {"enabled", "batches": [n...], "max_usd": 1.0, "current": {...},
"done": [...], "stopped": ""}. tick() is called once a minute by the server.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)
DEFAULTS = {"enabled": False, "batches": [], "max_usd": 1.0, "current": None, "done": [], "stopped": ""}


def _path(cfg: dict) -> Path:
    return Path(cfg["_data_dir"]) / "autopilot.json"


def load(cfg: dict) -> dict:
    try:
        st = json.loads(_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    return {**DEFAULTS, **st}


def save(cfg: dict, st: dict) -> None:
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def configure(cfg: dict, body: dict) -> dict:
    """{"batches": [11, 12], "enabled": true, "max_usd": 1.0}; batches already done or running are kept out."""
    st = load(cfg)
    if "batches" in body:
        done = {int(d["batch"]) for d in st["done"]}
        cur = (st.get("current") or {}).get("batch")
        st["batches"] = [int(b) for b in body["batches"] if int(b) not in done and int(b) != cur]
    if "max_usd" in body:
        st["max_usd"] = float(body["max_usd"])
    if "enabled" in body:
        st["enabled"] = bool(body["enabled"])
        if st["enabled"]:
            st["stopped"] = ""
    save(cfg, st)
    return st


def _stop(cfg: dict, st: dict, why: str) -> None:
    st["enabled"] = False
    st["stopped"] = why
    log.warning("autopilot stopped: %s", why)
    save(cfg, st)


def _crawl_cost(cfg: dict, run: str) -> float:
    from .config import run_dir
    from .costs import summary
    try:
        return float(summary(run_dir(cfg, run)).get("usd") or 0)
    except Exception:                                    # noqa: BLE001 - no cost file yet
        return 0.0


def _start_crawl(cfg: dict, st: dict, n: int, start_job) -> None:
    from . import routing_state as RS
    b = next((x for x in RS.summary(cfg)["batches"] if x["batch"] == n), None)
    if b and not b["reviewed"]:
        RS.review_batch(cfg, n, [])                      # the proposals as they are
    run = f"batch-{n}"
    RS.make_queue(cfg, n, run)
    err = start_job("all", run, {"keep_queue": True})
    if err:
        _stop(cfg, st, f"batch {n}: crawl not started: {err}")
        return
    st["current"] = {"batch": n, "phase": "crawl", "started": time.time()}
    save(cfg, st)
    log.info("autopilot: batch %d crawl started", n)


def tick(cfg: dict, jobs: dict, start_job) -> None:
    st = load(cfg)
    if not st["enabled"]:
        return
    crawl = jobs["crawl"].state
    if crawl.get("running"):
        return
    cur = st.get("current")
    if cur:
        n, phase = int(cur["batch"]), cur["phase"]
        finished = float(crawl.get("ended") or 0) >= float(cur["started"])
        if phase == "probe":
            if not (crawl.get("run") == "routing" and finished):
                return                                    # not ours yet (a person ran something else)
            if crawl.get("ok") is False:
                return _stop(cfg, st, f"batch {n}: market probe failed: {crawl.get('error', '')[:200]}")
            return _start_crawl(cfg, st, n, start_job)
        if phase == "crawl":
            if not (crawl.get("run") == f"batch-{n}" and finished):
                return
            if crawl.get("ok") is False:
                return _stop(cfg, st, f"batch {n}: crawl failed: {crawl.get('error', '')[:200]}")
            usd = _crawl_cost(cfg, f"batch-{n}")
            st["done"].append({"batch": n, "usd": round(usd, 4), "ended": time.time()})
            st["current"] = None
            st["batches"] = [b for b in st["batches"] if int(b) != n]
            save(cfg, st)
            log.info("autopilot: batch %d crawled ($%.3f)", n, usd)
            if usd > float(st["max_usd"]):
                return _stop(cfg, st, f"batch {n} cost ${usd:.2f}, over the ${float(st['max_usd']):.2f} cap")
            return
    if not st["batches"]:
        st["enabled"] = False
        st["stopped"] = "all batches crawled"
        save(cfg, st)
        return
    if jobs["site"].state.get("running"):
        return                                            # a publish is running: crawl after it
    n = int(st["batches"][0])
    from . import routing_state as RS
    b = next((x for x in RS.summary(cfg)["batches"] if x["batch"] == n), None)
    if b is None:
        return _stop(cfg, st, f"batch {n} is not in the routing table")
    if b["queued"]:
        st["batches"] = st["batches"][1:]
        st["done"].append({"batch": n, "usd": 0, "ended": time.time(), "note": "already crawled"})
        save(cfg, st)
        return
    if b["open"]:
        err = start_job("probe", "routing", {"batch": n})
        if err:
            return _stop(cfg, st, f"batch {n}: market probe not started: {err}")
        st["current"] = {"batch": n, "phase": "probe", "started": time.time()}
        save(cfg, st)
        log.info("autopilot: batch %d market probe started", n)
        return
    _start_crawl(cfg, st, n, start_job)


def status(cfg: dict, jobs: dict) -> dict:
    """What the public status page shows: no secrets, no logs, no names."""
    st = load(cfg)
    lanes = {k: {"running": bool(j.state.get("running")), "step": j.state.get("step", ""),
                 "run": j.state.get("run", ""), "ok": j.state.get("ok"),
                 "error": bool(j.state.get("error")), "started": j.state.get("started", 0),
                 "ended": j.state.get("ended", 0),
                 "progress": {k2: j.prog.get(k2) for k2 in ("step", "done", "total")} if hasattr(j, "prog") else {}}
             for k, j in jobs.items()}
    return {"time": time.time(), "lanes": lanes,
            "autopilot": {"enabled": st["enabled"], "batches": st["batches"], "current": st["current"],
                          "done": st["done"][-10:], "stopped": st["stopped"], "max_usd": st["max_usd"]}}
