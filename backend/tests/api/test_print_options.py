"""Issue #88 — GET/PUT the remembered print options."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from tests.bambuddy.conftest import recording

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
    client: TestClient, data_dir: Path
) -> None:
    response = client.put(
        ROUTE, json={"scope": "printer", "key": "1", "options": {"timelapse": False}}
    )

    assert response.status_code == 200
    assert response.json()["printers"] == {"1": {**_unset(), "timelapse": False}}

    stored = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
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
    client: TestClient, data_dir: Path
) -> None:
    client.put(ROUTE, json={"scope": "model", "key": "demo", "options": {"timelapse": False}})

    body = client.put(ROUTE, json={"scope": "model", "key": "demo", "options": {}}).json()

    assert body["models"] == {}
    stored = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
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


@respx.mock
def test_the_printer_the_scope_keys_on_comes_from_the_pipeline_when_no_printer_is_set(
    client: TestClient,
) -> None:
    """Otherwise the UI would save "off for this printer" under a key the send never reads."""
    client.put(
        "/api/v1/settings",
        json={"bambuddy_url": "https://bambuddy.test", "pipeline_id": 4},
    )
    respx.get("https://bambuddy.test/api/v1/slicer-pipelines/4").mock(
        return_value=httpx.Response(200, json=recording("slicer-pipeline.json"))
    )

    assert client.get(ROUTE).json()["printer_id"] == 1


def test_a_configured_printer_is_used_directly_without_asking_bambuddy(
    client: TestClient,
) -> None:
    client.put("/api/v1/settings", json={"printer_id": 7})
    assert client.get(ROUTE).json()["printer_id"] == 7


@respx.mock
def test_remembering_an_option_never_needs_a_reachable_bambuddy(client: TestClient) -> None:
    client.put(
        "/api/v1/settings",
        json={"bambuddy_url": "https://bambuddy.test", "pipeline_id": 4},
    )
    respx.get("https://bambuddy.test/api/v1/slicer-pipelines/4").mock(
        side_effect=httpx.ConnectError("no route to host")
    )

    response = client.put(ROUTE, json={"scope": "global", "options": {"timelapse": False}})

    assert response.status_code == 200
    assert response.json()["global_options"]["timelapse"] is False


@respx.mock
def test_a_models_own_pipeline_decides_the_printer_the_scope_keys_on(
    client: TestClient, model: str
) -> None:
    """#86 lets a model default to its own pipeline, which may aim elsewhere."""
    client.put(
        "/api/v1/settings",
        json={"bambuddy_url": "https://bambuddy.test", "pipeline_id": 4},
    )
    assert (
        client.put(f"/api/v1/print/models/{model}/pipeline", json={"pipeline_id": 9}).status_code
        == 200
    )
    respx.get("https://bambuddy.test/api/v1/slicer-pipelines/9").mock(
        return_value=httpx.Response(
            200, json={**recording("slicer-pipeline.json"), "id": 9, "target_printer_id": 3}
        )
    )

    assert client.get(ROUTE, params={"slug": model}).json()["printer_id"] == 3


@respx.mock
def test_an_unreachable_bambuddy_still_serves_what_needs_no_bambuddy(
    client: TestClient,
) -> None:
    """Only ``printer_id`` needs the network; losing it must not blank the whole panel.

    The read side now has the property the write side was built and tested for. Without
    it a transient outage — or a pipeline deleted on Bambuddy's side, which ScadBuddy
    cannot notice because it stores only the id — disabled Remember and blanked the global
    and per-model rows that were sitting in settings.json all along.
    """
    client.put(
        "/api/v1/settings",
        json={"bambuddy_url": "https://bambuddy.test", "pipeline_id": 4},
    )
    client.put(ROUTE, json={"scope": "global", "options": {"timelapse": False}})
    respx.get("https://bambuddy.test/api/v1/slicer-pipelines/4").mock(
        side_effect=httpx.ConnectError("no route to host")
    )

    response = client.get(ROUTE)

    assert response.status_code == 200
    body = response.json()
    assert body["printer_id"] is None
    assert body["global_options"]["timelapse"] is False
    assert body["defaults"]["bed_levelling"] == "auto"


@respx.mock
def test_a_pipeline_that_no_longer_exists_is_not_fatal_either(client: TestClient) -> None:
    client.put(
        "/api/v1/settings",
        json={"bambuddy_url": "https://bambuddy.test", "pipeline_id": 4},
    )
    respx.get("https://bambuddy.test/api/v1/slicer-pipelines/4").mock(
        return_value=httpx.Response(404, json={"detail": "Not found"})
    )

    assert client.get(ROUTE).json()["printer_id"] is None
