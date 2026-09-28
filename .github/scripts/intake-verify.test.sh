#!/usr/bin/env bash
# Tests for intake-verify.sh — when issue intake stamps failed:issue-intake.
#
# The load-bearing case is "recovered denials": a run that finished intake but
# hit a permission denial must NOT be labelled, or the label stops meaning
# "needs re-driving" (see intake-verify.sh). A stub `gh` records its arguments,
# so "was the issue labelled?" is observable without touching GitHub.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/intake-verify.sh"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/bin"
cat > "$work/bin/gh" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$work/gh-calls"
EOF
chmod +x "$work/bin/gh"

pass=0
fail=0

check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    printf 'ok   %s\n' "$name"
    pass=$((pass + 1))
  else
    printf 'FAIL %s\n       expected: %s\n       actual:   %s\n' "$name" "$expected" "$actual"
    fail=$((fail + 1))
  fi
}

# execution_file <is_error> <denial count> -> path
execution_file() {
  local path="$work/exec-$1-$2.json"
  jq -n --argjson e "$1" --argjson d "$2" \
    '[{type: "system"}, {type: "result", is_error: $e,
      permission_denials: [range($d) | {tool_name: "mcp__github__list_sub_issues"}]}]' > "$path"
  printf '%s' "$path"
}

# Returns "<exit>:<labelled yes|no>:<warned about denials yes|no>".
run() {
  local status labelled warned
  : > "$work/gh-calls"
  : > "$work/output"
  set +e
  env PATH="$work/bin:$PATH" GITHUB_OUTPUT="$work/output" GH_TOKEN=stub \
    REPO=owner/repo ISSUE_NUMBER=123 EXECUTION_FILE="$1" STEP_OUTCOME="$2" \
    bash "$script" > "$work/stdout" 2>&1
  status=$?
  set -e
  labelled=no
  grep -q -- '--add-label failed:issue-intake' "$work/gh-calls" && labelled=yes
  warned=no
  grep -q '::warning::Hit .* permission denial' "$work/stdout" && warned=yes
  printf '%s:%s:%s' "$status" "$labelled" "$warned"
}

# --- the bug: a finished run with recovered denials is not a failure --------

# Issue #365's run: intake done, one denied list_sub_issues, labelled anyway.
check 'recovered denials warn but do not label or fail' \
  '0:no:yes' "$(run "$(execution_file false 1)" success)"

check 'a clean run passes quietly' '0:no:no' "$(run "$(execution_file false 0)" success)"

# --- real failures stay loud ------------------------------------------------

check 'an aborted agent is labelled and fails' '1:yes:no' "$(run "$(execution_file true 0)" success)"
check 'an aborted agent with denials is labelled and fails' \
  '1:yes:yes' "$(run "$(execution_file true 2)" success)"
check 'the denial count is still an output' 'denials=2' "$(grep '^denials=' "$work/output")"

# A job-timeout kill leaves no execution log; the step outcome is all there is.
check 'a failed agent step with no log is labelled and fails' '1:yes:no' "$(run '' failure)"
check 'a cancelled agent step is labelled and fails' \
  '1:yes:no' "$(run "$(execution_file false 0)" cancelled)"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
