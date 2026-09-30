from __future__ import annotations

import json
from pathlib import Path

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from scadbuddy.core.settings import Settings
from scadbuddy.main import create_app
from tests.api.conftest import read_stored

# The trailing slash is load-bearing: /api/v1/printers is a 404 on Bambuddy 1.2.5.5.
PRINTERS_URL = "https://bambuddy.test/api/v1/printers/"
DEFAULTS: dict[str, object] = {
    "bambuddy_url": None,
    "has_api_key": False,
    "public_url": None,
    "library_folder_id": None,
    "printer_id": None,
    "default_plate": None,
    "display_unit": "mm",
    "media_upload_max_bytes": 1024**3,
    "last_project_id": None,
    "has_render_api_key": False,
    "render_key_fallback": False,
    "store_backend": "local",
}
PRINTERS_BODY = [{"id": 1, "name": "3DP-31B-598", "model": "H2C", "access_code": "xxxx"}]


def test_defaults_are_empty_and_the_key_is_absent(client: TestClient) -> None:
    body = client.get("/api/v1/settings").json()
    assert {name: body[name] for name in DEFAULTS} == DEFAULTS
    assert "bambuddy_api_key" not in body


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
    client.put("/api/v1/settings", json={"bambuddy_api_key": "s3cret", "printer_id": 3})
    assert not (data_dir / "settings.json").exists()


def test_each_setting_is_its_own_row_and_an_omitted_one_writes_nothing(
    client: TestClient, settings: Settings
) -> None:
    client.put("/api/v1/settings", json={"printer_id": 3})
    assert read_stored(settings.database_url) == {
        "printer_id": 3,
        "model_print_choices": {},
        "printer_bed_types": {},
    }

    # A clear of a field the environment does not seed goes back to "never set".
    client.put("/api/v1/settings", json={"printer_id": None})
    assert "printer_id" not in read_stored(settings.database_url)


@pytest.mark.requires_postgres
def test_settings_stored_before_312_still_load_and_shed_the_old_keys(
    client: TestClient, settings: Settings
) -> None:
    """The store is Postgres rows now, not ``settings.json``: seed the retired rows the
    way an older ScadBuddy wrote them, one per name."""
    legacy = {
        "bambuddy_url": "https://bambuddy.test",
        "pipeline_id": 4,
        "printer_id": 1,
        "printer_preset": {"source": "cloud", "id": "GM041"},
        "process_preset": {"source": "cloud", "id": "GP252"},
        "filament_presets": [{"source": "cloud", "id": "GFSA05_22"}],
        "bed_type": "Textured PEI Plate",
        "model_pipelines": {"demo": 9},
    }
    with psycopg.connect(settings.database_url) as conn:
        for name, value in legacy.items():
            conn.execute("INSERT INTO settings (name, value) VALUES (%s, %s)", (name, Jsonb(value)))

    body = client.get("/api/v1/settings").json()
    assert body["bambuddy_url"] == "https://bambuddy.test"
    assert body["printer_id"] == 1
    assert "pipeline_id" not in body
    assert "printer_preset" not in body

    assert client.put("/api/v1/settings", json={"public_url": "https://scad.test"}).is_success
    stored = read_stored(settings.database_url)
    for key in (
        "pipeline_id",
        "printer_preset",
        "process_preset",
        "filament_presets",
        "bed_type",
        "model_pipelines",
    ):
        assert key not in stored
    assert stored["printer_id"] == 1
    assert stored["bambuddy_url"] == "https://bambuddy.test"


def test_an_old_client_sending_a_pipeline_is_not_refused(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"pipeline_id": 3, "printer_id": 2})
    assert response.status_code == 200
    assert response.json()["printer_id"] == 2
    assert "pipeline_id" not in response.json()


def test_an_omitted_key_is_kept_and_an_empty_one_clears_it(client: TestClient) -> None:
    client.put("/api/v1/settings", json={"bambuddy_api_key": "s3cret"})
    assert client.put("/api/v1/settings", json={"printer_id": 3}).json()["has_api_key"] is True
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
    assert client.put("/api/v1/settings", json={"printer_id": 3}).json()["display_unit"] == "in"
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
        client.put("/api/v1/settings", json={"printer_id": 3})
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
    assert body["scopes"][0] == {
        "scope": "Read Status",
        "status": "ok",
        "required": True,
        "detail": "Printers, their status, and the print history.",
    }


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
    # Named in `sources` and `applies` like every env-seeded field, never as a value.
    assert "bambuddy_render_api_key" not in body
    assert "narrow" not in json.dumps(body)


def test_choosing_the_bambuddy_store_without_an_inbox_is_refused(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"store_backend": "bambuddy"})
    assert response.status_code == 422
    assert "library folder" in response.json()["detail"]


@pytest.mark.parametrize("cleared", ["bambuddy_url", "library_folder_id"])
def test_clearing_what_the_bambuddy_store_needs_while_on_it_is_refused(
    client: TestClient, cleared: str
) -> None:
    """The merged result is checked, not the patch: a store that could not start at the
    next boot is never saved."""
    ready = {
        "bambuddy_url": "http://bambuddy.test",
        "library_folder_id": 7,
        "store_backend": "bambuddy",
    }
    assert client.put("/api/v1/settings", json=ready).status_code == 200
    response = client.put("/api/v1/settings", json={cleared: None})
    assert response.status_code == 422
    assert "library folder" in response.json()["detail"]
    assert client.get("/api/v1/settings").json()["store_backend"] == "bambuddy"
    # Leaving the store first, then clearing, is fine.
    assert client.put("/api/v1/settings", json={"store_backend": "local"}).status_code == 200
    assert client.put("/api/v1/settings", json={cleared: None}).status_code == 200


def test_resetting_what_the_bambuddy_store_needs_while_on_it_is_refused(
    client: TestClient,
) -> None:
    """A reset clears the stored row, so the merged value is the deployment's (none here):
    the same unready store as an explicit clear, refused before anything is deleted."""
    ready = {
        "bambuddy_url": "http://bambuddy.test",
        "library_folder_id": 7,
        "store_backend": "bambuddy",
    }
    assert client.put("/api/v1/settings", json=ready).status_code == 200
    response = client.put("/api/v1/settings", json={"reset": ["bambuddy_url"]})
    assert response.status_code == 422
    assert "library folder" in response.json()["detail"]
    assert client.get("/api/v1/settings").json()["bambuddy_url"] == "http://bambuddy.test"
    assert client.put("/api/v1/settings", json={"store_backend": "local"}).status_code == 200
    assert client.put("/api/v1/settings", json={"reset": ["bambuddy_url"]}).status_code == 200
