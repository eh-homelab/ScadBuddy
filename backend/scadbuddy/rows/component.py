"""One pool for the features whose rows are short statements on the API's role (#5226).

Orders, printers, the native print library and the analyzer decisions each kept a pool
of four, which held a connection each whenever it had been used once. They share this
one now. Each kept its pool apart for one reason only: a database that cannot be
reached fails a request within about :data:`CONNECT_TIMEOUT`, never hangs it; this
pool keeps that bound. Two pools stay apart: auth's (every request with a cookie reads
it, so a burst of order writes must not queue a login behind it) and the rack usage's
(``rack/usage.py`` lowers each connection's ``statement_timeout``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.pg_pool import row_pool

#: Short statements and short transactions under a row lock: six cover four features.
POOL_SIZE = 6
#: An unreachable database fails a request within about this, never hangs it.
CONNECT_TIMEOUT = 5.0


class Rows:
    def __init__(self, conninfo: str) -> None:
        #: Unopened: each feature opens it (``open(wait=False)``, which an open pool
        #: ignores) on its first use, as it opened its own, and never closes it.
        self.pool: ConnectionPool[Connection[DictRow]] = row_pool(
            conninfo,
            name="scadbuddy-rows",
            max_size=POOL_SIZE,
            timeout=CONNECT_TIMEOUT,
            connect_timeout=CONNECT_TIMEOUT,
        )

    def close(self) -> None:
        self.pool.close()


ROWS: Key[Rows] = Key("rows")


def _build(core: Core, components: Components) -> Rows:
    return Rows(core.settings.database_url)


@asynccontextmanager
async def _run(rows: Rows) -> AsyncIterator[None]:
    # Built before every component that reads it, so this exits after them.
    try:
        yield
    finally:
        await asyncio.to_thread(rows.close)


COMPONENT = Component(ROWS, build=_build, run=_run)
