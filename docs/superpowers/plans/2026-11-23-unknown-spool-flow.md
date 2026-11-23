# Unknown Spool Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a printer reports a loaded tray that no ScadBuddy spool holds, ScadBuddy records it, makes and links a spool at once for an RFID tray, asks the owner which spool an untagged tray holds, and refuses ScadBuddy's own prints to that printer until the owner answers.

**Architecture:** A table `filament_unknown_trays` holds one row per unmatched, untagged, loaded tray. A new capture step (`capture.note_unknown`, its own savepoint in the report write) writes and clears the rows and makes RFID spools. The API lists the rows with ranked candidates and resolves one in one transaction. `prepare_run` and the LAN worker's `lan_print` read the rows (in-process or through a new CRUD route) and refuse. The frontend adds a "Needs you" source, a resolve dialog and its entry points; the agent gets two tools.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres 18, pytest, Temporal activities; React 19, vitest, msw; the agent's TypeScript tools (zod, vitest).

**Spec:** `docs/superpowers/specs/2026-11-23-unknown-spool-flow-design.md`

**Issue:** #7143, in the spool onboarding epic [#7171](https://github.com/eh-homelab/ScadBuddy/issues/7171).

## Global Constraints

- Table `filament_unknown_trays`: `id`, `printer_id` (FK `printers`, `ON DELETE CASCADE`), `ams_id`, `tray_id`, `tray_uuid`, `tag_uid`, `material`, `colour_rgba`, `remain_pct` (nullable), `first_seen_at`, `seen_at`; unique `(printer_id, ams_id, tray_id)`. A row exists only while the tray is loaded and unmatched.
- Routes: `GET /api/v1/filament/unknown-trays`, `POST /api/v1/filament/unknown-trays/{id}/resolve`, and the worker's `GET /api/v1/internal/crud/print/filament/printers/{printer_id}/unknown-trays` (as the spec's D7; every print-worker CRUD route lives under `crud_router("print")`, `crud/print.py` `BASE`).
- Problem codes: `tray_changed` (409), `code_unknown` (404, reused from `api/codes.py`), `spool_archived` (409), `no_ams_kind` (409), and the block's `unknown_spool_loaded` (422). Use these strings everywhere: backend, mocks, frontend, agent.
- Block text, word for word: `"<printer name> has an unknown spool in <slot>: link it on the Filament page first."`; several slots join with `", "` and a final `" and "`. `<slot>` is `bambuddy/trays.py` `tray_label` ("AMS-A slot 2").
- Needs-you title: `"Unknown spool in <printer name> <slot>"` ("Unknown spool in H2C AMS-A slot 2").
- Codes parse as `codes/parse.py` `parse_code` (any case, dash optional); only prefix `S` names a spool here.
- Candidates: at most 10, unarchived spools only.
- Only the API touches the database (#5295). The worker reads the block through the CRUD route with its service credential.
- New migration file only; no new event kind (the resolve books `weighed`, capture books `created`, `loaded`, `moved`, `ams_reading`).
- Every capture write stays inside the report write's transaction, in a savepoint of its own; a failure is logged (`prints.FILAMENT_CAPTURE_FAILED`, `what="unknown_trays"`) and never fails the report write.
- A tray ScadBuddy can match is never recorded unknown. An untagged spool is never linked by a heuristic.
- Prints started in Bambuddy or on the printer are not blocked; only `prepare_run` and `lan_print` refuse.
- Backend tests: `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55433/scadbuddy_test`, at most `-n 4`. `tests/api` needs a `temporal` CLI on `PATH` (it skips without one; a skip is not a pass). Put `tests/api/` paths together in one command.
- Frontend and agent: `pnpm gen:api` before any `pnpm exec vitest`. Never commit `backend/openapi.json` or a `schema.d.ts`.
- Commits: conventional title ending `(#7143)`, body ending with exactly:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_01GDryexNaHm8StnBqUH7kRK`

## Settled "Left to the builder" items

- **Colour distance:** `bambuddy/filaments.py` `colour_distance` (CIEDE2000, alpha dropped), the one `close_spools` and `inventory.tray_agreement` use. The material check is `inventory.material_key` against `inventory.material_names` (#6706 keys), as `tray_agreement` does.
- **RFID type match:** no existing helper matches by material and colour alone (`SpoolTypeStore.match` needs brand, subtype and label weight; a tray reports none of them). New `SpoolTypeStore.match_tray`: the oldest unarchived type whose material has the tray's `material_key` and whose `rgba` equals the tray's `rgba`, a type with a preset whose `slicer_filament` equals the tray's `tray_info_idx` first. No match: a spool with no type, its `material_id` from `catalog.resolve_or_add(conn, "materials", material, sort_order=1000)` and the tray's `rgba`. That is what the Bambuddy pull does (`bambuddy_pull._create` makes a typeless spool; it never makes a type), and type proposals group it later.
- **Polling cadence:** `ATTENTION_POLL_MS` (15 s, `frontend/src/agent/attention.ts:26`), plus a read on `visibilitychange` and `focus`, as `useGrantRequests` (`frontend/src/lib/needsYou.ts`) reads the flow grants.
- **Test split:** row SQL and capture in `tests/filament/`; store, ranking and resolve in `tests/filament/test_unknown_trays.py`; routes in `tests/api/`; the CRUD route in `tests/crud/`; the block in `tests/printers/`; dialog and sources in vitest.

## Decisions the code forced (deviations from the spec's wording)

D1 to D5 are the spec's D1 to D5 ("Decisions made while planning"), with the reasons the code gave. P1 is this plan's own.

- **D1 `seen_at` is a version, not a heartbeat.** Capture runs only when the tray projection changes (`capture.trays_changed`), and a change in any tray of the printer runs it. If every run refreshed `seen_at`, a change in slot 1 would refuse a resolve of slot 2. The upsert moves `seen_at` only when the row's reported fields (`tray_uuid`, `tag_uid`, `material`, `colour_rgba`, `remain_pct`) change.
- **D2 External holders (ams 255) never get a row.** `vt_tray` has no presence state; `_filled` is true whenever a `tray_type` is configured, so an empty external holder would block the printer for good. An RFID external tray is still auto-created. Decided 2026-11-23 (spec "Decisions made while planning").
- **D3 `remain_pct` is stored but is NULL in practice.** `trays._remain` gives a remain only for a tray with RFID keys (#6263 preflight C4), and only untagged trays get rows. The prefill code exists; it fires only if `_remain` changes. Decided 2026-11-23 (spec "Decisions made while planning").
- **D4 The label weight override applies only to a first weight.** The spec's rule makes `label_weight_g` the typed net weight when it differs from the type's. For a half-used spool that sets the label to the grams left and breaks every `remain_pct` reading. The override applies only when the spool has no event other than `created`, `edited`, `moved`, `tag_bound` and `tag_unbound` (no reading, use or load yet). The `weighed` reading is booked either way. Decided 2026-11-23 (spec "Decisions made while planning").
- **D5 "The owner is told" of an RFID spool** = the list route also returns the spools capture made from tags in the last 24 h (`made_from_tags`), and the frontend toasts each new one once. The spec does not say how.
- **P1 The rows' SQL lives in its own module** (`filament/unknown_tray_rows.py`) because `inventory.py` must clear a row inside `link_on`, and `unknown_trays.py` imports `inventory.py`.

## Review Focus

1. **A stale row blocks a printer after the tray was settled another way.** This covers the old tray link (`PUT /printers/{id}/trays/{ams}/{tray}/spool`), the print dialog's tray question (`api/printing.py` `post_tray_spool`, which calls `link`), and a capture that read "no holder" while a resolve was committing. Expected: no row once the tray has a holder. `link_on` deletes the row in its transaction. Capture writes a row only under `pg_try_advisory_xact_lock` on the tray's key, and it checks the holder again after it takes the lock. Tests in Task 2 and Task 3.
2. **An external holder with a configured type and no spool** (D2): no row, no block. Test in Task 2.
3. **Two people resolve one tray at once, or the tray changes during a resolve:** one wins; the other gets 409 `tray_changed`, and nothing of it is written. Test in Task 3.
4. **A tray flips unknown → known → unknown between reports:** the second unknown is a new row (new `id`, new `first_seen_at`); a resolve sent with the first id gets 409 `tray_changed`. Tests in Task 2 and Task 3.
5. **An RFID spool made by capture, then Bambuddy's pull of the BB spool it auto-added:** the pull links that BB spool to the SB spool (`bambuddy_pull._holder` finds the unlinked spool that holds the tag); it never makes a second one. Test in Task 2.
6. **Resolving with an archived spool:** 409 `spool_archived`. **With a spool in another tray:** that spool moves; the other tray's place no longer holds it. **With a code that names a place (`L-3`):** 404 `code_unknown`. Tests in Task 3.
7. **No location kind flagged `ams`:** an untagged tray still gets its row (with no place); an RFID tray falls back to a row; a resolve answers 409 `no_ams_kind`. Tests in Task 2 and Task 3.
8. **A printer deleted or merged:** deleting it removes its rows (cascade); merging it drops the merged printer's rows (the kept printer's next capture writes its own). Tests in Task 1.
9. **The CRUD read fails at `lan_print`:** the start goes on (logged), since `prepare_run` checked moments before. A failed read in `prepare_run` fails the run like any other read. Test in Task 4.

---

### Task 1: The table, its rows and the wire types

**Files:**
- Create: `backend/scadbuddy/migrations/20261123T1200Z_filament_unknown_trays.sql` (any UTC stamp that sorts after `20261123T0211Z_storefront_renders_kind.sql`; take `$(date -u +%Y%m%dT%H%MZ)` when you create it)
- Create: `backend/scadbuddy/filament/unknown_tray_rows.py`
- Modify: `backend/scadbuddy/printers/store.py` (`PRINTER_REFERENCES`, after the `filament_override_slots` entry at :395)
- Test: `backend/tests/filament/test_unknown_tray_rows.py`

**Interfaces:**
- Produces (all in `filament/unknown_tray_rows.py`):
  - `class UnknownTrayRow(BaseModel)`: `id: UUID, printer_id: UUID, ams_id: int, tray_id: int, tray_uuid: str | None, tag_uid: str | None, material: str, colour_rgba: str | None, remain_pct: int | None, first_seen_at: datetime, seen_at: datetime`.
  - `note_on(conn: Conn, printer_id: UUID, seen: SeenTray, seen_at: datetime) -> UnknownTrayRow`: upsert. `first_seen_at` and `seen_at` are `seen_at` on insert. On conflict it sets the reported fields and `seen_at` only `WHERE (tray_uuid, tag_uid, material, colour_rgba, remain_pct) IS DISTINCT FROM EXCLUDED's` (D1). `colour_rgba = seen.rgba`, `remain_pct = seen.remain`.
  - `clear_on(conn: Conn, printer_id: UUID, ams_id: int, tray_id: int) -> bool` (a row was deleted).
  - `rows_on(conn: Conn, printer_id: UUID | None = None) -> list[UnknownTrayRow]`, ordered by printer, `ams_id`, `tray_id`.
  - `class UnknownSpoolRef(BaseModel)`: `id: UUID, slot: str`.
  - `class UnknownSpoolBlock(BaseModel)`: `count: int, trays: list[UnknownSpoolRef]`.
  - `class UnknownSpoolReads(Protocol)`: `async def blocking(self, printer_id: str) -> list[UnknownSpoolRef]: ...`
  - `class PoolUnknownTrays`: `__init__(self, pool: ConnectionPool[Conn])`; `async def blocking(self, printer_id: str) -> list[UnknownSpoolRef]` (to_thread over `rows_on`; `slot = tray_label(ams_id, tray_id)`).
  - `UNKNOWN_SPOOL_LOADED: Final = "unknown_spool_loaded"`; `TRAY_CHANGED: Final = "tray_changed"`.
  - `blocked_detail(printer_name: str, refs: Sequence[UnknownSpoolRef]) -> str`: the Global Constraints text.

- [ ] **Step 1: Write the failing tests**

```python
def test_note_inserts_then_keeps_seen_at_while_nothing_changes(pg_pool): ...
    # note_on(T0) then note_on(T0+5s) with the same SeenTray: one row, first_seen_at == seen_at == T0
def test_a_changed_colour_moves_seen_at_only(pg_pool): ...
    # note_on(T0, red) then note_on(T1, blue): same id, first_seen_at T0, seen_at T1, colour_rgba "#0000FFFF"
def test_clear_deletes_one_tray_only(pg_pool): ...
    # rows at (0,1) and (0,2); clear_on(0,1) is True, again False; rows_on == [(0,2)]
def test_deleting_the_printer_deletes_its_rows(pg_pool): ...
    # DELETE FROM printers → rows_on(printer) == []
def test_merging_a_printer_drops_its_rows(pg_pool): ...
    # PrinterStore.merge(merged, into) (as tests/printers/test_store.py does): merged's rows gone, into's kept
def test_blocked_detail_names_every_slot() -> None:
    assert blocked_detail("H2C", [UnknownSpoolRef(id=A, slot="AMS-A slot 2")]) == (
        "H2C has an unknown spool in AMS-A slot 2: link it on the Filament page first.")
    # three refs → "in AMS-A slot 1, AMS-A slot 2 and HT-A: link it ..."
async def test_pool_blocking_lists_slots(pg_pool): ...
    # one row at (128, 0) → [UnknownSpoolRef(id=row.id, slot="HT-A")]
```

The printer fixture is `_printer` from `tests/filament/test_capture.py` (copy it; it is module-private).

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55433/scadbuddy_test uv run --frozen pytest tests/filament/test_unknown_tray_rows.py -v`
Expected: FAIL (`ModuleNotFoundError: scadbuddy.filament.unknown_tray_rows`).

- [ ] **Step 3: Write the migration, the module and the merge hook**

The migration: `CREATE TABLE filament_unknown_trays` with `id uuid PRIMARY KEY DEFAULT gen_random_uuid()`, `printer_id uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE`, `ams_id integer NOT NULL CHECK (ams_id BETWEEN 0 AND 255)`, `tray_id integer NOT NULL CHECK (tray_id BETWEEN 0 AND 15)`, `tray_uuid text`, `tag_uid text`, `material text NOT NULL`, `colour_rgba text`, `remain_pct integer CHECK (remain_pct BETWEEN 0 AND 100)`, `first_seen_at timestamptz NOT NULL`, `seen_at timestamptz NOT NULL`, `UNIQUE (printer_id, ams_id, tray_id)`. In `printers/store.py`, add `PrinterReference("filament_unknown_trays", "printer_id", repoint=_drop_unknown_trays)`; `_drop_unknown_trays` deletes the merged printer's rows, as `_drop_override_slots` does. Without it, `tests/printers/test_store.py`'s foreign-key coverage test fails.

- [ ] **Step 4: Run the tests and the printer store tests**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/filament/test_unknown_tray_rows.py tests/printers/test_store.py -n 4 -q`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat(filament): a table of the unknown trays a printer reports (#7143)`

---

### Task 2: Capture records unknown trays and makes RFID spools

**Files:**
- Modify: `backend/scadbuddy/filament/capture.py` (extract `_book_load` from `reconcile` :301-337; add `note_unknown`; call it in `reconcile_all` :361)
- Modify: `backend/scadbuddy/printers/reports.py` (`_capture` :129-187: a step `"unknown_trays"` after `"trays"`)
- Modify: `backend/scadbuddy/filament/inventory.py` (add `try_tray_lock` beside `_tray_lock` :838)
- Modify: `backend/scadbuddy/filament/types.py` (`SpoolTypeStore.match_tray`)
- Test: `backend/tests/filament/test_capture_unknown.py`

**Interfaces:**
- Consumes: `note_on`, `clear_on`, `rows_on` (Task 1).
- Produces:
  - `capture.note_unknown(conn: Conn, printer_id: str, report: Mapping[str, Any], seen_at: datetime, *, actor: str) -> UnknownTrayResult`.
  - `@dataclass(frozen=True) class UnknownTrayResult: made: int = 0; noted: int = 0; cleared: int = 0` (with `__add__`, as `CaptureResult`).
  - `capture.RFID_MADE: Final = "rfid_capture"`: the `data.by` of a `created` event capture books. Task 3's `made_from_tags` reads it.
  - `inventory.try_tray_lock(conn: Conn, printer_id: UUID, ams_id: int, tray_id: int) -> bool`: `pg_try_advisory_xact_lock(hashtext('filament_tray:<printer>:<ams>:<tray>'))`, the same key as `_tray_lock`.
  - `SpoolTypeStore.match_tray(conn: Conn, material: str, rgba: str | None, tray_info_idx: str | None) -> m.SpoolType | None` (static; `None` for no `rgba` or a material with no key).
  - `capture._book_load(conn, printer: UUID, name: str, seen: SeenTray, holder: UUID, found_by: FoundBy, *, actor: str, at: datetime, printing: bool, job: str | None) -> CaptureResult`: the existing `loaded` + `place_on` + `ams_reading` booking, which `reconcile` now calls.

`note_unknown` walks the keys `reconcile` walks (`_walk`), plus each existing row's key whose unit the report shows. A row of a unit the report does not show is left alone, as `_walk` leaves its spools. Per key:

```
key in loading_trays(status)              -> leave the row
seen is None (empty)                       -> clear_on
holder_on(conn, printer, seen) finds one   -> clear_on
seen has tray_uuid or tag_uid              -> with conn.transaction(): _spool_from_tag(...)   # savepoint per tray
                                              on any Exception: log FILAMENT_CAPTURE_FAILED (what="rfid_spool"),
                                              then fall through to the untagged branch (the owner sees a row)
ams_id == EXTERNAL                          -> no row (D2)
untagged                                   -> if not try_tray_lock: leave the row (a link or resolve is settling it)
                                              holder_on again; found -> clear_on
                                              place: ensure_ams_on in a savepoint (NoAmsKindError -> no place)
                                              note_on
```

`_spool_from_tag(conn, printer: UUID, name: str, seen: SeenTray, *, actor: str, at: datetime, printing: bool, job: str | None) -> UUID`, in this order:
1. `try_tray_lock`; if it is busy, raise `_TrayBusy` (caught: leave the tray to the next report).
2. `holder_on` again; a holder now means `clear_on` and return it.
3. The type is `SpoolTypeStore.match_tray(conn, seen.material, seen.rgba, seen.tray_info_idx)`. Insert `filament_spools` with `type_id`, else `material_id` (`resolve_or_add(conn, "materials", …, sort_order=1000)`) and `rgba`, as `bambuddy_pull._create` inserts. Then `ledger.record(conn, spool, "created", printer=printer, ams_id=…, tray_id=…, actor=actor, at=at, data={"by": RFID_MADE})` and `FilamentSpoolStore.read(conn, spool, check=True)`.
4. `FilamentSpoolStore.tag_on` then `bind_on(conn, spool, uid, tray, kind, None, actor)`; `kind` is `"bambu_mifare"` when `tray_uuid` is set, else `"other"`, as `FilamentInventory.link` picks it.
5. `_book_load(...)` with `found_by="tag"` (`ensure_ams_on`, `loaded`, `place_on`, `ams_reading` when `seen.remain` is set), then `clear_on`. Log at info: "made a spool from an unknown RFID tag", with `printer_id`, `ams_id`, `tray_id` and `number`.

`_capture` runs `"unknown_trays"` when `trays` is true, after `"trays"`. `reconcile_all` runs `note_unknown` after `reconcile` in a savepoint of its own (its own `try`, logged as `what="backstop_unknown"`), so the backstop heals a row that a missed or busy capture left.

- [ ] **Step 1: Write the failing tests** (helpers `_printer`, `_tray`, `_report`, `_spool`, `_link` as in `tests/filament/test_capture.py`; `_advance(pool, pid, report, at)` runs `PrinterReportStore.advance_in` in a transaction)

```python
def test_an_untagged_tray_with_no_holder_gets_a_row(pg_pool): ...
    # _advance(_report(_tray(2))): rows_on(pid) == one row (0, 2), material "PLA", colour_rgba "#FF0000FF", remain_pct None
def test_a_linked_tray_gets_no_row(pg_pool): ...
    # _link spool at (0,1), then _advance(_report(_tray(1))): rows_on == []
def test_emptying_the_tray_clears_its_row(pg_pool): ...
    # row at (0,2); next report _tray(2, tray_type="", state=0): rows_on == []
def test_a_loading_tray_keeps_its_row(pg_pool): ...
    # row at (0,2); next report _tray(2, state=5): the row is still there with its id
def test_a_unit_the_report_omits_keeps_its_rows(pg_pool): ...
    # row at (0,2); report with only unit "1": the row is kept
def test_a_tray_that_flips_known_and_back_gets_a_new_row(pg_pool): ...
    # row r1 at T0; link it (T1 report: no row); unlink, then a T2 report with a new colour: a row with id != r1.id, first_seen_at == T2
def test_an_external_holder_gets_no_row(pg_pool): ...
    # report {"vt_tray": {"id": "254", "tray_type": "PLA", "tray_color": "FF0000FF"}}: rows_on == []
def test_an_rfid_tray_gets_a_spool_of_the_matching_type(pg_pool): ...
    # a type (PLA, #FF0000FF, a preset slicer_filament "GFA00"); _advance(_report(_tray(1, U1, tray_info_idx="GFA00", remain=80)))
    # one new spool: type_id == that type, open binding of U1, location == the (0,1) tray place,
    # events created(data.by "rfid_capture"), tag_bound, loaded, moved, ams_reading(remain_pct 80); rows_on == []
def test_an_rfid_tray_with_no_type_gets_a_typeless_spool(pg_pool): ...
    # no type: the spool has type_id None, material_id of "PLA", rgba "#FF0000FF"
def test_an_archived_spools_tag_goes_to_the_new_spool(pg_pool): ...
    # U1 bound to an archived spool: a new spool holds U1; the old binding closed reason "rebound"
def test_the_pull_links_the_bb_spool_to_the_rfid_spool(pg_pool): ...
    # after the auto-create, bambuddy_pull._holder(conn, {"tag_uid": None, "tray_uuid": U1}, 7) == (new_spool, False)
def test_no_ams_kind_still_writes_the_rows(pg_pool): ...
    # archive every kind flagged ams; untagged (0,2) → a row (no place); RFID (0,1) → a row too, no spool made
def test_a_busy_tray_lock_writes_no_row(pg_pool): ...
    # another connection holds pg_advisory_lock on the tray's key; _advance → rows_on == []; the report is written
def test_a_failing_unknown_step_still_writes_the_report(pg_pool, monkeypatch, caplog): ...
    # monkeypatch capture.note_unknown to raise RuntimeError("secret"); reports.get(pid) is not None;
    # one FILAMENT_CAPTURE_FAILED record with what == "unknown_trays"; "secret" not in caplog.text
def test_the_backstop_writes_a_row_the_watch_missed(pg_pool): ...
    # _write_report (as test_capture.py) then capture.reconcile_all(pool, [pid], now=T0+1s): one row
def test_match_tray_prefers_the_preset_of_the_trays_idx(pg_pool): ...
    # two PLA #FF0000FF types, the newer with preset "GFA00": match_tray(conn, "PLA", "#FF0000FF", "GFA00") is the newer;
    # with idx None it is the older; rgba "#00FF00FF" → None
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/filament/test_capture_unknown.py -n 4 -v`
Expected: FAIL (`capture` has no `note_unknown`).

- [ ] **Step 3: Implement `try_tray_lock`, `match_tray`, `_book_load`, `note_unknown` and the `_capture` step as the Interfaces block says**

- [ ] **Step 4: Run the new tests, then every suite that writes reports**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/filament tests/printers tests/crud -n 4 -q`
Expected: PASS. A test that puts a report with an unbound tag (`lan-report-h2c.json`'s tray `0/1` holds tag keys) now gets a spool from capture; if one counts spools or events, make its count say so, and say in the commit body which tests changed and why.

- [ ] **Step 5: Lint and type-check**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: no errors.

- [ ] **Step 6: Commit** — `feat(filament): capture records unknown trays and makes RFID spools (#7143)`

---

### Task 3: List and resolve routes

**Files:**
- Create: `backend/scadbuddy/filament/unknown_trays.py`
- Modify: `backend/scadbuddy/filament/inventory.py` (`link` :654-723 → `link_on` + `link`)
- Modify: `backend/scadbuddy/filament/models.py` (the API models below)
- Modify: `backend/scadbuddy/filament/component.py` (`Filament.unknown_trays`)
- Modify: `backend/scadbuddy/api/filament.py` (two routes)
- Test: `backend/tests/filament/test_unknown_trays.py`, `backend/tests/api/test_filament_unknown_trays.py`

**Interfaces:**
- Consumes: Task 1's rows; Task 2's `RFID_MADE` and `try_tray_lock`.
- Produces:
  - `FilamentInventory.link_on(conn: Conn, printer_id: UUID, ams_id: int, tray_id: int, spool_id: UUID, seen: SeenTray, place_id: UUID, *, actor: str) -> None` (static). It holds today's transaction body, from `_tray_lock` to `bind_on`, then `clear_on(conn, printer_id, ams_id, tray_id)`. `link` keeps its signature and calls it.
  - Models in `filament/models.py`:
    - `UnknownSpoolCandidate`: `spool_id: UUID, number: int, code: str, material: str | None, rgba: str | None, grams_left: float | None, label_weight_g: int | None, location_id: UUID | None, location_name: str | None, same_material: bool, distance: float | None`.
    - `UnknownSpoolTray`: `id: UUID, printer_id: UUID, printer_name: str, ams_id: int, tray_id: int, slot: str, place_id: UUID | None, place_code: str | None, material: str, colour_rgba: str | None, remain_pct: int | None, first_seen_at: datetime, seen_at: datetime, candidates: list[UnknownSpoolCandidate]`.
    - `TagMadeSpool`: `spool_id: UUID, code: str, made_at: datetime, printer_name: str | None, slot: str | None`.
    - `UnknownSpoolTrays`: `trays: list[UnknownSpoolTray], made_from_tags: list[TagMadeSpool]`.
    - `UnknownSpoolResolve(_In)`: `spool_id: UUID | None = None, code: str | None = None, net_weight_g: Annotated[float, Field(gt=0, le=100_000)], seen_at: AwareDatetime`. A validator requires exactly one of `spool_id` and `code`.
  - `class UnknownTrayStore` in `filament/unknown_trays.py`: `__init__(self, pool: ConnectionPool[Conn], inventory: FilamentInventory, locations: LocationStore, defaults: Callable[[], FilamentDefaults])`; `list(self) -> m.UnknownSpoolTrays`; `resolve(self, tray_id: UUID, body: m.UnknownSpoolResolve, *, actor: str) -> m.Spool`.
  - `rank(row: UnknownTrayRow, spools: Sequence[m.Spool], keys: Mapping[UUID, frozenset[str]], *, limit: int = 10) -> list[m.UnknownSpoolCandidate]`.
  - Errors: `TrayChangedError(ValueError)`, `SpoolArchivedError(ValueError)`, `CodeUnknownError(LookupError)`.
  - Routes: `GET /filament/unknown-trays` (name `list_unknown_trays`, `response_model=m.UnknownSpoolTrays`) and `POST /filament/unknown-trays/{tray_id}/resolve` (name `resolve_unknown_tray`, `response_model=m.Spool`).

`rank` sorts every unarchived spool by this key and takes the first `limit`:

```python
reported = material_key(row.material)
def key(s: m.Spool):
    own = keys.get(s.effective.material_id, frozenset()) if s.effective.material_id else frozenset()
    distance = colour_distance(s.effective.rgba, row.colour_rgba)
    return (s.location_id is not None,             # spools with no place (invoice spools) first
            not (reported and reported in own),    # the tray's material next
            distance is None, distance or 0.0,     # the nearest colour, no colour last
            s.number)
```

`list` reads the rows (`rows_on`), the printers' names, each row's place (`filament_locations` by printer, `ams_id` and `tray_id`, unarchived; `place_code = format_code("L", number)`), the unarchived spools once (`FilamentSpoolStore.list()`) and `material_names(conn)` once. `made_from_tags` is the `created` events with `data->>'by' = 'rfid_capture'` and `at > now() - interval '24 hours'`, newest first, at most 20, each with the spool's code and the event's printer and slot.

`resolve`, in this order:
1. Outside the transaction: read the row (none → `TrayChangedError`). `place = locations.ensure_ams(...)` (`NoAmsKindError` passes up).
2. In one transaction: `_tray_lock`. Then `SELECT … FROM filament_unknown_trays WHERE id = %s FOR UPDATE`; none, or `seen_at != body.seen_at` → `TrayChangedError`.
3. The spool: by `spool_id` (none → `LookupError`), or by `parse_code(body.code)`. A code that does not parse, has a prefix other than `S` or names no spool → `CodeUnknownError`. An archived spool → `SpoolArchivedError`.
4. `seen = SeenTray(ams_id, tray_id, tray_uuid, tag_uid, material, colour=rgba[:7], rgba)` from the row; `link_on(conn, …, place.id, actor=actor)` (this also deletes the row).
5. First weight (D4): when the spool has no event of a kind other than `created`, `edited`, `moved`, `tag_bound` and `tag_unbound`, and `round(net_weight_g) != spool.effective.label_weight_g`, call `FilamentSpoolStore.update_on(conn, spool, SpoolPatch(label_weight_g=round(net_weight_g)), actor=actor, ref_kind="unknown_tray", ref_id=row.id)`.
6. `ledger.record(conn, spool, "weighed", source="person", absolute_g=body.net_weight_g, ref_kind="unknown_tray", ref_id=str(row.id), actor=actor)`.
7. Return `FilamentSpoolStore.read(conn, spool, defaults=…)`.

The routes map errors: `TrayChangedError` → 409 `tray_changed`; `CodeUnknownError` → 404 `code_unknown`; `LookupError` → 404 (`_no_spool`); `SpoolArchivedError` → 409 `spool_archived`; `NoAmsKindError` → 409 `no_ams_kind`; any other `ValueError` (`TrayTagClashError`, `TagKeysClashError`) → 409 with its message. Each route's `description` lists these.

- [ ] **Step 1: Write the failing store tests** (`tests/filament/test_unknown_trays.py`)

```python
def test_rank_puts_unplaced_then_material_then_colour(pg_pool): ...
    # row PLA #FF0000FF; spools: A placed PLA red, B unplaced PETG red, C unplaced PLA #EE0000FF, D unplaced PLA blue
    # [c.number for c in rank(...)] == [C, D, B, A]
def test_rank_keeps_ten_and_skips_archived(pg_pool): ...
def test_list_names_printer_slot_and_place(pg_pool): ...
    # row at (0,1) with a place: printer_name "H2C", slot "AMS-A slot 2", place_code startswith "L-"
def test_list_has_the_spools_made_from_tags(pg_pool): ...
    # after Task 2's auto-create: made_from_tags[0].code == f"S-{number}", slot "AMS-A slot 2"
def test_resolve_by_code_links_books_and_clears(pg_pool): ...
    # code "s-<n>" (lower case); spool location == tray place; tray_fingerprint "PLA|#FF0000FF";
    # a weighed event source "person" grams_after 980.0 ref_kind "unknown_tray"; rows_on == []
def test_a_first_weight_that_differs_sets_the_label_weight(pg_pool): ...
    # type label 1000, invoice spool (only created): net 1012 → spool.label_weight_g == 1012 and an edited event
def test_a_first_weight_equal_to_the_types_sets_nothing(pg_pool): ...  # net 1000 → spool.label_weight_g is None
def test_a_used_spool_keeps_its_label_weight(pg_pool): ...
    # spool with a weighed event: net 480 → label_weight_g unchanged; weighed 480 booked
def test_a_stale_seen_at_is_tray_changed(pg_pool): ...   # pytest.raises(TrayChangedError); nothing written
def test_a_flipped_trays_old_id_is_tray_changed(pg_pool): ...
def test_two_resolves_of_one_tray_one_wins(pg_pool): ...
    # ThreadPoolExecutor(2), the same body: one returns a Spool, the other raises TrayChangedError;
    # exactly one weighed event over both spools
def test_an_archived_spool_is_refused(pg_pool): ...      # SpoolArchivedError; the row is kept
def test_a_place_code_is_code_unknown(pg_pool): ...      # code "L-1" → CodeUnknownError
def test_a_spool_from_another_tray_moves_here(pg_pool): ...
    # spool linked at (0,0); resolve (0,2) with it: location == (0,2) place; (0,0) place holds no spool
def test_no_ams_kind_is_refused(pg_pool): ...           # NoAmsKindError; the row is kept
def test_the_old_tray_link_clears_the_row(pg_pool): ...
    # row at (0,1); FilamentInventory(pool).link(...) for that tray → rows_on == []
```

- [ ] **Step 2: Write the failing route tests** (`tests/api/test_filament_unknown_trays.py`; `client` fixture; seed rows with `note_on` on `_state(client).settings_store.pool`, as `test_filament_printer_labels.py` seeds printers)

```python
def test_list_answers_trays_and_candidates(client): ...
    # 200; body["trays"][0]["slot"] == "AMS-A slot 2"; len(candidates) <= 10
def test_resolve_answers_the_spool(client): ...          # 200; body["location_id"] == the tray place id
def test_resolve_refusals_carry_their_codes(client): ...
    # stale seen_at → 409 code "tray_changed"; code "S-999999" → 404 code "code_unknown";
    # archived → 409 "spool_archived"; both spool_id and code → 422; net_weight_g 0 → 422
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/filament/test_unknown_trays.py -n 4 -v`, then `… pytest tests/api/test_filament_unknown_trays.py -v`
Expected: FAIL (no module `unknown_trays`; 404 for the routes).

- [ ] **Step 4: Implement `link_on`, the models, `UnknownTrayStore`, `rank`, the component field and the routes**

- [ ] **Step 5: Run the tests, the tray link tests and the lint**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/filament -n 4 -q`, then `… pytest tests/api/test_filament_unknown_trays.py tests/api/test_filament_sync_nudge.py -n 4 -q`, then `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: PASS, no lint errors.

- [ ] **Step 6: Commit** — `feat(filament): list unknown trays and resolve one (#7143)`

---

### Task 4: Block ScadBuddy's prints to a printer with an unknown tray

**Files:**
- Modify: `backend/scadbuddy/bambuddy/print_run.py` (`RunRefusalError` :887; `PrintCheck` :988; `check_print` :1465 and its `RunRefusalError` catches at :1543 and :1625; `_native_check` :1865 and its catch at :1905; `prepare_run` :3000, the check before `if source.is_sliced:` at :3149; the callers at :2507 and :2575)
- Modify: `backend/scadbuddy/workflows/print_activities.py` (`PrintDeps` :146, the `prepare_run` call :385)
- Modify: `backend/scadbuddy/printers/print_activities.py` (`LanDeps` :208; `lan_print` :362, before `self._command`)
- Modify: `backend/scadbuddy/api/crud_print.py` (route), `backend/scadbuddy/api/crud_areas.py` (`CRUD_AREAS["print"]` and its comment), `backend/scadbuddy/worker.py` (`PRINT_CRUD_AREAS` :1497, `PrintDeps` :1405, `LanDeps` :1425), `backend/scadbuddy/crud/print.py` (`RemoteUnknownTrays`)
- Modify: `backend/scadbuddy/main.py` (`PrintDeps` :1772, `LanDeps` :1806), `backend/scadbuddy/api/printing.py` :1600 and `backend/scadbuddy/api/library_print.py` :1717 (pass `unknown_trays=PoolUnknownTrays(pool)` to the check)
- Test: `backend/tests/printers/test_unknown_spool_block.py`, `backend/tests/crud/test_crud_unknown_trays.py`

**Interfaces:**
- Consumes: `UnknownSpoolReads`, `UnknownSpoolRef`, `UnknownSpoolBlock`, `PoolUnknownTrays`, `blocked_detail`, `UNKNOWN_SPOOL_LOADED` (Task 1).
- Produces:
  - `class UnknownSpoolLoadedError(RunRefusalError)`: `__init__(self, printer_name: str, refs: Sequence[UnknownSpoolRef])`, detail `blocked_detail(...)`, extension `code=UNKNOWN_SPOOL_LOADED`, attribute `refs`.
  - `async def refuse_unknown_spools(reads: UnknownSpoolReads | None, row: PrinterRow | None) -> None`: does nothing when `reads` or `row` is `None`; raises `UnknownSpoolLoadedError(row.name, refs)` when `reads.blocking(row.id)` is non-empty.
  - `PrintCheck.unknown_trays: list[UnknownSpoolRef] = Field(default_factory=list)`.
  - `_refused_check(error: RunRefusalError) -> PrintCheck`: `PrintCheck(errors=[error.detail], unknown_trays=error.refs if isinstance(error, UnknownSpoolLoadedError) else [])`, used at the three catches.
  - A new keyword `unknown_trays: UnknownSpoolReads | None = None` on `prepare_run`, `check_print`, `_native_check` and the two check callers; `PrintDeps.unknown_trays` and `LanDeps.unknown_trays` (same type, default `None`).
  - CRUD route `GET /filament/printers/{printer_id}/unknown-trays` on `crud_router("print")` (name `crud_print_unknown_trays`, answers `UnknownSpoolBlock`), in a new area `"unknown-trays"`, last in `CRUD_AREAS["print"]` and in `PRINT_CRUD_AREAS`. It is an area of its own, so a new worker waits for an API that serves it.
  - `class RemoteUnknownTrays` in `crud/print.py`: `__init__(self, client: CrudClient)`; `async def blocking(self, printer_id: str) -> list[UnknownSpoolRef]` (GET `{BASE}/filament/printers/{printer_id}/unknown-trays`).

`prepare_run` calls `await refuse_unknown_spools(unknown_trays, row)` once the row is known (after the `if route is not None and route.native: … else: …` block), before `if source.is_sliced:`. So the check and the run both refuse, a sliced file too, and nothing is uploaded. `PrintActivities` passes `self.d.unknown_trays`. In `lan_print`, after the "an earlier attempt's start was taken" branch, call `self._refuse_unknown(row)`: a non-empty answer raises `_failed(blocked_detail(row.name, refs), status_code=422)`. Any exception from the read is logged at warning ("the unknown trays could not be read before a LAN start", `error` = type name) and the start goes on (Review Focus 9).

- [ ] **Step 1: Write the failing tests** (`tests/printers/test_unknown_spool_block.py`: `prepare`, `lan_linked`, `bambuddy_routes` and `run_request` as in `tests/printers/test_run_printer.py`; `Scripted` and `_input` as in `tests/printers/test_lan_start.py`; `class FakeReads` with `refs: list[UnknownSpoolRef]` and `fail: bool`)

```python
async def test_prepare_run_refuses_a_printer_with_an_unknown_tray(...): ...
    # prepare(..., unknown_trays=FakeReads([ref "AMS-A slot 2"])) raises UnknownSpoolLoadedError;
    # .status == 422; .detail == "Garage has an unknown spool in AMS-A slot 2: link it on the Filament page first."
    # the error's problem extension code == "unknown_spool_loaded"; no Bambuddy upload route called
async def test_prepare_run_without_unknown_trays_prepares(...): ...   # FakeReads([]) → PreparedRun
async def test_a_sliced_file_is_refused_too(...): ...
async def test_the_check_says_it_as_an_error_with_the_tray_ids(...): ...
    # check_print(..., unknown_trays=FakeReads([ref])) → errors == [detail], unknown_trays == [ref]
async def test_lan_print_refuses_before_the_command(pg_pool): ...
    # LanDeps(unknown_trays=FakeReads([ref])); ApplicationError; details[0].status == 422; Scripted.commands == []
async def test_lan_print_starts_when_the_read_fails(pg_pool, caplog): ...
    # FakeReads(fail=True) → LanStarted; one warning record
async def test_a_start_already_taken_is_not_refused(pg_pool): ...
    # baseline report already shows subtask_name RUNNING: LanStarted, FakeReads not called
```

`tests/crud/test_crud_unknown_trays.py` (fixtures `store`, `crud` as in `tests/crud/test_crud_filament.py`):

```python
def test_the_print_worker_waits_for_the_unknown_trays_area() -> None:
    assert "unknown-trays" in PRINT_CRUD_AREAS
    assert CRUD_AREAS["print"][-1] == "unknown-trays"
async def test_remote_blocking_reads_the_rows(store, crud): ...
    # note_on a row at (0,1) → await RemoteUnknownTrays(crud).blocking(pid) == [UnknownSpoolRef(id=row.id, slot="AMS-A slot 2")]
async def test_the_route_refuses_another_callers_credential(store): ...
    # as test_crud_filament.py checks the trays route: a render credential → 403
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/printers/test_unknown_spool_block.py tests/crud/test_crud_unknown_trays.py -n 4 -v`
Expected: FAIL (`ImportError: UnknownSpoolLoadedError`).

- [ ] **Step 3: Implement the error, `refuse_unknown_spools`, the `PrintCheck` field, the keywords, the CRUD route and client, the LAN check and the wiring**

- [ ] **Step 4: Run the print suites**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/printers tests/crud tests/test_print_replay.py tests/test_print_worker_nodb.py tests/test_print_plate_clear.py -n 4 -q`, then `… pytest tests/api/test_print_destination_native.py tests/api/test_print_plates.py tests/api/test_print_library.py -n 4 -q`
Expected: PASS. An API test whose LAN printer's fixture report shows a loaded untagged tray with no linked spool is now refused: link that tray in the test's setup, or assert the refusal when the test is about it. Never pass `unknown_trays=None` to dodge it.

- [ ] **Step 5: Lint and type-check** — as Task 2 Step 5.

- [ ] **Step 6: Commit** — `feat(print): refuse ScadBuddy's prints to a printer with an unknown spool (#7143)`

---

### Task 5: The frontend's Needs-you source and the resolve dialog

**Files:**
- Modify: `frontend/src/api/filament.ts` (types and two calls)
- Create: `frontend/src/mocks/features/filament/unknownTrays.ts`
- Modify: `frontend/src/mocks/features/filament/index.ts` (export `placeSpoolAtTray`)
- Create: `frontend/src/lib/unknownSpools.ts`
- Modify: `frontend/src/lib/needsYou.ts`, `frontend/src/agent/attentionNotifications.ts` (`noticeTitle`), `frontend/src/components/AppShell.tsx` (:212-276), `frontend/src/test/needsYou.tsx`
- Create: `frontend/src/components/filament/UnknownSpoolDialog.tsx`
- Modify: `frontend/src/components/activity/NeedsYou.tsx` (`kindLabel`, `sourceText`, `askText`, `NeedsYouRow` actions)
- Test: `frontend/src/lib/unknownSpools.test.ts`, `frontend/src/components/filament/UnknownSpoolDialog.test.tsx`, `frontend/src/components/activity/NeedsYou.test.tsx`

**Interfaces:**
- Consumes: Task 3's routes and models (through `pnpm gen:api`).
- Produces:
  - `api/filament.ts`: `type UnknownSpoolTray = Schemas['UnknownSpoolTray']`, `UnknownSpoolTrays`, `UnknownSpoolCandidate`, `UnknownSpoolResolve`, `TagMadeSpool`; `listUnknownTrays(): Promise<UnknownSpoolTrays>`; `resolveUnknownTray(id: string, body: UnknownSpoolResolve): Promise<Spool>` (POST with `request`).
  - `lib/unknownSpools.ts`:
    - `codeFromScan(text: string): string | null`: `"S-142"`, `" s142 "`, `"https://sb.example/x/S-142"` and `"/x/s-142"` give `"S-142"`; an `L-` code or junk gives `null`.
    - `prefillNetWeight(remainPct: number | null | undefined, labelWeightG: number | null | undefined): number | null`: `Math.round(remainPct * labelWeightG / 100)` when both are set, else `null`.
    - `unknownSpoolTitle(tray: UnknownSpoolTray): string`: `"Unknown spool in <printer_name> <slot>"`.
    - `useUnknownSpools(): { data: UnknownSpoolTrays | null; refresh: () => void }`: read at once, every `ATTENTION_POLL_MS`, on `visibilitychange` and `focus`; a failed read keeps the last answer (as `useGrantRequests`).
    - `useTagMadeToasts(made: readonly TagMadeSpool[] | null): void`: one toast per spool that first appears after the first answer: title `"Spool <code> made from its RFID tag"`, detail `"<printer_name> <slot>"`, action "Open" → `filamentLinks.spool(spool_id)`.
  - `needsYou.ts`: `NeedsYouItem` adds `{ kind: 'unknown_spool'; key: string; since: string; tray: UnknownSpoolTray }` (key `unknown_spool:<id>`, `since` = `first_seen_at`). `needsYouItems(waiting, grants, pairings, unknown: readonly UnknownSpoolTray[] = [])`; `NeedsYouSource` adds `'filament'`; `sourceOf`, `toastTitle`, `toastDetail` and `needsYouNotices` handle the kind (`reason: 'unknown_spool'`, message `unknownSpoolTitle(tray)`, `href: NEEDS_YOU_PATH`). `noticeTitle('unknown_spool')` is `'An unknown spool is loaded'`.
  - `UnknownSpoolDialog({ tray, open, onOpenChange, onResolved }: { tray: UnknownSpoolTray; open: boolean; onOpenChange: (open: boolean) => void; onResolved: (spool: Spool) => void })`.

The dialog (`components/ui/dialog`) shows: the title `unknownSpoolTitle(tray)`; what the tray reports (material, `RgbaSwatch` of `colour_rgba`); a "Spool code" input that runs `codeFromScan` on change and paste; the candidates as a radio list (code, material, swatch, grams left, the place name or "No place yet"); "Net weight (g)", prefilled by `prefillNetWeight(tray.remain_pct, candidate.label_weight_g)` when a candidate is picked and the field is still empty, required; a link "Add from an invoice" to `filamentLinks.invoices`; the button "Link spool". It sends `{spool_id | code, net_weight_g, seen_at: tray.seen_at}`. On 409 `tray_changed` it says "This tray changed since the dialog opened. Close it and open it again." and calls `ctx.refresh()`. On 404 `code_unknown` it says "No spool has the code <code>." Other `ApiError`s show `detail`. In `NeedsYouRow`, an `unknown_spool` item has the action "Tell ScadBuddy which spool…" (opens the dialog, compact or not), `kindLabel` "Unknown spool", `sourceText` `"<printer_name> <slot>"`, and `askText` `"Prints to <printer_name> wait until you link the spool in <slot>."`. `AppShell` and `test/needsYou.tsx` read `useUnknownSpools()` and pass `data?.trays` to `needsYouItems`; `refreshNeeds` refreshes it too.

The mock (`unknownTrays.ts`, exports `handlers` and `reset`) seeds one tray on `H2C` (`seed.ts`): ams 0, tray 1, PLA `#FF0000FF`, `remain_pct: null`, with candidates from the mock's spools ranked unplaced first, then by number, at most 10. `made_from_tags` starts empty. The resolve answers 409 `{code: 'tray_changed'}` for a stale `seen_at` or an id it does not hold, and 404 `{code: 'code_unknown'}` for a code no spool has. Otherwise it calls `placeSpoolAtTray(spoolId, printerId, amsId, trayId)` (new in `index.ts`: moves the mock spool to the tray's place, making the place as `index.ts` makes tray places) and drops the tray.

- [ ] **Step 1: Write the failing tests**

```ts
// lib/unknownSpools.test.ts
it.each([['S-142','S-142'],[' s142 ','S-142'],['https://sb.example/x/S-142','S-142'],['/x/s-142','S-142'],['L-3',null],['hello',null]])(
  'codeFromScan(%j) is %j', (text, code) => expect(codeFromScan(text)).toBe(code))
it('prefills from remain and label weight', () => {
  expect(prefillNetWeight(50, 1000)).toBe(500); expect(prefillNetWeight(null, 1000)).toBeNull() })
it('needsYouItems lists an unknown spool by its first sight', () => {
  // needsYouItems([], null, [], [tray]) → [{ kind: 'unknown_spool', key: `unknown_spool:${tray.id}`, since: tray.first_seen_at }]
  // sourceOf(item) === 'filament'
})

// UnknownSpoolDialog.test.tsx (msw mocks)
it('links the picked candidate with the typed weight')  // POST body { spool_id, net_weight_g: 980, seen_at }; onResolved called
it('takes a scanned label URL as a code')               // type 'https://x/x/S-1' → body.code === 'S-1'
it('says when the tray changed')                        // server.use a 409 tray_changed → that text; ctx.refresh called
it('says when no spool has the code')                   // 404 code_unknown → "No spool has the code S-999."
it('needs a weight')                                    // "Link spool" is disabled while the weight field is empty

// NeedsYou.test.tsx
it('shows an unknown spool and opens its dialog')
  // NeedsYouHarness → a row headed "Unknown spool" with "H2C AMS-A slot 2"; clicking "Tell ScadBuddy which spool…" opens the dialog
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/lib/unknownSpools.test.ts src/components/filament/UnknownSpoolDialog.test.tsx src/components/activity/NeedsYou.test.tsx`
Expected: FAIL (modules not found).

- [ ] **Step 3: Implement the API calls, the mock, the lib, the source, the dialog and the row**

- [ ] **Step 4: Run the tests, then the activity and shell tests**

Run: `cd frontend && pnpm exec vitest run src/lib src/components/activity src/components/filament/UnknownSpoolDialog.test.tsx src/components/AppShell.test.tsx src/mocks/features/filament`
Expected: PASS. Re-run once before debugging a timeout (the root CLAUDE.md "Known flakes").

- [ ] **Step 5: Lint and type-check**

Run: `cd frontend && pnpm lint && pnpm typecheck`
Expected: no errors.

- [ ] **Step 6: Commit** — `feat(filament): ask which spool an unknown tray holds, from Needs you (#7143)`

---

### Task 6: The dialog's other entry points and the RFID toasts

Split from Task 5: a reviewer can accept the dialog and its source and still reject where it shows up.

**Files:**
- Create: `frontend/src/components/filament/UnknownSpoolButton.tsx`
- Modify: `frontend/src/pages/filament/LocationPage.tsx`, `frontend/src/pages/filament/LocationsTab.tsx`, `frontend/src/components/print/PrintVerdict.tsx`, `frontend/src/components/AppShell.tsx` (`useTagMadeToasts`)
- Test: `frontend/src/pages/filament/LocationPage.test.tsx`, `frontend/src/pages/filament/LocationsTab.test.tsx`, `frontend/src/components/print/PrintVerdict.test.tsx` (create if absent), `frontend/src/lib/unknownSpools.test.ts`

**Interfaces:**
- Consumes: Task 5's dialog, `listUnknownTrays`, `useTagMadeToasts`; Task 4's `PrintCheck.unknown_trays`.
- Produces:
  - `UnknownSpoolButton({ trayId, label, onResolved }: { trayId: string; label?: string; onResolved?: () => void })`: on click it reads `listUnknownTrays()`, finds `trayId` and opens `UnknownSpoolDialog`. When the tray is gone it says "That tray is no longer unknown." The default label is "Tell ScadBuddy which spool…".
  - `LocationPage`: when `place.printer_id`, `place.ams_id` and `place.tray_id` match a tray of `listUnknownTrays()`, a callout above "Spools": `"An unknown spool is loaded here. Prints to this printer wait until you link it."` with the button; after a resolve it reloads the place.
  - `LocationsTab`: when trays are unknown, a list above the tree, one line per tray (`unknownSpoolTitle`), each with the button.
  - `PrintVerdict`: under the errors, one `UnknownSpoolButton` per `verdict.unknown_trays` entry, labelled `"Link the spool in <slot>"`; its `onResolved` calls the dialog's existing re-check (`onRetry`).
  - `AppShell` calls `useTagMadeToasts(unknown.data?.made_from_tags ?? null)`.

- [ ] **Step 1: Write the failing tests**

```ts
// LocationPage.test.tsx
it('says an unknown spool is loaded in this slot and links one')
  // msw: the place of H2C ams 0 tray 1 → the callout text; the button opens the dialog; after Link spool the callout is gone
it('says nothing for a place with no unknown tray')
// LocationsTab.test.tsx
it('lists the unknown trays above the tree')        // "Unknown spool in 3DP-31B-598 AMS-A slot 2"
// PrintVerdict.test.tsx
it('offers to link the spool the block names')
  // verdict { errors: [detail], unknown_trays: [{ id, slot: 'AMS-A slot 2' }] } → a button "Link the spool in AMS-A slot 2"
// unknownSpools.test.ts
it('toasts a spool made from a tag only when it appears after the first read')
  // first render with [a] → no toast; rerender with [a, b] → one toast titled "Spool S-2 made from its RFID tag"
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/pages/filament/LocationPage.test.tsx src/pages/filament/LocationsTab.test.tsx src/components/print/PrintVerdict.test.tsx src/lib/unknownSpools.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement the button, the callouts, the verdict button and the toasts**

- [ ] **Step 4: Run them and the print picker tests**

Run: `cd frontend && pnpm exec vitest run src/pages/filament src/components/print src/components/PrintPicker.test.tsx src/lib/unknownSpools.test.ts`
Expected: PASS.

- [ ] **Step 5: Lint and type-check** — `cd frontend && pnpm lint && pnpm typecheck`

- [ ] **Step 6: Commit** — `feat(filament): link an unknown spool from its place, Locations and the print dialog (#7143)`

---

### Task 7: The agent's tools

**Files:**
- Modify: `agent/src/tools/filament.ts` (two tools after `filament_printer_labels`; `filament_unknown_tray_resolve: 'spool'` in `filamentTools`' kinds)
- Modify: `agent/src/sessions/touched.ts` (`TOUCHES_NOTHING`, beside `filament_spool_move`)
- Test: `agent/test/filament.test.ts`

**Interfaces:**
- Consumes: Task 3's routes.
- Produces:
  - `filament_unknown_trays`: risk `read`; input `{}`; route `GET /api/v1/filament/unknown-trays`. The description says what a row is, what `candidates` are and how they are ordered, that `made_from_tags` lists spools capture made from RFID tags in the last 24 h, and that ScadBuddy refuses prints to a printer with a row (422 `unknown_spool_loaded`).
  - `filament_unknown_tray_resolve`: risk `write`, not gated; input `{ tray_id: uuid, spool_id?: uuid, code?: string, net_weight_g: number > 0, seen_at: string }`. Exactly one of `spool_id` and `code` is required, else `ToolError('give spool_id or code, not both')`. Route `POST /api/v1/filament/unknown-trays/{tray_id}/resolve`; `summarize`: `Link spool ${code ?? spool_id} to unknown tray ${tray_id}`. The description lists 409 `tray_changed` (read the list again), 404 `code_unknown`, 409 `spool_archived`, 409 `no_ams_kind`, and "only what the user says: never pick a candidate yourself".

- [ ] **Step 1: Write the failing tests** (beside `describe('filament location tools')`)

```ts
it.each([['filament_unknown_trays', 'read'], ['filament_unknown_tray_resolve', 'write']])(
  '%s is %s tier, never gated', (name, risk) => { expect(tool(name).risk).toBe(risk); expect(tool(name).gated).toBe(false) })
it('lists unknown trays and resolves one by code')
  // msw GET → { trays: [{ id: ROW, candidates: [{ spool_id: ROW }] }], made_from_tags: [] };
  // the answer's candidates[0] has spool_ui_path `/filament/spools/${ROW}`;
  // POST body === { code: 'S-12', net_weight_g: 980, seen_at: T }; the answer has ui_path `/filament/spools/${ROW}`
it('refuses both or neither of spool_id and code')    // isError true, no request made
```

`agent/test/touched.test.ts` already fails for a write tool that is not classified; it passes once `TOUCHES_NOTHING` has the new name.

- [ ] **Step 2: Run them to see them fail**

Run: `cd agent && pnpm gen:api && pnpm exec vitest run test/filament.test.ts test/touched.test.ts`
Expected: FAIL (`tool('filament_unknown_trays')` not found).

- [ ] **Step 3: Implement the two tools and the `TOUCHES_NOTHING` entry**

- [ ] **Step 4: Run them, then lint and type-check**

Run: `cd agent && pnpm exec vitest run test/filament.test.ts test/touched.test.ts test/filamentLinks.test.ts && pnpm lint && pnpm typecheck`
Expected: PASS, no errors.

- [ ] **Step 5: Commit** — `feat(agent): list unknown trays and resolve one (#7143)`

---

### Task 8: Docs

**Files:**
- Modify: `backend/CLAUDE.md` (the `filament/` bullet that describes `capture.py`, near :864; the print block beside the `filament` CRUD area text near :719-738)
- Modify: `frontend/CLAUDE.md` (the Filament section, :251-267: the dialog, its entry points, the Needs-you source)
- Modify: `agent/CLAUDE.md` (beside the filament tools, near :327-331)
- Modify: `docs/features/spools-and-filament.md` (What it does, How it works, Code, Data, Tests, Limits)
- Modify: `docs/architecture/data.md` (the new table, beside the other `filament_*` tables)
- Modify: `docs/superpowers/specs/2026-11-23-unknown-spool-flow-design.md` (an "As built (#7143)" section after "Left to the builder")
- Modify: `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§5.3: one "As built in #7143" line)

Write each note in ASD-STE100 style. Cite each measured fact with where it was measured (root CLAUDE.md "PR conventions", #259).

- [ ] **Step 1: Write the notes.** They say:
  - The table and when a row exists. D1: `seen_at` moves only when the reported fields change.
  - Capture's `unknown_trays` step: RFID auto-create (`match_tray`, else a typeless spool, `data.by = "rfid_capture"`), the try-lock, D2 (no external rows), and the backstop.
  - The routes, their codes and the ranking key. D4 (first weight only). D5 (`made_from_tags`).
  - The block: `prepare_run`, the check's `unknown_trays`, the CRUD area `unknown-trays`, and the `lan_print` check, which fails open.
  - The Needs-you source (15 s), the dialog, its four entry points, and the agent's two tools.
  - Limits: D2 and D3; prints started in Bambuddy or on the printer are not blocked; a row whose unit the report no longer shows stays until that unit returns (deleting or merging the printer drops it).

- [ ] **Step 2: Check that no doc now says "Needs you" has three sources**

Run: `cd "$(git rev-parse --show-toplevel)" && grep -rn "Needs you" --include=*.md docs frontend/CLAUDE.md agent/CLAUDE.md backend/CLAUDE.md | grep -i "grant\|pairing"`
Expected: each hit that lists the sources also names the unknown spools.

- [ ] **Step 3: Commit** — `docs(filament): the unknown spool flow as built (#7143)`
