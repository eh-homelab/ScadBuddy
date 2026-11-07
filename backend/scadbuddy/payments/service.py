"""The payments service (#4957): what the routes call. It opens a request through the
lifecycle, then asks the provider for its hosted page after the commit (plan gap 9), so
a slow or failed provider never holds the order's lock; it reads a provider's webhook;
and it connects the shop's account at a provider. No exception, log line or answer here
carries a secret."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.models import Actor, Money
from scadbuddy.orders.store import UNSET, UnsetType
from scadbuddy.payments import store as payments
from scadbuddy.payments.credentials import (
    CredentialUnreadableError,
    KekMissingError,
    PaymentCredentials,
    ProviderNotConnectedError,
)
from scadbuddy.payments.models import (
    EventOutcome,
    HostedRequest,
    PaymentRequestKind,
    PaymentRequestView,
)
from scadbuddy.payments.provider import (
    BadSignatureError,
    Credential,
    PaymentProvider,
    ProviderError,
    ProviderField,
)

if TYPE_CHECKING:
    from scadbuddy.orders.component import OrdersComponent

__all__ = [
    "BadSignatureError",
    "CredentialUnreadableError",
    "FieldError",
    "PaymentRequestCreate",
    "Payments",
    "ProviderConnect",
    "ProviderNotFoundError",
    "ProviderStatus",
    "WebhookAnswer",
]

logger = logging.getLogger(__name__)

#: The longest value a connect form field takes.
MAX_FIELD_CHARS = 2000

FieldValue = Annotated[str, Field(max_length=MAX_FIELD_CHARS)]


class ProviderNotFoundError(LookupError):
    """The provider is not offered here, or the shop has not connected it."""

    def __init__(self, provider: str) -> None:
        super().__init__(f"no connected payment provider {provider!r}")
        self.provider = provider


class FieldError(ValueError):
    """A connect body that names a field the provider does not declare, or leaves one
    out. ``loc`` is the field's place in the body."""

    def __init__(self, loc: tuple[str, str], message: str) -> None:
        super().__init__(message)
        self.loc = loc


class ProviderStatus(BaseModel):
    id: str
    label: str
    fields: list[ProviderField]
    connected: bool
    #: What the provider calls the shop's account ("Connected as ...").
    account_label: str | None
    is_default: bool
    connected_at: datetime | None
    last_webhook_at: datetime | None


class ProviderConnect(BaseModel):
    """The connect form: each field the provider declares, the secret ones in
    ``secrets``. The answer never repeats a secret."""

    model_config = ConfigDict(extra="forbid")

    settings: dict[str, FieldValue]
    secrets: dict[str, FieldValue]


class PaymentRequestCreate(BaseModel):
    """A request for ``amount_minor`` (the balance now when left out), through
    ``provider`` (the default provider when left out; null collects by hand)."""

    model_config = ConfigDict(extra="forbid")

    kind: PaymentRequestKind = "balance"
    amount_minor: Money | None = Field(default=None, ge=1)
    provider: str | UnsetType | None = UNSET


class WebhookAnswer(BaseModel):
    outcome: EventOutcome


