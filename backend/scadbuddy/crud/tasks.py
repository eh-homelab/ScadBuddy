"""The projects worker's `TaskStore` over the CRUD routes (#7364): `RemoteTasks` has the
store's writes, with its answers and errors, and holds no database secret. The routes are
`api/crud_tasks.py`.

Every write it sends is natural: the client may send it again after a lost answer, and
the route answers as it did the first time. So `close` on a task already in the asked
state answers its row, and `None` only for a task in another state (or none).
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import quote

from scadbuddy.crud.client import CrudClient, CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.guard import PREFIX
from scadbuddy.tasks.models import Task, TaskOpen
from scadbuddy.tasks.store import (
    RunBehindError,
    RunEndedError,
    RunNotFoundError,
    TaskNotOpenError,
)

BASE = f"/api/v1{PREFIX}/projects/tasks"
#: `api/crud_tasks.py`'s reasons.
RUN_NOT_FOUND = "run-not-found"
RUN_ENDED = "run-ended"
RUN_BEHIND = "run-behind"
TASK_NOT_FOUND = "task-not-found"
TASK_NOT_OPEN = "task-not-open"


def _part(value: str) -> str:
    return quote(value, safe="")


def _behind(error: CrudUnavailableError, task_id: str) -> None:
    """The run's row still lagged Temporal at the end of the client's budget."""
    if error.problem.get("reason") == RUN_BEHIND:
        raise RunBehindError(task_id) from error


class RemoteTasks:
    def __init__(self, crud: CrudClient) -> None:
        self._client = crud

    async def open(self, task: TaskOpen, *, workflow_run_id: str | None = None) -> Task:
        """`TaskStore.open`: the same id again answers the stored row. Raises
        `RunNotFoundError` for an unknown run and `RunEndedError` for one that ended, or
        whose execution ``workflow_run_id`` a Reset replaced."""
        body = task.model_dump(mode="json", by_alias=True) | {"workflow_run_id": workflow_run_id}
        try:
            response = await self._client.request("POST", BASE, json=body)
        except CrudRefusedError as error:
            reason = error.problem.get("reason")
            if error.status == 404 and reason == RUN_NOT_FOUND:
                raise RunNotFoundError(task.run_id) from error
            if error.status == 409 and reason == RUN_ENDED:
                raise RunEndedError(task.run_id) from error
            raise
        return Task.model_validate(response.json())

    async def close(
        self,
        task_id: str,
        state: Literal["expired", "cancelled"],
        *,
        workflow_run_id: str | None = None,
    ) -> Task | None:
        """`TaskStore.close_or_current`: the row once it is in ``state`` (sent again, it
        still is), or ``None`` when the task is in another state or missing. A missing task
        is not an error: the run may be forgotten while its timeout closes the task. Raises
        `RunBehindError` as the store does."""
        try:
            response = await self._client.request(
                "POST",
                f"{BASE}/{_part(task_id)}/close",
                json={"state": state, "workflow_run_id": workflow_run_id},
            )
        except CrudUnavailableError as error:
            _behind(error, task_id)
            raise
        except CrudRefusedError as error:
            reason = error.problem.get("reason")
            if (error.status, reason) in ((409, TASK_NOT_OPEN), (404, TASK_NOT_FOUND)):
                return None
            raise
        return Task.model_validate(response.json())

    async def mark_answered(
        self,
        task_id: str,
        answer: dict[str, Any],
        by: str | None,
        workflow_run_id: str,
        *,
        final: bool = False,
    ) -> Task:
        """`TaskStore.mark_answered`, with its rules; the same answer again answers the
        row, and so does one of another execution. Raises `TaskNotOpenError`, with ``final``
        `RunBehindError` and, for a missing task, `KeyError`."""
        try:
            response = await self._client.request(
                "POST",
                f"{BASE}/{_part(task_id)}/answered",
                json={
                    "answer": answer,
                    "by": by,
                    "workflow_run_id": workflow_run_id,
                    "final": final,
                },
            )
        except CrudUnavailableError as error:
            _behind(error, task_id)
            raise
        except CrudRefusedError as error:
            reason = error.problem.get("reason")
            if error.status == 409 and reason == TASK_NOT_OPEN:
                raise TaskNotOpenError(error.problem["state"]) from error
            if error.status == 404 and reason == TASK_NOT_FOUND:
                raise KeyError(task_id) from error
            raise
        return Task.model_validate(response.json())
