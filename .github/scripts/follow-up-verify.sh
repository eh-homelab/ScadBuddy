#!/usr/bin/env bash
# Body of pr-follow-up-issues.yml's "Verify the follow-up run delivered" step:
# decide from the RESULT, not from the agent step's exit status, whether the
# run did its job (#1020).
#
# On PR #973 the agent step ended `success` with 8 permission denials, filed no
# issue and posted no summary, and the workflow went green. The merge gate
# counts open `pr-feedback` issues as "addressed", so a green follow-up run that
# filed nothing reads as "the findings are tracked" when nothing is.
#
# The run's deliverable is a summary comment, by claude[bot], ending in
#
#     <!-- claude-follow-up: run=<RUN_ID> issues=<n,n,...|none> -->
#
# This passes only when the agent step succeeded, that comment exists for THIS
# run, and every issue it names is open, labelled `pr-feedback` and titled for
# this PR. A permission denial on its own is not a failure — #957's run hit 7,
# recovered and filed its issue — but each one is named in the log, so a
# missing tool shows up as the call that was refused, not as a bare count.
#
# Inputs, all from the environment: REPO, PR_NUMBER, RUN_ID, EXECUTION_FILE,
# STEP_OUTCOME (the agent step's outcome), GITHUB_OUTPUT, GH_TOKEN.
# Outputs: is_error, denials.
#
# Lives here rather than inline in the workflow so the rule is tested
# (follow-up-verify.test.sh): a regression is invisible on a green run.
set -uo pipefail

is_error=false
denials=0

if [ -n "${EXECUTION_FILE:-}" ] && [ -s "${EXECUTION_FILE}" ]; then
  result="$(jq -c '[.[] | select(.type == "result")] | last // empty' "$EXECUTION_FILE" 2>/dev/null || true)"
  if [ -n "$result" ]; then
    is_error="$(printf '%s' "$result" | jq -r '.is_error // false')"
    denials="$(printf '%s' "$result" | jq -r '(.permission_denials // []) | length')"
    echo "is_error=${is_error} permission_denials=${denials}"
    printf '%s' "$result" | jq -r '(.permission_denials // [])[]
      | "::warning::permission denied: \(.tool_name // "?"): \(.tool_input | tostring | .[0:300])"' 2>/dev/null || true
  else
    echo "::warning::execution log has no result message."
  fi
else
  echo "::warning::no execution log at '${EXECUTION_FILE:-<unset>}'."
fi

{
  echo "is_error=$is_error"
  echo "denials=$denials"
} >> "$GITHUB_OUTPUT"

if [ "${STEP_OUTCOME:-}" != "success" ] || [ "$is_error" = "true" ]; then
  echo "::error::The follow-up agent did not complete (outcome=${STEP_OUTCOME:-<unset>} is_error=${is_error} denials=${denials}). Re-apply the claude-make-follow-up-issues label to re-run it."
  exit 1
fi

if ! comments="$(gh api --paginate "repos/${REPO}/issues/${PR_NUMBER}/comments" | jq -s 'add // []')"; then
  echo "::error::Could not list PR #${PR_NUMBER}'s comments, so the run's summary could not be checked."
  exit 1
fi

# Newest claude[bot] comment carrying this run's marker; prints its issue list.
issues="$(printf '%s' "$comments" | jq -r --arg run "$RUN_ID" '
  [ .[]
    | select(.user.login == "claude[bot]")
    | . as $c
    | ( try ( $c.body
              | capture("<!--[[:space:]]*claude-follow-up:[[:space:]]*run=(?<run>[0-9]+)[[:space:]]+issues=(?<issues>[0-9,]+|none)[[:space:]]*-->")
            ) catch empty )
    | select(.run == $run)
    | { created_at: $c.created_at, issues }
  ]
  | sort_by(.created_at) | last | .issues // empty')"

if [ -z "$issues" ]; then
  echo "::error::The follow-up run posted no follow-up summary for run ${RUN_ID}, so there is no record of what it filed. With ${denials} permission denial(s) above, a refused call is the likely cause."
  exit 1
fi

if [ "$issues" = "none" ]; then
  echo "Summary posted; it reports no outstanding Blocking findings to file."
  exit 0
fi

bad=0
IFS=',' read -r -a numbers <<< "$issues"
for n in "${numbers[@]}"; do
  [ -n "$n" ] || continue
  if ! issue="$(gh api "repos/${REPO}/issues/${n}" 2>/dev/null)"; then
    echo "::error::The summary names #${n}, which does not exist."
    bad=1
    continue
  fi
  verdict="$(printf '%s' "$issue" | jq -r --arg pr "$PR_NUMBER" '
    if (.state | ascii_downcase) != "open" then "is \(.state), not open"
    elif ([.labels[].name] | index("pr-feedback")) == null then "has no pr-feedback label"
    elif (.title | startswith("[PR #\($pr) feedback]")) | not then "is not titled for PR #\($pr): \(.title)"
    else "ok" end')"
  if [ "$verdict" = "ok" ]; then
    echo "#${n}: open pr-feedback issue for PR #${PR_NUMBER}."
  else
    echo "::error::The summary names #${n}, which ${verdict}."
    bad=1
  fi
done

exit "$bad"
