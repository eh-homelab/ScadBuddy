# Filament invoice import (#6267) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person drops an invoice (PDF or image) on Filament → Invoices, presses
"Import with the assistant", and an assistant session reads it, applies the intake rules
(diff before create, tax-inclusive cost, Bambu colour codes) and writes the draft; the person
checks it on the Invoices page or in the chat and confirms it.

**Architecture:** The draft, its file and its confirm exist since #6266
(`api/filament_purchasing.py`). This plan adds what reads the file and what starts the
session. The agent gets an `invoice_file` reference kind, so a session sees the stored PDF or
image as a document or image block, fetched from the blob store at the Messages API
boundary; nothing is base64 in a prompt. The agent gets one route,
`POST /api/v1/ai/filament/invoice-import`, that starts a session with the import prompt and
records the session id as the invoice's `import_run_id`. Every session is already a Temporal
workflow, so a stopped import resumes with its session. The backend gets two read helpers the
rules need: the Bambu colour code table (read from the Bambu Studio profiles the image already
ships) and the per-line spool candidates of the diff rule. A new plugin skill carries the
rules; the print skill stops saying Bambuddy owns spools.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, TypeScript agent (`defineTool`, Claude
Agent SDK), React + TanStack Query.

**Spec:** `docs/superpowers/specs/2026-11-14-filament-management-design.md` §9, §5.8, §12 row
#6267. Plan of #6266 and its rulings: `docs/superpowers/plans/2026-11-17-filament-suppliers.md`.
The intake skill: `.claude/skills/filament-intake/SKILL.md`, `scripts/intake.py`.

## Global Constraints

