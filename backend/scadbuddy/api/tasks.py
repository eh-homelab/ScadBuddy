"""Tasks (#7364): the typed questions flows ask a person, and their answers.

An answer is checked here against the task's schema, then given to the run by the
`task_answer` operation (`flows/operations.py`). It reaches the operation sealed under
the run's key, so it is in no history, record or log in the clear.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Annotated, Any, Final, Literal

import psycopg
from fastapi import APIRouter, Path, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from scadbuddy.api.crud_agent_events import UUID_PATTERN
from scadbuddy.api.flows import browser_only, unavailable
from scadbuddy.api.limits import RouteLimit
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.core.authorship import browser_principal
from scadbuddy.core.events import TASK_PLACE_CHAR, TASK_PLACE_MAX
from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE, ApiError
from scadbuddy.flows.component import FlowsDep
from scadbuddy.flows.operations import TASK_ANSWER, TASK_NOT_OPEN, seal_answer, task_not_open
from scadbuddy.operations.component import OperationsDep
from scadbuddy.tasks.component import TasksComponent, TasksDep
from scadbuddy.tasks.models import MAX_BY_CHARS, MAX_JSON_BYTES, Task, TaskState, json_fits
from scadbuddy.tasks.validate import validate_answer
from scadbuddy.workflows.payload_codec import SubjectForgottenError

INVALID_ANSWER = "https://scadbuddy.dev/problems/tasks/invalid-answer"
MAX_TASKS: Final = 200
#: An answer of `MAX_JSON_BYTES` with its characters escaped, and the body around it.
MAX_BODY_BYTES: Final = 128 * 1024

#: For ``main.py``'s `BodySizeGate`: refused before the body is parsed.
ANSWER_ROUTE_LIMIT: Final = RouteLimit(
    "POST",
    re.compile(r"^/api/v1/tasks/[^/]+/answer$"),
    MAX_BODY_BYTES,
    "a task's answer",
    "fixed, not a setting",
)

router = APIRouter(prefix="/tasks", tags=["tasks"])

#: A task's id is its prompt's call id, a lowercase UUID.
TaskIdPath = Annotated[str, Path(pattern=UUID_PATTERN)]


class TaskList(BaseModel):
    tasks: list[Task]


class TaskAnswerBody(BaseModel):
    """A person's answer to a task."""

    #: The answer, by the schema's property names. At most 16 KiB of JSON.
    answer: dict[str, Any]


class TaskAnswered(BaseModel):
    """What the operation stores, and a follow of its 202 answers: never the answer."""

    run_id: str
    task_id: str
    outcome: Literal["answered"]


async def _found(tasks: TasksComponent, task_id: str) -> Task:
    task = await asyncio.to_thread(tasks.store.get, task_id)
    if task is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such task.")
    return task


@router.get("", summary="List tasks")
def list_tasks(
    tasks: TasksDep,
    state: TaskState = "open",
    place: Annotated[
        str | None,
        Query(min_length=1, max_length=TASK_PLACE_MAX, pattern=rf"^{TASK_PLACE_CHAR}+$"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_TASKS)] = 100,
) -> TaskList:
    """The tasks in ``state``, at ``place`` when it is given, oldest first."""
    return TaskList(tasks=tasks.store.list(state=state, place=place, limit=limit))


@router.get(
    "/{task_id}",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such task"}},
    summary="Get a task",
)
async def get_task(task_id: TaskIdPath, tasks: TasksDep) -> Task:
    return await _found(tasks, task_id)


def _fits(answer: dict[str, Any]) -> bool:
    try:
        return json_fits(answer)
    except RecursionError:
        return False


@router.post(
    "/{task_id}/answer",
    response_model=Task,
    responses={
        **OPERATION_RESPONSES,
        status.HTTP_404_NOT_FOUND: {"description": "No such task"},
        status.HTTP_409_CONFLICT: {
            "content": {PROBLEM_MEDIA_TYPE: {}},
            "description": f"`{TASK_NOT_OPEN}`, with the task's `state`, which may still be"
            " `open` for a moment; or the run ended or no longer waits for this answer. Read"
            " the task again",
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": f"The answer is more than {MAX_JSON_BYTES} bytes of JSON"
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "content": {PROBLEM_MEDIA_TYPE: {}},
            "description": f"`{INVALID_ANSWER}`, with `problems` (`field` and `message`): the"
            " answer does not fit the task's schema, and the task is still open",
        },
    },
    summary="Answer a task",
)
async def answer_task(
    task_id: TaskIdPath,
    body: TaskAnswerBody,
    response: Response,
    ops: OperationsDep,
    flows: FlowsDep,
    tasks: TasksDep,
    idempotency_key: IdempotencyKey = None,
) -> Task | JSONResponse:
    """Check the answer against the task's schema and give it to the run. The same request
    again answers the task as it stands. A 202's operation says only that it was answered:
    read the task itself at ``GET /tasks/{id}``. Read it again after a 409 too. A 409
    ``not-open`` has the task's own ``state``, which is ``open`` for a moment when the run
    has another answer that is not marked yet."""
    browser_only("Only a person answers a task.")
    answer = body.answer
    # First: nothing below walks an answer that is too large.
    if not _fits(answer):
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"An answer is at most {MAX_JSON_BYTES} bytes of JSON.",
        )
    task = await _found(tasks, task_id)
    if flows.keys is None:
        raise unavailable()
    # The same text for the same answer, so a resend seals to the same bytes. ASCII, so
    # that any text can be sealed: what cannot be stored is refused below.
    text = json.dumps(answer, sort_keys=True, separators=(",", ":"))
    try:
        sealed = await seal_answer(flows.keys, task.run_id, text)
    except SubjectForgottenError:
        raise ApiError(status.HTTP_404_NOT_FOUND, "No such task.") from None
    except psycopg.errors.UndefinedTable:
        raise unavailable() from None

    async def refuse() -> None:
        # Not for a repeat, which is its first answer however the task stands now.
        if task.state != "open":
            raise task_not_open(task.state)
        problems = await asyncio.to_thread(validate_answer, task.schema_, answer)
        if problems:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "The answer does not fit the question.",
                type_=INVALID_ANSWER,
                problems=[problem.model_dump() for problem in problems],
            )

    try:
        result = await run_operation(
            ops,
            response,
            kind=ops.kinds[TASK_ANSWER],
            subject=f"flow:{task.run_id}",
            request={
                "run_id": task.run_id,
                "task_id": task.id,
                "answer_sealed": sealed,
                "by": browser_principal()[:MAX_BY_CHARS],
            },
            idempotency_key=idempotency_key,
            before_start=refuse,
        )
    except ApiError as error:
        if error.type != TASK_NOT_OPEN:
            raise
        error.extensions["state"] = (await _found(tasks, task_id)).state
        raise
    answered = operation_answer(result, TaskAnswered)
    if isinstance(answered, JSONResponse):
        return answered
    return await _found(tasks, task_id)
