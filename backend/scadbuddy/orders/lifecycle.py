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
(``accept_order``, ``accept_quote``, ``record_fulfilment``, ``update_fulfilment``,
``accept_fulfilment``, ``add_shipment``, ``update_shipment``, ``close_order``) is
refused with ``order_on_hold``; ``release``, the cancels, a payment and the plain edits
go through, so a person can fix what the hold was for.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scadbuddy.core.events import EventBus, OrderChanged, OrderItemChanged, emit
from scadbuddy.orders.attachments import SHOWN_IMAGES
from scadbuddy.orders.models import (
    Actor,
    AddressSnapshot,
    Attachment,
    AttachmentKind,
    AttachmentPatch,
    AttachmentRole,
    ChargeInput,
    ColourSlot,
    Fulfilment,
    ItemStatus,
    Money,
    Order,
    OrderItem,
    OrderStatus,
    Outcome,
    Payment,
    Quote,
    QuoteLine,
    Shipment,
    ShipmentItem,
)
from scadbuddy.orders.pricing import Plate, Priced
from scadbuddy.orders.reference import ResolvedReference
from scadbuddy.orders.store import (
    UNSET,
    Conn,
    OrderNotFoundError,
    OrderStore,
    UnsetType,
    item_subject,
    order_subject,
)
from scadbuddy.store.refs import BlobRefs

logger = logging.getLogger(__name__)

#: The states an order's items and price may still change in (§4's `PATCH` rows).
OPEN_FOR_EDITS: frozenset[OrderStatus] = frozenset({"draft", "quoted"})
#: The states a person's decision has ended (nothing else may move).
ENDED: frozenset[OrderStatus] = frozenset({"closed", "cancelled"})
#: The states past a person's acceptance, which `derive` writes.
DERIVED: frozenset[OrderStatus] = frozenset({"accepted", "in_production", "ready", "shipped"})
#: An item nothing more happens to.
SETTLED: frozenset[ItemStatus] = frozenset({"fulfilled", "cancelled"})
#: The item states a fulfilment attempt may be recorded in (phase 1, Ruling 4).
FULFILLABLE: frozenset[ItemStatus] = frozenset({"approved", "failed"})

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


