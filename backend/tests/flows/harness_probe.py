"""A test-only harness agent shaped as ProjectWorkflow will be (plan
2026-10-09-durable-phase-6-flows.md, Task A3), with tools that record their effects.

No `from __future__ import annotations`: the harness reads tool and model annotations
at import (its own convention for tool modules).
"""

import asyncio
import json
import os
from datetime import timedelta

from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.contrib.workflow_streams import WorkflowStream
from temporalio.workflow import ActivityConfig

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_protocol import (
        AgentConfig,
        MidTurn,
        TextReply,
        ToolApprovalPolicy,
    )
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

#: The file each tool appends its effect to, one JSON object per line.
EFFECTS_ENV = "SCADBUDDY_TEST_PROBE_EFFECTS"
PROBE_WORKFLOW = "ProbeWorkflow"
PROBE_CHILD = "ProbeChild"
_CFG = ActivityConfig(start_to_close_timeout=timedelta(seconds=10))


def _effect(record: dict[str, object]) -> None:
    with open(os.environ[EFFECTS_ENV], "a") as f:
        f.write(json.dumps(record) + "\n")


class StepResult(BaseModel):
    """What a step did."""

    n: int
    note: str


class HumanAnswer(BaseModel):
    """A person's answer."""

    answer: str


@agent.activity_tool_defn(name="step", activity_config=_CFG, inherently_safe=True)
async def step(n: int) -> StepResult:
    """Do step `n` (records one effect)."""
    _effect({"tool": "step", "n": n})
    return StepResult(n=n, note=f"did {n}")


@agent.activity_tool_defn(name="outward", activity_config=_CFG)
async def outward(what: str) -> str:
    """An outward effect: needs a person's approval."""
    _effect({"tool": "outward", "what": what})
    return f"sent {what}"


@agent.callback_tool_defn(inherently_safe=True, timeout=timedelta(seconds=86_400))
async def human_answer(question: str) -> HumanAnswer:  # type: ignore[empty-body]
    """Wait for a person to answer `question`."""
    ...


@agent.tool_defn(inherently_safe=True)
async def wait(
    question: str, timeout_s: int, runner: agent.Injected[AgentWorkflowRunner]
) -> HumanAnswer:
    """Ask `question`; raise TimeoutError after `timeout_s` seconds with no answer."""
    result: HumanAnswer = await asyncio.wait_for(
        runner.run_tool(str(workflow.uuid4()), human_answer, question=question), timeout_s
    )
    return result


@agent.tool_defn(inherently_safe=True)
async def gated_via_nested(what: str, runner: agent.Injected[AgentWorkflowRunner]) -> str:
    """Run `outward` through a nested run_tool: its own gate applies."""
    result: str = await runner.run_tool(str(workflow.uuid4()), outward, what=what)
    return result


@workflow.defn(name=PROBE_CHILD)
class ProbeChild:
    @workflow.run
    async def run(self, n: int) -> int:
        await workflow.sleep(timedelta(seconds=8))
        return n * 10


@agent.tool_defn(inherently_safe=True)
async def child(n: int) -> int:
    """Run a child workflow and return its result. Its id carries the run id (Ruling 4)."""
    info = workflow.info()
    result: int = await workflow.execute_child_workflow(
        PROBE_CHILD,
        n,
        id=f"probe-child-{info.workflow_id}-{info.run_id}-{n}",
        result_type=int,
        parent_close_policy=workflow.ParentClosePolicy.ABANDON,
    )
    return result


#: Registered with the plugin; `human_answer` is not offered to the script.
PROBE_TOOLS = [step, outward, human_answer, wait, gated_via_nested, child]
SCRIPT_TOOLS = [step, outward, wait, gated_via_nested, child]


@activity.defn(name="probe_close")
async def probe_close(workflow_id: str) -> None:
    """The harness's public `close` Signal, sent to the run from an activity (Ruling 8)."""
    await activity.client().get_workflow_handle(workflow_id).signal("close")


class RunProbe(BaseModel):
    """A script to run once, and whether to close the workflow afterwards."""

    script: str
    close: bool = False


def run_probe_tool():  # type: ignore[no-untyped-def]
    return agent.code_mode_tool(SCRIPT_TOOLS, name="run_flow")


@agent.defn(name=PROBE_WORKFLOW)
class ProbeWorkflow:
    @agent.init
    def __init__(self, config: AgentConfig) -> None:
        self._runner = AgentWorkflowRunner(
            config,
            stream=WorkflowStream(),
            approval_policy_default=ToolApprovalPolicy.allow_inherently_safe(),
        )
        self._flow = agent.code_mode_tool(
            SCRIPT_TOOLS, name="run_flow", injections={"runner": self._runner}
        )

    @agent.accepts(mid_turn=MidTurn.REJECT)
    async def execute(self, message: RunProbe) -> TextReply:
        """Run a script, no model in the loop."""
        output: str = await self._runner.run_tool(
            str(workflow.uuid4()), self._flow, script=message.script
        )
        if message.close:
            await workflow.execute_activity(
                probe_close,
                workflow.info().workflow_id,
                start_to_close_timeout=timedelta(seconds=10),
            )
        return TextReply(text=output)
