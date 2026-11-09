"""`/api/v1/storefront/mail/password` (#5283, spec 2026-11-02 §5.7): the SMTP password is
sealed under the KEK and reported only as set, as a payment provider's secret is."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

KEK_MISSING = "https://scadbuddy.dev/problems/storefront/kek_missing"


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    key = tmp_path / "kek"
    key.write_text(base64.b64encode(b"\x07" * 32).decode())
    return settings.model_copy(update={"secret_key_file": key})


def test_the_storefront_mail_password_is_write_only(client: TestClient) -> None:
    assert client.get("/api/v1/storefront/mail/password").json() == {"set": False}
    put = client.put("/api/v1/storefront/mail/password", json={"password": "hunter2"})
    assert put.status_code == 200 and put.json() == {"set": True}
    assert "hunter2" not in client.get("/api/v1/settings").text
    assert "hunter2" not in client.get("/api/v1/storefront/mail/password").text
    assert client.get("/api/v1/storefront/mail/password").json() == {"set": True}
    gone = client.delete("/api/v1/storefront/mail/password")
    assert gone.status_code == 200 and gone.json() == {"set": False}
    assert client.get("/api/v1/storefront/mail/password").json() == {"set": False}


def test_an_empty_storefront_mail_password_is_refused(client: TestClient) -> None:
    put = client.put("/api/v1/storefront/mail/password", json={"password": ""})
    assert put.status_code == 422


def test_the_password_needs_the_key_file(settings: Settings) -> None:
    app = create_app(settings.model_copy(update={"secret_key_file": None}))
    with TestClient(app) as client:
        put = client.put("/api/v1/storefront/mail/password", json={"password": "pw"})
    assert put.status_code == 409 and put.json()["type"] == KEK_MISSING
