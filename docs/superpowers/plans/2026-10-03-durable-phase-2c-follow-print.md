# Phase 2c: `FollowPrint` replaces the print watcher — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each print is followed by a `FollowPrint` workflow on the `bambuddy` queue
(`follow-print-<output id>`) instead of by `bambuddy/watcher.py`'s in-process tasks. Temporal
keeps the follow across restarts, so the `print_watches` log, the advisory lock and the rescan
are no longer needed.

**Architecture:**
- `FollowPrint.run(output_id)` runs one heartbeating activity, `follow_print`. It contains
  today's `_loop`: read the progress, publish the change through `ProgressObserver`, and back
  off while nothing changes. It returns when the print settles, Bambuddy 404s, the output is
  deleted, or the print has been quiet for longer than `MAX_AGE`.
- The heartbeat details carry `active` (when the print last moved), so a retried attempt keeps
  the age.
- The `poke` signal stands in for today's `watch()` on a followed print. The workflow cancels
  the running attempt and starts a fresh one, which reads now and counts its age from now.
- `PrintRun` starts the workflow after `print_finish` for an output print, as an abandoned
  child, or pokes the one already running.
- The progress route signals-with-start it, as `watcher.watch` did.

**Tech Stack:** Python 3.12, temporalio 1.33, FastAPI, Postgres 17.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §4.4 (print
watcher row) and §5.3. Stacked on PR #1066 (`feat/1053-renders`).

## Rulings

1. **The loop is an activity, not workflow timers.** §4.4 says "polls Bambuddy inside a
   heartbeating activity until the print ends". The back-off is 2 s to 30 s, which is
   Temporal's guidance for polling inside an activity (§3.1).
2. **A poke restarts the activity.** An activity cannot receive a signal. Today's poke means
   "read now, and the age counts from now", which is what a fresh attempt does.
3. **`print_watches` is read at boot, not dropped.** Each boot starts a `FollowPrint`
   for each print recorded within `MAX_AGE` (the prints the old in-process watcher was
   following), and deletes a row once its follow is running; one that did not start waits
   for the next boot. A later migration drops the table once no release writes it, and
   removes the boot pass with it. Cost: one more table for a release. The upgrade needs a
   `Recreate` rollout, since an old pod would still write rows after the new one's pass.
