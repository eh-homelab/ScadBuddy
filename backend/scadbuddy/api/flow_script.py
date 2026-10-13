"""The flow script editor's routes (#2487): a type check that stores nothing, and what
the editor completes and shows on hover. Neither reads or writes a flow."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from scadbuddy.flows.component import FlowsDep
from scadbuddy.flows.host_interface import HostInterface, host_interface
from scadbuddy.flows.manifest import raise_tiers, tiers
from scadbuddy.flows.typecheck import ScriptProblem, check_script

router = APIRouter(tags=["flows"])


class ScriptCheck(BaseModel):
    """A script to check, as a registration would."""

    script: str


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
    return ScriptCheckResult(problems=await check_script(body.script))


@router.get(
    "/workflow-script/host-functions",
    response_model=HostInterface,
    summary="The host functions, result types and manifest tools a flow script may use",
)
async def flow_host_functions(flows: FlowsDep) -> HostInterface:
    """Rendered from the same host functions the type check uses, and the tool manifest
    `tool(name, args)` reads, each tool at its tier as a run enforces it: Settings'
    raised tiers applied, as the grant routes check a grant's rules (#2481)."""
    return host_interface(raise_tiers(tiers(), await flows.store.tool_overrides()))
