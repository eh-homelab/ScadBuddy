"""#322: every runtime setting in the UI, with its source; remembered choices; About."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.uploads import ProjectTarget
from scadbuddy.core.settings import APPLIES, ENV_SEEDED, Settings
from scadbuddy.main import create_app
from tests.api.conftest import read_stored

BAMBUDDY = "https://bambuddy.test/api/v1"


def _state(client: TestClient) -> AppState:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    return state


def _connect(client: TestClient) -> None:
    client.put(
        "/api/v1/settings",
        json={"bambuddy_url": "https://bambuddy.test", "bambuddy_api_key": "s3cret"},
    )


@pytest.fixture(autouse=True)
def _restore_log_level() -> Iterator[None]:
    root = logging.getLogger()
    level = root.level
    yield
    root.setLevel(level)


# -- GET /settings: values, sources, applies, bootstrap --------------------------------


def test_every_runtime_value_is_reported_with_its_source(client: TestClient) -> None:
    body = client.get("/api/v1/settings").json()
    assert body["render_timeout"] == 120.0
    assert body["render_concurrency"] == 2
    assert body["log_level"] == "INFO"
    assert body["has_google_fonts_api_key"] is False
    assert "google_fonts_api_key" not in body
    assert set(body["sources"]) == set(ENV_SEEDED)
    assert body["sources"]["render_timeout"] == "default"
    # The fixture's settings are what the deployment passed.
    assert body["sources"]["preview_renders"] == "env"
    assert body["applies"] == dict(APPLIES)
    assert body["restart_required"] == []


def test_the_bootstrap_values_are_read_only_and_the_database_password_never_shown(
    client: TestClient, settings: Settings
) -> None:
    body = client.get("/api/v1/settings").json()
    bootstrap = {entry["name"]: entry for entry in body["bootstrap"]}
    assert set(bootstrap) >= {"data_dir", "openscad", "database_url", "revision", "version"}
    assert bootstrap["data_dir"]["value"] == str(settings.data_dir)
    assert bootstrap["data_dir"]["env_var"] == "SCADBUDDY_DATA_DIR"
    assert bootstrap["data_dir"]["reason"]
    assert bootstrap["version"]["source"] == "default"
    assert "postgres:" not in str(bootstrap["database_url"]["value"])
    assert body["about"]["version"] == "dev"
    assert body["about"]["revision"] == "unknown"
    # A bootstrap field is refused, not silently ignored.
    refused = client.put("/api/v1/settings", json={"openscad": "/bin/sh"})
    assert refused.status_code == 422


def test_an_invalid_value_is_a_422_naming_the_field(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"render_concurrency": 0})
    assert response.status_code == 422
    errors = response.json()["errors"]
    assert errors[0]["loc"] == ["body", "render_concurrency"]
    assert "SCADBUDDY_RENDER_CONCURRENCY must be at least 1" in errors[0]["msg"]

    cleared = client.put("/api/v1/settings", json={"render_timeout": None})
    assert cleared.status_code == 422
    assert cleared.json()["errors"][0]["loc"] == ["body", "render_timeout"]


def test_reset_puts_a_field_back_on_the_deployment_value(settings: Settings) -> None:
    deployed = settings.model_copy(update={"public_url": "https://env.example"})
    with TestClient(create_app(deployed)) as client:
        cleared = client.put("/api/v1/settings", json={"public_url": None}).json()
        assert cleared["public_url"] is None
        assert cleared["sources"]["public_url"] == "cleared"

        body = client.put("/api/v1/settings", json={"reset": ["public_url"]}).json()
        assert body["public_url"] == "https://env.example"
        assert body["sources"]["public_url"] == "env"
    assert "public_url" not in read_stored(settings.database_url)


def test_reset_names_only_env_seeded_fields(client: TestClient) -> None:
    response = client.put("/api/v1/settings", json={"reset": ["printer_id"]})
    assert response.status_code == 422


# -- applying: live, and at restart ----------------------------------------------------


def test_a_live_field_applies_at_once(client: TestClient) -> None:
    state = _state(client)
    body = client.put(
        "/api/v1/settings",
        json={
            "render_timeout": 12.5,
            "job_ttl": 3600,
            "asset_max_count": 5,
            "library_max_bytes": 1000,
            "duplicate_staging_max_age": 60,
            "log_level": "debug",
            "fonts_catalogue_ttl": 10,
            "render_queue_max": 7,
        },
    ).json()
    assert body["log_level"] == "DEBUG"
    assert body["restart_required"] == []
    assert state.config.render_timeout == 12.5
    assert state.render.config.render_timeout == 12.5
    assert state.render.config.job_ttl == 3600
    assert state.settings.render_timeout == 12.5
    assert state.assets.max_count == 5
    assert state.libraries.max_bytes == 1000
    assert state.catalogue.duplicate_staging_max_age == 60
    assert state.fonts.catalogue_ttl == 10
    assert logging.getLogger().level == logging.DEBUG
    assert state.metrics.queue_max._value.get() == 7


def test_a_new_google_fonts_key_swaps_the_client_and_refetches(client: TestClient) -> None:
    state = _state(client)
    before = state.fonts.client
    body = client.put("/api/v1/settings", json={"google_fonts_api_key": "g-key"}).json()
    assert body["has_google_fonts_api_key"] is True
    assert "g-key" not in str(body)
    assert state.fonts.client is not before
    assert state.fonts.client.api_key == "g-key"
    assert state.fonts.catalogue_stale is True


def test_a_restart_field_is_listed_until_the_process_runs_with_it(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        body = client.put(
            "/api/v1/settings", json={"render_concurrency": 3, "check_concurrency": 2}
        ).json()
        assert body["render_concurrency"] == 3
        assert sorted(body["restart_required"]) == ["check_concurrency", "render_concurrency"]
        # Still running with what it started with.
        assert _state(client).settings.render_concurrency == 2
        # Putting one back to the running value takes it off the list.
        again = client.put("/api/v1/settings", json={"check_concurrency": 1}).json()
        assert again["restart_required"] == ["render_concurrency"]

    # The next start reads it before anything is sized.
    with TestClient(create_app(settings)) as client:
        state = _state(client)
        assert state.settings.render_concurrency == 3
        assert state.render.config.render_concurrency == 3
        assert client.get("/api/v1/settings").json()["restart_required"] == []


def test_the_semaphores_are_sized_from_the_stored_values_at_start(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        client.put("/api/v1/settings", json={"lsp_sessions": 1, "realtime_sockets": 9})
    with TestClient(create_app(settings)) as client:
        state = _state(client)
        assert state.language_servers._value == 1
        assert state.realtime_sockets._value == 9


def test_a_settings_change_on_another_replica_is_applied_here(settings: Settings) -> None:
    """``settings.changed`` from anywhere re-reads the store and applies the live fields."""
    with TestClient(create_app(settings)) as here, TestClient(create_app(settings)) as there:
        there.put("/api/v1/settings", json={"render_timeout": 7.0})
        state = _state(here)
        for _ in range(200):
            if state.config.render_timeout == 7.0:
                break
            time.sleep(0.01)
        assert state.config.render_timeout == 7.0


def test_the_upload_limit_is_editable_and_the_gate_follows_it(
    client: TestClient, model: str
) -> None:
    body = client.put("/api/v1/settings", json={"media_upload_max_bytes": 1000}).json()
    assert body["media_upload_max_bytes"] == 1000
    assert body["sources"]["media_upload_max_bytes"] == "stored"
    response = client.post(
        f"/api/v1/models/{model}/media",
        files={"file": ("v.mp4", b"\x00" * 4000, "video/mp4")},
    )
    assert response.status_code == 413, response.text
    assert "SCADBUDDY_MEDIA_UPLOAD_MAX_BYTES" in response.json()["detail"]


# -- remembered choices ----------------------------------------------------------------


def _remember_everything(client: TestClient) -> None:
    client.put("/api/v1/print/models/gear/choices", json={"tier": "fine"})
    client.put("/api/v1/print/models/box/choices", json={"tier": "draft"})
    client.put("/api/v1/print/printers/1/bed-type", json={"bed_type": "Cool Plate"})
    client.put("/api/v1/print/printers/2/bed-type", json={"bed_type": "Textured PEI Plate"})
    client.put(
        "/api/v1/settings/print-options",
        json={"scope": "model", "key": "gear", "options": {"timelapse": True}},
    )
    client.put(
        "/api/v1/settings/print-options",
        json={"scope": "global", "options": {"use_ams": True}},
    )


def test_the_remembered_choices_are_listed(client: TestClient) -> None:
    _remember_everything(client)
    body = client.get("/api/v1/settings/remembered").json()
    assert body["model_print_choices"]["gear"]["tier"] == "fine"
    assert body["printer_bed_types"] == {"1": "Cool Plate", "2": "Textured PEI Plate"}
    assert body["model_print_options"] == {"gear": {"timelapse": True}}
    assert body["print_options"] == {"use_ams": True}
    assert body["printer_print_options"] == {}


def test_forget_all_forgets_every_remembered_choice(client: TestClient) -> None:
    _remember_everything(client)
    client.put("/api/v1/settings", json={"printer_id": 4})
    body = client.delete("/api/v1/settings/remembered").json()
    assert body == {
        "model_print_choices": {},
        "printer_bed_types": {},
        "print_options": {},
        "printer_print_options": {},
        "model_print_options": {},
        "printer_rack_algorithms": {},
        "project_print_targets": {},
    }
    assert client.get("/api/v1/settings").json()["printer_id"] == 4


def _remember_project_targets(client: TestClient) -> None:
    uploads = _state(client).uploads

    async def seed() -> None:
        await uploads.remember_project_target(3, ProjectTarget(printer_id=1, nozzle_diameter="0.4"))
        await uploads.remember_project_target(7, ProjectTarget(printer_id=2))

    asyncio.run(seed())


def test_the_remembered_project_targets_are_listed(client: TestClient) -> None:
    assert client.get("/api/v1/settings/remembered").json()["project_print_targets"] == {}
    _remember_project_targets(client)
    body = client.get("/api/v1/settings/remembered").json()
    assert body["project_print_targets"] == {
        "3": {"printer_id": 1, "nozzle_diameter": "0.4"},
        "7": {"printer_id": 2},
    }


def test_forgetting_one_project_target_leaves_the_rest(client: TestClient) -> None:
    _remember_everything(client)
    _remember_project_targets(client)
    body = client.delete("/api/v1/settings/remembered/projects/3").json()
    assert body["project_print_targets"] == {"7": {"printer_id": 2}}
    assert body["printer_bed_types"] == {"1": "Cool Plate", "2": "Textured PEI Plate"}
    assert client.get("/api/v1/settings/remembered").json()["project_print_targets"] == {
        "7": {"printer_id": 2}
    }
    # Forgetting what is not remembered is not an error.
    assert client.delete("/api/v1/settings/remembered/projects/3").status_code == 200


def test_forget_all_clears_the_project_targets(client: TestClient) -> None:
    _remember_project_targets(client)
    body = client.delete("/api/v1/settings/remembered").json()
    assert body["project_print_targets"] == {}
    assert asyncio.run(_state(client).uploads.project_target(3)) is None


def test_the_default_project_is_a_setting(client: TestClient) -> None:
    assert (
        client.put("/api/v1/settings", json={"last_project_id": 9}).json()["last_project_id"] == 9
    )
    assert (
        client.put("/api/v1/settings", json={"last_project_id": None}).json()["last_project_id"]
        is None
    )


# -- connection test, per scope; Bambuddy's own status ---------------------------------


def _printers() -> None:
    respx.get(f"{BAMBUDDY}/printers/").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "3DP-31B-598"}])
    )


def _scopes(body: dict[str, Any]) -> dict[str, tuple[str, bool]]:
    return {row["scope"]: (row["status"], row["required"]) for row in body["scopes"]}


@respx.mock
def test_the_connection_test_only_reads(client: TestClient) -> None:
    """Bambuddy offers no read-only way to ask what a key carries, so only Read Status
    is checked (by the printer list); the write scopes are reported unchecked, and no
    write is ever sent. ``respx.mock`` refuses any request not mocked here."""
    _connect(client)
    _printers()
    body = client.post("/api/v1/settings/test").json()
    assert body["ok"] is True
    assert _scopes(body) == {
        "Read Status": ("ok", True),
        "Manage Library": ("unknown", True),
        "Manage Queue": ("unknown", True),
        "Manage Projects": ("unknown", False),
        "Manage Archives": ("unknown", False),
    }
    assert {call.request.method for call in respx.calls} == {"GET"}
    unchecked = next(row for row in body["scopes"] if row["scope"] == "Manage Library")
    assert "Not checked" in unchecked["detail"]


@respx.mock
def test_a_refused_printer_list_marks_every_scope(client: TestClient) -> None:
    _connect(client)
    respx.get(f"{BAMBUDDY}/printers/").mock(return_value=httpx.Response(403))
    body = client.post("/api/v1/settings/test").json()
    assert body["ok"] is False
    assert _scopes(body)["Read Status"] == ("missing", True)
    assert {call.request.method for call in respx.calls} == {"GET"}


@respx.mock
def test_bambuddy_status_reports_its_version_and_the_finish_photo_setting(
    client: TestClient,
) -> None:
    _connect(client)
    respx.get(f"{BAMBUDDY}/updates/version").mock(
        return_value=httpx.Response(200, json={"version": "1.2.5.6", "repo": "maziggy/bambuddy"})
    )
    respx.get(f"{BAMBUDDY}/settings/").mock(
        return_value=httpx.Response(200, json={"capture_finish_photo": False, "auto_archive": True})
    )
    body = client.get("/api/v1/settings/bambuddy").json()
    assert body == {
        "version": "1.2.5.6",
        "capture_finish_photo": False,
        "settings_url": "https://bambuddy.test/settings",
        "detail": None,
    }


@respx.mock
def test_an_unreadable_finish_photo_setting_is_unknown_not_an_error(client: TestClient) -> None:
    _connect(client)
    respx.get(f"{BAMBUDDY}/updates/version").mock(
        return_value=httpx.Response(200, json={"version": "1.2.5.6"})
    )
    respx.get(f"{BAMBUDDY}/settings/").mock(return_value=httpx.Response(403))
    response = client.get("/api/v1/settings/bambuddy")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "1.2.5.6"
    assert body["capture_finish_photo"] is None
    assert "Read Status" in body["detail"]
