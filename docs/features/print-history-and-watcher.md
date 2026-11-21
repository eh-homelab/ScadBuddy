# Prints, history and the print watcher

## What it does

The Prints page shows what is printing now, the queue and the history, with each print's
photos, videos and timelapse. A print ScadBuddy did not start can be linked to a model
by hand (**Import print**), and the print watcher does it with no user step for prints
seen on Bambuddy or on LAN-only printers. Print again re-runs a past print. See README
["The print watcher"](../../README.md#the-print-watcher-4699).

## How it works

History reads Bambuddy's archives plus ScadBuddy's own run records. The watcher is a
housekeeping sweep (`housekeeping_auto_import_prints`) that matches new archives to
outputs; a LAN job start nudges it at once. LAN-only printers' jobs come from
`printer_reports` through the `LanAutoImport` workflow.

## Code

- UI: `frontend/src/pages/PrintsPage.tsx:38`, `PrintDetailPage.tsx`, `QueueItemPage.tsx`,
  `frontend/src/components/prints/` (NowPrinting, PrintQueue, PrintHistory,
  UnsortedImports, ImportPrintDialog), `frontend/src/components/PrintAgainDialog.tsx:30`.
- Routes: `backend/scadbuddy/api/print_history.py:109` (`/prints`, reprint `:1600`),
  `backend/scadbuddy/api/print_imports.py:83`, `backend/scadbuddy/api/print_attachments.py:49`,
  `backend/scadbuddy/api/farm.py:36`.
- Watcher: `backend/scadbuddy/print_watch/sweep.py`, `nudge.py`, `lan.py:298`;
  `backend/scadbuddy/workflows/lan_auto_import.py:80`.

## Data

- Tables: `print_runs`, `print_outcomes`, `print_imports`, `print_auto_imports`,
  `print_auto_import_failures`, `print_lan_imports`, `lan_print_archives`,
  `print_attachments`, `print_reprints`, `output_last_prints`.
- Settings: `print_auto_import`, `print_auto_import_sources`,
  `print_auto_import_skip_printers`.

## Tests

- Backend: `backend/tests/test_print_watch*.py`, `backend/tests/api/test_print_progress.py`.
- e2e: `frontend/e2e/print-history.spec.ts`, `print-detail.spec.ts`,
  `import-print.spec.ts`, `output-rename.spec.ts`.

## Limits and known issues

- Matching a print to an item compares colour alpha too
  ([#6316](https://github.com/eh-homelab/ScadBuddy/issues/6316)).
