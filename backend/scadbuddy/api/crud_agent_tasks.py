"""The agent's command record behind CRUD routes (#5299 step 8): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`). Each
route is one transaction (D2); the SQL is in ``agent_data/operations.py``, and the agent's
store (``agent/src/operations/store.ts`` `OperationStore`) calls these routes.

Area ``background-tasks`` and the reads of area ``operations`` are Go's
(``tools/go/core/internal/tasks``, #7667). These stay here: they write the activity row.

Area ``operations``, ``ai_operations``:

- ``POST …/operations``: a record of an accepted execution, or the execution's row;
- ``POST …/operations/{id}/finish``: its end.

An inserted or ended operation's ``activity`` row is written in the same transaction (the
API's own table, ``activity/index.py`` ``ingest_agent_operation_in``). No route takes an
``Idempotency-Key``: an operation's insert and end are natural writes, safe to send again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal, TypeVar

from fastapi import Path
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scadbuddy.activity.component import ACTIVITY
from scadbuddy.activity.index import ActivityIndex
from scadbuddy.agent_data import operations
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

Pool = ConnectionPool[Connection[DictRow]]

T = TypeVar("T")

#: An operation's key: the agent's sha256 (`operationKey`).
OPERATION_KEY = r"^[0-9a-f]{64}$"
#: The longest operation id, kind, subject or workflow id.
NAME_MAX = 512

OperationIdPath = Annotated[str, Path(min_length=1, max_length=NAME_MAX)]


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
