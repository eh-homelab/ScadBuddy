# Filament UI and SB spools everywhere (#6262) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **UI work (Tasks 4, 5, 7, 9, 11, 13-20 touch the frontend):** the implementer loads `frontend-design:frontend-design` before writing any UI. Pass this line on to every UI subagent too.

**Size.** This is too big for one review, but the owner wants one PR per issue. So it is
one PR (`feat/filament-ui`) built as four stacked parts. Each part is a run of commits
that passes CI on its own: every backend shape change carries its frontend and agent
consumers, and their typecheck, in the same task (preflight F9). Review each part as a unit:

| Part | Tasks | What it leaves working |
|---|---|---|
| A. Backend reads and presets | 1-3 | New filament routes; no `('*','*')` presets |
| B. SB spools in every reader | 4-10 | Dialog, hot path, orders, agent inventory, colour widget read SB spools; #6316 |
| C. Retire the old surface | 11-13 | No native API, no overlay code (tables kept, unread); Settings' Filament section |
| D. The Filament section | 14-20 | `/filament`, spool edit page, picker RGBA rows, e2e, docs |

Part B changes what a plan's `spool_id` means (Task 7). Tasks 7 and 8 must land in the
same deploy. They are in the same PR, so they do.

**Goal:** ScadBuddy's own spools (`filament_*`) are the only inventory the print dialog,
the hot path, orders and the agent read, and a person manages them in a top-level
Filament section instead of Settings.

**Architecture:** One backend module (`filament/inventory.py`) answers "which SB spools
exist, and which tray holds each" from Postgres alone, and every reader uses it: the
dialog's options on both paths, the print worker (through one CRUD route), orders, the
customizer's colour widget and the agent's inventory route. The provider overlay
(`spools/` providers, `spool_provider_records`, `physical_spools`, `tray_spool_links`) and
the native API are removed from the code; their data is carried into the filament tables.
The old tables stay in the database, unread and unwritten; a follow-up PR after #6262
deploys drops them. The frontend gets `/filament` with tabs that read
`/api/v1/filament/*`, a spool edit page, and an RGBA swatch and colour field.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, pydantic v2, Temporal Python SDK,
Postgres 18; React 19, Vite, shadcn/ui, msw, vitest, Playwright; TypeScript agent tools
(zod, msw).

**Spec:** `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§5.1 colour,
§5.3 overrides, §5.4 tags, §5.5 ledger, §8 sync settings, §10 UI, §11 parity, §12 row
#6262, §13). Phase 1 plan: `docs/superpowers/plans/2026-11-14-filament-data-model.md`; its
"Decisions made while building (#6261)" bind this plan. Preflight scan:
`.superpowers/sdd/2026-11-15-filament-ui/preflight.md` (F1-F30). Issues: #6262, #6316
(folded in). Epic #6260.

## Global Constraints

- **Database access (#5295):** only the CRUD service has SQL. The print worker reads SB
  spools through a `crud_router("print")` route, area `filament`, with its service credential.
  No new pool, grant or cross-service table read. No untrusted code near a DB secret.
- **Migrations (#491):** new files only, `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`.
  Never edit a merged file. A data migration guards each row it rewrites (`ON CONFLICT DO
  NOTHING`, type guards), so it is a no-op elsewhere and never stops the API's start.
- **Generated API files are never committed** (`backend/openapi.json`, both `schema.d.ts`).
  Run `pnpm gen:api` in `frontend/` and `agent/` after every backend route change.
- **Each task is green alone:** a task that changes an API shape also changes its
  frontend and agent consumers and runs `cd frontend && pnpm gen:api && pnpm typecheck`
  and `cd agent && pnpm gen:api && pnpm typecheck` in its Step 4 (F9).
- **Each task fixes the notes it makes false** (`CLAUDE.md` files, docstrings, coverage
  reasons); a stale note is a Blocking review finding (root CLAUDE.md, F19).
- **Every route** sets `summary=` and `description=`. Every new public `/api/v1` operation
  gets an agent tool or a `NOT_A_TOOL` entry with a reason (`agent/src/tools/coverage.ts`,
  `agent/test/coverage.test.ts`). A removed operation leaves no stale `NOT_A_TOOL` entry.
- **Every tool works in every mode** (classic and durable).
- **Colour is `#RRGGBBAA`** in every new field. A `#RRGGBB` input reads as alpha `FF`
  (`filament/colour.py` `normalise_rgba`). A translucent spool never matches an opaque one
  by colour alone (spec §5.1); one helper decides it (`filament/colour.py` `same_opacity`).
- **Remembered choices (owner rule):** every print or quote option a person picks is a
  remembered choice behind the dialog's existing Remember toggle, never a hard-coded
  value. This PR adds no new print option; the SB spool per slot and "Wait for the right
  colour" stay in `ModelPrintChoices.filament_plan` and `force_colour_match`.
- **Hot path:** a native run touches Bambuddy nowhere. `e2e-stack/`'s recorder pass stays
  at zero calls (backend/CLAUDE.md, hot path).
- **Workflow changes** go behind `workflow.patched(...)`; `tests/test_spool_refresh_replay.py`
  and the print replay fixtures (`tests/fixtures/print_run_histories/`,
  `print_lan_sequence_histories/`) still pass. Recorded histories are never re-recorded.
- **Images and files are blobs** with `{name, mediaType}` pointers, never base64
  (`lint-base64-images.sh`). This PR adds no image path; CSV import sends parsed rows.
