# Durable Phase 1: PrintRun on Temporal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every print run (`POST /print/outputs/{id}/run`, `POST /print/library/{id}/run`)
runs as a Temporal workflow, `PrintRun`, that the route starts with update-with-start, so a
run survives the process that accepted it and the route never waits on a slice (#1052,
epic #1058).

**Architecture:** The route keeps its answers (202 / 200 `repeated` / 4xx refusal) and
`GET /print/runs/{id}` keeps reading `print_runs`. The route checks our record, then calls
`start_command` (`workflows/commands.py`), which runs
`execute_update_with_start_workflow` with the `accepted` Update. `PrintRun`
(`workflows/printing.py`, queue `bambuddy`) runs today's `prepare_run` and the row insert
as its first activity, then the rest of `execute_run` cut at each Bambuddy call into
activities (`workflows/print_activities.py`). The `print_runs` row is written only by
those activities. The in-process `PrintRuns` task runner, the heartbeat and lost-run expiry
are deleted.

**Tech Stack:** Python 3.12, temporalio 1.33 (`execute_update_with_start_workflow`,
`WithStartWorkflowOperation`, `WorkflowIDConflictPolicy`, `WorkflowIDReusePolicy`), psycopg 3,
FastAPI, pytest + respx, the Temporal dev server from `tests/support/temporal.py`.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md`, §4.1-§4.3
and §5 (merged in #972).

## Global Constraints

- §4.2: "The workflow ID is `<kind>-<key>`." For prints: `print-` + `run_key(subject, request)`.
- §4.2: "`id_conflict_policy = USE_EXISTING`" on every start; "`id_reuse_policy =
  ALLOW_DUPLICATE_FAILED_ONLY` for every `request_id`-keyed kind"; a body-only print key
  "uses `ALLOW_DUPLICATE` with the window".
- §4.2: "A command's workflow *fails* only when step 3 wrote no record ... Once the record
  exists, every outcome *completes* the workflow."
- §4.2: "The route also reads the record first, before calling Temporal."
- §4.2 step 3: the insert is "`ON CONFLICT (workflow_id, workflow_run_id) DO NOTHING`. On a
  conflict the activity re-reads the existing row and returns it. The event is published
  only when the row was actually inserted, in the same transaction."
- §4.2 step 4: "`command_answer_deadline` (default 10 s, below Envoy's 15 s)". For an
  `accepted` kind past the deadline, the route answers **503** `command-still-accepting`
  with `Retry-After`.
- §5.3: `print_slice_start` and `print_enqueue` have "**`maximum_attempts = 1`**", set
  explicitly and asserted by a test.
- §5.3: `print_slice_wait` "polls `/slice-jobs/{id}` every 2 s **inside the activity**,
  heartbeating, up to `DEFAULT_SLICE_TIMEOUT` (600 s) ... heartbeat timeout 30 s".
- §5.2: a run that "succeeded, or failed with `may_have_queued`, keeps its workflow open for
  `REPEAT_WINDOW` (10 min)"; a refused run fails its workflow and writes no record.
- §5.4: the migration "drops `heartbeat_at`; adds `workflow_id text` and `workflow_run_id
  text`, with a unique index on the pair". Retention is `print_run_retention_seconds`;
  "Empty, the default, keeps every row."
- §4.2: Search Attributes `ScadbuddyKind`, `ScadbuddySubject`, `ScadbuddyStatus`, plus
  `ScadbuddyMayHaveQueued` for prints. They hold identifiers and states only.
- Repo rules: a new migration file `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`, never edit a
  merged one; never commit `openapi.json` or a `schema.d.ts`; `git add` explicit paths.
- Workflow code imports only the workflow's models, inside
  `workflow.unsafe.imports_passed_through()`, as `workflows/pipelines.py` does.
- No secret crosses history: `StoredSettings` (it holds the Bambuddy key) is loaded inside
  each activity, never passed as an argument or result.

## Deviations from the spec's phase 1, recorded on #1052

1. **The `bambuddy` worker runs inside the API process in this phase**, not as the
   `scadbuddy-print` Deployment of §5.5. `OutputSource` reads the output's 3MF and writes
   `PlateSend` records on the data volume (`library/outputs.py` `record_send`), which a
   worker without the volume cannot do. A run still survives a restart: the workflow
   waits on the queue until the API is back. The separate Deployment, and the volume-free
   source it needs, become story #1060.
2. **The `operations` table and route** move to the first phase that has a kind to
   write them (phase 2's Bambuddy commands). This phase ships the shared helper and the
   `print_runs` record only.
3. **Search Attributes are registered by the clusters change.** Until that lands the
   workflow upserts them only when `SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES` is true
   (default false), so a namespace without them still runs prints.

## Review Focus

1. **A repeat that arrives while `print_accept` is still running** must get the same run,
   not a second insert: two POSTs with one body race into one workflow (USE_EXISTING),
   and the second Update returns the first's row with `repeated: true`. Task 5,
   `test_two_posts_that_race_get_one_run`.
2. **A worker killed after the insert committed but before the activity reported** must
   not insert a second row or publish twice. Task 1,
   `test_insert_accepted_twice_returns_the_first_row_and_publishes_once`.
3. **A `request_id` retry long after the run failed with a record** must answer that
   failed run and start nothing, even once the history is gone. Task 5,
   `test_a_request_id_retry_after_a_recorded_failure_returns_that_run`.
4. **An unexpected exception in any activity after the record** must leave the execution
   *completed* with the run `failed` (`unexpected`), never `Failed`. Task 4,
   `test_an_unexpected_activity_error_after_the_record_completes_the_workflow`.
5. **Temporal down when the route is called** must answer the documented 503
   (`bambuddy`-independent), not a 500 traceback, and leave no row. Task 5,
   `test_temporal_unreachable_is_a_503_and_writes_nothing`.

---

## File structure

| File | Responsibility |
|---|---|
| `backend/scadbuddy/migrations/<ts>_print_runs_on_temporal.sql` (new) | drop `heartbeat_at`, add `workflow_id`, `workflow_run_id`, unique index |
| `backend/scadbuddy/bambuddy/runs.py` | `PrintRun`, `PrintRunError`, `run_key`, `PrintRunStore` (record writes and reads only; no heartbeat, no lost expiry, no task runner) |
| `backend/scadbuddy/bambuddy/dispatch.py` | `slice_and_queue` split into `start_slice`, `wait_slice`, `enqueue_plate` (the old function composes them) |
| `backend/scadbuddy/bambuddy/print_run.py` | `plan_run` (upload + resolve, returns a serializable `PlannedRun`) and `queued_result`, cut from `execute_run` |
| `backend/scadbuddy/workflows/commands.py` (new) | `start_command`: update-with-start, deadline, `CommandStillAccepting` |
| `backend/scadbuddy/workflows/print_models.py` (new) | payloads that cross history: `PrintRunInput`, `SourceSpec`, `Accepted`, `PlannedRun`, ... |
| `backend/scadbuddy/workflows/printing.py` (new) | the `PrintRun` workflow |
| `backend/scadbuddy/workflows/print_activities.py` (new) | `PrintActivities` and `PrintDeps` |
| `backend/scadbuddy/workflows/client.py` | `print_worker()` |
| `backend/scadbuddy/core/settings.py`, `core/config.py` | `temporal_task_queue_bambuddy`, `temporal_search_attributes` |
| `backend/scadbuddy/library/settings_store.py`, `api/settings.py` | `print_run_retention_seconds` |
| `backend/scadbuddy/api/printing.py`, `api/library_print.py`, `api/deps.py`, `main.py` | the routes on `start_command`, the worker in the lifespan |
| `frontend/src/pages/settings/...` | the retention field (Settings → Printing) |

---

### Task 1: `print_runs` on Temporal: the migration and the record store

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_print_runs_on_temporal.sql`
- Modify: `backend/scadbuddy/bambuddy/runs.py`
- Test: `backend/tests/test_print_runs_store.py` (rewrite)

**Interfaces:**
- Produces, in `runs.py`:
  - `PrintRun`, `PrintRunError`, `run_key`, `REPEAT_WINDOW`, `UNEXPECTED_DETAIL` (unchanged)
  - `class PrintRunStore(pool, *, events: TransactionalEvents | None = None, repeat_window=REPEAT_WINDOW)`
  - `insert_accepted(run_id, subject, key, slug, workflow_id, workflow_run_id, retention: timedelta | None) -> PrintRun` (idempotent; prunes finished rows older than `retention`)
  - `start_enqueue(run_id) -> None`, `succeed(run_id, slug, result) -> PrintRun`, `fail(run_id, slug, error) -> PrintRun` (guarded on `status = 'running'`, event in the same transaction)
  - `get(run_id) -> PrintRun | None`
  - `find(key, *, has_request_id: bool) -> PrintRun | None`: with a `request_id`, the
    newest run of the key whatever its age; without, today's rule (running, or succeeded /
    may-have-queued inside the window)
