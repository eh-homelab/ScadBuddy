# Phase 2a: the `operations` record and Bambuddy commands — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every Bambuddy write route except the print run (already `PrintRun`, #1052) runs as a
Temporal command in the §4.2 shape, recorded in a generic `operations` table, with one client
helper in the browser and one in the agent that send an idempotency key and follow a 202.

**Architecture:** One workflow type, `Operation`, serves every generic kind. A kind is a pair of
activities, `op.<kind>.check` (refusals, writes nothing) and `op.<kind>.run` (the effect), plus
its answer and retry policy, declared in a registry. The workflow runs check, then
`op_insert` (the record and its event, idempotent on the execution), then run, then
`op_finish`; once the record exists every outcome completes the execution. The route starts
it with `start_command` and its `accepted` Update; every kind in this plan is a `done` kind,
so the route answers today's body, or 202 with the operation past the deadline.

**Tech Stack:** Python 3.12, FastAPI, temporalio 1.33, psycopg 3, Postgres 17; React 19 +
msw; the agent's TypeScript tools over openapi-fetch.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §4.1–§4.3,
§8 ("The command shape"). Phase 1's plan, whose shape this copies:
`docs/superpowers/plans/2026-10-02-durable-phase-1-printrun.md`. Stacked on PR #1061
(`workflows/commands.py` `start_command` and the `PrintRun` worker).

## Scope: phase 2 is three plans

§10 item 2 is three pieces that share only `start_command`. Each gets its own plan and PR,
in this order:

1. **2a (this plan):** `operations`, `GET /operations/{id}`, the `Operation` workflow, the
   Bambuddy kinds, `command()` in the browser and the agent.
2. **2b:** renders on the command shape (§4.5): `render-<render_key>`, claims in workflow
   state, `reconcile_once` deleted.
3. **2c:** `FollowPrint` (§4.4, §5.3) replacing `bambuddy/watcher.py`.

## Rulings this plan makes (record each in the ledger at the task that applies it)

1. **Every kind here answers `done`.** The spec lists send as `accepted`. A `done` kind
   answers today's body when the effect finishes inside `command_answer_deadline` (10 s) and
   202 with the operation otherwise, so a send that is quick keeps today's contract and a slow
   one is followed by `command()`. Cost if wrong: a slow send's client follows a 202 instead of
   getting one.
2. **The client's `request_id` is the `Idempotency-Key` header**, not a body field: two of the
   routes have no body (`reprint`, `register-sidebar`) and the rest have bodies that are
   today's API. Without the header the route mints a key of its own, so each request is a new
   command, as today.
3. **Analyzer fix apply is not a command.** It writes Postgres only (`api/analyzers.py`
   `post_apply`, "Nothing is sent to Bambuddy"): §4.1's single-transaction write.
4. **Output delete's Bambuddy part moves with the library commands (phase 3)**: the route
   deletes the output from the data volume as well, which is the `library` queue's.
5. **A kind's effect runs once unless the kind says otherwise.** Bambuddy dedupes none of
   them except the sidebar link (an upsert by name) and the send (`ensure_uploaded` reuses
   the inbox copy), which take `READ_RETRY`.

## Global Constraints

- Workflow ID `op-<kind>-<key>`; key = sha256 of kind, subject, canonical body and the
  `Idempotency-Key` (§4.2 step 1).
