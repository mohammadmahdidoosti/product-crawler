#!/usr/bin/env python3
"""(25 Nov, owner's OK) A command mailbox for the crawler host, so Claude can run the daily work without the owner's
browser. Runs on the HOST (not in the container), every minute from cron:

    * * * * * /usr/bin/flock -n /tmp/caravan-ops.lock /usr/bin/python3 /root/ops/ops_agent.py >> /root/ops/agent.log 2>&1

It keeps a clone of a PRIVATE GitHub repository (/root/ops/repo, reached with a deploy key for that repository
only). A command is a file inbox/<id>.cmd holding ONE line, "<action> key=value ...". Only the actions in ACTIONS
run, each a fixed command with checked arguments; anything else is refused and said so. The answer goes to
outbox/<id>.txt, the command moves to done/, and status/latest.txt is written again every 30 minutes and after
each command. Nothing here takes a shell command from the repository.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(os.environ.get("OPS_REPO", "/root/ops/repo"))
DATA = Path("/srv/caravan-data")
QUEUE = "kw100"
Q = DATA / "lanes" / QUEUE
KWLISTS = DATA / "kwlists"
STATUS_EVERY = 30 * 60
OUT_MAX = 12000                                   # characters of output kept per answer


def sh(args: list[str], timeout: int = 600) -> str:
    """One fixed command, no shell; stdout+stderr, cut to OUT_MAX."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode:
            out += f"\n[exit {r.returncode}]"
    except subprocess.TimeoutExpired:
        out = f"[timed out after {timeout} s]"
    return out[-OUT_MAX:]


def git(*a: str, timeout: int = 120) -> str:
    return sh(["git", "-C", str(REPO), *a], timeout)


def container() -> str:
    names = sh(["docker", "ps", "--format", "{{.Names}}"]).split()
    for n in names:
        if "d08ilk70" in n:
            return n
    raise RuntimeError("crawler container not found")


def dexec(*a: str, detach: bool = False, timeout: int = 600) -> str:
    return sh(["docker", "exec", *(["-d"] if detach else []), "-w", "/app", container(), *a], timeout)


def count(sub: str) -> int:
    p = Q / sub
    return len(list(p.iterdir())) if p.exists() else 0


def running(pattern: str) -> int:
    out = sh(["pgrep", "-fc", pattern]).strip().splitlines()
    return int(out[0]) if out and out[0].isdigit() else 0


def tail(path: Path, n: int, grep: str | None = None) -> str:
    if not path.exists():
        return "(none)"
    lines = path.read_text(errors="replace").splitlines()
    if grep:
        lines = [x for x in lines if re.search(grep, x)]
    return "\n".join(x[:160] for x in lines[-n:])


# ---------------------------------------------------------------- actions
def a_status(_: dict) -> str:
    c = container()
    marked = sum(1 for p in (Q / "ready").iterdir()
                 if (DATA / "runs" / (f"batch-{p.name}" if p.name.isdigit() else p.name) / "presend.ok").exists())
    left = sh(["docker", "exec", "-w", "/app", c, "python3", "-c",
               "from crawler.llm import openrouter_left as f; print(round(f() or 0, 2))"], 60).strip()
    parts = [
        f"time UTC {dt.datetime.utcnow():%Y-%m-%d %H:%M}",
        "queue " + "  ".join(f"{s} {count(s)}" for s in ("todo", "doing", "ready", "sending", "done", "failed"))
        + f"  ready_marked {marked}",
        "stop files " + (" ".join(f for f in ("STOP", "STOPSEND", "STOPPRESEND") if (Q / f).exists()) or "none"),
        f"lanes {running('lane_loop.sh ' + QUEUE)}  senders {running('send_loop.sh ' + QUEUE)}"
        f" (old-style {running('kw100/send')})  presend {running('presend_loop.sh ' + QUEUE)}"
        f"  workers send {(Q / 'workers_send').read_text().strip() if (Q / 'workers_send').exists() else '2'}"
        f" send2 {(Q / 'workers_send2').read_text().strip() if (Q / 'workers_send2').exists() else '2'}",
        "crawler load " + Path("/proc/loadavg").read_text().strip(),
        "crawler mem " + sh(["free", "-g"]).splitlines()[1],
        "crawler disk " + sh(["df", "-h", "/"]).splitlines()[-1],
        "shop " + sh(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "shop",
                      "cat /proc/loadavg; df -h / | tail -1; free -m | sed -n 2,3p"], 60).replace("\n", " | "),
        "oom " + (sh(["sh", "-c", "dmesg -T | grep -i 'killed process' | tail -1"]).strip() or "none"),
        f"openrouter left ${left}",
        "drive " + sh(["rclone", "about", "gdrive:"], 90).replace("\n", " ").strip(),
        "backup " + tail(Path("/root/backups/backup.log"), 2),
        "--- sends", tail(Q / "send.log", 4, r"BULK .*(SENT|FAILED|send limit)") + "\n"
        + tail(Q / "send2.log", 4, r"BULK .*(SENT|FAILED|send limit)"),
        "--- presend", tail(Q / "presend1.log", 3, r"DONE|FAILED") + "\n" + tail(Q / "presend2.log", 3, r"DONE|FAILED"),
        "--- failed batches " + " ".join(sorted(p.name for p in (Q / "failed").iterdir())[:40]),
    ]
    return "\n".join(parts)


