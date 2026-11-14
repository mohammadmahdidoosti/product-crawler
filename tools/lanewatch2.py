"""(14 Nov) Hand the kw1 lanes over to new lane processes (new code) without stopping any batch in the middle,
and watch them. Every 2 minutes:
  * what is still waiting in kw1 (todo, failed) moves to the kw2 queue; the kw1 lanes find nothing more to take
    and end after the batch they are on;
  * a lane number with no process (kw1 or kw2) gets a kw2 lane - never two processes for one number (memory);
  * a lane that died puts its batch back (kw1 doing -> kw2 todo, kw2 doing -> kw2 todo);
  * a lane whose log has not moved for 150 minutes is stopped and its batch put back;
  * a failed batch is tried once more (not when the reason is no credit).
Ends when both queues are empty and no lane runs. Log: /data/tmp/kw2-watch.log"""
import os, re, time, signal, subprocess
Q1, Q2 = "/data/lanes/kw1", "/data/lanes/kw2"
LANES = range(1, 9)
HUNG = 150 * 60
for sub in ("todo", "doing", "done", "failed"):
    os.makedirs(f"{Q2}/{sub}", exist_ok=True)
# (14 Nov) the shop gave HTTP 500s with three sends + categories: kw1 and kw2 lanes share the same send slots
# (kw2 slot files are links to kw1's), and slot 2 is held by a holder process, so at most 2 send at once
for i in range(3):
    s2 = f"{Q2}/sendslot-{i}"
    if not os.path.islink(s2):
        if os.path.exists(s2):
            os.remove(s2)
        os.symlink(f"{Q1}/sendslot-{i}", s2)

def say(*a):
    with open("/data/tmp/kw2-watch.log", "a") as f:
        print(time.strftime("%m-%d %H:%M"), *a, file=f, flush=True)

def log_of(q, k):
    return f"/data/tmp/{q}-lane{k}.log"

def procs(script):
    out = {}
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            c = [x.decode(errors="ignore") for x in open(f"/proc/{p}/cmdline", "rb").read().split(b"\0")]
        except OSError:
            continue
        if len(c) > 1 and c[0].startswith("python") and c[1].endswith(script):
            out[p] = c
    return out

def lanes():
    out = {}
    for p, c in procs("batch_lanes.py").items():
        if len(c) > 4 and c[2] == "lane" and c[3] in ("kw1", "kw2"):
            out[int(c[4])] = (c[3], int(p))
    return out

def last_batch(q, k):
    try:
        lines = [l for l in open(log_of(q, k), errors="ignore") if f"LANE{k} batch-" in l]
    except OSError:
        return None
    m = re.search(r"batch-(\d+)", lines[-1]) if lines else None
    return m.group(1) if m else None

def put_back(q, k):
    b = last_batch(q, k)
    src = f"/data/lanes/{q}/doing/{b}"
    if b and os.path.exists(src):
        os.rename(src, f"{Q2}/todo/{b}")
        say("lane", k, q, "batch", b, "back to the queue")

def start(k):
    subprocess.Popen(f"cd /app && exec python3 tools/batch_lanes.py lane kw2 {k} --slots 3 >> {log_of('kw2', k)} 2>&1",
                     shell=True, start_new_session=True)
    say("lane", k, "started on kw2")

# the old watch must not start kw1 lanes again
for p, c in procs("lanewatch.py").items():
    os.kill(int(p), signal.SIGTERM)
    say("old watch", p, "stopped")
retried = set()
say("watch started")
while True:
    if os.path.exists(f"{Q2}/STOP") or os.path.exists(f"{Q1}/STOP"):
        say("STOP file: watch ends"); break
    for f in os.listdir(f"{Q1}/todo"):
        os.rename(f"{Q1}/todo/{f}", f"{Q2}/todo/{f}")
    for q in (Q1, Q2):
        for f in os.listdir(f"{q}/failed"):
            note = open(f"{q}/failed/{f}", errors="ignore").read()
            if "OpenRouter" in note or "اعتبار" in note:
                continue
            if f in retried:
                continue
            retried.add(f)
            os.rename(f"{q}/failed/{f}", f"{Q2}/todo/{f}")
            say("failed batch", f, "tried once more:", note[:150])
    run = lanes()
    for k in LANES:
        if k in run:
            q, pid = run[k]
            age = time.time() - os.path.getmtime(log_of(q, k))
            if age > HUNG:
                say("lane", k, q, "silent for", int(age / 60), "min: stopped")
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except OSError:
                    os.kill(pid, signal.SIGKILL)
                time.sleep(5)
                put_back(q, k)
                if os.listdir(f"{Q2}/todo"):
                    start(k)
            continue
        put_back("kw1", k)
        put_back("kw2", k)
        if os.listdir(f"{Q2}/todo"):
            start(k)
    if not lanes() and not os.listdir(f"{Q2}/todo") and not os.listdir(f"{Q2}/doing") \
            and not os.listdir(f"{Q1}/todo") and not os.listdir(f"{Q1}/doing"):
        say("all batches done: watch ends"); break
    time.sleep(120)
