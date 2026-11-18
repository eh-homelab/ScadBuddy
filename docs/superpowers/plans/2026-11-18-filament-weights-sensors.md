# Filament weights and location sensors (#6268) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person (or the assistant) can sync every loaded, unlocked spool's weight from
the AMS in one action; Bambuddy's scale readings come in under their own sync setting;
each location shows its humidity and temperature, with history and alerts, from Home
Assistant and from the AMS; and a hygroscopic spool that has been out of dry storage too
long says it needs drying.

**Architecture:** All writes run in the CRUD service (the API process). "Sync weights
from AMS" is one route that reads the stored LAN reports and books `ams_reading` events
through `ledger.record`, the way `capture.reconcile` does. Scale readings already arrive
in the BB pull (`bambuddy_pull._book`, `ref_kind = bb_scale`, #6263); this plan only
gates them behind a new `filament_sync.weights` setting. Sensors are SB rows bound to SB
locations. An AMS sensor is fed by the report write (`printers/reports.py` `advance_in`),
in its own savepoint. A Home Assistant sensor is read through Bambuddy's
`GET /api/v1/location-ha-sensors/entities`, which returns each HA entity's live `state`:
Bambuddy already holds the HA token, so SB holds no new secret and opens no new outbound
path. Readings go to one history table.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, pydantic v2, Temporal Python SDK,
Postgres 18, Bambuddy 1.3.1 REST API, React + TanStack Query, the agent's `defineTool`.

**Spec:** `docs/superpowers/specs/2026-11-14-filament-management-design.md` §6 ("Scale",
"AMS weight sync"), §5.6 (sensors, time outside dry storage), §10 (Locations tab,
Settings "weight-sync policy"), §12 row #6268. Earlier plans whose rulings bind:
`2026-11-14-filament-data-model.md`, `2026-11-15-filament-capture.md` (Decisions sections).

## What the live Bambuddy says (read-only, 2026-11-18)

`GET https://bambuddy.internal.nullreference.io/openapi.json` answered 200, version
`1.3.1`. Read with `curl` and `jq`, no API key (the spec is public):

- `POST /api/v1/inventory/sync-ams-weights`: "Overwrites the database weight_used for
  every assigned spool using the current AMS remain% … it bypasses the normal 'only
  increase' guard, and the weight lock a hand-set or weighed figure carries, which it
  releases. A printer that is printing is left alone."
- `GET /api/v1/location-ha-sensors/entities?search=` returns `HADisplayEntity[]`:
  `entity_id`, `friendly_name`, `state` (string or null), `domain`, `device_class`,
  `unit_of_measurement`. No time of change.
- `LocationHASensorResponse` (BB's binding): `location_id`, `name`, `entity_id`, `kind`,
  `device_class`, `unit`, `alert_state`, `alert_above`, `alert_below`,
  `notify_on_alert`, `show_on_card`, `sort_order`, `last_state`, `last_changed`.

## Global Constraints

- Database rule (#5295): only the CRUD service has SQL. Every new route is on the API's
  `/api/v1/filament` router. No worker gets a pool, a grant or a new cross-service read.
- Migrations (#491): one new file
  `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_filament_sensors.sql`; never
  edit a merged file.
- Temporal determinism: do not change the commands `SpoolRefresh.run` issues. The HA read
  runs inside the existing pull activity body.
- Live Bambuddy access by agents and tests is READ-ONLY (GET only). Client methods are
  tested with respx and recordings.
- Every new public `/api/v1` route needs `summary=`, `description=` and an agent tool or
  a `NOT_A_TOOL` entry (`agent/src/tools/coverage.ts`). Every agent write tool is listed in
  `agent/src/sessions/touched.ts` `TOUCHES_NOTHING` (or gets an extractor).
- Capture never fails a report write: the AMS sensor capture runs in a savepoint; a failure
  is logged as `FILAMENT_CAPTURE_FAILED` with the error's type, never its message.
- Generated API files are never committed. Frontend mocks never call
  `crypto.randomUUID` (use `frontend/src/mocks/newId.ts` once #6266 is on main; until
  then, a counter).
- Load the `frontend-design` skill for every UI change.
- No task prints a secret.
- Commands: backend `cd backend && uv run --frozen pytest <files> -n 4` with
  `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/scadbuddy_test_6268`;
  `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`.
  Frontend `pnpm typecheck && pnpm lint && pnpm test`; Playwright `--workers=2`.
  Agent `pnpm typecheck && pnpm lint && pnpm test`.

## Decisions this plan makes

1. **AMS sync skips locked spools and printing printers.** The spec says "every loaded,
   unlocked spool". BB releases the lock; SB does not (the lock exists to keep AMS
   estimates out). A printer whose report is printing is skipped, as BB does, and its
   readings would never anchor anyway (§5.5 rule 2). Cost if wrong: one more flag.
2. **The sync books a normal `ams_reading`** (`source = ams_remain`, `data.remain_pct`,
   `data.printing = false`, `data.forced = true`, `actor`), with no `ref_id`. Being the
   newest reading, it anchors (§5.5), so no ledger rule changes. Only a tray whose report
   carries `remain` (RFID trays) can be synced; the others are counted as `no_remain`.
   Reports older than `reports.is_fresh` are counted as `stale`, not read.
3. **`filament_sync.weights`** (`"off" | "bb_to_sb"`, default `"bb_to_sb"`) is the
   "weight-sync policy" of §10 and the "BB→SB sync of weights" of §6. With `off`, the pull
   books no `bb_scale` weighing for any spool (a create too) and no `bb` weight reading on a
   spool SB already tracks (an update or a link); only a create keeps its first `bb` reading (see "Decisions made while
   building"). Everything else in the pull is unchanged. #6265 widens the values; it keeps the field name.
4. **Sensors are SB's own** (`filament_location_sensors`): location, `source`
   (`home_assistant` | `ams`), `kind` (`humidity` | `temperature`), `entity_id` (HA) or
   `printer_id` + `ams_id` (AMS), unit, `alert_above`, `alert_below`, `notify`, archived.
   Readings go to `filament_sensor_readings` (sensor, at, value). A reading is stored when
   the value changed or 15 minutes passed since the sensor's last stored reading.
5. **Home Assistant through Bambuddy.** SB reads HA states from BB's
   `/location-ha-sensors/entities`, one GET per pull, matched by `entity_id`. A state that
   is not a number (`unavailable`, `unknown`) stores nothing and marks the sensor
   `reachable = false`. SB gets no HA token (secure by default).
6. **AMS sensors are made automatically.** For each AMS unit seen in a report, SB makes an
   AMS-unit location (`printer_id`, `ams_id`, `tray_id IS NULL`, kind `ams`) as the parent
   of that unit's tray locations, and one humidity and one temperature sensor on it.
   A new partial unique index keeps one unit location per `(printer_id, ams_id)`.
7. **Alerts are shown, not sent.** A reading outside `alert_above` / `alert_below` marks
   the sensor alerting in the API and the UI. Sending a notification has no backend channel
   today; it is filed as #6646, linked from #6270, and the `notify` flag is kept
   for it.
8. **Time outside dry storage** is derived at read from the spool's `moved` events since
   its last `dried` event (or since it was created): the hours its location's kind was not
   `dry_storage`. A new material column `max_hours_outside_dry numeric(6,1)` (null = no
   limit) sets the rule. `needs_drying = material.hygroscopic AND max_hours_outside_dry IS
   NOT NULL AND hours_outside_dry > max_hours_outside_dry`. Spool reads gain
   `hours_outside_dry` and `needs_drying`.

## Review Focus

1. A tray that holds two spools' history (a re-load) syncs the spool that holds it now,
   never an older holder.
2. A sync while one printer prints and another is idle books only the idle one's trays and
   says so in its counts.
3. An HA state `"unavailable"` or `"45.2 %"` never raises and never stores a reading.
4. The AMS sensor capture on a report with no `ams` key, or `humidity: ""`, writes nothing
   and does not fail the report write.
5. A spool moved between two dry boxes accrues no hours outside dry storage.

---

### Task 1: Migration, models and the sensor store

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_sensors.sql`
- Create: `backend/scadbuddy/filament/sensors.py`
- Modify: `backend/scadbuddy/filament/models.py`, `backend/scadbuddy/filament/component.py`, `backend/scadbuddy/filament/locations.py` (`ensure_ams_unit_on`)
- Test: `backend/tests/filament/test_sensors.py`, `backend/tests/filament/test_migrations.py`

**Interfaces:**
- Produces: tables `filament_location_sensors`, `filament_sensor_readings`; column
  `filament_materials.max_hours_outside_dry`; the partial unique index on
  `filament_locations (printer_id, ams_id) WHERE tray_id IS NULL AND printer_id IS NOT NULL AND archived_at IS NULL`.
- `SensorStore` with `list(location_id: UUID | None) -> list[Sensor]`,
  `create(SensorIn, actor) -> Sensor`, `update(id, SensorPatch, actor) -> Sensor`,
  `archive(id, actor)`, `readings(id, since: datetime, limit: int) -> list[SensorReading]`,
  `record_on(conn, sensor_id, value: float, at: datetime) -> bool` (applies Decision 4's
  change-or-15-minutes rule, returns whether it stored).
- `Sensor` carries `last_value`, `last_at`, `alerting: bool`, `reachable: bool`.
- `LocationStore.ensure_ams_unit_on(conn, printer_id, ams_id) -> UUID`; tray locations
  made by `ensure_ams_on` get the unit location as parent (existing tray locations are
  re-parented by the migration only when their unit location exists; otherwise on the
  next capture).

- [ ] **Step 1: Failing tests:** `test_a_sensor_stores_a_changed_value`, `test_a_same_value_is_stored_again_after_15_minutes`, `test_alerting_follows_the_last_value`, `test_one_ams_unit_location_per_unit`, `test_tray_locations_hang_under_their_unit`, and a migration test that the column, tables and index exist.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Write the migration and the store.**
- [ ] **Step 4: Run** `tests/filament` → PASS.
- [ ] **Step 5: Commit** `feat(filament): location sensors and their readings (#6268)`.

### Task 2: Sync weights from AMS

**Files:**
- Modify: `backend/scadbuddy/filament/capture.py` (`sync_ams_weights`), `backend/scadbuddy/api/filament.py`, `backend/scadbuddy/filament/models.py` (`AmsSyncResult`)
- Test: `backend/tests/filament/test_ams_sync.py`

**Interfaces:**
- `capture.sync_ams_weights(pool, *, actor: str, now: datetime) -> AmsSyncResult`
  with counts `booked`, `locked`, `printing`, `no_remain`, `stale`, and `spools: list[UUID]`.
  It takes each printer's `printer_reports` row `FOR UPDATE`, then `capture.lock` on the
  spools it will book, as `reconcile_all` does. The holder of a tray is found with the
  same `filament_locations`/`filament_spools` query as `reconcile`.
- Route `POST /api/v1/filament/spools/sync-ams-weights` → `AmsSyncResult`.

- [ ] **Step 1: Failing tests** for Decisions 1 and 2 and Review Focus 1 and 2: `test_sync_books_every_loaded_unlocked_spool`, `test_sync_skips_a_locked_spool`, `test_sync_skips_a_printing_printer`, `test_sync_counts_a_tray_without_remain`, `test_sync_skips_a_stale_report`, `test_a_synced_reading_becomes_the_anchor` (a newer scale reading existed before → after the sync `grams_left` is remain × label).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(filament): sync weights from the AMS (#6268)`.

### Task 3: The weights sync setting

**Files:**
- Modify: `backend/scadbuddy/core/settings.py` (`FilamentSync.weights`), `backend/scadbuddy/library/settings_store.py`, `backend/scadbuddy/api/settings.py`, `backend/scadbuddy/core/setting_docs.py`, `backend/scadbuddy/filament/bambuddy_pull.py` (`_book`)
- Modify: `frontend/src/pages/settings/fields.ts`, `frontend/src/pages/settings/values.ts`, `frontend/src/mocks/features/settings.ts`, `frontend/src/mocks/fixtures.ts`
- Test: `backend/tests/filament/test_bambuddy_pull.py`, `backend/tests/test_settings*.py` (the file that covers `filament_sync`), `frontend/src/pages/settings/sections/filament.test.tsx`

**Interfaces:** `FilamentSync.weights: Literal["off", "bb_to_sb"] = "bb_to_sb"`.
`read_and_pull` passes it to `_book`; with `off` no `bb_scale` and no `bb` weight
reading is booked for a tracked spool, and no `bb_scale` for any. The settings mapping that today names only `spools` takes both fields.

- [ ] **Step 1: Failing tests:** `test_weights_off_books_no_bb_weighing` (and its `bb_to_sb` twin), a settings round-trip test for `weights`, and a frontend test that the row saves `filament_sync.weights` without touching `spools`.
- [ ] **Step 2-4:** Run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat(filament): a sync setting for Bambuddy weights (#6268)`.

### Task 4: AMS humidity and temperature from the report write

**Files:**
- Modify: `backend/scadbuddy/printers/reports.py` (`advance_in`), `backend/scadbuddy/filament/sensors.py` (`capture_ams_on(conn, printer_id, report, seen_at)`)
- Test: `backend/tests/filament/test_sensors.py`, `backend/tests/printers/test_reports*.py`

**Interfaces:** `capture_ams_on` parses `report["ams"]["ams"][]` (`id`, `humidity`,
`temp`, strings) the way `printers/status.py` `_ams` does (reuse it), ensures the unit
location and its two sensors (Decision 6), and calls `record_on` for each number. It runs
on every `advance_in`, in its own savepoint, after the tray capture.

- [ ] **Step 1: Failing tests:** `test_a_report_records_ams_humidity_and_temperature`, `test_a_report_without_ams_writes_nothing`, `test_an_empty_humidity_is_skipped`, `test_a_failing_sensor_capture_keeps_the_report` (Review Focus 4).
- [ ] **Step 2-4:** Run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat(filament): AMS humidity and temperature as sensor readings (#6268)`.

### Task 5: Home Assistant sensors through Bambuddy, and the sensor routes

**Files:**
- Modify: `backend/scadbuddy/bambuddy/client.py` (`ha_entities(search: str | None) -> list[HaEntity]`), `backend/scadbuddy/bambuddy/models.py`, `backend/scadbuddy/filament/bambuddy_pull.py` (the HA read, before the `spools` gate, inside the activity), `backend/scadbuddy/api/filament.py`
- Create: a recording under `backend/tests/bambuddy/recordings/` for the entities answer (hand-written from the schema above; no live write)
- Test: `backend/tests/bambuddy/test_client*.py`, `backend/tests/filament/test_sensors.py`, `backend/tests/api/test_filament*.py`

**Interfaces:** routes (all with summary and description):
`GET /filament/sensors?location_id=`, `POST /filament/sensors`,
`PATCH /filament/sensors/{id}`, `POST /filament/sensors/{id}/archive`,
`GET /filament/sensors/{id}/readings?since=&limit=`,
`GET /filament/sensors/entities?search=` (proxies `ha_entities`; 409 with a clear message
when Bambuddy is not configured). Creating an `ams` sensor by hand is refused (422): they
are made by Task 4. The pull reads HA whenever Bambuddy is configured, whatever
`filament_sync` says (sensors are not spool data).

- [ ] **Step 1: Failing tests:** client parse test with respx; `test_ha_states_become_readings`, `test_a_non_numeric_state_marks_unreachable` (Review Focus 3); route tests for create, patch, archive, readings and the 409.
- [ ] **Step 2-4:** Run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat(filament): Home Assistant sensors read through Bambuddy (#6268)`.

### Task 6: Time outside dry storage

**Files:**
- Modify: `backend/scadbuddy/filament/spools.py` (spool reads), `backend/scadbuddy/filament/models.py` (`hours_outside_dry`, `needs_drying`; material `max_hours_outside_dry`), the material catalog in/patch models
- Test: `backend/tests/filament/test_dry_storage.py`

**Interfaces:** `dry_hours.outside_dry(events, locations, now) -> float` (a pure function,
in a new `filament/dry_hours.py`), used by the spool detail and the list.

- [ ] **Step 1: Failing tests:** `test_hours_count_only_outside_dry_storage`, `test_a_move_between_dry_boxes_adds_nothing` (Review Focus 5), `test_drying_resets_the_count`, `test_needs_drying_needs_a_hygroscopic_material_and_a_limit`.
- [ ] **Step 2-4:** Run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat(filament): hours outside dry storage and needs drying (#6268)`.

### Task 7: Agent tools

**Files:**
- Modify: `agent/src/tools/filament.ts`, `agent/src/sessions/touched.ts`, `agent/src/tools/coverage.ts` (only if a route has no tool)
- Test: `agent/test/filament*.test.ts`, `agent/test/touched.test.ts`

**Interfaces:** `filament_sync_ams_weights` (write, no input), `filament_sensors_list`
(read), `filament_sensor_readings` (read), `filament_sensor_save` (write: create or patch),
`filament_sensor_archive` (write), `filament_ha_entities` (read). `EVENT_KINDS` unchanged.

- [ ] **Step 1-4:** tests per tool (route, input schema, summary), run → FAIL, implement, run → PASS.
- [ ] **Step 5: Commit** `feat(agent): filament sensor and AMS weight tools (#6268)`.

### Task 8: UI

Load `frontend-design` first.

**Files:**
- Modify: `frontend/src/pages/filament/SpoolsTab.tsx` (a "Sync weights from AMS" button in the toolbar; the result's counts in a toast), `frontend/src/pages/filament/LocationsTab.tsx` and a new `frontend/src/components/filament/LocationSensors.tsx` (each location's sensors: value, unit, alerting, reachable, a small history chart, add/edit/archive for HA sensors with the entity picker), spool list and `SpoolPage` ("Needs drying" badge, hours outside dry storage in Storage), the material editor (`max_hours_outside_dry`), `frontend/src/api/` hooks, `frontend/src/mocks/features/filament/`
- Test: vitest beside each component; one Playwright spec `e2e/filament-sensors.spec.ts` (sync button, a sensor's history, the badge)

- [ ] **Step 1-4:** tests, run → FAIL, implement, run → PASS (Playwright `--workers=2`).
- [ ] **Step 5: Commit** `feat(frontend): AMS weight sync, location sensors and needs drying (#6268)`.

### Task 9: Docs, follow-up and PR

- Spec: §6 "Scale" and "AMS weight sync", §5.6 sensors: "as built in #6268" notes for
  Decisions 1-8. `backend/CLAUDE.md` filament layout: `sensors.py`, `dry_hours.py`.
- File the notification follow-up issue (Decision 7), blocked by nothing, linked from
  #6270's body.
- Add "Decisions made while building (#6268)" here.
- Commit `docs(filament): weights and sensors as built (#6268)`.

## Decisions made while building (#6268)

- **`weights: off` and a new spool (Task 3 fix round 1).** With `off`, `_update` and `_link`
  book no `bb` weight reading (reading = None) for a spool SB already tracks, and `_book`
  books no `bb_scale` weighing for any spool, `_create`'s too. `_create` keeps its first `bb` reading: a spool SB has never
  seen needs a starting weight, and there is no SB weight to protect. The ruling of the
  controller. `bb_dried` is not a weight and stays booked.

1. **Order of the tasks.** Task 4 ran before Task 3, because Task 3 waited for #6617
   (`filament_sync.tags`, #6264) to merge. Task 4 does not depend on it. The order changed
   nothing else.
2. **A re-load syncs the current holder (Task 2).** A spool that moved into a tray between
   the two holder reads is still booked under its own row lock, because `ledger.record`
   locks the spool first. Only the id order of the locks is lost for that one spool. The
   cost is a rare deadlock retry.
3. **A stale report writes no sensor value (Task 4).** `advance_in` records what a report
   says at its `seen_at`, and `record_on` refuses an `at` not newer than the last reading.
   An older report cannot move a value back.
4. **A failed HA read marks the sensors unreachable (Task 5).** `reachable` means SB got a
   number on its last read. The cost is a short false "unreachable" badge.
5. **Empty text is not a reading (Task 4).** `AmsUnit` turns `""` humidity or temperature
   into no value, and a sensor is made only when a number is present.
6. **The place keeps its "Add a sensor" button (Task 8).** A sensor belongs to one place. A
   button at tab level needs a place picker, which is one more step. The cost is a busier
   tree.
7. **An add with no fields gives 422; an empty edit is a 200 (Task 7).** The agent tool
   sends the fields as given. `POST` with no fields is refused with a 422, which shows as an
   error through the shared `ok` helper. An edit with `fields: {}` sends `PATCH {}`, which
   `SensorPatch` accepts: it answers 200 and only `updated_at` moves. The cost is a less
   clear error text on the add.
8. **A whole-object PUT is not a defect (Task 3).** The UI sends the full settings object,
   and no agent tool writes it. A change made in another tab can be overwritten by an old
   form draft, as before this work. Nothing changed.
9. **Test infrastructure.** The test database ran on port 55432 with `timezone=UTC`. The
   LocationSensors history test depended on the clock (93 against 96 readings), and
   `7cde321` fixed it. The SettingsPage test "does not call this tab own Test connection"
   fails locally on branches based on main and passes in CI. It is not from this branch.
10. **Follow-up.** Decision 7 stays as made: alerts are shown, not sent. Issue #6646,
    "send a notification when a location sensor alerts" (a sub-issue of #6260, linked from
    #6270), tracks the notification.
11. **AMS humidity is the percent, `humidity_raw` (final review).** The LAN report's
    `humidity` is Bambu's 1-5 level; the percent is `humidity_raw` (recorded H2C report:
    `"humidity": "3"` beside `"humidity_raw": "29"`). `printers/status.py` `ams_units`
    fills `AmsUnit.humidity` from `humidity_raw`, as Bambuddy's status gives a percent. A
    report with no `humidity_raw` has no humidity and makes no humidity sensor.
12. **A sensor under `alert_below` says "Below the limit" (final review).** `Sensor.alerting`
    is true above `alert_above` and below `alert_below`, so `LocationSensors` says "Above
    the limit" or "Below the limit" by which band the value left.
13. **A patch cannot invert the stored band (review).** `SensorStore.update` checks the
    merged band (stored values with the patch laid over) under a row lock and raises
    `SensorBandError`, a 422 with "alert_below must be under alert_above". The table has the
    same CHECK. `SensorPatch` still checks a band sent whole.
14. **Tray capture does not need the unit's place (review).** `ensure_ams_on` returns an
    existing tray place whose unit place cannot be made (every `ams` kind is archived). A
    tray place that does not exist yet still raises `NoAmsKindError`.
15. **The sensors migration archives duplicate unit places (review).** Until now a hand-made
    place with a printer and an AMS id and no tray was allowed twice. Before the unit index
    is made, the migration keeps the oldest unarchived one per unit (`created_at`, then
    `id`) and sets `archived_at` on the rest.
16. **A printer's place with no AMS id is not a unit place (review of 236eb134d).** A place
    with a printer and no `ams_id` and no `tray_id` (a shelf beside the printer) is
    allowed many times. The archive pass and the `filament_locations_ams_unit` index both
    require `ams_id IS NOT NULL`, so the migration archives none of these places.
    `ensure_ams_unit_on` uses the same predicate in its `ON CONFLICT`.
17. **An archived duplicate hands on what it holds (review of 236eb134d).** Before the
    migration archives a duplicate unit place, it moves the place's spools to the kept
    place and books a `moved` event for each (`actor` `system:unit-place-merge`, `reason`
    `duplicate_unit_place`), as a printer merge does. It also moves the child places'
    `parent_id` to the kept place. No sensor can point at the duplicate, because this
    migration makes the sensors table.
18. **An AMS sensor follows its unit's live place (review of 236eb134d).** When a person
    archives an AMS unit's place, `capture_ams_on` moves the unit's sensors to the place
    that `ensure_ams_unit_on` returns (a new place when needed) before it stores a reading.
    The check uses the one sensor read, so an unchanged report still costs one query.
19. **AMS sync counts every tray holder it locked (review of 236eb134d).** A printer with no
    row (`PrinterStore.read_on` gives `None`) counts its spools as `stale`. A spool placed
    in a tray after the first holder read was not locked, so the sync does not book or
    count it; the next sync takes it.
20. **A printer's shelf place takes spools (review of 6904612c3).** `LocationStore.move_on`
    refuses only a unit place: `printer_id` and `ams_id` set and no `tray_id`, the
    predicate of the `filament_locations_ams_unit` index. Before, it refused every place
    with a printer and no tray, so a move or a CSV import into a shelf beside the printer
    failed. Decision 16 says such a place is not a unit place. `MoveTargetError` says
    "the place to move to does not exist, is archived or is an AMS unit's place", and
    the move route's description says the same.
21. **The mock's move refusal uses the backend's text (review of 6904612c3).** The
    frontend mock's `REFUSED.moveTarget` is the exact `MoveTargetError` message, so the
    mock and the API give the same `detail`.
22. **No reading lands on an archived unit place (review of 6904612c3).** When a sensor's
    place is archived and `ensure_ams_unit_on` raises `NoAmsKindError` (no AMS kind to make
    a new place), `capture_ams_on` skips that reading and stores nothing. Before, it
    stored the reading on the archived place, which Decision 18 forbids.
23. **The migration's `moved` event sorts last (review of 6904612c3).** A test puts a spool
    with an event stamped one day ahead on a duplicate unit place. The migration's `moved`
    event must come after it (`greatest(clock_timestamp(), max(at) + 1 µs)`). The test
    failed with only `clock_timestamp()` and passes with the migration as it is.
24. **The 409 for a second unit place names the AMS unit (#6679 item 1).** Decision 15's
    index refuses a second unarchived hand-made place for one printer and AMS unit. The
    refusal is `LocationExistsError`, so its text now says "a place with that name, that
    tray or that AMS unit already exists". The descriptions of `POST /locations` and
    `PATCH /locations/{id}` name the unit case too, and the frontend mock uses the same text.
25. **A sensor on an archived place is hidden and not read (#6679 item 2).** Decisions 18
    and 22 keep AMS readings off hidden places. A Home Assistant sensor has no live place
    to move to, so `apply_ha_states_on` skips one whose place is archived: it stores no
    reading and is not marked unreachable. `SensorStore.list` (`GET /filament/sensors`)
    leaves out every sensor whose place is archived, of either source, so the place and
    its sensors hide together. An AMS sensor shows again when the next report moves it to
    its unit's live place (Decision 18). `read_ha_sensors` reads the list, so it makes no
    Bambuddy call when every Home Assistant sensor is on an archived place.
26. **The Home Assistant read stays one unfiltered GET (#6679 item 3).** The ruling was to
    ask Bambuddy only for the entity ids the pull needs, if its route allows it. It does
    not. Bambuddy 1.3.1's OpenAPI (`GET /openapi.json`, `info.version` `1.3.1`, read with
    `jq` on 2026-11-18) gives `GET /api/v1/location-ha-sensors/entities` one query
    parameter, `search` (a string or null), and no list of ids. The other Home Assistant
    reads are no better: `GET /api/v1/ha-sensors/entities` also takes only `search`,
    `GET /api/v1/smart-plugs/ha/sensors` takes none and lists power and energy sensors
    only, and `GET /api/v1/bulk/ha-sensor-readings?ids=` takes printer ids, not entity
    ids. One `search` call per sensor would make N calls per pull in place of one, and
    `search` matches text, so it can return more entities than the one asked for. So
    `read_ha_sensors` keeps one GET with no `search`, and still makes none when no live
    Home Assistant sensor is set up. Revisit this when Bambuddy's route takes ids.
27. **The AMS sync names its stale printers (#6679 item 4).** `AmsSyncResult` gains
    `stale_printers`: each printer whose spools are in `stale`, once, as `StalePrinter`
    (`id`, and `name` from the printer's row, `null` when the row is gone). A printer that
    holds no spool is not named. The Spools tab's toast adds the names after the stale
    count ("Skipped 4 whose printer has not reported lately (H2C, P1S)."), and a printer
    with no name shows as "printer" and the first 8 characters of its id. The agent tool
    `filament_sync_ams_weights` describes `stale_printers` and tells the agent to name
    them. No plugin skill names the sync, so the plugin version does not change.
28. **Bambuddy's AMS humidity is a percent (#6679 item 5, measured).** The final review of
    #6650 could not verify Decision 11's "as Bambuddy's status gives a percent". Measured
    on 2026-11-18 (history point `recorded_at` `2026-11-18T18:54:01`) with read-only GETs (`curl` and `jq`, no write, no
    WebSocket) on the live Bambuddy 1.3.1 (`https://bambuddy.internal.nullreference.io`),
    printer 1 (the H2C, printing): `GET /api/v1/printers/1/status` gave `ams[].humidity`
    31, 17, 14, 15 and 19 for units 0, 1, 2, 3 and 128. Bambu's level is 1 to 5, so these
    are percents. `GET /api/v1/ams-history/1/{ams_id}` stores `humidity` and
    `humidity_raw` side by side; its latest point for each unit had both equal to the
    status value (31.0 and 31.0, and so on). The raw MQTT report was not read: Bambuddy's
    `GET /api/v1/printers/1/logging` held no messages (logging was off), and turning it on
    is a write. The comment in `printers/status.py` `ams_units` now says this.
