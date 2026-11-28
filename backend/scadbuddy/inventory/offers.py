"""Supplier offers and their pack prices (#7067, spec §6.5). An offer is one item at one
supplier (`filament_suppliers` until phase 3 renames it); a SKU is unique per supplier and
item among the unarchived, so one SKU may cover several items, such as a bundle (D13).
Offers are for non-filament items only until phase 3 (D12, Ruling 9). A price point is a
pack price in any ISO currency (R-7067-11). Nothing here reaches Bambuddy (D16).

A write with a ``key`` (the request's ``Idempotency-Key``, scoped to its sender by
`offer_key`) runs once per key (#8437). Its reply goes into `crud_requests`, under the
caller ``inventory``, in the write's own transaction. A retry with the key gets that reply
and writes nothing. A write that raises keeps no reply. A write with no key is a write of
its own."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow
from pydantic import BaseModel

from scadbuddy.crud.idempotency import Replay, idempotent_write
from scadbuddy.inventory.models import (
    Offer,
    OfferInput,
    OfferPatch,
    OfferPricePoint,
    PriceInput,
)
from scadbuddy.inventory.stock import FilamentItemRefused, ItemNotFound
from scadbuddy.orders.models import Actor

Conn = Connection[DictRow]


class OfferNotFound(LookupError):  # noqa: N818
    """No such offer."""


class OfferExists(ValueError):  # noqa: N818
    """An unarchived offer of the item at the supplier has that SKU in any case."""


class SupplierNotFound(LookupError):  # noqa: N818
    """No such supplier, or an archived one, for a new or restored offer."""


_PRICE = "SELECT id, price, currency, on_date, source, invoice_line_id FROM offer_price_points"
#: Newest first: by date, then by when it was written.
_PRICE_ORDER = " ORDER BY on_date DESC, created_at DESC, id DESC"

_OFFERS = f"""
SELECT o.id, o.supplier_id, s.name AS supplier_name, o.item_id, i.name AS item_name, o.sku,
    o.url, o.pack_size, o.pack_uom, o.note, o.archived_at,
    (SELECT to_jsonb(p) FROM ({_PRICE} WHERE offer_id = o.id{_PRICE_ORDER} LIMIT 1) p)
        AS latest_price
