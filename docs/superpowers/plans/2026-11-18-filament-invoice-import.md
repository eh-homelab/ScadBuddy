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

1. Decisions 1-7 stand as written above. The owner's rule is to decide and not re-ask. The cost of a wrong call is rework of the start path.
2. "Uncosted" means the spool's own `cost_per_kg` is NULL and it has no invoice line. A type's cost is a default, not a purchase. The intake rule targets spools that an RFID tag auto-created. If this were wrong, a line would update a spool from an earlier purchase.
   - **Amended by #6700 (2026-11-20).** On prod, 105 of the 106 open spools have a `cost_per_kg` that the Bambuddy import copied, and none has an invoice line (read-only query on `scadbuddy-db-1`, 2026-11-20, in #6700). So no invoice could link to the spools it paid for. The owner's rulings:
     - R-6700-1: a line may fill a spool that has a cost but no `invoice_line_id`. Confirm replaces `cost_per_kg` with the line's allocated cost.
     - R-6700-2: confirm writes one `edited` ledger event per such spool, with `changes: {"cost_per_kg": [old, new]}`. The actor is the caller's, as the other filament writes do. (The event also carries `type_id` when the spool had no type, and the line's `ref_kind`/`ref_id`, as every confirm event does. Spools with no cost keep the `invoice:<id>` actor.)
     - R-6700-3: a spool that already has an `invoice_line_id` is still refused. So are archived spools and spools of another type, as before.
     - R-6700-4: in the import prompt and the skill, the assistant offers each costed candidate. It shows the old and new cost per kg, and links it only when the person says yes. Uncosted candidates come first, then costed ones. `candidates` now gives each spool its `invoice_line_id`, so the assistant can tell a fillable costed spool from one on another invoice.
     - R-6700-5: the invoice lines table marks a filled spool that has a cost: "Spool #N replaces $X/kg", in the page's money format.
     - R-6700-6: no migration. The change is in `_claimed`, `_confirm_line`, the candidates read and the spool list's fields only.
     - R-6700-7: the invoice page's "Fills existing spools" picker offers costed spools that have no invoice line, and never a spool that is already on an invoice line. Spools with no cost come first. Each costed spool in the picker shows "replaces $X/kg" too. The spool list (`GET /spools`, `Spool`) now gives each spool its `invoice_line_id`, which the picker reads; the candidates route was not used, because it matches by material and RGBA, not by the line's type as the picker and the confirm do.
3. The candidates read scans all spools on each call. The review asked for a change. We made none, because the inventory holds hundreds of spools. If this is wrong, the read is slow later.
4. Decision 3 changed before Task 2. The invoice goes in the import session's first user message, as an image or a PDF document block. It is built at the `sessions/images.ts` SDK boundary. No PDF library exists, and a tool result cannot show a PDF to the model. If this were wrong, Task 2 would need a redo.
5. A session PDF is served on the app origin with `Content-Disposition: attachment` and the CSP sandbox kept. This is the secure default. The sandbox blocks the browser's PDF viewer, so the chat cannot preview a PDF inline. The attachment header also keeps the file out of the Bambuddy frame.
6. The preview of a sent file is optional for a turn that the server builds. The agent has no preview maker. If this were wrong, a large invoice image would show full-size as its thumbnail.
7. The lightbox fallback text for an image with no preview says "its preview". We carried that minor into Task 4, because it is in the same chat files.
8. An empty PATCH `{}` returns the invoice unchanged. It does not bump `updated_at` and does not recompute. Nothing changed, so nothing needs a recompute. If this were wrong, an empty PATCH would no longer refresh `updated_at`.
9. A narrow gap stays open. If the send throws after a turn was queued, the archive of the session is refused and the refusal is logged. We accepted this, because it needs two failures in a row.
10. An idle import session is the assistant waiting for the person. The start route keeps opening it, and the UI says "waiting for you" with no pulse. The UI reads the status again every 15 s and on focus. If this were wrong, a second import could not start until the first one is archived.
11. A hidden tab keeps polling the status. The 15 s read costs little. If this were wrong, background tabs would send idle requests.
12. Open check for the final review: a connected panel must learn of a session that the REST start route created. Verify this on the real stack. This check is not done yet.
13. Final review, Important 1: the start route compares the invoice file's size with the cap for its type before it reads the file. It answers 422 with a plain sentence: the file is too large for the assistant to read, the caps in MB for a photo and for a PDF, and to upload a smaller photo or a PDF. The panel shows that sentence.
14. Final review, Minor 1: rule 2 of the filament skill is split into short sentences and bullets.
15. Final review, Minor 2: a PDF's attachment pointer carries the file's own name (`{name, mediaType, fileName}`, never the bytes). The chip shows that name, and "Invoice PDF" for a turn that has none.
16. Final review, Minor 3: `unsupported()` names the kind of file in plain words ("a HEIC photo", "a text file") with the right article. It says "this kind of file" for a kind it does not know.
17. Final review, Minor 4: a rate-limited or not-ready session start answers its own status (429 or 503) with a plain reason and `type: session_refused`. The panel shows that reason.
18. Final review, Minor 5: the invoice page does not read the session status when the assistant is off (no opener).
19. Final review, Minor 6: the filament skill says that SB does not write back to BB "yet", and points to #6265 (filament spec §8).
20. Final review, Minor 7: `filament_invoice_upsert` refuses `import_run_id` and `if_import_run_id` in `fields`, for an add and for an edit. Only the import route links a session. The backend PATCH stays as it is, because the start route and its tests send `import_run_id` with `if_import_run_id`, and the tool is the only model path to it.
21. A live import session is always the caller's: ScadBuddy's browser sessions are one person (#4975 D8), so the start route opens it. A rival that closed during the link race is never handed to the page (502).