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
- A1 accepts only the `core/input_schema.py` subset in a prompt's schema (`schema_problems`); `x-sb-widget` and the other widget hints are A2.
- An answer body is at most 16 KB of JSON (the `AnswerBody` bound in `api/flows.py`).
- The answer route is browser-only, as `answer_flow_run` is (`_browser_only()`).
- Never commit `backend/openapi.json` or a `schema.d.ts`.
- Backend tests need `SCADBUDDY_TEST_DATABASE_URL` and a Temporal dev server (backend/CLAUDE.md "Commands").

## Review Focus

1. A run that ends (finished, failed, cancelled, reset past the prompt) while its task is open: the task must not stay open on screens. → Task 1 `cancel_for_run`, Task 5 sweep test.
2. Two people answer one task at once: one wins, the other gets 409 and the run gets one answer. → Task 4 test.
3. The harness already resolved the call (an earlier answer whose reply was lost, then a retry): the route answers 409 and the task row ends `answered`, not stuck `open`. → Task 4 test.
4. An answer that is not an object, or is over 16 KB: 422 or 413 before any validation or operation. → Task 4 test.
5. A schema the subset refuses (`$ref`, a nested object, an unknown keyword): `prompt()` raises `ValueError` naming it before any task row is written. → Task 5 test.

---

### Task 1: The `tasks` table, its store and the answer check

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_tasks.sql`
- Create: `backend/scadbuddy/tasks/__init__.py`, `tasks/models.py`, `tasks/store.py`, `tasks/validate.py`, `tasks/component.py`
- Modify: `backend/scadbuddy/core/events.py` (`TaskChanged`, added to the `Event` union)
- Test: `backend/tests/tasks/test_validate.py`, `backend/tests/tasks/test_store.py`

**Interfaces:**
- Produces (`tasks/models.py`):
  - `TaskState = Literal["open", "answered", "cancelled", "expired"]`
  - `class TaskOpen(BaseModel)`: `id: str` (the harness call id), `run_id: str`, `title: str` (1–500 chars), `schema_: dict[str, Any]` (alias `schema`), `context: list[dict[str, Any]] = []`, `place: str | None = None`, `ui: str | None = None`, `expires_at: datetime | None = None`
  - `class Task(TaskOpen)`: `project_id: str | None`, `state: TaskState`, `answer: dict[str, Any] | None`, `answered_by: str | None`, `created_at: datetime`, `updated_at: datetime`
  - `class AnswerProblem(BaseModel)`: `field: str` (JSON pointer path joined by `/`, `""` for the root), `message: str`
- Produces (`tasks/validate.py`): `validate_answer(schema: dict[str, Any], answer: Any) -> list[AnswerProblem]`; `prompt_schema_problems(schema: dict[str, Any]) -> list[str]` (delegates to `core/input_schema.schema_problems`).
- Produces (`tasks/store.py`, class `TaskStore` over the shared `scadbuddy-rows` pool, events through `TransactionalEvents.publish_in`):
  - `open(task: TaskOpen) -> Task` (insert; same id again returns the existing row unchanged)
  - `get(task_id: str) -> Task | None`
  - `list(*, state: TaskState | None = "open", place: str | None = None, limit: int = 100) -> list[Task]`
  - `mark_answered(task_id: str, answer: dict[str, Any], by: str) -> Task` (only while `open`; else `TaskNotOpenError(state)`)
  - `close(task_id: str, state: Literal["expired", "cancelled"]) -> Task | None` (only while `open`; returns None when it was not open)
  - `cancel_for_run(run_id: str) -> int` (every open task of the run → `cancelled`; returns the count)
  - Each state change publishes `TaskChanged` in its transaction: `class TaskChanged(BaseEvent)`: `kind: Literal["task.changed"]`, `task_id: str`, `run_id: str`, `state: TaskState`, `place: str | None`.
- Produces (`tasks/component.py`): `TASKS` component, `TasksDep` (the `core/components.py` pattern, `api/components.py` `component_dep`).

Table: `id text PRIMARY KEY`, `run_id text NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE` (match `workflow_runs.id`'s type), `project_id text NULL`, `title text NOT NULL CHECK (length(title) BETWEEN 1 AND 500)`, `schema jsonb NOT NULL`, `context jsonb NOT NULL DEFAULT '[]'`, `place text NULL`, `ui text NULL`, `expires_at timestamptz NULL`, `state text NOT NULL DEFAULT 'open' CHECK (state IN ('open','answered','cancelled','expired'))`, `answer jsonb NULL`, `answered_by text NULL`, `created_at`/`updated_at timestamptz NOT NULL DEFAULT now()`; index `tasks_open_place ON tasks (place, created_at) WHERE state = 'open'`; index on `run_id`.

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
    assert prompt_schema_problems({"type": "object", "properties": {"x": {"$ref": "#/a"}}})
```

