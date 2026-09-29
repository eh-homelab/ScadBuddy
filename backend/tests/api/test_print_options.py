"""Issue #88 — GET/PUT the remembered print options."""

from __future__ import annotations

import respx
from fastapi.testclient import TestClient

from scadbuddy.core.settings import Settings
from tests.api.conftest import read_stored

ROUTE = "/api/v1/settings/print-options"


def test_nothing_is_remembered_to_begin_with_and_bambuddys_defaults_are_served(
    client: TestClient,
) -> None:
    body = client.get(ROUTE).json()

    assert body["global_options"] == _unset()
    assert body["printers"] == {}
    assert body["models"] == {}
    # Served so the UI marks non-default values against one copy of them, not its own.
    assert body["defaults"]["timelapse"] is False
    assert body["defaults"]["bed_levelling"] == "auto"
    assert body["defaults"]["vibration_cali"] is True
    assert body["defaults"]["project_id"] is None


def test_a_per_printer_override_is_remembered_under_the_printer_id(
    client: TestClient, settings: Settings
) -> None:
    response = client.put(
        ROUTE, json={"scope": "printer", "key": "1", "options": {"timelapse": False}}
    )

    assert response.status_code == 200
    assert response.json()["printers"] == {"1": {**_unset(), "timelapse": False}}

    stored = read_stored(settings.database_url)
    assert stored["printer_print_options"]["1"]["timelapse"] is False


def test_each_scope_is_stored_separately(client: TestClient) -> None:
    client.put(ROUTE, json={"scope": "global", "options": {"use_ams": False}})
    client.put(ROUTE, json={"scope": "printer", "key": "1", "options": {"timelapse": False}})
    body = client.put(
        ROUTE, json={"scope": "model", "key": "demo", "options": {"quantity": 2}}
    ).json()

    assert body["global_options"]["use_ams"] is False
    assert body["printers"]["1"]["timelapse"] is False
    assert body["models"]["demo"]["quantity"] == 2


def test_a_scope_is_replaced_wholesale_not_merged(client: TestClient) -> None:
    client.put(
        ROUTE,
        json={"scope": "printer", "key": "1", "options": {"timelapse": False, "use_ams": False}},
    )

    body = client.put(
        ROUTE, json={"scope": "printer", "key": "1", "options": {"timelapse": False}}
    ).json()

    assert body["printers"]["1"]["use_ams"] is None


def test_clearing_a_scope_removes_it_rather_than_storing_an_empty_object(
    client: TestClient, settings: Settings
) -> None:
    client.put(ROUTE, json={"scope": "model", "key": "demo", "options": {"timelapse": False}})

    body = client.put(ROUTE, json={"scope": "model", "key": "demo", "options": {}}).json()

    assert body["models"] == {}
    stored = read_stored(settings.database_url)
    assert stored["model_print_options"] == {}


def test_saving_options_leaves_the_connection_settings_alone(client: TestClient) -> None:
    client.put("/api/v1/settings", json={"bambuddy_url": "https://bambuddy.test", "printer_id": 1})

    client.put(ROUTE, json={"scope": "global", "options": {"timelapse": False}})

    settings = client.get("/api/v1/settings").json()
    assert settings["bambuddy_url"] == "https://bambuddy.test"
    assert settings["printer_id"] == 1


def test_a_keyed_scope_needs_a_key_and_the_global_scope_refuses_one(client: TestClient) -> None:
    assert client.put(ROUTE, json={"scope": "printer", "options": {}}).status_code == 422
    assert client.put(ROUTE, json={"scope": "global", "key": "1", "options": {}}).status_code == 422


def test_a_misspelled_option_is_refused(client: TestClient) -> None:
    response = client.put(ROUTE, json={"scope": "global", "options": {"time_lapse": False}})
    assert response.status_code == 422


def test_bambuddys_own_bounds_are_enforced(client: TestClient) -> None:
    body = {"scope": "global", "options": {"preheat_chamber_target_override": 90}}
    assert client.put(ROUTE, json=body).status_code == 422


def _unset() -> dict[str, None]:
    """Every option null, which is how "leave it to Bambuddy" serialises."""
    return {
        name: None
        for name in (
            "bed_levelling",
            "flow_cali",
            "vibration_cali",
            "nozzle_offset_cali",
            "layer_inspect",
            "timelapse",
            "use_ams",
            "quantity",
            "manual_start",
            "insert_at_top",
            "auto_off_after",
            "project_id",
            "preheat_override",
            "preheat_chamber_target_override",
        )
    }


def test_a_configured_printer_is_used_directly_without_asking_bambuddy(
    client: TestClient,
) -> None:
    client.put("/api/v1/settings", json={"printer_id": 7})
    assert client.get(ROUTE).json()["printer_id"] == 7


@respx.mock
def test_the_options_never_touch_bambuddy(client: TestClient) -> None:
    """Both halves are the stored settings alone (#312: no pipeline to resolve a printer
    from). No Bambuddy route is mocked, so any call would fail this test."""
    client.put("/api/v1/settings", json={"bambuddy_url": "https://bambuddy.test"})

    saved = client.put(ROUTE, json={"scope": "global", "options": {"timelapse": False}})
    read = client.get(ROUTE)

    assert saved.status_code == 200
    body = read.json()
    assert body["printer_id"] is None
    assert body["global_options"]["timelapse"] is False
    assert body["defaults"]["bed_levelling"] == "auto"