FROM supplier_offers o
JOIN filament_suppliers s ON s.id = o.supplier_id
JOIN items i ON i.id = o.item_id
"""
_OFFER_ORDER = " ORDER BY upper(o.sku), lower(i.name), o.id"

#: The fields a null in a patch clears; any other null keeps the value.
_NULLABLE = frozenset({"url", "note"})

#: The ledger caller of an offer write's reply: the inventory's, whose replies stay 7 days
#: (`crud/idempotency.py` `RETENTION_BY_CALLER`). Go's inventory writes key their replies
#: by operation id. An offer key starts with ``offer:``, so the two never meet.
LEDGER_CALLER: Final = "inventory"
#: The route each keyed write records. The same key on another route is a 422.
CREATE_ROUTE: Final = "POST /inventory/offers"
UPDATE_ROUTE: Final = "PATCH /inventory/offers/{offer_id}"
ARCHIVE_ROUTE: Final = "POST /inventory/offers/{offer_id}/archive"
PRICE_ROUTE: Final = "POST /inventory/offers/{offer_id}/prices"


def offer_key(by: Actor, key: str) -> str:
    """The ledger key of ``key`` from ``by``: the keys of two senders never meet."""
    return f"offer:{by.kind}:{by.principal or ''}:{key}"


class OfferStore:
    def __init__(self, pool: Any, shop_currency: Callable[[], str]) -> None:
        self.pool = pool
        self._shop_currency = shop_currency

    def for_item(self, item_id: UUID, include_archived: bool = False) -> builtins.list[Offer]:
        return self._list("o.item_id = %s", item_id, include_archived)

    def for_supplier(
        self, supplier_id: UUID, include_archived: bool = False
    ) -> builtins.list[Offer]:
        return self._list("o.supplier_id = %s", supplier_id, include_archived)

    def by_sku(self, supplier_id: UUID, sku: str) -> builtins.list[Offer]:
        """Every unarchived offer of the supplier with that SKU in any case: each item a
        bundle SKU covers (D13)."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"{_OFFERS} WHERE o.supplier_id = %s AND upper(o.sku) = upper(%s)"
                f" AND o.archived_at IS NULL{_OFFER_ORDER}",
                (supplier_id, sku),
            ).fetchall()
        return [_of(r) for r in rows]

    def get(self, offer_id: UUID) -> Offer | None:
        with self.pool.connection() as conn:
            return _get(conn, offer_id)

    def supplier_exists(self, supplier_id: UUID) -> bool:
        """A supplier of that id, archived or not."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM filament_suppliers WHERE id = %s", (supplier_id,)
            ).fetchone()
        return row is not None

    def _once[T: BaseModel](
        self,
        key: str | None,
        route: str,
        asked: Any,
        reply_status: int,
        model: type[T],
        write: Callable[[Conn], T],
    ) -> T:
        """``write`` in one transaction. With a ``key``, once per key: the reply stored
        under it, else ``write``'s, stored in the same transaction (`idempotent_write`).
        ``asked`` is the request as JSON, ``route`` the route it came on."""
        with self.pool.connection() as conn:
            if key is None:
                with conn.transaction():
                    return write(conn)

            def run() -> Replay:
                return Replay(reply_status, write(conn).model_dump(mode="json"))

            reply = idempotent_write(conn, LEDGER_CALLER, key, route, asked, run)
        return model.model_validate(reply.body)

    def create(self, body: OfferInput, by: Actor, key: str | None = None) -> Offer:
        """`SupplierNotFound` for an unknown or archived supplier, `ItemNotFound` for an
        unknown or archived item, `FilamentItemRefused` for a filament item, `OfferExists`
        for a SKU the item already has at the supplier."""
        asked = body.model_dump(mode="json")
        return self._once(key, CREATE_ROUTE, asked, 201, Offer, lambda conn: _create(conn, body))

    def update(self, offer_id: UUID, patch: OfferPatch, by: Actor, key: str | None = None) -> Offer:
        """The fields `patch` names: `OfferNotFound`, or `OfferExists` for a SKU the item
        already has at the supplier."""
        asked = {"offer_id": str(offer_id), **patch.model_dump(mode="json", exclude_unset=True)}
        return self._once(
            key, UPDATE_ROUTE, asked, 200, Offer, lambda conn: _update(conn, offer_id, patch)
        )

    def archive(self, offer_id: UUID, key: str | None = None) -> Offer:
        asked = {"offer_id": str(offer_id)}
        return self._once(key, ARCHIVE_ROUTE, asked, 200, Offer, lambda c: _archive(c, offer_id))

    def restore(self, offer_id: UUID) -> Offer:
        """`SupplierNotFound` while its supplier is archived, `ItemNotFound` while its item
        is, `FilamentItemRefused` for a filament item; `OfferExists` when an unarchived
        offer of the item at the supplier took its SKU meanwhile."""
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT supplier_id, item_id, sku FROM supplier_offers WHERE id = %s FOR UPDATE",
                (offer_id,),
            ).fetchone()
            if row is None:
                raise OfferNotFound(str(offer_id))
            _lock_supplier(conn, row["supplier_id"])
            _lock_item(conn, row["item_id"])
            try:
                conn.execute(
                    "UPDATE supplier_offers SET archived_at = NULL, updated_at = now()"
                    " WHERE id = %s",
                    (offer_id,),
                )
            except psycopg.errors.UniqueViolation:
                raise OfferExists(row["sku"]) from None
            found = _get(conn, offer_id)
        assert found is not None
        return found

    def add_price(
        self, offer_id: UUID, body: PriceInput, key: str | None = None
    ) -> OfferPricePoint:
        """A `manual` pack price; `OfferNotFound`."""
        # The shop's currency is a settings read on a connection of its own: take it first.
        currency = body.currency or self._shop_currency()
        on_date = body.on_date or datetime.now(UTC).date()
        # The request as sent, not its defaults: a retry on another day is the same request.
        asked = {"offer_id": str(offer_id), **body.model_dump(mode="json")}

        def write(conn: Conn) -> OfferPricePoint:
            if (
                conn.execute(
                    "SELECT 1 FROM supplier_offers WHERE id = %s FOR SHARE", (offer_id,)
                ).fetchone()
                is None
            ):
                raise OfferNotFound(str(offer_id))
            row = conn.execute(
                "INSERT INTO offer_price_points (offer_id, price, currency, on_date, source)"
                " VALUES (%s, %s, %s, %s, 'manual') RETURNING id, price, currency, on_date,"
                " source, invoice_line_id",
                (offer_id, body.price, currency, on_date),
            ).fetchone()
            assert row is not None
            return OfferPricePoint.model_validate(dict(row))

        return self._once(key, PRICE_ROUTE, asked, 201, OfferPricePoint, write)

    def prices(self, offer_id: UUID) -> builtins.list[OfferPricePoint]:
        """The offer's price points, newest first."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"{_PRICE} WHERE offer_id = %s{_PRICE_ORDER}", (offer_id,)
            ).fetchall()
        return [OfferPricePoint.model_validate(dict(r)) for r in rows]

    def _list(self, where: str, value: UUID, include_archived: bool) -> builtins.list[Offer]:
        live = "" if include_archived else " AND o.archived_at IS NULL"
        with self.pool.connection() as conn:
            rows = conn.execute(f"{_OFFERS} WHERE {where}{live}{_OFFER_ORDER}", (value,)).fetchall()
        return [_of(r) for r in rows]