4. **No `workflow.patched` in `PrintRun`.** `PrintRun` is unreleased (#1061 is not merged), so
   no history holds the old commands.
5. **A follow that cannot be started from the progress route is a logged warning.** The read
   that called it still answers. This matches today's `watch()`, which can't fail.

## Global Constraints

- Workflow ID `follow-print-<output_id>`, task queue `settings.temporal_task_queue_bambuddy`
  (the in-process `bambuddy` worker, `main.py` `_run_print_worker`).
- Intervals unchanged: `MIN_INTERVAL` 2 s, `MAX_INTERVAL` 30 s, `ERROR_INTERVAL` 60 s, `MAX_AGE`
  24 h.
- Events carry ids, never content (`core/events.py`).
- Never commit `backend/openapi.json` or a `schema.d.ts`. Commit with `git add <paths>`.

## Review Focus

1. A poke while a read is in flight must not publish the same change twice. The observer dedupes
   by fingerprint.
2. A worker restart mid-follow resumes with the age from the heartbeat, not a fresh 24 h.
3. Two prints of one output in a row give one workflow, poked, not two.
4. A deleted output, or Bambuddy's 404, ends the workflow, and a later print starts a new one
   (`ALLOW_DUPLICATE`).
5. With Temporal unreachable, the progress route still answers its read.

---

### Task 1: The follow loop as an activity

**Files:**
- Create: `backend/scadbuddy/bambuddy/follow.py` (`Follower`, `follow_print` activity,
  `FollowInput`)
- Test: `backend/tests/bambuddy/test_follow.py` (the loop's cases, ported from
  `tests/bambuddy/test_watcher.py`)

**Interfaces:**
- Produces:
  - `FollowInput(output_id: str, fresh: bool = False)`.
  - `Follower(outputs, observer, read, events, *, min_interval, max_interval, error_interval,
    max_age, now, heartbeat)`, with `async def follow(self, output_id: str, active: datetime)
    -> str`. It returns why it stopped: `"settled" | "gone" | "deleted" | "quiet"`.
  - `@activity.defn(name="follow_print") async def follow_print(input: FollowInput) -> str`, a
    bound method of `FollowActivities(follower)`. Its `active` is the attempt's heartbeat
    detail, or now.

- [ ] **Step 1: Failing tests.** Port, each against `Follower.follow` with a `Script` reader
  and fast intervals:
  - `follows_a_print_until_it_settles_publishing_each_change_once`
  - `backs_off_while_nothing_changes`
  - `a_failure_is_announced_once_and_the_follow_carries_on`
  - `a_print_bambuddy_no_longer_has_ends_it` (returns `gone`)
  - `an_unexpected_error_does_not_end_it`
  - `a_long_print_that_keeps_moving_is_followed_past_the_age_limit`
  - `a_quiet_print_is_given_up_on` (returns `quiet`)
  - `a_deleted_output_ends_it` (returns `deleted`)
  - `an_output_read_blip_does_not_end_it`
  - `the_real_read_follows_a_failed_print_to_settled`

  Add `the_activity_resumes_the_age_from_its_heartbeat`: run `follow_print` under
  `temporalio.testing.ActivityEnvironment` with heartbeat details `[active]`, and check it gives
  up as quiet sooner than a fresh start would.
- [ ] **Step 2: Run them**
  - Command: `uv run --frozen pytest tests/bambuddy/test_follow.py -q`
  - Expected: FAIL (`follow` is not defined).
- [ ] **Step 3: Implement.**
  - `Follower.follow` is `PrintWatcher._loop` without the `PrintLog` and the pokes.
  - Each wait is `await asyncio.sleep(step)` in slices of `HEARTBEAT_SLICE = 5 s`, calling
    `heartbeat(active)` between slices.
  - `follow_print` reads `activity.info().heartbeat_details` for `active` when it is not
    `fresh`, and passes `activity.heartbeat` as `heartbeat`.
- [ ] **Step 4: Run them** (the same command). Expected: PASS.
- [ ] **Step 5: Commit** `feat(print): the print follow loop as an activity (#1053)`.

### Task 2: `FollowPrint`, started by `PrintRun` and the progress route

**Files:**
- Create: `backend/scadbuddy/workflows/follow.py` (`FollowPrint`, `FOLLOW_WORKFLOW`,
  `follow_id`, `follow(client, task_queue, output_id)`)
- Modify: `backend/scadbuddy/workflows/printing.py` (start the child after `print_finish` for an
  output)
- Modify: `backend/scadbuddy/workflows/print_activities.py` (drop `watcher`)
- Modify: `backend/scadbuddy/workflows/client.py` (`bambuddy_worker` registers `FollowPrint`)
- Modify: `backend/scadbuddy/main.py` (the activity in the worker; delete `print_watcher.start`
  and `aclose`; the boot pass over `print_watches`)
- Modify: `backend/scadbuddy/api/printing.py` (the progress route calls `follow`)
- Modify: `backend/scadbuddy/api/deps.py` (delete `print_watcher`, `PrintWatcherDep`)
- Delete: `backend/scadbuddy/bambuddy/watcher.py`, `backend/tests/bambuddy/test_watcher.py`
- Test: `backend/tests/test_follow_workflow.py`, `backend/tests/api/test_print_progress.py`,
  `backend/tests/test_print_workflow.py`

**Interfaces:**
- Consumes: Task 1's `FollowInput` and `follow_print`.
- Produces:
  - `FollowPrint.run(output_id: str) -> str` and the signal `FollowPrint.poke()`.
  - `follow(client: Client, task_queue: str, output_id: str) -> None`: signal-with-start
    (`start_signal="poke"`, `ALLOW_DUPLICATE`), bounded by `RPC_TIMEOUT` 5 s, errors logged.
  - `resume_followed(pool, client, task_queue, now) -> list[str]`: the boot pass.

- [ ] **Step 1: Failing tests.**
  - `test_follow_workflow.py`, on the dev server with a fake `follow_print`:
    - `test_it_completes_when_the_follow_ends`.
    - `test_a_poke_restarts_the_follow_fresh`: the fake blocks until cancelled. A second attempt
      sees `fresh=True`.
    - `test_follow_starts_or_pokes_one_execution`: two `follow` calls give one run id, and the
      second is a poke.
    - `test_a_finished_follow_can_start_again`.
  - `test_print_workflow.py`: `test_an_output_run_starts_its_follow`; and
    `test_a_second_run_pokes_the_follow_already_running` (the child start raises
    `WorkflowAlreadyStartedError`, and the workflow signals instead).
  - `tests/api/test_print_progress.py`:
    - Replace the `get_print_watcher` override with a recording `follow` patch.
    - `test_a_run_starts_the_print_watcher` becomes `test_a_run_starts_its_follow` (describe
      `follow-print-<id>`).
    - Add `test_the_progress_route_answers_when_temporal_is_unreachable`.
  - `test_resume_followed_starts_recent_prints_and_clears_them` (Postgres and dev server).
- [ ] **Step 2: Run them**
  - Command: `uv run --frozen pytest tests/test_follow_workflow.py tests/test_print_workflow.py -q`
  - Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `FollowPrint`:
    - It loops. Each pass calls `workflow.start_activity("follow_print", FollowInput(id,
      fresh=fresh), start_to_close_timeout=MAX_AGE + timedelta(hours=1),
      heartbeat_timeout=timedelta(seconds=30), retry_policy=RetryPolicy(maximum_interval=60 s),
      cancellation_type=WAIT_CANCELLATION_COMPLETED)`.
    - It waits on `self.poked or task.done()`. When poked, it cancels the attempt, awaits it
      with `suppress(ActivityError)`, and loops with `fresh=True`. Otherwise it returns the
      reason.
  - In `PrintRun`, after `print_finish` succeeds for `input.source.kind == "output"`, call
    `start_child_workflow(FollowPrint.run, output_id, id=follow_id(output_id),
    parent_close_policy=ABANDON, cancellation_type=ABANDON)`. On
    `WorkflowAlreadyStartedError`, signal `poke` through
    `get_external_workflow_handle_for(...)`. Signal errors (it closed meanwhile) are ignored.
- [ ] **Step 4: Run them** (plus `tests/api/test_print_progress.py`). Expected: PASS.
- [ ] **Step 5: Commit** `feat(print): FollowPrint follows each print on Temporal; the watcher goes (#1053)`.

### Task 3: Docs

- Update the `bambuddy/` entry in `CLAUDE.md`'s layout if it names the watcher.
- Add a spec §10 "As built (2c)" note.
- Replace the watcher's docstring rationale ("Why polling, with back-off") in `follow.py`'s
  module docstring.
- Commit `docs: FollowPrint (#1053)`.
