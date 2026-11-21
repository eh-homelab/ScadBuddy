# Print library, search, copies and batches

## What it does

Browse and print files already in the print library (Bambuddy's, or ScadBuddy's own
"native" library with a 30-day trash) through the same print dialog: a copy is laid out
for the nozzle side, spools are chosen and the copy is filed in the project's folder.
Search the library, copy files between Bambuddy's library and the native one, print
several files as a batch, and mount the library on a desktop with `bambuddy-fs`. User
guide: [Library](../user-guide.md#library).

## How it works

The stored setting `library_backend` picks Bambuddy's library or the native one
(`library_folders`, `library_files`, bytes in the blob store). Both answer through one read
protocol (`backend/scadbuddy/print_library/reads.py:1-4`). Printing a library file is a
`PrintRun` like any other ([workflows.md](../architecture/workflows.md#print-to-the-h2c)).
Copies between libraries run as the `LibraryCopy` workflow on `library`.

## Code

- UI: `frontend/src/pages/LibraryPage.tsx`, `frontend/src/components/library/`,
  `frontend/src/components/BatchPrintDialog.tsx:65`.
- Routes: `backend/scadbuddy/api/library_print.py:194` (`/print/library`: search `:369`,
  run `:1346`, progress `:1473`, upload `:677`), `backend/scadbuddy/api/library_copy.py`,
  `backend/scadbuddy/api/print_batches.py:52`.
- Native library: `backend/scadbuddy/print_library/native.py`, `store.py`, `copies.py:145`,
  `sweep.py`; workers read it through `print_library/remote.py`.
- Search: `backend/scadbuddy/library_search/index.py:51` (an in-memory snapshot of
  Bambuddy's library, fresh for 30 s), `backend/scadbuddy/bambuddy/library_listing.py:183`.
- Copies: `backend/scadbuddy/print_library/copy.py`, `backend/scadbuddy/workflows/library_copy.py:68`.
- Desktop mount: `tools/go/bambuddy-fs` ([README](../../tools/go/bambuddy-fs/README.md)).

## Data

- Tables: `library_folders`, `library_files`, `library_copies`, `library_copy_files`,
  `copy_leases`, `print_batches`, `print_batch_items`.
- Blob keys: `library-file-<id>`, `library-copy-<hash>`, `library-<id>`.
- Settings: `library_backend`.

## Tests

- Backend: `backend/tests/print_library/`, `backend/tests/api/test_print_library.py`,
  `test_library_cop*.py`, `test_print_batches.py`, `backend/tests/bambuddy/test_library_search.py`,
  `backend/tests/test_library_copy_replay.py`.
- e2e: `frontend/e2e/library.spec.ts`.
- `bambuddy-fs`: its Go tests and the live mounts in the `image` CI job
  (`.github/scripts/bambuddy-fs-live.sh`).

## Limits and known issues

- Copying the blob store between backends from Settings:
  [#4083](https://github.com/eh-homelab/ScadBuddy/issues/4083),
  [#3820](https://github.com/eh-homelab/ScadBuddy/issues/3820).
