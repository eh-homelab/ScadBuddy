#!/usr/bin/env bash
# Usage: lint-codec-copies.sh [repo root, default this checkout]
#
# The subject payload codec is kept three times (spec 2026-10-01 §6.5): agent/
# (TypeScript), agent-durable/ and backend/ (Python). The payload vectors pin the
# bytes; this pins the rest (plan 2026-10-09-durable-phase-6-flows.md Ruling 10):
#  - the backend's tests run in an image holding only backend/, so they read copies
#    of the agent's vectors and key migration, which must equal the originals;
#  - the two Python codecs must be the same code apart from their imports,
#    docstrings and converter function (the key lock and tombstone logic that no
#    vector covers).
set -euo pipefail
root="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
fail=0
same() {
  if ! cmp -s "$root/$1" "$root/$2"; then
    echo "FAIL: $2 is not a copy of $1; copy it again"; fail=1
  fi
}
same agent/test/fixtures/payload-vectors.json backend/tests/fixtures/payload-vectors.json
same agent/src/db/migrations/20261009T0421Z_payload_keys.sql \
  backend/tests/fixtures/agent-migrations/20261009T0421Z_payload_keys.sql

if ! python3 - "$root" <<'PY'
import ast
import sys

root = sys.argv[1]


def body(path: str, converter: str) -> dict[str, str]:
    out = {}
    for node in ast.parse(open(f"{root}/{path}").read()).body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        name = getattr(node, "name", None) or ast.unparse(node)
        if name != converter:
            out[name] = ast.dump(node)
    return out


durable = body("agent-durable/src/scadbuddy_durable/codec.py", "data_converter")
backend = body("backend/scadbuddy/workflows/payload_codec.py", "flows_converter")
differ = sorted(k for k in durable.keys() | backend.keys() if durable.get(k) != backend.get(k))
if differ:
    print("FAIL: the two Python payload codecs differ in: " + ", ".join(differ))
    sys.exit(1)
PY
then
  fail=1
fi
[ "$fail" -eq 0 ] && echo "ok: the codec copies match"
exit "$fail"
