"""Bambuddy's archive reads, kept briefly in process (#308; print-history plan §2.4).

The prints list reads one archive per print, and a history page, its next page and a
detail opened from it read the same ones again within seconds. Each read is kept for
`ARCHIVE_TTL` seconds, per Bambuddy and archive, and nothing longer: Bambuddy stays
the source of truth, and this is not a second store of its data. A failed read is not
kept, except the 404 of an archive deleted in Bambuddy, which is an answer. Requests
that miss on the same key while its read is in flight share that one read.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from typing import TypeVar, cast

from fastapi import status

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import (
    ArchiveDetail,
    ArchiveRunList,
    LibraryFile,
    Printer,
    TimelapseInfo,
)
from scadbuddy.core.problems import ApiError

ARCHIVE_TTL = 30.0
#: The key of a read that is of no one archive (the printers).
_ALL = 0
#: How long a timelapse's stills and probe are kept (#2360): Bambuddy extracts them with
#: ffmpeg on every read, 20-26 s for one print, and a timelapse does not change once
#: made. They are kept by the timelapse's path, so a different one is read afresh.
TIMELAPSE_TTL = 6 * 3600.0
#: How long a failed extraction of a timelapse's stills is kept (#2903): Bambuddy runs
#: ffmpeg for each read, and one that always fails would otherwise run on every view.
FAILED_FRAMES_TTL = 60.0
#: Enough for every print of a busy homelab several times over.
MAX_ENTRIES = 4096

T = TypeVar("T")

logger = logging.getLogger(__name__)


class ArchiveCache:
    def __init__(
        self,
        ttl: float = ARCHIVE_TTL,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._max_entries = max_entries
        self._entries: dict[tuple[str, str, int], tuple[float, object]] = {}
        self._loading: dict[tuple[str, str, int], asyncio.Task[object]] = {}

    async def archive(self, client: BambuddyClient, archive_id: int) -> ArchiveDetail | None:
        """The archive, or None when Bambuddy has none by that id (deleted there)."""

        async def load() -> ArchiveDetail | None:
            try:
                return await client.archive(archive_id)
            except ApiError as error:
                if error.status == status.HTTP_404_NOT_FOUND:
                    return None
                raise

        return await self._get(client, "archive", archive_id, load)

    async def prime(
        self,
        client: BambuddyClient,
        archive_ids: Sequence[int],
        *,
        date_from: date | None,
        date_to: date | None,
        limit: int,
    ) -> int:
        """Keep as many of ``archive_ids`` as one list read holds, ahead of `archive`
        (#2359). Bambuddy's list answers the same ``ArchiveResponse`` as a single read,
        but filters only by the day it created the archive, not by id: it is read over
        ``date_from``..``date_to``, ``limit`` rows at most, newest first. An archive it
        does not hold (outside the window, past the limit, deleted, or not visible to the
        key's user) is left to `archive` to read on its own. Nothing is read when at most
        one of them is missing: the list is then no cheaper.

        Returns how many of the missing archives the list held. Only an optimisation, so
        a failed list read, or a row of it that does not parse (one ScadBuddy may never
        have linked), is logged and leaves them all to `archive` (#2818)."""
        now = self._clock()
        missing = {
            archive_id
            for archive_id in archive_ids
            if (hit := self._entries.get(self._key(client, "archive", archive_id))) is None
            or hit[0] <= now
        }
        if len(missing) < 2:
            return 0
        try:
            rows = await client.archive_outcomes(date_from=date_from, date_to=date_to, limit=limit)
        except (ApiError, ValueError) as error:
            logger.info("could not list the archives", extra={"error": type(error).__name__})
            return 0
        held = [row for row in rows if row.id in missing]
        for row in held:
            self._put(self._key(client, "archive", row.id), row, self._ttl)
        return len(held)

    async def printers(self, client: BambuddyClient) -> list[Printer]:
        """Bambuddy's printers: what the prints name a printer by (#2363)."""
        return await self._get(client, "printers", _ALL, client.printers)

    async def library_file(self, client: BambuddyClient, file_id: int) -> LibraryFile | None:
        """A library file a print was made of (#1755), or None when Bambuddy has none
        by that id (deleted there): the history names a library print by it."""

        async def load() -> LibraryFile | None:
            try:
                return await client.library_file(file_id)
            except ApiError as error:
                if error.status == status.HTTP_404_NOT_FOUND:
                    return None
                raise

        return await self._get(client, "library_file", file_id, load)

    async def runs(self, client: BambuddyClient, archive_id: int) -> ArchiveRunList:
        return await self._get(client, "runs", archive_id, lambda: client.archive_runs(archive_id))

    async def timelapse_info(
        self, client: BambuddyClient, archive_id: int, timelapse_path: str
    ) -> TimelapseInfo:
        """Bambuddy's probe of the timelapse at ``timelapse_path``. A failed read raises
        and is not kept: the caller shows the video without it."""
        return await self._get(
            client,
            f"timelapse_info:{timelapse_path}",
            archive_id,
            lambda: client.timelapse_info(archive_id),
            ttl=TIMELAPSE_TTL,
        )

    async def timelapse_frames(
        self, client: BambuddyClient, archive_id: int, timelapse_path: str
    ) -> list[tuple[float, bytes]]:
        """The stills of the timelapse at ``timelapse_path``, each its time and its JPEG,
        decoded once (#2512). A failed read raises, and raises again without asking
        Bambuddy for `FAILED_FRAMES_TTL` (#2903)."""
        failed = self._key(client, f"timelapse_frames_failed:{timelapse_path}", archive_id)
        hit = self._entries.get(failed)
        if hit is not None and hit[0] > self._clock():
            raise cast(ApiError, hit[1])

        async def load() -> list[tuple[float, bytes]]:
            frames = await client.timelapse_thumbnails(archive_id)
            decoded: list[tuple[float, bytes]] = []
            for at, jpeg in zip(frames.timestamps, frames.thumbnails, strict=False):
                try:
                    decoded.append((at, base64.b64decode(jpeg, validate=True)))
                except binascii.Error:
                    continue
            return decoded

        try:
            return await self._get(
                client, f"timelapse_frames:{timelapse_path}", archive_id, load, ttl=TIMELAPSE_TTL
            )
        except ApiError as error:
            self._put(failed, error, FAILED_FRAMES_TTL)
            raise

    def forget(self, client: BambuddyClient, archive_id: int) -> None:
        """Drop everything kept for the archive, after ScadBuddy changed it."""
        for key in [
            k for k in self._entries if k[0] == client.config.base_url and k[2] == archive_id
        ]:
            del self._entries[key]
        # A read already in flight may predate the change: it is not kept when it lands.
        for key in [
            k for k in self._loading if k[0] == client.config.base_url and k[2] == archive_id
        ]:
            del self._loading[key]

    async def _get(
        self,
        client: BambuddyClient,
        kind: str,
        archive_id: int,
        load: Callable[[], Awaitable[T]],
        *,
        ttl: float | None = None,
    ) -> T:
        key = self._key(client, kind, archive_id)
        hit = self._entries.get(key)
        if hit is not None and hit[0] > self._clock():
            return cast(T, hit[1])
        task = self._loading.get(key)
        if task is None:
            task = asyncio.ensure_future(self._load(key, load, self._ttl if ttl is None else ttl))
            self._loading[key] = task
        # Shielded: one caller going away does not cancel the read the others wait on.
        return cast(T, await asyncio.shield(task))

    @staticmethod
    def _key(client: BambuddyClient, kind: str, archive_id: int) -> tuple[str, str, int]:
        return (client.config.base_url, kind, archive_id)

    async def _load(
        self, key: tuple[str, str, int], load: Callable[[], Awaitable[T]], ttl: float
    ) -> T:
        task = asyncio.current_task()
        try:
            value = await load()
        finally:
            owned = self._loading.get(key) is task
            if owned:
                del self._loading[key]
        if owned:
            self._put(key, value, ttl)
        return value

    def _put(self, key: tuple[str, str, int], value: object, ttl: float) -> None:
        now = self._clock()
        self._entries.pop(key, None)
        if len(self._entries) >= self._max_entries:
            for stale in [k for k, (expires, _) in self._entries.items() if expires <= now]:
                del self._entries[stale]
        while len(self._entries) >= self._max_entries:
            # Oldest first: a dict keeps insertion order, and a refresh re-inserts.
            del self._entries[next(iter(self._entries))]
        self._entries[key] = (now + ttl, value)
