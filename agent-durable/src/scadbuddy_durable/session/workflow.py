"""DurableSession: one assistant session as a Temporal workflow (spec 2026-10-01 §6, §8).

``session-<ai_sessions.id>`` on the ``agent`` queue. It runs the plugin's
``DurableClaudeAgent`` with the agent's tools (``session.tools``), one turn per
``send_message``, and continues as new between turns when the server suggests it
(and, the plugin's ``auto_continue_as_new``, between the steps of a long turn).

The gate (§6.6, 5b): when the plugin's ``pending_approvals()`` gains a call, the call
is described (an approval's summary and hash come from the agent, ``gate.describe_call``;
an answer's card is built and checked here, ``gate.entry``), opened (``open_input``),
and timed. It ends by the ``respond`` Update, by its timer, by ``cancel_input``
(handoff) or by the ``interrupt`` Signal (which also stops the turn), each through
``resolve_input`` before the plugin is told (``decide``). A timer never approves and
never answers: approval → expired, question → cancelled, attention → timed out.

The workflow holds no credential (the segment runner opens it), and the answer
tools' input is checked before any of it is kept (Ruling 9).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError, FailureError

with workflow.unsafe.imports_passed_through():
    from temporalio.claude_agent_sdk import AgentState, DurableClaudeAgent

    from scadbuddy_durable.gate.activities import RESOLVE_ON_RESPOND, RESOLVE_ON_TIMER
    from scadbuddy_durable.gate.entry import (
        ASK_USER,
        DEFAULT_TIMEOUT_S,
        MalformedAnswerInputError,
        build_entry,
    )
    from scadbuddy_durable.gate.ids import durable_request_id
    from scadbuddy_durable.gate.names import (
        CANCEL_INPUT_UPDATE,
        INTERRUPT_SIGNAL,
        PENDING_INPUT_QUERY,
        RESPOND_UPDATE,
        refused_failure_type,
    )
    from scadbuddy_durable.gate.store import SYSTEM, OpenInput, Outcome, ResolveInput
    from scadbuddy_durable.gate.validate import (
        GateEntry,
        Principal,
        RespondRefused,
        RespondRequest,
        validate_respond,
    )
    from scadbuddy_durable.session import tools as manifest
    from scadbuddy_durable.session.models import (
        BUDGET_EXHAUSTED,
        MAX_SEGMENTS,
        SEGMENT_CONTEXT_QUERY,
        SEND_MESSAGE_UPDATE,
        TOOLS_QUEUE,
        WORKFLOW,
        FinishTurn,
        FollowArgs,
        GateSettings,
        Message,
        SegmentContext,
        SendAnswer,
        SessionStart,
        TurnOutcome,
        TurnStart,
    )

DESCRIBE_CALL = "gate.describe_call"
FOLLOW_TIMEOUT = timedelta(days=30)
FOLLOW_HEARTBEAT = timedelta(seconds=30)
FOLLOW_DRAIN = timedelta(seconds=30)
SHORT = timedelta(seconds=30)
IMAGES_NOTE = (
    "\n\n[The user attached {n} image{s} to this message. Call the view_user_images tool"
    " to see {them} before you answer.]"
)
TURN_ENDED = "the turn ended"
INTERRUPTED = "the turn was interrupted"
NOBODY_ANSWERED = "nobody answered in time"


def prompt_of(message: Message) -> str:
    n = len(message.images)
    if not n:
        return message.text
    note = IMAGES_NOTE.format(n=n, s="" if n == 1 else "s", them="it" if n == 1 else "them")
    return message.text + note


@dataclass
class Parked:
    """One call parked at the gate: what the validator reads and the Query lists."""

    call_id: str
    entry: GateEntry
    tool: str
    summary: str
    prompt: str
    requested_by: dict[str, str]
    created_at: datetime
    expires_at: datetime
    attention: dict[str, Any] | None

    @property
    def kind(self) -> Literal["approval", "answer"]:
        return self.entry.kind

    def set_state(self, state: Literal["pending", "resolving", "resolved"]) -> None:
        self.entry = dataclasses.replace(self.entry, state=state)


def _iso(at: datetime) -> str:
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _owner(o: Any) -> dict[str, str]:
    return {"kind": o.kind, "id": o.id, "label": o.label}


def _refused(code: str, message: str) -> ApplicationError:
    return ApplicationError(message, type=refused_failure_type(code), non_retryable=True)


@workflow.defn(name=WORKFLOW)
class DurableSession:
    @workflow.init
    def __init__(self, start: SessionStart) -> None:
        self._start = start
        entries = manifest.manifest()
        self._kinds = {e.name: e.hitl for e in entries}
        self._turn: TurnStart | None = start.turn
        self._message: Message | None = None
        self._parked: dict[str, Parked] = {}
        self._seen: set[str] = set()
        self._parks: list[asyncio.Task[None]] = []
        self._task: asyncio.Task[str] | None = None
        # From a message's acceptance until its turn's finish_turn has run: the plugin's
        # run() returns before the gate is drained and the turn is written (review of #1958).
        self._in_turn = start.turn is not None
        self._interrupted: str | None = None
        self._settings = GateSettings(approval_expiry_s=600, question_expiry_s=3600)
        self._agent = DurableClaudeAgent(
            tools=manifest.durable_tools(entries),
            max_turns=start.max_turns,
            builtin_tools=["Skill"],
            tool_activities=(),
            max_segments=MAX_SEGMENTS,
            state=start.agent,
            auto_continue_as_new=True,
            continue_as_new_args=self._next_run,
            live_output=True,
        )

    def _next_run(self, state: AgentState) -> list[Any]:
        return [dataclasses.replace(self._start, agent=state, turn=self._turn)]

    @workflow.run
    async def run(self, start: SessionStart) -> None:
        while True:
            if self._agent.busy:
                # A turn handed over by continue-as-new: the prompt was sent already.
                await self._run_turn(None)
            else:
                await workflow.wait_condition(lambda: self._message is not None)
                message, self._message = self._message, None
                assert message is not None
                self._turn = TurnStart(message.turn_id, message.author, list(message.images))
                await self._run_turn(prompt_of(message))
            self._turn = None
            self._in_turn = False
            if self._message is None and self._agent.should_continue_as_new():
                # Refuses sends from here: one accepted now would not be carried over.
                self._in_turn = True
                await self._agent.continue_as_new()

    # ---- a turn ----
    async def _run_turn(self, prompt: str | None) -> None:
        sid = self._start.session_id
        turn_id = self._turn.turn_id if self._turn else ""
        self._interrupted = None
        self._settings = await workflow.execute_activity(
            "gate_settings", start_to_close_timeout=SHORT, result_type=GateSettings
        )
        follow = workflow.start_activity(
            "follow_session",
            FollowArgs(session_id=sid, workflow_id=workflow.info().workflow_id),
            start_to_close_timeout=FOLLOW_TIMEOUT,
            heartbeat_timeout=FOLLOW_HEARTBEAT,
            retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=10)),
        )
        watcher = asyncio.create_task(self._watch_gate())
        outcome: TurnOutcome = "done"
        message: str | None = None
        self._task = asyncio.create_task(self._agent.run(prompt))
        try:
            await self._task
        except asyncio.CancelledError:
            if self._interrupted is None:
                raise  # the workflow itself is cancelled
            outcome = "interrupted"
        except FailureError as err:
            text = f"{err}: {err.cause}" if err.cause else str(err)
            if self._interrupted is not None:
                outcome = "interrupted"
            elif BUDGET_EXHAUSTED in text or "max_budget_usd" in text:
                outcome = "budget_exhausted"
            else:
                outcome, message = "failed", text
        finally:
            self._task = None
        watcher.cancel()
        reason = self._interrupted or TURN_ENDED
        await self._cancel_parked(reason)
        for park in self._parks:
            park.cancel()
        self._parks = []
        self._seen = set()
        try:
            await workflow.wait_condition(follow.done, timeout=FOLLOW_DRAIN)
        except TimeoutError:
            follow.cancel()
        await workflow.execute_activity(
            "finish_turn",
            FinishTurn(session_id=sid, turn_id=turn_id, outcome=outcome, message=message),
            start_to_close_timeout=SHORT,
        )

    # ---- the gate ----
    async def _watch_gate(self) -> None:
        while True:
            await workflow.wait_condition(
                lambda: any(c["id"] not in self._seen for c in self._agent.pending_approvals())
            )
            for call in self._agent.pending_approvals():
                if call["id"] not in self._seen:
                    self._seen.add(call["id"])
                    self._parks.append(asyncio.create_task(self._park_or_refuse(call)))

    async def _park_or_refuse(self, call: dict[str, Any]) -> None:
        """A call that could not be described or opened is refused to the model, never
        left undecided: the turn would wait on it until an interrupt (review of #1958)."""
        try:
            await self._park(call)
        except asyncio.CancelledError:
            raise
        except FailureError:
            if any(p.call_id == str(call["id"]) for p in self._parked.values()):
                raise  # parked, and failed resolving: its entry is the gate's to end
            with contextlib.suppress(ValueError):
                self._agent.decide(str(call["id"]), False, approver="scadbuddy")

    async def _park(self, call: dict[str, Any]) -> None:
        sid = self._start.session_id
        name = str(call["name"])
        rid = durable_request_id(sid, workflow.info().run_id, str(call["id"]))
        kind: Literal["approval", "answer"] = (
            "answer" if self._kinds.get(name) == "answer" else "approval"
        )
        author = self._turn.author if self._turn else self._start.owner
        summary, input_hash = "", None
        if kind == "approval":
            described = await workflow.execute_activity(
                DESCRIBE_CALL,
                {"tool": name, "input": call["input"]},
                task_queue=TOOLS_QUEUE,
                start_to_close_timeout=SHORT,
                retry_policy=RetryPolicy(
                    maximum_attempts=5, non_retryable_error_types=["UnknownSession", "ToolError"]
                ),
                result_type=dict,
            )
            summary, input_hash = str(described["summary"]), str(described["input_hash"])
        try:
            built = build_entry(
                request_id=rid,
                kind=kind,
                tool=name,
                tool_input=call["input"],
                input_hash=input_hash,
                requested_by=Principal(author.kind, author.id),
                session_owner=Principal(self._start.owner.kind, self._start.owner.id),
                session_creator=Principal(self._start.creator.kind, self._start.creator.id),
            )
        except MalformedAnswerInputError:
            # Refused to the model, never parked (Ruling 9); the error is not kept.
            self._agent.decide(str(call["id"]), False, approver="scadbuddy")
            return
        if kind == "approval":
            timeout = self._settings.approval_expiry_s
        elif name == ASK_USER:
            timeout = self._settings.question_expiry_s
        else:
            timeout = built.timeout_s or DEFAULT_TIMEOUT_S
        now = workflow.now()
        parked = Parked(
            call_id=str(call["id"]),
            entry=built.entry,
            tool=name,
            summary=summary,
            prompt=built.prompt,
            requested_by=_owner(author),
            created_at=now,
            expires_at=now + timedelta(seconds=timeout),
            attention=built.attention,
        )
        await workflow.execute_activity(
            "open_input",
            OpenInput(
                request_id=rid,
                session_id=sid,
                workflow_id=workflow.info().workflow_id,
                workflow_run_id=workflow.info().run_id,
                kind=kind,
                tool=name,
                tool_use_id=parked.call_id,
                summary=summary,
                input_hash=input_hash,
                prompt=built.prompt,
                requested_by=parked.requested_by,
                responders=["browser", "grant"] if kind == "approval" else ["browser"],
                expires_at=_iso(parked.expires_at),
                questions=built.questions,
                attention=built.attention,
            ),
            start_to_close_timeout=SHORT,
        )
        # Answerable only once its row is there, so no response races the open.
        self._parked[rid] = parked
        try:
            await workflow.wait_condition(
                lambda: parked.entry.state != "pending", timeout=timedelta(seconds=timeout)
            )
        except TimeoutError:
            if parked.entry.state != "pending":
                return
            if kind == "approval":
                await self._resolve(parked, "expired", SYSTEM, None, None, timer=True)
            elif name == ASK_USER:
                await self._resolve(parked, "cancelled", SYSTEM, None, NOBODY_ANSWERED, timer=True)
            else:
                await self._resolve(parked, "timed_out", SYSTEM, None, None, timer=True)

    async def _resolve(
        self,
        parked: Parked,
        outcome: Outcome,
        responder: dict[str, str],
        response: Any | None,
        reason: str | None,
        *,
        timer: bool = False,
    ) -> bool:
        parked.set_state("resolving")
        try:
            written: bool = await workflow.execute_activity(
                "resolve_input",
                ResolveInput(
                    request_id=parked.entry.id,
                    outcome=outcome,
                    responder=responder,
                    response=response,
                    reason=reason,
                ),
                **(RESOLVE_ON_TIMER if timer else RESOLVE_ON_RESPOND),
            )
        except BaseException:
            parked.set_state("pending")
            raise
        parked.set_state("resolved")
        self._parked.pop(parked.entry.id, None)
        # An approval runs only with its approved row (Ruling 10); an answer's call reads
        # whatever was recorded, so it is let through to report it.
        go = parked.kind == "answer" or (written and outcome == "approved")
        self._agent.decide(parked.call_id, go, approver=responder.get("id"))
        return written

    async def _cancel_parked(self, reason: str) -> bool:
        pending = [p for p in self._parked.values() if p.entry.state == "pending"]
        for parked in pending:
            await self._resolve(parked, "cancelled", SYSTEM, None, reason)
        return bool(pending)

    # ---- handlers ----
    @workflow.update(name=SEND_MESSAGE_UPDATE)
    async def send_message(self, message: Message) -> SendAnswer:
        self._message = message
        self._in_turn = True
        return SendAnswer(accepted=True, turn_id=message.turn_id)

    @send_message.validator
    def _validate_send(self, message: Message) -> None:
        if self._in_turn or self._message is not None or self._task is not None or self._agent.busy:
            raise ApplicationError("the session is running a turn", type="busy", non_retryable=True)

    def _request(self, args: dict[str, Any]) -> RespondRequest:
        responder = args.get("responder") or {}
        return RespondRequest(
            request_id=str(args.get("request_id", "")),
            response=args.get("response"),
            responder=Principal(str(responder.get("kind", "")), str(responder.get("id", ""))),
            role=args.get("role", ""),
        )

    @workflow.update(name=RESPOND_UPDATE)
    async def respond(self, args: dict[str, Any]) -> dict[str, str]:
        request = self._request(args)
        parked = self._parked.get(request.request_id)
        if parked is None:
            raise _refused("stale", f"no pending input {request.request_id}")
        try:
            # The validator passed, but another handler may have moved the entry since.
            valid = validate_respond(parked.entry, request)
        except RespondRefused as refused:
            raise _refused(refused.code, str(refused)) from None
        responder = args.get("responder") or {}
        who = {
            "kind": str(responder.get("kind", "")),
            "id": str(responder.get("id", "")),
            "label": str(responder.get("label", "")),
        }
        if parked.kind == "approval":
            outcome: Outcome = "approved" if valid.decision == "approve" else "denied"
            await self._resolve(parked, outcome, who, {"decision": valid.decision}, None)
        else:
            outcome = "answered"
            await self._resolve(parked, outcome, who, {"answers": list(valid.answers)}, None)
        return {"kind": parked.kind, "outcome": outcome}

    @respond.validator
    def _validate_respond(self, args: dict[str, Any]) -> None:
        request = self._request(args)
        parked = self._parked.get(request.request_id)
        try:
            validate_respond(parked.entry if parked else None, request)
        except RespondRefused as refused:
            raise _refused(refused.code, str(refused)) from None
        assert parked is not None
        try:
            self._agent.validate_decision(parked.call_id)
        except ValueError:
            raise _refused("stale", f"{request.request_id} is no longer waiting") from None

    @workflow.update(name=CANCEL_INPUT_UPDATE)
    async def cancel_input(self, args: dict[str, Any]) -> str:
        reason = str((args or {}).get("reason") or "cancelled")[:200]
        return "cancelled" if await self._cancel_parked(reason) else "none"

    @workflow.signal(name=INTERRUPT_SIGNAL)
    async def interrupt(self, args: dict[str, Any]) -> None:
        if self._task is None or self._task.done():
            return
        self._interrupted = INTERRUPTED
        await self._cancel_parked(INTERRUPTED)
        if self._task is not None:
            self._task.cancel()

    @workflow.query(name=PENDING_INPUT_QUERY)
    def pending_input(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in self._parked.values():
            if p.entry.state == "resolved":
                continue
            entry: dict[str, Any] = {
                "id": p.entry.id,
                "kind": p.kind,
                "session_id": self._start.session_id,
                "tool": p.tool,
                "summary": p.summary,
                "input_hash": p.entry.input_hash,
                "prompt": p.prompt,
                "requested_by": p.requested_by if p.kind == "approval" else None,
                "responders": ["browser", "grant"] if p.kind == "approval" else ["browser"],
                "created_at": _iso(p.created_at),
                "expires_at": _iso(p.expires_at),
            }
            if p.attention is not None:
                entry["attention"] = p.attention
            if p.entry.state == "resolving":
                entry["expiring"] = True
            out.append(entry)
        return out

    @workflow.query(name=SEGMENT_CONTEXT_QUERY)
    def segment_context(self) -> SegmentContext:
        return SegmentContext(
            images=list(self._turn.images) if self._turn else [],
            system_append=self._start.system_append,
        )
