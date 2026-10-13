"""#717 — filament profiles: Silk PLA's slower outer wall, suggested by the print dialog,
shown against the print's own process preset and sent as process overrides in the
shape that preset stores them."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from tests.api.test_print_choices import h2c_presets
from tests.api.test_print_filaments import prepared, queue_route, slice_routes
from tests.api.test_print_run_choices import body, declare_print_settings, run_print, run_routes
from tests.api.test_send import BASE, configure, upload_route

pytestmark = pytest.mark.requires_postgres

API = f"{BASE}/api/v1"

#: ``0.20mm Standard @BBL H2C`` at BambuStudio f977235e: one outer wall speed per
#: extruder variant (Direct Drive Standard, High Flow, E3D High Flow, Standard, High Flow).
H2C_OUTER_WALL = ["200", "500", "500", "200", "500"]


def preset_values(values: dict[str, Any] | None = None, **reply: Any) -> respx.Route:
    return respx.get(f"{API}/slicer/preset-values").mock(
        return_value=httpx.Response(
            200,
            json=reply
            or {"resolved": True, "values": values or {"outer_wall_speed": H2C_OUTER_WALL}},
        )
    )


def test_the_profiles_are_listed_with_their_sources(client: TestClient) -> None:
    profiles = client.get("/api/v1/print/filament-profiles").json()

    silk = next(profile for profile in profiles if profile["id"] == "silk-pla")
    assert silk["match"] == {"material": "PLA", "word": "silk"}
    assert [(s["key"], s["value"], s["unit"]) for s in silk["settings"]] == [
        ("outer_wall_speed", "50", "mm/s")
    ]
    source = silk["settings"][0]["sources"][0]
    assert source["url"].startswith("https://wiki.bambulab.com/")
    assert "Try 50 mm/s first." in source["quote"]
    # The temperature is advice, never sent: no filament channel is verified.
    assert "235" in silk["notes"][0]["text"]


@respx.mock
def test_a_preview_shows_each_change_against_the_presets_value(client: TestClient) -> None:
    configure(client)
    h2c_presets()
    values = preset_values()

    response = client.post(
        "/api/v1/print/filament-profiles/preview",
        json={"choices": {"nozzles": [{"size": "0.2"}], "tier": "fine"}},
    )

    assert response.status_code == 200, response.text
    (silk,) = response.json()
    assert silk["applicable"] is True
    assert silk["process_preset"] == {"source": "cloud", "id": "GP243"}
    assert silk["changes"] == [
        {
            "key": "outer_wall_speed",
            "label": "Outer wall speed",
            "unit": "mm/s",
            "current": H2C_OUTER_WALL,
            "value": "50",
        }
    ]
    params = values.calls.last.request.url.params
    assert (params["source"], params["id"], params["slot"]) == ("cloud", "GP243", "process")


@respx.mock
def test_unreadable_values_make_a_profile_unapplicable_saying_why(client: TestClient) -> None:
    configure(client)
    h2c_presets()
    preset_values(resolved=False, values={}, reason="sidecar_unavailable")

    (silk,) = client.post(
        "/api/v1/print/filament-profiles/preview",
        json={"choices": {"nozzles": [{"size": "0.2"}], "tier": "fine"}, "profiles": ["silk-pla"]},
    ).json()

    assert silk["applicable"] is False
    assert silk["reason"] == (
        "couldn't read the preset's outer wall speed (Bambuddy's slicer is not reachable)"
    )
    assert silk["changes"] == []


def test_an_unknown_profile_is_refused(client: TestClient, model: str) -> None:
    assert (
        client.post(
            "/api/v1/print/filament-profiles/preview",
            json={"choices": {"nozzles": [{"size": "0.2"}]}, "profiles": ["glitter"]},
        ).status_code
        == 422
    )
    output_id = prepared(client, model)
    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={**body(), "filament_profiles": ["glitter"]}
    )
    assert response.status_code == 422


@respx.mock
def test_a_profile_goes_out_in_the_presets_shape_under_the_templates_settings(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = prepared(client, model)
    declare_print_settings(paths, model, {"enable_prime_tower": "1"})
    upload_route()
    run_routes()
    preset_values()
    sliced = slice_routes()
    queue_route()

    response = run_print(client, output_id, json={**body(), "filament_profiles": ["silk-pla"]})

    assert response.status_code == 200, response.text
    overrides = json.loads(sliced.calls.last.request.content)["process_overrides"]
    # Every extruder variant slowed alike, as a list: a bare "50" would collapse it.
    assert overrides == {"outer_wall_speed": ["50"] * 5, "enable_prime_tower": "1"}


@respx.mock
def test_a_scalar_setting_stays_a_scalar(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    preset_values({"outer_wall_speed": "200"})
    sliced = slice_routes()
    queue_route()

    response = run_print(client, output_id, json={**body(), "filament_profiles": ["silk-pla"]})

    assert response.status_code == 200, response.text
    assert json.loads(sliced.calls.last.request.content)["process_overrides"] == {
        "outer_wall_speed": "50"
    }


@respx.mock
def test_a_profile_that_cannot_apply_is_skipped_with_a_warning_and_the_print_goes_ahead(
    client: TestClient, model: str
) -> None:
    """A print is never refused for a profile: with the preset's values unreadable, the
    profile's overrides are skipped (never sent in a guessed shape), Checks says so before
    Print, and the run prints with the preset as it is, saying so again."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    preset_values(resolved=False, values={}, reason="sidecar_unavailable")
    sliced = slice_routes()
    queue_route()
    request = {**body(), "filament_profiles": ["silk-pla"]}
    expected = {
        "kind": "profile-not-applied",
        "slot_id": None,
        "message": (
            "Silk PLA profile not applied: couldn't read the preset's outer wall speed "
            "(Bambuddy's slicer is not reachable). The print uses the preset as it is."
        ),
    }

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=request)
    response = run_print(client, output_id, json=request)

    assert check.status_code == 200, check.text
    assert check.json()["errors"] == []
    assert expected in check.json()["warnings"]
    assert response.status_code == 200, response.text
    assert expected in response.json()["warnings"]
    assert "process_overrides" not in json.loads(sliced.calls.last.request.content)