- Deletes: `PrintRuns`, `claim`, `heartbeat`, `_expire_lost`, `LOST*`, `RunLostError`, `HEARTBEAT_INTERVAL`, `LOST_AFTER`, `RETENTION`, `RUN_LOCK_CLASS`.

- [ ] **Step 1: Write the migration**

```sql
-- #1052: print runs run as Temporal workflows (spec 2026-10-01 §5). The row is written
-- only by the `PrintRun` workflow's activities, so nothing beats a heartbeat and nothing
-- expires a lost run. `workflow_id`/`workflow_run_id` name the execution that owns the
-- row; the unique pair makes the first activity's insert idempotent under retry (§4.2).
ALTER TABLE print_runs DROP COLUMN heartbeat_at;
ALTER TABLE print_runs ADD COLUMN workflow_id text;
ALTER TABLE print_runs ADD COLUMN workflow_run_id text;
CREATE UNIQUE INDEX print_runs_execution ON print_runs (workflow_id, workflow_run_id);
-- Runs a pre-#1052 process left `running` have no workflow to finish them.
UPDATE print_runs SET status = 'failed', finished_at = now(),
    error = jsonb_build_object('type', 'about:blank', 'status', 500,
        'title', 'Internal Server Error', 'extensions', '{}'::jsonb,
        'detail', 'ScadBuddy restarted onto a new version while it was preparing this print, so it cannot tell whether the print was queued.')
  WHERE status = 'running';
```