@dataclass
class _Change:
    """What one transition did, told after commit."""

    order: Order
    items: list[tuple[OrderItem, int | None]] = field(default_factory=list)
    #: The quotes whose sliced-file holder §3.4 releases, dropped once committed.
    released: list[str] = field(default_factory=list)


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
    the shipments cover every fulfilled item, `in_production` when any item has a
    fulfilment attempt (``attempted``: the ids with one) and not every item is
    settled, which is §3.5's "any item past `approved`" restricted to phase 1's states
    (plan Ruling 4; phase 3's `queued` writer must widen it to "any item `queued` or
    beyond, or with an attempt"); else `accepted`.
    """
    if order.status not in DERIVED:
        return order.status
    settled = all(item.status in SETTLED for item in items)
    fulfilled = [item for item in items if item.status == "fulfilled"]
    if settled and fulfilled:
        return "ready" if _covered(fulfilled, shipments) else "shipped"
    if any(item.id in attempted for item in items):
        return "in_production"
    return "accepted"


def _settle_open(items: Sequence[OrderItem]) -> OrderStatus:
    """`draft` or `quoted` (§3.5): quoted once every item not cancelled is approved."""
    open_items = [item for item in items if item.status != "cancelled"]
    return "quoted" if open_items and all(i.status == "approved" for i in open_items) else "draft"


class Lifecycle:
    def __init__(self, store: OrderStore, bus: EventBus | None, refs: BlobRefs | None = None):
        self.store = store
        self.bus = bus
        #: The slice quotes' holds (#4250 Ruling 3); None where nothing holds a file.
        self.refs = refs

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

    def _quote_ids(self, conn: Conn, item_ids: Iterable[str]) -> list[str]:
        return [quote.id for item_id in item_ids for quote in self.store.quotes(item_id, conn=conn)]

    def _told(self, change: _Change) -> None:
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
                raise LifecycleError(
                    "order_not_editable",
                    f"order {order.number} is {order.status}: only its notes and due date"
                    " may change now",
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
    ) -> Payment:
        """Money the shop already received: a record, allowed on hold (Ruling 14)."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            if order.status == "cancelled":
                raise LifecycleError("order_cancelled", f"order {order.number} is cancelled")
            payment = self.store.insert_payment(
                conn,
                order.id,
                amount_minor=amount_minor,
                method=method,
                reference=reference,
                received_at=received_at,
                notes=notes,
            )
            self._event(conn, order, by, "payment.added", payment_id=payment.id)
        self._told(_Change(order))
        return payment

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
        """§3.5: from any state before `shipped`; cancels the open items and keeps every
        fulfilled one's attempts. Allowed on hold (Ruling 14)."""
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
        self._told(_Change(closed, released=released))
        return closed

    # -- items (§3.3) ---------------------------------------------------------------------

    def _open_for_items(self, order: Order, what: str) -> None:
        if order.status not in OPEN_FOR_EDITS:
            raise LifecycleError(
                "item_locked",
                f"order {order.number} is {order.status}: {what} is pinned once accepted",
            )

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
    ) -> OrderItem:
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            self._open_for_items(order, "the list of items")
            item = self.store.insert_item(
                conn,
                order.id,
                reference=resolved.reference,
                colours=resolved.colours,
                output_id=output_id,
                quantity=quantity,
                title=title,
                notes=notes,
            )
            self._event(conn, order, by, "item.added", item_id=item.id)
            order = self._rederive(conn, order, by)
        self._told(_Change(order, [(item, None)]))
        return item

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
    ) -> OrderItem:
        """The reference (replaced whole), the quantity and the slots' filaments while
        `draft` or `quoted`, each dropping the accepted quote (Ruling 1; #4250 Ruling 6:
        §5.2's "changing the colours makes the next quote a new row"); title and notes in
        any open state. A reference replaced without an ``output_id`` clears the item's:
        the output it pointed at no longer matches what the item says.

        ``filaments`` (``{slot: filament_id | None}``) repins only when it changes a
        slot: the same filaments sent again write nothing and drop nothing. A slot the
        item lacks, or a filament not in the book or archived, is refused (422).
        ``colours`` gives an item made by hand, which has none, the slots its first
        render made (the quote's run, #4250); it repins nothing, as it changes no slot
        the item had."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            item = self._item_of(conn, order, item_id)
            if item.status in SETTLED:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
            base = list(reference.colours if reference is not None else item.colours)
            slots = base if base or colours is None else list(colours)
            if filaments is not None:
                slots = self._with_filaments(conn, slots, filaments)
            # Only a slot the item had can change; one the fill brings had no filament.
            before = {slot.slot: slot.filament_id for slot in base}
            refiled = any(
                slot.slot in before and before[slot.slot] != slot.filament_id for slot in slots
            )
            repins = reference is not None or quantity is not None or refiled
            if repins:
                self._open_for_items(order, "an item's reference, quantity and filaments")
            fields: dict[str, Any] = {}
            if reference is not None:
                fields.update(reference=reference.reference, colours=slots)
                if isinstance(output_id, UnsetType):
                    output_id = None
            elif slots != item.colours:
                fields["colours"] = slots
            if not isinstance(output_id, UnsetType):
                fields["output_id"] = output_id
            if quantity is not None:
                fields["quantity"] = quantity
            if title is not None:
                fields["title"] = title
            if notes is not None:
                fields["notes"] = notes
            if not fields:
                # Nothing changes (the item's own filaments sent again): nothing written.
                return item
            if repins:
                self.store.unaccept_quotes(conn, item.id)
                fields["status"] = "draft"
            item = self.store.update_item(conn, item.id, **fields)
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
        """Only a `draft` item deletes (Ruling 6, 10); else `item_not_draft`, and the
        caller decides whether to cancel it."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            item = self._item_of(conn, order, item_id)
            # Its files go with it, each on the audit line, and with them their holds
            # on the blobs: nothing could show or remove an item's file once the item
            # is gone. A refusal below rolls this back.
            for attachment in self.store.attachments(order.id, conn=conn):
                if attachment.item_id == item.id:
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
        self._told(_Change(order, [(item, None)], released))
        return item

    # -- quotes (§3.4) --------------------------------------------------------------------

    def record_manual_quote(
        self, order_id: str, item_id: str, body: ManualQuoteInput, by: Actor
    ) -> Quote:
        """A typed price (Ruling 1): one `manual` line, cost = price, quantity the
        item's now; the item is `quoted` (unless already approved on another quote)."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            self._open_for_items(order, "an item's price")
            item = self._item_of(conn, order, item_id)
            if item.status in SETTLED:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
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
        quote_id: str | None = None,
    ) -> Quote:
        """A `slice` quote (#4250, §5.2 step 5) as the ``order_item_quote`` run priced
        it, with the plates it read and the sliced file it read them from, under the id
        its ref was taken with (``quote_id``, Ruling 3). As a manual quote: the order
        open for items, the item not settled, allowed on hold (it advances nothing)."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            self._open_for_items(order, "an item's price")
            item = self._item_of(conn, order, item_id)
            if item.status in SETTLED:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
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
            )
            change = self._recorded(conn, order, item, quote, by)
        self._told(change)
        return quote

    def accept_quote(self, order_id: str, item_id: str, quote_id: str, by: Actor) -> OrderItem:
        """Pins the quote (§3.4): the item is `approved`, the order `quoted` once every
        item not cancelled is."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            self._open_for_items(order, "an item's price")
            self._not_on_hold(order)
            item = self._item_of(conn, order, item_id)
            if item.status in SETTLED:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
            quote = self.store.quote(quote_id, conn=conn)
            if quote is None or quote.order_item_id != item.id:
                raise LifecycleError(
                    "quote_not_found", f"no quote {quote_id} of item {item_id}", not_found=True
                )
            if quote.quantity != item.quantity:
                raise LifecycleError(
                    "quote_stale",
                    f"quote {quote_id} priced {quote.quantity}, the item is now {item.quantity}",
                )
            self.store.accept_quote(conn, item.id, quote.id)
            item = self.store.update_item(conn, item.id, status="approved")
            self._event(conn, order, by, "quote.accepted", item_id=item.id, quote_id=quote.id)
            order = self._rederive(conn, order, by)
            # §3.4: accepting a quote releases every other quote's sliced file.
            released = [other for other in self._quote_ids(conn, [item.id]) if other != quote.id]
        self._told(_Change(order, [(item, None)], released))
        return item

    # -- fulfilment (§6.1 by hand) ---------------------------------------------------------

    def _in_production(self, conn: Conn, order_id: str, item_id: str) -> tuple[Order, OrderItem]:
        """The order accepted and not on hold, and the item one of its own."""
        order = self._lock(conn, order_id)
        self._not_ended(order)
        if order.status not in DERIVED:
            raise LifecycleError(
                "order_not_accepted", f"order {order.number} is {order.status}, not accepted"
            )
        self._not_on_hold(order)
        return order, self._item_of(conn, order, item_id)

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

    def _attempt_of(self, conn: Conn, item: OrderItem, fulfilment_id: str) -> Fulfilment:
        attempt = self.store.fulfilment(fulfilment_id, conn=conn)
        if attempt is None or attempt.order_item_id != item.id:
            raise LifecycleError(
                "fulfilment_not_found",
                f"no attempt {fulfilment_id} of item {item.id}",
                not_found=True,
            )
        return attempt

    def update_fulfilment(
        self, order_id: str, item_id: str, fulfilment_id: str, body: FulfilmentInput, by: Actor
    ) -> Fulfilment:
        """An attempt not yet accepted, re-read whole; the item follows the outcome of
        its latest attempt between `failed` and `approved`."""
        with self.store.transaction() as conn:
            order, item = self._in_production(conn, order_id, item_id)
            attempt = self._attempt_of(conn, item, fulfilment_id)
            if attempt.accepted_at is not None:
                raise LifecycleError(
                    "fulfilment_accepted", f"attempt {attempt.attempt} is accepted"
                )
            if item.status not in FULFILLABLE:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
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
            latest = self.store.fulfilments(item.id, conn=conn)[-1]
            status = self._after_attempt(latest.outcome)
            changed: list[tuple[OrderItem, int | None]] = []
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
        accepted attempts reach its quantity."""
        with self.store.transaction() as conn:
            order, item = self._in_production(conn, order_id, item_id)
            attempt = self._attempt_of(conn, item, fulfilment_id)
            if attempt.accepted_at is not None:
                raise LifecycleError(
                    "fulfilment_accepted", f"attempt {attempt.attempt} is already accepted"
                )
            if item.status not in FULFILLABLE:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
            if attempt.outcome == "failed":
                raise LifecycleError(
                    "fulfilment_failed", f"attempt {attempt.attempt} failed; nothing was made"
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
    ) -> Attachment:
        """The row of a file already kept as ``blob_key``, on the order or one of its
        items; the store holds the blob for it in the same transaction."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            item = None if item_id is None else self._item_of(conn, order, item_id)
            attachment = self.store.insert_attachment(
                conn,
                attachment_id=attachment_id,
                order_id=order.id,
                subject=order_subject(order.id) if item is None else item_subject(item.id),
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
                role=attachment.role,
            )
            change = _Change(order)
            self._subject_item(change, conn, attachment)
        self._told(change)
        return attachment

    def edit_attachment(
        self, order_id: str, attachment_id: str, patch: AttachmentPatch, by: Actor
    ) -> Attachment:
        """The caption and the role; a field left out stays. An image marked delivered
        keeps its mark whatever its role becomes."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            before = self._attachment_of(conn, order, attachment_id)
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
            change = _Change(order)
            self._subject_item(change, conn, attachment)
        self._told(change)
        return attachment

    def remove_attachment(self, order_id: str, attachment_id: str, by: Actor) -> None:
        """A mis-upload (Ruling 6): the row goes, and with it its hold on the file and
        the item's delivered mark when it was this one."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            attachment = self._attachment_of(conn, order, attachment_id)
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
        """Mark one of the item's own image attachments, one a browser draws, as what
        was actually delivered, or with None clear the mark."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            item = self._item_of(conn, order, item_id)
            if attachment_id is not None:
                found = self.store.attachment(attachment_id, conn=conn)
                if found is None or found[0].subject != item_subject(item.id):
                    raise LifecycleError(
                        "attachment_id",
                        f"attachment {attachment_id} is not attached to this item",
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