class Payments:
    def __init__(
        self,
        orders: OrdersComponent,
        credentials: PaymentCredentials,
        registry: Mapping[str, PaymentProvider],
        public_url: Callable[[], str | None],
    ) -> None:
        self._orders = orders
        self._credentials = credentials
        self._registry = registry
        self._public_url = public_url

    # -- requests ---------------------------------------------------------------------

    def _is_connected(self, provider: str) -> bool:
        return provider in self._registry and any(
            row.provider == provider for row in self._credentials.rows()
        )

    async def request_payment(
        self, order_id: str, by: Actor, body: PaymentRequestCreate
    ) -> PaymentRequestView:
        """Open the request, then link it. A named provider that is not offered or not
        connected is refused before any row is written."""
        provider = body.provider
        if isinstance(provider, str) and not await asyncio.to_thread(self._is_connected, provider):
            raise ProviderNotFoundError(provider)
        opened = await asyncio.to_thread(
            lambda: self._orders.lifecycle.open_payment_request(
                order_id, by, kind=body.kind, amount_minor=body.amount_minor, provider=provider
            )
        )
        return await self.link(opened.id)

    def _request(self, request_id: str) -> PaymentRequestView:
        with self._orders.store.transaction() as conn:
            found = payments.request(conn, request_id)
        if found is None:
            raise LifecycleError(
                "payment_request_not_found", f"no payment request {request_id}", not_found=True
            )
        return found

    def _return_url(self, order_id: str) -> str | None:
        public = self._public_url()
        return f"{public.rstrip('/')}/orders/{order_id}" if public else None

    async def link(self, request_id: str) -> PaymentRequestView:
        """Ask the request's provider for its hosted page, with the request id as the
        idempotency key. A request with no provider, one already linked and one no
        longer open are returned as they are. A failure is kept as ``link_error``."""
        found = await asyncio.to_thread(self._request, request_id)
        if found.provider is None or found.hosted_url is not None or found.status != "open":
            return found
        hosted: HostedRequest | None = None
        error: str | None = None
        try:
            adapter, credential = await asyncio.to_thread(self._connected, found.provider)
            hosted = await adapter.create_request(
                credential,
                found,
                return_url=self._return_url(found.order_id),
                idempotency_key=found.id,
            )
        except (
            ProviderNotFoundError,
            ProviderError,
            KekMissingError,
            CredentialUnreadableError,
        ) as failed:
            # Each message names the provider or the provider's own safe text only.
            error = str(failed)
            logger.warning("payment request %s was not linked: %s", found.id, error)
        return await asyncio.to_thread(
            lambda: self._orders.lifecycle.set_request_link(found.id, hosted=hosted, error=error)
        )

    async def link_pending(self, order_id: str) -> list[PaymentRequestView]:
        """Link every open request of the order that has a provider and no page yet."""

        def pending() -> list[PaymentRequestView]:
            with self._orders.store.transaction() as conn:
                found = payments.requests_for_order(conn, order_id)
            return [
                r
                for r in found
                if r.status == "open" and r.provider is not None and r.hosted_url is None
            ]

        return [await self.link(r.id) for r in await asyncio.to_thread(pending)]

    # -- webhooks ---------------------------------------------------------------------

    def _connected(self, provider: str) -> tuple[PaymentProvider, Credential]:
        adapter = self._registry.get(provider)
        if adapter is None:
            raise ProviderNotFoundError(provider)
        credential = self._credentials.credential(provider)
        if credential is None:
            raise ProviderNotFoundError(provider)
        return adapter, credential

    async def handle_webhook(
        self, provider: str, headers: Mapping[str, str], body: bytes
    ) -> EventOutcome:
        """Check the signature, then record the event. ``BadSignatureError`` leaves
        before anything is written; ``ProviderError`` is a signed body it cannot read."""
        adapter, credential = await asyncio.to_thread(self._connected, provider)
        event = adapter.parse_webhook(credential, headers, body)
        await asyncio.to_thread(self._credentials.touch_webhook, provider)
        outcome = await asyncio.to_thread(
            self._orders.lifecycle.record_provider_event, provider, event
        )
        logger.info("payment event from %s (%s): %s", provider, event.kind, outcome)
        return outcome

    # -- the shop's accounts ----------------------------------------------------------

    async def connect(self, provider: str, body: ProviderConnect) -> ProviderStatus:
        """Check the fields, refuse without a KEK, let the provider verify the account,
        then seal and store it."""
        adapter = self._registry.get(provider)
        if adapter is None:
            raise ProviderNotFoundError(provider)
        for part, given in (("settings", body.settings), ("secrets", body.secrets)):
            declared = {f.name for f in adapter.fields if f.secret == (part == "secrets")}
            if extra := sorted(set(given) - declared):
                raise FieldError((part, extra[0]), f"{provider} takes no {part} field {extra[0]!r}")
            if missing := sorted(declared - set(given)):
                raise FieldError(
                    (part, missing[0]), f"{provider} needs the {part} field {missing[0]!r}"
                )
        if not self._credentials.can_seal:
            raise KekMissingError()
        account_label = await adapter.verify(
            Credential(settings=dict(body.settings), secrets=dict(body.secrets))
        )
        await asyncio.to_thread(
            lambda: self._credentials.connect(
                provider,
                settings=body.settings,
                secrets=body.secrets,
                account_label=account_label,
            )
        )
        logger.info("payment provider %s connected", provider)
        found = await asyncio.to_thread(self.statuses)
        return next(s for s in found if s.id == provider)

    def disconnect(self, provider: str) -> None:
        """``ProviderBusyError`` while an open request names it."""
        if not self._credentials.disconnect(provider):
            raise ProviderNotFoundError(provider)
        logger.info("payment provider %s disconnected", provider)

    def set_default(self, provider: str) -> None:
        if provider not in self._registry:
            raise ProviderNotFoundError(provider)
        try:
            self._credentials.set_default(provider)
        except ProviderNotConnectedError:
            raise ProviderNotFoundError(provider) from None

    def statuses(self) -> list[ProviderStatus]:
        """Every provider offered, then any connected one no longer offered (so the
        shop can still disconnect it)."""
        rows = {row.provider: row for row in self._credentials.rows()}
        found: list[ProviderStatus] = []
        for provider_id in [*self._registry, *(p for p in rows if p not in self._registry)]:
            adapter = self._registry.get(provider_id)
            row = rows.get(provider_id)
            found.append(
                ProviderStatus(
                    id=provider_id,
                    label=provider_id if adapter is None else adapter.label,
                    fields=[] if adapter is None else list(adapter.fields),
                    connected=row is not None,
                    account_label=None if row is None else row.account_label,
                    is_default=row is not None and row.is_default,
                    connected_at=None if row is None else row.connected_at,
                    last_webhook_at=None if row is None else row.last_webhook_at,
                )
            )
        return found
