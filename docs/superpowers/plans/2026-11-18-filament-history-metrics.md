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
kustomize v5.8.1.

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
- Dashboards pass `.github/scripts/lint-dashboard.sh` (kustomize v5.8.1): uid equals the
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
  Dashboards `KUSTOMIZE=<kustomize v5.8.1> .github/scripts/lint-dashboard.sh`.

## Decisions this plan makes

1. **Build on #6268.** PR #6650 touches `filament/models.py`, `spools.py`,
   `api/filament.py`, the settings, `agent/src/tools/filament.ts` and `touched.ts`, and
   gives `hours_outside_dry` and `SensorChart`. Task 1 starts only after #6650 is merged and
   merges `origin/main`. Why: building beside it means a large conflict in five files.
2. **Prometheus gauges, not an OTel meter.** The spec says "OpenTelemetry metrics with the
   existing tracing setup". The backend has no OTel meter provider: every metric is a
   prometheus_client series in `core/metrics.py` at `GET /metrics`, the dashboards read
   Prometheus, and `lint-dashboard.sh` accepts only series declared there. So the filament
   numbers are declared there as the `FILAMENT_SERIES` table, which the lint reads, and a
   collector exposes them (Decision 3, #6785). Cost if wrong: a later move to an OTel meter
   renames nine series.
3. **The gauges come from a snapshot, refreshed at most every 60 s per process**
   (`FILAMENT_METRICS_MAX_AGE = 60.0`), in a thread, as the store-usage gauges are. One
   custom collector (`FilamentCollector`, `core/metrics.py`) yields the metric families of the
   last good snapshot. `export` builds the whole new snapshot first, then swaps one
   reference. A material that leaves stock leaves the series, a failed export keeps the old
   snapshot whole, and a scrape sees the old snapshot or the new one, never a mix (#6785;
   the first build set and removed gauge series one by one, so a failure or a scrape
   between the loops saw a mix).
   Every API pod exports the same values, so the dashboard reads them with `max by (...)`. For a counter-like value (`used_grams`) the `max by` comes before `delta`, through a subquery (R-T6-1).
4. **Labels.** `material` (the material's catalog `name`), `brand` (catalog name, a closed
   vocabulary), `location_kind` (the kind's catalog `name`, not the location's free-text
   name), `outcome`
   (`completed`, `failed`, `cancelled`, `unknown`), `currency` (the shop's). The spec's
   "location" is read as the location kind, because a location name is free text a person
   types. More than 20 values of one label fold into `other`. `filament_materials` and
   `filament_location_kinds` have no code column (F2), and the fold to 20 limits the
   cardinality. A rename of a material, brand or kind starts a new series.
5. **Consumption is a cumulative gauge.** `scadbuddy_filament_used_grams{material,outcome}`
   is the grams of every plate ever booked. Panels use `delta(...)` over the range. A
   counter would need the API to remember what it already counted across restarts.
6. **A plate is counted by §5.5 rule 3, with no anchor.** `metrics.plates` pairs SB's and
   BB's events of one job as `ledger.basis` does: BB's usage row wins. An SB plate with
   `basis` `not_applied` is a failed plate of 0 g; one with `basis` `covered` is skipped (its
   grams are on another spool). An open estimate (a print still running) is not used yet.
   Outcome: BB's `bb_status` mapped through `bambuddy/stages.py` `stage_of` (so `complete`, `canceled`,
   `aborted` and `error` count too; changed by the final review's fix, b4497b59a); a status
   whose stage is not done, failed or cancelled (`printing`, an unknown word) is `unknown`. The live BB status words are not measured yet. An SB
   plate is `completed` when applied, else `failed`.
7. **Waste is the grams of `failed` and `cancelled` plates.** An SB-only failed plate
   books 0 g, so its waste is 0 until BB's usage row is paired with it. The waste rate is
   wasted grams over all used grams.
8. **Burn rate** is the grams of plates that ended inside the window, divided by the
   window's days, or by the spool's age in days when it is younger (never less than 1 day).
   The burn of a type or a material is the burn of the union of its spools' plates, with
   the oldest spool's `created_at` as its age (F16).
   The window is the setting `filament_burn_window_days` (default 30, 1 to 365). The
   forecast empty date is `now + grams_left / burn`; none when the burn is 0.
9. **SKU settings are two type columns**, `lead_time_days` and `safety_margin_days`
   (integers ≥ 0, null = the setting). Settings `filament_lead_time_days` (default 7) and
   `filament_safety_margin_days` (default 3). Reorder date of a type =
   its empty date − lead time − safety margin. A type is "reorder now" when that date is
   today or past. BB parity with `FilamentSkuSettings`, with no separate table.
10. **Who changed it.** `ledger.record` adds `data.author` from `provenance.current_by()`
    (built on `orders/actor.py` `current_actor()`) when an agent or flow made the call:
    `{principal, session, tool, flow_run}`, the keys that are set. The key is `author`, not
    `by`: `capture.py` already writes `data.by` as a string (`"tag"`/`"link"`) on
    `loaded`/`unloaded` events (F1). `actor` keeps its meaning (`service:agent`,
    `bambuddy`, `print`, `system:*`, a person's principal). Every event kind gets it, not only
    `edited`. No new column.
11. **A type edit fans out.** A change to a type's field in `types.OVERRIDABLE`, or to its
    presets or K values, writes one `edited` event on every
    unarchived spool of that type that does not override the field, with
    `data.changes` and `data.type_id`. Other type fields (`code`, `note`, `archived`,
    `filament_id`, `lead_time_days`, `safety_margin_days`) write no spool event (F6). So a
    value a spool takes from its type can be traced
    too, and a label-weight change refreshes the spool's grams (the event has `changes`, so
    `refresh_derived` takes the full walk). `SpoolTypeStore._refresh_spools` goes: the
    fan-out does that refresh (F5). An archived spool gets none. Cost: one event per
    spool per type edit; types hold few spools.
12. **The invoice confirm writes its edit.** Setting `invoice_line_id` on spools that existed
    before the confirm (`update_spool_ids`) goes into the one `edited` event that
    `update_on` already writes with the invoice line's ref: `update_on` takes
    `invoice_line_id`, puts `[before, after]` in `changes` and sets the column in the same
    UPDATE (F4). A second event with the same ref would be dropped by
    `filament_spool_events_once`. New spools already get `created` with the ref.
13. **The chart shows what SB believed then.** Each event stores `grams_before`, the spool's
    grams left when it was booked. The value after an event is the next event's
    `grams_before` (by `seq`), and the spool's `grams_left` after the last. Points are
    sorted by `at`. A reading that `ledger.basis` would take as its anchor at the reading's
    own time is marked (F9). This is
    O(n) and needs no replay. A point whose value is unknown (None) is skipped (F11). More
    than 500 points thin out: the manual (`scale`, `person`) and BB anchors, the plates and
    the last point stay; AMS anchors thin with the rest, every k-th; the result never
    passes `limit` (F10).
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
    the run in `ref_id`. The history route adds `order_id` (text, as `orders.id` is) and
    `archive_id`; the browser builds the paths. The route returns the events newest first
    by `(at, seq)`, as `ledger.page` does (F28).
18. **"Consumed" tile** is the grams of every plate of every spool, archived ones included
    (`InventorySummary.consumed_g`), as BB's tile is. It and `SupplierStats.consumed_g`
    both count plates with `metrics.plates`, so a paired plate counts once (F15).
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
   `test_a_bare_spool_has_no_metrics`, Task 4 `test_history_of_a_new_spool`: its one
   `created` row, `series == []`).
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
  `SpoolTypePatch`), `backend/scadbuddy/core/settings.py`,
  `backend/scadbuddy/core/setting_docs.py`,
  `agent/src/tools/filament.ts` (`filament_type_upsert` description only),
  `frontend/src/mocks/features/filament/seed.ts` (type literals),
  `frontend/src/mocks/fixtures.ts` (settings), `frontend/src/pages/settings/fields.ts`
  (the three settings). `types.py` needs no edit: `_TYPE_COLUMNS` comes from
  `SpoolType.model_fields`.
- Test: `backend/tests/filament/test_types.py`, `backend/tests/filament/test_migrations.py`,
  `backend/tests/api/test_settings.py`

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
  `GET /api/v1/settings` shows 30, 7 and 3. No agent test: `filament_type_upsert` takes a
  free-form `fields` record, so `lead_time_days` already reaches the PATCH body.