def a_requeue_failed(_: dict) -> str:
    names = sorted(p.name for p in (Q / "failed").iterdir())
    if not names:
        return "no failed batches"
    keep = Path("/root/fix") / f"failed_{dt.datetime.utcnow():%m%d_%H%M}"
    keep.mkdir(parents=True, exist_ok=True)
    reasons = {n: (Q / "failed" / n).read_text(errors="replace")[:200] for n in names}
    for n in names:
        shutil.move(str(Q / "failed" / n), str(keep / n))
    out = dexec("python3", "tools/batch_lanes.py", "fill", QUEUE, *names)
    return f"requeued {len(names)}: {' '.join(names)}\nreasons (first): {list(reasons.values())[0]}\n{out[-2000:]}"


def a_queue_list(arg: dict) -> str:
    name = arg.get("file", "")
    if re.fullmatch(r"kw_\d{3}_\d{3}\.csv", name) and not (KWLISTS / name).exists() and (REPO / "lists" / name).exists():
        KWLISTS.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "lists" / name, KWLISTS / name)      # (25 Nov) lists come through the private repo
    if not re.fullmatch(r"kw_\d{3}_\d{3}\.csv", name) or not (KWLISTS / name).exists():
        return f"refused: file must be kw_NNN_NNN.csv and exist in {KWLISTS}"
    shutil.copy(KWLISTS / name, DATA / "tmp" / name)
    dry = dexec("python3", "tools/kw_tax_import.py", f"/data/tmp/{name}", "base=150", timeout=1200)
    m = re.search(r"batches (\d+) - (\d+)", dry)
    if not m:
        return "dry run did not show the batches; nothing imported\n" + dry[-3000:]
    lo, hi = int(m.group(1)), int(m.group(2))
    if hi - lo > 150:
        return f"refused: {hi - lo + 1} batches is more than expected\n" + dry[-2000:]
    go = dexec("python3", "tools/kw_tax_import.py", f"/data/tmp/{name}", "base=150", "go", timeout=1200)
    fill = dexec("python3", "tools/batch_lanes.py", "fill", QUEUE, *map(str, range(lo, hi + 1)))
    return f"imported {name}: batches {lo}-{hi}\n{dry[-600:]}\n{go[-400:]}\n{fill[-400:]}"


def a_start_all(_: dict) -> str:
    """Lanes 1-9, two senders, two presend workers: each started only if it is not running."""
    for f in ("STOP", "STOPSEND", "STOPPRESEND"):
        (Q / f).unlink(missing_ok=True)
    done = []
    for i in range(1, 10):
        if not running(f"lane_loop.sh {QUEUE} {i}$"):
            dexec("sh", "/data/lanes/lane_loop.sh", QUEUE, str(i), detach=True)
            done.append(f"lane {i}")
    for name in ("send", "send2"):
        if not running(f"send_loop.sh {QUEUE} {name}$") and not running(f"kw100/{name}.log"):
            dexec("sh", "tools/lanes/send_loop.sh", QUEUE, name, detach=True)
            done.append(name)
    for w in ("1", "2"):
        if not running(f"presend_loop.sh {QUEUE} {w}$") and not running(f"presend{w}.log"):
            dexec("sh", "tools/lanes/presend_loop.sh", QUEUE, w, detach=True)
            done.append(f"presend {w}")
    return "started: " + (", ".join(done) or "nothing (all running)")


def a_stop_all(_: dict) -> str:
    """STOP: lanes and senders end after their current batch; presend after its current batch."""
    for f in ("STOP", "STOPSEND", "STOPPRESEND"):
        (Q / f).write_text("ops " + dt.datetime.utcnow().strftime("%H:%M"))
    return "stop files written; everything ends after its current batch"


