"""DurableSessions with test-only pacing: continuing as new after a few history events
(test 8), and a Stop that lingers long enough to send to it while it stops."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.claude_agent_sdk import AgentState

with workflow.unsafe.imports_passed_through():
    from scadbuddy_durable.models import SessionInput
    from scadbuddy_durable.workflow import DurableSession

AFTER_EVENTS = 40


@workflow.defn(name="DurableSessionShortRuns")
class ShortRuns(DurableSession):
    @workflow.init
    def __init__(self, inp: SessionInput, state: AgentState | None = None) -> None:
        super().__init__(inp, state)
        self.agent._continue_as_new_after_events = AFTER_EVENTS

    @workflow.run
    async def run(self, inp: SessionInput, state: AgentState | None = None) -> AgentState:
        return await super().run(inp, state)


@workflow.defn(name="DurableSessionLongStop")
class LongStop(DurableSession):
    """Stays open a minute after its `cancelled` event (the plugin's linger, 0.5 s in
    production), so a test can send while it stops."""

    @workflow.init
    def __init__(self, inp: SessionInput, state: AgentState | None = None) -> None:
        super().__init__(inp, state)
        self.agent._linger = timedelta(minutes=1)

    @workflow.run
    async def run(self, inp: SessionInput, state: AgentState | None = None) -> AgentState:
        return await super().run(inp, state)
