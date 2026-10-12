"""A flow run's grant at its gate (#2481, spec 2026-10-11-flow-grants-and-git-design.md
§3.3, plan Task A3): the `flow_grant_check` and `flow_grant_approve` activities on the
`projects` queue.

`flow_grant_check` is `match_grant` with the facts its bounds need, read through
ScadBuddy's own API at `SCADBUDDY_API_INTERNAL_URL` as the host functions read it: the
`projects` worker mounts no data volume. `flow_grant_approve` records an inside verdict
where a person's approval of the same call is recorded (`workflow_run_decisions`,
responder `grant:<id>`, with the call's input hash), then lets the parked call through
with the harness's public `tool_approval` Update, as `flow_entry_timeout` denies one.
"""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote

import httpx
from temporal_agent_harness.harness.agent_client import AgentClient, ToolApprovalError
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from scadbuddy.flows.approval_hash import input_hash
from scadbuddy.flows.grants import CallFacts, Inside, match_grant
from scadbuddy.flows.models import Decision, Run
from scadbuddy.flows.store import FlowStore
from scadbuddy.workflows.flow_models import (
    FLOW_GRANT_APPROVE,
    FLOW_GRANT_CHECK,
    GRANT_REFUSED,
    GrantApprove,
    GrantCheck,
    GrantVerdict,
)


def _segment(value: Any) -> str:
    text = str(value)
    if text in ("", ".", "..") or "/" in text or "\\" in text:
        raise ValueError(f"{text!r} is not an id")
    return quote(text, safe="")


