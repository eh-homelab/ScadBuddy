# Phase 3a: housekeeping as a Temporal Schedule — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **As built (PR #1095):** the sections below are corrected to what shipped; the spec's §10
> "As built so far (3a)" note is the summary. Changed from the first draft: two Schedules with
> queue-suffixed ids, not one `scadbuddy-housekeeping`; a failing sweep fails its activity
> instead of returning; per-sweep timeouts with heartbeats, and a run-level
> `execution_timeout`; and the boot no longer sweeps the uploads itself, since the trigger
> does.

**Goal:** Replace the API process's periodic housekeeping loops with Temporal Schedules.
The loops are `main._asset_sweeper` (unused uploads, unreferenced blobs, duplicate staging)
and `RenderService._prune_forever` (settled render jobs). Two Schedules start a `Housekeeping`
workflow on the `library` queue, served in the API process. That process holds the data
volume, which is RWO:
- `scadbuddy-housekeeping-<queue>` runs every sweep every `asset_sweep_interval`.
- `scadbuddy-prune-<queue>` runs only the prune every 300 s, the old loop's cadence. It stays
  when the interval is `0`.

**Architecture:**
- `Housekeeping.run()` executes one activity per sweep, in today's order: prune jobs, sweep
  assets, sweep blobs, sweep duplicate staging.
- Each activity logs a failure and re-raises it, so its activity fails and Temporal's UI shows
  it. The workflow catches `ActivityError` per sweep and returns the failed sweeps, so one
  sweep that fails never stops the rest.
- Timeouts: the prune has a 2-minute `start_to_close`. The other sweeps have 30 minutes and
  heartbeat, with a 1-minute heartbeat timeout. Each run has an `execution_timeout`
  (`housekeeping_timeout`), so a run stuck on replay cannot hold back the `SKIP` overlap.
- Once its worker connects, the API creates both Schedules, or updates them, with overlap
  `SKIP`. An interval of `0` deletes the sweeps' one. A paused Schedule keeps its state and is
  not triggered.
- A `library` worker runs in the API process, beside the `bambuddy` one, with the same
  reconnect loop.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §4.3
(`library` row) and §4.4 ("asset/blob/staging sweeper → Temporal Schedules on `library`").
This is phase 3's first plan. The library commands themselves (git, files, downloads) are
3b onward, one plan per route group, as §10 allows. Stacked on PR #1091.

## Rulings

1. **The boot passes in `_prepare_catalogue` stay in the boot.** They must run before the first
   request is served (stranded claims, tombstones, orphans, staging), which a Schedule trigger
   cannot promise. The spec's "the boot sweeps run once more as a schedule trigger at deploy"
   is met by `trigger()` on the Schedule at boot, with `BUFFER_ONE` so it queues behind a run
   still open. That trigger is the start's only asset sweep: the boot no longer sweeps the
   uploads itself (review #1095 1). While the Schedule is paused, the start backfills the
   uploads to the store itself (review #1095 2).
2. **The render prune joins the Schedule.** It is a periodic pass over Postgres and the volume
   (`prune_revision_exports`), like the others. `RenderService` keeps `prune()` and loses its
   loop.
3. **`library` is a new setting**, `temporal_task_queue_library` (default `"library"`), paired
   with the Temporal address like the other queues.
4. **The interval is read at boot.** `asset_sweep_interval` is a deployment setting (an env
   var), so a change already needs a restart.

## Global Constraints

- Schedule ids `scadbuddy-housekeeping-<queue>` and `scadbuddy-prune-<queue>`
  (`schedule_id_for`, `prune_schedule_id_for`). Overlap policy `SKIP`.
- Never commit `backend/openapi.json` or a `schema.d.ts`. Commit with `git add <paths>`.

## Review Focus

1. Temporal down at boot must not stop the API from starting. The Schedule is set up by the
   `library` worker task once it connects.
2. Setting `asset_sweep_interval=0` must leave no Schedule running.
3. One failing sweep must not skip the sweeps after it.

---

### Task 1: `Housekeeping`, its activities and the Schedule

**Files:**
- Create: `backend/scadbuddy/workflows/housekeeping.py` (`Housekeeping`, `schedule_id_for`,
  `prune_schedule_id_for`, `ensure_schedule`, `ensure_schedules(client, task_queue, interval)`)
- Modify: `backend/scadbuddy/main.py`:
  - `_housekeeping_activities(state)` holds the four sweep functions as activities.
  - `_run_library_worker` follows `_run_print_worker` and calls `ensure_schedules` once
    connected.
  - Delete `_asset_sweeper`.
- Modify: `backend/scadbuddy/render/submit.py` (delete `_prune_forever`, `_pruner`,
  `prune_interval`)
- Modify: `backend/scadbuddy/core/settings.py` (`temporal_task_queue_library`)
- Test: `backend/tests/test_housekeeping.py`; update the tests that use `prune_interval` or
  `_pruner`.

- [ ] **Step 1: Failing tests** (dev server):
  - `test_housekeeping_runs_every_sweep_in_order` (fake activities).
  - `test_a_failing_sweep_does_not_stop_the_rest`.
  - `test_ensure_schedule_creates_then_updates_the_interval`.
  - `test_an_interval_of_zero_deletes_the_schedule`.
- [ ] **Step 2: Run them**
  - Command: `uv run --frozen pytest tests/test_housekeeping.py -q`
  - Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Each activity is tried once (`RetryPolicy(maximum_attempts=1)`): the next tick retries,
    as the loop did. Its timeouts are the ones under Architecture.
  - The workflow catches `ActivityError` per sweep.
  - `ensure_schedule`:
    1. `create_schedule` with `ScheduleSpec(intervals=[ScheduleIntervalSpec(every=interval)])`
       and `ScheduleOverlapPolicy.SKIP`.
    2. If the Schedule already exists, `update` it instead.
    3. At an interval of `0`, delete it.
    4. Once it exists and is not paused, `trigger()` it with `BUFFER_ONE`.
- [ ] **Step 4: Run them**, plus the tests touched by the deleted loop. Expected: PASS.
- [ ] **Step 5: Commit** `feat(housekeeping): a Temporal Schedule replaces the in-process sweeps (#1054)`.

### Task 2: Docs

- Add a spec §10 "As built (3a)" note.
- Update `CLAUDE.md` for the `library` queue.
- Update the README to name the setting.
- Commit `docs: housekeeping on a Schedule (#1054)`.
