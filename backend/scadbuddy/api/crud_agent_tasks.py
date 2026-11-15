"""The agent's background tasks and its command record behind CRUD routes (#5299 step 8):
under ``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`).
Each route is one transaction (D2); the SQL is in ``agent_data/background.py`` and
``agent_data/operations.py``, and the agent's stores (``agent/src/background/store.ts``
`BackgroundTaskStore`, ``agent/src/operations/store.ts`` `OperationStore`) call these
routes once ``…/areas`` lists the area, and their own SQL until then (D9).

Area ``background-tasks``, a session's tasks (``ai_background_tasks``), a Monitor's events
(``ai_background_events``) and a Workflow's journal and child chats
(``ai_workflow_calls``, ``ai_workflow_children``):

- ``POST …/background-tasks``: a new active task, or ``task: null`` when the session has
  ``MAX_ACTIVE`` (counted under the session's lock);
- ``GET …/background-tasks?session_id=…`` (``all`` for every state), ``GET
  …/background-tasks/recurring-crons`` and ``GET …/background-tasks/{id}``: the reads;
- ``POST …/background-tasks/{id}/scheduled``, ``…/fired``, ``…/finish``, ``…/spec`` and
  ``…/progress``: the task's writes;
- ``POST …/background-tasks/{id}/events`` (``notice`` for the end, past the cap), ``GET
  …/events/pending``, ``GET …/events/count`` and ``POST …/events/delivered``: a Monitor's
  events;
- ``GET`` and ``POST …/background-tasks/{id}/calls``, ``POST …/children`` and ``GET
  …/children/open``: a Workflow's journal.

Area ``operations``, ``ai_operations``:

- ``GET …/operations/by-key/{operation_key}`` and ``GET …/operations/{id}``: the reads
  (``operation: null`` for none);
- ``POST …/operations``: a record of an accepted execution, or the execution's row;
- ``POST …/operations/{id}/finish``: its end.

An inserted or ended operation's ``activity`` row is written in the same transaction (the
API's own table, ``activity/index.py`` ``ingest_agent_operation_in``). No route takes an
``Idempotency-Key``. A task's state writes, ``delivered``, the journal, a child, and an
operation's insert and end are natural writes, safe to send again. ``POST …/background-tasks``
makes a new task each time, and ``POST …/events`` without ``notice`` adds a row each time,
as the agent's SQL did: the agent sends neither again after a lost answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal, TypeVar

from fastapi import Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scadbuddy.activity.component import ACTIVITY
from scadbuddy.activity.index import ActivityIndex
from scadbuddy.agent_data import background, operations
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.flows.agent_reads import Pool

router = crud_router("agent")

T = TypeVar("T")

#: A task's id (the table's CHECK).
TASK_ID = r"^[0-9a-f]{8}$"
#: A session's or a child chat's id: a UUID.
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
#: A Workflow call's key (the table's CHECK).
CALL_KEY = r"^[0-9a-f]{64}$"
#: An operation's key: the agent's sha256 (`operationKey`).
OPERATION_KEY = r"^[0-9a-f]{64}$"
#: The longest text a task's detail or progress carries (an end notice, an error).
DETAIL_MAX = 64 * 1024
#: The longest operation id, kind, subject or workflow id.
NAME_MAX = 512

TaskIdPath = Annotated[str, Path(pattern=TASK_ID)]
OperationIdPath = Annotated[str, Path(min_length=1, max_length=NAME_MAX)]
TaskKind = Literal["cron", "wakeup", "monitor", "workflow"]
TaskState = Literal["active", "done", "cancelled", "failed"]


def _pool(state: Any) -> Pool:
    pool: Pool = state.settings_store.pool
    return pool


def _activity(state: Any) -> ActivityIndex | None:
    try:
        index: ActivityIndex = state.components.get(ACTIVITY).index
    except KeyError:
        return None
    return index


async def _in_conn[T](state: Any, fn: Callable[[Any], T]) -> T:
    def run() -> T:
        with _pool(state).connection() as conn:
            return fn(conn)

    return await asyncio.to_thread(run)


# --- background tasks -------------------------------------------------------------------


class Owner(BaseModel):
    """Who owned the turn that made a task (the agent's ``Owner``)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["browser", "bearer", "oidc", "anonymous", "flow"]
    id: str = Field(max_length=NAME_MAX)
    label: str = Field(max_length=NAME_MAX)


class Task(BaseModel):
    """A row of ``ai_background_tasks`` (the agent's ``BackgroundTask``)."""

    id: str
    session_id: str
    kind: TaskKind
    description: str
    spec: dict[str, Any]
    created_by: dict[str, Any]
    state: TaskState
    next_at: datetime | None
    fired: int
    last_fired_at: datetime | None
    expires_at: datetime | None
    detail: str | None
    created_at: datetime
    updated_at: datetime


class OneTask(BaseModel):
    task: Task | None


class TaskList(BaseModel):
    tasks: list[Task]


class NewTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(pattern=UUID_PATTERN)
    kind: TaskKind
    description: str = Field(min_length=1, max_length=500)
    spec: dict[str, Any]
    created_by: Owner
    next_at: datetime | None = None
    expires_at: datetime | None = None


class At(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: datetime


class Finish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["done", "cancelled", "failed"]
    detail: str | None = Field(default=None, max_length=DETAIL_MAX)


class Finished(BaseModel):
    #: False when the task had ended already.
    finished: bool


class Spec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: dict[str, Any]


class Progress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: str = Field(max_length=DETAIL_MAX)


class Done(BaseModel):
    ok: Literal[True] = True


def _task(row: dict[str, Any] | None) -> Task | None:
    return None if row is None else Task.model_validate(row)


@router.post("/background-tasks", name="crud_agent_background_task_create")
async def create_task(body: NewTask, state: StateDep) -> OneTask:
    row = await _in_conn(
        state,
        lambda conn: background.create(
            conn,
            session_id=body.session_id,
            kind=body.kind,
            description=body.description,
            spec=body.spec,
            created_by=body.created_by.model_dump(),
            next_at=body.next_at,
            expires_at=body.expires_at,
        ),
    )
    return OneTask(task=_task(row))


@router.get("/background-tasks", name="crud_agent_background_tasks")
async def list_tasks(
    state: StateDep,
    session_id: Annotated[str, Query(pattern=UUID_PATTERN)],
    all_states: Annotated[bool, Query(alias="all")] = False,
) -> TaskList:
    rows = await _in_conn(
        state, lambda conn: background.list_tasks(conn, session_id, all_states=all_states)
    )
    return TaskList(tasks=[Task.model_validate(r) for r in rows])


@router.get("/background-tasks/recurring-crons", name="crud_agent_background_tasks_recurring_crons")
async def recurring_crons(state: StateDep) -> TaskList:
    rows = await _in_conn(state, background.active_recurring_crons)
    return TaskList(tasks=[Task.model_validate(r) for r in rows])


@router.get("/background-tasks/{task_id}", name="crud_agent_background_task")
async def get_task(task_id: TaskIdPath, state: StateDep) -> OneTask:
    return OneTask(task=_task(await _in_conn(state, lambda conn: background.get(conn, task_id))))


@router.post("/background-tasks/{task_id}/scheduled", name="crud_agent_background_task_scheduled")
async def task_scheduled(task_id: TaskIdPath, body: At, state: StateDep) -> Done:
    await _in_conn(state, lambda conn: background.scheduled(conn, task_id, body.at))
    return Done()


@router.post("/background-tasks/{task_id}/fired", name="crud_agent_background_task_fired")
async def task_fired(task_id: TaskIdPath, body: At, state: StateDep) -> Done:
    await _in_conn(state, lambda conn: background.fired(conn, task_id, body.at))
    return Done()


@router.post("/background-tasks/{task_id}/finish", name="crud_agent_background_task_finish")
async def task_finish(task_id: TaskIdPath, body: Finish, state: StateDep) -> Finished:
    finished = await _in_conn(
        state, lambda conn: background.finish(conn, task_id, body.state, body.detail)
    )
    return Finished(finished=finished)


@router.post("/background-tasks/{task_id}/spec", name="crud_agent_background_task_spec")
async def task_spec(task_id: TaskIdPath, body: Spec, state: StateDep) -> Done:
    await _in_conn(state, lambda conn: background.set_spec(conn, task_id, body.spec))
    return Done()


@router.post("/background-tasks/{task_id}/progress", name="crud_agent_background_task_progress")
async def task_progress(task_id: TaskIdPath, body: Progress, state: StateDep) -> Done:
    await _in_conn(state, lambda conn: background.progress(conn, task_id, body.detail))
    return Done()


class NewEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=background.NOTICE_MAX_CHARS)
    #: A monitor's last notice (its end): recorded past ``MAX_EVENTS``.
    notice: bool = False


class Appended(BaseModel):
    #: False once the monitor has ``MAX_EVENTS`` (it is then stopped); a notice is always True.
    appended: bool


class PendingEvent(BaseModel):
    seq: str
    text: str


class PendingEvents(BaseModel):
    events: list[PendingEvent]


class Count(BaseModel):
    count: int


class Delivered(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The last ``seq`` sent to the session (the agent keeps it as text: a bigint).
    up_to: str = Field(pattern=r"^[0-9]{1,19}$")


@router.post("/background-tasks/{task_id}/events", name="crud_agent_background_task_event")
async def task_event(task_id: TaskIdPath, body: NewEvent, state: StateDep) -> Appended:
    text = body.text

    def write(conn: Any) -> bool:
        if body.notice:
            background.append_notice(conn, task_id, text)
            return True
        return background.append_event(conn, task_id, text)

    return Appended(appended=await _in_conn(state, write))


@router.get("/background-tasks/{task_id}/events/pending", name="crud_agent_background_task_pending")
async def task_pending_events(
    task_id: TaskIdPath,
    state: StateDep,
    max_chars: Annotated[int, Query(ge=1, le=10_000_000)] = background.DELIVER_MAX_CHARS,
) -> PendingEvents:
    rows = await _in_conn(state, lambda conn: background.pending_events(conn, task_id, max_chars))
    return PendingEvents(events=[PendingEvent.model_validate(r) for r in rows])


@router.get("/background-tasks/{task_id}/events/count", name="crud_agent_background_task_count")
async def task_event_count(task_id: TaskIdPath, state: StateDep, pending: bool = False) -> Count:
    count = await _in_conn(
        state, lambda conn: background.event_count(conn, task_id, pending=pending)
    )
    return Count(count=count)


@router.post(
    "/background-tasks/{task_id}/events/delivered", name="crud_agent_background_task_delivered"
)
async def task_delivered(task_id: TaskIdPath, body: Delivered, state: StateDep) -> Done:
    await _in_conn(state, lambda conn: background.delivered(conn, task_id, int(body.up_to)))
    return Done()


class Call(BaseModel):
    """A journalled ``agent()`` result (``result`` as the agent wrote it)."""

    result: Any
    ok: bool


class OneCall(BaseModel):
    call: Call | None


class NewCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=CALL_KEY)
    occurrence: int = Field(ge=0, le=2**31 - 1)
    result: Any = None
    ok: bool
    child_id: str | None = Field(default=None, pattern=UUID_PATTERN)


class NewChild(BaseModel):
    model_config = ConfigDict(extra="forbid")

    child_id: str = Field(pattern=UUID_PATTERN)


class Children(BaseModel):
    children: list[str]


@router.get("/background-tasks/{task_id}/calls", name="crud_agent_background_task_call")
async def task_call(
    task_id: TaskIdPath,
    state: StateDep,
    key: Annotated[str, Query(pattern=CALL_KEY)],
    occurrence: Annotated[int, Query(ge=0, le=2**31 - 1)],
) -> OneCall:
    row = await _in_conn(state, lambda conn: background.journalled(conn, task_id, key, occurrence))
    # Kept as {value}, so a null result is a row too. The agent's SQL kept an undefined result
    # as `{}`, which reads as null.
    return OneCall(
        call=None if row is None else Call(result=row["result"].get("value"), ok=row["ok"])
    )


@router.post("/background-tasks/{task_id}/calls", name="crud_agent_background_task_journal")
async def task_journal(task_id: TaskIdPath, body: NewCall, state: StateDep) -> Done:
    await _in_conn(
        state,
        lambda conn: background.journal(
            conn,
            task_id,
            body.key,
            body.occurrence,
            result=body.result,
            ok=body.ok,
            child_id=body.child_id,
        ),
    )
    return Done()


@router.post("/background-tasks/{task_id}/children", name="crud_agent_background_task_child")
async def task_child(task_id: TaskIdPath, body: NewChild, state: StateDep) -> Done:
    await _in_conn(state, lambda conn: background.record_child(conn, task_id, body.child_id))
    return Done()


@router.get(
    "/background-tasks/{task_id}/children/open", name="crud_agent_background_task_open_children"
)
async def task_open_children(task_id: TaskIdPath, state: StateDep) -> Children:
    return Children(
        children=await _in_conn(state, lambda conn: background.open_children(conn, task_id))
    )


# --- operations -------------------------------------------------------------------------


class Operation(BaseModel):
    """A row of ``ai_operations`` (the agent's ``Operation``, and its execution)."""

    id: str
    kind: str
    subject: str
    status: Literal["running", "succeeded", "failed"]
    result: Any
    error: dict[str, Any] | None
    created_at: datetime
    finished_at: datetime | None
    workflow_id: str
    workflow_run_id: str


class OneOperation(BaseModel):
    operation: Operation | None


class Actor(BaseModel):
    """Who started a command (the agent's ``OperationActor``)."""

    model_config = ConfigDict(extra="forbid")

    principal: str = Field(max_length=NAME_MAX)
    session: str | None = Field(default=None, max_length=NAME_MAX)
    via_tool: str | None = Field(default=None, max_length=NAME_MAX)


class NewOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=NAME_MAX)
    kind: str = Field(min_length=1, max_length=NAME_MAX)
    subject: str = Field(max_length=NAME_MAX)
    operation_key: str = Field(pattern=OPERATION_KEY)
    request: Any = None
    workflow_id: str = Field(min_length=1, max_length=NAME_MAX)
    workflow_run_id: str = Field(min_length=1, max_length=NAME_MAX)
    actor: Actor | None = None
    retention_seconds: int = Field(
        default=operations.OPERATION_RETENTION_SECONDS, ge=3600, le=10 * 365 * 86400
    )


