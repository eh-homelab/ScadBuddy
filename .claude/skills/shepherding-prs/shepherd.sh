#!/usr/bin/env bash
# Shepherd a session's open PRs into the merge queue and release their issue claims.
#
#   bash shepherd.sh --owner session_01ABC [--repo eh-homelab/ScadBuddy] [--hours 4] [--every 180] [--once]
#
# Each pass: list open PRs whose body carries `claude-agent-owner: <owner>`, enqueue
# (`gh pr merge --auto`) every one whose base is the default branch, checks are green
# and claude-review is SUCCESS, and when one merges, append "released" to the owner's
# claim comment on every issue its body closes. A red check or a conflict is logged
# once per head SHA and never stops the loop: fix it and the next pass picks it up.
# Stacked PRs (base is another branch) are left to `gh stack submit --auto`.
# The marker is only text anyone can paste, so a PR is also required to be authored
# by the authenticated gh user (or --author) and to come from a branch of the repo
# itself, never a fork: otherwise anyone could get a PR enqueued by copying it.
# `--once` runs a single pass and exits (shepherd.test.sh, or a sweep by hand).
set -uo pipefail
owner="" author="" repo="eh-homelab/ScadBuddy" hours=4 every=180 once=""
while [ $# -gt 0 ]; do
  case "$1" in
    --owner) owner=$2; shift 2 ;;
    --repo) repo=$2; shift 2 ;;
    --author) author=$2; shift 2 ;;
    --hours) hours=$2; shift 2 ;;
    --every) every=$2; shift 2 ;;
    --once) once=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$owner" ] || { echo "--owner <session marker id> is required" >&2; exit 2; }
[ -n "$author" ] || author=$(gh api user -q .login)
here=$(cd "$(dirname "$0")" && pwd)
default=$(gh repo view "$repo" --json defaultBranchRef -q .defaultBranchRef.name)
declare -A tracked noted done_
mine=".[]|select(.isCrossRepository|not)|select(.body|contains(\"claude-agent-owner: $owner\"))|.number"
# PRs that merged while no shepherd ran (between --hours restarts): their claims are
# released on the first pass, as an open PR's are when it merges (#2570).
for pr in $(gh pr list -R "$repo" --state merged --limit 50 --author "$author" --json number,body,isCrossRepository \
    -q "$mine" 2>/dev/null); do
  tracked[$pr]=1
done
end=$(( $(date +%s) + hours * 3600 ))
while [ "$(date +%s)" -lt "$end" ]; do
  for pr in $(gh pr list -R "$repo" --state open --limit 100 --author "$author" --json number,body,isCrossRepository \
      -q "$mine" 2>/dev/null); do
    tracked[$pr]=1
  done
  queued=" $(gh api graphql -f query="{repository(owner:\"${repo%/*}\",name:\"${repo#*/}\"){mergeQueue(branch:\"$default\"){entries(first:100){nodes{pullRequest{number}}}}}}" \
      -q '[.data.repository.mergeQueue.entries.nodes[].pullRequest.number]|map(tostring)|join(" ")' 2>/dev/null) "
  for pr in "${!tracked[@]}"; do
    [ -n "${done_[$pr]:-}" ] && continue
    j=$(gh pr view "$pr" -R "$repo" --json state,baseRefName,headRefOid,body,mergeStateStatus,statusCheckRollup 2>/dev/null) || continue
    state=$(jq -r .state <<<"$j") sha=$(jq -r .headRefOid <<<"$j")
    if [ "$state" = MERGED ]; then
      # Done only once every claim is released: a release that failed is logged and
      # tried again on the next pass (#2570).
      released=1
      for iss in $(jq -r .body <<<"$j" | grep -oiE '(closes|fixes|resolves) #[0-9]+' | grep -oE '[0-9]+'); do
        bash "$here/release-claim.sh" --repo "$repo" --owner "$owner" --author "$author" "$pr" "$iss" >/dev/null 2>&1 ||
          { released=""; echo "$(date +%H:%M) release failed #$pr #$iss"; }
      done
      [ -n "$released" ] && { done_[$pr]=1; echo "$(date +%H:%M) merged #$pr"; }
      continue
    fi
    [ "$state" = OPEN ] || { done_[$pr]=1; echo "$(date +%H:%M) #$pr $state"; continue; }
    [ "$(jq -r .baseRefName <<<"$j")" = "$default" ] || continue
    review=$(jq -r '[.statusCheckRollup[]|select((.context//.name)=="claude-review")|(.state//.conclusion)]|join(",")' <<<"$j")
    # Every conclusion that leaves a check failed, not only FAILURE (#2570).
    fail=$(jq -r '[.statusCheckRollup[]|select((.conclusion as $c|["FAILURE","TIMED_OUT","CANCELLED","STARTUP_FAILURE","ACTION_REQUIRED"]|index($c)) or .state=="FAILURE" or .state=="ERROR")|(.name//.context)]|join("|")' <<<"$j")
    running=$(jq -r '[.statusCheckRollup[]|select(.status=="IN_PROGRESS" or .status=="QUEUED" or .state=="PENDING")]|length' <<<"$j")
    if [ -n "$fail" ] && [ "$running" = 0 ]; then
      [ "${noted[$pr]:-}" = "$sha" ] || { noted[$pr]=$sha; echo "$(date +%H:%M) FAILING #$pr ${sha:0:9} ($fail)"; }
      continue
    fi
    if [ "$(jq -r .mergeStateStatus <<<"$j")" = DIRTY ]; then
      [ "${noted[$pr]:-}" = "dirty$sha" ] || { noted[$pr]="dirty$sha"; echo "$(date +%H:%M) CONFLICTS #$pr: rebase it"; }
      continue
    fi
    if [ "$review" = SUCCESS ] && [ "$running" = 0 ] && [[ "$queued" != *" $pr "* ]]; then
      gh pr merge "$pr" -R "$repo" --auto >/dev/null 2>&1 && echo "$(date +%H:%M) enqueued #$pr"
    fi
  done
  [ -n "$once" ] && exit 0
  sleep "$every"
done
echo "$(date +%H:%M) shepherd stopped after ${hours}h; restart it if PRs are still open"
