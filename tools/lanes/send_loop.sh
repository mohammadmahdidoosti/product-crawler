#!/bin/sh
# (25 Nov) one sender: one batch per process (--max 1), so a new worker count in /data/lanes/QUEUE/workers_NAME
# (1-3, read before each batch) applies at the next batch and no batch is cut. usage: send_loop.sh QUEUE NAME
cd /app
Q=/data/lanes/$1
while [ ! -f $Q/STOPSEND ]; do
  W=$(cat $Q/workers_$2 2>/dev/null | tr -cd 0-9)
  case "$W" in 1|2|3) ;; *) W=2 ;; esac
  WC_BRIDGE=unix:/data/tmp/bridge.sock python3 tools/bulk_send.py $1 --workers $W --max 1 >> $Q/$2.log 2>&1
  if tail -n1 $Q/$2.log | grep -q "ALLDONE 0 batch"; then sleep 120; else sleep 10; fi   # nothing was ready
done
