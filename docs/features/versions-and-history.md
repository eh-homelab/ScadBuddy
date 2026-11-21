# Versions and history

## What it does

Every output keeps the parameters it was made with (the model's History page: edit,
print, send again, rename, delete, add to an order, sell). Every model change is a git
commit, so the Versions page can compare revisions, customize an old one, or restore it
(your own models only). User guide: [History and versions](../user-guide.md#history-and-versions).

## How it works

The models repository is git; versions are `git log`, `git show` and `git diff` over a
model's folder. Restore commits the old source as a new revision. An order line item pins
one commit.

## Code

- Pages: `frontend/src/pages/HistoryPage.tsx` (row actions `:933-1015`),
  `frontend/src/pages/VersionsPage.tsx` (compare `:231`, customize `:362`, restore
  `:273-291`).
- Routes: `backend/scadbuddy/api/versions.py:54` (`/models/{slug}/versions`, source,
  schema, diff, restore `:158-298`), `backend/scadbuddy/api/outputs.py:139`.
- Git: `backend/scadbuddy/library/history.py` (`restore` `:208`, `diff` `:462`),
  `backend/scadbuddy/core/gitrepo.py`.
- Off-box history: an optional push to a git remote, `backend/scadbuddy/git_remote/push.py:181`.

## Data

- The models git repository; outputs in `output_index`; pushes in `git_remote_pushes`.
- Settings: `SCADBUDDY_GIT_REMOTE_URL`, `_USERNAME`, `_TOKEN`.

## Tests

- Backend: `backend/tests/test_history.py`, `test_library_versioning.py`,
  `backend/tests/api/test_versions.py`, `backend/tests/test_git_remote.py`.
- e2e: `frontend/e2e/versions.spec.ts`, `output-rename.spec.ts`.

## Limits and known issues

- The user guide lists fewer row actions than the History page has (see
  [Drift from specs](../architecture/README.md#drift-from-specs)).
