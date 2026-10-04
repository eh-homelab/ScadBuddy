# Phase 3f: the preview backfill as a Schedule-triggered workflow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The boot's pass over every model for default-render previews
(`PreviewScheduler.request_all`, called from `main.py`'s lifespan) becomes `PreviewBackfill`, a
workflow on the `library` queue that a Temporal Schedule, `scadbuddy-previews-<queue>`, starts
every hour and once at each boot. The per-change requests (`PreviewScheduler.request`, hooked to
`Catalogue.on_change` and `OutputStore.on_change`) stay in-process.

**Architecture:**
- `PreviewBackfill.run(slugs=None)` runs `previews_due` (one activity: list the models, keep
  those whose `PreviewScheduler.plan` names a source key to render), then `preview_refresh(slug)`
  for each, one at a time, with `PAUSE` (1 s, today's `DEFAULT_INTERVAL`) after each one that
  rendered. Past `BATCH` refreshes, or when Temporal suggests it, it continues as new with the
  slugs left.
- `preview_refresh` is `PreviewScheduler.refresh(slug)`: re-plan, then
  `RenderService.render_preview` (which starts `RenderPreview` on the render queue as
  `preview-<slug>-<key[:12]>` with `USE_EXISTING`), then write or record the failure. It is the
  same call the in-process worker makes, under one lock shared with it.
- The Schedule is set up beside the housekeeping ones (`_set_up_housekeeping`, retried until
  Temporal takes it) through `ensure_workflow_schedule`, the create/update/delete/trigger logic
  factored out of `housekeeping.ensure_schedule`: overlap `SKIP`, a paused Schedule kept paused
  and not triggered, the boot trigger with `BUFFER_ONE`. Previews off (`preview_renders=false`)
  deletes it.

**Tech Stack:** Python 3.12, temporalio (Schedules, `workflow.continue_as_new`), FastAPI, pytest
with the Temporal dev server.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §4.4, row
"preview scheduler (`PreviewScheduler.start`, `render/previews.py:155`) and its boot pass over
every model (`request_all`, `:184`) → `RenderPreview` is already a workflow; the backfill becomes a
Schedule-triggered workflow". Phase 3's 3a notes (§10) for the Schedule conventions. Stacked on
`feat/1054-uploads` (3e).

## Rulings

1. **The per-change requests stay in-process.** A catalogue or output change calls
   `PreviewScheduler.request(slug)` from the thread that made it; the scheduler's debounce (2 s)
   folds a burst of edits into one render, and the render is already the `RenderPreview`
   workflow. A workflow per edit would add a Temporal round trip to every save for no
   durability gain: a lost request is picked up by the next backfill tick. `start`, `aclose`,
   `request`, the debounce and the worker loop are unchanged; `request_all` is deleted.
2. **Only the backfill moves**, to `PreviewBackfill` on `library`. It needs the data volume (the
   listing, `source_key`) and `PreviewStore`, which the API process holds, as the housekeeping
   sweeps do. The render itself stays on the render queue.
3. **`RenderPreview` is not started as a child workflow.** temporalio's
   `workflow.start_child_workflow` takes no `id_conflict_policy` (checked against the installed
   SDK), so a child with the ID the in-process scheduler already started would fail to start
   instead of joining. The activity calls `RenderService.render_preview`, the client start with
   `USE_EXISTING` both paths already use, so they dedupe on the one workflow ID
   `preview-<slug>-<key>`. Starting a workflow from an activity's client is the SDK's
   documented way to do it from outside a workflow. `render_key` and `piece_key` are untouched:
   a preview is neither a job nor a piece.
4. **One preview at a time in the process, as before.** `PreviewScheduler.refresh` holds one
   `asyncio.Lock` (the activity runs on the API's loop, beside the scheduler's worker), and
   re-plans under it. A model the edit path rendered while the backfill waited is then current,
   and is skipped, rather than rendered twice and written twice.
5. **The interval is fixed, one hour (`BACKFILL_INTERVAL`), with no new setting.** Today the
   backfill runs only at boot; the boot trigger keeps that. The hourly tick also retries what an
   infrastructure failure left unrendered ("tried again later" in `refresh`), which today waited
   for the next edit or boot. After the first pass a tick is one listing activity: every
   current preview is skipped by `plan`.
6. **Bounds.** `previews_due`: 5 min. `preview_refresh`: start-to-close 1 h, heartbeat 1 min
   (it waits on the lock and on a render bounded by `3 × render_timeout`, plus the snapshot
   transfer on the Bambuddy store). Each tried once: the next tick is the retry. A run:
   `execution_timeout` 12 h for the whole continue-as-new chain, so a run that fails on replay
   (the `library` worker is unversioned) ends, and the Schedule's `SKIP` stops holding ticks
   back. A backlog longer than that resumes at the next tick, since rendered previews are
   skipped.
7. **A listing that fails fails the run** (logged; the next tick tries again). One model whose
   plan raises is logged and skipped, as the old worker loop logged and moved on.
8. **Previews off deletes the Schedule.** `preview_renders` applies at the next start (#322), as
   the scheduler already does; a run still open when it is turned off finds no scheduler, and its
   activities do nothing.

## Global Constraints

- Schedule id `scadbuddy-previews-<library queue>`; workflow type `PreviewBackfill`; activity
  names `previews_due`, `preview_refresh`; overlap `SKIP`; boot trigger `BUFFER_ONE`.
- `PreviewBackfill.run` must stay replay-compatible (unversioned `library` worker): a change to
  its command sequence goes behind `workflow.patched`.
- Follow the Temporal Python SDK as documented; stop and report on any deviation.
- Never commit `backend/openapi.json` or a `schema.d.ts`. `git add` explicit paths only. Commits
  name #1054 and end with the `Co-Authored-By` line.

## Review Focus

1. An edit while the backfill is rendering the same model: one render, one write, and the
   preview kept is the edited source's (`test_an_edit_and_the_backfill_render_a_model_once`).
2. Previews turned off: no Schedule left starting backfills
   (`test_previews_off_leaves_no_backfill_schedule`).
3. A catalogue of hundreds of models on the first boot after the upgrade: the run continues as
   new instead of growing one history past Temporal's limits
   (`test_a_long_backlog_continues_as_new`).
4. One model that cannot be planned (unreadable source) or whose refresh fails: the rest are
   still rendered (`test_a_failing_refresh_does_not_stop_the_rest`,
   `test_due_skips_a_model_it_cannot_plan`).
5. A restart with the Schedule paused by an operator: it stays paused and is not triggered
   (shared `ensure_workflow_schedule`; `test_a_paused_schedule_stays_paused_and_is_not_triggered`
   keeps covering it).

---

### Task 1: `PreviewBackfill` and its Schedule

**Files:**
- Modify: `backend/scadbuddy/workflows/housekeeping.py`: factor `ensure_workflow_schedule(client,
  schedule_id, interval, action)` out of `ensure_schedule`; `_schedule` takes the action.
- Create: `backend/scadbuddy/workflows/previews.py`: `PreviewBackfill`, `ensure_preview_schedule`,
  `preview_schedule_id_for`, the constants.
- Test: `backend/tests/test_preview_backfill.py` (new); `tests/test_housekeeping.py` stays green
  unchanged.

**Interfaces:**
- Produces: `PREVIEW_BACKFILL_WORKFLOW = "PreviewBackfill"`, `DUE_ACTIVITY = "previews_due"`,
  `REFRESH_ACTIVITY = "preview_refresh"`, `BACKFILL_INTERVAL = 3600.0`, `BATCH = 100`,
  `REFRESH_TIMEOUT`, `HEARTBEAT_TIMEOUT` (housekeeping's), `preview_schedule_id_for(queue) -> str`,
  `ensure_preview_schedule(client, queue, enabled: bool) -> None`,
  `PreviewBackfill.run(slugs: list[str] | None = None) -> list[str]` (the slugs whose refresh
  failed in this run).

- [ ] **Step 1: Failing tests** (`requires_temporal`, fake activities named `previews_due` /
  `preview_refresh`):
  - `test_backfill_refreshes_each_due_model_in_order`: due `["a","b","c"]` → refreshed in that
    order; the history has one timer per refresh that returned True.
  - `test_a_failing_refresh_does_not_stop_the_rest`: refresh of `b` raises → `["a","c"]` still
    refreshed, run returns `["b"]`.
  - `test_a_long_backlog_continues_as_new`: due returns `BATCH + 1` slugs (refresh returns False,
    so no timers) → every slug refreshed once, `previews_due` called once, and the first run
    closed as continued-as-new.
  - `test_the_preview_schedule_runs_the_backfill_hourly`: describe → interval 3600 s, `SKIP`,
    action `PreviewBackfill` on the queue, no args, `execution_timeout == BACKFILL_TIMEOUT`.
  - `test_previews_off_deletes_the_schedule`: enabled then disabled → describe raises; disabled
    with none is not an error.
- [ ] **Step 2: Run** `uv run --frozen pytest -q -p no:cacheprovider tests/test_preview_backfill.py`
  → FAIL (module missing).
- [ ] **Step 3: Implement.**

```python
# workflows/housekeeping.py
def _schedule(interval: float, action: ScheduleActionStartWorkflow) -> Schedule:
    return Schedule(
        action=action,
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(seconds=max(interval, MIN_INTERVAL)))]
        ),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )


async def ensure_workflow_schedule(
    client: Client, schedule_id: str, interval: float, action: ScheduleActionStartWorkflow
) -> None:
    """(the body of today's `ensure_schedule`, from `handle = …` on, with `_schedule(interval,
    action)`)"""


async def ensure_schedule(client, task_queue, interval, *, schedule_id=None, sweeps=SWEEPS):
    schedule_id = schedule_id or schedule_id_for(task_queue)
    action = ScheduleActionStartWorkflow(
        HOUSEKEEPING_WORKFLOW, list(sweeps), id=schedule_id, task_queue=task_queue,
        execution_timeout=housekeeping_timeout(sweeps),
    )
    await ensure_workflow_schedule(client, schedule_id, interval, action)
```

```python
# workflows/previews.py
@workflow.defn(name=PREVIEW_BACKFILL_WORKFLOW)
class PreviewBackfill:
    @workflow.run
    async def run(self, slugs: list[str] | None = None) -> list[str]:
        if slugs is None:
            slugs = await workflow.execute_activity(
                DUE_ACTIVITY, result_type=list[str], start_to_close_timeout=DUE_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        failed: list[str] = []
        for index, slug in enumerate(slugs):
            if index >= BATCH or workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(args=[slugs[index:]])
            try:
                rendered = await workflow.execute_activity(
                    REFRESH_ACTIVITY, slug, result_type=bool,
                    start_to_close_timeout=REFRESH_TIMEOUT, heartbeat_timeout=HEARTBEAT_TIMEOUT,
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except ActivityError:
                workflow.logger.warning("could not refresh the preview of %s", slug)
                failed.append(slug)
                continue
            if rendered:
                await workflow.sleep(PAUSE)
        return failed


async def ensure_preview_schedule(client: Client, task_queue: str, enabled: bool) -> None:
    schedule_id = preview_schedule_id_for(task_queue)
    action = ScheduleActionStartWorkflow(
        PREVIEW_BACKFILL_WORKFLOW, id=schedule_id, task_queue=task_queue,
        execution_timeout=BACKFILL_TIMEOUT,
    )
    await ensure_workflow_schedule(
        client, schedule_id, BACKFILL_INTERVAL if enabled else 0.0, action
    )
```

- [ ] **Step 4: Run** `tests/test_preview_backfill.py tests/test_housekeeping.py` → PASS.
- [ ] **Step 5: Commit** `feat(previews): the preview backfill is a workflow on a Schedule (#1054)`.

### Task 2: wire it: the activities, the lock, the boot

**Files:**
- Modify: `backend/scadbuddy/render/previews.py`: `_refresh` → `refresh` under one
  `asyncio.Lock`, `_plan` → `plan`, new `due() -> list[str]`; delete `request_all`; module
  docstring.
- Modify: `backend/scadbuddy/main.py`: `_preview_activities(state)`; `_heartbeating` generic in
  its result; the `library` worker registers `PreviewBackfill` and the two activities;
  `_set_up_housekeeping` also calls `ensure_preview_schedule(client, queue, state.previews is not
  None)` first; the lifespan's listing and `request_all` go (`start()` stays).
- Modify: `backend/tests/api/conftest.py`: delete the preview Schedule after each test.
- Test: `backend/tests/api/test_previews.py`.

**Interfaces:**
- Consumes: Task 1's names.
- Produces: `PreviewScheduler.refresh(slug) -> bool` (async), `PreviewScheduler.plan(slug) -> str
  | None`, `PreviewScheduler.due() -> list[str]` (sync; run in a thread).

```python
# main.py
def _preview_activities(state: AppState) -> list[Callable[..., Any]]:
    """The preview backfill's steps (#1054): what the boot's `request_all` did, on the
    Schedule. With previews off (a run left open), both do nothing."""

    @activity.defn(name=DUE_ACTIVITY)
    async def previews_due() -> list[str]:
        previews = state.previews
        return [] if previews is None else await asyncio.to_thread(previews.due)

    @activity.defn(name=REFRESH_ACTIVITY)
    async def preview_refresh(slug: str) -> bool:
        previews = state.previews
        return False if previews is None else await _heartbeating(previews.refresh(slug))

    return [previews_due, preview_refresh]
```

- [ ] **Step 1: Failing tests** (`tests/api/test_previews.py`):
  - `test_boot_renders_each_model_without_a_thumbnail_once` keeps its assertions; its `settle`
    after a boot also waits for that boot's backfill run (`_backfilled(client, booted, runs)`:
    describe the Schedule until `num_actions >= runs`, then the last action's workflow result).
  - `test_an_edit_and_the_backfill_render_a_model_once`: two concurrent `scheduler.refresh(SLUG)`
    with a stub that blocks until released → one stub call, the preview stored.
  - `test_due_skips_a_model_it_cannot_plan`: two models, `source_key` made to raise for one →
    `due()` returns the other.
  - `test_previews_off_leaves_no_backfill_schedule`: boot with previews on (the Schedule exists),
    boot again off; once the prune Schedule has been re-ensured by that boot, describing the
    preview Schedule raises.
- [ ] **Step 2: Run** `tests/api/test_previews.py` → FAIL (`refresh`/`due` missing; no Schedule).
- [ ] **Step 3: Implement** as above; `refresh` is today's `_refresh` body inside
  `async with self._lock:`; `due` lists `catalogue.list_models()` and keeps each slug whose
  `plan` is not None, logging and skipping one that raises.
- [ ] **Step 4: Run** `tests/api/test_previews.py tests/api/test_health.py` and
  `tests/test_previews.py tests/test_preview_backfill.py tests/test_housekeeping.py` → PASS.
- [ ] **Step 5: Commit** `feat(previews): the boot's preview pass runs on the Schedule (#1054)`.

### Task 3: docs

**Files:** the spec's §10 phase 3 (an "As built so far (3f)" note), `CLAUDE.md` (the
workflows layout line), `README.md` (`SCADBUDDY_PREVIEW_RENDERS`).

- [ ] **Step 1:** Write the notes; no test.
- [ ] **Step 2: Commit** `docs: the preview backfill on its Schedule (#1054)`.
