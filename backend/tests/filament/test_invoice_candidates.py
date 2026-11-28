"""What an invoice line could update instead of creating (#6267)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament.models import SpoolCreate, SpoolPatch, SpoolTypePatch
from scadbuddy.filament.purchasing_models import InvoiceLineIn, LineCandidates
from tests.conftest import PgPool
from tests.support.purchasing import Shop

pytestmark = pytest.mark.requires_postgres

D = Decimal


def _spool(shop: Shop, type_id: object, cost: str | None = None) -> int:
    made = shop.spools.create(
        SpoolCreate.model_validate(
            {"type_id": str(type_id), "cost_per_kg": cost, "weight_locked": False}
        ),
        actor="test",
    )
    return made.number


def _candidates(shop: Shop, invoice_id: UUID) -> list[LineCandidates]:
    found = shop.invoices.candidates(invoice_id)
    assert found is not None
    return found


def test_candidates_split_uncosted_and_costed(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    type_id = shop.type_id()
    bare = _spool(shop, type_id)
    priced = _spool(shop, type_id, "21.50")
    gone = shop.spools.create(SpoolCreate.model_validate({"type_id": str(type_id)}), actor="test")
    shop.spools.update(gone.id, SpoolPatch(archived=True), actor="test")
    other = shop.type_id("PLA-BLK")
    shop.types.update(other, SpoolTypePatch(rgba="#000000FF"), actor="ann")
    _spool(shop, other)
    invoice = shop.draft(
        [InvoiceLineIn(description="PLA white", quantity=1, unit_price=D(40), type_id=type_id)]
    )
    (line,) = _candidates(shop, invoice.id)
    assert line.position == invoice.lines[0].position
    # An archived spool was still bought (#6707 R-6707-12): it is listed, marked archived.
    assert [(s.number, s.archived) for s in line.uncosted] == [(bare, False), (gone.number, True)]
    assert [s.number for s in line.costed] == [priced]
    assert line.costed[0].cost_per_kg == D("21.5")
    # A costed spool with no invoice line may still be filled (#6700, R-6700-1).
    assert line.costed[0].invoice_line_id is None


def test_a_missing_invoice_has_no_candidates_answer(pg_pool: PgPool) -> None:
    assert Shop(pg_pool).invoices.candidates(uuid4()) is None


def test_a_proposed_type_matches_by_material_and_rgba(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    type_id = shop.type_id()
    match = _spool(shop, type_id)
    translucent = shop.type_id("PLA-WHT-T")
    shop.types.update(translucent, SpoolTypePatch(rgba="#FFFFFF80"), actor="ann")
    _spool(shop, translucent)
    proposed = shop.type_body("PLA-NEW")
    invoice = shop.draft(
        [
            InvoiceLineIn(
                description="PLA white", quantity=1, unit_price=D(40), proposed_type=proposed
            )
        ]
    )
    (line,) = _candidates(shop, invoice.id)
    assert [s.number for s in line.uncosted] == [match]
    assert line.costed == []


def test_a_proposed_type_that_names_its_material_matches_that_material(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    match = _spool(shop, shop.type_id())
    named = shop.type_body("PLA-NEW", material_id=None, material_name="pla")
    unknown = shop.type_body("XYZ-NEW", material_id=None, material_name="Unobtainium")
    invoice = shop.draft(
        [
            InvoiceLineIn(description="a", quantity=1, unit_price=D(20), proposed_type=named),
            InvoiceLineIn(description="b", quantity=1, unit_price=D(20), proposed_type=unknown),
        ]
    )
    first, second = _candidates(shop, invoice.id)
    assert [s.number for s in first.uncosted] == [match]
    assert (second.uncosted, second.costed) == ([], [])


def test_a_line_with_no_type_has_no_candidates(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    _spool(shop, shop.type_id())
    invoice = shop.draft([InvoiceLineIn(description="a gift card", quantity=1, unit_price=D(40))])
    (line,) = _candidates(shop, invoice.id)
    assert line.uncosted == []
    assert line.costed == []


def test_a_type_cost_does_not_make_a_spool_costed(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    type_id = shop.types.create(shop.type_body("PLA-PRICED", cost_per_kg="19")).id
    bare = _spool(shop, type_id)
    invoice = shop.draft(
        [InvoiceLineIn(description="PLA white", quantity=1, unit_price=D(40), type_id=type_id)]
    )
    (line,) = _candidates(shop, invoice.id)
    assert [s.number for s in line.uncosted] == [bare]
    assert line.uncosted[0].cost_per_kg == D("19")
    assert line.costed == []


def test_a_spool_linked_to_an_invoice_line_is_costed(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    type_id = shop.type_id()
    linked = _spool(shop, type_id)
    invoice = shop.draft(
        [InvoiceLineIn(description="PLA white", quantity=1, unit_price=D(40), type_id=type_id)]
    )
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_spools SET invoice_line_id = %s WHERE number = %s",
            (invoice.lines[0].id, linked),
        )
    (line,) = _candidates(shop, invoice.id)
    assert line.uncosted == []
    assert [s.number for s in line.costed] == [linked]
    # It is refused at confirm (#6700, R-6700-3): the session tells it by its line.
    assert line.costed[0].invoice_line_id == invoice.lines[0].id
