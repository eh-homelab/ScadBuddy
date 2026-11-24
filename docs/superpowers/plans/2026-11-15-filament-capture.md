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
made while building (#6261)". Epic #6260. Parallel plan that lands first:
`docs/superpowers/plans/2026-11-15-filament-ui.md` (#6262). Preflight scan and its
rulings: `.superpowers/sdd/2026-11-15-filament-capture/preflight.md`.

## Order of work

#6262 lands first. This plan has two parts.

- **Part 1, built now on main:** Task 1, Task 1A (the spec text), Tasks 2, 3, 4 and 7.
  They touch #6262's code only in `filament/ledger.py`, `filament/bambuddy_pull.py` and
  the BB client. Those parts rebase as text (preflight D1, D10, D11).
- **Part 2, started only after #6262 merges:** Tasks 5, 6 and 8, written against
  #6262's names (`filament/print_uses.py`, `filament/inventory.py`, the tray place).
  Rebase Part 1 onto main first.
- **Last:** Task 9.

Task order: 1, 1A, 2, 3, 4, 7 | (#6262 merges) | 5, 6, 8 | 9.

## What a read-only probe of the live Bambuddy showed (2026-11-15)

Measured from this WSL host on 2026-11-15 at about 05:50 UTC against
`https://bambuddy.internal.nullreference.io` (Bambuddy 1.2.6b1), with
`GET /api/v1/inventory/usage?limit=60`, `GET /api/v1/archives/?limit=40` and
`GET /api/v1/inventory/spools`, read-only. Task 1 reuses these facts. It measures again
only the archive window (preflight A2) and the clock skew, which the probe did not
measure. The design below rests on these facts:

- BB keeps one usage row per spool per print (`SpoolUsageHistoryResponse`: `id`,
  `spool_id`, `printer_id`, `print_name`, `weight_used`, `percent_used`, `status`,
  `cost`, `created_at`). BB has rows for SB's own LAN prints too. Their `print_name` is
  SB's `subtask_name` (`scadbuddy-ac13ac3c-p1`, `scadbuddy-9b69b0b9-p1`).
- A usage row's `created_at` equals its spool's `updated_at` to the microsecond (usage
  457 and spool 31: `2026-11-13T08:24:44.614397`; usage 465 and spool 103). So BB
  deducts in the same write that moves `updated_at`.
- BB deducts as the print ends. The archive's `completed_at` is 0.4 s to 4.3 s after
  the usage rows' `created_at` (7 pairs: archives 248, 249, 250, 254, 255, 256, 264).
  That window was searched only after `created_at`, so Task 1 measures it again.
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
- Capture locks, in one pass and in id order, every spool that the plate end and the
  tray reconcile of one write will touch, before either books an event. It never takes a
  tag lock. The report row's lock (`printer_reports ... FOR UPDATE`) is held first, by
  the watch's write and by the backstop alike.
- One constant per `ref_kind`: code uses `ledger.PRINT_RUN` and `ledger.BB_USAGE`, never
  the literal strings.
- Commands: `cd backend && uv run --frozen pytest <files>` with
  `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test`;
  `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`.
- Out of scope: per-type sync settings and SB→BB push. Both are #6265's, as the epic's
  table says. The gate that exists today is #6262's `filament_sync.spools` setting
  (`off` | `bb_to_sb`). The BB usage booking runs inside `read_and_pull`, so it obeys
  that setting. Do not read any other sync setting here. The AMS weight sync button
  (#6268) is out of scope too.

## Decisions this plan makes

Each decision names what it costs if it is wrong.

- **The plate's end time is the report that shows the end.** `advance_in` already finds
  the end (`advance_print` → `PrintEnd`). When the end is timed (`FINISH` or `FAILED`),
  it books `print_ended` for that plate's open estimates in the same transaction, at the
  report's `seen_at`. An untimed end (moved on, `IDLE`) is left to the follow's settle.
  The settle stays as the backstop and books what is still open at its own clock time.
  This replaces #6261's plate-start rule. Cost: a plate whose end no report showed is
  booked late, an upper bound.
- **BB weighings are booked with the source `scale`** (item 5). This changes the label
  only, never `grams_left`. `grams_left` cannot tell `bb` from `scale`: an
  `ams_remain` reading newer than the newest scale reading is the anchor (spec §5.5),
  whatever the scale's source, unless the weight lock is on. The label matters for spec
  §5.5's sources and §5.7's "AMS % against scale readings" metric. The provenance stays
  in `ref_kind = 'bb_scale'` and `actor = 'bambuddy'`. Cost: none on grams (preflight C1,
  C14).
- **Prints SB did not start are booked from BB's usage history, not the archive.** An
  archive has only total grams. The print watcher imports an archive while the print
  still runs, before any actual exists. BB's usage rows are per spool and are what BB
  deducted. Each row gets the BB archive id that matches by printer and time (§8.2).
  Cost: a usage row with no matched archive has `bb_archive_id` null; grams unaffected.
- **Prints sent through BB's queue get no SB `print_started` estimate** (owner's ruling,
  2026-11-15). Their grams come only from BB's usage rows (Task 7). Only SB's native LAN
  runs write `print_started`. Cost: no estimate on the spool while a queued print runs.
- **One plate, two records, counted once.** SB's `print_run` events and BB's `bb_usage`
  event are paired when they have the same spool and job (`subtask_name` =
  `print_name`). BB's grams count (owner's ruling, 2026-11-15), because BB keeps a failed
  plate's partial grams, and SB books 0 g for one.
  To decide whether a pair lies after the anchor, the ledger compares a BB-clock anchor
  with BB's time and every other anchor with SB's time. So clock skew between BB and SB
  never counts a plate twice. Cost: if BB credits the plate to another spool, it counts on
  two spools (a known limit, Task 1A).
- **AMS readings taken while the printer prints are recorded, never anchors.** This
  replaces the limit "a reading taken in the middle of a plate hides the rest of that
  plate". Cost: during a plate, the spool shows the anchor before it minus the estimate.
- **At one instant, deltas come before readings.** A reading at the moment a plate ends
  holds that plate. BB's usage row and the spool's `updated_at` carry the same time.
  Cost: none found.
- **A loaded spool's location is its AMS tray** (spec §5.6). An unload moves it back to
  where it was before that load (the `from` of the `moved` event that put it in the tray),
  else to no location. Cost: a spool moved by hand while loaded goes back to its older place.
- **One tray resolver, #6262's.** `capture` asks `filament/inventory.py` which spool a
  tray holds, as the print dialog does. A tray whose keys no binding holds falls through
  to the placed-spool rule, as an untagged tray does (preflight C5). The fingerprint
  includes alpha (#6262's `tray_fingerprint`, preflight C6). Cost: the dialog's tray
  answer changes for a tagged but unbound tray (it now keeps a placed spool).

### Preflight rulings (2026-11-15)

From `.superpowers/sdd/2026-11-15-filament-capture/preflight.md`, with the controller's
rulings. "Cost" is what the ruling costs if it is wrong.

| # | Ruling | Where | Cost if wrong |
|---|---|---|---|
| A1 | Task 2 starts after Task 1's commit. | Order | None. |
| A2 | Task 1 measures the archive match in `[created_at − 60 s, created_at + 60 s]`. `ARCHIVE_BEFORE` and `ARCHIVE_AFTER` get their bounds from the widest offsets seen, a negative lower bound included. | Tasks 1, 7 | A row loses its archive id; no grams effect. |
| A3 | Task 9 never deletes Task 1's measured statement. It moves into §8.2 beside the dated paragraph, and Task 1A deletes only the three limits #6263 fixes. | Tasks 1, 1A | Measured facts lost from the spec. |
| A4 | The fast path sets `last_weighed_*` only when `grams_after` is not null. | Task 3 | `last_weighed_*` set to null. |
| A5 | `bb_scale` stays in `BB_REFS`, so a BB weighing keeps BB's clock. | Tasks 2, 4 | None. |
| A6 | One constant, `ledger.PRINT_RUN`. | Tasks 2, 5 | A typo splits pairing. |
| A7, A8 | Names agree (`data.printing`, `bb_usage`, `data.job`). | Tasks 2, 6, 7 | None. |
| A9 | Task 3 matches the spec; Task 4's test changes (C1). | Task 4 | A red test "fixed" by breaking §5.5. |
| A10 | `move_on(..., at=)`; capture passes `seen_at`. | Task 6 | Flaky order tests; slow path. |
| A11 | The migration backfills `data.job` on old `print_started` rows. | Task 4 | One upper-bound end per in-flight plate at deploy. |
| A12 | `_book` and `read_and_pull` are separate functions; Task 7 rewrites the docstring's last paragraph. | Tasks 4, 7 | None. |
| A13 | Plate end first, then trays, after one id-order lock of the union. | Tasks 5, 6 | One lost capture per deadlock. |
| A14 | The backstop locks the report row and reconciles the row it reads under that lock. | Task 8 | Duplicate `loaded` events. |
| A15 | Pull, then backstop. | Task 8 | None. |
| A16 | The spec text moves before Task 2 (Task 1A). | Order | Review blocks on code that contradicts the spec. |
| B1 | No `kubectl exec`. Check the key length before the first request. | Task 1 | A prod exec nobody approved; an error string sent to BB as a key. |
| B2, B3 | Task 2's eight tests and Task 3's counter test stand. | Tasks 2, 3 | None. |
| B4 | See C1. | Task 4 | See C1. |
| B5 | `plate_ended` returns nothing; untimed ends go to the settle (C2). The settle's answer keeps #6262's meaning (C16 is moot). | Task 5 | See C2. |
| B6 | `RUNNING` moves to `printers/status.py`. Trays come from the report's `ams.ams[].tray[].id` and the unit's existing tray locations. | Task 6 | ImportError, or an emptied tray never unloads. |
| B7 | See C9, C10. | Task 7 | See C9, C10. |
| B8 | `test_record` gets a `CAPTURE_ACTIVITY` stub. | Task 8 | The recording hangs. |
| B9 | The stale-text grep is `plate's start\|at its plate's start`, so #6262's `plate_started_at` does not match. C22's limit is added. | Task 9 | A false pass or fail. |
| C1 | Task 4's test asserts `source == "scale"`. A later `ams_remain` gives 500 (the spec), and 1000 with the weight lock on. | Task 4 | None; the other choice breaks §5.5. |
| C2 | Book at the report only on a timed end. | Task 5 | Lost grams on every plate whose `FINISH` was missed. |
| C3 | `RUNNING` lives in `printers/status.py`, re-exported by `reports.py`. | Task 6 | ImportError. |
| C4 | `remain` is `None` when the key is absent and for the external holders. A reading is booked only for a tray with RFID keys. | Task 6 | A spool's grams set to 0. |
| C5 | Unbound keys fall through to the placed-spool rule. | Task 6 | A placed spool moves out on every tray change. |
| C6 | The fingerprint includes alpha. | Task 6 | A translucent spool stays loaded after an opaque one replaced it. |
| C7 | The backstop runs under the report row's lock (A14). | Task 8 | Duplicate events. |
| C8 | The stub (B8). | Task 8 | Hang. |
| C9 | One shared helper `bambuddy/models.py` `as_utc`, used by `Spool`, `SpoolUsage` and `match_archive` for `Archive.completed_at`. | Task 7 | `TypeError` on every pull. |
| C10 | The end-to-end test links the spool in a first pull at t0, then pulls at t1 with `updated_at == created_at`. | Task 7 | A regression in rule 1 passes CI. |
| C11 | Per-type sync settings are #6265's (the epic's table). Today's gate is #6262's `filament_sync.spools`, and the usage booking obeys it. | Global Constraints | A weights-only "off" cannot stop usage rows until #6265. |
| C12, C13 | Spec §6, §5.5 and §8.2 are changed in Task 1A, before any code. | Task 1A | Review blocks on a spec contradiction. |
| C14 | The source rewrite is safe on prod data; no recompute. | Task 4 | None found. |
| C15 | The pull logs `usage_booked`. The PR body says the first pull books up to 200 historic usage rows, and that paired historic plates then take BB's grams. | Tasks 7, 9 | A surprised owner; no data loss. |
| C16 | Moot: #6262's `print_use_hook` has no recache. | Task 5 | Stale dialog grams for one refresh. |
| C17 | One lock pass (A13). | Task 6 | One lost capture per deadlock. |
| C18 | `move_on(at=seen_at)` (A10). | Task 6 | Flaky tests. |
| C19 | `trays_changed` compares a tray projection, not the raw blocks. | Task 6 | Extra SQL per report write. |
| C20 | The backfill (A11). | Task 4 | One upper-bound end at deploy. |
| C21 | The new step is compliant with replay. | Task 8 | None. |
| C22 | A plate that BB credits to another spool counts on two spools; listed as a known limit. | Task 1A | An unlisted known error. |
| D1 | One walk: the rules go into #6262's `ledger.basis`, and `grams_left` stays built on it. Both test sets pass. | Task 2 | The UI's anchor and deltas disagree with `grams_left`. |
| D2 | Task 5 targets `filament/print_uses.py`. `prints.py` owns the lookups and `book_end`; `print_uses.settle_uses_in` calls them. | Task 5 | Two settle implementations. |
| D3 | New settle tests go in `test_print_uses.py` and `test_prints.py`. | Task 5 | Lost tests. |
| D4, D5 | No `tray_spool_links`; one resolver in `inventory.py`. | Task 6 | Capture dead after #6262. |
| D6 | The tray-place clearing lives in `move_on`; capture's load sets `tray_fingerprint` through the same function as `link`. | Task 6 | A captured spool is lost on the next rescan. |
| D7 | Moot: #6262 lands first, so its carry migration runs before capture exists. | Order | Two spools in one tray. |
| D8 | `spool-providers-gone` and `filament-capture-backstop` both stay; neither returns early. Capture runs after the pull on every branch. One new history carries both markers. | Task 8 | A replay error, or a skipped backstop. |
| D9 | The backstop's docstring names it as the successor of `spool_trays_seen`. | Task 8 | None. |
| D10 | Read order in one `client_for`: `spool_rows()`, `spool_usage()`, `spool_catalog()`, then `archives()` when needed. Each optional read fails alone. | Task 7 | A deduction counted twice or missed. |
| D11 | Merge both `Operation` entries and models. | Task 7 | None. |
| D12 | Moot (C16). | Task 5 | None. |
| D13 | No edit to the print activities. | — | None. |
| D14 | The usage booking obeys `filament_sync.spools`; tested once after the rebase. | Task 7 | Usage booked with the pull "off". |

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
Part 1
backend/scadbuddy/migrations/<ts>_filament_capture.sql     Task 4
backend/scadbuddy/filament/
  ledger.py          pairing, tie order, printing readings     Task 2
                     fast path of refresh_derived              Task 3
  bambuddy_usage.py  BB usage rows → print_ended                Task 7  (new)
  bambuddy_pull.py   weighings as scale; usage in read_and_pull Tasks 4, 7
backend/scadbuddy/bambuddy/models.py     SpoolUsage, as_utc                          Task 7
backend/scadbuddy/bambuddy/client.py     spool_usage()                               Task 7
docs/superpowers/specs/2026-11-14-filament-management-design.md  Tasks 1, 1A

Part 2 (after #6262)
backend/scadbuddy/filament/
  prints.py          plate ends: open estimates, book an end   Task 5  (new)
  print_uses.py      (#6262's) data.job; settle through prints  Task 5
  capture.py         trays → loaded/unloaded/ams_reading       Tasks 6, 8  (new)
  inventory.py       (#6262's) one conn-level resolver; place_on Task 6
  locations.py       ensure_ams_on, move_on                     Task 6
backend/scadbuddy/spools/models.py       SeenTray.remain                             Task 6
backend/scadbuddy/bambuddy/trays.py      seen_trays fills remain                     Task 6
backend/scadbuddy/printers/status.py     RUNNING moves here                          Task 6
backend/scadbuddy/printers/reports.py    advance_in calls capture                    Tasks 5, 6
backend/scadbuddy/printers/store.py      PrinterStore.read_on                        Task 6
backend/scadbuddy/spools/activities.py   capture_backstop_activity                   Task 8
backend/scadbuddy/workflows/spool_refresh.py  CAPTURE step behind its patch          Task 8
backend/scadbuddy/main.py                register the backstop activity              Task 8

Last
backend/CLAUDE.md                        filament layout line                        Task 9
```

---

## Part 1 — built now

### Task 1: Measure the archive window and the clock skew (read-only)

**Files:**
- Modify: `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§8.2)
- Scratch only (never committed): a script in the session scratchpad that reads the
  saved JSON. Keep the data and the script in separate directories, and run Python with
  `-I`.

**Interfaces:**
- Produces: measured facts that Tasks 2 and 7 rely on, and the bounds of `ARCHIVE_BEFORE` and `ARCHIVE_AFTER`. If
  a fact does not hold, stop and report to the owner before you start Task 2.

Strictly read-only: only `GET` requests, no print, no write, no `kubectl exec`. Facts
(b), (c) and (d) of the 2026-11-15 probe above are reused, not measured again.

- [ ] **Step 1: Read the API key without printing it.** The key is the
  `SCADBUDDY_BAMBUDDY_API_KEY` field of the item `scadbuddy-bambuddy-api-key` in the
  vault "homelab - prod" (named in eh-homelab/clusters
  `clusters/prod/scadbuddy/onepassworditem-bambuddy-api-key.yaml`).
  Under WSL, `op.exe read` writes the value to **stderr**. This is the only allowed
  form:
  `KEY=$(op.exe read "op://homelab - prod/scadbuddy-bambuddy-api-key/SCADBUDDY_BAMBUDDY_API_KEY" 2>&1)`.
  Then print only `${#KEY}`, and go on only when it is 46 (the length on 2026-11-15).
  Any other length is an `op.exe` error message, and it must never be sent to BB.
  Never echo `$KEY`, and never send it to the terminal, a file or a log. Pass it only as
  the `X-API-Key` header.
- [ ] **Step 2: Read the usage rows and archives.** Save
  `GET /api/v1/inventory/usage?limit=500` and `GET /api/v1/archives/?limit=200`.
- [ ] **Step 3: Measure the archive window (preflight A2).** For every usage row, find
  the archive of the same BB printer whose `completed_at` is in
  `[created_at − 60 s, created_at + 60 s]`, the nearest one. Keep the distribution of
  `completed_at − created_at`: min, median, max, n, and how many rows match none.
- [ ] **Step 4: Measure the clock skew between BB and SB from server clocks, not the WSL
  host.** This is a first measurement; the probe had one sample only. Send 120
  alternating requests, 0.25 s apart: `GET /api/v1/system/storage-usage` on BB and
  `GET https://scadbuddy.internal.nullreference.io/healthz` on SB. Record each response's
  `Date` header. Estimate the offset from where each server's second boundary falls in
  the sample order (resolution about 0.25 s).
- [ ] **Step 5: Write the results into spec §8.2,** after "Consumption is counted once":
  one dated paragraph with the probe's facts (b) to (d), the window of Step 3 and the
  skew of Step 4. Give the date, the URLs, the endpoints, n and the method of each.
  Also write the measured statement that replaces §5.5's limit "The rule assumes BB
  deducts `weight_used` when a print ends … not yet measured": BB deducts in the write
  that ends the print, at the usage row's `created_at`, which equals the spool's
  `updated_at`.
- [ ] **Step 6: Commit** `docs(filament): measure when Bambuddy deducts a print and its clock skew (#6263)`.

### Task 1A: The spec says the new rules first

**Files:**
- Modify: `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§5.5, §6, §8.2)

**Interfaces:**
- Produces: the binding text that Tasks 2 to 8 implement (preflight A16, C12, C13).

- [ ] **Step 1: Spec §5.5.** Change "a print's `print_started` and `print_ended` at the
  plate's start" to: `print_started` when the printer takes the plate, and `print_ended`
  at the report that shows its timed end (the settle's time when no report showed it).
  Add the three rules of Task 2. In "Known limits of the time rule", delete the three
  limits #6263 fixes (the BB write during a plate, the BB clock behind SB's by a plate,
  the reading in the middle of a plate). Keep the fourth, now as Task 1's measured
  statement, with a pointer to §8.2. Then add the limits that remain after #6263:
  - A print BB did not deduct (BB down at the end) and a later BB reading: SB's plate
    sits inside the BB anchor's time but not inside its value, so it is under-counted
    until the next scale or person reading.
  - A plate that BB credits to another spool (the spool BB assigned to the tray, not the
    one SB printed from) counts on two spools: SB's record on SB's spool, and BB's usage
    row on the other. #6264 and #6265 fix this when they sync the tray assignment.
  - A BB usage row with no SB record (a print SB did not start) is compared with an AMS
    or person anchor by raw time, so the measured skew decides a plate that ends within
    that skew of the reading.
  - During a plate the spool shows the anchor before the plate minus the estimate, since
    AMS readings taken while printing are no anchor.
  - The pull reads the newest 200 usage rows. More than 200 rows between two pulls are
    not all booked. They are inside the next BB reading's value.
  - A plate end that no report showed is booked at the settle's clock time, an upper
    bound.
- [ ] **Step 2: Spec §6 and §8.2.** In §6 "Prints", say that the pull books BB's usage
  rows, per spool, with the matched archive id. It does not book the print watcher's
  archive import. Give the reasons under "Decisions this plan makes", cite the owner's
  rulings of 2026-11-15 and Task 1's measurement. Also say that only SB's native LAN
  runs write `print_started`: a print sent through BB's queue gets its grams only from
  BB's usage rows. When SB and BB both record one plate, BB's grams count. In §6 "Tray
  changes", name the report write (`printers/reports.py` `advance_in`) and the backstop.
  In §8.2, rewrite the "As built in #6261" paragraph: count once by pairing a job's SB and
  BB records, each anchor compared in its own clock.
- [ ] **Step 3: Commit** `docs(filament): the capture rules in the spec (#6263)`.

### Task 2: The ledger counts one plate once

**Files:**
- Modify: `backend/scadbuddy/filament/ledger.py` (module docstring, `grams_left`, new helpers; after #6262, `basis`)
- Test: `backend/tests/filament/test_ledger.py`

**Interfaces:**
- Produces in `ledger.py`:
  - `PRINT_RUN = "print_run"`, `BB_USAGE = "bb_usage"` (the `ref_kind`s of SB's plates and of BB's usage rows);
  - `BB_REFS = frozenset({"bb_spool", "bb_scale", "bb_dried", BB_USAGE})`, the events that carry BB's clock;
  - `job_of(event: m.SpoolEvent) -> str | None`: `data["job"]` when it is a string, else, for `ref_kind == PRINT_RUN`, the middle part of `ref_id` (`run/subtask/slot` → `subtask`; rows from before #6263), else `None`;
  - `grams_left(events, *, weight_locked, label_weight_g) -> float | None` (same signature; the rules below).
- Consumes: nothing new. When the rebase finds #6262's
  `basis(events, *, weight_locked, label_weight_g) -> GramsBasis` on main, the rules go
  into `basis`'s walk, and `grams_left` stays built on it (preflight D1). A paired job's
  `bb_usage` event is in `GramsBasis.deltas`; its skipped `PRINT_RUN` events are not.

The rules the walk adds to #6261's (spec §5.5, as Task 1A wrote it):
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
- [ ] **Step 4: Run** the ledger tests and `tests/filament tests/spools` → PASS. Fix any older test that asserted the plate-start rule, and say so in the commit. After the rebase onto #6262, also run `tests/filament/test_spool_detail.py` (`test_basis_matches_grams_left`).
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
  leaves `grams_left` as it is. A `weighed` or `scale` event whose `grams_after` is not
  null also sets `last_weighed_*` (the full path's rule, preflight A4). A `dried` event
  sets `last_dried_*`. Any other kind updates nothing. Every other case takes the full
  path.

- [ ] **Step 1: Write the failing tests:**
  - `test_an_ams_reading_on_top_does_not_read_the_history(pg_pool, monkeypatch)`: make a spool with a scale reading. Monkeypatch `ledger.events` with a counting wrapper. `record(..., "ams_reading", source="ams_remain", data={"remain_pct": 60})` at a newer `at` → the counter stays 0, and `grams_left` is 60 % of the label weight.
  - `test_the_fast_path_matches_a_full_recompute(pg_pool)`: book in order scale 900; ams 85 %; `print_started` −40; ams 80 % `printing=True`; `print_ended` −38; ams 79 %; a `loaded` event; a BB weighing with no core weight (`weighed`, source `scale`, `grams_after` null); scale 760 with an `at` older than the newest; ams 75 % with `weight_locked` set between. After each `record`, compare `grams_left`, `last_weighed_g` and `last_weighed_at` with `grams_left(events(conn, id), ...)` and the full `refresh_derived` → equal.
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
- Produces, in one file:
  - the index `filament_spool_events_job ON filament_spool_events ((data ->> 'job')) WHERE kind = 'print_started' AND ref_kind = 'print_run'` (Task 5's `open_for_job` repeats both predicates and the exact expression);
  - the backfill `UPDATE filament_spool_events SET data = data || jsonb_build_object('job', regexp_replace(ref_id, '^[^/]*/(.*)/[^/]*$', '\1')) WHERE kind = 'print_started' AND ref_kind = 'print_run' AND jsonb_typeof(data -> 'job') IS DISTINCT FROM 'string' AND ref_id ~ '^[^/]*/.*/[^/]*$'` (preflight A11, C20);
  - `UPDATE filament_spool_events SET source = 'scale' WHERE kind = 'weighed' AND ref_kind = 'bb_scale' AND source = 'bb'`.
  - A header comment that says why no `grams_left` changes: `grams_left` cannot tell `bb` from `scale` (preflight C1, C14).

- [ ] **Step 1: Write the failing tests:**
  - `test_capture_migration_makes_bb_weighings_scale` (the `MIGRATIONS[:idx]` pattern of `tests/test_pg_migrations.py`): migrate to before the file; insert a spool, a `weighed`/`bb` event with `ref_kind='bb_scale'`, a `person` weighing and a `print_started`/`print_run` event with `ref_id='r1/J/0'` and no `data.job`; migrate → the first has source `scale`, the person weighing is untouched, the estimate has `data.job == "J"`, and `pg_indexes` lists `filament_spool_events_job`.
  - `test_a_bb_weighing_is_a_scale_reading` (in `test_bambuddy_pull.py`): pull a BB spool whose `last_scale_weight` is 1250 with core 250 → a `weighed` event with `source == "scale"`, grams 1000. Then `ledger.record` an `ams_remain` 50 % of 1000 at a later `at` → `grams_left == 500` (spec §5.5: the newer AMS reading is the anchor). The same with the spool's weight lock on → `1000`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Write the migration; change `_book`.**
- [ ] **Step 4: Run** `tests/filament tests/test_pg_migrations.py` → PASS.
- [ ] **Step 5: Commit** `feat(filament): a Bambuddy weighing is a scale reading (#6263)`.

### Task 7: Bambuddy's usage rows become print ends

**Files:**
- Create: `backend/scadbuddy/filament/bambuddy_usage.py`
- Modify: `backend/scadbuddy/bambuddy/models.py` (`as_utc`, `SpoolUsage`; `Spool._utc` calls `as_utc`), `backend/scadbuddy/bambuddy/client.py` (`spool_usage`, `Operation` gains `"spools.usage"`), `backend/scadbuddy/filament/bambuddy_pull.py` (`PullReport.usage_booked`, `usage_unlinked`; `read_and_pull`; the module docstring's last paragraph)
- Test: `backend/tests/filament/test_bambuddy_usage.py` (new), `backend/tests/bambuddy/test_client*.py` (the usage read), `backend/tests/filament/test_bambuddy_pull.py`

**Interfaces:**
- Produces `as_utc(value: datetime | None) -> datetime | None` in `bambuddy/models.py`: a naive time reads as UTC (BB's times are naive UTC, the `Spool` docstring). `Spool`'s validator calls it (preflight C9).
- Produces `SpoolUsage(BambuddyModel)`: `id: int`, `spool_id: int`, `printer_id: int | None`, `print_name: str | None`, `weight_used: float`, `percent_used: float | None`, `status: str | None`, `created_at: datetime`, with a validator that calls `as_utc`. Cite the BB schema `SpoolUsageHistoryResponse` (BB 1.2.6b1 `openapi.json`) and the probe.
- Produces `BambuddyClient.spool_usage(*, limit: int = 200) -> list[SpoolUsage]`: `GET /inventory/usage`, `Scope.READ_STATUS` (the prod key's scopes answered 200 on 2026-11-15), and one bad row skipped and logged, as `parse_rows` does.
- Produces in `bambuddy_usage.py`:
  - `ARCHIVE_BEFORE: timedelta` and `ARCHIVE_AFTER: timedelta`, set from Task 1 Step 3: an archive matches when `completed_at − created_at` is in `[−ARCHIVE_BEFORE, ARCHIVE_AFTER]`. Each bound covers the widest offset seen plus 2 s, and `ARCHIVE_BEFORE` is 0 when no negative offset was seen. Cite Task 1's paragraph.
  - `@dataclass UsageReport(booked: int = 0, unlinked: int = 0)`.
  - `unbooked(conn, rows: Sequence[SpoolUsage]) -> set[int]`: the ids of the rows that would book (the rules below) and that no `BB_USAGE` event names yet. It shares its link query and its check with `book_usage`.
  - `match_archive(row: SpoolUsage, archives: Sequence[Archive]) -> int | None`: the archive of the same BB printer in that window, the nearest one, with `as_utc(archive.completed_at)`.
  - `book_usage(conn, rows: Sequence[SpoolUsage], archives: Sequence[Archive], *, actor: str) -> UsageReport`. For each row with `weight_used > 0`, each in its own savepoint (a failing row is logged and skipped):
    - The SB spool is found through `filament_bb_links.bb_spool_id`. With no link, the row is `unlinked` and skipped.
    - A row whose SB spool is archived is skipped and counted neither booked nor unlinked.
    - A row whose `created_at` is not later than the spool's first `bb_spool` event is skipped. It lies inside the anchor the link made.
    - Otherwise, `ledger.record(kind="print_ended", source="print_actual", delta_g=-weight_used, ref_kind=ledger.BB_USAGE, ref_id=str(row.id), at=row.created_at, printer=<SB printer whose bambuddy_printer_id is row.printer_id, else None>, actor=actor, data={"job": print_name, "basis": "bb_usage", "bb_usage_id", "bb_spool_id", "bb_status": status, "bb_archive_id": match_archive(...)})`.
- `read_and_pull`: within one `client_for`, read `spool_rows()` first, then
  `spool_usage()`, back to back. This order matters: a deduction between the two reads
  then shows in the usage rows, never only in a spool's value. After the rebase onto
  #6262, `spool_catalog()` comes next (preflight D10). When `unbooked(...)` is not empty
  (its own failure is logged and reads no archives), also read `archives(limit=50)`. Each optional read fails alone. Then run `pull(...)`,
  then `book_usage(...)` in one `store.spools.transaction()`. Set the report's
  `usage_booked` and `usage_unlinked`, and log `usage_booked` per pull (preflight C15). A
  failure of the usage read or its booking is logged and leaves the spool pull's report
  standing.
- After the rebase onto #6262: the booking runs only when `filament_sync.spools` lets the
  pull run (preflight D14). It runs inside the pull, so it adds no check of its own.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_usage_row_of_a_print_sb_did_not_start_is_a_print_end(pg_pool)`: a linked spool (first BB event at t0), a row at t1 > t0 with 75.1 g, and an archive of the same printer completed at t1 + 0.9 s → one `print_ended` with `delta_g == -75.1`, `ref_kind == "bb_usage"`, `data.bb_archive_id` set, `at == t1`.
  - `test_a_naive_archive_time_matches`: the archive's `completed_at` naive, the row's aware → matched, no `TypeError`.
  - `test_a_usage_row_is_booked_once`: `book_usage` twice → `booked` 1, then 0.
  - `test_a_usage_row_before_the_link_is_not_booked`.
  - `test_a_usage_row_of_an_unlinked_bb_spool_is_counted_unlinked`.
  - `test_bb_reading_and_usage_of_one_write_count_once` (preflight C10): link the BB spool in a first pull at t0. Then pull a second time, with the BB spool's `updated_at` equal to a usage row's `created_at` at t1 > t0, and `weight_used` holding that row → `grams_left == label − weight_used`, not that minus the row.
  - `test_an_sb_plate_and_its_usage_row_count_once`: an SB `print_started`/`print_ended` of job J (`ref_kind` `PRINT_RUN`, through `ledger.record`) at T; a usage row J at T − 1 s, and a BB reading at T − 1 s → `grams_left` equals the BB value.
  - `test_spool_usage_reads_and_skips_a_bad_row` (client test with `httpx.MockTransport`, as the file's other reads do).
  - After the rebase onto #6262: `test_usage_is_not_booked_with_the_pull_off` (`filament_sync = {spools: "off"}` → no `bb_usage` event).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Rewrite the `bambuddy_pull.py` docstring's last paragraph:
  the reading sits at BB's `updated_at`, and BB's usage rows sit at their `created_at`,
  on the same instant as the deduction. SB's own plates pair with them by job (`ledger.py`).
- [ ] **Step 4: Run** `tests/filament tests/bambuddy` → PASS.
- [ ] **Step 5: Commit** `feat(filament): book Bambuddy's usage rows as print ends (#6263)`.

## Part 2 — after #6262 merges

Start only once #6262 is on main, and rebase Part 1 onto it first. Each task below names
the #6262 code it uses. Check that each name exists on main before you write the first
test. If one is missing or differs, stop and report.

### Task 5: A plate's end is booked at the report that shows it

**Files:**
- Create: `backend/scadbuddy/filament/prints.py`
- Modify: `backend/scadbuddy/filament/print_uses.py` (#6262's: `record_uses` writes `data={"job": subtask}`; `settle_uses_in` books through `prints`; its docstrings drop the plate-start rule for ends)
- Modify: `backend/scadbuddy/printers/reports.py` (`advance_in`, and the module docstring)
- Test: `backend/tests/filament/test_prints.py` (new), `backend/tests/filament/test_print_uses.py` (#6262's)

**Interfaces:**
- Consumes from #6262: `filament/print_uses.py`
  `record_uses(pool, run_id: str, subtask: str, uses: Sequence[SlotUse]) -> int`,
  `settle_uses(pool, run_id: str, stages: Sequence[str]) -> bool`,
  `settle_uses_in(conn, run_id, stages) -> bool`,
  `SlotUse(slot_id: int, spool_id: UUID, grams: float, plate_started_at: datetime)`, and
  `print_use_hook(uses: PrintUses) -> ReadHook` (no recache, so preflight C16 is moot).
  The `print_started` event stays at `plate_started_at`.
- Consumes from Part 1: `ledger.PRINT_RUN`; Task 4's index.
- Produces in `filament/prints.py`:
  - `@dataclass(frozen=True) OpenUse(spool_id: UUID, ref_id: str, delta_g: float | None, job: str)`.
  - `open_for_run(conn, run_id: str) -> list[OpenUse]`: the lookup that `settle_uses_in` runs today, moved here.
  - `open_for_job(conn, job: str) -> list[OpenUse]`: `WHERE kind = 'print_started' AND ref_kind = 'print_run' AND (data ->> 'job') = %s`, as Task 4's index has it, and no `print_ended` with the same `ref_id`.
  - `book_end(conn, use: OpenUse, *, applied: bool, at: datetime | None, actor: str, ended: str) -> m.SpoolEvent | None`: `print_ended`, `print_actual`, `delta_g = use.delta_g if applied else 0.0`, `ref_kind=ledger.PRINT_RUN`, `data={"basis": "sliced" | "not_applied", "job": use.job, "ended": ended}`. `ended` is `"report"` or `"settle"`.
  - `plate_ended(conn, subtask_name: str, state: str, at: datetime) -> None`: books every `open_for_job(subtask_name)` with `applied = state == "FINISH"`, `at=at`, `actor="system:printer-watch"`, `ended="report"`.
  - `FILAMENT_CAPTURE_FAILED = "filament capture failed; the report is written"` (log message; Task 6 uses it too).
- `settle_uses_in` keeps #6262's signature and the meaning of its answer. It books each
  `open_for_run` use with `at=None` (the clock, by the ledger's guard) and
  `ended="settle"`.
- `advance_in`: after a write that answers `ended` with `ended.timed` true, it calls
  `plate_ended(conn, ended.subtask_name, report["gcode_state"], seen_at)` inside
  `with conn.transaction():`. An untimed end (moved on, `IDLE`) books nothing; the
  settle books it (preflight C2). An exception is caught, logged with
  `FILAMENT_CAPTURE_FAILED` (`extra`: `printer_id`, `what="plate_end"`, the error's type),
  and dropped. The answer (`Advance`) is unchanged. `PrintEnd` and `EndedPrint` get no
  new field, so an older print worker still reads the answer.

- [ ] **Step 1: Write the failing tests:**
  - `test_a_plate_ends_at_the_report_that_shows_it(pg_pool)` (`test_prints.py`): an SB spool; `print_uses.record_uses(pool, "r1", "scadbuddy-r1-p1", [SlotUse(slot_id=0, spool_id=S, grams=40.0, plate_started_at=T0)])`; `PrinterReportStore.advance_in` with a running report, then one with `gcode_state="FINISH"` at `seen_at=T` → one `print_ended` event, `at == T`, `delta_g == -40`, `data.job == "scadbuddy-r1-p1"`, `data.ended == "report"`.
  - `test_a_failed_plate_books_no_grams`: as above with `FAILED` → `delta_g == 0`, `basis == "not_applied"`.
  - `test_a_moved_on_end_is_left_to_the_settle`: the next report names another `subtask_name` → no `print_ended`; then `settle_uses(pool, "r1", ["done"])` books −40 with `data.ended == "settle"`.
  - `test_the_settle_books_only_what_the_report_did_not` (`test_print_uses.py`): the report books the end; then `settle_uses(pool, "r1", ["done"])` → no second event. Without the report end, the settle books at the clock time with `data.ended == "settle"`.
  - `test_a_plate_end_capture_that_raises_still_writes_the_report` (Review Focus 3): monkeypatch `prints.plate_ended` to raise → the report row holds the new report, `Advance.ended` is returned, and the log has `FILAMENT_CAPTURE_FAILED`.
  - Update #6262's tests that asserted a settle's `print_ended` at `plate_started_at`.
- [ ] **Step 2: Run** `tests/filament/test_prints.py tests/filament/test_print_uses.py tests/printers/test_watch.py` → the new tests FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** them, `tests/api/test_crud_print*.py`, `tests/crud` and `tests/filament` → PASS.
- [ ] **Step 5: Commit** `feat(filament): book a plate's end when the printer reports it (#6263)`.

### Task 6: Tray changes write loaded, unloaded and AMS readings at once

**Files:**
- Create: `backend/scadbuddy/filament/capture.py`
- Modify: `backend/scadbuddy/filament/inventory.py` (#6262's: a conn-level resolver that `FilamentInventory.trays` and capture both call; `place_on`, which `link` calls too)
- Modify: `backend/scadbuddy/filament/locations.py` (`ensure_ams_on`, `move_on`; the pool methods call them; #6262's tray-place clearing moves into `move_on`)
- Modify: `backend/scadbuddy/spools/models.py` (`SeenTray.remain: int | None = None`), `backend/scadbuddy/bambuddy/trays.py` (`seen_trays` fills `remain`)
- Modify: `backend/scadbuddy/printers/status.py` (`RUNNING` moves here), `backend/scadbuddy/printers/reports.py` (re-exports `RUNNING`; `advance_in` reads the old `report` and calls capture), `backend/scadbuddy/printers/store.py` (`PrinterStore.read_on`)
- Test: `backend/tests/filament/test_capture.py` (new), `backend/tests/filament/test_locations.py`, `backend/tests/filament/test_inventory_trays.py` (#6262's)

**Interfaces:**
- Consumes from #6262: `filament/inventory.py` `tray_fingerprint(material: str | None, colour: str | None) -> str` (`MATERIAL|#RRGGBBAA`, alpha included), `TrayHolder(ams_id, tray_id, spool: Spool | None, found_by: Literal["tag", "link"] | None)`, `FilamentInventory.trays(printer_id: UUID, seen: Sequence[SeenTray]) -> TrayView` and `FilamentInventory.link(...)`; `filament_spools.tray_fingerprint` (#6262's `tray_place` migration); `LocationStore.move`, which clears the tray place.
- Consumes from Task 5: `prints.plate_ended`, `prints.open_for_job`, `prints.FILAMENT_CAPTURE_FAILED`.
- Produces in `inventory.py`:
  - `@staticmethod FilamentInventory.holder_on(conn, printer_id: UUID, seen: SeenTray) -> TrayHolder`. `trays` calls it for each tray. Order: an open binding by `tray_uuid`, then `tag_uid` (`found_by="tag"`). Else, for a tray with no keys *or with keys that no binding holds* (preflight C5), the unarchived spool placed at `(printer, ams, tray)` whose `tray_fingerprint` equals `tray_fingerprint(seen.material, seen.colour)` (`found_by="link"`). Else none. This covers the AMS rescan of Review Focus 2.
  - `@staticmethod place_on(conn, spool_id: UUID, location_id: UUID, seen: SeenTray, *, actor: str, at: datetime | None = None) -> m.SpoolEvent | None`: `move_on`, then it sets `tray_fingerprint`. `FilamentInventory.link` calls it (preflight D6).
- Produces in `locations.py`: `@staticmethod ensure_ams_on(conn, printer_id: UUID, ams_id: int, tray_id: int, *, label: str) -> m.Location` and `@staticmethod move_on(conn, spool_id: UUID, location_id: UUID | None, *, actor: str, at: datetime | None = None) -> m.SpoolEvent | None`. Same rules as the pool methods. `move_on` clears `tray_fingerprint`, as #6262's `move` does, and books `moved` at `at` (preflight A10, C18).
- Produces in `printers/status.py`: `RUNNING = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})`. `reports.py` and `progress.py` import it from there; `reports.py` re-exports it (preflight C3).
- Produces in `printers/store.py`: `@staticmethod read_on(conn, printer_id: str) -> PrinterRow | None`.
- `seen_trays` sets `remain` only when the tray carries RFID keys, is not an external
  holder, has `"remain" in tray.model_fields_set` and `tray.remain >= 0`; else `None`
  (preflight C4). `AmsTray` itself is unchanged.
- Produces in `capture.py` (it never imports `printers.reports`):
  - `tray_projection(report: Mapping[str, Any]) -> tuple[tuple[Any, ...], ...]`: the AMS unit ids, and per tray `(ams_id, tray_id, tray_uuid, tag_uid, material, colour, remain)`, from `ams.ams[].tray[]`, `vt_tray` and `vir_slot`. Humidity and temperature are left out (preflight C19).
  - `trays_changed(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> bool`: the two projections differ.
  - `WATCH_ACTOR = "system:printer-watch"`, `BACKSTOP_ACTOR = "system:capture-backstop"`;
  - `@dataclass CaptureResult(loaded: int = 0, unloaded: int = 0, readings: int = 0)`, with `__add__`;
  - `touched(conn, printer_id: str, report: Mapping[str, Any], ended_job: str | None) -> list[UUID]`: the spools of `open_for_job(ended_job)`, the spools at this printer's tray locations, and each reported tray's `holder_on` spool, sorted by id.
  - `lock(conn, spool_ids: Sequence[UUID]) -> None`: `SELECT 1 FROM filament_spools WHERE id = ANY(%s) ORDER BY id FOR UPDATE`.
  - `reconcile(conn, printer_id: str, report: Mapping[str, Any], seen_at: datetime, *, actor: str) -> CaptureResult`. It reads the printer row (`read_on`) and the trays (`seen_trays(status_from_report(...))`). It walks the trays of each AMS unit the report shows: the ids in that unit's `tray[]`, joined with this printer's existing tray locations of that unit, so an emptied tray is still visited (preflight B6). It also walks the external holders when `vt_tray` or `vir_slot` is present. For each tray:
    - `current` = the unarchived spools whose `location_id` is the tray's AMS location (`ensure_ams_on`, label `"<printer name> AMS <n> slot <m>"`, the external holder `"<printer name> external"`).
    - `holder` = `holder_on(...)` for a filled tray, else none.
    - If the holder's spool is not in `current`: each spool in `current` gets `unloaded` (printer, `ams_id`, `tray_id`, `data={"by": found_by}`, `at=seen_at`) and moves back (`move_on(..., at=seen_at)`) to the `from` of the newest `moved` event whose `to` is this location, else to `None`. The holder's spool then gets `loaded` (`at=seen_at`) and `place_on(..., at=seen_at)`.
    - A filled tray whose holder has a spool and whose `remain` is not `None` gets an `ams_reading` (`source="ams_remain"`, `at=seen_at`, `data={"remain_pct": remain, "printing": gcode_state in RUNNING, "job": subtask_name if printing else None}`). It is booked when the spool was just loaded, or when its newest `ams_reading` has another `remain_pct` or another tray.
    - Trays of an AMS unit the report does not show are left alone (Review Focus 1).
- `advance_in` selects `report` beside `running_subtask` and `seen_at`. After a written
  write it runs, in one savepoint: `lock(conn, touched(...))`; then the plate end
  (Task 5, timed ends only); then, when `trays_changed(old_report, report)` or the old row
  is missing, `reconcile(conn, printer_id, report, seen_at, actor=WATCH_ACTOR)`. The order
  is fixed: plate end first, then trays (preflight A13, C17). A failure is logged with
  `FILAMENT_CAPTURE_FAILED` (`what="plate_end"` or `"trays"`) and dropped. A stale write
  returns before this, as it does today.

- [ ] **Step 1: Write the failing tests** (`test_capture.py`, with a LAN printer row and the report fixtures of `tests/printers/fixtures`):
  - `test_a_tagged_spool_loads_and_reads(pg_pool)`: a spool bound to tray uuid U. A report with AMS 0 tray 1 carrying U, `remain=80` → events `loaded`, `moved` (to the tray's location, `at == seen_at`) and `ams_reading` with `remain_pct == 80`. The spool's `location_id` is the tray's location, and its `tray_fingerprint` is set.
  - `test_a_swap_unloads_the_old_spool_and_loads_the_new`: A loaded in tray 1 from shelf S; the next report shows B's tag → A gets `unloaded` and moves back to S, and its `tray_fingerprint` is cleared; B gets `loaded`.
  - `test_an_emptied_tray_unloads`: the tray is no longer filled → `unloaded`; no reading.
  - `test_a_partial_report_unloads_nothing` (Review Focus 1): a report with no `ams` key → `CaptureResult()` and no events.
  - `test_a_rescan_with_no_keys_keeps_the_spool` (Review Focus 2): keys go to zeros with the same material and RGBA → no event.
  - `test_unbound_keys_keep_the_placed_spool` (preflight C5): a spool placed at the tray by `link`, and a report whose tray carries keys no binding holds, with the same fingerprint → no event.
  - `test_a_translucent_spool_is_not_an_opaque_trays_spool` (preflight C6): placed at `#FFFFFF80`, the tray now shows `#FFFFFFFF` → `unloaded`.
  - `test_the_same_remain_books_one_reading`: two reports, `remain` 80 then 80 → one `ams_reading`; then 79 → a second one.
  - `test_no_reading_without_a_remain` (preflight C4): an external holder with `remain: 0`, and an AMS tray whose report has no `remain` key → no `ams_reading`.
  - `test_a_reading_while_printing_is_marked`: `gcode_state="RUNNING"` → `data.printing is True`, and `grams_left` stays as before.
  - `test_humidity_alone_runs_no_reconcile` (preflight C19): two reports that differ only in an AMS unit's humidity → `trays_changed` is false.
  - `test_a_resent_report_books_nothing` (Review Focus 4): `advance_in` twice with the same report and `seen_at` → the events of the first write only.
  - `test_a_tray_capture_that_raises_still_writes_the_report` (Review Focus 3): monkeypatch `capture.reconcile` to raise → the row is written, `FILAMENT_CAPTURE_FAILED` is logged with `what="trays"`.
  - `test_move_on_and_ensure_ams_on_match_the_pool_methods` (`test_locations.py`).
  - #6262's `test_inventory_trays.py` still passes on `holder_on`.
- [ ] **Step 2: Run** `tests/filament/test_capture.py tests/filament/test_locations.py tests/filament/test_inventory_trays.py` → the new tests FAIL.
- [ ] **Step 3: Implement.** Extend the `printers/reports.py` module docstring with one paragraph: the write that shows a timed plate end or a tray change books the ledger events in its own savepoint (Tasks 5 and 6).
- [ ] **Step 4: Run** them, `tests/printers tests/filament tests/bambuddy/test_trays*.py tests/api/test_crud_print*.py tests/api/test_tray_spools.py` → PASS.
- [ ] **Step 5: Commit** `feat(filament): tray changes write loaded, unloaded and AMS readings (#6263)`.

### Task 8: SpoolRefresh's capture backstop

**Files:**
- Modify: `backend/scadbuddy/filament/capture.py` (`reconcile_all`), `backend/scadbuddy/spools/activities.py` (`capture_backstop_activity`), `backend/scadbuddy/workflows/spool_refresh.py`, `backend/scadbuddy/main.py` (register it beside `filament_pull_activity`)
- Create: `backend/tests/fixtures/spool_refresh_histories/capture.json` (recorded, never edited by hand)
- Test: `backend/tests/test_spool_refresh_replay.py`, `backend/tests/test_spool_refresh_workflow.py`, `backend/tests/filament/test_capture.py`

**Interfaces:**
- Consumes from #6262: the `workflow.patched("spool-providers-gone")` branch of
  `SpoolRefresh.run`, which guards `LIST_ACTIVITY`, `PROVIDER_ACTIVITY` and
  `SEEN_ACTIVITY` (each a registered no-op); its replay histories and `test_record` stubs.
- Consumes: `capture.reconcile`, `capture.touched`, `capture.lock`, `BACKSTOP_ACTOR`; `Printers` (`printers/component.py` `PRINTERS`); `is_fresh` (`printers/status.py`).
- Produces: `reconcile_all(pool: ConnectionPool[Conn], printer_ids: Sequence[str], *, now: datetime) -> CaptureResult`. For each printer, in its own transaction, it reads `SELECT report, seen_at FROM printer_reports WHERE printer_id = %s FOR UPDATE`. It skips a report that is not `is_fresh(seen_at, now)`. Otherwise it runs `lock(conn, touched(conn, id, report, None))`, then `reconcile(conn, id, report, seen_at, actor=BACKSTOP_ACTOR)` on the row it read under that lock (preflight A14, C7). A printer that raises is logged and skipped.
- Produces: `capture_backstop_activity(filament: Filament, printers: Printers) -> Callable[[], Any]`, activity name `filament_capture_backstop`. It returns `"captured"` or `"failed"`, and never raises. Its docstring names it the successor of `spool_trays_seen`, a no-op since #6262 (preflight D9).
- Produces in `spool_refresh.py`: `CAPTURE_ACTIVITY = "filament_capture_backstop"`,
  `CAPTURE_PATCH = "filament-capture-backstop"`, `CAPTURE_TIMEOUT = timedelta(minutes=2)`.
  The step runs after the `FILAMENT_PULL_PATCH` step, inside `if workflow.patched(CAPTURE_PATCH):`,
  with `_ONCE`. It runs on every branch of `spool-providers-gone`, and nothing returns
  before it. If #6262's code returns early, move that return after this step: it then
  issues the same commands as before up to the new step (preflight D8). An
  `ActivityError` is logged ("could not reconcile the printers' trays with the ledger")
  and the run goes on. Add one sentence to the module docstring.

- [ ] **Step 1: Write the failing tests:**
  - `test_the_backstop_books_a_change_the_watch_missed` (`test_capture.py`): write a report row directly (no capture) with a tagged spool in a tray → `reconcile_all` books `loaded`; a second call books nothing.
  - `test_a_stale_report_is_left_to_the_watch`: a report older than `STALE_AFTER` → nothing.
  - In `test_spool_refresh_workflow.py`, the run test gains a `filament_capture_backstop` stub. The run calls it once, after `filament_bambuddy_pull`, with and without `spool-providers-gone`. A raising stub still completes the run.
  - Add `"capture"` to the replay test's parameter list and to `test_record`'s. Add to `test_record` a `CAPTURE_ACTIVITY` stub that returns `"captured"` (preflight B8).
- [ ] **Step 2: Run** → FAIL (no `capture.json`, no step).
- [ ] **Step 3: Implement**, then record the history on the merged code, so it carries
  both `spool-providers-gone` and `filament-capture-backstop`:
  `SCADBUDDY_RECORD_HISTORIES=1 uv run --frozen pytest tests/test_spool_refresh_replay.py -k "test_record and capture"`.
  Check the WSL clock first (Global Constraints). Commit `capture.json` as recorded.
  Keep every existing history file and parametrize entry, #6262's included.
- [ ] **Step 4: Run** `tests/test_spool_refresh_replay.py tests/test_spool_refresh_workflow.py tests/filament/test_capture.py` → PASS. Every history replays.
- [ ] **Step 5: Commit** `feat(filament): reconcile trays with the ledger on the spool refresh (#6263)`.

## Last

### Task 9: Notes, whole-branch check and PR

**Files:**
- Modify: `backend/CLAUDE.md`, this plan ("Decisions made while building")

- [ ] **Step 1: `backend/CLAUDE.md`.** In the `backend/scadbuddy/filament/` line, add
  `prints.py`, `capture.py` and `bambuddy_usage.py` in one clause each, beside #6262's
  `inventory.py`, `presets.py` and `print_uses.py`. Change "ordered by `(at, seq)`" to the
  new order.
- [ ] **Step 2: Run every check:** `uv run --frozen pytest -n auto tests/filament tests/spools tests/printers tests/bambuddy tests/crud tests/api/test_crud_print*.py tests/api/test_filament.py tests/api/test_tray_spools.py tests/test_pg_migrations.py tests/test_spool_refresh_replay.py tests/test_spool_refresh_workflow.py tests/api/test_openapi.py`; ruff; mypy. `grep -rn "plate's start\|at its plate's start" backend/scadbuddy docs/superpowers/specs/2026-11-14-filament-management-design.md` finds no stale prose. The grep is for prose, so #6262's identifier `plate_started_at` does not match it.
- [ ] **Step 3: Fill "Decisions made while building (#6263)"** below with each ruling and what it costs if wrong.
- [ ] **Step 4: Open the PR** `feat(filament): capture tray changes and print ends (#6263)`. The body says `Closes #6263`, carries the session marker and the footer, and says this: the first pull after deploy books up to 200 historic BB usage rows on linked spools, and paired historic plates then take BB's grams instead of SB's sliced grams (preflight C15). Shepherd it to green and a clean review.

## Decisions made while building (#6263)

- Task 1 (measured 2026-11-15, spec §8.2): an archive ends 0.085 to 17.4 s after its usage rows (n 379), never before. Task 7 matches an archive inside `[created_at, created_at + 30 s]` (`ARCHIVE_BEFORE = 0`, `ARCHIVE_AFTER = 30 s`), not the planned 10 s. Cost if wrong: a slower failure leaves `bb_archive_id` null; the plate's grams still count.
- Task 1: BB and SB clocks agree within about 0.25 s (BB − SB +0.011 s). Booked times are not corrected for skew.
- Task 1 kept its clock-skew step (Date headers, read-only), although the controller first asked to re-measure only the window: issue item 6 needs the skew, and the earlier probe had one sample.
- The owner judged the earlier local exposure of the BB API key acceptable and chose not to rotate it (2026-11-15).
- Task 4: the backfill equals `ledger.job_of` for a subtask that contains `/` (`run/a/b/slot` gives `a/b`); `split_part(ref_id, '/', 2)` would cut it to `a`. It also treats a non-string `data.job` as absent, as `job_of` does.
- Task 7: a usage row with no `print_name` is booked with `data.job` null. SB's own plates always carry a subtask name with the run id, so a nameless row is never an SB plate and pairs with nothing. Cost if wrong: an SB plate whose BB row lost its name counts twice.
- Task 7 (controller ruling, review round 1): a usage row whose SB spool is archived is not booked and is counted neither booked nor unlinked, the same as the pull, which never writes to an archived spool. Cost if wrong: a spool restored from the archive misses the grams of the prints BB booked while it was archived, until its next reading.
- Task 7 (re-review): `unbooked` runs before `pull()`, so a spool first linked in this pull has no link yet and its rows do not trigger the archive read; `book_usage`, after `pull()`, can then book such a row with `bb_archive_id` null. Accepted: only the archive id is lost, not grams, and the next pull cannot recover it because the row is booked.
- From #6262 Task 7 (re-review 2): `filament_bb_links` keeps the slicer preset Bambuddy last sent (`bb_slicer_filament`, `bb_slicer_filament_name`). An SB→BB push of a preset must write those two columns too, or the next pull reads SB's own pushed value as a Bambuddy change.
- Merge of main (#6262, PR #6550): one walk. `ledger.basis` holds all of #6263's rules (the delta-first order at one instant, the paired-plate rule, the anchor's own clock) and returns `GramsBasis(anchor, deltas)`. `grams_left` calls `basis` and takes the deltas off the anchor, so the UI's anchor and deltas agree with `grams_left` by construction (D1). `basis` sorts by `(at, delta before reading, seq)`: #6262's `seq` breaks the ties that were insert order. A paired job's `bb_usage` event is in `GramsBasis.deltas`; its skipped `print_run` events are not. Cost if wrong: the UI's basis and `grams_left` could differ; `test_basis_matches_grams_left` pins them.
- Merge of main: the `ref_kind` constants live once in `ledger.py`, `Final`-typed: `PRINT_RUN`, `PRINT_STARTED`, `PRINT_ENDED`, `PRINT_ESTIMATE`, `PRINT_ACTUAL` (#6262 Task 6; the same meaning as #6263's) and `BB_USAGE`, `BB_SPOOL`, `BB_REFS` (#6263). #6263's second `PRINT_RUN` definition is gone.
- Merge of main: `20261115T0706Z_filament_capture.sql` sorts before 17 files that main added. It depends only on `filament_spool_events` (`20261114T1301Z`, `20261115T0029Z`). No later main file writes a `print_run` or `bb_scale` event (`20261116T0547Z_filament_carry_tray_links.sql` writes `moved` events only), so the order is safe.
- Merge of main: the pull's gate is #6262's `filament_sync.spools` (`scheduled_pull`'s `sync`); the plan's earlier name for a separate pull setting never existed. The usage booking runs inside `read_and_pull`, so `spools` "off" stops it too (D14 holds; #6265 splits it).
- Task 5: an end no report timed (moved on, `IDLE`, a missed `FINISH`) is booked at the settle's clock time, as spec §5 "known limits" says (an upper bound). A reading after the true end and before the settle then counts the plate once more; keeping the end at the plate's start would instead never count a plate that a mid-print Bambuddy reading did not yet reflect (Bambuddy deducts at the end). Pinned by `test_an_untimed_end_with_a_reading_before_the_settle_is_the_accepted_upper_bound`. Cost if wrong: an over-count of one plate in that window. The `job` fallback in `prints._open` stays for starts an older print worker books without `data.job` during a rolling deploy.
- Task 6: keys no binding holds fall through to the placed-spool rule (preflight C5, "One tray resolver"), so #6262's `test_a_link_on_a_tray_that_gained_an_unbound_tag_holds_nothing` became `..._still_holds`. Cost if wrong: the print dialog keeps a placed spool for a tagged tray whose tag no spool holds.
- Task 6: a tray place is named by one helper, `bambuddy/trays.py` `place_label` (`"<printer> <tray_label>"`, the external slot "External slot n"), for capture and `link` alike; it matches #6262's carry migration, and places are found by (printer, ams, tray), never by name. The brief's "AMS n slot m" would have made a second naming.
- Task 6: the tray projection includes each tray's `state` and the AMS's `tray_exist_bits`, so a pulled AMS-HT spool whose `tray_type` stays stale still unloads. A report whose projection is unchanged costs no SQL (per-tick AMS readings stay incremental); a state flicker costs one extra reconcile. `ams_reading` is booked only when the percent or the tray differs from the spool's newest reading.
- Task 6: the report write runs the lock, the plate end and the trays each in its own savepoint; any failure logs `FILAMENT_CAPTURE_FAILED` with `what` and never loses the report.
- Task 8: the capture backstop is not gated by `filament_sync.spools`: it reconciles SB's own trays from the stored printer reports, not a Bambuddy pull. It skips a printer whose report is missing or older than 90 s (`is_fresh`), so a stale report never unloads a spool. It runs in the API process's library worker, which already holds the pool (#5295: no new client). `capture_backstop_activity` takes only `printers`; the brief's unused `filament` argument is dropped (a factory argument, so replay is unaffected).
- Final review I1: `reconcile` compares each tray with a snapshot taken before the tray loop, so a spool moved to a lower tray was unloaded again by its old, higher tray. It now remembers the spools it loaded in the pass and does not unload those. Only a tagged spool can move trays this way: a fingerprint spool is found only where it is placed, so a fingerprint spool shown in another tray is correctly an unload plus an unknown tray. Cost if wrong: a false `unloaded` event and a wrong place after a slot reorder, healed at the next backstop for a tagged spool.
- Final review I2: `book_usage` locks the spools it books first, in id order, with `capture.lock` (one statement), before it books any row; the report write, the backstop, the settle and the pull now share one lock order. Cost if wrong: a deadlock that the per-row savepoint or the report's `lock` step catches and the next tick retries.
- Final review M1, M3: `spool_overlay_refresh_seconds` 0 also stops the capture backstop (same Schedule); the text says "all three". `filament_spool_events (ref_kind, ref_id)` serves `unbooked` (migration `20261117T2016Z`; EXPLAIN uses it).
- Final review M2, M4 (no code change): backstop readings are stamped at `printer_reports.seen_at`, which `touch` also advances. This is harmless for a `printing` reading (it anchors nothing), and no separate report-time column exists. A BB-queue plate's non-printing reading booked just before BB's `created_at` counts twice until the next reading (spec §5.5 names it); the first-pull PR note carries it.
- Final re-review (accepted): `bambuddy_usage._links` reads a spool's archived state and link before `capture.lock`, so a spool archived or unlinked in that window (milliseconds) still gets one booking, the same outcome as a booking just before the archive. The I2 test checks the lock call and its order, not that it precedes the first booking. Prod's `filament_spool_events` held 532 rows (576 kB) on 2026-11-17, so `20261117T2016Z`'s plain `CREATE INDEX` blocks writes for milliseconds.
- Review of #6590 (round 1, blocking): a tray that is loading or scanning (SPOOL bit set, STEADY clear) is "no news": its spools, bindings and readings are left alone. Only a really empty tray unloads.
- Review of #6590 (round 2): `prints.open_for_job` and `plate_ended` are scoped to the event's printer through `print_runs.printer`; a run with no printer still matches.
- Review of #6590 (non-blocking): two items were deferred to #6598: pairing a plate across the spools of one tray (`ledger.basis`), and a time-bounded archive read in `read_and_pull`. #6598's PR fixes the archive read: it reads the archives made from the day before the oldest usage row it books (`ARCHIVE_LEAD`), paged as `bambuddy/linking.py` pages them, instead of `archives(limit=50)`. Neither event keeps the tray, so the cross-spool pairing is built on the printer and the job (ruling on #6598): `bambuddy_usage._cover`, as each usage row is booked, in that row's savepoint (a failed cover rolls the booking back and the next pull retries both), books a `print_ended` (`print_actual`, 0 g, the estimate's ref, `data.covered_by` the usage event) on the one other spool that has an open estimate of the row's job on that printer, when the row's own spool has no estimate of the job and no other usage row of the job is unpaired. Any other shape changes nothing and logs `filament.usage.unpaired`; zero candidates (a Bambuddy-queue print) is silent. `ledger.basis` stays a per-spool function. Cost if wrong: a two-slot job whose slots both moved spools in one print counts twice, as before.
- A backend pytest shard timeout on #6590 was runner variance, tracked by #6596.
