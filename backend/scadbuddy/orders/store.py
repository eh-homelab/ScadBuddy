"""The orders rows (#4249, spec 2026-10-18 §3): plain CRUD per table over a small
autocommit pool of dict rows, as ``print_library/store.py`` is.

A write that must hold with others runs on the connection :meth:`OrderStore.transaction`
yields; the lifecycle (``lifecycle.py``) opens one, takes :meth:`lock_order` and
writes everything of a transition inside it. A method given no ``conn`` opens its own
short transaction. Rows are keyed by text uuids Postgres makes.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, NoReturn

from psycopg import Connection, errors, sql
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.orders.models import (
    Actor,
    Address,
    AddressInput,
    AddressSnapshot,
    Attachment,
    AttachmentKind,
    AttachmentRole,
    Charge,
    ChargeInput,
    ColourSlot,
    Contact,
    ContactInput,
    Customer,
    Filament,
    FilamentInput,
    FilamentPatch,
    Fulfilment,
    ItemStatus,
    Order,
    OrderEvent,
    OrderFilter,
    OrderItem,
    OrderStatus,
    Outcome,
    Payment,
    Quote,
    QuoteLine,
    QuoteSource,
    Reference,
    Shipment,
    ShipmentItem,
)
from scadbuddy.orders.numbers import next_number

Conn = Connection[DictRow]
Pool = ConnectionPool[Conn]


class UnsetType(Enum):
    UNSET = 0


#: "Not given" for an update, as against ``None`` (clear it).
UNSET = UnsetType.UNSET


class OrderNotFoundError(LookupError):
    def __init__(self, order_id: str) -> None:
        super().__init__(f"no order {order_id}")
        self.order_id = order_id


class RowNotOwnedError(LookupError):
    """A body named a contact or address ``id`` that is not this customer's (Ruling 11):
    the routes answer 422 on it."""

    def __init__(self, table: str, row_id: str, customer_id: str) -> None:
        super().__init__(f"{table} {row_id} is not a row of customer {customer_id}")
        self.table = table
        self.row_id = row_id
        self.customer_id = customer_id


class FilamentExistsError(ValueError):
    """A filament record with the same identity (material, subtype, brand and colour,
    upper-cased; #4250 Ruling 4) is there already: the routes answer 409 naming it."""

    def __init__(self, existing_id: str) -> None:
        super().__init__("a filament with that material, subtype, brand and colour exists")
        self.existing_id = existing_id


_CUSTOMER = "id, display_name, company, notes, tags, created_at, updated_at, archived_at"
_FILAMENT = (
    "id, material, subtype, brand, colour_name, colour, cost_per_kg, density_g_cm3,"
    " archived_at, created_at, updated_at"
)
_CONTACT = 'id, kind, value, label, "primary", position'
_ADDRESS = (
    "id, label, recipient, lines, locality, region, postal_code, country, phone,"
    " default_shipping, default_billing, position"
)
_ORDER = (
    "id, number, customer_id, status, currency, on_hold, shipping_address_id,"
    " billing_address_id, shipping_address, billing_address, notes, due_at, accepted_at,"
    " closed_at, cancelled_at, created_at, updated_at, created_by"
)
_ITEM = (
    "id, order_id, position, status, quantity, title, notes, slug, commit, inputs, libraries,"
    " template_name, render_key, colours, output_id, delivered_attachment_id, created_at,"
    " updated_at"
)
_QUOTE = (
    "id, order_item_id, created_at, source, accepted, sliced_sha256, sliced_blob_key, plates,"
    " lines, unit_cost_minor, unit_price_minor, quantity, total_minor, created_by"
)
_FULFILMENT = (
    "id, order_item_id, attempt, quantity, outcome, failure_reason, started_at, finished_at,"
    " notes, accepted_at, created_by, created_at"
)
_SHIPMENT = (
    "id, order_id, carrier, service, tracking, shipped_at, address, cost_minor, notes, created_at"
)
_CHARGE = "id, order_id, kind, label, amount_minor, position"
_PAYMENT = "id, order_id, amount_minor, method, reference, received_at, notes, created_at"
_EVENT = "id, order_id, at, by, kind, detail"
_ATTACHMENT = (
    "id, order_id, subject, kind, role, filename, media_type, size_bytes, sha256, blob_key,"
    " caption, created_at, created_by"
)

#: The status timestamps `set_order_status` stamps (§3.2).
_STATUS_STAMPS: dict[str, str] = {
    "accepted": "accepted_at",
    "closed": "closed_at",
    "cancelled": "cancelled_at",
}


def encode_cursor(at: datetime, order_id: str) -> str:
    """As ``activity/index.py`` encodes one: the position of the page's last row."""
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{order_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """The position :func:`encode_cursor` wrote; ``ValueError`` for anything else."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, UnicodeError) as error:
        raise ValueError("not a cursor") from error
    created, sep, order_id = raw.partition("|")
    if not sep or not order_id or "\x00" in order_id:
        raise ValueError("not a cursor")
    try:
        at = datetime.fromisoformat(created)
    except ValueError as error:
        raise ValueError("not a cursor") from error
    if at.tzinfo is None:
        raise ValueError("not a cursor")
    return at, order_id


def _item_of(row: DictRow) -> OrderItem:
    return OrderItem(
        id=row["id"],
        order_id=row["order_id"],
        position=row["position"],
        status=row["status"],
        quantity=row["quantity"],
        title=row["title"],
        notes=row["notes"],
        reference=Reference(
            slug=row["slug"],
            commit=row["commit"],
            inputs=row["inputs"],
            libraries=row["libraries"],
            template_name=row["template_name"],
            render_key=row["render_key"],
        ),
        colours=row["colours"],
        output_id=row["output_id"],
        delivered_attachment_id=row["delivered_attachment_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


#: The ``blob_refs`` holder kind of an attachment's file (§6.2's ``attachment:<id>``),
#: written and removed with the attachment's row, in its transaction.
ATTACHMENT_HOLDER = "attachment"


def item_subject(item_id: str) -> str:
    """An item's attachment subject (§6.2)."""
    return f"order_item:{item_id}"


def order_subject(order_id: str) -> str:
    """An order's own attachment subject (§6.2)."""
    return f"order:{order_id}"


def _attachment_of(row: DictRow) -> tuple[Attachment, str]:
    """The attachment and its blob key, which no wire shape carries."""
    subject: str = row["subject"]
    kind, _, subject_id = subject.partition(":")
    return (
        Attachment(
            id=row["id"],
            order_id=row["order_id"],
            subject=subject,
            item_id=subject_id if kind == "order_item" else None,
            kind=row["kind"],
            role=row["role"],
            filename=row["filename"],
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            caption=row["caption"],
            created_at=row["created_at"],
            created_by=row["created_by"],
        ),
        row["blob_key"],
    )


def _decimal_text(value: Decimal | None) -> str | None:
    """A ``numeric`` as the shortest decimal string that is the same number: ``25.5000``
    reads ``25.5``, and ``0E-4`` reads ``0``."""
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return text


def _filament_of(row: DictRow) -> Filament:
    return Filament.model_validate(
        {
            **row,
            "cost_per_kg": _decimal_text(row["cost_per_kg"]),
            "density_g_cm3": _decimal_text(row["density_g_cm3"]),
        }
    )


def _dump(value: Any) -> Any:
    """A model (or list of them) as the JSON its column holds."""
    if value is None:
        return None
    if isinstance(value, list):
        return [_dump(one) for one in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


class OrderStore:
    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    # -- connections ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[Conn]:
        """A connection inside one transaction: what a transition writes everything on."""
        with self._pool.connection() as conn, conn.transaction():
            yield conn

    @contextmanager
    def _write(self, conn: Conn | None) -> Iterator[Conn]:
        """``conn`` as given (the caller's transaction holds), else a short one."""
        if conn is not None:
            yield conn
            return
        with self.transaction() as own:
            yield own

    @contextmanager
    def _read(self, conn: Conn | None) -> Iterator[Conn]:
        if conn is not None:
            yield conn
            return
        with self._pool.connection() as own:
            yield own

    # -- customers (§3.1) -------------------------------------------------------------

    def _customer_of(self, conn: Conn, row: DictRow) -> Customer:
        contacts = conn.execute(
            f"SELECT {_CONTACT} FROM customer_contacts WHERE customer_id = %s ORDER BY position",
            (row["id"],),
        ).fetchall()
        addresses = conn.execute(
            f"SELECT {_ADDRESS} FROM customer_addresses WHERE customer_id = %s ORDER BY position",
            (row["id"],),
        ).fetchall()
        return Customer(
            **row,
            contacts=[Contact.model_validate(c) for c in contacts],
            addresses=[Address.model_validate(a) for a in addresses],
        )

    def insert_customer(
        self,
        *,
        display_name: str,
        company: str | None,
        notes: str,
        tags: Sequence[str],
        contacts: Sequence[ContactInput],
        addresses: Sequence[AddressInput],
        conn: Conn | None = None,
    ) -> Customer:
        with self._write(conn) as c:
            row = c.execute(
                "INSERT INTO customers (display_name, company, notes, tags)"
                f" VALUES (%s, %s, %s, %s) RETURNING {_CUSTOMER}",
                (display_name, company, notes, list(tags)),
            ).fetchone()
            assert row is not None
            self.replace_contacts(c, row["id"], contacts)
            self.replace_addresses(c, row["id"], addresses)
            return self._customer_of(c, row)

    def update_customer(
        self,
        customer_id: str,
        *,
        display_name: str | UnsetType = UNSET,
        company: str | UnsetType | None = UNSET,
        notes: str | UnsetType = UNSET,
        tags: Sequence[str] | UnsetType = UNSET,
        conn: Conn | None = None,
    ) -> Customer | None:
        fields = {
            "display_name": display_name,
            "company": company,
            "notes": notes,
            "tags": list(tags) if isinstance(tags, Sequence) else tags,
        }
        with self._write(conn) as c:
            row = _update(c, "customers", customer_id, fields, _CUSTOMER)
            return None if row is None else self._customer_of(c, row)

    def archive_customer(self, customer_id: str, *, conn: Conn | None = None) -> Customer | None:
        with self._write(conn) as c:
            row = c.execute(
                "UPDATE customers SET archived_at = COALESCE(archived_at, now()),"
                " updated_at = now()"
                f" WHERE id = %s RETURNING {_CUSTOMER}",
                (customer_id,),
            ).fetchone()
            return None if row is None else self._customer_of(c, row)

    def unarchive_customer(self, customer_id: str, *, conn: Conn | None = None) -> Customer | None:
        """Back in the pickers: ``archived_at`` cleared (a customer never archived is
        returned as is)."""
        with self._write(conn) as c:
            row = c.execute(
                "UPDATE customers SET archived_at = NULL, updated_at = now()"
                f" WHERE id = %s RETURNING {_CUSTOMER}",
                (customer_id,),
            ).fetchone()
            return None if row is None else self._customer_of(c, row)

    def customer(
        self, customer_id: str, *, conn: Conn | None = None, share: bool = False
    ) -> Customer | None:
        """``share`` takes the row's share lock (``FOR SHARE``, so ``conn`` must be in
        a transaction): a writer that would archive it waits for this transaction."""
        with self._read(conn) as c:
            row = c.execute(
                f"SELECT {_CUSTOMER} FROM customers WHERE id = %s"
                + (" FOR SHARE" if share else ""),
                (customer_id,),
            ).fetchone()
            return None if row is None else self._customer_of(c, row)

    def customers(
        self, *, q: str | None = None, include_archived: bool = False, limit: int = 100
    ) -> list[Customer]:
        """By name; ``q`` matches the name, the company and any contact's value."""
        where: list[sql.Composable] = []
        params: list[Any] = []
        if not include_archived:
            where.append(sql.SQL("archived_at IS NULL"))
        if q:
            where.append(
                sql.SQL(
                    "(display_name ILIKE %s OR company ILIKE %s OR EXISTS (SELECT 1 FROM"
                    " customer_contacts k WHERE k.customer_id = customers.id AND k.value ILIKE %s))"
                )
            )
            like = f"%{_escape_like(q)}%"
            params += [like, like, like]
        query = sql.SQL("SELECT {} FROM customers{} ORDER BY display_name, id LIMIT %s").format(
            sql.SQL(_CUSTOMER),
            sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where) if where else sql.SQL(""),
        )
        with self._pool.connection() as c:
            rows = c.execute(query, [*params, limit]).fetchall()
            return [self._customer_of(c, row) for row in rows]

    def replace_contacts(
        self, conn: Conn, customer_id: str, contacts: Sequence[ContactInput]
    ) -> list[Contact]:
        """Ruling 11: a row the body names by ``id`` is updated in place, one without is
        inserted, and the rest are deleted. An ``id`` of another customer's row raises
        :class:`RowNotOwnedError`.

        The rows the body drops go first, and every primary is cleared before the body's
        rows are written: `customer_contacts_primary` is a partial unique index checked
        per row as each statement runs, not deferrable (as `accept_quote`), so writing in
        the body's order would refuse a body that replaces the primary email, or that
        lists the new primary before the old one, as a second primary."""
        named = [contact.id for contact in contacts if contact.id is not None]
        conn.execute(
            "DELETE FROM customer_contacts WHERE customer_id = %s AND NOT (id = ANY(%s))",
            (customer_id, named),
        )
        conn.execute(
            'UPDATE customer_contacts SET "primary" = false WHERE customer_id = %s AND "primary"',
            (customer_id,),
        )
        for position, contact in enumerate(contacts):
            if contact.id is None:
                row = conn.execute(
                    "INSERT INTO customer_contacts"
                    ' (customer_id, kind, value, label, "primary", position)'
                    " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (
                        customer_id,
                        contact.kind,
                        contact.value,
                        contact.label,
                        contact.primary,
                        position,
                    ),
                ).fetchone()
            else:
                row = conn.execute(
                    "UPDATE customer_contacts SET kind = %s, value = %s, label = %s,"
                    ' "primary" = %s,'
                    " position = %s WHERE id = %s AND customer_id = %s RETURNING id",
                    (
                        contact.kind,
                        contact.value,
                        contact.label,
                        contact.primary,
                        position,
                        contact.id,
                        customer_id,
                    ),
                ).fetchone()
                if row is None:
                    raise RowNotOwnedError("contact", contact.id, customer_id)
            assert row is not None
        conn.execute("UPDATE customers SET updated_at = now() WHERE id = %s", (customer_id,))
        rows = conn.execute(
            f"SELECT {_CONTACT} FROM customer_contacts WHERE customer_id = %s ORDER BY position",
            (customer_id,),
        ).fetchall()
        return [Contact.model_validate(r) for r in rows]

    def replace_addresses(
        self, conn: Conn, customer_id: str, addresses: Sequence[AddressInput]
    ) -> list[Address]:
        """As :meth:`replace_contacts`. An address an open order points at keeps its id
        while the body carries it; one the body drops is deleted and the order's pointer
        clears (``ON DELETE SET NULL``)."""
        kept: list[str] = []
        for position, address in enumerate(addresses):
            values = (
                address.label,
                address.recipient,
                list(address.lines),
                address.locality,
                address.region,
                address.postal_code,
                address.country,
                address.phone,
                address.default_shipping,
                address.default_billing,
                position,
            )
            if address.id is None:
                row = conn.execute(
                    "INSERT INTO customer_addresses (customer_id, label, recipient, lines,"
                    " locality, region, postal_code, country, phone, default_shipping,"
                    " default_billing, position)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (customer_id, *values),
                ).fetchone()
            else:
                row = conn.execute(
                    "UPDATE customer_addresses SET label = %s, recipient = %s, lines = %s,"
                    " locality = %s, region = %s, postal_code = %s, country = %s, phone = %s,"
                    " default_shipping = %s, default_billing = %s, position = %s"
                    " WHERE id = %s AND customer_id = %s RETURNING id",
                    (*values, address.id, customer_id),
                ).fetchone()
                if row is None:
                    raise RowNotOwnedError("address", address.id, customer_id)
            assert row is not None
            kept.append(row["id"])
        conn.execute(
            "DELETE FROM customer_addresses WHERE customer_id = %s AND NOT (id = ANY(%s))",
            (customer_id, kept),
        )
        conn.execute("UPDATE customers SET updated_at = now() WHERE id = %s", (customer_id,))
        rows = conn.execute(
            f"SELECT {_ADDRESS} FROM customer_addresses WHERE customer_id = %s ORDER BY position",
            (customer_id,),
        ).fetchall()
        return [Address.model_validate(r) for r in rows]

    def address(self, address_id: str, *, conn: Conn | None = None) -> tuple[str, Address] | None:
        """An address row with its customer's id, for the checks an order's pointer needs."""
        with self._read(conn) as c:
            row = c.execute(
                f"SELECT customer_id, {_ADDRESS} FROM customer_addresses WHERE id = %s",
                (address_id,),
            ).fetchone()
        if row is None:
            return None
        customer_id = row.pop("customer_id")
        return customer_id, Address.model_validate(row)

    # -- the price book's filaments (§5.1, #4250) -------------------------------------

    def _existing_filament(
        self, conn: Conn, material: str, subtype: str | None, brand: str | None, colour: str
    ) -> str | None:
        row = conn.execute(
            "SELECT id FROM filaments WHERE upper(material) = upper(%s)"
            " AND upper(coalesce(subtype, '')) = upper(coalesce(%s, ''))"
            " AND upper(coalesce(brand, '')) = upper(coalesce(%s, ''))"
            " AND upper(colour) = upper(%s) AND archived_at IS NULL",
            (material, subtype, brand, colour),
        ).fetchone()
        return None if row is None else str(row["id"])

    def insert_filament(self, body: FilamentInput, *, conn: Conn | None = None) -> Filament:
        """A new record; :class:`FilamentExistsError` for an identity a record in the book
        holds (an archived one frees its identity: the index is partial)."""
        values = (
            body.material,
            body.subtype,
            body.brand,
            body.colour_name,
            body.colour,
            Decimal(body.cost_per_kg),
            None if body.density_g_cm3 is None else Decimal(body.density_g_cm3),
        )
        try:
            with self._write(conn) as c:
                row = c.execute(
                    "INSERT INTO filaments (material, subtype, brand, colour_name, colour,"
                    " cost_per_kg, density_g_cm3) VALUES (%s, %s, %s, %s, %s, %s, %s)"
                    f" RETURNING {_FILAMENT}",
                    values,
                ).fetchone()
        except errors.UniqueViolation:
            self._raise_exists(body.material, body.subtype, body.brand, body.colour)
        assert row is not None
        return _filament_of(row)

    def _raise_exists(
        self, material: str, subtype: str | None, brand: str | None, colour: str
    ) -> NoReturn:
        with self._pool.connection() as c:
            existing = self._existing_filament(c, material, subtype, brand, colour)
        raise FilamentExistsError(existing or "")

    def update_filament(
        self, filament_id: str, patch: FilamentPatch, *, conn: Conn | None = None
    ) -> Filament | None:
        """The fields ``patch`` sends; ``None`` for no such record."""
        sent = patch.model_fields_set
        fields: dict[str, Any] = {}
        for name in ("material", "colour", "cost_per_kg"):
            value = getattr(patch, name)
            if name in sent and value is not None:
                fields[name] = Decimal(value) if name == "cost_per_kg" else value
        for name in ("subtype", "brand", "colour_name", "density_g_cm3"):
            if name in sent:
                value = getattr(patch, name)
                fields[name] = (
                    Decimal(value) if name == "density_g_cm3" and value is not None else value
                )
        try:
            with self._write(conn) as c:
                row = _update(c, "filaments", filament_id, fields, _FILAMENT)
        except errors.UniqueViolation:
            current = self.filament(filament_id)
            assert current is not None
            merged = current.model_copy(update=fields)
            self._raise_exists(merged.material, merged.subtype, merged.brand, merged.colour)
        return None if row is None else _filament_of(row)

    def archive_filament(self, filament_id: str, *, conn: Conn | None = None) -> Filament | None:
        """Hidden from the book from now on; a quote priced with it keeps its lines."""
        with self._write(conn) as c:
            row = c.execute(
                "UPDATE filaments SET archived_at = COALESCE(archived_at, now()),"
                f" updated_at = now() WHERE id = %s RETURNING {_FILAMENT}",
                (filament_id,),
            ).fetchone()
        return None if row is None else _filament_of(row)

    def filament(self, filament_id: str, *, conn: Conn | None = None) -> Filament | None:
        with self._read(conn) as c:
            row = c.execute(
                f"SELECT {_FILAMENT} FROM filaments WHERE id = %s", (filament_id,)
            ).fetchone()
        return None if row is None else _filament_of(row)

    def filaments(
        self, *, include_archived: bool = False, conn: Conn | None = None
    ) -> list[Filament]:
        """By material, brand and colour name."""
        where = "" if include_archived else " WHERE archived_at IS NULL"
        with self._read(conn) as c:
            rows = c.execute(
                f"SELECT {_FILAMENT} FROM filaments{where}"
                " ORDER BY upper(material), upper(coalesce(subtype, '')),"
                " upper(coalesce(brand, '')), upper(coalesce(colour_name, colour)), id"
            ).fetchall()
        return [_filament_of(row) for row in rows]

    # -- orders (§3.2) ----------------------------------------------------------------

    def insert_order(
        self,
        conn: Conn,
        *,
        customer_id: str,
        currency: str,
        created_by: Actor,
        notes: str = "",
        due_at: datetime | None = None,
        shipping_address_id: str | None = None,
        billing_address_id: str | None = None,
    ) -> Order:
        """A draft order with the year's next number (Ruling 5), in the caller's
        transaction so a failed insert gives the number back."""
        number = next_number(conn, datetime.now(UTC).year)
        row = conn.execute(
            "INSERT INTO orders (number, customer_id, currency, notes, due_at,"
            " shipping_address_id, billing_address_id, created_by)"
            f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_ORDER}",
            (
                number,
                customer_id,
                currency,
                notes,
                due_at,
                shipping_address_id,
                billing_address_id,
                Jsonb(created_by.model_dump(mode="json")),
            ),
        ).fetchone()
        assert row is not None
        return Order.model_validate(row)

    def order(self, order_id: str, *, conn: Conn | None = None) -> Order | None:
        with self._read(conn) as c:
            row = c.execute(f"SELECT {_ORDER} FROM orders WHERE id = %s", (order_id,)).fetchone()
        return None if row is None else Order.model_validate(row)

    def lock_order(self, conn: Conn, order_id: str) -> Order:
        """The order's row, locked for the transaction (§3.5's close rule runs under it)."""
        row = conn.execute(
            f"SELECT {_ORDER} FROM orders WHERE id = %s FOR UPDATE", (order_id,)
        ).fetchone()
        if row is None:
            raise OrderNotFoundError(order_id)
        return Order.model_validate(row)

    def orders(
        self, filters: OrderFilter, cursor: str | None, limit: int
    ) -> tuple[list[Order], str | None]:
        """Newest first, and the cursor of the next page (``None`` on the last)."""
        where: list[sql.Composable] = []
        params: list[Any] = []

        def add(clause: str, *values: Any) -> None:
            where.append(sql.SQL(clause))
            params.extend(values)

        if filters.status is not None:
            add("o.status = %s", filters.status)
        if filters.customer_id is not None:
            add("o.customer_id = %s", filters.customer_id)
        if filters.q:
            like = f"%{_escape_like(filters.q)}%"
            add(
                "(o.number ILIKE %s OR o.notes ILIKE %s OR EXISTS (SELECT 1 FROM customers c"
                " WHERE c.id = o.customer_id AND (c.display_name ILIKE %s OR c.company ILIKE %s)))",
                like,
                like,
                like,
                like,
            )
        if filters.since is not None:
            add("o.created_at >= %s", filters.since)
        if filters.until is not None:
            add("o.created_at <= %s", filters.until)
        if cursor is not None:
            add("(o.created_at, o.id) < (%s, %s)", *decode_cursor(cursor))
        query = sql.SQL(
            "SELECT {} FROM orders o{} ORDER BY o.created_at DESC, o.id DESC LIMIT %s"
        ).format(
            sql.SQL(", ".join(f"o.{column}" for column in _ORDER.replace(" ", "").split(","))),
            sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where) if where else sql.SQL(""),
        )
        with self._pool.connection() as c:
            found = c.execute(query, [*params, limit + 1]).fetchall()
        rows = [Order.model_validate(r) for r in found[:limit]]
        more = len(found) > limit
        return rows, encode_cursor(rows[-1].created_at, rows[-1].id) if more else None

    def update_order_fields(
        self,
        conn: Conn,
        order_id: str,
        *,
        notes: str | UnsetType = UNSET,
        due_at: datetime | UnsetType | None = UNSET,
        shipping_address_id: str | UnsetType | None = UNSET,
        billing_address_id: str | UnsetType | None = UNSET,
        shipping_address: AddressSnapshot | UnsetType | None = UNSET,
        billing_address: AddressSnapshot | UnsetType | None = UNSET,
        on_hold: bool | UnsetType = UNSET,
    ) -> Order:
        fields = {
            "notes": notes,
            "due_at": due_at,
            "shipping_address_id": shipping_address_id,
            "billing_address_id": billing_address_id,
            "shipping_address": _jsonb(shipping_address),
            "billing_address": _jsonb(billing_address),
            "on_hold": on_hold,
        }
        row = _update(conn, "orders", order_id, fields, _ORDER)
        if row is None:
            raise OrderNotFoundError(order_id)
        return Order.model_validate(row)

    def set_order_status(
        self, conn: Conn, order_id: str, status: OrderStatus, at: datetime | None = None
    ) -> Order:
        """The status, and the timestamp it stamps (`accepted_at`, `closed_at`,
        `cancelled_at`) set to ``at`` (now) the first time."""
        at = at or datetime.now(UTC)
        stamp = _STATUS_STAMPS.get(status)
        query = sql.SQL(
            "UPDATE orders SET status = %s, updated_at = now(){} WHERE id = %s RETURNING {}"
        ).format(
            sql.SQL(", {} = COALESCE({}, %s)").format(sql.Identifier(stamp), sql.Identifier(stamp))
            if stamp
            else sql.SQL(""),
            sql.SQL(_ORDER),
        )
        params: list[Any] = [status, *([at] if stamp else []), order_id]
        row = conn.execute(query, params).fetchone()
        if row is None:
            raise OrderNotFoundError(order_id)
        return Order.model_validate(row)

    def hold_quote_files(self, conn: Conn, holder_kind: str, quotes: Sequence[Quote]) -> None:
        """Hold each quote's sliced files (every plate's ``blob_key`` and its
        ``sliced_blob_key``) under ``(holder_kind, <quote id>)``, in the caller's
        transaction: a rollback leaves no hold, and a commit leaves every one. A row a
        failed release left behind is taken over with a fresh ``created_at`` (and locked
        until commit), so the reaper's grace counts from this hold, never the old one.
        One statement for every quote (#4725), so a large order's reopen is one round
        trip; each ``(key, quote)`` pair once, since ``ON CONFLICT DO UPDATE`` refuses to
        touch a row twice in one statement. The rows are fed sorted (`ORDER BY` the
        arrays' ordinality), which in practice locks overlapping keys in one order."""
        pairs: set[tuple[str, str]] = set()
        for quote in quotes:
            keys = {plate.get("blob_key") for plate in quote.plates} | {quote.sliced_blob_key}
            pairs |= {(key, quote.id) for key in keys if isinstance(key, str) and key}
        if not pairs:
            return
        ordered = sorted(pairs)
        conn.execute(
            "INSERT INTO blob_refs (key, holder_kind, holder_id)"
            " SELECT key, %s::text, holder_id"
            " FROM unnest(%s::text[], %s::text[]) WITH ORDINALITY AS t(key, holder_id, n)"
            " ORDER BY n"
            " ON CONFLICT (key, holder_kind, holder_id) DO UPDATE SET created_at = now()",
            (holder_kind, [key for key, _ in ordered], [quote_id for _, quote_id in ordered]),
        )

    def reopen_order_row(self, conn: Conn, order_id: str, status: OrderStatus) -> Order:
        """A closed or cancelled order back to ``status``, its ``closed_at`` and
        ``cancelled_at`` cleared, and ``accepted_at`` too when ``status`` is one before
        acceptance (an accepted order that falls back to ``draft``)."""
        row = conn.execute(
            "UPDATE orders SET status = %s, closed_at = NULL, cancelled_at = NULL,"
            " accepted_at = CASE WHEN %s IN ('draft', 'quoted') THEN NULL ELSE accepted_at END,"
            " updated_at = now()"
            f" WHERE id = %s RETURNING {_ORDER}",
            (status, status, order_id),
        ).fetchone()
        if row is None:
            raise OrderNotFoundError(order_id)
        return Order.model_validate(row)

    # -- items (§3.3) -----------------------------------------------------------------

    def insert_item(
        self,
        conn: Conn,
        order_id: str,
        *,
        reference: Reference,
        colours: Sequence[ColourSlot],
        output_id: str | None,
        quantity: int,
        title: str | None = None,
        notes: str = "",
    ) -> OrderItem:
        """Appended after the order's items; ``title`` defaults to the template's name."""
        row = conn.execute(
            "INSERT INTO order_items (order_id, position, quantity, title, notes, slug, commit,"
            " inputs, libraries, template_name, render_key, colours, output_id)"
            " VALUES (%s, (SELECT COALESCE(MAX(position) + 1, 0) FROM order_items"
            " WHERE order_id = %s),"
            f" %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_ITEM}",
            (
                order_id,
                order_id,
                quantity,
                title or reference.template_name,
                notes,
                reference.slug,
                reference.commit,
                Jsonb(reference.inputs),
                Jsonb(_dump(reference.libraries)),
                reference.template_name,
                reference.render_key,
                Jsonb(_dump(list(colours))),
                output_id,
            ),
        ).fetchone()
        assert row is not None
        return _item_of(row)

    def item(self, item_id: str, *, conn: Conn | None = None) -> OrderItem | None:
        with self._read(conn) as c:
            row = c.execute(f"SELECT {_ITEM} FROM order_items WHERE id = %s", (item_id,)).fetchone()
        return None if row is None else _item_of(row)

    def items(self, order_id: str, *, conn: Conn | None = None) -> list[OrderItem]:
        with self._read(conn) as c:
            rows = c.execute(
                f"SELECT {_ITEM} FROM order_items WHERE order_id = %s ORDER BY position, id",
                (order_id,),
            ).fetchall()
        return [_item_of(r) for r in rows]

    def update_item(
        self,
        conn: Conn,
        item_id: str,
        *,
        reference: Reference | UnsetType = UNSET,
        colours: Sequence[ColourSlot] | UnsetType = UNSET,
        output_id: str | UnsetType | None = UNSET,
        quantity: int | UnsetType = UNSET,
        title: str | UnsetType = UNSET,
        notes: str | UnsetType = UNSET,
        status: ItemStatus | UnsetType = UNSET,
    ) -> OrderItem:
        fields: dict[str, Any] = {
            "output_id": output_id,
            "quantity": quantity,
            "title": title,
            "notes": notes,
            "status": status,
            "colours": _jsonb(list(colours) if isinstance(colours, Sequence) else colours),
        }
        if isinstance(reference, Reference):
            fields.update(
                slug=reference.slug,
                commit=reference.commit,
                inputs=Jsonb(reference.inputs),
                libraries=Jsonb(_dump(reference.libraries)),
                template_name=reference.template_name,
                render_key=reference.render_key,
            )
        row = _update(conn, "order_items", item_id, fields, _ITEM)
        if row is None:
            raise LookupError(f"no order item {item_id}")
        return _item_of(row)

    def set_item_filaments(
        self, conn: Conn, item_id: str, colours: Sequence[ColourSlot]
    ) -> OrderItem:
        """The item's colour slots with their price-book filaments (§3.3, #4250)."""
        return self.update_item(conn, item_id, colours=colours)

    def delete_draft_item(self, conn: Conn, item_id: str) -> bool:
        """Deletes the item while it is `draft` (Ruling 6, 10); False when it is not."""
        deleted = conn.execute(
            "DELETE FROM order_items WHERE id = %s AND status = 'draft' RETURNING id", (item_id,)
        ).fetchone()
        return deleted is not None

    def item_inputs(self) -> Iterator[dict[str, Any]]:
        """Every item's ``inputs``, for the asset scan (§3.3): the uploads they name stay."""
        with self._pool.connection() as c:
            for row in c.execute("SELECT inputs FROM order_items ORDER BY created_at, id"):
                yield row["inputs"]

    # -- quotes (§3.4) ----------------------------------------------------------------

    def insert_quote(
        self,
        conn: Conn,
        item_id: str,
        *,
        source: QuoteSource,
        lines: Sequence[QuoteLine],
        unit_cost_minor: int,
        unit_price_minor: int,
        quantity: int,
        created_by: Actor,
        plates: Sequence[dict[str, Any]] = (),
        sliced_sha256: str | None = None,
        sliced_blob_key: str | None = None,
        quote_id: str | None = None,
    ) -> Quote:
        """``quote_id`` when the caller made it first: a slice quote's ref is taken
        under it before the row exists (#4250 Ruling 3)."""
        row = conn.execute(
            "INSERT INTO order_item_quotes (id, order_item_id, source, sliced_sha256,"
            " sliced_blob_key, plates, lines, unit_cost_minor, unit_price_minor, quantity,"
            " total_minor, created_by) VALUES (COALESCE(%s, gen_random_uuid()::text),"
            f" %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_QUOTE}",
            (
                quote_id,
                item_id,
                source,
                sliced_sha256,
                sliced_blob_key,
                Jsonb(list(plates)),
                Jsonb(_dump(list(lines))),
                unit_cost_minor,
                unit_price_minor,
                quantity,
                unit_price_minor * quantity,
                Jsonb(created_by.model_dump(mode="json")),
            ),
        ).fetchone()
        assert row is not None
        return Quote.model_validate(row)

    def quote(self, quote_id: str, *, conn: Conn | None = None) -> Quote | None:
        with self._read(conn) as c:
            row = c.execute(
                f"SELECT {_QUOTE} FROM order_item_quotes WHERE id = %s", (quote_id,)
            ).fetchone()
        return None if row is None else Quote.model_validate(row)

    def quotes(self, item_id: str, *, conn: Conn | None = None) -> list[Quote]:
        with self._read(conn) as c:
            rows = c.execute(
                f"SELECT {_QUOTE} FROM order_item_quotes WHERE order_item_id = %s"
                " ORDER BY created_at, id",
                (item_id,),
            ).fetchall()
        return [Quote.model_validate(r) for r in rows]

    def accept_quote(self, conn: Conn, item_id: str, quote_id: str) -> Quote:
        """Marks ``quote_id`` accepted and the item's others not, in the caller's
        transaction: the others first, since the partial unique index is checked per
        row as a statement runs (not at its end: `order_item_quotes_accepted` is not
        deferrable, https://www.postgresql.org/docs/17/sql-createindex.html), so one
        statement setting both would trip it between the two rows."""
        conn.execute(
            "UPDATE order_item_quotes SET accepted = false"
            " WHERE order_item_id = %s AND accepted AND id <> %s",
            (item_id, quote_id),
        )
        row = conn.execute(
            "UPDATE order_item_quotes SET accepted = true"
            f" WHERE id = %s AND order_item_id = %s RETURNING {_QUOTE}",
            (quote_id, item_id),
        ).fetchone()
        if row is None:
            raise LookupError(f"no quote {quote_id} of item {item_id}")
        return Quote.model_validate(row)

    def drop_released_holds(self, holder_kind: str, quote_ids: Sequence[str]) -> list[str]:
        """Drop the ``holder_kind`` holds of those of ``quote_ids`` whose item is still
        cancelled or whose order is still closed or cancelled (what a cancel or a close
        released, after its commit), and return their ids. The orders' rows are locked
        first, as every transition locks its order, so a reopen either committed before
        the check (its quotes are left out) or waits for this commit and holds them again:
        never a hold dropped after a reopen took it."""
        if not quote_ids:
            return []
        ids = list(quote_ids)
        with self.transaction() as c:
            c.execute(
                "SELECT o.id FROM orders o WHERE o.id IN (SELECT i.order_id"
                " FROM order_item_quotes q JOIN order_items i ON i.id = q.order_item_id"
                " WHERE q.id = ANY(%s)) ORDER BY o.id FOR SHARE",
                (ids,),
            )
            rows = c.execute(
                "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id IN ("
                "SELECT q.id FROM order_item_quotes q"
                " JOIN order_items i ON i.id = q.order_item_id"
                " JOIN orders o ON o.id = i.order_id"
                " WHERE q.id = ANY(%s)"
                " AND (i.status = 'cancelled' OR o.status IN ('closed', 'cancelled')))"
                " RETURNING holder_id",
                (holder_kind, ids),
            ).fetchall()
        dropped = {row["holder_id"] for row in rows}
        return [quote_id for quote_id in ids if quote_id in dropped]

    def standing_quote_ids(self) -> set[str]:
        """The quotes whose sliced file §3.4 keeps (#4250 Ruling 3): each item's accepted
        quote and its newest unaccepted one, while the item is not cancelled and its
        order neither closed nor cancelled. A fulfilled item's accepted quote stands
        until its order closes."""
        with self._pool.connection() as c:
            rows = c.execute(
                "SELECT q.id FROM order_item_quotes q"
                " JOIN order_items i ON i.id = q.order_item_id"
                " JOIN orders o ON o.id = i.order_id"
                " WHERE i.status <> 'cancelled' AND o.status NOT IN ('closed', 'cancelled')"
                " AND (q.accepted OR q.id = (SELECT n.id FROM order_item_quotes n"
                " WHERE n.order_item_id = q.order_item_id AND NOT n.accepted"
                " ORDER BY n.created_at DESC, n.id DESC LIMIT 1))"
            ).fetchall()
        return {row["id"] for row in rows}

    def unaccept_quotes(self, conn: Conn, item_id: str) -> None:
        """A reference or quantity change drops the accepted quote (Ruling 1); the row
        stays, unaccepted (Ruling 6)."""
        conn.execute(
            "UPDATE order_item_quotes SET accepted = false WHERE order_item_id = %s AND accepted",
            (item_id,),
        )

    # -- fulfilment attempts (§6.1 by hand) -------------------------------------------

    def insert_fulfilment(
        self,
        conn: Conn,
        item_id: str,
        *,
        quantity: int,
        outcome: Outcome,
        created_by: Actor,
        failure_reason: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        notes: str = "",
    ) -> Fulfilment:
        """The item's next attempt; numbered under the order's lock, which every writer
        of an item holds."""
        row = conn.execute(
            "INSERT INTO order_item_fulfilments (order_item_id, attempt, quantity, outcome,"
            " failure_reason, started_at, finished_at, notes, created_by)"
            " VALUES (%s, (SELECT COALESCE(MAX(attempt) + 1, 1) FROM order_item_fulfilments"
            f" WHERE order_item_id = %s), %s, %s, %s, %s, %s, %s, %s) RETURNING {_FULFILMENT}",
            (
                item_id,
                item_id,
                quantity,
                outcome,
                failure_reason,
                started_at,
                finished_at,
                notes,
                Jsonb(created_by.model_dump(mode="json")),
            ),
        ).fetchone()
        assert row is not None
        return Fulfilment.model_validate(row)

    def update_fulfilment(
        self,
        conn: Conn,
        fulfilment_id: str,
        *,
        quantity: int | UnsetType = UNSET,
        outcome: Outcome | UnsetType = UNSET,
        failure_reason: str | UnsetType | None = UNSET,
        started_at: datetime | UnsetType | None = UNSET,
        finished_at: datetime | UnsetType | None = UNSET,
        notes: str | UnsetType = UNSET,
    ) -> Fulfilment:
        fields = {
            "quantity": quantity,
            "outcome": outcome,
            "failure_reason": failure_reason,
            "started_at": started_at,
            "finished_at": finished_at,
            "notes": notes,
        }
        row = _update(
            conn, "order_item_fulfilments", fulfilment_id, fields, _FULFILMENT, stamp=False
        )
        if row is None:
            raise LookupError(f"no fulfilment {fulfilment_id}")
        return Fulfilment.model_validate(row)

    def accept_fulfilment(self, conn: Conn, fulfilment_id: str, at: datetime) -> Fulfilment:
        row = conn.execute(
            "UPDATE order_item_fulfilments SET accepted_at = COALESCE(accepted_at, %s)"
            f" WHERE id = %s RETURNING {_FULFILMENT}",
            (at, fulfilment_id),
        ).fetchone()
        if row is None:
            raise LookupError(f"no fulfilment {fulfilment_id}")
        return Fulfilment.model_validate(row)

    def fulfilment(self, fulfilment_id: str, *, conn: Conn | None = None) -> Fulfilment | None:
        with self._read(conn) as c:
            row = c.execute(
                f"SELECT {_FULFILMENT} FROM order_item_fulfilments WHERE id = %s", (fulfilment_id,)
            ).fetchone()
        return None if row is None else Fulfilment.model_validate(row)

    def fulfilments(self, item_id: str, *, conn: Conn | None = None) -> list[Fulfilment]:
        with self._read(conn) as c:
            rows = c.execute(
                f"SELECT {_FULFILMENT} FROM order_item_fulfilments WHERE order_item_id = %s"
                " ORDER BY attempt",
                (item_id,),
            ).fetchall()
        return [Fulfilment.model_validate(r) for r in rows]

    # -- shipments (§6.3) -------------------------------------------------------------

    def _shipment_of(self, conn: Conn, row: DictRow) -> Shipment:
        items = conn.execute(
            "SELECT order_item_id, quantity FROM order_shipment_items WHERE shipment_id = %s"
            " ORDER BY order_item_id",
            (row["id"],),
        ).fetchall()
        return Shipment(**row, items=[ShipmentItem.model_validate(i) for i in items])

    def _set_shipment_items(
        self, conn: Conn, shipment_id: str, items: Sequence[tuple[str, int]]
    ) -> None:
        conn.execute("DELETE FROM order_shipment_items WHERE shipment_id = %s", (shipment_id,))
        for item_id, quantity in items:
            conn.execute(
                "INSERT INTO order_shipment_items (shipment_id, order_item_id, quantity)"
                " VALUES (%s, %s, %s)",
                (shipment_id, item_id, quantity),
            )

    def insert_shipment(
        self,
        conn: Conn,
        order_id: str,
        *,
        carrier: str | None,
        service: str | None,
        tracking: str | None,
        shipped_at: datetime | None,
        address: AddressSnapshot | None,
        cost_minor: int | None,
        notes: str,
        items: Sequence[tuple[str, int]],
    ) -> Shipment:
        row = conn.execute(
            "INSERT INTO order_shipments (order_id, carrier, service, tracking, shipped_at,"
            " address, cost_minor, notes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            f" RETURNING {_SHIPMENT}",
            (order_id, carrier, service, tracking, shipped_at, _jsonb(address), cost_minor, notes),
        ).fetchone()
        assert row is not None
        self._set_shipment_items(conn, row["id"], items)
        return self._shipment_of(conn, row)

    def update_shipment(
        self,
        conn: Conn,
        shipment_id: str,
        *,
        carrier: str | UnsetType | None = UNSET,
        service: str | UnsetType | None = UNSET,
        tracking: str | UnsetType | None = UNSET,
        shipped_at: datetime | UnsetType | None = UNSET,
        address: AddressSnapshot | UnsetType | None = UNSET,
        cost_minor: int | UnsetType | None = UNSET,
        notes: str | UnsetType = UNSET,
        items: Sequence[tuple[str, int]] | UnsetType = UNSET,
    ) -> Shipment:
        fields = {
            "carrier": carrier,
            "service": service,
            "tracking": tracking,
            "shipped_at": shipped_at,
            "address": _jsonb(address),
            "cost_minor": cost_minor,
            "notes": notes,
        }
        row = _update(conn, "order_shipments", shipment_id, fields, _SHIPMENT, stamp=False)
        if row is None:
            raise LookupError(f"no shipment {shipment_id}")
        if not isinstance(items, UnsetType):
            self._set_shipment_items(conn, shipment_id, items)
        return self._shipment_of(conn, row)

    def shipment(self, shipment_id: str, *, conn: Conn | None = None) -> Shipment | None:
        with self._read(conn) as c:
            row = c.execute(
                f"SELECT {_SHIPMENT} FROM order_shipments WHERE id = %s", (shipment_id,)
            ).fetchone()
            return None if row is None else self._shipment_of(c, row)

    def shipments(self, order_id: str, *, conn: Conn | None = None) -> list[Shipment]:
        with self._read(conn) as c:
            rows = c.execute(
                f"SELECT {_SHIPMENT} FROM order_shipments WHERE order_id = %s"
                " ORDER BY created_at, id",
                (order_id,),
            ).fetchall()
            return [self._shipment_of(c, r) for r in rows]

    # -- charges and payments (§3.2) --------------------------------------------------

    def charges(self, order_id: str, *, conn: Conn | None = None) -> list[Charge]:
        with self._read(conn) as c:
            rows = c.execute(
                f"SELECT {_CHARGE} FROM order_charges WHERE order_id = %s ORDER BY position, id",
                (order_id,),
            ).fetchall()
        return [Charge.model_validate(r) for r in rows]

    def replace_charges(
        self, conn: Conn, order_id: str, charges: Sequence[ChargeInput]
    ) -> list[Charge]:
        """The charges, replaced whole (§4's `PATCH` row)."""
        conn.execute("DELETE FROM order_charges WHERE order_id = %s", (order_id,))
        for position, charge in enumerate(charges):
            conn.execute(
                "INSERT INTO order_charges (order_id, kind, label, amount_minor, position)"
                " VALUES (%s, %s, %s, %s, %s)",
                (order_id, charge.kind, charge.label, charge.amount_minor, position),
            )
        conn.execute("UPDATE orders SET updated_at = now() WHERE id = %s", (order_id,))
        return self.charges(order_id, conn=conn)

    def payments(self, order_id: str, *, conn: Conn | None = None) -> list[Payment]:
        with self._read(conn) as c:
            rows = c.execute(
                f"SELECT {_PAYMENT} FROM order_payments WHERE order_id = %s"
                " ORDER BY created_at, id",
                (order_id,),
            ).fetchall()
        return [Payment.model_validate(r) for r in rows]

    def insert_payment(
        self,
        conn: Conn,
        order_id: str,
        *,
        amount_minor: int,
        method: str | None,
        reference: str | None,
        received_at: datetime | None,
        notes: str,
    ) -> Payment:
        row = conn.execute(
            "INSERT INTO order_payments"
            " (order_id, amount_minor, method, reference, received_at, notes)"
            f" VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_PAYMENT}",
            (order_id, amount_minor, method, reference, received_at, notes),
        ).fetchone()
        assert row is not None
        conn.execute("UPDATE orders SET updated_at = now() WHERE id = %s", (order_id,))
        return Payment.model_validate(row)

    # -- attachments (§6.2, #4251) ------------------------------------------------------

    def insert_attachment(
        self,
        conn: Conn,
        *,
        attachment_id: str,
        order_id: str,
        subject: str,
        kind: AttachmentKind,
        role: AttachmentRole,
        filename: str,
        media_type: str,
        size_bytes: int,
        sha256: str,
        blob_key: str,
        caption: str,
        created_by: Actor,
    ) -> Attachment:
        """The row and its hold on ``blob_key`` (Ruling 8), in one transaction: the
        blob was written and touched just before, so the blob sweep's grace keeps it
        until both are committed, and neither is ever there without the other."""
        conn.execute(
            "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES (%s, %s, %s)"
            " ON CONFLICT DO NOTHING",
            (blob_key, ATTACHMENT_HOLDER, attachment_id),
        )
        row = conn.execute(
            "INSERT INTO attachments (id, order_id, subject, kind, role, filename, media_type,"
            " size_bytes, sha256, blob_key, caption, created_by)"
            f" VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_ATTACHMENT}",
            (
                attachment_id,
                order_id,
                subject,
                kind,
                role,
                filename,
                media_type,
                size_bytes,
                sha256,
                blob_key,
                caption,
                Jsonb(created_by.model_dump(mode="json")),
            ),
        ).fetchone()
        assert row is not None
        return _attachment_of(row)[0]

    def attachment(
        self, attachment_id: str, *, conn: Conn | None = None
    ) -> tuple[Attachment, str] | None:
        """The attachment and its blob key, or None."""
        with self._read(conn) as c:
            row = c.execute(
                f"SELECT {_ATTACHMENT} FROM attachments WHERE id = %s", (attachment_id,)
            ).fetchone()
        return None if row is None else _attachment_of(row)

    def attachments(self, order_id: str, *, conn: Conn | None = None) -> list[Attachment]:
        """Every attachment of the order and of its items, oldest first."""
        with self._read(conn) as c:
            rows = c.execute(
                f"SELECT {_ATTACHMENT} FROM attachments WHERE order_id = %s"
                " ORDER BY created_at, id",
                (order_id,),
            ).fetchall()
        return [_attachment_of(r)[0] for r in rows]

    def update_attachment(
        self,
        conn: Conn,
        attachment_id: str,
        *,
        caption: str | UnsetType = UNSET,
        role: AttachmentRole | UnsetType = UNSET,
    ) -> Attachment:
        row = _update(
            conn,
            "attachments",
            attachment_id,
            {"caption": caption, "role": role},
            _ATTACHMENT,
            stamp=False,
        )
        assert row is not None
        return _attachment_of(row)[0]

    def delete_attachment(self, conn: Conn, attachment_id: str) -> None:
        """The row and its hold on the file, together; the blob sweep takes the bytes
        once nothing else holds them."""
        conn.execute(
            "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            (ATTACHMENT_HOLDER, attachment_id),
        )
        conn.execute("DELETE FROM attachments WHERE id = %s", (attachment_id,))

    def set_delivered(self, conn: Conn, item_id: str, attachment_id: str | None) -> OrderItem:
        row = _update(
            conn, "order_items", item_id, {"delivered_attachment_id": attachment_id}, _ITEM
        )
        assert row is not None
        return _item_of(row)

    # -- the audit line (§3.2, Ruling 12) ---------------------------------------------

    def insert_event(
        self, conn: Conn, order_id: str, by: Actor, kind: str, detail: dict[str, Any]
    ) -> OrderEvent:
        row = conn.execute(
            "INSERT INTO order_events (order_id, by, kind, detail)"
            f" VALUES (%s, %s, %s, %s) RETURNING {_EVENT}",
            (order_id, Jsonb(by.model_dump(mode="json")), kind, Jsonb(detail)),
        ).fetchone()
        assert row is not None
        return OrderEvent.model_validate(row)

    def events(self, order_id: str, *, conn: Conn | None = None) -> list[OrderEvent]:
        with self._read(conn) as c:
            rows = c.execute(
                f"SELECT {_EVENT} FROM order_events WHERE order_id = %s ORDER BY id", (order_id,)
            ).fetchall()
        return [OrderEvent.model_validate(r) for r in rows]


