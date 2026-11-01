"""`ORDERS`: the orders feature's service (#4249), discovered as every component is.

Building it does no I/O: the pool opens on first use, as `print_library/component.py`'s
does, since `Components.build_all` runs before the lifespan opens anything. The sweep of
uploads reads every item's inputs through `store` (``main.sweep_assets``); the routes
reach `store` and `lifecycle` (every transition, publishing on the core's bus) through
`OrdersDep`. ``refs`` holds a slice quote's sliced files (#4250), which the lifecycle
releases as §3.4 says. Running, it follows every print an item's attempt started
(``printing.py`` ``listen_for_prints``, #4719). ``integrations`` is the Bambuddy seam (#4253, §8): chosen for
the running ``bambuddy_url``, and chosen again when that changes, which ``api/runtime.py``
applies on every ``settings.changed``, so no restart is needed to light it up.
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
from scadbuddy.orders.integrations import OrderIntegrations, choose
from scadbuddy.orders.lifecycle import Lifecycle
from scadbuddy.orders.pricebook import PriceBookReader
from scadbuddy.orders.printing import listen_for_prints
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs

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
        self._integrations: tuple[str | None, OrderIntegrations] | None = None
        #: The slice quotes' holds on their sliced files (#4250 Ruling 3), on the
        #: projection's autocommit pool as every `blob_refs` writer's.
        self.refs = BlobRefs(core.projection.pool)
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
    def integrations(self) -> OrderIntegrations:
        """The seam for the running ``bambuddy_url`` (F10, Ruling 7)."""
        url = self.core.settings.bambuddy_url
        if self._integrations is None or self._integrations[0] != url:
            self._integrations = (url, choose(url, self.core.settings_store))
        return self._integrations[1]

    @property
    def price_book(self) -> PriceBookReader:
        """The book in force (#4250): the stored policy and the filament records."""
        return PriceBookReader(
            self.core.settings_store, self.store, spool_import=self.integrations.linked
        )

    @property
    def lifecycle(self) -> Lifecycle:
        if self._lifecycle is None:
            self._lifecycle = Lifecycle(self.store, self.core.events, self.refs)
        return self._lifecycle

    def close(self) -> None:
        self._pool.close()


ORDERS: Key[OrdersComponent] = Key("orders")


def _build(core: Core, components: Components) -> OrdersComponent:
    return OrdersComponent(core)


@asynccontextmanager
async def _run(orders: OrdersComponent) -> AsyncIterator[None]:
    # The print path's follow (#4719): an item follows the print its attempt started.
    remove = listen_for_prints(orders.core.events, orders.lifecycle)
    try:
        yield
    finally:
        await remove()
        await asyncio.to_thread(orders.close)


COMPONENT = Component(ORDERS, build=_build, run=_run)

OrdersDep = Annotated[OrdersComponent, component_dep(ORDERS)]
