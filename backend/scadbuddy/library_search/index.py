"""Bambuddy's whole library, kept briefly in process for the Library page's search (#3630).

Bambuddy has no search over its library, and only one read lists every file at once
(``GET /library/files/?include_root=false``): measured against the live library (2,367
files, 466 folders) it is 1.3 MB and 1.6-2.9 s, with the folder tree another 0.6-1.0 s
(#3633). So one such read, with the tree, is kept and searched in memory. It is fresh for
:data:`FRESH` seconds; after that, for up to :data:`STALE` seconds, a search is answered
from it at once while one read in the background replaces it. ``fresh`` asks for a read
that started at most :data:`SHARED` seconds before the request did, as after ScadBuddy's
own write: requests that close together share one read rather than stack whole-library
reads on Bambuddy. Bambuddy stays the
source of truth: nothing is kept past :data:`STALE`, nor across a restart.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.library_listing import LibraryFolderView, folder_views
from scadbuddy.bambuddy.models import LibraryListRow

FRESH = 30.0
STALE = 600.0
#: How long before a ``fresh`` request a read may have started and still answer it.
SHARED = 2.0

logger = logging.getLogger(__name__)

#: Opens a client on the Bambuddy the settings name; a background read opens its own.
Connect = Callable[[], AbstractAsyncContextManager[BambuddyClient]]


@dataclass(frozen=True)
class LibrarySnapshot:
    folders: list[LibraryFolderView]
    files: list[LibraryListRow]
    #: When the read began (the index's clock): nothing it holds is older.
    read_at: float


class LibraryIndex:
    def __init__(
        self,
        *,
        fresh: float = FRESH,
        stale: float = STALE,
        shared: float = SHARED,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fresh = fresh
        self._stale = stale
        self._shared = shared
        self._clock = clock
        self._kept: dict[str, LibrarySnapshot] = {}
        self._reading: dict[str, tuple[float, asyncio.Task[LibrarySnapshot]]] = {}

    async def snapshot(
        self, source: str, connect: Connect, *, fresh: bool = False
    ) -> LibrarySnapshot:
        """The library of ``source`` (a Bambuddy and the key it is read with): what is
        kept while it is fresh, or stale with a read under way; else a read's answer."""
        now = self._clock()
        kept = self._kept.get(source)
        if kept is not None and not fresh:
            age = now - kept.read_at
            if age < self._fresh:
                return kept
            if age < self._stale:
                self._read(source, connect, since=kept.read_at)
                return kept
        since = now - self._shared if fresh else now
        if kept is not None and kept.read_at >= since:
            return kept
        # Shielded: one caller going away does not cancel the read the others wait on.
        return await asyncio.shield(self._read(source, connect, since=since))

    def _read(
        self, source: str, connect: Connect, *, since: float
    ) -> asyncio.Task[LibrarySnapshot]:
        """The read in flight if it started at or after ``since``, else a new one."""
        reading = self._reading.get(source)
        if reading is not None and reading[0] >= since:
            return reading[1]
        started = self._clock()
        task = asyncio.ensure_future(self._load(source, connect, started))
        task.add_done_callback(_log_failure)
        self._reading[source] = (started, task)
        return task

    async def _load(self, source: str, connect: Connect, started: float) -> LibrarySnapshot:
        try:
            async with connect() as client:
                tree, files = await asyncio.gather(
                    client.folders(), client.library_listing(folder_id=None, every_folder=True)
                )
        finally:
            reading = self._reading.get(source)
            if reading is not None and reading[0] == started:
                del self._reading[source]
        snapshot = LibrarySnapshot(folders=folder_views(tree), files=files, read_at=started)
        kept = self._kept.get(source)
        # A read that began before the one kept is older than it, and is not kept.
        if kept is None or kept.read_at <= started:
            # One Bambuddy at a time: a changed URL or key drops what the old one had.
            self._kept = {source: snapshot}
        return snapshot

    async def close(self) -> None:
        """Cancel the reads under way and wait for them, at shutdown."""
        tasks = [task for _, task in self._reading.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _log_failure(task: asyncio.Task[LibrarySnapshot]) -> None:
    """A background read's failure is logged; a waiting search raises it itself."""
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.info("could not read the library index", extra={"error": type(error).__name__})
