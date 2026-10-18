#!/usr/bin/env bash
#
# Mount a live ScadBuddy's print library with bambuddy-fs and walk a file through it
# (tools/go/bambuddy-fs/internal/fsys/live_test.go TestLiveMount), on one library
# backend (#3827):
#
#   bambuddy-fs-live.sh native     ScadBuddy's own library, on moto's S3
#   bambuddy-fs-live.sh bambuddy   Bambuddy's, a fake one in memory
#                                  (backend/tests/support/fake_bambuddy_library.py)
#
# It needs the images scadbuddy:ci-test and scadbuddy:ci, a Postgres it can create
# databases in and a Temporal dev server, both reached on this host: in CI's `image`
# job the postgres service and the `temporal-ci` container. Locally, point it at your
# own with SCADBUDDY_FS_LIVE_PG_PORT and SCADBUDDY_FS_LIVE_TEMPORAL_PORT (the Temporal
# server must listen on 0.0.0.0, as the app reaches it through host.docker.internal).
# The mount needs /dev/fuse and fusermount3; without them the Go test skips.
set -euo pipefail

backend="${1:-}"
case "$backend" in
  native) port=18081 ;;
  bambuddy) port=18082 ;;
  *)
    echo "usage: $0 <native|bambuddy>" >&2
    exit 2
    ;;
esac

pg_port="${SCADBUDDY_FS_LIVE_PG_PORT:-5432}"
temporal_port="${SCADBUDDY_FS_LIVE_TEMPORAL_PORT:-7233}"
database="scadbuddy_fs_${backend}"
namespace="scadbuddy-fs-${backend}"
moto_port=18091
fake_port=18092
bucket=sb-fs
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

app="bambuddy-fs-live-${backend}-$$"
side="bambuddy-fs-live-${backend}-side-$$"
started=()

finish() {
  status=$?
  if [ "$status" -ne 0 ]; then
    for name in "${started[@]}"; do
      echo "--- logs of $name"
      docker logs "$name" 2>&1 || true
    done
  fi
  for name in "${started[@]}"; do
    docker stop "$name" >/dev/null 2>&1 || true
  done
  exit "$status"
}
trap finish EXIT

in_test_image() {
  docker run --rm -i --network host -w /app/backend scadbuddy:ci-test uv run --frozen --no-sync "$@"
}

in_test_image python - "$pg_port" "$database" <<'EOF'
import sys

import psycopg

port, database = sys.argv[1], sys.argv[2]
url = f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"
with psycopg.connect(url, autocommit=True) as conn:
    conn.execute(f'DROP DATABASE IF EXISTS "{database}"')
    conn.execute(f'CREATE DATABASE "{database}"')
EOF

docker run --rm --network host --entrypoint temporal scadbuddy:ci-test \
  operator namespace create --namespace "$namespace" --address "127.0.0.1:${temporal_port}"

library_env=()
if [ "$backend" = native ]; then
  started+=("$side")
  docker run -d --rm --name "$side" --network host -w /app/backend scadbuddy:ci-test \
    uv run --frozen --no-sync moto_server -H 0.0.0.0 -p "$moto_port" >/dev/null
  for _ in $(seq 1 30); do
    curl --silent --output /dev/null "http://127.0.0.1:${moto_port}/" && break
    sleep 1
  done
  curl --fail --silent --show-error -X PUT "http://127.0.0.1:${moto_port}/${bucket}" >/dev/null
  library_env=(
    -e SCADBUDDY_STORE_BACKEND=s3
    -e "SCADBUDDY_STORE_S3_ENDPOINT=http://host.docker.internal:${moto_port}"
    -e "SCADBUDDY_STORE_S3_BUCKET=${bucket}"
    -e SCADBUDDY_STORE_S3_PREFIX=fs
    -e SCADBUDDY_STORE_S3_ACCESS_KEY_ID=testing
    -e SCADBUDDY_STORE_S3_SECRET_ACCESS_KEY=testing
  )
else
  started+=("$side")
  docker run -d --rm --name "$side" --network host -w /app/backend scadbuddy:ci-test \
    uv run --frozen --no-sync python -m tests.support.fake_bambuddy_library \
    --host 0.0.0.0 --port "$fake_port" >/dev/null
  for _ in $(seq 1 30); do
    curl --fail --silent --output /dev/null "http://127.0.0.1:${fake_port}/api/v1/queue/" && break
    sleep 1
  done
  curl --fail --silent --show-error --output /dev/null "http://127.0.0.1:${fake_port}/api/v1/queue/"
  library_env=(
    -e "SCADBUDDY_BAMBUDDY_URL=http://host.docker.internal:${fake_port}"
    -e SCADBUDDY_BAMBUDDY_API_KEY=fake
  )
fi

# Published, not on the host network: the image always binds 8080 (Dockerfile).
started+=("$app")
docker run -d --rm --name "$app" -p "${port}:8080" \
  --add-host=host.docker.internal:host-gateway \
  -e "SCADBUDDY_DATABASE_URL=postgresql://postgres:postgres@host.docker.internal:${pg_port}/${database}" \
  -e "SCADBUDDY_TEMPORAL_ADDRESS=host.docker.internal:${temporal_port}" \
  -e "SCADBUDDY_TEMPORAL_NAMESPACE=${namespace}" \
  -e SCADBUDDY_TEMPORAL_WORKER_INPROCESS=true \
  -e "SCADBUDDY_LIBRARY_BACKEND=${backend}" \
  "${library_env[@]}" \
  scadbuddy:ci >/dev/null

health=""
for _ in $(seq 1 90); do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$app" 2>/dev/null || true)"
  case "$health" in
    healthy | unhealthy) break ;;
  esac
  sleep 2
done
if [ "$health" != healthy ]; then
  echo "::error::ScadBuddy (${backend}) never became healthy (last status: ${health:-<none>})"
  exit 1
fi

cd "$repo/tools/go/bambuddy-fs"
BAMBUDDY_FS_LIVE_API="http://127.0.0.1:${port}/api/v1" \
  go test -run TestLiveMount -count=1 -v ./internal/fsys/
