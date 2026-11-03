"""An attempt's own files (#4251, spec 2026-10-18 §6.2, plan 2026-11-03 Ruling 3): an
attachment whose subject is ``order_item_fulfilment:<id>``, listed under its item too."""

from __future__ import annotations

import uuid

import pytest

from scadbuddy.orders.lifecycle import FulfilmentInput, LifecycleError
from scadbuddy.orders.models import Attachment, Fulfilment, Order, OrderItem
from scadbuddy.orders.store import OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop

pytestmark = pytest.mark.requires_postgres


@pytest.fixture(name="shop")
def _shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def attempted(shop: Shop) -> tuple[Order, OrderItem, Fulfilment]:
    order, item = shop.accepted()
    attempt = shop.lifecycle.record_fulfilment(
        order.id, item.id, FulfilmentInput(quantity=1, outcome="succeeded"), BY
    )
    return order, item, attempt


def add(
    shop: Shop, order: Order, item_id: str | None, fulfilment_id: str | None = None
) -> Attachment:
    return shop.lifecycle.add_attachment(
        order.id,
        item_id,
        BY,
        fulfilment_id=fulfilment_id,
        attachment_id=str(uuid.uuid4()),
        kind="image",
        role="finished",
        filename="done.jpg",
        media_type="image/jpeg",
        size_bytes=10,
        sha256="b" * 64,
        blob_key="attachment-" + "b" * 64,
        caption="",
    )


def test_a_file_on_an_attempt_names_the_attempt_and_its_item(shop: Shop) -> None:
    order, item, attempt = attempted(shop)

    file = add(shop, order, item.id, attempt.id)

    assert file.subject == f"order_item_fulfilment:{attempt.id}"
    assert (file.item_id, file.fulfilment_id) == (item.id, attempt.id)
    assert shop.rows.attachments(order.id) == [file]
    found = shop.rows.attachment(file.id)
    assert found is not None and found[0] == file
    assert "attachment.added" in shop.kinds(order)


def test_a_released_claims_files_become_the_items(shop: Shop) -> None:
    """#5268 review: a file attached to a print's claim before its start was refused
    outlives the attempt, as the item's."""
    order, item = shop.accepted()
    claim = shop.lifecycle.claim_print(
        order.id, item.id, BY, request_id="r-1", sliced_sha256=None, sliced_blob_key=None
    )
    file = add(shop, order, item.id, claim.id)

    shop.lifecycle.release_print(claim, "refused")

    assert shop.rows.fulfilments(item.id) == []
    [kept] = shop.rows.attachments(order.id)
    assert (kept.id, kept.subject, kept.item_id, kept.fulfilment_id) == (
        file.id,
        f"order_item:{item.id}",
        item.id,
        None,
    )


def test_an_item_file_names_no_attempt(shop: Shop) -> None:
    order, item, _ = attempted(shop)
    file = add(shop, order, item.id)
    assert (file.item_id, file.fulfilment_id) == (item.id, None)


def test_an_attempt_of_another_item_is_not_found(shop: Shop) -> None:
    order, item, _ = attempted(shop)
    _, _, other = attempted(shop)
    with pytest.raises(LifecycleError) as raised:
        add(shop, order, item.id, other.id)
    assert raised.value.slug == "fulfilment_not_found"


def test_an_attempts_image_may_be_the_items_picture(shop: Shop) -> None:
    order, item, attempt = attempted(shop)
    file = add(shop, order, item.id, attempt.id)

    marked = shop.lifecycle.set_delivered(order.id, item.id, file.id, BY)

    assert marked.delivered_attachment_id == file.id
    shop.lifecycle.remove_attachment(order.id, file.id, BY)
    assert shop.get_item(item).delivered_attachment_id is None
