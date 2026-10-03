# Phase 2b: renders on the command shape — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `POST /models/{slug}/render` starts `TemplatePipeline` as `render-<render_key>` with
update-with-start. The workflow's first activity writes the `render_jobs` row. Coalescing, claims
and superseding live in workflow state, and the render reconciler is deleted.

**Architecture:**
- `RenderService.submit` keeps today's checks before Temporal: the snapshot pin and the input
  size.
- It then calls `start_command(TemplatePipeline, RenderStart, id=render-<render_key>)` with
  `ALLOW_DUPLICATE` reuse and the `accepted` Update.
- The workflow's first step is the local activity `render_accept`. It applies the
  `render_queue_max` check and inserts the row, idempotently on `(workflow_id, workflow_run_id)`.
- The first `accepted` answers that row. Each later one adds a claim and answers
  `coalesced: true`.
- A supersede, or `RenderService.cancel`, sends the old execution the `release` Update. The last
  release cancels the render task, which projects `cancelled`. Its `RenderPiece` children are
  left running (`ABANDON`).

**Tech Stack:** Python 3.12, FastAPI, temporalio 1.33, psycopg 3, Postgres 17; React 19 + msw;
the agent's TypeScript tools.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §4.2, §4.4
(render reconciler row), §4.5. Template spec §3.3/§3.4 for what claims and pieces mean today.
Stacked on PR #1063 (`feat/1053-operations`).

## Rulings this plan makes (record each in the ledger at the task that applies it)

1. **Validation stays in the route; `render_accept` only counts and inserts.**
   - §4.5 puts the revision and schema resolution in the first activity. Both need the API's
     git and volume, which a render worker does not have (template spec §9, phase 3's
     volume-free workers).
   - They are reads, and the spec lets a route make reads before it calls Temporal (§4.1).
   - Cost if wrong: one more round of reads per render, as today.
2. **`render_accept` and `render_claims` are local activities.**
   - The render worker's activity slots (`max_concurrent_activities`) are taken by openscad runs
     for minutes. A regular activity would wait behind them, and the route would answer
     `command-still-accepting` under load.
   - Local activities have their own slots, and the worker already holds the projection's
     Postgres pool.
   - Cost if wrong: none for behaviour. A Postgres outage retries in the workflow task.
3. **A full queue stays a 503 with `Retry-After`**, as `api/jobs.py` answers today. §4.5 says 429,
   but changing the status code is not part of moving the check.
4. **Coalescing covers a running job too**, not only a pending one. That is §4.5: the request
   reaches the open execution.
5. **Supersede: start first, then release.** A refused start (full queue, Temporal down) then
   supersedes nothing, which is today's guarantee ("a refused submit supersedes nothing").
   Cost: at exactly a full queue, the superseded job still counts, so the submit is refused
   where today it was accepted.
6. **A supersede of a job with the same `render_key` answers that job without a new claim**, as
   `JobProjection.submit` does today.
7. **Drop `render_jobs_pending_key`.** Coalescing is no longer the row's job. A new execution's
   pending row may coexist with a legacy pending row for the same key while old executions drain.
   Cost: an old-build API pod overlapping the new one during a rolling deploy gets an
   `ON CONFLICT` inference error on its render submits for that overlap.
8. **The reconciler goes; one boot pass settles legacy pending rows.**
   - A pending row with no `workflow_run_id` and no execution `render-<id>` (`describe` says
     NOT_FOUND) is failed with `LEGACY_UNSTARTED_ERROR`.
   - A pre-Temporal row (no `workflow_id`) is failed too. Today `adopt_legacy_pending` re-renders
     it, but its release (#546) is long gone.
   - Rows whose old execution still runs are left to it.
   - If Temporal is unreachable at boot, the pass is skipped with a log, and the next boot retries.
9. **A re-sent render may add an extra claim.** Renders are content-keyed and carry no request id
   (§4.5), so a client re-sending after `command-still-accepting` lands a second `accepted`
   Update.
   - The cost is that a later supersede does not cancel that job: it renders to the end, and
     `piece_key` dedupes its openscad work.
   - Pinning it would need an update id per request, which the route has no key for.
10. **An `accepted` that reaches an execution already released to zero** answers
    `closing: true`. `submit` then waits up to `CLOSING_WAIT` (5 s) for the execution to close
    and starts again. This mirrors the repeat race in phase 1's `PrintRun`.

## Global Constraints

- Workflow ID `render-<render_key>` (`render/job_models.py` `render_key`). Policies:
  `id_conflict_policy=USE_EXISTING`, `id_reuse_policy=ALLOW_DUPLICATE` (§4.5).
- `render_key` coalesces jobs and `piece_key` dedupes openscad renders. Never swap them
  (CLAUDE.md).
- Search Attributes (`ScadbuddyKind=render`, `ScadbuddySubject=<slug>`, `ScadbuddyStatus`) are
  upserted only when `Settings.temporal_search_attributes` is true, as in phase 1.
- The memo keeps `activity_timeout`, and `start_command` gains a `memo` parameter.
- Migrations: a NEW file `backend/scadbuddy/migrations/<utc>_render_jobs_run_id.sql`. Edit no
  merged one.
- Never commit `backend/openapi.json` or a `schema.d.ts`. Commit with `git add <paths>`.
- Tests that share the session's Temporal dev server use unique params per test (a uuid-derived
  `width`), so no two tests share a `render-<render_key>`.

