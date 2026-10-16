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

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
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
    FLOW_DECISION,
    FLOW_GRANT_APPROVE,
    FLOW_GRANT_CHECK,
    FLOW_GRANT_WITHDRAW,
    FLOW_HOP_DUE,
    GRANT_REFUSED,
    DecisionAsk,
    DecisionRead,
    GrantApproval,
    GrantApprove,
    GrantCheck,
    GrantVerdict,
    HopRecord,
)

logger = logging.getLogger(__name__)


def _segment(value: Any) -> str:
    text = str(value)
    if text in ("", ".", "..") or "/" in text or "\\" in text:
        raise ValueError(f"{text!r} is not an id")
    return quote(text, safe="")


def _chosen_plates(request: dict[str, Any]) -> int:
    """How many distinct plates a print's `plate_ids` chose, 1 when it names none.

    Anything but a non-empty list of whole plate numbers is refused, so the call is
    outside its grant (a person decides it) rather than a TypeError. That is stricter
    than the print route, whose lax parsing also takes `["3"]` or `[true]`: such a call
    asks a person, never passes a grant on a count it could not read."""
    plate_ids = request.get("plate_ids")
    if plate_ids is None:
        return 1
    if (
        not isinstance(plate_ids, list)
        or not plate_ids
        or not all(isinstance(p, int) and not isinstance(p, bool) and p >= 1 for p in plate_ids)
    ):
        raise ValueError("plate_ids is not a list of plate numbers")
    return len(set(plate_ids))


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
        # Several chosen plates (#3652) each print, as every plate of an all-plates one.
        chosen = _chosen_plates(request)
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
            plates = None if all_plates else chosen
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
            prints=None if plates is None else int(copies) * (plates if all_plates else chosen),
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
        # The caller and the request id are the calling workflow's own, never the
        # payload's word for them: a flow run's checks are its calls
        # (`flow:<run>:<workflow run>:`), a session's its own (`durable:<session>:…`),
        # in a flow turn of a step the run recorded talking to that session.
        narrowing = None
        on_outside: Literal["escalate", "deny"] = "escalate"
        if req.session_id is None:
            own = _flow_request_prefix()
            if own is None or req.run_id != own[0] or not req.request_id.startswith(own[1]):
                return GrantVerdict(verdict="outside", reason="not this run's call")
        else:
            prefix = _session_request_prefix()
            if prefix is None or prefix[0] != req.session_id:
                return GrantVerdict(verdict="outside", reason="not this session's call")
            if not req.request_id.startswith(prefix[1]) or req.flow_call_id is None:
                return GrantVerdict(verdict="outside", reason="not this session's call")
            # The run is the one whose step talks to this very session, from its own
            # record; the payload's run id must agree with it. The step must be an
            # `agent`/`ask_session` call still running (its turn is the flow turn), and
            # its policy recorded: anything missing is no flow turn (fail closed).
            run = await self._store.run_talking_to(req.session_id, req.flow_call_id)
            sent = next(
                (s for s in (run.steps if run else []) if s.call_id == req.flow_call_id), None
            )
            if (
                run is None
                or run.id != req.run_id
                or sent is None
                or sent.session_id != req.session_id
                or sent.fn not in ("agent", "ask_session")
                or sent.status != "running"
                or sent.policy is None
            ):
                return GrantVerdict(verdict="outside", reason="not a flow turn of this run")
            # The script's narrowing and `on_outside`, as the run recorded the step
            # (spec §3.4, §4.3).
            narrowing = sent.policy.grant
            on_outside = sent.policy.on_outside
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
            narrowing=narrowing,
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
        # A parent hop ends after the run's timeout, or `approval_expiry_seconds` for a run
        # that never times out, so a starter session that never answers cannot hold the
        # person's queue back (spec §4.6, Ruling 11).
        hop_s = 0
        parent_session = parent_principal = None
        if verdict.parent_hop:
            hop_s = verdict.timeout_s or await self._store.approval_expiry_s()
            starter = await self._store.get_run(req.run_id)
            by = starter.started_by if starter is not None else {}
            parent_session, parent_principal = by.get("session"), by.get("principal")
            if not isinstance(parent_session, str) or not isinstance(parent_principal, str):
                # No starter session after all: the person's at once.
                hop_s, parent_session, parent_principal = 0, None, None
        return GrantVerdict(
            verdict="outside",
            reason=verdict.reason,
            timeout_s=verdict.timeout_s,
            parent_hop=hop_s > 0,
            hop_s=hop_s,
            on_outside=on_outside,
            parent_session=parent_session,
            parent_principal=parent_principal,
        )

    @activity.defn(name=FLOW_HOP_DUE)
    async def hop_due(self, call_id: str) -> bool:
        """The calling run's parked call waited its parent hop out: it passes up to the
        person (a parent that never answers never denies; spec §4.6). False when it had
        moved already (passed up, or decided)."""
        own = _flow_request_prefix()
        if own is None:
            return False
        record = HopRecord(hop="parent", action="timeout", actor="system", at=datetime.now(UTC))
        return await self._store.move_hop(own[0], call_id, record) is not None

    @activity.defn(name=FLOW_DECISION)
    async def decision(self, ask: DecisionAsk) -> DecisionRead:
        """What the calling run's approved call runs (#2482, plan Task B4): a narrowed
        decision's input, only when it hashes to the hash the decision approved (#2318).
        The decision is the calling workflow's own call's, never one named here."""
        own = _flow_request_prefix()
        if own is None:
            return DecisionRead()
        found = await self._store.get_decision(f"{own[1]}{ask.call_id}")
        if found is None or found.outcome != "approved" or "args" not in found.response:
            return DecisionRead()
        args = found.response.get("args")
        by = found.response.get("narrowed_by")
        narrowed_by = by if isinstance(by, str) else None
        if (
            not isinstance(args, dict)
            or self._hash_key is None
            or found.input_hash is None
            or self._hash(self._hash_key)(ask.fn, ask.tool, args) != found.input_hash
        ):
            return DecisionRead(narrowed=True, narrowed_by=narrowed_by)
        return DecisionRead(narrowed=True, args=args, narrowed_by=narrowed_by)

    @activity.defn(name=FLOW_GRANT_APPROVE)
    async def approve(self, req: GrantApprove) -> GrantApproval:
        """Let the calling run's parked call through on its grant's say. Nothing of the
        verdict is taken from the payload, which names only the call: the call's tool and
        input are read from the harness as it is parked, hashed here, and approved only
        when the grant's recorded use of this request id is for that very hash and the
        grant is in force, in one statement (`record_grant_decision`). "ask" when it is
        not, and the workflow lists the call for a person; "resolved" when something else
        decided it first."""
        own = _flow_request_prefix()
        if own is None or self._hash_key is None:
            return "ask"
        run_id, prefix = own
        info = activity.info()
        request_id = f"{prefix}{req.call_id}"
        client = AgentClient(activity.client(), info.workflow_id)
        parked = next(
            (c for c in await client.get_pending_approvals() if c.tool_id == req.call_id), None
        )
        if parked is None:
            # Not parked yet (the workflow starts the call as it schedules this), or
            # decided already: retried, and the workflow asks once the retries are spent.
            raise ApplicationError("the call is not parked")
        call = parked_call(parked.tool_name, parked.tool_input)
        use = await self._store.grant_use(request_id)
        if call is None or use is None or (call[0], call[1]) != (use.fn, use.tool):
            return "ask"
        hashed = self._hash(self._hash_key)(*call)
        recorded = await self._store.record_grant_decision(
            Decision(
                request_id=request_id,
                run_id=run_id,
                workflow_run_id=info.workflow_run_id or "",
                call_id=req.call_id,
                kind="approval",
                outcome="approved",
                responder=f"grant:{use.grant_id}",
                input_hash=hashed,
            )
        )
        if not recorded:
            return "ask"
        try:
            await client.approve_tool(req.call_id, approved=True, update_id=f"grant-{req.call_id}")
        except ToolApprovalError as err:
            # Decided otherwise: no row says the grant approved a call it did not.
            await self._store.withdraw_grant_decision(request_id)
            if err.error_type == "ToolApprovalAlreadyResolved":
                return "resolved"
            raise ApplicationError(
                f"the harness refused the grant's approval ({err.error_type})",
                type=GRANT_REFUSED,
                non_retryable=True,
            ) from None
        except BaseException:
            # The Update may not have arrived: a retry records the row again, and once
            # the retries are spent the call is listed for a person, who must be able to
            # decide it (flow_decide answers stale to a `grant:` row). Had it arrived,
            # the agent finds no approval and refuses the call: closed, not open.
            await self._store.withdraw_grant_decision(request_id)
            raise
        return "approved"

    @activity.defn(name=FLOW_GRANT_WITHDRAW)
    async def withdraw(self, req: GrantApprove) -> None:
        """Before the workflow lists a call whose grant approval never completed (the
        retries ran out, or a worker died with the row written): remove the grant's own
        decision for it, never a person's, so a person can decide it. The request id is
        the calling workflow's own, as in `approve`."""
        own = _flow_request_prefix()
        if own is None:
            return
        _, prefix = own
        await self._store.withdraw_grant_decision(f"{prefix}{req.call_id}")

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
            call = parked_call(parked.tool_name, parked.tool_input)
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
            if not await self._store.record_grant_decision(
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
                await self._store.withdraw_grant_decision(request_id)
                continue
            except Exception:
                # The Update may not have arrived; the call stays listed for a person,
                # who must be able to decide it (see `approve`).
                logger.warning("could not release a parked call into its grant", exc_info=True)
                await self._store.withdraw_grant_decision(request_id)
                continue
            released.append(parked.tool_id)
        return released

    def all(self) -> list[Callable[..., Any]]:
        return [self.check, self.approve, self.withdraw, self.hop_due, self.decision]


_PRINT_PATH = re.compile(r"^/api/v1/print/(outputs|library)/([^/]+)/run$")
_FLOW_WORKFLOW = re.compile(r"^flow-([0-9a-f-]{36})$")
_SESSION_WORKFLOW = re.compile(r"^session-([0-9a-f-]{36})$")


def _session_request_prefix() -> tuple[str, str] | None:
    """The calling durable session's id and its calls' request id prefix
    (`durable:<session>:<workflow run>:`), from the activity's own workflow; None when
    the caller is no durable session."""
    info = activity.info()
    found = _SESSION_WORKFLOW.match(info.workflow_id or "")
    if found is None:
        return None
    session_id = found.group(1)
    return session_id, f"durable:{session_id}:{info.workflow_run_id}:"


def _flow_request_prefix() -> tuple[str, str] | None:
    """The calling flow run's id and its calls' request id prefix, from the activity's
    own workflow; None when the caller is no flow run."""
    info = activity.info()
    found = _FLOW_WORKFLOW.match(info.workflow_id or "")
    if found is None:
        return None
    run_id = found.group(1)
    return run_id, f"flow:{run_id}:{info.workflow_run_id}:"


def parked_call(
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