`test_store.py` (marked `requires_postgres`, takes `pg_conninfo`): `test_open_twice_keeps_one_row`, `test_mark_answered_only_while_open` (second call raises `TaskNotOpenError` with `state == "answered"`), `test_close_of_an_answered_task_returns_none`, `test_cancel_for_run_cancels_only_open_tasks_of_that_run` (returns 2 for a run with 2 open + 1 answered, other run untouched), `test_list_filters_by_place`.

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/tasks -v`
Expected: FAIL, `ModuleNotFoundError: scadbuddy.tasks`

- [ ] **Step 3: Implement the migration, models, `validate_answer` (`Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)`, one problem per error, `field` from `error.absolute_path`), `prompt_schema_problems` and `TaskStore`**

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

- [ ] **Step 1: Failing test** — `test_a_task_change_reaches_its_topics`: `topics_of(TaskChanged(task_id="t1", run_id="r1", state="open", place="packing bench")) == ["tasks", "task:t1", "tasks:packing bench"]`; and the `_TOPIC` regex accepts `task:t1` and `tasks:packing bench`.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest <that module> -k task -v` → FAIL
- [ ] **Step 3: Implement** the event and the three topic forms.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** — `feat(tasks): task.changed event and realtime topics (#7364)`

### Task 3: The CRUD area `tasks` for the projects worker

