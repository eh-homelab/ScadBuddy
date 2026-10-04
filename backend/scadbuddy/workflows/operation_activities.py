"""``Operation``'s activities (#1053): the record's two writes, and each kind's check and
run under its own name, ``op.<kind>.check`` and ``op.<kind>.run``.

A kind's ``ApiError`` leaves as a non-retryable ``ApplicationError`` carrying the
problem the route answers with, ``REFUSED`` from the check and ``FAILED`` from the run.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

from fastapi import status
from temporalio import activity

from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.core.authorship import AgentAuthor, authored_as
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.operations.kinds import OperationKind
from scadbuddy.operations.store import Operation, OperationStore
from scadbuddy.workflows.operation_models import (
    FINISH_ACTIVITY,
    INSERT_ACTIVITY,
    FinishOp,
    InsertOp,
    RunOp,
    check_activity,
    run_activity,
)
from scadbuddy.workflows.print_activities import raised_as
from scadbuddy.workflows.print_models import FAILED, REFUSED

#: Below the check activity's 8 s start-to-close (`workflows/operation.py` CHECK_TIMEOUT).
CHECK_BUDGET_SECONDS = 6.0


def operation_activities(
    store: OperationStore, settings_store: SettingsStore, kinds: Mapping[str, OperationKind]
) -> list[Callable[..., Any]]:
    @activity.defn(name=INSERT_ACTIVITY)
    async def insert(input: InsertOp) -> Operation:
        info = activity.info()
        assert info.workflow_id is not None and info.workflow_run_id is not None
        retention = settings_store.load().operation_retention_seconds
        op = input.input
        return await store.insert(
            uuid.uuid4().hex,
            kind=op.kind,
            subject=op.subject,
            key=op.key,
            request=op.request,
            workflow_id=info.workflow_id,
            workflow_run_id=info.workflow_run_id,
            retention=timedelta(seconds=retention) if retention is not None else None,
        )

    @activity.defn(name=FINISH_ACTIVITY)
    async def finish(input: FinishOp) -> Operation:
        return await store.finish(input.operation_id, result=input.result, error=input.error)

    activities: list[Callable[..., Any]] = [insert, finish]
    for kind in kinds.values():
        activities += _kind_activities(kind)
    return activities


#: How often a run tells Temporal it is alive (the workflow's ``RUN_HEARTBEAT`` is 30 s).
HEARTBEAT_EVERY = 5.0


async def _heartbeating[T](work: Awaitable[T]) -> T:
    """Await ``work``, heartbeating: the Python SDK delivers a timeout or a cancel to an
    activity only through a heartbeat, so without one a run past its timeout would go
    on to finish an effect the record already calls failed."""
    task = asyncio.ensure_future(work)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_EVERY)
            if done:
                return task.result()
            activity.heartbeat()
    finally:
        task.cancel()


def _kind_activities(kind: OperationKind) -> list[Callable[..., Any]]:
    @activity.defn(name=check_activity(kind.name))
    async def check(request: dict[str, Any]) -> dict[str, Any]:
        try:
            # Inside the activity's own timeout, so a slow Bambuddy is its problem, as
            # the route answered it before #1053, not an unexpected failure.
            async with asyncio.timeout(CHECK_BUDGET_SECONDS):
                return await kind.check(request)
        except TimeoutError:
            slow = ApiError(
                status.HTTP_504_GATEWAY_TIMEOUT,
                f"Bambuddy did not answer within {CHECK_BUDGET_SECONDS:.0f}s; nothing was done",
                type_=UNAVAILABLE_PROBLEM,
            )
            raise raised_as(slow, REFUSED) from None
        except ApiError as error:
            raise raised_as(error, REFUSED) from None

    @activity.defn(name=run_activity(kind.name))
    async def run(input: RunOp) -> dict[str, Any]:
        author = input.author
        try:
            # Set before the run's task is made, which copies the context: its commits,
            # in threads, are the agent's that asked (#252).
            with authored_as(
                None if author is None else AgentAuthor(author.principal, author.session)
            ):
                return await _heartbeating(kind.run(input.request, input.checked))
        except ApiError as error:
            raise raised_as(error, FAILED) from None

    return [check, run]
