"""The projects worker's task routes (#7364): thin calls to the tasks component's
`TaskStore`, for `crud/tasks.py` `RemoteTasks`. The projects worker has no SQL (#5295).

Every write is natural (plan D5): sent again, it answers as it did the first time. A
refusal carries a ``reason`` the client turns back into its typed error. The routes are
plain ``def``, so the store's blocking pool runs in a thread. A task id is the harness's
call id and a run id a run's: both a lowercase UUID, refused with 422 otherwise.

Nothing here logs a task's schema, context or answer. A refusal never quotes an answer or
a context; a refused schema's problems name its parts.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import status
from pydantic import BaseModel, field_validator

from scadbuddy.core.json_values import unstorable
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.tasks.component import TasksDep
from scadbuddy.tasks.models import MAX_BY_CHARS, MAX_JSON_BYTES, Task, TaskOpen, json_fits
from scadbuddy.tasks.store import RunEndedError, RunNotFoundError, TaskNotOpenError

#: `crud/tasks.py` repeats these.
RUN_NOT_FOUND = "run-not-found"
RUN_ENDED = "run-ended"
TASK_NOT_FOUND = "task-not-found"
TASK_NOT_OPEN = "task-not-open"
#: Matched whole (`fullmatch`: a trailing newline fails).
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

router = crud_router("projects")


class CloseWrite(BaseModel):
    state: Literal["expired", "cancelled"]


class AnswerWrite(BaseModel):
    answer: dict[str, Any] | None
    by: str | None

    @field_validator("answer", "by")
    @classmethod
    def _can_be_stored(cls, value: Any) -> Any:
        for bad in unstorable(value):
            raise ValueError(bad.message)
        return value

    @field_validator("answer")
    @classmethod
    def _answer_is_small(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if not json_fits(value):
            raise ValueError(f"the answer is more than {MAX_JSON_BYTES} bytes of JSON")
        return value

    @field_validator("by")
    @classmethod
    def _by_is_short(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_BY_CHARS:
            raise ValueError(f"the name is more than {MAX_BY_CHARS} characters")
        return value


def _uuid(what: str, value: str) -> str:
    if not _UUID.fullmatch(value):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"A {what} is a lowercase UUID.",
            reason="not-a-uuid",
        )
    return value


def _not_open(state: str) -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT, f"The task is {state}.", reason=TASK_NOT_OPEN, state=state
    )


def _missing() -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, "No such task.", reason=TASK_NOT_FOUND)


@router.post("/tasks", name="crud_projects_open_task")
def open_task(body: TaskOpen, tasks: TasksDep) -> Task:
    """`TaskStore.open`: the same id again answers the stored row, whatever its run's
    state."""
    _uuid("task id", body.id)
    _uuid("run id", body.run_id)
    try:
        return tasks.store.open(body)
    except RunNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such run.", reason=RUN_NOT_FOUND) from None
    except RunEndedError:
        raise ApiError(status.HTTP_409_CONFLICT, "The run has ended.", reason=RUN_ENDED) from None


@router.post("/tasks/{task_id}/close", name="crud_projects_close_task")
def close_task(task_id: str, body: CloseWrite, tasks: TasksDep) -> Task:
    """`TaskStore.close_or_current`. A task already in the asked state answers its row, so
    a retry after a lost answer is the same request; another state is 409
    ``task-not-open``."""
    _uuid("task id", task_id)
    held = tasks.store.close_or_current(task_id, body.state)
    if held is None:
        raise _missing()
    if held.state == body.state:
        return held
    raise _not_open(held.state)


@router.post("/tasks/{task_id}/answered", name="crud_projects_answer_task")
def answer_task(task_id: str, body: AnswerWrite, tasks: TasksDep) -> Task:
    """`TaskStore.mark_answered`, with its rules: the same answer again answers the row."""
    _uuid("task id", task_id)
    try:
        return tasks.store.mark_answered(task_id, body.answer, body.by)
    except KeyError:
        raise _missing() from None
    except TaskNotOpenError as error:
        raise _not_open(error.state) from None