**Files:**
- Create: `backend/scadbuddy/api/crud_tasks.py` (`router = crud_router("projects")`)
- Modify: `backend/scadbuddy/api/crud_areas.py` (`CRUD_AREAS["projects"]` gains `"tasks"`), `backend/scadbuddy/worker.py` (`PROJECTS_CRUD_AREAS` gains `"tasks"`), `backend/scadbuddy/auth/routes.py` (`CRUD_CLASSES`, as the projects caller's other routes are listed), the API's router registration
- Create: `backend/scadbuddy/crud/tasks.py` — `RemoteTasks(crud: CrudClient)` with `open(task: TaskOpen) -> Task`, `close(task_id: str, state: Literal["expired","cancelled"]) -> Task | None`
- Test: `backend/tests/crud/test_crud_tasks.py`

**Interfaces:**
- Produces routes: `POST /api/v1/internal/crud/projects/tasks` (body `TaskOpen`, natural: the same id again answers the stored row), `POST /api/v1/internal/crud/projects/tasks/{task_id}/close` (body `{"state": "expired"|"cancelled"}`; answers the row, or 409 `task-not-open` with the current `state`).
- Consumes: `TaskStore` (Task 1).

- [ ] **Step 1: Failing tests** — `test_open_without_a_credential_is_401`; `test_open_with_another_callers_credential_is_403`; `test_open_twice_answers_the_same_row`; `test_close_of_an_answered_task_is_409_with_its_state`; `test_the_projects_areas_include_tasks` (`"tasks" in CRUD_AREAS["projects"]` and `"tasks" in PROJECTS_CRUD_AREAS`).
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
- Produces operation `task_answer` (queue `projects`): input `{run_id, task_id, answer (sealed with seal_answer), by}`; it records a `workflow_run_decisions` row as `run_answer` does, calls `AgentClient(...).provide_callback_result(task_id, result={"answer": answer}, update_id=<the decision's request_id>)`, then `TaskStore.mark_answered`. `CallbackAlreadyResolved` → the task is marked `answered` with the answer it carried and the route answers 409 `not-open` with `state: "answered"`; `MalformedCallbackResult` → 422.
- Consumes: `validate_answer`, `TaskStore`.

- [ ] **Step 1: Failing tests** (use a run parked on a `prompt`, built with the Task 5 helper once it exists; until then, a task row and a stub `AgentClient` as `tests/api/test_flows.py` stubs `provide_callback_result`):
  - `test_a_valid_answer_resolves_the_callback_and_marks_the_task_answered`
  - `test_an_invalid_email_is_422_naming_the_field_and_the_task_stays_open` (`problems == [{"field": "email", …}]`, state still `open`)
  - `test_two_answers_at_once_one_wins_the_other_is_409` (two concurrent posts with different keys; exactly one 200, one 409; `provide_callback_result` called once)
  - `test_an_already_resolved_callback_ends_the_task_answered_and_answers_409`
  - `test_an_answer_that_is_not_an_object_is_422` and `test_a_17_kb_answer_is_refused`
  - `test_the_answer_route_refuses_a_service_caller` (browser-only)
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/api/test_tasks.py -v` → FAIL
- [ ] **Step 3: Implement** the routes and the operation kind; the concurrency rule is `mark_answered`'s `open` check under the row lock, taken before the callback is resolved.
- [ ] **Step 4: Run** → PASS
- [ ] **Step 5: Commit** — `feat(tasks): public task routes and the task_answer operation (#7364)`

### Task 5: `prompt()` in flows, and `wait_for_human` with no default limit

**Files:**
- Modify: `backend/scadbuddy/workflows/flow_entries.py` (`run_callback`'s `timeout_s: float | None`)
- Modify: `backend/scadbuddy/workflows/flow_tools.py` (`task_answer` and `human_answer_open` callbacks, `prompt`, `wait_for_human`, `FLOW_TOOLS`, `_INNER`)
- Modify: `backend/scadbuddy/workflows/flow_activities.py` (activities `flow_task_open`, `flow_task_close` over `RemoteTasks`), `backend/scadbuddy/worker.py` (register them on the projects worker and the in-process one)
- Modify: `backend/scadbuddy/flows/models.py` `HOST_FN` (`"task_answer": "prompt"`, `"human_answer_open": "wait_for_human"`)
- Modify: `backend/scadbuddy/flows/sweep.py` (closed runs: `TaskStore.cancel_for_run`)
- Modify: `backend/scadbuddy/api/flows.py` (`PendingEntry` gains `task: Task | None`, filled by `TaskStore.get(call_id)` for `kind == "answer"`)
- Create fixtures: `backend/tests/fixtures/project_workflow_histories/prompted.json`, `waited_open.json` (recorded with `SCADBUDDY_RECORD_HISTORIES=1`), added to `NAMES` in `tests/flows/test_project_replay.py`
- Test: `backend/tests/flows/test_prompt.py`, `tests/flows/test_harness_facts.py` (one new fact)

**Interfaces:**
- Produces:
  - `OPEN_WAIT = timedelta(days=3650)`: the declared timeout of both new callbacks (the harness requires one; it is never the limit a person meets).
  - `@agent.callback_tool_defn(inherently_safe=True, timeout=OPEN_WAIT) async def task_answer(title: str, schema: dict[str, Any]) -> TaskAnswer` with `class TaskAnswer(BaseModel): answer: dict[str, Any]`
  - `@agent.callback_tool_defn(inherently_safe=True, timeout=OPEN_WAIT) async def human_answer_open(question: str) -> HumanAnswer`
  - `@agent.tool_defn(inherently_safe=True) async def prompt(title: str, schema: dict[str, Any], context: list[dict[str, Any]] | None = None, where: str | None = None, ui: str | None = None, timeout_s: int | None = None) -> dict[str, Any]`
  - `run_callback(run_tool, tool, timeout_s: float | None, *, call_id=None, **kwargs)`: `None` waits with no timer.
  - `WAIT_OPEN_PATCH = "flow-open-wait-7364"`.
- `prompt` does, in order: `prompt_schema_problems(schema)` → `ValueError` naming them; `timeout_s` below `MIN_WAIT_S` → `ValueError`; `call_id = str(workflow.uuid4())`; activity `flow_task_open(TaskOpen(id=call_id, run_id=owner.run_id, title=…, schema=…, context=…, place=where, ui=…, expires_at=now+timeout_s or None))`; `async with step("prompt", call_id, outward=False, waiting="answer", prompt=title)`: `run_callback(owner.runner.run_tool, task_answer, timeout_s, call_id=call_id, title=title, schema=schema)`; on `TimeoutError` → activity `flow_task_close(call_id, "expired")` and re-raise; the result's `answer` is validated with `inputs_model(schema)` and returned as a dict.
- `wait_for_human(question: str, timeout_s: int | None = None) -> HumanAnswer`: when `workflow.patched(WAIT_OPEN_PATCH)`: no `MAX_WAIT_S`, `None` waits with no timer, callback `human_answer_open`; otherwise the old body unchanged (`None` means `DEFAULT_WAIT_S`, callback `human_answer`), so `answered.json` replays.

- [ ] **Step 1: Failing tests** (`test_prompt.py`, `requires_temporal`, `requires_postgres`, the flow-run harness the existing `wait_for_human` tests use):
  - `test_a_prompt_without_a_timeout_starts_no_timer` (history of the parked run has no `TimerStarted` after the task's activity)
  - `test_a_prompt_answer_reaches_the_script_as_a_dict` (answer `{"email": "jane@example.com"}` through `POST /tasks/{id}/answer`; the script returns `answer["email"]`)
  - `test_a_prompt_with_timeout_expires_its_task_and_raises` (time-skipping, `timeout_s=60`; the script catches `TimeoutError`; task state `expired`)
  - `test_a_refused_schema_raises_before_any_task_row` (`{"properties": {"x": {"$ref": "#/a"}}}` → `ValueError` naming `$ref`; no row)
  - `test_a_closed_run_cancels_its_open_task` (cancel the run, run the sweep; task `cancelled`)
  - `test_wait_for_human_has_no_default_limit` (patched run parks with no timer; `timeout_s=200_000` is accepted)
  - `test_harness_facts.py::test_a_callback_declared_with_a_ten_year_timeout_parks_and_answers`
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/flows/test_prompt.py tests/flows/test_harness_facts.py -v` → FAIL
- [ ] **Step 3: Implement** as the Interfaces block says; record `prompted.json` and `waited_open.json`.
- [ ] **Step 4: Run** `cd backend && uv run --frozen pytest tests/flows -v` → PASS, `answered.json` included
- [ ] **Step 5: Commit** — `feat(flows): prompt() with typed answers and no default limit (#7364)`

### Task 6: The run page answers a prompt from its schema

**Files:**
- Create: `frontend/src/components/flows/SchemaForm.tsx` (the form `StartRun.tsx` draws today, moved out: fields from `inputFields`, values, per-field errors)
- Modify: `frontend/src/components/flows/StartRun.tsx` (uses `SchemaForm`; no behaviour change), `frontend/src/lib/flowInputs.ts` (`InputField.format?: 'email' | 'uri' | 'date' | 'date-time'`), `frontend/src/components/flows/PendingCard.tsx` (an `answer` entry with `task` draws `SchemaForm`), `frontend/src/api/client.ts` (`answerTask(taskId: string, answer: Record<string, unknown>)` through `command()`, so it sends and re-sends one `Idempotency-Key`)
- Create: `frontend/src/mocks/features/tasks.ts` (`handlers`: the answer route, refusing a bad email with the backend's 422 shape)
- Test: `frontend/src/components/flows/SchemaForm.test.tsx`, `PendingCard.test.tsx`

**Interfaces:**
- Produces: `SchemaForm({ schema, submitLabel, busy, problems, onSubmit }: { schema: unknown; submitLabel: string; busy: boolean; problems?: { field: string; message: string }[]; onSubmit: (values: Record<string, unknown>) => void })`.
- Input type by format: `email` → `type="email" autoComplete="email"`, `uri` → `url`, `date` → `date`, `date-time` → `datetime-local`.

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
- [ ] **Step 2:** `rg -n "MAX_WAIT_S|one day|86.?400" docs backend/CLAUDE.md agent/src` shows no text that still says `wait_for_human` is capped at one day or defaults to an hour.
- [ ] **Step 3: Commit** — `docs(flows): prompts, tasks and waits with no default limit (#7364)`
