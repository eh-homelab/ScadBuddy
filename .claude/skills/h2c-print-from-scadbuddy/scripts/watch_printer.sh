#!/usr/bin/env bash
# Watch the H2C through Bambuddy: wait for a job other than PREVIOUS to start, read its
# mapping off the printer, then print state transitions until it ends.
#   watch_printer.sh [PREVIOUS_JOB_NAME] [INTERVAL_SECONDS]
# Run it in the background; it exits when the job leaves RUNNING/PREPARE, or after
# ~6 hours. A plate-clear hold before the start is reported, not waited out silently.
set -u
B=https://bambuddy.internal.nullreference.io/api/v1
prev="${1:-}"; every="${2:-30}"; here=$(cd "$(dirname "$0")" && pwd)
phase=hold; last=""
read_status() {
  curl -s -m 10 "$B/printers/1/status" | python3 -c '
import sys,json
try: d=json.load(sys.stdin)
except Exception: sys.exit(1)
print(d.get("state"), d.get("layer_num"), d.get("total_layers"), d.get("progress"), d.get("stg_cur_name"), d.get("subtask_name"), "clear=%s" % d.get("awaiting_plate_clear"))'
}
for _ in $(seq 1 800); do
  st=$(read_status) || { sleep 15; continue; }
  state=${st%% *}; layer=$(echo "$st" | cut -d' ' -f2); sub=$(echo "$st" | awk '{print $(NF-1)}')
  if [ "$phase" = hold ]; then
    if { [ "$state" = RUNNING ] || [ "$state" = PREPARE ]; } && [ "$sub" != "$prev" ]; then
      phase=run; echo "$(date -u +%H:%M:%S) started: $st"; sleep 20
      uv run --with paho-mqtt "$here/printer_report.py" 2>/dev/null || echo "(mapping read failed)"
    elif [ "$st" != "$last" ]; then echo "$(date -u +%H:%M:%S) waiting: $st"; last="$st"; fi
    sleep "$every"; continue
  fi
  key="$state $layer"
  if [ "$key" != "$last" ]; then echo "$(date -u +%H:%M:%S) $st"; last="$key"; fi
  case "$state" in RUNNING|PREPARE) ;; *) echo "$(date -u +%H:%M:%S) ended: $st"; exit 0;; esac
  sleep "$every"
done
echo "watch_printer: gave up after the iteration cap"; exit 2
