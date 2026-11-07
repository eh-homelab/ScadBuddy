"""The order and item lifecycle (#4249, spec 2026-10-18 §3.5; plan Rulings 1-5, 11, 12,
14).

Every transition is one method: it opens a transaction, takes the order's row lock
(:meth:`OrderStore.lock_order`), checks, writes, derives the order's status from its
items and shipments (:func:`derive`), writes the audit line (``order_events``, in the
same transaction, Ruling 12) and, once committed, tells the bus (``order.changed``,
``order_item.changed``; ids and the new status only, §7.1).

``orders.status`` is never written directly past ``accepted`` (Ruling 5): a writer
sets what a person decided (``accepted``, ``cancelled``, ``closed``, the hold flag) and
the rest follows from the rows. A refusal is a :class:`LifecycleError` with a ``slug``
and the offending ids, with no HTTP shape: ``api/orders.py`` maps each to a 409 (or a
404 for a missing row), and ``orders/`` never imports from ``api/``.

On hold (§7.2 as plan Ruling 14 narrows it): every transition that advances the order
(``accept_order``, ``accept_quote``, ``record_fulfilment``, ``claim_print``,
``update_fulfilment``, ``accept_fulfilment``, ``unfulfil_item``, ``add_shipment``,
``update_shipment``, ``close_order``, an item added after acceptance, #5349, the
charges changed after acceptance, #5351, and an accepted item revised after acceptance,
#5350) is refused with ``order_on_hold``; ``release``,
the cancels, a payment and the plain edits go through, so a person can fix what the hold
was for. The print follow (``follow_print``) records what a printer did, on hold too.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from psycopg import errors
from pydantic import BaseModel, ConfigDict, Field, field_validator

from scadbuddy.core.events import EventBus, OrderChanged, OrderItemChanged, emit
from scadbuddy.orders.models import (
    OPEN_FOR_EDITS,
    REVISION_ROLES,
    SHOWN_IMAGES,
    TAKES_ITEMS,
    Actor,
    AddressSnapshot,
    Approval,
    Attachment,
    AttachmentKind,
    AttachmentPatch,
    AttachmentRole,
    ChargeInput,
    ColourSlot,
    CostEntry,
    CostEntryInput,
    Fulfilment,
    FulfilmentPurpose,
    ItemStatus,
    LibraryFileRef,
    Money,
    Order,
    OrderItem,
    OrderStatus,
    Outcome,
    Payment,
    Quote,
    QuoteLine,
    Reference,
    Revision,
    Shipment,
    ShipmentItem,
    TimeEntry,
    TimeEntryInput,
)
from scadbuddy.orders.parts import (
    ItemPart,
    ItemPartRow,
    PartNotFoundError,
    PartStore,
    rule_parts,
    use_amount,
)
from scadbuddy.orders.pricing import Plate, Priced, to_minor
from scadbuddy.orders.reference import ResolvedReference
from scadbuddy.orders.revisions import RevisionAnswerInput
from scadbuddy.orders.store import (
    UNSET,
    Conn,
    OrderNotFoundError,
    OrderStore,
    UnsetType,
    fulfilment_subject,
    item_subject,
    order_subject,
    quote_file_keys,
    revision_subject,
)
from scadbuddy.orders.totals import totals_of
from scadbuddy.payments import store as payments
from scadbuddy.payments.models import (
    EventOutcome,
    HostedRequest,
    PaymentRequestKind,
    PaymentRequestView,
    ProviderEvent,
)
from scadbuddy.store.refs import BlobRefs

logger = logging.getLogger(__name__)

#: The states a person's decision has ended (nothing else may move).
ENDED: frozenset[OrderStatus] = frozenset({"closed", "cancelled"})
#: The states past a person's acceptance, which `derive` writes.
DERIVED: frozenset[OrderStatus] = frozenset({"accepted", "in_production", "ready", "shipped"})
#: An item nothing more happens to.
SETTLED: frozenset[ItemStatus] = frozenset({"fulfilled", "cancelled"})
#: The item states a fulfilment attempt may be recorded in (phase 1, Ruling 4), and a
#: print started in (§5.4).
FULFILLABLE: frozenset[ItemStatus] = frozenset({"approved", "failed"})
#: The print path's states (§5.4, #4719): its run's attempt is open, and the item follows
#: the run. An attempt is edited and accepted by hand in them too, which is how a print
#: whose end the follow could not read is settled.
PRINTING: frozenset[ItemStatus] = frozenset({"queued", "printing", "printed"})

#: The ``blob_refs`` holder kind of a print attempt's sliced file (§6.1's
#: ``order_item_fulfilment:<id>``); the holder id is the attempt's id. Never released:
#: the file is the attempt's provenance.
FULFILMENT_HOLDER = "order_item_fulfilment"

#: The ``blob_refs`` holder kind of a slice quote's sliced files (#4250 Ruling 3, §3.4's
#: ``order_item_quote:<id>``); the holder id is the quote's id.
QUOTE_HOLDER = "order_item_quote"
#: How long one ``order_item_quote`` run may take (the kind's ``run_timeout``), and so
#: the reaper's grace: a ref younger than this may belong to a run about to insert its
#: row (Ruling 3).
QUOTE_TIMEOUT = timedelta(minutes=30)


class LifecycleError(Exception):
    """A transition the record refuses: ``slug`` names why (the problem type's last
    segment), ``items`` and ``shipments`` the rows in the way."""

    def __init__(
        self,
        slug: str,
        message: str,
        *,
        items: Sequence[str] = (),
        shipments: Sequence[str] = (),
        not_found: bool = False,
        unprocessable: bool = False,
    ) -> None:
        super().__init__(message)
        self.slug = slug
        self.items = list(items)
        self.shipments = list(shipments)
        #: The row the call names is not there: a 404 rather than a 409.
        self.not_found = not_found
        #: The body names something that cannot be (a slot the item lacks, a filament
        #: not in the book): a 422 on the field ``slug`` names, rather than a 409.
        self.unprocessable = unprocessable


class ClaimRefusedError(LifecycleError):
    """A retry's claim, with no run attached, that may no longer start its print
    (``Lifecycle.claim_print``). Nothing is written: whether a print of its request
    already exists decides between attaching it and releasing the claim
    (``orders/printing.py`` ``print_item``)."""

    def __init__(self, claim: Fulfilment, refusal: LifecycleError) -> None:
        super().__init__(refusal.slug, str(refusal), items=refusal.items)
        self.claim = claim


# -- what a person sends (the routes' bodies, Ruling 12: no author, no status) ------------


class ManualQuoteInput(BaseModel):
    """A typed price (§3.4's last amendment, Ruling 1): one `manual` line."""

    model_config = ConfigDict(extra="forbid")

    unit_price_minor: Money = Field(ge=0)
    label: str = Field(default="Quoted by hand", min_length=1, max_length=200)


class FulfilmentInput(BaseModel):
    """An attempt and its outcome, by hand (§6.1, Ruling 3)."""

    model_config = ConfigDict(extra="forbid")

    quantity: int = Field(ge=0)
    outcome: Outcome
    failure_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    notes: str = ""
    #: A Bambuddy archive this attempt is (#4251, "Attach to order item" on a print's
    #: page): its facts are copied as a print's are (§8.2). Read when an attempt is
    #: recorded; an edit leaves the attempt's archives as they are.
    archive_id: int | None = Field(default=None, ge=1)


def _refuse_revision_role(role: AttachmentRole) -> None:
    """A `render` or a `test_print` is a design revision's file (storefront spec §4),
    never an order's, an item's or an attempt's."""
    if role in REVISION_ROLES:
        raise LifecycleError(
            "role",
            f"a {role} file is a design revision's: attach it to the revision",
            unprocessable=True,
        )


def _distinct_lines(items: list[ShipmentItem]) -> list[ShipmentItem]:
    """One line per item: a second line for the same item would hit the shipment
    items' primary key, a 500 where the sender deserves a 422."""
    seen: set[str] = set()
    for line in items:
        if line.order_item_id in seen:
            raise ValueError(f"item {line.order_item_id} is listed twice")
        seen.add(line.order_item_id)
    return items


class ShipmentInput(BaseModel):
    """A shipment (§6.3): what went, how, and to which address (the order's snapshot
    when none is given)."""

    model_config = ConfigDict(extra="forbid")

    carrier: str | None = None
    service: str | None = None
    tracking: str | None = None
    shipped_at: datetime | None = None
    address: AddressSnapshot | None = None
    cost_minor: Money | None = None
    notes: str = ""
    items: list[ShipmentItem] = Field(default_factory=list)

    _lines = field_validator("items")(_distinct_lines)


class ShipmentEdit(BaseModel):
    """`PATCH …/shipments/{shipment}`: only what is sent changes. A field left out
    keeps the shipment's value (``model_fields_set`` tells), so an edit that adds a
    tracking number never clears the carrier or the lines; ``items`` replaces the
    lines whole when sent."""

    model_config = ConfigDict(extra="forbid")

    carrier: str | None = None
    service: str | None = None
    tracking: str | None = None
    shipped_at: datetime | None = None
    address: AddressSnapshot | None = None
    cost_minor: Money | None = None
    notes: str | None = None
    items: list[ShipmentItem] | None = None

    _lines = field_validator("items")(
        lambda items: None if items is None else _distinct_lines(items)
    )


class OrderEdit(BaseModel):
    """`PATCH /orders/{id}`: notes and the due date in any open state; the addresses
    and the charges, replaced whole, while `draft` or `quoted`."""

    model_config = ConfigDict(extra="forbid")

    notes: str | None = None
    due_at: datetime | UnsetType | None = UNSET
    shipping_address_id: str | UnsetType | None = UNSET
    billing_address_id: str | UnsetType | None = UNSET
    charges: list[ChargeInput] | None = None


@dataclass(frozen=True)
class LabourRate:
    """``pricing_labour_rate_per_hour`` and the shop's ``orders_currency`` it is in."""

    per_hour: Decimal
    currency: str


@dataclass
class _Change:
    """What one transition did, told after commit."""

    order: Order
    items: list[tuple[OrderItem, int | None]] = field(default_factory=list)
    #: The quotes whose sliced-file holder §3.4 releases, dropped once committed.
    released: list[str] = field(default_factory=list)
    #: Released by a cancel or a close: dropped, under the orders' locks, only while that
    #: still holds when the drop runs, so a reopen keeps the holds it took (§3.5 "Undo").
    ended: bool = False


def _covered(items: Iterable[OrderItem], shipments: Iterable[Shipment]) -> list[str]:
    """The fulfilled items no shipment covers in full (§6.3)."""
    shipped: dict[str, int] = {}
    for shipment in shipments:
        for line in shipment.items:
            shipped[line.order_item_id] = shipped.get(line.order_item_id, 0) + line.quantity
    return [
        item.id
        for item in items
        if item.status == "fulfilled" and shipped.get(item.id, 0) < item.quantity
    ]


def derive(
    order: Order,
    items: Sequence[OrderItem],
    shipments: Sequence[Shipment],
    attempted: Collection[str] = (),
) -> OrderStatus:
    """The order's status from its rows (§3.5's close rule, Ruling 5). Pure.

    Before `accepted` and once ended, the status is what was written. Past it:
    `ready` when every item is settled and at least one is fulfilled, `shipped` when
    the shipments cover every fulfilled item, `in_production` when any item is
    `queued` or beyond or has a fulfilment attempt (``attempted``: the ids with one)
    and not every item is settled, which is §3.5's "any item past `approved`" (plan
    Ruling 4, widened by the print path, #4719); else `accepted`.
    """
    if order.status not in DERIVED:
        return order.status
    settled = all(item.status in SETTLED for item in items)
    fulfilled = [item for item in items if item.status == "fulfilled"]
    if settled and fulfilled:
        return "ready" if _covered(fulfilled, shipments) else "shipped"
    if any(item.id in attempted or item.status in PRINTING for item in items):
        return "in_production"
    return "accepted"


def _charge_line(charge: ChargeInput) -> dict[str, Any]:
    """A charge as an audit row holds it: its kind, label and signed amount."""
    return {"kind": charge.kind, "label": charge.label, "amount_minor": charge.amount_minor}


def item_open(order: Order, quotes: Iterable[Quote]) -> bool:
    """Whether an item's design, quantity and price may still change: while the order is
    `draft` or `quoted`, and past it while the item has no accepted quote, which only an
    item added after acceptance can lack (#5349). An accepted price stays pinned; an item
    revised after acceptance takes a new one through :func:`requote_open` (#5350)."""
    if order.status in OPEN_FOR_EDITS:
        return True
    return order.status in TAKES_ITEMS and not any(quote.accepted for quote in quotes)


#: The kinds that never take a design revision: a custom item names nothing (#4954), a
#: library file item names its file (#5544).
NO_REVISIONS: frozenset[str] = frozenset({"custom", "library_file"})


def has_design(item: OrderItem) -> bool:
    """Whether the item names something to slice and print: a template reference, or a
    library file (#5544)."""
    return item.reference is not None or item.library_file is not None


#: The states an accepted item goes back to `draft` from when it is revised after
#: acceptance (#5350): it waits for a quote of its new design. `queued` and `printing`
#: refuse the revision (`print_in_flight`); a settled item refuses it too.
REQUOTED: frozenset[ItemStatus] = frozenset({"quoted", "approved", "printed", "failed"})


def stale_attempt(
    attempt: Fulfilment, quotes: Iterable[Quote], revisions: Iterable[Revision]
) -> bool:
    """Whether a production attempt was made before the revision the accepted quote
    prices: it printed an earlier design (#5350), so it is never accepted
    (`fulfilment_stale`). A test print's attempt, a quote that names no revision and an
    item with no accepted quote say nothing. The order detail marks such an attempt
    ``stale`` by this same rule (#5592).

    The two ``created_at`` values come from two transactions: the attempt's is ``now()``,
    its transaction's start, and the revision's is ``clock_timestamp()``. Their order holds
    because every writer of an item takes the order's row lock and reads the item after it:
    a revision after acceptance is refused while a print of the item is open
    (``print_in_flight``) and puts the item back to `draft`, and no attempt is recorded or
    claimed on a `draft` item. So an attempt whose transaction began before the revision and
    waited on the lock finds the item `draft` and is refused, and every attempt made after
    the revision starts after the new quote's acceptance, later than the revision."""
    if attempt.purpose != "production":
        return False
    accepted = next((q for q in quotes if q.accepted), None)
    if accepted is None or accepted.revision_id is None:
        return False
    priced = next((r for r in revisions if r.id == accepted.revision_id), None)
    return priced is not None and attempt.created_at < priced.created_at


def requote_open(order: Order, item: OrderItem, quotes: Iterable[Quote]) -> bool:
    """Whether the item takes a new design revision, a quote and a quote's acceptance:
    while :func:`item_open`, and past `quoted` while it was revised after acceptance
    (#5350, ``revised_after_acceptance``). Its old accepted quote stands until a quote of
    the new design is accepted. Its quantity stays pinned."""
    if item_open(order, quotes):
        return True
    return order.status in TAKES_ITEMS and item.revised_after_acceptance


def _settle_open(items: Sequence[OrderItem]) -> OrderStatus:
    """`draft` or `quoted` (§3.5): quoted once every item not cancelled is approved."""
    open_items = [item for item in items if item.status != "cancelled"]
    return "quoted" if open_items and all(i.status == "approved" for i in open_items) else "draft"


def _parts_detail(parts: Sequence[ItemPart]) -> list[dict[str, Any]]:
    return [{"part_id": p.part_id, "per_unit": p.per_unit} for p in parts]


class Lifecycle:
    def __init__(self, store: OrderStore, bus: EventBus | None, refs: BlobRefs | None = None):
        self.store = store
        self.bus = bus
        #: The slice quotes' holds (#4250 Ruling 3); None where nothing holds a file.
        self.refs = refs
        #: The parts inventory (#5543): an item's parts, used when it is fulfilled.
        self.parts = PartStore(store)
        #: The payment providers offered (#4957, ``payments/registry.py``): a default
        #: provider not among them is no provider. Set by ``OrdersComponent``.
        self.offered_providers: frozenset[str] = frozenset()

    # -- the frame every transition runs in -------------------------------------------

    def _release(self, quote_ids: Iterable[str]) -> None:
        """Drop the holders §3.4 releases, after the commit that released them (a drop
        before a rollback would lose bytes a quote still names). A drop that fails is
        logged: `reap_quote_holds` drops it within a prune interval."""
        if self.refs is None:
            return
        for quote_id in quote_ids:
            try:
                self.refs.drop_holder(QUOTE_HOLDER, quote_id)
            except Exception:
                logger.exception("could not release a quote's sliced file", extra={"id": quote_id})

    def _hold(self, conn: Conn, quotes: Iterable[Quote]) -> None:
        """Take the holders §3.4 keeps again for quotes that stand once more (a reopened
        item or order), in the reopen's own transaction, so a reopen never commits
        without them; a rollback leaves none. A sliced file the sweep took meanwhile
        stays gone: the hold keeps nothing, and the item needs a new quote."""
        self.store.hold_quote_files(conn, QUOTE_HOLDER, list(quotes))

    def _standing(self, conn: Conn, item_id: str) -> list[Quote]:
        """The quotes §3.4 keeps a file for: the accepted one and the newest unaccepted."""
        quotes = self.store.quotes(item_id, conn=conn)
        accepted = [q for q in quotes if q.accepted]
        pending = [q for q in quotes if not q.accepted]
        newest = max(pending, key=lambda q: (q.created_at, q.id), default=None)
        return accepted + ([newest] if newest is not None else [])

    def _quote_ids(self, conn: Conn, item_ids: Iterable[str]) -> list[str]:
        return [quote.id for item_id in item_ids for quote in self.store.quotes(item_id, conn=conn)]

    def _release_ended(self, quote_ids: Sequence[str]) -> None:
        """A cancel's or a close's release: only the quotes still released when it runs,
        under their orders' locks (`OrderStore.drop_released_holds`), so a reopen that
        took the holds again keeps them. A failure is logged: the reaper drops them."""
        if self.refs is None or not quote_ids:
            return
        try:
            self.store.drop_released_holds(QUOTE_HOLDER, quote_ids)
        except Exception:
            logger.exception("could not release an ended item's quotes; the reaper drops them")

    def _told(self, change: _Change) -> None:
        if change.ended:
            self._release_ended(change.released)
        else:
            self._release(change.released)
        emit(self.bus, OrderChanged(order_id=change.order.id, status=change.order.status))
        for item, attempt in change.items:
            emit(
                self.bus,
                OrderItemChanged(
                    order_id=change.order.id, item_id=item.id, status=item.status, attempt=attempt
                ),
            )

    def _lock(self, conn: Conn, order_id: str) -> Order:
        try:
            return self.store.lock_order(conn, order_id)
        except OrderNotFoundError:
            raise LifecycleError(
                "order_not_found", f"no order {order_id}", not_found=True
            ) from None

    def _item_of(self, conn: Conn, order: Order, item_id: str) -> OrderItem:
        item = self.store.item(item_id, conn=conn)
        if item is None or item.order_id != order.id:
            raise LifecycleError(
                "item_not_found", f"no item {item_id} of order {order.number}", not_found=True
            )
        return item

    @staticmethod
    def _not_on_hold(order: Order) -> None:
        if order.on_hold:
            raise LifecycleError("order_on_hold", f"order {order.number} is on hold")

    @staticmethod
    def _not_ended(order: Order) -> None:
        if order.status in ENDED:
            raise LifecycleError(f"order_{order.status}", f"order {order.number} is {order.status}")

    def _event(self, conn: Conn, order: Order, by: Actor, kind: str, **detail: Any) -> None:
        self.store.insert_event(conn, order.id, by, kind, detail)

    def _rederive(self, conn: Conn, order: Order, by: Actor) -> Order:
        """The status the rows now say, written when it moved, on the audit line."""
        items = self.store.items(order.id, conn=conn)
        if order.status in OPEN_FOR_EDITS:
            status = _settle_open(items)
        elif order.status in DERIVED:
            attempted = {item.id for item in items if self.store.fulfilments(item.id, conn=conn)}
            status = derive(order, items, self.store.shipments(order.id, conn=conn), attempted)
        else:
            return order
        if status == order.status:
            return order
        moved = self.store.set_order_status(conn, order.id, status)
        self._event(conn, order, by, "order.status", from_status=order.status, to=status)
        return moved

    # -- orders (§3.2) --------------------------------------------------------------------

    def create_order(
        self,
        customer_id: str,
        by: Actor,
        *,
        currency: str,
        notes: str = "",
        due_at: datetime | None = None,
    ) -> Order:
        """A draft order, pointing at the customer's default addresses (Ruling 11).
        The customer is read under a share lock inside the insert's transaction, so an
        archive running at the same moment either comes first and refuses this order,
        or waits for it."""
        with self.store.transaction() as conn:
            customer = self.store.customer(customer_id, conn=conn, share=True)
            if customer is None:
                raise LifecycleError(
                    "customer_not_found", f"no customer {customer_id}", not_found=True
                )
            if customer.archived_at is not None:
                raise LifecycleError("customer_archived", f"customer {customer_id} is archived")
            shipping = next((a.id for a in customer.addresses if a.default_shipping), None)
            billing = next((a.id for a in customer.addresses if a.default_billing), None)
            order = self.store.insert_order(
                conn,
                customer_id=customer_id,
                currency=currency,
                created_by=by,
                notes=notes,
                due_at=due_at,
                shipping_address_id=shipping,
                billing_address_id=billing,
            )
            self._event(conn, order, by, "order.created")
        self._told(_Change(order))
        return order

    def edit_order(self, order_id: str, by: Actor, edit: OrderEdit) -> Order:
        """Notes and the due date in any open state; addresses (the order's own
        customer's) and charges while `draft` or `quoted`."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            structural = (
                edit.charges is not None
                or not isinstance(edit.shipping_address_id, UnsetType)
                or not isinstance(edit.billing_address_id, UnsetType)
            )
            if structural and order.status not in OPEN_FOR_EDITS:
                # Past `quoted` the charges change on their own route (#5351).
                route = (
                    f"; change the charges with PUT /orders/{order.id}/charges/after-acceptance"
                    if edit.charges is not None and order.status in TAKES_ITEMS
                    else ""
                )
                raise LifecycleError(
                    "order_not_editable",
                    f"order {order.number} is {order.status}: only its notes and due date"
                    f" may change now{route}",
                )
            for name in ("shipping_address_id", "billing_address_id"):
                wanted = getattr(edit, name)
                if isinstance(wanted, str):
                    found = self.store.address(wanted, conn=conn)
                    if found is None or found[0] != order.customer_id:
                        raise LifecycleError(
                            "address_not_found",
                            f"{name}: no address {wanted} of the order's customer",
                        )
            order = self.store.update_order_fields(
                conn,
                order.id,
                notes=UNSET if edit.notes is None else edit.notes,
                due_at=edit.due_at,
                shipping_address_id=edit.shipping_address_id,
                billing_address_id=edit.billing_address_id,
            )
            edited = [
                name
                for name, value in (
                    ("notes", edit.notes),
                    ("due_at", edit.due_at),
                    ("shipping_address_id", edit.shipping_address_id),
                    ("billing_address_id", edit.billing_address_id),
                )
                if value is not None and not isinstance(value, UnsetType)
            ]
            if edited:
                self._event(conn, order, by, "order.edited", fields=edited)
            if edit.charges is not None:
                self.store.replace_charges(conn, order.id, edit.charges)
                self._event(conn, order, by, "charges.replaced", count=len(edit.charges))
        self._told(_Change(order))
        return order

    def replace_charges_after_acceptance(
        self, order_id: str, by: Actor, charges: Sequence[ChargeInput]
    ) -> Order:
        """The charges, replaced whole, on an order past `quoted` (#5351): a fee or a
        discount that keeps an agreed total when an item is added after acceptance. It
        changes what the customer pays, so it is its own route and the agent's `outward`
        tier. Only an `accepted`, `in_production` or `ready` order (`TAKES_ITEMS`) that is
        not on hold takes it: `order_not_accepted` before that (use ``edit_order``),
        `order_not_editable` once `shipped`, `order_closed`/`order_cancelled` after. The
        audit row ``charges.replaced_after_acceptance`` holds the charges before and after,
        written in the same transaction."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            if order.status in OPEN_FOR_EDITS:
                raise LifecycleError(
                    "order_not_accepted",
                    f"order {order.number} is {order.status}, not accepted: change the charges"
                    f" with PATCH /orders/{order.id}",
                )
            if order.status not in TAKES_ITEMS:
                raise LifecycleError(
                    "order_not_editable",
                    f"order {order.number} is {order.status}: its charges are final once shipped",
                )
            self._not_on_hold(order)
            before = self.store.charges(order.id, conn=conn)
            after = self.store.replace_charges(conn, order.id, charges)
            self._event(
                conn,
                order,
                by,
                "charges.replaced_after_acceptance",
                order_status=order.status,
                before=[_charge_line(charge) for charge in before],
                after=[_charge_line(charge) for charge in after],
            )
            order = self.store.order(order.id, conn=conn) or order
        self._told(_Change(order))
        return order

    def add_payment(
        self,
        order_id: str,
        by: Actor,
        *,
        amount_minor: int,
        method: str | None = None,
        reference: str | None = None,
        received_at: datetime | None = None,
        notes: str = "",
        payment_request_id: str | None = None,
    ) -> Payment:
        """Money the shop already received: a record, allowed on hold (Ruling 14). A
        payment that names an open request of this order closes it. Named or not, a
        payment that brings the balance to zero or below closes the open ``balance``
        requests. A refund (zero or below) closes nothing. It writes no fee entry: only a
        provider's event does."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            if order.status == "cancelled":
                raise LifecycleError("order_cancelled", f"order {order.number} is cancelled")
            named = None
            if payment_request_id is not None:
                named = self._open_request(conn, order, payment_request_id)
                if amount_minor < 1:
                    raise LifecycleError(
                        "amount_minor",
                        "a payment that closes a request must be above zero",
                        unprocessable=True,
                    )
            payment = self.store.insert_payment(
                conn,
                order.id,
                amount_minor=amount_minor,
                method=method,
                reference=reference,
                received_at=received_at,
                notes=notes,
                payment_request_id=payment_request_id,
            )
            if named is not None:
                payments.set_request_status(conn, named.id, "paid", payment_id=payment.id)
            if payment.amount_minor > 0:
                self._close_balance(conn, order, payment)
            self._event(
                conn,
                order,
                by,
                "payment.added",
                payment_id=payment.id,
                payment_request_id=payment_request_id,
            )
        self._told(_Change(order))
        return payment

    # -- payment requests (#4957) ---------------------------------------------------------
    # A request asks the customer for money, through a provider's hosted page or by hand.
    # Its amount is fixed once opened. Its link is made after commit (``set_request_link``).

    def _close_balance(self, conn: Conn, order: Order, payment: Payment) -> None:
        """Once the balance is zero or below, ``payment`` closes the open balance requests."""
        if totals_of(self.store, conn, order.id).balance_minor > 0:
            return
        for open_request in payments.open_requests_for_order(conn, order.id):
            if open_request.kind == "balance":
                payments.set_request_status(conn, open_request.id, "paid", payment_id=payment.id)

    def _request_of(self, conn: Conn, order: Order, request_id: str) -> PaymentRequestView:
        found = payments.request(conn, request_id)
        if found is None or found.order_id != order.id:
            raise LifecycleError(
                "payment_request_not_found",
                f"no payment request {request_id} of order {order.number}",
                not_found=True,
            )
        return found

    def _open_request(self, conn: Conn, order: Order, request_id: str) -> PaymentRequestView:
        found = self._request_of(conn, order, request_id)
        if found.status != "open":
            raise LifecycleError(
                "payment_request_not_open", f"payment request {request_id} is {found.status}"
            )
        return found

    def open_payment_request(
        self,
        order_id: str,
        by: Actor,
        *,
        kind: PaymentRequestKind = "balance",
        amount_minor: Money | None = None,
        provider: str | UnsetType | None = UNSET,
    ) -> PaymentRequestView:
        """A request for ``amount_minor`` (the balance now when None), through
        ``provider`` (the default provider when unset and still offered; None collects by
        hand). A named provider that is not connected is refused as 422 ``provider``."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            amount = (
                totals_of(self.store, conn, order.id).balance_minor
                if amount_minor is None
                else amount_minor
            )
            if amount <= 0:
                raise LifecycleError("nothing_due", f"order {order.number} has nothing due")
            if isinstance(provider, UnsetType):
                provider = payments.default_provider(conn, self.offered_providers)
            elif provider is not None and not payments.hold_provider(conn, provider):
                raise LifecycleError(
                    "provider",
                    f"no connected payment provider {provider!r}",
                    unprocessable=True,
                )
            opened = payments.insert_request(
                conn,
                order_id=order.id,
                kind=kind,
                provider=provider,
                amount_minor=amount,
                currency=order.currency,
                created_by=by,
            )
            # ``kind`` is ``_event``'s own argument, so the detail goes in whole.
            self.store.insert_event(
                conn,
                order.id,
                by,
                "payment_request.opened",
                {"request_id": opened.id, "kind": kind, "amount_minor": amount},
            )
        self._told(_Change(order))
        return opened

    def cancel_payment_request(
        self, order_id: str, request_id: str, by: Actor
    ) -> PaymentRequestView:
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._open_request(conn, order, request_id)
            cancelled = payments.set_request_status(conn, request_id, "cancelled")
            self._event(conn, order, by, "payment_request.cancelled", request_id=request_id)
        self._told(_Change(order))
        return cancelled

    def set_request_link(
        self, request_id: str, *, hosted: HostedRequest | None, error: str | None
    ) -> PaymentRequestView:
        """The provider's answer to a request's link, under the request's order lock: its
        hosted page, or why it failed (the link it had stays). A request no longer open
        (cancelled or expired while the provider answered) keeps no new link, so no one
        pays it. A paid request is returned as it is: it needs no link and no error. A
        reference another request holds is kept as ``link_error``, not as the link. No
        audit row: the link is the provider's, not a person's decision."""
        with self.store.transaction() as conn:
            found = payments.request(conn, request_id)
            if found is None:
                raise LifecycleError(
                    "payment_request_not_found", f"no payment request {request_id}", not_found=True
                )
            order = self._lock(conn, found.order_id)
            found = payments.request(conn, request_id) or found
            if found.status == "paid":
                return found
            if found.status != "open":
                hosted, error = None, f"the payment request is {found.status}"
            try:
                # A savepoint: a refused write leaves the transaction usable.
                with conn.transaction():
                    linked = payments.set_request_link(
                        conn,
                        request_id,
                        provider_ref=found.provider_ref if hosted is None else hosted.provider_ref,
                        hosted_url=found.hosted_url if hosted is None else str(hosted.hosted_url),
                        link_error=error,
                    )
            except errors.UniqueViolation:
                # ``payment_requests_ref``: the provider answered with another request's
                # reference. The request keeps no link, so no one pays the wrong page.
                linked = payments.set_request_link(
                    conn,
                    request_id,
                    provider_ref=found.provider_ref,
                    hosted_url=found.hosted_url,
                    link_error="the provider's reference is held by another payment request",
                )
        self._told(_Change(order))
        return linked

    def record_provider_event(self, provider: str, event: ProviderEvent) -> EventOutcome:
        """A provider's event, in one transaction. The event row is written first, so a
        second delivery waits on the first's unique key and then reads ``replayed``. A
        ``paid`` event is recorded on an ended or held order too: the money arrived."""
        by = Actor(kind="provider", principal=f"payment:{provider}")
        change: _Change | None = None
        with self.store.transaction() as conn:
            row = payments.insert_event(conn, provider, event)
            if row is None:
                return "replayed"
            found = (
                None
                if event.provider_ref is None
                else payments.request_by_ref(conn, provider, event.provider_ref)
            )
            if found is None:
                payments.finish_event(conn, row, outcome="unmatched")
                return "unmatched"
            order = self._lock(conn, found.order_id)
            found = payments.request(conn, found.id) or found
            outcome, payment = self._apply_event(conn, order, found, provider, event, by)
            payments.finish_event(
                conn,
                row,
                outcome=outcome,
                order_id=order.id,
                payment_request_id=found.id,
                payment_id=None if payment is None else payment.id,
            )
            if outcome == "recorded":
                change = _Change(order)
        if change is not None:
            self._told(change)
        return outcome

    def _paid_by(self, conn: Conn, order: Order, found: PaymentRequestView, provider: str) -> bool:
        """Whether this provider's own payment already paid the request. A request paid
        by hand, cancelled or expired takes a provider's payment: that money is real."""
        if found.status != "paid" or found.payment_id is None:
            return False
        paid = next(
            (p for p in self.store.payments(order.id, conn=conn) if p.id == found.payment_id),
            None,
        )
        return (
            paid is not None and paid.provider == provider and paid.reference == found.provider_ref
        )

    def _apply_event(
        self,
        conn: Conn,
        order: Order,
        found: PaymentRequestView,
        provider: str,
        event: ProviderEvent,
        by: Actor,
    ) -> tuple[EventOutcome, Payment | None]:
        if event.kind == "expired":
            if found.status == "open":
                payments.set_request_status(conn, found.id, "expired")
                self._event(conn, order, by, "payment_request.expired", request_id=found.id)
            return "recorded", None
        if event.kind not in ("paid", "refunded"):
            return "ignored", None
        if event.currency != order.currency:
            return "currency_mismatch", None
        if event.kind == "refunded" and event.amount_minor is None:
            # No amount: a guess at a full refund could be wrong, so nothing is written.
            return "ignored", None
        if event.kind == "paid" and self._paid_by(conn, order, found, provider):
            # A hosted page is paid once: this is the same money told again.
            return "ignored", None
        amount = found.amount_minor if event.amount_minor is None else event.amount_minor
        payment = self.store.insert_payment(
            conn,
            order.id,
            amount_minor=amount if event.kind == "paid" else -amount,
            method=provider,
            reference=event.provider_ref,
            received_at=None,
            notes="",
            provider=provider,
            payment_request_id=found.id,
        )
        self._event(
            conn,
            order,
            by,
            "payment.added",
            payment_id=payment.id,
            payment_request_id=found.id,
            provider=provider,
        )
        if event.kind == "refunded":
            return "recorded", payment
        # A request already paid keeps the payment that paid it first.
        if found.status != "paid":
            payments.set_request_status(conn, found.id, "paid", payment_id=payment.id)
        self._close_balance(conn, order, payment)
        if event.fee_minor:
            # Job cost spec §4.4: the provider's fee, in the payment's own transaction.
            fee, _ = self.store.insert_cost_entry(
                conn,
                order.id,
                CostEntryInput(
                    kind="payment_fee",
                    amount_minor=event.fee_minor,
                    source=f"payment:{payment.id}",
                    note=f"{provider} fee",
                ),
                created_by=by,
            )
            self._event(
                conn,
                order,
                by,
                "cost.added",
                entry_id=fee.id,
                item_id=None,
                entry_kind=fee.kind,
                amount_minor=fee.amount_minor,
            )
        return "recorded", payment

    # -- time and cost entries (job cost spec 2026-11-05 §4.3-4.5, #5326) ---------------
    # A record of what the job took, allowed in every status, ended and on hold included:
    # a fee or a label often arrives after the order moved on. Nothing here moves a status.

    def _entry_item(self, conn: Conn, order: Order, item_id: str | None) -> list[OrderItem]:
        return [] if item_id is None else [self._item_of(conn, order, item_id)]

    def add_time_entry(
        self, order_id: str, body: TimeEntryInput, by: Actor, *, labour_rate: LabourRate
    ) -> tuple[TimeEntry, bool]:
        """Time spent, at the labour rate now (copied, in the order's minor units, under
        the order's lock, so a currency change cannot slip in between), and whether this
        call wrote it: a second write of one ``source`` returns the first entry and writes
        nothing, not even an audit row."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            items = self._entry_item(conn, order, body.order_item_id)
            rate = (
                to_minor(labour_rate.per_hour, order.currency)
                if order.currency == labour_rate.currency
                else None
            )
            entry, written = self.store.insert_time_entry(
                conn, order.id, body, rate_minor_per_hour=rate, created_by=by
            )
            if not written:
                return entry, False
            self._event(
                conn,
                order,
                by,
                "time.added",
                entry_id=entry.id,
                item_id=entry.order_item_id,
                entry_kind=entry.kind,
                minutes=entry.minutes,
            )
        self._told(_Change(order, [(item, None) for item in items]))
        return entry, True

    def remove_time_entry(self, order_id: str, entry_id: str, by: Actor) -> None:
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            entry = self.store.delete_time_entry(conn, order.id, entry_id)
            if entry is None:
                raise LifecycleError(
                    "time_entry_not_found",
                    f"no time entry {entry_id} of order {order.number}",
                    not_found=True,
                )
            self._event(
                conn,
                order,
                by,
                "time.removed",
                entry_id=entry.id,
                item_id=entry.order_item_id,
                entry_kind=entry.kind,
                minutes=entry.minutes,
            )
            items = self._entry_item(conn, order, entry.order_item_id)
        self._told(_Change(order, [(item, None) for item in items]))

    def add_cost_entry(
        self, order_id: str, body: CostEntryInput, by: Actor
    ) -> tuple[CostEntry, bool]:
        """Money spent, and whether this call wrote it: a second write of one ``source``
        returns the first entry and writes nothing, not even an audit row."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            items = self._entry_item(conn, order, body.order_item_id)
            entry, written = self.store.insert_cost_entry(conn, order.id, body, created_by=by)
            if not written:
                return entry, False
            self._event(
                conn,
                order,
                by,
                "cost.added",
                entry_id=entry.id,
                item_id=entry.order_item_id,
                entry_kind=entry.kind,
                amount_minor=entry.amount_minor,
            )
        self._told(_Change(order, [(item, None) for item in items]))
        return entry, True

    def remove_cost_entry(self, order_id: str, entry_id: str, by: Actor) -> None:
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            entry = self.store.delete_cost_entry(conn, order.id, entry_id)
            if entry is None:
                raise LifecycleError(
                    "cost_entry_not_found",
                    f"no cost entry {entry_id} of order {order.number}",
                    not_found=True,
                )
            self._event(
                conn,
                order,
                by,
                "cost.removed",
                entry_id=entry.id,
                item_id=entry.order_item_id,
                entry_kind=entry.kind,
                amount_minor=entry.amount_minor,
            )
            items = self._entry_item(conn, order, entry.order_item_id)
        self._told(_Change(order, [(item, None) for item in items]))

    def hold(self, order_id: str, by: Actor) -> Order:
        return self._set_hold(order_id, by, True)

    def release(self, order_id: str, by: Actor) -> Order:
        return self._set_hold(order_id, by, False)

    def _set_hold(self, order_id: str, by: Actor, on_hold: bool) -> Order:
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            if order.on_hold != on_hold:
                order = self.store.update_order_fields(conn, order.id, on_hold=on_hold)
                self._event(conn, order, by, "order.hold" if on_hold else "order.release")
        self._told(_Change(order))
        return order

    def accept_order(self, order_id: str, by: Actor) -> Order:
        """§3.5: every item not cancelled has an accepted quote; the addresses are
        copied (Ruling 11); `accepted_at` is stamped."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            if order.status not in OPEN_FOR_EDITS:
                raise LifecycleError(
                    "order_not_acceptable", f"order {order.number} is {order.status}"
                )
            self._not_on_hold(order)
            items = self.store.items(order.id, conn=conn)
            open_items = [item for item in items if item.status != "cancelled"]
            if not open_items:
                raise LifecycleError("no_open_items", f"order {order.number} has no open items")
            unquoted = [item.id for item in open_items if item.status != "approved"]
            if unquoted:
                raise LifecycleError(
                    "items_not_quoted",
                    f"{len(unquoted)} item(s) of order {order.number} have no accepted quote",
                    items=unquoted,
                )
            snapshots: dict[str, AddressSnapshot | None] = {}
            for name in ("shipping_address_id", "billing_address_id"):
                address_id = getattr(order, name)
                found = self.store.address(address_id, conn=conn) if address_id else None
                snapshots[name.removesuffix("_id")] = (
                    AddressSnapshot.model_validate(found[1].model_dump(exclude={"position"}))
                    if found
                    else None
                )
            self.store.update_order_fields(
                conn,
                order.id,
                shipping_address=snapshots["shipping_address"],
                billing_address=snapshots["billing_address"],
            )
            accepted = self.store.set_order_status(conn, order.id, "accepted")
            self._event(conn, order, by, "order.status", from_status=order.status, to="accepted")
        self._told(_Change(accepted))
        return accepted

    def cancel_order(self, order_id: str, by: Actor, reason: str | None = None) -> Order:
        """§3.5: from any state before `shipped`; cancels the open items and the open
        payment requests, and keeps every fulfilled one's attempts. Allowed on hold
        (Ruling 14)."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            if order.status in ("shipped", *ENDED):
                raise LifecycleError(
                    "order_not_cancellable", f"order {order.number} is {order.status}"
                )
            change = _Change(order)
            every = self.store.items(order.id, conn=conn)
            for item in every:
                if item.status not in SETTLED:
                    moved = self.store.update_item(conn, item.id, status="cancelled")
                    change.items.append((moved, None))
            # §3.4: cancelling the order releases every quote's sliced file.
            change.released = self._quote_ids(conn, (item.id for item in every))
            change.ended = True
            # No one pays a cancelled order: its open requests end with it.
            for open_request in payments.open_requests_for_order(conn, order.id):
                payments.set_request_status(conn, open_request.id, "cancelled")
                self._event(
                    conn, order, by, "payment_request.cancelled", request_id=open_request.id
                )
            cancelled = self.store.set_order_status(conn, order.id, "cancelled")
            self._event(
                conn,
                order,
                by,
                "order.status",
                from_status=order.status,
                to="cancelled",
                items=[item.id for item, _ in change.items],
                reason=reason,
            )
            change.order = cancelled
        self._told(change)
        return cancelled

    def close_order(self, order_id: str, by: Actor) -> Order:
        """§3.5's close rule: refused, naming every item not fulfilled or cancelled and
        every fulfilled item no shipment covers, unless the rows say `shipped`."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            self._not_on_hold(order)
            items = self.store.items(order.id, conn=conn)
            shipments = self.store.shipments(order.id, conn=conn)
            unsettled = [item.id for item in items if item.status not in SETTLED]
            unshipped = _covered(items, shipments)
            fulfilled = any(item.status == "fulfilled" for item in items)
            if order.status not in DERIVED or unsettled or unshipped or not fulfilled:
                raise LifecycleError(
                    "order_not_closable",
                    f"order {order.number} cannot close: {len(unsettled)} item(s) still open,"
                    f" {len(unshipped)} fulfilled item(s) not shipped"
                    + ("" if fulfilled else ", nothing fulfilled"),
                    items=[*unsettled, *unshipped],
                )
            closed = self.store.set_order_status(conn, order.id, "closed")
            self._event(conn, order, by, "order.status", from_status=order.status, to="closed")
            # §3.4: closing the order releases every quote's sliced file.
            released = self._quote_ids(conn, (item.id for item in items))
        self._told(_Change(closed, released=released, ended=True))
        return closed

    def _reopened_status(self, conn: Conn, item: OrderItem) -> tuple[ItemStatus, bool]:
        """What a cancelled item comes back as, from its quotes and attempts: `approved`
        with an accepted quote (`failed` when its latest attempt failed), `quoted` with
        a quote not accepted, else `draft`; and whether it has an accepted quote."""
        quotes = self.store.quotes(item.id, conn=conn)
        accepted = any(quote.accepted for quote in quotes)
        if accepted and item.revised_after_acceptance:
            # #5350: priced at its kept quote, but its new design waits for its own.
            new = any(q.revision_id == item.current_revision_id for q in quotes if not q.accepted)
            return ("quoted" if new else "draft"), True
        attempts = self.store.fulfilments(item.id, conn=conn)
        latest = max(attempts, key=lambda a: a.attempt, default=None)
        if accepted:
            failed = latest is not None and latest.outcome == "failed"
            return ("failed" if failed else "approved"), True
        return ("quoted" if quotes else "draft"), False

    def _last_cancel(self, conn: Conn, order: Order) -> tuple[OrderStatus, list[str]]:
        """The status the order was cancelled from and the items that cancel took with
        it, from its audit row; an order cancelled before that row carried them goes
        back to `accepted` when it had been accepted, else `draft`, with no items."""
        for event in reversed(self.store.events(order.id, conn=conn)):
            if event.kind == "order.status" and event.detail.get("to") == "cancelled":
                before = event.detail.get("from_status")
                items = event.detail.get("items")
                if isinstance(before, str) and before in OPEN_FOR_EDITS | DERIVED:
                    ids = items if isinstance(items, list) else []
                    status: OrderStatus = before
                    return status, [each for each in ids if isinstance(each, str)]
                reason = "its cancel's audit row names no status it was cancelled from"
                break
        else:
            reason = "it has no cancel audit row"
        fallback: OrderStatus = "accepted" if order.accepted_at is not None else "draft"
        # #4725: so an order that reopened with none of its items back can be explained.
        logger.warning("order %s reopens as %s with no items: %s", order.number, fallback, reason)
        return fallback, []

    def reopen_order(self, order_id: str, by: Actor) -> Order:
        """Undo a close or a cancel.

        A closed order takes the status its rows give it again (`shipped`, as it
        closed, unless a row changed since). A cancelled one goes back to the status it
        was cancelled from, and the items that cancel took with it come back as
        `reopen_item` brings one back (an item with no accepted quote too, as `draft` or
        `quoted`: an order cancelled from `accepted`, `in_production` or `ready` takes
        such an item, #5349); then its status follows from its rows. Either way `closed_at`
        and `cancelled_at` are cleared and the quotes' sliced files are held again."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            if order.status not in ENDED:
                raise LifecycleError(
                    "order_not_ended",
                    f"order {order.number} is {order.status}: only a closed or cancelled"
                    " order reopens",
                )
            restored: list[tuple[OrderItem, int | None]] = []
            if order.status == "closed":
                base: OrderStatus = "accepted"
            else:
                base, cancelled_items = self._last_cancel(conn, order)
                taken: list[tuple[OrderItem, ItemStatus, bool]] = []
                for item_id in cancelled_items:
                    item = self.store.item(item_id, conn=conn)
                    if item is None or item.order_id != order.id or item.status != "cancelled":
                        continue
                    status, priced = self._reopened_status(conn, item)
                    taken.append((item, status, priced))
                others_open = any(
                    item.status != "cancelled" for item in self.store.items(order.id, conn=conn)
                )
                if base in DERIVED and not others_open and not any(p for _, _, p in taken):
                    # Nothing would come back priced: an accepted order with no open item
                    # is one `accept_order` refuses, so it reopens open for edits instead,
                    # with every item the cancel took.
                    base = "draft"
                for item, status, priced in taken:
                    # Past `quoted` an unpriced item comes back where the order takes one
                    # (`TAKES_ITEMS`, #5349); a cancel is refused once shipped, so every
                    # base here takes one.
                    if not priced and base not in OPEN_FOR_EDITS | TAKES_ITEMS:
                        continue
                    restored.append((self.store.update_item(conn, item.id, status=status), None))
            # The status its rows give it now, written once, on one audit row.
            items = self.store.items(order.id, conn=conn)
            if base in OPEN_FOR_EDITS:
                target = _settle_open(items)
            else:
                attempted = {
                    item.id for item in items if self.store.fulfilments(item.id, conn=conn)
                }
                target = derive(
                    order.model_copy(update={"status": "accepted"}),
                    items,
                    self.store.shipments(order.id, conn=conn),
                    attempted,
                )
            reopened = self.store.reopen_order_row(conn, order.id, target)
            self._event(
                conn,
                order,
                by,
                "order.status",
                from_status=order.status,
                to=target,
                reopened=True,
                items=[item.id for item, _ in restored],
            )
            self._hold(
                conn,
                (
                    quote
                    for item in items
                    if item.status != "cancelled"
                    for quote in self._standing(conn, item.id)
                ),
            )
        self._told(_Change(reopened, restored))
        return reopened

    def reopen_item(self, order_id: str, item_id: str, by: Actor) -> OrderItem:
        """Undo a cancel: the item takes the state its quotes and attempts give it
        (`_reopened_status`), and its quotes' sliced files are held again. Refused once
        the order ended (reopen the order instead). An item with no accepted quote comes
        back as `draft` or `quoted` while the order takes a new item (`draft`, `quoted`,
        and `TAKES_ITEMS`, #5349), on hold too, as every reopen; on a `shipped` order it
        is refused (`item_not_priced`), since a shipped order takes no unpriced item."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            item = self._item_of(conn, order, item_id)
            if item.status != "cancelled":
                raise LifecycleError(
                    "item_not_cancelled",
                    f"item {item_id} is {item.status}: only a cancelled item reopens",
                    items=[item.id],
                )
            status, priced = self._reopened_status(conn, item)
            if not priced and order.status not in OPEN_FOR_EDITS | TAKES_ITEMS:
                raise LifecycleError(
                    "item_not_priced",
                    f"item {item_id} has no accepted quote, and order {order.number} is"
                    f" {order.status}: a shipped order takes back a priced item only",
                    items=[item.id],
                )
            item = self.store.update_item(conn, item.id, status=status)
            self._event(conn, order, by, "item.reopened", item_id=item.id, to=status)
            order = self._rederive(conn, order, by)
            self._hold(conn, self._standing(conn, item.id))
        self._told(_Change(order, [(item, None)]))
        return item

    # -- items (§3.3) ---------------------------------------------------------------------

    def _open_for_items(self, order: Order, what: str) -> None:
        if order.status not in OPEN_FOR_EDITS:
            raise LifecycleError(
                "item_locked",
                f"order {order.number} is {order.status}: {what} is pinned once accepted",
            )

    def _open_for_item(self, conn: Conn, order: Order, item: OrderItem, what: str) -> None:
        """As :meth:`_open_for_items`, but an item with no accepted quote on an order
        past `quoted` stays open (:func:`item_open`, #5349)."""
        if not item_open(order, self.store.quotes(item.id, conn=conn)):
            raise LifecycleError(
                "item_locked",
                f"order {order.number} is {order.status}: {what} is pinned once accepted",
                items=[item.id],
            )

    def _open_for_requote(self, conn: Conn, order: Order, item: OrderItem, what: str) -> None:
        """As :meth:`_open_for_item`, but an item revised after acceptance stays open for
        its design and price (:func:`requote_open`, #5350). The refusal names the route
        that revises an accepted item."""
        if not requote_open(order, item, self.store.quotes(item.id, conn=conn)):
            route = (
                f"; revise it with POST /orders/{order.id}/items/{item.id}"
                "/revisions/after-acceptance"
                if order.status in TAKES_ITEMS and item.kind not in NO_REVISIONS
                else ""
            )
            raise LifecycleError(
                "item_locked",
                f"order {order.number} is {order.status}: {what} is pinned once accepted{route}",
                items=[item.id],
            )

    def _takes_item(self, order: Order, after_acceptance: bool) -> None:
        """The checks before an item is added. A plain add needs a `draft` or `quoted`
        order. An add after acceptance (#5349) needs an `accepted`, `in_production` or
        `ready` order that is not on hold: it changes what the customer gets."""
        self._not_ended(order)
        if not after_acceptance:
            if order.status in TAKES_ITEMS:
                raise LifecycleError(
                    "item_locked",
                    f"order {order.number} is {order.status}: the list of items is pinned once"
                    f" accepted; add an item with POST /orders/{order.id}/items/after-acceptance",
                )
            self._open_for_items(order, "the list of items")
            return
        if order.status in OPEN_FOR_EDITS:
            raise LifecycleError(
                "order_not_accepted",
                f"order {order.number} is {order.status}, not accepted: add the item with"
                f" POST /orders/{order.id}/items",
            )
        if order.status not in TAKES_ITEMS:
            raise LifecycleError(
                "item_locked",
                f"order {order.number} is {order.status}: it takes no new item once shipped",
            )
        self._not_on_hold(order)

    def _added(
        self,
        conn: Conn,
        order: Order,
        item: OrderItem,
        by: Actor,
        after_acceptance: bool,
        **detail: Any,
    ) -> None:
        """The add's audit row. After acceptance it is ``item.added_after_acceptance``,
        naming the item and the status the order had (#5349)."""
        if after_acceptance:
            self._event(
                conn,
                order,
                by,
                "item.added_after_acceptance",
                item_id=item.id,
                title=item.title,
                quantity=item.quantity,
                order_status=order.status,
                **detail,
            )
        else:
            self._event(conn, order, by, "item.added", item_id=item.id, **detail)

    def _revise(
        self,
        conn: Conn,
        order: Order,
        item: OrderItem,
        by: Actor,
        *,
        reference: Reference,
        colours: Sequence[ColourSlot],
        note: str = "",
        output_id: str | UnsetType | None = UNSET,
        amend: bool = False,
        repair: bool = False,
        after_acceptance: bool = False,
    ) -> tuple[OrderItem, Revision]:
        """The item's next design revision, and the item's design a copy of it (spec
        2026-11-02 §4.1): the one caller of `OrderStore.set_design`. The newest revision,
        when it is sent and unanswered, is `superseded`. An unset ``output_id`` keeps the
        item's while the reference stays, and clears it when the reference moves: the
        output no longer matches what the item says. Every quote goes stale (§4.3), so
        a `quoted` or `approved` item is `draft` again. The caller checks the item may
        change and rederives the order.

        With ``amend`` the current revision takes ``colours`` in place (the colours'
        fill: an item made by hand given the slots its first render made, which changes
        no design a customer saw): no new revision, nothing superseded, the quotes and
        the status kept, the audit row `revision.amended`.

        With ``repair`` (`_ensure_revision`) the new revision is the design the item
        already has: a sent, unanswered one is superseded, but the quotes and the
        status are kept, and the audit row is `revision.repaired`.

        With ``after_acceptance``, or on an item already revised after acceptance, while
        the order takes items (#5350), the accepted quote stays accepted: the price the
        customer agreed stands until a quote of the new revision is accepted. The item is
        marked ``revised_after_acceptance``, a `REQUOTED` one is `draft` again (so it is
        not printed or fulfilled at the old quote), and the audit row is
        `revision.added_after_acceptance`, naming the quote kept."""
        if isinstance(output_id, UnsetType):
            output_id = item.output_id if item.reference == reference else None
        if amend:
            assert item.current_revision_id is not None
            revision = self.store.amend_revision_colours(conn, item.current_revision_id, colours)
            item = self.store.set_design(conn, item.id, revision, output_id=output_id)
            self._event(
                conn, order, by, "revision.amended", item_id=item.id, revision_id=revision.id
            )
            return item, revision
        newest = next(reversed(self.store.revisions(item.id, conn=conn)), None)
        superseded = None
        if newest is not None and newest.sent_at is not None and newest.answer is None:
            self.store.stamp_revision(
                conn, newest.id, answer="superseded", answered_at=datetime.now(UTC), answered_by=by
            )
            superseded = newest.id
        revision = self.store.insert_revision(
            conn, item.id, reference=reference, colours=colours, note=note, created_by=by
        )
        requote = (
            not repair
            and order.status in TAKES_ITEMS
            and (after_acceptance or item.revised_after_acceptance)
        )
        kept = None
        if requote:
            kept = next((q.id for q in self.store.quotes(item.id, conn=conn) if q.accepted), None)
        item = self.store.set_design(conn, item.id, revision, output_id=output_id)
        detail: dict[str, Any] = {}
        if requote:
            item = self.store.update_item(
                conn,
                item.id,
                revised_after_acceptance=True,
                status="draft" if item.status in REQUOTED else UNSET,
            )
            detail = {"order_status": order.status, "accepted_quote_id": kept}
        elif not repair:
            self.store.unaccept_quotes(conn, item.id)
            if item.status in ("quoted", "approved"):
                item = self.store.update_item(conn, item.id, status="draft")
        kind = "revision.repaired" if repair else "revision.added"
        self._event(
            conn,
            order,
            by,
            "revision.added_after_acceptance" if requote else kind,
            item_id=item.id,
            revision_id=revision.id,
            number=revision.number,
            superseded=superseded,
            **detail,
        )
        return item, revision

    def _ensure_revision(self, conn: Conn, order: Order, item: OrderItem, by: Actor) -> OrderItem:
        """The rollout's repair (spec 2026-11-02 §4.1): an item with a design whose copy
        is not its current revision's, because a build before revisions wrote it (no
        current revision) or edited it (a reference or colours its revision lacks), gets
        its next revision made from the copy (`_revise` with ``repair``). Anything else,
        and an item of an ended order or a settled item (whose record stays as it was), is
        returned as it is, writing nothing."""
        if item.kind == "custom" or item.reference is None:
            return item
        if order.status in ENDED or item.status in SETTLED:
            return item
        if item.current_revision_id is not None:
            current = self.store.revision(item.current_revision_id, conn=conn)
            if (
                current is not None
                and current.reference == item.reference
                and current.colours == item.colours
            ):
                return item
        item, _ = self._revise(
            conn,
            order,
            item,
            by,
            reference=item.reference,
            colours=list(item.colours),
            repair=True,
        )
        return item

    def ensure_revision(self, order_id: str, item_id: str, by: Actor) -> OrderItem:
        """`_ensure_revision` in a transaction of its own, so the repair stands when the
        call that needed it is refused (a send naming the revision it superseded). Every
        path that reads the item's current revision runs it first."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            before = self._item_of(conn, order, item_id)
            item = self._ensure_revision(conn, order, before, by)
        if item is not before:
            self._told(_Change(order, [(item, None)]))
        return item

    def add_revision(
        self,
        order_id: str,
        item_id: str,
        by: Actor,
        resolved: ResolvedReference | None = None,
        *,
        output_id: str | UnsetType | None = UNSET,
        filaments: Mapping[int, str | None] | None = None,
        note: str = "",
        after_acceptance: bool = False,
    ) -> Revision:
        """A new design for the item while its order is open (§4.3). Without
        ``resolved`` it repeats the current revision's reference and colours: a revision
        for new photos or a new note. ``filaments`` repins its slots as `update_item`'s
        does. A custom item has no design (`item_custom`); a request has none until its
        first revision names one (`item_has_no_design`). Past `quoted` an item with an
        accepted quote takes one only while it was revised after acceptance
        (:func:`requote_open`).

        With ``after_acceptance`` (#5350) the item is an accepted one, on an `accepted`,
        `in_production` or `ready` order not on hold: `order_not_accepted` before that,
        `item_locked` once shipped, `item_not_priced` for an item with no accepted quote
        (use the plain route), `print_in_flight` while a print of it runs,
        `item_partly_fulfilled` once an attempt of it is accepted (#5592). Its accepted
        quote stays accepted until a quote of the new revision is (`_revise`)."""
        self.ensure_revision(order_id, item_id, by)
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            item = self._item_of(conn, order, item_id)
            if item.kind == "custom":
                raise LifecycleError("item_custom", f"item {item_id} is a custom item")
            if item.kind == "library_file":
                raise LifecycleError(
                    "item_library_file",
                    f"item {item_id} prints a library file: it has no design revisions;"
                    " add a new item to order something else",
                    items=[item.id],
                )
            if item.status in SETTLED:
                raise LifecycleError(
                    "item_not_open", f"item {item_id} is {item.status}", items=[item.id]
                )
            if after_acceptance:
                self._revisable_after_acceptance(conn, order, item)
            else:
                self._open_for_requote(conn, order, item, "an item's design")
            if resolved is not None:
                reference, colours = resolved.reference, list(resolved.colours)
            elif item.reference is not None:
                reference, colours = item.reference, list(item.colours)
            else:
                raise LifecycleError("item_has_no_design", f"item {item_id} has no design yet")
            if filaments is not None:
                colours = self._with_filaments(conn, colours, filaments)
            item, revision = self._revise(
                conn,
                order,
                item,
                by,
                reference=reference,
                colours=colours,
                note=note,
                output_id=output_id,
                after_acceptance=after_acceptance,
            )
            order = self._rederive(conn, order, by)
        self._told(_Change(order, [(item, None)]))
        return revision

    def _revisable_after_acceptance(self, conn: Conn, order: Order, item: OrderItem) -> None:
        """The checks before an accepted item is revised after acceptance (#5350), past
        the item's own (not custom, not settled)."""
        if order.status in OPEN_FOR_EDITS:
            raise LifecycleError(
                "order_not_accepted",
                f"order {order.number} is {order.status}, not accepted: revise the item with"
                f" POST /orders/{order.id}/items/{item.id}/revisions",
                items=[item.id],
            )
        if order.status not in TAKES_ITEMS:
            raise LifecycleError(
                "item_locked",
                f"order {order.number} is {order.status}: its items' designs are final once"
                " shipped",
                items=[item.id],
            )
        self._not_on_hold(order)
        if not any(quote.accepted for quote in self.store.quotes(item.id, conn=conn)):
            raise LifecycleError(
                "item_not_priced",
                f"item {item.id} has no accepted quote: revise it with"
                f" POST /orders/{order.id}/items/{item.id}/revisions",
                items=[item.id],
            )
        accepted = [f for f in self.store.fulfilments(item.id, conn=conn) if f.accepted_at]
        if accepted:
            # #5592: the accepted units are real goods of the agreed design. A new design
            # would count them as made of it, or leave them uncounted; neither is true.
            # This refusal is permanent and `print_in_flight` is transient, so it answers
            # first: a caller that waits for the print to end is not refused after (#5598).
            made = sum(f.quantity for f in accepted)
            raise LifecycleError(
                "item_partly_fulfilled",
                f"item {item.id} has accepted attempts ({made} of {item.quantity} made of the"
                " agreed design): cancel the item and add one with the new design",
                items=[item.id],
            )
        if item.status in ("queued", "printing"):
            raise LifecycleError(
                "print_in_flight",
                f"item {item.id} is {item.status}: settle its print first",
                items=[item.id],
            )
        self._none_in_flight(conn, item)

    def add_item(
        self,
        order_id: str,
        by: Actor,
        resolved: ResolvedReference,
        *,
        output_id: str | None,
        quantity: int,
        title: str | None = None,
        notes: str = "",
        sku: str | None = None,
        after_acceptance: bool = False,
    ) -> OrderItem:
        """A model item. With ``after_acceptance`` (#5349) the order must be past
        `quoted` (:meth:`_takes_item`); the item starts `draft` with no quote either way,
        and the order's accepted prices and charges stay as they are."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._takes_item(order, after_acceptance)
            item = self.store.insert_item(
                conn,
                order.id,
                reference=resolved.reference,
                colours=resolved.colours,
                output_id=output_id,
                quantity=quantity,
                title=title,
                notes=notes,
                sku=sku,
            )
            self._added(conn, order, item, by, after_acceptance)
            item, _ = self._revise(
                conn,
                order,
                item,
                by,
                reference=resolved.reference,
                colours=resolved.colours,
                output_id=output_id,
            )
            rule = self.parts.rule(resolved.reference.slug, conn=conn)
            given = rule_parts(rule.lines, resolved.colours)
            if given:
                self.parts.set_item_parts(conn, item.id, given)
                self._event(
                    conn,
                    order,
                    by,
                    "item.parts_set",
                    item_id=item.id,
                    parts=_parts_detail(given),
                    from_rule=True,
                )
            order = self._rederive(conn, order, by)
        self._told(_Change(order, [(item, None)]))
        return item

    def add_request_item(
        self,
        order_id: str,
        by: Actor,
        *,
        title: str,
        quantity: int,
        notes: str = "",
        sku: str | None = None,
        after_acceptance: bool = False,
    ) -> OrderItem:
        """A request: what a customer wants made, before any design exists. It has no
        reference, and is approved design first; its first revision (`add_revision`
        with a resolved reference) gives it one. ``after_acceptance`` as `add_item`'s."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._takes_item(order, after_acceptance)
            item = self.store.insert_item(
                conn,
                order.id,
                kind="request",
                reference=None,
                colours=[],
                output_id=None,
                quantity=quantity,
                title=title,
                notes=notes,
                sku=sku,
                approval="design_first",
            )
            self._added(conn, order, item, by, after_acceptance, item_kind="request")
            order = self._rederive(conn, order, by)
        self._told(_Change(order, [(item, None)]))
        return item

    def add_library_item(
        self,
        order_id: str,
        by: Actor,
        file: LibraryFileRef,
        *,
        quantity: int,
        title: str | None = None,
        notes: str = "",
        sku: str | None = None,
        after_acceptance: bool = False,
    ) -> OrderItem:
        """A library file item (#5544): the file's id, name and library, with no template
        commit and no inputs. It starts `draft`, unpriced, approved with its quote (it has
        no design to approve first), and takes a slice quote of the file or a manual one.
        The caller has read the file from the library that serves. ``after_acceptance``
        as `add_item`'s."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._takes_item(order, after_acceptance)
            item = self.store.insert_library_item(
                conn,
                order.id,
                file=file,
                quantity=quantity,
                title=title,
                notes=notes,
                sku=sku,
            )
            self._added(
                conn,
                order,
                item,
                by,
                after_acceptance,
                item_kind="library_file",
                library_file_id=file.id,
            )
            order = self._rederive(conn, order, by)
        self._told(_Change(order, [(item, None)]))
        return item

    def add_custom_item(
        self,
        order_id: str,
        by: Actor,
        *,
        title: str,
        quantity: int,
        unit_price_minor: int,
        notes: str = "",
        sku: str | None = None,
        requires_printing: bool = False,
        accept: bool = True,
        after_acceptance: bool = False,
    ) -> OrderItem:
        """A custom item (#4954): a title, a quantity and a typed price, with no template
        behind it. Its price is a `manual` quote. With ``accept`` (the browser's) it is
        accepted as it is saved, so the item is `approved` at once, and a hold refuses it
        (accepting a quote advances the order). Without (the agent's, whose `write` tier
        accepts nothing) the item is `quoted`, for `accept_quote` to pin. With
        ``after_acceptance`` (#5349) the price is never accepted as it is saved: the item
        is `quoted`, and ``accept`` is refused (422)."""
        if after_acceptance and accept:
            raise LifecycleError(
                "accept",
                "accept: an item added after acceptance needs its quote accepted on its own",
                unprocessable=True,
            )
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._takes_item(order, after_acceptance)
            if accept:
                self._not_on_hold(order)
            item = self.store.insert_custom_item(
                conn,
                order.id,
                title=title,
                quantity=quantity,
                notes=notes,
                sku=sku,
                requires_printing=requires_printing,
            )
            self._added(conn, order, item, by, after_acceptance, item_kind="custom")
            item = self._custom_price(conn, order, item, unit_price_minor, by, accept=accept)
            order = self._rederive(conn, order, by)
        self._told(_Change(order, [(item, None)]))
        return item

    def _custom_price(
        self,
        conn: Conn,
        order: Order,
        item: OrderItem,
        unit_price_minor: int,
        by: Actor,
        *,
        accept: bool,
    ) -> OrderItem:
        """A custom item's typed price as a `manual` quote at its quantity now, on the
        audit line: accepted and the item `approved`, or else the item `quoted`."""
        line = QuoteLine(
            kind="manual",
            label="Custom item",
            quantity=1,
            unit_minor=unit_price_minor,
            amount_minor=unit_price_minor,
        )
        quote = self.store.insert_quote(
            conn,
            item.id,
            source="manual",
            lines=[line],
            unit_cost_minor=unit_price_minor,
            unit_price_minor=unit_price_minor,
            quantity=item.quantity,
            created_by=by,
        )
        if not accept:
            self._event(conn, order, by, "quote.recorded", item_id=item.id, quote_id=quote.id)
            return self.store.update_item(conn, item.id, status="quoted")
        self.store.accept_quote(conn, item.id, quote.id)
        self._event(conn, order, by, "quote.accepted", item_id=item.id, quote_id=quote.id)
        return self.store.update_item(conn, item.id, status="approved")

    def update_item(
        self,
        order_id: str,
        item_id: str,
        by: Actor,
        *,
        reference: ResolvedReference | None = None,
        output_id: str | UnsetType | None = UNSET,
        quantity: int | None = None,
        title: str | None = None,
        notes: str | None = None,
        filaments: Mapping[int, str | None] | None = None,
        colours: Sequence[ColourSlot] | None = None,
        unit_price_minor: int | None = None,
        sku: str | UnsetType | None = UNSET,
        requires_printing: bool | None = None,
        accept: bool = True,
        approval: Approval | None = None,
        expected_revision_id: str | None = None,
    ) -> OrderItem:
        """The reference (replaced whole), the quantity and the slots' filaments while
        `draft` or `quoted`, or past that while the item has no accepted quote
        (:func:`item_open`, #5349), each dropping the accepted quote (Ruling 1; #4250 Ruling 6:
        §5.2's "changing the colours makes the next quote a new row"); title, notes and
        ``approval`` in any open state. A design change (a different reference or a
        slot's filament) is the item's next revision (spec 2026-11-02 §4.1, `_revise`);
        the item's own reference sent again changes nothing and makes none. A reference
        changed without an ``output_id`` clears the item's: the output it pointed at no
        longer matches what the item says.

        ``filaments`` (``{slot: filament_id | None}``) repins only when it changes a
        slot: the same filaments sent again write nothing and drop nothing. A slot the
        item lacks, or a filament not in the book or archived, is refused (422).
        ``colours`` gives an item made by hand, which has none, the slots its first
        render made (the quote's run, #4250); it repins nothing, as it changes no slot
        the item had, and amends the current revision rather than making one.

        A custom item (#4954) takes ``unit_price_minor`` and ``requires_printing``, which
        a model item refuses (422). A new quantity or price on it is priced at once: a
        new `manual` quote at the price sent, else at the price it has (its accepted
        quote's, or its newest's). With ``accept`` that quote is accepted and the item
        stays `approved` (refused on hold, as an accepted quote is); without (the
        agent's) the item is `quoted`. A ``reference`` given to it makes it a model item,
        on the normal flow: its quote is dropped as any reference change drops one. A
        custom item takes no ``approval`` (422): it has no design to approve.

        With ``expected_revision_id`` (the quote's run) it writes nothing once the item's
        current revision is another (`revision_not_newest`): the caller's work was of a
        design since replaced."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            item = self._item_of(conn, order, item_id)
            if item.status in SETTLED:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
            if (
                expected_revision_id is not None
                and item.current_revision_id != expected_revision_id
            ):
                raise LifecycleError(
                    "revision_not_newest",
                    f"item {item_id} has a newer revision than the one rendered",
                    items=[item.id],
                )
            library = item.kind == "library_file"
            if library and reference is not None:
                raise LifecycleError(
                    "reference",
                    "reference: a library file item prints its file; add a new item to order"
                    " a template",
                    unprocessable=True,
                )
            if library and approval == "design_first":
                raise LifecycleError(
                    "approval",
                    "approval: a library file item has no design to approve first",
                    unprocessable=True,
                )
            custom = item.kind == "custom" and reference is None
            for name, value in (
                ("unit_price_minor", unit_price_minor),
                ("requires_printing", requires_printing),
            ):
                if value is not None and not custom:
                    raise LifecycleError(
                        name, f"{name}: only a custom item takes one", unprocessable=True
                    )
            if approval is not None and custom:
                raise LifecycleError(
                    "approval", "approval: a custom item has no design", unprocessable=True
                )
            price = None
            if custom:
                quotes = self.store.quotes(item.id, conn=conn)
                accepted = next((q for q in quotes if q.accepted), None)
                newest = accepted or max(quotes, key=lambda q: (q.created_at, q.id), default=None)
                current = None if newest is None else newest.unit_price_minor
                wanted = current if unit_price_minor is None else unit_price_minor
                if quantity == item.quantity:
                    quantity = None
                # A price sent to be accepted when none is: the same amount, accepted now.
                unpinned = accept and accepted is None and unit_price_minor is not None
                if wanted is not None and (quantity is not None or wanted != current or unpinned):
                    price = wanted
            base = list(reference.colours if reference is not None else item.colours)
            slots = base if base or colours is None else list(colours)
            if filaments is not None:
                slots = self._with_filaments(conn, slots, filaments)
            # Only a slot the item had can change; one the fill brings had no filament.
            before = {slot.slot: slot.filament_id for slot in base}
            refiled = any(
                slot.slot in before and before[slot.slot] != slot.filament_id for slot in slots
            )
            # The colours' fill: an item with no slots given its first render's.
            filled = not base and bool(slots)
            designed = reference.reference if reference is not None else item.reference
            amend = (
                filled
                and item.kind != "custom"
                and designed == item.reference
                and item.current_revision_id is not None
            )
            changed = reference is not None and (
                item.kind == "custom"
                or reference.reference != item.reference
                or slots != item.colours
            )
            revised = not amend and (changed or refiled or filled)
            # A library file item (#5544) has no revisions: its slots are written on it.
            recolour = library and (refiled or filled)
            if library:
                amend, revised = False, refiled
            design = not library and (revised or amend)
            repins = revised or quantity is not None or price is not None
            if repins and (quantity is not None or price is not None):
                self._open_for_item(
                    conn, order, item, "an item's reference, quantity and filaments"
                )
            elif repins:
                # A design change alone: an item revised after acceptance takes it (#5350).
                self._open_for_requote(conn, order, item, "an item's reference and filaments")
            if price is not None and accept:
                self._not_on_hold(order)
            if design and designed is None:
                raise LifecycleError(
                    "colours", f"item {item_id} has no design to colour", unprocessable=True
                )
            fields: dict[str, Any] = {}
            if not design and not isinstance(output_id, UnsetType):
                fields["output_id"] = output_id
            if quantity is not None:
                fields["quantity"] = quantity
            if title is not None:
                fields["title"] = title
            if notes is not None:
                fields["notes"] = notes
            if not isinstance(sku, UnsetType) and sku != item.sku:
                fields["sku"] = sku
            if requires_printing is not None and requires_printing != item.requires_printing:
                fields["requires_printing"] = requires_printing
            if approval is not None and approval != item.approval:
                fields["approval"] = approval
            if not fields and price is None and not design and not recolour:
                # Nothing changes (the item's own filaments sent again): nothing written.
                return item
            if design:
                assert designed is not None
                item, _ = self._revise(
                    conn,
                    order,
                    item,
                    by,
                    reference=designed,
                    colours=slots,
                    output_id=output_id,
                    amend=amend,
                )
            if recolour:
                item = self.store.set_library_colours(conn, item.id, slots)
            if repins and (library or not revised):
                # A revision drops the quotes itself; a quantity or price change here, and
                # any repin of a library file item, which has no revisions.
                self.store.unaccept_quotes(conn, item.id)
                fields["status"] = "draft"
            if fields:
                item = self.store.update_item(conn, item.id, **fields)
            if price is not None:
                item = self._custom_price(conn, order, item, price, by, accept=accept)
            self._event(conn, order, by, "item.updated", item_id=item.id, repinned=repins)
            order = self._rederive(conn, order, by)
        self._told(_Change(order, [(item, None)]))
        return item

    def _with_filaments(
        self, conn: Conn, slots: list[ColourSlot], filaments: Mapping[int, str | None]
    ) -> list[ColourSlot]:
        """``slots`` with ``filaments`` applied, each named record one of the book's."""
        known = {slot.slot for slot in slots}
        unknown = sorted(set(filaments) - known)
        if unknown:
            raise LifecycleError(
                "filaments",
                f"the item has no colour slot {', '.join(map(str, unknown))}",
                unprocessable=True,
            )
        for slot, filament_id in filaments.items():
            if filament_id is None:
                continue
            record = self.store.filament(filament_id, conn=conn)
            if record is None:
                raise LifecycleError(
                    "filaments",
                    f"slot {slot}: no filament {filament_id} in the price book",
                    unprocessable=True,
                )
            if record.archived_at is not None:
                raise LifecycleError(
                    "filaments",
                    f"slot {slot}: filament {filament_id} is archived",
                    unprocessable=True,
                )
        return [
            slot.model_copy(update={"filament_id": filaments[slot.slot]})
            if slot.slot in filaments
            else slot
            for slot in slots
        ]

    def delete_item(self, order_id: str, item_id: str, by: Actor) -> None:
        """Only a `draft` item with no print attempt, a test print's too, deletes
        (Ruling 6, 10); else `item_not_draft`, and the caller decides whether to cancel
        it."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            item = self._item_of(conn, order, item_id)
            if self.store.fulfilments(item.id, conn=conn, purpose=None):
                # A test print's attempt and the sliced file it holds (storefront spec
                # §4.4) stay with the record: an item printed once is cancelled.
                raise LifecycleError(
                    "item_not_draft",
                    f"item {item_id} has print attempts: cancel it instead"
                    f" (POST /orders/{order_id}/items/{item_id}/cancel)",
                    items=[item.id],
                )
            if self.store.item_has_entries(conn, item.id):
                # Logged time and costs are the job's record (#5326): never dropped with
                # the item, and its delete has no audit row for them.
                raise LifecycleError(
                    "item_has_entries",
                    f"item {item_id} has time or cost entries: remove them or cancel it"
                    f" (POST /orders/{order_id}/items/{item_id}/cancel)",
                    items=[item.id],
                )
            # Its files go with it, each on the audit line, and with them their holds
            # on the blobs: nothing could show or remove an item's file once the item
            # is gone. A refusal below rolls this back.
            subjects = [item_subject(item.id)] + [
                revision_subject(r.id) for r in self.store.revisions(item.id, conn=conn)
            ]
            for subject in subjects:
                for attachment in self.store.subject_attachments(subject, conn=conn):
                    self.store.delete_attachment(conn, attachment.id)
                    self._event(
                        conn,
                        order,
                        by,
                        "attachment.removed",
                        attachment_id=attachment.id,
                        item_id=item.id,
                        filename=attachment.filename,
                        sha256=attachment.sha256,
                        was_delivered=item.delivered_attachment_id == attachment.id,
                    )
            if not self.store.delete_draft_item(conn, item.id):
                raise LifecycleError(
                    "item_not_draft",
                    f"item {item_id} is {item.status}: cancel it instead"
                    f" (POST /orders/{order_id}/items/{item_id}/cancel)",
                    items=[item.id],
                )
            self._event(conn, order, by, "item.deleted", item_id=item.id)
            order = self._rederive(conn, order, by)
        self._told(_Change(order))

    def cancel_item(self, order_id: str, item_id: str, by: Actor) -> OrderItem:
        """From any open state (§3.5); allowed on hold (Ruling 14)."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            item = self._item_of(conn, order, item_id)
            if item.status in SETTLED:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
            item = self.store.update_item(conn, item.id, status="cancelled")
            self._event(conn, order, by, "item.cancelled", item_id=item.id)
            order = self._rederive(conn, order, by)
            # §3.4: cancelling the item releases every quote's sliced file.
            released = self._quote_ids(conn, [item.id])
        self._told(_Change(order, [(item, None)], released, ended=True))
        return item

    # -- quotes (§3.4) --------------------------------------------------------------------

    def record_manual_quote(
        self, order_id: str, item_id: str, body: ManualQuoteInput, by: Actor
    ) -> Quote:
        """A typed price (Ruling 1): one `manual` line, cost = price, quantity the
        item's now; the item is `quoted` (unless already approved on another quote). A
        custom item's price is its own field (#4954): ``item_custom``."""
        self.ensure_revision(order_id, item_id, by)
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            item = self._item_of(conn, order, item_id)
            self._open_for_requote(conn, order, item, "an item's price")
            if item.status in SETTLED:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
            if item.kind == "custom":
                raise LifecycleError(
                    "item_custom",
                    f"item {item_id} is a custom item: its price is its own field"
                    " (PATCH the item's unit_price_minor)",
                    items=[item.id],
                )
            if not has_design(item):
                raise LifecycleError(
                    "item_has_no_design", f"item {item_id} has no design yet", items=[item.id]
                )
            line = QuoteLine(
                kind="manual",
                label=body.label,
                quantity=1,
                unit_minor=body.unit_price_minor,
                amount_minor=body.unit_price_minor,
            )
            quote = self.store.insert_quote(
                conn,
                item.id,
                source="manual",
                lines=[line],
                unit_cost_minor=body.unit_price_minor,
                unit_price_minor=body.unit_price_minor,
                quantity=item.quantity,
                created_by=by,
                revision_id=item.current_revision_id,
            )
            change = self._recorded(conn, order, item, quote, by)
        self._told(change)
        return quote

    def _recorded(
        self, conn: Conn, order: Order, item: OrderItem, quote: Quote, by: Actor
    ) -> _Change:
        """What recording ``quote`` does past its row: the item `quoted` from `draft`,
        the audit row, the order rederived, and §3.4's release of the item's older
        unaccepted quotes' sliced files (the newest unaccepted one is this)."""
        changed: list[tuple[OrderItem, int | None]] = []
        if item.status == "draft":
            item = self.store.update_item(conn, item.id, status="quoted")
            changed.append((item, None))
        self._event(conn, order, by, "quote.recorded", item_id=item.id, quote_id=quote.id)
        order = self._rederive(conn, order, by)
        released = [
            other.id
            for other in self.store.quotes(item.id, conn=conn)
            if not other.accepted and other.id != quote.id
        ]
        return _Change(order, changed, released)

    def record_slice_quote(
        self,
        order_id: str,
        item_id: str,
        priced: Priced,
        plates: Sequence[Plate],
        *,
        sliced_sha256: str,
        sliced_blob_key: str,
        by: Actor,
        revision_id: str | None,
        quote_id: str | None = None,
        part_settings: dict[str, dict[str, Any]] | None = None,
    ) -> Quote:
        """A `slice` quote (#4250, §5.2 step 5) as the ``order_item_quote`` run priced
        it, with the plates it read and the sliced file it read them from, under the id
        its ref was taken with (``quote_id``, Ruling 3). As a manual quote: the order
        open for items, the item not settled, allowed on hold (it advances nothing). It names
        the revision the run sliced; one since superseded is `revision_not_newest`.
        A ``quote_id`` already recorded (by an earlier attempt of the same operation,
        #5353) is answered with, under the order's lock, and nothing is written but the
        drop of this holder's holds on keys the recorded quote does not name (#5415: a
        re-slice that gave other bytes). The holder is shared by every attempt, so those
        holds may be a sibling attempt's that still runs; that attempt's own record is
        answered with this quote too, so it never needs them (#5448). The quote's holds
        are taken again in its own transaction, so a row never commits without them,
        whatever another attempt dropped before it. A library file item's quote (#5544)
        names no revision (``revision_id`` None): the item has none."""
        self.ensure_revision(order_id, item_id, by)
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            if quote_id is not None:
                recorded = self.store.quote(quote_id, conn=conn)
                if recorded is not None and recorded.order_item_id == item_id:
                    self.store.drop_holds(
                        conn, QUOTE_HOLDER, quote_id, keep=quote_file_keys(recorded)
                    )
                    return recorded
            self._not_ended(order)
            item = self._item_of(conn, order, item_id)
            self._open_for_requote(conn, order, item, "an item's price")
            if item.status in SETTLED:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
            if revision_id != item.current_revision_id:
                raise LifecycleError(
                    "revision_not_newest",
                    f"item {item_id} has a newer revision than the one sliced",
                    items=[item.id],
                )
            quote = self.store.insert_quote(
                conn,
                item.id,
                source="slice",
                lines=priced.lines,
                unit_cost_minor=priced.unit_cost_minor,
                unit_price_minor=priced.unit_price_minor,
                quantity=item.quantity,
                created_by=by,
                plates=[plate.model_dump(mode="json") for plate in plates],
                sliced_sha256=sliced_sha256,
                sliced_blob_key=sliced_blob_key,
                quote_id=quote_id,
                revision_id=revision_id,
                part_settings=part_settings,
            )
            self._hold(conn, [quote])
            change = self._recorded(conn, order, item, quote, by)
        self._told(change)
        return quote

    def drop_unrecorded_quote_holds(self, order_id: str, quote_id: str) -> bool:
        """A failed quote run's holds (Ruling 3), unless its quote was recorded (#5353:
        another attempt of the same operation may have recorded it): then only those on
        keys the quote does not name (#5415). Under the order's lock, which
        :meth:`record_slice_quote` takes too, so a drop never lands between that
        record's check and its commit. Whether it dropped them all."""
        with self.store.transaction() as conn:
            self._lock(conn, order_id)
            recorded = self.store.quote(quote_id, conn=conn)
            if recorded is not None:
                self.store.drop_holds(conn, QUOTE_HOLDER, quote_id, keep=quote_file_keys(recorded))
                return False
            self.store.drop_holds(conn, QUOTE_HOLDER, quote_id)
        return True

    def accept_quote(self, order_id: str, item_id: str, quote_id: str, by: Actor) -> OrderItem:
        """Pins the quote (§3.4): the item is `approved`, the order `quoted` once every
        item not cancelled is. A quote of an earlier revision is `quote_stale`, and on a
        `design_first` item the quote's revision must be accepted first
        (`revision_not_accepted`); a custom item has neither."""
        self.ensure_revision(order_id, item_id, by)
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            item = self._item_of(conn, order, item_id)
            self._open_for_requote(conn, order, item, "an item's price")
            self._not_on_hold(order)
            quote = self.store.quote(quote_id, conn=conn)
            if quote is None or quote.order_item_id != item.id:
                raise LifecycleError(
                    "quote_not_found", f"no quote {quote_id} of item {item_id}", not_found=True
                )
            order, item, released = self._accept_quote(conn, order, item, quote, by)
        self._told(_Change(order, [(item, None)], released))
        return item

    def _accept_quote(
        self, conn: Conn, order: Order, item: OrderItem, quote: Quote, by: Actor
    ) -> tuple[Order, OrderItem, list[str]]:
        """Accepting ``quote`` once the caller has checked the order and the item: the
        refusals, the row, the audit line, the order rederived and §3.4's release of
        every other quote's sliced file."""
        if item.status in SETTLED:
            raise LifecycleError("item_not_open", f"item {item.id} is {item.status}")
        if quote.quantity != item.quantity:
            raise LifecycleError(
                "quote_stale",
                f"quote {quote.id} priced {quote.quantity}, the item is now {item.quantity}",
            )
        if item.kind not in NO_REVISIONS:
            if quote.revision_id is not None and quote.revision_id != item.current_revision_id:
                raise LifecycleError(
                    "quote_stale", f"quote {quote.id} prices an earlier revision of the design"
                )
            if item.approval == "design_first":
                revision_id = quote.revision_id or item.current_revision_id
                revision = (
                    None if revision_id is None else self.store.revision(revision_id, conn=conn)
                )
                if revision is None or revision.answer != "accepted":
                    raise LifecycleError(
                        "revision_not_accepted",
                        f"the customer has not accepted the design of item {item.id}",
                    )
        self.store.accept_quote(conn, item.id, quote.id)
        # #5350: the new price replaces the one kept since a revision after acceptance.
        item = self.store.update_item(
            conn, item.id, status="approved", revised_after_acceptance=False
        )
        self._event(conn, order, by, "quote.accepted", item_id=item.id, quote_id=quote.id)
        order = self._rederive(conn, order, by)
        released = [other for other in self._quote_ids(conn, [item.id]) if other != quote.id]
        return order, item, released

    # -- revisions sent and answered (storefront spec §4.3) -------------------------------

    def _newest(self, conn: Conn, item: OrderItem, revision_id: str) -> Revision:
        """The item's revision ``revision_id``, which must be its newest: a customer
        sees and answers only the latest design."""
        revisions = self.store.revisions(item.id, conn=conn)
        found = next((r for r in revisions if r.id == revision_id), None)
        if found is None:
            raise LifecycleError(
                "revision_not_found",
                f"no revision {revision_id} of item {item.id}",
                not_found=True,
            )
        if revisions[-1].id != found.id:
            raise LifecycleError(
                "revision_not_newest",
                f"revision {found.number} of item {item.id} is not its newest",
            )
        return found

    def _quote_of(self, conn: Conn, item: OrderItem, revision: Revision) -> Quote | None:
        """The newest quote that names ``revision``."""
        named = [q for q in self.store.quotes(item.id, conn=conn) if q.revision_id == revision.id]
        return named[-1] if named else None

    def send_revision(self, order_id: str, item_id: str, revision_id: str, by: Actor) -> Revision:
        """Sends the item's newest revision to the customer: it needs a file on its
        subject (`revision_empty`) and, on a `together` item, a quote that names it
        (`revision_not_quoted`). Sent and unanswered already returns it, writing nothing."""
        self.ensure_revision(order_id, item_id, by)
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            item = self._item_of(conn, order, item_id)
            if item.status in SETTLED:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
            revision = self._newest(conn, item, revision_id)
            if revision.answer is not None:
                raise LifecycleError(
                    "revision_answered", f"revision {revision.number} is {revision.answer}"
                )
            if revision.sent_at is not None:
                return revision
            if not self.store.subject_attachments(revision_subject(revision.id), conn=conn):
                raise LifecycleError(
                    "revision_empty", f"revision {revision.number} has no file to send"
                )
            if item.approval == "together" and self._quote_of(conn, item, revision) is None:
                raise LifecycleError(
                    "revision_not_quoted",
                    f"revision {revision.number} has no quote: this item is approved together",
                )
            revision = self.store.stamp_revision(conn, revision.id, sent_at=datetime.now(UTC))
            self._event(conn, order, by, "revision.sent", item_id=item.id, revision_id=revision.id)
        self._told(_Change(order, [(item, None)]))
        return revision

    def answer_revision(
        self,
        order_id: str,
        item_id: str,
        revision_id: str,
        body: RevisionAnswerInput,
        by: Actor,
    ) -> Revision:
        """The customer's answer to the newest sent revision. On a `together` item an
        `accepted` also accepts the newest quote that names it, in the same transaction."""
        if body.answer == "changes_requested" and not body.note.strip():
            raise LifecycleError("note", "note: say what should change", unprocessable=True)
        self.ensure_revision(order_id, item_id, by)
        released: list[str] = []
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            item = self._item_of(conn, order, item_id)
            if item.status in SETTLED:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
            revision = self._newest(conn, item, revision_id)
            if revision.answer is not None:
                raise LifecycleError(
                    "revision_answered", f"revision {revision.number} is {revision.answer}"
                )
            if revision.sent_at is None:
                raise LifecycleError(
                    "revision_not_sent", f"revision {revision.number} was not sent"
                )
            quote = None
            if body.answer == "accepted" and item.approval == "together":
                quote = self._quote_of(conn, item, revision)
                if quote is None:
                    raise LifecycleError(
                        "revision_not_quoted",
                        f"revision {revision.number} has no quote: this item is approved together",
                    )
                self._open_for_requote(conn, order, item, "an item's price")
                self._not_on_hold(order)
            revision = self.store.stamp_revision(
                conn,
                revision.id,
                answer=body.answer,
                answer_note=body.note,
                answered_at=datetime.now(UTC),
                answered_by=by,
            )
            self._event(
                conn,
                order,
                by,
                "revision.answered",
                item_id=item.id,
                revision_id=revision.id,
                answer=body.answer,
                note=body.note,
            )
            if quote is not None:
                order, item, released = self._accept_quote(conn, order, item, quote, by)
        self._told(_Change(order, [(item, None)], released))
        return revision

    # -- fulfilment (§6.1 by hand) ---------------------------------------------------------

    def _in_production(self, conn: Conn, order_id: str, item_id: str) -> tuple[Order, OrderItem]:
        """The order accepted and not on hold, and the item one of its own."""
        order = self._lock(conn, order_id)
        self._producing(order)
        return order, self._item_of(conn, order, item_id)

    def _producing(self, order: Order) -> None:
        """The order accepted, not ended and not on hold."""
        self._not_ended(order)
        if order.status not in DERIVED:
            raise LifecycleError(
                "order_not_accepted", f"order {order.number} is {order.status}, not accepted"
            )
        self._not_on_hold(order)

    def _fulfillable(self, conn: Conn, order_id: str, item_id: str) -> tuple[Order, OrderItem]:
        order, item = self._in_production(conn, order_id, item_id)
        if item.status not in FULFILLABLE:
            raise LifecycleError(
                "item_not_open" if item.status in SETTLED else "item_not_approved",
                f"item {item_id} is {item.status}",
            )
        return order, item

    @staticmethod
    def _after_attempt(outcome: Outcome) -> ItemStatus:
        return "failed" if outcome == "failed" else "approved"

    @staticmethod
    def _after(latest: Fulfilment, current: ItemStatus) -> ItemStatus:
        """The status the item's latest attempt gives it: `failed` when it failed; for a
        print's attempt (§5.4) `printed` once it made something, else the print state the
        item is in (`queued` if it is in none); for one by hand `approved`. A claim whose
        run is not attached yet is a print's: it keeps the item in its print state, so a
        second print stays refused."""
        if latest.outcome == "failed":
            return "failed"
        if latest.print_run_id is None and latest.print_request_id is None:
            return "approved"
        if latest.outcome in ("succeeded", "partial"):
            return "printed"
        return current if current in PRINTING else "queued"

    def record_fulfilment(
        self, order_id: str, item_id: str, body: FulfilmentInput, by: Actor
    ) -> Fulfilment:
        """The item's next attempt. A `failed` outcome fails the item; a new attempt on a
        failed item first returns it to `approved` (Ruling 4)."""
        with self.store.transaction() as conn:
            order, item = self._fulfillable(conn, order_id, item_id)
            if item.status == "failed":
                # Ruling 4: the new attempt first returns the item to `approved`; the
                # bus hears the attempt's own status once, below.
                item = self.store.update_item(conn, item.id, status="approved")
            if body.archive_id is not None and self.store.archive_claimed(conn, body.archive_id):
                # An archive is one print: another attempt's (this order's or any
                # other's) is never copied into this one (#5268 security review).
                raise LifecycleError(
                    "archive_claimed",
                    f"Bambuddy archive {body.archive_id} is already another attempt's print",
                )
            attempt = self.store.insert_fulfilment(
                conn,
                item.id,
                quantity=body.quantity,
                outcome=body.outcome,
                created_by=by,
                failure_reason=body.failure_reason,
                started_at=body.started_at,
                finished_at=body.finished_at,
                notes=body.notes,
                archive_ids=[] if body.archive_id is None else [body.archive_id],
            )
            status = self._after_attempt(body.outcome)
            if status != item.status:
                item = self.store.update_item(conn, item.id, status=status)
            changed: list[tuple[OrderItem, int | None]] = [(item, attempt.attempt)]
            self._event(
                conn,
                order,
                by,
                "fulfilment.recorded",
                item_id=item.id,
                fulfilment_id=attempt.id,
                attempt=attempt.attempt,
                outcome=attempt.outcome,
            )
            order = self._rederive(conn, order, by)
        self._told(_Change(order, changed))
        return attempt

    def _attempt_of(
        self,
        conn: Conn,
        item: OrderItem,
        fulfilment_id: str,
        purpose: FulfilmentPurpose | None = "production",
    ) -> Fulfilment:
        """The item's attempt ``fulfilment_id`` of ``purpose`` (None, either): a test
        print's attempt is no production attempt, so it is not found there."""
        attempt = self.store.fulfilment(fulfilment_id, conn=conn)
        if (
            attempt is None
            or attempt.order_item_id != item.id
            or (purpose is not None and attempt.purpose != purpose)
        ):
            raise LifecycleError(
                "fulfilment_not_found",
                f"no attempt {fulfilment_id} of item {item.id}",
                not_found=True,
            )
        return attempt

    def _before_priced_design(self, conn: Conn, item: OrderItem, attempt: Fulfilment) -> bool:
        """Whether the attempt was made before the revision the accepted quote prices: it
        printed an earlier design (#5350). The rule, and why the order of the two
        ``created_at`` values holds across their transactions, is :func:`stale_attempt`."""
        return stale_attempt(
            attempt,
            self.store.quotes(item.id, conn=conn),
            self.store.revisions(item.id, conn=conn),
        )

    def update_fulfilment(
        self, order_id: str, item_id: str, fulfilment_id: str, body: FulfilmentInput, by: Actor
    ) -> Fulfilment:
        """An attempt not yet accepted, re-read whole; the item follows the outcome of
        its latest attempt between `failed` and `approved`. A test print's attempt
        (storefront spec §4.4) is settled on an order not yet accepted too, and for an
        item in any state, and never moves the item."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            found = self.store.fulfilment(fulfilment_id, conn=conn)
            test = found is not None and found.purpose == "test"
            if test:
                self._not_ended(order)
                self._not_on_hold(order)
            else:
                self._producing(order)
            item = self._item_of(conn, order, item_id)
            attempt = self._attempt_of(conn, item, fulfilment_id, purpose=None)
            if attempt.accepted_at is not None:
                raise LifecycleError(
                    "fulfilment_accepted", f"attempt {attempt.attempt} is accepted"
                )
            if not test and item.status not in FULFILLABLE | PRINTING:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
            if (
                attempt.print_request_id is not None
                and attempt.print_run_id is None
                and self.store.print_starting(item.id, conn=conn)
            ):
                raise LifecycleError(
                    "print_starting",
                    f"attempt {attempt.attempt}'s print is still starting",
                    items=[item.id],
                )
            attempt = self.store.update_fulfilment(
                conn,
                attempt.id,
                quantity=body.quantity,
                outcome=body.outcome,
                failure_reason=body.failure_reason,
                started_at=body.started_at,
                finished_at=body.finished_at,
                notes=body.notes,
            )
            changed: list[tuple[OrderItem, int | None]] = []
            if not test:
                latest = self.store.fulfilments(item.id, conn=conn)[-1]
                status = self._after(latest, item.status)
                if status != item.status:
                    item = self.store.update_item(conn, item.id, status=status)
            changed.append((item, attempt.attempt))
            self._event(
                conn,
                order,
                by,
                "fulfilment.updated",
                item_id=item.id,
                fulfilment_id=attempt.id,
                attempt=attempt.attempt,
                outcome=attempt.outcome,
            )
            order = self._rederive(conn, order, by)
        self._told(_Change(order, changed))
        return attempt

    def accept_fulfilment(
        self, order_id: str, item_id: str, fulfilment_id: str, by: Actor
    ) -> Fulfilment:
        """§6.1: the attempt's quantity counts as made; the item is `fulfilled` once its
        accepted attempts reach its quantity. An attempt made before the accepted quote's
        revision printed an earlier design (an item revised after acceptance, #5350): it is
        refused (`fulfilment_stale`)."""
        with self.store.transaction() as conn:
            order, item = self._in_production(conn, order_id, item_id)
            attempt = self._attempt_of(conn, item, fulfilment_id)
            if attempt.accepted_at is not None:
                raise LifecycleError(
                    "fulfilment_accepted", f"attempt {attempt.attempt} is already accepted"
                )
            if item.status not in FULFILLABLE | {"printed"}:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
            if attempt.outcome == "failed":
                raise LifecycleError(
                    "fulfilment_failed", f"attempt {attempt.attempt} failed; nothing was made"
                )
            if self._before_priced_design(conn, item, attempt):
                raise LifecycleError(
                    "fulfilment_stale",
                    f"attempt {attempt.attempt} printed an earlier design than the accepted"
                    " quote's",
                    items=[item.id],
                )
            attempt = self.store.accept_fulfilment(conn, attempt.id, datetime.now(UTC))
            made = sum(
                f.quantity
                for f in self.store.fulfilments(item.id, conn=conn)
                if f.accepted_at is not None
            )
            changed: list[tuple[OrderItem, int | None]] = []
            if made >= item.quantity:
                item = self.store.update_item(conn, item.id, status="fulfilled")
                self._use_parts(conn, order, item, by)
            elif item.status == "printed":
                # Some are still to make: the next print is for the rest (§5.4 `copies`).
                item = self.store.update_item(conn, item.id, status="approved")
            changed.append((item, attempt.attempt))
            self._event(
                conn,
                order,
                by,
                "fulfilment.accepted",
                item_id=item.id,
                fulfilment_id=attempt.id,
                attempt=attempt.attempt,
                made=made,
            )
            order = self._rederive(conn, order, by)
        self._told(_Change(order, changed))
        return attempt

    def unfulfil_item(
        self, order_id: str, item_id: str, by: Actor, *, allowed: bool = True
    ) -> OrderItem:
        """Undo a fulfilment (#4952, §3.5 "Undo"): the item's latest accepted attempt
        is marked not accepted (the row stays), and the item takes the status its
        latest attempt gives it (`approved`, or `failed`). Refused for an item not
        `fulfilled` and for one a shipment carries (take it off the shipment first),
        and with ``allowed`` false (``orders_allow_unfulfil`` off, #4953) before
        anything is read."""
        if not allowed:
            raise LifecycleError(
                "unfulfil_disabled",
                "unfulfilling a line item is turned off in Settings (orders_allow_unfulfil)",
            )
        with self.store.transaction() as conn:
            order, item = self._in_production(conn, order_id, item_id)
            if item.status != "fulfilled":
                raise LifecycleError(
                    "item_not_fulfilled",
                    f"item {item_id} is {item.status}: only a fulfilled item unfulfils",
                    items=[item.id],
                )
            carrying = [
                shipment.id
                for shipment in self.store.shipments(order.id, conn=conn)
                if any(line.order_item_id == item.id for line in shipment.items)
            ]
            if carrying:
                raise LifecycleError(
                    "order_item_shipped",
                    f"item {item_id} is on a shipment: take it off the shipment first",
                    items=[item.id],
                    shipments=carrying,
                )
            attempts = self.store.fulfilments(item.id, conn=conn)
            # The accept that made it fulfilled: the latest one.
            _, _, accepted = max(
                (a.accepted_at, a.attempt, a) for a in attempts if a.accepted_at is not None
            )
            attempt = self.store.unaccept_fulfilment(conn, accepted.id)
            status = self._after(attempts[-1], item.status)
            item = self.store.update_item(conn, item.id, status=status)
            self._return_parts(conn, order, item, by)
            self._event(
                conn,
                order,
                by,
                "item.unfulfilled",
                item_id=item.id,
                fulfilment_id=attempt.id,
                attempt=attempt.attempt,
                to=status,
            )
            order = self._rederive(conn, order, by)
        self._told(_Change(order, [(item, attempt.attempt)]))
        return item

    # -- parts (job cost spec §11, #5543) --------------------------------------------------
    # An item's parts leave stock when it becomes `fulfilled` and come back when it is
    # unfulfilled, under the order's lock and then the parts' (in id order). Stock never
    # refuses: it can go below zero. A failed attempt uses nothing.

    def set_item_parts(
        self, order_id: str, item_id: str, parts: Sequence[ItemPart], by: Actor
    ) -> list[ItemPartRow]:
        """The parts one unit of the item uses, replaced whole. Refused once the order is
        ended, and for a fulfilled item, whose parts are already out of stock (unfulfil it
        first), so an unfulfil gives back what was taken."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            item = self._item_of(conn, order, item_id)
            if item.status == "fulfilled":
                raise LifecycleError(
                    "item_parts_used",
                    f"item {item_id} is fulfilled: its parts are out of stock; unfulfil it"
                    " to change them",
                    items=[item.id],
                )
            try:
                self.parts.set_item_parts(conn, item.id, parts)
            except PartNotFoundError as error:
                raise LifecycleError("part_not_found", str(error)) from None
            self._event(
                conn, order, by, "item.parts_set", item_id=item.id, parts=_parts_detail(parts)
            )
            rows = self.parts.item_parts([item.id], conn=conn)[item.id]
        self._told(_Change(order, [(item, None)]))
        return rows

    def _use_parts(self, conn: Conn, order: Order, item: OrderItem, by: Actor) -> None:
        """The item's parts times its quantity out of stock, each a `material` cost entry
        (``part:<item>:<part>``) at the part's unit cost now, when the part is priced in the
        order's currency and costs anything."""
        rows = [r for r in self.parts.item_parts([item.id], conn=conn)[item.id] if r.used == 0]
        if not rows:
            return
        locked = self.parts.lock_parts(conn, [r.part_id for r in rows])
        used: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda r: r.part_id):
            part = locked[row.part_id]
            count = row.per_unit * item.quantity
            amount = use_amount(part.unit_cost_minor, count)
            entry_id: str | None = None
            if part.currency == order.currency and amount > 0:
                entry, _ = self.store.insert_cost_entry(
                    conn,
                    order.id,
                    CostEntryInput(
                        order_item_id=item.id,
                        kind="material",
                        amount_minor=amount,
                        note=f"{count} x {part.name}",
                        source=f"part:{item.id}:{part.id}",
                    ),
                    created_by=by,
                )
                entry_id = entry.id
            costed = amount if entry_id is not None else None
            self.parts.move(
                conn,
                part,
                "use",
                -count,
                by,
                amount_minor=costed,
                order_id=order.id,
                order_item_id=item.id,
            )
            self.parts.set_used(conn, item.id, part.id, count)
            used.append(
                {"part_id": part.id, "count": count, "entry_id": entry_id, "amount_minor": costed}
            )
        self._event(conn, order, by, "parts.used", item_id=item.id, parts=used)

    def _return_parts(self, conn: Conn, order: Order, item: OrderItem, by: Actor) -> None:
        """What the item's fulfilment took, back in stock at the cost it was taken at, and
        its cost entries removed."""
        rows = [r for r in self.parts.item_parts([item.id], conn=conn)[item.id] if r.used > 0]
        if not rows:
            return
        locked = self.parts.lock_parts(conn, [r.part_id for r in rows])
        entries = {
            e.source: e
            for e in self.store.cost_entries(order.id, conn=conn)
            if e.kind == "material" and e.source is not None
        }
        returned: list[dict[str, Any]] = []
        for row in sorted(rows, key=lambda r: r.part_id):
            part = locked[row.part_id]
            entry = entries.get(f"part:{item.id}:{part.id}")
            if entry is not None:
                self.store.delete_cost_entry(conn, order.id, entry.id)
            self.parts.move(
                conn,
                part,
                "return",
                row.used,
                by,
                amount_minor=None if entry is None else entry.amount_minor,
                unit_cost=self.parts.used_at(conn, item.id, part.id),
                order_id=order.id,
                order_item_id=item.id,
            )
            self.parts.set_used(conn, item.id, part.id, 0)
            returned.append(
                {
                    "part_id": part.id,
                    "count": row.used,
                    "entry_id": None if entry is None else entry.id,
                }
            )
        self._event(conn, order, by, "parts.returned", item_id=item.id, parts=returned)

    # -- the print path (§5.4, #4719) -----------------------------------------------------

    def claim_print(
        self,
        order_id: str,
        item_id: str,
        by: Actor,
        *,
        request_id: str,
        sliced_sha256: str | None,
        sliced_blob_key: str | None,
        project_id: int | None = None,
        revision_id: str | None = None,
    ) -> Fulfilment:
        """Claim the item for one print before it starts (§5.4, #4719): under the order's
        lock, its next attempt, made nothing yet (``quantity`` 0, outcome `unknown`, no
        run yet), keyed by the run's ``request_id``, ``copies`` the quantity its attempts
        have still to make, the quote's sliced file held by
        ``(order_item_fulfilment, <id>)``, and the item `queued` (from `failed` too).
        Only then does the print start, so of two prints of one item (two tabs, the
        browser and the agent) one claims and the other is refused here, before anything
        prints. A claim this ``request_id`` made already (a retried or re-sent
        operation) is that claim, and nothing is written, once its run is attached;
        before that, only while it may still print (the order not ended or on hold, the
        item still `queued`, the attempt still open), else ``ClaimRefusedError``, writing
        nothing. A ``request_id`` another item claimed, or claimed for a print of another
        purpose or revision, is ``print_request_conflict``. One print of an item is in
        flight at a time, of either purpose (``print_in_flight``). Refused as a
        hand-recorded attempt is: the order accepted, not ended, not on hold, the item
        `approved` or `failed`; and ``item_made`` when its attempts made its quantity. A
        new claim records ``project_id``, the Bambuddy project its run is keyed with
        (#5263), which a retry keys it with again.

        With ``revision_id`` it is a test print of that revision (storefront spec §4.4):
        an attempt of purpose `test` naming it, of one copy, that counts for nothing and
        never moves the item. It is refused only when the order ended or is on hold, the
        item is settled (``item_not_open``), has no design (``item_custom``,
        ``item_has_no_design``) or ``revision_id`` is not its current design
        (``revision_not_found``, ``revision_not_newest``): the item prints the output of
        its current design, whatever its status, on an order accepted or not."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            existing = self.store.print_claim(request_id, conn=conn)
            if existing is not None:
                purpose = "production" if revision_id is None else "test"
                if (
                    existing.order_item_id != item_id
                    or existing.purpose != purpose
                    or existing.revision_id != revision_id
                ):
                    # One key on `/print` and on a revision's `/test-print` is two
                    # requests: neither answers with the other's attempt.
                    raise LifecycleError(
                        "print_request_conflict",
                        f"request {request_id} is another item's or another kind of print",
                        items=[item_id],
                    )
                if existing.print_run_id is not None:
                    return existing
                # A claim whose start may not have run: it starts only if it still may.
                item = self._item_of(conn, order, item_id)
                refusal = self._claim_refusal(order, item, existing)
                if refusal is None:
                    return existing
                raise ClaimRefusedError(existing, refusal)
        if revision_id is not None:
            return self._claim_test_print(
                order_id,
                item_id,
                by,
                revision_id,
                request_id=request_id,
                sliced_sha256=sliced_sha256,
                sliced_blob_key=sliced_blob_key,
                project_id=project_id,
            )
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._producing(order)
            item = self._item_of(conn, order, item_id)
            if item.status not in FULFILLABLE:
                raise LifecycleError(
                    "item_not_open" if item.status in SETTLED else "item_not_approved",
                    f"item {item_id} is {item.status}",
                    items=[item.id],
                )
            self._none_in_flight(conn, item)
            attempts = self.store.fulfilments(item.id, conn=conn)
            copies = item.quantity - sum(
                a.quantity for a in attempts if a.outcome in ("succeeded", "partial")
            )
            if copies < 1:
                raise LifecycleError(
                    "item_made",
                    f"item {item_id}'s attempts made its {item.quantity} already: accept them",
                    items=[item.id],
                )
            attempt = self.store.insert_fulfilment(
                conn,
                item.id,
                quantity=0,
                outcome="unknown",
                created_by=by,
                started_at=datetime.now(UTC),
                print_request_id=request_id,
                copies=copies,
                sliced_sha256=sliced_sha256,
                sliced_blob_key=sliced_blob_key,
            )
            self.store.set_claim_project(conn, attempt.id, project_id)
            if sliced_blob_key is not None:
                self.store.hold_attempt_file(conn, FULFILMENT_HOLDER, sliced_blob_key, attempt.id)
            item = self.store.update_item(conn, item.id, status="queued")
            self._event(
                conn,
                order,
                by,
                "print.claimed",
                item_id=item.id,
                fulfilment_id=attempt.id,
                attempt=attempt.attempt,
                copies=copies,
                purpose=attempt.purpose,
            )
            order = self._rederive(conn, order, by)
        self._told(_Change(order, [(item, attempt.attempt)]))
        return attempt

    def _test_printable(
        self, conn: Conn, order: Order, item_id: str, revision_id: str
    ) -> OrderItem:
        """`claim_print`'s refusals of a test print of ``revision_id``."""
        self._not_ended(order)
        self._not_on_hold(order)
        item = self._item_of(conn, order, item_id)
        if item.status in SETTLED:
            raise LifecycleError(
                "item_not_open", f"item {item_id} is {item.status}", items=[item.id]
            )
        if item.kind == "custom":
            raise LifecycleError("item_custom", f"item {item_id} is a custom item")
        if item.kind == "library_file":
            raise LifecycleError(
                "item_library_file",
                f"item {item_id} prints a library file: it has no revision to test print",
            )
        if item.reference is None:
            raise LifecycleError("item_has_no_design", f"item {item_id} has no design yet")
        self._newest(conn, item, revision_id)
        self._none_in_flight(conn, item)
        return item

    def _none_in_flight(self, conn: Conn, item: OrderItem) -> None:
        """One print of an item at a time, of either purpose: every print of an item is
        a print of its output, and a print's outcome is recorded against its subject's
        newest run only (``bambuddy/outcomes.py``), so a second print in flight would
        take the first one's outcome."""
        open_attempt = self.store.print_in_flight(item.id, conn=conn)
        if open_attempt is not None:
            raise LifecycleError(
                "print_in_flight",
                f"item {item.id}'s attempt {open_attempt.attempt} ({open_attempt.purpose}) is"
                " still printing: settle it first",
                items=[item.id],
            )

    def _claim_test_print(
        self,
        order_id: str,
        item_id: str,
        by: Actor,
        revision_id: str,
        *,
        request_id: str,
        sliced_sha256: str | None,
        sliced_blob_key: str | None,
        project_id: int | None,
    ) -> Fulfilment:
        """`claim_print`'s new claim of a test print: the attempt and its file's hold,
        the item left as it is."""
        self.ensure_revision(order_id, item_id, by)
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            item = self._test_printable(conn, order, item_id, revision_id)
            attempt = self.store.insert_fulfilment(
                conn,
                item.id,
                quantity=0,
                outcome="unknown",
                created_by=by,
                started_at=datetime.now(UTC),
                print_request_id=request_id,
                copies=1,
                sliced_sha256=sliced_sha256,
                sliced_blob_key=sliced_blob_key,
                purpose="test",
                revision_id=revision_id,
            )
            self.store.set_claim_project(conn, attempt.id, project_id)
            if sliced_blob_key is not None:
                self.store.hold_attempt_file(conn, FULFILMENT_HOLDER, sliced_blob_key, attempt.id)
            self._event(
                conn,
                order,
                by,
                "print.claimed",
                item_id=item.id,
                fulfilment_id=attempt.id,
                attempt=attempt.attempt,
                copies=1,
                purpose="test",
                revision_id=revision_id,
            )
        self._told(_Change(order, [(item, attempt.attempt)]))
        return attempt

    def record_archive_facts(
        self,
        attempt: Fulfilment,
        archive_ids: Sequence[int],
        *,
        started_at: datetime | None,
        finished_at: datetime | None,
        actual_seconds: int | None,
        actual_grams: float | None,
    ) -> Fulfilment:
        """What the attempt's archives say (§8.2, ``OrderStore.set_archive_facts``), under
        the order's lock as every writer of an item is, and told as `order_item.changed`
        when it changed the attempt (#5263). Allowed on any order: it records a print.
        Nothing is written when another attempt names one of the archives (#5273)."""
        item = self.store.item(attempt.order_item_id)
        if item is None:
            return attempt
        with self.store.transaction() as conn:
            order = self._lock(conn, item.order_id)
            if self.store.archives_named_elsewhere(conn, attempt.id, archive_ids):
                # Named by hand since its run's archives were read (#5273): they are
                # read again, without it, when the attempt's archives are next shown.
                return attempt
            written = self.store.set_archive_facts(
                attempt.id,
                archive_ids,
                started_at=started_at,
                finished_at=finished_at,
                actual_seconds=actual_seconds,
                actual_grams=actual_grams,
                conn=conn,
            )
            item = self.store.item(item.id, conn=conn) or item
        if written is None:
            return attempt
        if written != attempt:
            self._told(_Change(order, [(item, written.attempt)]))
        return written

    def attach_print(self, claim: Fulfilment, run_id: str, printer: str | None) -> Fulfilment:
        """The run a claim's print started (`print.started` on the audit line); again
        with the same run, nothing is written. Allowed on hold and on an ended order: the
        print is the printer's now, and the record must name it."""
        if claim.print_run_id == run_id:
            return claim
        owner = self.store.item(claim.order_item_id)
        assert owner is not None
        with self.store.transaction() as conn:
            order = self._lock(conn, owner.order_id)
            attempt = self.store.attach_print_run(conn, claim.id, run_id, printer)
            if attempt is None:
                raise LifecycleError(
                    "print_request_conflict",
                    f"attempt {claim.attempt} is gone or is another run's",
                    items=[owner.id],
                )
            self._event(
                conn,
                order,
                attempt.created_by,
                "print.started",
                item_id=owner.id,
                fulfilment_id=attempt.id,
                attempt=attempt.attempt,
                print_run_id=run_id,
                copies=attempt.copies,
            )
        return attempt

    def release_print(self, claim: Fulfilment, reason: str) -> None:
        """A claim whose print was refused before it started: the attempt and its hold
        go, the item takes back the status its other attempts give it (`approved`, or
        `failed` after a failed one), and the audit line says why. Only a claim with no
        run yet and still open (a person's settled attempt stays), and the item only
        while it is still the `queued` the claim made it."""
        owner = self.store.item(claim.order_item_id)
        if owner is None:
            return
        with self.store.transaction() as conn:
            order = self._lock(conn, owner.order_id)
            row = self.store.fulfilment(claim.id, conn=conn)
            if row is None or row.print_run_id is not None:
                return
            if row.outcome != "unknown" or row.finished_at is not None:
                # A person settled it: their record, not a claim, and it stays.
                return
            changed = self._release_claim(conn, order, row, reason)
            order = self._rederive(conn, order, row.created_by)
        self._told(_Change(order, changed))

    def _claim_refusal(
        self, order: Order, item: OrderItem, claim: Fulfilment
    ) -> LifecycleError | None:
        """Why a claim with no run may no longer start its print: the order ended or on
        hold since, the item moved off the `queued` the claim made it (a cancel), or the
        attempt settled by hand. A test print's claim made the item nothing: it may no
        longer print once the item settled or its revision is no longer the item's."""
        try:
            self._not_ended(order)
            self._not_on_hold(order)
        except LifecycleError as error:
            return error
        if claim.purpose == "test":
            if item.status in SETTLED:
                return LifecycleError(
                    "item_not_open", f"item {item.id} is {item.status}", items=[item.id]
                )
            if claim.revision_id != item.current_revision_id:
                return LifecycleError(
                    "revision_not_newest",
                    f"the revision attempt {claim.attempt} prints is not item {item.id}'s newest",
                    items=[item.id],
                )
        elif item.status != "queued":
            return LifecycleError(
                "item_not_open" if item.status in SETTLED else "item_not_approved",
                f"item {item.id} is {item.status}",
                items=[item.id],
            )
        if claim.outcome != "unknown" or claim.finished_at is not None:
            return LifecycleError(
                "item_not_approved",
                f"attempt {claim.attempt} is settled: {claim.outcome}",
                items=[item.id],
            )
        return None

    def _release_claim(
        self, conn: Conn, order: Order, row: Fulfilment, reason: str
    ) -> list[tuple[OrderItem, int | None]]:
        """A claim with no run released in ``conn``: its row and hold, the item back to
        what its other attempts give it while it is still `queued` (a test print's claim
        queued it not, and leaves it), `print.refused`."""
        self.store.release_print_claim(conn, FULFILMENT_HOLDER, row.id)
        item = self.store.item(row.order_item_id, conn=conn)
        assert item is not None
        changed: list[tuple[OrderItem, int | None]] = []
        if row.purpose == "test":
            changed.append((item, None))
        elif item.status == "queued":
            rest = self.store.fulfilments(item.id, conn=conn)
            back: ItemStatus = "failed" if rest and rest[-1].outcome == "failed" else "approved"
            item = self.store.update_item(conn, item.id, status=back)
            changed.append((item, None))
        self._event(
            conn,
            order,
            row.created_by,
            "print.refused",
            item_id=item.id,
            fulfilment_id=row.id,
            reason=reason,
        )
        return changed

    def follow_print(self, run_id: str, *, progressed: bool = False) -> Fulfilment | None:
        """The item follows its print (§5.4), from what the print path recorded of the
        run (``OrderStore.print_state``), so a repeat, a late event or a reconcile
        writes the same: the print's outcome (``print_outcomes``) settles the attempt,
        `succeeded` with ``copies`` made and the item `printed`, or `failed` (a failed
        or cancelled print) with the item `failed`; one whose end the follow could not
        read (`unknown`) gets its end and stays for a person to settle (``PATCH``). With
        no outcome, a run that failed fails the attempt with its refusal, and the order's
        projects are asked of Bambuddy again by the next ensure (#5276); one that
        succeeded and whose print moved (``progressed``, a ``print.progress``) makes a
        `queued` item `printing`, when the run is still its output's newest (a later dialog
        print's progress is that print's). Only an attempt still open (outcome `unknown`, no end)
        moves, so a person's edit stands; the item moves only while it is in a print
        state, so a cancel stands. Allowed on hold: it records what the printer did.
        Its audit rows are written as the person who started the print. A test print's
        attempt (storefront spec §4.4) settles the same way, and the item never follows
        it. The attempt as it now is, or None when nothing moved."""
        attempt = self.store.print_attempt(run_id)
        if attempt is None or attempt.outcome != "unknown" or attempt.finished_at is not None:
            return None
        status, detail, outcome = self.store.print_state(run_id)
        now = datetime.now(UTC)
        settle: tuple[Outcome, int, str | None, ItemStatus | None] | None = None
        run_failed = False
        if outcome == "finished":
            settle = ("succeeded", attempt.copies or 0, None, "printed")
        elif outcome in ("failed", "cancelled"):
            settle = ("failed", 0, f"the print was {outcome}", "failed")
        elif outcome == "unknown":
            settle = ("unknown", 0, "the print ended, but how could not be read", None)
        elif status == "failed":
            settle = ("failed", 0, detail or "the print run failed", "failed")
            # Only a run that failed before it printed (#5276): a print Bambuddy started
            # and reported failed (an outcome) was filed under a project that stood.
            run_failed = True
        elif attempt.purpose == "test" or not (
            status == "succeeded" and progressed and self.store.runs_newest(run_id)
        ):
            # A test print's progress moves no item: it is in no print state for it.
            # A print of the same output the dialog queued since is that run's, whose
            # progress is not this attempt's (`bambuddy/outcomes.py` reads the same way).
            return None
        owner = self.store.item(attempt.order_item_id)
        if owner is None:
            return None
        with self.store.transaction() as conn:
            # Read again under the order's lock: another replica may have moved it.
            order = self._lock(conn, owner.order_id)
            row = self.store.print_attempt(run_id, conn=conn)
            item = self.store.item(row.order_item_id, conn=conn) if row is not None else None
            if row is None or item is None or row.outcome != "unknown" or row.finished_at:
                return None
            by = row.created_by
            changed: list[tuple[OrderItem, int | None]] = []
            if settle is None:
                if item.status != "queued":
                    return None
                item = self.store.update_item(conn, item.id, status="printing")
                self._event(
                    conn, order, by, "print.printing", item_id=item.id, fulfilment_id=row.id
                )
                changed.append((item, row.attempt))
            else:
                result, made, reason, item_status = settle
                row = self.store.update_fulfilment(
                    conn,
                    row.id,
                    quantity=made,
                    outcome=result,
                    failure_reason=reason,
                    finished_at=now,
                )
                if run_failed:
                    # Its project may be one just deleted in Bambuddy (#5276): the
                    # retry asks for the order's projects again, not taking them to stand.
                    self.store.forget_projects_seen(order.id, conn=conn)
                # A test print's attempt settles alone: the item never follows it.
                if item_status is not None and item.status in PRINTING and row.purpose != "test":
                    item = self.store.update_item(conn, item.id, status=item_status)
                changed.append((item, row.attempt))
                self._event(
                    conn,
                    order,
                    by,
                    "print.settled",
                    item_id=item.id,
                    fulfilment_id=row.id,
                    attempt=row.attempt,
                    outcome=result,
                    purpose=row.purpose,
                )
            order = self._rederive(conn, order, by)
        self._told(_Change(order, changed))
        return row

    # -- shipments (§6.3) -----------------------------------------------------------------

    def _shippable(
        self,
        conn: Conn,
        order: Order,
        lines: Sequence[ShipmentItem],
        *,
        replacing: str | None = None,
    ) -> None:
        """The lines name the order's own fulfilled items, and with the order's other
        shipments (``replacing`` names the one these lines replace) cover no item past
        its quantity."""
        items = {item.id: item for item in self.store.items(order.id, conn=conn)}
        wrong = [line.order_item_id for line in lines if line.order_item_id not in items]
        if wrong:
            raise LifecycleError(
                "item_not_found", f"not items of order {order.number}", items=wrong, not_found=True
            )
        unmade = [
            line.order_item_id for line in lines if items[line.order_item_id].status != "fulfilled"
        ]
        if unmade:
            raise LifecycleError(
                "items_not_fulfilled", "a shipment carries fulfilled items only", items=unmade
            )
        shipped: dict[str, int] = {}
        for shipment in self.store.shipments(order.id, conn=conn):
            if shipment.id == replacing:
                continue
            for line in shipment.items:
                shipped[line.order_item_id] = shipped.get(line.order_item_id, 0) + line.quantity
        over = [
            line.order_item_id
            for line in lines
            if shipped.get(line.order_item_id, 0) + line.quantity
            > items[line.order_item_id].quantity
        ]
        if over:
            raise LifecycleError(
                "shipment_over_quantity",
                "the shipments would cover more than the item's quantity",
                items=over,
            )

    def add_shipment(self, order_id: str, body: ShipmentInput, by: Actor) -> Shipment:
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            if order.status not in DERIVED:
                raise LifecycleError(
                    "order_not_accepted", f"order {order.number} is {order.status}, not accepted"
                )
            self._not_on_hold(order)
            self._shippable(conn, order, body.items)
            shipment = self.store.insert_shipment(
                conn,
                order.id,
                carrier=body.carrier,
                service=body.service,
                tracking=body.tracking,
                shipped_at=body.shipped_at,
                address=body.address or order.shipping_address,
                cost_minor=body.cost_minor,
                notes=body.notes,
                items=[(line.order_item_id, line.quantity) for line in body.items],
            )
            self._event(conn, order, by, "shipment.added", shipment_id=shipment.id)
            order = self._rederive(conn, order, by)
        self._told(_Change(order))
        return shipment

    def update_shipment(
        self, order_id: str, shipment_id: str, edit: ShipmentEdit, by: Actor
    ) -> Shipment:
        """Only what ``edit`` carries changes (its ``model_fields_set``); the rest of
        the shipment, its lines included, stays as it is."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            self._not_on_hold(order)
            shipment = self.store.shipment(shipment_id, conn=conn)
            if shipment is None or shipment.order_id != order.id:
                raise LifecycleError(
                    "shipment_not_found",
                    f"no shipment {shipment_id} of order {order.number}",
                    shipments=[shipment_id],
                    not_found=True,
                )
            sent = edit.model_fields_set
            if edit.items is not None:
                self._shippable(conn, order, edit.items, replacing=shipment.id)

            def given(name: str) -> Any:
                return getattr(edit, name) if name in sent else UNSET

            shipment = self.store.update_shipment(
                conn,
                shipment.id,
                carrier=given("carrier"),
                service=given("service"),
                tracking=given("tracking"),
                shipped_at=given("shipped_at"),
                address=edit.address if edit.address is not None else UNSET,
                cost_minor=given("cost_minor"),
                notes=UNSET if edit.notes is None else edit.notes,
                items=UNSET
                if edit.items is None
                else [(line.order_item_id, line.quantity) for line in edit.items],
            )
            self._event(conn, order, by, "shipment.updated", shipment_id=shipment.id)
            order = self._rederive(conn, order, by)
        self._told(_Change(order))
        return shipment

    # -- attachments (§6.2, #4251) ---------------------------------------------------------
    #
    # Records, not steps: a file may be added, captioned or removed in any state, on hold
    # or after the order ended (a photo of the delivered object comes after close), and
    # nothing here moves a status. A removal is the one thing a person may delete
    # besides a draft item (Ruling 6), on the audit line.

    def _attachment_of(self, conn: Conn, order: Order, attachment_id: str) -> Attachment:
        found = self.store.attachment(attachment_id, conn=conn)
        if found is None or found[0].order_id != order.id:
            raise LifecycleError(
                "attachment_not_found",
                f"no attachment {attachment_id} of order {order.number}",
                not_found=True,
            )
        return found[0]

    def _subject_item(self, change: _Change, conn: Conn, attachment: Attachment) -> None:
        if attachment.item_id is not None:
            item = self.store.item(attachment.item_id, conn=conn)
            if item is not None:
                change.items.append((item, None))

    def add_attachment(
        self,
        order_id: str,
        item_id: str | None,
        by: Actor,
        *,
        attachment_id: str,
        kind: AttachmentKind,
        role: AttachmentRole,
        filename: str,
        media_type: str,
        size_bytes: int,
        sha256: str,
        blob_key: str,
        caption: str,
        fulfilment_id: str | None = None,
        revision_id: str | None = None,
    ) -> Attachment:
        """The row of a file already kept as ``blob_key``, on the order, one of its
        items, or with ``fulfilment_id`` one of that item's attempts (#4251); the store
        holds the blob for it in the same transaction. With ``revision_id`` it is on
        that revision of the item (storefront spec §4): a render, or beside a
        ``fulfilment_id`` the photo of that revision's test print."""
        if revision_id is None:
            _refuse_revision_role(role)
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            item = None if item_id is None else self._item_of(conn, order, item_id)
            if revision_id is not None:
                found = self.store.revision(revision_id, conn=conn)
                if item is None or found is None or found.item_id != item.id:
                    raise LifecycleError(
                        "revision_not_found",
                        f"no revision {revision_id} of that item",
                        not_found=True,
                    )
                subject = revision_subject(found.id)
            if fulfilment_id is not None:
                attempt = self.store.fulfilment(fulfilment_id, conn=conn)
                if (
                    item is None
                    or attempt is None
                    or attempt.order_item_id != item.id
                    or (
                        revision_id is not None
                        and (attempt.purpose != "test" or attempt.revision_id != revision_id)
                    )
                ):
                    raise LifecycleError(
                        "fulfilment_not_found",
                        f"no attempt {fulfilment_id} of that item",
                        not_found=True,
                    )
                if revision_id is None:
                    subject = fulfilment_subject(attempt.id)
            elif revision_id is None:
                subject = order_subject(order.id) if item is None else item_subject(item.id)
            attachment = self.store.insert_attachment(
                conn,
                subject_item_id=None if fulfilment_id is None and revision_id is None else item_id,
                fulfilment_id=fulfilment_id if revision_id is not None else None,
                attachment_id=attachment_id,
                order_id=order.id,
                subject=subject,
                kind=kind,
                role=role,
                filename=filename,
                media_type=media_type,
                size_bytes=size_bytes,
                sha256=sha256,
                blob_key=blob_key,
                caption=caption,
                created_by=by,
            )
            self._event(
                conn,
                order,
                by,
                "attachment.added",
                attachment_id=attachment.id,
                item_id=attachment.item_id,
                fulfilment_id=attachment.fulfilment_id,
                revision_id=attachment.revision_id,
                role=attachment.role,
            )
            change = _Change(order)
            self._subject_item(change, conn, attachment)
        self._told(change)
        return attachment

    def edit_attachment(
        self, order_id: str, attachment_id: str, patch: AttachmentPatch, by: Actor
    ) -> Attachment:
        """The caption and the role; a field left out stays. Each change is on the audit
        line. An image marked delivered keeps its mark whatever its role becomes. A
        file of a revision sent to the customer, or answered, keeps both
        (`revision_sent`), as `remove_attachment`."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            before = self._attachment_of(conn, order, attachment_id)
            if (patch.role not in (None, before.role)) or (
                patch.caption not in (None, before.caption)
            ):
                self._refuse_sent_revision(conn, before)
            if patch.role not in (None, before.role):
                if before.revision_id is None:
                    _refuse_revision_role(patch.role)
                elif patch.role == "test_print":
                    # A test print's photo may join the model's photos (`attachment_to_model`),
                    # so it only ever arrives by upload: no re-role, from a render or through
                    # another role, makes one.
                    raise LifecycleError(
                        "role",
                        "only an upload is the photo of a test print: upload that photo",
                        unprocessable=True,
                    )
            attachment = self.store.update_attachment(
                conn,
                before.id,
                caption=UNSET if patch.caption is None else patch.caption,
                role=UNSET if patch.role is None else patch.role,
            )
            if attachment.role != before.role:
                self._event(
                    conn,
                    order,
                    by,
                    "attachment.role",
                    attachment_id=attachment.id,
                    from_role=before.role,
                    to=attachment.role,
                )
            if attachment.caption != before.caption:
                self._event(
                    conn,
                    order,
                    by,
                    "attachment.caption",
                    attachment_id=attachment.id,
                    from_caption=before.caption,
                    to=attachment.caption,
                )
            change = _Change(order)
            self._subject_item(change, conn, attachment)
        self._told(change)
        return attachment

    def _refuse_sent_revision(self, conn: Conn, attachment: Attachment) -> None:
        if attachment.revision_id is None:
            return
        revision = self.store.revision(attachment.revision_id, conn=conn)
        if revision is not None and (revision.sent_at is not None or revision.answer is not None):
            raise LifecycleError(
                "revision_sent",
                f"revision {revision.number} was sent to the customer: its files"
                " are the record of what they saw",
            )

    def remove_attachment(self, order_id: str, attachment_id: str, by: Actor) -> None:
        """A mis-upload (Ruling 6): the row goes, and with it its hold on the file and
        the item's delivered mark when it was this one. A file of a revision sent to the
        customer, or answered, stays (`revision_sent`): it is the record of what they
        saw."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            attachment = self._attachment_of(conn, order, attachment_id)
            self._refuse_sent_revision(conn, attachment)
            item = (
                None
                if attachment.item_id is None
                else self.store.item(attachment.item_id, conn=conn)
            )
            delivered = item is not None and item.delivered_attachment_id == attachment.id
            self.store.delete_attachment(conn, attachment.id)
            self._event(
                conn,
                order,
                by,
                "attachment.removed",
                attachment_id=attachment.id,
                item_id=attachment.item_id,
                filename=attachment.filename,
                sha256=attachment.sha256,
                was_delivered=delivered,
            )
            change = _Change(order)
            self._subject_item(change, conn, attachment)
        self._told(change)

    def set_delivered(
        self, order_id: str, item_id: str, attachment_id: str | None, by: Actor
    ) -> OrderItem:
        """Mark one of the item's own image attachments (or of its attempts), one a
        browser draws, as what was actually delivered, or with None clear the mark."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            item = self._item_of(conn, order, item_id)
            if attachment_id is not None:
                found = self.store.attachment(attachment_id, conn=conn)
                # The item's own image, or one of its attempts' (#4251).
                if found is None or found[0].item_id != item.id:
                    raise LifecycleError(
                        "attachment_id",
                        f"attachment {attachment_id} is not attached to this item",
                        unprocessable=True,
                    )
                if found[0].revision_id is not None:
                    # A revision's file shows a design, not what was delivered.
                    raise LifecycleError(
                        "attachment_id",
                        f"attachment {attachment_id} is a revision's file, not the delivered"
                        " object",
                        unprocessable=True,
                    )
                if found[0].kind != "image" or found[0].media_type not in SHOWN_IMAGES:
                    raise LifecycleError(
                        "attachment_id",
                        f"attachment {attachment_id} is not an image a browser shows"
                        " (PNG, JPEG, WebP, GIF or AVIF), so it cannot stand as the item's"
                        " picture",
                        unprocessable=True,
                    )
            if item.delivered_attachment_id != attachment_id:
                item = self.store.set_delivered(conn, item.id, attachment_id)
                self._event(
                    conn,
                    order,
                    by,
                    "item.delivered",
                    item_id=item.id,
                    attachment_id=attachment_id,
                )
        self._told(_Change(order, [(item, None)]))
        return item


def reap_quote_holds(refs: BlobRefs, store: OrderStore, *, grace: timedelta = QUOTE_TIMEOUT) -> int:
    """#4250 Ruling 3's reaper, the ``housekeeping_reap_quote_holds`` sweep: drop every
    ``order_item_quote`` holder §3.4 says should be gone, older than ``grace``. One
    whose quote has no row (a run that crashed between its ref and its row), and one
    whose quote is unaccepted and not its item's newest, or whose item is cancelled, or
    whose order is closed or cancelled (a release lost between a commit and its drop).
    The grace is every row's, so a quote recorded a moment ago is never caught between
    its ref and its row. How many holders went; the blob sweep frees the bytes."""
    return refs.drop_unlisted(QUOTE_HOLDER, store.standing_quote_ids(), older_than=grace)
