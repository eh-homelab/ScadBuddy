#!/usr/bin/env bash
# Mark an issue's claim released once its PR merged:
#   bash release-claim.sh --owner session_01ABC [--repo eh-homelab/ScadBuddy] PR ISSUE
# Rewrites the owner's "Working on this" comment on ISSUE to
# "Working on this: #PR (merged) released", keeping the owner marker.
set -euo pipefail
owner="" repo="eh-homelab/ScadBuddy"
while [ $# -gt 2 ]; do
  case "$1" in
    --owner) owner=$2; shift 2 ;;
    --repo) repo=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$owner" ] && [ $# -eq 2 ] || { echo "usage: release-claim.sh --owner ID [--repo R] PR ISSUE" >&2; exit 2; }
pr=$1 iss=$2
id=$(gh api "repos/$repo/issues/$iss/comments" --paginate \
  --jq ".[]|select(.body|contains(\"claude-agent-owner: $owner\"))|select(.body|startswith(\"Working on this\"))|.id" | head -1)
[ -n "$id" ] || { echo "no claim by $owner on #$iss"; exit 0; }
case "$(gh api "repos/$repo/issues/comments/$id" --jq .body)" in *released*) echo "#$iss already released"; exit 0 ;; esac
gh api -X PATCH "repos/$repo/issues/comments/$id" \
  -f body="Working on this: #$pr (merged) released <!-- claude-agent-owner: $owner -->" --jq .html_url
