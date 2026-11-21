"""Bambuddy's tray assignments, cached per app (#6580).

The filament component owns one `AssignmentsCache`, so two apps (two tests) never share
what was read. A read of Bambuddy is single-flight: a caller that comes while one runs
waits for that read's result, and no lock is held across it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from scadbuddy.bambuddy.models import SpoolAssignment
from scadbuddy.library.settings_store import StoredSettings

#: How long a read of Bambuddy's assignments serves the next requests (I-1 of the final
#: review of #6262), and how long after a failed or timed-out read none is tried again:
#: the page and the hover card then wait on Bambuddy at most once per window, not on
#: every load.
ASSIGNMENTS_FRESH_SECONDS = 5.0
ASSIGNMENTS_BACKOFF_SECONDS = 30.0
now = time.monotonic

#: What reads the assignments: `bambuddy_pull.try_assignments`, passed in because that
#: module imports the filament component, which owns the cache.
type Read = Callable[[StoredSettings], Awaitable[list[SpoolAssignment] | None]]


class AssignmentsCache:
    def __init__(self) -> None:
        #: The BB URL read, when, and what it answered (``None``: it failed, serve none).
        self.seen: tuple[str | None, float, list[SpoolAssignment] | None] | None = None
        self._inflight: tuple[str | None, asyncio.Task[list[SpoolAssignment] | None]] | None = None

    async def get(self, settings: StoredSettings, read: Read) -> list[SpoolAssignment]:
        """The assignments (`read`), read at most once per
        `ASSIGNMENTS_FRESH_SECONDS`; after a failed read, none for
        `ASSIGNMENTS_BACKOFF_SECONDS` without asking Bambuddy again. A caller during a
        read waits for its result."""
        url = settings.bambuddy_url
        seen = self.seen
        if seen is not None and seen[0] == url:
            window = (
                ASSIGNMENTS_FRESH_SECONDS if seen[2] is not None else ASSIGNMENTS_BACKOFF_SECONDS
            )
            if now() - seen[1] < window:
                return seen[2] or []
        flight = self._inflight
        if flight is None or flight[0] != url:
            flight = (url, asyncio.create_task(self._read(settings, read)))
            self._inflight = flight
        # Shielded: a caller that is cancelled does not cancel the read the others wait on.
        return await asyncio.shield(flight[1]) or []

    async def _read(self, settings: StoredSettings, read: Read) -> list[SpoolAssignment] | None:
        try:
            found = await read(settings)
            self.seen = (settings.bambuddy_url, now(), found)
            return found
        finally:
            self._inflight = None
