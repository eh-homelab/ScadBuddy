"""Bambuddy's archive reads, kept 30 s for the prints API (#308), as a component
(`core/components.py`). Its run also stops the preset catalogue's background
refreshes at shutdown (#3446)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.bambuddy.archive_cache import ArchiveCache
from scadbuddy.bambuddy.catalogue import stop_refreshes
from scadbuddy.core.components import Component, Components, Core, Key

ARCHIVE_CACHE: Key[ArchiveCache] = Key("archive_cache")


def _build(core: Core, components: Components) -> ArchiveCache:
    return ArchiveCache()


@asynccontextmanager
async def _run(cache: ArchiveCache) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await stop_refreshes()


COMPONENT = Component(ARCHIVE_CACHE, build=_build, run=_run)

ArchiveCacheDep = Annotated[ArchiveCache, component_dep(ARCHIVE_CACHE)]
