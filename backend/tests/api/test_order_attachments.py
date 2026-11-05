"""An order's files (#4251, spec 2026-10-18 §6.2): any file on an order or a line item,
typed by its bytes, kept as a blob held by `blob_refs` while it stands, served inline
only when a browser shows it and never runs it, and an item's image marked as what was
delivered."""

from __future__ import annotations

import hashlib
import io
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from scadbuddy.orders import attachments as attachments_module
from scadbuddy.orders.component import ORDERS
from scadbuddy.orders.revisions import RevisionAnswerInput
from scadbuddy.orders.store import ATTACHMENT_HOLDER
from scadbuddy.store.cache import StaleBlobError
from tests.api.test_customers import create as create_customer
from tests.api.test_orders import AGENT, create_order
from tests.api.test_orders_lifecycle import add_item, quote_and_accept
from tests.api.test_settings_runtime import _state
from tests.api.test_versions import SLUG, upload
from tests.orders.test_lifecycle import BY
from tests.support.operations import press

pytestmark = pytest.mark.requires_git

PDF = b"%PDF-1.7\n" + b"0" * 64
PAGE = b"<html><script>alert(1)</script></html>"
PROBLEMS = "https://scadbuddy.dev/problems/orders/"


def png(size: tuple[int, int] = (1200, 900)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


def attach(
    client: TestClient,
    order_id: str,
    data: bytes,
    filename: str,
    *,
    item_id: str | None = None,
    fulfilment_id: str | None = None,
    role: str | None = None,
    caption: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    path = (
        f"/api/v1/orders/{order_id}"
        + (f"/items/{item_id}" if item_id else "")
        + (f"/fulfilments/{fulfilment_id}" if fulfilment_id else "")
        + "/attachments"
    )
    form = {
        key: value for key, value in (("role", role), ("caption", caption)) if value is not None
    }
    return client.post(
        path,
        files={"file": (filename, data, "application/x-whatever")},
        data=form,
        headers={**press(), **(headers or {})},
    )


def shop(client: TestClient) -> tuple[dict[str, Any], dict[str, Any]]:
    upload(client)
    order = create_order(client, create_customer(client)["id"])
    commit = client.get(f"/api/v1/models/{SLUG}").json()["version"]
    item = add_item(
        client,
        order["id"],
        slug=SLUG,
        commit=commit,
        inputs={"params": {"width": 12}},
        quantity=1,
    )
    return order, item


def holders(client: TestClient) -> dict[str, str]:
    refs = _state(client).refs
    with refs.pool.connection() as conn:
        rows = conn.execute(
            "SELECT holder_id, key FROM blob_refs WHERE holder_kind = %s", (ATTACHMENT_HOLDER,)
        ).fetchall()
    return {row["holder_id"]: row["key"] for row in rows}


def test_any_file_is_kept_typed_by_its_bytes(client: TestClient) -> None:
    order, item = shop(client)
    photo = png()
    added = attach(
        client,
        order["id"],
        photo,
        "C:\\phone\\IMG_1.png",
        item_id=item["id"],
        role="issue",
        caption="stringing on the left wall",
        headers=AGENT,
    )
    assert added.status_code == 200, added.text
    body = added.json()
    assert (body["kind"], body["media_type"], body["role"]) == ("image", "image/png", "issue")
    assert (body["filename"], body["item_id"], body["subject"]) == (
        "IMG_1.png",
        item["id"],
        f"order_item:{item['id']}",
    )
    assert body["caption"] == "stringing on the left wall"
    assert (body["size_bytes"], body["sha256"]) == (len(photo), hashlib.sha256(photo).hexdigest())
    assert body["created_by"] == {"kind": "agent", "principal": "token:t1", "session": "s1"}

    pdf = attach(client, order["id"], PDF, "invoice.pdf", role="reference").json()
    assert (pdf["kind"], pdf["media_type"], pdf["item_id"]) == ("document", "application/pdf", None)
    page = attach(client, order["id"], PAGE, "notes.html").json()
    assert (page["kind"], page["media_type"], page["role"]) == ("other", "text/html", "other")
    # A name claiming a photo does not make one.
    fake = attach(client, order["id"], b"not an image at all", "photo.png").json()
    assert (fake["kind"], fake["media_type"]) == ("other", "image/png")

    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert [a["id"] for a in detail["attachments"]] == [
        body["id"],
        pdf["id"],
        page["id"],
        fake["id"],
    ]
    listed = client.get(f"/api/v1/orders/{order['id']}/attachments").json()["items"]
    assert [a["id"] for a in listed] == [a["id"] for a in detail["attachments"]]
    kinds = [e["kind"] for e in client.get(f"/api/v1/orders/{order['id']}/events").json()]
    assert kinds.count("attachment.added") == 4
    # Each attachment holds its file.
    assert holders(client) == {a["id"]: f"attachment-{a['sha256']}" for a in detail["attachments"]}


def test_only_what_a_browser_shows_is_served_as_itself(client: TestClient) -> None:
    order, item = shop(client)
    base = f"/api/v1/orders/{order['id']}/attachments"
    photo = png()
    shown = attach(client, order["id"], photo, "print.png", item_id=item["id"]).json()
    page = attach(client, order["id"], PAGE, "notes.html").json()
    pdf = attach(client, order["id"], PDF, "invoice.pdf").json()

    served = client.get(f"{base}/{shown['id']}/content")
    assert served.status_code == 200
    assert served.content == photo
    assert served.headers["content-type"] == "image/png"
    assert served.headers["content-disposition"].startswith("inline")
    assert served.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in served.headers["content-security-policy"]
    assert "immutable" in served.headers["cache-control"]
    ranged = client.get(f"{base}/{shown['id']}/content", headers={"Range": "bytes=0-7"})
    assert ranged.status_code == 206
    assert ranged.content == photo[:8]
    download = client.get(f"{base}/{shown['id']}/content", params={"download": "true"})
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.headers["content-type"] == "application/octet-stream"

    html = client.get(f"{base}/{page['id']}/content")
    assert html.content == PAGE
    assert html.headers["content-type"] == "application/octet-stream"
    assert html.headers["content-disposition"] == 'attachment; filename="notes.html"'
    assert "sandbox" in html.headers["content-security-policy"]

    document = client.get(f"{base}/{pdf['id']}/content")
    assert document.headers["content-type"] == "application/pdf"
    assert document.headers["content-disposition"].startswith("inline")
    # A browser's PDF viewer does not run under a sandboxing policy.
    assert "content-security-policy" not in document.headers

    thumbnail = client.get(f"{base}/{shown['id']}/thumbnail")
    assert thumbnail.status_code == 200, thumbnail.text
    assert thumbnail.headers["content-type"] == "image/webp"
    with Image.open(io.BytesIO(thumbnail.content)) as small:
        assert max(small.size) <= 800
    assert client.get(f"{base}/{pdf['id']}/thumbnail").status_code == 404

    # Another order's attachment is not this one's.
    other = create_order(client, create_customer(client)["id"])
    assert (
        client.get(f"/api/v1/orders/{other['id']}/attachments/{shown['id']}/content").status_code
        == 404
    )


def test_an_items_image_is_marked_as_delivered(client: TestClient) -> None:
    order, item = shop(client)
    other = add_item(
        client,
        order["id"],
        slug=SLUG,
        commit=item["reference"]["commit"],
        inputs={"params": {"width": 14}},
        quantity=1,
    )
    delivered = f"/api/v1/orders/{order['id']}/items/{item['id']}/delivered"
    photo = attach(
        client, order["id"], png(), "final.png", item_id=item["id"], role="finished"
    ).json()
    pdf = attach(client, order["id"], PDF, "spec.pdf", item_id=item["id"]).json()
    on_order = attach(client, order["id"], png((10, 10)), "box.png", role="packed").json()
    elsewhere = attach(client, order["id"], png((20, 20)), "x.png", item_id=other["id"]).json()

    # An iPhone's photo is an image, but no browser draws it, so it cannot be the picture.
    heic = b"\0\0\0\x18ftypheic\0\0\0\0" + b"\0" * 32
    phone = attach(client, order["id"], heic, "IMG_0001.HEIC", item_id=item["id"]).json()
    assert (phone["kind"], phone["media_type"]) == ("image", "image/heic")
    for wrong in (pdf["id"], on_order["id"], elsewhere["id"], phone["id"], "nothing"):
        refused = client.put(delivered, json={"attachment_id": wrong})
        assert refused.status_code == 422, wrong
        assert refused.json()["errors"][0]["loc"] == ["body", "attachment_id"]

    marked = client.put(delivered, json={"attachment_id": photo["id"]})
    assert marked.status_code == 200, marked.text
    assert marked.json()["delivered_attachment_id"] == photo["id"]
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert detail["items"][0]["delivered_attachment_id"] == photo["id"]

    cleared = client.put(delivered, json={"attachment_id": None})
    assert cleared.json()["delivered_attachment_id"] is None
    client.put(delivered, json={"attachment_id": photo["id"]})

    # Removing the marked image clears the mark, on the audit line.
    gone = client.delete(f"/api/v1/orders/{order['id']}/attachments/{photo['id']}")
    assert gone.status_code == 204
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert detail["items"][0]["delivered_attachment_id"] is None
    assert photo["id"] not in [a["id"] for a in detail["attachments"]]
    events = client.get(f"/api/v1/orders/{order['id']}/events").json()
    removed = next(e for e in events if e["kind"] == "attachment.removed")
    assert removed["detail"]["was_delivered"] is True
    assert removed["detail"]["filename"] == "final.png"
    assert [e["detail"]["attachment_id"] for e in events if e["kind"] == "item.delivered"] == [
        photo["id"],
        None,
        photo["id"],
    ]
    assert (
        client.get(f"/api/v1/orders/{order['id']}/attachments/{photo['id']}/content").status_code
        == 404
    )


def test_identical_bytes_share_a_blob_each_holding_it(client: TestClient) -> None:
    order, item = shop(client)
    photo = png((64, 64))
    first = attach(client, order["id"], photo, "a.png").json()
    second = attach(client, order["id"], photo, "b.png", item_id=item["id"]).json()
    key = f"attachment-{hashlib.sha256(photo).hexdigest()}"
    assert holders(client) == {first["id"]: key, second["id"]: key}
    assert (
        client.delete(f"/api/v1/orders/{order['id']}/attachments/{first['id']}").status_code == 204
    )
    assert holders(client) == {second["id"]: key}
    served = client.get(f"/api/v1/orders/{order['id']}/attachments/{second['id']}/content")
    assert served.content == photo


def test_caption_and_role_are_edited_and_refusals_are_answered(client: TestClient) -> None:
    order, _ = shop(client)
    added = attach(client, order["id"], PDF, "drawing.pdf").json()
    base = f"/api/v1/orders/{order['id']}/attachments/{added['id']}"
    patched = client.patch(base, json={"caption": "the customer's drawing", "role": "reference"})
    assert patched.status_code == 200, patched.text
    assert (patched.json()["caption"], patched.json()["role"]) == (
        "the customer's drawing",
        "reference",
    )
    assert client.patch(base, json={"role": "nonsense"}).status_code == 422
    assert client.patch(base, json={"filename": "x"}).status_code == 422
    # The same caption again is no change, so it is not on the audit line twice.
    assert client.patch(base, json={"caption": "the customer's drawing"}).status_code == 200
    events = client.get(f"/api/v1/orders/{order['id']}/events").json()
    role = next(e for e in events if e["kind"] == "attachment.role")
    assert role["detail"] == {"attachment_id": added["id"], "from_role": "other", "to": "reference"}
    # #4676: a caption edit is on the audit line too.
    assert [e["detail"] for e in events if e["kind"] == "attachment.caption"] == [
        {"attachment_id": added["id"], "from_caption": "", "to": "the customer's drawing"}
    ]

    assert attach(client, order["id"], PDF, "x.pdf", role="bogus").status_code == 422
    # A render and a test print are a design revision's files, never the order's.
    for role in ("render", "test_print"):
        assert attach(client, order["id"], PDF, "x.pdf", role=role).status_code == 422
        assert client.patch(base, json={"role": role}).status_code == 422
    assert attach(client, "no-such-order", PDF, "x.pdf").status_code == 404
    assert attach(client, order["id"], PDF, "x.pdf", item_id="no-such-item").status_code == 404
    empty = client.post(
        f"/api/v1/orders/{order['id']}/attachments",
        files={"file": ("x.bin", b"", "application/octet-stream")},
        headers=press(),
    )
    assert empty.status_code == 422
    missing = client.delete(f"/api/v1/orders/{order['id']}/attachments/nothing")
    assert missing.status_code == 404
    assert missing.json()["type"] == PROBLEMS + "attachment_not_found"

    # A closed or cancelled order still takes a file: a record, never a step.
    assert client.post(f"/api/v1/orders/{order['id']}/cancel").status_code == 200
    assert attach(client, order["id"], PDF, "after.pdf").status_code == 200


def test_deleting_a_draft_item_removes_its_files_and_their_holds(client: TestClient) -> None:
    order, item = shop(client)
    on_order = attach(client, order["id"], PDF, "brief.pdf").json()
    photo = attach(client, order["id"], png((32, 32)), "mock.png", item_id=item["id"]).json()
    sketch = attach(client, order["id"], PDF + b"x", "sketch.pdf", item_id=item["id"]).json()
    commit = client.get(f"/api/v1/models/{SLUG}").json()["version"]
    other = add_item(
        client, order["id"], slug=SLUG, commit=commit, inputs={"params": {"width": 9}}, quantity=1
    )
    kept = attach(client, order["id"], PDF + b"y", "other.pdf", item_id=other["id"]).json()
    marked = client.put(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/delivered",
        json={"attachment_id": photo["id"]},
    )
    assert marked.status_code == 200, marked.text

    assert client.delete(f"/api/v1/orders/{order['id']}/items/{item['id']}").status_code == 204
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    # `Lifecycle.delete_item` takes the item's files by subject (`subject_attachments`,
    # #4724): the order's own file and the other item's stay, with their holds.
    assert [a["id"] for a in detail["attachments"]] == [on_order["id"], kept["id"]]
    assert holders(client) == {
        on_order["id"]: f"attachment-{on_order['sha256']}",
        kept["id"]: f"attachment-{kept['sha256']}",
    }
    removed = [
        e["detail"]
        for e in client.get(f"/api/v1/orders/{order['id']}/events").json()
        if e["kind"] == "attachment.removed"
    ]
    assert [(e["attachment_id"], e["was_delivered"]) for e in removed] == [
        (photo["id"], True),
        (sketch["id"], False),
    ]


def test_an_item_past_draft_keeps_its_files_when_its_delete_is_refused(
    client: TestClient,
) -> None:
    order, item = shop(client)
    kept = attach(client, order["id"], PDF, "spec.pdf", item_id=item["id"]).json()
    quoted = client.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/quotes",
        json={"unit_price_minor": 100, "label": "typed"},
    )
    assert quoted.status_code == 201, quoted.text
    refused = client.delete(f"/api/v1/orders/{order['id']}/items/{item['id']}")
    assert refused.status_code == 409
    assert refused.json()["type"] == PROBLEMS + "item_not_draft"
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    assert [a["id"] for a in detail["attachments"]] == [kept["id"]]
    assert holders(client) == {kept["id"]: f"attachment-{kept['sha256']}"}


def test_an_upload_while_the_store_moves_is_told_to_wait(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid `/store/migrate`, bytes stored on the other backend lose the publish's swap
    (`CachedBlobStore.claim`): the answer says to send them again, not a bare failure."""
    order, _ = shop(client)

    async def moving(*_: object) -> str:
        raise StaleBlobError("piece attachment-x was published by a later attempt")

    monkeypatch.setattr(attachments_module, "keep", moving)
    answer = attach(client, order["id"], PDF, "brief.pdf")
    assert answer.status_code == 503, answer.text
    assert "not on this store's backend" in answer.json()["detail"]


def test_a_file_on_an_attempt(client: TestClient) -> None:
    """#4251: `POST …/items/{item}/fulfilments/{f}/attachments`, listed under its item."""
    order, item = shop(client)
    quote_and_accept(client, order["id"], item["id"])
    assert client.post(f"/api/v1/orders/{order['id']}/accept").status_code == 200
    attempt = client.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/fulfilments",
        json={"quantity": 1, "outcome": "succeeded"},
    ).json()

    added = attach(
        client, order["id"], png(), "done.png", item_id=item["id"], fulfilment_id=attempt["id"]
    )

    assert added.status_code == 200, added.text
    body = added.json()
    assert body["subject"] == f"order_item_fulfilment:{attempt['id']}"
    assert (body["item_id"], body["fulfilment_id"]) == (item["id"], attempt["id"])
    listed = client.get(f"/api/v1/orders/{order['id']}").json()["attachments"]
    assert [a["fulfilment_id"] for a in listed] == [attempt["id"]]
    missing = attach(
        client, order["id"], png(), "done.png", item_id=item["id"], fulfilment_id="nope"
    )
    assert missing.status_code == 404, missing.text


def test_a_file_on_a_revision(client: TestClient) -> None:
    """`POST …/items/{item}/revisions/{r}/attachments`, listed under its item."""
    order, item = shop(client)
    revision = client.get(f"/api/v1/orders/{order['id']}").json()["items"][0]["current_revision_id"]
    path = f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions/{revision}/attachments"

    answer = client.post(
        path,
        files={"file": ("t.png", png(), "image/png")},
        data={"role": "test_print"},
        headers=press(),
    )

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["revision_id"] == revision and body["role"] == "test_print"
    assert body["subject"] == f"order_item_revision:{revision}" and body["item_id"] == item["id"]
    missing = client.post(
        path.replace(revision, "nope"),
        files={"file": ("t.png", png(), "image/png")},
        headers=press(),
    )
    assert missing.status_code == 404, missing.text


def _revision_upload(
    client: TestClient, order: dict[str, Any], item: dict[str, Any], **fields: str
) -> Any:
    detail = client.get(f"/api/v1/orders/{order['id']}").json()
    revision = detail["items"][0]["current_revision_id"]
    return client.post(
        f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions/{revision}/attachments",
        files={"file": ("t.png", png(), "image/png")},
        data={"role": "test_print", **fields},
        headers=press(),
    )


def test_a_revision_file_names_its_test_attempt(client: TestClient) -> None:
    order, item = shop(client)
    store = _state(client).components.get(ORDERS).store
    revision = store.revisions(item["id"])[0]
    with store.transaction() as conn:
        test = store.insert_fulfilment(
            conn,
            item["id"],
            quantity=0,
            outcome="unknown",
            created_by=BY,
            purpose="test",
            revision_id=revision.id,
        )
        production = store.insert_fulfilment(
            conn, item["id"], quantity=1, outcome="succeeded", created_by=BY
        )

    named = _revision_upload(client, order, item, fulfilment_id=test.id)
    assert named.status_code == 200, named.text
    assert named.json()["fulfilment_id"] == test.id
    assert named.json()["revision_id"] == revision.id

    assert _revision_upload(client, order, item, fulfilment_id="nope").status_code == 404
    assert _revision_upload(client, order, item, fulfilment_id=production.id).status_code == 404

    blank = _revision_upload(client, order, item, fulfilment_id="  ")
    assert blank.status_code == 200, blank.text
    assert blank.json()["fulfilment_id"] is None


def test_the_revision_route_declares_its_fulfilment_field(client: TestClient) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert "fulfilment_id" in schemas["RevisionAttachmentUpload"]["properties"]
    assert "fulfilment_id" not in schemas["AttachmentUpload"]["properties"]


def _revision_photo(
    client: TestClient, order: dict[str, Any], item: dict[str, Any]
) -> tuple[str, str]:
    """A test-print photo on the item's first revision, which is sent (it has a render)."""
    revision = _state(client).components.get(ORDERS).store.revisions(item["id"])[0]
    base = f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions/{revision.id}/attachments"
    render = client.post(
        base,
        files={"file": ("r.png", png(), "image/png")},
        data={"role": "render"},
        headers=press(),
    )
    assert render.status_code == 200, render.text
    photo = client.post(
        base,
        files={"file": ("t.png", png((300, 200)), "image/png")},
        data={"role": "test_print", "caption": "first try"},
        headers=press(),
    )
    assert photo.status_code == 200, photo.text
    return revision.id, photo.json()["id"]


def _answer(client: TestClient, order: dict[str, Any], item: dict[str, Any], rev: str) -> None:
    lifecycle = _state(client).components.get(ORDERS).lifecycle
    lifecycle.update_item(order["id"], item["id"], BY, approval="design_first")
    lifecycle.send_revision(order["id"], item["id"], rev, BY)
    lifecycle.answer_revision(
        order["id"], item["id"], rev, RevisionAnswerInput(answer="accepted"), BY
    )


def test_an_accepted_revisions_print_photo_joins_the_models_photos(client: TestClient) -> None:
    order, item = shop(client)
    rev, photo = _revision_photo(client, order, item)
    _answer(client, order, item, rev)
    commit = _state(client).components.get(ORDERS).store.revision(rev).reference.commit  # type: ignore[union-attr]
    path = f"/api/v1/orders/{order['id']}/attachments/{photo}/to-model"

    answer = client.post(path, headers={"Idempotency-Key": "k"})

    assert answer.status_code == 200, answer.text
    added = answer.json()["media"][-1]
    assert added["source_commit"] == commit and added["source_revision_id"] == rev
    assert added["caption"] == "first try" and added["kind"] == "image"
    content = client.get(f"/api/v1/orders/{order['id']}/attachments/{photo}/content")
    assert content.status_code == 200  # the order's copy stays


def test_a_second_add_to_model_returns_the_existing_copy(client: TestClient) -> None:
    """#5318: two clicks, each with a key of its own, give one copy in the gallery."""
    order, item = shop(client)
    rev, photo = _revision_photo(client, order, item)
    _answer(client, order, item, rev)
    path = f"/api/v1/orders/{order['id']}/attachments/{photo}/to-model"

    first = client.post(path, headers={"Idempotency-Key": "first"})
    second = client.post(path, headers={"Idempotency-Key": "second"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    copies = [m for m in second.json()["media"] if m["source_revision_id"] == rev]
    assert [m["id"] for m in copies] == [first.json()["media"][-1]["id"]]
    assert second.json()["media"] == first.json()["media"]


def test_another_photo_of_the_same_revision_is_a_new_copy(client: TestClient) -> None:
    order, item = shop(client)
    rev, photo = _revision_photo(client, order, item)
    _answer(client, order, item, rev)
    base = f"/api/v1/orders/{order['id']}/items/{item['id']}/revisions/{rev}/attachments"
    other = client.post(
        base,
        files={"file": ("u.png", png((120, 80)), "image/png")},
        data={"role": "test_print"},
        headers=press(),
    )
    assert other.status_code == 200, other.text

    for attachment in (photo, other.json()["id"]):
        added = client.post(
            f"/api/v1/orders/{order['id']}/attachments/{attachment}/to-model", headers=press()
        )
        assert added.status_code == 200, added.text

    copies = [m for m in added.json()["media"] if m["source_revision_id"] == rev]
    assert len(copies) == 2


def test_a_photo_of_a_revision_not_accepted_stays_on_the_order(client: TestClient) -> None:
    order, item = shop(client)
    _, photo = _revision_photo(client, order, item)
    path = f"/api/v1/orders/{order['id']}/attachments/{photo}/to-model"

    answer = client.post(path, headers=press())

    assert answer.status_code == 409 and answer.json()["type"].endswith("/revision_not_accepted")
    other = attach(client, order["id"], png(), "x.png", item_id=item["id"])
    refused = client.post(
        f"/api/v1/orders/{order['id']}/attachments/{other.json()['id']}/to-model", headers=press()
    )
    assert refused.status_code == 422, refused.text
    missing = client.post(
        f"/api/v1/orders/{order['id']}/attachments/nope/to-model", headers=press()
    )
    assert missing.status_code == 404


def _accepted_photo(client: TestClient, data: bytes | None = None) -> tuple[dict[str, Any], str]:
    order, item = shop(client)
    rev, photo = _revision_photo(client, order, item)
    _answer(client, order, item, rev)
    if data is not None:
        # Swap the stored photo's bytes for ones the route must refuse.
        store = _state(client).components.get(ORDERS).store
        found = store.attachment(photo)
        assert found is not None
        blob = _state(client).store.blobs.dir_for(found[1]) / attachments_module.BLOB_FILE
        blob.write_bytes(data)
    return order, photo


def test_a_deleted_template_keeps_the_photo_on_the_order(client: TestClient) -> None:
    order, photo = _accepted_photo(client)
    assert client.delete(f"/api/v1/models/{SLUG}", headers=press()).status_code in (200, 202, 204)

    answer = client.post(
        f"/api/v1/orders/{order['id']}/attachments/{photo}/to-model", headers=press()
    )

    assert answer.status_code == 409 and answer.json()["type"].endswith("/template_deleted")


def test_a_photo_the_model_cannot_take_is_refused(client: TestClient) -> None:
    order, photo = _accepted_photo(client, b"not an image at all, just text")
    path = f"/api/v1/orders/{order['id']}/attachments/{photo}/to-model"
    assert client.post(path, headers=press()).status_code == 422


def test_a_photo_past_the_image_cap_is_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order, photo = _accepted_photo(client)
    monkeypatch.setattr("scadbuddy.api.order_attachments.MAX_IMAGE_BYTES", 10)
    path = f"/api/v1/orders/{order['id']}/attachments/{photo}/to-model"
    assert client.post(path, headers=press()).status_code == 413


#: The head of an MP4: what `library/media.py` `sniff_kind` takes for a video.
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 64


def test_a_test_print_video_joins_the_models_photos_without_a_poster(
    client: TestClient,
) -> None:
    order, video = _accepted_photo(client, MP4)
    path = f"/api/v1/orders/{order['id']}/attachments/{video}/to-model"

    answer = client.post(path, headers=press())

    assert answer.status_code == 200, answer.text
    added = answer.json()["media"][-1]
    assert added["kind"] == "video" and added["poster"] is None


def test_a_video_past_the_media_upload_limit_is_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    order, video = _accepted_photo(client, MP4)
    state = _state(client)
    monkeypatch.setattr(
        state, "settings", state.settings.model_copy(update={"media_upload_max_bytes": 10})
    )
    path = f"/api/v1/orders/{order['id']}/attachments/{video}/to-model"

    answer = client.post(path, headers=press())

    assert answer.status_code == 413, answer.text
    assert "media_upload_max_bytes" in answer.json()["detail"]
