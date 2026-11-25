#!/bin/sh
# (25 Nov) one presend worker, restarted if it stops. usage: presend_loop.sh QUEUE WORKER
cd /app
Q=/data/lanes/$1
while [ ! -f $Q/STOPPRESEND ]; do
  WC_BRIDGE=unix:/data/tmp/bridge.sock python3 tools/presend.py $1 --worker $2 >> $Q/presend$2.log 2>&1
  sleep 60
done