class OperationError(BaseModel):
    """The problem a failed operation's route answers with (the agent's ``OperationError``),
    kept as the agent sent it."""

    model_config = ConfigDict(extra="allow")

    status: int
    title: str
    detail: str
    type: str | None = None
    extensions: dict[str, Any] | None = None


class FinishOperation(BaseModel):
    """Either ``result`` (a success, ``null`` included) or ``error``."""

    model_config = ConfigDict(extra="forbid")

    result: Any = None
    error: OperationError | None = None

    @model_validator(mode="after")
    def _one(self) -> FinishOperation:
        if self.error is not None and self.result is not None:
            raise ValueError("an operation ends with a result or an error, not both")
        return self


def _operation(row: dict[str, Any] | None) -> Operation | None:
    return None if row is None else Operation.model_validate(row)


@router.get("/operations/by-key/{operation_key}", name="crud_agent_operation_by_key")
async def find_operation(
    operation_key: Annotated[str, Path(pattern=OPERATION_KEY)], state: StateDep
) -> OneOperation:
    row = await _in_conn(state, lambda conn: operations.find(conn, operation_key))
    return OneOperation(operation=_operation(row))


@router.get("/operations/{operation_id}", name="crud_agent_operation")
async def get_operation(operation_id: OperationIdPath, state: StateDep) -> OneOperation:
    row = await _in_conn(state, lambda conn: operations.get(conn, operation_id))
    return OneOperation(operation=_operation(row))


