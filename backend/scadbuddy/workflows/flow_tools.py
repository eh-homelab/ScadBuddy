"""ScadBuddy's flow host functions (spec 2026-10-01 §7.1), as harness tools.

Every function records its step on the run's row (plan 2026-10-09 Ruling 7). No
``from __future__ import annotations``: the harness reads these signatures to type-check
scripts and to coerce their arguments.
"""

from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal_agent_harness.harness import agent
    from temporal_agent_harness.harness.agent_workflow import AgentWorkflowRunner

    from scadbuddy.workflows.flow_entries import run_callback
    from scadbuddy.workflows.flow_steps import step

MIN_WAIT_S = 10
MAX_WAIT_S = 86_400


class HumanAnswer(BaseModel):
    """A person's answer on the Workflows page."""

    answer: str


@agent.callback_tool_defn(inherently_safe=True, timeout=timedelta(seconds=MAX_WAIT_S))
async def human_answer(question: str) -> HumanAnswer:  # type: ignore[empty-body]
    """Wait for a person to answer `question` on the Workflows page."""
    ...


@agent.tool_defn(inherently_safe=True)
async def wait_for_human(
    question: str, timeout_s: int, runner: agent.Injected[AgentWorkflowRunner]
) -> HumanAnswer:
    """Ask a person `question` and wait up to `timeout_s` seconds (10 to 86400) for the
    answer. Raises TimeoutError if nobody answers in time."""
    # timeout_s has no default: the pinned harness's stubs render every parameter as
    # required (code_mode/stubs.py at 04a49d1), so a default would never be used.
    if not MIN_WAIT_S <= timeout_s <= MAX_WAIT_S:
        raise ValueError(f"timeout_s must be {MIN_WAIT_S} to {MAX_WAIT_S} seconds")
    call_id = str(workflow.uuid4())
    async with step("wait_for_human", call_id, outward=False, waiting="answer", prompt=question):
        answer: HumanAnswer = await run_callback(
            runner.run_tool, human_answer, timeout_s, call_id=call_id, question=question
        )
        return answer


@agent.tool_defn(inherently_safe=True)
async def sleep(seconds: float) -> None:
    """Wait `seconds` seconds, durably (a Temporal timer)."""
    async with step("sleep", str(workflow.uuid4()), outward=False):
        await workflow.sleep(timedelta(seconds=seconds))


#: Registered with the harness plugin. `human_answer` is the callback behind
#: `wait_for_human`, never offered to a script.
FLOW_TOOLS: list[Any] = [sleep, wait_for_human, human_answer]
SCRIPT_TOOLS: list[Any] = [t for t in FLOW_TOOLS if t is not human_answer]


def run_flow_tool(runner: AgentWorkflowRunner | None = None) -> Any:
    """The Code Mode tool over the host functions a script may call. Outside a workflow
    (the type check) no runner is injected."""
    injections = {"runner": runner} if runner is not None else None
    return agent.code_mode_tool(SCRIPT_TOOLS, name="run_flow", injections=injections)
