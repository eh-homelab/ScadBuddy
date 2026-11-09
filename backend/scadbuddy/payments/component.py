"""`PAYMENTS`: the payments service (#4957), discovered as every component is.

It reads `ORDERS`: the requests, events and credentials are rows the orders pool reads
and writes (CLAUDE.md "Database access"). Building it does no I/O: the service is made
on first use, as `orders/component.py`'s store is. The fake provider is offered only
when ``SCADBUDDY_PAYMENTS_FAKE_PROVIDER`` is true (plan gap 14).
"""

from __future__ import annotations

from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.library.settings_store import settings_kek
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.payments.bank import BankLinks
from scadbuddy.payments.credentials import PaymentCredentials
from scadbuddy.payments.registry import providers
from scadbuddy.payments.service import Payments


class PaymentsComponent:
    def __init__(self, core: Core, orders: OrdersComponent) -> None:
        self.core = core
        self.orders = orders
        self._service: Payments | None = None
        self._bank: BankLinks | None = None

    @property
    def service(self) -> Payments:
        if self._service is None:
            settings = self.core.settings
            self._service = Payments(
                self.orders,
                PaymentCredentials(self.orders.store, settings_kek(settings.secret_key_file)),
                providers(fake=settings.payments_fake_provider),
                # Live (#322): read on each call.
                lambda: self.core.settings.public_url,
                lambda: self.core.settings.storefront_public_url,
            )
        return self._service

    @property
    def bank(self) -> BankLinks:
        """The shop's bank account linked through Plaid (#4961): beside the seam, not a
        provider."""
        if self._bank is None:
            kek = settings_kek(self.core.settings.secret_key_file)
            self._bank = BankLinks(self.orders.store, kek)
        return self._bank


PAYMENTS: Key[PaymentsComponent] = Key("payments")


def _build(core: Core, components: Components) -> PaymentsComponent:
    return PaymentsComponent(core, components.get(ORDERS))


COMPONENT = Component(PAYMENTS, build=_build)

PaymentsDep = Annotated[PaymentsComponent, component_dep(PAYMENTS)]
