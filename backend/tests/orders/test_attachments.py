"""What an attached file is, from its bytes, and the name it keeps (#4251, §6.2)."""

from __future__ import annotations

import pytest

from scadbuddy.orders.attachments import clean_filename, sniff
from scadbuddy.orders.lifecycle import FulfilmentInput, LifecycleError
from scadbuddy.orders.models import (
    INLINE,
    SHOWN_IMAGES,
    THUMBNAILED,
    Attachment,
    AttachmentPatch,
    AttachmentRole,
    Order,
    OrderItem,
    Revision,
)
from scadbuddy.orders.store import ATTACHMENT_HOLDER, UNSET, OrderStore
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, NOW, Shop, resolved


@pytest.mark.parametrize(
    ("head", "name", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"\0" * 8, "x.bin", ("image", "image/png")),
        (b"\xff\xd8\xff\xe0" + b"\0" * 12, "x", ("image", "image/jpeg")),
        (b"RIFF\0\0\0\0WEBPVP8 ", "x", ("image", "image/webp")),
        (b"GIF89a" + b"\0" * 10, "x", ("image", "image/gif")),
        (b"\0\0\0\x1cftypavif\0\0\0\0", "x", ("image", "image/avif")),
        # An iPhone's photo shares the container with MP4, and is a photo.
        (b"\0\0\0\x18ftypheic\0\0\0\0", "IMG.HEIC", ("image", "image/heic")),
        (b"\0\0\0\x18ftypmif1\0\0\0\0", "x", ("image", "image/heic")),
        (b"\0\0\0\x14ftypqt  \0\0\0\0", "x.mov", ("video", "video/quicktime")),
        (b"\0\0\0\x18ftypisom\0\0\0\0", "x", ("video", "video/mp4")),
        (b"\0\0\0\x18ftypM4A \0\0\0\0", "x.m4a", ("other", "audio/mp4")),
        (b"\x1a\x45\xdf\xa3" + b"\0" * 12, "x", ("video", "video/webm")),
        (b"%PDF-1.7\n" + b"\0" * 7, "x", ("document", "application/pdf")),
        # Anything else is `other`, typed by its name for display only.
        (b"<svg xmlns=...>", "logo.svg", ("other", "image/svg+xml")),
        (b"solid part\n", "part.stl", ("other", "model/stl")),
        (b"\0\1\2", "blob", ("other", "application/octet-stream")),
    ],
)
def test_sniff(head: bytes, name: str, expected: tuple[str, str]) -> None:
    assert sniff(head, name) == expected


def test_only_sniffed_types_are_inline_and_never_a_page() -> None:
    assert "image/svg+xml" not in INLINE
    assert "text/html" not in INLINE
    assert THUMBNAILED <= SHOWN_IMAGES <= INLINE


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        ("photo.jpg", "photo.jpg"),
        ("C:\\Users\\me\\Desktop\\photo.jpg", "photo.jpg"),
        ("../../etc/passwd", "passwd"),
        ("a\x00b\nc.png", "abc.png"),
        ("", "file"),
        ("..", "file"),
        ("x" * 300 + ".png", "x" * 251 + ".png"),
        ("y" * 300, "y" * 255),
    ],
)
def test_clean_filename(sent: str, kept: str) -> None:
    assert clean_filename(sent) == kept


# -- a revision's files (#5280, storefront spec §4) -----------------------------------------


@pytest.fixture
def shop(rows: OrderStore, pool: PgPool) -> Shop:
    return Shop(rows, BlobRefs(pool))


def attach_to(
    shop: Shop,
    order: Order,
    item: OrderItem,
    rev: Revision,
    *,
    role: AttachmentRole = "render",
    fulfilment_id: str | None = None,
    name: str = "r",
) -> Attachment:
    sha = (name * 64)[:64].replace("r", "e")
    return shop.lifecycle.add_attachment(
        order.id,
        item.id,
        BY,
        attachment_id=f"att-{name}-{rev.id}",
        kind="image",
        role=role,
        filename=f"{name}.png",
        media_type="image/png",
        size_bytes=1,
        sha256=sha,
        blob_key=f"attachment-{sha}",
        caption="",
        fulfilment_id=fulfilment_id,
        revision_id=rev.id,
    )


def holders_of(pool: PgPool, attachment_id: str) -> list[str]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            (ATTACHMENT_HOLDER, attachment_id),
        ).fetchall()
    return [row["key"] for row in rows]