- [ ] **Step 2: Write the failing store tests** (`tests/test_print_runs_store.py`, `requires_postgres`, on `pg_conninfo`):

```python
def test_insert_accepted_twice_returns_the_first_row_and_publishes_once(store, events): ...
def test_find_with_a_request_id_returns_a_failed_run_of_any_age(store): ...
def test_find_without_a_request_id_keeps_the_repeat_window(store): ...
def test_finish_is_guarded_on_running_and_publishes_in_the_transaction(store, events): ...
def test_start_enqueue_marks_may_have_queued_on_a_later_failure(store): ...
def test_retention_none_keeps_every_row_and_a_number_prunes_older_finished_rows(store): ...
```

`events` is a recorder with `publish_in(conn, event)` appending `event`.

- [ ] **Step 3: Run them to verify they fail**: `cd backend && uv run --frozen pytest tests/test_print_runs_store.py -v` → FAIL (`insert_accepted` missing).

- [ ] **Step 4: Rewrite `PrintRunStore`** with the interfaces above. `insert_accepted`
  runs, in one transaction: the retention `DELETE` when `retention` is set;
  `INSERT ... ON CONFLICT (workflow_id, workflow_run_id) DO NOTHING RETURNING`; on no
  row returned, `SELECT` the existing row by the pair; `publish_in(PrintRunEvent)` only
  when inserted. The finishing writes are `UPDATE ... WHERE id = %s AND status = 'running'
  RETURNING` plus `publish_in` when a row changed, else a `SELECT` of the row as it is.

- [ ] **Step 5: Run the tests to verify they pass**, plus `ruff`, `mypy`.

- [ ] **Step 6: Commit** `feat(print): print_runs is written only by its workflow (#1052)`.

---

### Task 2: Settings: the `bambuddy` queue, Search Attributes and run retention

**Files:**
- Modify: `backend/scadbuddy/core/settings.py`, `backend/scadbuddy/core/config.py`,
  `backend/scadbuddy/library/settings_store.py`, `backend/scadbuddy/api/settings.py`
- Test: `backend/tests/test_settings.py`, `backend/tests/api/test_settings_api.py` (whichever hold the existing `temporal_task_queue_render` and `event_log_retention_seconds` cases; add beside them)

