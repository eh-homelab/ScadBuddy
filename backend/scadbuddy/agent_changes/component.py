"""`CHANGES`: the agent's change stream hub (`hub.py`), on the process's one LISTEN connection.

Built with the core, before the lifespan starts the event bus and so its listener: the hub's
channels must be registered before the listener runs (`core/pg_listener.py`).

Its ``run`` ends every stream when the process is asked to stop. uvicorn waits for every open
response before it runs the lifespan's shutdown (`uvicorn/server.py` ``shutdown``; the image's
server sets no ``timeout_graceful_shutdown``), and a stream never ends by itself, so ``run``
chains a SIGTERM and SIGINT handler in front of the server's: uvicorn installs its own with
``signal.signal`` and puts the old ones back after (``capture_signals``). The signal ends the
streams first, then the server's handler runs as before. Python runs signal handlers in the main
thread only, so elsewhere (a test's portal thread) the lifespan's exit ends the streams.
"""

from __future__ import annotations

import asyncio
import signal
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import FrameType
from typing import Any

from scadbuddy.agent_changes.hub import ChangeHub
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.pg_events import PgNotifyEventBus

CHANGES: Key[ChangeHub] = Key("agent-changes")

SIGNALS = (signal.SIGTERM, signal.SIGINT)


def _build(core: Core, components: Components) -> ChangeHub:
    hub = ChangeHub()
    if isinstance(core.events, PgNotifyEventBus):
        hub.attach(core.events.listener, core.events)
    return hub


def chain_signals(hub: ChangeHub, loop: asyncio.AbstractEventLoop) -> dict[int, Any]:
    """Our handler in front of each of `SIGNALS`' current one; returns what it replaced."""
    replaced: dict[int, Any] = {}
    for sig in SIGNALS:
        previous = signal.getsignal(sig)

        def handler(signum: int, frame: FrameType | None, previous: Any = previous) -> None:
            if previous == signal.SIG_IGN:
                return  # the process goes on: its streams do too
            loop.call_soon_threadsafe(hub.shutdown)
            if callable(previous):
                previous(signum, frame)
            elif previous == signal.SIG_DFL:
                # What the signal would have done with no handler of ours.
                signal.signal(signum, signal.SIG_DFL)
                signal.raise_signal(signum)

        replaced[sig] = signal.signal(sig, handler)
    return replaced


@asynccontextmanager
async def _run(hub: ChangeHub) -> AsyncIterator[None]:
    replaced: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        replaced = chain_signals(hub, asyncio.get_running_loop())
    ours = {sig: signal.getsignal(sig) for sig in replaced}
    try:
        yield
    finally:
        hub.shutdown()
        for sig, previous in replaced.items():
            # Only our own is put back: a handler set over ours since is not ours to remove.
            if signal.getsignal(sig) is ours[sig]:
                signal.signal(sig, previous)


COMPONENT = Component(CHANGES, build=_build, run=_run)
