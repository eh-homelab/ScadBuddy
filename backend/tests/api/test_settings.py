from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.settings import parse_printers
from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app

PRINTERS_URL = "https://bambuddy.test/api/v1/printers"


def test_defaults_are_empty_and_the_key_is_absent(client: TestClient) -> None:
    assert client.get("/api/v1/settings").json() == {
        "bambuddy_url": None,
        "has_api_key": False,
        "library_folder_id": None,
        "pipeline_id": None,
        "printer_id": None,
        "public_url": None,
    }


def test_the_api_key_is_write_only(client: TestClient, data_dir: Path) -> None:
    response = client.put(
        "/api/v1/settings",
        json={
            "bambuddy_url": "https://bambuddy.test",
            "bambuddy_api_key": "s3cret",
            "library_folder_id": "7",
            "printer_id": "p1",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["has_api_key"] is True
    assert body["bambuddy_url"] == "https://bambuddy.test"
    assert "bambuddy_api_key" not in body
    assert "s3cret" not in response.text

    stored = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
    assert stored["bambuddy_api_key"] == "s3cret"


def test_the_settings_file_is_not_world_readable(client: TestClient, data_dir: Path) -> None:
    client.put("/api/v1/settings", json={"bambuddy_api_key": "s3cret"})
    mode = (data_dir / "settings.json").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_an_omitted_key_is_kept_and_an_empty_one_clears_it(client: TestClient) -> None:
    client.put("/api/v1/settings", json={"bambuddy_api_key": "s3cret"})
    assert client.put("/api/v1/settings", json={"pipeline_id": "3"}).json()["has_api_key"] is True
    assert (
        client.put("/api/v1/settings", json={"bambuddy_api_key": ""}).json()["has_api_key"] is False
    )


def test_the_environment_seeds_the_settings_and_the_file_then_wins(
    data_dir: Path, seed_dir: Path, fake_openscad: str
) -> None:
    settings = Settings(
        openscad=fake_openscad,
        data_dir=data_dir,
        seed_models_dir=seed_dir,
        frontend_dir=Path("/nonexistent"),
        bambuddy_url="https://from-the-environment.test",
        bambuddy_api_key="env-key",
    )
    with TestClient(create_app(settings)) as client:
        seeded = client.get("/api/v1/settings").json()
        assert seeded["bambuddy_url"] == "https://from-the-environment.test"
        assert seeded["has_api_key"] is True

        client.put("/api/v1/settings", json={"bambuddy_url": "https://edited-in-the-ui.test"})
        assert client.get("/api/v1/settings").json()["bambuddy_url"] == (
            "https://edited-in-the-ui.test"
        )


@respx.mock
def test_the_connection_test_reports_the_printers(client: TestClient) -> None:
    client.put(
        "/api/v1/settings",
        json={"bambuddy_url": "https://bambuddy.test/", "bambuddy_api_key": "s3cret"},
    )
    route = respx.get(PRINTERS_URL).mock(
        return_value=httpx.Response(
            200, json=[{"id": 1, "name": "X1C", "model": "X1 Carbon", "extra": "ignored"}]
        )
    )

    body = client.post("/api/v1/settings/test").json()
    assert body == {
        "ok": True,
        "printers": [{"id": "1", "name": "X1C", "model": "X1 Carbon"}],
        "error": None,
    }
    assert route.calls.last.request.headers["X-API-Key"] == "s3cret"


@respx.mock
def test_a_rejected_key_is_reported_not_raised(client: TestClient) -> None:
    client.put(
        "/api/v1/settings",
        json={"bambuddy_url": "https://bambuddy.test", "bambuddy_api_key": "wrong"},
    )
    respx.get(PRINTERS_URL).mock(return_value=httpx.Response(401, json={"detail": "nope"}))

    body = client.post("/api/v1/settings/test").json()
    assert body["ok"] is False
    assert body["error"] == "Bambuddy answered 401"
    assert body["printers"] == []


@respx.mock
def test_an_unreachable_bambuddy_is_reported_not_raised(client: TestClient) -> None:
    client.put("/api/v1/settings", json={"bambuddy_url": "https://bambuddy.test"})
    respx.get(PRINTERS_URL).mock(side_effect=httpx.ConnectError("no route to host"))

    body = client.post("/api/v1/settings/test").json()
    assert body["ok"] is False
    assert "ConnectError" in (body["error"] or "")


def test_testing_without_a_url_configured_is_a_conflict(client: TestClient) -> None:
    response = client.post("/api/v1/settings/test")
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.parametrize(
    "payload",
    [
        [{"id": "a", "name": "n", "model": "m"}],
        {"items": [{"id": "a", "name": "n", "model": "m"}]},
        {"printers": [{"id": "a", "name": "n", "model": "m"}]},
    ],
)
def test_printers_are_read_out_of_either_shape(payload: object) -> None:
    assert [printer.id for printer in parse_printers(payload)] == ["a"]


def test_an_unexpected_printers_payload_yields_nothing() -> None:
    assert parse_printers({"unexpected": True}) == []
    assert parse_printers("not json at all") == []