## Review Focus

1. A supersede whose old execution has already closed (done) must not fail the new submit:
   `release` on a closed run is NOT_FOUND, and that answer is ignored. (Task 3 test.)
2. Two identical submits at once must give one job, `claims == 2`, and one execution. (Task 2
   test.)
3. An `accepted` arriving after the last `release` must not hand back a cancelled job. (Task 2
   and Task 3 tests.)
4. A legacy `render-<job id>` row superseded after the rollout must still be released through the
   old path (`release_claim` plus a workflow cancel). (Task 3 test.)
5. Temporal unreachable gives a 503 `temporal-unavailable` with no row. A slow accept gives a 503
   `command-still-accepting`, which the browser and the agent re-send. (Tasks 3 and 4 tests.)

---

### Task 1: The row: `workflow_run_id`, `accept`, `set_claims`, legacy reads

**Files:**
- Create: `backend/scadbuddy/migrations/<utc>_render_jobs_run_id.sql`
- Modify: `backend/scadbuddy/render/job_models.py` (`Job.workflow_run_id`)
- Modify: `backend/scadbuddy/render/projection.py`
- Test: `backend/tests/test_projection.py`

**Interfaces:**
- Produces:
  - `JobProjection.accept(job: Job, key: str, *, workflow_id: str, run_id: str,
    max_pending: int) -> Job`. It raises `QueueFullError`. It is idempotent on
    `(workflow_id, run_id)`: a second call returns the row, with no event and no count.
  - `JobProjection.set_claims(job_id: str, claims: int) -> None`.
  - `JobProjection.legacy_pending() -> list[Job]`: pending rows whose `workflow_run_id` is NULL.
  - `JobProjection.fail_legacy(job_ids: list[str], error: str) -> list[Job]`: guarded on
    pending; announces `job.failed`.
  - `LEGACY_UNSTARTED_ERROR`.
  - `PROJECTION_COLUMNS` gains `workflow_run_id`.
  - `Job.workflow_run_id: str | None = None`.

- [ ] **Step 1: Failing tests** in `tests/test_projection.py`:
  - `test_accept_inserts_once_per_execution`: the first call inserts and announces
    `job.pending`. A second call with the same `(workflow_id, run_id)` returns the same id, and
    no second event.
  - `test_accept_counts_the_queue`: with `max_pending=1` and one pending row, a new execution
    raises `QueueFullError(1)`. A retry of an already-inserted execution returns its row even at
    a full queue.
  - `test_two_executions_of_one_key_each_get_a_row`: the same `render_key` under two run ids
    gives two rows. The pending index is gone.
  - `test_set_claims_moves_only_an_unfinished_row`.
  - `test_legacy_pending_and_fail_legacy`: a row inserted without a run id is listed. `fail_legacy`
    fails it, and a row of the new shape is never listed.
- [ ] **Step 2: Run them**
  - Command: `uv run --frozen pytest tests/test_projection.py -q`
  - Expected: FAIL (`accept` is not defined).
- [ ] **Step 3: Migration.**
  ```sql
  ALTER TABLE render_jobs ADD COLUMN workflow_run_id text;
  CREATE UNIQUE INDEX render_jobs_execution ON render_jobs (workflow_id, workflow_run_id);
  DROP INDEX render_jobs_pending_key;
  ```