**Interfaces:**
- `Settings.temporal_task_queue_bambuddy: str = "bambuddy"` (`SCADBUDDY_TEMPORAL_TASK_QUEUE_BAMBUDDY`, validated like the render queue; boot-only)
- `Settings.temporal_search_attributes: bool = False` (`SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES`; boot-only)
- `StoredSettings.print_run_retention_seconds: float | None = None` (Postgres only; `None` keeps forever; must be > 0 when set), exposed on the settings API's Printing section like `event_log_retention_seconds` is on its own.

- [ ] **Step 1: Failing tests** for each field's default, env parsing, and the
  validator's message (`SCADBUDDY_TEMPORAL_TASK_QUEUE_BAMBUDDY must not be empty`;
  `print_run_retention_seconds must be greater than 0`), and a settings API round trip
  of `print_run_retention_seconds` (set 604800, read back; `null` clears).
- [ ] **Step 2: Run, verify they fail.**
- [ ] **Step 3: Implement**, mirroring `temporal_task_queue_render` (settings.py:178, the
  validator at :203, the `boot` classification at :355) and the stored-field pattern of
  `event_log_retention_seconds` (settings_store.py:285, api/settings.py:110).
- [ ] **Step 4: Run, verify they pass.**
- [ ] **Step 5: Commit** `feat(settings): the bambuddy task queue and print run retention (#1052)`.

---

### Task 3: `start_command`, the shared update-with-start helper

**Files:**
- Create: `backend/scadbuddy/workflows/commands.py`
- Test: `backend/tests/workflows/test_commands.py` (`requires_temporal`; a test-only workflow `EchoCommand` defined in the test module)

**Interfaces:**

```python
COMMAND_ANSWER_DEADLINE = timedelta(seconds=10)
#: Retry-After for a command still being accepted.
RETRY_AFTER_SECONDS = 2

class CommandStillAcceptingError(Exception):
    """The command's Update did not answer within the deadline; the execution goes on,
    and the same request attaches to it."""

class AlreadyClosedError(Exception):
    """The ID's last execution closed and the reuse policy refuses a new one: answer
    from our record."""

async def start_command(
    client: Client,
    workflow: str,          # the workflow type name, e.g. "PrintRun"
    arg: BaseModel,
    *,
    id: str,
    task_queue: str,
    update: str,            # e.g. "accepted"
    result_type: type[T],
    reuse: WorkflowIDReusePolicy,
    search_attributes: TypedSearchAttributes | None = None,
    deadline: timedelta = COMMAND_ANSWER_DEADLINE,
) -> T: ...
```

It builds `WithStartWorkflowOperation(workflow, arg, id=id, task_queue=task_queue,
id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING, id_reuse_policy=reuse, ...)`,
awaits `client.execute_update_with_start_workflow(update, start_workflow_operation=op,
result_type=result_type)` under `asyncio.timeout(deadline)`, maps `TimeoutError` to
`CommandStillAcceptingError` and `WorkflowAlreadyStartedError` to `AlreadyClosedError`.

- [ ] **Step 1: Failing tests** with `EchoCommand` (its `accepted` Update returns a
  counter of how many Updates it has had, then the run waits for a `finish` signal):
  - `test_the_first_call_starts_and_answers_from_the_update`
  - `test_a_second_call_while_running_attaches_to_the_same_execution` (answer 2, one execution)
  - `test_failed_only_refuses_a_new_start_after_a_completed_execution` (`AlreadyClosedError`)
  - `test_allow_duplicate_starts_a_new_execution_after_one_completed`
  - `test_an_update_slower_than_the_deadline_is_still_accepting` (deadline 0.2 s, the Update sleeps 1 s)
- [ ] **Step 2: Run, verify they fail.**
- [ ] **Step 3: Implement** `commands.py`.
- [ ] **Step 4: Run, verify they pass.**
- [ ] **Step 5: Commit** `feat(workflows): start_command, update-with-start for every command (#1052)`.

---

### Task 4: `PrintRun` and its activities

**Files:**
- Modify: `backend/scadbuddy/bambuddy/dispatch.py`, `backend/scadbuddy/bambuddy/print_run.py`
- Create: `backend/scadbuddy/workflows/print_models.py`, `workflows/printing.py`,
  `workflows/print_activities.py`; modify `workflows/client.py`
