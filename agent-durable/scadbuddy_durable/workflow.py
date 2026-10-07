"""The DurableSession workflow: one chat session on DurableClaudeAgent (spec §6, plan task 9)."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.claude_agent_sdk import AgentState, DurableClaudeAgent
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError, FailureError

with workflow.unsafe.imports_passed_through():
    from .segments import SAVE_SNAPSHOT
    from .tools import TOOLS

from .models import (
    ABANDONED,
    BUSY,
    DECISIONS_QUERY,
    EXPIRED_BY,
    LOAD_INPUTS,
    PENDING_QUERY,
    RECENT_INPUTS,
    REVIEW_UPDATE,
    SEND_UPDATE,
    START_INPUT,
    STOPPING,
    UNKNOWN_INPUT,
    WORKFLOW_NAME,
    InFlight,
    Loaded,
    LoadInputs,
    Message,
    Nudge,
    SessionInput,
    SnapshotInput,
    StartInput,
    render_prompt,
    restore_state,
)

RAN = "ran"


# The input activities (inputs.py) are short database calls on this worker.
_INPUT_TIMEOUT = timedelta(seconds=10)


@workflow.defn(name=WORKFLOW_NAME)
class DurableSession:
    """One chat session. Its messages are committed to `ai_durable_inputs` by the agent
    service before Temporal is asked anything; the `send_message` Update is only a nudge
    with a message's id. The run loads the pending messages when it starts and when
    nudged, and runs them in order, taking each (START_INPUT, a compare-and-set) just
    before its turn: a message abandoned meanwhile (a Stop) is skipped, and one that ran
    never runs again, so a lost or repeated nudge neither loses nor repeats a turn."""

    @workflow.init
    def __init__(self, inp: SessionInput, state: AgentState | None = None) -> None:
        if state is not None and inp.restored is not None:
            state = restore_state(state, inp.restored.in_flight)  # ruling 15
        # Continue-As-New must not restore again.
        self._inp = dataclasses.replace(inp, restored=None)
        self._inbox: list[Message] = []
        # What became of each message this run took or was asked about: "run", or why it
        # will not run here. Messages that ran in an earlier run of this chain count too.
        self._outcome: dict[str, str] = dict.fromkeys(inp.ran, RAN)
        self._ran: list[str] = list(inp.ran)
        # Nudge handlers loading right now: the run does not continue as new under them.
        self._loading = 0
        # When each waiting call's expiry timer started (ruling 8).
        self._timed: dict[str, datetime] = {}
        self._saved: tuple[Any, ...] | None = None
        self.agent = DurableClaudeAgent(
            tools=TOOLS,
            model=inp.model,
            max_turns=inp.max_turns,
            builtin_tools=["Skill"],  # §6.3b: skills only
            tool_activities=(),  # no Bash, no MCP server: nothing runs as a tool step
            max_segments=None,  # a chat; the budget is the limit (ruling 3)
            state=state,
            auto_continue_as_new=True,
            continue_as_new_args=lambda s: [
                dataclasses.replace(self._inp, ran=self._ran[-RECENT_INPUTS:]),
                s,
            ],
            live_output=True,
        )

    @workflow.run
    async def run(self, inp: SessionInput, state: AgentState | None = None) -> AgentState:
        asyncio.create_task(self._expire_approvals())
        asyncio.create_task(self._snapshots())
        try:
            # Whatever was committed and not run yet: a nudge lost with a Temporal restart,
            # or a message sent while no run ran.
            self._enqueue(await self._load(LoadInputs(self._inp.session_id)))
            while True:
                prompt: str | None = None
                if not self.agent.busy:
                    await workflow.wait_condition(
                        lambda: (
                            bool(self._inbox) or (self._loading == 0 and self.agent.should_continue_as_new())
                        )
                    )
                    if not self._inbox:
                        await self.agent.continue_as_new()
                    message = self._inbox.pop(0)
                    if not await self._take(message.id):
                        self._outcome[message.id] = ABANDONED
                        continue
                    self._outcome[message.id] = RAN
                    self._ran = [*self._ran, message.id][-RECENT_INPUTS:]
                    prompt = render_prompt(message)
                try:
                    await self.agent.run(prompt)
                except FailureError as err:
                    if isinstance(err, ActivityError) and isinstance(err.cause, CancelledError):
                        raise asyncio.CancelledError() from err  # Stop reached a running step
                    # Otherwise the agent published `error`; the session takes the next message.
        except asyncio.CancelledError:
            # Stop (deviation 4). agent.run() already ended the task (_end_task: owed error
            # results in pending, fork_next) and published `cancelled`. The state is this
            # execution's result, so the next message's execution resumes the same Claude
            # session from the SessionStore at the checkpoint. A nudge still waiting for its
            # message refuses it as stopping (`send_message`) before the run returns.
            await workflow.wait_condition(workflow.all_handlers_finished)
            return self.agent.state()

    async def _load(self, inp: LoadInputs) -> Loaded:
        loaded: Loaded = await workflow.execute_local_activity(
            LOAD_INPUTS, inp, result_type=Loaded, start_to_close_timeout=_INPUT_TIMEOUT
        )
        return loaded

    async def _take(self, message_id: str) -> bool:
        taken: bool = await workflow.execute_local_activity(
            START_INPUT,
            StartInput(self._inp.session_id, message_id),
            result_type=bool,
            start_to_close_timeout=_INPUT_TIMEOUT,
        )
        return taken

    def _enqueue(self, loaded: Loaded) -> None:
        queued = {m.id for m in self._inbox}
        for message in loaded.pending:
            if message.id not in queued and message.id not in self._outcome:
                self._inbox.append(message)
                queued.add(message.id)

    def _known(self, message_id: str) -> bool:
        return message_id in self._outcome or any(m.id == message_id for m in self._inbox)

    @workflow.update(name=SEND_UPDATE)
    async def send_message(self, nudge: Nudge) -> None:
        """Answers once the nudged message's turn started, or refuses it.

        Never accepted and then dropped: a Stop before the turn started refuses it as
        STOPPING (the run waits for this handler before it returns), and so does another
        message's turn starting first (BUSY), so the agent service always learns that the
        message did not run, and abandons it (START_INPUT then skips it here too).
        """
        if not self._known(nudge.id):
            self._loading += 1
            try:
                loaded = await self._load(LoadInputs(self._inp.session_id, asked=nudge.id))
            finally:
                self._loading -= 1
            self._enqueue(loaded)
            if not self._known(nudge.id):
                if loaded.status == "run":
                    self._outcome[nudge.id] = RAN  # an earlier run took it
                else:
                    self._outcome[nudge.id] = ABANDONED if loaded.status == "abandoned" else UNKNOWN_INPUT
        await workflow.wait_condition(
            lambda: nudge.id in self._outcome or workflow.cancellation_reason() is not None or self.agent.busy
        )
        outcome = self._outcome.get(nudge.id)
        if outcome == RAN:
            return
        if outcome is None:
            # Refused while still queued: it must not run in this run any more (the agent
            # service abandons it, so no later run takes it either).
            self._inbox = [m for m in self._inbox if m.id != nudge.id]
            outcome = STOPPING if workflow.cancellation_reason() is not None else BUSY
            self._outcome[nudge.id] = outcome
        raise ApplicationError(outcome, non_retryable=True)

    @send_message.validator
    def check_message(self, nudge: Nudge) -> None:
        if not nudge.id:
            raise ValueError("the message has no id")
        # A Stop ends the plugin's task (the session reads idle) before this run returns
        # its state: a message must not start a turn meanwhile.
        if workflow.cancellation_reason() is not None and nudge.id not in self._outcome:
            raise ValueError(STOPPING)
        if self.agent.busy and not self._known(nudge.id):
            raise ValueError(BUSY)

    @workflow.update(name=REVIEW_UPDATE)
    def review(self, tool_use_id: str, approved: bool, approver: str) -> None:
        self.agent.decide(tool_use_id, approved, approver)

    @review.validator
    def check_review(self, tool_use_id: str, approved: bool, approver: str) -> None:
        del approved
        self.agent.validate_decision(tool_use_id, approver)  # refused Updates never reach history

    @workflow.query(name=PENDING_QUERY)
    def pending_approvals(self) -> list[dict[str, Any]]:
        """The plugin's waiting calls, each with when its expiry denies it (ISO 8601, UTC):
        this run's `approval_expiry_seconds`, fixed at its start, from its timer's start."""
        expiry = timedelta(seconds=self._inp.approval_expiry_seconds)
        return [
            {**p, "expires_at": (self._timed[p["id"]] + expiry).isoformat()} if p["id"] in self._timed else p
            for p in self.agent.pending_approvals()
        ]

    @workflow.query(name=DECISIONS_QUERY)
    def decisions(self) -> dict[str, str]:
        return {c["id"]: c["decided_by"] for c in self.agent.tool_calls if c.get("decided_by")}

    def _untimed(self) -> list[str]:
        return [p["id"] for p in self.agent.pending_approvals() if p["id"] not in self._timed]

    async def _expire_approvals(self) -> None:
        """Spec §6.4 / ruling 8: approval_expiry_seconds becomes a timer per waiting call."""
        while True:
            await workflow.wait_condition(lambda: bool(self._untimed()))
            for call_id in self._untimed():
                self._timed[call_id] = workflow.now()
                asyncio.create_task(self._expire(call_id))

    async def _expire(self, call_id: str) -> None:
        await workflow.sleep(self._inp.approval_expiry_seconds)
        if any(p["id"] == call_id for p in self.agent.pending_approvals()):
            self.agent.decide(call_id, False, EXPIRED_BY)

    def _mark(self) -> tuple[Any, ...]:
        return (
            self.agent.segments,
            self.agent.total_tool_calls,
            tuple((c["id"], c["status"]) for c in self.agent.tool_calls),
        )

    async def _snapshots(self) -> None:
        """Ruling 15: save AgentState whenever a segment commits or a tool call moves."""
        while True:
            await workflow.wait_condition(lambda: self._mark() != self._saved)
            self._saved = self._mark()
            state = self.agent.state()
            # The whole unanswered batch, whatever its status: a call that finished joins
            # recent_call_ids only when the next segment commits.
            answered = set(state.recent_call_ids)
            in_flight = [
                InFlight(c["id"], c["name"], c["status"])
                for c in self.agent.tool_calls
                if c["id"] not in answered
            ]
            await workflow.execute_local_activity(
                SAVE_SNAPSHOT,
                SnapshotInput(
                    self._inp.session_id,
                    state,
                    in_flight,
                    self.agent.segments + self.agent.total_tool_calls,
                ),
                start_to_close_timeout=timedelta(seconds=10),
            )
