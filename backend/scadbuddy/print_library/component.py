"""`PRINT_LIBRARY`: which library the print routes serve, and ScadBuddy's own (plan
2026-10-17-native-print-library, Task 3, Ruling 15).

Building it does no I/O: `Components.build_all` runs before the lifespan opens the
settings pool (`api/deps.py` `build_state`), and `AppState.store` is set only after
(`main.py` `lifespan`). So `backend` is read from the settings on first use and cached
(`library_backend` applies on restart), and `native` is built on first use too."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.config import LibraryBackend
from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.store import NativeLibraryStore

#: The rows' own pool: a library write is one statement or one short transaction.
ROW_POOL_SIZE = 4
#: As `analyzers/decisions.py` and `rack/usage.py`: an unreachable database fails a
#: request within about this, never hangs it.
CONNECT_TIMEOUT = 5.0


class PrintLibrary:
    def __init__(self, core: Core) -> None:
        self.core = core
        self._backend: LibraryBackend | None = None
        self._native: NativeLibrary | None = None
        # Opened only when `native` is first built; `open=False` connects to nothing.
        self._pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            core.settings.database_url,
            min_size=1,
            max_size=ROW_POOL_SIZE,
            open=False,
            timeout=CONNECT_TIMEOUT,
            connection_class=Connection[DictRow],
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "connect_timeout": max(1, int(CONNECT_TIMEOUT)),
                **TCP_KEEPALIVE,
            },
            name="scadbuddy-print-library",
        )

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

    def close(self) -> None:
        self._pool.close()


PRINT_LIBRARY: Key[PrintLibrary] = Key("print_library")


def _build(core: Core, components: Components) -> PrintLibrary:
    return PrintLibrary(core)


@asynccontextmanager
async def _run(library: PrintLibrary) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await asyncio.to_thread(library.close)


COMPONENT = Component(PRINT_LIBRARY, build=_build, run=_run)

PrintLibraryDep = Annotated[PrintLibrary, component_dep(PRINT_LIBRARY)]
