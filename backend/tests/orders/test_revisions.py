"""Design revisions (#5280, spec 2026-11-02 §4): every design an item has had, written
only by the revision writer, each new one dropping the quote and superseding a sent,
unanswered one."""

from __future__ import annotations

import pytest

from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.models import FilamentInput
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, NOW, Shop, resolved

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def test_a_new_item_has_revision_one(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    [rev] = shop.rows.revisions(item.id)
    assert rev.number == 1 and rev.reference == item.reference
    assert item.current_revision_id == rev.id and rev.colours == item.colours
    assert "revision.added" in shop.kinds(order) and "item.added" in shop.kinds(order)


def test_a_reference_edit_is_the_next_revision(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    item = shop.lifecycle.update_item(order.id, item.id, BY, reference=resolved("other"))
    revisions = shop.rows.revisions(item.id)
    assert [r.number for r in revisions] == [1, 2]
    assert item.reference is not None and item.reference.slug == "other"
    assert item.current_revision_id == revisions[1].id


def test_a_filament_change_is_the_next_revision_and_the_same_filaments_are_not(
    shop: Shop,
) -> None:
    order = shop.order()
    item = shop.item(order)
    fil = shop.rows.insert_filament(
        FilamentInput(material="PLA", colour="#111111", cost_per_kg="1")
    )
    shop.lifecycle.update_item(order.id, item.id, BY, filaments={0: fil.id})
    shop.lifecycle.update_item(order.id, item.id, BY, filaments={0: fil.id})
    revisions = shop.rows.revisions(item.id)
    assert len(revisions) == 2
    assert revisions[1].colours[0].filament_id == fil.id


def test_a_quantity_change_makes_no_revision(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.lifecycle.update_item(order.id, item.id, BY, quantity=4)
    assert len(shop.rows.revisions(item.id)) == 1


def test_a_new_revision_drops_the_accepted_quote(shop: Shop) -> None:
    order = shop.order()
    item = shop.quoted(order, shop.item(order))
    assert item.status == "approved"
    shop.lifecycle.add_revision(order.id, item.id, BY, note="thinner walls")
    assert shop.get_item(item).status == "draft"
    assert not any(q.accepted for q in shop.rows.quotes(item.id))
    assert shop.get(order).status == "draft"


def test_a_revision_without_a_reference_copies_the_current_design(shop: Shop) -> None:
    order = shop.order()
    item = shop.lifecycle.add_item(order.id, BY, resolved(), output_id="out-1", quantity=1)
    rev = shop.lifecycle.add_revision(order.id, item.id, BY, note="photo of the test")
    assert rev.number == 2 and rev.reference == item.reference and rev.note == "photo of the test"
    assert rev.colours == item.colours
    after = shop.get_item(item)
    assert after.current_revision_id == rev.id and after.output_id == "out-1"


def test_a_new_revision_supersedes_a_sent_unanswered_one(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    [first] = shop.rows.revisions(item.id)
    with shop.rows.transaction() as conn:
        shop.rows.stamp_revision(conn, first.id, sent_at=NOW)
    shop.lifecycle.add_revision(order.id, item.id, BY)
    first = shop.rows.revisions(item.id)[0]
    assert first.answer == "superseded" and first.answered_by == BY
    assert first.answered_at is not None
    events = [e for e in shop.rows.events(order.id) if e.kind == "revision.added"]
    assert events[-1].detail["superseded"] == first.id


def test_an_unsent_revision_is_not_superseded(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    shop.lifecycle.add_revision(order.id, item.id, BY)
    assert shop.rows.revisions(item.id)[0].answer is None


def test_a_custom_item_takes_no_revision(shop: Shop) -> None:
    order = shop.order()
    custom = shop.lifecycle.add_custom_item(
        order.id, BY, title="Design work", quantity=1, unit_price_minor=100
    )
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.add_revision(order.id, custom.id, BY)
    assert refused.value.slug == "item_custom"


def test_a_request_without_a_design_needs_a_reference(shop: Shop) -> None:
    order = shop.order()
    with shop.rows.transaction() as conn:
        request = shop.rows.insert_item(
            conn,
            order.id,
            kind="request",
            reference=None,
            colours=[],
            output_id=None,
            quantity=1,
            title="A bracket",
        )
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.add_revision(order.id, request.id, BY)
    assert refused.value.slug == "item_has_no_design"
    rev = shop.lifecycle.add_revision(order.id, request.id, BY, resolved())
    item = shop.get_item(request)
    assert rev.number == 1 and item.kind == "request" and item.current_revision_id == rev.id


def test_a_revision_is_refused_once_the_order_is_accepted(shop: Shop) -> None:
    order, item = shop.accepted()
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.add_revision(order.id, item.id, BY)
    assert refused.value.slug == "item_locked"


def test_a_source_on_a_custom_item_makes_it_a_model_with_revision_one(shop: Shop) -> None:
    order = shop.order()
    custom = shop.lifecycle.add_custom_item(
        order.id, BY, title="Design work", quantity=1, unit_price_minor=100
    )
    item = shop.lifecycle.update_item(order.id, custom.id, BY, reference=resolved())
    assert item.kind == "model" and [r.number for r in shop.rows.revisions(item.id)] == [1]


def test_approval_is_set_while_the_item_is_open(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    assert (
        shop.lifecycle.update_item(order.id, item.id, BY, approval="design_first").approval
        == "design_first"
    )
    assert len(shop.rows.revisions(item.id)) == 1


def test_a_custom_item_takes_no_approval(shop: Shop) -> None:
    order = shop.order()
    custom = shop.lifecycle.add_custom_item(
        order.id, BY, title="Design work", quantity=1, unit_price_minor=100
    )
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.update_item(order.id, custom.id, BY, approval="design_first")
    assert (refused.value.slug, refused.value.unprocessable) == ("approval", True)
