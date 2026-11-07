"""`PRINT_LIBRARY`: which library the print routes serve, and ScadBuddy's own (plan
2026-10-17-native-print-library, Task 3, Ruling 15).

Building it does no I/O: `Components.build_all` runs before the lifespan opens the
settings pool (`api/deps.py` `build_state`), and `AppState.store` is set only after
(`main.py` `lifespan`). So `backend` is read from the settings on first use and cached
(`library_backend` applies on restart), and `native` is built on first use too."""

from __future__ import annotations

import asyncio
from typing import Annotated

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.config import LibraryBackend
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.store import NativeLibraryStore
from scadbuddy.rows.component import ROWS


class PrintLibrary:
    def __init__(self, core: Core, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self.core = core
        self._backend: LibraryBackend | None = None
        self._native: NativeLibrary | None = None
        #: The rows' shared pool (`rows/component.py`, #5226), which `ROWS` closes;
        #: opened here only when `native` is first built.
        self._pool = pool

    @property
    def backend(self) -> LibraryBackend:
        if self._backend is None:
            self._backend = self.core.settings_store.load().library_backend
        return self._backend

    @property
    def native(self) -> NativeLibrary:
        """ScadBuddy's own library. A caller checks `backend` first: on ``bambuddy`` this
        raises `RuntimeError`."""
        if self.backend != "native":
            raise RuntimeError(f"library_backend is {self.backend}, not native")
        if self._native is None:
            content = self.core.store.content
            if content is None:  # the start refuses native over the local store
                raise RuntimeError("library_backend is native, but the store has no content")
            self._pool.open(wait=False)
            self._native = NativeLibrary(NativeLibraryStore(self._pool), content, content.spool)
        return self._native

    async def serving_native(self) -> NativeLibrary | None:
        """The native library when it serves the routes, else None (``bambuddy``). The
        first read of `backend` is a blocking settings read, so it runs off the event
        loop; later ones are the cached value."""
        backend = self._backend
        if backend is None:
            backend = await asyncio.to_thread(lambda: self.backend)
        return self.native if backend == "native" else None


PRINT_LIBRARY: Key[PrintLibrary] = Key("print_library")


def _build(core: Core, components: Components) -> PrintLibrary:
    return PrintLibrary(core, components.get(ROWS).pool)


COMPONENT = Component(PRINT_LIBRARY, build=_build)

PrintLibraryDep = Annotated[PrintLibrary, component_dep(PRINT_LIBRARY)]
