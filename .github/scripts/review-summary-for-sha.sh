#!/usr/bin/env bash
# Find the review summary comment written FOR one specific commit.
#
# Why this exists (eh-homelab/ScadBuddy#123): the merge gate used to classify
# "the most recent `claude[bot]` review summary" with nothing tying that summary
# to the commit being gated. When a review run completed but posted no comment —
# which happened on two of three runs on PR #115 — the classifier silently graded
# the PREVIOUS head's review. That failed both ways:
#
#   * closed, blocking a correct PR on findings about deleted code; and
#   * OPEN, merging `b7b1a1c` (a `main` merge plus a squashed PR, ~2000 lines)
#     on a review that had never seen any of it.
#
# The second is why this is keyed on the SHA and not on a timestamp. The stale
# summary there was NEWER than the previous push and still stale: it post-dated
# the commit it reviewed and pre-dated the one being gated. Only an explicit
# commit identity distinguishes those, so the review stamps its comment with
#
#     <!-- claude-review-sha: <40-hex> -->
#
# and this reads it back. A summary with no marker, or one carrying a different
# SHA, is not a review of this commit and does not count as one.
#
# Usage:
#   review-summary-for-sha.sh <sha> [comments.json]
#
# Reads the GitHub issue-comments payload from the file or stdin. Prints the id
# of the newest matching comment and exits 0; prints nothing and exits 1 when
# there is none. Kept free of `gh` so both branches are testable offline —
# the caller does the fetching.
set -euo pipefail

sha="${1:-}"
source="${2:--}"

if [ -z "$sha" ]; then
  echo "usage: review-summary-for-sha.sh <sha> [comments.json]" >&2
  exit 2
fi

# A short SHA in the marker still identifies the commit, so match on a prefix
# rather than demanding the caller and the reviewer agree on length. Anchored at
# both ends of the marker so `abc123` cannot match a DIFFERENT commit that merely
# starts the same way round the wrong way: the marker's value must be a prefix of
# the gated SHA, never the reverse.
found="$(
  jq -r --arg sha "$sha" '
    [ .[]
      | select(.user.login == "claude[bot]")
      | . as $c
      # `capture` RAISES on no match, which inside a select would abort the whole
      # program rather than skip one comment — so a comment with no marker (every
      # ordinary PR comment) has to be caught, not filtered afterwards.
      | ( try ( $c.body
                | capture("<!--[[:space:]]*claude-review-sha:[[:space:]]*(?<marked>[0-9a-fA-F]{7,40})[[:space:]]*-->")
                | .marked )
          catch empty ) as $marked
      # A short marker still identifies the commit, so match on a prefix. The
      # direction matters: the marker must be a prefix OF the gated sha, never the
      # reverse, or a 7-char gated sha would accept a marker for another commit.
      | select($sha | ascii_downcase | startswith($marked | ascii_downcase))
      | { id: $c.id, created_at: $c.created_at }
    ]
    | sort_by(.created_at)
    | last
    | if . == null then empty else .id end
  ' "$source"
)"

# jq exits 0 having printed nothing when no comment matched. Turn that into a
# non-zero exit, so a caller reads it as "no review for this commit" rather than
# as an empty-but-successful answer — the exact confusion #123 was about.
if [ -z "$found" ]; then
  exit 1
fi
printf '%s\n' "$found"