- [ ] **Step 4: Implement `accept`.**
  - In one transaction, `SELECT` the row by `(workflow_id, workflow_run_id)` and return it if it
    exists.
  - Otherwise, when `max_pending` is set, count pending rows and raise `QueueFullError` at the
    limit.
  - Then `INSERT ... ON CONFLICT (workflow_id, workflow_run_id) DO UPDATE SET claims =
    render_jobs.claims RETURNING *, (xmax = 0) AS inserted`, and announce `job.pending` only
    when inserted.
  - Also add `set_claims`, `legacy_pending` and `fail_legacy`.
- [ ] **Step 5: Run them**
  - Command: `uv run --frozen pytest tests/test_projection.py -q`
  - Expected: PASS.
- [ ] **Step 6: Commit** `feat(render): render_jobs rows keyed by their execution (#1053)`.

### Task 2: `TemplatePipeline` on the command shape

**Files:**
- Modify: `backend/scadbuddy/workflows/models.py` (`RenderStart`, `AcceptRender`,
  `RenderAnswer`, `ReleaseAnswer`, constants)
- Modify: `backend/scadbuddy/workflows/pipelines.py`
- Modify: `backend/scadbuddy/workflows/activities.py` (`render_accept`, `render_claims` in
  `all()`)
- Modify: `backend/scadbuddy/workflows/commands.py` (`memo`)
- Create: `backend/tests/support/renders.py` (`start_render`)
- Test: `backend/tests/test_workflows.py`, `backend/tests/test_activities.py`,
  `backend/tests/test_worker.py`, `backend/tests/test_commands.py`

**Interfaces:**
- Produces:
  - `RenderStart(slug, params, inputs, model_version, render_key, kind="render", max_pending=0,
    search_attributes=False)`.
  - `AcceptRender(start: RenderStart, workflow_id: str, run_id: str)`.
  - `RenderAnswer(job: Job | None, coalesced: bool = False, queue_full: int | None = None,
    closing: bool = False)`.
  - `ReleaseAnswer(cancelled: Job | None)`.
  - Constants `ACCEPT_ACTIVITY = "render_accept"`, `CLAIMS_ACTIVITY = "render_claims"`,
    `RELEASE_UPDATE = "release"`, `QUEUE_FULL = "QueueFull"`.
  - Updates `TemplatePipeline.accepted() -> RenderAnswer` (name `ACCEPTED_UPDATE`) and
    `TemplatePipeline.release(reason: str) -> ReleaseAnswer`.
  - `start_command(..., memo: Mapping[str, Any] | None = None)`.
  - Test helper `start_render(client, queue, start, *, id=None) -> tuple[WorkflowHandle, Job]`.

- [ ] **Step 1: Failing tests.**
  - Rewrite `test_workflows.py`'s starts to `start_render`. A test that needs two jobs sharing
    a piece passes its own ids. `FakeActivities` gains a fake `render_accept`, which builds a
    `Job` with a uuid id and records the call, and a fake `render_claims`. `_worker` registers
    both.
  - Add:
    - `test_a_second_accepted_coalesces_with_one_more_claim`: two `start_render` calls on one id
      give one job, the second with `coalesced`. `render_claims` was called with 2.
    - `test_release_of_one_of_two_claims_keeps_rendering`.
    - `test_the_last_release_cancels_and_projects_cancelled_with_its_reason`: the `block_main`
      render, then `release("superseded")`, gives a `cancelled` projection whose error is
      `SUPERSEDED_ERROR`. The workflow completes, and the piece is still running.
    - `test_accepted_after_the_last_release_answers_closing`.
    - `test_a_full_queue_answers_queue_full_and_fails_the_execution`: the fake raises
      `ApplicationError(type=QUEUE_FULL, details=[3], non_retryable=True)`. The answer has
      `queue_full == 3`, and the execution failed.
  - In `test_commands.py`, add `test_the_memo_reaches_the_execution`.
  - Update `test_activities.py` and `test_worker.py` starts to `start_render`.
