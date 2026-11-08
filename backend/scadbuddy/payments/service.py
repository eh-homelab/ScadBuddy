"""The payments service (#4957): what the routes call. It opens a request through the
lifecycle, then asks the provider for its hosted page after the commit (plan gap 9), so
a slow or failed provider never holds the order's lock. In the same way, once a cancel,
an accept, a deposit change or a manual payment closes a request, it asks the provider
to close the request's page (``run_closing``).
It reads a provider's webhook, and it connects the shop's account at a provider. No
exception, log line or answer here carries a secret."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Annotated, cast

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.orders.lifecycle import ENDED, LifecycleError
from scadbuddy.orders.models import Actor, Money, Order, Payment
from scadbuddy.orders.store import UNSET, Conn, UnsetType
from scadbuddy.payments import store as payments
from scadbuddy.payments.credentials import (
    CredentialUnreadableError,
    KekMissingError,
    PaymentCredentials,
    ProviderNotConnectedError,
    WebhookError,
)
from scadbuddy.payments.models import (
    EventOutcome,
    HostedRequest,
    PaymentRequestKind,
    PaymentRequestView,
    ProviderEvent,
)
from scadbuddy.payments.provider import (
    BadSignatureError,
    CapturingProvider,
    Credential,
    EventTooEarlyError,
    PaymentProvider,
    ProviderError,
    ProviderField,
    ProviderUnavailableError,
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


class CurrencyMismatchError(Exception):
    """The account takes payments in one currency, and it is not the shop's (#5666)."""

    def __init__(self, provider: str, account: str, shop: str) -> None:
        super().__init__(
            f"{provider} takes payments in {account}, but the shop's orders are in {shop}"
        )


class CurrencyUnknownError(Exception):
    """A provider whose account takes one currency did not name it (#5752 review): such a
    row would read "Currency not checked" for good, so it is not connected."""

    def __init__(self, provider: str) -> None:
        super().__init__(f"{provider} did not say which currency the account takes")


class FieldError(ValueError):
    """A connect body that names a field the provider does not declare, or leaves one
    out. ``loc`` is the field's place in the body."""

    def __init__(self, loc: tuple[str, str], message: str) -> None:
        super().__init__(message)
        self.loc = loc


