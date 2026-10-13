#!/usr/bin/env bash
# Tests for shepherd.sh and release-claim.sh (#2570, #2571). A stub `gh` serves
# fixtures and applies the scripts' own --jq/-q filters to them with jq, so the real
# filters run; a PATCH is recorded instead of sent.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin" "$work/fx"
OWNER=session_test

cat > "$work/bin/gh" <<EOF
#!/usr/bin/env bash
fx="$work/fx"
filter=""
args=()
while [ \$# -gt 0 ]; do
  case "\$1" in
    -q|--jq) filter=\$2; shift 2 ;;
    *) args+=("\$1"); shift ;;
  esac
done
set -- "\${args[@]}"
serve() { if [ -n "\$filter" ]; then jq -r "\$filter" "\$1"; else cat "\$1"; fi; }
case "\$*" in
  "api user") echo me ;;
  "repo view"*) echo main ;;
  "api graphql"*) echo "" ;;
  "pr list"*"--state merged"*) serve "\$fx/merged.json" ;;
  "pr list"*"--state open"*) serve "\$fx/open.json" ;;
  "pr view "*) serve "\$fx/pr-\$3.json" ;;
  "pr merge"*) echo "\$*" >> "\$fx/merges" ;;
  "api -X PATCH "*)
    [ -f "\$fx/patch-fails" ] && { echo "gh: HTTP 502" >&2; exit 1; }
    echo "\$4" >> "\$fx/patches"; echo https://example.invalid ;;
  "api repos/"*"/issues/"*"/comments --paginate") n=\${2#*/issues/}; serve "\$fx/comments-\${n%%/*}.json" ;;
  *) echo "unexpected gh call: \$*" >&2; exit 1 ;;
esac
EOF
chmod +x "$work/bin/gh"
export PATH="$work/bin:$PATH"

pass=0 fail=0
check() {
  if [ "$2" = "$3" ]; then printf 'ok   %s\n' "$1"; pass=$((pass + 1))
  else printf 'FAIL %s\n       expected: %s\n       actual:   %s\n' "$1" "$2" "$3"; fail=$((fail + 1)); fi
}
reset() { rm -f "$work"/fx/*; echo '[]' > "$work/fx/open.json"; echo '[]' > "$work/fx/merged.json"; }
claim() { # id body
  jq -n --argjson id "$1" --arg body "$2" '{id: $id, user: {login: "me"}, body: $body}'
}
mark="<!-- claude-agent-owner: $OWNER -->"

# --- release-claim.sh (#2571) ---
reset
jq -s . <(claim 1 "Working on this: #10 (merged) released $mark") <(claim 2 "Working on this: #20 $mark") \
  > "$work/fx/comments-7.json"
bash "$here/release-claim.sh" --owner "$OWNER" 20 7 >/dev/null
check "a claim made again after a released one is the one released" \
  "repos/eh-homelab/ScadBuddy/issues/comments/2" "$(cat "$work/fx/patches" 2>/dev/null)"

reset
jq -s . <(claim 1 "Working on this: #10 (merged) released $mark") > "$work/fx/comments-7.json"
check "a claim released already is left alone" "#7 already released" \
  "$(bash "$here/release-claim.sh" --owner "$OWNER" 10 7)"
check "... with no PATCH" "" "$(cat "$work/fx/patches" 2>/dev/null)"

# --- shepherd.sh (#2570) ---
pr_json() { # number state body [rollup]
  jq -n --arg s "$2" --arg b "$3" --argjson r "${4:-[]}" \
    '{state: $s, baseRefName: "main", headRefOid: "abcdef1234567", body: $b, mergeStateStatus: "CLEAN", statusCheckRollup: $r}' \
    > "$work/fx/pr-$1.json"
}
listed() { jq -n --argjson n "$1" --arg b "Closes #$2 $mark" '[{number: $n, body: $b, isCrossRepository: false}]'; }

reset
listed 30 8 > "$work/fx/merged.json"
pr_json 30 MERGED "Closes #8 $mark"
jq -s . <(claim 5 "Working on this: #30 $mark") > "$work/fx/comments-8.json"
out=$(bash "$here/shepherd.sh" --owner "$OWNER" --once)
check "a PR merged while no shepherd ran has its claim released" \
  "repos/eh-homelab/ScadBuddy/issues/comments/5" "$(cat "$work/fx/patches" 2>/dev/null)"
check "... and is logged merged" "1" "$(grep -c 'merged #30' <<<"$out")"

reset
listed 31 9 > "$work/fx/open.json"
pr_json 31 MERGED "Closes #9 $mark"
jq -s . <(claim 6 "Working on this: #31 $mark") > "$work/fx/comments-9.json"
touch "$work/fx/patch-fails"
out=$(bash "$here/shepherd.sh" --owner "$OWNER" --once)
check "a failed release is logged" "1" "$(grep -c 'release failed #31 #9' <<<"$out")"
check "... and the PR is not reported done" "0" "$(grep -c 'merged #31' <<<"$out" || true)"

for conclusion in TIMED_OUT CANCELLED STARTUP_FAILURE ACTION_REQUIRED FAILURE; do
  reset
  listed 32 10 > "$work/fx/open.json"
  pr_json 32 OPEN "Closes #10 $mark" "[{\"name\": \"Backend\", \"status\": \"COMPLETED\", \"conclusion\": \"$conclusion\"}]"
  out=$(bash "$here/shepherd.sh" --owner "$OWNER" --once)
  check "a check concluding $conclusion is FAILING" "1" "$(grep -c 'FAILING #32' <<<"$out")"
  check "... and nothing is enqueued" "" "$(cat "$work/fx/merges" 2>/dev/null)"
done

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
