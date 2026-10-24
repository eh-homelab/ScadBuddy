#!/usr/bin/env bash
#
# Seed the hot-path stack (e2e-stack/compose.yaml, project sb-hotpath) for one mode:
#
#   seed.sh recorder   bambuddy_url is the recorder, which the overlay refresh reads
#                      once; its list is then cleared, so what it sees afterwards is
#                      the hot path alone (AC1)
#   seed.sh unset      no bambuddy_url at all (AC2)
#
# What it sets, in order (plan 2026-10-18 Task 13):
#   1. the bucket on moto, then store_backend=s3 with the store_s3_* of the `s3`
#      service and library_backend=native; both apply at a start, so the app restarts;
#   2. slicer_backend=native, and bambuddy_url (recorder) or null (unset);
#   3. the printer: POST /printers with the fake's serial, preferred_path native, then
#      PUT /printers/{id}/lan with host `printer` and the access code (the LAN client's
#      ports are fixed at 8883 and 990, `printers/lan.py`);
#   4. recorder mode: the spool-providers Schedule triggered once and waited for in
#      spool_provider_refreshes, then spool_overlay_refresh_seconds=0, which applies at
#      a start (`core/settings.py`), so the app restarts once more and its Schedule is
#      deleted; then the recorder's POST /__reset;
#   5. the printer's POST /__reset, so its lists hold only what the test sends.
#
# run.sh calls it with the stack up. It talks to the app on SB_HOTPATH_API
# (http://127.0.0.1:18080) and reaches the other services through `docker compose exec`.
set -euo pipefail

mode="${1:-}"
case "$mode" in
  recorder | unset) ;;
  *)
    echo "usage: $0 <recorder|unset>" >&2
    exit 2
    ;;
esac

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
compose=(docker compose -p sb-hotpath -f "$here/compose.yaml")
api="${SB_HOTPATH_API:-http://127.0.0.1:18080}/api/v1"
serial="${SB_HOTPATH_SERIAL:-01P00FAKE000001}"
access_code="${SB_HOTPATH_ACCESS_CODE:-12345678}"
bucket=sb-hotpath
# The library worker's queue (`temporal_task_queue_library`'s default): the Schedule is
# `scadbuddy-spool-providers-<queue>` (workflows/spool_refresh.py `spool_schedule_id_for`).
spool_schedule=scadbuddy-spool-providers-library

log() { echo "seed($mode): $*" >&2; }

# A request with a JSON body; prints the answer, fails on a non-2xx with the answer shown.
call() {
  local method="$1" path="$2" body="${3:-}"
  local out status
  out="$(mktemp)"
  if [ -n "$body" ]; then
    status="$(curl --silent --show-error --output "$out" --write-out '%{http_code}' \
      -X "$method" -H 'Content-Type: application/json' --data "$body" "$api$path")"
  else
    status="$(curl --silent --show-error --output "$out" --write-out '%{http_code}' \
      -X "$method" "$api$path")"
  fi
  if [ "${status:0:1}" != 2 ]; then
    echo "seed($mode): $method $path answered $status:" >&2
    cat "$out" >&2
    echo >&2
    rm -f "$out"
    return 1
  fi
  cat "$out"
  rm -f "$out"
}

psql_value() {
  "${compose[@]}" exec -T postgres psql -U postgres -d scadbuddy -tAc "$1"
}

wait_healthy() {
  local id status=""
  id="$("${compose[@]}" ps -q app)"
  for _ in $(seq 1 120); do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$id" 2>/dev/null || true)"
    if [ "$status" = healthy ]; then
      return 0
    fi
    sleep 2
  done
  echo "seed($mode): the app never became healthy (last status: ${status:-<none>})" >&2
  "${compose[@]}" logs --tail 200 app >&2 || true
  return 1
}

restart_app() {
  log "restarting the app"
  "${compose[@]}" restart app >/dev/null
  # A start resets the container's health to `starting`, so `healthy` is the new process.
  wait_healthy
}

wait_healthy

log "the bucket, the S3 store and the native library"
"${compose[@]}" exec -T s3 curl --fail --silent --show-error -X PUT "http://127.0.0.1:5000/${bucket}" >/dev/null
call PUT /settings "$(
  cat <<EOF
{
  "store_backend": "s3",
  "store_s3_endpoint": "http://s3:5000",
  "store_s3_region": "us-east-1",
  "store_s3_bucket": "${bucket}",
  "store_s3_prefix": "scadbuddy",
  "store_s3_access_key_id": "testing",
  "store_s3_secret_access_key": "testing",
  "store_s3_path_style": true,
  "library_backend": "native"
}
EOF
)" >/dev/null
restart_app

if [ "$mode" = recorder ]; then
  bambuddy_url='"http://bambuddy:8000"'
else
  bambuddy_url=null
fi
log "the native slicer, bambuddy_url=${bambuddy_url}"
call PUT /settings "{\"slicer_backend\": \"native\", \"bambuddy_url\": ${bambuddy_url}}" >/dev/null

log "the printer ${serial}, linked over the LAN"
printer="$(call POST /printers "{\"serial\": \"${serial}\", \"name\": \"H2C\", \"model\": \"H2C\", \"preferred_path\": \"native\"}")"
printer_id="$(python3 -c 'import json, sys; print(json.load(sys.stdin)["id"])' <<<"$printer")"
call PUT "/printers/${printer_id}/lan" "{\"host\": \"printer\", \"access_code\": \"${access_code}\"}" >/dev/null

if [ "$mode" = recorder ]; then
  log "one spool-providers refresh from the recorder"
  since="$(psql_value "SELECT now()")"
  "${compose[@]}" exec -T temporal temporal schedule trigger \
    --schedule-id "$spool_schedule" --address 127.0.0.1:7233 --namespace default >/dev/null
  refreshed=""
  for _ in $(seq 1 60); do
    refreshed="$(psql_value "SELECT count(*) FROM spool_provider_refreshes WHERE provider = 'bambuddy' AND refreshed_at > '${since}'")"
    [ "${refreshed:-0}" -ge 1 ] && break
    sleep 1
  done
  if [ "${refreshed:-0}" -lt 1 ]; then
    echo "seed($mode): no bambuddy refresh recorded since ${since}:" >&2
    psql_value "SELECT * FROM spool_provider_refreshes" >&2 || true
    exit 1
  fi
  call PUT /settings '{"spool_overlay_refresh_seconds": 0}' >/dev/null
  restart_app
  curl --fail --silent --show-error -X POST http://127.0.0.1:18000/__reset >/dev/null
fi

curl --fail --silent --show-error -X POST http://127.0.0.1:18081/__reset >/dev/null
echo "$printer_id"
