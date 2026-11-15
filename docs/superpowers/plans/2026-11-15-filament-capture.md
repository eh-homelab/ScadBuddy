# Filament capture (#6263) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ScadBuddy's spool ledger learns what happens to a spool when it happens: a
tray change, an AMS reading, a plate's end and every print Bambuddy deducted, each
counted once.

**Architecture:** Capture runs in the CRUD service (the API process), inside the
transaction that writes each LAN report (`printers/reports.py` `advance_in`, reached by
the print worker's watch through `PUT /api/v1/internal/crud/print/reports/{id}`), so
no worker gets a new route or pool. The report that shows a plate end books that
plate's `print_ended` at the report's time. The report whose trays changed books
`loaded`, `unloaded`, `moved` and `ams_reading` events. The scheduled BB pull also books
BB's per-spool usage history as `print_ended` events. The ledger pairs a BB usage row
with SB's own record of the same plate, so one plate is counted once whichever clock is
ahead. `refresh_derived` gets a fast path for a reading booked on top of a spool's
history. `SpoolRefresh` gains a backstop step that reconciles trays against the ledger.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3 (sync, `dict_row`), pydantic v2,
Temporal Python SDK, Postgres 18, Bambuddy 1.2.6b1 REST API.

**Spec:** `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§5.5 and
its "Known limits of the time rule", §6, §8.2, §12 row #6263). Phase 1 plan and its
binding rulings: `docs/superpowers/plans/2026-11-14-filament-data-model.md`, "Decisions
made while building (#6261)". Epic #6260.

## What a read-only probe of the live Bambuddy showed (2026-11-15)

Measured from this WSL host on 2026-11-15 at about 05:50 UTC against
`https://bambuddy.internal.nullreference.io` (Bambuddy 1.2.6b1), with
`GET /api/v1/inventory/usage?limit=60`, `GET /api/v1/archives/?limit=40` and
`GET /api/v1/inventory/spools`, read-only. Task 1 repeats it over a larger sample and
writes the result into the spec. The design below rests on these facts:

- BB keeps one usage row per spool per print (`SpoolUsageHistoryResponse`: `id`,
  `spool_id`, `printer_id`, `print_name`, `weight_used`, `percent_used`, `status`,
  `cost`, `created_at`). BB has rows for SB's own LAN prints too. Their `print_name` is
  SB's `subtask_name` (`scadbuddy-ac13ac3c-p1`, `scadbuddy-9b69b0b9-p1`).
- A usage row's `created_at` equals its spool's `updated_at` to the microsecond (usage
  457 and spool 31: `2026-11-13T08:24:44.614397`; usage 465 and spool 103). So BB
  deducts in the same write that moves `updated_at`.
- BB deducts as the print ends. The archive's `completed_at` is 0.4 s to 4.3 s after
  the usage rows' `created_at` (7 pairs: archives 248, 249, 250, 254, 255, 256, 264).
- A failed print has usage rows with partial grams (usage 460: 23.4 g, `failed`). SB's
  settle books 0 g for a plate that did not end `done`.
- An archive's `print_name` can differ from the usage rows' (`Unicorn pencil holder`
  against `Unicorn_pencil_holder`). Match a usage row to its archive by printer and time,
  never by name.

## Global Constraints

- Database rule (CLAUDE.md "Database access (#5295)"): only the CRUD service has SQL
  access. Workers reach data only through `crud_router(...)` routes with a service
  credential. The print worker holds no Postgres pool. This plan adds no new route: capture
  runs inside the existing report write on the API, and the pull and the backstop run on
  the `library` worker in the API process. Never add a pool, a grant or a cross-service
  table read.
- Migrations (#491): one new file
  `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a
  merged file. A migration may depend only on files already on main.
- Temporal determinism: a change to the commands `SpoolRefresh.run` issues goes behind
  `workflow.patched(...)`, with a history recorded on the changed code beside the old ones
  in `backend/tests/fixtures/spool_refresh_histories/`. Never re-record an existing
  history. Activity bodies may change freely.
- Generated API files (`backend/openapi.json`, both `schema.d.ts`) are never committed.
- Every new public `/api/v1` route needs `summary=`, `description=` and an agent tool or
  a `NOT_A_TOOL` entry (`agent/src/tools/coverage.ts`). This plan adds none. If a task
  finds it needs one, it follows that rule.
- Every external fact gets a link; every measured fact says where and how it was
  measured (#259, spec §3).
- WSL clock oscillation (memory `wsl-clock-oscillation`): the host's wall clock can jump
  ±14 s every few seconds. Before you chase a local Temporal timing failure, or record a
  replay history, compare `time.time()` with `time.monotonic()` for 20 s. Tests take
  times as arguments (`at=`, `seen_at=`), never the wall clock. The ledger's
  clock-step guard (`ledger.record`, an event with no `at`) stays.
- No task prints a secret. A secret goes into a shell variable or an environment
  variable only. No command, log, test output, commit or PR shows its value; print its
  length at most.
- Capture never fails a report write. Every capture call in `advance_in` runs in a
  savepoint (`with conn.transaction():`). A failure is logged with
  `FILAMENT_CAPTURE_FAILED` and the error's type, never its message, and the write goes on.
- Capture locks spools in id order and never takes a tag lock. The report row's lock
  (`printer_reports ... FOR UPDATE`) is already held.
- Commands: `cd backend && uv run --frozen pytest <files>` with
  `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test`;
  `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`.
- Out of scope: per-type sync settings and SB→BB push (#6265). The BB usage booking runs
  inside `read_and_pull`, so #6265's gate on the pull gates it too. Do not read sync
  settings here. The AMS weight sync button (#6268) is out of scope too.

## Decisions this plan makes

- **The plate's end time is the report that shows the end.** `advance_in` already finds
  the end (`advance_print` → `PrintEnd`). In the same transaction it books
  `print_ended` for that plate's open estimates, at the report's `seen_at`. The
  follow's settle (`settle_uses_in`) stays as the backstop and books what is still open at
  its own clock time. This replaces #6261's plate-start rule.
- **BB weighings are booked with the source `scale`** (item 5): spec §5.5 says "a scale
  weighing always counts, a BB one included". The provenance stays in `ref_kind =
  'bb_scale'` and `actor = 'bambuddy'`. The migration rewrites the old rows. No spool's
  `grams_left` changes: before #6263 no `ams_remain` event exists, and `scale` and `bb`
  are both absolute readings.
- **Prints SB did not start are booked from BB's usage history, not the archive.** An
  archive has only total grams. The print watcher imports an archive while the print
  still runs, before any actual exists. BB's usage rows are per spool and are what BB
  deducted. Each row gets the BB archive id that matches by printer and time (§8.2).
- **Prints sent through BB's queue get no SB `print_started` estimate** (owner's ruling,
  2026-11-15). Their grams come only from BB's usage rows (Task 7). Only SB's native LAN
  runs write `print_started`.
- **One plate, two records, counted once.** SB's `print_run` events and BB's `bb_usage`
  event are paired when they have the same spool and job (`subtask_name` =
  `print_name`). BB's grams count (owner's ruling, 2026-11-15), because BB keeps a failed
  plate's partial grams, and SB books 0 g for one.
  To decide whether a pair lies after the anchor, the ledger compares a BB-clock anchor
  with BB's time and every other anchor with SB's time. So clock skew between BB and SB
  never counts a plate twice.
- **AMS readings taken while the printer prints are recorded, never anchors.** This
  replaces the limit "a reading taken in the middle of a plate hides the rest of that
  plate".
- **At one instant, deltas come before readings.** A reading at the moment a plate ends
  holds that plate. BB's usage row and the spool's `updated_at` carry the same time.
- **A loaded spool's location is its AMS tray** (spec §5.6). An unload moves it back to
  where it was before that load (the `from` of the `moved` event that put it in the tray),
  else to no location.

## Review Focus

1. A partial report (a delta before a reconnect's `pushall`, with no `ams` key) must not
   unload every spool. A tray of an AMS unit that the report does not show is left alone
   (Task 6).
2. A tray's RFID keys vanish for one report while the AMS rescans, and its material and
   colour stay the same. The spool stays loaded, with no `unloaded`/`loaded` pair (Task 6).
3. A capture that raises (a bad tray row, a missing AMS location kind) never fails or
   rolls back the report write, and the plate end is still answered to the watch (Tasks 5, 6).
4. A report resent after a lost answer (same `seen_at`, or `stale`) books no second
   event (Task 6).
5. BB's clock is ahead of SB's, or behind it, by more than a plate. A paired plate is
   still counted once against a BB reading and once against an AMS reading (Task 2).

---

## File structure

```
backend/scadbuddy/migrations/<ts>_filament_capture.sql     Task 4
backend/scadbuddy/filament/
  ledger.py          pairing, tie order, printing readings     Task 2
                     fast path of refresh_derived              Task 3
  prints.py          plate ends: open estimates, book an end   Task 5  (new)
  capture.py         trays → loaded/unloaded/ams_reading       Task 6  (new), 8
  bambuddy_usage.py  BB usage rows → print_ended                Task 7  (new)
  bambuddy_pull.py   weighings as scale; usage in read_and_pull Tasks 4, 7
  locations.py       conn variants of ensure_ams and move       Task 6
backend/scadbuddy/spools/native.py       record_uses job, settle through prints.py  Task 5
backend/scadbuddy/spools/models.py       SeenTray.remain                             Task 6
backend/scadbuddy/bambuddy/trays.py      seen_trays fills remain                     Task 6
backend/scadbuddy/bambuddy/models.py     SpoolUsage                                  Task 7
backend/scadbuddy/bambuddy/client.py     spool_usage()                               Task 7
backend/scadbuddy/printers/reports.py    advance_in calls capture                    Tasks 5, 6
backend/scadbuddy/printers/store.py      PrinterStore.read_on                        Task 6
backend/scadbuddy/spools/activities.py   capture_backstop_activity                   Task 8
backend/scadbuddy/workflows/spool_refresh.py  CAPTURE step behind its patch          Task 8
backend/scadbuddy/main.py                register the backstop activity              Task 8
docs/superpowers/specs/2026-11-14-filament-management-design.md  Tasks 1, 9
backend/CLAUDE.md                        filament layout line                        Task 9
```

---

### Task 1: Measure BB's deduction timing and the clock skew (read-only)

**Files:**
- Modify: `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§5.5 and §8.2)
- Scratch only (never committed): a script in the session scratchpad that reads the
  saved JSON. Keep the data and the script in separate directories, and run Python with
  `-I`.

**Interfaces:**
- Produces: measured facts that Tasks 2, 5 and 7 rely on. If a fact below does not hold,
  stop and report to the owner before you start Task 2.

Strictly read-only: only `GET` requests to Bambuddy, no print, no write.

- [ ] **Step 1: Read the API key without printing it.** The key is the
  `SCADBUDDY_BAMBUDDY_API_KEY` field of item `nxnqxhmmnzeyg2r23mxqhdmanq` in vault
  `fke5nrl36m3y5luczv3wy5vb4q` ("homelab - prod", `scadbuddy-bambuddy-api-key`; named in
  eh-homelab/clusters `clusters/prod/scadbuddy/onepassworditem-bambuddy-api-key.yaml`).
  Under WSL, `op.exe read` writes the value to **stderr**. This is the only allowed
  form:
  `KEY=$(op.exe read "op://homelab - prod/scadbuddy-bambuddy-api-key/SCADBUDDY_BAMBUDDY_API_KEY" 2>&1)`.
  Then print only `${#KEY}`. Never echo `$KEY`, and never send it to the terminal, a
  file or a log. Pass it only as the `X-API-Key` header.
- [ ] **Step 2: Read the usage, archives and spools.** Save
  `GET /api/v1/inventory/usage?limit=500`, `GET /api/v1/archives/?limit=200` and
  `GET /api/v1/inventory/spools?include_archived=true` with the header `X-API-Key`.
  For SB's runs, read the last 50 native runs' `result.lan[].subtask_name` through
  SB's read API at `https://scadbuddy.internal.nullreference.io` (find the runs route in
  `backend/scadbuddy/api/` first), or through the `scadbuddy-run-forensics` skill.
