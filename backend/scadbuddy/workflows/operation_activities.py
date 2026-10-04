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
from scadbuddy.operations.kinds import (
    CHECK_ON_BAMBUDDY,
    THREAD_STEPS,
    OperationKind,
    ThreadSteps,
)
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
            operation_key=op.key,
            request=op.request,
            workflow_id=info.workflow_id,
            workflow_run_id=info.workflow_run_id,
            retention=timedelta(seconds=retention) if retention is not None else None,
            idempotency_key=op.idempotency_key,
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


#: How long a cancelled run is given to unwind (its locks released) before the activity
#: returns; a run still waiting on a thread then finishes unwinding on its own.
CANCEL_GRACE = 30.0


#: How long before the run's start-to-close a run still in a thread answers for itself,
#: so its answer reaches the record before Temporal's own timeout does.
LANDING_MARGIN = 1.0


async def _heartbeating[T](work: Awaitable[T], *, where: str) -> T:
    """Await ``work``, heartbeating: the Python SDK delivers a timeout or a cancel to an
    activity only through a heartbeat, so without one a run past its timeout would go
    on to its next step after the record already calls it failed. A step already in a
    thread cannot be stopped: one under ``operations.kinds.to_thread_to_end`` keeps its
    locks until it returns, and its effect (a commit in flight) can still land. A run
    still in such a step just before its timeout fails saying so (review #1119 2-4),
    rather than as the timeout's unexpected failure, and is left to unwind."""
    steps = ThreadSteps()
    token = THREAD_STEPS.set(steps)
    try:
        task = asyncio.ensure_future(work)  # copies the context, and so ``steps``
    finally:
        THREAD_STEPS.reset(token)
    loop = asyncio.get_running_loop()
    limit = activity.info().start_to_close_timeout
    landing = None if limit is None else loop.time() + limit.total_seconds() - LANDING_MARGIN
    left = False
    try:
        while True:
            wait = HEARTBEAT_EVERY
            if landing is not None and loop.time() < landing:
                wait = min(wait, landing - loop.time())
            done, _ = await asyncio.wait({task}, timeout=wait)
            if done:
                return task.result()
            if landing is not None and loop.time() >= landing and steps.running:
                left = True
                raise ApiError(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "This ran out of time while a step that cannot be stopped was still "
                    f"running, so it may have been done. Reload and check {where} before "
                    "trying again.",
                )
            activity.heartbeat()
    finally:
        if not task.done():
            task.cancel()
            if not left:
                await asyncio.wait({task}, timeout=CANCEL_GRACE)


def _kind_activities(kind: OperationKind) -> list[Callable[..., Any]]:
    @activity.defn(name=check_activity(kind.name))
    async def check(request: dict[str, Any]) -> dict[str, Any]:
        waiting = [False]
        CHECK_ON_BAMBUDDY.set(waiting)
        try:
            # Inside the activity's own timeout, so a slow check is a problem the route
            # answers, not an unexpected failure. Only a wait on Bambuddy blames it, as
            # the route did before #1053.
            async with asyncio.timeout(CHECK_BUDGET_SECONDS):
                return await kind.check(request)
        except TimeoutError:
            if waiting[0]:
                slow = ApiError(
                    status.HTTP_504_GATEWAY_TIMEOUT,
                    f"Bambuddy did not answer within {CHECK_BUDGET_SECONDS:.0f}s; nothing was done",
                    type_=UNAVAILABLE_PROBLEM,
                )
            else:
                slow = ApiError(
                    status.HTTP_504_GATEWAY_TIMEOUT,
                    f"the check did not finish within {CHECK_BUDGET_SECONDS:.0f}s; "
                    "nothing was done",
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
                return await _heartbeating(
                    kind.run(input.request, input.checked), where=str(kind.where)
                )
        except ApiError as error:
            raise raised_as(error, FAILED) from None

    return [check, run]
