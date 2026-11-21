# Source editing and libraries

## What it does

Edit a model's `.scad` files in the browser (Monaco) with diagnostics from
`openscad-lsp`, add and delete files, and use OpenSCAD libraries (BOSL2 is bundled). New
source is parse-checked by OpenSCAD before it is saved. User guide:
[Writing a model](../user-guide.md#writing-a-model), [Includes](../user-guide.md#includes).

## How it works

The editor talks to the API over a WebSocket; the API runs `openscad-lsp` under a seccomp
filter that blocks every socket but `AF_UNIX`. Saving is a git commit.

## Code

- Pages: `frontend/src/pages/EditSourcePage.tsx`, `EditSourceFilePage.tsx`,
  `NewModelPage.tsx`; `frontend/src/components/SourceEditor.tsx:105`,
  `SourceWorkbench.tsx:63`, `SourceFileTabs.tsx`; LSP client
  `frontend/src/lib/languageClient.ts:34-36`.
- Routes: `backend/scadbuddy/api/lsp.py:74` (WebSocket), `backend/scadbuddy/api/model_files.py`,
  `backend/scadbuddy/api/libraries.py:71`.
- LSP bridge: `backend/scadbuddy/library/lsp.py:211`, `backend/scadbuddy/editor/nonet.py:3-6`.
- Libraries: the `libraries` Dockerfile stage (`Dockerfile:685`),
  `backend/scadbuddy/library/library_seed.py`.

## Data

- The models git repository; `library_leases`, `library_pin_holds`,
  `library_install_slots` for library checkouts on render workers.

## Tests

- Backend: `backend/tests/test_editor_nonet.py`, `backend/tests/api/test_lsp*.py`.
- e2e: `frontend/e2e/paste-source.spec.ts`, `customize.spec.ts`.

## Limits and known issues

- `openscad-lsp` and the parse check run in the API's pod, which holds a database
  secret; moving them out is [#5296](https://github.com/eh-homelab/ScadBuddy/issues/5296).