- [ ] **Step 2: Run them**
  - Command: `uv run --frozen pytest tests/test_workflows.py tests/test_commands.py -q`
  - Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `run(start)` steps:
    1. Upsert the Search Attributes (gated).
    2. Run `execute_local_activity(ACCEPT_ACTIVITY, AcceptRender(...), result_type=Job,
       start_to_close_timeout=SHORT, retry_policy=PROJECT_RETRY)`.
    3. If its `ApplicationError` has `type == QUEUE_FULL`, set `self.queue_full`, wait for
       `all_handlers_finished`, and raise `ApplicationError(type=REFUSED, non_retryable=True)`.
    4. Set `self.job`, `self.claims = 1`, and `self.work = asyncio.create_task(self._render(job))`,
       then await it. If `self.released` is set, swallow the `CancelledError`.
    5. Wait for `all_handlers_finished`.
  - `_render` is today's body. Its cancelled branch also covers `self.released`: it projects
    `cancelled` with `Failure(error=self.released)` and returns.
  - `accepted`:
    - Wait for a job or a refusal.
    - If `self.released` is set, answer `closing`.
    - Every call after the first adds a claim and runs `render_claims` as a local activity.
    - Answer with `job.model_copy(update={"claims": self.claims})`.
  - `release`:
    - Wait for a job or a refusal; with no job, answer `ReleaseAnswer(None)`.
    - Take one claim. Above zero, project the claims and answer `None`.
    - At zero:
      1. Set `self.released = SUPERSEDED_ERROR` if `reason == "superseded"`, else
         `CANCELLED_ERROR`.
      2. Cancel `self.work` and wait for it to end.
      3. Answer the job with `state="cancelled"`.
  - `render_accept` in `RenderActivities`:
    1. Build `Job(id=uuid4().hex, created_at=now(), ...)` from the start.
    2. Call `projection.accept`.
    3. Map `QueueFullError` to `ApplicationError(str(e), e.depth, type=QUEUE_FULL,
       non_retryable=True)`.
  - `render_claims` calls `set_claims`.
- [ ] **Step 4: Run them**
  - Command: `uv run --frozen pytest tests/test_workflows.py tests/test_commands.py
    tests/test_activities.py tests/test_worker.py -q`
  - Expected: PASS.
- [ ] **Step 5: Commit** `feat(render): TemplatePipeline holds claims and answers its first Update (#1053)`.

### Task 3: `RenderService` and the route

**Files:**
- Modify: `backend/scadbuddy/render/submit.py`
- Modify: `backend/scadbuddy/render/projection.py` (delete `submit`, `stale_pending`,
  `adopt_legacy_pending`)
- Modify: `backend/scadbuddy/main.py` (`_start_render`)
- Modify: `backend/scadbuddy/api/deps.py` (pass `search_attributes`)
- Modify: `backend/scadbuddy/api/jobs.py` (still-accepting and unavailable responses)
- Test: `backend/tests/test_submit.py`, `backend/tests/api/test_jobs.py`

**Interfaces:**
- Consumes: Task 1's projection, and Task 2's models and updates.
- Produces:
  - `RenderService.submit(...) -> Job`, unchanged in signature. It raises `QueueFullError`,
    `CommandStillAcceptingError` and `TemporalUnavailableError`.
  - `RenderService.cancel(job_id, *, slug) -> Job | None`.
  - `RenderService.settle_legacy() -> list[str]`.
  - `RenderService.start()` runs `settle_legacy` and the prune loop.
  - `CLOSING_WAIT = 5.0`.

- [ ] **Step 1: Failing tests.**
  - Delete the reconciler and unstartable tests: `..._is_started_by_the_reconciler`,
    `a_row_whose_start_fails...`, `a_start_that_can_never_succeed...` and
    `a_reconciled_start...`.
  - Rewrite the remaining tests on the new ids.
  - Add:
    - `test_a_submit_runs_as_render_of_its_key`.
    - `test_an_identical_submit_joins_the_running_job_with_a_claim`.
    - `test_superseding_releases_the_old_execution_after_the_new_one_starts`.
    - `test_superseding_a_finished_job_still_submits`.
    - `test_a_refused_submit_supersedes_nothing`.
    - `test_superseding_a_legacy_row_cancels_its_workflow_the_old_way`: insert a row with
      `workflow_id=render-<id>` and no run id, and start the old workflow id with a blocking
      fake.
    - `test_settle_legacy_fails_rows_with_no_execution_and_leaves_running_ones`.
    - `test_a_submit_after_the_last_release_waits_for_close_and_starts_again`.
  - In `tests/api/test_jobs.py`:
    - Keep `test_a_full_render_queue_is_a_503_with_retry_after`.
    - Add `test_a_render_when_temporal_is_unreachable_is_a_503_and_no_row`: patch `submit` to
      raise `TemporalUnavailableError`, and check the problem type.
