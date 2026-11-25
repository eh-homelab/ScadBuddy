"""`INVENTORY`: the kinds, items, offers and stock stores (#7067) over the shared rows pool
(`rows/component.py`), opened on first use as the entities' stores are. Only the API process
builds it (#5295)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.entities.registry import EntityStore
from scadbuddy.inventory.items import ItemStore
from scadbuddy.inventory.kinds import KindStore
from scadbuddy.inventory.offers import OfferStore
from scadbuddy.inventory.part_alias import PartAliases
from scadbuddy.inventory.stock import StockStore
from scadbuddy.rows.component import ROWS


class InventoryComponent:
    def __init__(
        self, pool: ConnectionPool[Connection[DictRow]], shop_currency: Callable[[], str]
    ) -> None:
        self.pool = pool
        entities = EntityStore(pool)
        self._kinds = KindStore(pool)
        self._items = ItemStore(pool, entities)
        self._offers = OfferStore(pool, shop_currency)
        self._stock = StockStore(pool, entities, shop_currency)
        self._parts = PartAliases(pool, entities, self._stock, shop_currency)

    def opened(self) -> ConnectionPool[Connection[DictRow]]:
        """The shared pool, opened on first use (an open pool ignores it)."""
        self.pool.open(wait=False)
        return self.pool

    @property
    def kinds(self) -> KindStore:
        self.opened()
        return self._kinds

    @property
    def items(self) -> ItemStore:
        self.opened()
        return self._items

    @property
    def offers(self) -> OfferStore:
        self.opened()
        return self._offers

    @property
    def stock(self) -> StockStore:
        self.opened()
        return self._stock

    @property
    def parts(self) -> PartAliases:
        """The `/parts` aliases (#7067 Task 9)."""
        self.opened()
        return self._parts


INVENTORY: Key[InventoryComponent] = Key("inventory")


def _build(core: Core, components: Components) -> InventoryComponent:
    def shop_currency() -> str:
        # A settings read on a connection of its own, on each use (as the filament's).
        return core.settings_store.snapshot().runtime.orders_currency

    return InventoryComponent(components.get(ROWS).pool, shop_currency)


COMPONENT = Component(INVENTORY, build=_build)

InventoryDep = Annotated[InventoryComponent, component_dep(INVENTORY)]
