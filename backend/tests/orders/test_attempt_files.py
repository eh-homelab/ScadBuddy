"""An attempt's own files (#4251, spec 2026-10-18 §6.2, plan 2026-11-03 Ruling 3): an
attachment whose subject is ``order_item_fulfilment:<id>``, listed under its item too."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from scadbuddy.orders.lifecycle import FulfilmentInput, LifecycleError
from scadbuddy.orders.models import ArchiveCopyMedia, Attachment, Fulfilment, Order, OrderItem
from scadbuddy.orders.store import ArchiveCopyExistsError, OrderStore
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


def copy(
    shop: Shop,
    order: Order,
    subject: str,
    filename: str,
    *,
    archive_id: int = 81,
    archive_media: ArchiveCopyMedia = "finished_photo",
    item_id: str | None = None,
) -> Attachment:
    """A row as an archive copy writes it, through the store alone."""
    sha = "c" * 64
    with shop.rows.transaction() as conn:
        return shop.rows.insert_attachment(
            conn,
            attachment_id=str(uuid.uuid4()),
            order_id=order.id,
            subject=subject,
            kind="image",
            role="finished",
            filename=filename,
            media_type="image/jpeg",
            size_bytes=10,
            sha256=sha,
            blob_key="attachment-" + sha,
            caption=f"Attempt 1, Bambuddy archive {archive_id}",
            created_by=BY,
            archive_id=archive_id,
            archive_media=archive_media,
            subject_item_id=item_id,
        )


def holds(pool: PgPool, key: str) -> int:
    with pool.connection() as conn:
        row = conn.execute("SELECT count(*) AS n FROM blob_refs WHERE key = %s", (key,)).fetchone()
    assert row is not None
    return int(row["n"])


def test_an_archive_copy_names_its_archive_and_medium(shop: Shop) -> None:
    """#7231 (spec 2026-11-05 §4.2)."""
    order, item, attempt = attempted(shop)

    file = copy(shop, order, f"order_item_fulfilment:{attempt.id}", "finish_1.jpg", item_id=item.id)

    assert (file.archive_id, file.archive_media) == (81, "finished_photo")
    assert shop.rows.attachments(order.id) == [file]


def test_a_second_copy_of_one_archive_file_is_refused_and_holds_nothing(
    shop: Shop, pool: PgPool
) -> None:
    """#7231 (spec §4.6): the partial unique index is the net for a race. The refused
    insert leaves no row and no second hold."""
    order, item, attempt = attempted(shop)
    subject = f"order_item_fulfilment:{attempt.id}"
    first = copy(shop, order, subject, "finish_1.jpg", item_id=item.id)

    with pytest.raises(ArchiveCopyExistsError):
        copy(shop, order, subject, "finish_1.jpg", item_id=item.id)

    assert shop.rows.attachments(order.id) == [first]
    assert holds(pool, "attachment-" + "c" * 64) == 1
    # Another finish photo of the same archive is a copy of its own.
    second = copy(shop, order, subject, "finish_2.jpg", item_id=item.id)
    assert shop.rows.attachments(order.id) == [first, second]


def test_the_media_present_are_the_attempts_and_its_items(shop: Shop) -> None:
    """#7231 (spec §4.2): a copy on the item, from before #5268, counts as present."""
    order, item, attempt = attempted(shop)
    copy(shop, order, f"order_item_fulfilment:{attempt.id}", "finish_1.jpg", item_id=item.id)
    copy(shop, order, f"order_item:{item.id}", "timelapse-81.mp4", archive_media="timelapse")
    copy(shop, order, f"order_item:{item.id}", "finish_9.jpg", archive_id=90)
    add(shop, order, item.id, attempt.id)  # a person's own photo: no archive

    present = shop.rows.archive_media_present(attempt.id, item.id)

    assert present == {(81, "finished_photo"), (81, "timelapse"), (90, "finished_photo")}
    _, _, other = attempted(shop)
    assert shop.rows.archive_media_present(other.id, other.order_item_id) == set()


def test_an_attempt_records_its_check(shop: Shop) -> None:
    """#7231 (spec §4.6)."""
    _, _, attempt = attempted(shop)
    assert attempt.media_checked_at is None
    checked = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)

    shop.rows.stamp_media_checked(attempt.id, checked)

    found = shop.rows.fulfilment(attempt.id)
    assert found is not None
    assert found.media_checked_at == checked
