"""Bambuddy's archive reads, kept briefly in process (#308; print-history plan §2.4).

The prints list reads one archive per print, and a history page, its next page and a
detail opened from it read the same ones again within seconds. Each read is kept for
`ARCHIVE_TTL` seconds, per Bambuddy and archive, and nothing longer: Bambuddy stays
the source of truth, and this is not a second store of its data. A failed read is not
kept, except the 404 of an archive deleted in Bambuddy, which is an answer.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TypeVar, cast

from fastapi import status

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import (
    ArchiveDetail,
    ArchiveRunList,
    TimelapseInfo,
    TimelapseThumbnails,
)
from scadbuddy.core.problems import ApiError

ARCHIVE_TTL = 30.0
#: Enough for every print of a busy homelab several times over.
MAX_ENTRIES = 4096

T = TypeVar("T")


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

    async def runs(self, client: BambuddyClient, archive_id: int) -> ArchiveRunList:
        return await self._get(client, "runs", archive_id, lambda: client.archive_runs(archive_id))

    async def timelapse_info(self, client: BambuddyClient, archive_id: int) -> TimelapseInfo | None:
        """None when Bambuddy cannot describe the video (its ffprobe failed, say):
        the video itself still plays through the proxy."""

        async def load() -> TimelapseInfo | None:
            try:
                return await client.timelapse_info(archive_id)
            except ApiError:
                return None

        return await self._get(client, "timelapse_info", archive_id, load)

    async def timelapse_thumbnails(
        self, client: BambuddyClient, archive_id: int
    ) -> TimelapseThumbnails:
        """No frames when Bambuddy cannot extract them."""

        async def load() -> TimelapseThumbnails:
            try:
                return await client.timelapse_thumbnails(archive_id)
            except ApiError:
                return TimelapseThumbnails()

        return await self._get(client, "timelapse_thumbnails", archive_id, load)

    def forget(self, client: BambuddyClient, archive_id: int) -> None:
        """Drop everything kept for the archive, after ScadBuddy changed it."""
        for key in [
            k for k in self._entries if k[0] == client.config.base_url and k[2] == archive_id
        ]:
            del self._entries[key]

    async def _get(
        self,
        client: BambuddyClient,
        kind: str,
        archive_id: int,
        load: Callable[[], Awaitable[T]],
    ) -> T:
        key = (client.config.base_url, kind, archive_id)
        hit = self._entries.get(key)
        if hit is not None and hit[0] > self._clock():
            return cast(T, hit[1])
        value = await load()
        self._put(key, value)
        return value

    def _put(self, key: tuple[str, str, int], value: object) -> None:
        now = self._clock()
        self._entries.pop(key, None)
        if len(self._entries) >= self._max_entries:
            for stale in [k for k, (expires, _) in self._entries.items() if expires <= now]:
                del self._entries[stale]
        while len(self._entries) >= self._max_entries:
            # Oldest first: a dict keeps insertion order, and a refresh re-inserts.
            del self._entries[next(iter(self._entries))]
        self._entries[key] = (now + self._ttl, value)
