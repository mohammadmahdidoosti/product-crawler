#!/bin/sh
# one batch per process, so each batch starts with fresh memory (24 Nov). usage: lane_loop.sh QUEUE LANE
cd /app
Q=/data/lanes/$1
while [ ! -f $Q/STOP ] && [ -n "$(ls $Q/todo)" ]; do
  python3 tools/batch_lanes.py lane $1 $2 --slots 1 --max-usd 2 --no-send --once
  sleep 5
done