- **Citations (#259):** every external fact in code comments and docs gets a link; every
  measured fact says where and how it was measured. Bambuddy facts cite the 1.2.6b1
  OpenAPI measured in spec §3.
- **Frontend e2e:** msw-mocked Playwright against `pnpm preview`; new mocks go in
  `src/mocks/features/filament/` and export `handlers` (and `reset`); one route has one
  feature module (`mocks/features.test.ts`). A type error in a test fails all three
  `frontend-e2e` shards, so run `pnpm typecheck` first. Shared test helpers go in `src/test/`.
- **UI links** to ScadBuddy pages use `AppLink`/`appUrl` (host the user is on). The
  `embed.ts` new-tab rule is for Bambuddy deep links only.
- **Commands:** backend `cd backend && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test uv run --frozen pytest <files>`,
  then `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`;
  frontend `cd frontend && pnpm gen:api && pnpm lint && pnpm typecheck && pnpm exec vitest run <file>`,
  `pnpm exec playwright test e2e/<spec>`; agent `cd agent && pnpm gen:api && pnpm test <file> && pnpm typecheck`.

## Decisions this plan makes

Each with what it costs if wrong. Rulings decided during the build go to "Decisions made
while building (#6262)" at the end.

Design
1. **Hot-path replacement for the native provider:** `filament/inventory.py`
   `FilamentInventory.trays(printer_id, seen)` resolves each reported tray to an SB spool:
   an open tag binding by `tray_uuid`, then `tag_uid`; else, for an untagged tray, the
   spool placed at that tray whose `tray_fingerprint` still equals the tray's
   (`MATERIAL|#RRGGBBAA`). The print worker calls it through
   `POST /api/v1/internal/crud/print/filament/trays` (area `filament`). A LAN start's
   estimates and the settle move from `spools/native.py` to `filament/print_uses.py`
   (same ledger rows, same `ref_id` form), behind `…/filament/print-uses` CRUD routes with
   an `Idempotency-Key`. Cost: one more CRUD round trip per plan, as today.
2. **A plan's `spool_id` is the SB spool's `number`** on both paths; a tray chosen for
   itself keeps its negative id (`tray_spool_id`). `SpoolOption` gains `sb_spool_id`.
   Task 8 rewrites every stored Bambuddy spool id; an id with no link is dropped (the
   picker then auto-matches that slot). Cost: a remembered choice of a never-pulled BB
   spool is forgotten once.
3. **Bambuddy path:** the options are SB spools. Where each is loaded comes from the SB
   tray resolution over every registry printer's latest report, else Bambuddy's
   assignment of the linked BB spool (until #6263 captures loads). Presets: the SB
   effective preset for the printer model and nozzle, else the linked BB spool's
   `filament-presets` (one read per chosen spool, as today; dropped by #6265). Queueing is
   unchanged: Bambuddy still gets type and colour only (`queue_filaments`). Cost: a load
   Bambuddy knows and SB does not shows from BB until #6263.
4. **Presets:** every `('*','*')` row is expanded to one row per registry printer model ×
   nozzle `0.2, 0.4, 0.6, 0.8` (the H2C's nozzles, spec §9 step 7), marked `migrated`, then
   deleted, and a CHECK refuses `'*'`. Readers ask only for `(printer_model, nozzle_diameter)`.
   Cost: none at deploy (the same preset on every nozzle, as before).
5. **Native API retired, not redirected (owner ruling, 2026-11-15: no aliases).**
   `/api/v1/native-spools*` and the four tools `native_spool_list`, `native_spool_get`,
   `native_spool_upsert`, `native_spool_delete` are removed. Each has a `filament_*`
   equivalent, the native shape loses alpha (`#RRGGBB`, the defect §5.1 names), and a
   `NOT_A_TOOL` entry would describe a route that no longer exists. The PR body (Task 20)
   names them, so a saved flow's author knows what to change. Cost: a saved flow that
   calls one fails until its author edits it.
6. **Overlay code removed; tables kept (controller ruling on F30).** This PR carries
   `tray_spool_links` into SB tray places and `spool_record_links` (provider `bambuddy`)
   into `filament_bb_links`, then stops every read and write of `native_spools`,
   `native_spool_uses`, `physical_spools`, `spool_provider_records`, `spool_record_links`,
   `spool_provider_refreshes` and `tray_spool_links`. **Nothing in this PR drops them.**
   The DROP ships as a separate issue and PR after #6262 deploys (the controller files
   it). Cost: the tables take space for one more deploy; in return an old pod during the
   rollout or a rollback still finds them.
7. **Restore and tags:** a restore never rebinds a tag silently (anti-goal: a silent
   overwrite). The spool page shows the last tag the spool held (`last_tag`): "Bind tag
   again" when it is free, or "Tag is on spool #N — Move it here", which rebinds through
   the existing rebind rule (ledger events on both spools). Cost: one click per restore.
8. **Price book editor:** `FilamentsEditor` stays in `components/orders/` (three orders
   components import `filamentLabel` from it). Task 16 mounts it on the Types tab as the
   "Price book" panel and, in the same commit, takes it out of Settings → Orders, so the
   book always has one UI (F23). Pricing rates stay in Settings → Orders. Cost: none.
9. **Settings → Printing → Filament** holds what exists now: `filament_sync`, in spec
   §8's per-data-type shape (`FilamentSync`, one field per data type; today only
   `spools: "off" | "bb_to_sb"`; #6265 widens the values and adds the other data types,
   no settings migration then), the pull interval (the renamed
   `spool_overlay_refresh_seconds`, label "Pull from Bambuddy every"),
   `filament_low_stock_pct` (default 20, our call), `filament_default_cost_per_kg`
   (default none), and the currency read-only with a link to Orders (`orders_currency`).
   RFID override mode and weight-sync policy arrive with #6264 and #6268. Old anchors
   `#inventory` and `#native-spools` resolve to this section. Cost: none.
10. **Tabs shown now:** Spools, Types, Tags, Locations, Catalog, Ledger. Suppliers,
    Invoices, Metrics and Shopping list are in the tab registry with `available: false`
    until #6266/#6269 flip them. Cost: none.
11. **Spools tab scope (owner ruling, 2026-11-15):** table and cards views, filters,
    columns picker, group by, bulk archive/restore/delete/move/set type, summary tiles,
    CSV export **and CSV import** (Task 15). Labels with QR codes (§7) go to #6264. The
    forecast view (it needs the burn rate, §5.7) and the "consumed" tile go to #6269.
    Cost: two parity items wait one phase.
12. **Picker colour matching:** a slot's colour is opaque unless its source carries alpha
    (OpenSCAD's alpha byte is ignored, CLAUDE.md). `same_opacity(a, b)` is false when one
    side is translucent (`alpha < FF`) and the other opaque; `colours_match`, orders'
    `_of` and the frontend `seedPlan` all use it; a person can still pick any spool.
    Cost: none.
13. **Deploy skew:** the old CRUD routes (`/spools/overlay`, `/native-spools*`) are removed
    in this PR. A print worker on the old build gets 404, and its activity retries
    (`crud/client.py`) until the new build (AUTO_UPGRADE) takes it. Cost: a few minutes
    of retried plans during a rollout.
14. **The translucent 3D preview** (spec §5.1) is not in this PR: the controller files a
    new issue for it. Cost: none now.

Preflight rulings (`.superpowers/sdd/2026-11-15-filament-ui/preflight.md`)
- F1: `resolve` takes `Mapping[int, Sequence[EffectivePreset]]`; the BB fallback converts `SpoolFilamentPreset` to `EffectivePreset(overridden=False)` at the read; the worker route's body field is `numbers`. Cost: one adapter later.
- F2: Task 13 lists the summary code and `spools/activities.py`, with a test that `off` skips the pull. Cost: none.
- F3: `Spool` rows carry a server-computed `low_stock: bool`; the frontend filters on it. Cost: one extra field.
- F4: the ledger pages by an opaque `(at, seq)` cursor, never by time alone. Cost: if wrong, rows lost or repeated.
- F5: Task 3 also changes the native write to per-nozzle rows and the read to H2C/0.4. Cost: none; Task 11 deletes it.
- F6: the shelf is every unarchived spool not resolved as held by any printer's latest report; `link` first moves the tray's previous holder to no location; a `''` or `None` colour gives an empty colour part. Cost: if wrong, duplicate holders.
- F7: `report_status` moves to `printers/reports.py`; `SeenTray` and the key cleaners stay in `spools/models.py`; Task 12 deletes only the overlay half of `spools/`. Cost: none.
- F8: the carried fingerprint is `material || '|' || CASE WHEN part = '' THEN '' ELSE '#' || part || 'FF' END`, the same as `tray_fingerprint`. Cost: if wrong, one re-ask per tray.
- F9: each backend shape change carries its frontend consumer and its typecheck in the same task (controller ruling). Cost: if wrong, a part is not a green unit.
- F10: Task 5 owns the preset logic of `resolve_plates` on both paths; Task 7 only builds Bambuddy-path options. Cost: none.
- F11: one backend helper (`same_opacity`, on `is_translucent`) for Tasks 5 and 9; Task 5 changes `lib/filaments.ts` `seedPlan` with a parity test. Cost: if wrong, dialog and server open with different plans.
- F12: the other overlay readers (`bambuddy/choices.py`, `api/analyzers.py`, `api/accepted_fixes.py`, `api/library_print.py`, `api/settings.py` → `offered_spools`) move in Task 5; Task 12 drops `SpoolsDep` where only the overlay used it. Cost: none.
- F13: an in-flight LAN run across the upgrade decodes old `SpoolOption`s with no `sb_spool_id` and books no estimate for its remaining plates (the cost #6261 accepted); replay tests run in Tasks 5 and 6. Cost: a few grams unbooked until the next absolute reading.
- F14: Task 8 also rewrites `print_batch_items.request` and `refused_request` and pending `operations.request`; `FilamentPlan` gains `ids: Literal["bb", "sb"] = "bb"`, every new request sends `"sb"`, and a `"bb"` plan is mapped through `filament_bb_links` at read. Cost: if wrong, a queued run picks an unrelated SB spool.
- F15: Task 8's migration first carries `spool_record_links` (provider `bambuddy`) into `filament_bb_links`, then rewrites. Cost: if wrong, a remembered choice forgotten once.
- F16: the new models are `FilamentInventorySpool` and `FilamentSpoolSummary`; `farm.py`'s models go with its routes; `test_openapi.py` asserts no module-qualified schema names. Cost: none.
- F17: Task 9 moves the customizer's `FilamentColourPicker` to `GET /filament/inventory`. Cost: if wrong, the colour widget fails.
- F18: Task 12 removes the pull's `spool_record_links` branch (step 2) and its docstring line. Cost: none.
- F19: each task updates the notes it makes false; Task 20 adds only the Filament section's notes. Cost: none.
- F20: Task 12 changes `e2e-stack/seed.sh` to wait for the pull's result, not `spool_provider_refreshes`; Task 5 changes `real-hot-path.spec.ts` to the SB spool label (`#N`), still asserting zero recorder calls. Cost: if wrong, the recorder check cannot run.
- F21: `backend/tests/auth/test_revoke.py` belongs to Task 12; with no drop in this PR its table list is unchanged, and the drop PR updates it. Cost: none.
- F22: Task 13's link to `/filament` lands before the route (same PR, accepted); the tab ids follow spec §10's order. Cost: none.
- F23: `FilamentsEditor` is not moved (Decision 8). Cost: none.
- F24: CSV `location` resolves only to an existing unarchived location with a unique name (else a row error) and is applied through `LocationStore.move`; the import route takes an `Idempotency-Key` and the tool sends one; derived columns (`grams_left`, `last_weighed_*`, `last_used_at`, …) are export-only, ignored on import, and the preview says so. Cost: if wrong, duplicate spools after a retry.
- F25: `CsvRow` is an OpenAPI model; the frontend column list is `readonly (keyof CsvRow)[]` with a `satisfies` exhaustiveness check; the backend pins `COLUMNS == list(CsvRow.model_fields)`. No cross-tree read. Cost: none.
- F26: Task 7 rewrites the `NOT_A_TOOL` reason of `POST /print/printers/{id}/trays/{ams}/{tray}/spool`. Cost: none.
- F27: when a needs-type spool gets its first type (PATCH `type_id` from null), its `migrated` preset rows are deleted, with one `edited` event, so the type's per-nozzle presets show. Cost: if wrong, a migrated spool slices with its old preset on every nozzle until reset.
- F28: tasks name test files that exist (`tests/api/test_print.py`, `test_print_choices.py`, `test_print_destination_native.py`, `test_print_filaments.py`, `tests/orders/test_spools.py`, `agent/test/printers.test.ts`) or create the named ones. Cost: none.
- F29: Task 11's grep covers `backend/scadbuddy`, `agent/src` and `frontend/src` (F9 moves its frontend consumers into it); Task 13 runs it again; Task 11 does not edit files that name no native tool. Cost: none.
- F30 (as changed by the controller): the carry migrations stay in this PR with `ON CONFLICT DO NOTHING` on both keys, `WHERE record_id ~ '^[0-9]+$'`, and BB-only tray links resolved through the BB record link; the DROP leaves this plan (Decision 6). Cost: if wrong, one more small PR.
- T6: `CRUD_AREAS` holds area names; `api/crud_areas.py`'s docstring lists the new paths. The routes take a new area, `filament` (in `CRUD_AREAS["print"]` and `worker.PRINT_CRUD_AREAS`), so a new worker waits for an API that serves them (Task 6 review).

SpoolRefresh replay conditions (preflight §4; Task 12)
- `workflow.patched("spool-providers-gone")` is the **first** call in `run`, and its `not patched` branch keeps the exact old `LIST` → `PROVIDER`×n → `SEEN` sequence. Cost if wrong: a nondeterminism error on an open run.
- The old activities stay registered with results of the old types: `LIST` returns `[]`, `PROVIDER` returns the `str` `"skipped"`, `SEEN` returns `None`. Cost: none.
- `filament_sync.spools = "off"` is checked inside the pull activity, never in the workflow. Cost: none.
- Keep every recorded history and add one recorded after the patch (`SCADBUDDY_RECORD_HISTORIES=1`); never re-record the old ones. `deprecate_patch` and the no-op removal come in a later PR (runs last at most `RUN_TIMEOUT`, 15 min, so one deploy cycle is enough). Cost: none.

## Review Focus

1. A remembered plan, quote or queued batch that names a Bambuddy spool id with no SB
   link: after Task 8 the slot has no choice and auto-matches; it never names an
   unrelated SB spool whose `number` happens to equal the old id (Task 8 test).
2. An opaque and a translucent spool of one hue are both loaded: the picker suggests the
   opaque one for an RGB slot, an item print fills the right one, and the price-book
   import keeps two rows (Tasks 5, 9).
3. An untagged tray a person linked, then refilled with another colour: the link no
   longer counts, and the dialog asks about the tray again (Task 4).
4. A restored spool whose last tag is now on another spool: nothing moves on restore;
   "Move it here" moves it with `tag_unbound`/`tag_bound` events on both (Tasks 2, 17).
5. A spool overrides its type's colour, then the person resets it: PATCH `rgba: null`, the
   effective colour is the type's again, the "overridden" mark is gone, and one `edited`
   event records it (Task 17 test, backend rule already from #6261).

---

## File structure

```
backend/scadbuddy/migrations/
  <ts>_filament_presets_per_nozzle.sql      Task 3
  <ts>_filament_tray_place.sql              Task 4
  <ts>_filament_plan_spool_numbers.sql      Task 8 (carries BB record links first)
  <ts>_filament_insert_bb_catalog.sql       Task 10
  <ts>_filament_carry_tray_links.sql        Task 12 (no DROP in this PR)
backend/scadbuddy/filament/
  presets.py        effective presets and K values per spool       Task 1
  inventory.py      SB spools with where each is loaded; trays     Tasks 4, 7
  print_uses.py     a LAN run's estimates and settle (from native) Task 6
  csv_import.py     CSV rows to spools, with a dry run             Task 15
  colour.py         same_opacity                                   Task 5
  spools.py, models.py, ledger.py, bambuddy_pull.py (modify)       Tasks 1-3, 10, 12
backend/scadbuddy/printers/reports.py       report_status (from spools/seen.py) Task 4
backend/scadbuddy/api/filament.py           new read, preset, inventory, import routes
backend/scadbuddy/api/crud_print.py, crud/print.py  trays, presets, print-uses routes Task 6
backend/scadbuddy/api/tray_spools.py        tray links on SB spools     Task 4
backend/scadbuddy/bambuddy/filaments.py, trays.py, resolver.py, print_run.py, choices.py  Tasks 5, 7
backend/scadbuddy/orders/spools.py, spool_import.py, bambuddy.py, integrations.py  Task 9
deleted code: spools/native.py, api/native_spools.py (Task 11); spools/overlay.py,
  providers.py, spool_providers.py, seen.py, store.py's overlay half,
  bambuddy/spool_providers.py (Task 12)
agent/src/tools/filament.ts, traySpools.ts, farm.ts, coverage.ts; nativeSpools.ts deleted
frontend/src/pages/filament/                FilamentPage + one file per tab  Tasks 14-18
frontend/src/components/filament/           RgbaSwatch, RgbaColourField, …
frontend/src/api/filament.ts                typed client for /filament/*
frontend/src/mocks/features/filament/       msw handlers and seed data
frontend/src/pages/settings/sections/filament.tsx  config section   Task 13
frontend/e2e/filament.spec.ts                                        Task 20
```

---

## Part A — backend reads and presets

### Task 1: A spool's own presets and K values, and the effective set

**Files:**
- Create: `backend/scadbuddy/filament/presets.py`
- Modify: `backend/scadbuddy/filament/spools.py`, `filament/models.py`, `api/filament.py`, `agent/src/tools/filament.ts`, `agent/src/tools/coverage.ts`
- Test: `backend/tests/filament/test_presets.py`, `backend/tests/api/test_filament.py`, `agent/test/filament.test.ts`

**Interfaces:**
- Consumes: `filament_spool_presets`, `filament_spool_k_values`, the type tables (#6261); `SpoolPresetCreate`, `SpoolKValueCreate` (already in `models.py`).
- Produces in `models.py`: `EffectivePreset(printer_model: str, nozzle_diameter: str, slicer_filament: str, slicer_filament_name: str | None, overridden: bool)`; `EffectiveKValue(printer_id: UUID, extruder: int, nozzle_diameter: str, nozzle_type: str | None, k_value: Decimal, name: str | None, cali_idx: int | None, setting_id: str | None, overridden: bool)`.
- Produces in `presets.py`: `effective_presets(conn, spool_id: UUID) -> list[EffectivePreset]` (type rows, each replaced by the spool's row of the same key; `overridden` true for a spool row); `effective_k_values(conn, spool_id) -> list[EffectiveKValue]`; `preset_for(presets: Sequence[EffectivePreset], printer_model: str, nozzle_diameter: str) -> EffectivePreset | None` (exact key only).
- Produces on `FilamentSpoolStore`: `presets(spool_id) -> list[EffectivePreset] | None`, `set_presets(spool_id, rows: list[SpoolPresetCreate], *, actor) -> list[EffectivePreset] | None` (replaces the spool's own rows; writes one `edited` event `data={"changes": {"presets": [before, after]}}`), and the same pair for K values.
- Routes: `GET/PUT /filament/spools/{id}/presets`, `GET/PUT /filament/spools/{id}/k-values`. Tools: `filament_spool_set_presets`, `filament_spool_set_k_values` (write); the GETs are covered by `filament_spool_get` (Task 2 adds them to its answer), so they get `NOT_A_TOOL` entries naming that.

- [ ] **Step 1: Failing tests:** `test_a_spool_preset_overrides_its_types_for_that_key` (type rows H2C/0.4 `GFA00` and H2C/0.6 `GFA01`; spool row H2C/0.4 `P1` → effective H2C/0.4 is `P1` `overridden=True`, H2C/0.6 is `GFA01` `overridden=False`); `test_setting_an_empty_list_resets_to_the_type` (PUT `[]` → every row `overridden=False`, one `edited` event); `test_preset_for_is_exact` (`preset_for(rows, "H2C", "0.2")` is `None` when only 0.4 exists); API `PUT` with an unknown spool → 404.
- [ ] **Step 2:** Run `uv run --frozen pytest tests/filament/test_presets.py tests/api/test_filament.py` — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run backend tests; `cd agent && pnpm gen:api && pnpm test filament coverage && pnpm typecheck`; `cd frontend && pnpm gen:api && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): a spool's own presets and K values (#6262)`.

### Task 2: Reads the Filament section needs

**Files:**
- Modify: `backend/scadbuddy/filament/spools.py`, `ledger.py`, `models.py`, `api/filament.py`, `agent/src/tools/filament.ts`
- Test: `backend/tests/filament/test_spool_detail.py`, `backend/tests/api/test_filament.py`, `agent/test/filament.test.ts`

**Interfaces:**
- `Spool` gains `names: SpoolNames` (`brand`, `material`, `subtype`, `category`, `effect`, `insert_type`, `location` as text or `None`, resolved from the effective ids by `_hydrate`) and `low_stock: bool` (server-computed, F3: `grams_left / label_weight_g * 100 < ` the effective `low_stock_pct`, else `DEFAULT_LOW_STOCK_PCT = 20`, which Task 13 replaces with the setting `filament_low_stock_pct`; false when either weight is unknown).
- Produces `SpoolDetail(Spool)` with `tag: Tag | None` (the open binding's), `last_tag: LastTag | None` (`tag: Tag`, `unbound_at`, `reason`, `held_by: int | None` — the `number` of the spool that holds it now), `presets: list[EffectivePreset]`, `k_values: list[EffectiveKValue]`, `basis: GramsBasis` (`anchor: SpoolEvent | None`, `deltas: list[SpoolEvent]`; spec §5.5 "the UI shows the anchor and the deltas"), `in_use: bool` (any `print_started`/`print_ended` event; the UI offers delete only when false).
- `GET /filament/spools/{id}` answers `SpoolDetail` (was `Spool`).
- `ledger.basis(events, *, weight_locked, label_weight_g) -> GramsBasis` — the same walk as `grams_left`, returning the anchor and the deltas it used. `grams_left` is reimplemented on it, so the two never disagree.
- `GET /filament/tags?include_unbound=bool` → `list[TagRow]` (`Tag` + `binding: TagBinding | None` + `spool_number: int | None`); `GET /filament/tags/{id}` → `TagDetail` (+ `history: list[TagBinding]`).
- `GET /filament/events?spool_id&kind&source&since&until&cursor&limit≤500` (`since` included, `until` excluded) → `EventPage(events: list[LedgerRow], next_cursor: str | None)`; `LedgerRow = SpoolEvent + spool_number`. Ordered `(at, seq)` descending; `cursor` is opaque (hex of `at` and `seq`; no base64 outside image boundaries, #2479) and names the last row returned (F4).
- `GET /filament/summary` → `InventorySummary(spools: int, grams_left: float, value_left: Decimal | None, by_material: list[MaterialStock], loaded: int, low_stock: int, needs_type: int)`; `low_stock` counts `Spool.low_stock`; value uses `cost_per_kg`, else `filament_default_cost_per_kg` (Task 13). No "consumed" figure (Decision 11).
- Tools: `filament_tag_list`, `filament_tag_get`, `filament_ledger_list`, `filament_summary` (read). `filament_spool_get` describes the new fields.

- [ ] **Step 1: Failing tests:** `test_detail_names_the_last_tag_and_its_holder` (bind T to A, archive A, bind T to B → A's `last_tag.held_by == B.number`; restore A → `tag is None`); `test_basis_matches_grams_left` (scale 800, print_actual −50, ams_reading 700 on a locked spool → `basis.anchor.source == "scale"`, one delta of −50, and `grams_left == 750`); `test_events_page_by_at_and_seq` (two events with one `at` across the page edge at `limit=2` → the second page starts at the next row, no row lost or repeated); `test_low_stock_uses_the_spool_then_the_default` (15 % left with `low_stock_pct` 10 → `low_stock` false; with none → true); `test_in_use_follows_print_events`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run backend tests; agent `pnpm gen:api && pnpm test filament coverage && pnpm typecheck`; frontend `pnpm gen:api && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): spool detail, tags, ledger and summary reads (#6262)`.

### Task 3: Presets per printer and nozzle only

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_presets_per_nozzle.sql`
- Modify: `backend/scadbuddy/spools/native.py` (until Task 11 deletes it: its write makes the per-nozzle rows the migration makes, F5; its `preset` read is the H2C/0.4 row), `filament/models.py` (`Name` for `printer_model`/`nozzle_diameter` refuses `*`), `filament/spools.py` (F27: the first type set on a needs-type spool deletes its `migrated` preset rows)
- Test: `backend/tests/filament/test_presets_migration.py`, `backend/tests/filament/test_presets.py`

**Interfaces:**
- Migration: both preset tables gain `migrated boolean NOT NULL DEFAULT false`; every `('*','*')` row expands to `migrated = true` rows; then no `'*'` key, and CHECK `printer_model <> '*' AND nozzle_diameter <> '*'` on `filament_spool_presets` and `filament_spool_type_presets`.
- The model key must equal what `bambuddy/resolver.py` compares (`PRINTER_MODEL = "H2C"`). Step 3 measures the registry's `printers.model` values (dev database and `backend/tests/bambuddy/recordings/` printer bodies) and writes the SQL expression that maps them to that form, recorded in a comment with where it was measured. With no registry printer, the rows expand to `H2C`.
- `FilamentSpoolStore.update`: a PATCH that sets `type_id` on a spool whose `type_id` was NULL deletes its `migrated` spool preset rows and records them in the same `edited` event (`changes.presets`).

- [ ] **Step 1: Failing tests:** migration (the `MIGRATIONS[:idx]` pattern of `tests/test_pg_migrations.py`): a spool with a `('*','*')` preset `P1` and one registry H2C printer → rows H2C/0.2, 0.4, 0.6, 0.8 all `P1`, `migrated`, no `*` row; inserting `('*','0.4')` raises `CheckViolation`; a spool that already had H2C/0.4 `P2` keeps `P2`. Store: `test_a_first_type_drops_the_migrated_presets` (type has H2C/0.6 `T6` → after PATCH `type_id`, effective 0.6 is `T6` `overridden=False`); `test_a_native_upsert_with_a_preset_still_works`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Measure the model values; write the migration (`INSERT … SELECT … ON CONFLICT DO NOTHING`, then `DELETE`, then the CHECKs); change the native write and the store.
- [ ] **Step 4:** Run it, `tests/test_pg_migrations.py`, `tests/filament`, `tests/spools/test_native.py` — PASS.
- [ ] **Step 5:** Commit `feat(filament): presets per printer model and nozzle, no wildcard row (#6262)`.

## Part B — SB spools in every reader

### Task 4: Which SB spool is in each tray

**Files:**
- Create: `backend/scadbuddy/filament/inventory.py`, `backend/scadbuddy/migrations/<ts>_filament_tray_place.sql`
- Modify: `backend/scadbuddy/filament/locations.py` (`move` clears `tray_fingerprint`), `printers/reports.py` (gains `report_status`, moved from `spools/seen.py`, which re-exports it until Task 12, F7), `api/tray_spools.py`, `agent/src/tools/traySpools.ts`, `frontend/src/lib/trays.ts`, `frontend/src/components/print/TrayQuestion.tsx` (the native answer sends `{spool_id}`), the tray mocks in `frontend/src/mocks/features/hotPath.ts`
- Test: `backend/tests/filament/test_inventory_trays.py`, `backend/tests/api/test_tray_spools.py`, `agent/test/traySpools.test.ts`, `frontend/src/components/print/TrayQuestion.test.tsx`

**Interfaces:**
- Migration: `filament_spools` gains `tray_fingerprint text` (derived and stored, spec §5.3 "current tray"); the tray place is the spool's `location_id` when that location has `printer_id` set.
- Produces in `inventory.py`:
  - `tray_fingerprint(material: str | None, colour: str | None) -> str` = upper-cased stripped material, `"|"`, then `normalise_rgba(colour)`, or nothing after `"|"` when the colour is `None` or `""` (F6). Alpha is included (spec §5.1).
  - `TrayHolder(ams_id: int, tray_id: int, spool: Spool | None, found_by: Literal["tag", "link"] | None)`; `TrayView(trays: list[TrayHolder], shelf: list[Spool])`. The shelf is every unarchived spool that no printer's latest report resolves as held (F6).
  - `class FilamentInventory(pool)`: `trays(printer_id: UUID, seen: Sequence[SeenTray]) -> TrayView` (Postgres only; order: open binding by `tray_uuid`, then `tag_uid`; else, for an untagged tray, the spool placed at `(printer, ams, tray)` whose `tray_fingerprint == tray_fingerprint(seen.material, seen.colour)`); `link(printer_id, ams_id, tray_id, spool_id: UUID, seen: SeenTray, *, actor) -> Spool` (locks the tag before the spool, #6261; moves the tray's previous holder to no location with one `moved` event, then `LocationStore.ensure_ams` + `move`, then sets `tray_fingerprint`; raises `TrayNotFilledError` when `seen` is not filled, `TrayTagClashError` when the tray's tag is bound to another spool); `unlink(printer_id, ams_id, tray_id, *, actor) -> None` (moves the spool to no location).
- `PUT /printers/{printer_id}/trays/{ams_id}/{tray_id}/spool` body becomes `{spool_id: UUID}` (an SB spool), 409 for the two errors, 404 for an unknown printer or spool. `DELETE` unchanged. Tool `link_tray_spool` takes `spool_id` (uuid) only; its description and `SOURCE` drop "provider record". `lib/trays.ts` sends the new body.

- [ ] **Step 1: Failing tests:** `test_a_tagged_tray_is_its_bound_spool`; `test_an_untagged_link_holds_while_the_tray_matches` (link A to tray 0/1 seen `PLA` `#FF0000FF` → found_by `link`; then seen `PLA` `#00FF00FF` → that tray's spool is `None` and A is on the shelf) — Review Focus 3; `test_alpha_is_part_of_the_fingerprint` (linked at `#FFFFFFFF`, seen `#FFFFFF80` → `None`); `test_an_empty_colour_fingerprints` (`tray_fingerprint("PLA", "") == "PLA|"`); `test_link_moves_the_previous_holder_off` (B at the tray, link A → B has no location, one `moved` event on each); `test_a_tag_bound_elsewhere_refuses_the_link` (409 through the API); frontend: the native tray answer posts `{spool_id: <uuid>}`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run backend tests; agent `pnpm gen:api && pnpm test traySpools && pnpm typecheck`; frontend `pnpm gen:api && pnpm typecheck && pnpm exec vitest run src/components/print/TrayQuestion.test.tsx` — PASS.
- [ ] **Step 5:** Commit `feat(filament): which SB spool each tray holds (#6262)`.

### Task 5: The native dialog's options from SB spools

**Files:**
- Modify: `backend/scadbuddy/filament/colour.py` (`same_opacity`), `bambuddy/filaments.py`, `bambuddy/trays.py`, `bambuddy/resolver.py`, `bambuddy/print_run.py` (`resolve_plates`), `bambuddy/choices.py` (`native_choices_for`), `api/printing.py`, `api/storefront_slice.py`, `api/analyzers.py`, `api/accepted_fixes.py`, `api/library_print.py`, `api/settings.py` (`check_profile` → `offered_spools`) (F12); frontend `components/FilamentPicker.tsx`, `components/PrintPicker.tsx`, `lib/filaments.ts`, mocks `mocks/features/hotPath.ts`, `printPreview.ts` (minimal: drop the removed fields, no new UI, F9); `frontend/e2e/real-hot-path.spec.ts` (the SB spool label `#N`, F20)
- Test: `backend/tests/bambuddy/test_trays.py`, `test_resolver.py`, `test_filaments.py`, `backend/tests/api/test_print.py`, `test_print_choices.py`, `test_print_destination_native.py`, `test_print_filaments.py` (F28), `backend/tests/filament/test_colour.py`, `frontend/src/lib/filaments.test.ts`, `components/FilamentPicker.test.tsx`

**Interfaces:**
- Consumes: `FilamentInventory.trays` (Task 4), `effective_presets`/`preset_for` (Task 1).
- `same_opacity(left: str | None, right: str | None) -> bool` in `filament/colour.py` (`#RRGGBB` and `None` read as opaque; false when exactly one side `is_translucent`).
- `SpoolOption` loses `physical_spool_id` and `records`; gains `sb_spool_id: UUID | None`, `sb_number: int | None` (for a tray: the SB spool in it; for a spool row: equal to `spool_id`), `rgba: str | None` (`#RRGGBBAA`), `needs_type: bool = False`. `colour` stays `#RRGGBB` (the render splits by RGB).
- `FilamentOptions` loses `overlay_notes` and `provider_labels`.
- `gather_plate_options(..., trays_from=status, held: TrayView | None = None)` replaces `overlay=` and `provider_labels=`. `loaded_trays(status, printer_id, printer_name, held)` fills brand, colour name, grams left, label weight and `rgba` from the held SB spool's effective values and `Spool.names`, else the tray's own. `tray_questions(status, held)` asks about each filled, untagged, unheld tray with shelf spools of its material whose colour `colours_match`; `shelf_options(held.shelf)` gives each its SB `number` as `spool_id`.
- `colours_match(slot_colour, spool_colour, *, spool_rgba: str | None = None) -> bool` is false unless `same_opacity(slot_colour, spool_rgba)` (Decision 12); `_match_score` passes the spool's `rgba`. `lib/filaments.ts` `seedPlan` mirrors it (`sameOpacity`), pinned by a parity test over the same vectors (F11).
- `resolve(..., spool_presets: Mapping[int, Sequence[EffectivePreset]])` (F1), keyed by `sb_number` for a tray option too; `_record_preset` is deleted, its place taken by `preset_for(spool_presets.get(option.sb_number, ()), PRINTER_MODEL, size)`.
- `resolve_plates` (both paths, F10) builds `spool_presets` from `effective_presets` of every chosen `sb_number` (through the store, or the worker's CRUD route in Task 6); for a chosen spool with a BB link and no SB row for the model and nozzle, and only when `client` is not `None`, it reads `spool_filament_presets(bb_spool_id)` and converts each to `EffectivePreset(overridden=False)`.

- [ ] **Step 1: Failing tests:** `test_a_tray_option_carries_its_sb_spool` (tray tagged and bound to spool #7 → option `sb_number=7`, `brand` from the type, `remaining_g` the ledger's); `test_an_opaque_slot_does_not_suggest_a_translucent_spool` (two trays `#FFFFFFFF`, `#FFFFFF80`, slot `#FFFFFF` → suggested the opaque tray) — Review Focus 2; `test_same_opacity` (`("#FFFFFF", "#FFFFFF80")` false, `("#FFFFFF", None)` true); `test_a_trays_preset_comes_from_its_spool` (spool #7 H2C/0.4 `Bambu PLA Basic @BBL H2C` → that preset on a 0.4 nozzle; none on 0.6 → falls back to the tray's `tray_info_idx`); `test_a_bb_preset_is_read_only_without_an_sb_one`; `test_native_options_read_no_bambuddy` (client `None`, no `TypeError`); frontend `seedPlan` parity test.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement; every overlay reader (Files) calls `FilamentInventory.trays` instead of `status_overlay`.
- [ ] **Step 4:** Run `tests/bambuddy tests/filament tests/api/test_print.py tests/api/test_print_choices.py tests/api/test_print_destination_native.py tests/api/test_print_filaments.py tests/api/test_storefront_slice.py` and the print replay tests (`print_run_histories`, `print_lan_sequence_histories`, F13); frontend `pnpm gen:api && pnpm typecheck && pnpm exec vitest run src/lib/filaments.test.ts src/components/FilamentPicker.test.tsx` — PASS.
- [ ] **Step 5:** Commit `feat(print): the native dialog reads SB spools (#6262)`.

### Task 6: The print worker reads SB spools through CRUD

**Files:**
- Create: `backend/scadbuddy/filament/print_uses.py`
- Modify: `backend/scadbuddy/api/crud_print.py`, `crud/print.py`, `api/crud_areas.py` (docstring lists the new paths, T6), `workflows/print_activities.py`, `printers/print_activities.py`, `worker.py`, `main.py`, `backend/CLAUDE.md` (the hot-path sentences on the overlay and `native_use_hook`, F19)
- Test: `backend/tests/filament/test_print_uses.py`, `backend/tests/crud/test_crud_filament.py`, existing `tests/spools/test_native_uses.py` cases moved to `test_print_uses.py`

**Interfaces:**
- Produces in `print_uses.py` (moved from `NativeSpoolStore`, same rules, same `ref_id = run_id/subtask/slot`): `record_uses(pool, run_id: str, subtask: str, uses: Sequence[SlotUse]) -> int`, `settle_uses(pool, run_id: str, stages: Sequence[str]) -> bool`, `settle_uses_in(conn, run_id, stages) -> bool`; `SlotUse(slot_id: int, spool_id: UUID, grams: float, plate_started_at: datetime)`.
- `print_use_hook(uses: PrintUses) -> ReadHook` replaces `native_use_hook` (no recache step: there is no cache).
- CRUD routes on `crud_router("print")`, area `filament`: `POST /filament/trays` (body `{printer_id, seen: list[SeenTray]}` → `TrayView`), `POST /filament/presets` (body `{numbers: list[int]}` → `dict[int, list[EffectivePreset]]`, F1), `POST /filament/print-uses` and `POST /filament/print-uses/settle` (both take `Idempotency-Key`). Clients in `crud/print.py`: `RemoteFilamentTrays`, `RemotePrintUses`.
- The worker's plan (`workflows/print_activities.py`) reads `RemoteFilamentTrays.trays` instead of `printer_overlay`.
- The worker's tray read keeps the three rules of the deleted `PrintActivities._overlay` (Task 5 review): (a) it reads the printer's **stored report** (`printers/reports.py`), not `prepared.printer_status`, because the history's `AmsTray` drops `tray_uuid` and a tagged tray would hold nothing; (b) a failed read plans on the trays alone and never fails the run; (c) the plan's low-filament warnings (#4551) judge the held spool's grams. Each has a Step 1 test: `test_the_worker_reads_tags_from_the_stored_report`, `test_a_failed_trays_read_still_prints`, `test_a_native_plan_warns_on_the_held_spools_grams`.
- The preview (`bambuddy/preview.py` `preview_run`, Task 5) reads its held trays and presets through the same `POST /filament/trays` and `/filament/presets` routes, as the run does, so the preview request carries nothing volatile. It still reads `request["sb"]` when present, for preview operations in flight across the deploy. Test: `test_the_preview_and_the_run_resolve_one_slice` (a run of the preview's choices reuses its slice).

- [ ] **Step 1: Failing tests:** `test_a_lan_start_books_estimates_on_sb_spools` (tray held by #7, start 12 g → one `print_started`/`print_estimate` −12 on #7); `test_settle_replaces_the_estimate_once` (settle twice → one `print_ended`; −10 when sliced 10 and `done`, 0 when `failed`); `test_the_trays_route_needs_the_print_credential` (no credential → 401, the print credential → 200 with a `TrayView`); `test_a_retried_print_uses_call_writes_once` (same `Idempotency-Key` → one set of events).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement and wire the worker.
- [ ] **Step 4:** Run `tests/filament tests/crud tests/printers tests/workflows` and the print replay tests; run `e2e-stack/run.sh --mode recorder` — zero Bambuddy calls — PASS.
- [ ] **Step 5:** Commit `feat(print): the hot path reads SB spools through CRUD routes (#6262)`.

### Task 7: The Bambuddy path's options from SB spools

**Files:**
- Modify: `backend/scadbuddy/filament/inventory.py`, `bambuddy/filaments.py` (`build_options`, `gather_plate_options`, `FilamentPlan.ids`), `workflows/print_activities.py` (a `"bb"` plan mapped at read), `api/printing.py` (the Bambuddy-path tray question `POST /print/printers/{id}/trays/{ams}/{tray}/spool`), `agent/src/tools/print.ts` (descriptions that say "Bambuddy spool id"; plans it sends carry `ids: "sb"`), `agent/src/tools/coverage.ts` (the tray question's `NOT_A_TOOL` reason, F26), frontend `components/PrintPicker.tsx` and `components/print/TrayQuestion.tsx` (plans send `ids: 'sb'`; the Bambuddy-path answer sends the SB number)
- Test: `backend/tests/filament/test_inventory_rows.py`, `backend/tests/bambuddy/test_filaments.py`, `backend/tests/api/test_print.py`, `agent/test/printers.test.ts` (F28), `frontend/src/components/print/TrayQuestion.test.tsx`

**Interfaces:**
- Produces in `inventory.py`: `TrayPlace(printer_id: str, bambuddy_printer_id: int | None, printer_name: str | None, ams_id: int, tray_id: int)`; `FilamentInventorySpool(spool: Spool, loaded: TrayPlace | None, bb_spool_id: int | None)` (F16); `FilamentInventory.rows(*, include_archived=False, bb_assignments: Sequence[SpoolAssignment] = ()) -> list[FilamentInventorySpool]` — `loaded` from `trays()` over each registry printer's latest report (`printers/reports.py`), else a BB assignment of the linked BB spool (Decision 3); `FilamentInventory.bb_to_sb(ids: Iterable[int]) -> dict[int, int]` (BB id → SB number through `filament_bb_links`).
- `build_options(*, library_file_id, rows: Sequence[FilamentInventorySpool], requirements, printer, slot_materials)` replaces the BB `spools`/`assignments` pair. `spool_id = spool.number`; `remaining_g` = the slot's `inventory-remain` when the spool is loaded in this printer and BB reconciled it, else `grams_left`.
- Each Bambuddy-path option is filled through `trays._from_spool` (Task 5), so it carries `rgba`, `sb_number`, `sb_spool_id` and `needs_type` like a native one; `same_opacity` then holds on this path too (Review Focus 2). Delete the interim `preset_key` fallback to the BB spool id (`bambuddy/resolver.py`) and the `(spool.spool_id, spool.spool_id)` branch in `print_run.resolve_plates`, both dead once options are SB numbers.
- `FilamentPlan.ids: Literal["bb", "sb"] = "bb"` (F14). Every request this PR builds (frontend, agent, orders' `item_plan`, the storefront) sends `"sb"`. A `"bb"` plan (an in-flight workflow input, an old client) is mapped through `bb_to_sb` when it is read, an unmapped slot dropped.
- The Bambuddy-path tray answer takes `{spool_id: int}` (an SB number): it calls `FilamentInventory.link` and, when the spool has a BB link, writes Bambuddy's assignment as today. Its `NOT_A_TOOL` reason says it records the SB tray place first and Bambuddy's assignment for a linked spool.

- [ ] **Step 1: Failing tests:** `test_rows_place_a_spool_by_its_tray_then_by_bb` (SB tray link → `loaded.printer_id` registry id; no SB place, BB assignment of its BB spool → that place; neither → `None`); `test_bambuddy_options_are_sb_numbers` (SB #3 linked to BB 41 → option `spool_id == 3`, `sb_spool_id` set); `test_a_bb_plan_is_mapped_at_read` (`ids="bb"`, slot 41 → 3, slot 99 unlinked → dropped, never SB #99); `test_queue_filaments_still_sends_type_and_colour`; frontend: the Bambuddy-path answer posts the SB number.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/bambuddy tests/filament tests/api/test_print.py tests/api/test_print_filaments.py tests/workflows` and the print replay tests; agent `pnpm gen:api && pnpm test printers coverage && pnpm typecheck`; frontend `pnpm gen:api && pnpm typecheck && pnpm exec vitest run src/components/print` — PASS.
- [ ] **Step 5:** Commit `feat(print): the Bambuddy path's picker reads SB spools (#6262)`.

### Task 8: Stored spool ids become SB numbers

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_plan_spool_numbers.sql`
- Test: `backend/tests/filament/test_plan_numbers_migration.py`

**Interfaces:**
- First (F15, F30): each `spool_record_links` row of provider `bambuddy` with `record_id ~ '^[0-9]+$'`, whose physical spool is a `filament_spools.id`, becomes a `filament_bb_links` row (`bb_spool_id = record_id::int`, `bb_updated_at` NULL so the next pull refreshes it), `ON CONFLICT DO NOTHING` on both `spool_id` and `bb_spool_id`.
- Then every positive spool id is rewritten through `filament_bb_links (bb_spool_id → spool_id → filament_spools.number)` in: `model_print_choices.choices->'filament_plan'[*].spool_id`; `order_item_quotes.filament_plan->'slots'[*].spool_id`; the stored `storefront_slice_profile`'s `spools` values (where `library/settings_store.py` keeps it); `filaments.spool_ids`; `rack_nozzle_pick_spools.spool_id`; `print_batch_items.request` and `refused_request` (their `filament_plan`); `operations.request` of operations not yet ended (F14). A rewritten plan gets `"ids": "sb"`. An id with no link: dropped from a plan or a slice profile, removed from `spool_ids`, set `NULL` in a rack pick (its label stays, as an untracked spool's row does, `rack/usage.py`). Negative ids are left alone (native shelf ids are per-answer and never stored, `bambuddy/trays.py`).
- Must ship with Task 7 (same PR).
- Window (Task 7 review M4): an old API pod still serving after the migration ran can write an unmarked BB id into a remembered plan or the storefront profile, which the new code then reads as an SB number. The migration runs at the new API's start, so the window is one API rollout; the PR body says so.

- [ ] **Step 1: Failing test:** before the file, BB 41 linked to SB #3, BB 99 unlinked, SB #99 exists, BB 55 known only by a record link to SB #5; a remembered plan `[{1, 41}, {2, 99}, {3, -2}, {4, 55}]` → after: `[{1, 3}, {3, -2}, {4, 5}]`, `ids = "sb"` — slot 2 is not `99` (Review Focus 1); the same for a quote plan, a batch request and a pending operation; a slice profile; `spool_ids {41, 99}` → `{3}`; a rack pick 99 → `NULL` with its label; a non-numeric `record_id` and a record link whose BB id another SB spool already holds are skipped, not errors.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Write the migration.
- [ ] **Step 4:** Run it and `tests/test_pg_migrations.py` — PASS.
- [ ] **Step 5:** Commit `feat(filament): remembered and quoted spools name SB spools (#6262)`.

### Task 9: Orders, the colour widget and the agent's inventory read SB spools; alpha-aware matching (#6316)

**Files:**
- Modify: `backend/scadbuddy/orders/integrations.py` (`ShelfSpool`, `SpoolGroup`), `orders/bambuddy.py` (drop `spool_inventory` and the spool read for groups), `orders/spools.py`, `orders/spool_import.py`, `orders/component.py` (inject `FilamentInventory`), `api/orders.py` (item print passes the registry printer), `api/farm.py` and `bambuddy/farm.py` (remove `GET /farm/inventory`, `GET /farm/spools/{id}/summary` and their models, F16), `api/filament.py`, `agent/src/tools/farm.ts`, `agent/src/tools/orders.ts` (`price_book_import_spools` description), `agent/src/tools/coverage.ts`; frontend `components/widgets/FilamentColourPicker.tsx` (F17), `api/client.ts` (`getSpoolInventory` → `GET /filament/inventory`), `pages/settings/sections/inventory.tsx` and `pages/settings/SpoolInventoryPanel.tsx` (+ test) deleted with `mocks/features/spoolInventory.ts` (their route goes here, F9); `backend/CLAUDE.md` orders paragraph (SB spools, RGBA matching)
- Test: `backend/tests/orders/test_spools.py` (spool import tests live here too, F28), `backend/tests/api/test_filament.py`, `backend/tests/api/test_openapi.py`, `agent/test/farm.test.ts`, `agent/test/orders.test.ts`, `frontend/src/components/widgets/FilamentColourPicker.test.tsx`

**Interfaces:**
- `ShelfSpool`: `id: int` is the SB `number`; gains `rgba: str | None` (`#RRGGBBAA`); `loaded_printer_id: int | None` becomes `loaded_printer: str | None` (registry id). Built from `FilamentInventory.rows(include_archived=True)`.
- Task 7 left `item_plan` merging supplied SB numbers (the agent and dialog send `ids: "sb"`) with BB-id fills and returning a `"bb"` plan; this task makes it all SB. Its tests supply an SB number that differs from every BB id. One test takes the plan from the quote (Task 8 rewrote stored quotes to `"sb"`), not only a supplied one.
- `item_plan(store, inventory: FilamentInventory, item_id, plan, printer: str | None)`; it no longer needs Bambuddy (`integrations.linked` check removed); its plan has `ids="sb"`.
- `record_rgba(record: Filament) -> str` = `record.colour + f"{record.alpha:02X}"` (`orders/spools.py`); `_of` compares `spool.rgba` with the link's `spool_colour` + record alpha, else `record_rgba(record)`, and is false unless `same_opacity` holds — never RGB alone (#6316 reader 3).
- `spool_import.py`: groups are SB spools by effective (material, subtype, brand, RGBA); `_by_colour` and the `colour_taken` check compare `#RRGGBBAA` (#6316 readers 1 and 2); `spool_ids` are SB numbers; the cost is the newest priced spool's `cost_per_kg` (highest `number`).
- Routes: `GET /filament/inventory` → `FilamentInventoryView(spools: list[FilamentInventorySpool], trays: list[PrinterTrays])` (each registry printer's reported trays with the held spool); `GET /filament/spools/{spool_id}/summary?grams=` → `FilamentSpoolSummary` (the fields `farm.py`'s answered: remaining, place, cost per kg, value left, the same-filament stock). Tools `get_spool_inventory` and `get_spool_summary` keep their names and point at these; `bambuddyScope` is removed from both; `get_spool_summary`'s `spool_id` is an SB spool id (uuid), as `get_spool_inventory` lists them.
- The order quote's preview (`orders/operations.py` `PreviewSlicer` → `orders/bambuddy.py` `start_preview_plates`) resolves with the held SB spools and their presets on a native printer, as the dialog's preview does (Task 5), so a quote's grams, time and price use the preset the print will. Test: `test_a_native_quote_slices_with_the_held_spools_preset`.
- `FilamentColourPicker` reads `GET /filament/inventory` and draws each spool with its `rgba` (`Swatch`/`ClearSwatch` until Task 14's `RgbaSwatch`).

- [ ] **Step 1: Failing tests (one per #6316 reader, each with an opaque and a translucent row of one hue):** `test_import_by_colour_keeps_alpha_apart` (book rows `#FFFFFF`/`FF` and `#FFFFFF`/`80`; opaque group → links the opaque row only); `test_an_opaque_group_is_not_colour_taken_by_a_translucent_row`; `test_item_print_fills_the_spool_of_its_alpha`. Also `test_item_print_needs_no_bambuddy`; `test_inventory_route_lists_sb_spools`; `test_openapi_has_no_module_qualified_names`; frontend: the colour widget lists the seed's SB spools.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/orders tests/api/test_filament.py tests/api/test_farm.py tests/api/test_openapi.py tests/test_orders_bambuddy_seam.py`; agent `pnpm gen:api && pnpm test farm orders coverage && pnpm typecheck`; frontend `pnpm gen:api && pnpm typecheck && pnpm exec vitest run src/components/widgets src/pages/settings` — PASS.
- [ ] **Step 5:** Commit `fix(orders): orders read SB spools and match colour with alpha (#6262, #6316)`.

### Task 10: The pull maps BB's spool catalog to insert types

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_insert_bb_catalog.sql`
- Modify: `backend/scadbuddy/bambuddy/client.py`, `bambuddy/models.py`, `filament/bambuddy_pull.py`, `filament/catalog.py` (`insert-types` exposes `bb_catalog_id` read-only)
- Test: `backend/tests/filament/test_bambuddy_pull.py`, `backend/tests/bambuddy/test_client.py`

**Interfaces:**
- Migration: `filament_insert_types.bb_catalog_id integer UNIQUE` (nullable).
- `BambuddyClient.spool_catalog() -> list[SpoolCatalogEntry]` (`GET /api/v1/inventory/catalog`, `CatalogEntry {id, name, weight}` per the 1.2.6b1 OpenAPI of spec §3; a `Scope` like `spools()`'s; a recording in `tests/bambuddy/recordings/`).
- `pull(..., catalog: Sequence[SpoolCatalogEntry])`: once per pull, each entry becomes the insert type with that `bb_catalog_id`, else the unlinked one whose `upper(name)` matches (linked), else a new one (`empty_weight_g = weight`, insert material none). A BB spool's `core_weight_catalog_id` sets its insert type: on the type when the spool is created with a new type, else as the spool's override only when it differs from the type's. A catalog read that fails leaves insert types alone and pulls the rest (logged; `PullReport.catalog_failed: bool`).
- The module docstring's "the insert type is not pulled" sentence changes to say what it does now.

- [ ] **Step 1: Failing tests:** `test_a_bb_catalog_entry_becomes_an_insert_type` (entry 5 "Bambu reusable" 250 g → insert type with `bb_catalog_id=5`); `test_a_second_pull_reuses_it`; `test_a_spool_takes_its_core_weight_insert_type`; `test_a_failed_catalog_read_pulls_the_spools_anyway`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/filament tests/bambuddy/test_client.py tests/test_spool_refresh_replay.py`; frontend `pnpm gen:api && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): the Bambuddy pull maps its spool catalog to insert types (#6262)`.

## Part C — retire the old surface

### Task 11: Remove the native API, its tools and its store

**Files:**
- Delete: `backend/scadbuddy/api/native_spools.py`, `backend/scadbuddy/spools/native.py`, `agent/src/tools/nativeSpools.ts`, `agent/test/nativeSpools.test.ts`, `backend/tests/api/test_native_spools.py`, `backend/tests/spools/test_native*.py`, `backend/tests/filament/test_native_adapter.py`; frontend `pages/settings/sections/native-spools.tsx`, `components/settings/NativeSpools.tsx` (+ test), `mocks/features/nativeSpools.ts` (F9)
- Modify: `main.py`, `worker.py`, `api/crud_print.py` and `crud/print.py` (drop `/native-spools*` and `RemoteNativeSpools`), `printers/print_activities.py`, `spools/spool_providers.py` (the native provider goes; Task 12 removes the rest), `agent/src/tools/index.ts`, `agent/src/sessions/touched.ts`, `frontend/src/api/client.ts` (the native functions), `backend/CLAUDE.md` (the native routes and `NativeSpools.tsx` sentences, F19)
- Test: `backend/tests/api/test_routes.py`, `tests/api/test_openapi.py`, `agent/test/coverage.test.ts`, `frontend/src/pages/settings/registry.test.ts`

**Interfaces:**
- After it, `grep -rn "native_spool\|native-spools\|NativeSpoolStore\|NativeSpools" backend/scadbuddy agent/src frontend/src` finds only migrations (F29).

- [ ] **Step 1:** Change `test_routes.py`'s expected route list (no `/native-spools`); add `test_native_spool_tools_are_gone` in `agent/test/coverage.test.ts` (no `ALL_TOOLS` name starts `native_spool_`); no Settings section with id `native-spools`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Delete and rewire.
- [ ] **Step 4:** Run backend `tests/api tests/crud tests/printers tests/filament`; agent `pnpm gen:api && pnpm test && pnpm typecheck`; frontend `pnpm gen:api && pnpm typecheck && pnpm exec vitest run src/pages/settings`; the grep — PASS.
- [ ] **Step 5:** Commit `refactor(filament): retire the native spool API and tools (#6262)`.

### Task 12: Remove the provider overlay; the old tables stay, unread

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_carry_tray_links.sql`
- Delete: `backend/scadbuddy/spools/overlay.py`, `providers.py`, `spool_providers.py`, `seen.py` (its `report_status` moved in Task 4), the overlay half of `store.py`, `backend/scadbuddy/bambuddy/spool_providers.py`, their tests
- Modify: `backend/scadbuddy/spools/activities.py`, `workflows/spool_refresh.py`, `spools/component.py`, `spools/models.py` (keeps `SeenTray` and the key cleaners only, F7), `filament/bambuddy_pull.py` (step 2's `spool_record_links` branch and its docstring line go, F18), `printers/store.py` (the `tray_spool_links` merge reference goes; `filament_locations` already merges), `api/crud_print.py` (drop `/spools/records`, `/spools/refreshes/*`, `/spools/overlay`), `crud/print.py` (`RemoteSpoolStore`), the overlay readers of Task 5's list (drop `SpoolsDep` where only the overlay used it, F12), `core/settings.py` comment for `spool_overlay_refresh_seconds`, `e2e-stack/seed.sh` (wait for the pull's result, not `spool_provider_refreshes`, F20), `backend/CLAUDE.md` and `e2e-stack/CLAUDE.md` overlay sentences (F19)
- Test: `backend/tests/filament/test_carry_tray_links.py`, `backend/tests/test_spool_refresh_replay.py` (+ one new post-patch history in `tests/fixtures/spool_refresh_histories/`), `backend/tests/test_pg_migrations.py`, `backend/tests/filament/test_bambuddy_pull.py`, `backend/tests/auth/test_revoke.py` (unchanged: no table is dropped, F21)

**Interfaces:**
- Carry (F8, F30): each `tray_spool_links` row resolves its SB spool as the `filament_spools.id` equal to its `physical_spool_id`, else the SB spool that `filament_bb_links` names for that physical spool's `spool_record_links` row of provider `bambuddy` (a BB-only physical spool, pulled with a new id). That spool's location becomes the AMS tray location (created like `ensure_ams`, kind `AMS`, `ON CONFLICT DO NOTHING`), its `tray_fingerprint` = `material || '|' || CASE WHEN part = '' THEN '' ELSE '#' || part || 'FF' END` from the old `MATERIAL|RRGGBB`, and one `moved` event (actor `system:migration`). A spool already placed in another tray, or a tray another spool already holds, is skipped.
- **No DROP.** After this task no code reads or writes `native_spools`, `native_spool_uses`, `physical_spools`, `spool_provider_records`, `spool_record_links`, `spool_provider_refreshes` or `tray_spool_links`; the tables stay for the follow-up PR the controller files (Decision 6). A test pins that no module under `backend/scadbuddy` (migrations aside) names them.
- `SpoolRefresh.run` follows the replay conditions above: `workflow.patched("spool-providers-gone")` first; the old branch keeps `LIST` → `PROVIDER`×n → `SEEN`; the new branch runs only the held presets and the BB pull. `LIST_ACTIVITY` returns `[]`, `PROVIDER_ACTIVITY` the `str` `"skipped"`, `SEEN_ACTIVITY` `None`, each with a comment naming the patch.
- `spool_overlay_refresh_seconds` keeps its env name (no settings migration); its comment and help say it is the Bambuddy pull and held-presets interval.

- [ ] **Step 1: Failing tests:** `test_a_tray_link_becomes_a_tray_place` (SB spool S and a `tray_spool_links` row `PLA|FF0000` at tray 0/1 → S's location is that tray, fingerprint `PLA|#FF0000FF`, a `moved` event); `test_an_empty_colour_link_carries` (`PLA|` → `PLA|`); `test_a_bb_only_tray_link_finds_its_pulled_spool`; `test_the_carry_skips_a_taken_tray`; `test_no_code_names_the_old_tables`; the replay test over every old history and the new post-patch one.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Write the migration; delete the code; patch the workflow; record the post-patch history (`SCADBUDDY_RECORD_HISTORIES=1`).
- [ ] **Step 4:** Run `tests/filament tests/spools tests/test_spool_refresh_replay.py tests/test_pg_migrations.py tests/printers tests/auth/test_revoke.py tests/api`, mypy; run `e2e-stack/run.sh --mode recorder` — zero Bambuddy calls — PASS.
- [ ] **Step 5:** Commit `refactor(spools): remove the provider overlay; its tables are no longer read (#6262)`.

### Task 13: Settings is configuration only

**Files:**
- Create: `frontend/src/pages/settings/sections/filament.tsx` (+ test)
- Modify: `backend/scadbuddy/core/settings.py`, `library/settings_store.py`, `api/settings.py`, `filament/spools.py` (the summary and `low_stock` read the setting, F2), `spools/activities.py` (the pull activity skips when `filament_sync.spools == "off"`, F2), `frontend/src/pages/settings/fields.ts`, `areas.ts` (`LEGACY_ANCHORS`), `frontend/src/mocks/fixtures.ts`
- Test: `backend/tests/api/test_settings.py`, `backend/tests/filament/test_spool_detail.py`, `backend/tests/filament/test_bambuddy_pull.py`, `frontend/src/pages/settings/sections/filament.test.tsx`, `frontend/src/pages/settings/registry.test.ts`

**Interfaces:**
- Backend settings (runtime, env-seeded like their neighbours): `filament_sync: FilamentSync = FilamentSync()` with `FilamentSync(BaseModel, extra="forbid")`, `spools: Literal["off", "bb_to_sb"] = "bb_to_sb"` (spec §8's per-data-type shape; #6265 adds data types and the values `sb_to_bb`, `two_way`); `filament_low_stock_pct: int = Field(default=20, ge=0, le=100)`; `filament_default_cost_per_kg: Decimal | None = Field(default=None, ge=0)`. `DEFAULT_LOW_STOCK_PCT` is gone. `SectionId` gains `'filament'`; `spool_overlay_refresh_seconds` moves from `administration` to `filament`, label "Pull from Bambuddy every".
- Section `filament`, area `printing`, order 30, title "Filament". It shows the spools sync choice ("Off" / "Bambuddy → ScadBuddy"), the interval, the low-stock %, the default cost, the currency read-only with an `AppLink` to `#orders`, and one line with a link to `/filament` ("Spools, types and tags are in Filament.").
- `LEGACY_ANCHORS`: `inventory` and `native-spools` → `{ area: 'printing', section: 'filament' }`.

- [ ] **Step 1: Failing tests:** backend `test_filament_settings_round_trip` (PUT then GET; 422 for 101 % and for `spools: "two_way"`); `test_low_stock_reads_the_setting` (setting 10 → a 15 % spool is not low); `test_off_skips_the_pull` (the activity writes nothing and reports skipped); frontend `filament.test.tsx` (renders the fields; saving sends `filament_sync: {spools: 'off'}`), `registry.test.ts` `resolves the old spool anchors to Filament` (`resolveLocation('/settings', '#native-spools')` → `{area: 'printing', hash: '#filament'}`), and no section titled "Spool inventory" or "ScadBuddy's spools".
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run backend tests; frontend `pnpm gen:api && pnpm typecheck && pnpm exec vitest run src/pages/settings`; agent `pnpm gen:api && pnpm typecheck`; the Task 11 grep again over the whole tree — PASS.
- [ ] **Step 5:** Commit `feat(settings): a Filament configuration section (#6262)`.

## Part D — the Filament section

Design direction for every task below (the implementer still runs the
frontend-design process; this is the brief). The subject is a shop's physical
filament: spools of coloured plastic, many translucent. The one memorable element is the
**spool chip**: an RGBA disc on a checkerboard with a thin ring that shows grams left as an
arc. Everything else stays quiet and uses the app's existing tokens, shadcn components and
type (`index.css`); no new palette. Tables are dense and left-aligned; numbers are
right-aligned with tabular figures. Bambu Studio is the capability bar (vision memory):
every value its filament settings hold for a spool has a place on the edit page, with
fewer clicks; the builder cites the Bambu Studio source they compare against.

### Task 14: The section's frame, client, mocks and RGBA controls

**Files:**
- Create: `frontend/src/api/filament.ts`, `frontend/src/pages/filament/FilamentPage.tsx`, `frontend/src/pages/filament/tabs.ts`, `frontend/src/components/filament/RgbaSwatch.tsx`, `RgbaColourField.tsx`, `frontend/src/lib/rgba.ts`, `frontend/src/mocks/features/filament/index.ts`, `seed.ts`
- Modify: `frontend/src/App.tsx`, `frontend/src/components/AppShell.tsx` (nav item "Filament" between Library and Orders, `/filament`), `frontend/src/components/print/Swatch.tsx` (delegates to `RgbaSwatch`)
- Test: `frontend/src/lib/rgba.test.ts`, `components/filament/RgbaSwatch.test.tsx`, `RgbaColourField.test.tsx`, `pages/filament/FilamentPage.test.tsx`

**Interfaces:**
- `lib/rgba.ts`: `parseRgba(value: string): string | null` (as the backend's `normalise_rgba`: `#RRGGBB` → `…FF`, upper case); `alphaOf(rgba: string): number` (0-255); `withAlpha(rgba: string, alpha: number): string`; `isTranslucent(rgba: string): boolean`; `sameOpacity(a, b)` (moved here from Task 5's `lib/filaments.ts` copy); `CLEAR_ALPHA = 0x10`, `TRANSLUCENT_ALPHA = 0x80` (spec §5.1 presets: clear `00`-`20`, translucent `80`).
- `RgbaSwatch({ rgba, size?: 'sm'|'md'|'lg', gramsLeft?: number | null, labelWeight?: number | null })`: the colour over a checkerboard (CSS `conic-gradient` checker under the colour layer), `data-colour` the `#RRGGBBAA`, the grams arc only when both weights are known.
- `RgbaColourField({ value, onChange, inherited?: string | null })`: hex input, native colour input for RGB, an opacity slider (0-100 %, shadcn `Slider`), preset buttons "Opaque", "Translucent", "Clear", and the swatch. `inherited` shows the type's value as a placeholder when `value` is `null`.
- `tabs.ts`: `FILAMENT_TABS: readonly { id: TabId; title: string; path: string; available: boolean }[]` with ids in spec §10's order (F22): `spools, types, tags, suppliers, invoices, locations, catalog, metrics, shopping-list, ledger`; `available` false for `suppliers, invoices, metrics, shopping-list`. `FilamentPage({ tab })` renders `NavLink` tabs (the `WorkflowsPage` pattern) for available tabs only; `/filament` redirects to `/filament/spools`.
- `api/filament.ts`: one typed function per route the tabs use (`listSpools`, `getSpool`, `patchSpool`, `createSpool`, `archiveSpool`, `deleteSpool`, `bindTag`, `unbindTag`, `setSpoolPresets`, `setSpoolKValues`, `moveSpool`, `listTypes`, `getType`, `upsertType`, `setTypePresets`, `setTypeKValues`, `listCatalog`, `upsertCatalog`, `listLocations`, `upsertLocation`, `listTags`, `getTag`, `listEvents`, `getSummary`, `getInventory`), typed from `schema.d.ts`; Task 15 adds `importSpools`.
- Mocks: every `/api/v1/filament/*` route over an in-memory seed of 12 spools (opaque, translucent `80`, clear `10`, one needing a type, one with material `Unknown`, one archived with a last tag now on spool #3), with `reset`. `GET /filament/inventory` moves here from Task 9's mock (one route, one module).

- [ ] **Step 1: Failing tests:** `parseRgba('#ff8800') === '#FF8800FF'`; `withAlpha('#FF8800FF', 0x80) === '#FF880080'`; swatch renders `data-colour="#FFFFFF80"` and a checker layer; the field's "Translucent" sets alpha `80` and calls `onChange('#RRGGBB80')`; the slider at 50 % gives alpha `80`; `FilamentPage` shows six tabs in spec order and no "Invoices".
- [ ] **Step 2:** Run `pnpm exec vitest run src/lib/rgba.test.ts src/components/filament src/pages/filament` — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them, `pnpm lint && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): the Filament section's frame and RGBA swatch (#6262)`.

### Task 15: Spools tab, with CSV export and import

**Files:**
- Create: backend `backend/scadbuddy/filament/csv_import.py`; frontend `frontend/src/pages/filament/SpoolsTab.tsx`, `frontend/src/components/filament/spoolsQuery.ts`, `SpoolsTable.tsx`, `SpoolCards.tsx`, `SpoolTiles.tsx`, `BulkBar.tsx`, `spoolsCsv.ts`, `SpoolImport.tsx`
- Modify: `backend/scadbuddy/api/filament.py`, `agent/src/tools/filament.ts`, `frontend/src/api/filament.ts`, `frontend/src/mocks/features/filament/index.ts`
- Test: `backend/tests/filament/test_csv_import.py`, `backend/tests/api/test_filament.py`, `agent/test/filament.test.ts`; frontend `spoolsQuery.test.ts`, `spoolsCsv.test.ts`, `SpoolImport.test.tsx`, `pages/filament/SpoolsTab.test.tsx`

**Interfaces:**
- `spoolsQuery.ts`: `SpoolsQuery = { view: 'table'|'cards', status: 'active'|'archived'|'all', needsType?: boolean, lowStock?: boolean, used?: boolean, fresh?: boolean, material?: string, brand?: string, insertType?: string, location?: string, group?: 'none'|'material'|'brand'|'type'|'location', columns: ColumnId[], sort: string }`; `readQuery(params: URLSearchParams): SpoolsQuery`, `writeQuery(q): URLSearchParams` (state in the URL, as `listingsQuery.ts` does); `filterSpools(spools, q)` (`lowStock` reads `Spool.low_stock`, F3), `groupSpools(spools, group)`. "used" = grams left below label weight; "new" = grams left at label weight and no print event.
- Columns picker: the chosen columns are remembered per viewer in `localStorage` (try/catch; a view convenience, not a print option).
- Tiles from `GET /filament/summary`: total spools, grams left, by material (top 5), loaded, low stock, needs type; each tile click sets its filter. No "consumed" tile (#6269, Decision 11).
- Bulk: select rows → Archive, Restore, Delete (only for rows with `in_use` false; others are named and skipped), Move to location, Set type. One request per spool; the bar reports each failure by spool number.
- CSV export: `spoolsCsv(spools, columns): string` (RFC 4180 quoting), downloaded through `lib/embed.ts` `downloadBlob` (the Bambuddy iframe rule for downloads).
- A row links to `/filament/spools/<id>`; "needs type" and `Unknown` material show a badge with "Set type".
- CSV import (owner ruling):
  - `CsvRow` is a pydantic model and an OpenAPI schema; `csv_import.COLUMNS = list(CsvRow.model_fields)`. The frontend's `SPOOL_CSV_COLUMNS` is `readonly (keyof CsvRow)[]` with a `satisfies` check that names every key (F25); export writes these columns plus the derived ones, which import ignores (F24c, the preview says so). The browser parses the file and sends rows as JSON (no file bytes in text).
  - `import_rows(store: Filament, rows: Sequence[CsvRow], *, dry_run: bool, actor: str) -> ImportPreview` with `ImportPreview(rows: list[ImportRowResult])`, `ImportRowResult(line: int, action: Literal["create", "update", "error"], number: int | None, changes: dict[str, list[Any]], error: str | None)`. A row with `number` updates that spool (only the fields that differ); an unknown `number` is an error; a row with no `number` creates a spool. Catalog names (material, subtype, brand, category, effect, insert type) resolve by `upper(name)` and are inserted when missing, as the pull does (`bambuddy_pull.py`); in a dry run a missing name is reported in `changes` as `"new": true` and nothing is inserted. `location` resolves only to an existing unarchived location whose name is unique (else a row error) and is applied through `LocationStore.move` (F24a, the #6261 move-only ruling). `dry_run=false` writes every row in one transaction and refuses (422, nothing written) when any row is an error.
  - Route `POST /filament/spools/import` (body `{rows: list[CsvRow], dry_run: bool}`, header `Idempotency-Key`, required when `dry_run` is false; a retry with the same key answers the first result and writes nothing, F24b) → `ImportPreview`; tools `filament_spool_import_preview` (read; sends `dry_run: true` and returns the preview) and `filament_spool_import` (write; it sends `dry_run: true` first and refuses when the preview has an error line; its write sends one `Idempotency-Key` per call). Final review ruling: the preview is its own read tool, so a preview asks no write approval.
  - UI: `SpoolImport` — choose a file, the preview table (create/update/error per line with its changes, and "ignored: derived columns"), "Import N spools" enabled only with no errors, one `Idempotency-Key` per opened preview, then the tab reloads.

- [ ] **Step 1: Failing tests:** backend `test_a_dry_run_writes_nothing` (two rows → one `create`, one `update`; spool count and catalog rows unchanged); `test_a_row_with_a_number_updates_only_what_differs` (one `edited` event naming only `note`); `test_an_unknown_number_is_an_error_and_blocks_the_import` (422, nothing written); `test_a_missing_catalog_name_is_inserted_once` ("Sunlu" and "SUNLU" → one brand row); `test_a_location_is_a_move_and_must_exist` (existing "Shelf A" → one `moved` event; "Nowhere" → row error); `test_a_retried_import_writes_once` (same key twice → one set of spools); `test_derived_columns_are_ignored`; `test_columns_are_the_csv_row_fields`. Frontend: the preview lists an error line and disables Import; a clean preview's Import sends `dry_run: false` with the key; `readQuery(writeQuery(q))` round-trips; `filterSpools` with `lowStock` keeps rows with `low_stock`; the CSV quotes a name with a comma; the tab shows the 11 active seed spools, the translucent one's swatch `#…80`; bulk Delete skips an in-use spool and names it.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run the backend tests; agent `pnpm gen:api && pnpm test filament coverage && pnpm typecheck`; frontend `pnpm gen:api && pnpm lint && pnpm typecheck` and the vitest files — PASS.
- [ ] **Step 5:** Commit `feat(filament): the Spools tab with CSV export and import (#6262)`.

### Task 16: Types tab with the price book

**Files:**
- Create: `frontend/src/pages/filament/TypesTab.tsx`, `frontend/src/pages/filament/TypePage.tsx`, `frontend/src/components/filament/PresetTable.tsx`, `KValueTable.tsx`
- Modify: `frontend/src/pages/settings/sections/orders.tsx` (no `FilamentsEditor`); `FilamentsEditor.tsx` stays in `components/orders/` and is imported from there (Decision 8, F23)
- Test: `pages/filament/TypesTab.test.tsx`, `TypePage.test.tsx`, `components/filament/PresetTable.test.tsx`, `frontend/src/pages/settings/sections/orders.test.tsx`

**Interfaces:**
- Route `/filament/types/<id>` (or `new`): the type's fields (catalog selects from `listCatalog`, `RgbaColourField`, weights, temperatures, cost per kg, low stock %, insert type), its presets per printer model and nozzle (`PresetTable` rows `{printer_model, nozzle_diameter, slicer_filament, slicer_filament_name}`, no `*`), its K values, and its spools. Save sends one PATCH and, when changed, the preset and K PUTs. Unsaved edits hold navigation (`lib/useLeaveGuard.ts`).
- `PresetTable({ rows, onChange, inherited?: EffectivePreset[] })` is shared with Task 17: with `inherited`, a row the spool does not override shows the type's value dimmed with "From type"; an overridden row shows "Reset to type".
- The Price book panel (`FilamentsEditor`) lists book rows; a row linked to a type links to it; a row with no type has "Make a type" (POST `/filament/types` from its identity and cost).

- [ ] **Step 1: Failing tests:** the list shows each type's code, colour and price; saving a type with a new 0.6 preset sends `PUT …/presets` with the row; the preset table refuses `*` as a nozzle; "Make a type" posts the book row's material, colour and alpha; Settings → Orders no longer renders the price book.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run; also `pnpm exec vitest run src/components/orders/FilamentsEditor.test.tsx src/pages/settings`, `pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): the Types tab and the price book (#6262)`.

### Task 17: The spool edit page

**Files:**
- Create: `frontend/src/pages/filament/SpoolPage.tsx`, `frontend/src/components/filament/SpoolSections.tsx`, `OverrideField.tsx`, `TagPanel.tsx`, `GramsBasis.tsx`
- Test: `pages/filament/SpoolPage.test.tsx`, `components/filament/OverrideField.test.tsx`, `TagPanel.test.tsx`

**Interfaces:**
- Route `/filament/spools/<id>` (or `new`). One page, sections in spec §10's order, each a landmark with a heading and in-page anchors: **Identity** (number, type picker with "Create type from this spool", tags), **Weight** (label, empty from insert type, measured, grams left with `GramsBasis`, weight lock), **Colour and cost** (`RgbaColourField`, colour name, extra colours, effect, cost per kg; invoice link hidden until #6266), **Printers** (`PresetTable` and K values with `inherited`), **Storage** (location via `moveSpool`, last dried), **History** (the ledger list for this spool; the chart and metrics arrive with #6269). At ≥1024 px a sticky section index sits left; below it the sections stack and the index becomes a select (spec §13).
- `OverrideField({ name, value, effective, inherited, onChange, onReset })`: when the spool overrides the field (`overridden.includes(name)`) it shows a mark ("Set on this spool") and "Reset to type", which sends `null` for that field.
- Migrated values (phase 1 decision): material `Unknown` shows "Choose a material" with the material select open; a needs-type spool shows the type picker first, and its first type drops the migrated presets (Task 3), which the Printers section then shows as "From type".
- `TagPanel({ spool })`: the open tag with "Take off"; "Bind a tag" (tag uid or tray uuid, kind, override flag); for a spool with no tag and a `last_tag`, "Bind tag again" when `held_by` is null, else "Tag is on spool #N — Move it here" (Decision 7). Both call `bindTag` with the last tag's keys.
- Restore (archived spool header) calls `archiveSpool(id, false)` and nothing else; the TagPanel then shows the last tag.
- Delete is offered only when `in_use` is false; otherwise "Archive" with the reason ("It has prints, so job costs keep their source").

- [ ] **Step 1: Failing tests:** reset of an overridden colour sends `PATCH {rgba: null}` and the mark disappears after the answer (Review Focus 5); a restored spool shows "Tag is on spool #3 — Move it here" and its click posts the tag to this spool (Review Focus 4); `Unknown` material shows "Choose a material"; an in-use spool has no Delete; the Printers section shows a type preset as "From type".
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run, `pnpm lint && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): the spool edit page (#6262)`.

### Task 18: Tags, Locations, Catalog and Ledger tabs

**Files:**
- Create: `frontend/src/pages/filament/TagsTab.tsx`, `LocationsTab.tsx`, `CatalogTab.tsx`, `LedgerTab.tsx`, `frontend/src/components/filament/CatalogList.tsx`, `LocationTree.tsx`
- Test: one `*.test.tsx` per tab

**Interfaces:**
- Tags: every tag, kind, current spool (link), bound since, override flag; a row opens its history (`getTag`). Filter by kind and bound/unbound.
- Locations: the tree (`listLocations`), each node's spool count and spools; add, rename, re-parent (a cycle's 409 is shown inline), archive. AMS tray locations are read-only (named by printer). Sensors and QR labels arrive with #6268/#6264 and are not shown.
- Catalog: one `CatalogList({ kind })` per catalog kind of `CATALOGS` (materials, subtypes, brands, categories, effects, insert types, insert materials, location kinds, colours); add, rename, re-order (`sort_order`), archive and restore; materials and subtypes edit their value columns; colours use `RgbaColourField`; insert types show `bb_catalog_id` read-only.
- Ledger: `listEvents` paged by `next_cursor` (F4); filters spool number, kind, source and date range in the URL; each row links its spool.

- [ ] **Step 1: Failing tests:** Tags lists a rebound tag on its new spool; Locations shows the 409 text for a cycle; Catalog archives "Wood" and it leaves the default list but shows under "Archived"; Ledger's "Older" asks with the answer's `cursor`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run, `pnpm lint && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): the Tags, Locations, Catalog and Ledger tabs (#6262)`.

### Task 19: The print dialog's picker rows in RGBA

**Files:**
- Modify: `frontend/src/components/FilamentPicker.tsx` (+ test), `frontend/src/components/print/TrayQuestion.tsx` (+ test), `frontend/src/components/orders/QuoteFilaments.tsx`, `frontend/src/components/widgets/FilamentColourPicker.tsx` (`RgbaSwatch`)
- Test: `FilamentPicker.test.tsx`, `TrayQuestion.test.tsx`

**Interfaces:**
- Only the new rendering (the shape changes landed in Tasks 4, 5, 7, F9): a row shows `RgbaSwatch` with `rgba` and the grams arc, the SB number (`#7`), brand/material/subtype/colour name, and a "Needs type" badge that is an `AppLink` to `/filament/spools/<sb_spool_id>` (a ScadBuddy page, not the Bambuddy deep-link rule).
- The chosen spool per slot and "Wait for the right colour" still go through the dialog's Remember toggle (`toRemember`), unchanged; a test pins that a remembered plan carries SB numbers and `ids: 'sb'`.

- [ ] **Step 1: Failing tests:** a translucent spool row has `data-colour` ending `80`; a needs-type spool shows the badge linking to its page; remembering a plan sends `filament_plan` with SB numbers and `ids: 'sb'`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `pnpm exec vitest run src/components/FilamentPicker.test.tsx src/components/print src/components/widgets`, `pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(print): the filament picker shows SB spools in RGBA (#6262)`.

### Task 20: End-to-end, docs and the PR

**Files:**
- Create: `frontend/e2e/filament.spec.ts`
- Modify: `frontend/e2e/hot-path.spec.ts`, `print.spec.ts`, `settings.spec.ts`, `phone-width.spec.ts`, `page-titles.spec.ts` (`PAGES` gains `/filament/spools`, `/filament/spools/<seed id>`, `/filament/types`, `/filament/tags`, `/filament/locations`, `/filament/catalog`, `/filament/ledger`; each page has an `<h1>`, `usePageTitle`); `backend/CLAUDE.md` (the `filament/` paragraph names `inventory.py`, `presets.py`, `print_uses.py`, `csv_import.py`), `frontend/CLAUDE.md` (a Filament section entry: routes, mocks, e2e), `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§5.1's "until #6262" sentence and §5.5's "dropped in #6262" say what this PR did and that the drop is the follow-up PR). Earlier tasks already fixed the notes they made false (F19).

**Interfaces:**
- `filament.spec.ts`: open `/filament` → Spools; filter low stock; import a CSV with one error (Import disabled), fix it, import; open the translucent spool; set opacity to Clear and save; reset its colour to the type; restore the archived spool and move its tag back; at 390 px the edit page has no horizontal scroll and the section select works.
- `hot-path.spec.ts`: the trays show SB spool numbers, still with zero Bambuddy calls (`bambuddyCalls`).
- `settings.spec.ts`: `/settings#native-spools` lands on the Filament section.

- [ ] **Step 1:** Write the specs.
- [ ] **Step 2:** Run `pnpm build && pnpm exec playwright test e2e/filament.spec.ts e2e/hot-path.spec.ts e2e/print.spec.ts e2e/settings.spec.ts e2e/phone-width.spec.ts e2e/page-titles.spec.ts` — FAIL first, then PASS after fixes.
- [ ] **Step 3:** Update the docs; grep `native-spools`, `overlay`, `NativeSpools`, `spool_provider` across every `CLAUDE.md` and fix any copy left.
- [ ] **Step 4:** Whole-branch check: backend `uv run --frozen pytest -n auto`, ruff, mypy; frontend `pnpm lint && pnpm typecheck && pnpm test && pnpm build`; agent `pnpm test && pnpm typecheck`; `e2e-stack/run.sh --mode recorder`.
- [ ] **Step 5:** Commit `docs(filament): notes for the Filament section and SB spools (#6262)`; open the PR `feat(filament): Filament section, spool edit page, SB spools everywhere (#6262)`, body `Closes #6262`, `Closes #6316`, the four parts and what each leaves working, a section naming the removed tools `native_spool_list`, `native_spool_get`, `native_spool_upsert` and `native_spool_delete` with their `filament_*` replacements, so a saved flow's author knows what to change, a line that the old tables stay until the follow-up drop PR (link the controller's issue), the session marker and the footer.

## Decisions made while building (#6262)

- Task 2: the summary's `loaded` counts unarchived spools in `ams`-kind locations until
  Task 4 defines trays; `value_left` uses the effective `cost_per_kg` until Task 13's
  default lands.
- Task 2: a type's `label_weight_g` edit refreshes every unarchived spool that inherits it
  (locked in id order), because a stale grams-left would be visible to the user.
- Task 3: the migration expands a row with `*` in either key column, not only `('*','*')`;
  a single-`*` row the old API accepted would otherwise fail the new CHECK and stop the
  API's start.
- Task 3: the printer model key is `upper(btrim(printers.model))` plus `H2C` always, the
  form in every Bambuddy recording, `printers/store.py` and `e2e-stack/seed.sh`.
- Task 3: a spool's first type drops its `migrated` rows wherever `type_id` is first set:
  `FilamentSpoolStore.update` and `bambuddy_pull._link` (the only two writers).
- Task 3: a whole-list `set_presets` keeps `migrated` on each row whose key and values are
  unchanged; a new or changed row is the person's.
- Task 3: until Task 11 deletes `spools/native.py`, a native preset edit records the old
  value from a person's own H2C/0.4 row when one exists. Accepted: short-lived.
- Task 3: the model rule lives in `filament/presets.py` and in the migration's SQL, linked
  by comments, with no parity test: the merged migration cannot change.
- Task 4: the summary's `loaded` counts unarchived spools whose location has `printer_id`
  set (the tray place), replacing Task 2's interim `ams`-kind count.
- Task 4: `SeenTray` gains `rgba` (from `tray_color`, `#RRGGBBAA`); `colour` stays
  `#RRGGBB` for the overlay and the slicer. The tray fingerprint uses `rgba`, so a
  translucent refill of one hue is a different tray (spec §5.1). No other task changed
  `seen_trays`.
- Task 4: a link to a tagged tray binds the tray's free tag to the spool; a tag held by an
  archived spool counts as free (as `bambuddy_pull._bind` reads it); a spool that holds
  another tag is rebound to the tray's (the tray's reading is the physical tag, one tag per
  spool).
- Task 4: `link`/`unlink` take a per-tray advisory lock, then the tag rows, then spools by
  id, then the place `FOR SHARE`. Two theoretical lock-order bends stay: the archived
  holder's spool and the spool's own old tag are locked after the link's spools; every path
  that could invert them takes the tag lock first, which the link already holds.
- Task 4: `link` refuses an archived spool.
- Task 5: `SpoolOption.needs_type` is `bool | None = None`, not `bool = False`: a `False` default makes the field required in the output schema and breaks every frontend fixture; the frontend reads it as falsy.
- Task 5: an SB spool's own preset beats the tray's `tray_info_idx` (F1).
- Task 5 (review ruling): the native tray question keeps `close_spools`' wider cut (CIEDE2000 ≤ `CLOSE_COLOUR`, both colours required, for a hand-set tray colour), filtered by `same_opacity(seen.rgba, spool.rgba)`, instead of `colours_match`; a colourless shelf spool is not offered for every untagged tray.
- Task 5 (review): the dialog's background preview (`bambuddy/preview.py`) resolves with the held spools and SB presets, as `/check` does, so the slice shown is the one printed and Task 6's run can reuse it.
- Task 5 (review): Task 6's worker read carries the deleted overlay reader's three rules, and Task 7 fills Bambuddy-path options through `trays._from_spool` and deletes the interim `preset_key` fallback (both written into those tasks).
- Task 5 (re-review): the preview request's `sb` is unkeyed (`OperationKind.unkeyed`), so a re-send under one Idempotency-Key is its first answer whatever changed since (§4.2), and it carries only what the plan reads (per held tray its spool id, number and the effective fields `loaded_trays` and the low-filament warnings read; presets per SB number), never notes or timestamps, so it stays well under `MAX_REQUEST_BYTES`. Task 6 replaces it with the worker's own CRUD read; Task 9 gives the quote preview the same.
- Task 6 (review ruling): the print worker's filament routes take a new CRUD area, `filament`, not `spools`. `spools` already exists on main's API, so with it a new worker started before an API that serves the routes, and a print in the roll lost its SB-spool booking silently. The legacy `/native-spools/uses` routes stay as delegates for an old worker against a new API until Task 12.
- Task 6: estimates are booked at the plate's start (the ledger's named-time rule); print events name the actor `print`.
- Task 7 (review ruling R1): the Bambuddy pull writes a linked BB spool's `slicer_filament`/`slicer_filament_name` as SB preset rows (H2C, nozzles 0.2/0.4/0.6/0.8, `migrated=true`) only when Bambuddy's value changed since it last sent one (kept in the link), and then only for a key with no row or a row still holding Bambuddy's last value; so a preset a person deletes stays deleted, and a link with no last value fills its empty keys once; a row a person or the native migration set is never rewritten. A clear in Bambuddy deletes only rows that still hold its last value. Each change records an event with `changes.presets`. A type still overrides (F27). (Refined in the Task 7 re-review: `migrated` alone does not mean Bambuddy wrote the row.) The resolver's own-preset-by-name step stays: BB's values are filament ids or local ids that Bambuddy's catalogue ref ids do not hold.
- Task 7 (review ruling R2): `SpoolOption` carries the resolved SB preset name, so name-based filament-profile matching and the analyzers' preset-based silk check keep working.
- Task 7 (review ruling): `FilamentPlan.ids` is left out of `run_key` and the operation key when it is `"bb"`, so a retry that straddles the deploy joins its run.
- Task 7 (review ruling, accepted window): between the API's rollout and the new print worker's first poll, an old worker reads an `"sb"` plan's numbers as BB ids, and a Bambuddy-queue print started with an explicit spool in that window can take another spool's type and colour. The window is one worker rollout (the worker waits for the API's `filament` area, then polls). Accepted rather than changing the payload's type so an old worker fails closed; the PR body says to deploy when no Bambuddy-queue print is being started.
- Task 7 (review): `FilamentInventory.rows` places a spool by its SB tray place on a registry printer with no stored report too, so a tray answer on a Bambuddy-only printer shows the spool loaded.
- Task 7 (re-review): the filament step names each option's preset for the printer's mounted nozzle when the route knows it (`options.nozzles`), else 0.4.
- Task 7 (re-review 2): the pull's preset sync locks the spool row first (advisory → tags → spool), so a person's concurrent PUT /presets is never overwritten or deleted.
- Task 7 (re-review 3): only a `migrated` row that still holds Bambuddy's last value counts as Bambuddy's; a person's row is never rewritten by the pull, even when it holds the same value.
- Task 8 (review): a `print_batch_items` row whose outcome is `pending` or `started` keeps its `request` and `refused_request` unchanged, so the next Queue all resends the same body, keeps its `request_id` and run key, and attaches to the run instead of starting a second print; its unmarked plan maps at read (Task 7).
- Task 8: remembered choices are a bare list with no `ids`; every reader (frontend and agent `seedPlan`) treats their numbers as SB numbers and submits with `ids: "sb"`, so they are rewritten without a marker. A second run of the file would map them again: only the migration ledger prevents that.
- Task 8 (PR body): the storefront profile's rewrite changes `profile_digest`, so cached storefront slices miss once and slice again; a material whose spool has no BB→SB link is dropped and its listings show no price until a spool is picked.
- Task 9 (review ruling): the Bambuddy-path item fill (`orders/spools.py` `item_plan`) and `GET /filament/inventory` place a spool by the stored report, then by Bambuddy's assignment of its linked BB spool while Bambuddy is linked (Decision 3, until #6263); a failed assignment read places by reports alone.
- Task 9 (review ruling): a record's `spool_ids` link matches its spools whatever their alpha; only the colour fallbacks (`_by_colour`, the RGBA lookups) require `same_opacity`. A link by id is not a match by colour (spec §5.1), and prod's two clear stand-in records (`20261114T1607Z`) keep their spools.
- Task 9 (review ruling): the density chain reads the spool's own density, then its preset, then its held tray's `tray_info_idx`. The K-profile and Bambuddy-catalogue steps are gone with the BB inventory read; a record they alone filled stays as it is (densities are never overwritten), and a new record they alone could have filled has no rough estimate until a person types its density.
- Task 9 (review ruling, fix round 2): Bambuddy's tray-assignment read (`filament/bambuddy_pull.py` `read_assignments`) waits at most `ASSIGNMENTS_TIMEOUT` (2.5 s) inside `waiting_on_bambuddy()`, as it runs inside an item print check's 6 s budget and in `GET /filament/inventory` and the spool summary; a timeout, a refusal or an answer that is not JSON is no assignments (logged), so the spools are placed by the reports alone.
- Task 9 (review ruling, fix round 2): a record's `spool_colour` is its spools' own `#RRGGBBAA` at the last import, and the colour fallbacks (`spool_import.py` `_link_rgba`, `spools.py` `_of`) compare a spool's `rgba` with it as it is, not with the record's alpha. `20261116T0300Z_filament_spool_colour_rgba.sql` converts the old `#RRGGBB` (the two stand-ins to `#00000000`, others with the record's alpha); an old `#RRGGBB` still written during the rollout reads with the record's alpha, as the migration converts it (fix round 3). So a stand-in whose linked spools are all archived finds a new clear spool, and the import makes no second `#000000` record.
- Task 10 (review ruling): Bambuddy's catalog `weight` is read as a number (int or float) and stored rounded to whole grams; a result of 0 or less is skipped, so a fractional weight never fails the whole catalog read.
- Task 10 (review ruling): a BB spool's `core_weight_catalog_id` is a pulled field like the others. It is set on create and on link where the spool has no insert type; a recorded value that BB moves (including a recorded None to a mapped id) is written unless an `edited` event since BB's change names `insert_type_id`; a move to the spool type's own insert type clears the override. A key never recorded (spools linked before this change, or an entry unmapped with no last id) stays absent, and its first read fills only a spool with no insert type of its own and no person's clear since the link; a spool's existing insert type is kept.
- Task 10 (review ruling): while BB's entry is unmapped or the catalog read failed, the recorded reading keeps the last id, so a later pull with the entry mapped applies it. The catalog map is all-or-nothing; an entry whose name clashes with a type linked to another BB id is skipped and counted (`PullReport.catalog_skipped`).
- Task 10 (accepted at the fix-round cap): a row whose key stays absent because its entry never maps is read in full on every pull (a spool lock and a same-value `bb_updated_at` update, no event, not in `updated_ids`); a spool created while its entry was unmapped takes the insert type on the next pull where its BB row did not move, not on a pull where it moved. Both are bounded and never lose a value.
- Task 11 (review ruling, PR body): the `/print/native-spools*` CRUD routes go in this PR. During the worker rollout an old print worker gets 404 from them and logs it: a LAN start's grams are not booked (so the new settle has nothing to deduct), a run's `print_ended` settle is not booked, and a native LAN run it plans uses the trays without the spool overlay. Prints still start and finish; a weighing corrects the spools. Accepted because workers roll in minutes.
- Task 12 (review, PR body): during the rollout an old API replica may write a tray answer to `tray_spool_links` after the carry ran; nothing carries it and the tray asks again. A BB-only tray link carries only if the pull has linked its BB record (`filament_bb_links`); a skipped link stays in the kept table. Both are recoverable until the drop PR.
- Task 15 (ruling): a CSV row that names an insert type not in the catalog is a row error, not an insert: an insert type needs `empty_weight_g`, which no CSV cell holds; a person creates it first.
- Task 15 (security ruling): the CSV export prefixes a text cell that starts with `= + - @ TAB CR` (or with `'` before one of them) with `'`, and the import strips exactly one such `'`; numeric columns are never prefixed. A spreadsheet never runs a stored value as a formula, and export → import round-trips.
- Task 15 (review ruling): "Needs type" (tile, filter, badge) keeps the backend meaning, `type_id` null; a typed spool whose material is Unknown shows a separate "Unknown material" badge with the same Set type link.
- Task 16 (review ruling): a type created on `/filament/types/new` keeps its id as soon as the POST succeeds; a failed preset or K-value PUT is retried as an edit, never a second POST. Flow ratio, max volumetric speed, per-plate bed temperatures and soluble flags stay in the slicer filament preset the type names (no new type fields in #6262; revisit at #6270).
- Task 17 (rulings): an Unknown-material spool shows "Choose a material" with the material select marked (aria-invalid) and focusable, not opened on load (accessibility). On a needs-type spool the type picker is disabled while the preset draft is unsaved; a clean first-type pick shows the migrated rows as dropped and person-saved rows as kept, and that save sends no presets PUT. "Reset to type" shows only for an own row the type itself has (the page reads the type's rows); otherwise Remove; while the type is loading or failed to load, own rows show no action. `EffectivePreset` gains `migrated` so the page can name the migrated rows.
- Task 17 (accepted at the fix-round cap): the eight-field K-value mapping of "Create type from this spool" has no page test, and the mock's extra-key refusal does not cover the catalog upserts, type archive, Bambuddy link and CSV import bodies; both go to the final review's fix wave.
- Task 18 (ruling): `GET /filament/events` gains `until` (exclusive; `since` stays inclusive) end to end (route, store, tool, client), so the Ledger tab can ask for a date range.
- Task 19 (ruling): the picker's "Needs type" badge links to the spool page in a new tab and stops the click, so it never toggles the row's radio.
- Task 20 (ruling): Task 20 writes the e2e specs and docs and commits; the controller opens the PR after the final review. The backend's full pytest and e2e-stack run in CI (machine load, Docker). The native `<select>` in four Filament files (Tasks 17-19) fails `ui-guard.test.ts`, so Task 20 switches them to the shared Select. The six tab routes are literal `path="filament/..."` routes, as the page-titles tripwire requires.
- Final review (rulings): the API caches Bambuddy's tray assignments for 5 s, and for 30 s after a failed read it serves "no assignments" without asking, so a hung Bambuddy cannot stall every Filament page (cost: up to 5 s stale; a changed API key is seen up to 5 s late). `FilamentPlan.ids` keeps its `"bb"` default for old raw clients, documented on the field and in the print skill. An unexpected database error in a CSV import write stays a 500 with nothing written (no input found that reaches it). CSV cells cap at 200 characters, `note` at 2000. The move pickers leave out AMS tray places: loading a tray is the tray's job. The CSV import dialog stays open while its write runs. The rollback caveats (an old worker's settle 404, the 0706Z CHECK, the 0034Z plan rewrite) are in the PR body.
