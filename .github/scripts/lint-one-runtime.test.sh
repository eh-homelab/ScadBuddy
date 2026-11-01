#!/usr/bin/env bash
# lint-one-runtime.sh (plan 2026-10-13 Step 6.6, the old Task 6.3) passes on this checkout,
# fails on each fixture tree with a second turn path, and passes on the ones without.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT
fail=0

"$here/lint-one-runtime.sh" "$root" >/dev/null || { echo "FAIL: this checkout does not pass"; fail=1; }

# A tree of its own holding one file, agent/src/<path>, with <content>.
tree() {
  rm -rf "${work:?}/t"
  mkdir -p "$work/t/agent/src/$(dirname "$1")"
  printf '%s\n' "$2" >"$work/t/agent/src/$1"
}

expect_fail() {
  local why="$1"
  tree "$2" "$3"
  if "$here/lint-one-runtime.sh" "$work/t" >/dev/null 2>&1; then
    echo "FAIL: passed, but should fail: $why"
    fail=1
  fi
}

expect_pass() {
  local why="$1" out
  tree "$2" "$3"
  if ! out="$("$here/lint-one-runtime.sh" "$work/t" 2>&1)"; then
    echo "FAIL: failed, but should pass: $why"
    printf '  %s\n' "$out"
    fail=1
  fi
}

SDK="'@anthropic-ai/claude-agent-sdk'"

# A session mode branch.
expect_fail "mode === 'durable' in the harness" harness/x.ts "if (session.mode === 'durable') run()"
expect_fail "mode == 'classic'" sessions/y.ts "const c = mode == 'classic'"
expect_fail "mode !== 'durable'" sessions/y.ts "if (row.mode !== 'durable') return"
expect_fail "a double-quoted mode" sessions/y.ts 'if (row.mode === "durable") return'
# shellcheck disable=SC2016 # the backticks are the fixture's TypeScript, not an expansion
expect_fail "a template-literal mode" sessions/y.ts 'if (row.mode === `classic`) return'
expect_fail "a double-quoted case" sessions/y.ts 'switch (m) { case "classic": break }'
expect_fail "a case of a mode" sessions/y.ts "switch (row.mode) {
  case 'durable':
    return
}"
expect_fail "sessionModeOf" sessions/y.ts "const m = sessionModeOf(row)"
expect_fail "session_mode in code" sessions/y.ts "await settings.get('session_mode')"

# A root without agent/src (a wrong path, a moved tree) fails rather than finds nothing.
rm -rf "${work:?}/t"
mkdir -p "$work/t"
if "$here/lint-one-runtime.sh" "$work/t" >/dev/null 2>&1; then
  echo "FAIL: passed, but should fail: a root without agent/src"
  fail=1
fi

# The SDK's query outside the harness's two call sites, in every form.
expect_fail "a named import of query" sessions/q.ts "import { query } from $SDK
query(opts)"
expect_fail "a named import across lines" sessions/q.ts "import {
  type Options,
  query,
} from $SDK
query(
  {
  })"
expect_fail "a renamed import" sessions/q.ts "import { query as q } from $SDK
q(opts)"
expect_fail "a namespace import" sessions/q.ts "import * as sdk from $SDK
sdk.query(opts)"
expect_fail "a dynamic import" sessions/q.ts "const { query } = await import($SDK)"

# What is not a second turn path.
expect_pass "a type-only import" sessions/ok.ts "import type { Options } from $SDK"
expect_pass "an import without query" tools/ok.ts "import { createSdkMcpServer, tool } from $SDK"
expect_pass "a line comment naming session_mode" sessions/ok.ts "// the session_mode setting is gone"
expect_pass "a block comment naming sessionModeOf" sessions/ok.ts "/**
 * sessionModeOf was here
 */"
expect_pass "the harness's own query" harness/run.ts "import { query } from $SDK
query(opts)"
expect_pass "testConnection's query" harness/testConnection.ts "import { query } from $SDK
query(opts)"
expect_pass "a .sql migration naming session_mode" db/migrations/x.sql "ALTER TABLE ai_sessions DROP COLUMN session_mode;"

exit "$fail"
