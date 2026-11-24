# Flow prompts and tasks (A1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A flow can ask a person for a typed answer (`prompt(title, schema, …)`), with no time limit unless the script sets one; the answer is checked on the server against the schema and returned to the script; `wait_for_human` loses its default and its one-day ceiling.

**Architecture:** A `tasks` table in the CRUD service holds each prompt (its schema, context, place, state, answer). The `prompt` host function opens the task through a new CRUD area, then waits on a new temporal-agent-harness callback whose call id is the task id, exactly as `wait_for_human` waits on `human_answer` today. `POST /api/v1/tasks/{id}/answer` validates the answer with `jsonschema` and resolves the callback through a new `task_answer` operation (the `flow_answer` operation's path). The run page's pending card draws a form from the schema; "Needs you" already lists parked calls through the run's `waiting_on`.

**Tech Stack:** Python 3.12, FastAPI, psycopg, Temporal + temporal-agent-harness (pinned), `jsonschema` (already a dependency), React 19 + vitest + msw.

**Spec:** `docs/superpowers/specs/2026-11-24-shop-floor-flows-design.md` §6.3, D11a. Issue #7364, epic #7361.

## Global Constraints

- Only the CRUD service holds SQL; the projects worker reaches the table only through `crud_router("projects")` routes with its `crud.projects` scope (root CLAUDE.md "Database access").
- A migration is a NEW file `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_tasks.sql`; edit no other migration.
- `ProjectWorkflow` is AUTO_UPGRADE: every recorded history in `backend/tests/fixtures/project_workflow_histories/` must still replay (`tests/flows/test_project_replay.py`). A changed command path goes behind `workflow.patched`, id format `"flow-<slug>-7364"`.
- No wait has a default limit (D11a). An explicit `timeout_s` is the only limit; its floor stays `MIN_WAIT_S` (10 s).
- A1 accepts only the `core/input_schema.py` subset in a prompt's schema (`schema_problems(formats=True)`, which also takes `format` `email`, `date` and `date-time` on a string, each checked by a dedicated `FormatChecker` that needs no dev dependency); `x-sb-widget` and the other widget hints are A2.
- An answer body is at most 16 KB of JSON (the serialized size, checked before the answer is validated; `AnswerBody` in `api/flows.py` bounds one string's characters and is not that bound). `validate_answer` runs off the event loop.
- The answer route is browser-only, as `answer_flow_run` is (`_browser_only()`).
- Never commit `backend/openapi.json` or a `schema.d.ts`.
- Backend tests need `SCADBUDDY_TEST_DATABASE_URL` and a Temporal dev server (backend/CLAUDE.md "Commands").

## Review Focus

1. A run that ends (finished, failed, cancelled, reset past the prompt) while its task is open: the task must not stay open on screens. → Task 1 `cancel_for_run_in` and `match_waiting_in`, used in the run's own transactions; Task 5 tests.
2. Two people answer one task at once: one wins, the other gets 409 and the run gets one answer. → Task 4 test.
3. The harness already resolved the call (an earlier answer whose reply was lost, then a retry): the route answers 409, and the task row ends `answered` by the run's own mark, not stuck `open`. → Task 4 and Task 5 tests.
4. An answer that is not an object, or is over 16 KB: 422 or 413 before any validation or operation. → Task 4 test.
5. A schema the subset refuses (`$ref`, a nested object, an unknown keyword): `prompt()` raises `ValueError` naming it before any task row is written. → Task 5 test.

---

### Task 1: The `tasks` table, its store and the answer check

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_tasks.sql`
- Create: `backend/scadbuddy/tasks/__init__.py`, `tasks/models.py`, `tasks/store.py`, `tasks/validate.py`, `tasks/component.py`
- Modify: `backend/scadbuddy/core/events.py` (`TaskChanged`, added to the `Event` union)
- Modify: `backend/scadbuddy/core/input_schema.py` and `backend/tests/test_input_schema.py` (the shared check, as below)
- Test: `backend/tests/tasks/test_validate.py`, `backend/tests/tasks/test_store.py`

**Interfaces:**
- Produces (`tasks/models.py`):
  - `TaskState = Literal["open", "answered", "cancelled", "expired"]`
  - `class TaskOpen(BaseModel)`: `id: str` (the harness call id), `run_id: str`, `title: str` (1–500 chars), `schema_: dict[str, Any]` (alias `schema`), `context: list[dict[str, Any]] = []`, `place: str | None = None`, `ui: str | None = None`, `expires_at: datetime | None = None`
  - `class Task(TaskOpen)`: `project_id: str | None`, `state: TaskState`, `answer: dict[str, Any] | None`, `answered_by: str | None`, `created_at: datetime`, `updated_at: datetime`
  - `class AnswerProblem(BaseModel)`: `field: str` (a path of declared property names and array indexes joined by `/`, `""` for the root), `message: str` (never holds the rejected value)
- Produces (`core/input_schema.py`, shared with flow inputs and pipelines, whose behaviour does not change):
  - `schema_problems(..., formats: bool = False, defaults: bool = True)`: with `formats`, a string (or string items) may have `format` `email`, `date` or `date-time`, and a default is checked against it; any other format is refused by name; without it `format` stays refused. `defaults=False` skips only the check of `default` values. The patterns deadline covers the whole property pass and starts after `check_schema`.
  - `FORMATS` and `FORMAT_CHECKER`: a `FormatChecker(formats=())` with exactly those three, `date-time` by our own RFC 3339 check with an offset (the `rfc3339-validator` package is a dev dependency only). `Validator(schema)` without it checks no format, as today; `validate_answer` and the default check of a `formats=True` schema pass `format_checker=FORMAT_CHECKER`.
  - `multipleOf` and `uniqueItems` in `Validator` never raise, and `uniqueItems` is linear.
  - Tests for each: `test_format_needs_the_flag`, `test_a_format_outside_the_list_is_named`, `test_a_default_is_checked_against_its_format`, `test_defaults_false_skips_only_the_defaults`, `test_the_format_checker_holds_exactly_formats`, `test_a_date_time_needs_an_offset`.
- Produces (`tasks/validate.py`): `validate_answer(schema: dict[str, Any], answer: Any) -> list[AnswerProblem]`; `prompt_schema_problems(schema: dict[str, Any]) -> list[str]` (delegates to `core/input_schema.schema_problems`).
- Produces (`tasks/store.py`, class `TaskStore` over the shared `scadbuddy-rows` pool, events through `TransactionalEvents.publish_in`):
  - `open(task: TaskOpen) -> Task` (insert; same id again returns the existing row unchanged)
  - `get(task_id: str) -> Task | None`
  - `list(*, state: TaskState | None = "open", place: str | None = None, limit: int = 100) -> list[Task]`
  - `mark_answered(task_id: str, answer: dict[str, Any], by: str | None, workflow_run_id: str, *, final: bool = False) -> Task`: locks the run row `FOR SHARE`, then the task; a run on another execution (a Reset) leaves the task alone; from `open`, or `cancelled` by the run's end, it is `answered`; the same answer again only fills a NULL `by`; anything else raises `TaskNotOpenError(state)`; with `final=True` (the run's own mark, Task 5) the answer replaces another answered or cancelled row's on the same execution, an `expired` row is returned as it stands, an execution the run superseded gets the row as it stands, and an unknown execution on an ended run gets the row as it stands, and on an open run (a row that lags Temporal) it is refused as retryable
  - `close(task_id: str, state: Literal["expired", "cancelled"]) -> Task | None` (only while `open`; returns None when it was not open)
  - `cancel_for_run(run_id: str) -> int` (every open task of the run → `cancelled`; returns the count)
  - `cancel_for_run_in(conn, run_id) -> int` and `match_waiting_in(conn, run_id, waiting_ids, done_ids) -> int`, on the caller's connection, for the run's own transactions in `flows/store.py` (Task 5). After a Reset, an open task in neither set is `cancelled`, and a task in `waiting_ids` that is not open is `open` again with its answer cleared.
  - Each state change publishes `TaskChanged` in its transaction: `class TaskChanged(BaseEvent)`: `kind: Literal["task.changed"]`, `task_id: str`, `run_id: str`, `state: TaskState`, `place: str | None`.
- Produces (`tasks/component.py`): `TASKS` component, `TasksDep` (the `core/components.py` pattern, `api/components.py` `component_dep`).

Table: `id text PRIMARY KEY`, `run_id text NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE` (match `workflow_runs.id`'s type), `project_id text NULL`, `title text NOT NULL CHECK (length(title) BETWEEN 1 AND 500)`, `schema jsonb NOT NULL`, `context jsonb NOT NULL DEFAULT '[]'`, `place text NULL CHECK (1 to 100 characters, no control characters: it is part of a topic)`, `ui text NULL`, `expires_at timestamptz NULL`, `state text NOT NULL DEFAULT 'open' CHECK (state IN ('open','answered','cancelled','expired'))`, `answer jsonb NULL`, `answered_by text NULL`, `created_at`/`updated_at timestamptz NOT NULL DEFAULT now()`; index `tasks_open_place ON tasks (place, created_at) WHERE state = 'open'`; index on `run_id`.

- [ ] **Step 1: Write the failing tests**

```python
EMAIL = {"type": "object", "required": ["email"],
         "properties": {"email": {"type": "string", "format": "email", "title": "Email"},
                        "also_sms": {"type": "boolean", "default": False}}}

def test_a_valid_answer_has_no_problems():
    assert validate_answer(EMAIL, {"email": "jane@example.com"}) == []

def test_a_bad_email_names_its_field():
    [p] = validate_answer(EMAIL, {"email": "not-an-address"})
    assert p.field == "email" and "email" in p.message

def test_a_missing_required_field_is_a_root_problem_naming_it():
    [p] = validate_answer(EMAIL, {})
    assert p.field == "" and "email" in p.message

def test_a_nested_or_referencing_schema_is_refused_for_a_prompt():
    assert prompt_schema_problems({"type": "object", "properties": {"x": {"type": "string", "$ref": "#/a"}}})
```

`test_store.py` (marked `requires_postgres`, takes `pg_conninfo`): `test_open_twice_keeps_one_row`, `test_a_different_answer_to_an_answered_task_is_refused` (`TaskNotOpenError` with `state == "answered"`), `test_an_answer_that_landed_before_the_runs_end_is_kept` (a task `cancelled` by its run's end becomes `answered`), `test_the_runs_own_mark_is_final`, `test_close_of_an_answered_task_returns_none`, `test_cancel_for_run_cancels_only_open_tasks_of_that_run` (returns 2 for a run with 2 open + 1 answered, other run untouched), `test_list_filters_by_place`.

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/tasks -v`
Expected: FAIL, `ModuleNotFoundError: scadbuddy.tasks`

- [ ] **Step 3: Implement the migration, models, `validate_answer`, `prompt_schema_problems` and `TaskStore`**

`validate_answer` never raises and stays bounded: it refuses what jsonb cannot hold (NUL, lone surrogates, non-finite numbers) and a too deep answer, then a stored schema outside the subset (`schema_problems(stored=True, formats=True, defaults=False)`, so no `$ref` is ever fetched), then validates with `core/input_schema.Validator`, its formats and its pattern deadline and budget (#2661), extra keys refused. `TaskOpen` refuses a schema `prompt_schema_problems` refuses; reading a stored `Task` does not check it again.

- [ ] **Step 4: Run them to see them pass**

Run: `cd backend && uv run --frozen pytest tests/tasks -v`
Expected: PASS

- [ ] **Step 5: Commit** — `feat(tasks): the tasks table, its store and the answer check (#7364)`

### Task 2: The `tasks` realtime topics

**Files:**
- Modify: `backend/scadbuddy/api/realtime.py` (`COLLECTION_TOPICS`, the `_TOPIC` regex, `topics_of`)
- Test: the realtime topics test module that covers `topics_of` today (grep `topics_of` under `backend/tests`)

**Interfaces:**
- Consumes: `TaskChanged` (Task 1). Produces topics: `"tasks"`, `f"task:{task_id}"`, and `f"tasks:{place}"` when `place` is set.

- [ ] **Step 1: Failing test** — `test_a_task_change_reaches_its_topics`: `topics_of(TaskChanged(task_id=T, run_id="r1", state="open", place="packing bench")) == ["tasks", f"task:{T}", "tasks:packing bench"]` with `T` a uuid; `valid_topic` accepts both forms and refuses a trailing newline; and every member of the `Event` union is handled by `topics_of`.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest <that module> -k task -v` → FAIL
- [ ] **Step 3: Implement** the event and the three topic forms.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** — `feat(tasks): task.changed event and realtime topics (#7364)`

### Task 3: The CRUD area `tasks` for the projects worker

**Files:**
- Create: `backend/scadbuddy/api/crud_tasks.py` (`router = crud_router("projects")`)
- Modify: `backend/scadbuddy/api/crud_areas.py` (`CRUD_AREAS["projects"]` gains `"tasks"`), `backend/scadbuddy/worker.py` (`PROJECTS_CRUD_AREAS` gains `"tasks"`) (`CRUD_CLASSES` needs no change: it classes by caller), the API's router registration
- Create: `backend/scadbuddy/crud/tasks.py` — `RemoteTasks(crud: CrudClient)` with `open(task: TaskOpen) -> Task`, `close(task_id: str, state: Literal["expired","cancelled"]) -> Task | None`, `mark_answered(task_id: str, answer: dict[str, Any], by: str | None, workflow_run_id: str, *, final: bool = False) -> Task`
- Test: `backend/tests/crud/test_crud_tasks.py`

**Interfaces:**
- Produces routes: `POST /api/v1/internal/crud/projects/tasks` (body `TaskOpen`, natural: the same id again answers the stored row; a run that ended is 409 `run-ended`, read `FOR SHARE` in the insert's transaction; an unknown one 404; a schema `TaskOpen` refuses is a 422 that names the problem, so a worker that skipped the check cannot store it), `POST /api/v1/internal/crud/projects/tasks/{task_id}/close` (body `{"state": "expired"|"cancelled"}`; answers the row, or 409 `task-not-open` with the current `state`); and `POST /api/v1/internal/crud/projects/tasks/{task_id}/answered` (body `{answer, by, workflow_run_id, final}`, `final` false when left out, `TaskStore.mark_answered`'s rules; with `final` a different answer, an `expired` row or a superseded execution is 200 with the row, an unknown task 404 `task-not-found`, and a lagging row 503 so the client retries, 409 `task-not-open`): the `task_answer` operation runs on the projects worker, which has no SQL.
- Consumes: `TaskStore` (Task 1).

- [ ] **Step 1: Failing tests** — `test_open_without_a_credential_is_401`; `test_open_with_another_callers_credential_is_403`; `test_open_twice_answers_the_same_row`; `test_close_of_an_answered_task_is_409_with_its_state`; `test_the_same_answer_twice_answers_the_same_row`; `test_a_final_mark_replaces_a_different_answer`; `test_a_final_mark_of_an_expired_task_answers_the_row`; `test_a_final_mark_of_an_unknown_task_is_404`; `test_the_projects_areas_include_tasks` (`"tasks" in CRUD_AREAS["projects"]` and `"tasks" in PROJECTS_CRUD_AREAS`).
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/crud/test_crud_tasks.py -v` → FAIL
- [ ] **Step 3: Implement** the router, the area entries and `RemoteTasks`.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** — `feat(tasks): CRUD area for the projects worker (#7364)`

### Task 4: Public task routes and the `task_answer` operation

**Files:**
- Create: `backend/scadbuddy/api/tasks.py`
- Modify: `backend/scadbuddy/flows/operations.py` (new kind `TASK_ANSWER = "task_answer"` in `flow_kinds()`, beside `run_answer`)
- Test: `backend/tests/api/test_tasks.py`

**Interfaces:**
- Produces routes:
  - `GET /api/v1/tasks?state=open&place=…&limit=` → `{"tasks": [Task]}`
  - `GET /api/v1/tasks/{task_id}` → `Task` | 404
  - `POST /api/v1/tasks/{task_id}/answer`, `Idempotency-Key` required, body `{"answer": {…}}` (an object; body ≤ 16 KB), browser-only → `Task` (state `answered`). Refusals: 404 unknown; 409 `https://scadbuddy.dev/problems/tasks/not-open` with `state`; 422 `https://scadbuddy.dev/problems/tasks/invalid-answer` with `problems: [AnswerProblem]` and the task left open.
- Produces operation `task_answer` (queue `projects`): input `{run_id, task_id, answer_sealed, by}` (`seal_answer` over the answer's JSON text); it records a `workflow_run_decisions` row as `run_answer` does, calls `AgentClient(...).provide_callback_result(task_id, result={"answer": answer}, update_id=<the decision's request_id>)`, then one best-effort `RemoteTasks.mark_answered` (Task 3; the durable mark is `prompt()`'s own, Task 5). A retry that finds its decision recorded asks `landed` with the pinned execution and never sends into another one: an answer undone by a Reset stays undone. The decision row, one per call, picks the winner of two answers. `CallbackAlreadyResolved` → nothing is marked (this request's answer lost) and the route answers 409 `not-open` with the row's own `state` (`open` until the run's own mark lands); `MalformedCallbackResult` → 422.
- Consumes: `validate_answer`, `TaskStore`.

- [ ] **Step 1: Failing tests** (a task row and a stub `AgentClient`, as `tests/api/test_flows.py` stubs `provide_callback_result`; Task 5 tests the real path):
  - `test_a_valid_answer_resolves_the_callback_and_marks_the_task_answered`
  - `test_an_invalid_email_is_422_naming_the_field_and_the_task_stays_open` (`problems == [{"field": "email", …}]`, state still `open`)
  - `test_two_answers_at_once_one_wins_the_other_is_409` (two concurrent posts with different keys; exactly one 200, one 409; `provide_callback_result` called once)
  - `test_an_already_resolved_callback_marks_nothing_and_answers_409`
  - `test_a_retry_after_a_reset_sends_and_marks_nothing`
  - `test_an_answer_that_is_not_an_object_is_422` and `test_a_17_kb_answer_is_refused`
  - `test_the_answer_route_refuses_a_service_caller` (browser-only)
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/api/test_tasks.py -v` → FAIL
- [ ] **Step 3: Implement** the routes and the operation kind; the concurrency rule is `record_decision` (one per call); the task is marked only after the callback landed.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** — `feat(tasks): public task routes and the task_answer operation (#7364)`

### Task 5: `prompt()` in flows, and `wait_for_human` with no default limit

**Files:**
- Modify: `backend/scadbuddy/workflows/flow_entries.py` (`run_callback`'s `timeout_s: float | None`)
- Modify: `backend/scadbuddy/workflows/flow_tools.py` (`task_answer` and `human_answer_open` callbacks, `prompt`, `wait_for_human`, `FLOW_TOOLS`, `_INNER`)
- Modify: `backend/scadbuddy/workflows/flow_activities.py` (activities `flow_task_open`, `flow_task_answered`, `flow_task_close` over `RemoteTasks`), `backend/scadbuddy/worker.py` (register them on the projects worker and the in-process one)
- Modify: `backend/scadbuddy/flows/typecheck.py` and `backend/tests/flows/test_typecheck.py` (a literal `timeout_s` of `wait_for_human` or `prompt` is refused only under `MIN_WAIT_S`: registration and every start refuse one over `MAX_WAIT_S` today)
- Modify: `backend/scadbuddy/flows/models.py` `HOST_FN` (`"task_answer": "prompt"`, `"human_answer_open": "wait_for_human"`)
- Modify: `backend/scadbuddy/flows/store.py` (in the same transaction: a terminal status and `mark_terminated` call `cancel_for_run_in`; `_reset_run` calls `match_waiting_in` with the new waiting and done ids)
- Modify: `backend/scadbuddy/api/flows.py` (`PendingEntry` gains `task: Task | None`, filled by `TaskStore.get(call_id)` for `kind == "answer"`)
- Create fixtures: `backend/tests/fixtures/project_workflow_histories/prompted.json`, `waited_open.json` (recorded with `SCADBUDDY_RECORD_HISTORIES=1`), added to `NAMES` in `tests/flows/test_project_replay.py`
- Test: `backend/tests/flows/test_prompt.py`, `tests/flows/test_harness_facts.py` (one new fact)

**Interfaces:**
- Produces:
  - `OPEN_WAIT = timedelta(days=3650)`: the declared timeout of both new callbacks (the harness requires one; it is never the limit a person meets).
  - `@agent.callback_tool_defn(inherently_safe=True, timeout=OPEN_WAIT) async def task_answer(title: str, schema: dict[str, Any]) -> TaskAnswer` with `class TaskAnswer(BaseModel): answer: dict[str, Any]`
  - `@agent.callback_tool_defn(inherently_safe=True, timeout=OPEN_WAIT) async def human_answer_open(question: str) -> HumanAnswer`
  - `@agent.tool_defn(inherently_safe=True) async def prompt(title: str, schema: dict[str, Any], context: list[dict[str, Any]] | None = None, place: str | None = None, ui: str | None = None, timeout_s: int | None = None) -> dict[str, Any]`
  - `run_callback(run_tool, tool, timeout_s: float | None, *, call_id=None, **kwargs)`: `None` waits with no timer.
  - `WAIT_OPEN_PATCH = "flow-open-wait-7364"`.
- `prompt` does, in order: `prompt_schema_problems(schema)` → `ValueError` naming them; `timeout_s` below `MIN_WAIT_S` → `ValueError`; `call_id = str(workflow.uuid4())`; activity `flow_task_open(TaskOpen(id=call_id, run_id=owner.run_id, title=…, schema=…, context=…, place=place, ui=…, expires_at=now+timeout_s or None))`; `async with step("prompt", call_id, outward=False, waiting="answer", prompt=title)`: `run_callback(owner.runner.run_tool, task_answer, timeout_s, call_id=call_id, title=title, schema=schema)`; on `TimeoutError` → activity `flow_task_close(call_id, "expired")` and re-raise; the activity `flow_task_answered(call_id, answer, workflow_run_id)` marks the task with the harness result's `answer` unchanged (the JSON the person sent, never the resolved dict), under the workflow's own retry (no attempt limit), so a task is never left open by an operation that could not mark it. That mark is the run's and it is final (`mark_answered(..., final=True)`): on the same execution it sets the answer the run received over any answered or cancelled row, so it never raises for a different answer; an `expired` task is left alone (200 with the row), and the activity takes the 404 of an unknown one (its run was forgotten) as done, so neither is retried. The operation's best-effort mark keeps `final=False`. Then the answer is resolved without running any pattern (`filled(schema, answer)`, the default-filling and model half of `_resolve_inputs`, taken out of it and shared: `resolve_inputs` itself runs patterns on a wall-clock deadline, which a workflow must never do, and the server checked them already) and returned as a dict, an omitted optional field as None. An answer the server accepted and the model refuses raises `ValueError` in the script, which it may catch; uncaught, it fails the run. The task stays `answered`: that is what the person sent.
- `wait_for_human(question: str, timeout_s: int | None = None) -> HumanAnswer`: when `workflow.patched(f"{WAIT_OPEN_PATCH}:{call_id}")` (per call, as `FLOW_PINNED_CALL_PATCH`, so an older run's next question is open too): no `MAX_WAIT_S`, `None` waits with no timer, callback `human_answer_open`; otherwise the old body unchanged (`None` means `DEFAULT_WAIT_S`, callback `human_answer`), so `answered.json` replays.

- [ ] **Step 1: Failing tests** (`test_prompt.py`, `requires_temporal`, `requires_postgres`, the flow-run harness the existing `wait_for_human` tests use):
  - `test_a_prompt_without_a_timeout_starts_no_timer` (no timer shorter than `OPEN_WAIT` in the parked run's history)
  - `test_a_prompt_answer_reaches_the_script_as_a_dict` (answer `{"email": "jane@example.com"}` through `POST /tasks/{id}/answer`; the script returns `answer["email"]`)
  - `test_omitted_fields_reach_the_script_and_the_task_ends_answered` (a schema with a defaulted boolean and an optional string, both left out: the script reads the default and None, and the stored answer is what was sent)
  - `test_the_runs_mark_replaces_a_wrong_best_effort_mark`
  - `test_a_prompt_with_timeout_expires_its_task_and_raises` (time-skipping, `timeout_s=60`; the script catches `TimeoutError`; task state `expired`)
  - `test_a_refused_schema_raises_before_any_task_row` (`{"type": "object", "properties": {"x": {"type": "string", "$ref": "#/a"}}}` → `ValueError` with `'$ref' is not supported for string`; no row)
  - `test_a_closed_run_cancels_its_open_task` (a cancelled run and one the sweep ends; task `cancelled`)
  - `test_a_reset_before_the_answer_opens_the_task_again` and `test_a_reset_before_the_prompt_cancels_its_task`
  - `test_wait_for_human_has_no_default_limit` (a patched run parks with no timer shorter than `OPEN_WAIT`; `timeout_s=200_000` is accepted, through `check_script` too)
  - `test_typecheck.py`: a literal `timeout_s=200_000` has no problem; `timeout_s=5` is refused, for `wait_for_human` and `prompt`
  - `test_harness_facts.py::test_a_callback_declared_with_a_ten_year_timeout_parks_and_answers`
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/flows/test_prompt.py tests/flows/test_harness_facts.py -v` → FAIL
- [ ] **Step 3: Implement** as the Interfaces block says; record `prompted.json` and `waited_open.json`.
- [ ] **Step 4: Run** `cd backend && uv run --frozen pytest tests/flows -v` → PASS, `answered.json` included
- [ ] **Step 5: Commit** — `feat(flows): prompt() with typed answers and no default limit (#7364)`

### Task 6: The run page answers a prompt from its schema

**Files:**
- Create: `frontend/src/components/flows/SchemaForm.tsx` (the form `StartRun.tsx` draws today, moved out: fields from `inputFields`, values, per-field errors)
- Modify: `frontend/src/components/flows/StartRun.tsx` (uses `SchemaForm`; no behaviour change), `frontend/src/lib/flowInputs.ts` (`InputField.format?: 'email' | 'date' | 'date-time'`), `frontend/src/components/flows/PendingCard.tsx` (an `answer` entry with `task` draws `SchemaForm`), `frontend/src/api/client.ts` (`answerTask(taskId: string, answer: Record<string, unknown>)` through `command()`, so it sends and re-sends one `Idempotency-Key`)
- Create: `frontend/src/mocks/features/tasks.ts` (`handlers`: the answer route, refusing a bad email with the backend's 422 shape)
- Test: `frontend/src/components/flows/SchemaForm.test.tsx`, `PendingCard.test.tsx`

**Interfaces:**
- Produces: `SchemaForm({ schema, submitLabel, busy, problems, onSubmit }: { schema: unknown; submitLabel: string; busy: boolean; problems?: { field: string; message: string }[]; onSubmit: (values: Record<string, unknown>) => void })`.
- Input type by format: `email` → `type="email" autoComplete="email"`, `date` → `date`, `date-time` → `datetime-local`, sent as ISO 8601 with the offset of the browser's own time zone at that local time (`getTimezoneOffset` of the parsed date; the server refuses a value without an offset). After a 202 that was followed, the client reads `GET /tasks/{id}`. A 409 closes the form whatever `state` it names: the card says the question was answered elsewhere and reads the task again on `task.changed` and on reload, so a row that still says `open` does not show the form again.

- [ ] **Step 1: Failing tests**
  - `SchemaForm`: an `email` field renders `input[type=email]` labelled "Email"; a `problems` entry `{field: "email", message: "not a valid address"}` shows under that field; a root problem shows above the button.
  - `PendingCard`: an entry with a task shows the schema's fields instead of the textarea and posts `{answer: {email, also_sms}}` to `/api/v1/tasks/<id>/answer`; a 422 from the mock leaves the form filled and shows the field's message.
  - `StartRun`'s existing tests pass unchanged.
- [ ] **Step 2: Run** `cd frontend && pnpm gen:api && pnpm exec vitest run src/components/flows` → FAIL
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run** the same → PASS; then `pnpm lint && pnpm typecheck`
- [ ] **Step 5: Commit** — `feat(frontend): answer a flow prompt from its schema (#7364)`

### Task 7: Docs

**Files:**
- Modify: `docs/features/flows.md` (prompts and tasks: the table, the routes, no default limit), `backend/CLAUDE.md` (the `workflows/` and `flows/` bullets: `prompt`, `task_answer`, `WAIT_OPEN_PATCH`, the `tasks` area), and the place flow authors read host functions (grep `wait_for_human` under `docs/` and `agent/src` for the authoring guide)

- [ ] **Step 1:** Write the changes; every claim names its file and symbol.
- [ ] **Step 2:** `rg -n "MAX_WAIT_S|one day|86.?400" docs backend/CLAUDE.md backend/scadbuddy backend/tests/flows agent/src` shows, but for the pre-patch branch that old runs replay, no code or text that still says `wait_for_human` is capped at one day or defaults to an hour.
- [ ] **Step 3: Commit** — `docs(flows): prompts, tasks and waits with no default limit (#7364)`
