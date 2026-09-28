"""One ``LISTEN`` connection per process, shared by every Postgres channel it follows.

A notification only reaches the session that listens, so the listener holds a
dedicated connection outside any pool: a pooled connection goes back to other
callers between uses. Each process needs one, whatever it listens for -- the render
queue's wake-ups (`scadbuddy_render_queue`, #270) and the event bus
(`scadbuddy_events`, spec §7) -- so both register a channel here and share it.

Every (re)connect tells each channel's ``on_connect``, with whether it is a
*re*connect: anything NOTIFYed while nothing listened is gone, and each channel's
owner decides what that costs it (the queue wakes its workers to look; the event bus
publishes ``bus.resync``). A dropped or refused connection is retried after a capped
exponential back-off with jitter, so replicas do not all reconnect in step after a
database restart. An idle connection is checked every ``check_interval``, since a
half-open TCP connection delivers nothing and raises nothing until something is sent
on it.

Several owners may :meth:`~PgListener.run` it; the connection lives while at least
one does. Channels are registered before the first run: a channel added to a live
connection would silently miss what was sent before its ``LISTEN``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from psycopg import AsyncConnection, sql

logger = logging.getLogger(__name__)

#: How the listening connection shows in `pg_stat_activity`.
LISTENER_APPLICATION_NAME = "scadbuddy-listener"

DEFAULT_CHECK_INTERVAL = 30.0


@dataclass(frozen=True)
class _Channel:
    on_notify: Callable[[str], None]
    on_connect: Callable[[bool], None] | None


class PgListener:
    """This process's LISTEN connection. See the module docstring."""

    def __init__(
        self,
        conninfo: str,
        *,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        connect_timeout: float = 30.0,
        backoff: float = 0.5,
        max_backoff: float = 30.0,
    ) -> None:
        self.conninfo = conninfo
        self.check_interval = check_interval
        self.connect_timeout = connect_timeout
        self.backoff = backoff
        self.max_backoff = max_backoff
        #: Times a LISTEN has been established.
        self.connects = 0
        #: The listening session's server process while connected, else `None`.
        self.backend_pid: int | None = None
        self._channels: dict[str, _Channel] = {}
        self._state_callbacks: list[Callable[[bool], None]] = []
        self._task: asyncio.Task[None] | None = None
        self._runners = 0

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(self._channels)

    def listen(
        self,
        channel: str,
        *,
        on_notify: Callable[[str], None],
        on_connect: Callable[[bool], None] | None = None,
    ) -> None:
        """Follow ``channel``: ``on_notify(payload)`` per notification, and
        ``on_connect(reconnected)`` each time the LISTEN is (re)established.

        Both run on the event loop and must be quick. Registering a channel twice
        replaces its callbacks."""
        if self._task is not None and channel not in self._channels:
            raise RuntimeError(f"register {channel!r} before the listener runs")
        self._channels[channel] = _Channel(on_notify=on_notify, on_connect=on_connect)

    def on_state(self, callback: Callable[[bool], None]) -> None:
        """Call ``callback(connected)`` whenever the connection comes up or drops."""
        self._state_callbacks.append(callback)

    async def run(self) -> None:
        """Listen until cancelled, reconnecting on its own; never returns otherwise.

        Shared: the connection is opened by the first caller and closed when the
        last one is cancelled."""
        self._runners += 1
        if self._task is None:
            self._task = asyncio.create_task(self._supervise())
        try:
            await asyncio.shield(self._task)
        finally:
            self._runners -= 1
            if self._runners == 0 and self._task is not None:
                task, self._task = self._task, None
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _supervise(self) -> None:
        delay = self.backoff
        while True:
            connects = self.connects
            try:
                await self._listen()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(
                    "the LISTEN connection is down; reconnecting",
                    extra={"error": str(error), "channels": list(self._channels)},
                )
            if self.connects != connects:
                delay = self.backoff  # it had been up: a new outage starts short
            await asyncio.sleep(delay * random.uniform(0.5, 1.0))
            delay = min(delay * 2, self.max_backoff)

    def _state(self, connected: bool) -> None:
        for callback in self._state_callbacks:
            try:
                callback(connected)
            except Exception:
                logger.exception("a listener state callback failed")

    async def _listen(self) -> None:
        conn = await AsyncConnection.connect(
            self.conninfo,
            autocommit=True,
            connect_timeout=max(1, round(self.connect_timeout)),
            application_name=LISTENER_APPLICATION_NAME,
        )
        async with conn:
            for name in self._channels:
                await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(name)))
            reconnected = self.connects > 0
            self.connects += 1
            self.backend_pid = conn.info.backend_pid
            self._state(True)
            try:
                for registered in self._channels.values():
                    if registered.on_connect is not None:
                        self._call(registered.on_connect, reconnected)
                while True:
                    async for notify in conn.notifies(timeout=self.check_interval):
                        target = self._channels.get(notify.channel)
                        if target is not None:
                            self._call(target.on_notify, notify.payload)
                    await conn.execute(b"SELECT 1")
            finally:
                self.backend_pid = None
                self._state(False)

    @staticmethod
    def _call[T](callback: Callable[[T], None], argument: T) -> None:
        # One owner's bug must not take the connection down for the others.
        try:
            callback(argument)
        except Exception:
            logger.exception("a LISTEN callback failed")
