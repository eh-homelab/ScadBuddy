"""ProjectWorkflow: one run of a flow (spec 2026-10-01 §7.2, plan
2026-10-09-durable-phase-6-flows.md Task B3). A model-free temporal-agent-harness agent
in the shape of the harness's agent_dag `DagBuilderAgent.execute`: its one handler runs
the script through the Code Mode tool over ScadBuddy's host functions.

It registers no Update, Query or Signal of its own: answers, approvals and status are the
harness's (`provide_callback_result`, `tool_approval`, `agent_status`), and `close` ends
it. Unversioned (Ruling 13): a change must replay
`tests/fixtures/project_workflow_histories/`, or go behind `workflow.patched()`.
"""

from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel
from temporalio import workflow
from temporalio.common import SearchAttributeKey, SearchAttributeUpdate
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        MidTurn,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from scadbuddy.flows.models import RESULT_MAX
    from scadbuddy.workflows.flow_activities import FLOW_CLOSE, FLOW_RECORD
    from scadbuddy.workflows.flow_models import FlowRecord, ProjectionWrite
    from scadbuddy.workflows.flow_steps import project
    from scadbuddy.workflows.flow_tools import run_flow_tool

PROJECT_WORKFLOW = "ProjectWorkflow"
KIND = SearchAttributeKey.for_keyword("ScadbuddyKind")
SUBJECT = SearchAttributeKey.for_keyword("ScadbuddySubject")
STATUS = SearchAttributeKey.for_keyword("ScadbuddyStatus")
_ACTIVITY = timedelta(seconds=8)


class RunFlow(BaseModel):
    """A flow script to run once, with its inputs (#2483). They are in the run's history
    (sealed, as every payload of the run is), so a Reset replays the same values."""

    script: str
    #: The flow's inputs schema, which types `inputs()`; None for a flow with none.
    inputs_schema: dict[str, Any] | None = None
    #: The run's inputs, checked against the schema and its defaults filled in.
    inputs: dict[str, Any] | None = None
    #: The event a trigger fired this run on, as the fire recorded it (#2666): what
    #: `trigger()` returns. Data only; None for a run started by hand or on a schedule.
    event: dict[str, Any] | None = None


class FlowStart(BaseModel):
    """Who and what this run is; fixed for the execution."""

    run_id: str
    definition_id: str
    version: int
    name: str
    started_by: dict[str, Any]
    #: Seconds an outward call waits for a decision; 0 is never.
    approval_timeout_s: int = 0
    #: Where the agent serves the tools `tool(...)` calls.
    tools_queue: str = "agent-tools"
    #: Seconds a turn tool's call waits for a worker on one agent replica's own queue
    #: before it fails as that replica gone (#2937). Tests shorten it.
    host_queue_start_s: int = 60
    #: The same wait for a call a person already approved: longer, since failing it asks
    #: them to approve it again (#3420). Tests shorten it.
    approved_queue_start_s: int = 600
    #: The agent's tools and their tiers as the agent resolved them when the run started
    #: (#2704, `flows/agent_tools.py`); each call asks the agent again, whose answer only
    #: raises them. A run from before #2704 took them from a file and asks nothing.
    tool_tiers: dict[str, str] = {}
    search_attributes: bool = False
    #: The commit of the version it runs, in `data/flows` (#2503); "" from a run started
    #: before flows were in git, so recorded histories decode unchanged.
    commit: str = ""


@agent.defn(name=PROJECT_WORKFLOW)
class ProjectWorkflow:
    @agent.init
    def __init__(self, config: AgentConfig, data: FlowStart) -> None:
        self._start = data
        self._executed = False
        self._seq = 0
        self._started: set[str] = set()
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._flow = run_flow_tool(self._runner)

    @property
    def run_id(self) -> str:
        """The flow run's id (not Temporal's run id)."""
        return self._start.run_id

    def next_seq(self) -> int:
        """The next host call's step number, from 1."""
        self._seq += 1
        return self._seq

    @property
    def approval_timeout_s(self) -> int:
        """Seconds a gated call waits for a decision before it is denied; 0 is never."""
        return self._start.approval_timeout_s

    @property
    def tools_queue(self) -> str:
        return self._start.tools_queue

    @property
    def host_queue_start_s(self) -> int:
        return self._start.host_queue_start_s

    @property
    def approved_queue_start_s(self) -> int:
        return self._start.approved_queue_start_s

    def tool_tier(self, name: str) -> str | None:
        return self._start.tool_tiers.get(name)

    def mark_started(self, call_id: str) -> None:
        """A gated call was approved and its body runs: its timer is over."""
        self._started.add(call_id)

    def started(self, call_id: str) -> bool:
        return call_id in self._started

    @agent.accepts(mid_turn=MidTurn.REJECT)
    async def execute(self, message: RunFlow) -> TextReply:
        """Run the flow's script, no model in the loop, then close the run."""
        if self._executed:
            # One script per run (Ruling 3): a second `execute` under another update id.
            raise ApplicationError(
                "this run has a script already", type="AlreadyExecuted", non_retryable=True
            )
        self._executed = True
        if message.inputs_schema is not None or message.event is not None:
            self._flow = run_flow_tool(
                self._runner, message.inputs_schema, message.inputs, message.event
            )
        info = workflow.info()
        start = self._start
        await workflow.execute_activity(
            FLOW_RECORD,
            FlowRecord(
                run_id=start.run_id,
                definition_id=start.definition_id,
                version=start.version,
                name=start.name,
                workflow_id=info.workflow_id,
                workflow_run_id=info.run_id,
                started_by=start.started_by,
                approval_timeout_s=start.approval_timeout_s,
                commit=start.commit,
                inputs=message.inputs if message.inputs_schema is not None else None,
            ),
            start_to_close_timeout=_ACTIVITY,
        )
        self._upsert("running")
        output: str = await self._runner.run_tool(
            str(workflow.uuid4()), self._flow, script=message.script
        )
        status: Literal["succeeded", "failed"] = (
            "failed" if output.startswith("Script error") else "succeeded"
        )
        await project(
            ProjectionWrite(
                run_id=start.run_id,
                workflow_run_id=workflow.info().run_id,
                status=status,
                result=output,
            )
        )
        self._upsert(status)
        await workflow.execute_activity(
            FLOW_CLOSE, info.workflow_id, start_to_close_timeout=_ACTIVITY
        )
        return TextReply(text=output[:RESULT_MAX])

    def _upsert(self, status: str) -> None:
        if not self._start.search_attributes:
            return
        updates: list[SearchAttributeUpdate[Any]] = [
            KIND.value_set("flow"),
            SUBJECT.value_set(self._start.definition_id),
            STATUS.value_set(status),
        ]
        workflow.upsert_search_attributes(updates)
