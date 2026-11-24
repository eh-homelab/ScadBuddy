"""A process's cached plain settings (#7384), dropped on every write to ``settings``.

`library/settings_store.py` `SettingsStore.get` reads one setting from it, so a hot path
(an operation's insert, a LAN print's checks, the cross-site gate) runs no query for it.
It holds what `SettingsStore` resolves from the ``settings`` rows less the
`SECRET_FIELDS`: no secret, and nothing of the other tables (``model_print_choices``, the
plates, the printers), which a load still reads each time.

A cache is used only while its process's one `PgListener` listens on `SETTINGS_CHANNEL`,
which a trigger on ``settings`` NOTIFYs at each write's commit, whoever wrote it
(migration ``20261124T1216Z_settings_changed_notify.sql``). Each notification drops it,
and so does each (re)connect, since a notification sent while nothing listened is gone.
While the listener is down, or in a process that never follows one (the workers, a CLI),
nothing is cached and every read goes to Postgres. `ttl` is the fallback for a
notification lost some other way.

A writer in this process drops it in the write's own path (`settings_written`, or the
store's own drop), so its next read is exact without waiting for the notification. A
read that began before a drop never keeps what it read: each drop moves the generation
on, and a read keeps its entry only under the generation it started with.
"""

from __future__ import annotations

import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scadbuddy.core.pg_listener import PgListener

#: The channel the ``settings`` trigger NOTIFYs (an empty payload) at each write's commit.
SETTINGS_CHANNEL = "scadbuddy_settings"
#: How long an entry is used with no word from the listener, in seconds.
SETTINGS_CACHE_TTL = 10.0

#: Every cache of this process, for `settings_written`.
_CACHES: weakref.WeakSet[SettingsCache[Any]] = weakref.WeakSet()


def settings_written() -> None:
    """Drop every cache of this process: for a writer of ``settings`` rows outside
    `SettingsStore` (the printer registry's merge and delete), called after the write's
    commit: a read between a drop and the commit would keep the old rows. The trigger's
    notification drops them too, and every other replica's."""
    for cache in list(_CACHES):
        cache.drop()


@dataclass(frozen=True)
class _Entry[E]:
    value: E
    at: float


class SettingsCache[E]:
    """One store's cached entry. Thread-safe: reads run on worker threads, the
    listener's callbacks on the event loop."""

    def __init__(
        self, ttl: float = SETTINGS_CACHE_TTL, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._live = False
        self._generation = 0
        self._entry: _Entry[E] | None = None
        _CACHES.add(self)

    def follow(self, listener: PgListener) -> None:
        """Cache while ``listener`` listens on `SETTINGS_CHANNEL`. Before it runs."""
        listener.listen(
            SETTINGS_CHANNEL,
            on_notify=lambda _payload: self.drop(),
            on_connect=lambda _reconnected: self._listening(True),
        )
        listener.on_state(lambda connected: None if connected else self._listening(False))

    @property
    def live(self) -> bool:
        """Whether entries are kept: the listener listens."""
        return self._live

    def _listening(self, live: bool) -> None:
        with self._lock:
            self._live = live
            self._generation += 1
            self._entry = None

    def drop(self) -> None:
        with self._lock:
            self._generation += 1
            self._entry = None

    def fresh(self) -> tuple[E | None, int | None]:
        """The entry, when one is kept and younger than `ttl`, and the generation a
        read that fills it keeps it under (`keep`); ``None`` while not live."""
        with self._lock:
            if not self._live:
                return None, None
            entry = self._entry
            if entry is not None and self.clock() - entry.at < self.ttl:
                return entry.value, self._generation
            return None, self._generation

    def keep(self, generation: int, at: float, value: E) -> None:
        """Keep ``value``, read from a start at ``at``, unless a drop came since
        ``generation``."""
        with self._lock:
            if self._live and generation == self._generation:
                self._entry = _Entry(value, at)