@pytest.mark.requires_postgres
def test_a_file_on_a_revision_is_listed_under_its_item(shop: Shop, pool: PgPool) -> None:
    order = shop.order()
    item = shop.item(order)
    [rev] = shop.rows.revisions(item.id)
    att = attach_to(shop, order, item, rev)
    assert att.subject == f"order_item_revision:{rev.id}"
    assert att.item_id == item.id and att.revision_id == rev.id and att.fulfilment_id is None
    assert holders_of(pool, att.id) == [f"attachment-{att.sha256}"]
    assert [a.id for a in shop.rows.attachments(order.id)] == [att.id]
    added = [e for e in shop.rows.events(order.id) if e.kind == "attachment.added"]
    assert added[-1].detail["revision_id"] == rev.id


@pytest.mark.requires_postgres
def test_another_items_revision_is_refused(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    other = shop.item(order)
    [rev] = shop.rows.revisions(other.id)
    with pytest.raises(LifecycleError) as refused:
        attach_to(shop, order, item, rev)
    assert refused.value.slug == "revision_not_found" and refused.value.not_found


@pytest.mark.requires_postgres
def test_a_test_print_photo_names_its_attempt(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    [rev] = shop.rows.revisions(item.id)
    with shop.rows.transaction() as conn:
        test = shop.rows.insert_fulfilment(
            conn,
            item.id,
            quantity=0,
            outcome="unknown",
            created_by=BY,
            purpose="test",
            revision_id=rev.id,
        )
    att = attach_to(shop, order, item, rev, role="test_print", fulfilment_id=test.id)
    assert att.fulfilment_id == test.id and att.revision_id == rev.id


@pytest.mark.requires_postgres
def test_a_production_attempt_is_not_a_revisions_print(shop: Shop) -> None:
    order, item = shop.accepted()
    [rev] = shop.rows.revisions(item.id)
    production = shop.lifecycle.record_fulfilment(
        order.id,
        item.id,
        FulfilmentInput(quantity=1, outcome="succeeded"),
        BY,
    )
    with pytest.raises(LifecycleError) as refused:
        attach_to(shop, order, item, rev, role="test_print", fulfilment_id=production.id)
    assert refused.value.slug == "fulfilment_not_found"


@pytest.mark.requires_postgres
def test_deleting_a_draft_item_removes_its_revisions_files_and_holds(
    shop: Shop, pool: PgPool
) -> None:
    order = shop.order()
    item = shop.item(order)
    item = shop.lifecycle.update_item(order.id, item.id, BY, reference=resolved("other"))
    first, second = shop.rows.revisions(item.id)
    one = attach_to(shop, order, item, first, name="a")
    two = attach_to(shop, order, item, second, name="b")
    assert holders_of(pool, one.id) and holders_of(pool, two.id)

    shop.lifecycle.delete_item(order.id, item.id, BY)

    assert shop.rows.attachments(order.id) == []
    assert not holders_of(pool, one.id) and not holders_of(pool, two.id)
    removed = [e for e in shop.rows.events(order.id) if e.kind == "attachment.removed"]
    assert {e.detail["attachment_id"] for e in removed} == {one.id, two.id}


@pytest.mark.requires_postgres
def test_a_released_test_claim_leaves_its_photo_on_the_revision(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    [rev] = shop.rows.revisions(item.id)
    claim = shop.lifecycle.claim_print(
        order.id,
        item.id,
        BY,
        request_id="trial",
        sliced_sha256=None,
        sliced_blob_key=None,
        revision_id=rev.id,
    )
    att = attach_to(shop, order, item, rev, role="test_print", fulfilment_id=claim.id)
    shop.lifecycle.release_print(claim, "refused")
    [kept] = shop.rows.attachments(order.id)
    assert kept.id == att.id and kept.revision_id == rev.id and kept.fulfilment_id is None


@pytest.mark.requires_postgres
@pytest.mark.parametrize("answer", [None, "changes_requested"])
def test_a_file_of_a_sent_revision_stays(shop: Shop, answer: str | None) -> None:
    order = shop.order()
    item = shop.item(order)
    [rev] = shop.rows.revisions(item.id)
    att = attach_to(shop, order, item, rev)
    with shop.rows.transaction() as conn:
        shop.rows.stamp_revision(conn, rev.id, sent_at=NOW)
        if answer is not None:
            shop.rows.stamp_revision(
                conn, rev.id, answer="changes_requested", answered_at=NOW, answered_by=BY
            )
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.remove_attachment(order.id, att.id, BY)
    assert refused.value.slug == "revision_sent" and not refused.value.unprocessable
    assert [a.id for a in shop.rows.attachments(order.id)] == [att.id]


@pytest.mark.requires_postgres
@pytest.mark.parametrize(
    "patch", [AttachmentPatch(role="reference"), AttachmentPatch(caption="another")]
)
def test_a_file_of_a_sent_revision_keeps_its_role_and_caption(
    shop: Shop, patch: AttachmentPatch
) -> None:
    order = shop.order()
    item = shop.item(order)
    [rev] = shop.rows.revisions(item.id)
    att = attach_to(shop, order, item, rev)
    with shop.rows.transaction() as conn:
        shop.rows.stamp_revision(conn, rev.id, sent_at=NOW)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.edit_attachment(order.id, att.id, patch, BY)
    assert refused.value.slug == "revision_sent"
    [kept] = shop.rows.attachments(order.id)
    assert (kept.role, kept.caption) == ("render", "")


@pytest.mark.requires_postgres
def test_a_render_never_becomes_a_test_print(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    [rev] = shop.rows.revisions(item.id)
    att = attach_to(shop, order, item, rev)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.edit_attachment(order.id, att.id, AttachmentPatch(role="test_print"), BY)
    assert (refused.value.slug, refused.value.unprocessable) == ("role", True)
    assert [a.role for a in shop.rows.attachments(order.id)] == ["render"]


@pytest.mark.requires_postgres
def test_an_unsent_revisions_file_is_re_roled(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    [rev] = shop.rows.revisions(item.id)
    att = attach_to(shop, order, item, rev)
    edited = shop.lifecycle.edit_attachment(
        order.id, att.id, AttachmentPatch(role="reference", caption="sketch"), BY
    )
    assert (edited.role, edited.caption) == ("reference", "sketch")


@pytest.mark.requires_postgres
@pytest.mark.parametrize("role", ["render", "test_print"])
def test_only_a_revision_takes_a_revision_role(shop: Shop, role: AttachmentRole) -> None:
    order = shop.order()
    item = shop.item(order)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.add_attachment(
            order.id,
            item.id,
            BY,
            attachment_id="att-plain",
            kind="image",
            role=role,
            filename="p.png",
            media_type="image/png",
            size_bytes=1,
            sha256="d" * 64,
            blob_key="attachment-" + "d" * 64,
            caption="",
        )
    assert (refused.value.slug, refused.value.unprocessable) == ("role", True)
    plain = shop.lifecycle.add_attachment(
        order.id,
        item.id,
        BY,
        attachment_id="att-plain",
        kind="image",
        role="finished",
        filename="p.png",
        media_type="image/png",
        size_bytes=1,
        sha256="d" * 64,
        blob_key="attachment-" + "d" * 64,
        caption="",
    )
    with pytest.raises(LifecycleError) as reroled:
        shop.lifecycle.edit_attachment(order.id, plain.id, AttachmentPatch(role=role), BY)
    assert (reroled.value.slug, reroled.value.unprocessable) == ("role", True)
    assert shop.rows.attachments(order.id)[0].role == "finished"
    # A row from before the rule keeps its role; resending that role is no change.
    with shop.rows.transaction() as conn:
        shop.rows.update_attachment(conn, plain.id, caption=UNSET, role=role)
    kept = shop.lifecycle.edit_attachment(
        order.id, plain.id, AttachmentPatch(role=role, caption="old"), BY
    )
    assert (kept.role, kept.caption) == (role, "old")


@pytest.mark.requires_postgres
def test_an_unsent_revisions_file_is_removed(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    [rev] = shop.rows.revisions(item.id)
    att = attach_to(shop, order, item, rev)
    shop.lifecycle.remove_attachment(order.id, att.id, BY)
    assert shop.rows.attachments(order.id) == []


@pytest.mark.requires_postgres
def test_a_revisions_file_is_not_the_delivered_picture(shop: Shop) -> None:
    order = shop.order()
    item = shop.item(order)
    [rev] = shop.rows.revisions(item.id)
    att = attach_to(shop, order, item, rev)
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.set_delivered(order.id, item.id, att.id, BY)
    assert (refused.value.slug, refused.value.unprocessable) == ("attachment_id", True)
    assert shop.get_item(item).delivered_attachment_id is None
