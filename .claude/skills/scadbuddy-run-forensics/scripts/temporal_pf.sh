#!/usr/bin/env bash
# Port-forward ScadBuddy's Temporal UI to 127.0.0.1:18234 (idempotent) and wait for it.
set -u
PORT=${TEMPORAL_UI_PORT:-18234}
probe() { curl -s -m 4 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/v1/namespaces/scadbuddy/workflows?query="; }
if [ "$(probe)" = 200 ]; then echo "temporal ui already on $PORT"; exit 0; fi
(kubectl -n bambuddy port-forward svc/scadbuddy-temporal-v2-ui "$PORT:8080" >/dev/null 2>&1 &)
for _ in $(seq 1 15); do sleep 2; [ "$(probe)" = 200 ] && { echo "temporal ui on $PORT"; exit 0; }; done
echo "port-forward did not come up (kube context? pods?)"; exit 1
