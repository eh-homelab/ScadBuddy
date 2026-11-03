#!/usr/bin/env bash
#
# Cases for the wait step in deploy.reusable.yml (step id `wait`), which lets
# an open clusters deploy PR land before a newer deploy replaces it (#5168:
# merges faster than the PR's checks closed every deploy PR, and prod stayed
# on one build for hours). It runs the step's own `run:` script, read out of
# the workflow with yq, against a fake `gh` on PATH, so the test cannot drift
# from the code it covers. No network.
#
# A scenario is one line per poll: `<pr> <armed> <mergeStateStatus> <failed>`,
# or `none` for no open deploy PR, `err` for a list that fails, or `<pr> gone`
# for a PR that merged between the list and the view; `<failed>` is how many
# workflow runs on its head failed, or `err` for a read that fails. The fake
# `gh` answers each `gh pr list` with the next line (the last one again once
# they run out), and `gh pr view` and `gh run list` with that line's fields.
#
# Runs in ci.yml's `lint` job. Needs mikefarah yq v4.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workflow="$here/../workflows/deploy.reusable.yml"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

yq '.jobs[].steps[] | select(.id == "wait") | .run' "$workflow" > "$work/wait.sh"
[ -s "$work/wait.sh" ] || { echo "FAIL: no step with id 'wait' in $workflow" >&2; exit 1; }

mkdir -p "$work/bin"
cat > "$work/bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> "$FAKE/calls"
line() {
  local n total
  n=$(cat "$FAKE/n"); total=$(wc -l < "$FAKE/scenario")
  sed -n "$(( n < total ? n : total ))p" "$FAKE/scenario"
}
case "$1 $2" in
  "pr list")
    echo $(( $(cat "$FAKE/n") + 1 )) > "$FAKE/n"
    read -r pr _ <<< "$(line)"
    [ "$pr" != err ] || { echo "HTTP 502" >&2; exit 1; }
    [ "${pr:-none}" = none ] || echo "$pr" ;;
  "pr view")
    read -r _ armed state _ <<< "$(line)"
    [ "$armed" != gone ] || { echo "no pull requests found" >&2; exit 1; }
    printf '%s\t%s\t%s\n' "$armed" "$state" "$(printf 'a%.0s' {1..40})" ;;
  "run list")
    [[ " $* " == *" --commit $(printf 'a%.0s' {1..40}) "* ]] || { echo "no head sha: $*" >> "$FAKE/bad"; exit 2; }
    read -r _ _ _ failed <<< "$(line)"
    if [ "$failed" = err ]; then echo "HTTP 502" >&2; exit 1; fi
    echo "$failed" ;;
  *) echo "unexpected: $*" >> "$FAKE/bad"; exit 2 ;;
esac
EOF
chmod +x "$work/bin/gh"

export DEPLOY_BRANCH=deploy/scadbuddy
export POLL_SECONDS=0
failures=0

fail() {
  echo "FAIL: $1" >&2
  failures=$((failures + 1))
}

# `run <name> <wait seconds> <expected outcome> <expected lists>` runs the step
# over the scenario on stdin; `any` skips the count.
run() {
  local name=$1 wait=$2 want=$3 lists=$4 case_dir="$work/$1"
  mkdir -p "$case_dir"
  cat > "$case_dir/scenario"
  echo 0 > "$case_dir/n"
  : > "$case_dir/calls"
  : > "$case_dir/out"
  if ! FAKE="$case_dir" WAIT_SECONDS="$wait" GITHUB_OUTPUT="$case_dir/out" PATH="$work/bin:$PATH" \
    bash "$work/wait.sh" > "$case_dir/log" 2>&1; then
    fail "$name: the step failed"; sed 's/^/  /' "$case_dir/log" >&2; return
  fi
  local got
  got=$(sed -n 's/^outcome=//p' "$case_dir/out")
  [ "$got" = "$want" ] || fail "$name: outcome '$got', want '$want'"
  local n
  n=$(grep -c '^pr list' "$case_dir/calls" || true)
  [ "$lists" = any ] || [ "$n" = "$lists" ] || fail "$name: listed $n times, want $lists"
  # The step tolerates a failed read, so a call the fake did not expect
  # would pass unseen without this.
  if [ -s "$case_dir/bad" ]; then
    fail "$name: the fake gh refused: $(cat "$case_dir/bad")"
  fi
  if grep -q '^pr close\|^pr merge\|^pr edit' "$case_dir/calls"; then
    fail "$name: the wait changed a PR"
  fi
}

run no-open-pr 3600 none 1 <<'EOF'
none
EOF

# The case #5168 is about: checks still running. The old step closed the PR
# at once; now it waits until the PR has merged.
run lands-after-checks 3600 none 4 <<'EOF'
42 true BLOCKED 0
42 true BLOCKED err
42 true CLEAN 0
none
EOF

# A transient failure of the list is retried, not a failed deploy.
run list-fails-once 3600 none 3 <<'EOF'
err
42 true BLOCKED 0
none
EOF

run merged-between-list-and-view 3600 none 2 <<'EOF'
42 gone
none
EOF

run checks-failed 3600 failed 2 <<'EOF'
42 true BLOCKED 0
42 true BLOCKED 1
EOF

run auto-merge-off 3600 unarmed 1 <<'EOF'
42 false BLOCKED 0
EOF

run conflict 3600 conflict 1 <<'EOF'
42 true DIRTY 0
EOF

run still-open-after-the-wait 1 timeout any <<'EOF'
42 true BLOCKED 0
EOF

# The deadline is checked before any read: the watch token expires an hour
# after it is minted, and a read past that would fail the step.
run no-read-past-the-deadline 0 timeout 0 <<'EOF'
42 true BLOCKED 0
EOF

if [ "$failures" -gt 0 ]; then
  echo "$failures case(s) failed" >&2
  exit 1
fi
echo "deploy-wait: all cases passed"