def _jsonb(value: Any) -> Any:
    """A model (or list) for a jsonb column: ``None`` stays null, UNSET stays unset."""
    if value is None or isinstance(value, UnsetType):
        return value
    return Jsonb(_dump(value))


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _update(
    conn: Conn,
    table: str,
    row_id: str,
    fields: dict[str, Any],
    columns: str,
    *,
    stamp: bool = True,
) -> DictRow | None:
    """``UPDATE table SET <the fields not UNSET> [, updated_at = now()] WHERE id``,
    returning the row, or ``None`` when there is none. With nothing to set, a read."""
    given = {name: value for name, value in fields.items() if not isinstance(value, UnsetType)}
    assignments: list[sql.Composable] = [
        sql.SQL("{} = %s").format(sql.Identifier(name)) for name in given
    ]
    if stamp:
        assignments.append(sql.SQL("updated_at = now()"))
    if not assignments:
        return conn.execute(
            sql.SQL("SELECT {} FROM {} WHERE id = %s").format(
                sql.SQL(columns), sql.Identifier(table)
            ),
            (row_id,),
        ).fetchone()
    query = sql.SQL("UPDATE {} SET {} WHERE id = %s RETURNING {}").format(
        sql.Identifier(table), sql.SQL(", ").join(assignments), sql.SQL(columns)
    )
    return conn.execute(query, [*given.values(), row_id]).fetchone()
