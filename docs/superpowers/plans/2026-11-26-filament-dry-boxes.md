# Filament Dry Boxes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A dry box holds one spool. When a spool with a home box comes out of the AMS, ScadBuddy asks which spool goes into that box, and the person identifies it (RFID tag, QR code or a pick by hand). When the identified spool is not the box's spool, the box's spool is archived at 0 g. Grams left on an archived spool are booked as non-print waste, which metrics keep apart from print waste.

**Architecture:** A dry box is a location whose kind has `dry_storage` and whose existing `capacity` is 1. No new table. A spool gets a `home_location_id` (its last dry box) and a `home_asked_at` (set when capture unloads it). The person identifies the spool with routes that exist today (`POST /filament/tags/reads` for a tag, `GET /codes/{code}` for a QR code or a typed code, `GET /filament/spools` for a pick). The answer is the existing `POST /filament/spools/{id}/move` with one new request field, `if_occupied`. Waste is one new ledger event kind, `non_print_waste`, written by the archive code. Metrics add a separate figure.

**Tech Stack:** Python (FastAPI, psycopg), Postgres migration, React with msw mocks, the agent's TypeScript tools.

**Spec:** Issue #7167 and its ruling comment; epic #7171; `docs/superpowers/specs/2026-11-23-unknown-spool-flow-design.md` (the non-goal "Dry boxes, a spool's home, waste on archive (slice 3)"); `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§5.5 ledger); `docs/superpowers/specs/2026-11-22-printer-location-labels-design.md` (place labels, #6955).

## Rulings (binding; do not reopen)

