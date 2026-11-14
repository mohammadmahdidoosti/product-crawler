"""(14 Nov, owner: 'it must run by itself, nothing left waiting') Watchdog for the kw1 lanes, inside the container.
Every 5 minutes:
  * a lane that is no longer running while batches are still waiting is started again; the batch it was on
    goes back to the queue first (so nothing stays in doing/ for ever);
  * a lane whose log has not moved for 150 minutes is taken as hung: stopped, its batch back in the queue, restarted;
  * a failed batch is tried once more (not when the reason is no credit).
Ends when the queue is empty and no lane runs. Log: /data/tmp/kw1-watch.log"""
import os, re, time, signal, subprocess
Q = "/data/lanes/kw1"
LANES = range(1, 7)
LOG = "/data/tmp/kw1-lane{}.log"
HUNG = 150 * 60

def say(*a):
    with open("/data/tmp/kw1-watch.log", "a") as f:
        print(time.strftime("%m-%d %H:%M"), *a, file=f, flush=True)

def lane_pids():
    out = {}
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            cmd = open(f"/proc/{p}/cmdline", "rb").read().split(b"\0")
        except OSError:
            continue
        c = [x.decode(errors="ignore") for x in cmd]
        if len(c) > 4 and c[0].startswith("python") and c[1].endswith("batch_lanes.py") and c[2] == "lane" and c[3] == "kw1":
            out[int(c[4])] = int(p)
    return out

def last_batch(k):
    try:
        lines = [l for l in open(LOG.format(k), errors="ignore") if f"LANE{k} batch-" in l]
    except OSError:
        return None
    m = re.search(r"batch-(\d+)", lines[-1]) if lines else None
    return m.group(1) if m else None

def requeue(k):
    b = last_batch(k)
    if b and os.path.exists(f"{Q}/doing/{b}"):
        os.rename(f"{Q}/doing/{b}", f"{Q}/todo/{b}")
        say("lane", k, "batch", b, "back to the queue")

def start(k):
    subprocess.Popen(f"cd /app && exec python3 tools/batch_lanes.py lane kw1 {k} --slots 3 >> {LOG.format(k)} 2>&1",
                     shell=True, start_new_session=True)
    say("lane", k, "started")

retried = set()
say("watch started")
while True:
    time.sleep(300)
    if os.path.exists(f"{Q}/STOP"):
        say("STOP file: watch ends"); break
    pids = lane_pids()
    todo = os.listdir(f"{Q}/todo")
    for f in os.listdir(f"{Q}/failed"):
        note = open(f"{Q}/failed/{f}", errors="ignore").read()
        if f not in retried and "OpenRouter" not in note and "اعتبار" not in note:
            retried.add(f)
            os.rename(f"{Q}/failed/{f}", f"{Q}/todo/{f}")
            say("failed batch", f, "tried once more:", note[:150])
            todo.append(f)
    for k in LANES:
        if k in pids:
            age = time.time() - os.path.getmtime(LOG.format(k))
            if age > HUNG:
                say("lane", k, "silent for", int(age / 60), "min: stopped")
                try:
                    os.killpg(os.getpgid(pids[k]), signal.SIGKILL)
                except OSError:
                    os.kill(pids[k], signal.SIGKILL)
                time.sleep(5)
                requeue(k)
                start(k)
        elif todo or (last_batch(k) and os.path.exists(f"{Q}/doing/{last_batch(k)}")):
            requeue(k)
            start(k)
    if not pids and not os.listdir(f"{Q}/todo") and not os.listdir(f"{Q}/doing"):
        say("all batches done: watch ends"); break
