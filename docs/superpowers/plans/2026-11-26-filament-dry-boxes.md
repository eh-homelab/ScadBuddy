# Filament Dry Boxes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A dry box holds one spool. When a spool comes out of the AMS and it went into its tray from its home box (a place that is still a live dry box), ScadBuddy asks which spool goes into that box (P18). A spool taken from a shelf goes back to the shelf. The person identifies the spool for the box (RFID tag, QR code or a pick by hand). When the identified spool is not the box's spool, the box's spool is archived at 0 g. Grams left on an archived spool are booked as non-print waste, which metrics keep apart from print waste.

**Architecture:** A dry box is a location whose kind has `dry_storage` and whose existing `capacity` is 1. No new table. A spool gets a `home_location_id` (its last dry box) and a `home_asked_at` (set when capture unloads it). The person identifies the spool with routes that exist today (`POST /filament/tags/reads` for a tag, `GET /codes/{code}` for a QR code or a typed code, `GET /filament/spools` for a pick). The answer is the existing `POST /filament/spools/{id}/move` with three new request fields: `if_occupied`, `answers` and `expected_occupant_id`. A replace must send `expected_occupant_id` (422 `expected_occupant_required`), and the dialog sends it (P28, P29). Waste is one new ledger event kind, `non_print_waste`, written by the archive code. Metrics add a separate figure.

**Tech Stack:** Python (FastAPI, psycopg), Postgres migration, React with msw mocks, the agent's TypeScript tools.

**Spec:** Issue #7167 and its ruling comment; epic #7171; `docs/superpowers/specs/2026-11-23-unknown-spool-flow-design.md` (the non-goal "Dry boxes, a spool's home, waste on archive (slice 3)"); `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§5.5 ledger); `docs/superpowers/specs/2026-11-22-printer-location-labels-design.md` (place labels, #6955).

## Rulings (binding; do not reopen)

- Ruling: A dry box is a filament location with a limit of one spool. It uses the existing `filament_locations.capacity` (integer, nullable, today never enforced) on a location whose kind has `dry_storage`. No container table. — Inventory epic #6949 K2 owns containers and would have to migrate a table made here. — K2 must read `capacity = 1` on dry-storage places as boxes; a small data step.
- Ruling: Waste on archive and the home return are built in Python, in `backend/scadbuddy/filament/` and `backend/scadbuddy/api/filament.py`. — Inventory K4 ports today's filament code to Go unchanged, so it carries this code with it. — K4 ports a little more code.
- Ruling: No new HTTP route is added in Python. The answer (the identified spool goes into the box) is the existing `POST /filament/spools/{spool_id}/move` with a new request field; the prompt list is a new query parameter on the existing `GET /filament/spools`. — Owner policy, epic #7667: new routes go in Go. — If review finds these two do not fit, the new route is built in `tools/go/core`.
- Ruling: State is in Postgres. One new migration file, `backend/scadbuddy/migrations/20261127T0754Z_filament_dry_boxes.sql` (P1). — Root CLAUDE.md "Migrations". — A merged migration edited by mistake stops the API at start.
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

1. A box that is full: a move, a create with a place, a CSV import, a restore and a Bambuddy pull must all refuse or skip it the same way (`409 box_full`). An invoice confirm writes no place (P4). Test: `test_every_location_writer_respects_a_full_box`.
2. Two moves into one empty box at once: exactly one wins. Test: `test_two_concurrent_moves_into_one_box_one_wins`.
3. Archive of a spool with 0 g, with no reading, or with reason `respooled`: no waste event, no negative or null grams. Tests in Task 3.
4. The identified spool is the box's spool, or the box is empty: a plain move, no archive, no waste. The identified spool is the unloaded spool: it goes home. Tests in Task 4.
5. A spool whose home box was archived or turned into a non-box: no prompt, today's unload behaviour. Test in Task 4.

---

### Task 1: Schema and models