- Ruling: A dry box is a filament location with a limit of one spool. It uses the existing `filament_locations.capacity` (integer, nullable, today never enforced) on a location whose kind has `dry_storage`. No container table. — Inventory epic #6949 K2 owns containers and would have to migrate a table made here. — K2 must read `capacity = 1` on dry-storage places as boxes; a small data step.
- Ruling: Waste on archive and the home return are built in Python, in `backend/scadbuddy/filament/` and `backend/scadbuddy/api/filament.py`. — Inventory K4 ports today's filament code to Go unchanged, so it carries this code with it. — K4 ports a little more code.
- Ruling: No new HTTP route is added in Python. The answer (the identified spool goes into the box) is the existing `POST /filament/spools/{spool_id}/move` with a new request field; the prompt list is a new query parameter on the existing `GET /filament/spools`. — Owner policy, epic #7667: new routes go in Go. — If review finds these two do not fit, the new route is built in `tools/go/core`.
- Ruling: State is in Postgres. One new migration file, `backend/scadbuddy/migrations/20261126T1700Z_filament_dry_boxes.sql`. — Root CLAUDE.md "Migrations". — A merged migration edited by mistake stops the API at start.
- Ruling: Non-print waste is a ledger event of kind `non_print_waste` (source `person`, `grams_after` 0, `grams_before` the grams left). Metrics sum these events only. Print waste stays the failed and cancelled plates. The two never share an event. — One event per fact means no double count, and the zero reading is what sets the spool to zero. — A second source for the same grams would count twice.
- Ruling (the owner, 2026-11-26, on #7167): the system never assumes which spool goes into a dry box. The person always identifies it: by an RFID tag scan, by a QR code (a ScadBuddy label), or by a pick by hand. No spool is preselected, the unloaded spool included. When the identified spool is not the box's current spool, the current spool is archived and set to 0 g, and its grams left are booked as non-print waste. This replaces the issue's "yes / assume new" prompt. — The owner's words: "Always require a selection: via RFID scan of $thing or QR code or manual selection." — A person who only wants to say "yes" scans or picks once more than before.
- Ruling: an identified tag that no spool holds goes through today's tag flow (`POST /filament/tags/reads` answers `next: "bind_or_create"` with a `draft` from the decoded tag): the dialog offers "Make a spool from this tag" with that draft, or a pick by hand, and then goes on with the new spool. No new tag code. — #7143's unknown trays are tray rows; a tag scanned at a dry box is not in a tray. — If the owner wants the #7143 dialog here, Task 6 reuses its shell.
- Ruling: "QR code" in the dialog is the browser's `BarcodeDetector` on the camera where the browser has it (Chrome on Android), and a code field (the label prints `S-<n>` under the QR) everywhere. Both resolve through `GET /codes/{code}`. No new dependency. — A phone camera that opens the label's `/x/<code>` link leaves the dialog; the in-dialog reader keeps the person in the flow. — Where `BarcodeDetector` is missing, the person types the code.
- Ruling: Waste is booked in `FilamentSpoolStore.archive` and when an identified spool replaces a box's spool. It is not booked for reason `respooled` (the grams continue on another spool, #7684), for bulk archive, or for the Bambuddy pull's archive. — Only the two paths have a person or a weighing behind them; a respool moves grams, it does not lose them. — Grams archived in bulk leave stock with no waste row.

## Global Constraints

- Only the CRUD service (the API) touches the database (root CLAUDE.md "Database access"). No new DB client.
- New data that crosses services goes through a CRUD route that needs a service credential. This plan adds none.
- Never edit, rename or remove a merged migration. The new file lists the full `filament_spool_events_kind_check` set in one `DROP` and `ADD` (copy the newest file that holds it, add `'non_print_waste'`).
- Generated API types (`backend/openapi.json`, both `schema.d.ts`) are never committed.
- Tests: write each test first. Backend tests go in `backend/tests/filament/` (routes in `backend/tests/api/`). Run locally only `ruff`, `mypy`, `eslint` and `pnpm typecheck`; CI runs the tests.
- Print and quote options are remembered choices with a toggle (#6219). This plan adds none.
- UI tasks load the `frontend-design` skill first and follow `frontend/CLAUDE.md`.
- A request change on a route changes the agent tool that calls it and `agent/src/sessions/touched.ts` in the same task.
- No counts in docs.
- Commits: Conventional titles, `Fixes #7167` in the PR body. Never amend.

## Facts measured on origin/main (e56ad635e, 2026-11-26)

- #7143's code is in PR #8123 (2026-11-27): the `filament_unknown_trays` table, capture's unknown-tray step, the Go list and dismiss routes, the shell-owned `UnknownSpoolDialog` and a Needs-you filament source. This plan builds on its branch after it merges, and its Needs-you source and dialog shell are the patterns for Task 6.
- `POST /filament/tags/reads` (`api/filament.py`) answers `TagReadResult` with `next` of `open_spool`, `open_code`, `bind_or_create` or `none`; `bind_or_create` carries a `decoded` tag and a `draft` spool. `GET /api/v1/codes/{code}` resolves a typed code (`S-<n>`). The scan page (`frontend/src/pages/filament/ScanPage.tsx`) reads tags through Web NFC (#7273). No QR reader and no spool picker component exist in the frontend.
- `capture.reconcile` (`filament/capture.py`) already detects "unloaded": for each tray it books an `unloaded` event and moves the spool to `_moved_from` (its previous place that is not a tray).
- `LocationStore.move_on` (`filament/locations.py`) is the one mover; it writes the `moved` event and raises `MoveTargetError`. `capacity` is never enforced.
- `FilamentSpoolStore.archive` books the final weighing, then `ledger.closing`, then `_archive`. `bulk_archive` and the pull go through `update_on`.
- `ledger.reading` treats an event as an anchor by `source` (`scale`, `person`, `bb`, `ams_remain`), so a `person` event with `grams_after` 0 sets `grams_left` to 0.
- Metrics (`filament/metrics_store.py` `inventory`, `metrics.py` `spool_metrics`) call print waste the grams of `failed` and `cancelled` plates. Nothing books purge, poop or prime-tower grams apart from the plate.
- Needs-you (`frontend/src/lib/needsYou.ts`) has agent, run-grant, trigger-grant and pairing items; no filament item.

## Review Focus

1. A box that is full: a move, a create with a place, a CSV import, an invoice confirm and a Bambuddy pull must all refuse or skip it the same way (`409 box_full`). Test: `test_every_location_writer_respects_a_full_box`.
2. Two moves into one empty box at once: exactly one wins. Test: `test_two_concurrent_moves_into_one_box_one_wins`.
3. Archive of a spool with 0 g, with no reading, or with reason `respooled`: no waste event, no negative or null grams. Tests in Task 3.
4. The identified spool is the box's spool, or the box is empty: a plain move, no archive, no waste. The identified spool is the unloaded spool: it goes home. Tests in Task 4.
5. A spool whose home box was archived or turned into a non-box: no prompt, today's unload behaviour. Test in Task 4.

---

### Task 1: Schema and models

**Files:**
- Create: `backend/scadbuddy/migrations/20261126T1700Z_filament_dry_boxes.sql`
- Modify: `backend/scadbuddy/filament/models.py` (`EventKind` near line 1636, `Spool`, `SpoolMove` near line 830, `Closing` near line 1877)
- Test: `backend/tests/filament/test_dry_boxes_migration.py`

**Interfaces:**
- Produces: columns `filament_spools.home_location_id uuid REFERENCES filament_locations(id) ON DELETE SET NULL`, `filament_spools.home_asked_at timestamptz`; `'non_print_waste'` in the kind CHECK and in `EventKind`; `m.Spool.home_location_id: UUID | None`, `m.Spool.home_asked_at: datetime | None`; `m.SpoolMove.if_occupied: Literal["refuse", "replace"] = "refuse"`; `m.Closing.non_print_waste_g: float | None = None`.

- [ ] **Step 1: Write the failing test.** `test_migration_adds_home_columns_and_waste_kind`: after `migrate`, both columns exist; inserting an event of kind `non_print_waste` succeeds; kind `bogus` still fails the CHECK. `test_home_backfilled_from_moved_events`: a spool whose newest `moved` event `to` is a dry-storage place with `capacity = 1` gets that place as `home_location_id`; one whose newest move is to a shelf gets none.
- [ ] **Step 2: Run** `pytest backend/tests/filament/test_dry_boxes_migration.py -v` in CI (no local Postgres). Expected: FAIL (columns missing).
- [ ] **Step 3: Write the migration.** Add the columns, the new CHECK, and the backfill `UPDATE` (newest `moved` event per spool whose `data->>'to'` is a live dry box; see "Dry box" in Task 2 for the test). Add the fields to the models and to `_SPOOL_COLUMNS`/`_hydrate` in `filament/spools.py`.
- [ ] **Step 4: Run** the test in CI plus `ruff check` and `mypy` locally. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): dry box schema, spool home and waste event kind (#7167)`.

### Task 2: One spool per dry box, and a spool's home

**Files:**
- Create: `backend/scadbuddy/filament/dry_box.py`
- Modify: `backend/scadbuddy/filament/locations.py` (`move_on`, `update`), `backend/scadbuddy/filament/spools.py` (the create-with-place writer near line 962, import, invoice confirm paths), `backend/scadbuddy/filament/invoices.py`, `backend/scadbuddy/api/filament.py` (route descriptions: 409)
- Test: `backend/tests/filament/test_dry_box.py`, `backend/tests/api/test_filament_dry_box.py`

**Interfaces:**
- Produces in `dry_box.py`:
  - `def is_dry_box(conn: Conn, location_id: UUID) -> bool` (kind `dry_storage` and `capacity = 1`, place unarchived)
  - `def occupant_on(conn: Conn, location_id: UUID, *, but: UUID | None = None) -> UUID | None` (the unarchived spool there; locks the place row `FOR UPDATE` first, then reads)
  - `class BoxFullError(ValueError)` with `occupant: UUID` (API answers `409 box_full`)
  - `def keep_home_on(conn: Conn, spool_id: UUID, location_id: UUID | None) -> None` (sets `home_location_id` when the target is a dry box; clears `home_asked_at` on any move)
- Consumes: Task 1 columns.

- [ ] **Step 1: Write failing tests** in `test_dry_box.py`:
  - `test_move_into_empty_box_sets_home`: `move_on` into a box sets `home_location_id`.
  - `test_move_into_full_box_raises_box_full`: names the occupant; no event, no change.
  - `test_move_to_shelf_keeps_home`: home stays the last box.
  - `test_same_spool_into_its_own_box_is_not_full`.
  - `test_archived_occupant_does_not_fill_the_box`.
  - `test_every_location_writer_respects_a_full_box` (Review Focus 1): create with a place, CSV import row with a place, invoice confirm with a place.
  - `test_two_concurrent_moves_into_one_box_one_wins` (Review Focus 2): two threads; one `BoxFullError`.
  - `test_cannot_make_a_place_with_two_spools_a_box`: `LocationStore.update` setting `capacity = 1` on a dry-storage place holding two spools raises `BoxHasManyError` (API 422 `box_has_many`).
  - Route test: `POST /spools/{id}/move` into a full box answers 409 `box_full` with the occupant's id in the body.
- [ ] **Step 2: Run** in CI. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `dry_box.py`; call `occupant_on` and `keep_home_on` from `move_on` (after the target check, before the UPDATE) and from the other `location_id` writers. A tray place and the external holder are never boxes. The Bambuddy pull's placement, if it hits `BoxFullError`, leaves the spool unplaced and logs; it never fails the pull.
- [ ] **Step 4: Run** the tests in CI, and `ruff`, `mypy` locally. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): a dry box holds one spool and a spool keeps its home (#7167)`.

### Task 3: Non-print waste on archive

**Files:**
- Modify: `backend/scadbuddy/filament/spools.py` (`archive`, a new `archive_on`, `_archive`), `backend/scadbuddy/filament/ledger.py` (`closing`, `refresh_derived` fast path), `backend/scadbuddy/filament/models.py`, `backend/scadbuddy/api/filament.py` (archive route description), `agent/src/tools/filament.ts` (`filament_spool_archive` description: the answer's `closing.non_print_waste_g`), `docs/features/spools-and-filament.md`
- Test: `backend/tests/filament/test_archive_waste.py`

**Interfaces:**
- Produces:
  - `def archive_on(conn: Conn, spool_id: UUID, *, reason: m.ArchiveReason | None, final_reading_g: float | None, final_grams: float | None, actor: str, cause: str | None = None) -> m.Spool | None` on `FilamentSpoolStore`: the body of today's `archive` for `archived=True`, now callable inside a caller's transaction. `archive` calls it.
  - Event: after the final weighing and before `archived`, when `final_filament_g > 0` and `reason != "respooled"`, book `ledger.record(conn, id, "non_print_waste", source="person", absolute_g=0, actor=actor, data={"waste_g": left, "cause": cause or "archive"})`.
  - `Closing.non_print_waste_g` is filled by `ledger.closing` from the same number.
- Consumes: Task 1 kind and field.

- [ ] **Step 1: Write failing tests** in `test_archive_waste.py`:
  - `test_archive_with_grams_left_books_waste_and_zeroes`: spool at 120 g, archive `damaged` with no weighing: one `non_print_waste` event, `data.waste_g == 120`, `grams_before 120`, `grams_after 0`; `grams_left == 0`; `closing.non_print_waste_g == 120`.
  - `test_final_weighing_sets_the_waste`: final grams 35: waste 35, not the ledger's number.
  - `test_finished_at_zero_books_no_waste`.
  - `test_respooled_books_no_waste` (Review Focus 3).
  - `test_no_reading_no_waste_event_and_no_null_grams` (a spool with no reading).
  - `test_waste_is_not_counted_as_a_plate`: `metrics.plates` of the spool ignores the event.
  - `test_bulk_archive_and_pull_archive_book_no_waste` (pins the ruling).
  - `test_restore_after_waste_keeps_zero_grams`.
- [ ] **Step 2: Run** in CI. Expected: FAIL.
- [ ] **Step 3: Implement.** `ledger.record` already anchors a `person` reading; check `_on_top` and `_check_empty` do not add a shopping row twice (a spool reaching 0 calls `add_for_empty_on`; archive of a spool at 0 g must not queue a purchase: pass through only when the spool was not archived before this event, or skip with `data.cause` — decide by the test `test_waste_zero_does_not_add_a_shopping_row`, add it).
- [ ] **Step 4: Run** in CI; `ruff`, `mypy`, agent `pnpm typecheck` locally. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): book grams left at archive as non-print waste (#7167)`.

### Task 4: Home return: the identified spool goes into the box

**Files:**
- Create: `backend/scadbuddy/filament/homes.py`
- Modify: `backend/scadbuddy/filament/capture.py` (unload branch near line 305), `backend/scadbuddy/filament/spools.py` (`list` filter), `backend/scadbuddy/api/filament.py` (`move_spool`, `list_spools`), `agent/src/tools/filament.ts` (`filament_spool_move`, `filament_spool_list`), `agent/src/sessions/touched.ts` (check the `filament_spool_move` entry still names the right keys; add none unless a field map needs it)
- Test: `backend/tests/filament/test_homes.py`, `backend/tests/filament/test_capture.py` (new cases), `backend/tests/api/test_filament_dry_box.py`, `agent/test/` (the tool's route and body)

**Interfaces:**
- Produces in `homes.py`:
  - `def move_into(conn: Conn, spool_id: UUID, location_id: UUID | None, *, if_occupied: Literal["refuse", "replace"], actor: str) -> m.SpoolEvent | None`: `refuse` is `move_on` (raises `BoxFullError`). `replace`, for a dry box that holds another spool: `FilamentSpoolStore.archive_on(conn, occupant, reason="other", final_reading_g=None, final_grams=None, actor=actor, cause="replaced")`, then `move_on`. `spool_id` is always the spool the person identified; the server never picks it. Locks in the order place, occupant, mover.
  - When the identified spool is not the unloaded spool, the unloaded spool keeps no place, and its `home_asked_at` is cleared (it was answered): it stays in the list of unplaced spools as today.
  - `FilamentSpoolStore.list(..., awaiting_home: bool | None = None)`: keeps spools with `home_asked_at` set, no place, and an unarchived home that is still a dry box.
  - `GET /filament/spools?awaiting_home=true` (existing route, new query parameter).
  - `POST /filament/spools/{id}/move` body `{location_id, if_occupied, answers}` (existing route, new optional fields; `answers: UUID | None` names the unloaded spool whose prompt this move answers, so its `home_asked_at` clears when another spool is identified).
- Consumes: Tasks 1 to 3.

- [ ] **Step 1: Write failing tests.**
  - `test_homes.py`: `test_replace_archives_the_occupant_and_books_waste` (occupant 80 g: archived, `grams_left 0`, one waste event with `cause == "replaced"` and `waste_g == 80`; the identified spool is in the box); `test_replace_with_empty_box_is_a_plain_move` (Review Focus 4); `test_identified_spool_is_the_occupant_is_a_noop`; `test_refuse_is_the_default_and_answers_box_full`; `test_replace_is_atomic` (a failure in the move rolls the archive back); `test_answering_with_another_spool_clears_the_unloaded_spools_prompt`.
  - `test_capture.py`: `test_unload_of_a_spool_with_a_live_home_parks_it_and_asks` (location none, `home_asked_at == seen_at`, `unloaded` event as before); `test_unload_without_a_home_goes_back_as_today`; `test_unload_with_an_archived_home_goes_back_as_today` (Review Focus 5); `test_resent_report_asks_once`; `test_asked_flag_clears_on_move_archive_and_reload`.
  - Route tests: `GET /spools?awaiting_home=true` lists only the asked spools; `POST …/move` with `if_occupied: "replace"` end to end; `if_occupied: "assume_new"` and any other unknown value is 422.
  - Agent: `filament_spool_move` sends `if_occupied` and `answers` only when given, and its description says the spool must be the one the user identified (scan, code or a named spool), never a guess; `filament_spool_list` accepts `awaiting_home`.
- [ ] **Step 2: Run** in CI. Expected: FAIL.
- [ ] **Step 3: Implement** `homes.py`; the capture change stays inside capture's savepoint and its tray-lock rules (it moves a spool only through `LocationStore.move_on`); route code calls `homes.move_into` in `asyncio.to_thread` as `move_spool` does today; update both agent tool descriptions and `touched.ts` if needed.
- [ ] **Step 4: Run** in CI; `ruff`, `mypy`, agent `pnpm typecheck` and `eslint` locally. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): home return puts the identified spool in the box (#7167)`.

### Task 5: Metrics keep non-print waste apart

**Files:**
- Modify: `backend/scadbuddy/filament/metrics_models.py`, `backend/scadbuddy/filament/metrics_store.py` (`_read`, `inventory`), `backend/scadbuddy/filament/metrics.py` (`spool_metrics`), `backend/scadbuddy/filament/ledger.py` (a reader `waste_events(conn)`, next to `plate_events`), `backend/scadbuddy/filament/metrics_export.py`, `backend/scadbuddy/api/filament.py` (metrics route descriptions), `agent/src/tools/filament.ts` (`filament_metrics` description), `frontend/src/pages/filament/MetricsTab.tsx`, its test, `frontend/src/mocks/` (the metrics fixture), `docs/features/spools-and-filament.md`
- Test: `backend/tests/filament/test_metrics_waste.py`, `frontend/src/pages/filament/MetricsTab.test.tsx`

**Interfaces:**
- Produces:
  - `m.SpoolMetrics.non_print_waste_g: float = 0.0`
  - `mm.NonPrintWasteRow(dimension: WasteDimension, key: str | None, label: str, wasted_g: float)` (dimension `material` or `type`; no `printer`: it has no printer)
  - `mm.InventoryMetrics.non_print_waste_g: float` and `non_print_waste: list[NonPrintWasteRow]`, by the event's day within `since..until` (UTC)
  - `ledger.waste_events(conn, spool_ids=None) -> dict[UUID, list[m.SpoolEvent]]` (partial index not needed; filter `kind = 'non_print_waste'`)
- Existing `wasted_g` fields keep their meaning (failed and cancelled plates). Their descriptions now say so.

- [ ] **Step 1: Write failing tests.**
  - `test_non_print_waste_is_not_in_wasted_g_or_used_g`: a spool with one failed plate (10 g) and one archive waste (50 g): `wasted_g == 10`, `used_g` unchanged, `non_print_waste_g == 50`.
  - `test_inventory_waste_rows_do_not_change_rate`: `WasteRow.rate` is the same with and without the waste event.
  - `test_non_print_waste_by_material_and_type_in_range`: an event outside `since..until` is left out; archived spools count (waste is lifetime of the event, not of the stock).
  - `test_export_has_a_separate_column` (metrics_export).
  - Frontend: the "Waste" section keeps its text and shows a second block "Non-print waste" with its own total and rows; a fixture with no waste shows none.
- [ ] **Step 2: Run** in CI. Expected: FAIL.
- [ ] **Step 3: Implement.** Load the `frontend-design` skill first for the MetricsTab block; follow the existing `Section` and chart helpers in `MetricsTab.tsx`; add the field to the mock fixture so unit tests and the preview mocks match the spec.
- [ ] **Step 4: Run** in CI; `ruff`, `mypy`, `pnpm typecheck`, `pnpm exec eslint` locally. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): metrics keep non-print waste apart from print waste (#7167)`.

### Task 6: Dry box and prompt UI

**Files:**
- Create: `frontend/src/components/filament/HomeReturnDialog.tsx` (+ `.test.tsx`), `frontend/src/components/filament/IdentifySpool.tsx` (+ `.test.tsx`: the three ways to identify a spool, for reuse)
- Modify: `frontend/src/lib/needsYou.ts` (+ its test), the Needs-you list and bell components that render items (find by `needsYouItems` use), the spool page (`frontend/src/pages/filament/` spool detail), `frontend/src/components/filament/LocationTree.tsx` and the location edit form (a "Dry box" toggle), `frontend/src/components/filament/ArchiveSpoolDialog.tsx` (shows the waste the archive will book, from the closing preview), `frontend/src/mocks/` (handlers for `POST /filament/spools/{id}/move` with `if_occupied`, `GET /filament/spools?awaiting_home=true`, 409 `box_full`), `frontend/e2e/` (one spec if the dialog has an e2e neighbour), `frontend/CLAUDE.md`
- Test: `HomeReturnDialog.test.tsx`, `needsYou.test.ts`, `LocationTree` test, `ArchiveSpoolDialog.test.tsx`

**Interfaces:**
- Consumes: Task 4's route and query; Task 3's `closing.non_print_waste_g` (the closing preview carries it).
- Produces: `NeedsYouItem` kind `home-return` (`{kind, key: 'home-return:<spool_id>', since: home_asked_at, spool}`); `HomeReturnDialog({spool, onDone})`; `IdentifySpool({onIdentified}: {onIdentified: (spool: Spool) => void})`.

- [ ] **Step 1: Load `frontend-design`. Write failing tests.**
  - `needsYou.test.ts`: `home-return` items sort by `home_asked_at` with the others.
  - `IdentifySpool.test.tsx`: three ways, none chosen at first. "Scan tag" (shown only where Web NFC exists, as the scan page tests it) posts the read to `POST /filament/tags/reads`: `open_spool` identifies that spool; `bind_or_create` shows "This tag is not on a spool yet" with "Make a spool from this tag" (creates from `draft`, then identifies it) and a link to pick by hand. "QR code": a code field (`S-12`) resolved by `GET /codes/{code}`, plus "Use camera" only where `window.BarcodeDetector` exists; a code that is not a spool says so. "Pick by hand": a search over `GET /filament/spools` (unarchived), with no row selected until the person clicks one.
  - `HomeReturnDialog.test.tsx`: the title says "S-12 came out of <printer> <slot>. Which spool goes into <box>?". No spool is selected and "Put in box" is disabled until `IdentifySpool` identifies one. Identified = the box's spool or an empty box: sends `{location_id: home, if_occupied: 'refuse', answers: <unloaded id>}`. Identified ≠ the box's spool: shows that the box's spool (S-7) is archived at 0 g and its N g are booked as non-print waste, and "Put in box" sends `if_occupied: 'replace'`. A 409 `box_full` after a race re-reads and shows the new occupant. Keyboard and focus: the dialog traps focus, Escape closes it with nothing sent.
  - `LocationTree` test: the "Dry box" toggle is offered only for a dry-storage kind and sets `capacity: 1`; a 422 `box_has_many` shows its text.
  - `ArchiveSpoolDialog.test.tsx`: the preview shows "N g will be booked as non-print waste", none for 0 g and none for reason "respooled".
- [ ] **Step 2: Run** `pnpm exec vitest run <files>` after `pnpm gen:api` in CI. Expected: FAIL.
- [ ] **Step 3: Implement** the item source (poll the same cadence as the other sources), the dialog, the spool page banner that opens the same dialog, the toggle and the archive text.
- [ ] **Step 4: Run** `pnpm typecheck` and `pnpm exec eslint` locally; tests in CI. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): the home-return prompt and the dry box toggle (#7167)`.

### Task 7: Dry box labels and docs sweep

**Files:**
- Modify: `frontend/src/components/filament/LocationTree.tsx` (a "Print label" action on a dry box, if the place label action is not already shown for every place), `backend/scadbuddy/filament/labels.py` only if a test shows a dry box is refused, `docs/features/spools-and-filament.md`, `docs/features/inventory.md`, `backend/CLAUDE.md` (the `filament/` layout: `dry_box.py`, `homes.py`), `frontend/CLAUDE.md` (the new component and item kind), `docs/architecture/` (the data and ledger pages if they list event kinds)
- Test: `backend/tests/filament/test_label_sheets.py` (new case), `LocationTree` test

**Interfaces:** Consumes the place labels of #6955 (`L-<n>`, `/x/<code>`): a dry box is a place, so its label is the place label. Produces no new label kind.

- [ ] **Step 1: Write failing tests.** `test_dry_box_gets_a_place_label`: the existing place-label route and sheet accept a dry box's id and give code `L-<number>`. `LocationTree`: a dry box row has a "Print label" action that opens the existing label flow.
- [ ] **Step 2: Run** in CI. Expected: the backend case passes at once if no change is needed (then keep it as a pin); the UI case FAILS.
- [ ] **Step 3: Implement** the action. Then grep the docs for text this plan made false and fix every copy: the archive description ("what the archive books"), the unload sentence in `capture.py`'s module docstring ("moves back to the place it came from"), `ledger.py`'s event list, the metrics descriptions of "waste", and the `EventKind` lists in docs and in `frontend/src/components/filament/eventText.ts` (add a text for `non_print_waste`, with its test).
- [ ] **Step 4: Run** `ruff`, `mypy`, `pnpm typecheck`, `pnpm exec eslint` locally; markdownlint config is in the `markdown` CI job. Expected: clean.
- [ ] **Step 5: Commit** `docs(filament): dry boxes, home return and non-print waste (#7167)`.

---

## Open risks

- **#7143 must merge first** (PR #8123). Cut this plan's branch from main after it merges; Task 6 adds its `home-return` item to the same Needs-you list and follows the shell-owned dialog pattern of `useUnknownSpoolDialog`.
- **The owner's ruling replaced "assume new"** (see Rulings). The server never chooses the spool; a guessed spool from the agent is the risk, so the agent tool's description says the spool must be one the user identified.
- **The unload behaviour changes.** A spool with a live home now has no place until answered (before: it went back to its earlier place). If the owner never answers, the spool stays unplaced. Task 4 tests pin this; say so in the PR.
- **`capacity` was never enforced.** A place that already has a `capacity` of 1 and a dry-storage kind becomes a box on deploy. The migration backfill and Task 2 must list such places in the PR body, since two spools in one would make `BoxHasManyError` on its next edit.
- **No new route.** If a reviewer finds the `if_occupied` field on `move` or the `awaiting_home` filter unfit, the answer needs a Go route in `tools/go/core`; stop and ask.
- **Fact (c4f175d1, fleet chat DM 2026-11-27 04:03Z):** the Go tag decode route (#7905) keeps `POST /api/v1/filament/tags/reads` and `TagReadResult` backward-compatible: it adds fields only, and removes or renames none. A Go path for MIFARE reads would answer a superset. Check c4f175d1's PR before Task 6.