- `id_conflict_policy = USE_EXISTING`; `id_reuse_policy = ALLOW_DUPLICATE_FAILED_ONLY` (§4.2).
- The route reads `operations` by key before it calls Temporal (§4.2 "The record lookup comes
  first").
- The insert is `ON CONFLICT (workflow_id, workflow_run_id) DO NOTHING`, re-reads on conflict,
  and publishes only when it inserted, in the same transaction (§4.2 step 3).
- The check activity's `start_to_close_timeout` is 8 s (§4.2 step 4).
- Search Attributes `ScadbuddyKind` (`operation.<kind>`), `ScadbuddySubject`,
  `ScadbuddyStatus`, upserted only when `SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES` is true
  (phase 1's ruling).
- Pruning: `operation_retention_seconds`, a Postgres setting, `None` keeps every row (§4.2
  "Our record").
- New migrations are new files `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql` (CLAUDE.md).
- Never commit `backend/openapi.json` or a `schema.d.ts`.

## Review Focus

1. **A retried request after its command completed** must answer the recorded outcome,
   including a recorded *failure*, and run nothing (Task 3,
   `test_a_retry_after_a_recorded_failure_answers_it_and_runs_nothing`).
2. **An effect that raised after the record** (Bambuddy 500 on the reprint enqueue) must leave
   the operation `failed` with the problem, the execution completed, and the route answering
   today's problem status (Task 2, `test_an_effect_failure_completes_with_the_problem`).
3. **A request with no `Idempotency-Key`** must behave as today: two identical reprints are
   two queue items (Task 4, `test_reprint_without_a_key_queues_twice`).
4. **A `done` command slower than the deadline** must answer 202 with the operation, and
   `GET /operations/{id}` must reach its result (Task 3,
   `test_a_slow_done_command_answers_202_and_is_followed`).
5. **The browser's `command()` given a 202** must follow `GET /operations/{id}` to the result
   and turn a failed operation into the same `ApiError` the route would have thrown (Task 5,
   `follows a 202 to the operation's result`).

---

## File structure

| File | Responsibility |
|---|---|
| `backend/scadbuddy/migrations/<ts>_operations.sql` (new) | the `operations` table |
| `backend/scadbuddy/operations/store.py` (new) | `Operation`, `OperationStore`: find, get, insert, finish, prune |
| `backend/scadbuddy/operations/kinds.py` (new) | `OperationKind`, the registry `KINDS`, `operation_key` |
| `backend/scadbuddy/workflows/operation.py` (new) | `OperationWorkflow` and its models |
| `backend/scadbuddy/workflows/operation_activities.py` (new) | `op_insert`, `op_finish`, and each kind's check/run as activities |
| `backend/scadbuddy/bambuddy/operations.py` (new) | the Bambuddy kinds' check and run bodies, moved out of the routes |
| `backend/scadbuddy/api/operations.py` (new) | `run_operation` (the route helper) and `GET /operations/{id}` |
| `backend/scadbuddy/core/events.py` | `OperationEvent` (`operation.changed`) |
| `backend/scadbuddy/library/settings_store.py`, `api/settings.py` | `operation_retention_seconds` |
| `backend/scadbuddy/api/outputs.py`, `printing.py`, `print_history.py`, `settings.py` | the routes call `run_operation` |
| `backend/scadbuddy/workflows/client.py`, `main.py` | the `bambuddy` worker also serves `Operation` |
| `frontend/src/api/client.ts` | `command()`; the Bambuddy calls use it |
| `frontend/src/pages/SettingsPage.tsx` | the retention field |
| `agent/src/api/command.ts` (new) | the agent's `command()` |
| `agent/src/tools/print.ts`, `prints.ts`, `coverage.ts` | the tools use it; `get_operation` tool |

---

### Task 1: The `operations` record

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_operations.sql`, `backend/scadbuddy/operations/__init__.py`, `backend/scadbuddy/operations/store.py`
- Modify: `backend/scadbuddy/core/events.py`, `backend/scadbuddy/library/settings_store.py`, `backend/scadbuddy/api/settings.py`
- Test: `backend/tests/test_operations_store.py`, `backend/tests/api/test_settings.py`

**Interfaces:**
- Produces: `Operation` (pydantic: `id, kind, subject, status: Literal["running","succeeded","failed"], result: dict | None, error: PrintRunError | None, created_at, finished_at`), `OperationStore(pool, *, events)` with
  `async find(operation_key) -> Operation | None`, `async get(id) -> Operation | None`,
  `async insert(op_id, *, kind, subject, operation_key, request, workflow_id, workflow_run_id, retention) -> Operation`,
  `async finish(op_id, *, result=None, error=None) -> Operation`;
  `OperationEvent(kind="operation.changed", operation_id, op_kind, subject)`.

Migration:

```sql
CREATE TABLE operations (
    id text PRIMARY KEY,
    kind text NOT NULL,
    subject text NOT NULL,
    operation_key text NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    request jsonb NOT NULL,
    result jsonb,
    error jsonb,
    workflow_id text NOT NULL,
    workflow_run_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE UNIQUE INDEX operations_execution ON operations (workflow_id, workflow_run_id);
CREATE INDEX operations_operation_key ON operations (operation_key, created_at DESC);
CREATE INDEX operations_finished ON operations (finished_at) WHERE finished_at IS NOT NULL;
```

- [ ] **Step 1: Write the failing store tests** (`requires_postgres`, the `pg_conninfo` fixture as `tests/test_print_runs_store.py` uses it):

```python
async def test_insert_twice_for_one_execution_returns_the_first_row_and_publishes_once(store, events):
    first = await store.insert("a", kind="reprint", subject="archive:5", operation_key="k", request={},
                               workflow_id="op-reprint-k", workflow_run_id="r1", retention=None)
    again = await store.insert("b", kind="reprint", subject="archive:5", operation_key="k", request={},
                               workflow_id="op-reprint-k", workflow_run_id="r1", retention=None)
    assert again.id == first.id == "a"
    assert [e.operation_id for e in events.published] == ["a"]

async def test_find_returns_the_keys_newest_operation_whatever_its_status(store):
    await insert(store, "a", key="k", run="r1")
    await store.finish("a", error=PrintRunError(status=502, title="Bad Gateway", detail="x"))
    found = await store.find("k")
    assert found is not None and found.id == "a" and found.status == "failed"

async def test_finish_changes_only_a_running_row(store):
    await insert(store, "a", key="k", run="r1")
    done = await store.finish("a", result={"queue_item_id": 7})
    again = await store.finish("a", error=PrintRunError(status=500, title="x", detail="y"))
    assert done.status == again.status == "succeeded" and again.result == {"queue_item_id": 7}

async def test_insert_prunes_operations_finished_before_the_retention(store, pg_conninfo):
    await insert(store, "old", key="k1", run="r1")
    await store.finish("old", result={})
    backdate(pg_conninfo, "old", days=2)
    await insert(store, "new", key="k2", run="r2", retention=timedelta(days=1))
    assert await store.get("old") is None
```

- [ ] **Step 2: Run them, expect ImportError/FAIL**: `uv run --frozen pytest tests/test_operations_store.py -v`.
- [ ] **Step 3: Implement** `operations/store.py` on `PrintRunStore`'s pattern (`bambuddy/runs.py` `_insert`, `_finish`): `asyncio.to_thread` bodies, one transaction per write, `RETURNING`, `ON CONFLICT (workflow_id, workflow_run_id) DO NOTHING` then re-read, `events.publish_in(conn, OperationEvent(...))` only when inserted and on a finish that changed the row. Add `OperationEvent` to `core/events.py` and to the `Event` union:

```python
class OperationEvent(BaseEvent):
    """An operation (§4.2) was accepted or ended: re-read ``GET /operations/{operation_id}``."""

    kind: Literal["operation.changed"] = "operation.changed"
    operation_id: str
    op_kind: str
    subject: str
```

- [ ] **Step 4: The setting.** Add `operation_retention_seconds: float | None = None` beside `print_run_retention_seconds` in `StoredSettings`, `SettingsPatch` (`Field(default=None, gt=0)`) and `SettingsView`; extend `tests/api/test_settings.py` DEFAULTS and add a round-trip and a refused-zero test, as phase 1 Task 2 did for `print_run_retention_seconds`.
- [ ] **Step 5: Run** `uv run --frozen pytest tests/test_operations_store.py tests/api/test_settings.py tests/test_events.py -q`, ruff, mypy. Expected: PASS.
- [ ] **Step 6: Commit** `feat(operations): the operations record (#1053)`.

### Task 2: The `Operation` workflow

**Files:**
- Create: `backend/scadbuddy/operations/kinds.py`, `backend/scadbuddy/workflows/operation.py`, `backend/scadbuddy/workflows/operation_activities.py`
- Test: `backend/tests/test_operation_workflow.py`

**Interfaces:**
- Consumes: `OperationStore`, `Operation` (Task 1); `start_command` (phase 1).
- Produces:
  - `OperationKind(name: str, check: CheckFn, run: RunFn, run_retry: RetryPolicy)` where `CheckFn = Callable[[OperationDeps, dict], Awaitable[dict]]` (raises `ApiError` to refuse; returns what `run` needs) and `RunFn = Callable[[OperationDeps, dict, dict], Awaitable[dict]]` (returns the result body).
  - `OperationInput(kind, subject, key, request: dict, run_attempts: int, search_attributes=False)`, `OperationAnswer(operation: Operation | None, refusal: PrintRunError | None, repeated: bool)`.
  - `OPERATION_WORKFLOW = "Operation"`, Update `accepted`.
  - `operation_activities(deps: OperationDeps, kinds: Mapping[str, OperationKind]) -> list[Callable]`: `op_insert`, `op_finish`, and per kind `op.<kind>.check`, `op.<kind>.run` (each wraps the kind's function; an `ApiError` leaves as a non-retryable `ApplicationError` with `problem(error)`, `REFUSED` from check, `FAILED` from run, as `print_activities._raised` does).

The workflow, in the order §4.2 gives:

```python
@workflow.defn(name=OPERATION_WORKFLOW)
class OperationWorkflow:
    def __init__(self) -> None:
        self.done: Operation | None = None
        self.refusal: PrintRunError | None = None
        self.updates = 0

    @workflow.update(name=ACCEPTED_UPDATE)
    async def accepted(self) -> OperationAnswer:
        # A `done` kind: the answer is the finished operation (§4.2 step 4).
        self.updates += 1
        await workflow.wait_condition(lambda: self.done is not None or self.refusal is not None)
        return OperationAnswer(operation=self.done, refusal=self.refusal, repeated=self.updates > 1)

    @workflow.run
    async def run(self, input: OperationInput) -> Operation:
        try:
            checked = await workflow.execute_activity(
                f"op.{input.kind}.check", input.request, result_type=dict,
                start_to_close_timeout=CHECK_TIMEOUT, retry_policy=READ_RETRY)
        except ActivityError as error:
            self.refusal = _problem(error)
            await workflow.wait_condition(workflow.all_handlers_finished)
            raise ApplicationError(self.refusal.detail, type=REFUSED, non_retryable=True) from None
        op = await workflow.execute_activity(
            "op_insert", InsertOp(input=input), result_type=Operation,
            start_to_close_timeout=SHORT, retry_policy=RECORD_RETRY)
        try:
            result = await workflow.execute_activity(
                f"op.{input.kind}.run", RunOp(request=input.request, checked=checked),
                result_type=dict, start_to_close_timeout=RUN_TIMEOUT,
                retry_policy=RunRetry.of(input.run_attempts))
            self.done = await self._finish(op.id, result=result)
        except (ActivityError, ApplicationError, asyncio.CancelledError) as error:
            self.done = await self._finish(op.id, error=_problem(error))
        await workflow.wait_condition(workflow.all_handlers_finished)
        return self.done
```

`run_attempts` travels in `OperationInput` (the route copies it from the kind), so the workflow
needs no registry and stays deterministic. `_problem` is `workflows/printing.py`'s; move it to
`workflows/problems.py` and import it from both. `CHECK_TIMEOUT = 8 s` (§4.2 step 4),
`RUN_TIMEOUT = 5 min` (one 3MF upload, `DEFAULT_UPLOAD_TIMEOUT` 180 s, plus margin).

- [ ] **Step 1: Failing tests** with a test-only kind registered as fake activities under the real names (`tests/test_print_workflow.py`'s pattern), on the dev server:

```python
async def test_a_refusal_answers_the_update_and_writes_nothing(client, worker, fake):
    fake.refuse = True
    answer = await start(client, worker, op_input())
    assert answer.refusal == REFUSAL and fake.calls == ["check"]

async def test_a_done_kind_answers_the_finished_operation(client, worker, fake):
    answer = await start(client, worker, op_input())
    assert answer.operation is not None and answer.operation.status == "succeeded"
    assert fake.calls == ["check", "insert", "run", "finish:ok"]

async def test_an_effect_failure_completes_with_the_problem(client, worker, fake):
    fake.run_error = ApplicationError("x", BAMBUDDY_502, type=FAILED, non_retryable=True)
    arg = op_input()
    answer = await start(client, worker, arg)
    assert answer.operation is not None and answer.operation.error == BAMBUDDY_502
    assert (await ended(client, arg)).status == "failed"   # completed, not Failed

async def test_the_run_activity_takes_the_kinds_attempts(client, worker, fake):
    arg = op_input(run_attempts=1)
    await start(client, worker, arg)
    assert await scheduled_attempts(client, arg, "op.test.run") == 1

async def test_a_second_update_is_a_repeat(client, worker, fake):
    arg = op_input(hold=True)        # the fake's run waits for a signal
    first, second = await asyncio.gather(start(client, worker, arg), start(client, worker, arg))
    assert sorted([first.repeated, second.repeated]) == [False, True]
    assert fake.calls.count("run") == 1
```

- [ ] **Step 2: Run, expect FAIL** (`uv run --frozen pytest tests/test_operation_workflow.py -v`).
- [ ] **Step 3: Implement** `kinds.py`, `operation.py`, `operation_activities.py`, `workflows/problems.py`; the module that defines the workflow imports scadbuddy modules only under `workflow.unsafe.imports_passed_through()`.
- [ ] **Step 4: Run** the test file and `tests/test_print_workflow.py` (the moved `_problem`). Expected: PASS.
- [ ] **Step 5: Commit** `feat(operations): the Operation workflow (#1053)`.

### Task 3: The route helper, `GET /operations/{id}`, and the worker

**Files:**
- Create: `backend/scadbuddy/api/operations.py`
- Modify: `backend/scadbuddy/api/deps.py` (an `OperationCommands` beside `PrintCommands`), `backend/scadbuddy/workflows/client.py` (`print_worker` → `bambuddy_worker`, registering `OperationWorkflow` and the operation activities too), `backend/scadbuddy/main.py`
- Test: `backend/tests/api/test_operations.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces: `async run_operation(ops: OperationCommands, response: Response, *, kind: OperationKind, subject: str, request: BaseModel | dict, idempotency_key: str | None) -> dict | Operation`. It returns the result body (the route answers it with its usual status), raises the refusal or the recorded error as `ApiError`, or sets 202 and returns the `Operation`. `GET /api/v1/operations/{operation_id} -> Operation`. `IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]`.

Route logic (phase 1's `accept_run` without the repeat window):

```python
key = operation_key(kind.name, subject, body, idempotency_key or uuid.uuid4().hex)
recorded = await ops.store.find(key)
if recorded is not None:
    return _answer(recorded, response, repeated=True)
try:
    answer = await start_command(ops.client, OPERATION_WORKFLOW, arg, id=f"op-{kind.name}-{key}",
                                 task_queue=ops.task_queue, update=ACCEPTED_UPDATE,
                                 result_type=OperationAnswer,
                                 reuse=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
except AlreadyClosedError:
    recorded = await ops.store.find(key)
    if recorded is None:
        raise ApiError(409, "This operation has ended and left no record. Try again.") from None
    return _answer(recorded, response, repeated=True)
except CommandStillAcceptingError:
    # A `done` kind past the deadline: the record may exist by now (§4.2 step 4).
    recorded = await ops.store.find(key)
    if recorded is not None:
        response.status_code = 202
        return recorded
    raise ApiError(503, ..., type_=STILL_ACCEPTING_PROBLEM, headers={"Retry-After": "2"}) from None
except (RPCError, TemporalUnavailableError):
    raise ApiError(503, ..., type_=TEMPORAL_UNAVAILABLE_PROBLEM, headers={"Retry-After": "5"}) from None
```

`_answer` returns `op.result` for `succeeded`, raises `ApiError(**op.error)` for `failed`, and
202 with the operation for `running`. `STILL_ACCEPTING_PROBLEM` and
`TEMPORAL_UNAVAILABLE_PROBLEM` move from `api/printing.py` to `api/operations.py`.

- [ ] **Step 1: Failing API tests** (the `client`/`app` fixtures; a test-only kind registered through `app.state` before the lifespan, so these tests are independent of Task 4):

```python
def test_a_done_operation_answers_its_result(client): ...
def test_a_retry_with_the_same_key_answers_the_record_and_runs_nothing(client, fake_kind): ...
def test_a_retry_after_a_recorded_failure_answers_it_and_runs_nothing(client, fake_kind): ...
def test_a_slow_done_command_answers_202_and_is_followed(client, fake_kind):
    fake_kind.delay = 12   # past COMMAND_ANSWER_DEADLINE
    started = post(client, key="k")
    assert started.status_code == 202
    assert follow(client, started.json()["id"])["status"] == "succeeded"
def test_get_operation_404s_an_unknown_id(client): ...
def test_temporal_unreachable_is_a_503_and_writes_nothing(client, app, pg_conninfo): ...
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**; rename `print_worker` to `bambuddy_worker(client, task_queue, activities)` (workflows `[PrintRunWorkflow, OperationWorkflow]`), and in `main._run_print_worker` add `operation_activities(...)` to the activities list.
- [ ] **Step 4: Run** `tests/api/test_operations.py tests/api/test_print_runs.py tests/test_print_worker_task.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(operations): routes start operations with update-with-start (#1053)`.

### Task 4: The Bambuddy kinds

**Files:**
- Create: `backend/scadbuddy/bambuddy/operations.py`
- Modify: `backend/scadbuddy/api/outputs.py` (`send_output_to_bambuddy`, `post_project_file`), `api/printing.py` (`post_project`, `post_attach_project`), `api/print_history.py` (`reprint`, `pull_timelapse`), `api/settings.py` (`post_register_sidebar`)
- Test: the routes' existing tests (`tests/api/test_send.py`, `test_project_file.py`, `test_print_projects.py`, `test_print_history.py`, `test_settings.py`) plus `tests/api/test_bambuddy_operations.py`

**Interfaces:**
- Consumes: `OperationKind`, `run_operation`, `IdempotencyKey`.
- Produces: `BAMBUDDY_KINDS: dict[str, OperationKind]` with these entries. Each check is the route's refusals today; each run is the route's effect today, moved verbatim:

| kind | subject | check (today's refusals) | run (today's effect) | attempts |
|---|---|---|---|---|
| `send` | output id | `require_output` | `send_output` | 3 (`ensure_uploaded` reuses the copy) |
| `project_file` | output id | `require_output`, `output_stem` | `file_into_project` | 1 |
| `create_project` | `project` | — | `ensure_project` | 1 |
| `attach_project` | output id | `require_output`, `chosen_project` 409 | `owned_queue_items` + `attach_results` | 1 |
| `reprint` | `archive:<id>` | `_require_print`, archive deleted 409, no printer 409 | `client.enqueue`, `cache.forget` | 1 |
| `timelapse_pull` | `archive:<id>` | `_require_print`, archive deleted 409 | `select_timelapse`, `cache.forget` | 1 |
| `register_sidebar` | `sidebar` | — | `register_sidebar` | 3 (upsert by name) |

The check returns the values the run needs (for `reprint`: `printer_id`, `plate_id`); the run
reloads settings itself, as phase 1's activities do, so no secret enters history. Each route
becomes, for example:

```python
async def reprint(archive_id: ArchiveIdPath, response: Response, ops: OperationsDep,
                  idempotency_key: IdempotencyKey = None) -> PrintAgain:
    result = await run_operation(ops, response, kind=BAMBUDDY_KINDS["reprint"],
                                 subject=f"archive:{archive_id}",
                                 request={"archive_id": archive_id},
                                 idempotency_key=idempotency_key)
    return result if isinstance(result, Operation) else PrintAgain.model_validate(result)
```

The route's `responses=` gains `202: {"model": Operation}`. `pull_timelapse` answers 204 for a
`succeeded` result and 202 otherwise.

- [ ] **Step 1: Failing tests** in `tests/api/test_bambuddy_operations.py`:

```python
def test_reprint_with_a_key_twice_queues_once(client, ...):  # respx counts POST /queue/
def test_reprint_without_a_key_queues_twice(client, ...):
def test_a_reprint_refusal_writes_no_operation(client, pg_conninfo, ...):  # deleted archive -> 409, count(*) = 0
def test_a_send_answers_todays_body(client, ...):
def test_create_project_with_a_key_twice_creates_once(client, ...):
```

- [ ] **Step 2: Run them and the routes' existing tests; expect the new ones to FAIL and the existing ones to PASS.**
- [ ] **Step 3: Implement** `bambuddy/operations.py` and convert the seven routes; register `BAMBUDDY_KINDS` in `main._run_print_worker`.
- [ ] **Step 4: Run** the new file and every existing test file of those routes. Expected: PASS, with today's bodies and statuses unchanged.
- [ ] **Step 5: Commit** `feat(bambuddy): Bambuddy writes run as operations (#1053)`.

### Task 5: `command()` in the browser

**Files:**
- Modify: `frontend/src/api/client.ts`, `frontend/src/pages/SettingsPage.tsx`, `frontend/src/mocks/fixtures.ts`, `frontend/src/mocks/features/` (an `operations.ts` with `handlers` for `GET /api/v1/operations/:id`)
- Test: `frontend/src/api/client.test.ts`, `frontend/src/pages/SettingsPage.test.tsx`

**Interfaces:**
- Produces: `command<T>(path: string, init: RequestInit & { signal?: AbortSignal }): Promise<T>`. It sends `Idempotency-Key: newRequestId()` (one per call; re-sends reuse it); a 2xx other than 202 is `T`; a 202 is an `Operation` followed through `GET /operations/{id}` every `printRunPoll.intervalMs` until it ends, for at most `printRunPoll.operationFollowMs` (15 min; past it, a 504 `urn:scadbuddy:operation-unfinished` saying it may have been done: review #1063, so the follow is bounded, not a deferred gap); `succeeded` → its `result` as `T`; `failed` → `new ApiError(error.status, error)`; re-sends while `unanswered()` (the same rule `reattach` uses, including `command-still-accepting`).
- `sendOutput`, `createProject`, `fileIntoProject`, `attachToProject`, `reprint`, `pullTimelapse`, `registerSidebar` call `command()` instead of `request()`.

- [ ] **Step 1: Failing tests:** `sends one Idempotency-Key and re-sends it after an unanswered answer`; `follows a 202 to the operation's result`; `turns a failed operation into the route's ApiError`; `reprint goes through command()` (asserts the header on the msw request); Settings: `keeps finished operations for the days given`.
- [ ] **Step 2: Run** `pnpm exec vitest run src/api/client.test.ts src/pages/SettingsPage.test.tsx`; expect FAIL.
- [ ] **Step 3: Implement** `command()` beside `reattach` and reuse `reattach` for the send; add the Settings field as phase 1's `print-run-retention` row, id `operation-retention`, label "Keep finished operations for (days)".
- [ ] **Step 4: Run** those files plus `pnpm lint && pnpm typecheck`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(frontend): command() sends a key and follows an operation (#1053)`.

### Task 6: The agent's wrapper

**Files:**
- Create: `agent/src/api/command.ts`
- Modify: `agent/src/tools/print.ts` (`send_to_bambuddy`, `create_print_project`, `file_output_in_project_folder`, `file_output_under_project`), `agent/src/tools/prints.ts` (`print_again`, `pull_print_timelapse`), a new `get_operation` tool (read tier) in `agent/src/tools/prints.ts`
- Test: `agent/test/command.test.ts`, `agent/test/tools.test.ts`, `agent/test/coverage.test.ts` (must stay green with the new route)

**Interfaces:**
- Produces: `command<T>(ctx: ToolContext, what: string, send: (headers: { 'Idempotency-Key': string }) => Promise<FetchResult<T | Operation>>): Promise<T>`, with `print.ts`'s `reattach` rules (re-send on `TypeError`, a proxy's own 502/503/504/524, and `command-still-accepting`; up to `RUN_REATTEMPTS` and then a `ToolError` that says the effect may have happened), and a 202 followed through `GET /api/v1/operations/{id}` every `ctx.pollIntervalMs` for at most `ctx.operationFollowMs` (15 min, as the browser; review #1063), then a `ToolError` naming the operation for `get_operation`.

- [ ] **Step 1: Failing tests:** `re-sends with the same Idempotency-Key`; `follows a 202 to the result`; `print_again sends an Idempotency-Key`; `get_operation reads an operation`.
- [ ] **Step 2: Run** `pnpm exec vitest run test/command.test.ts test/tools.test.ts test/coverage.test.ts`; expect FAIL.
- [ ] **Step 3: Implement**; move `print.ts`'s `unanswered` and `reattach` into `command.ts` and import them back, so the print run and the operations share one rule.
- [ ] **Step 4: Run** `pnpm lint && pnpm typecheck && pnpm test`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(agent): one command wrapper for the Bambuddy tools (#1053)`.

### Task 7: Docs

- [ ] `CLAUDE.md` layout: `backend/scadbuddy/operations/` and the new workflow files; spec §10 item 2 gets an "As built (2a)" note with the five rulings above.
- [ ] Commit `docs: operations and the Bambuddy commands (#1053)`.
