"""A DurableSession that continues as new after a few history events (test 8)."""

from __future__ import annotations

from temporalio import workflow
from temporalio.claude_agent_sdk import AgentState

with workflow.unsafe.imports_passed_through():
    from scadbuddy_durable.models import Message, SessionInput
    from scadbuddy_durable.workflow import DurableSession

AFTER_EVENTS = 40


@workflow.defn(name="DurableSessionShortRuns")
class ShortRuns(DurableSession):
    @workflow.init
    def __init__(
        self, inp: SessionInput, state: AgentState | None = None, inbox: list[Message] | None = None
    ) -> None:
        super().__init__(inp, state, inbox)
        self.agent._continue_as_new_after_events = AFTER_EVENTS

    @workflow.run
    async def run(
        self, inp: SessionInput, state: AgentState | None = None, inbox: list[Message] | None = None
    ) -> AgentState:
        return await super().run(inp, state, inbox)
