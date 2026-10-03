"""``Operation``'s activities (#1053): the record's two writes, and each kind's check and
run under its own name, ``op.<kind>.check`` and ``op.<kind>.run``.

A kind's ``ApiError`` leaves as a non-retryable ``ApplicationError`` carrying the
problem the route answers with, ``REFUSED`` from the check and ``FAILED`` from the run.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

from temporalio import activity

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


def _kind_activities(kind: OperationKind) -> list[Callable[..., Any]]:
    @activity.defn(name=check_activity(kind.name))
    async def check(request: dict[str, Any]) -> dict[str, Any]:
        try:
            return await kind.check(request)
        except ApiError as error:
            raise raised_as(error, REFUSED) from None

    @activity.defn(name=run_activity(kind.name))
    async def run(input: RunOp) -> dict[str, Any]:
        try:
            return await kind.run(input.request, input.checked)
        except ApiError as error:
            raise raised_as(error, FAILED) from None

    return [check, run]