def _quantity(options: Any) -> int | None:
    value = options.get("quantity") if isinstance(options, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class FlowGrants:
    """The activities over the run store, the API's client (`None`: no URL, and every
    lookup fails, so every call asks) and the approvals' hash key."""

    def __init__(
        self, store: FlowStore, client: httpx.AsyncClient | None, hash_key: bytes | None
    ) -> None:
        self._store = store
        self._client = client
        self._hash_key = hash_key

    async def _get(self, path: str) -> Any:
        if self._client is None:
            raise RuntimeError("SCADBUDDY_API_INTERNAL_URL is not set")
        response = await self._client.get(path)
        response.raise_for_status()
        return response.json()

    async def facts_of(self, fn: str, tool: str | None, args: dict[str, Any]) -> CallFacts:
        """What the call's bounds need. An agent tool's are its own arguments; a print's
        and an arrange's are read from the API. Grams are never known here: ScadBuddy
        uploads unsliced plates, so a filament bound does not match (amendment Ruling 4)."""
        if fn == "tool":
            return CallFacts(fn=fn, tool=tool, args=args)
        request: dict[str, Any] = args.get("request") or {}
        if fn == "arrange":
            models: set[str] = set()
            for obj in request.get("objects") or []:
                if obj.get("output_id") is not None:
                    output = await self._get(f"/api/v1/outputs/{_segment(obj['output_id'])}")
                    models.add(str(output["slug"]))
                else:
                    # A library file has no template: it never matches a models bound.
                    models.add(f"library:{obj.get('library_file_id')}")
            return CallFacts(fn=fn, args=args, models=sorted(models))
        if fn != "queue_print":
            raise ValueError(f"{fn} is not gated")
        source: dict[str, Any] = args.get("source") or {}
        options = await self._get("/api/v1/settings/print-options")
        printer = request.get("printer_id")
        if printer is None:
            printer = options.get("printer_id")
        all_plates = bool(request.get("all_plates"))
        plates: int | None
        if source.get("output_id") is not None:
            output = await self._get(f"/api/v1/outputs/{_segment(source['output_id'])}")
            models_of, library_file = [str(output["slug"])], None
            scope = str(output["slug"])
            plates = len(output.get("layout_plates") or []) or 1
        else:
            library_file = int(source["file_id"])
            models_of, scope = [], f"library:{library_file}"
            # A library file's plates are not counted here: an all-plates print of one
            # does not match a print bound.
            plates = None if all_plates else 1
        # As print_run.py resolves `copies`: the request's, else the most specific
        # remembered quantity (global, then the printer's, then the model's), else 1.
        copies = request.get("copies")
        if copies is None:
            copies = next(
                (
                    q
                    for q in (
                        _quantity(request.get("options")),
                        _quantity((options.get("models") or {}).get(scope)),
                        _quantity((options.get("printers") or {}).get(str(printer))),
                        _quantity(options.get("global_options")),
                    )
                    if q is not None
                ),
                1,
            )
        pins: dict[str, Any] = {}
        if request.get("printer_id") is None and printer is not None:
            pins["/request/printer_id"] = int(printer)
        if request.get("copies") is None:
            pins["/request/copies"] = int(copies)
        return CallFacts(
            fn=fn,
            args=args,
            pins=pins,
            printer_id=None if printer is None else int(printer),
            models=models_of,
            library_file=library_file,
            prints=None if plates is None else int(copies) * (plates if all_plates else 1),
        )

    def _hash(self, key: bytes) -> Callable[[str, str | None, dict[str, Any]], str]:
        def hash_of(fn: str, tool: str | None, args: dict[str, Any]) -> str:
            # As the agent will hash the call it runs (#2318): an agent tool's name and
            # input; a host function's name and arguments, for the record.
            return input_hash(key, tool if fn == "tool" and tool else fn, args)

        return hash_of

    @activity.defn(name=FLOW_GRANT_CHECK)
    async def check(self, req: GrantCheck) -> GrantVerdict:
        if self._hash_key is None:
            return GrantVerdict(verdict="outside", reason="no approval key")
        verdict = await match_grant(
            self._store,
            self.facts_of,
            self._hash(self._hash_key),
            run_id=req.run_id,
            request_id=req.request_id,
            fn=req.fn,
            tool=req.tool,
            args=req.args,
            session_id=req.session_id,
            flow_call_id=req.flow_call_id,
            narrowing=req.narrowing,
            now=datetime.now(UTC),
        )
        if isinstance(verdict, Inside):
            return GrantVerdict(
                verdict="inside",
                grant_id=verdict.grant_id,
                rule_index=verdict.rule_index,
                input_hash=verdict.input_hash,
                args=verdict.args,
            )
        return GrantVerdict(
            verdict="outside",
            reason=verdict.reason,
            timeout_s=verdict.timeout_s,
            parent_hop=verdict.parent_hop,
        )

    @activity.defn(name=FLOW_GRANT_APPROVE)
    async def approve(self, req: GrantApprove) -> bool:
        """Record the grant's approval, then send it. False when something else decided
        the call first (a person can't: it is never listed; a Reset can)."""
        request_id = f"flow:{req.run_id}:{req.workflow_run_id}:{req.call_id}"
        recorded = await self._store.record_decision(
            Decision(
                request_id=request_id,
                run_id=req.run_id,
                workflow_run_id=req.workflow_run_id,
                call_id=req.call_id,
                kind="approval",
                outcome="approved",
                responder=f"grant:{req.grant_id}",
                input_hash=req.input_hash,
            )
        )
        if not recorded:
            return False
        client = AgentClient(activity.client(), req.workflow_id)
        try:
            await client.approve_tool(req.call_id, approved=True, update_id=f"grant-{req.call_id}")
        except ToolApprovalError as err:
            if err.error_type == "UnknownToolApproval":
                # Not parked yet: the workflow sends this as it starts the call, and the
                # call parks in the same task. Retried.
                raise ApplicationError("the call is not parked yet") from None
            if err.error_type == "ToolApprovalAlreadyResolved":
                # Decided otherwise: no row says the grant approved a call it did not.
                await self._store.delete_decision(request_id)
                return False
            raise ApplicationError(
                f"the harness refused the grant's approval ({err.error_type})",
                type=GRANT_REFUSED,
                non_retryable=True,
            ) from None
        return True

    async def release(self, client: Client, run: Run) -> list[str]:
        """After a requested grant is approved: let through the run's parked flow-own
        calls it admits (the user's ruling: an agent-started run's outward calls wait
        until its grant, or each call, is approved). A parked call's input is fixed, so
        one whose defaults the check would pin (a print naming no printer or copies)
        still asks. Returns the call ids let through."""
        if self._hash_key is None:
            return []
        hash_of = self._hash(self._hash_key)
        handle = AgentClient(client, run.workflow_id)
        released: list[str] = []
        for parked in await handle.get_pending_approvals():
            call = _parked_call(parked.tool_name, parked.tool_input)
            if call is None:
                continue
            fn, tool, args = call

            async def unpinned(fn: str, tool: str | None, args: dict[str, Any]) -> CallFacts:
                facts = await self.facts_of(fn, tool, args)
                if facts.pins:
                    raise ValueError("a parked call's defaults cannot be pinned")
                return facts

            request_id = f"flow:{run.id}:{run.workflow_run_id}:{parked.tool_id}"
            verdict = await match_grant(
                self._store,
                unpinned,
                hash_of,
                run_id=run.id,
                request_id=request_id,
                fn=fn,
                tool=tool,
                args=args,
                now=datetime.now(UTC),
            )
            if not isinstance(verdict, Inside):
                continue
            if not await self._store.record_decision(
                Decision(
                    request_id=request_id,
                    run_id=run.id,
                    workflow_run_id=run.workflow_run_id,
                    call_id=parked.tool_id,
                    kind="approval",
                    outcome="approved",
                    responder=f"grant:{verdict.grant_id}",
                    input_hash=verdict.input_hash,
                )
            ):
                continue
            try:
                await handle.approve_tool(
                    parked.tool_id, approved=True, update_id=f"grant-{parked.tool_id}"
                )
            except ToolApprovalError:
                # Decided meanwhile (a person, its timer): not the grant's.
                await self._store.delete_decision(request_id)
                continue
            released.append(parked.tool_id)
        return released

    def all(self) -> list[Callable[..., Any]]:
        return [self.check, self.approve]


_PRINT_PATH = re.compile(r"^/api/v1/print/(outputs|library)/([^/]+)/run$")


def _parked_call(
    tool_name: str, tool_input: dict[str, Any]
) -> tuple[str, str | None, dict[str, Any]] | None:
    """A parked gated body's call as the grant sees it: `(fn, tool, args)` as the host
    function was given them (`flow_tools.queue_print`, `arrange`, `tool`). None for a
    plugin's or http_request's call, which no grant names."""
    request = tool_input.get("request")
    if tool_name == "approved_print" and isinstance(request, dict):
        found = _PRINT_PATH.match(str(tool_input.get("path", "")))
        if found is None:
            return None
        kind, ident = found.groups()
        source: dict[str, Any] = (
            {"output_id": unquote(ident)} if kind == "outputs" else {"file_id": int(ident)}
        )
        return "queue_print", None, {"source": source, "request": request}
    if tool_name == "approved_arrange" and isinstance(request, dict):
        return "arrange", None, {"request": request}
    args = tool_input.get("args")
    if tool_name == "approved_tool" and isinstance(args, dict):
        return "tool", str(tool_input.get("name")), args
    return None
