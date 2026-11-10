"""The bot check's CRUD routes and settings (#5283, spec 2026-11-02 §5.8): the site key is
public, the secret is sealed under the KEK and never read back, and only this side calls
Cloudflare's Siteverify, for the ``storefront`` credential only."""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from scadbuddy.orders.storefront.turnstile import SITEVERIFY_URL
from scadbuddy.storefront.wire import CLIENT_HEADER, VISITOR_HEADER
from tests.api.test_crud_storefront_pay import token

SITE = "/api/v1/internal/crud/storefront/turnstile"
VERIFY = f"{SITE}/verify"
SECRET = "0x4AAAAAAA-turnstile-s3cret"
SITE_KEY = "0x4AAAAAAA-site-key"
HEADERS = {VISITOR_HEADER: "v" * 43, CLIENT_HEADER: "203.0.113.9"}


@pytest.fixture
def keyed_client(settings: Settings, tmp_path: Path) -> Iterator[TestClient]:
    kek = tmp_path / "kek"
    kek.write_text(base64.b64encode(os.urandom(32)).decode())
    keyed = settings.model_copy(
        update={"secret_key_file": kek, "storefront_public_url": "https://shop.example.com"}
    )
    with TestClient(create_app(keyed)) as client:
        yield client


def check(client: TestClient, body: dict[str, Any]) -> Any:
    answer = client.post(VERIFY, json=body, headers={**token(client), **HEADERS})
    assert answer.status_code == 200, answer.text
    return answer.json()["verdict"]


def test_the_check_is_off_by_default(client: TestClient) -> None:
    body = client.get("/api/v1/settings").json()
    assert body["storefront_turnstile_site_key"] is None
    assert body["has_storefront_turnstile_secret"] is False
    assert client.get(SITE, headers=token(client)).json() == {"site_key": None}
    with respx.mock(assert_all_called=False) as cloudflare:
        assert check(client, {"token": None, "action": "submit"}) == "off"
        assert not cloudflare.calls


def test_a_secret_is_refused_without_the_kek(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"storefront_turnstile_secret": SECRET})
    assert response.status_code == 422
    assert "SCADBUDDY_SECRET_KEY_FILE" in response.text
    assert SECRET not in response.text


def test_a_site_key_with_no_secret_fails_closed(client: TestClient) -> None:
    client.put("/api/v1/settings", json={"storefront_turnstile_site_key": SITE_KEY})
    assert client.get(SITE, headers=token(client)).json() == {"site_key": SITE_KEY}
    assert check(client, {"token": "t", "action": "submit"}) == "unavailable"


def test_the_secret_is_sealed_never_read_back_and_sent_only_to_cloudflare(
    keyed_client: TestClient, settings: Settings
) -> None:
    saved = keyed_client.put(
        "/api/v1/settings",
        json={"storefront_turnstile_site_key": SITE_KEY, "storefront_turnstile_secret": SECRET},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["has_storefront_turnstile_secret"] is True
    assert SECRET not in saved.text
    assert SECRET not in keyed_client.get("/api/v1/settings").text
    assert SECRET not in keyed_client.get(SITE, headers=token(keyed_client)).text
    with psycopg.connect(settings.database_url) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE name = 'storefront_turnstile_secret'"
        ).fetchone()
    assert row is not None and "sealed" in row[0] and SECRET not in str(row[0])

    with respx.mock(assert_all_called=False) as cloudflare:
        route = cloudflare.post(SITEVERIFY_URL).respond(
            200,
            json={"success": True, "hostname": "shop.example.com", "action": "request"},
        )
        assert check(keyed_client, {"token": "tok", "action": "request"}) == "pass"
        sent = route.calls.last.request.content.decode()
        assert f"secret={SECRET}" in sent and "remoteip=203.0.113.9" in sent
        # A token made for another form fails.
        assert check(keyed_client, {"token": "tok", "action": "sign_up"}) == "fail"

    cleared = keyed_client.put("/api/v1/settings", json={"storefront_turnstile_secret": ""})
    assert cleared.json()["has_storefront_turnstile_secret"] is False


def test_only_the_storefront_credential_reaches_the_check(client: TestClient) -> None:
    body = {"token": "t", "action": "submit"}
    assert client.get(SITE).status_code == 401
    assert client.post(VERIFY, json=body, headers=HEADERS).status_code == 401
    assert client.get(SITE, headers=token(client, "agent")).status_code == 403
    agent = {**token(client, "agent"), **HEADERS}
    assert client.post(VERIFY, json=body, headers=agent).status_code == 403


def test_a_check_takes_only_a_known_form_and_a_bounded_token(client: TestClient) -> None:
    sent = {**token(client), **HEADERS}
    for body in (
        {"token": "t", "action": "checkout"},
        {"token": "t" * 2049, "action": "submit"},
        {"token": "t", "action": "submit", "secret": "x"},
    ):
        assert client.post(VERIFY, json=body, headers=sent).status_code == 422
    # The verdict needs the visitor and the client's address, as every visitor route.
    assert client.post(VERIFY, json={"action": "submit"}, headers=token(client)).status_code == 400