def _create(conn: Conn, body: OfferInput) -> Offer:
    _lock_supplier(conn, body.supplier_id)
    _lock_item(conn, body.item_id)
    try:
        row = conn.execute(
            "INSERT INTO supplier_offers (supplier_id, item_id, sku, url, pack_size,"
            " pack_uom, note) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                body.supplier_id,
                body.item_id,
                body.sku,
                body.url,
                body.pack_size,
                body.pack_uom,
                body.note,
            ),
        ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise OfferExists(body.sku) from None
    assert row is not None
    made = _get(conn, row["id"])
    assert made is not None
    return made


def _update(conn: Conn, offer_id: UUID, patch: OfferPatch) -> Offer:
    fields = patch.model_dump(exclude_unset=True)
    changes = {k: v for k, v in fields.items() if v is not None or k in _NULLABLE}
    if (
        conn.execute(
            "SELECT 1 FROM supplier_offers WHERE id = %s FOR UPDATE", (offer_id,)
        ).fetchone()
        is None
    ):
        raise OfferNotFound(str(offer_id))
    if changes:
        sets = ", ".join(f"{k} = %s" for k in changes)
        try:
            conn.execute(
                f"UPDATE supplier_offers SET {sets}, updated_at = now() WHERE id = %s",
                (*changes.values(), offer_id),
            )
        except psycopg.errors.UniqueViolation:
            raise OfferExists(str(changes.get("sku"))) from None
    found = _get(conn, offer_id)
    assert found is not None
    return found


def _archive(conn: Conn, offer_id: UUID) -> Offer:
    done = conn.execute(
        "UPDATE supplier_offers SET archived_at = coalesce(archived_at, now()),"
        " updated_at = now() WHERE id = %s",
        (offer_id,),
    ).rowcount
    if not done:
        raise OfferNotFound(str(offer_id))
    found = _get(conn, offer_id)
    assert found is not None
    return found


def _lock_supplier(conn: Conn, supplier_id: UUID) -> None:
    """The supplier, held until commit so it cannot be archived meanwhile;
    `SupplierNotFound` for an unknown or archived one."""
    row = conn.execute(
        "SELECT archived_at FROM filament_suppliers WHERE id = %s FOR SHARE", (supplier_id,)
    ).fetchone()
    if row is None or row["archived_at"] is not None:
        raise SupplierNotFound(str(supplier_id))


def _lock_item(conn: Conn, item_id: UUID) -> None:
    """The item, held until commit so it cannot be archived meanwhile; `ItemNotFound` for
    an unknown or archived one, `FilamentItemRefused` for one of a filament kind (D12,
    Ruling 9)."""
    item = conn.execute(
        "SELECT i.archived_at, k.tracking FROM items i"
        " JOIN item_kinds k ON k.id = i.kind_id WHERE i.id = %s FOR SHARE OF i",
        (item_id,),
    ).fetchone()
    if item is None or item["archived_at"] is not None:
        raise ItemNotFound(str(item_id))
    if item["tracking"] == "filament":
        raise FilamentItemRefused(str(item_id))


def _get(conn: Conn, offer_id: UUID) -> Offer | None:
    row = conn.execute(f"{_OFFERS} WHERE o.id = %s", (offer_id,)).fetchone()
    return None if row is None else _of(row)


def _of(row: dict[str, Any]) -> Offer:
    return Offer.model_validate(dict(row))
