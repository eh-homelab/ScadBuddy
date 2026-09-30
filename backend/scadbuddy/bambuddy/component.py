"""Bambuddy's archive reads, kept 30 s for the prints API (#308), as a component
(`core/components.py`)."""

from __future__ import annotations

from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.bambuddy.archive_cache import ArchiveCache
from scadbuddy.core.components import Component, Components, Core, Key

ARCHIVE_CACHE: Key[ArchiveCache] = Key("archive_cache")


def _build(core: Core, components: Components) -> ArchiveCache:
    return ArchiveCache()


COMPONENT = Component(ARCHIVE_CACHE, build=_build)

ArchiveCacheDep = Annotated[ArchiveCache, component_dep(ARCHIVE_CACHE)]
