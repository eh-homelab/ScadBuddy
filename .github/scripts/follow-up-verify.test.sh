#!/usr/bin/env bash
# Tests for follow-up-verify.sh — whether a claude-make-follow-up-issues run
# actually delivered (#1020).
#
# The load-bearing case is #973's run: the agent step ended `success` with 8
# permission denials, filed nothing and posted nothing, and the workflow went
# green. That must fail. A stub `gh` serves the PR's comments and the issues the
# summary names, so both checks run without touching GitHub.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/follow-up-verify.sh"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

RUN_ID=4242

# The stub answers the two calls the script makes: the PR's comment list, and
# one issue by number. An issue with no fixture file is a 404.
mkdir -p "$work/bin" "$work/issues"
cat > "$work/bin/gh" <<EOF
#!/usr/bin/env bash
case "\$*" in
  *"/issues/973/comments"*) cat "$work/comments.json" ;;
  *"/issues/"*)
    n="\${2##*/issues/}"
    if [ -f "$work/issues/\$n.json" ]; then
      cat "$work/issues/\$n.json"
    else
      echo "gh: Not Found (HTTP 404)" >&2; exit 1
    fi ;;
  *) echo "unexpected gh call: \$*" >&2; exit 1 ;;
esac
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
    '[{type: "system"}, {type: "result", subtype: "success", is_error: $e,
      permission_denials: [range($d) | {tool_name: "Bash",
        tool_input: {command: "gh issue create --body \"$(cat <<EOF ...)\""}}]}]' > "$path"
  printf '%s' "$path"
}

# comments <json objects...> -> writes the PR comment list
comments() {
  printf '[%s]' "$(IFS=,; echo "$*")" > "$work/comments.json"
}

comment() { # login, body
  jq -n --arg l "$1" --arg b "$2" '{id: 1, user: {login: $l}, created_at: "2026-10-02T01:56:00Z", body: $b}'
}

marker() { printf '<!-- claude-follow-up: run=%s issues=%s -->' "$1" "$2"; }

issue() { # number, state, title, labels(csv)
  jq -n --argjson n "$1" --arg s "$2" --arg t "$3" --arg l "$4" \
    '{number: $n, state: $s, title: $t, labels: ($l | split(",") | map({name: .}))}' \
    > "$work/issues/$1.json"
}

# Returns "<exit>:<named the denied call yes|no>".
run() {
  local status named
  : > "$work/output"
  set +e
  env PATH="$work/bin:$PATH" GITHUB_OUTPUT="$work/output" GH_TOKEN=stub \
    REPO=owner/repo PR_NUMBER=973 RUN_ID="$RUN_ID" EXECUTION_FILE="$1" STEP_OUTCOME="$2" \
    bash "$script" > "$work/stdout" 2>&1
  status=$?
  set -e
  named=no
  grep -q 'permission denied: Bash: .*gh issue create' "$work/stdout" && named=yes
  printf '%s:%s' "$status" "$named"
}

# --- the bug: #973 ----------------------------------------------------------

# 8 denials, step `success`, no summary and no issue. Was green; must be red,
# and must say which calls were refused.
comments "$(comment 'claude[bot]' "## Review$(printf '\n')<!-- claude-review-sha: abc1234 -->")"
check "#973: denials, nothing posted -> fails and names the denied calls" \
  '1:yes' "$(run "$(execution_file false 8)" success)"
check '... and says no summary was posted' yes \
  "$(grep -q '::error::.*posted no follow-up summary' "$work/stdout" && echo yes || echo no)"

# A clean run that simply forgot to post is the same failure.
check 'no denials, nothing posted -> fails' '1:no' "$(run "$(execution_file false 0)" success)"

# --- the passing paths ------------------------------------------------------

comments "$(comment 'claude[bot]' "## PR feedback follow-up
Blocking findings filed: 0
$(marker "$RUN_ID" none)")"
check 'nothing to file, summary posted -> passes' '0:no' "$(run "$(execution_file false 0)" success)"

# #957's shape: denials the agent recovered from, issue filed, summary posted.
issue 1006 open '[PR #973 feedback] client.py token validation' 'pr-feedback'
issue 1007 open '[PR #973 feedback] size bound' 'bug,pr-feedback'
comments "$(comment 'claude[bot]' "summary $(marker "$RUN_ID" 1006,1007)")"
check 'recovered denials, issues filed and summarised -> passes, denials still named' \
  '0:yes' "$(run "$(execution_file false 7)" success)"
check 'the denial count is an output' 'denials=7' "$(grep '^denials=' "$work/output")"

# --- the summary must be this run's, by claude ------------------------------

comments "$(comment 'claude[bot]' "older run $(marker 1111 none)")"
check "a previous run's summary is not this run's" '1:no' "$(run "$(execution_file false 0)" success)"

comments "$(comment 'someone' "forged $(marker "$RUN_ID" none)")"
check "a summary not posted by claude[bot] does not count" '1:no' \
  "$(run "$(execution_file false 0)" success)"

# --- every issue the summary claims must exist as filed ---------------------

comments "$(comment 'claude[bot]' "summary $(marker "$RUN_ID" 1006,9999)")"
check 'a claimed issue that does not exist -> fails' '1:no' "$(run "$(execution_file false 0)" success)"

issue 1008 closed '[PR #973 feedback] closed one' 'pr-feedback'
comments "$(comment 'claude[bot]' "summary $(marker "$RUN_ID" 1008)")"
check 'a claimed issue that is closed -> fails' '1:no' "$(run "$(execution_file false 0)" success)"

issue 1009 open '[PR #973 feedback] unlabelled' 'bug'
comments "$(comment 'claude[bot]' "summary $(marker "$RUN_ID" 1009)")"
check 'a claimed issue without pr-feedback -> fails' '1:no' "$(run "$(execution_file false 0)" success)"

issue 1010 open '[PR #12 feedback] another PR' 'pr-feedback'
comments "$(comment 'claude[bot]' "summary $(marker "$RUN_ID" 1010)")"
check "a claimed issue for another PR -> fails" '1:no' "$(run "$(execution_file false 0)" success)"

# --- the agent step itself failing ------------------------------------------

comments "$(comment 'claude[bot]' "summary $(marker "$RUN_ID" none)")"
check 'an aborted agent fails even with a summary' '1:no' "$(run "$(execution_file true 0)" success)"
# A job-timeout kill leaves no execution log; the step outcome is all there is.
check 'a failed agent step with no log fails' '1:no' "$(run '' failure)"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
