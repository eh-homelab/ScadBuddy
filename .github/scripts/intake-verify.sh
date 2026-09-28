#!/usr/bin/env bash
# Body of issue-intake.yml's "Verify intake completed" step: read the agent's
# verdict out of claude-code-action's execution log, and stamp
# failed:issue-intake on the issue only when intake really failed.
#
# Inputs, all from the environment: REPO, ISSUE_NUMBER, EXECUTION_FILE,
# STEP_OUTCOME (the agent step's outcome), GITHUB_OUTPUT, GH_TOKEN.
# Outputs: is_error, denials.
#
# A real failure is the agent aborting (is_error) or the agent step itself not
# succeeding. A tool permission denial the agent recovered from is NOT one: it
# gets a warning and the uploaded transcript, never the label. Labelling on
# denials branded runs that finished intake in full as failed (a denied
# `mcp__github__list_sub_issues` did it on nearly every run), so
# `gh issue list --label failed:issue-intake` stopped meaning "needs
# re-driving". Same rule as forge#3219.
#
# Lives here rather than inline in the workflow so the rule is tested
# (intake-verify.test.sh): a regression is invisible on a green run.
set -uo pipefail

is_error=false
denials=0

if [ -n "${EXECUTION_FILE:-}" ] && [ -s "${EXECUTION_FILE}" ]; then
  result="$(jq -c '[.[] | select(.type == "result")] | last // empty' "$EXECUTION_FILE" 2>/dev/null || true)"
  if [ -n "$result" ]; then
    is_error="$(printf '%s' "$result" | jq -r '.is_error // false')"
    denials="$(printf '%s' "$result" | jq -r '(.permission_denials // []) | length')"
    echo "is_error=${is_error} permission_denials=${denials}"
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

if [ "$denials" != "0" ]; then
  echo "::warning::Hit ${denials} permission denial(s) — a tool is likely missing from --allowedTools. Not treated as a failure; the uploaded execution transcript names the denied calls."
fi

if [ "${STEP_OUTCOME:-}" = "success" ] && [ "$is_error" != "true" ]; then
  echo "Intake completed without errors."
  exit 0
fi

echo "::error::Intake did not complete (outcome=${STEP_OUTCOME:-<unset>} is_error=${is_error} denials=${denials})."
gh label create "failed:issue-intake" --repo "$REPO" --color b60205 \
  --description "The issue-intake agent aborted on this issue — it needs re-driving" >/dev/null 2>&1 || true
gh issue edit "$ISSUE_NUMBER" --repo "$REPO" --add-label "failed:issue-intake" \
  || echo "::warning::Failed to apply failed:issue-intake."
exit 1
