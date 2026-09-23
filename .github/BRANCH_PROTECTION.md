# Branch protection for `main`

**Status: NOT YET APPLIED.** Apply it *after* the container/CI PR merges — see
[Why not yet](#why-not-yet).

## Why not yet

Branch protection makes `CI Summary` and `claude-review` required. Both are
introduced by the CI PR itself, so applying protection while other branches are
in flight would block every one of them behind checks their heads have never
run:

- A required check that has **never reported** on a SHA reads as *pending*, not
  as *passing*. A PR opened before the workflow existed can only clear it by
  rebasing onto a `main` that has it.
- `claude-review` is a **commit status** posted by `claude-code-review.yml`,
  which is `workflow_dispatch`-only. `workflow_dispatch` requires the workflow
  to exist **on the default branch**, so nothing can dispatch it until the CI PR
  is merged. Until then `ci.yml`'s `dispatch-review` job detects that case and
  posts `success` with a "bootstrap" description rather than leaving the context
  pending forever.

So: merge the CI PR, confirm one ordinary PR goes green end to end, then run the
commands below.

## Apply

```bash
REPO=eh-homelab/ScadBuddy

gh api -X PUT "repos/${REPO}/branches/main/protection" \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["CI Summary", "claude-review"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": false,
  "block_creations": false,
  "lock_branch": false,
  "allow_fork_syncing": false
}
JSON
```

Then the two repository-level settings, which are **not** part of branch
protection and have to be set separately:

```bash
# Auto-merge (so `gh pr merge --auto --squash` works) and branch cleanup.
gh api -X PATCH "repos/${REPO}" \
  -F allow_auto_merge=true \
  -F delete_branch_on_merge=true \
  -F allow_merge_commit=false \
  -F allow_rebase_merge=false \
  -F allow_squash_merge=true
```

`allow_merge_commit=false` is what actually enforces linear history at merge
time; `required_linear_history` above only rejects a merge commit pushed
directly to the branch.

## Verify

The only check that matters is what the PR's **status rollup** contains —
branch protection evaluates nothing else:

```bash
gh pr view <N> --repo "${REPO}" --json statusCheckRollup \
  --jq '.statusCheckRollup[] | {name: (.name // .context), state: (.conclusion // .state)}'
```

Both `CI Summary` and `claude-review` must appear in that output. If
`claude-review` is missing from the rollup but `gh api
repos/eh-homelab/ScadBuddy/commits/<sha>/check-runs` shows something named
`claude-review`, the gate is being satisfied by the wrong object: a check run
from a `workflow_dispatch` run belongs to a check suite that is **not
associated with the pull request**, so it never reaches the rollup no matter how
green it is. The merge gate must be the **commit status**
`claude-code-review.yml` posts, which is why that workflow's job is displayed as
"Claude Review Gate" and deliberately not named `claude-review` — one object per
context.

Read the current state back with:

```bash
gh api "repos/${REPO}/branches/main/protection" --jq '{
  contexts: .required_status_checks.contexts,
  strict: .required_status_checks.strict,
  linear: .required_linear_history.enabled,
  force_push: .allow_force_pushes.enabled
}'
gh api "repos/${REPO}" --jq '{auto_merge: .allow_auto_merge, delete_on_merge: .delete_branch_on_merge, squash_only: (.allow_squash_merge and (.allow_merge_commit | not))}'
```

## Notes

- `enforce_admins: false` is deliberate. The commit status is posted by a job
  that can itself be killed mid-run, which leaves the context `pending` —
  blocking, but recoverable, since statuses are last-write-wins per context and
  re-running `claude-code-review.yml` against the same branch overwrites it.
  An admin bypass is the escape hatch for the case where it is not.
- `required_approving_review_count: 0`: this is a single-maintainer repo and the
  Claude review gate is the review. Raise it if that changes.
- `strict: true` requires branches to be up to date before merging. With
  `gh pr merge --auto --squash` that is handled automatically; without it,
  expect to rebase.