- Database rule (#5295): only the CRUD service has SQL. The agent writes the draft only
  through the existing `/api/v1/filament/*` routes. No new pool or grant.
- Images and PDFs move as blobs and `{name, mediaType}` pointers, never base64 in text or
  JSON (`lint-base64-images.sh`, #2479). A block is built from the blob only at the Messages
  API boundary that `lint-base64-images.sh` allows; a new boundary needs its allowlist entry
  and a reason.
- Every tool works in every mode (classic and durable).
- Every new public `/api/v1` route needs `summary=`, `description=` and an agent tool or a
  `NOT_A_TOOL` entry (`agent/src/tools/coverage.ts`); every agent write tool is classified in
  `agent/src/sessions/touched.ts`.
- Plugin skills cite their sources (`lint-plugin.sh`); every external fact gets a link.
- Migrations: none expected. If one is needed, one new timestamped file (#491).
- Load `frontend-design` for every UI change. Mocks never call `crypto.randomUUID`
  (`frontend/src/mocks/newId.ts`).
- Commands: backend `cd backend && uv run --frozen pytest <files> -n 4` with
  `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/scadbuddy_test_6267`;
  ruff, ruff format, mypy. Agent and frontend `pnpm typecheck && pnpm lint && pnpm test`;
  Playwright `--workers=2`.

## Decisions this plan makes

1. **The profile-fetch worker is a separate issue.** It needs a new image with Chromium, a
   new service caller, a 1Password item and a clusters deployment: a PR of its own. It is filed
   as #6625, a sub-issue of #6260, blocked by #6267 and blocking #6270. Until it lands, the session
   proposes a non-Bambu type with its 3DFP id and hex and says the presets are still to fetch.
2. **The import is an assistant session, not a `ProjectWorkflow` flow.** Every session runs
   as a Temporal workflow already (`agent/src/tools/sessions.ts`), so it resumes when stopped,
   which is what spec §9 step 2 asks of "a flow". Flow inputs take no blob; a session can read
   the invoice through the new reference. `import_run_id` is the session id.
3. **The invoice reaches the model in the import session's first user message**, as an
   image block (PNG/JPEG/GIF/WebP) or, for a PDF, a `document` block, built only at the SDK
   boundary that already builds image blocks (`agent/src/sessions/images.ts` `userPrompt`, on
   `lint-base64-images.sh`'s allowlist). The session's stored attachments accept
   `application/pdf` beside the image types (the same blob store, the same size cap rules);
   any other type is refused with a clear message. A durable session keeps that message in its
   history, so a resumed import still has the invoice. (Revised before Task 2, 2026-11-18: no
   PDF library exists in the backend or the agent, and a tool result cannot show a PDF to the
   model; the Messages API takes a PDF as a document block, which also covers a scan with no
   text layer.) The `GET /invoices/{id}/file` route stays `NOT_A_TOOL`, its reason updated.
4. **The start route lives in the agent**, `POST /api/v1/ai/filament/invoice-import`
   `{invoice_id}` → `{session_id}`. It refuses a confirmed invoice (409), an invoice without a
   file (422), and an invoice whose `import_run_id` names a live session (409 with that id, so
   the UI opens it). It writes `import_run_id` through the backend's invoice PATCH (or the
   field the #6266 routes accept; the implementer adds it to `InvoicePatch` if absent).
5. **Colour codes:** `GET /api/v1/filament/bambu-colour-codes?code=` reads
   `filaments_color_codes.json` from `settings.slicer_profiles_dir/BBL/filament/` (copied into
   the image by the `slicer-dist` stage, `Dockerfile` "slicer-profiles"), cached per process,
   and answers `{code, name, rgba[], fila_type, fila_id}`. A missing file is 503 naming the path.
   Translucent colours keep alpha `80` as the table gives it.
6. **Diff before create is a backend read:** `GET /api/v1/filament/invoices/{id}/candidates`
   returns, per line, the open spools with no cost whose effective material and RGBA match the
   line's type (or proposed type) exactly, and those with a cost. The session decides with the
   skill's rules and asks the person when a line has more candidates than it can place.
7. **The skill:** `plugins/scadbuddy/skills/filament/SKILL.md` takes over the intake rules for
   SB (import, types, instances, tags, ledger); `.claude/skills/filament-intake/SKILL.md` keeps
   its BB-era steps but its first line points to the new skill for ScadBuddy work. The print
   skill's line 8 and the sentences after it say SB owns spools.

## Review Focus

1. A PDF with no text layer (a scan) still reaches the model as a document block.
2. A second "Import" press while a session runs opens that session, never a second one.
3. A colour code that is not in the table (`GFA17`, missing per the skill) gives a 404 the
   session can explain, not a 500.
4. A line whose type is still only proposed matches candidates by the proposed material and
   RGBA.
5. An invoice whose file is a HEIC or a text file is refused at import with a message the
   person understands.

---

### Task 1: Colour codes and line candidates (backend)

**Files:** Create `backend/scadbuddy/filament/colour_codes.py`; modify
`backend/scadbuddy/api/filament_purchasing.py` (or `api/filament.py`), `filament/invoices.py`
(`candidates`), models. Test: `backend/tests/filament/test_colour_codes.py` (a small fixture
JSON under `backend/tests/fixtures/`), `backend/tests/api/test_filament_purchasing.py`.

**Interfaces:** `ColourCodes.lookup(code: str) -> ColourCode | None`;
`InvoiceStore.candidates(invoice_id) -> list[LineCandidates]` with, per line, `position`,
`uncosted: list[SpoolBrief]`, `costed: list[SpoolBrief]`.

- [ ] Tests first: `test_a_code_gives_its_rgba_and_name`, `test_a_translucent_keeps_alpha_80`,
  `test_an_unknown_code_is_404` (Review Focus 3), `test_a_missing_table_is_503`,
  `test_candidates_split_uncosted_and_costed`, `test_a_proposed_type_matches_by_material_and_rgba`
  (Review Focus 4). Run → FAIL; implement; run → PASS.
- [ ] Commit `feat(filament): Bambu colour codes and invoice line candidates (#6267)`.

### Task 2: A session's first message can carry a PDF; the read tools

**Files:** `agent/src/sessions/images.ts` (and the attachment store and schemas it uses: the
media types and the size cap), `agent/src/tools/filamentPurchasing.ts`
(`filament_bambu_colour`, `filament_invoice_candidates`), `agent/src/tools/coverage.ts`,
`agent/src/sessions/touched.ts` if needed. Tests beside each.

**Interfaces:** `userPrompt(text, attachments)` builds an image block for an image and a
`document` block (`source: { type: 'base64', media_type: 'application/pdf', data }`) for a
PDF; the attachment schema accepts `application/pdf`. Task 3 uses this to start the import.

- [ ] Tests first: a PDF attachment becomes a document block in the SDK user message and
  nowhere earlier; an image stays an image block; a text or HEIC file is refused (Review
  Focus 5); a PDF over the cap is refused; each tool's route and input schema. Run → FAIL;
  implement; run → PASS (`pnpm test`, `pnpm typecheck`, `pnpm lint`,
  `bash .github/scripts/lint-base64-images.sh`).
- [ ] Commit `feat(agent): a session's first message can carry an invoice PDF (#6267)`.

### Task 3: Start the import

**Files:** `agent/src/routes/` (the new route), an import prompt module, backend
`InvoicePatch.import_run_id` if absent. Tests: agent route tests (backend mocked), backend
route test for the PATCH field.

- [ ] Tests first for Decision 4: starts a session whose prompt names the invoice id and the
  skill, writes `import_run_id`; a second call while that session lives answers 409 with its id
  (Review Focus 2); confirmed 409; no file 422. Run → FAIL; implement; run → PASS.
- [ ] Commit `feat(agent): start an invoice import session (#6267)`.

### Task 4: UI

Load `frontend-design` first. **Files:** `frontend/src/pages/filament/InvoicePage.tsx`,
`InvoiceUpload.tsx`, an API hook, mocks. An "Import with the assistant" button on a draft with
a file: it starts the import and opens the session in the assistant panel; a draft with a live
import shows "Import running" with a link to it. Tests: vitest beside; one Playwright case in
`e2e/filament-purchasing.spec.ts`.

- [ ] Tests, run → FAIL, implement, run → PASS.
- [ ] Commit `feat(frontend): import an invoice with the assistant (#6267)`.

### Task 5: Skills and docs

**Files:** `plugins/scadbuddy/skills/filament/SKILL.md` (new; frontmatter `name`,
`description`; cites the spec §9 and the colour table path), `plugins/scadbuddy/skills/print/SKILL.md`
(line 8 and the sentences after), `.claude/skills/filament-intake/SKILL.md` (first line points
to the new skill), spec §9 "as built in #6267" note for Decisions 1-7, `backend/CLAUDE.md` /
`agent/CLAUDE.md` layout lines, this plan's "Decisions made while building (#6267)".

- [ ] `bash .github/scripts/lint-plugin.sh` passes; the plugin version bump the hook makes is kept.
- [ ] Commit `docs(filament): the filament skill and the import as built (#6267)`.

## Decisions made while building (#6267)
