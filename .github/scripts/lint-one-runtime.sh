#!/usr/bin/env bash
# One agent runtime (spec 2026-10-13 §3 goal 7, plan Step 6.6): fail if a second turn path
# appears in agent/src. Every session is an AgentSession (Step 6.3), so nothing branches on a
# session mode, and only the harness calls the SDK's query().
set -euo pipefail
root="${1:-.}"
fail=0
# --include='*.ts' already leaves out the .sql migrations. A comment line (`//`, `/*`, ` *`)
# is not a branch, so the comments that still name the old setting pass.
if grep -rnE "mode ?[!=]==? ?'(durable|classic)'|sessionModeOf|session_mode" "$root/agent/src" --include='*.ts' \
    | grep -vE '^[^:]+:[0-9]+:[[:space:]]*(//|/\*|\*)'; then
  echo "a session mode branch is back: every session is an AgentSession" >&2
  fail=1
fi
# Every import of the SDK's `query` (named, renamed, across lines, a namespace import, a
# dynamic import), wherever it is called, outside the harness's two call sites.
python3 - "$root/agent/src" <<'PY' || fail=1
import re, sys
from pathlib import Path
SDK = r"['\"]@anthropic-ai/claude-agent-sdk['\"]"
STATIC = re.compile(r"\bimport\s+(?!type\s)([^;]*?)\s+from\s+" + SDK, re.S)
DYNAMIC = re.compile(r"\bimport\(\s*" + SDK)
ALLOWED = {"harness/run.ts", "harness/testConnection.ts"}
root, bad = Path(sys.argv[1]), 0
for f in sorted(root.rglob("*.ts")):
    if f.relative_to(root).as_posix() in ALLOWED:
        continue
    text = f.read_text(encoding="utf-8")
    clauses = [m.group(1) for m in STATIC.finditer(text)]
    if DYNAMIC.search(text) or any(
        re.search(r"\*\s+as\s|(^|[{,])\s*query\b", c) for c in clauses
    ):
        print(f"query() outside the harness: {f}", file=sys.stderr)
        bad = 1
sys.exit(bad)
PY
exit "$fail"