- Test: `backend/tests/bambuddy/test_dispatch.py` (the split), `backend/tests/workflows/test_print_workflow.py` (new, `requires_temporal`, fake activities registered under the real names)

**Interfaces:**
- `dispatch.py`: `start_slice(client, library_file_id, plan, plate_id) -> SliceStarted(job_id, preset_key)`; `wait_slice(client, job_id) -> int` (the sliced file id; raises the 502 `ApiError`s `slice_and_queue` raises today); `enqueue_plate(client, *, sliced, printer_id, filaments, plate_id, copies, project_id, options) -> int` (the queue item id). `slice_and_queue` composes them and keeps its signature.
- `print_run.py`: `plan_run(client, source, settings, request, prepared: PreparedPlates) -> PlannedRun` (everything in `execute_run` before the plate loop, and the warnings it computes); `queued_result(client, planned, outcomes) -> PrintRunResult`. `execute_run` composes `plan_run`, the plate loop over the dispatch pieces, and `queued_result`, so `check_print` and existing unit tests keep working.
- `print_models.py`:

```python
class SourceSpec(BaseModel):
    kind: Literal["output", "library"]
    output_id: str | None = None
    file_id: int | None = None
    #: Computed by `print_accept` before the record, so a model.json that refuses
    #: them refuses the run (#770).
    stem: str | None = None
    print_settings: dict[str, str] = Field(default_factory=dict)

class PrintRunInput(BaseModel):
    subject: str            # an output id or "library:<file id>"
    slug: str
    key: str                # run_key(subject, request)
    has_request_id: bool
    source: SourceSpec
    request: PrintRunRequest

class PreparedPlates(BaseModel):      # PreparedRun without the catalogue
    plate_ids: list[int]
    printer_id: int
    printer_status: PrinterStatus | None

class Accepted(BaseModel):
    run: PrintRun
    source: SourceSpec
    prepared: PreparedPlates

class AcceptAnswer(BaseModel):        # the `accepted` Update's result
    run: PrintRun | None = None
    refusal: PrintRunError | None = None
    repeated: bool = False

class PlatePlan(BaseModel):
    plate_id: int
    plan: SlicePlan
    filaments: QueueFilaments | None
    warnings: list[FilamentWarning]

class PlannedRun(BaseModel):
    library_file_id: int
    folder_id: int | None
    project_id: int | None
    copies: int
    options: PrintOptions
    plates: list[PlatePlan]
    warnings: list[FilamentWarning]   # low-filament across plates + hardware
```

- `printing.py`: `@workflow.defn(name="PrintRun") class PrintRunWorkflow` with
  `@workflow.update(name="accepted") async def accepted(self) -> AcceptAnswer` (waits for
  the row or the refusal; the first Update is not a repeat, every later one is) and
  `@workflow.run async def run(self, input: PrintRunInput) -> PrintRun`.
- `print_activities.py`: `PrintDeps(settings_store, outputs, uploads, catalogue, store: PrintRunStore, observer, watcher)`; `PrintActivities(deps)` with activities named `print_accept`, `print_plan`, `print_slice_start`, `print_slice_wait`, `print_enqueue`, `print_record`, `print_finish`, `print_fail`, `print_start_enqueue`.
- `client.py`: `print_worker(client, task_queue, activities) -> Worker` (unversioned:
  `PrintRun` is patched with `workflow.patched` when it changes; see Task 4 Step 5).

The workflow body (the shape every later command copies):

```python
@workflow.run
async def run(self, input: PrintRunInput) -> PrintRun:
    try:
        accepted = await workflow.execute_activity(
            "print_accept", input, result_type=Accepted,
            start_to_close_timeout=ACCEPT_TIMEOUT, retry_policy=READ_RETRY,
        )
    except ActivityError as error:
        self.refusal = refusal_of(error)        # PrintRunError from the ApplicationError details
        await workflow.wait_condition(workflow.all_handlers_finished)
        raise ApplicationError(self.refusal.detail, type="PrintRefused", non_retryable=True)
    self.run_row = accepted.run
    try:
        result = await self._print(input, accepted)
        self.run_row = await self._activity("print_finish", FinishInput(...), PrintRun)
    except (ActivityError, ApplicationError, asyncio.CancelledError) as error:
        # The record exists: whatever happened is recorded, and the execution completes.
        self.run_row = await self._activity("print_fail", FailInput(run_id, slug, error_of(error)), PrintRun)
    if self.run_row.status == "succeeded" or self.run_row.may_have_queued:
        await workflow.sleep(REPEAT_WINDOW)      # repeats inside the window get this row
    await workflow.wait_condition(workflow.all_handlers_finished)
    return self.run_row
```