- [ ] **Step 3: Compute and keep the numbers:**
  - (a) for every usage row: the matching archive (same BB printer, `completed_at` in
    `[created_at, created_at + 10 s]`), and the distribution of `completed_at −
    created_at` (min, median, max, n);
  - (b) for every spool whose newest usage row is newer than its last edit: whether
    `updated_at == created_at`;
  - (c) for every SB LAN plate of the last 50 runs: whether a usage row's `print_name`
    equals its `subtask_name`, with one usage row per spool per plate;
  - (d) failed and cancelled prints: whether their rows carry partial grams.
- [ ] **Step 4: Measure the clock skew between BB and SB from server clocks, not the WSL
  host.** Send 120 alternating requests, 0.25 s apart: `GET /api/v1/system/storage-usage`
  on BB and `GET https://scadbuddy.internal.nullreference.io/healthz` on SB.
  Record each response's `Date` header. Estimate the offset from where each server's
  second boundary falls in the sample order (resolution about 0.25 s). Also report SB's
  pod clock against BB's pod clock with `kubectl exec ... -- date -u +%s.%N`, read-only,
  if the context allows `exec`. Never touch the pods otherwise.
- [ ] **Step 5: Write the results into the spec.** In §8.2, after "Consumption is counted
  once", write a dated paragraph with each fact from (a) to (d) and the skew. Give the
  date, the URLs, the endpoints, n, the method of each, and the 2026-11-15 probe above.
  In §5.5, replace the limit "The rule assumes BB deducts `weight_used` when a print
  ends … not yet measured" with the measured statement.
