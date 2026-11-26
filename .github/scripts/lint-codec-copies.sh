#!/usr/bin/env bash
# Usage: lint-codec-copies.sh [repo root, default this checkout]
#
# The subject payload codec is kept three times (spec 2026-10-01 §6.5): agent/
# (TypeScript), agent-durable/ and backend/ (Python). The payload vectors pin the
# bytes; this pins the rest (plan 2026-10-09-durable-phase-6-flows.md Ruling 10):
#  - the backend's tests run in an image holding only backend/, so they read copies
#    of the agent's vectors, which must equal the originals (the agent's migrations
#    live in backend/scadbuddy/migrations_agent/ itself, #5299);
#  - the two Python codecs must be the same code apart from their imports,
#    docstrings (module, class and function ones) and converter function (the key lock and tombstone logic that no
#    vector covers);
#  - the hosted plugin name rules (#6932): `hosted_plugins/rules.py` `RESERVED_NAMES` must
#    be the same set as `agent/src/plugins/registry.ts` `RESERVED_PLUGIN_NAMES`.
set -euo pipefail
root="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
fail=0
same() {
  if ! cmp -s "$root/$1" "$root/$2"; then
    echo "FAIL: $2 is not a copy of $1; copy it again"; fail=1
  fi
}
same agent/test/fixtures/payload-vectors.json backend/tests/fixtures/payload-vectors.json
same agent/test/fixtures/subject-vectors.json backend/tests/fixtures/subject-vectors.json
# The agent's credential vectors, which the API opens behind its CRUD routes (#5299 step 4).
same agent/test/fixtures/secret-vectors.json backend/tests/fixtures/secret-vectors.json
# The vectors that pin the gate's validator and response hash (agent-durable's) to the
# agent's; the Go API's gate (tools/go/core/internal/gate, #7667) reads this copy.
same agent/test/fixtures/pending-input-vectors.json backend/tests/fixtures/pending-input-vectors.json
same agent/test/fixtures/response-hash-vectors.json backend/tests/fixtures/response-hash-vectors.json
# The hosted plugin content hash's vectors (#6932): `hash.ts` and `hosted_plugins/bundle.py`
# must give the same hash, and the backend's tests read this copy.
same agent/test/fixtures/hosted-plugin-hash-vectors.json backend/tests/fixtures/hosted-plugin-hash-vectors.json

if ! python3 - "$root" <<'PY'
import ast
import sys

root = sys.argv[1]


def is_doc(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def strip_docs(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if len(node.body) > 1 and is_doc(node.body[0]):
                node.body = node.body[1:]


def body(path: str, converter: str) -> dict[str, str]:
    out = {}
    tree = ast.parse(open(f"{root}/{path}").read())
    strip_docs(tree)
    for node in tree.body:
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
if ! python3 - "$root" <<'PY'
import ast
import re
import sys

root = sys.argv[1]
ts = open(f"{root}/agent/src/plugins/registry.ts").read()
found = re.search(r"RESERVED_PLUGIN_NAMES[^=]*=\s*new Set\(\[(.*?)\]\)", ts, re.S)
if found is None:
    print("FAIL: RESERVED_PLUGIN_NAMES not found in agent/src/plugins/registry.ts")
    sys.exit(1)
agent = set(re.findall(r"'([^']+)'", found.group(1)))
backend: set[str] = set()
for node in ast.parse(open(f"{root}/backend/scadbuddy/hosted_plugins/rules.py").read()).body:
    if isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "RESERVED_NAMES" for t in node.targets
    ):
        backend = set(ast.literal_eval(node.value.args[0]))  # type: ignore[attr-defined]
if not agent or agent != backend:
    print(
        "FAIL: the reserved plugin names differ; only in the agent: "
        f"{sorted(agent - backend)}, only in the backend: {sorted(backend - agent)}"
    )
    sys.exit(1)
PY
then
  fail=1
fi
[ "$fail" -eq 0 ] && echo "ok: the codec copies match"
exit "$fail"
