"""Starting a flow run's execution: the one start the run route and a trigger's fire
(#2484, spec 2026-10-11 §12.2) both make.

A run is started by update-with-start through the harness's own client (spec 2026-10-01
§7.3); its row is written moments later by the run's first activity.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporal_agent_harness.harness.agent_client import AgentClient
from temporal_agent_harness.harness.agent_protocol import AgentConfig
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.flows.component import Flows
from scadbuddy.flows.models import Definition
from scadbuddy.workflows.flow_tools import HOST_FUNCTION_NAMES
from scadbuddy.workflows.project import PROJECT_WORKFLOW, FlowStart, RunFlow

_STATUS_SECONDS = 2.0


async def execution_status(client: Client, run_id: str) -> WorkflowExecutionStatus | None:
    """The run's execution's status; None when there is none."""
    try:
        described = await client.get_workflow_handle(f"flow-{run_id}").describe(
            rpc_timeout=timedelta(seconds=_STATUS_SECONDS)
        )
    except RPCError as err:
        if err.status == RPCStatusCode.NOT_FOUND:
            return None
        raise
    return described.status


def approval_timeout(flows: Flows, definition: Definition, override: int | None) -> int:
    """The run's approval timeout: its own, else its flow's, else the global setting."""
    return next(
        value
        for value in (
            override,
            definition.approval_timeout_s,
            flows.settings.flow_approval_timeout_seconds,
        )
        if value is not None
    )


async def start_execution(
    flows: Flows,
    client: Client,
    *,
    definition: Definition,
    run_id: str,
    update_id: str,
    started_by: dict[str, Any],
    inputs: dict[str, Any] | None,
    approval_timeout_s: int,
    tool_tiers: dict[str, str],
    event: dict[str, Any] | None = None,
) -> None:
    """Start run `run_id` of `definition` with its checked `inputs`, as `started_by`;
    `event` is the trigger's event it fired on, if any (#2666)."""
    await AgentClient(client, f"flow-{run_id}").start_and_submit_message(
        "execute",
        RunFlow(
            script=definition.script,
            inputs_schema=definition.inputs_schema,
            inputs=inputs if definition.inputs_schema is not None else None,
            event=event,
        ).model_dump(exclude_none=True),
        workflow_name=PROJECT_WORKFLOW,
        task_queue=flows.queue,
        start_config=AgentConfig(),
        start_data=FlowStart(
            run_id=run_id,
            definition_id=definition.id,
            version=definition.version,
            name=definition.name,
            started_by=started_by,
            approval_timeout_s=approval_timeout_s,
            tools_queue=flows.settings.temporal_task_queue_agent_tools,
            tool_tiers=tool_tiers,
            search_attributes=flows.search_attributes,
            commit=definition.commit or "",
            host_functions=sorted(HOST_FUNCTION_NAMES),
        ),
        update_id=update_id,
    )
