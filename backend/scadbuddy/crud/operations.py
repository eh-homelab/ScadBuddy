"""The `operations` area of the CRUD routes (#5298, inventory J24, J25): an `Operation`
workflow's record, written by the API for the worker that runs it, and
`RemoteOperationStore`, that worker's `OperationStore` over them.

Both writes are natural (plan D5). An insert sent again finds the execution's row by its
workflow ids and answers it. A finish sent again finds the row ended and answers it, and
only the write that ended the row counts it (#2295). Each write's `operation.changed`
event commits with it (D7). The insert prunes past the API's own
`operation_retention_seconds`, so a worker loads no settings for it.

A row names no caller, so any caller served this area can end any operation, as each
worker's role could over SQL before.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, status
from pydantic import BaseModel

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.bambuddy.runs import PrintRunError
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.client import CrudClient, CrudRefusedError
from scadbuddy.crud.guard import PREFIX, CrudCaller, crud_router
from scadbuddy.operations.component import OperationsDep
from scadbuddy.operations.store import Operation
from scadbuddy.workflows.operation_models import OperationAuthor


class OperationWrite(BaseModel):
    op_id: str
    kind: str
    subject: str
    operation_key: str
    request: dict[str, Any]
    workflow_id: str
    workflow_run_id: str
    idempotency_key: str | None = None
    author: OperationAuthor | None = None
    person: str | None = None


class OperationEnd(BaseModel):
    result: dict[str, Any] | None = None
    error: PrintRunError | None = None


def operations_router(caller: CrudCaller) -> APIRouter:
    """``POST /operations`` and ``POST /operations/{op_id}/finish`` for ``caller``."""
    router = crud_router(caller)

    @router.post("/operations", name=f"crud_{caller}_insert_operation")
    async def insert(
        body: OperationWrite, ops: OperationsDep, settings: SettingsStoreDep
    ) -> Operation:
        seconds = (await asyncio.to_thread(settings.load)).operation_retention_seconds
        return await ops.store.insert(
            body.op_id,
            kind=body.kind,
            subject=body.subject,
            operation_key=body.operation_key,
            request=body.request,
            workflow_id=body.workflow_id,
            workflow_run_id=body.workflow_run_id,
            retention=timedelta(seconds=seconds) if seconds is not None else None,
            idempotency_key=body.idempotency_key,
            author=body.author,
            person=body.person,
        )

    @router.post("/operations/{op_id}/finish", name=f"crud_{caller}_finish_operation")
    async def finish(op_id: str, body: OperationEnd, ops: OperationsDep) -> Operation:
        try:
            return await ops.store.finish(op_id, result=body.result, error=body.error)
        except LookupError:
            raise ApiError(status.HTTP_404_NOT_FOUND, "No such operation.") from None

    return router


class RemoteOperationStore:
    """`OperationStore.insert` and `finish` over the routes above, with the same
    signatures and answers."""

    def __init__(self, client: CrudClient, caller: CrudCaller) -> None:
        self._client = client
        self._base = f"/api/v1{PREFIX}/{caller}/operations"

    async def insert(
        self,
        op_id: str,
        *,
        kind: str,
        subject: str,
        operation_key: str,
        request: dict[str, Any],
        workflow_id: str,
        workflow_run_id: str,
        retention: timedelta | None = None,
        idempotency_key: str | None = None,
        author: OperationAuthor | None = None,
        person: str | None = None,
    ) -> Operation:
        """``retention`` is not sent: the route prunes past the API's own setting."""
        body = OperationWrite(
            op_id=op_id,
            kind=kind,
            subject=subject,
            operation_key=operation_key,
            request=request,
            workflow_id=workflow_id,
            workflow_run_id=workflow_run_id,
            idempotency_key=idempotency_key,
            author=author,
            person=person,
        )
        response = await self._client.request("POST", self._base, json=body.model_dump(mode="json"))
        return Operation.model_validate(response.json())

    async def finish(
        self,
        op_id: str,
        *,
        result: dict[str, Any] | None = None,
        error: PrintRunError | None = None,
    ) -> Operation:
        end = OperationEnd(result=result, error=error)
        try:
            response = await self._client.request(
                "POST",
                f"{self._base}/{quote(op_id, safe='')}/finish",
                json=end.model_dump(mode="json"),
            )
        except CrudRefusedError as refused:
            if refused.status == 404:
                raise LookupError(f"there is no operation {op_id}") from refused
            raise
        return Operation.model_validate(response.json())
