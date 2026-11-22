"""Links on any entity (#6950, spec 6.3): `/entities/{kind}:{ref}/links`."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi.testclient import TestClient

from tests.api.test_settings_runtime import _state
from tests.support.operations import press

PROBLEMS = "https://scadbuddy.dev/problems/entities/"


def _printer(client: TestClient) -> str:
    with _state(client).refs.pool.connection() as conn:
        row = conn.execute(
            b"INSERT INTO filament_suppliers (name) VALUES (%s) RETURNING id",
            (f"S {uuid.uuid4()}",),
        ).fetchone()
    assert row is not None
    return f"supplier:{row['id']}"


def _code(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["type"] == PROBLEMS + code


def _add(client: TestClient, entity: str, url: str, **extra: Any) -> Any:
    return client.post(
        f"/api/v1/entities/{entity}/links", json={"url": url, **extra}, headers=press()
    )


def test_a_support_link_is_added_and_listed(client: TestClient) -> None:
    entity = _printer(client)
    added = _add(client, entity, "https://example.com/help", kind="support", title="Help")
    assert added.status_code == 200, added.text
    body = added.json()
    assert (body["kind"], body["title"], body["source"], body["entity"]) == (
        "support",
        "Help",
        "manual",
        entity,
    )
    listed = client.get(f"/api/v1/entities/{entity}/links").json()["links"]
    assert [x["id"] for x in listed] == [body["id"]]


def test_a_url_that_is_not_a_web_page_is_refused(client: TestClient) -> None:
    entity = _printer(client)
    for url in ("javascript:alert(1)", "ftp://x", "http://"):
        _code(_add(client, entity, url), 422, "bad_link_url")
    assert _add(client, entity, "https://x.io/" + "a" * 2000).status_code == 422


def test_a_patch_makes_the_link_manual_and_keeps_its_id(client: TestClient) -> None:
    entity = _printer(client)
    link = _add(client, entity, "https://example.com/a").json()
    with _state(client).refs.pool.connection() as conn:
        conn.execute(b"UPDATE entity_links SET source = 'bambu_store' WHERE id = %s", (link["id"],))
    path = f"/api/v1/entities/{entity}/links/{link['id']}"
    patched = client.patch(path, json={"title": "New"}, headers=press())
    assert patched.status_code == 200, patched.text
    assert (patched.json()["id"], patched.json()["title"]) == (link["id"], "New")
    assert patched.json()["source"] == "manual"
    _code(
        client.patch(path, json={"url": "ftp://x"}, headers=press()),
        422,
        "bad_link_url",
    )


def test_a_delete_removes_the_link(client: TestClient) -> None:
    entity = _printer(client)
    link = _add(client, entity, "https://example.com/a").json()
    path = f"/api/v1/entities/{entity}/links/{link['id']}"
    assert client.delete(path, headers=press()).status_code == 204
    assert client.get(f"/api/v1/entities/{entity}/links").json()["links"] == []
    _code(client.delete(path, headers=press()), 404, "link_not_found")


def test_another_entitys_path_does_not_reach_a_link(client: TestClient) -> None:
    one, other = _printer(client), _printer(client)
    link = _add(client, one, "https://example.com/a").json()
    _add(client, other, "https://example.com/b")
    path = f"/api/v1/entities/{other}/links/{link['id']}"
    _code(client.patch(path, json={"title": "x"}, headers=press()), 404, "link_not_found")
    _code(client.delete(path, headers=press()), 404, "link_not_found")
    _code(
        client.delete(f"/api/v1/entities/{other}/links/nope", headers=press()),
        404,
        "link_not_found",
    )


def test_a_missing_entity_and_a_print_archive(client: TestClient) -> None:
    _code(_add(client, f"spool:{uuid.uuid4()}", "https://example.com/a"), 404, "entity_not_found")
    _code(_add(client, "print_archive:7", "https://example.com/a"), 422, "unknown_entity_kind")
    _code(client.get("/api/v1/entities/nope/links"), 422, "bad_entity_ref")


def test_position_moves_the_others_down(client: TestClient) -> None:
    entity = _printer(client)
    ids = [_add(client, entity, f"https://example.com/{n}").json()["id"] for n in range(3)]
    path = f"/api/v1/entities/{entity}/links/{ids[2]}"
    assert client.patch(path, json={"position": 0}, headers=press()).status_code == 200
    listed = client.get(f"/api/v1/entities/{entity}/links").json()["links"]
    assert [x["id"] for x in listed] == [ids[2], ids[0], ids[1]]
