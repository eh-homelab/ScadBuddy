"""DurableSession: one assistant session as a Temporal workflow (spec 2026-10-01 §6, §8).

``session-<ai_sessions.id>`` on the ``agent`` queue. It runs the plugin's
``DurableClaudeAgent`` with the agent's tools (``session.tools``), one turn per
``send_message``, and continues as new between turns when the server suggests it
(and, the plugin's ``auto_continue_as_new``, between the steps of a long turn).

A tool parks by its effective tier (#2088): ``gate_settings`` reads Settings → Plugins
as each turn starts, a tool raised to ``outward`` parks like any outward tool, and a
disabled one is left out of what the segment runner declares (``segment_context``).

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
from temporalio.exceptions import ActivityError, ApplicationError, FailureError

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
        END_SIGNAL,
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
        MESSAGE_MAX,
        SEGMENT_ATTEMPTS,
        SEGMENT_CONTEXT_QUERY,
        SEND_MESSAGE_UPDATE,
        TOOLS_QUEUE,
        TURN_QUERY,
        WORKFLOW,
        FinishTurn,
        FollowArgs,
        GateSettings,
        MemoryRecalled,
        MemoryTurn,
        Message,
        SegmentContext,
        SendAnswer,
        SessionStart,
        TurnOutcome,
        TurnPackage,
        TurnStart,
        TurnTools,
    )

DESCRIBE_CALL = "gate.describe_call"
# agent/src/temporal/turnTools.ts (#2320).
TURN_TOOLS = "turn_tools"
TURN_TOOL_TIER = "turn_tool_tier"
TURN_TOOLS_PATCH = "turn-tools-2320"
# The built-ins a package may name that run as their own activities, so a call can park.
ENGINE_PARKED = frozenset({"Bash", "PowerShell"})
TURN_TOOLS_TIMEOUT = timedelta(minutes=2)
# #2581: a session's plugin calls are pinned to the replica that listed them, which keeps
# its MCP client on each plugin (agent temporal/sessionClients.ts), so a stateful plugin
# (Playwright MCP) keeps its page. The next turn is listed on that replica's own queue
# first, so the pin and the page outlive the turn; a replica that does not pick it up
# within HOST_WAIT is gone, and the turn is listed on agent-tools (a new client there).
# turn_tools_close ends the clients there when the session ends.
TURN_TOOLS_CLOSE = "turn_tools_close"
HOST_PIN_PATCH = "turn-tools-host-2581"
HOST_WAIT = timedelta(seconds=5)
# agent/src/temporal/memoryActivities.ts (#2320, superseding #825): automatic Hindsight
# memory around a turn, as a classic turn's hooks do it.
MEMORY_RECALL = "memory_recall"
MEMORY_RETAIN = "memory_retain"
MEMORY_PATCH = "memory-2320"
# Past the hooks' own limits (3 s recall, 15 s retain) and the plugin lookup.
MEMORY_RECALL_TIMEOUT = timedelta(seconds=30)
MEMORY_RETAIN_TIMEOUT = timedelta(minutes=1)
# Never retried, never failing the turn: the agent records a failure in the session.
MEMORY_ONCE = RetryPolicy(maximum_attempts=1)
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
# workflow.patched ids (#2123): each changes the commands of a path an older history
# may have taken, so that history replays the old way.
STOPPED_BEFORE_RUN = "stop-before-run-skips-the-turn"
SETTLE_PARKED = "turn-end-settles-resolving-entries"
STOP_FIRST = "stop-cancels-the-run-first"


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


def refusal(tool: str, request_id: str, outcome: str, reason: str | None) -> str:
    """What the model is told when its call was not approved, in the words a classic turn
    uses (agent/src/approvals/service.ts ``refusal``)."""
    what = f"{tool} (approval {request_id})"
    if outcome == "denied":
        return (
            f"The user denied {what}; it was not run. Do not retry it unless the user asks you to."
        )
    if outcome == "expired":
        return (
            f"Nobody decided on {what} before it expired, so it was not run. "
            "Tell the user it needs their approval."
        )
    if outcome == "cancelled":
        return (
            f"The approval for {what} was cancelled ({reason or 'no reason given'}), "
            "so it was not run."
        )
    if outcome == "withdrawn":
        return (
            f"The approval for {what} was withdrawn before it ran "
            f"({reason or 'no reason given'}), so it was not run."
        )
    return f"{what} was not approved, so it was not run."


@workflow.defn(name=WORKFLOW)
class DurableSession:
    @workflow.init
    def __init__(self, start: SessionStart) -> None:
        self._start = start
        entries = manifest.manifest()
        self._entries = entries
        self._kinds = {e.name: e.hitl for e in entries}
        # The plugin keeps these objects and reads needs_approval as each call comes:
        # a turn sets it from Settings → Plugins (manifest.apply_overrides, #2088).
        self._tools = {t.name: t for t in manifest.durable_tools(entries)}
        # This turn's other tools (plugins, http_request; #2320):
        # offered from what turn_tools listed, replaced every turn.
        self._turn_tools: set[str] = set()
        # This turn's plugin packages (#2320), from turn_tools: the segment runner loads them.
        self._packages: list[TurnPackage] = []
        # The replica this session's pinned turn tools run on (#2581), from the last listing.
        self._host_queue: str | None = start.host_queue
        self._turn: TurnStart | None = start.turn
        self._message: Message | None = None
        self._parked: dict[str, Parked] = {}
        self._seen: set[str] = set()
        self._parks: list[asyncio.Task[None]] = []
        self._task: asyncio.Task[str] | None = None
        # From a message's acceptance until its turn's finish_turn has run: the plugin's
        # run() returns before the gate is drained and the turn is written (review of #1958).
        self._in_turn = start.turn is not None
        self._interrupted: str | None = start.interrupted
        # Marked done (#1056): the run returns once no turn runs.
        self._ended = False
        self._settings = GateSettings(approval_expiry_s=600, question_expiry_s=3600)
        self._agent = DurableClaudeAgent(
            tools=list(self._tools.values()),
            max_turns=start.max_turns,
            # The own plugin's skills and subagents (#2320: subagents in durable too).
            builtin_tools=["Skill", "Agent"],
            tool_activities=(),
            max_segments=MAX_SEGMENTS,
            state=start.agent,
            auto_continue_as_new=True,
            continue_as_new_args=self._next_run,
            live_output=True,
            segment_retry_policy=RetryPolicy(
                maximum_interval=timedelta(seconds=100), maximum_attempts=SEGMENT_ATTEMPTS
            ),
        )

    def _next_run(self, state: AgentState) -> list[Any]:
        return [
            dataclasses.replace(
                self._start,
                agent=state,
                turn=self._turn,
                interrupted=self._interrupted,
                host_queue=self._host_queue,
            )
        ]

    @workflow.run
    async def run(self, start: SessionStart) -> None:
        while True:
            if self._agent.busy:
                # A turn handed over by continue-as-new: the prompt was sent already.
                await self._run_turn(None)
            else:
                await workflow.wait_condition(lambda: self._message is not None or self._ended)
                if self._message is None:
                    await self._close_turn_tools()
                    return
                message, self._message = self._message, None
                assert message is not None
                self._turn = TurnStart(
                    message.turn_id,
                    message.author,
                    list(message.images),
                    message.system_append,
                    started_at=workflow.now().isoformat(),
                )
                await self._run_turn(prompt_of(message))
            self._turn = None
            self._in_turn = False
            self._interrupted = None
            if self._message is None and self._agent.should_continue_as_new():
                # Refuses sends from here: one accepted now would not be carried over.
                self._in_turn = True
                await self._agent.continue_as_new()

    # ---- a turn ----
    async def _run_turn(self, prompt: str | None) -> None:
        sid = self._start.session_id
        turn_id = self._turn.turn_id if self._turn else ""
        self._settings = await workflow.execute_activity(
            "gate_settings", start_to_close_timeout=SHORT, result_type=GateSettings
        )
        # From the activity's result, so a replay sets the same gates: a turn an older
        # worker started read no overrides, and parks by the manifest as it did then.
        manifest.apply_overrides(
            self._tools,
            self._entries,
            self._settings.tool_tiers,
            set(self._settings.disabled_tools),
        )
        if workflow.patched(TURN_TOOLS_PATCH):
            # What a classic turn would hand Claude Code beside ScadBuddy's own tools,
            # listed by the agent as it is now; from the result, so a replay offers the same.
            offered = await self._list_turn_tools()
            # The plugin's own table, which each segment declares its tools from.
            self._turn_tools = manifest.offer_turn_tools(
                self._agent._tools, self._tools, self._turn_tools, offered.tools
            )
            self._packages = list(offered.packages)
            # The built-ins the allowed packages name, as a classic turn offers them; each
            # call is outward there, so here it runs as its own activity after a human
            # approved it (the agent offers only Bash and PowerShell, which can).
            named = sorted(
                {t for p in offered.packages for t in p.builtin_tools} - {"Skill", "Agent"}
            )
            parked = [t for t in named if t in ENGINE_PARKED]
            # Only those: one the agent should have left out never runs unasked here.
            self._agent._builtin_tools = ["Skill", "Agent", *parked]
            self._agent._tool_activities = parked
            self._agent._tool_approvals = parked
        if prompt is not None and workflow.patched(MEMORY_PATCH):
            prompt += await self._recall(turn_id)
        if self._interrupted is not None and workflow.patched(STOPPED_BEFORE_RUN):
            # Stopped before its run began (#2123): a run cancelled before its first
            # step publishes nothing, not even its prompt, so a subscriber would wait
            # the whole FOLLOW_DRAIN for an end that never comes. Nothing ran, so
            # there is nothing to follow, park or cancel.
            await self._finish(sid, turn_id, "interrupted", None)
            return
        follow = workflow.start_activity(
            "follow_session",
            FollowArgs(
                session_id=sid,
                workflow_id=workflow.info().workflow_id,
                prompted=prompt is not None,
                since=self._turn.started_at if self._turn else None,
            ),
            start_to_close_timeout=FOLLOW_TIMEOUT,
            heartbeat_timeout=FOLLOW_HEARTBEAT,
            retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=10)),
        )
        watcher = asyncio.create_task(self._watch_gate())
        outcome: TurnOutcome = "done"
        message: str | None = None
        self._task = asyncio.create_task(self._agent.run(prompt))
        if self._interrupted is not None:
            self._task.cancel()
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
                outcome, message = "failed", text[:MESSAGE_MAX]
        finally:
            self._task = None
        watcher.cancel()
        reason = self._interrupted or TURN_ENDED
        await self._cancel_parked(reason)
        if workflow.patched(SETTLE_PARKED):
            await self._settle_parked(reason)
        for park in self._parks:
            park.cancel()
        self._parks = []
        self._seen = set()
        self._parked = {}
        try:
            await workflow.wait_condition(follow.done, timeout=FOLLOW_DRAIN)
        except TimeoutError:
            follow.cancel()
        if workflow.patched(MEMORY_PATCH):
            await self._retain(turn_id)
        await self._finish(sid, turn_id, outcome, message)

    async def _finish(
        self, sid: str, turn_id: str, outcome: TurnOutcome, message: str | None
    ) -> None:
        await workflow.execute_activity(
            "finish_turn",
            FinishTurn(session_id=sid, turn_id=turn_id, outcome=outcome, message=message),
            start_to_close_timeout=SHORT,
        )

    async def _list_turn_tools(self) -> TurnTools:
        """This turn's other tools: on the replica last turn's were pinned to, if it is
        still there (#2581), else on agent-tools."""
        if self._host_queue is not None and workflow.patched(HOST_PIN_PATCH):
            with contextlib.suppress(FailureError):
                pinned: TurnTools = await workflow.execute_activity(
                    TURN_TOOLS,
                    task_queue=self._host_queue,
                    schedule_to_start_timeout=HOST_WAIT,
                    start_to_close_timeout=TURN_TOOLS_TIMEOUT,
                    retry_policy=RetryPolicy(maximum_attempts=1),
                    result_type=TurnTools,
                )
                self._pin(pinned)
                return pinned
        offered: TurnTools
        try:
            offered = await workflow.execute_activity(
                TURN_TOOLS,
                task_queue=TOOLS_QUEUE,
                start_to_close_timeout=TURN_TOOLS_TIMEOUT,
                retry_policy=RetryPolicy(
                    maximum_attempts=3, non_retryable_error_types=["UnknownSession"]
                ),
                result_type=TurnTools,
            )
        except FailureError:
            # As a classic turn whose plugins cannot be reached: it goes on with
            # ScadBuddy's own tools, and offers none of last turn's others.
            offered = TurnTools()
        self._pin(offered)
        return offered

    def _pin(self, offered: TurnTools) -> None:
        """The replica this turn's pinned tools run on, kept for the next turn (#2581)."""
        if workflow.patched(HOST_PIN_PATCH):
            self._host_queue = next((t.task_queue for t in offered.tools if t.task_queue), None)

    async def _close_turn_tools(self) -> None:
        """Ends the session's kept MCP clients on its replica (#2581); a replica that is
        gone took them with it, and one that does not answer ends them when they go idle."""
        if self._host_queue is None or not workflow.patched(HOST_PIN_PATCH):
            return
        with contextlib.suppress(ActivityError):
            await workflow.execute_activity(
                TURN_TOOLS_CLOSE,
                task_queue=self._host_queue,
                schedule_to_close_timeout=HOST_WAIT,
                retry_policy=RetryPolicy(maximum_attempts=1),
            )

    async def _recall(self, turn_id: str) -> str:
        """The memories a classic turn's recall hook would add, to follow the prompt;
        nothing when memory is off, nothing matched, or the recall failed."""
        try:
            recalled = await workflow.execute_activity(
                MEMORY_RECALL,
                MemoryTurn(turn_id),
                task_queue=TOOLS_QUEUE,
                start_to_close_timeout=MEMORY_RECALL_TIMEOUT,
                retry_policy=MEMORY_ONCE,
                result_type=MemoryRecalled,
            )
        except ActivityError:
            return ""
        return f"\n\n{recalled.context}" if recalled.context else ""

    async def _retain(self, turn_id: str) -> None:
        """The session's transcript retained, as a classic turn's Stop hook does."""
        with contextlib.suppress(ActivityError):
            await workflow.execute_activity(
                MEMORY_RETAIN,
                MemoryTurn(turn_id),
                task_queue=TOOLS_QUEUE,
                start_to_close_timeout=MEMORY_RETAIN_TIMEOUT,
                retry_policy=MEMORY_ONCE,
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
                return  # parked: its entry is ended by the gate (cancel, interrupt, turn end)
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
        if name in self._turn_tools and not await self._outward(name, call):
            # A read or write call (an http GET, a plugin tool tiered read) goes straight
            # through, as the permission seam lets it; its activity checks the tier again.
            self._agent.decide(str(call["id"]), True, approver="scadbuddy")
            return
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

    async def _outward(self, name: str, call: dict[str, Any]) -> bool:
        """Whether a turn tool's call is outward, by its input (turn_tool_tier). Only a
        tool listed as may_park is asked about, so one the agent cannot tier (its plugin
        did not answer, or is gone) parks: fail toward asking. Its activity still refuses
        a call whose tool is gone, saying why."""
        try:
            tiered = await workflow.execute_activity(
                TURN_TOOL_TIER,
                {"tool": name, "input": call["input"]},
                task_queue=TOOLS_QUEUE,
                start_to_close_timeout=SHORT,
                retry_policy=RetryPolicy(
                    maximum_attempts=3, non_retryable_error_types=["UnknownSession", "ToolError"]
                ),
                result_type=dict,
            )
        except FailureError:
            return True
        return bool(tiered.get("tier") == "outward")

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
        if not go and self._turn is not None:
            # The plugin gives the model a fixed rejection; the runner adds why (#2392).
            self._turn.refusals[parked.call_id] = refusal(
                parked.tool,
                parked.entry.id,
                "withdrawn" if outcome == "approved" else outcome,
                reason,
            )
        # The turn may have ended while resolve_input ran (an interrupt): the call then
        # waits no more, and its row is already right (review of #1958).
        with contextlib.suppress(ValueError):
            self._agent.decide(parked.call_id, go, approver=responder.get("id"))
        return written

    async def _cancel_parked(self, reason: str, *, bounded: bool = False) -> bool:
        """Only cancel_input, an Update with a caller to tell, gives up after 30 s. A Stop
        or a turn's end retries without end, as the timer does: a failure raised from the
        interrupt Signal or from run() would fail the whole session (review of #1958)."""
        pending = [p for p in self._parked.values() if p.entry.state == "pending"]
        for parked in pending:
            await self._resolve(parked, "cancelled", SYSTEM, None, reason, timer=not bounded)
        return bool(pending)

    async def _settle_parked(self, reason: str) -> None:
        """Lets every resolve_input in flight finish before the parks are cancelled (#2123):
        one whose timer fired is in a park task, and cancelling it there would leave its
        row pending. One that fails (a respond's bounded attempt) is cancelled after."""
        while True:
            await workflow.wait_condition(
                lambda: all(p.entry.state != "resolving" for p in self._parked.values())
            )
            if not await self._cancel_parked(reason):
                return

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
        if self._ended:
            raise ApplicationError("the session was marked done", type="ended", non_retryable=True)

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
        return "cancelled" if await self._cancel_parked(reason, bounded=True) else "none"

    @workflow.signal(name=INTERRUPT_SIGNAL)
    async def interrupt(self, args: dict[str, Any]) -> None:
        if not self._in_turn:
            return
        if self._task is None:
            # The turn is starting (gate_settings): it stops before its run begins. While
            # it drains or writes its end, there is nothing left to stop, and the flag is
            # cleared with the turn.
            self._interrupted = INTERRUPTED
            return
        if self._task.done():
            return
        self._interrupted = INTERRUPTED
        if workflow.patched(STOP_FIRST):
            # The run stops now, even with Postgres down (#2123); the turn's end then
            # cancels the parked entries with this reason, retrying until it can.
            self._task.cancel()
            return
        await self._cancel_parked(INTERRUPTED)
        if self._task is not None:
            self._task.cancel()

    @workflow.signal(name=END_SIGNAL)
    def end(self, args: dict[str, Any]) -> None:
        self._ended = True

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
            # The turn's own append; a turn sent without one keeps the session's first (#2525).
            system_append=(
                self._turn.system_append
                if self._turn is not None and self._turn.system_append is not None
                else self._start.system_append
            ),
            disabled_tools=list(self._settings.disabled_tools),
            packages=list(self._packages),
            refusals=dict(self._turn.refusals) if self._turn else {},
        )

    @workflow.query(name=TURN_QUERY)
    def turn(self) -> str | None:
        """The turn this workflow holds: running (or handed over by continue-as-new) or
        finishing, else a message taken but not yet started. None between turns (#2078)."""
        if self._turn is not None:
            return self._turn.turn_id
        return self._message.turn_id if self._message is not None else None
