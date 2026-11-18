# Filament history timeline and metrics (#6269) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every change to a spool says who made it, field by field. The spool page shows a
timeline and a grams-left chart with the anchor readings marked, and the spool's metrics.
Filament → Metrics shows stock, consumption, waste, price trends, supplier spend and days of
stock. The Spools tab gets its forecast view and "Consumed" tile. The same numbers go to
Prometheus, and a Filament dashboard joins the Grafana dashboards.

**Architecture:** All reads and writes run in the CRUD service (the API process). The ledger
(`filament_spool_events`, spec §5.5) stays the only history. A pure module
(`filament/metrics.py`) turns one spool's events into plates, metrics, the AMS error and the
chart series; a store (`filament/metrics_store.py`) reads the rows for one spool or for the
inventory and calls it. Four GET routes serve the UI and four read-only agent tools. The
`/metrics` scrape reads one cached inventory snapshot (60 s) into labelled gauges in
`core/metrics.py`; `deploy/grafana/scadbuddy-filament.json` charts them. The charts in the
UI are plain SVG, as `SensorChart` (#6268) is: the app has no chart library.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, pydantic v2, prometheus_client, Postgres 18,
React + TanStack Query, plain SVG, the agent's `defineTool`, Grafana dashboard JSON,
kustomize v5.6.0.

**Spec:** `docs/superpowers/specs/2026-11-14-filament-management-design.md` §5.7 (history and
metrics), §5.5 (the ledger and its "As built" rules), §5.2 (`sku_settings`), §10 (Spools tab
forecast view, spool edit History section, Metrics tab), §12 row #6269. Earlier plans whose
rulings bind: `2026-11-14-filament-data-model.md`, `2026-11-15-filament-capture.md`,
`2026-11-15-filament-ui.md` (Decision 11: the forecast view and "Consumed" tile come here),
`2026-11-17-filament-suppliers.md` (Decision 16: SKU settings come here),
`2026-11-18-filament-weights-sensors.md` (#6268: `hours_outside_dry`, scale weighings,
`SensorChart`).

## Global Constraints

- Database rule (#5295): only the CRUD service has SQL. Every new route is on the API's
  `/api/v1/filament` router. No worker gets a pool, a grant or a new cross-service read. The
  scrape runs in the API.
- Migrations (#491): one new file
  `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_filament_history_metrics.sql`,
  named after every merged file; never edit a merged file.
- Every new public `/api/v1` route has `summary=`, `description=` and an agent tool or a
  `NOT_A_TOOL` entry (`agent/src/tools/coverage.ts`). Every agent write tool is classified in
  `agent/src/sessions/touched.ts`. Every tool works in classic and durable modes.
- Metric labels are low-cardinality: `material`, `brand`, `location_kind`, `outcome`,
  `currency`. Never a spool id, a spool number, a type, a colour name, a location name or a
  person. A label with more than 20 values folds the rest into `other`; null is `none`.
- `/metrics` never fails a scrape: a failed read keeps the last values and sets
  `scadbuddy_filament_metrics_up` to 0.
- Money is `Decimal`, never float, serialized as a decimal string. A spool's cost and value
  are in the shop's `orders_currency`; invoice spend stays in each invoice's currency and is
  never converted. The frontend shows money with `useMoney` and `lib/decimal.ts`.
- Every setting is rememberable: the burn window and the lead-time defaults are stored
  settings; the Metrics tab's view sits behind "Remember my view".
- Dashboards pass `.github/scripts/lint-dashboard.sh` (kustomize v5.6.0): uid equals the
  file name, datasources only as `${DS_PROMETHEUS}`, every series declared in
  `core/metrics.py` by its constructor, every `by (...)` label in that constructor's label
  list.
- Load the `frontend-design` skill (and `dataviz` for chart colours) for every UI change.
  Mocks never call `crypto.randomUUID` (`frontend/src/mocks/newId.ts`).
- Secure by default: no route widens access; the history shows a principal only to callers
  who can read the spool already.
- Commands: backend `cd backend && uv run --frozen pytest <files> -n 4` with
  `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/scadbuddy_test_6269`;
  `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`.
  Frontend and agent `pnpm typecheck && pnpm lint && pnpm test`; Playwright `--workers=2`.
  Dashboards `KUSTOMIZE=<kustomize v5.6.0> .github/scripts/lint-dashboard.sh`.

## Decisions this plan makes

1. **Build on #6268.** PR #6650 touches `filament/models.py`, `spools.py`,
   `api/filament.py`, the settings, `agent/src/tools/filament.ts` and `touched.ts`, and
   gives `hours_outside_dry` and `SensorChart`. Task 1 starts only after #6650 is merged and
   merges `origin/main`. Why: building beside it means a large conflict in five files.
2. **Prometheus gauges, not an OTel meter.** The spec says "OpenTelemetry metrics with the
   existing tracing setup". The backend has no OTel meter provider: every metric is a
   prometheus_client series in `core/metrics.py` at `GET /metrics`, the dashboards read
   Prometheus, and `lint-dashboard.sh` accepts only series declared there. So the filament
   numbers are `Gauge`s in that registry. Cost if wrong: a later move to an OTel meter
   renames nine series.
3. **The gauges come from a snapshot, refreshed at most every 60 s per process**
   (`FILAMENT_METRICS_MAX_AGE = 60.0`), in a thread, as the store-usage gauges are. Each
   gauge is cleared before it is set, so a material that leaves stock leaves the series.
   Every API pod exports the same values, so the dashboard reads them with `max by (...)`.
4. **Labels.** `material` (catalog code), `brand` (catalog name, a closed vocabulary),
   `location_kind` (the kind's code, not the location's free-text name), `outcome`
   (`completed`, `failed`, `cancelled`, `unknown`), `currency` (the shop's). The spec's
   "location" is read as the location kind, because a location name is free text a person
   types. More than 20 values of one label fold into `other`.
5. **Consumption is a cumulative gauge.** `scadbuddy_filament_used_grams{material,outcome}`
   is the grams of every plate ever booked. Panels use `delta(...)` over the range. A
   counter would need the API to remember what it already counted across restarts.
6. **A plate is counted by §5.5 rule 3, with no anchor.** `metrics.plates` pairs SB's and
   BB's events of one job as `ledger.basis` does: BB's usage row wins. An SB plate with
   `basis` `not_applied` is a failed plate of 0 g; one with `basis` `covered` is skipped (its
   grams are on another spool). An open estimate (a print still running) is not used yet.
   Outcome: BB's `bb_status` `completed`, `failed` or `cancelled`; any other BB status is
   `unknown`; an SB plate is `completed` when applied, else `failed`.
7. **Waste is the grams of `failed` and `cancelled` plates.** An SB-only failed plate
   books 0 g, so its waste is 0 until BB's usage row is paired with it. The waste rate is
   wasted grams over all used grams.
8. **Burn rate** is the grams of plates that ended inside the window, divided by the
   window's days, or by the spool's age in days when it is younger (never less than 1 day).
   The window is the setting `filament_burn_window_days` (default 30, 1 to 365). The
   forecast empty date is `now + grams_left / burn`; none when the burn is 0.
9. **SKU settings are two type columns**, `lead_time_days` and `safety_margin_days`
   (integers ≥ 0, null = the setting). Settings `filament_lead_time_days` (default 7) and
   `filament_safety_margin_days` (default 3). Reorder date of a type =
   its empty date − lead time − safety margin. A type is "reorder now" when that date is
   today or past. BB parity with `FilamentSkuSettings`, with no separate table.
10. **Who changed it.** `ledger.record` adds `data.by` from `core/authorship.py`
    `current_author()` when an agent or flow made the call: `{principal, session, tool,
    flow_run}`, the keys that are set. `actor` keeps its meaning (`service:agent`,
    `bambuddy`, `print`, `system:*`, a person's principal). Every event kind gets it, not only
    `edited`. No new column.
11. **A type edit fans out.** A change to a type's field writes one `edited` event on every
    unarchived spool of that type that does not override the field, with
    `data.changes` and `data.type_id`. So a value a spool takes from its type can be traced
    too, and a label-weight change refreshes the spool's grams (the event has `changes`, so
    `refresh_derived` takes the full walk). An archived spool gets none. Cost: one event per
    spool per type edit; types hold few spools.
12. **The invoice confirm writes its edit.** Setting `invoice_line_id` on spools that existed
    before the confirm (`update_spool_ids`) writes an `edited` event with that change and
    the invoice line's ref. New spools already get `created` with the ref.
13. **The chart shows what SB believed then.** Each event stores `grams_before`, the spool's
    grams left when it was booked. The value after an event is the next event's
    `grams_before` (by `seq`), and the spool's `grams_left` after the last. Points are
    sorted by `at`. A reading that `ledger.basis` would take as an anchor is marked. This is
    O(n) and needs no replay. More than 500 points thin out: anchors, plates and the last
    point stay; the rest are kept every k-th.
14. **AMS error.** Each scale reading (`source` `scale`, `grams_after` set) is paired with
    the newest `ams_remain` reading before it that is not `printing`, within 24 h, with no
    plate between them. Error = AMS grams − scale grams. The metric is the pair count, the
    mean absolute error and the mean signed bias, per spool and per type.
15. **"Opened"** is the spool's first `loaded` event, else its first plate. Days since
    opened and since last use are null when that never happened.
16. **Routes.** `GET /filament/spools/{id}/history` (timeline rows with their links, the
    series, the spool's metrics, its type's AMS error), `GET /filament/forecast` (per spool
    and per type), `GET /filament/metrics` (stock, consumption, waste), and
    `GET /filament/metrics/purchasing` (price per kg over time, spend per supplier and
    month). One agent read tool each.
17. **Links on the timeline.** A plate links to its BB print (`/prints/{data.bb_archive_id}`)
    when BB matched one, and to its order when `order_item_fulfilments.print_run_id` names
    the run in `ref_id`. The history route adds `order_id` and `archive_id`; the browser
    builds the paths.
18. **"Consumed" tile** is the grams of every plate of every spool, archived ones included
    (`InventorySummary.consumed_g`), as BB's tile is.
19. **Metrics tab view**: range (`90d`, `180d`, `365d`, default `180d`), bucket (`week`,
    `month`, default `month`) and dimension (`material`, `brand`, `type`, `supplier`,
    `location`, default `material`) in the URL, remembered behind "Remember my view" under
    `scadbuddy.filament.metrics.view`, as the Invoices tab does.
20. **One index.** `filament_spool_events_plates ON filament_spool_events (spool_id, at)
    WHERE kind IN ('print_started', 'print_ended')`, so the inventory reads skip the AMS
    readings.

## Review Focus

1. A plate that both SB and BB booked on one spool counts once in used grams, waste and the
   burn rate (Task 3 test `test_a_paired_plate_counts_once`).
2. A spool with no reading and no plate (a new, never-weighed spool) gives null burn, null
   empty date, an empty series and no error in any route (Task 3
   `test_a_bare_spool_has_no_metrics`, Task 4 `test_history_of_a_new_spool`).
3. A type with a lead time longer than its days of stock has a reorder date in the past and
   says "reorder now", never a negative day count (Task 3
   `test_reorder_date_in_the_past_is_now`).
4. Twenty-five brands in stock export 20 brand values plus `other`, and the sum of the
   gauges equals the stock total (Task 6 `test_brands_past_twenty_fold_into_other`).
5. A failing snapshot read keeps the scrape at 200 with `scadbuddy_filament_metrics_up 0`
   and the last values (Task 6 `test_a_failed_snapshot_keeps_the_scrape`).

---

### Task 1: Merge main, migration, SKU settings and the new settings

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_history_metrics.sql`
- Modify: `backend/scadbuddy/filament/models.py` (`SpoolType`, `SpoolTypeCreate`,
  `SpoolTypePatch`), `backend/scadbuddy/filament/types.py` (columns),
  `backend/scadbuddy/core/settings.py`, `backend/scadbuddy/core/setting_docs.py`,
  `agent/src/tools/filament.ts` (`filament_type_upsert` fields)
- Test: `backend/tests/filament/test_types.py`, `backend/tests/filament/test_migrations.py`,
  `backend/tests/api/test_settings.py`, `agent/test/filament.test.ts`

**Interfaces:**
- Produces: columns `filament_spool_types.lead_time_days integer CHECK (>= 0)`,
  `safety_margin_days integer CHECK (>= 0)`, both nullable; the index of Decision 20.
  Settings `filament_burn_window_days: int = Field(default=30, ge=1, le=365)`,
  `filament_lead_time_days: int = Field(default=7, ge=0, le=365)`,
  `filament_safety_margin_days: int = Field(default=3, ge=0, le=365)`, all `"live"`.
  `SpoolType.lead_time_days: int | None`, `safety_margin_days: int | None`.

- [ ] **Step 1: Merge main.** Run `gh pr view 6650 --json state -q .state`. Expected:
  `MERGED`. If not, stop and report BLOCKED. Then `git fetch origin && git merge origin/main`.
- [ ] **Step 2: Write the failing tests.**
  `test_type_keeps_lead_time_and_safety_margin`: create a type with `lead_time_days=10`,
  `safety_margin_days=2`; `get` returns both; a patch with `lead_time_days=None` clears it.
  `test_negative_lead_time_is_refused`: `SpoolTypePatch(lead_time_days=-1)` raises
  `ValidationError`. `test_migration_adds_sku_columns_and_plate_index`: the columns exist and
  `pg_indexes` has `filament_spool_events_plates`. `test_filament_metrics_settings_defaults`:
  `GET /api/v1/settings` shows 30, 7 and 3. Agent: `filament_type_upsert` passes
  `lead_time_days` through to the PATCH body.
- [ ] **Step 3: Run them.** Expected: FAIL (no column, no field).
- [ ] **Step 4: Implement** the migration, model fields, setting fields and docs
  ("Days of print history the burn rate and the forecast look at", "Days a type takes to
  arrive once ordered, when the type sets none", "Days of stock kept on top of the lead
  time, when the type sets none"), and the tool's two optional integer fields.
- [ ] **Step 5: Run them.** Expected: PASS. Run ruff, mypy, agent typecheck.
- [ ] **Step 6: Commit** `feat(filament): SKU lead time and safety margin, metrics settings (#6269)`.

### Task 2: Who changed a spool, field by field

**Files:**
- Create: `backend/scadbuddy/filament/provenance.py`
- Modify: `backend/scadbuddy/filament/ledger.py` (`record`), `backend/scadbuddy/filament/types.py`
  (`update`, `set_presets`, `set_k_values`), `backend/scadbuddy/filament/invoices.py`
  (the confirm's `invoice_line_id` write)
- Test: `backend/tests/filament/test_ledger.py`, `backend/tests/filament/test_types.py`,
  `backend/tests/filament/test_invoice_confirm.py`, `backend/tests/api/test_filament.py`

**Interfaces:**
- Produces: `provenance.current_by() -> dict[str, str] | None` (Decision 10).
  `ledger.record` merges `{"by": ...}` into `data` when it is not None and `data` has no
  `by`. Type-edit events: `kind="edited"`, `data={"changes": {field: [before, after]},
  "type_id": str(type_id)}`, `actor` the editor's.

- [ ] **Step 1: Write the failing tests.**
  `test_record_adds_the_agent_author`: inside `authorship` context with principal `user:a`,
  session `s1`, tool `filament_spool_upsert`, a recorded event has
  `data["by"] == {"principal": "user:a", "session": "s1", "tool": "filament_spool_upsert"}`.
  `test_record_without_an_author_adds_nothing`: `"by" not in data`.
  `test_type_edit_writes_edited_on_spools_that_inherit`: a type with spools A (no override),
  B (overrides `cost_per_kg`), C (archived); patch the type's `cost_per_kg` 20 → 25. A has one
  new `edited` event whose `changes` has only `cost_per_kg`, with
  `[Decimal(v) for v in changes["cost_per_kg"]] == [20, 25]` (Decimals go to JSON as
  strings), and `data["type_id"] == str(type_id)`; B and C have none.
  `test_type_label_weight_edit_refreshes_grams`: a spool whose anchor is an `ams_remain` at
  50 % of a 1000 g label reads 500 g; the type's label weight goes to 800 g; the spool reads
  400 g. `test_confirm_writes_edited_on_existing_spools`: an invoice line with
  `update_spool_ids=[s]` confirmed; `s` has an `edited` event with
  `changes["invoice_line_id"] == [None, str(line_id)]` and `ref_kind` the invoice line's.
  API: `test_patch_spool_by_agent_records_session` sends the author headers with the agent
  service credential and reads the event back.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** `current_by` reads `current_author()` and keeps the set keys.
  The type fan-out runs in the type update's transaction: list the type's unarchived spools
  `FOR UPDATE` in id order, compare each changed type field against the spool's override
  (`types.overridden(row)`), and call `ledger.record` for each spool that inherits a changed
  field. Presets and K values fan out with `changes.presets` / `changes.k_values` to spools
  with no own rows.
- [ ] **Step 4: Run them, plus `tests/filament/test_spools.py` and `test_bambuddy_pull.py`.**
  Expected: PASS (no existing assertion compares a whole `data` dict that now has `by`; fix
  any that does by asserting the keys it checks).
- [ ] **Step 5: Commit** `feat(filament): edited events say who changed each field, type edits included (#6269)`.

### Task 3: Plates, spool metrics, AMS error and the series (pure)

**Files:**
- Create: `backend/scadbuddy/filament/metrics.py`, `backend/scadbuddy/filament/metrics_models.py`
- Test: `backend/tests/filament/test_metrics.py` (no database)

**Interfaces:**
- Produces, in `metrics_models.py` (pydantic, `_Out` style of `models.py`):
  - `Outcome = Literal["completed", "failed", "cancelled", "unknown"]`
  - `Plate(job: str | None, at: datetime, grams: float, outcome: Outcome, printer_id: UUID | None, event_id: UUID)`
  - `AmsError(pairs: int, mean_abs_g: float, bias_g: float)`
  - `GramsPoint(at: datetime, grams: float, anchor: bool, kind: EventKind)`
  - `SpoolMetrics(used_g, prints: int, grams_per_print: float | None, wasted_g, cost_used: Decimal | None, value_left: Decimal | None, burn_g_per_day: float | None, empty_on: date | None, opened_at: datetime | None, days_since_opened: float | None, days_since_last_use: float | None, hours_outside_dry: float, ams_error: AmsError | None)`
- Produces, in `metrics.py`:
  - `plates(events: Sequence[SpoolEvent]) -> list[Plate]` (Decision 6)
  - `burn(plates: Sequence[Plate], *, window_days: int, born: datetime, now: datetime) -> float | None` (Decision 8; `None` when no plate in the window)
  - `empty_on(grams_left: float | None, burn_g_per_day: float | None, now: datetime) -> date | None`
  - `reorder_on(empty: date | None, lead_time_days: int, safety_margin_days: int) -> date | None`
  - `ams_pairs(events: Sequence[SpoolEvent], label_weight_g: int | None) -> list[float]` and `ams_error(errors: Sequence[float]) -> AmsError | None` (Decision 14)
  - `series(events: Sequence[SpoolEvent], grams_left: float | None, *, weight_locked: bool, label_weight_g: int | None, limit: int = 500) -> list[GramsPoint]` (Decision 13)
  - `spool_metrics(events, *, grams_left, cost_per_kg: Decimal | None, created_at, last_used_at, hours_outside_dry, label_weight_g, window_days, now) -> SpoolMetrics`

- [ ] **Step 1: Write the failing tests** (events built with a small `event(...)` helper,
  as `test_ledger.py` does):
  - `test_a_paired_plate_counts_once`: SB `print_started` −30 estimate, SB `print_ended`
    −30 actual, BB usage `print_ended` −28 of the same job → one `Plate(grams=28.0,
    outcome="completed")`.
  - `test_bb_status_sets_the_outcome`: `bb_status` `failed` → `failed`, `cancelled` →
    `cancelled`, `"aborted"` → `unknown`.
  - `test_not_applied_is_a_failed_plate_of_zero` and `test_covered_plate_is_skipped`.
  - `test_open_estimate_is_not_used`: a `print_started` with no end → `plates == []`.
  - `test_burn_over_the_window`: plates of 60 g and 30 g inside a 30-day window, one of
    100 g outside, spool 90 days old → `burn == 3.0`.
  - `test_young_spool_burns_over_its_age`: spool 5 days old, one 50 g plate, window 30 →
    `10.0`; spool 6 hours old → divides by 1 day.
  - `test_empty_on`: 300 g at 10 g/day from 2026-11-18T12:00Z → `date(2026, 12, 18)`;
    burn `None` or `0` → `None`.
  - `test_reorder_date_in_the_past_is_now`: empty in 5 days, lead 7, safety 3 → a date
    5 days before today (the caller reads "reorder now"; the function never clamps).
  - `test_ams_error_pairs_the_newest_reading_before_a_scale`: AMS 520 g at t0, AMS 510 g at
    t1, scale 500 g at t2 (< 24 h) → `[10.0]`; with a plate between t1 and t2 → `[]`; an AMS
    reading with `printing` true is not used; one 25 h before → `[]`.
  - `test_ams_error_summary`: errors `[10, -4]` → `AmsError(pairs=2, mean_abs_g=7.0, bias_g=3.0)`.
  - `test_series_uses_grams_before_of_the_next_event`: three events with `grams_before`
    `None, 1000, 970` and `grams_left` 950 → grams `[1000, 970, 950]`; the scale reading is
    `anchor=True`.
  - `test_series_thins_to_the_limit_and_keeps_anchors`: 1200 AMS readings and 3 scale
    readings, `limit=500` → `len <= 500`, all 3 scale points present, last point present.
  - `test_a_bare_spool_has_no_metrics`: no events → `used_g == 0`, `prints == 0`,
    `grams_per_print is None`, `burn_g_per_day is None`, `empty_on is None`,
    `ams_error is None`.
  - `test_cost_used_and_value_left`: 250 g used, 750 g left at 24.0000 per kg →
    `cost_used == Decimal("6.00")`, `value_left == Decimal("18.00")`.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/filament/test_metrics.py -n 4`.
  Expected: FAIL (no module).
- [ ] **Step 3: Implement.** `plates` reuses `ledger.job_of`, `ledger.BB_USAGE`,
  `ledger.PRINT_RUN`. `series` marks an anchor with `ledger.basis` on the whole history
  (one call) plus every absolute reading `basis` would accept at its time: a reading is an
  anchor when its source is in `ledger`'s absolute set and it is not an `ams_remain`
  reading that is `printing`, locked, or older than a later manual reading. Money is
  quantized to `Decimal("0.01")` as `spools.summary` does.
- [ ] **Step 4: Run.** Expected: PASS; ruff and mypy clean.
- [ ] **Step 5: Commit** `feat(filament): plates, burn rate, AMS error and the grams series (#6269)`.

### Task 4: The store and the four routes

**Files:**
- Create: `backend/scadbuddy/filament/metrics_store.py`
- Modify: `backend/scadbuddy/filament/metrics_models.py`, `backend/scadbuddy/filament/component.py`,
  `backend/scadbuddy/filament/spools.py` (`summary`: `consumed_g`),
  `backend/scadbuddy/filament/models.py` (`InventorySummary.consumed_g: float`),
  `backend/scadbuddy/api/filament.py`
- Test: `backend/tests/filament/test_metrics_store.py`, `backend/tests/api/test_filament_metrics.py`

**Interfaces:**
- Consumes: Task 3's functions; `spools._hours_outside_dry` (#6268); settings of Task 1.
- Produces, in `metrics_models.py`:
  - `TimelineRow(event: SpoolEvent, order_id: UUID | None, archive_id: int | None)`
  - `SpoolHistory(events: list[TimelineRow], series: list[GramsPoint], metrics: SpoolMetrics, type_ams_error: AmsError | None, currency: str)`
  - `Dimension = Literal["material", "brand", "type", "supplier", "location"]`
  - `StockRow(key: str | None, label: str, spools: int, grams_left: float, value: Decimal | None)`
  - `ConsumptionRow(bucket: date, key: str | None, label: str, used_g: float, wasted_g: float)`
  - `WasteRow(dimension: Literal["material", "type", "printer"], key: str | None, label: str, used_g: float, wasted_g: float, rate: float | None)`
  - `InventoryMetrics(currency: str, dimension: Dimension, bucket: Literal["week", "month"], since: date, until: date, stock: list[StockRow], consumption: list[ConsumptionRow], waste: list[WasteRow])`
  - `SpoolForecast(spool_id, number: int, type_id: UUID | None, grams_left: float | None, burn_g_per_day: float | None, empty_on: date | None)`
  - `TypeForecast(type_id, label: str, material: str | None, spools: int, grams_left: float, burn_g_per_day: float | None, days_of_stock: float | None, empty_on: date | None, lead_time_days: int, safety_margin_days: int, reorder_on: date | None, reorder_now: bool, low_stock: bool)`
  - `Forecast(window_days: int, spools: list[SpoolForecast], types: list[TypeForecast])`
  - `PriceSeries(type_id, type_label: str, supplier_id, supplier_name: str, currency: str, points: list[PricePerKg])` with `PricePerKg(on_date: date, price_per_kg: Decimal)`
  - `SpendRow(supplier_id, supplier_name: str, month: date, currency: str, total: Decimal, invoices: int)`
  - `PurchasingMetrics(prices: list[PriceSeries], spend: list[SpendRow])`
  - `Snapshot(stock: list[tuple[str | None, str | None, str | None, int, float, Decimal | None]], used: dict[tuple[str | None, Outcome], float], burn: dict[str | None, float], days_of_stock: dict[str | None, float], low_stock: dict[str | None, int], reorder: dict[str | None, int], currency: str)`: `stock` rows are (material, brand, location_kind, spools, grams, value).
- `MetricsStore(pool, settings: Callable[[], StoredSettings])` with `history(spool_id) -> SpoolHistory | None`, `forecast(now: datetime | None = None) -> Forecast`, `inventory(dimension, bucket, since, until) -> InventoryMetrics`, `purchasing(since, until) -> PurchasingMetrics`, `snapshot() -> Snapshot`. Registered on the `Filament` component as `metrics`.
- Routes (each with `summary=` and `description=`, `response_model`):
  `GET /api/v1/filament/spools/{spool_id}/history` (404 for no spool);
  `GET /api/v1/filament/forecast`;
  `GET /api/v1/filament/metrics?dimension=&bucket=&since=&until=` (defaults Decision 19;
  `since` defaults to 180 days before `until`; `until` to today; `since >= until` is 422;
  a range over 3 years is 422);
  `GET /api/v1/filament/metrics/purchasing?since=&until=` (default the last 365 days).

- [ ] **Step 1: Write the failing tests.**
  Store: `test_history_links_order_and_archive`: a plate whose `ref_id` run has an
  `order_item_fulfilments` row → `order_id` set; a BB plate with `bb_archive_id` 41 →
  `archive_id == 41`. `test_history_of_a_new_spool`: `events == []`, `series == []`,
  `metrics.burn_g_per_day is None`. `test_type_ams_error_spans_the_types_spools`: two spools
  of one type, one pair each (+10, −4) → `pairs == 2`. `test_forecast_type_reorder`: a
  type with lead 7, safety 3 and 5 days of stock → `reorder_now is True`; a type with no
  lead time uses the settings 7 and 3. `test_inventory_stock_by_supplier`: a spool with an
  invoice line of supplier S counts under S; one with none under `key None`, label
  `"No supplier"`. `test_consumption_buckets_by_month`: plates on 2026-10-03 and 2026-11-05
  → two rows, buckets `2026-10-01` and `2026-11-01`. `test_waste_by_printer`: one failed
  BB plate of 12 g and one completed of 36 g on printer P → `rate == 0.25`.
  `test_spend_keeps_each_currency`: two confirmed invoices of one supplier in one month, USD
  and CAD → two rows; drafts are not counted. `test_price_per_kg_series`: a pack of 2 spools
  of 1000 g at 40.0000 → `price_per_kg == Decimal("20.0000")`.
  `test_summary_consumed_includes_archived`: archived spool's plates count.
  `test_snapshot_folds_nothing_itself`: raw keys come back; folding is Task 6's.
  API: `test_history_route_404`, `test_metrics_route_rejects_reversed_range` (422),
  `test_metrics_routes_in_openapi_have_summary` (each new path has `summary` and
  `description`).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** One query per read: the spools with their names, effective
  cost, location kind and invoice supplier (reuse `spools._list` for effective values), then
  their plate events through the Decision 20 index
  (`WHERE kind IN ('print_started','print_ended')`), grouped by spool in Python and fed to
  `metrics.plates`. Purchasing reads `filament_price_points` joined to products and types
  (`price / pack_size / label_weight_g * 1000`, four places) and confirmed
  `filament_invoices` grouped by `supplier_id, date_trunc('month', invoice_date), currency`.
- [ ] **Step 4: Run them.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): spool history, forecast and inventory metrics routes (#6269)`.

### Task 5: Agent tools

**Files:**
- Modify: `agent/src/tools/filament.ts`, `agent/src/tools/coverage.ts` (nothing to add when
  every route has a tool; the coverage test must pass)
- Test: `agent/test/filament.test.ts`

**Interfaces:**
- Produces read tools (risk `read`, so no `touched.ts` entry):
  `filament_spool_history({spool_id})` → the route's answer without `series`, with the
  newest 50 `events` and `truncated: boolean`;
  `filament_forecast({})`; `filament_metrics({dimension?, bucket?, since?, until?})`;
  `filament_purchasing_metrics({since?, until?})`.

- [ ] **Step 1: Write the failing tests**: each tool calls its path with its query
  (`filament_metrics` with `dimension: 'brand'` sends `?dimension=brand`);
  `filament_spool_history` drops `series` and keeps 50 of 80 events with `truncated: true`;
  the coverage test lists no uncovered `/api/v1/filament/*` route.
- [ ] **Step 2: Run** `cd agent && pnpm test -- filament coverage`. Expected: FAIL.
- [ ] **Step 3: Implement** with `defineTool`, as `filament_summary` is.
- [ ] **Step 4: Run** `pnpm typecheck && pnpm lint && pnpm test`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(agent): filament history, forecast and metrics tools (#6269)`.

### Task 6: Prometheus gauges and the Filament dashboard

**Files:**
- Modify: `backend/scadbuddy/core/metrics.py`, `backend/scadbuddy/api/metrics.py`,
  `deploy/grafana/kustomization.yaml`, `deploy/CLAUDE.md` (the area list)
- Create: `backend/scadbuddy/filament/metrics_export.py`, `deploy/grafana/scadbuddy-filament.json`
- Test: `backend/tests/filament/test_metrics_export.py`, `backend/tests/api/test_metrics.py`

**Interfaces:**
- Consumes: `MetricsStore.snapshot()`.
- Produces, in `Metrics.__init__` (API role only; not in `ROLE_SERIES`), all `Gauge`:
  `scadbuddy_filament_metrics_up` (no labels);
  `scadbuddy_filament_stock_grams` and `scadbuddy_filament_stock_spools`
  `["material", "brand", "location_kind"]`;
  `scadbuddy_filament_stock_value` `["material", "currency"]`;
  `scadbuddy_filament_used_grams` `["material", "outcome"]`;
  `scadbuddy_filament_burn_grams_per_day`, `scadbuddy_filament_days_of_stock`,
  `scadbuddy_filament_low_stock_spools`, `scadbuddy_filament_reorder_types` `["material"]`.
- `metrics_export.fold(values: Mapping[str | None, float], top: int = 20) -> dict[str, float]`
  (null → `none`, the 20 largest kept, the rest summed into `other`) and
  `metrics_export.export(metrics: Metrics, snapshot: Snapshot) -> None` (clears, folds, sets).
- `api/metrics.py` `refresh_filament_metrics(state) -> None`: snapshot at most every
  `FILAMENT_METRICS_MAX_AGE = 60.0` s, in `asyncio.to_thread`; on any exception log it, set
  `metrics_up` 0 and keep the values.

- [ ] **Step 1: Write the failing tests.**
  `test_brands_past_twenty_fold_into_other`: 25 brands → 21 label values, `other` holds the
  5 smallest, the sum equals the total. `test_null_label_is_none`.
  `test_export_clears_a_gone_material`: export PLA and PETG, then PLA only → no PETG series.
  `test_a_failed_snapshot_keeps_the_scrape`: the store raises → `GET /metrics` 200, body has
  `scadbuddy_filament_metrics_up 0.0` and the earlier stock value.
  `test_snapshot_is_cached_for_a_minute`: two scrapes inside 60 s call `snapshot` once.
  `test_worker_roles_do_not_export_filament`: `Metrics(role="print")` exposition has no
  `scadbuddy_filament_`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the gauges, `fold`, `export` and the refresh, called from
  `metrics()` beside `refresh_store_metrics`, in its own try.
- [ ] **Step 4: Write the dashboard** `scadbuddy-filament.json`: uid `scadbuddy-filament`,
  title `ScadBuddy / Filament`, tags `["scadbuddy"]`, the overview's `links`, the
  `DS_PROMETHEUS`, `DS_TEMPO` and `namespace` variables copied from
  `scadbuddy-printing.json`. Panels (every query filtered by `namespace="$namespace"`):
  1. Stat "Stock (kg)": `sum(max by (material, brand, location_kind) (scadbuddy_filament_stock_grams{…})) / 1000`.
  2. Stat "Stock value": `sum(max by (material, currency) (scadbuddy_filament_stock_value{…}))`, legend `{{currency}}`.
  3. Bar gauge "Stock by material (g)": `sum by (material) (max by (material, brand, location_kind) (scadbuddy_filament_stock_grams{…}))`.
  4. Time series "Used per day by material (g)": `sum by (material) (max by (material, outcome) (delta(scadbuddy_filament_used_grams{…}[1d])))`.
  5. Time series "Waste rate": failed plus cancelled `delta` over all `delta`, unit `percentunit`.
  6. Bar gauge "Days of stock by material": `max by (material) (scadbuddy_filament_days_of_stock{…})`, thresholds red < 14, amber < 30.
  7. Stat "Low-stock spools": `sum(max by (material) (scadbuddy_filament_low_stock_spools{…}))`.
  8. Stat "Types to reorder": `sum(max by (material) (scadbuddy_filament_reorder_types{…}))`.
  9. Stat "Metrics read": `min(scadbuddy_filament_metrics_up{…})`, 0 red.
  Add the `scadbuddy-filament-dashboard` generator entry to `kustomization.yaml`.
- [ ] **Step 5: Run** `uv run --frozen pytest tests/filament/test_metrics_export.py tests/api/test_metrics.py -n 4`
  and `KUSTOMIZE=… .github/scripts/lint-dashboard.sh`. Expected: PASS, no problems.
- [ ] **Step 6: Commit** `feat(filament): filament gauges and the Grafana Filament dashboard (#6269)`.

### Task 7: Spool timeline and grams-left chart

Load `frontend-design` and `dataviz` first.

**Files:**
- Create: `frontend/src/components/filament/SpoolTimeline.tsx`,
  `frontend/src/components/filament/GramsChart.tsx`,
  `frontend/src/components/filament/SpoolMetricsPanel.tsx`,
  `frontend/src/components/filament/eventText.ts` (actor and change text)
- Modify: `frontend/src/api/filament.ts` (`getSpoolHistory`),
  `frontend/src/pages/filament/SpoolPage.tsx` (History section),
  `frontend/src/components/filament/SpoolSections.tsx` (remove `HistoryList`),
  `frontend/src/mocks/features/filament/index.ts`, `seed.ts`
- Test: `SpoolTimeline.test.tsx`, `GramsChart.test.tsx`, `eventText.test.ts`,
  `frontend/src/pages/filament/SpoolPage.test.tsx`

**Interfaces:**
- Consumes: `GET /filament/spools/{id}/history` (`SpoolHistory`).
- Produces: `actorText(event: SpoolEvent): string` (`service:agent` with `data.by.session`
  → `Assistant`; with `data.by.flow_run` → `Flow run`; `bambuddy` → `Bambuddy sync`;
  `print` and `system:printer-watch` → `Printer`; other `system:*` → `ScadBuddy`; a principal
  → the principal; null → `Unknown`). `changeLines(changes, names: CatalogNames): string[]`
  (`Cost per kg: 20.00 → 25.00`; ids resolved to names through `catalogs.ts`; `presets` and
  `k_values` → `Slicer presets changed` / `K values changed`; a `data.type_id` event says
  `from its type`).
  `GramsChart({ points, label })` (plain SVG, 320 × 120 viewBox, anchors as filled circles,
  plates as short ticks on the x axis, `role="img"` with an `aria-label` of
  `"<label>: <first> g to <last> g, <n> anchor readings"`).

- [ ] **Step 1: Write the failing tests.** `eventText.test.ts`: each `actorText` case above;
  `changeLines({cost_per_kg: ['20.0000', '25.0000']})` → `['Cost per kg: 20.00 → 25.00']`;
  a `type_id` change shows the type names. `GramsChart.test.tsx`: three points with one
  anchor → one `circle[data-anchor]`, the aria label; no points → "No weight history yet."
  `SpoolTimeline.test.tsx`: an `edited` row lists each change and its actor; a plate with
  `order_id` links to `/orders/<id>`, with `archive_id` 41 to `/prints/41`; a filter of kinds
  (All, Prints, Weighings, Edits, Moves, Tags) narrows the rows. `SpoolPage.test.tsx`: the
  History section shows the chart, the metrics ("Burn rate", "Empty around", "Waste",
  "AMS error") and the timeline from the mock.
- [ ] **Step 2: Run** `pnpm exec vitest run src/components/filament src/pages/filament/SpoolPage.test.tsx`
  after `pnpm gen:api`. Expected: FAIL.
- [ ] **Step 3: Implement.** The metrics panel shows money with `useMoney`; null metrics show
  "—" with a line that says why ("No prints in the last 30 days"). The mock answers the
  history from the seed's events, with ids from `newId`.
- [ ] **Step 4: Run** `pnpm typecheck && pnpm lint && pnpm test`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(frontend): spool timeline, grams-left chart and spool metrics (#6269)`.

### Task 8: Metrics tab, forecast view, "Consumed" tile and the settings

Load `frontend-design` and `dataviz` first.

**Files:**
- Create: `frontend/src/pages/filament/MetricsTab.tsx`,
  `frontend/src/components/filament/metricsQuery.ts`,
  `frontend/src/components/filament/charts/BarList.tsx`,
  `frontend/src/components/filament/charts/ColumnChart.tsx`,
  `frontend/src/components/filament/charts/LineChart.tsx`,
  `frontend/src/components/filament/SpoolForecast.tsx`,
  `frontend/src/mocks/features/filament/metrics.ts`
- Modify: `frontend/src/pages/filament/tabs.ts` (Metrics `available: true`),
  `frontend/src/App.tsx` (route `filament/metrics`), `frontend/src/pages/filament/FilamentPage.tsx`,
  `frontend/src/components/filament/spoolsQuery.ts` (`view: 'forecast'`),
  `frontend/src/pages/filament/SpoolsTab.tsx`, `frontend/src/components/filament/SpoolTiles.tsx`,
  `frontend/src/pages/settings/sections/filament.tsx`, `frontend/src/pages/filament/TypePage.tsx`
  (lead time and safety margin fields), `frontend/src/api/filament.ts`
- Test: `MetricsTab.test.tsx`, `metricsQuery.test.ts`, `SpoolForecast.test.tsx`,
  `spoolsQuery.test.ts`, `SpoolTiles` test in `SpoolsTab.test.tsx`, `tabs.test.ts`,
  the Settings Filament section test, `TypePage.test.tsx`

**Interfaces:**
- Consumes: Task 4's routes; Task 1's settings and type fields.
- Produces: `MetricsQuery = { range: '90d' | '180d' | '365d'; bucket: 'week' | 'month'; dimension: Dimension }`,
  `parseMetricsQuery(params)`, `metricsParams(q)`, `rememberedMetricsQuery()`,
  `rememberMetrics(q)`, key `scadbuddy.filament.metrics.view` (try/catch around storage).

- [ ] **Step 1: Write the failing tests.** `metricsQuery.test.ts`: defaults `180d`, `month`,
  `material`; an unknown value falls back; remembering on keeps the view across a remount;
  a storage that throws is ignored. `MetricsTab.test.tsx`: sections "Stock", "Consumption",
  "Waste", "Prices", "Spend" and "Days of stock" render from the mock; changing the dimension
  to Brand refetches with `dimension=brand`; spend rows show each currency apart; a type
  with `reorder_now` shows "Reorder now". `SpoolForecast.test.tsx`: rows sorted by empty
  date, nulls last; burn shown as `g/day`. `spoolsQuery.test.ts`: `?view=forecast` parses.
  Tiles: "Consumed" shows `consumed_g`. `tabs.test.ts`: Metrics is available. Settings:
  "Burn rate window (days)", "Default lead time (days)", "Default safety margin (days)"
  save with "Save Filament". TypePage: the two fields save and clear.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Charts are plain SVG with `role="img"` and a text summary;
  colours from the `dataviz` palette mapped to theme tokens; at phone width the charts take
  the full width and tables scroll inside their own box. The forecast view is a third view
  button beside table and cards.
- [ ] **Step 4: Run** `pnpm typecheck && pnpm lint && pnpm test`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(frontend): Filament metrics tab, forecast view and consumed tile (#6269)`.

### Task 9: End-to-end, docs and the PR

**Files:**
- Create: `frontend/e2e/filament-metrics.spec.ts`
- Modify: `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§5.7 and §5.2
  "As built in #6269" notes: Decisions 2, 4-9, 11, 13, 14), `backend/CLAUDE.md` (filament
  layout: `metrics.py`, `metrics_models.py`, `metrics_store.py`, `metrics_export.py`,
  `provenance.py`), `frontend/CLAUDE.md` (Metrics tab, forecast view, timeline; drop "Metrics
  as not available until #6269"), `deploy/CLAUDE.md` (`scadbuddy-filament`), this plan
  ("Decisions made while building (#6269)")

- [ ] **Step 1: Write the e2e**: open a seeded spool, see the chart with its anchors and an
  `Edited` row naming its changes; open Filament → Metrics, switch to Brand, see the
  Consumption chart change; open Spools, choose the forecast view, see a "Reorder now" type.
  Run `pnpm exec playwright test e2e/filament-metrics.spec.ts e2e/filament.spec.ts e2e/page-titles.spec.ts --workers=2`.
  Expected: PASS.
- [ ] **Step 2: Run the full gates**: backend ruff, format, mypy and the filament and API
  tests; frontend and agent `typecheck`, `lint`, `test`; `lint-dashboard.sh`.
  Expected: all pass.
- [ ] **Step 3: Write the docs** listed above, with the measured facts' sources.
- [ ] **Step 4: Commit** `docs(filament): history and metrics as built (#6269)`. Open the PR
  `feat(filament): spool history timeline and metrics, with a Grafana panel`, body
  `Fixes #6269`.

## Self-review

- **Spec coverage.** §5.7 History: `edited` with before, after and who (Task 2, Decisions
  10-12); timeline with moves, prints with print and order links, drying, weighings, tag
  changes (Task 7, Decision 17); grams-left chart with anchors (Tasks 3, 7, Decision 13).
  Per spool: used, prints, grams per print, wasted, cost used, value left, burn and forecast,
  days since opened and last use, hours outside dry storage (#6268's value), AMS error per
  spool and type (Tasks 3-4). Inventory: stock and consumption by material, brand, type,
  supplier and location (Task 4, location by name in the UI, by kind in the gauges); waste
  by material, type, printer; price per kg over time per type and supplier; spend per
  supplier and month; days of stock, low stock, reorder date from lead time (Tasks 1, 3, 4,
  8). Metrics tab (Task 8). Gauges and the Filament dashboard (Task 6, Decisions 2-5). §10
  forecast view and "Consumed" tile (Task 8, Decision 18). §5.2 `sku_settings` (Task 1,
  Decision 9).
- **Types.** `Plate`, `SpoolMetrics`, `AmsError`, `GramsPoint`, `Snapshot` are defined in
  Task 3 or 4 and used by those names in Tasks 4-8. `Dimension` is one Literal in Task 4 and
  the same five values in Task 8.
- **Review Focus.** Each line has its test in Tasks 3, 4 or 6.
