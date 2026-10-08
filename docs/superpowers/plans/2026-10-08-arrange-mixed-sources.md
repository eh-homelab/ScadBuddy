# Arrange from mixed sources (#1864) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One Arrange dialog, agent tool and route over a mix of model outputs and Bambuddy library files, from any template.

**Architecture:** `POST /outputs/arrange` already packs outputs across templates. Each object may name a
`library_file_id` instead of an `output_id`; the route resolves it through `output_bambuddy_uploads`
(#455) to the output ScadBuddy uploaded as that file and arranges through its manifest. A file no
output recorded is refused with code `library_file_not_arrangeable` until #1863. The Library listing
carries each file's `output_id`, so the page knows up front. `slug` on the request picks the template
the result is filed under (one of the sources'; default the first object's). The frontend dialog takes
a list of sources; the agent gets an `arrange` tool that polls the job and saves the output.

**Tech Stack:** FastAPI + psycopg (backend), React 19 + msw/vitest (frontend), Hono + zod (agent).

**Spec:** GitHub issue #1864; Arrange is spec `2026-09-27-template-pipelines-design.md` §7.

## Global Constraints

- Generated API files (`openapi.json`, `schema.d.ts`) are never committed.
- Every `/api/v1` operation has an agent tool or a `coverage.ts` entry.
- Plain library files (no output behind them) are #1863: shown as not arrangeable, never block the rest.

## Review Focus

- A library file recorded by two outputs: the most recently recorded upload wins.
- The same output picked twice (as itself and as a library file): its objects are listed once.
- `slug` naming a template none of the objects come from: 422.
- An object with `part` omitted expands to every manifest entry; the 2000-copy cap is checked after expansion.
- A mix where one library file is plain: the dialog arranges the rest and says which file was left out.

---

### Task 1: Backend — resolve library files, optional part/count, filing slug (PR 1)

**Files:** `backend/scadbuddy/bambuddy/uploads.py` (`outputs_for_files(ids) -> dict[int, str]`),
`backend/scadbuddy/api/outputs.py` (`ArrangeObject`, `ArrangeRequest.slug`, `arrange_inputs`, route),
`backend/scadbuddy/bambuddy/library_listing.py` + `api/library_print.py` (`LibraryEntry.output_id`),
tests `backend/tests/api/test_arrange_api.py`, `backend/tests/test_bambuddy_uploads.py`.

- [ ] Failing tests: mixed arrange of two templates plus a library file (route, real Postgres uploads store);
      plain library file is 422 `library_file_not_arrangeable` naming the ids; `slug` picks the template
      and a foreign one is 422; part omitted expands; listing carries `output_id`.
- [ ] Implement; ruff, ruff format, mypy, pytest.
- [ ] Commit `feat(arrange): library files and a filing template in POST /outputs/arrange`.

### Task 2: Agent — `arrange` tool (PR 1)

**Files:** `agent/src/tools/arrange.ts`, `agent/src/tools/index.ts`, `agent/src/tools/coverage.ts`
(drop the arrange entry, reword backfill's), `agent/test/` tool test.

- [ ] Failing test: tool posts the mixed sources, polls `GET /jobs/{id}`, saves with
      `POST /models/{slug}/outputs` under the job's slug; a 409 `needs_backfill` is a ToolError naming the outputs.
- [ ] Implement with the print tools' `slotChoice` filament plan shape; lint, typecheck, test, build.
- [ ] Commit `feat(agent): arrange tool over outputs and library files`. Open PR 1 (`Part of #1864`).

### Task 3: Frontend — sources dialog, Library page, History (PR 2)

**Files:** `frontend/src/lib/arrange.ts` (`ArrangeSource`, `runArrange` saves under `job.slug`),
`frontend/src/components/ArrangeDialog.tsx` (sources, Add files, Add from a model, filing template),
`frontend/src/pages/LibraryPage.tsx` (checkbox per printable file, Arrange selected (N)),
`frontend/src/pages/HistoryPage.tsx`, msw handlers, tests.

- [ ] Failing vitest: mixed arrange (two templates + a generated library file + a plain one) sends the
      right objects and `slug`, plain file shown as not arrangeable yet; Library page selection opens the dialog.
- [ ] Implement; lint, typecheck, test, build.
- [ ] Commit `feat(ui): arrange library files and outputs from any template together`. Open PR 2 (`Fixes #1864`).
