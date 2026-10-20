"""`ORDERS`: the orders feature's service (#4249), discovered as every component is.

Building it does no I/O: the pool opens on first use, as `print_library/component.py`'s
does, since `Components.build_all` runs before the lifespan opens anything. The sweep of
uploads reads every item's inputs through `store` (``main.sweep_assets``); the routes
reach `store` and `lifecycle` (every transition, publishing on the core's bus) through
`OrdersDep`.
"""

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
from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE
from scadbuddy.orders.lifecycle import Lifecycle
from scadbuddy.orders.store import OrderStore

#: The rows' own pool: an order's write is one short transaction under its row lock.
ROW_POOL_SIZE = 4
#: As `print_library/component.py`: an unreachable database fails a request within
#: about this, never hangs it.
CONNECT_TIMEOUT = 5.0


class OrdersComponent:
    def __init__(self, core: Core) -> None:
        self.core = core
        self._store: OrderStore | None = None
        self._lifecycle: Lifecycle | None = None
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
            name="scadbuddy-orders",
        )

    @property
    def store(self) -> OrderStore:
        if self._store is None:
            self._pool.open(wait=False)
            self._store = OrderStore(self._pool)
        return self._store

    @property
    def lifecycle(self) -> Lifecycle:
        if self._lifecycle is None:
            self._lifecycle = Lifecycle(self.store, self.core.events)
        return self._lifecycle

    def close(self) -> None:
        self._pool.close()


ORDERS: Key[OrdersComponent] = Key("orders")


def _build(core: Core, components: Components) -> OrdersComponent:
    return OrdersComponent(core)


@asynccontextmanager
async def _run(orders: OrdersComponent) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await asyncio.to_thread(orders.close)


COMPONENT = Component(ORDERS, build=_build, run=_run)

OrdersDep = Annotated[OrdersComponent, component_dep(ORDERS)]
