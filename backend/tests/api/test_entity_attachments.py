"""Attachments on any entity (#6950, spec 6.2): `/entities/{kind}:{ref}/attachments`,
uploaded as an order's files are, served by the same rules, and read for an order's
entities and a print archive's."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from botocore.exceptions import ConnectTimeoutError
from fastapi.testclient import TestClient

from scadbuddy.api.attachment_serving import INERT
from scadbuddy.entities.component import ENTITIES
from scadbuddy.orders import attachments as attachments_module
from scadbuddy.orders.store import ATTACHMENT_HOLDER
from tests.api.test_order_attachments import PAGE, attach, png, shop
from tests.api.test_settings_runtime import _state
from tests.support.operations import press

PROBLEMS = "https://scadbuddy.dev/problems/entities/"


def _insert(client: TestClient, sql: str) -> str:
    with _state(client).refs.pool.connection() as conn:
        row = conn.execute(sql.encode()).fetchone()
    assert row is not None
    return str(row["id"])


def _supplier(client: TestClient) -> str:
    return "supplier:" + _insert(
        client, f"INSERT INTO filament_suppliers (name) VALUES ('S {uuid.uuid4()}') RETURNING id"
    )


def upload(
    client: TestClient,
    entity: str,
    data: bytes,
    filename: str,
    *,
    role: str | None = None,
    caption: str | None = None,
) -> Any:
    form = {
        key: value for key, value in (("role", role), ("caption", caption)) if value is not None
    }
    return client.post(
        f"/api/v1/entities/{entity}/attachments",
        files={"file": (filename, data, "application/x-whatever")},
        data=form,
        headers=press(),
    )


def _code(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["type"] == PROBLEMS + code


def test_a_photo_on_a_supplier_is_kept_and_served(client: TestClient) -> None:
    entity = _supplier(client)
    photo = png()
    added = upload(client, entity, photo, "shelf.png", role="photo", caption="the rack")
    assert added.status_code == 200, added.text
    body = added.json()
    assert (body["kind"], body["media_type"], body["role"], body["entity"]) == (
        "image",
        "image/png",
        "photo",
        entity,
    )
    assert (body["caption"], body["source"]) == ("the rack", "store")

    base = f"/api/v1/entities/{entity}/attachments"
    listed = client.get(base).json()["attachments"]
    assert [a["id"] for a in listed] == [body["id"]]

    served = client.get(f"{base}/{body['id']}/content")
    assert served.status_code == 200
    assert served.content == photo
    assert served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["content-security-policy"] == INERT["Content-Security-Policy"]
    assert "immutable" in served.headers["cache-control"]
    ranged = client.get(f"{base}/{body['id']}/content", headers={"Range": "bytes=0-7"})
    assert (ranged.status_code, ranged.content) == (206, photo[:8])
    thumbnail = client.get(f"{base}/{body['id']}/thumbnail")
    assert (thumbnail.status_code, thumbnail.headers["content-type"]) == (200, "image/webp")

    # Another supplier's path does not reach it.
    other = _supplier(client)
    _code(
        client.get(f"/api/v1/entities/{other}/attachments/{body['id']}/content"),
        404,
        "attachment_not_found",
    )


def test_a_page_is_only_ever_a_download(client: TestClient) -> None:
    entity = _supplier(client)
    page = upload(client, entity, PAGE, "notes.html").json()
    assert (page["kind"], page["role"]) == ("other", "other")
    served = client.get(f"/api/v1/entities/{entity}/attachments/{page['id']}/content")
    assert served.content == PAGE
    assert served.headers["content-type"] == "application/octet-stream"
    assert served.headers["content-disposition"] == 'attachment; filename="notes.html"'
    assert "sandbox" in served.headers["content-security-policy"]


def test_caption_role_and_delete(client: TestClient) -> None:
    entity = _supplier(client)
    added = upload(client, entity, png(), "a.png").json()
    base = f"/api/v1/entities/{entity}/attachments/{added['id']}"
    patched = client.patch(base, json={"role": "receipt", "caption": "paid"})
    assert patched.status_code == 200, patched.text
    assert (patched.json()["role"], patched.json()["caption"]) == ("receipt", "paid")
    _code(client.patch(base, json={"role": "Bad Role"}), 422, "bad_role")
    _code(upload(client, entity, png(), "b.png", role="Bad Role"), 422, "bad_role")

    other = _supplier(client)
    _code(
        client.delete(f"/api/v1/entities/{other}/attachments/{added['id']}"),
        404,
        "attachment_not_found",
    )
    assert client.delete(base).status_code == 204
    assert client.get(f"/api/v1/entities/{entity}/attachments").json()["attachments"] == []
    _code(client.delete(base), 404, "attachment_not_found")


@pytest.mark.requires_git
def test_order_entities_are_read_here_and_written_on_the_order_routes(
    client: TestClient,
) -> None:
    order, item = shop(client)
    added = attach(client, order["id"], png(), "print.png", item_id=item["id"]).json()

    _code(upload(client, f"order:{order['id']}", png(), "x.png"), 409, "use_order_route")
    listed = client.get(f"/api/v1/entities/order_item:{item['id']}/attachments")
    assert listed.status_code == 200, listed.text
    assert [(a["id"], a["entity"]) for a in listed.json()["attachments"]] == [
        (added["id"], f"order_item:{item['id']}")
    ]
    base = f"/api/v1/entities/order_item:{item['id']}/attachments/{added['id']}"
    assert client.get(f"{base}/content").status_code == 200
    _code(client.patch(base, json={"caption": "x"}), 409, "use_order_route")
    _code(client.delete(base), 409, "use_order_route")


@pytest.mark.requires_git
def test_an_order_route_does_not_reach_another_entitys_file(client: TestClient) -> None:
    order, item = shop(client)
    supplier = _supplier(client)
    added = upload(client, supplier, png(), "shelf.png").json()
    base = f"/api/v1/orders/{order['id']}/attachments/{added['id']}"
    assert client.get(f"{base}/content").status_code == 404
    assert client.get(f"{base}/thumbnail").status_code == 404
    assert client.patch(base, json={"caption": "x"}, headers=press()).status_code == 404
    assert client.delete(base, headers=press()).status_code == 404
    delivered = f"/api/v1/orders/{order['id']}/items/{item['id']}/delivered"
    refused = client.put(delivered, json={"attachment_id": added["id"]}, headers=press())
    assert refused.status_code == 422, refused.text
    # Still there, on its own entity.
    kept = client.get(f"/api/v1/entities/{supplier}/attachments/{added['id']}/content")
    assert kept.status_code == 200


def test_references_that_name_nothing(client: TestClient) -> None:
    missing = f"spool:{uuid.uuid4()}"
    _code(upload(client, missing, png(), "x.png"), 404, "entity_not_found")
    _code(client.get(f"/api/v1/entities/{missing}/attachments"), 404, "entity_not_found")
    _code(client.get("/api/v1/entities/banana:1/attachments"), 422, "unknown_entity_kind")
    _code(upload(client, "banana:1", png(), "x.png"), 422, "unknown_entity_kind")
    _code(client.get("/api/v1/entities/spool/attachments"), 422, "bad_entity_ref")
    _code(upload(client, "spool", png(), "x.png"), 422, "bad_entity_ref")
    # A print archive's files are Bambuddy's: read here, written on the print routes.
    _code(upload(client, "print_archive:7", png(), "x.png"), 422, "unknown_entity_kind")
    _code(client.get("/api/v1/entities/print_archive:x/attachments"), 422, "bad_entity_ref")


def test_a_print_archive_lists_its_print_attachments(client: TestClient) -> None:
    _insert(
        client,
        "INSERT INTO print_attachments (archive_id, kind, archive_photo, filename,"
        " size_bytes, caption) VALUES (41, 'photo', 'ab12cd34.jpg', 'done.jpg', 9, NULL)"
        " RETURNING id",
    )
    listed = client.get("/api/v1/entities/print_archive:41/attachments")
    assert listed.status_code == 200, listed.text
    [row] = listed.json()["attachments"]
    assert (row["entity"], row["source"], row["kind"], row["media_type"]) == (
        "print_archive:41",
        "bambuddy",
        "image",
        "image/jpeg",
    )
    assert (row["filename"], row["size_bytes"], row["sha256"], row["caption"]) == (
        "done.jpg",
        9,
        None,
        "",
    )
    assert client.get("/api/v1/entities/print_archive:42/attachments").json() == {"attachments": []}


def _rows_and_holds(client: TestClient, entity: str) -> tuple[list[str], list[str]]:
    with _state(client).refs.pool.connection() as conn:
        rows = conn.execute(
            "SELECT id FROM entity_attachments WHERE subject = %s", (entity,)
        ).fetchall()
        ids = [str(row["id"]) for row in rows]
        holds = conn.execute(
            "SELECT holder_id FROM blob_refs WHERE holder_kind = %s AND holder_id = ANY(%s)",
            (ATTACHMENT_HOLDER, ids),
        ).fetchall()
    return ids, [str(row["holder_id"]) for row in holds]


def test_a_connect_timeout_to_the_store_is_tried_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#8471: a connect timeout that outlasts the store client's own retries fails the
    run's first attempt; the next attempt adds the file, once."""
    entity = _supplier(client)
    keep = attachments_module.keep
    calls = 0

    async def flaky(*args: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectTimeoutError(endpoint_url="https://store.test/piece/x")
        return await keep(*args)

    monkeypatch.setattr(attachments_module, "keep", flaky)
    added = upload(client, entity, png(), "label.png", role="filament_label")
    assert added.status_code == 200, added.text
    assert calls == 2
    assert _rows_and_holds(client, entity) == ([added.json()["id"]], [added.json()["id"]])


def test_a_run_that_committed_then_failed_adds_one_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#8471: the row's id is the operation's, so the attempt after one that committed
    the row and then failed (its reply lost) finds that row instead of adding another."""
    entity = _supplier(client)
    store = _state(client).components.get(ENTITIES).attachments
    add = store.add
    calls = 0

    def committed_then_lost(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        added = add(*args, **kwargs)
        if calls == 1:
            raise RuntimeError("the worker stopped after the commit")
        return added

    monkeypatch.setattr(store, "add", committed_then_lost)
    added = upload(client, entity, png(), "label.png")
    assert added.status_code == 200, added.text
    assert calls == 2
    assert _rows_and_holds(client, entity) == ([added.json()["id"]], [added.json()["id"]])
    listed = client.get(f"/api/v1/entities/{entity}/attachments").json()["attachments"]
    assert [a["id"] for a in listed] == [added.json()["id"]]
