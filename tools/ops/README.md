# Ops mailbox (25 Nov)

Claude runs the daily work on the crawler host without the owner's browser through a PRIVATE GitHub repository
(`caravan-ops`, never this public one). Claude writes `inbox/<id>.cmd` (one line: `action key=value ...`); the host
runs `tools/ops/ops_agent.py` every minute from cron, answers in `outbox/<id>.txt` and keeps `status/latest.txt`
fresh (every 30 minutes and after each command).

Actions (nothing else runs; no shell command is ever taken from the repository):

| action | arguments | does |
| --- | --- | --- |
| `status` | | queue, processes, loads, disks, OOM, OpenRouter credit, Drive, backup, last sends |
| `requeue_failed` | | failed/ batches back to todo/ (kept in /root/fix/failed_<time>) |
| `queue_list` | `file=kw_NNN_NNN.csv` | a list from /srv/caravan-data/kwlists: dry run, import, fill (at most 151 batches) |
| `start_all` | | lanes 1-9, senders, presend workers: only those not running; removes STOP files |
| `stop_all` | | STOP files: each part ends after its current batch |
| `workers` | `sender=send|send2 n=1|2|3` | a sender's worker count from its next batch |

Install on the host: a deploy key with write access to `caravan-ops` only, a clone at /root/ops/repo, and

    * * * * * /usr/bin/flock -n /tmp/caravan-ops.lock /usr/bin/python3 /root/ops/ops_agent.py >> /root/ops/agent.log 2>&1

Senders started by `tools/lanes/send_loop.sh` send one batch per process and read `workers_<name>` before each.