def a_workers(arg: dict) -> str:
    who, n = arg.get("sender", ""), arg.get("n", "")
    if who not in ("send", "send2") or n not in ("1", "2", "3"):
        return "refused: sender=send|send2 n=1|2|3"
    (Q / f"workers_{who}").write_text(n)
    return f"{who}: {n} workers from its next batch (needs send_loop.sh senders)"


def a_costs(arg: dict) -> str:
    """Read only: OpenRouter/Serper cost per unit and per step before and after a time (YYYY-MM-DDTHH:MM, UTC as
    the ledger writes it), from every run's 09_costs.csv of this queue's batches."""
    at = arg.get("at", "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", at):
        return "refused: at=YYYY-MM-DDTHH:MM"
    code = ("import pandas as pd,glob\n"
            "fs=glob.glob('/data/runs/batch-*/09_costs.csv')\n"
            "d=pd.concat([pd.read_csv(f,dtype=str).assign(b=f) for f in fs]);d['usd']=d.usd.astype(float);d['units']=d.units.astype(float)\n"
            f"t='{at.replace('T', ' ')}';d=d[d.time>=(pd.Timestamp(t)-pd.Timedelta(days=1)).strftime('%Y-%m-%d %H:%M')]\n"
            "d['when']=(d.time>=t).map({True:'after',False:'before'})\n"
            "g=d.groupby(['step','when']).agg(usd=('usd','sum'),units=('units','sum'),rows=('usd','size'),batches=('b','nunique'))\n"
            "g['usd_per_unit_x1000']=(1000*g.usd/g.units.where(g.units>0)).round(4);print(g.round(3).to_string())\n"
            "print('batches before/after',d[d.when=='before'].b.nunique(),d[d.when=='after'].b.nunique())\n")
    return dexec("python3", "-c", code, timeout=300)


ACTIONS = {"costs": a_costs, "status": a_status, "requeue_failed": a_requeue_failed, "queue_list": a_queue_list,
           "start_all": a_start_all, "stop_all": a_stop_all, "workers": a_workers}


# ---------------------------------------------------------------- mailbox
def parse(line: str) -> tuple[str, dict]:
    words = line.strip().split()
    if not words:
        return "", {}
    arg = {}
    for w in words[1:]:
        k, _, v = w.partition("=")
        if re.fullmatch(r"[a-z_]{1,20}", k) and re.fullmatch(r"[A-Za-z0-9_.:\-]{0,60}", v):
            arg[k] = v
    return words[0], arg


def push(msg: str) -> None:
    git("add", "-A")
    if "nothing to commit" in git("commit", "-q", "-m", msg):
        return
    for _ in range(3):
        if "[exit" not in git("push", "-q", "origin", "HEAD:main"):
            return
        git("pull", "-q", "--rebase", "origin", "main")


def main() -> None:
    if not (REPO / ".git").exists():
        sys.exit(f"no clone at {REPO}")
    git("pull", "-q", "--rebase", "origin", "main")
    for d in ("inbox", "outbox", "done", "status"):
        (REPO / d).mkdir(exist_ok=True)
    did = False
    for f in sorted((REPO / "inbox").glob("*.cmd")):
        action, arg = parse(f.read_text(errors="replace").splitlines()[0] if f.read_text().strip() else "")
        t0 = time.time()
        try:
            out = ACTIONS[action](arg) if action in ACTIONS else f"refused: unknown action {action!r}; known: {', '.join(ACTIONS)}"
        except Exception as e:                       # noqa: BLE001 - the answer says what went wrong
            out = f"error: {type(e).__name__}: {e}"
        (REPO / "outbox" / (f.stem + ".txt")).write_text(
            f"{action} {arg}  ({time.time() - t0:.0f} s, {dt.datetime.utcnow():%Y-%m-%d %H:%M} UTC)\n\n{out}\n")
        git("mv", f"inbox/{f.name}", f"done/{f.name}")
        did = True
    st = REPO / "status" / "latest.txt"
    if did or not st.exists() or time.time() - st.stat().st_mtime > STATUS_EVERY:
        try:
            st.write_text(a_status({}) + "\n")
        except Exception as e:                       # noqa: BLE001
            st.write_text(f"status failed: {e}\n")
    push(f"ops {dt.datetime.utcnow():%m-%d %H:%M}")


if __name__ == "__main__":
    main()
