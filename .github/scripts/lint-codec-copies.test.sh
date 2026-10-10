#!/usr/bin/env bash
# lint-codec-copies.sh passes on this checkout and fails on a stale copy or a
# Python codec that drifted.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

"$here/lint-codec-copies.sh" "$root" >/dev/null

copy() {
  rm -rf "${work:?}/r"
  for f in agent/test/fixtures/payload-vectors.json \
    agent/src/db/migrations/20261009T0421Z_payload_keys.sql \
    agent-durable/src/scadbuddy_durable/codec.py \
    backend/scadbuddy/workflows/payload_codec.py \
    backend/tests/fixtures/payload-vectors.json \
    backend/tests/fixtures/agent-migrations/20261009T0421Z_payload_keys.sql; do
    mkdir -p "$work/r/$(dirname "$f")"
    cp "$root/$f" "$work/r/$f"
  done
}

copy
echo " " >>"$work/r/backend/tests/fixtures/payload-vectors.json"
if out="$("$here/lint-codec-copies.sh" "$work/r" 2>&1)" || ! grep -q "payload-vectors.json is not a copy" <<<"$out"; then
  echo "FAIL: a stale vectors copy passed: $out"; exit 1
fi

copy
sed -i 's/pg_advisory_xact_lock/pg_advisory_lock/' "$work/r/backend/scadbuddy/workflows/payload_codec.py"
if out="$("$here/lint-codec-copies.sh" "$work/r" 2>&1)" || ! grep -q "differ in: PgPayloadKeys" <<<"$out"; then
  echo "FAIL: a drifted codec passed: $out"; exit 1
fi
echo "ok: stale copies and drifted codecs fail"
