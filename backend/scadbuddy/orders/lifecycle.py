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

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scadbuddy.core.events import EventBus, OrderChanged, OrderItemChanged, emit
from scadbuddy.orders.models import (
    Actor,
    AddressSnapshot,
    ChargeInput,
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
from scadbuddy.orders.reference import ResolvedReference
from scadbuddy.orders.store import UNSET, Conn, OrderNotFoundError, OrderStore, UnsetType

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
    ) -> None:
        super().__init__(message)
        self.slug = slug
        self.items = list(items)
        self.shipments = list(shipments)
        #: The row the call names is not there: a 404 rather than a 409.
        self.not_found = not_found


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
    def __init__(self, store: OrderStore, bus: EventBus | None) -> None:
        self.store = store
        self.bus = bus

    # -- the frame every transition runs in -------------------------------------------

    def _told(self, change: _Change) -> None:
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
            for item in self.store.items(order.id, conn=conn):
                if item.status not in SETTLED:
                    moved = self.store.update_item(conn, item.id, status="cancelled")
                    change.items.append((moved, None))
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
        self._told(_Change(closed))
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
    ) -> OrderItem:
        """The reference (replaced whole) and the quantity while `draft` or `quoted`,
        each dropping the accepted quote (Ruling 1); title and notes in any open state."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            self._not_ended(order)
            item = self._item_of(conn, order, item_id)
            if item.status in SETTLED:
                raise LifecycleError("item_not_open", f"item {item_id} is {item.status}")
            repins = reference is not None or quantity is not None
            if repins:
                self._open_for_items(order, "an item's reference and quantity")
            fields: dict[str, Any] = {}
            if reference is not None:
                fields.update(reference=reference.reference, colours=reference.colours)
            if not isinstance(output_id, UnsetType):
                fields["output_id"] = output_id
            if quantity is not None:
                fields["quantity"] = quantity
            if title is not None:
                fields["title"] = title
            if notes is not None:
                fields["notes"] = notes
            if repins:
                self.store.unaccept_quotes(conn, item.id)
                fields["status"] = "draft"
            item = self.store.update_item(conn, item.id, **fields)
            self._event(conn, order, by, "item.updated", item_id=item.id, repinned=repins)
            order = self._rederive(conn, order, by)
        self._told(_Change(order, [(item, None)]))
        return item

    def delete_item(self, order_id: str, item_id: str, by: Actor) -> None:
        """Only a `draft` item deletes (Ruling 6, 10); else `item_not_draft`, and the
        caller decides whether to cancel it."""
        with self.store.transaction() as conn:
            order = self._lock(conn, order_id)
            item = self._item_of(conn, order, item_id)
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
        self._told(_Change(order, [(item, None)]))
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
            changed: list[tuple[OrderItem, int | None]] = []
            if item.status == "draft":
                item = self.store.update_item(conn, item.id, status="quoted")
                changed.append((item, None))
            self._event(conn, order, by, "quote.recorded", item_id=item.id, quote_id=quote.id)
        self._told(_Change(order, changed))
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
        self._told(_Change(order, [(item, None)]))
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