- [ ] **Step 3: Run them.** Expected: FAIL (no column, no field).
- [ ] **Step 4: Implement** the migration, model fields, setting fields and docs
  ("Days of print history the burn rate and the forecast look at", "Days a type takes to
  arrive once ordered, when the type sets none", "Days of stock kept on top of the lead
  time, when the type sets none"). Name `lead_time_days` and `safety_margin_days` in
  `filament_type_upsert`'s description; add no schema field. Add the two fields
  (`null`) to every type literal in `seed.ts`, the three settings (30, 7, 3) to
  `fixtures.ts`, and their entries to `pages/settings/fields.ts`, so the frontend
  typecheck holds after this task.
- [ ] **Step 5: Run them.** Expected: PASS. Run ruff, mypy, agent typecheck and test, and
  frontend `pnpm typecheck && pnpm test`.
- [ ] **Step 6: Commit** `feat(filament): SKU lead time and safety margin, metrics settings (#6269)`.

### Task 2: Who changed a spool, field by field

**Files:**
- Create: `backend/scadbuddy/filament/provenance.py`
- Modify: `backend/scadbuddy/filament/ledger.py` (`record`), `backend/scadbuddy/filament/types.py`
  (`update`, `set_presets`, `set_k_values`; remove `_refresh_spools`),
  `backend/scadbuddy/filament/spools.py` (`update_on` takes `invoice_line_id`),
  `backend/scadbuddy/filament/invoices.py` (`_confirm_line` passes `invoice_line_id`),
  `backend/scadbuddy/api/filament.py` (`patch_type`, `put_type_presets`,
  `put_type_k_values` take `caller: CallerDep` and pass `actor=actor(caller)`)
- Test: `backend/tests/filament/test_ledger.py`, `backend/tests/filament/test_types.py`,
  `backend/tests/filament/test_invoice_confirm.py`, `backend/tests/api/test_filament.py`

**Interfaces:**
- Produces: `provenance.current_by() -> dict[str, str] | None` (Decision 10). It derives
  from `orders/actor.py` `current_actor()`: `None` when the actor's `kind` is `browser`;
  else its `principal`, `session` and `run_id` (as `flow_run`), plus `tool` from
  `current_author()` (`Actor` has no tool), the keys that are set. Its docstring says why
  it is not `current_actor()` itself: the ledger stores a plain dict with `tool`.
  `ledger.record` merges `{"author": ...}` into `data` when it is not None and `data` has no
  `author`. Never write the key `by`: `capture.py` writes `data.by` as a string.
  Type-edit events: `kind="edited"`, `data={"changes": {field: [before, after]},
  "type_id": str(type_id)}`, `actor` the editor's. Only fields in `types.OVERRIDABLE`, and
  `presets` / `k_values`, fan out.
- `SpoolTypeStore.update(type_id, patch, *, actor: str)`,
  `set_presets(type_id, rows, *, actor: str)`, `set_k_values(type_id, rows, *, actor: str)`.
- `FilamentSpoolStore.update_on(conn, spool_id, patch, *, actor, ref_kind=None,
  ref_id=None, invoice_line_id: UUID | None = None)`: a set `invoice_line_id` that differs
  from the spool's goes into `changes` as `[before, after]` and into the same UPDATE, so the
  confirm writes one `edited` event.

- [ ] **Step 1: Write the failing tests.**
  `test_record_adds_the_agent_author`: inside `authorship` context with principal `user:a`,
  session `s1`, tool `filament_spool_upsert`, a recorded event has
  `data["author"] == {"principal": "user:a", "session": "s1", "tool": "filament_spool_upsert"}`.
  `test_record_without_an_author_adds_nothing`: `"author" not in data`.
  `test_type_edit_writes_edited_on_spools_that_inherit`: a type with spools A (no override),
  B (overrides `cost_per_kg`), C (archived); patch the type's `cost_per_kg` 20 → 25. A has one
  new `edited` event whose `changes` has only `cost_per_kg`, with
  `[Decimal(v) for v in changes["cost_per_kg"]] == [20, 25]` (Decimals go to JSON as
  strings), and `data["type_id"] == str(type_id)`; B and C have none.
  `test_type_edit_of_a_field_not_overridable_writes_nothing`: patch the type's `note` and
  `lead_time_days` → no new event on A.
  `test_type_label_weight_edit_writes_edited_on_inheriting_spools`: a spool with no own label
  weight gets one `edited` event with `changes["label_weight_g"] == [1000, 800]`. It asserts
  only the event; `test_spool_detail.py::test_a_types_label_weight_edit_moves_its_spools_stored_grams`
  keeps covering the grams.
  `test_confirm_writes_edited_on_existing_spools`: an invoice line with
  `update_spool_ids=[s]` confirmed, which also changes `s`'s type and cost; `s` has exactly
  one new `edited` event, with `changes["invoice_line_id"] == [None, str(line_id)]`, the
  `type_id` and `cost_per_kg` changes, and `ref_kind` the invoice line's.
  API: `test_patch_spool_by_agent_records_session` sends the author headers with the agent
  service credential and reads the event back. `test_patch_type_records_the_caller`: a
  `PATCH /types/{id}` by a signed-in person writes the fan-out event with that person's
  principal as `actor`, not `Unknown`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** `current_by` builds on `current_actor()` as the Interfaces say.
  The type fan-out runs in the type update's transaction: list the type's unarchived spools
  `FOR UPDATE` in id order, compare each changed `OVERRIDABLE` type field against the
  spool's override (`types.overridden(row)`), and call `ledger.record` for each spool that
  inherits a changed field. That event refreshes the spool's grams, so remove
  `_refresh_spools` and its call in `update`. Presets and K values fan out with
  `changes.presets` / `changes.k_values` to spools with no own rows. `_confirm_line` passes
  `invoice_line_id=line["id"]` to `update_on` for each known spool.
- [ ] **Step 4: Run them, plus `tests/filament/test_spools.py`, `test_spool_detail.py` and
  `test_bambuddy_pull.py`.**
  Expected: PASS (no existing assertion compares a whole `data` dict that now has `author`;
  fix any that does by asserting the keys it checks).
- [ ] **Step 5: Commit** `feat(filament): edited events say who changed each field, type edits included (#6269)`.

### Task 3: Plates, spool metrics, AMS error and the series (pure)

**Files:**
- Create: `backend/scadbuddy/filament/metrics.py`, `backend/scadbuddy/filament/metrics_models.py`
- Modify: `backend/scadbuddy/filament/ledger.py` (extract `pairs` and `is_anchor` from
  `basis`; `basis` calls them)
- Test: `backend/tests/filament/test_metrics.py` (no database), `backend/tests/filament/test_ledger.py`

**Interfaces:**
- Produces, in `ledger.py` (one source for §5.5 rule 3, used by `basis` and `metrics`):
  - `pairs(events: Sequence[SpoolEvent]) -> set[str]`: the jobs with a BB usage
    `print_ended` and an SB `print_run` event on this spool.
  - `is_anchor(event: SpoolEvent, *, weight_locked: bool, label_weight_g: int | None) -> bool`:
    the event has a reading in the absolute set, and it is not an `ams_remain` reading that
    is locked or `printing`. `basis` also skips an AMS reading older than the newest manual
    one, as today.
- Produces, in `metrics_models.py` (pydantic, plain `BaseModel`, as `InventorySummary` is):
  - `Outcome = Literal["completed", "failed", "cancelled", "unknown"]`
  - `Plate(job: str | None, at: datetime, grams: float, outcome: Outcome, printer_id: UUID | None, event_id: UUID)`
  - `AmsError(pairs: int, mean_abs_g: float, bias_g: float)`
  - `GramsPoint(at: datetime, grams: float, anchor: bool, kind: EventKind)`
  - `SpoolMetrics(used_g, prints: int, grams_per_print: float | None, wasted_g, cost_used: Decimal | None, value_left: Decimal | None, burn_g_per_day: float | None, empty_on: date | None, opened_at: datetime | None, days_since_opened: float | None, days_since_last_use: float | None, hours_outside_dry: float, ams_error: AmsError | None)`
- Produces, in `metrics.py`:
  - `plates(events: Sequence[SpoolEvent]) -> list[Plate]` (Decision 6)
  - `burn(plates: Sequence[Plate], *, window_days: int, born: datetime, now: datetime) -> float | None` (Decision 8; `None` when no plate in the window). For a type or a material, the caller passes the union of its spools' plates and `born` = the oldest spool's `created_at`; Task 4 uses this for `TypeForecast.burn_g_per_day`, `days_of_stock` and `Snapshot.burn`.
  - `empty_on(grams_left: float | None, burn_g_per_day: float | None, now: datetime) -> date | None`
  - `reorder_on(empty: date | None, lead_time_days: int, safety_margin_days: int) -> date | None`
  - `ams_pairs(events: Sequence[SpoolEvent], label_weight_g: int | None) -> list[float]` and `ams_error(errors: Sequence[float]) -> AmsError | None` (Decision 14)
  - `series(events: Sequence[SpoolEvent], grams_left: float | None, *, weight_locked: bool, label_weight_g: int | None, limit: int = 500) -> list[GramsPoint]` (Decision 13; a point whose value is None is skipped, so `GramsPoint.grams` stays `float`)
  - `spool_metrics(events, *, grams_left, cost_per_kg: Decimal | None, created_at, last_used_at, hours_outside_dry, label_weight_g, window_days, now) -> SpoolMetrics`

- [ ] **Step 1: Write the failing tests** (events built with a small `event(...)` helper,
  as `test_ledger.py` does):
  - `test_a_paired_plate_counts_once`: SB `print_started` −30 estimate, SB `print_ended`
    −30 actual, BB usage `print_ended` −28 of the same job → one `Plate(grams=28.0,
    outcome="completed")`.
  - `test_bb_status_sets_the_outcome`: `bb_status` `failed` → `failed`, `cancelled` →
    `cancelled`, `"aborted"` → `cancelled` (through `stages.stage_of`), `"printing"` →
    `unknown`.
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
  - `test_series_skips_an_unknown_value`: a spool with `grams_left` None and one event with
    `grams_before` None → `series == []`.
  - `test_series_marks_an_ams_anchor_at_its_time`: AMS 900 g at t0 (not `printing`), scale
    800 g at t1 → both points `anchor=True` (the AMS reading was the anchor at t0).
  - `test_series_thins_to_the_limit_and_keeps_anchors`: 1200 AMS readings (not `printing`,
    so anchors) and 3 scale readings, `limit=500` → `len <= 500`, all 3 scale points
    present, last point present; the AMS anchors are thinned.
  - `test_type_burn_is_the_union_of_its_spools`: spool X (born 90 days ago) with a 60 g
    plate and spool Y (born 10 days ago) with a 30 g plate, both in a 30-day window →
    `burn(X + Y plates, born=X's created_at) == 3.0`.
  - `test_a_bare_spool_has_no_metrics`: no events → `used_g == 0`, `prints == 0`,
    `grams_per_print is None`, `burn_g_per_day is None`, `empty_on is None`,
    `ams_error is None`.
  - `test_cost_used_and_value_left`: 250 g used, 750 g left at 24.0000 per kg →
    `cost_used == Decimal("6.00")`, `value_left == Decimal("18.00")`.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/filament/test_metrics.py -n 4`.
  Expected: FAIL (no module).
- [ ] **Step 3: Implement.** Extract `ledger.pairs` and `ledger.is_anchor` from `basis`
  first, and make `basis` call them; `basis`'s behaviour does not change. `plates` reuses
  `ledger.job_of`, `ledger.pairs`, `ledger.BB_USAGE`, `ledger.PRINT_RUN`. `series` marks
  a point as an anchor when `ledger.is_anchor` is true for it: the reading `basis` would
  accept at its own time (Decision 13: what SB believed then). A later manual reading does
  not unmark it. `metrics` never reads `ledger._ABSOLUTE` or `_MANUAL`. Thinning keeps
  every `scale`, `person` and BB anchor, every plate and the last point, then keeps every
  k-th of the rest (AMS anchors included) so the result is at most `limit`. Money is
  quantized to `Decimal("0.01")` as `spools.summary` does.
- [ ] **Step 4: Run** `test_metrics.py` and `test_ledger.py`. Expected: PASS; ruff and mypy clean.
- [ ] **Step 5: Commit** `feat(filament): plates, burn rate, AMS error and the grams series (#6269)`.

### Task 4: The store and the four routes

**Files:**
- Create: `backend/scadbuddy/filament/metrics_store.py`
- Modify: `backend/scadbuddy/filament/metrics_models.py`, `backend/scadbuddy/filament/component.py`,
  `backend/scadbuddy/filament/spools.py` (`summary`: `consumed_g`),
  `backend/scadbuddy/filament/models.py` (`InventorySummary.consumed_g: float`),
  `backend/scadbuddy/filament/suppliers.py` (`stats`: `consumed_g`),
  `backend/scadbuddy/api/filament.py`
- Test: `backend/tests/filament/test_metrics_store.py`, `backend/tests/api/test_filament_metrics.py`,
  `backend/tests/filament/test_suppliers.py`

**Interfaces:**
- Consumes: Task 3's functions; `Spool.hours_outside_dry` (#6268) from
  `FilamentSpoolStore.list(include_archived=True)`; `invoice_money.price_per_kg`;
  settings of Task 1.
- Produces, in `metrics_models.py`:
  - `TimelineRow(event: SpoolEvent, order_id: str | None, archive_id: int | None)` (`orders.id` is text and `Order.id` is `str`)
  - `SpoolHistory(events: list[TimelineRow], series: list[GramsPoint], metrics: SpoolMetrics, type_ams_error: AmsError | None, currency: str, window_days: int)`: `events` newest first by `(at, seq)`, as `ledger.page` orders them; `window_days` is `filament_burn_window_days`, as `Forecast` has it
  - `Dimension = Literal["material", "brand", "type", "supplier", "location"]`
  - `StockRow(key: str | None, label: str, spools: int, grams_left: float, value: Decimal | None)`
  - `ConsumptionRow(bucket: date, key: str | None, label: str, used_g: float, wasted_g: float)`
  - `WasteRow(dimension: Literal["material", "type", "printer"], key: str | None, label: str, used_g: float, wasted_g: float, rate: float | None)`
  - `InventoryMetrics(currency: str, dimension: Dimension, bucket: Literal["week", "month"], since: date, until: date, stock: list[StockRow], consumption: list[ConsumptionRow], waste: list[WasteRow])`
  - `SpoolForecast(spool_id, number: int, type_id: UUID | None, grams_left: float | None, burn_g_per_day: float | None, empty_on: date | None)`
  - `TypeForecast(type_id, label: str, material: str | None, spools: int, grams_left: float, burn_g_per_day: float | None, days_of_stock: float | None, empty_on: date | None, lead_time_days: int, safety_margin_days: int, reorder_on: date | None, reorder_now: bool, low_stock: bool)`
  - `Forecast(window_days: int, spools: list[SpoolForecast], types: list[TypeForecast])`
  - `PriceSeries(type_id, type_label: str, supplier_id, supplier_name: str, currency: str, points: list[PricePerKg])` with `PricePerKg(on_date: date, price_per_kg: Decimal)`: one series per (type, supplier, currency), so no series mixes currencies
  - `SpendRow(supplier_id, supplier_name: str, month: date, currency: str, total: Decimal, invoices: int)`
  - `PurchasingMetrics(prices: list[PriceSeries], spend: list[SpendRow])`
  - `Snapshot(stock: list[tuple[str | None, str | None, str | None, int, float, Decimal | None]], used: dict[tuple[str | None, Outcome], float], burn: dict[str | None, float], days_of_stock: dict[str | None, float], low_stock: dict[str | None, int], reorder: dict[str | None, int], currency: str)`: `stock` rows are (material, brand, location_kind, spools, grams, value); the three labels are catalog `name`s (Decision 4). `burn` per material is Task 3's union burn.
- `MetricsStore(pool, spools: FilamentSpoolStore, settings: Callable[[], StoredSettings])`
  (it gets the spool store as `InvoiceStore` does) with `history(spool_id) -> SpoolHistory | None`, `forecast(now: datetime | None = None) -> Forecast`, `inventory(dimension, bucket, since, until) -> InventoryMetrics`, `purchasing(since, until) -> PurchasingMetrics`, `snapshot() -> Snapshot`. Registered on the `Filament` component as `metrics`.
- Routes (each with `summary=` and `description=`, `response_model`):
  `GET /api/v1/filament/spools/{spool_id}/history` (404 for no spool);
  `GET /api/v1/filament/forecast`;
  `GET /api/v1/filament/metrics?dimension=&bucket=&since=&until=` (defaults Decision 19;
  `since` defaults to 180 days before `until`; `until` to today; `since > until` is 422 (a single day is allowed);
  a range over 3 years is 422);
  `GET /api/v1/filament/metrics/purchasing?since=&until=` (default the last 365 days).

- [ ] **Step 1: Write the failing tests.**
  Store: `test_history_links_order_and_archive`: a plate whose `ref_id` run has an
  `order_item_fulfilments` row → `order_id` set; a BB plate with `bb_archive_id` 41 →
  `archive_id == 41`. `test_history_of_a_new_spool`: `events` holds one row, its
  `created` event; `series == []`; `metrics.burn_g_per_day is None`.
  `test_history_is_newest_first`: three events → `events` in `(at, seq)` descending order.
  `test_history_has_the_window`: `window_days == 30` by default.
  `test_type_ams_error_spans_the_types_spools`: two spools
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
  `test_price_series_split_by_currency`: one type and supplier with a USD point and a CAD
  point → two `PriceSeries`, one per currency.
  `test_type_burn_uses_all_its_spools`: two spools of one type with plates → the type's
  `burn_g_per_day` is Task 3's union burn.
  `test_summary_consumed_includes_archived`: archived spool's plates count.
  `test_summary_consumed_counts_a_paired_plate_once`: SB −30 and BB −28 of one job → 28.
  In `test_suppliers.py`, `test_stats_consumed_counts_a_paired_plate_once`: the same pair
  on a spool of supplier S → `consumed_g == 28.0`; an open estimate counts 0.
  `test_snapshot_folds_nothing_itself`: raw keys come back; folding is Task 6's.
  API: `test_history_route_404`, `test_metrics_route_rejects_reversed_range` (422),
  `test_metrics_routes_in_openapi_have_summary` (each new path has `summary` and
  `description`).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** One query per read: the spools from
  `self._spools.list(include_archived=True)` (effective values, names and
  `hours_outside_dry`; archived spools are needed for consumption), plus each spool's
  location kind and invoice supplier, then
  their plate events through the Decision 20 index
  (`WHERE kind IN ('print_started','print_ended')`), grouped by spool in Python and fed to
  `metrics.plates`. `history` lists its events newest first by `(at, seq)`. Purchasing
  reads `filament_price_points` joined to products and types, computes price per kg in
  Python with `invoice_money.price_per_kg(price, pack_size, label_weight_g)` (no SQL copy
  of the formula), and groups the points by (type, supplier, currency). It reads confirmed
  `filament_invoices` grouped by `supplier_id, date_trunc('month', invoice_date), currency`.
  `spools.summary` computes `consumed_g` with the pure `metrics.plates` over every
  spool's plate events (it imports `metrics`, never `metrics_store`, so there is no
  import cycle). `SuppliersStore.stats` reads its spools' plate events and computes
  `consumed_g` with `metrics.plates` in place of its SQL sum, so the Suppliers tab and the
  Metrics tab show the same grams.
- [ ] **Step 4: Run them, plus `tests/filament/test_suppliers.py` and
  `tests/api/test_filament_purchasing.py`.** Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): spool history, forecast and inventory metrics routes (#6269)`.

### Task 5: Agent tools

**Files:**
- Modify: `agent/src/tools/filament.ts`, `agent/src/tools/coverage.ts` (nothing to add when
  every route has a tool; the coverage test must pass)
- Test: `agent/test/filament.test.ts`

**Interfaces:**
- Produces read tools (risk `read`, so no `touched.ts` entry):
  `filament_spool_history({spool_id})` → the route's answer without `series`, with the
  newest 50 `events` (the first 50: the route returns them newest first) and
  `truncated: boolean`;
  `filament_forecast({})`; `filament_metrics({dimension?, bucket?, since?, until?})`;
  `filament_purchasing_metrics({since?, until?})`.

- [ ] **Step 1: Write the failing tests**: each tool calls its path with its query
  (`filament_metrics` with `dimension: 'brand'` sends `?dimension=brand`);
  `filament_spool_history` drops `series` and keeps the first 50 of 80 newest-first events
  (the newest 50) with `truncated: true`;
  the coverage test lists no uncovered `/api/v1/filament/*` route.
- [ ] **Step 2: Run** `cd agent && pnpm test -- filament coverage`. Expected: FAIL.
- [ ] **Step 3: Implement** with `defineTool`, as `filament_summary` is.
- [ ] **Step 4: Run** `pnpm typecheck && pnpm lint && pnpm test`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(agent): filament history, forecast and metrics tools (#6269)`.

### Task 6: Prometheus gauges and the Filament dashboard

**Files:**
- Modify: `backend/scadbuddy/core/metrics.py`, `backend/scadbuddy/api/metrics.py`,
  `deploy/grafana/kustomization.yaml`, `deploy/CLAUDE.md` (the area list; the only
  `scadbuddy-filament` edit there, Task 9 does not repeat it)
- Create: `backend/scadbuddy/filament/metrics_export.py`, `deploy/grafana/scadbuddy-filament.json`
- Test: `backend/tests/filament/test_metrics_export.py`, `backend/tests/api/test_metrics.py`

**Interfaces:**
- Consumes: `MetricsStore.snapshot()`.
- Produces, in `Metrics.__init__` (API role only; not in `ROLE_SERIES`), `scadbuddy_filament_metrics_up` (a `Gauge`, no
  labels) and, as the module-level `FILAMENT_SERIES` table of `(name, help, labels)`
  that `FilamentCollector` exposes (#6785):
  `scadbuddy_filament_stock_grams` and `scadbuddy_filament_stock_spools`
  `["material", "brand", "location_kind"]`;
  `scadbuddy_filament_stock_value` `["material", "currency"]`;
  `scadbuddy_filament_used_grams` `["material", "outcome"]`;
  `scadbuddy_filament_burn_grams_per_day`, `scadbuddy_filament_days_of_stock`,
  `scadbuddy_filament_low_stock_spools`, `scadbuddy_filament_reorder_types` `["material"]`.
- Labels are catalog `name`s, from the snapshot as it is (Decision 4); a rename starts a
  new series.
- `metrics_export.fold(totals: Mapping[str | None, float], top: int = 20) -> frozenset[str | None]`
  ranks the values of one label by their totals and returns the kept set (the 20 largest).
  `metrics_export.label(value: str | None, kept: frozenset[str | None]) -> str` maps one
  row's value: null → `none`, kept → itself, else `other`. Rows that map to one label
  set are summed. There is one ranking per label, shared by every gauge with that label:
  `stock_grams` and `stock_spools` use the same kept brands (ranked by stock grams).
  `metrics_export.export(metrics: Metrics, snapshot: Snapshot) -> None` (clears, folds, sets).
- `api/metrics.py` `refresh_filament_metrics(state, *, now: Callable[[], float] = time.monotonic) -> None`:
  snapshot at most every `FILAMENT_METRICS_MAX_AGE = 60.0` s by `now()`, in
  `asyncio.to_thread`; on any exception log it, set `metrics_up` 0 and keep the values.

- [ ] **Step 1: Write the failing tests.**
  `test_brands_past_twenty_fold_into_other`: 25 brands → 21 label values, `other` holds the
  5 smallest, the sum equals the total. `test_null_label_is_none`.
  `test_stock_spools_folds_with_the_stock_grams_brands`: 25 brands, where the brand with the
  most spools has few grams → `stock_spools` and `stock_grams` carry the same 20 brand
  values plus `other`.
  `test_export_clears_a_gone_material`: export PLA and PETG, then PLA only → no PETG series.
  `test_a_failed_snapshot_keeps_the_scrape`: with an injected `now`, a first scrape
  succeeds; `now` moves 61 s; the store raises → `GET /metrics` 200, body has
  `scadbuddy_filament_metrics_up 0.0` and the earlier stock value.
  `test_snapshot_is_cached_for_a_minute`: two scrapes inside 60 s by the injected `now`
  call `snapshot` once.
  `test_worker_roles_do_not_export_filament` (a guard; it passes before this task too):
  `Metrics(role="print")` exposition has no `scadbuddy_filament_`.
- [ ] **Step 2: Run them.** Expected: FAIL, except the worker-role guard, which passes.
- [ ] **Step 3: Implement** the gauges, `fold`, `export` and the refresh, called from
  `metrics()` beside `refresh_store_metrics`, in its own try.
- [ ] **Step 4: Write the dashboard** `scadbuddy-filament.json`: uid `scadbuddy-filament`,
  title `ScadBuddy / Filament`, tags `["scadbuddy"]`, the overview's `links`, the
  `DS_PROMETHEUS`, `DS_TEMPO` and `namespace` variables copied from
  `scadbuddy-printing.json`. Panels (every query filtered by `namespace="$namespace"`):
  1. Stat "Stock (kg)": `sum(max by (material, brand, location_kind) (scadbuddy_filament_stock_grams{…})) / 1000`.
  2. Stat "Stock value": `sum by (currency) (max by (material, currency) (scadbuddy_filament_stock_value{…}))`, legend `{{currency}}` (a bare `sum` drops `currency` and leaves the legend blank).
  3. Bar gauge "Stock by material (g)": `sum by (material) (max by (material, brand, location_kind) (scadbuddy_filament_stock_grams{…}))`.
  4. Time series "Used per day by material (g)": `sum by (material) (delta((max by (material, outcome) (scadbuddy_filament_used_grams{…}))[1d:5m]))`.
  5. Time series "Waste rate": failed plus cancelled `delta` over all `delta`, unit `percentunit`. Each `delta` takes the deduped series: `sum(delta((max by (material, outcome) (scadbuddy_filament_used_grams{…, outcome=~"failed|cancelled"}))[1d:5m])) / sum(delta((max by (material, outcome) (scadbuddy_filament_used_grams{…}))[1d:5m]))`.
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
  `frontend/src/components/filament/charts/LineChart.tsx` (the one SVG line chart; Task 8
  reuses it),
  `frontend/src/components/filament/GramsChart.tsx` (wraps `LineChart`),
  `frontend/src/components/filament/SpoolMetricsPanel.tsx`,
  `frontend/src/components/filament/eventText.ts` (actor and change text, and the
  id-to-name lookup `EventNames`: `catalogs.ts` exports no `CatalogNames`)
- Modify: `frontend/src/api/filament.ts` (`getSpoolHistory`),
  `frontend/src/pages/filament/SpoolPage.tsx` (History section),
  `frontend/src/components/filament/SpoolSections.tsx` (remove `HistoryList`),
  `frontend/src/mocks/features/filament/index.ts`, `seed.ts`
- Test: `SpoolTimeline.test.tsx`, `GramsChart.test.tsx`, `charts/LineChart.test.tsx`,
  `eventText.test.ts`, `frontend/src/pages/filament/SpoolPage.test.tsx`

**Interfaces:**
- Consumes: `GET /filament/spools/{id}/history` (`SpoolHistory`, newest first, with
  `window_days`).
- Produces: `actorText(event: SpoolEvent): string` (`service:agent` with `data.author.session`
  → `Assistant`; with `data.author.flow_run` → `Flow run`; `bambuddy` → `Bambuddy sync`;
  `print` and `system:printer-watch` → `Printer`; other `system:*` → `ScadBuddy`; a principal
  → the principal; null → `Unknown`). It reads `data.author`, never `data.by` (a string on
  `loaded`/`unloaded` events). `EventNames = Map<string, string>` (id → name) and
  `eventNames(catalogRows, types): EventNames`, built from the catalog queries plus the
  types list. `changeLines(changes, names: EventNames): string[]`
  (`Cost per kg: 20.00 → 25.00`; ids resolved to names through `names`; `presets` and
  `k_values` → `Slicer presets changed` / `K values changed`; a `data.type_id` event says
  `from its type`).
  `LineChart({ points, label, ... })` in `charts/LineChart.tsx`: the plain SVG scale, axes
  and line, with optional marks; Task 8's charts reuse it.
  `GramsChart({ points, label })` (wraps `LineChart`; 320 × 120 viewBox, anchors as filled circles,
  plates as short ticks on the x axis, `role="img"` with an `aria-label` of
  `"<label>: <first> g to <last> g, <n> anchor readings"`).

- [ ] **Step 1: Write the failing tests.** `eventText.test.ts`: each `actorText` case above;
  `changeLines({cost_per_kg: ['20.0000', '25.0000']}, names)` → `['Cost per kg: 20.00 → 25.00']`;
  a `type_id` change shows the type names from `eventNames`; a `loaded` event with
  `data.by: 'tag'` does not read as an author. `LineChart.test.tsx`: the path spans the
  points' range. `GramsChart.test.tsx`: three points with one
  anchor → one `circle[data-anchor]`, the aria label; no points → "No weight history yet."
  `SpoolTimeline.test.tsx`: an `edited` row lists each change and its actor; a plate with
  `order_id` links to `/orders/<id>`, with `archive_id` 41 to `/prints/41`; a filter of kinds
  (All, Prints, Weighings, Edits, Moves, Tags) narrows the rows. `SpoolPage.test.tsx`: the
  History section shows the chart, the metrics ("Burn rate", "Empty around", "Waste",
  "AMS error") and the timeline from the mock; with a mock `window_days` of 14 and no plate,
  the panel says "No prints in the last 14 days".
- [ ] **Step 2: Run** `pnpm exec vitest run src/components/filament src/pages/filament/SpoolPage.test.tsx`
  after `pnpm gen:api`. Expected: FAIL.
- [ ] **Step 3: Implement.** The metrics panel shows money with `useMoney`; null metrics show
  "—" with a line that says why ("No prints in the last {window_days} days", from
  `SpoolHistory.window_days`, never a fixed 30). The mock answers the
  history from the seed's events, newest first, with ids from `newId`. `SpoolTimeline`
  builds `EventNames` from the catalog queries and the types list.
- [ ] **Step 4: Run** `pnpm typecheck && pnpm lint && pnpm test`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(frontend): spool timeline, grams-left chart and spool metrics (#6269)`.

### Task 8: Metrics tab, forecast view, "Consumed" tile and the settings

Load `frontend-design` and `dataviz` first.

**Files:**
- Create: `frontend/src/pages/filament/MetricsTab.tsx`,
  `frontend/src/components/filament/metricsQuery.ts`,
  `frontend/src/components/filament/charts/BarList.tsx`,
  `frontend/src/components/filament/charts/ColumnChart.tsx`,
  `frontend/src/components/filament/SpoolForecast.tsx`,
  `frontend/src/mocks/features/filament/metrics.ts`
- Reuse, do not create: `frontend/src/components/filament/charts/LineChart.tsx` (Task 7)
  for the price and consumption lines.
- Modify: `frontend/src/pages/filament/tabs.ts` (Metrics `available: true`),
  `frontend/src/App.tsx` (route `filament/metrics`),
  `frontend/e2e/page-titles.spec.ts` (`PAGES` gets `'/filament/metrics'`, or "covers every
  route App.tsx declares" fails), `frontend/src/pages/filament/FilamentPage.tsx`,
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
  date, nulls last; burn shown as `g/day`; the view also lists the type rows, and a type
  with `reorder_now` shows "Reorder now" there (spec §5.7: BB parity with the Forecast
  view; Task 9's e2e looks for it here). `spoolsQuery.test.ts`: `?view=forecast` parses.
  Tiles: "Consumed" shows `consumed_g`. `tabs.test.ts`: Metrics is available. Settings:
  "Burn rate window (days)", "Default lead time (days)", "Default safety margin (days)"
  save with "Save Filament" (their `fields.ts` entries and fixtures came in Task 1).
  TypePage: the two fields save and clear.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** Charts are plain SVG with `role="img"` and a text summary;
  colours from the `dataviz` palette mapped to theme tokens; at phone width the charts take
  the full width and tables scroll inside their own box. The forecast view is a third view
  button beside table and cards. It shows the spool rows and, from `Forecast.types`, the
  type rows with "Reorder now"; the Metrics tab's "Days of stock" section shows them too.
  Line charts use Task 7's `charts/LineChart`. Add `'/filament/metrics'` to `PAGES` in
  `e2e/page-titles.spec.ts`.
- [ ] **Step 4: Run** `pnpm typecheck && pnpm lint && pnpm test`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(frontend): Filament metrics tab, forecast view and consumed tile (#6269)`.

### Task 9: End-to-end, docs and the PR

R-T6-5: Task 9's as-built notes must say a material crossing rank 20 makes a one-day step

**Files:**
- Create: `frontend/e2e/filament-metrics.spec.ts`
- Modify: `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§5.7 and §5.2
  "As built in #6269" notes: Decisions 2, 4-9, 11, 13, 14; Decision 4's note says the
  labels are catalog names, so a rename of a material, brand or location kind starts a new
  series), `backend/CLAUDE.md` (filament
  layout: `metrics.py`, `metrics_models.py`, `metrics_store.py`, `metrics_export.py`,
  `provenance.py`), `frontend/CLAUDE.md` (Metrics tab, forecast view, timeline; drop "Metrics
  as not available until #6269"), this plan
  ("Decisions made while building (#6269)"). Not `deploy/CLAUDE.md`: Task 6 made the
  `scadbuddy-filament` edit.

- [ ] **Step 1: Write the e2e**: open a seeded spool, see the chart with its anchors and an
  `Edited` row naming its changes; open Filament → Metrics, switch to Brand, see the
  Consumption chart change; open Spools, choose the forecast view, see a "Reorder now" type
  row (Task 8 puts the type rows there).
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

## Decisions made while building (#6269)

(The builder adds one numbered line per decision, each with its reason.)

1. Task 1: The two SKU fields use a `Days` type (`ge=0, le=365`) in `filament/models.py`, so the model bound matches the settings' bound of 365 and a day count above a year is refused with a 422 and not stored.
2. Task 1: The three new settings also go into `api/settings.py` `SettingsResponse` and `library/settings_store.py` `SettingsPatch`, because `GET` and `PUT /api/v1/settings` only carry the fields those two models name.
3. Task 1: The filament mock (`frontend/src/mocks/features/filament/index.ts`) gained `lead_time_days` and `safety_margin_days` in `createType` and in the `typeCreate` field list, because the generated `SpoolType` now requires both and the frontend typecheck failed without them.
4. Task 2: `provenance.current_by` stores `Actor.run_id` under the key `flow_run`, the header's name (`X-ScadBuddy-Flow-Run`), as the brief says; the other keys keep `Actor`'s names.
5. Task 2: The type fan-out compares each changed field with the type row the UPDATE returns, not with the patch, so a value the database normalises (an `rgba`) is not a change, and a patch that sends a field's current value writes no event.
6. Task 2: A spool overrides `presets` or `k_values` when it has any own row of that list, and a type's presets or K values write no event when the list is the same before and after, so a whole-list save that changes nothing books nothing.
7. Task 2: `SpoolTypeStore.archive` also takes `actor`, because it calls `update`, which now needs one; archiving writes no spool event, as `archived` is not in `OVERRIDABLE`.
8. Task 2: The two API provenance tests build their own app with `tests/api/test_authorship_session.py`'s setup (a `secret_key_file`, `public_url` and `auth_mode` `observe`), because the module's `client` has no key to seal a service credential with, and `observe` is where that file tests that only the agent's credential may name an author.
9. Task 2: `test_presets.py`'s `test_setting_an_empty_list_resets_to_the_type` and `test_a_spool_k_value_overrides_its_types_and_resets` now count only the spool's own `edited` events (no `data.type_id`), because the type's preset or K-value save before them now writes a fan-out event on the spool, which had no rows of its own yet.
10. Task 3 (Task 2 review): The type fan-out of presets and K values works key by key, because `presets.py` merges them per key: a spool gets an `edited` event when the save added, removed or changed a key (a printer model and nozzle; a printer, extruder and nozzle) that the spool has no own row for, and none when it sets every changed key. This replaces the first clause of build decision 6 (any own row was an override).
11. Task 3 (Task 2 review): `test_types.py` `test_type_k_value_edit_writes_edited_on_an_inheriting_spool` covers the K-value fan-out on a spool with no K values of its own. It passed when it was written, because Task 2's code already fanned K values out; it guards that path, which no test reached before.
12. Task 3: `ledger._reading` is now the public `ledger.reading`, because `metrics.ams_pairs` needs an AMS reading's grams from `data.remain_pct` and `metrics` reads no private name of the ledger. `_book_on_top` now calls `is_anchor` too, so rule 2 is written once.
13. Task 3: `metrics` orders events by `(at, seq)`, not by `basis`'s deltas-first order, because neither plates nor the AMS pairing needs that tie-break. A plate event at the same instant as a scale reading and booked before it counts as between, so that pair is dropped (the safe side).
14. Task 3 (Task 3 review): For the AMS error, "a plate between them" is a `print_started` or `print_ended` event on the spool that consumed grams (a `delta_g` other than 0), because only a plate that took grams can have moved the spool away from the AMS reading. A 0 g plate (`not_applied`, `covered`) does not break the pair. A scale reading pairs only with the newest usable AMS reading; it never falls back to an older one past a plate. This replaces the first clause (any plate event broke the pair), which dropped pairs for no reason.
15. Task 3: `prints` counts every plate, failed and cancelled ones too, and `burn` counts their grams, because those grams left the spool.
16. Task 3: `days_since_last_use` reads the caller's `last_used_at` (the spool column), as the brief's signature passes it; `metrics` does not derive it from the plates.
17. Task 3: `series` sorts the points by `(at, seq)`; "the last point" is the last by time. When the kept points alone pass `limit - 1`, they thin every k-th too, so the result never passes `limit`.
18. Task 3: `Plate.printer_id` is the plate event's `printer_id`. BB usage events carry it. SB `print_ended` events carry none today, so an SB-only plate has `None`.
19. Task 3 (Task 3 review): `empty_on` returns `None` when the projected day is more than `MAX_FORECAST_DAYS` (3650) after `now`, and it checks that bound before any date arithmetic, because a tiny but non-zero burn (a 1 kg spool with one 0.1 g plate in a 365-day window) put the day past year 9999 and raised `OverflowError`, which would have failed the whole metrics read.
20. Task 3 (Task 3 review): `burn` still returns `0.0` for a window whose only plates weigh 0 g, and `None` for no plate; `empty_on` treats a burn of 0 or less as no forecast. Callers that divide by a burn must guard 0.0, because the two values both mean "no use" and a change of `burn`'s type would touch every caller.
21. Task 3 (Task 3 review): `test_equal_times_are_ordered_by_seq` guards that events with the same `at` are ordered by `seq` in `ams_pairs`, `plates` and `series`, because the code sorts by `(at, seq)` and no test showed it.
22. Task 4 (Task 3 re-review): `empty_on` returns `None` when `grams_left` or the burn is not finite (`math.isfinite`), because a NaN reached `timedelta` and raised `ValueError`, and an infinite burn gave today as the empty day.
23. Task 4 (Task 3 re-review): `MAX_FORECAST_DAYS` sits above the `_BB_OUTCOMES` comment, so each constant has its own comment again.
24. Task 4: `MetricsStore` takes `Callable[[], MetricsSettings]` (a frozen dataclass: burn window, lead time, safety margin, `orders_currency`, `filament_default_cost_per_kg`), not `StoredSettings`, because the three #6269 settings and the currency are runtime `Settings` fields that `StoredSettings` does not hold; the component reads all five with one `settings_store.read`, as `FilamentDefaults` does.
25. Task 4: `ledger.plate_events(conn, spool_ids=None)` reads the `print_started`/`print_ended` events per spool (Decision 20's index), because `spools.summary`, `SuppliersStore.stats` and `MetricsStore` all feed `metrics.plates` with the same read and `ledger` is a module all three import with no cycle.
26. Task 4: A store read takes a few queries on one connection (the spool list on its own first, then the place kinds and suppliers, the plate events, the types and the printers), not one SQL statement, because the spools come from `FilamentSpoolStore.list` with their effective values, as F13 rules.
27. Task 4: A spool's cost in its history metrics and in a stock value is its effective `cost_per_kg`, else `filament_default_cost_per_kg`, as `spools.summary` values a spool, so the Metrics tab and the Spools tiles agree.
28. Task 4: A type's burn takes the plates of all its spools, archived ones too (born at the oldest), and its `grams_left` and `spools` only the unarchived ones, because an emptied and archived spool's use is still the type's demand. A type is listed when it has a spool, unless the type is archived and none of its spools is in stock; a type whose spools are all archived has 0 g and, with use in the window, says "reorder now".
29. Task 4: `TypeForecast.low_stock` is true when every unarchived spool of the type is low (a type with none in stock is not low), because one nearly empty spool among full ones is not a low type.
30. Task 4: `days_of_stock` is `None` when the burn is `None`, 0 or less, or not finite, or when the result passes `MAX_FORECAST_DAYS`, so it agrees with `empty_on` and never divides by 0.
31. Task 4: The inventory and purchasing ranges include both `since` and `until` (UTC days); a week bucket starts on Monday, a month bucket on its first day. Both routes refuse `since > until` (a single day is allowed) and a range over 1096 days (three years, one leap day) with a 422; the purchasing route gets the same checks as the metrics route, because the same unbounded read would cost the same.
32. Task 4: A key with no value has the label "No material", "No brand", "No type", "No supplier", "No location" or "No printer"; a type's label is its `code`; a printer id with no `printers` row is labelled by its id. A spool's supplier is its invoice line's confirmed invoice's supplier.
33. Task 4: `Snapshot.burn` lists every material with an unarchived spool (0 with no use in the window) and any other material whose burn is above 0; `low_stock` and `reorder` list every material in stock (0 when none). A type's reorder counts under the effective material of one of the type's spools, unarchived ones first, else archived ones; among the candidates it takes the material with the most grams, ties by material name ascending. It falls back to the type's own material only when the type has no spool. So a reorder can add a material that is not in stock (an archived spool's material).
34. Task 4: `SuppliersStore.stats` sorts in Python after `metrics.plates` (most used first, then name, then id), and `test_stats_count_spend_and_stock` now expects 50 g, not 57 g, because the 7 g of an open estimate is a print still running, which counts 0 (the brief).
35. Task 4: The history route reads the spool and its events on two connections, as `get` and `ledger.events` do; an event booked between them can show in the timeline with the grams of the read before it, which the next read corrects.
36. Task 4: The frontend summary mock gives `consumed_g` (its `print_ended` grams), because the generated `InventorySummary` now requires it and the frontend typecheck failed without it.
37. Task 4 (R-T4-1, agent tools first): The agent coverage test may fail between tasks. Task 5 adds a real agent tool for each of the four routes, not a `NOT_A_TOOL` entry, before the branch is pushed. Why: the routes are reads an agent needs. Cost if wrong: none, because the branch is not pushed until then.
38. Task 4 (R-T4-2, settings input): `MetricsStore` takes a `MetricsSettings` callable (Decision 24). The burn and lead-time settings and `orders_currency` are runtime `Settings` fields, and one `settings_store.read` gives them all.
39. Task 4 (R-T4-3, queries per read): A store read may use several queries on one connection (Decision 26). The next read corrects any skew between them.
40. Task 4 (R-T4-4, open estimates): An open estimate counts 0 g (Decision 6), so `test_stats_count_spend_and_stock` expects 50 g.
41. Task 4 (R-T4-5, no dated test input): The metrics route test builds `since` relative to today. A fixed 2026-01-01 would fail the 1096-day bound on 2029-01-02 and turn the test red.
42. Task 4 (R-T4-6, suppliers mock): The frontend suppliers mock's `spoolUsage` uses the backend's rule now: an open estimate counts 0, and an SB+BB pair counts once. The mock must not show numbers the API never gives. The summary mock uses the same `plateGrams` helper, and the seed has one pair and one open estimate to show it.
43. Task 4 (R-T4-7, seed order): The three seed events that Decision 42's test needs are dated 2026-10-30, before every other seed event. The newest-first ledger order that the existing tests and e2e specs read does not change, and no existing expectation was edited.
44. Task 5 (R-T4-1, one tool per route): The four routes each get a `read` tool in `filament.ts`, with no `coverage.ts` entry.
45. Task 5 (history shape): `filament_spool_history` returns the route's answer without `series`, the first 50 events (the route sends newest first) and `truncated`. The description sends the reader to `filament_spool_events` for older events.
46. Task 5 (date input): `since` and `until` are `YYYY-MM-DD` strings checked by a regex. The 1096-day bound stays on the route, which answers 422; the schema does not repeat it, and the description names it.
47. Task 5 (optional query values): `dimension` and `bucket` have no schema default. The route owns the defaults (material, month), so an omitted value sends no query parameter.
48. Task 5 (R-T5-1, real dates): The `DATE` schema for `since` and `until` keeps the `YYYY-MM-DD` regex and adds a refine. The refine parses the value as a UTC date and requires `toISOString().slice(0, 10)` to equal the input. So 2026-13-45 and 2026-02-30 fail in the schema, and 2028-02-29 passes. This replaces Decision 46's regex-only check. The 1096-day bound still stays on the route.
49. Task 5 (R-T5-2, history description): The `filament_spool_history` description says that `filament_spool_events` lists every event oldest first, so the older events are at the start of its list.
50. Task 6 (material ranking): The `material` label is ranked by stock grams plus every plate's grams (and each `burn` key at 0), not by stock grams alone, because a material out of stock that was used must keep its name in `used_grams` and `burn`. Brands and location kinds are ranked by stock grams, as the brief says for brands. Null is never ranked: `label` maps it to `none` on its own, so the 20 slots go to named values and a label has at most 22 values.
51. Task 6 (folded days of stock): Rows that fold into one label set are summed, except `days_of_stock`, where `other` takes its soonest material's days. A sum of days is not a duration; the minimum keeps the dashboard's red threshold on the material that runs out first.
52. Task 6 (a try counts): `refresh_filament_metrics` records the clock before the read, whether the read then fails or not. So a database that does not answer is asked once a minute, not on every scrape, and a scrape that arrives during a read does not start a second one. The cache time is the plain attribute `Metrics.filament_snapshot_at`, which the role filter ignores (it is not a metric).
53. Task 6 (one try): The `try` is inside `refresh_filament_metrics`, which must set `metrics_up` 0 when it fails, and `metrics()` calls it with no second `try` around it. The filament component's lookup is inside that `try` too.
54. Task 6 (export order): `export` builds every family before it swaps the collector's snapshot (#6785; first, it set the gauges and then removed stale series), so a failure anywhere leaves the last snapshot whole.
55. Task 6 (route clock in tests): The API tests inject the clock through `refresh_filament_metrics.__kwdefaults__["now"]` (`monkeypatch.setitem`), so the signature stays as the brief gives it and production code has no test-only seam.
56. Task 6 (dashboard frame): The dashboard copies `scadbuddy-printing.json`'s `links`, `annotations`, `templating`, `editable`, `graphTooltip` and `schemaVersion`. Its default range is 30 days and it refreshes every 5 minutes, because the gauges change at most once a minute and the use panels read a 1-day `delta`. The waste rate's `delta` window is 1 day, the same as the use panel's. Units: `masskg`, `massg`, `d`, `percentunit`; the stock value has no unit, because the currency is its legend.
57. Task 6 (kustomize version): The lint ran with kustomize v5.8.1, the version `ci.yml`'s lint job pins (sha256 checked against the pin there) and the root CLAUDE.md names since #6621. The plan's own lines that said v5.6.0 (the header, the constraints and Task 6) now say v5.8.1. The root CLAUDE.md needs no edit. (R-T6-2)
58. R-T6-1 (dedupe before delta): The "Used per day" and "Waste rate" panels dedupe across API pods before `delta`, through a subquery: `delta((max by (material, outcome) (scadbuddy_filament_used_grams{…}))[1d:5m])`, summed by material afterwards. `max by` after `delta` shows only one pod's share of the day after each rollout.
59. R-T6-3 (async refresh): `refresh_filament_metrics` stays `async def`, with its `try` inside (Decision 53). It never blocks the event loop.
60. R-T6-4 (reserved label names): a real catalog value that equals `other` or `none` (any case) gets the label `other*` or `none*` (its own case, plus `*`). It then never merges with the fold label or the null label. A test covers it.
61. R-T6-5 (rank-20 spike): a material that crosses rank 20 makes a one-day step in "Used per day" and in `other`. This is a known limit of folding, the same as a rename (Decision 4). Task 9's as-built notes say so. No cheap query avoids it, and the totals stay right. The same holds when a used spool changes material: its past grams move to the new material, a one-day negative and positive step.
62. Task 7 (old list kept): The timeline's list keeps the name "History of this spool" and the kind words ("Added", "Weighed") of the removed `HistoryList`. So the existing SpoolPage tests pass and no expectation was edited.
63. Task 7 (filter groups): Prints are `print_started` and `print_ended`. Weighings are `weighed` and `ams_reading`. Edits are `edited`, `synced_from_bb`, `archived` and `restored`. Moves are `moved`, `loaded` and `unloaded`. Tags are `tag_bound` and `tag_unbound`. `created` and `dried` show only under All, because they fit no group.
64. Task 7 (step line): `GramsChart` draws a step line. A value holds until the next event, because the grams change only at an event (Decision 13). A sloped line would show use between plates that never happened.
65. (Replaced by Decision 82.) Task 7 (plate ticks): A tick marks each `print_ended` point only, so one plate gives one tick. An open `print_started` estimate drops the line with no tick; its row in the timeline names it.
66. Task 7 (aria plural): The chart's label says "1 anchor reading" for one anchor and "<n> anchor readings" for any other count, as `SensorChart` does. The brief's text is kept for every count but 1.
67. Task 7 (actor fallbacks): `actorText` checks `data.author.session` before `flow_run`, in the brief's order. A `service:agent` event with neither reads "Assistant". Any other actor (`service:<name>`, `anonymous`, `invoice:…`) shows as it is stored.
68. Task 7 (change values): `changeLines` shows `null`, an empty string and an empty list as "not set", a boolean as "on" or "off", and `cost_per_kg`, `diameter_mm` and `density_g_cm3` through `formatDecimal` (at least two decimals, so `20.0000` is `20.00`). An id with no name shows as the id. A field with no label is its name with spaces.
69. Task 7 (type edits): An `edited` event with `data.type_id` reads "Edited, from its type <code>" (`fromTypeText`), and "from its type" alone when the type is not in the list.
70. Task 7 (place names): `EventNames` also holds every place by its path, printer trays too (`SpoolPage` `placeNames`), so a move names both places. A place that is gone reads "a place no longer listed"; no place reads "nowhere".
71. (Replaced by Decision 79 for the y scale.) Task 7 (chart frame): `LineChart` has a zero-based y scale whose top is rounded up (`niceMax`), three grid lines, the first and last dates, and the accent ink (`text-primary`) for the single series, so it needs no legend. Marks and ticks take a name and get the attribute `data-<name>`, which keeps the chart free of grams words for Task 8. Under the pointer it shows a crosshair and the nearest point's value and time. It has no keyboard focus; the timeline under it lists every value, so it is the table view.
72. Task 7 (metrics panel): The panel shows eight values: Used, Burn rate, Empty around, Waste, Cost used, Value left, Opened (with the last use) and AMS error (with its bias and the type's error). Each empty value shows "—" and why. Empty around says why from the burn: no prints in the window, no grams used, grams left not known, or more than ten years away (`MAX_FORECAST_DAYS`).
73. Task 7 (seed): Spool #2 has a history: three readings the grams anchor on, a Bambuddy plate with archive 41, a plate of an order's run, a failed plate (42), a move, an assistant's edit and a person's reset of the cost, a type's edit, and an AMS reading paired with a scale reading (+20 g). Every event is dated from 2026-11-02 to 2026-11-13, before #1's weighing on 2026-11-14, so the ledger's newest rows and Decision 43's three events do not move. #2 also gets `last_weighed_*` and `last_used_at` to agree with its history. The summary's `consumed_g` is 140 g after R-T7-8 (one plate rule); Task 8's "Consumed" tile expects it. The seed's `orderRuns` maps a run id to its order (`ORDER_CLOSED`), as `order_item_fulfilments.print_run_id` does.
74. Task 7 (mock rules), replaced by R-T7-2 (Decision 77): The history mock reads the burn window from the mock settings (`filament_burn_window_days`), so a test can set 14. It counts a spool's age from the older of `created_at` and its first event, because the seed's spools have a `created_at` after their events. Money is cut to the cent, as `metrics._money` does. The history adds no ids of its own; its events keep their seed ids, and an event a mock route adds takes its id from `newId` (`addEvent`).
75. Task 7 (page reads): The spool page reads the history route where it read the events, after every write too. A failed read shows "The history could not be loaded" and keeps the last history it had.
76. R-T7-1 (the mock's grams series): The mock's grams series follows `metrics.series`. An anchor comes from the source and the reading, as `ledger.is_anchor` does: `scale`, `person`, `bb` or `ams_remain` with a reading, the `remain_pct` × label-weight fallback included, and not an AMS one while the weight is locked or the AMS is printing. The value after an event is the next event's `grams_before` by `seq`, and the series is sorted by `(at, seq)`. The seed's BB plates have the shape `bambuddy_usage._book` writes: `print_actual`, `delta_g` and no `grams_after`; the SB run's plate has no `grams_after` either, as `ledger.record` books it. #2's events have seqs in time order: its `dried` event moved into #2's block with `grams_before: 875`, and its AMS reading moved to 2026-11-12 21:00, 12 h before the scale reading it pairs with, so the two marks no longer overlap (M7). The SpoolPage test pins what the backend's own `metrics.series` and `spool_metrics` give for that ledger at 2026-11-18 12:00 UTC: "1000 g to 820 g, 3 anchor readings", 7 g a day and empty on 2027-03-15.
77. R-T7-2 (burn rate): #2's `created_at` is its `created` event's time, 2026-11-01 09:00. The mock counts a spool's age from `created_at`, as `metrics_store.history` does; Decision 74's rule (the older of `created_at` and the first event) is gone.
78. R-T7-3 (AMS error tile): The tile shows the type's line ("Its type: ±…") whenever `type_ams_error` is set, also beside the reason for a spool with no pair of its own. The mock's AMS pairs follow `metrics.ams_pairs`, the label-weight fallback included.
79. R-T7-4 (y scale): `LineChart`'s y scale (`yScale`) starts at zero only when the lowest value is below 40 % of the rounded-up top. Otherwise it starts at a round number below the lowest value, with two equal round steps to at or over the highest (#2: 800, 900, 1000 g). This replaces Decision 71's zero-based scale; Task 8 reuses the rule through `LineChart`.
80. R-T7-5 (colours): One colour means one thing. Readings are the accent: the chart's anchors and the timeline's weighing and AMS dots. Prints are ink (`text-foreground`): the chart's plate ticks, its legend tick and the timeline's print dots.
81. R-T7-6 (tooltip): The hover tooltip is `aria-hidden="true"`; the chart's label and the timeline carry its values.
82. R-T7-7 (plate ticks): One plate gives one tick. `plateTimes` takes the ticks from the history's events by `metrics.plates`' rule: an ended BB usage row, and an ended SB run BB did not book too. A `covered` or `not_applied` end gives none. This replaces Decision 65's rule (every `print_ended` point).
83. R-T7-8 (one plate rule): The mock has one plate rule, `metrics.plates`, in one helper (`platesOf`), per spool. The summary's `consumed_g` and the history both use it, so #4's `print` ref counts in neither. The seed's `consumed_g` is 140 g (#2's 120 g and #3's 20 g); Task 8's "Consumed" tile expects the figure this gives.
84. R-T7-9 (texts): A reading's change reads "20 g less than the last value" (or "more"). Under Burn rate the text says "Over the last N days", or "Since it was added N days ago" (R-T7-11: the age counts from `created_at`, not from when it was opened) when the spool's age from `created_at` is shorter than the window (at least 1 day, as `metrics.burn` divides). The day count rounds to the nearest day (R-T7-13).
85. R-T7-10 (concerns): The commit title stays, because the squash title decides. "1 anchor reading" stays (Decision 66). The date-dependent burn is fine for the tests, which assert only stable values or fix the time. The chart needs no keyboard focus, because the timeline lists every value. `frontend/CLAUDE.md` is Task 9's job.
86. Task 8 (setting labels): The Settings test finds the three settings by the titles `core/setting_docs.py` gives them ("Burn window, days", "Lead time, days", "Safety margin, days"), not the brief's "Burn rate window (days)", "Default lead time (days)" and "Default safety margin (days)". The section draws every field from `setting_docs.py`, Task 1 wrote these titles, and every other setting there reads "<name>, <unit>". A rename is a backend edit outside Task 8's files.
87. Task 8 (a type to reorder): The seed's orange type (`BL-PETG-HF-TRANSLUCENT-ORANGE`) has `lead_time_days` 30 and `safety_margin_days` 90, because no seed type said "Reorder now" and a new plate would change `consumed_g` (140 g, R-T7-8) and the SpoolPage pins. The backend's own `metrics.burn`, `empty_on` and `reorder_on` on the seed's plates at 2026-11-18 12:00 UTC give the orange type 7.007 g/day, empty on 2027-03-15 and a reorder day of 2026-11-15 (reorder now), and the pink type 6.234 g/day, empty on 2027-04-16, reorder on 2027-04-06 (`backend/` `uv run --frozen python <scratchpad>/t8_forecast.py`). The burn depends on today, so the tests fix the clock at that time, and Task 9's e2e must too (`page.clock.setFixedTime`).
88. (Replaced by Decision 99 for the colours and Decision 102 for the series count.) Task 8 (consumption chart): Consumption is `ColumnChart`, one stacked column per week or month from `since` to `until` (an empty bucket is a 0 column), stacked by the dimension's values. `LineChart` draws the price lines only: a sum per bucket is a bar's job. The four values that used most get the categorical colours, the rest fold into one grey "Other (n)", with a legend that names each value and its grams, the text summary as the chart's label, and a "Show the numbers" table.
89. (Replaced by Decisions 99 and 102.) Task 8 (series colours): The four colours are the dataviz palette's blue, orange, aqua and violet (`--sb-series-1..4`, with `--sb-series-other`, in `index.css` for both themes). Yellow is left out because it sits next to the amber accent, which already means "a reading" and "a price line". The set was checked with the dataviz `validate_palette.js` in both modes: every hard check passes; light aqua is under 3:1 on the surface, which the legend and the table view relieve. A value's colour follows its rank by grams used in the range, so a change of range can recolour a value.
90. Task 8 (BarList): `BarList` is an HTML list with a bar under each row, not an SVG with `role="img"`, because every value is text in it, so it is its own table view. It is used for the stock and the waste.
91. Task 8 (waste groups): The Waste section shows its three groupings (material, type, printer) side by side, so it has no choice of its own that "Remember my view" would have to keep.
92. Task 8 (purchasing range): The Prices and Spend sections read `/metrics/purchasing` with the tab's own range, so the whole tab covers one span. Spend rows stay one per supplier, month and currency, with one total per currency; nothing is converted.
93. (Decision 104 places the badge below `sm`; Decision 110 replaces that with the container's width.) Task 8 (runway): "Days of stock" draws each type's days of stock as a bar with a mark at its lead time plus safety margin; a type to reorder has its bar in the warn colour and a "Reorder now" badge with an icon. A type with no days of stock gets no bar. The rows sort "Reorder now" first, then by reorder day, then by code.
94. Task 8 (forecast view): The Spools tab's forecast view lists the forecast's spools that the tab's filters keep (so "Archived" lists none), sorted by empty day with none last by number, then every type row of the forecast.
95. Task 8 (FilamentPage test): `FilamentPage.test.tsx` now expects ten tabs with Metrics, because the brief makes Metrics available; it expected nine with no Metrics.
96. (Replaced by Decision 107 for the stock value's grams.) Task 8 (mock): The metrics routes are `mocks/features/filament/metrics.ts`. It reads the Filament mock through `metricsInput()` and the purchasing mock through `purchasingMetrics()`. The history and the forecast share one `burnOf` and one `emptyOn`, and `platesOf` now also gives each plate's printer. The labels are catalog names as they are, as in the routes (`other`, `none` are folded only in the Prometheus export). The stock value rounds half up at the cent, as `lib/decimal.ts` `divDecimal` does, where the backend rounds half to even; the seed has no exact half cent.
97. Task 8 (type fields): The type page's "Lead time (days)" and "Safety margin (days)" are in a "Reordering" section. An empty field sends `null` (the setting's default). A value above 365 is refused on the page with the bound `filament/models.py` `Days` sets.
98. Task 8 (commit title): The commit is `feat(frontend): the Filament Metrics tab (#6269)`, as the dispatch says, not the brief's title.
99. R-T8-1 (colour follows the value): A value's colour follows the value, never its rank in a range. The ranges are nested, so the Metrics tab reads `/filament/metrics` once more per dimension over 365 days and ranks the values by their grams used there; that rank sets the colour in every range (`MetricsTab` `ranked`, `Consumption` `order`). A value outside the first slots folds into "Other". This replaces Decision 89's last sentence and Decision 88's "the four values that used most".
100. R-T8-2 (money is never a float): The price tooltip shows the point's decimal string through `useMoney` ("$17.305/kg", as the headline). `LinePoint` takes an optional `text` for the hover. An axis tick is a round number that `charts/scale.ts` `tickDecimal` writes as an exact decimal string for `useMoney`; nothing calls `toFixed` on a price.
101. R-T8-3 (calendar dates): A price day is formatted in UTC: the axis through `formatTime` and the tooltip through the new `formatPointTime` (`LineChart` keeps its viewer-zone date and time as the default for the grams chart). A viewer west of UTC sees the day the price was recorded.
102. R-T8-4 (palette): The consumption chart uses the palette's first three slots (`--sb-series-1..3`) plus "Other", the palette's all-pairs cap, because a zero segment is skipped and any two slots can touch. `validate_palette.js --pairs all` passes in both modes (light on #ffffff: CVD worst ΔE 9.2, normal 24.0, aqua 2.82:1 under 3:1, relieved by the legend and table; dark on #171717: CVD 9.4, normal 20.9, all over 3:1). `ColumnChart` draws a 2 px surface gap between segments, a 4 px rounded end on each column's top segment (square at the baseline) and columns at most 24 px wide. The `--sb-series-4` token stays (R-T8-11).
103. R-T8-5 (390 px axes): `LineChart` and `ColumnChart` measure their container (`charts/useWidth.ts`, a `ResizeObserver`) and draw at its width with 11 px axis text; the left margin grows to fit the longest y label (`axisLeft`). The `width` prop is the width drawn before the first measure (and in jsdom).
104. (Replaced by Decision 110 for the badge's switch: the container's width, not `sm`.) R-T8-6 (forecast at 390 px): Below `sm`, the "Reorder now" badge sits under the type name and the "Reorder by" cell shows the reorder day; from `sm` up the badge stays in "Reorder by". Number cells use `whitespace-nowrap`. The tests that counted one "Reorder now" now count rows, because the badge has a copy for each width.
105. R-T8-7 (price steps): `LineChart` takes `until`; the x axis ends there and a step line holds its last value to it. The price lines pass the range's last day.
106. R-T8-8 (type rows): The Spools tab renders the forecast view whenever it is chosen. With no spool shown, the tab says "No spool matches these filters." and the forecast shows only "When to reorder each type".
107. R-T8-9 (mock stock value): The mock's stock value multiplies the cost by the exact grams as a decimal string (`String(grams_left)`, as `Decimal(str(grams_left))` reads them), with `lib/decimal.ts` scaled integers. A spool of 823.5 g beside #2's 820 g, both at 19.99/kg, gives 32.85, the figure the backend's own `_value` and `_money` give. The half-up rounding of Decision 96 stays.
108. R-T8-10 (Spools view): The Spools view (table, cards or forecast) sits behind a "Remember my view" switch, the Metrics and Invoices tabs' mechanism: with it on, a visit with no query opens the last view. The browser keeps it under `scadbuddy.filament.spools.view` (`spoolsQuery.ts`); storage that throws leaves the tab working.
109. R-T8-11 (concerns): The report's other concerns stand: setting labels come from `setting_docs`; the orange type's lead time and safety margin are in the seed; Task 9's e2e fixes the clock at 2026-11-18T12:00Z; FilamentPage has ten tabs; the series tokens are kept.
110. R-T8-12 (forecast badge, first paint, toolbar): The forecast table's "Reorder now" badge sits under the type name whenever the table's container is narrower than the table's 44 rem, by a container query (`@container` on the scroll wrapper, `@max-[44rem]`), not at the viewport's `sm`. This replaces Decision 104's "below `sm`": at 640 to about 750 px the table scrolls, and the badge in "Reorder by" was out of view. The Consumption chart draws as soon as the range request returns; until the 365-day rank request returns it uses the range's own rank (`ranked(metrics)`), then the 365-day rank sets the colours (Decision 99). The Spools toolbar's button group wraps (`flex-wrap`), so "Export CSV" is not cut off at 390 px. The e2e checks the badge at 390, 680 and 1600 px against whether the container scrolls.

## Pre-flight rulings (2026-11-18)

The pre-flight scan found F1-F29. The controller took each proposed ruling. The task text
above carries each one.

1. F1: The author goes in `data.author`, because `capture.py` already writes `data.by` as a string; `actorText` reads `data.author`. Changed: Decision 10, Tasks 2, 7.
2. F2: The `material`, `brand` and `location_kind` labels are catalog `name`s (there is no code column); the as-built notes say a rename starts a new series. Changed: Decision 4, Tasks 4, 6, 9.
3. F3: `SpoolTypeStore.update`, `set_presets` and `set_k_values` take `actor`, and `patch_type`, `put_type_presets` and `put_type_k_values` pass `actor(caller)`; `api/filament.py` is in Task 2's Files. Changed: Task 2.
4. F4: The confirm's `invoice_line_id` change goes into the one `edited` event `update_on` writes, through a new `invoice_line_id` argument and the same UPDATE. Changed: Decision 12, Task 2.
5. F5: `_refresh_spools` goes, the fan-out event does the refresh, and Task 2's label-weight test asserts only the event. Changed: Decision 11, Task 2.
6. F6: Only `types.OVERRIDABLE` fields, presets and K values fan out to spools. Changed: Decision 11, Task 2.
7. F7: `provenance.current_by` derives the author from `orders/actor.py` `current_actor()` (plus `tool` from `current_author()`), with the reason in a docstring. Changed: Decision 10, Task 2.
8. F8: `ledger.pairs` and `ledger.is_anchor` are extracted from `basis`, and both `basis` and `metrics` use them. Changed: Task 3.
9. F9: A reading is a chart anchor when `basis` would accept it at its own time. Changed: Decision 13, Task 3.
10. F10: Thinning keeps the manual, scale and BB anchors, the plates and the last point, thins AMS anchors with the rest, and caps at `limit`. Changed: Decision 13, Task 3.
11. F11: A new spool's history has one `created` row and `series == []`, and `series` skips a point whose value is None. Changed: Decision 13, Review Focus 2, Tasks 3, 4.
12. F12: `TimelineRow.order_id` is `str | None`. Changed: Decision 17, Task 4.
13. F13: `MetricsStore` gets the `FilamentSpoolStore`, calls `list(include_archived=True)`, reads `Spool.hours_outside_dry`, and `summary` computes `consumed_g` with the pure `metrics.plates`. Changed: Task 4.
14. F14: Purchasing reuses `invoice_money.price_per_kg`, and a `PriceSeries` is keyed by (type, supplier, currency). Changed: Task 4.
15. F15: `SupplierStats.consumed_g` is computed with `metrics.plates`; no issue is filed. Changed: Decision 18, Task 4.
16. F16: The burn of a type or a material is the burn of the union of its spools' plates, born at the oldest spool's `created_at`. Changed: Decision 8, Tasks 3, 4.
17. F17: `fold` ranks one label's values and returns the kept set, `label` maps each row, and one ranking per label serves both stock gauges. Changed: Task 6.
18. F18: Panel 2 uses `sum by (currency) (max by (material, currency) …)`. Changed: Task 6.
19. F19: `refresh_filament_metrics` takes an injected clock, and the worker-role test is a guard that passes before Task 6. Changed: Task 6.
20. F20: Task 1 only names the two fields in `filament_type_upsert`'s description, with no schema field and no failing agent test. Changed: Task 1.
21. F21: The `seed.ts`, `mocks/fixtures.ts` and `pages/settings/fields.ts` edits go in Task 1, so the frontend typecheck holds after every task. Changed: Tasks 1, 8.
22. F22: Task 8 adds `/filament/metrics` to `PAGES` in `e2e/page-titles.spec.ts`. Changed: Task 8.
23. F23: Task 7 builds the id-to-name lookup `EventNames` in `eventText.ts` from the catalog queries and the types list. Changed: Task 7.
24. F24: `SpoolHistory` has `window_days`, and the UI text reads it. Changed: Tasks 4, 7.
25. F25: The Spools forecast view shows the type rows with "Reorder now" too, with a Task 8 test. Changed: Tasks 8, 9.
26. F26: Only Task 6 edits `deploy/CLAUDE.md`. Changed: Tasks 6, 9.
27. F27: Task 7 creates `charts/LineChart.tsx`, `GramsChart` wraps it, and Task 8 reuses it. Changed: Tasks 7, 8.
28. F28: The history route returns events newest first by `(at, seq)`, as `ledger.page` does. Changed: Decision 17, Tasks 4, 5, 7.
29. F29: The metrics models are plain `BaseModel`, as `InventorySummary` is. Changed: Task 3.
30. R-T9-2 (invoice link in the ledger): The `edited` event of an invoice confirm records the invoice link, `invoice_line_id: [None, id]`, alongside the cost change (Decision 12), because the history view uses it to name the invoice. `test_a_line_fills_a_costed_spool_and_replaces_its_cost` (#6700) expects both keys.
31. R-T9-3 (a profile fetch's import fans out): The import's `finish()` writes the type's presets and K values through `types.list_edit`, the helper `set_presets` and `set_k_values` use. It reads the list's rows before and after the write, in the import's transaction, and when they differ it fans out `{"presets": [before, after]}` or `{"k_values": [before, after]}` as the run's `started_by` (Decision 11). An import that changes nothing writes no event. The spec sentence "A type edit fans out" stays as written. Why: a fetch is the most common way a type's presets change, and an inheriting spool's history must show it. Cost if wrong: two more `edited` events per inheriting spool per import that changes the lists.
32. R-T9-4 (a profile fill fans out, after #6714 merged): A fill from a slicer profile is a type edit. `TypeFiller.fill`, `apply` and `fill_quietly` take an `actor`. The routes pass `actor(caller)`, and a profile fetch's import passes the run's `started_by`. `fill_empty` fans out the fields it writes (`{field: [None, value]}`), and `fill_k_values` writes through `list_edit`, as `set_k_values` does. The fill still runs after the preset write commits, as #6714 has it, so a type's first presets write their own `edited` event, then the fill writes another. Why: the spec's "A type edit fans out" covers every path that changes a type. Cost if wrong: one more `edited` event per inheriting spool when a type is filled.
