"""Issue #27 — POST /settings/register-sidebar, and the picker data behind it."""

from __future__ import annotations

import json
from typing import Any

import httpx
import respx
from fastapi.testclient import TestClient

from tests.bambuddy.conftest import recording

BASE = "https://bambuddy.test"
API = f"{BASE}/api/v1"
PUBLIC = "https://scadbuddy.internal.example"

LINK: dict[str, Any] = {
    "id": 3,
    "name": "Customize",
    "url": PUBLIC,
    "icon": "shapes",
    "open_in_new_tab": False,
    "sort_order": 0,
    "created_at": "2026-09-23T01:00:00Z",
    "updated_at": "2026-09-23T01:00:00Z",
}


def configure(client: TestClient, **extra: Any) -> None:
    body: dict[str, Any] = {
        "bambuddy_url": BASE,
        "bambuddy_api_key": "s3cret",
        "public_url": PUBLIC,
    }
    body.update(extra)
    assert client.put("/api/v1/settings", json=body).status_code == 200


@respx.mock
def test_registering_creates_the_link_when_there_is_none(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/external-links/").mock(
        return_value=httpx.Response(200, json=recording("external-links.json"))
    )
    create = respx.post(f"{API}/external-links/").mock(return_value=httpx.Response(200, json=LINK))

    body = client.post("/api/v1/settings/register-sidebar").json()

    assert body["created"] is True
    assert body["id"] == 3
    # Bambuddy renders an open_in_new_tab=false link in a sandboxed iframe here.
    assert body["embed_path"] == "/external/3"
    assert json.loads(create.calls.last.request.read()) == {
        "name": "Customize",
        "url": PUBLIC,
        "icon": "shapes",
        "open_in_new_tab": False,
    }


@respx.mock
def test_registering_again_patches_the_existing_link_by_name(client: TestClient) -> None:
    configure(client, public_url=f"{PUBLIC}/moved")
    respx.get(f"{API}/external-links/").mock(
        return_value=httpx.Response(200, json=[{**LINK, "url": PUBLIC}])
    )
    create = respx.post(f"{API}/external-links/")
    patch = respx.patch(f"{API}/external-links/3").mock(
        return_value=httpx.Response(200, json={**LINK, "url": f"{PUBLIC}/moved"})
    )

    body = client.post("/api/v1/settings/register-sidebar").json()

    assert body["created"] is False
    assert body["url"] == f"{PUBLIC}/moved"
    assert not create.called
    assert json.loads(patch.calls.last.request.read())["url"] == f"{PUBLIC}/moved"


@respx.mock
def test_an_unrelated_link_is_left_alone(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/external-links/").mock(
        return_value=httpx.Response(
            200, json=[{**LINK, "id": 1, "name": "Wiki", "url": "https://wiki.example"}]
        )
    )
    respx.post(f"{API}/external-links/").mock(return_value=httpx.Response(200, json=LINK))
    patch = respx.patch(f"{API}/external-links/1")

    assert client.post("/api/v1/settings/register-sidebar").json()["created"] is True
    assert not patch.called


@respx.mock
def test_registering_without_a_public_url_says_so(client: TestClient) -> None:
    configure(client, public_url="")

    response = client.post("/api/v1/settings/register-sidebar")

    assert response.status_code == 409
    assert "public" in response.json()["detail"]


def test_registering_without_a_bambuddy_url_is_a_conflict(client: TestClient) -> None:
    assert client.post("/api/v1/settings/register-sidebar").status_code == 409


@respx.mock
def test_a_refused_key_names_the_scope(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/external-links/").mock(
        return_value=httpx.Response(403, json={"detail": "forbidden"})
    )

    response = client.post("/api/v1/settings/register-sidebar")

    assert response.status_code == 409
    assert response.json()["required_scope"] == "Manage Library"


@respx.mock
def test_targets_feed_the_settings_pickers(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/library/folders").mock(
        return_value=httpx.Response(200, json=recording("library-folders.json"))
    )
    respx.get(f"{API}/slicer-pipelines/").mock(
        return_value=httpx.Response(200, json=recording("slicer-pipelines.json"))
    )
    respx.get(f"{API}/printers/").mock(
        return_value=httpx.Response(200, json=recording("printers.json"))
    )

    body = client.get("/api/v1/settings/targets").json()

    assert [folder["id"] for folder in body["folders"]] == [1, 2]
    assert body["pipelines"] == []
    assert body["printers"][0]["name"] == "3DP-31B-598"


def test_targets_without_a_url_configured_is_a_conflict(client: TestClient) -> None:
    assert client.get("/api/v1/settings/targets").status_code == 409
