"""The flow script editor's routes (#2487): a type check that stores nothing, and what
the editor completes and shows on hover. Neither reads or writes a flow."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from scadbuddy.flows.agent_tools import AgentToolsUnavailableError
from scadbuddy.flows.component import FlowsDep
from scadbuddy.flows.host_interface import HostInterface, host_interface
from scadbuddy.flows.typecheck import ScriptProblem, check_script

router = APIRouter(tags=["flows"])

#: What the editor shows in place of the tools when the agent did not list them (#2704).
TOOLS_UNAVAILABLE = "ScadBuddy's assistant service did not say what its tools are."


class ScriptCheck(BaseModel):
    """A script to check, as a registration would, with the inputs schema it would be
    registered with (#2483), if any."""

    script: str
    inputs_schema: dict[str, Any] | None = None


class ScriptCheckResult(BaseModel):
    #: Empty when the script would register; each problem by 1-based line, or None for
    #: the whole script.
    problems: list[ScriptProblem]


@router.post(
    "/workflow-script/check",
    response_model=ScriptCheckResult,
    summary="Type-check a flow script without registering it",
)
async def check_flow_script(body: ScriptCheck) -> ScriptCheckResult:
    """The check `POST /workflows` runs before it registers a version, and nothing else:
    no version, no commit."""
    return ScriptCheckResult(problems=await check_script(body.script, body.inputs_schema))


@router.get(
    "/workflow-script/host-functions",
    response_model=HostInterface,
    summary="The host functions, result types and agent tools a flow script may use",
)
async def flow_host_functions(flows: FlowsDep) -> HostInterface:
    """Rendered from the same host functions the type check uses, and the agent's tools
    `tool(name, args)` may call, each at its tier as the agent resolves it now (#2704):
    Settings' raised tiers applied and disabled tools left out, as the grant routes check
    a grant's rules. When the agent does not answer, no tools, and `tools_unavailable`
    says why."""
    try:
        return host_interface(await flows.agent_tools.tool_tiers())
    except AgentToolsUnavailableError:
        return host_interface({}, tools_unavailable=TOOLS_UNAVAILABLE)