**Files:**
- Create: `backend/scadbuddy/migrations/20261127T0754Z_filament_dry_boxes.sql` (P1)
- Modify: `backend/scadbuddy/filament/models.py` (`EventKind` near line 1636, `Spool`, `SpoolMove` near line 830, `Closing` near line 1877)
- Test: `backend/tests/filament/test_dry_boxes_migration.py`

**Interfaces:**
- Produces: columns `filament_spools.home_location_id uuid REFERENCES filament_locations(id) ON DELETE SET NULL`, `filament_spools.home_asked_at timestamptz`; `'non_print_waste'` in the kind CHECK and in `EventKind`; `m.Spool.home_location_id: UUID | None`, `m.Spool.home_asked_at: datetime | None`; `m.SpoolMove.if_occupied: Literal["refuse", "replace"] = "refuse"` (Task 4 adds `answers` and `expected_occupant_id`); `m.Closing.non_print_waste_g: float | None = None`.

- [ ] **Step 1: Write the failing test.** `test_migration_adds_home_columns_and_waste_kind`: after `migrate`, both columns exist; inserting an event of kind `non_print_waste` succeeds; kind `bogus` still fails the CHECK. `test_home_backfilled_from_moved_events`: a spool whose newest `moved` event `to` is a dry-storage place with `capacity = 1` gets that place as `home_location_id`; one whose newest move is to a shelf gets none.
- [ ] **Step 2: Run** `pytest backend/tests/filament/test_dry_boxes_migration.py -v` in CI (no local Postgres). Expected: FAIL (columns missing).
- [ ] **Step 3: Write the migration.** Add the columns, the new CHECK, and the backfill `UPDATE` (newest `moved` event per spool whose `data->>'to'` is a live dry box; see "Dry box" in Task 2 for the test). The migration is `20261127T0754Z_filament_dry_boxes.sql` (P1); its backfill uses `dry_box.BOX`, which adds `printer_id IS NULL` (P9). Add the fields to the models and to `_SPOOL_COLUMNS`/`_hydrate` in `filament/spools.py`.
- [ ] **Step 4: Run** the test in CI plus `ruff check` and `mypy` locally. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): dry box schema, spool home and waste event kind (#7167)`.

### Task 2: One spool per dry box, and a spool's home

**Files:**
- Create: `backend/scadbuddy/filament/dry_box.py`
- Modify: `backend/scadbuddy/filament/locations.py` (`move_on`, `update`), `backend/scadbuddy/filament/spools.py` (the create-with-place writer near line 962, import, restore), `backend/scadbuddy/filament/catalog.py` (a kind's `dry_storage` flip, P5), `backend/scadbuddy/api/filament.py` (route descriptions: 409). Invoices write no place, so `invoices.py` is unchanged (P4).
- Test: `backend/tests/filament/test_dry_box.py`, `backend/tests/api/test_filament_dry_box.py`

**Interfaces:**
- Produces in `dry_box.py`:
  - `def is_dry_box(conn: Conn, location_id: UUID) -> bool` (kind `dry_storage` and `capacity = 1`, place unarchived)
  - `def occupant_on(conn: Conn, location_id: UUID, *, but: UUID | None = None) -> UUID | None` (the unarchived spool there; locks the place row `FOR NO KEY UPDATE` first, then reads in a second statement, P8)
  - `class BoxFullError(ValueError)` with `occupant: UUID` (API answers `409 box_full`)
  - `def keep_home_on(conn: Conn, spool_id: UUID, location_id: UUID | None) -> None` (sets `home_location_id` when the target is a dry box; clears `home_asked_at` on any move)
- Consumes: Task 1 columns.

- [ ] **Step 1: Write failing tests** in `test_dry_box.py`:
  - `test_move_into_empty_box_sets_home`: `move_on` into a box sets `home_location_id`.
  - `test_move_into_full_box_raises_box_full`: names the occupant; no event, no change.
  - `test_move_to_shelf_keeps_home`: home stays the last box.
  - `test_same_spool_into_its_own_box_is_not_full`.
  - `test_archived_occupant_does_not_fill_the_box`.
  - `test_every_location_writer_respects_a_full_box` (Review Focus 1): create with a place, CSV import row with a place; an invoice confirm writes no place (P4).
  - `test_two_concurrent_moves_into_one_box_one_wins` (Review Focus 2): two threads; one `BoxFullError`.
  - `test_cannot_make_a_place_with_two_spools_a_box`: `LocationStore.update` setting `capacity = 1` on a dry-storage place holding two spools raises `BoxHasManyError` (API 422 `box_has_many`).
  - Route test: `POST /spools/{id}/move` into a full box answers 409 `box_full` with the occupant's id in the body.
- [ ] **Step 2: Run** in CI. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `dry_box.py`; call `occupant_on` and `keep_home_on` from `move_on` (after the target check, before the UPDATE) and from the other `location_id` writers. A tray place and the external holder are never boxes. The Bambuddy pull's placement, if it hits `BoxFullError`, leaves the spool where it was and logs; it never fails the pull (P10). The pull's restore of a spool whose box another spool holds moves the restored spool to no place and logs (P7). A person's restore into a taken box is 409 `box_full` (P7). A kind's `dry_storage` flip over a capacity-1 place with two spools is 422 `box_has_many` (P5).
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
- [ ] **Step 3: Implement.** `ledger.record` already anchors a `person` reading; check `_on_top` and `_check_empty` do not add a shopping row twice. An archive adds one shopping row, as #6868 requires; the waste event adds none (`test_waste_zero_does_not_add_a_shopping_row`, P12). An archive, a restore and a second archive add one waste event and one shopping row in total (`test_re_archive_after_restore_keeps_one_waste_event_and_one_shopping_row`, P15). `closing(waste=True)` is opt-in: only `archive_on` and the closing preview pass it (P13). The waste is the raw grams, not rounded (P16).
- [ ] **Step 4: Run** in CI; `ruff`, `mypy`, agent `pnpm typecheck` locally. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): book grams left at archive as non-print waste (#7167)`.

### Task 4: Home return: the identified spool goes into the box

**Files:**
- Create: `backend/scadbuddy/filament/homes.py`
- Modify: `backend/scadbuddy/filament/capture.py` (unload branch near line 305), `backend/scadbuddy/filament/spools.py` (`list` filter), `backend/scadbuddy/api/filament.py` (`move_spool`, `list_spools`), `agent/src/tools/filament.ts` (`filament_spool_move`, `filament_spool_list`), `agent/src/sessions/touched.ts` (check the `filament_spool_move` entry still names the right keys; add none unless a field map needs it)
- Test: `backend/tests/filament/test_homes.py`, `backend/tests/filament/test_capture.py` (new cases), `backend/tests/api/test_filament_dry_box.py`, `agent/test/` (the tool's route and body)

**Interfaces:**
- Produces in `homes.py`:
  - `def move_into(conn: Conn, spool_id: UUID, location_id: UUID | None, *, if_occupied: Literal["refuse", "replace"], actor: str) -> m.SpoolEvent | None`: `refuse` is `move_on` (raises `BoxFullError`). `replace`, for a dry box that holds another spool: `FilamentSpoolStore.archive_on(conn, occupant, reason="other", final_reading_g=None, final_grams=None, actor=actor, cause="replaced")`, then `move_on`. `spool_id` is always the spool the person identified; the server never picks it. Locks as `LocationStore.move_on` takes them: the spools the move writes (the mover and the answered spool, in id order), then the box place, then the occupant's open tags, then the occupant's row (P11). A replace whose occupant is not `expected_occupant_id` is `BoxFullError` and archives nothing (P28).
  - When the identified spool is not the unloaded spool, the unloaded spool keeps no place, and its `home_asked_at` is cleared (it was answered): it stays in the list of unplaced spools as today.
  - `FilamentSpoolStore.list(..., awaiting_home: bool | None = None)`: keeps spools with `home_asked_at` set, no place, and an unarchived home that is still a dry box.
  - `GET /filament/spools?awaiting_home=true` (existing route, new query parameter).
  - `POST /filament/spools/{id}/move` body `{location_id, if_occupied, answers, expected_occupant_id}` (existing route, new optional fields; `answers: UUID | None` names the unloaded spool whose prompt this move answers, so its `home_asked_at` clears when another spool is identified; `expected_occupant_id: UUID | None` names the box's spool the person saw, and a replace without it is 422 `expected_occupant_required`, P28 and P29).
  - Capture parks the spool and asks only when it went into its tray from its home, and the home is still a live dry box. A spool taken from a shelf goes back to the shelf, and its home does not change (P18). "The box's spool" is the current occupant (P19).
- Consumes: Tasks 1 to 3.

- [ ] **Step 1: Write failing tests.**
  - `test_homes.py`: `test_replace_archives_the_occupant_and_books_waste` (occupant 80 g: archived, `grams_left 0`, one waste event with `cause == "replaced"` and `waste_g == 80`; the identified spool is in the box); `test_replace_with_empty_box_is_a_plain_move` (Review Focus 4); `test_identified_spool_is_the_occupant_is_a_noop`; `test_refuse_is_the_default_and_answers_box_full`; `test_replace_is_atomic` (a failure in the move rolls the archive back); `test_answering_with_another_spool_clears_the_unloaded_spools_prompt`.
  - `test_capture.py`: `test_unload_of_a_spool_with_a_live_home_parks_it_and_asks` (location none, `home_asked_at == seen_at`, `unloaded` event as before); `test_unload_without_a_home_goes_back_as_today`; `test_unload_with_an_archived_home_goes_back_as_today` (Review Focus 5); `test_unload_of_a_spool_taken_from_a_shelf_goes_back_to_the_shelf` (P18); `test_resent_report_asks_once`; `test_asked_flag_clears_on_move_archive_and_reload`.
  - Route tests: `GET /spools?awaiting_home=true` lists only the asked spools; `POST …/move` with `if_occupied: "replace"` and `expected_occupant_id` end to end; a replace without `expected_occupant_id` is 422 `expected_occupant_required`; a replace of another occupant than expected is 409 `box_full`; `if_occupied: "assume_new"` and any other unknown value is 422.
  - Agent: `filament_spool_move` sends `if_occupied`, `answers` and `expected_occupant_id` only when given (a zod `refine` requires `expected_occupant_id` with `replace`), and its description says the spool must be the one the user identified (scan, code or a named spool), never a guess; `filament_spool_list` accepts `awaiting_home`.
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
- Modify: `frontend/src/lib/needsYou.ts` (+ its test), the Needs-you list and bell components that render items (find by `needsYouItems` use), the spool page (`frontend/src/pages/filament/` spool detail), `frontend/src/pages/filament/LocationsTab.tsx` (the place form's "Dry box" switch, P25), `frontend/src/components/filament/ArchiveSpoolDialog.tsx` (shows the waste the archive will book, from the closing preview), `frontend/src/mocks/` (handlers for `POST /filament/spools/{id}/move` with `if_occupied`, `GET /filament/spools?awaiting_home=true`, 409 `box_full`), `frontend/e2e/` (one spec if the dialog has an e2e neighbour), `frontend/CLAUDE.md`
- Test: `HomeReturnDialog.test.tsx`, `needsYou.test.ts`, `LocationsTab.test.tsx` (P25), `ArchiveSpoolDialog.test.tsx`

**Interfaces:**
- Consumes: Task 4's route and query; Task 3's `closing.non_print_waste_g` (the closing preview carries it).
- Produces: `NeedsYouItem` kind `home-return` (`{kind, key: 'home-return:<spool_id>', since: home_asked_at, spool}`), from its own Needs-you source `homes` (P24); `HomeReturnDialog({spool, onDone})`; `IdentifySpool({onIdentified}: {onIdentified: (spool: Spool | null) => void})`: it calls `onIdentified(null)` when the person changes the way, edits the code or a read fails (P24).

- [ ] **Step 1: Load `frontend-design`. Write failing tests.**
  - `needsYou.test.ts`: `home-return` items sort by `home_asked_at` with the others.
  - `IdentifySpool.test.tsx`: three ways, none chosen at first. "Scan tag" (shown only where Web NFC exists, as the scan page tests it) posts the read to `POST /filament/tags/reads`: `open_spool` identifies that spool; `bind_or_create` shows "This tag is not on a spool yet" with "Make a spool from this tag" (creates from `draft`, then identifies it) and a link to pick by hand. "QR code": a code field (`S-12`) resolved by `GET /codes/{code}`, plus "Use camera" only where `window.BarcodeDetector` exists, and "Take a photo" through #8155's scan routes where it is missing (a 404 from those routes hides it, P23); a code that is not a spool says so. "Pick by hand": a search over `GET /filament/spools` (unarchived), with no row selected until the person clicks one.
  - `HomeReturnDialog.test.tsx`: the title says "S-12 came out of <printer> <slot>. Which spool goes into <box>?". No spool is selected and "Put in box" is disabled until `IdentifySpool` identifies one. Identified = the box's spool or an empty box: sends `{location_id: home, if_occupied: 'refuse', answers: <unloaded id>}`. Identified ≠ the box's spool: shows that the box's spool (S-7) is archived at 0 g and its N g are booked as non-print waste, and "Put in box" sends `if_occupied: 'replace'` with `expected_occupant_id` the box's spool it showed (P28, P29). A 409 `box_full` after a race re-reads and shows the new occupant. Keyboard and focus: the dialog traps focus, Escape closes it with nothing sent.
  - `LocationsTab.test.tsx` (P25): the "Dry box" switch is offered only for a dry-storage kind and sets `capacity: 1`; a 422 `box_has_many` shows its text.
  - `ArchiveSpoolDialog.test.tsx`: the preview shows "N g will be booked as non-print waste", none for 0 g and none for reason "respooled".
- [ ] **Step 2: Run** `pnpm exec vitest run <files>` after `pnpm gen:api` in CI. Expected: FAIL.
- [ ] **Step 3: Implement** the item source (poll the same cadence as the other sources), the dialog, the spool page banner that opens the same dialog, the toggle and the archive text.
- [ ] **Step 4: Run** `pnpm typecheck` and `pnpm exec eslint` locally; tests in CI. Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): the home-return prompt and the dry box toggle (#7167)`.

### Task 7: Dry box labels and docs sweep

**Files:**
- Modify: `frontend/src/components/filament/LocationTree.tsx` (a "Print label" action on every place row that is not a tray, P31), `backend/scadbuddy/filament/labels.py` only if a test shows a dry box is refused, `docs/features/spools-and-filament.md`, `docs/features/inventory.md`, `backend/CLAUDE.md` (the `filament/` layout: `dry_box.py`, `homes.py`), `frontend/CLAUDE.md` (the new component and item kind), `docs/architecture/` (the data and ledger pages if they list event kinds)
- Test: `backend/tests/api/test_filament_label_pdf.py` (new case, where the place-label route tests are, P30), `LocationTree` test

**Interfaces:** Consumes the place labels of #6955 (`L-<n>`, `/x/<code>`): a dry box is a place, so its label is the place label. Produces no new label kind.

- [ ] **Step 1: Write failing tests.** `test_a_dry_box_gets_a_place_label` in `tests/api/test_filament_label_pdf.py` (P30): the existing place-label route and sheet accept a dry box's id and give code `L-<number>`. `LocationTree`: every place row that is not a tray has a "Print label" action that opens the existing label flow (P31).
- [ ] **Step 2: Run** in CI. Expected: the backend case passes at once if no change is needed (then keep it as a pin); the UI case FAILS.
- [ ] **Step 3: Implement** the action. Then grep the docs for text this plan made false and fix every copy: the archive description ("what the archive books"), the unload sentence in `capture.py`'s module docstring ("moves back to the place it came from"), `ledger.py`'s event list, the metrics descriptions of "waste", and the `EventKind` lists in docs and in `frontend/src/components/filament/eventText.ts` (add a text for `non_print_waste`, with its test).
- [ ] **Step 4: Run** `ruff`, `mypy`, `pnpm typecheck`, `pnpm exec eslint` locally; markdownlint config is in the `markdown` CI job. Expected: clean.
- [ ] **Step 5: Commit** `docs(filament): dry boxes, home return and non-print waste (#7167)`.

---

## Decisions made while building

The rulings below were made while the plan was built (the ledger is
`.superpowers/sdd/2026-11-26-filament-dry-boxes/progress.md`). The task text above states what
was built; each line here says what was decided and why.

- P1. The migration takes the UTC timestamp of the day it was written (`20261127T0754Z`), not `20261126T1700Z`. Newer files were already on main, and a fresh name keeps the order clear.
- P2. The kind CHECK copies the newest file that holds `filament_spool_events_kind_check` (`20261123T0609Z_filament_spool_bb_fields.sql`) and adds `non_print_waste`. A kind left out of the CHECK makes inserts fail.
- P3. Task 2 needs no RED run; the reviewer checks that each test fails without `dry_box.py`. The tests already existed and passed, and a re-run against removed code adds nothing that CI does not.
- P4. Review Focus 1 drops the invoice confirm. Invoices call `create_on` only and write no place.
- P5. A kind's `dry_storage` flip is refused with 422 `box_has_many` when a capacity-1 place of that kind holds two spools or more. The box rule must hold on every way that a place becomes a box.
- P6. A kind's archive state does not change whether its places are boxes. `BOX` has the same meaning as the migration's backfill.
- P7. A person's restore into a box that another spool holds is 409 `box_full`. The Bambuddy pull's restore moves the restored spool to no place and logs. A person can choose; the pull must never fail.
- P8. `move_on` locks every target place `FOR NO KEY UPDATE` (no pre-read, no lock upgrade), and `occupant_on` takes the same lock. This removes a deadlock path; moves into one shelf wait on each other, which is fine at a person's rate.
- P9. `BOX` adds `printer_id IS NULL`, and the backfill has the same condition. A printer's place is never a box.
- P10. When Bambuddy's box is full, the pull leaves the spool where it was and logs it. Keeping the place is safer than taking it away.
- P11. The home return uses `move_on`'s lock order (the spools the move writes, then the box place, then the occupant's tags, then the occupant's row), not "place, occupant, mover". One order everywhere prevents deadlocks.
- P12. An archive adds one shopping row, as #6868 requires; the waste event adds none. The aim was no second row, not no row.
- P13. `closing(waste=True)` is opt-in: only `archive_on` and the closing preview pass it. Bulk archive, the pull and PATCH must not book waste.
- P14. The archive route still answers a `Spool`. The waste is read through `GET …/closing`, the spool detail's closing and the `archived` event, so no route answer changes.
- P15. An archive, a restore and a second archive give one waste event and one shopping row in total. A spool that is already empty asks for no second restock.
- P16. The waste is decided and written with the raw grams (no rounding to 0.1 g), in `waste_g` and in `grams_before`. One fact has one number.
- P17. A deadlock between a replace and a tag bind of the occupant's tag onto the mover is accepted. It needs two people acting on the same two spools at once, and Postgres aborts one; a full fix is not cheap.
- P18. Capture parks a spool and asks only when it went into its tray from its home and the home is still a live dry box. A spool taken from a shelf goes back to the shelf, and its home does not change. Returning it where the person took it assumes nothing about a box.
- P19. "The box's spool" is the box's current occupant. This matches the owner's words on #7167.
- P20. A P17 deadlock on the move route answers 409 `move_conflict` (try again), and nothing is written. P33 replaces the first ruling, which answered the generic 500.
- P21. The export's separate column is a new Prometheus series, `scadbuddy_filament_non_print_waste_grams`, by material. Prometheus is the only export, and an outcome label on the used grams would mix the two wastes.
- P22. The mocks' archive and replace move book `non_print_waste` events as the backend does, so the preview shows the figure.
- P23. `IdentifySpool`'s QR way offers "Take a photo" through #8155's scan routes where `BarcodeDetector` is missing; a 404 from those routes hides the button. The plan named #8155 as the fallback once it landed.
- P24. Home returns are their own Needs-you source, `homes`. A failed read of one source must not hide the items of another. `onIdentified` takes `Spool | null`, so the dialog forgets a spool when the person changes the way.
- P25. The "Dry box" switch and its tests are in `LocationsTab.tsx` and `LocationsTab.test.tsx`, where the place form is. `LocationTree` does not change for it.
- P26. Replaced by P32. It said that the mocks' kind PATCH need not answer `box_has_many`.
- P27. The dialog's title falls back to "came out of its tray" when the spool has no `moved` event. That event is the only record of the slot.
- P28. The move takes `expected_occupant_id`. A replace whose occupant is another spool is 409 `box_full`, so ScadBuddy never archives a spool that the person did not see (the owner's rule).
- P29. `expected_occupant_id` is required with `if_occupied: replace` (422 `expected_occupant_required`). An optional field left a path where an agent or an old client archives a spool nobody saw.
- P30. The label pin test is in `tests/api/test_filament_label_pdf.py`, where the place-label route tests are.
- P31. "Print label" shows on every place row that is not a tray, not only on dry boxes. A place's label is the same for any place.
- P32. The frontend mocks refuse what the backend refuses, and this replaces P26 (final review I3). A copy leaves a dry box out of the draft. A bulk create into a full box, or of two spools or more into one box, is 409 `box_full`. A restore into a taken box is 409 `box_full` with `occupant_id` on the archive route, PATCH and `bulk-restore`, and nothing is written. A kind's `dry_storage` flip over a capacity-1 place with two spools is 422 `box_has_many`. The constraint is that the mocks match the backend, and each fix is a few lines.
- P33. The PR review of #8270: `archive_on` and `homes.move_into` take the Filament settings as a parameter, and the caller reads them before it takes its connection, because the settings reader may take a pool connection of its own. A move that Postgres aborts in a deadlock answers 409 `move_conflict`, not a 500 (this changes P20).

## Open risks

- **#7143 must merge first** (PR #8123). Cut this plan's branch from main after it merges; Task 6 adds its `home-return` item to the same Needs-you list and follows the shell-owned dialog pattern of `useUnknownSpoolDialog`.
- **The owner's ruling replaced "assume new"** (see Rulings). The server never chooses the spool; a guessed spool from the agent is the risk, so the agent tool's description says the spool must be one the user identified.
- **The unload behaviour changes.** A spool that went into its tray from its home box, while that box is still a live dry box, now has no place until answered (before: it went back to its earlier place). A spool taken from a shelf goes back to the shelf (P18). If the owner never answers, the spool stays unplaced. Task 4 tests pin this; say so in the PR.
- **`capacity` was never enforced.** A place that already has a `capacity` of 1 and a dry-storage kind becomes a box on deploy. The migration backfill and Task 2 must list such places in the PR body, since two spools in one would make `BoxHasManyError` on its next edit.
- **No new route.** If a reviewer finds the `if_occupied` field on `move` or the `awaiting_home` filter unfit, the answer needs a Go route in `tools/go/core`; stop and ask.
- **Fact (c4f175d1, fleet chat DM 2026-11-27 04:03Z):** the Go tag decode route (#7905) keeps `POST /api/v1/filament/tags/reads` and `TagReadResult` backward-compatible: it adds fields only, and removes or renames none. A Go path for MIFARE reads would answer a superset. Check c4f175d1's PR before Task 6.
