"""The Library page's search index (#3630) as a component (`core/components.py`); its
run stops the background reads at shutdown."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.library_search.index import LibraryIndex

LIBRARY_INDEX: Key[LibraryIndex] = Key("library_index")


def _build(core: Core, components: Components) -> LibraryIndex:
    return LibraryIndex()


@asynccontextmanager
async def _run(index: LibraryIndex) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await index.close()


COMPONENT = Component(LIBRARY_INDEX, build=_build, run=_run)

LibraryIndexDep = Annotated[LibraryIndex, component_dep(LIBRARY_INDEX)]
