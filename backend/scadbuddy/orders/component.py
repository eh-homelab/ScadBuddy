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
from scadbuddy.flow_triggers.store import TriggerStore
from scadbuddy.flows.component import FLOWS
from scadbuddy.flows.definitions import FlowDefinitions
from scadbuddy.library.settings_store import settings_kek
from scadbuddy.orders.archives import mirror_run
from scadbuddy.orders.builtin_flow import ensure_builtin_flow
from scadbuddy.orders.density import DensityTable, density_table
from scadbuddy.orders.integrations import OrderIntegrations, choose
from scadbuddy.orders.lifecycle import Lifecycle
from scadbuddy.orders.machine_rate import MachineRates, MachineRateStore
from scadbuddy.orders.money import OrderRules
from scadbuddy.orders.pricebook import PriceBookReader
from scadbuddy.orders.printing import listen_for_prints
from scadbuddy.orders.store import OrderStore
from scadbuddy.orders.storefront.listings import ListingStore
from scadbuddy.orders.storefront.mail import MAIL_SETTINGS, MailConfig, StorefrontMail
from scadbuddy.payments.registry import providers
from scadbuddy.printers.component import PRINTERS
from scadbuddy.rows.component import ROWS
from scadbuddy.store.refs import BlobRefs

#: The settings ``OrdersComponent.rules`` reads (#5281, #6133).
ORDER_RULE_SETTINGS = frozenset(
    {
        "orders_currency",
        "orders_tax_percent",
        "orders_tax_shipping",
        "orders_purchase_fee_flat_minor",
        "orders_purchase_fee_percent",
        "orders_deposit_policy",
        "storefront_shipping_options",
    }
)


class OrdersComponent:
    def __init__(
        self,
        core: Core,
        pool: ConnectionPool[Connection[DictRow]],
        components: Components | None = None,
        flows: FlowDefinitions | None = None,
    ) -> None:
        self.core = core
        #: Read when a machine rate needs the printer registry (#4625).
        self._components = components
        #: Where the built-in `order-fulfilment` flow is registered at start (#4252).
        self.flows = flows
        self._store: OrderStore | None = None
        self._lifecycle: Lifecycle | None = None
        self._listings: ListingStore | None = None
        self._mail: StorefrontMail | None = None
        self._integrations: tuple[str | None, OrderIntegrations] | None = None
        #: The slice quotes' holds on their sliced files (#4250 Ruling 3), on the
        #: projection's autocommit pool as every `blob_refs` writer's.
        self.refs = BlobRefs(core.projection.pool)
        #: The rows' shared pool (`rows/component.py`), which `ROWS` closes.
        self._pool = pool

    def _opened(self) -> ConnectionPool[Connection[DictRow]]:
        """The shared pool, opened on first use."""
        if self._store is None:
            self._pool.open(wait=False)
            self._store = OrderStore(self._pool)
        return self._pool

    @property
    def store(self) -> OrderStore:
        self._opened()
        assert self._store is not None
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

    def densities(self) -> DensityTable | None:
        """The image's vendor presets by id (#6136, `orders/density.py`), built once per
        process; ``None`` without the image's tree. Blocking: call it in a thread."""
        return density_table(self.core.settings.slicer_profiles_dir)

    @property
    def price_book(self) -> PriceBookReader:
        """The book in force (#4250): the stored policy and the filament records."""
        return PriceBookReader(
            self.core.settings_store,
            self.store,
            spool_import=self.integrations.linked,
        )

    def rules(self) -> OrderRules:
        """The shop's money rules (#5281) as stored now: the lifecycle reads them on each
        use, so a change applies to the next transition. One query on their seven rows
        (``SettingsStore.read``), never the whole snapshot or a secret; a transition reads
        them once."""
        values = self.core.settings_store.read(ORDER_RULE_SETTINGS)
        return OrderRules(
            shop_currency=values["orders_currency"],
            tax_percent=Decimal(values["orders_tax_percent"]),
            tax_shipping=values["orders_tax_shipping"],
            deposit_policy=tuple(values["orders_deposit_policy"] or ()),
            shipping_options=tuple(values["storefront_shipping_options"] or ()),
            purchase_fee_flat_minor=values["orders_purchase_fee_flat_minor"],
            purchase_fee_percent=Decimal(values["orders_purchase_fee_percent"]),
        )

    def mail_config(self) -> MailConfig:
        """The storefront's SMTP settings as stored now (#5283): one query on their rows,
        read by each send and each transition that would mail."""
        values = self.core.settings_store.read(MAIL_SETTINGS)
        return MailConfig(
            host=values["storefront_mail_host"],
            port=values["storefront_mail_port"],
            user=values["storefront_mail_user"],
            sender=values["storefront_mail_sender"],
            tls=values["storefront_mail_tls"],
        )

    @property
    def mail(self) -> StorefrontMail:
        """The storefront's mail (#5283): the outbox's sender and the sealed SMTP
        password. Its links use the env-only ``storefront_public_url``."""
        if self._mail is None:
            self._mail = StorefrontMail(
                self.store,
                settings_kek(self.core.settings.secret_key_file),
                self.mail_config,
                lambda: self.core.settings.storefront_public_url,
            )
        return self._mail

    @property
    def machine_rates(self) -> MachineRates:
        """Each registry printer's machine rate (#4625): its inputs, and Bambuddy's
        maintenance intervals for one linked to it."""
        assert self._components is not None
        return MachineRates(
            MachineRateStore(self._opened()),
            self._components.get(PRINTERS).store,
            self.integrations,
            self.core.settings_store,
        )

    @property
    def lifecycle(self) -> Lifecycle:
        if self._lifecycle is None:
            self._lifecycle = Lifecycle(self.store, self.core.events, self.refs, self.rules)
            self._lifecycle.offered_providers = frozenset(
                providers(fake=self.core.settings.payments_fake_provider)
            )
            self._lifecycle.mail_ready = lambda: self.mail_config().ready
            self._lifecycle.on_mail = self.mail.kick
            self._lifecycle.kek = settings_kek(self.core.settings.secret_key_file)
        return self._lifecycle


ORDERS: Key[OrdersComponent] = Key("orders")


def _build(core: Core, components: Components) -> OrdersComponent:
    # Flows is built first, so it runs first: its repository is ready when this one
    # starts.
    return OrdersComponent(
        core, components.get(ROWS).pool, components, components.get(FLOWS).definitions
    )


@asynccontextmanager
async def _run(orders: OrdersComponent) -> AsyncIterator[None]:
    # §7.2: the built-in flow, when its name never had a version or its newest is an
    # unedited older built-in (#4673). Beside the start,
    # so a slow flows repository delays nothing (review of #5682).
    registering: asyncio.Task[bool] | None = None
    if orders.flows is not None and orders.core.settings.orders_builtin_flow:
        registering = asyncio.create_task(
            # #4673: its triggers, on the pool `FLOW_TRIGGERS` uses, move with an upgrade.
            ensure_builtin_flow(orders.flows, TriggerStore(orders.core.projection.pool)),
            name="register built-in flow",
        )
    # The print path's follow (#4719): an item follows the print its attempt started.
    # §8.2: a settled print's attempt copies its Bambuddy archives.
    # #5283: a commit that queued storefront mail sends it on this loop, off the request.
    orders.mail.bind(asyncio.get_running_loop())
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
        await orders.mail.stop()


COMPONENT = Component(ORDERS, build=_build, run=_run)

OrdersDep = Annotated[OrdersComponent, component_dep(ORDERS)]
