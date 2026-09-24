#!/usr/bin/env bash
# Tests for review-summary-for-sha.sh — the merge gate's fail-closed check (#123).
#
# Both paths are proved here, because #123 was a bug where only one of them was
# ever exercised in anger: a stale summary must NOT be accepted for the commit
# being gated, and a fresh one must still be found.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/review-summary-for-sha.sh"

GATED=b7b1a1c0000000000000000000000000000000ab
STALE=d9d82630000000000000000000000000000000cd

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

# Returns "<exit>:<stdout>" so a test can assert on both at once.
run() {
  local sha="$1" json="$2" out status
  set +e
  out="$(printf '%s' "$json" | "$script" "$sha" -)"
  status=$?
  set -e
  printf '%s:%s' "$status" "$out"
}

comment() { # id, login, created_at, body
  printf '{"id":%s,"user":{"login":"%s"},"created_at":"%s","body":%s}' \
    "$1" "$2" "$3" "$(printf '%s' "$4" | jq -Rs .)"
}

marker() { printf '<!-- claude-review-sha: %s -->' "$1"; }

# --- the bug itself ---------------------------------------------------------

# PR #115 exactly: a real review of the PREVIOUS head, and nothing for this one.
# The gate graded this and passed, merging ~2000 unreviewed lines.
stale_only="[$(comment 5809355005 'claude[bot]' '2026-09-24T06:59:33Z' "# Review$(marker "$STALE")")]"
check 'a summary for an earlier commit is not a summary for this one' \
  '1:' "$(run "$GATED" "$stale_only")"

# The same payload, asked about the commit it IS for.
check 'that same summary is found for its own commit' \
  '0:5809355005' "$(run "$STALE" "$stale_only")"

# --- the ordinary passing path ----------------------------------------------

fresh="[$(comment 1 'claude[bot]' '2026-09-24T06:59:33Z' "# Review$(marker "$STALE")"),
        $(comment 2 'claude[bot]' '2026-09-24T07:11:00Z' "# Review$(marker "$GATED")")]"
check 'the summary for this commit is found among older ones' \
  '0:2' "$(run "$GATED" "$fresh")"

# --- things that must not be mistaken for a review --------------------------

check 'no comments at all fails closed' '1:' "$(run "$GATED" '[]')"

unmarked="[$(comment 3 'claude[bot]' '2026-09-24T07:11:00Z' '# Review with no marker')]"
check 'an unmarked bot comment does not count' '1:' "$(run "$GATED" "$unmarked")"

# A human quoting the marker (this issue's own thread does exactly that) must not
# satisfy the gate.
human="[$(comment 4 'ElanHasson' '2026-09-24T07:11:00Z' "see $(marker "$GATED")")]"
check 'a human comment carrying the marker does not count' '1:' "$(run "$GATED" "$human")"

other="[$(comment 5 'claude[bot]' '2026-09-24T07:11:00Z' "$(marker 0000000000000000000000000000000000000000)")]"
check 'a marker for an unrelated commit does not count' '1:' "$(run "$GATED" "$other")"

# --- shape of the marker ----------------------------------------------------

short="[$(comment 6 'claude[bot]' '2026-09-24T07:11:00Z' "$(marker b7b1a1c)")]"
check 'a short sha in the marker still identifies the commit' '0:6' "$(run "$GATED" "$short")"

# The reverse must NOT hold: a marker longer than the gated sha we were asked
# about would otherwise let one commit vouch for another.
check 'a gated short sha does not accept a longer marker' \
  '1:' "$(run b7b1a1c "[$(comment 7 'claude[bot]' '2026-09-24T07:11:00Z' "$(marker "$GATED")")]")"

mixed="[$(comment 8 'claude[bot]' '2026-09-24T07:11:00Z' "$(marker "$(printf '%s' "$GATED" | tr 'a-f' 'A-F')")")]"
check 'case does not matter in a sha' '0:8' "$(run "$GATED" "$mixed")"

# --- newest wins ------------------------------------------------------------

twice="[$(comment 9 'claude[bot]' '2026-09-24T07:00:00Z' "$(marker "$GATED")"),
        $(comment 10 'claude[bot]' '2026-09-24T07:30:00Z' "$(marker "$GATED")")]"
check 'the newest summary for this commit wins' '0:10' "$(run "$GATED" "$twice")"

# --- misuse -----------------------------------------------------------------

set +e
printf '[]' | "$script" >/dev/null 2>&1
usage=$?
set -e
check 'no sha argument is a usage error, not a verdict' '2' "$usage"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
