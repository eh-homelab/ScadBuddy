"""Forgetting a flow run (spec 2026-10-01 §6.5): `agent/src/sessions/forget.ts`
`forgetSubject` for `flow-<uuid>`, in the same order, plus the run's row.

1. The subject's tombstone and its key's deletion, under the lock key creation takes,
   in one transaction: from then on every copy of the run's payloads (history,
   Visibility, Archival) is undecryptable, whatever fails next. A process that read the
   key keeps it 30 s at most (`PgPayloadKeys`).
2. The workflow terminated if open, then its execution deleted.
3. The `workflow_runs` row.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel
from temporalio.api.common.v1 import WorkflowExecution
from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.flows.store import FlowStore
from scadbuddy.workflows.payload_codec import Connect, payload_key_lock, subject_of


class Forgotten(BaseModel):
    """What forgetting found: whether there was a key, an execution and a row."""

    key: bool
    workflow: Literal["deleted", "not_found"]
    row: bool


async def forget_run(
    run_id: str, *, connect: Connect, client: Client, store: FlowStore
) -> Forgotten:
    subject = f"flow-{run_id}"
    if subject_of(subject) is None:
        raise ValueError(f"{run_id} is not a flow run id")
    async with connect() as conn, conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (payload_key_lock(subject),)
        )
        await conn.execute(
            "INSERT INTO ai_forgotten_subjects (subject) VALUES (%s)"
            " ON CONFLICT (subject) DO NOTHING",
            (subject,),
        )
        cur = await conn.execute("DELETE FROM ai_payload_keys WHERE subject = %s", (subject,))
        key = cur.rowcount > 0
    try:
        await client.get_workflow_handle(subject).terminate(
            reason="forgotten (DELETE /workflow-runs)"
        )
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise
    workflow: Literal["deleted", "not_found"] = "deleted"
    try:
        await client.workflow_service.delete_workflow_execution(
            DeleteWorkflowExecutionRequest(
                namespace=client.namespace,
                workflow_execution=WorkflowExecution(workflow_id=subject),
            )
        )
    except RPCError as err:
        if err.status != RPCStatusCode.NOT_FOUND:
            raise
        workflow = "not_found"
    row = await store.delete_run(run_id)
    return Forgotten(key=key, workflow=workflow, row=row)
