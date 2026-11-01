#!/usr/bin/env bash
# One agent runtime (spec 2026-10-13 §3 goal 7, plan Step 6.6): fail if a second turn path
# appears in agent/src. Every session is an AgentSession (Step 6.3), so nothing branches on a
# session mode, and only the harness calls the SDK's query().
set -euo pipefail
root="${1:-.}"
fail=0
# A wrong root would find nothing and pass.
[ -d "$root/agent/src" ] || { echo "no agent/src under $root" >&2; exit 1; }
# A branch is a comparison with a mode (`mode === 'durable'`, `mode != "classic"`) or a case
# of one (`case 'durable':`), in any quotes. --include='*.ts' already leaves out the .sql
# migrations. A comment line (`//`, `/*`, ` *`) is not a branch, so the comments that still
# name the old setting pass.
q="['\"\`]"
if grep -rnE "mode ?[!=]==? ?$q(durable|classic)$q|case $q(durable|classic)$q|sessionModeOf|session_mode" "$root/agent/src" --include='*.ts' \
    | grep -vE '^[^:]+:[0-9]+:[[:space:]]*(//|/\*|\*)'; then
  echo "a session mode branch is back: every session is an AgentSession" >&2
  fail=1
fi
# The Python turn is retired (plan Step 8.1, spec §14.4): agent-durable/ neither imports the
# Claude Agent SDK (`claude_agent_sdk`, or the Temporal plugin's `temporalio.claude_agent_sdk`)
# nor depends on it, and no file of it names DurableSession, the workflow that turn ran in.
if [ -d "$root/agent-durable" ]; then
  if grep -rnE "^[[:space:]]*(from|import)[[:space:]][^#]*\bclaude_agent_sdk\b|\bDurableSession\b" "$root/agent-durable" \
      --include='*.py' --exclude-dir=.venv --exclude-dir=__pycache__; then
    echo "agent-durable/ runs a turn again: the Python turn is retired" >&2
    fail=1
  fi
  if grep -nE "claude-agent-sdk" "$root/agent-durable/pyproject.toml" 2>/dev/null; then
    echo "agent-durable/ depends on the Claude Agent SDK again: the Python turn is retired" >&2
    fail=1
  fi
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
