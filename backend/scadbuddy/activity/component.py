"""Activity as a component (spec 2026-10-09): the index, the kinds, and the bus
consumer that ingests the agent's operations."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.activity.kinds import KindRegistry, discover_kinds, load_agent_kinds
from scadbuddy.api.deps import transactional_events
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.events import AgentOperationEvent, Event, EventBus
from scadbuddy.operations.component import OPERATIONS

KINDS_TTL = 60.0


@dataclass
class ActivityComponent:
    index: ActivityIndex
    #: The kinds, reloaded at most every ``KINDS_TTL`` seconds (the agent's are data).
    kinds: Callable[[], KindRegistry]
    bus: EventBus


ACTIVITY: Key[ActivityComponent] = Key("activity")


def _build(core: Core, components: Components) -> ActivityComponent:
    ops = components.get(OPERATIONS)
    index = ActivityIndex(
        core.projection.pool, events=transactional_events(core.events), metrics=core.metrics
    )
    core.projection.activity = index
    ops.store.activity = index
    core.print_runs.store.activity = index  # `PrintCommands.store`, as `api/outputs.py` reads it
    loaded: list[tuple[float, KindRegistry]] = []

    def kinds() -> KindRegistry:
        if not loaded or time.monotonic() - loaded[0][0] > KINDS_TTL:
            with core.projection.pool.connection() as conn:
                registry = discover_kinds(ops.kinds.keys(), load_agent_kinds(conn))
            loaded[:] = [(time.monotonic(), registry)]
        return loaded[0][1]

    return ActivityComponent(index=index, kinds=kinds, bus=core.events)


@asynccontextmanager
async def _run(component: ActivityComponent) -> AsyncIterator[None]:
    """Ingest every ``ai_operation.changed`` while the app runs. The bus calls listeners
    on its own thread (``PgNotifyEventBus.add_listener``), so hop onto the loop, as
    ``api/runtime.py`` ``follow_changes`` does."""
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()

    def ingest(operation_id: str) -> None:
        task = loop.create_task(component.index.ingest_agent_operation(operation_id))
        pending.add(task)
        task.add_done_callback(pending.discard)

    def on_event(event: Event) -> None:
        if isinstance(event, AgentOperationEvent):
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(ingest, event.operation_id)

    remove = component.bus.add_listener(on_event)
    try:
        yield
    finally:
        remove()
        for task in pending:
            task.cancel()


COMPONENT = Component(ACTIVITY, build=_build, run=_run)
