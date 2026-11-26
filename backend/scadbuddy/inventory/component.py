"""`INVENTORY`: the inventory's client of scadbuddy-core (#6949 K1: the kinds, the items and
the stock), and the offers and stock stores Python still runs (#7067) over the shared rows
pool (`rows/component.py`), opened on first use as the entities' stores are. Only the API
process builds it (#5295)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.api.components import component_dep
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.service_credentials import InProcessProvider, ServiceAuth
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.entities.registry import EntityStore
from scadbuddy.inventory.core_client import InventoryCore
from scadbuddy.inventory.offers import OfferStore
from scadbuddy.inventory.part_alias import PartAliases
from scadbuddy.inventory.stock import StockStore
from scadbuddy.rows.component import ROWS


class InventoryComponent:
    def __init__(
        self,
        pool: ConnectionPool[Connection[DictRow]],
        shop_currency: Callable[[], str],
        core: InventoryCore,
    ) -> None:
        self.pool = pool
        #: Go's kinds, items and stock: reads over its CRUD routes, writes as its
        #: activities (`core_client.py`).
        self.core = core
        entities = EntityStore(pool)
        self._offers = OfferStore(pool, shop_currency)
        self._stock = StockStore(pool, entities, shop_currency)
        self._parts = PartAliases(pool, entities, self._stock, shop_currency)

    def opened(self) -> ConnectionPool[Connection[DictRow]]:
        """The shared pool, opened on first use (an open pool ignores it)."""
        self.pool.open(wait=False)
        return self.pool

    @property
    def offers(self) -> OfferStore:
        self.opened()
        return self._offers

    @property
    def stock(self) -> StockStore:
        """Python's stock writes for the orders, until they move to Go (Task 16)."""
        self.opened()
        return self._stock

    @property
    def parts(self) -> PartAliases:
        """The `/parts` aliases (#7067 Task 9)."""
        self.opened()
        return self._parts


INVENTORY: Key[InventoryComponent] = Key("inventory")


def _build(core: Core, components: Components) -> InventoryComponent:
    auth = components.get(AUTH)

    def shop_currency() -> str:
        # A settings read on a connection of its own, on each use (as the filament's).
        return core.settings_store.snapshot().runtime.orders_currency

    settings = core.settings
    inventory = InventoryCore(
        url=settings.core_crud_url,
        # The API's lazy client (`api/deps.py`): built here, it connects on its first call.
        temporal=core.render.client,
        queue=settings.temporal_task_queue_inventory,
        # R-K1-3: minted in this process, with `proof = inprocess`.
        auth=ServiceAuth(InProcessProvider("inventory", lambda: auth.credentials)),
    )
    return InventoryComponent(components.get(ROWS).pool, shop_currency, inventory)


@asynccontextmanager
async def _run(component: InventoryComponent) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await component.core.aclose()


COMPONENT = Component(INVENTORY, build=_build, run=_run)

InventoryDep = Annotated[InventoryComponent, component_dep(INVENTORY)]
