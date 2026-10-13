---
name: shepherding-prs
description: Use when working through ScadBuddy issues end to end, from claiming an issue to its PR merging, or when asked to "babysit", "shepherd", "keep going through the issues", "handle the pr-feedback", or get open PRs merged. Also when a claude-review gate is red, a PR fell out of the merge queue, or several agents might pick up the same issue.
---

# Shepherding PRs

## Overview

Every change goes issue → claim → branch → PR → green CI → `claude-review` →
merge queue → claim released. `shepherd.sh` automates the tail end. The rest is
the protocol that stops parallel agents duplicating work and keeps the gate honest.

## Claim before you start

1. Skip an issue that already has an owner:
   - a later comment saying "Working on this" (a marker in the issue *body* is
     provenance only);
   - an assignee;
   - an open or recent PR or branch that names it:
     `gh pr list --state all --search "<N> in:body"`, `git branch -r | grep <N>`.
2. Claim it with a comment:
   `Working on this. <!-- claude-agent-owner: <session-id> -->`
3. Once the PR is open, edit that same comment to
   `Working on this: #<PR> <!-- claude-agent-owner: <session-id> -->`.
   `release-claim.sh` turns it into "... (merged) released" once the PR merges.
   If you stop without a PR, append "released" yourself.

The session id is the one in your `Claude-Session:` trailer (`session_01…`).

## Branch and PR

- Work in one worktree per PR, cut from fresh `origin/main`. Never `git stash`,
  never amend, never push to `main`. Rebase onto `origin/main` before pushing;
  `--force-with-lease` is for your own branches only.
- Each behaviour fix needs a test you have watched fail without it.
- The PR body ends with the `Closes #N` lines, then
  `<!-- claude-agent-owner: <session-id> -->` and the Claude Code footer.
  The shepherd finds PRs by that marker.
- Stack dependent PRs with `gh stack` (`init`/`add`, then `submit --auto`). The
  shepherd leaves PRs whose base isn't `main` alone.

## Run the shepherd

```bash
bash .claude/skills/shepherding-prs/shepherd.sh --owner <session-id> --hours 4
```

It only acts on PRs authored by the authenticated `gh` user (`--author` to
override) from branches of the repo itself, and only rewrites that user's own
claim comments: the marker is plain text anyone can paste.

Run it in the background; its log is the status. Each pass it:
- enqueues green, reviewed PRs with `gh pr merge --auto` and no merge strategy: the
  queue's own setting picks it ("When targeting a branch that requires a merge
  queue, no merge strategy is required", `gh pr merge --help`, gh 2.45);
- releases the claims of merged PRs;
- logs a failure or conflict once per head SHA and keeps going.

Restart it when it stops after `--hours`. Its first pass also releases the claims of
PRs that merged while it was stopped (the last 50 merged PRs carrying the marker). If
that list cannot be read, it logs "merged PRs unread" and the sweep waits for the next
restart. A sweep releases only claims that name the merged PR, so a later claim of a
reopened issue stays live.

## When the log says FAILING or CONFLICTS

| Symptom | Do |
|---|---|
| A test job is red | Read the log: `gh api repos/<o>/<r>/actions/jobs/<id>/logs`. Fix a real failure. For a known flake (CLAUDE.md "Known flakes"), `gh run rerun <run> --failed` once the run has finished. |
| `claude-review` is red with findings | Only `## Blocking` gates. Fix each finding in code, or answer it on the PR with evidence when it is wrong. Non-blocking findings never gate. |
| `claude-review` is red, `num_turns: 1`, `total_cost_usd: 0`, or "session limit" | The review never ran. Re-dispatch `claude-code-review.yml` for the PR. |
| The review was skipped because the workflow file is stale | Rebase onto `main`, then re-dispatch the review. |
| A check is `CANCELLED` | Usually a `concurrency: cancel-in-progress` run replaced by a newer one on the same SHA; FAILING clears once that run lands. If nothing replaces it, re-run it. |
| CONFLICTS | Rebase, resolve, re-run the checks you touched, then lease-push. |
| Green and reviewed, but not in the queue | It was dropped (a conflict or a queue failure). The next pass enqueues it again. |

## Common mistakes

- Starting on an issue without checking its later comments, so two agents fix it twice.
- Treating a red `claude-review` as findings when the review errored before it ran.
- Merging past a Blocking finding. Fix it, or track it in an open `pr-feedback` issue; never ignore it.
- Leaving a claim on an issue you gave up on.
