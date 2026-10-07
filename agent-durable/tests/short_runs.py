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
    continue_as_new_after_events = AFTER_EVENTS

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


@workflow.defn(name="DurableSessionForcedHandOver")
class ForcedHandOver(DurableSession):
    """Continues as new once a test suggests it (`suggest`), as Temporal would; and can
    hold a handler open (`hold` until `release`) so a test can nudge while the run waits
    in the plugin's hand-over for every handler to finish."""

    @workflow.init
    def __init__(self, inp: SessionInput, state: AgentState | None = None) -> None:
        super().__init__(inp, state)
        self._held = False

    @workflow.run
    async def run(self, inp: SessionInput, state: AgentState | None = None) -> AgentState:
        return await super().run(inp, state)

    @workflow.signal(name="suggest")
    def suggest(self) -> None:
        self.agent._continue_as_new_after_events = 1

    @workflow.update(name="hold")
    async def hold(self) -> None:
        self._held = True
        await workflow.wait_condition(lambda: not self._held)

    @workflow.query(name="handing_over")
    def handing_over(self) -> bool:
        return self._handing_over

    @workflow.signal(name="release")
    def release(self) -> None:
        self._held = False


@workflow.defn(name="DurableSessionSlowTakes")
class SlowTakes(DurableSession):
    """Gives its input activities a minute, so a test can hold a take open across the
    workflow task's heartbeats (which admit a nudge meanwhile) without it timing out."""

    input_timeout = timedelta(minutes=1)

    @workflow.run
    async def run(self, inp: SessionInput, state: AgentState | None = None) -> AgentState:
        return await super().run(inp, state)


@workflow.defn(name="DurableSessionLongRetries")
class LongRetries(DurableSession):
    """A nudge for an id not seen yet waits a minute before it looks again, so a test can
    tell a Stop that wakes that wait from one that waits it out."""

    unseen_retries = (60.0,)

    @workflow.run
    async def run(self, inp: SessionInput, state: AgentState | None = None) -> AgentState:
        return await super().run(inp, state)