def _webhook_error(failed: Exception) -> WebhookError:
    """The fixed category Settings shows for a failed webhook; never its text."""
    if isinstance(failed, BadSignatureError):
        return "signature"
    if isinstance(failed, ProviderUnavailableError):
        return "provider_unavailable"
    return "handler_error"


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
    #: Why the last webhook failed, cleared by the next that succeeds (#5684).
    last_webhook_error: WebhookError | None
    last_webhook_error_at: datetime | None
    #: The one currency the connected account takes, when it has one (#5666: a Square
    #: location's); Settings warns when it is not the shop's ``orders_currency``.
    currency: str | None = None
    #: Whether a failed request's page can still be paid (#4959): true for a provider
    #: that captures an approval (PayPal), whose order the customer can approve again.
    reapprovable: bool
    #: Whether the provider's account takes one currency (`PaymentProvider
    #: .reports_currency`): a connected row of it with no ``currency`` was connected
    #: before the currency was kept, and is asked to reconnect.
    reports_currency: bool = False


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
        order = await asyncio.to_thread(self._orders.store.order, found.order_id)
        # The request's foreign key keeps its order; the number goes in the provider's
        # metadata (#4958). A cart has no number, but it never holds a request (#5282).
        assert order is not None and order.number is not None
        hosted: HostedRequest | None = None
        error: str | None = None
        try:
            adapter, credential = await asyncio.to_thread(self._connected, found.provider)
            hosted = await adapter.create_request(
                credential,
                found,
                return_url=self._return_url(found.order_id),
                idempotency_key=found.id,
                order_number=order.number,
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
        linked = await asyncio.to_thread(
            lambda: self._orders.lifecycle.set_request_link(found.id, hosted=hosted, error=error)
        )
        if (
            hosted is not None
            and linked.status != "open"
            and linked.provider_ref == hosted.provider_ref
            and linked.close_pending_at is not None
        ):
            # Closed while the provider answered: the new page must not stay payable. The
            # lifecycle kept its reference with the mark (#5700), so a failure here is
            # tried again by the sweep. A reference another request holds names that
            # request's live page: it is not kept, and not closed.
            await self._close_page(found.id, found.provider, hosted.provider_ref)
        return linked

    async def run_linking[T](self, order_id: str, call: Callable[[], T]) -> T:
        """Runs ``call``, a lifecycle call that may open a request on a provider (a balance
        request, #5281), then links each request it opened. An older request whose link
        failed is left to ``POST …/link``, so an edit does not call the provider again."""
        before = await self.request_ids(order_id)
        result = await asyncio.to_thread(call)
        await self.link_opened(order_id, before)
        return result

    async def request_ids(self, order_id: str) -> set[str]:
        """The order's request ids now, for ``link_opened``."""
        return {r.id for r in await asyncio.to_thread(self._requests, order_id)}

    async def link_opened(self, order_id: str, before: set[str]) -> None:
        """Link each open request on a provider that is not in ``before``."""
        for found in await asyncio.to_thread(self._requests, order_id):
            if found.id not in before and found.status == "open" and found.provider is not None:
                await self.link(found.id)

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

    # -- closed requests --------------------------------------------------------------
    # Each call closes requests through the lifecycle (a request's cancel, an order's
    # cancel or accept, a deposit set or applied, a manual payment), then, after the
    # commit, closes the provider's page of each request it moved from ``open`` or
    # ``failed`` to ``cancelled`` or to ``paid`` by hand. A provider that fails is logged
    # and undoes nothing: the request stays closed here, and a provider's paid event on it
    # is still recorded. The close
    # transaction marks each such page (``close_pending_at``, #5700); a confirmed close
    # clears the mark, and a failed one leaves it for `retry_closes`, the housekeeping
    # sweep's, which tries again with a backoff.

    async def cancel_request(
        self, order_id: str, request_id: str, by: Actor, reason: str | None = None
    ) -> PaymentRequestView:
        cancelled = await asyncio.to_thread(
            self._orders.lifecycle.cancel_payment_request, order_id, request_id, by, reason
        )
        if await self._close_pages([cancelled]):
            return cancelled.model_copy(update={"close_pending_at": None})
        return cancelled

    async def run_closing[T](self, order_id: str, call: Callable[[], T]) -> T:
        """Runs ``call``, a lifecycle call that may cancel requests of the order (an order's
        cancel, its accept, a deposit set or applied), then closes the page of each request
        it cancelled. A request cancelled before the call, or kept by it, is not touched."""
        before = {
            r.id
            for r in await asyncio.to_thread(self._requests, order_id)
            if r.status == "cancelled"
        }
        result = await asyncio.to_thread(call)
        after = await asyncio.to_thread(self._requests, order_id)
        await self._close_pages(r for r in after if r.status == "cancelled" and r.id not in before)
        return result

    async def cancel_order(self, order_id: str, cancel: Callable[[], Order]) -> Order:
        """``cancel`` cancels the order and its open and failed requests."""
        return await self.run_closing(order_id, cancel)

    async def add_payment(self, order_id: str, add: Callable[[], Payment]) -> Payment:
        """``add`` records a payment the shop received, which can pay open and failed
        requests."""
        payment = await asyncio.to_thread(add)
        after = await asyncio.to_thread(self._requests, order_id)
        await self._close_pages(r for r in after if r.payment_id == payment.id)
        return payment

    def _requests(self, order_id: str) -> list[PaymentRequestView]:
        with self._orders.store.transaction() as conn:
            return payments.requests_for_order(conn, order_id)

    async def retry_closes(self) -> int:
        """#5700, the housekeeping sweep: close each marked page whose wait is over
        (`payments.closes_due`). Returns how many the providers confirmed."""

        def due() -> list[PaymentRequestView]:
            with self._orders.store.transaction() as conn:
                return payments.closes_due(conn)

        return await self._close_pages(await asyncio.to_thread(due))

    async def _close_pages(self, closed: Iterable[PaymentRequestView]) -> int:
        """Close each page; how many the providers confirmed. A failed request's page is
        asked too (#4959): PayPal's can still be approved, and an adapter whose page closed
        already answers so (Stripe's ``cancel_request`` reads a completed session as
        closed), which confirms the close."""
        confirmed = 0
        for request in closed:
            if request.provider is not None and request.provider_ref is not None:
                confirmed += await self._close_page(
                    request.id, request.provider, request.provider_ref
                )
        return confirmed

    def _reapprovable(self, provider: str) -> bool:
        adapter = self._registry.get(provider)
        return adapter is not None and hasattr(adapter, "capture_request")

    async def _close_page(self, request_id: str, provider: str, provider_ref: str) -> bool:
        """Clears the request's mark when the provider confirms; counts a failed try."""
        try:
            adapter, credential = await asyncio.to_thread(self._connected, provider)
            await adapter.cancel_request(credential, provider_ref)
        except (
            ProviderNotFoundError,
            ProviderError,
            KekMissingError,
            CredentialUnreadableError,
        ) as failed:
            logger.warning("the page of payment request %s was not closed: %s", request_id, failed)
            gone = isinstance(failed, ProviderNotFoundError)
            await asyncio.to_thread(
                self._note_close,
                request_id,
                provider_ref,
                partial(payments.close_failed, provider_gone=gone),
            )
            return False
        await asyncio.to_thread(
            self._note_close, request_id, provider_ref, payments.close_confirmed
        )
        return True

    def _note_close(
        self, request_id: str, provider_ref: str, note: Callable[[Conn, str, str], None]
    ) -> None:
        with self._orders.store.transaction() as conn:
            note(conn, request_id, provider_ref)

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
        self,
        provider: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        after: Callable[[Callable[[], Awaitable[object]]], None] | None = None,
    ) -> EventOutcome:
        """Check the signature, then record the event. ``BadSignatureError`` leaves
        before any event or money is written (only its category, below);
        ``ProviderError`` is a signed body it cannot read;
        ``ProviderUnavailableError`` is a provider call the reading needed that failed,
        and also leaves before anything is written. A capture after an ``approved`` event
        (#4959) that the provider did not answer raises it too, after the request moved to
        ``pending`` (its audit row written): the capture may have landed, so the request
        stays ``pending`` and the event's row stays, and the provider's retry reads
        ``replayed`` and captures again under the same key. An event that came before one it
        depends on (``EventTooEarlyError``, a late fee within ``FEE_RETRY_WINDOW``) writes
        nothing and answers the same 503, so the provider sends it again; that bounded wait
        is not a failure, so it keeps no category. Every other failure but an unknown
        provider keeps its category on the credential row, and the next success clears it
        (#5684). The success's start is the database's clock (``PaymentCredentials.now``),
        as the failure's stamp is. The pages the event's payment marked are closed by
        a job handed to ``after`` (the route runs it once the answer is sent, so a slow
        provider never delays the reply), or awaited here when there is none.
        """
        began = await asyncio.to_thread(self._credentials.now)
        try:
            outcome, closes = await self._handle_webhook(provider, headers, body)
        except ProviderNotFoundError:
            raise
        except EventTooEarlyError as early:
            raise ProviderUnavailableError(str(early)) from None
        except Exception as failed:
            await self._note_webhook_error(provider, _webhook_error(failed))
            raise
        await asyncio.to_thread(self._credentials.clear_webhook_error, provider, since=began)
        if closes:
            job = partial(self._close_pages, closes)
            if after is None:
                await job()
            else:
                after(job)
        return outcome

    async def _note_webhook_error(self, provider: str, error: WebhookError) -> None:
        """Keep the category; a failure to keep it is logged, never raised over the
        webhook's own failure, so the route still answers for that one."""
        try:
            await asyncio.to_thread(self._credentials.set_webhook_error, provider, error)
        except Exception:
            logger.exception("could not keep the webhook failure of %s (%s)", provider, error)

    async def _handle_webhook(
        self, provider: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[EventOutcome, list[PaymentRequestView]]:
        adapter, credential = await asyncio.to_thread(self._connected, provider)
        event = await adapter.parse_webhook(credential, headers, body)
        await asyncio.to_thread(self._credentials.touch_webhook, provider)
        capture = getattr(adapter, "capture_request", None)
        if event.kind == "approved" and capture is None:
            # Only a provider that can capture sends ``approved`` (#4959).
            logger.warning("%s sent an approval but cannot capture; ignored", provider)
            event = event.model_copy(update={"kind": "other"})
        order_id = await asyncio.to_thread(self._order_of, provider, event.provider_ref)
        before = set() if order_id is None else await asyncio.to_thread(self._marked, order_id)
        outcome = await asyncio.to_thread(
            self._orders.lifecycle.record_provider_event, provider, event
        )
        logger.info("payment event from %s (%s): %s", provider, event.kind, outcome)
        if event.kind == "approved" and capture is not None and outcome != "ignored":
            await self._capture(provider, cast(CapturingProvider, adapter), credential, event)
        if order_id is None:
            return outcome, []
        # #5700: the open balance and deposit requests this payment settled
        # (`_close_balance`, `_close_deposit`) are marked; their pages are closed after the
        # reply, as a manual payment's are.
        now = await asyncio.to_thread(self._requests, order_id)
        return outcome, [r for r in now if r.close_pending_at is not None and r.id not in before]

    def _order_of(self, provider: str, provider_ref: str | None) -> str | None:
        if provider_ref is None:
            return None
        with self._orders.store.transaction() as conn:
            found = payments.request_by_ref(conn, provider, provider_ref)
        return None if found is None else found.order_id

    def _marked(self, order_id: str) -> set[str]:
        return {r.id for r in self._requests(order_id) if r.close_pending_at is not None}

    async def _capture(
        self,
        provider: str,
        adapter: CapturingProvider,
        credential: Credential,
        event: ProviderEvent,
    ) -> None:
        """#4959: capture an approval after its ``pending`` committed, with no lock held.
        A replay captures only a request still ``pending`` (the process may have stopped
        before its capture); the event id keys the provider's capture, so one that did
        land is not made twice. A capture the provider refused or declined puts the
        request back to ``failed``. One whose outcome is unknown (no answer, a timeout)
        leaves it ``pending``, so a cancel or a manual payment on it is refused until the
        provider's capture event or its retried approval settles it. Only the approval
        that moved the request to ``pending`` last (its event recorded, not ``ignored``, and
        named on that audit row) captures, and never on an order that has ended or is a
        cart since."""
        assert event.provider_ref is not None

        def pending() -> PaymentRequestView | None:
            with self._orders.store.transaction() as conn:
                found = payments.request_by_ref(conn, provider, event.provider_ref or "")
                if found is None or found.status != "pending":
                    return None
                if payments.event_outcome(conn, provider, event.event_id) != "recorded":
                    return None
                order = self._orders.store.order(found.order_id, conn=conn)
                if order is None or order.status in ENDED or order.unsubmitted:
                    return None
                return found

        found = await asyncio.to_thread(pending)
        if found is None:
            return
        if not await asyncio.to_thread(
            self._orders.lifecycle.approval_pending, found.id, event.event_id
        ):
            return

        def failed() -> Awaitable[None]:
            return asyncio.to_thread(self._orders.lifecycle.capture_failed, provider, found.id)

        try:
            captured = await adapter.capture_request(credential, event.provider_ref, event.event_id)
        except ProviderUnavailableError:
            raise
        except ProviderError as refused:
            logger.warning("payment request %s was not captured: %s", found.id, refused)
            await failed()
            return
        if captured == "declined":
            logger.info("%s declined the capture of payment request %s", provider, found.id)
            await failed()

    # -- the shop's accounts ----------------------------------------------------------

    async def connect(
        self, provider: str, body: ProviderConnect, *, shop_currency: str
    ) -> ProviderStatus:
        """Check the fields, refuse without a KEK, let the provider verify the account,
        refuse an account in another currency than ``shop_currency`` (the shop's
        ``orders_currency``, #5666), then seal and store it."""
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
        verified = await adapter.verify(
            Credential(settings=dict(body.settings), secrets=dict(body.secrets))
        )
        if adapter.reports_currency and verified.currency is None:
            raise CurrencyUnknownError(provider)
        if verified.currency is not None and verified.currency != shop_currency:
            raise CurrencyMismatchError(provider, verified.currency, shop_currency)
        account_label = verified.account_label
        await asyncio.to_thread(
            lambda: self._credentials.connect(
                provider,
                settings=body.settings,
                secrets=body.secrets,
                account_label=account_label,
                currency=verified.currency,
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
                    last_webhook_error=None if row is None else row.last_webhook_error,
                    last_webhook_error_at=None if row is None else row.last_webhook_error_at,
                    currency=None if row is None else row.currency,
                    reapprovable=self._reapprovable(provider_id),
                    reports_currency=adapter is not None and adapter.reports_currency,
                )
            )
        return found