`_print` runs `print_plan`, then per plate `print_slice_start` (`maximum_attempts=1`),
`print_slice_wait` (heartbeat 30 s, start-to-close 660 s), `print_start_enqueue` (once,
before the first enqueue; sets workflow state `enqueue_attempted` first), `print_enqueue`
(`maximum_attempts=1`), `print_record`. `print_finish` builds the result
(`queued_result`), remembers the project, records `succeeded`, then calls the observer's
and watcher's `started` for an output. `print_fail` and `print_finish` use
`PROJECT_RETRY` (unlimited, as `workflows/pipelines.py`'s `project`).

- [ ] **Step 1: Failing dispatch tests** for the three pieces
  (`test_start_slice_posts_once_and_returns_the_job`, `test_wait_slice_raises_bambuddys_words_on_failure`,
  `test_enqueue_plate_sends_the_item_once`) and that `slice_and_queue` still passes its
  existing tests.
- [ ] **Step 2: Failing workflow tests** (`tests/workflows/test_print_workflow.py`, fake
  activities under the real names, a real worker on the dev server):
  - `test_a_refusal_answers_the_update_and_fails_the_execution_with_no_record`
  - `test_an_accepted_run_answers_the_row_then_succeeds`
  - `test_a_second_update_is_a_repeat_with_the_same_row`
  - `test_an_unexpected_activity_error_after_the_record_completes_the_workflow`
  - `test_an_enqueue_failure_records_may_have_queued_and_holds_the_window`
  - `test_slice_start_and_enqueue_have_maximum_attempts_one` (reads the history's
    `ActivityTaskScheduled` events and asserts `retry_policy.maximum_attempts == 1`)
  - `test_a_failure_before_any_enqueue_closes_at_once`
- [ ] **Step 3: Run, verify they fail.**
- [ ] **Step 4: Implement** dispatch's split, `plan_run`/`queued_result`, the models,
  the activities and the workflow. Refusals: `print_accept` raises
  `ApplicationError(detail, PrintRunError(...), type="PrintRefused", non_retryable=True)`
  for every `ApiError` (a refusal or a Bambuddy read that failed: nothing was written);
  any other exception is retried by `READ_RETRY` (3 attempts) and then also refuses, as a
  500 `UNEXPECTED_DETAIL`. After the record, `ApiError` from any activity becomes a
  non-retryable `ApplicationError` carrying its `PrintRunError`.
- [ ] **Step 5: Run, verify they pass**; `ruff`, `mypy`.
- [ ] **Step 6: Commit** `feat(print): PrintRun, the print dialog's run as a Temporal workflow (#1052)`.

---

### Task 5: The routes on `start_command`, and the worker in the API

**Files:**
- Modify: `backend/scadbuddy/api/printing.py` (`accept_run`, `post_run`), `api/library_print.py`, `api/deps.py` (`print_runs` → `print_run_store`; `PrintRunsDep` → `PrintRunStoreDep`), `main.py` (the print worker task), `worker.py` (`--queue` is not added in this phase; see Deviations)
- Test: `backend/tests/api/test_print_runs.py`, `tests/api/test_print_library.py`, `tests/api/conftest.py` (`temporal_task_queue_bambuddy` per test, reaped like the render queue)

**Interfaces:**
- `accept_run(store, temporal, settings_env, response, *, subject, slug, request, source: SourceSpec) -> PrintRun`
- The lifespan starts `print_worker` on `settings.temporal_task_queue_bambuddy` with
  `PrintDeps` from `AppState`, connecting eagerly with retry (`connect` every 5 s until it
  answers), and stops it on shutdown with `graceful_shutdown_timeout` 30 s.

Route logic:

```python
key = run_key(subject, request)
has_id = request.request_id is not None
repeated = await store.find(key, has_request_id=has_id)
if repeated is not None:
    response.status_code = 200
    return repeated.model_copy(update={"repeated": True})
try:
    answer = await start_command(temporal, "PrintRun", PrintRunInput(...), id=f"print-{key}",
        task_queue=settings.temporal_task_queue_bambuddy, update="accepted",
        result_type=AcceptAnswer,
        reuse=ALLOW_DUPLICATE_FAILED_ONLY if has_id else ALLOW_DUPLICATE)
except AlreadyClosedError:
    found = await store.find(key, has_request_id=True)   # any age: the closed execution's row
    ...200 repeated, or 503 if somehow none
except CommandStillAcceptingError:
    raise ApiError(503, "...", type_="command-still-accepting", headers={"Retry-After": "2"})
except RPCError:
    raise ApiError(503, "ScadBuddy cannot reach Temporal ...", type_="temporal-unavailable", headers={"Retry-After": "5"})
if answer.refusal is not None:
    raise ApiError(answer.refusal.status, answer.refusal.detail, title=..., type_=..., **answer.refusal.extensions)
if answer.repeated: response.status_code = 200
return answer.run.model_copy(update={"repeated": answer.repeated})
```

- [ ] **Step 1: Update the API tests.** Keep every behavior test in
  `tests/api/test_print_runs.py` and `test_print_library.py` as it is. Delete the
  heartbeat/lost tests (`test_a_run_whose_process_died_*`, `_insert_run`,
  `test_a_live_run_found_by_its_key_*` becomes a `find` test over a row the store
  inserted). Add:
  - `test_two_posts_that_race_get_one_run` (two threads POST the same body; one 202, one 200, one row)
  - `test_a_request_id_retry_after_a_recorded_failure_returns_that_run` (a `request_id` body whose run fails before enqueue; the retry is 200 with that failed run, and the workflow is not started again)
  - `test_temporal_unreachable_is_a_503_and_writes_nothing` (the app's Temporal client replaced with one on a closed port)
  - `test_the_run_survives_the_app_that_accepted_it` (gate the slice, close the `TestClient`, open a new app on the same schema and queue, open the gate; the run ends `succeeded`)
- [ ] **Step 2: Run, verify the new ones fail.**
- [ ] **Step 3: Implement** the route, the deps and the lifespan worker; delete `PrintRuns`.
- [ ] **Step 4: Run** `uv run --frozen pytest tests/api/test_print_runs.py tests/api/test_print_library.py tests/api/test_print_run_choices.py tests/api/test_print_run_plan_options.py tests/api/test_print_progress.py -v` with Postgres and Temporal up; all pass. Then the whole backend suite.
- [ ] **Step 5: Commit** `feat(print): both print routes start PrintRun with update-with-start (#1052)`.

---

### Task 6: Retention in Settings → Printing, and the docs

**Files:**
- Modify: the frontend's Printing settings panel, `frontend/src/mocks/...` for the settings handler, `CLAUDE.md` (Layout: `workflows/printing.py`, `print_activities.py`, `commands.py`), `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` (§10 phase 1: the three deviations above, pointing at #1060)
- Test: the panel's vitest

- [ ] **Step 1: Failing vitest**: the Printing section shows "Keep print runs" with
  "Forever" (empty) or a number of days, and saving 7 sends
  `print_run_retention_seconds: 604800`; clearing sends `null`.
- [ ] **Step 2: Run, verify it fails.**
- [ ] **Step 3: Implement**, following how the panel already edits a numeric stored setting.
- [ ] **Step 4: `pnpm lint && pnpm typecheck && pnpm test`.**
- [ ] **Step 5: Commit** `feat(settings): keep print runs for a chosen number of days (#1052)`.

---

## Self-review

- Spec coverage: §4.2 steps 1-5 → Tasks 3-5; §5.1 → Tasks 4-5; §5.2 → Tasks 1, 4, 5; §5.3 → Task 4; §5.4 → Tasks 1, 2, 6; §5.5 → deviation 1 (#1060); §5.6 → no change (Task 5 keeps the answers); §8's print list → Tasks 4-5 (the killed worker mid-enqueue is `test_an_enqueue_failure_records_may_have_queued_*` plus `maximum_attempts = 1`).
- `operations` and the Search Attribute registration are deviations 2 and 3.
