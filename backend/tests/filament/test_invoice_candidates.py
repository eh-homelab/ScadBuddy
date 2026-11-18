"""What an invoice line could update instead of creating (#6267)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from scadbuddy.filament.models import SpoolCreate, SpoolPatch, SpoolTypePatch
from scadbuddy.filament.purchasing_models import InvoiceLineIn
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


def test_candidates_split_uncosted_and_costed(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    type_id = shop.type_id()
    bare = _spool(shop, type_id)
    priced = _spool(shop, type_id, "21.50")
    gone = shop.spools.create(SpoolCreate.model_validate({"type_id": str(type_id)}), actor="test")
    shop.spools.update(gone.id, SpoolPatch(archived=True), actor="test")
    other = shop.type_id("PLA-BLK")
    shop.types.update(other, SpoolTypePatch(rgba="#000000FF"))
    _spool(shop, other)
    invoice = shop.draft(
        [InvoiceLineIn(description="PLA white", quantity=1, unit_price=D(40), type_id=type_id)]
    )
    (line,) = shop.invoices.candidates(invoice.id)
    assert line.position == invoice.lines[0].position
    assert [s.number for s in line.uncosted] == [bare]
    assert [s.number for s in line.costed] == [priced]
    assert line.costed[0].cost_per_kg == D("21.5")


def test_a_proposed_type_matches_by_material_and_rgba(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    type_id = shop.type_id()
    match = _spool(shop, type_id)
    translucent = shop.type_id("PLA-WHT-T")
    shop.types.update(translucent, SpoolTypePatch(rgba="#FFFFFF80"))
    _spool(shop, translucent)
    proposed = shop.type_body("PLA-NEW")
    invoice = shop.draft(
        [
            InvoiceLineIn(
                description="PLA white", quantity=1, unit_price=D(40), proposed_type=proposed
            )
        ]
    )
    (line,) = shop.invoices.candidates(invoice.id)
    assert [s.number for s in line.uncosted] == [match]
    assert line.costed == []


def test_a_line_with_no_type_has_no_candidates(pg_pool: PgPool) -> None:
    shop = Shop(pg_pool)
    _spool(shop, shop.type_id())
    invoice = shop.draft([InvoiceLineIn(description="a gift card", quantity=1, unit_price=D(40))])
    (line,) = shop.invoices.candidates(invoice.id)
    assert line.uncosted == []
    assert line.costed == []