- [ ] **Step 2: Run them**
  - Command: `uv run --frozen pytest tests/test_submit.py -q`
  - Expected: FAIL.
- [ ] **Step 3: Implement `submit`.**
  1. Pin and check the size (unchanged).
  2. Compute `key`.
  3. Handle the supersede: read the row. If it is unfinished, has the same slug and the same
     key, return it.
  4. Call `start_command(..., result_type=RenderAnswer, memo=self._memo())`.
  5. On `closing`, wait for the execution's result (bounded by `CLOSING_WAIT`, errors ignored)
     and call it once more.
  6. On `queue_full`, count `render_rejected` and raise `QueueFullError(depth,
     self.retry_after())`.
  7. Release the superseded row (`_release(row, "superseded")`). A row with no
     `workflow_run_id` takes `store.release_claim` plus `_cancel_workflow`. Any other takes
     `get_workflow_handle(row.workflow_id, run_id=row.workflow_run_id).execute_update(
     RELEASE_UPDATE, reason, result_type=ReleaseAnswer, rpc_timeout=RPC_TIMEOUT)`. NOT_FOUND
     (closed) is ignored, and other errors are logged and counted as today's
     `cancel_workflow` errors.
  8. Update the metrics and return the job.
- [ ] **Step 4: Implement the rest.**
  - `cancel` goes through the same `_release` with `"withdrawn"`.
  - `settle_legacy`: for each row in `legacy_pending()`, a row with no `workflow_id` is failed.
    Otherwise `describe` it (bounded); NOT_FOUND is failed. If Temporal is unreachable, log and
    stop.
  - `start` runs `settle_legacy`, then `_prune_forever`.
  - Delete `reconcile_once`, `_start`, `_fail_unstartable`, `UNSTARTABLE`, `reconcile_after` and
    `reconcile_interval`.
  - `main._start_render` drops `adopt_legacy_pending`.
  - `api/jobs.py` maps `CommandStillAcceptingError` to `still_accepting()` and
    `TemporalUnavailableError` to `temporal_unavailable("renders")`, both from `api/operations`.
    Document both in `responses`.
- [ ] **Step 5: Run them**
  - Command: `uv run --frozen pytest tests/test_submit.py tests/test_projection.py
    tests/api/test_jobs.py -q`
  - Expected: PASS.
- [ ] **Step 6: Commit** `feat(render): renders start with update-with-start; the reconciler goes (#1053)`.

### Task 4: The browser and the agent re-send a render still being accepted

**Files:**
- Modify: `frontend/src/api/client.ts` (`render` through `reattach`)
- Modify: `agent/src/tools/customizer.ts` (`render_model` through `reattach` from
  `../api/command.js`)
- Test: `frontend/src/api/client.test.ts`, `agent/test/tools.test.ts`

- [ ] **Step 1: Failing tests.**
  - Browser: msw answers the render POST once with 503 `command-still-accepting`, then 202. The
    call resolves with the job, after two requests.
  - Agent: the same against its fake backend. `render_model` returns the job, and the POST was
    sent twice.
- [ ] **Step 2: Run them**
  - Browser: `pnpm vitest run src/api/client.test.ts`. Agent: `pnpm vitest run test/tools.test.ts`.
  - Expected: FAIL.
- [ ] **Step 3: Implement.**
  - Browser: `render: (...) => reattach(() => request<RenderAccepted>(...))`.
  - Agent: `await reattach(ctx, () => ctx.backend.POST(...), \`render ${slug}\`)` in place of
    `ok(...)`.
- [ ] **Step 4: Run them** (the same commands). Expected: PASS.
- [ ] **Step 5: Commit** `feat(render): clients re-send a render Temporal is still accepting (#1053)`.

### Task 5: Docs

- In `CLAUDE.md`'s layout entry for `submit.py`, replace "submit inserts the row, starts the
  workflow, and a reconciler starts any pending row nothing picked up" with the update-with-start
  shape.
- Add a spec §10 "As built (2b)" note.
- In `README.md`, remove any mention of the render reconciler.
- Commit `docs: renders on the command shape (#1053)`.
