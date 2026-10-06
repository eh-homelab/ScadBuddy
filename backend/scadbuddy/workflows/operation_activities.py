"""``Operation``'s activities (#1053): the record's two writes, and each kind's check and
run under its own name, ``op.<kind>.check`` and ``op.<kind>.run``.

A kind's ``ApiError`` leaves as a non-retryable ``ApplicationError`` carrying the
problem the route answers with, ``REFUSED`` from the check and ``FAILED`` from the run.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

from fastapi import status
from temporalio import activity

from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.operations.kinds import CHECK_ON_BAMBUDDY, OperationKind
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
from scadbuddy.workflows.print_activities import heartbeating, raised_as
from scadbuddy.workflows.print_models import FAILED, REFUSED

#: Below the check activity's 8 s start-to-close (`workflows/operation.py` CHECK_TIMEOUT).
#: It stops the wait, not the work: a check's ``asyncio.to_thread`` (the settings, the
#: output, ``output_stem``'s reads) runs on after the 504, and each re-send may start
#: another. Accepted: those reads are bounded, and the thread ends when they do (review
#: #1063 5).
CHECK_BUDGET_SECONDS = 6.0


def operation_activities(
    store: OperationStore, settings_store: SettingsStore, kinds: Mapping[str, OperationKind]
) -> list[Callable[..., Any]]:
    @activity.defn(name=INSERT_ACTIVITY)
    async def insert(input: InsertOp) -> Operation:
        info = activity.info()
        assert info.workflow_id is not None and info.workflow_run_id is not None
        retention = (await asyncio.to_thread(settings_store.load)).operation_retention_seconds
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
        )

    @activity.defn(name=FINISH_ACTIVITY)
    async def finish(input: FinishOp) -> Operation:
        return await store.finish(input.operation_id, result=input.result, error=input.error)

    activities: list[Callable[..., Any]] = [insert, finish]
    for kind in kinds.values():
        activities += _kind_activities(kind)
    return activities


def _kind_activities(kind: OperationKind) -> list[Callable[..., Any]]:
    @activity.defn(name=check_activity(kind.name))
    async def check(request: dict[str, Any]) -> dict[str, Any]:
        waiting = [False]
        CHECK_ON_BAMBUDDY.set(waiting)
        try:
            # Inside the activity's own timeout, so a slow check is a problem the route
            # answers, not an unexpected failure (its thread work runs on; see
            # CHECK_BUDGET_SECONDS). Only a wait on Bambuddy blames it, as the route did
            # before #1053.
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
        try:
            # Heartbeats, so a run on a worker that died ends at the heartbeat timeout.
            return await heartbeating(kind.run(input.request, input.checked))
        except ApiError as error:
            raise raised_as(error, FAILED) from None

    return [check, run]
