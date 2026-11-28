"""Tasks (#7364): the typed questions flows ask, and their answers.

An answer is checked here against the task's schema, then given to the run by an
operation (`flows/operations.py`): `task_answer` for a person's, `flow_agent_answer` for
an assistant session's or MCP token's that `flow_answerers` or the run's grant admits
(#7471). It reaches the operation sealed under the run's key, so it is in no history,
record or log in the clear.
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
from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.api.flows import admission, agent_answer, answerer, unavailable
from scadbuddy.api.limits import RouteLimit
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.auth.component import CallerDep
from scadbuddy.codes.parse import parse_code
from scadbuddy.core.authorship import browser_principal
from scadbuddy.core.events import TASK_PLACE_CHAR, TASK_PLACE_MAX
from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE, ApiError
from scadbuddy.flows.answerers import BY_SETTING
from scadbuddy.flows.component import Flows, FlowsDep
from scadbuddy.flows.operations import (
    AGENT_ANSWER,
    TASK_ANSWER,
    TASK_NOT_OPEN,
    seal_answer,
    task_not_open,
)
from scadbuddy.operations.component import OperationsDep
from scadbuddy.tasks.component import TasksComponent, TasksDep
from scadbuddy.tasks.models import (
    MAX_BY_CHARS,
    MAX_JSON_BYTES,
    Task,
    TaskQuestion,
    TaskState,
    folded_place,
    json_fits,
)
from scadbuddy.tasks.validate import validate_answer
from scadbuddy.workflows.payload_codec import SubjectForgottenError

INVALID_ANSWER = "https://scadbuddy.dev/problems/tasks/invalid-answer"
MAX_TASKS: Final = 200
MAX_CODE_CHARS: Final = 40
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
    tasks: list[TaskQuestion]


class TaskView(Task):
    """A task, and for an agent's answer what admitted it (#7471): `setting`, or the id of
    the run's grant."""

    admitted_by: str | None = None


class TaskAnswerBody(BaseModel):
    """An answer to a task: a person's, or an admitted assistant's (#7471)."""

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
    code: Annotated[
        str | None,
        Query(max_length=MAX_CODE_CHARS, description="A scanned code, in any case, such as `l-7`."),
    ] = None,
    assignee: Annotated[Literal["me"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_TASKS)] = 100,
) -> TaskList:
    """The tasks in ``state``, at ``place`` when it is given, oldest first. Without their
    answers: ``GET /tasks/{id}`` has one task's own. With ``code``, the open tasks that
    expect that code or its kind (422 for text that is no code). With ``assignee=me``,
    the tasks for the signed-in person."""
    parsed = None
    if code is not None:
        parsed = parse_code(code)
        if parsed is None:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "That is not a code.")
    return TaskList(
        tasks=tasks.store.list(
            state=state,
            # A task opened before #8062 kept its place as it was given (`l7`).
            places=None if place is None else (place, folded_place(place)),
            code=parsed,
            assignee=browser_principal()[:MAX_BY_CHARS] if assignee == "me" else None,
            limit=limit,
        )
    )


@router.get(
    "/{task_id}",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such task"}},
    summary="Get a task",
)
async def get_task(task_id: TaskIdPath, tasks: TasksDep, flows: FlowsDep) -> TaskView:
    # The answer and who gave it too, for every caller of an `api` route
    # (`auth/routes.py`). No task route is a person's alone: an agent answers only when
    # the setting `flow_answerers` or the run's grant admits it (`api/flows.py` `answerer`
    # and `admit`, #7471).
    return await _view(tasks, flows, task_id)


async def _view(tasks: TasksComponent, flows: Flows, task_id: str) -> TaskView:
    """The task, with what admitted an agent's answer from its decision's row."""
    task = await _found(tasks, task_id)
    view = TaskView.model_validate(task.model_dump(by_alias=True))
    if task.answered_by is not None and task.answered_by.startswith(("session:", "token:")):
        answers = [
            d
            for d in await flows.store.answers_of_call(task.run_id, task.id)
            if d.responder == task.answered_by
        ]
        view.admitted_by = answers[-1].admitted_by if answers else None
    return view


def _fits(answer: dict[str, Any]) -> bool:
    try:
        return json_fits(answer)
    except RecursionError:
        return False


@router.post(
    "/{task_id}/answer",
    response_model=TaskView,
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
    settings: SettingsStoreDep,
    caller: CallerDep,
    idempotency_key: IdempotencyKey = None,
) -> TaskView | JSONResponse:
    """Check the answer against the task's schema and give it to the run. The same request
    again returns the task as it stands. A 202's operation says only that it was answered:
    read the task itself at ``GET /tasks/{id}``. Read it again after a 409 too. A 409
    ``not-open`` has the task's own ``state``, which is ``open`` for a moment when the run
    has another answer that is not marked yet. A person answers; an assistant session or
    MCP token only when the setting ``flow_answerers`` or the run's approved grant names it and the
    question is not for people only (#7471). The task read says which (``admitted_by``)."""
    found = answerer(caller)
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

    person = found.kind == "person"
    # A task's id is its prompt's call id (`workflows/flow_tools.py` `_prompt_task`): the
    # parked entry's key.
    admitted_by, refusal = (
        (BY_SETTING, None)
        if person
        else await admission(found, settings, flows, task.run_id, task.id)
    )

    async def refuse() -> None:
        # Not for a repeat, which is its first answer however the task stands now.
        if refusal is not None:
            raise refusal
        if task.state != "open":
            raise task_not_open(task.state)
        problems = await asyncio.to_thread(validate_answer, task.schema_, answer)
        if not problems:
            problems = await asyncio.to_thread(
                tasks.store.answer_problems, task.id, task.schema_, answer
            )
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
            kind=ops.kinds[TASK_ANSWER if person else AGENT_ANSWER],
            subject=f"flow:{task.run_id}",
            request=(
                {
                    "run_id": task.run_id,
                    "task_id": task.id,
                    "answer_sealed": sealed,
                    "by": browser_principal()[:MAX_BY_CHARS],
                }
                if person
                else {
                    "run_id": task.run_id,
                    "task_id": task.id,
                    "answer_sealed": sealed,
                    **agent_answer(found, admitted_by),
                }
            ),
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
    return await _view(tasks, flows, task_id)
