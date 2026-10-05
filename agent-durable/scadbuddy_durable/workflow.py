"""The DurableSession workflow: one chat session on DurableClaudeAgent (spec §6, plan task 9)."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.claude_agent_sdk import AgentState, DurableClaudeAgent
from temporalio.exceptions import ActivityError, CancelledError, FailureError

with workflow.unsafe.imports_passed_through():
    from .segments import SAVE_SNAPSHOT
    from .tools import TOOLS

from .models import (
    DECISIONS_QUERY,
    EXPIRED_BY,
    PENDING_QUERY,
    REVIEW_UPDATE,
    SEND_UPDATE,
    WORKFLOW_NAME,
    InFlight,
    Message,
    SessionInput,
    SnapshotInput,
    render_prompt,
    restore_state,
)

_IN_FLIGHT = ("started", "waiting for approval")


@workflow.defn(name=WORKFLOW_NAME)
class DurableSession:
    @workflow.init
    def __init__(
        self, inp: SessionInput, state: AgentState | None = None, inbox: list[Message] | None = None
    ) -> None:
        if state is not None and inp.restored is not None:
            state = restore_state(state, inp.restored.in_flight)  # ruling 15
        # Continue-As-New must not restore again.
        self._inp = dataclasses.replace(inp, restored=None)
        self._inbox: list[Message] = list(inbox or [])
        self._timed: set[str] = set()
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
            continue_as_new_args=lambda s: [self._inp, s, self._inbox],
            live_output=True,
        )

    @workflow.run
    async def run(
        self, inp: SessionInput, state: AgentState | None = None, inbox: list[Message] | None = None
    ) -> AgentState:
        asyncio.create_task(self._expire_approvals())
        asyncio.create_task(self._snapshots())
        try:
            while True:
                prompt: str | None = None
                if not self.agent.busy:
                    await workflow.wait_condition(
                        lambda: bool(self._inbox) or self.agent.should_continue_as_new()
                    )
                    if not self._inbox:
                        await self.agent.continue_as_new()
                    prompt = render_prompt(self._inbox.pop(0))
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
            # session from the SessionStore at the checkpoint.
            return self.agent.state()

    @workflow.update(name=SEND_UPDATE)
    def send_message(self, message: Message) -> None:
        self._inbox.append(message)

    @send_message.validator
    def check_message(self, message: Message) -> None:
        if not message.text.strip():
            raise ValueError("the message is empty")
        if self.agent.busy or self._inbox:
            raise ValueError("the session is busy")

    @workflow.update(name=REVIEW_UPDATE)
    def review(self, tool_use_id: str, approved: bool, approver: str) -> None:
        self.agent.decide(tool_use_id, approved, approver)

    @review.validator
    def check_review(self, tool_use_id: str, approved: bool, approver: str) -> None:
        del approved
        self.agent.validate_decision(tool_use_id, approver)  # refused Updates never reach history

    @workflow.query(name=PENDING_QUERY)
    def pending_approvals(self) -> list[dict[str, Any]]:
        return self.agent.pending_approvals()

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
                self._timed.add(call_id)
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
            in_flight = [
                InFlight(c["id"], c["name"], c["status"])
                for c in self.agent.tool_calls
                if c["status"] in _IN_FLIGHT
            ]
            await workflow.execute_local_activity(
                SAVE_SNAPSHOT,
                SnapshotInput(
                    self._inp.session_id,
                    self.agent.state(),
                    in_flight,
                    self.agent.segments + self.agent.total_tool_calls,
                ),
                start_to_close_timeout=timedelta(seconds=10),
            )
