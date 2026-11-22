# MakerWorld import

## What it does

Paste a MakerWorld model link and import its 3MF into the print library, ready to print
through the print dialog.

## How it works

ScadBuddy holds no MakerWorld credential: the download goes through Bambuddy's own
MakerWorld routes with the Bambuddy key. On the native library the file is then copied
into ScadBuddy's library (identical bytes are skipped). The import is the
`makerworld_import` operation on `library`, retried on a 504.

## Code

- Routes: `backend/scadbuddy/api/makerworld.py` (`/makerworld/status` `:48`, `/resolve`
  `:68`, `/import` `:126`).
- Operation: `backend/scadbuddy/makerworld/operations.py` (`KIND` `:33`),
  `backend/scadbuddy/makerworld/imports.py:35`.
- Agent tools: `agent/src/tools/makerworld.ts`.
- Background: [`docs/makerworld/`](../makerworld/makerlab-protocol.md).

## Data

- The print library (`library_files` or Bambuddy's); no table of its own.

## Tests

- Backend: `backend/tests/api/test_makerworld.py`, `test_makerworld_fixtures.py`.

## Limits and known issues

- Depends on Bambuddy's MakerWorld support and its key; there is no direct client.
