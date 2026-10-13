#!/usr/bin/env bash
# Mark an issue's claim released once its PR merged:
#   bash release-claim.sh --owner session_01ABC [--repo eh-homelab/ScadBuddy] PR ISSUE
# Rewrites the owner's "Working on this" comment on ISSUE to
# "Working on this: #PR (merged) released", keeping the owner marker. Only a
# comment written by the authenticated gh user (or --author) counts: the marker is
# text anyone can paste, and someone else's comment must never be rewritten.
set -euo pipefail
owner="" author="" repo="eh-homelab/ScadBuddy"
while [ $# -gt 2 ]; do
  case "$1" in
    --owner) owner=$2; shift 2 ;;
    --repo) repo=$2; shift 2 ;;
    --author) author=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$owner" ] && [ $# -eq 2 ] || { echo "usage: release-claim.sh --owner ID [--author LOGIN] [--repo R] PR ISSUE" >&2; exit 2; }
pr=$1 iss=$2
[ -n "$author" ] || author=$(gh api user -q .login)
# The owner's claims for THIS PR, oldest first, each with whether it is released
# already: one naming #PR, or the bare pre-PR "Working on this." made before the PR
# merged. A claim naming another PR is a later re-claim of the issue and stays live,
# even when a restarted shepherd sweeps an old merged PR again (#2571).
merged=$(gh api "repos/$repo/pulls/$pr" --jq '.merged_at // ""')
claims=$(gh api "repos/$repo/issues/$iss/comments" --paginate \
  --jq ".[]|select(.user.login==\"$author\")|select(.body|contains(\"claude-agent-owner: $owner\"))|select((.body|test(\"^Working on this[^\\\\n]*#$pr([^0-9]|$)\")) or ((.body|startswith(\"Working on this.\")) and (\"$merged\" == \"\" or .created_at <= \"$merged\")))|\"\\(.id) \\(.body|contains(\"released\"))\"")
[ -n "$claims" ] || { echo "no claim by $owner for #$pr on #$iss"; exit 0; }
# The newest one not yet released: a session that released a claim and claimed the
# issue again for the same PR has both, and the old one must not hide the live one.
id=$(awk '$2 == "false" { id = $1 } END { print id }' <<<"$claims")
[ -n "$id" ] || { echo "#$iss already released"; exit 0; }
gh api -X PATCH "repos/$repo/issues/comments/$id" \
  -f body="Working on this: #$pr (merged) released <!-- claude-agent-owner: $owner -->" --jq .html_url
