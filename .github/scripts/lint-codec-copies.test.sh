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
copy
python3 - "$work/r/backend/scadbuddy/workflows/payload_codec.py" <<'PY'
import ast
import sys

# A method gains a docstring the other copy lacks: wording, not the codec.
path = sys.argv[1]
lines = open(path).read().split("\n")
tree = ast.parse("\n".join(lines))
cls = next(n for n in tree.body if getattr(n, "name", None) == "PgPayloadKeys")
first = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)).body[0]
lines.insert(first.lineno - 1, " " * first.col_offset + '"""Only in this copy."""')
open(path, "w").write("\n".join(lines))
PY
if ! out="$("$here/lint-codec-copies.sh" "$work/r" 2>&1)"; then
  echo "FAIL: a docstring inside a method failed the check: $out"; exit 1
fi
echo "ok: stale copies and drifted codecs fail; docstrings do not"