@router.post("/operations", name="crud_agent_operation_insert")
async def insert_operation(body: NewOperation, state: StateDep) -> OneOperation:
    activity = _activity(state)
    actor = body.actor
    row = await _in_conn(
        state,
        lambda conn: operations.insert(
            conn,
            activity,
            operation_id=body.id,
            kind=body.kind,
            subject=body.subject,
            operation_key=body.operation_key,
            request=body.request,
            workflow_id=body.workflow_id,
            workflow_run_id=body.workflow_run_id,
            principal=None if actor is None else actor.principal,
            session=None if actor is None else actor.session,
            via_tool=None if actor is None else actor.via_tool,
            retention_seconds=body.retention_seconds,
        ),
    )
    return OneOperation(operation=_operation(row))


@router.post("/operations/{operation_id}/finish", name="crud_agent_operation_finish")
async def finish_operation(
    operation_id: OperationIdPath, body: FinishOperation, state: StateDep
) -> OneOperation:
    activity = _activity(state)
    error = None if body.error is None else body.error.model_dump(exclude_unset=True)
    row = await _in_conn(
        state,
        lambda conn: operations.finish(
            conn, activity, operation_id, result=body.result, error=error
        ),
    )
    if row is None:
        raise ApiError(404, f"There is no operation {operation_id}.")
    return OneOperation(operation=_operation(row))
