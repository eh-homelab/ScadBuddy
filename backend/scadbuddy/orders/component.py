"""`ORDERS`: the orders feature's service (#4249), discovered as every component is.

Building it does no I/O: the shared rows pool (`rows/component.py`, #5226) opens on
first use, as it does for `print_library/component.py`, since `Components.build_all`
runs before the lifespan opens anything. The sweep of uploads reads every item's
inputs through `store` (``main.sweep_assets``); the routes reach `store`,
`lifecycle` (every transition, publishing on the core's bus) and the storefront's
`listings` (#5282) through `OrdersDep`.
``refs`` holds a slice quote's sliced files (#4250), which the lifecycle releases as
§3.4 says. Running, it follows every print an item's attempt started
(``printing.py`` ``listen_for_prints``, #4719). ``integrations`` is the Bambuddy seam
(#4253, §8): chosen for the running ``bambuddy_url``, and chosen again when that
changes, which ``api/runtime.py`` applies on every ``settings.changed``, so no restart
is needed to light it up.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Annotated

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.flows.component import FLOWS
from scadbuddy.flows.definitions import FlowDefinitions
from scadbuddy.orders.archives import mirror_run
from scadbuddy.orders.builtin_flow import ensure_builtin_flow
from scadbuddy.orders.integrations import OrderIntegrations, choose
from scadbuddy.orders.lifecycle import Lifecycle
from scadbuddy.orders.money import OrderRules
from scadbuddy.orders.pricebook import PriceBookReader
from scadbuddy.orders.printing import listen_for_prints
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.listings import ListingStore
from scadbuddy.payments.registry import providers
from scadbuddy.rows.component import ROWS
from scadbuddy.store.refs import BlobRefs

#: The settings ``OrdersComponent.rules`` reads (#5281).
ORDER_RULE_SETTINGS = frozenset(
    {
        "orders_currency",
        "orders_tax_percent",
        "orders_tax_shipping",
        "orders_deposit_policy",
        "storefront_shipping_options",
    }
)


class OrdersComponent:
    def __init__(
        self,
        core: Core,
        pool: ConnectionPool[Connection[DictRow]],
        flows: FlowDefinitions | None = None,
    ) -> None:
        self.core = core
        #: Where the built-in `order-fulfilment` flow is registered at start (#4252).
        self.flows = flows
        self._store: OrderStore | None = None
        self._lifecycle: Lifecycle | None = None
        self._listings: ListingStore | None = None
        self._integrations: tuple[str | None, OrderIntegrations] | None = None
        #: The slice quotes' holds on their sliced files (#4250 Ruling 3), on the
        #: projection's autocommit pool as every `blob_refs` writer's.
        self.refs = BlobRefs(core.projection.pool)
        #: The rows' shared pool (`rows/component.py`), which `ROWS` closes.
        self._pool = pool

    @property
    def store(self) -> OrderStore:
        if self._store is None:
            self._pool.open(wait=False)
            self._store = OrderStore(self._pool)
        return self._store

    @property
    def listings(self) -> ListingStore:
        """The storefront's listings (#5282), on the rows' pool."""
        if self._listings is None:
            self._pool.open(wait=False)
            self._listings = ListingStore(self._pool)
        return self._listings

    @property
    def integrations(self) -> OrderIntegrations:
        """The seam for the running ``bambuddy_url`` (F10, Ruling 7)."""
        url = self.core.settings.bambuddy_url
        if self._integrations is None or self._integrations[0] != url:
            self._integrations = (
                url,
                choose(url, self.core.settings_store, self.core.uploads),
            )
        return self._integrations[1]

    @property
    def price_book(self) -> PriceBookReader:
        """The book in force (#4250): the stored policy and the filament records."""
        return PriceBookReader(
            self.core.settings_store, self.store, spool_import=self.integrations.linked
        )

    def rules(self) -> OrderRules:
        """The shop's money rules (#5281) as stored now: the lifecycle reads them on each
        use, so a change applies to the next transition. One query on their five rows
        (``SettingsStore.read``), never the whole snapshot or a secret; a transition reads
        them once."""
        values = self.core.settings_store.read(ORDER_RULE_SETTINGS)
        return OrderRules(
            shop_currency=values["orders_currency"],
            tax_percent=Decimal(values["orders_tax_percent"]),
            tax_shipping=values["orders_tax_shipping"],
            deposit_policy=tuple(values["orders_deposit_policy"] or ()),
            shipping_options=tuple(values["storefront_shipping_options"] or ()),
        )

    @property
    def lifecycle(self) -> Lifecycle:
        if self._lifecycle is None:
            self._lifecycle = Lifecycle(self.store, self.core.events, self.refs, self.rules)
            self._lifecycle.offered_providers = frozenset(
                providers(fake=self.core.settings.payments_fake_provider)
            )
        return self._lifecycle


ORDERS: Key[OrdersComponent] = Key("orders")


def _build(core: Core, components: Components) -> OrdersComponent:
    # Flows is built first, so it runs first: its repository is ready when this one
    # starts.
    return OrdersComponent(core, components.get(ROWS).pool, components.get(FLOWS).definitions)


@asynccontextmanager
async def _run(orders: OrdersComponent) -> AsyncIterator[None]:
    # §7.2: the built-in flow, once, when its name never had a version. Beside the start,
    # so a slow flows repository delays nothing (review of #5682).
    registering: asyncio.Task[bool] | None = None
    if orders.flows is not None and orders.core.settings.orders_builtin_flow:
        registering = asyncio.create_task(
            ensure_builtin_flow(orders.flows), name="register built-in flow"
        )
    # The print path's follow (#4719): an item follows the print its attempt started.
    # §8.2: a settled print's attempt copies its Bambuddy archives.
    remove = listen_for_prints(
        orders.core.events,
        orders.lifecycle,
        lambda run_id: mirror_run(orders.lifecycle, orders.integrations, run_id),
    )
    try:
        yield
    finally:
        if registering is not None:
            registering.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await registering
        await remove()


COMPONENT = Component(ORDERS, build=_build, run=_run)

OrdersDep = Annotated[OrdersComponent, component_dep(ORDERS)]