- [ ] **Step 6: Commit** `docs(filament): measure when Bambuddy deducts a print and its clock skew (#6263)`.

### Task 2: The ledger counts one plate once

**Files:**
- Modify: `backend/scadbuddy/filament/ledger.py` (module docstring, `grams_left`, new helpers)
- Test: `backend/tests/filament/test_ledger.py`

**Interfaces:**
- Produces in `ledger.py`:
  - `PRINT_RUN = "print_run"`, `BB_USAGE = "bb_usage"` (the `ref_kind`s of SB's plates and of BB's usage rows);
  - `BB_REFS = frozenset({"bb_spool", "bb_scale", "bb_dried", BB_USAGE})`, the events that carry BB's clock;
  - `job_of(event: m.SpoolEvent) -> str | None`: `data["job"]` when it is a string, else, for `ref_kind == PRINT_RUN`, the middle part of `ref_id` (`run/subtask/slot` → `subtask`; rows from before #6263), else `None`;
  - `grams_left(events, *, weight_locked, label_weight_g) -> float | None` (same signature; the rules below).
- Consumes: nothing new.

The rules that `grams_left` adds to #6261's (spec §5.5):
1. Order is `(at, 0 if delta else 1, given order)`. At one instant every delta comes
   before every reading, and events of one kind keep the order they were given in.
2. An `ams_remain` reading with `data["printing"] is True` is never an anchor.
3. A job is paired on this spool when a `print_ended` event with `ref_kind == BB_USAGE`
   and a `print_started` or `print_ended` with `ref_kind == PRINT_RUN` share
   `job_of(...)`. For a paired job, every `PRINT_RUN` event is skipped. The `BB_USAGE`
   event's `delta_g` counts once, when the pair lies after the anchor. The pair lies after
   the anchor when the `BB_USAGE` event's position is after the anchor's, for an anchor
   whose `ref_kind` is in `BB_REFS`. For any other anchor, it lies after when the newest
   `PRINT_RUN` `print_ended` of the job is after the anchor, else its `print_started` is.
4. Everything else stays as in #6261.

- [ ] **Step 1: Write the failing tests** (build events with the file's existing helper; times are fixed `datetime`s):
  - `test_a_reading_at_a_plate_end_holds_that_plate`: scale 800 at t0; `print_ended` print_actual −50 at t1; `ams_remain` 70 % of 1000 at t1, given *before* the end in the list → `grams_left == 700`.
  - `test_an_ams_reading_while_printing_is_no_anchor`: scale 800 at t0; `print_started` estimate −40 (job J) at t1; `ams_remain` 74 % with `data.printing=True` at t2 → `760`; the same reading without `printing` → `740`.
  - `test_bb_usage_and_sb_end_of_one_plate_count_once`: scale 800 at t0; SB `print_started` −40 and `print_ended` −38 (ref `r/J/0`) at t1 and t3; `bb_usage` `print_ended` −41 with `data.job="J"` at t2 → `759`.
  - `test_a_bb_reading_holds_its_own_usage_row` (Review Focus 5, BB ahead): SB `print_ended` −38 (job J) at t5; `bb_usage` −41 (job J) at t10; `synced_from_bb` reading 500 (`ref_kind="bb_spool"`) at t10, given after the usage → `500`. With the BB pair at t2 and t2 (BB behind) → `500`.
  - `test_an_ams_reading_after_the_sb_end_holds_a_pair_whose_bb_time_is_later` (Review Focus 5): SB `print_ended` −38 (J) at t5; `ams_remain` 50 % of 1000 at t6; `bb_usage` −41 (J) at t9 → `500`.
  - `test_a_failed_plate_takes_bbs_partial_grams`: SB `print_ended` delta 0 `basis=not_applied` (J); `bb_usage` −23.4 (J) after an anchor of 600 → `576.6`.
  - `test_one_spool_in_two_slots_is_one_bb_row`: SB ends −10 (`r/J/0`) and −15 (`r/J/1`), `bb_usage` −26 (J), anchor 300 before → `274`.
  - `test_a_job_from_an_old_ref_id_pairs`: a `PRINT_RUN` `print_ended` with no `data.job` and `ref_id="run1/J/2"` → `job_of(...) == "J"`, and it pairs with a `bb_usage` of job J.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/filament/test_ledger.py` → the new tests FAIL.
- [ ] **Step 3: Implement** the helpers and the rules in `ledger.py`. Rewrite the module
  docstring's ordering paragraph: drop "a print's `print_ended` at its plate's start",
  and describe rules 1 to 3 and why (BB's usage row and `updated_at` share one instant;
  each anchor is compared in its own clock).
- [ ] **Step 4: Run** the ledger tests and `tests/filament tests/spools` → PASS. Fix any older test that asserted the plate-start rule, and say so in the commit.
- [ ] **Step 5: Commit** `feat(filament): count a plate once across SB and Bambuddy (#6263)`.

### Task 3: A reading on top of the history does not re-read it

**Files:**
- Modify: `backend/scadbuddy/filament/ledger.py` (`record`, `refresh_derived`)
- Test: `backend/tests/filament/test_ledger.py`

**Interfaces:**
- Produces: `refresh_derived(conn: Conn, spool_id: UUID, event: m.SpoolEvent | None = None) -> None`. `record` passes the event it inserted. Every other caller passes none and gets a full recompute.
- Fast path, taken only when `event` is given and all of these hold:
  - its `source` is not in `_DELTA`, and its `kind` is not `print_started` or `print_ended`;
  - no other event of the spool has an `at` later than `event.at`. Check this with one
    query, `SELECT max(at) ... WHERE spool_id = %s AND id <> %s`, which the
    `(spool_id, at)` index serves.

  On the fast path, an absolute reading that qualifies as an anchor sets `grams_left` to
  that reading. An `ams_remain` reading qualifies when the spool is not
  `weight_locked` and the reading is not `printing`. A reading that does not qualify
  leaves `grams_left` as it is. A `weighed` or `scale` reading also sets
  `last_weighed_*`, and a `dried` event sets `last_dried_*`. Any other kind updates
  nothing. Every other case takes the full path.

- [ ] **Step 1: Write the failing tests:**
  - `test_an_ams_reading_on_top_does_not_read_the_history(pg_pool, monkeypatch)`: make a spool with a scale reading. Monkeypatch `ledger.events` with a counting wrapper. `record(..., "ams_reading", source="ams_remain", data={"remain_pct": 60})` at a newer `at` → the counter stays 0, and `grams_left` is 60 % of the label weight.
  - `test_the_fast_path_matches_a_full_recompute(pg_pool)`: book in order scale 900; ams 85 %; `print_started` −40; ams 80 % `printing=True`; `print_ended` −38; ams 79 %; a `loaded` event; scale 760 with an `at` older than the newest; ams 75 % with `weight_locked` set between. After each `record`, compare `grams_left`, `last_weighed_g` and `last_weighed_at` with `grams_left(events(conn, id), ...)` and the full `refresh_derived` → equal.
  - `test_an_event_with_an_older_time_takes_the_full_path`: monkeypatch counter; a BB reading whose `at` is older than the spool's newest event → counter is 1.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Replace the docstring note "#6263's AMS readings, one per tick, will need an incremental path" with the fast path's rule.
- [ ] **Step 4: Run** `tests/filament` → PASS.
- [ ] **Step 5: Commit** `perf(filament): book a reading without re-reading the spool's history (#6263)`.

### Task 4: Migration; BB weighings are scale readings

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_capture.sql`
- Modify: `backend/scadbuddy/filament/bambuddy_pull.py` (`_book`: `source="scale"`; module docstring step 1 and `_book` docstring)
- Test: `backend/tests/filament/test_migrations.py`, `backend/tests/filament/test_bambuddy_pull.py`

**Interfaces:**
- Produces: the index `filament_spool_events_job ON filament_spool_events ((data ->> 'job')) WHERE kind = 'print_started' AND ref_kind = 'print_run'` (Task 5 finds a plate's open estimates by it). The file also holds `UPDATE filament_spool_events SET source = 'scale' WHERE kind = 'weighed' AND ref_kind = 'bb_scale' AND source = 'bb'`. Explain in a header comment why no `grams_left` changes (see "Decisions this plan makes").

- [ ] **Step 1: Write the failing tests:**
  - `test_capture_migration_makes_bb_weighings_scale` (the `MIGRATIONS[:idx]` pattern of `tests/test_pg_migrations.py`): migrate to before the file; insert a spool and a `weighed`/`bb` event with `ref_kind='bb_scale'`; migrate → its source is `scale`; a `person` weighing is untouched; `pg_indexes` lists `filament_spool_events_job`.
  - `test_an_ams_reading_does_not_take_the_anchor_from_a_bb_weighing` (in `test_bambuddy_pull.py`): pull a BB spool whose `last_scale_weight` is 1250 with core 250 → a `weighed` event with `source == "scale"`, grams 1000. Then `ledger.record` an `ams_remain` 50 % of 1000 at a later `at` → `grams_left == 1000`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Write the migration; change `_book`.**
- [ ] **Step 4: Run** `tests/filament tests/test_pg_migrations.py` → PASS.
- [ ] **Step 5: Commit** `feat(filament): a Bambuddy weighing is a scale reading (#6263)`.

### Task 5: A plate's end is booked at the report that shows it

**Files:**
- Create: `backend/scadbuddy/filament/prints.py`
- Modify: `backend/scadbuddy/spools/native.py` (`record_uses` writes `data={"job": subtask_name}`; `settle_uses_in` uses `prints`; the docstrings of the module, `settle_uses` and `settle_uses_in` drop the plate-start rule)
- Modify: `backend/scadbuddy/printers/reports.py` (`advance_in`, and the module docstring)
- Test: `backend/tests/filament/test_prints.py` (new), `backend/tests/spools/test_native_uses.py`, `backend/tests/filament/test_native_adapter.py`

**Interfaces:**
- Produces in `filament/prints.py`:
  - `@dataclass(frozen=True) OpenUse(spool_id: UUID, ref_id: str, delta_g: float | None, job: str)`.
  - `open_for_run(conn, run_id: str) -> list[OpenUse]`: the existing lookup of `settle_uses_in`, moved here.
  - `open_for_job(conn, job: str) -> list[OpenUse]`: by `data ->> 'job'` (Task 4's index). It also checks that no `print_ended` with the same `ref_id` exists.
  - `book_end(conn, use: OpenUse, *, applied: bool, at: datetime | None, actor: str, ended: str) -> m.SpoolEvent | None`: `print_ended`, `print_actual`, `delta_g = use.delta_g if applied else 0.0`, `ref_kind="print_run"`, `data={"basis": "sliced" | "not_applied", "job": use.job, "ended": ended}`. `ended` is `"report"` or `"settle"`.
  - `plate_ended(conn, subtask_name: str, state: str | None, at: datetime) -> set[UUID]`: books every `open_for_job(subtask_name)` with `applied = state == "FINISH"`, `at=at`, `actor="system:printer-watch"`, `ended="report"`. Returns the spools it booked on.
  - `FILAMENT_CAPTURE_FAILED = "filament capture failed; the report is written"` (log message; Task 6 uses it too).
- `settle_uses_in` keeps its signature and return value. It books each open use with `at=None` (the clock, by the ledger's guard) and `ended="settle"`.
- `advance_in`: after a write that answers `ended`, it calls
  `plate_ended(conn, ended.subtask_name, report.get("gcode_state") if ended.timed else None, seen_at)`
  inside `with conn.transaction():`. An exception is caught, logged with
  `FILAMENT_CAPTURE_FAILED` (`extra`: `printer_id`, `what="plate_end"`, the error's type),
  and dropped. The answer (`Advance`) is unchanged. `PrintEnd` and `EndedPrint` get no
  new field, so an older print worker still reads the answer.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_plate_ends_at_the_report_that_shows_it(pg_pool)`: a native spool; `record_uses("r1", "scadbuddy-r1-p1", [use 40 g])`; `PrinterReportStore.advance_in` with a running report, then one with `gcode_state="FINISH"` at `seen_at=T` → one `print_ended` event, `at == T`, `delta_g == -40`, `data.job == "scadbuddy-r1-p1"`, `data.ended == "report"`.
  - `test_a_failed_plate_books_no_grams`: as above with `FAILED` → `delta_g == 0`, `basis == "not_applied"`.
  - `test_a_moved_on_end_books_no_grams`: the next report names another `subtask_name` → `delta_g == 0`.
  - `test_the_settle_books_only_what_the_report_did_not` (in `test_native_uses.py`): the report books the end; then `settle_uses("r1", ["done"])` → no second event, and the return value is empty. Without the report end, the settle books at the clock time with `data.ended == "settle"`.
  - `test_a_plate_end_capture_that_raises_still_writes_the_report` (Review Focus 3): monkeypatch `prints.plate_ended` to raise → the report row holds the new report, `Advance.ended` is returned, and the log has `FILAMENT_CAPTURE_FAILED`.
  - Update the old tests that asserted `at == print_started.at` for a settle.
- [ ] **Step 2: Run** `tests/filament/test_prints.py tests/spools/test_native_uses.py tests/filament/test_native_adapter.py tests/printers/test_watch.py` → the new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** them, `tests/api/test_crud_print*.py` and `tests/crud` → PASS.
- [ ] **Step 5: Commit** `feat(filament): book a plate's end when the printer reports it (#6263)`.

### Task 6: Tray changes write loaded, unloaded and AMS readings at once

**Files:**
- Create: `backend/scadbuddy/filament/capture.py`
- Modify: `backend/scadbuddy/spools/models.py` (`SeenTray.remain: int | None = None`), `backend/scadbuddy/bambuddy/trays.py` (`seen_trays`: `remain = tray.remain if tray.remain >= 0 else None`)
- Modify: `backend/scadbuddy/printers/store.py` (`PrinterStore.read_on`), `backend/scadbuddy/filament/locations.py` (`ensure_ams_on`, `move_on`; the pool methods call them), `backend/scadbuddy/printers/reports.py` (`advance_in` reads the old `report` and calls capture)
- Test: `backend/tests/filament/test_capture.py` (new), `backend/tests/filament/test_locations.py`

**Interfaces:**
- Consumes: `ledger.record`; `seen_trays(status_from_report(row, LanReport(...), now=seen_at))`; `prints.FILAMENT_CAPTURE_FAILED`.
- Produces in `printers/store.py`: `@staticmethod read_on(conn, printer_id: str) -> PrinterRow | None`.
- Produces in `locations.py`: `@staticmethod ensure_ams_on(conn, printer_id: UUID, ams_id: int, tray_id: int, *, label: str) -> m.Location` and `@staticmethod move_on(conn, spool_id: UUID, location_id: UUID | None, *, actor: str) -> m.SpoolEvent | None`, with the same rules as the pool methods.
- Produces in `capture.py`:
  - `TRAY_KEYS = ("ams", "vt_tray", "vir_slot")`; `trays_changed(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> bool` (any of those keys differs);
  - `WATCH_ACTOR = "system:printer-watch"`, `BACKSTOP_ACTOR = "system:capture-backstop"`;
  - `@dataclass CaptureResult(loaded: int = 0, unloaded: int = 0, readings: int = 0)`, with `__add__`;
  - `occupant(conn, printer_id: str, tray: SeenTray, current: Sequence[UUID]) -> tuple[UUID, str] | None`, in this order:
    1. the tray's `tray_uuid`, then its `tag_uid`, through an open binding on an unarchived spool → `"rfid"`;
    2. with no keys: a `tray_spool_links` row of this tray whose fingerprint equals the tray's and whose `physical_spool_id` is an unarchived `filament_spools` id → `"link"`;
    3. with no keys: a spool in `current` whose effective material and RGB make the tray's `tray_fingerprint` → `"fingerprint"` (Review Focus 2);
    4. else `None`. A tray with keys that no binding holds has no occupant. Binding it is #6264's job.
  - `reconcile(conn, printer_id: str, report: Mapping[str, Any], seen_at: datetime, *, actor: str) -> CaptureResult`. For each AMS unit the report shows (`ams.ams[].id`), plus the external holders when `vt_tray` or `vir_slot` is present:
    - `current` = the unarchived spools whose `location_id` is the tray's AMS location (`ensure_ams_on`, label `"<printer name> AMS <n> slot <m>"`, the external holder `"<printer name> external"`).
    - If the occupant is not in `current`: each spool in `current` gets `unloaded` (printer, `ams_id`, `tray_id`, `data={"by": ...}`, at `seen_at`) and moves back (`move_on`) to the `from` of the newest `moved` event whose `to` is this location, else to `None`. The occupant then gets `loaded` and `move_on` to the tray.
    - A filled tray whose spool is the occupant and whose `remain` is not `None` gets an `ams_reading` (`source="ams_remain"`, `at=seen_at`, `data={"remain_pct": remain, "printing": gcode_state in RUNNING, "job": subtask_name if printing else None}`). It is booked when the spool was just loaded, or when its newest `ams_reading` has another `remain_pct` or another tray.
    - Trays of an AMS unit the report does not show are left alone (Review Focus 1).
    - Spools are locked in id order before the first event.
- `advance_in` selects `report` beside `running_subtask` and `seen_at`. After a written
  write, when `trays_changed(old_report, report)` or the old row is missing, it calls
  `reconcile(conn, printer_id, report, seen_at, actor=WATCH_ACTOR)` in a savepoint. A
  failure is logged with `FILAMENT_CAPTURE_FAILED` (`what="trays"`) and dropped. A stale
  write returns before this, as it does today.
- `capture.py` must not import `printers.reports` (`reports.py` imports `capture`).

- [ ] **Step 1: Write the failing tests** (`test_capture.py`, with a LAN printer row and the report fixtures of `tests/printers/fixtures`):
  - `test_a_tagged_spool_loads_and_reads(pg_pool)`: a spool bound to tray uuid U. A report with AMS 0 tray 1 carrying U, `remain=80` → events `loaded`, `moved` (to the tray's location) and `ams_reading` with `remain_pct == 80`; the spool's `location_id` is the tray's location.
  - `test_a_swap_unloads_the_old_spool_and_loads_the_new`: A loaded in tray 1 from shelf S; the next report shows B's tag → A gets `unloaded` and moves back to S; B gets `loaded`.
  - `test_an_emptied_tray_unloads`: the tray is no longer filled → `unloaded`; no reading.
  - `test_a_partial_report_unloads_nothing` (Review Focus 1): a report with no `ams` key → `CaptureResult()` and no events.
  - `test_a_rescan_with_no_keys_keeps_the_spool` (Review Focus 2): keys go to zeros with the same material and colour → no event.
  - `test_the_same_remain_books_one_reading`: two reports, `remain` 80 then 80 → one `ams_reading`; then 79 → a second one.
  - `test_a_reading_while_printing_is_marked`: `gcode_state="RUNNING"` → `data.printing is True`, and `grams_left` stays as before.
  - `test_an_untagged_tray_finds_its_linked_spool`: a `tray_spool_links` row with a matching fingerprint → `loaded` with `data.by == "link"`.
  - `test_a_resent_report_books_nothing` (Review Focus 4): `advance_in` twice with the same report and `seen_at` → the events of the first write only.
  - `test_a_tray_capture_that_raises_still_writes_the_report` (Review Focus 3): monkeypatch `capture.reconcile` to raise → the row is written, `FILAMENT_CAPTURE_FAILED` is logged with `what="trays"`.
  - `test_move_on_and_ensure_ams_on_match_the_pool_methods` (`test_locations.py`).
- [ ] **Step 2: Run** `tests/filament/test_capture.py tests/filament/test_locations.py` → FAIL.
- [ ] **Step 3: Implement.** Extend the `printers/reports.py` module docstring with one paragraph: the write that shows a plate end or a tray change books the ledger events in its own transaction (Tasks 5 and 6).
- [ ] **Step 4: Run** them, `tests/printers tests/spools tests/bambuddy/test_trays*.py tests/api/test_crud_print*.py` → PASS.
- [ ] **Step 5: Commit** `feat(filament): tray changes write loaded, unloaded and AMS readings (#6263)`.

### Task 7: Bambuddy's usage rows become print ends

**Files:**
- Create: `backend/scadbuddy/filament/bambuddy_usage.py`
- Modify: `backend/scadbuddy/bambuddy/models.py` (`SpoolUsage`), `backend/scadbuddy/bambuddy/client.py` (`spool_usage`, `Operation` gains `"spools.usage"`), `backend/scadbuddy/filament/bambuddy_pull.py` (`PullReport.usage_booked`, `usage_unlinked`; `read_and_pull`; module docstring paragraph on time)
- Test: `backend/tests/filament/test_bambuddy_usage.py` (new), `backend/tests/bambuddy/test_client*.py` (the usage read), `backend/tests/filament/test_bambuddy_pull.py`

**Interfaces:**
- Produces `SpoolUsage(BambuddyModel)`: `id: int`, `spool_id: int`, `printer_id: int | None`, `print_name: str | None`, `weight_used: float`, `percent_used: float | None`, `status: str | None`, `created_at: datetime`, with the naive-UTC validator that `Spool` uses. Cite the BB schema `SpoolUsageHistoryResponse` (BB 1.2.6b1 `openapi.json`) and Task 1's measurement.
- Produces `BambuddyClient.spool_usage(*, limit: int = 200) -> list[SpoolUsage]`: `GET /inventory/usage`, `Scope.READ_STATUS` (the prod key's scopes answered 200 on 2026-11-15), and one bad row skipped and logged, as `parse_rows` does.
- Produces in `bambuddy_usage.py`:
  - `ARCHIVE_WINDOW = timedelta(seconds=10)`: Task 1 measured 0.4 s to 4.3 s between a usage row and its archive's `completed_at`. If Task 1 finds a wider spread, widen this to cover its max.
  - `@dataclass UsageReport(booked: int = 0, unlinked: int = 0)`.
  - `unbooked(conn, ids: Sequence[int]) -> set[int]`: the usage ids that no `bb_usage` event names.
  - `match_archive(row: SpoolUsage, archives: Sequence[Archive]) -> int | None`: the archive of the same BB printer whose `completed_at` is in `[row.created_at, row.created_at + ARCHIVE_WINDOW]`, the nearest one.
  - `book_usage(conn, rows: Sequence[SpoolUsage], archives: Sequence[Archive], *, actor: str) -> UsageReport`. For each row with `weight_used > 0`:
    - The SB spool is found through `filament_bb_links.bb_spool_id`. With no link, the row is `unlinked` and skipped.
    - A row whose `created_at` is not later than the spool's first `bb_spool` event is skipped. It lies inside the anchor the link made.
    - Otherwise, `ledger.record(kind="print_ended", source="print_actual", delta_g=-weight_used, ref_kind="bb_usage", ref_id=str(row.id), at=row.created_at, printer=<SB printer whose bambuddy_printer_id is row.printer_id, else None>, actor=actor, data={"job": print_name, "basis": "bb_usage", "bb_usage_id", "bb_spool_id", "bb_status": status, "bb_archive_id": match_archive(...)})`.
- `read_and_pull`: within one `client_for`, read `spool_rows()` first, then
  `spool_usage()`. This order matters: a deduction between the two reads then shows in
  the usage rows, never only in a spool's value. When `unbooked(...)` is not empty, also
  read `archives(limit=50)`. Then run `pull(...)`, then `book_usage(...)` in one
  `store.spools.transaction()`, and set the report's `usage_booked` and `usage_unlinked`.
  A failure of the usage read or its booking is logged and leaves the spool pull's
  report standing.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_usage_row_of_a_print_sb_did_not_start_is_a_print_end(pg_pool)`: a linked spool (first BB event at t0), a row at t1 > t0 with 75.1 g, and an archive of the same printer completed at t1 + 0.9 s → one `print_ended` with `delta_g == -75.1`, `ref_kind == "bb_usage"`, `data.bb_archive_id` set, `at == t1`.
  - `test_a_usage_row_is_booked_once`: `book_usage` twice → `booked` 1, then 0.
  - `test_a_usage_row_before_the_link_is_not_booked`.
  - `test_a_usage_row_of_an_unlinked_bb_spool_is_counted_unlinked`.
  - `test_bb_reading_and_usage_of_one_write_count_once` (end to end through `pull` and `book_usage`): the BB spool's `updated_at` equals the row's `created_at`, and `weight_used` holds the row → `grams_left == label − weight_used`, not that minus the row.
  - `test_an_sb_plate_and_its_usage_row_count_once`: a native `print_started`/`print_ended` of job J at the report end; a usage row J at a time 1 s earlier, and a BB reading at that time → `grams_left` equals the BB value.
  - `test_spool_usage_reads_and_skips_a_bad_row` (client test with `httpx.MockTransport`, as the file's other reads do).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Rewrite the `bambuddy_pull.py` docstring's last paragraph:
  the reading sits at BB's `updated_at`, and BB's usage rows sit at their `created_at`,
  on the same instant as the deduction. SB's own plates pair with them by job (`ledger.py`).
- [ ] **Step 4: Run** `tests/filament tests/bambuddy` → PASS.
- [ ] **Step 5: Commit** `feat(filament): book Bambuddy's usage rows as print ends (#6263)`.

### Task 8: SpoolRefresh's capture backstop

**Files:**
- Modify: `backend/scadbuddy/filament/capture.py` (`reconcile_all`), `backend/scadbuddy/spools/activities.py` (`capture_backstop_activity`), `backend/scadbuddy/workflows/spool_refresh.py`, `backend/scadbuddy/main.py` (register it beside `filament_pull_activity`, line ~1810)
- Create: `backend/tests/fixtures/spool_refresh_histories/capture.json` (recorded, never edited by hand)
- Test: `backend/tests/test_spool_refresh_replay.py`, `backend/tests/test_spool_refresh_workflow.py`, `backend/tests/filament/test_capture.py`

**Interfaces:**
- Consumes: `capture.reconcile`, `BACKSTOP_ACTOR`; `Printers` (`printers/component.py` `PRINTERS`) for `reports.all()` and `is_fresh` (`printers/status.py`).
- Produces: `reconcile_all(pool: ConnectionPool[Conn], reports: Mapping[str, tuple[dict[str, Any], datetime]], *, now: datetime) -> CaptureResult`. It reconciles each fresh report (`is_fresh(seen_at, now)`) in its own transaction, with `actor=BACKSTOP_ACTOR` and the report's own `seen_at`. A printer that raises is logged and skipped.
- Produces: `capture_backstop_activity(filament: Filament, printers: Printers) -> Callable[[], Any]`, activity name `filament_capture_backstop`, returns `"captured"` or `"failed"`, and never raises.
- Produces in `spool_refresh.py`: `CAPTURE_ACTIVITY = "filament_capture_backstop"`,
  `CAPTURE_PATCH = "filament-capture-backstop"`, `CAPTURE_TIMEOUT = timedelta(minutes=2)`.
  The step runs after the `FILAMENT_PULL_PATCH` step, inside `if workflow.patched(CAPTURE_PATCH):`,
  with `_ONCE`. An `ActivityError` is logged ("could not reconcile the printers' trays
  with the ledger") and the run goes on. Add one sentence to the module docstring.

- [ ] **Step 1: Write the failing tests:**
  - `test_the_backstop_books_a_change_the_watch_missed` (`test_capture.py`): write a report row directly (no capture) with a tagged spool in a tray → `reconcile_all` books `loaded`; a second call books nothing.
  - `test_a_stale_report_is_left_to_the_watch`: a report older than `STALE_AFTER` → nothing.
  - In `test_spool_refresh_workflow.py`, the existing run test gains a `filament_capture_backstop` stub. The run calls it once, after `filament_bambuddy_pull`. A raising stub still completes the run.
  - Add `"capture"` to the replay test's parameter list and to `test_record`'s.
- [ ] **Step 2: Run** → FAIL (no `capture.json`, no step).
- [ ] **Step 3: Implement**, then record the history:
  `SCADBUDDY_RECORD_HISTORIES=1 uv run --frozen pytest tests/test_spool_refresh_replay.py -k "test_record and capture"`.
  Check the WSL clock first (Global Constraints). Commit `capture.json` as recorded.
  Leave the existing four histories alone.
- [ ] **Step 4: Run** `tests/test_spool_refresh_replay.py tests/test_spool_refresh_workflow.py tests/filament/test_capture.py` → PASS. All five histories replay.
- [ ] **Step 5: Commit** `feat(filament): reconcile trays with the ledger on the spool refresh (#6263)`.

### Task 9: Spec, notes, whole-branch check and PR

**Files:**
- Modify: `docs/superpowers/specs/2026-11-14-filament-management-design.md`, `backend/CLAUDE.md`, this plan ("Decisions made while building")

- [ ] **Step 1: Spec §5.5.** Change "a print's `print_started` and `print_ended` at the
  plate's start" to: `print_started` when the printer takes the plate, and `print_ended`
  at the report that shows its end (the settle's time when the watch missed it). Add the
  three rules of Task 2. Replace "Known limits of the time rule" with the limits that
  remain after #6263, with the skew Task 1 measured:
  - A print BB did not deduct (BB down at the end, or BB credits it to the spool BB
    assigned to the tray) and a later BB reading: SB's plate sits inside the BB anchor's
    time but not inside its value, so it is under-counted until the next scale or person
    reading.
  - A BB usage row with no SB record (a print SB did not start) is compared with an AMS
    or person anchor by raw time, so the measured skew decides a plate that ends within
    that skew of the reading.
  - During a plate the spool shows the anchor before the plate minus the estimate, since
    AMS readings taken while printing are no anchor.
  - The pull reads the newest 200 usage rows. More than 200 rows between two pulls are
    not all booked. They are inside the next BB reading's value.
  - A plate end that no report showed is booked at the settle's clock time, an upper
    bound.
  Delete the four #6261 limits. #6263 fixes three of them, and Task 1 measured the fourth.
- [ ] **Step 2: Spec §6 and §8.2.** In §6 "Prints", say that the pull books BB's usage
  rows, per spool, with the matched archive id. It does not book the print watcher's
  archive import, and the reasons are those under "Decisions this plan makes". Also say
  that only SB's native LAN runs write `print_started`: a print sent through BB's queue
  gets its grams only from BB's usage rows. When SB and BB both record one plate, BB's
  grams count (owner's rulings, 2026-11-15). In §6
  "Tray changes", name the report write (`printers/reports.py` `advance_in`) and the
  backstop. In §8.2, rewrite the "As built in #6261" paragraph: count once by pairing a
  job's SB and BB records, each anchor compared in its own clock.
- [ ] **Step 3: `backend/CLAUDE.md`.** In the `backend/scadbuddy/filament/` line, add
  `prints.py`, `capture.py` and `bambuddy_usage.py` in one clause each. Change "ordered by
  `(at, seq)`" to the new order.
- [ ] **Step 4: Run every check:** `uv run --frozen pytest -n auto tests/filament tests/spools tests/printers tests/bambuddy tests/crud tests/api/test_crud_print*.py tests/api/test_filament.py tests/test_pg_migrations.py tests/test_spool_refresh_replay.py tests/test_spool_refresh_workflow.py tests/api/test_openapi.py`; ruff; mypy. `grep -rn "plate's start\|plate start" backend/scadbuddy docs/superpowers/specs/2026-11-14-filament-management-design.md` finds no stale text.
- [ ] **Step 5: Fill "Decisions made while building (#6263)"** below with each ruling and what it costs if wrong.
- [ ] **Step 6: Open the PR** `feat(filament): capture tray changes and print ends (#6263)`, body `Closes #6263`, the session marker and the footer. Shepherd it to green and a clean review.

## Decisions made while building (#6263)
