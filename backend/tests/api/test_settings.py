from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from tests.api.conftest import read_stored

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
        "display_unit": "mm",
        "media_upload_max_bytes": 1024**3,
        "has_render_api_key": False,
        "render_key_fallback": False,
        "store_backend": "local",
    }


def test_the_api_key_is_write_only(client: TestClient, settings: Settings) -> None:
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

    # Stored as given, in plain text, as the old 0600 file held it: the backend has no
    # secret store, so it is in every backup of the database.
    stored = read_stored(settings.database_url)
    assert stored["bambuddy_api_key"] == "s3cret"


def test_settings_live_in_the_database_not_in_a_file(client: TestClient, data_dir: Path) -> None:
    client.put("/api/v1/settings", json={"bambuddy_api_key": "s3cret", "pipeline_id": 3})
    assert not (data_dir / "settings.json").exists()


def test_each_setting_is_its_own_row_and_an_omitted_one_writes_nothing(
    client: TestClient, settings: Settings
) -> None:
    client.put("/api/v1/settings", json={"pipeline_id": 3})
    assert read_stored(settings.database_url) == {
        "pipeline_id": 3,
        "model_print_choices": {},
        "printer_bed_types": {},
    }

    # A clear of a field the environment does not seed goes back to "never set".
    client.put("/api/v1/settings", json={"pipeline_id": None})
    assert "pipeline_id" not in read_stored(settings.database_url)


def test_an_omitted_key_is_kept_and_an_empty_one_clears_it(client: TestClient) -> None:
    client.put("/api/v1/settings", json={"bambuddy_api_key": "s3cret"})
    assert client.put("/api/v1/settings", json={"pipeline_id": 3}).json()["has_api_key"] is True
    assert (
        client.put("/api/v1/settings", json={"bambuddy_api_key": ""}).json()["has_api_key"] is False
    )


def test_the_display_unit_is_stored_and_a_clear_puts_millimetres_back(
    client: TestClient, settings: Settings
) -> None:
    assert (
        client.put("/api/v1/settings", json={"display_unit": "in"}).json()["display_unit"] == "in"
    )
    # Another field's save leaves it alone.
    assert client.put("/api/v1/settings", json={"pipeline_id": 3}).json()["display_unit"] == "in"
    assert read_stored(settings.database_url)["display_unit"] == "in"

    assert (
        client.put("/api/v1/settings", json={"display_unit": None}).json()["display_unit"] == "mm"
    )


def test_an_unknown_display_unit_is_refused(client: TestClient) -> None:
    assert client.put("/api/v1/settings", json={"display_unit": "cm"}).status_code == 422


def test_the_environment_seeds_the_settings_and_a_stored_value_then_wins(
    settings: Settings,
) -> None:
    settings = settings.model_copy(
        update={
            "bambuddy_url": "https://from-the-environment.test",
            "bambuddy_api_key": "env-key",
        }
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
    # The clear is a JSON null row; a field never stored has no row at all.
    stored = read_stored(settings.database_url)
    assert stored["bambuddy_api_key"] is None
    assert stored["public_url"] is None
    assert "default_plate" not in stored


def test_a_field_never_stored_follows_the_environment_it_starts_with(
    settings: Settings,
) -> None:
    """A variable added to a deployment later is honoured: nothing stored beats it."""
    with TestClient(create_app(settings)) as client:
        client.put("/api/v1/settings", json={"pipeline_id": 3})
    later = settings.model_copy(update={"public_url": "https://scad.example"})
    with TestClient(create_app(later)) as client:
        assert client.get("/api/v1/settings").json()["public_url"] == "https://scad.example"


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


def test_the_upload_limit_is_read_only(client: TestClient) -> None:
    """It comes from SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES alone: a PUT cannot store one."""
    saved = client.put("/api/v1/settings", json={"media_upload_max_bytes": 5 * 1024 * 1024})
    assert saved.status_code == 200, saved.text
    assert saved.json()["media_upload_max_bytes"] == 1024**3
    assert client.get("/api/v1/settings").json()["media_upload_max_bytes"] == 1024**3


def test_the_render_key_is_write_only_and_its_absence_is_flagged(client: TestClient) -> None:
    body = client.put(
        "/api/v1/settings",
        json={"bambuddy_url": "https://bambuddy.test", "bambuddy_api_key": "full"},
    ).json()
    assert body["render_key_fallback"] is True
    assert body["has_render_api_key"] is False
    response = client.put("/api/v1/settings", json={"bambuddy_render_api_key": "narrow"})
    body = response.json()
    assert body["has_render_api_key"] is True
    assert body["render_key_fallback"] is False
    assert "narrow" not in response.text
    assert "bambuddy_render_api_key" not in json.dumps(body)


def test_choosing_the_bambuddy_store_without_an_inbox_is_refused(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"store_backend": "bambuddy"})
    assert response.status_code == 422
    assert "library folder" in response.json()["detail"]
