"""A flow run's parked entries, ended by a timer through the harness's own Updates
(plan 2026-10-09-durable-phase-6-flows.md Ruling 11, as revised).

The harness's callback gate has one timeout, fixed when the tool is declared, and does
not clear its entry when the waiting call is cancelled: the entry stays in
`agent_status.pending_callbacks` and a late `provide_callback_result` is accepted
(tests/flows/test_harness_facts.py case 7). So a per-call timeout never cancels the
wait. It runs a workflow timer beside it, and when the timer fires, the
`flow_entry_timeout` activity resolves the entry with the harness's public Update, as a
person's answer would be (decisions A and B). The harness then ends the call its own
way, and the caller sees `TimeoutError`.

Approvals the same way (the user's answer to plan open question 5): an outward call
parked longer than its run's approval timeout is denied with `tool_approval`'s public
deny, and the script sees `ToolApprovalDenied: timed out`.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_client import (
        AgentClient,
        CallbackResultError,
        ToolApprovalError,
    )

FLOW_ENTRY_TIMEOUT = "flow_entry_timeout"
#: The error a timed-out callback is resolved with; what the harness hands the caller.
TIMED_OUT = "timed out"
#: A transport failure is retried a few times (the same Update id makes a resend
#: safe); any other refusal from the harness is final.
TIMEOUT_RETRY = RetryPolicy(maximum_attempts=5, non_retryable_error_types=["EntryRefused"])


class EntryTimeout(BaseModel):
    """Which run's which parked call to end."""

    workflow_id: str
    call_id: str
    #: A callback is answered with an error; an approval is denied.
    kind: Literal["answer", "approval"] = "answer"


@activity.defn(name=FLOW_ENTRY_TIMEOUT)
async def flow_entry_timeout(entry: EntryTimeout) -> bool:
    """Resolve a parked callback as timed out; False when an answer resolved it first.
    A retried attempt sends the same Update id, so Temporal answers it as the first."""
    client = AgentClient(activity.client(), entry.workflow_id)
    try:
        if entry.kind == "approval":
            await client.approve_tool(
                entry.call_id,
                approved=False,
                reason=TIMED_OUT,
                update_id=f"timeout-{entry.call_id}",
            )
        else:
            await client.provide_callback_result(
                entry.call_id, error=TIMED_OUT, update_id=f"timeout-{entry.call_id}"
            )
    except (CallbackResultError, ToolApprovalError) as err:
        if err.error_type in {
            "CallbackAlreadyResolved",
            "UnknownCallback",
            "ToolApprovalAlreadyResolved",
            "UnknownToolApproval",
        }:
            return False
        raise ApplicationError(
            f"the harness refused the timeout ({err.error_type})",
            type="EntryRefused",
            non_retryable=True,
        ) from None
    return True


async def run_callback(
    run_tool: Callable[..., Awaitable[Any]],
    tool: Callable[..., Awaitable[Any]],
    timeout_s: float,
    *,
    call_id: str | None = None,
    **kwargs: Any,
) -> Any:
    """Run the callback `tool` through the runner's `run_tool`, ending it after
    `timeout_s` seconds with the harness's own Update. Raises `TimeoutError` then.

    `call_id` is the harness's id for the call (a fresh one when omitted): the id the
    entry is listed and answered under, which the caller may record first."""
    call_id = call_id if call_id is not None else str(workflow.uuid4())
    call = asyncio.ensure_future(run_tool(call_id, tool, **kwargs))
    timed_out = False
    try:
        await workflow.wait_condition(call.done, timeout=timedelta(seconds=timeout_s))
    except TimeoutError:
        try:
            timed_out = await workflow.execute_activity(
                FLOW_ENTRY_TIMEOUT,
                EntryTimeout(workflow_id=workflow.info().workflow_id, call_id=call_id),
                result_type=bool,
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=TIMEOUT_RETRY,
            )
        except ActivityError as err:
            # The harness never ended the entry: stop waiting on it, so the caller sees
            # its timeout and no task is left behind. The entry stays listed until the
            # run closes (Ruling 11, "When the activity itself fails").
            call.cancel()
            with contextlib.suppress(asyncio.CancelledError, agent.CallbackToolError):
                await call
            raise TimeoutError(f"no answer within {timeout_s:g} seconds") from err
    try:
        return await call
    except agent.CallbackToolError as err:
        if timed_out:
            raise TimeoutError(f"no answer within {timeout_s:g} seconds") from err
        raise


async def run_gated(
    run_tool: Callable[..., Awaitable[Any]],
    tool: Callable[..., Awaitable[Any]],
    timeout_s: float,
    started: Callable[[], bool],
    *,
    call_id: str,
    **kwargs: Any,
) -> Any:
    """Run the gated `tool` through `run_tool`; while it waits for approval, deny it
    after `timeout_s` seconds with the harness's own Update (0: never). `started` says
    the tool's body is running, so the call was approved and the timer is over."""
    call = asyncio.ensure_future(run_tool(call_id, tool, **kwargs))
    if timeout_s > 0:
        try:
            await workflow.wait_condition(
                lambda: call.done() or started(), timeout=timedelta(seconds=timeout_s)
            )
        except TimeoutError:
            try:
                await workflow.execute_activity(
                    FLOW_ENTRY_TIMEOUT,
                    EntryTimeout(
                        workflow_id=workflow.info().workflow_id, call_id=call_id, kind="approval"
                    ),
                    result_type=bool,
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=TIMEOUT_RETRY,
                )
            except ActivityError as err:
                call.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await call
                raise TimeoutError(f"no decision within {timeout_s:g} seconds") from err
    return await call
