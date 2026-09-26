from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app

# The trailing slash is load-bearing: /api/v1/printers is a 404 on Bambuddy 1.2.5.5.
PRINTERS_URL = "https://bambuddy.test/api/v1/printers/"
PRINTERS_BODY = [{"id": 1, "name": "3DP-31B-598", "model": "H2C", "access_code": "xxxx"}]


def test_defaults_are_empty_and_the_key_is_absent(client: TestClient) -> None:
    assert client.get("/api/v1/settings").json() == {
        "bambuddy_url": None,
        "has_api_key": False,
        "public_url": None,
        "library_folder_id": None,
        "pipeline_id": None,
        "printer_id": None,
        "printer_preset": None,
        "process_preset": None,
        "filament_presets": [],
        "bed_type": None,
        "default_plate": None,
    }


def test_the_api_key_is_write_only(client: TestClient, data_dir: Path) -> None:
    response = client.put(
        "/api/v1/settings",
        json={
            "bambuddy_url": "https://bambuddy.test",
            "bambuddy_api_key": "s3cret",
            "library_folder_id": 7,
            "printer_id": 1,
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
    assert client.put("/api/v1/settings", json={"pipeline_id": 3}).json()["has_api_key"] is True
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

        # A clear is a stored answer too: the environment's key does not come back.
        client.put("/api/v1/settings", json={"public_url": None, "bambuddy_api_key": ""})
    with TestClient(create_app(settings)) as client:
        body = client.get("/api/v1/settings").json()
        assert body["has_api_key"] is False
        assert body["bambuddy_url"] == "https://edited-in-the-ui.test"


@respx.mock
def test_the_connection_test_reports_the_printers(client: TestClient) -> None:
    client.put(
        "/api/v1/settings",
        json={"bambuddy_url": "https://bambuddy.test/", "bambuddy_api_key": "s3cret"},
    )
    route = respx.get(PRINTERS_URL).mock(return_value=httpx.Response(200, json=PRINTERS_BODY))

    body = client.post("/api/v1/settings/test").json()
    assert body["ok"] is True
    assert "3DP-31B-598" in body["detail"]
    assert body["printers"] == [
        {"id": 1, "name": "3DP-31B-598", "model": "H2C", "is_active": True, "nozzle_count": None}
    ]
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
    # The scope, not a bare "401" — that is the whole point of the mapping.
    assert "Read Status" in body["detail"]
    assert body["printers"] == []


@respx.mock
def test_an_unreachable_bambuddy_is_reported_not_raised(client: TestClient) -> None:
    client.put("/api/v1/settings", json={"bambuddy_url": "https://bambuddy.test"})
    respx.get(PRINTERS_URL).mock(side_effect=httpx.ConnectError("no route to host"))

    body = client.post("/api/v1/settings/test").json()
    assert body["ok"] is False
    assert "ConnectError" in body["detail"]


def test_testing_without_a_url_configured_is_a_conflict(client: TestClient) -> None:
    response = client.post("/api/v1/settings/test")
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
