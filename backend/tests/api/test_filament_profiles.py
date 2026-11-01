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
    # #2799: the temperature is a filament setting, which the native slicer applies.
    assert [(s["key"], s["value"], s["unit"]) for s in silk["filament_settings"]] == [
        ("nozzle_temperature", "235", "°C"),
        ("nozzle_temperature_initial_layer", "235", "°C"),
    ]
    assert "235" in silk["filament_settings"][0]["sources"][0]["quote"]
    assert silk["notes"] == []


@respx.mock
def test_on_bambuddys_slicer_the_temperature_is_previewed_as_not_applied(
    client: TestClient,
) -> None:
    """#2799: Bambuddy's slice request has no filament field, so the dialog says the
    temperature is not applied there, while the wall speed still is."""
    configure(client)
    h2c_presets()
    preset_values()

    (silk,) = client.post(
        "/api/v1/print/filament-profiles/preview",
        json={"choices": {"nozzles": [{"size": "0.2"}], "tier": "fine"}, "profiles": ["silk-pla"]},
    ).json()

    assert silk["applicable"] is True
    assert silk["filament_applied"] is False
    assert silk["filament_reason"].startswith("Bambuddy's slicer has no filament setting override")


@respx.mock
def test_on_bambuddys_slicer_a_run_says_the_temperature_was_not_applied(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    preset_values()
    sliced = slice_routes()
    queue_route()
    request = {**body(), "filament_profiles": ["silk-pla"]}
    expected = {
        "kind": "profile-not-applied",
        "slot_id": None,
        "message": (
            "Silk PLA profile's nozzle temperature and nozzle temperature (first layer) not "
            "applied: Bambuddy's slicer has no filament setting override, so set them in "
            "the spool's filament preset. The native slicer applies them."
        ),
    }

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=request)
    response = run_print(client, output_id, json=request)

    assert check.status_code == 200, check.text
    assert expected in check.json()["warnings"]
    assert response.status_code == 200, response.text
    assert expected in response.json()["warnings"]
    sent = json.loads(sliced.calls.last.request.content)
    # The wall speed still goes out; nothing about the temperature does.
    assert sent["process_overrides"] == {"outer_wall_speed": ["50"] * 5}
    assert "nozzle_temperature" not in sliced.calls.last.request.content.decode()


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
def test_a_repeated_profile_is_previewed_once(client: TestClient) -> None:
    """#2804 review: as a run applies a repeated id once, the preview shows it once."""
    configure(client)
    h2c_presets()
    preset_values()

    response = client.post(
        "/api/v1/print/filament-profiles/preview",
        json={
            "choices": {"nozzles": [{"size": "0.2"}], "tier": "fine"},
            "profiles": ["silk-pla", "silk-pla"],
        },
    )

    assert response.status_code == 200, response.text
    assert [preview["profile"]["id"] for preview in response.json()] == ["silk-pla"]


@respx.mock
def test_a_check_and_a_run_read_the_presets_values_once_for_profiles_and_supports(
    client: TestClient, model: str
) -> None:
    """#2804 review: the profiles and the supports change the same process preset, so
    its values are read once for both, by the check and by the run."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    values = preset_values(
        {"outer_wall_speed": H2C_OUTER_WALL, "enable_support": "0", "support_type": "tree(auto)"}
    )
    slice_routes()
    queue_route()
    request = {**body(), "filament_profiles": ["silk-pla"], "supports": {"enabled": True}}

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=request)
    assert check.status_code == 200, check.text
    assert values.call_count == 1

    response = run_print(client, output_id, json=request)
    assert response.status_code == 200, response.text
    assert values.call_count == 2


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


@respx.mock
def test_a_bambuddy_error_reading_the_values_skips_the_profile_and_the_print_goes_ahead(
    client: TestClient, model: str
) -> None:
    """#2804 review: the route answering an HTTP error (not ``resolved: false``) skips
    the profile too. A print with Silk on, which it is by default, is never refused for
    a failure the user has no reason to tie to the profile."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    respx.get(f"{API}/slicer/preset-values").mock(
        return_value=httpx.Response(500, json={"detail": "sidecar exploded"})
    )
    sliced = slice_routes()
    queue_route()
    request = {**body(), "filament_profiles": ["silk-pla"]}

    check = client.post(f"/api/v1/print/outputs/{output_id}/check", json=request)
    response = run_print(client, output_id, json=request)

    assert check.status_code == 200, check.text
    assert check.json()["errors"] == []
    (warning,) = [w for w in check.json()["warnings"] if w["kind"] == "profile-not-applied"]
    # The fixed phrase supports.py uses, not the error's detail, which repeats the
    # operation the sentence already names (#2804 review).
    assert warning["message"] == (
        "Silk PLA profile not applied: couldn't read the preset's outer wall speed (the "
        "read failed (Bambuddy unreachable or answering with an error)). The print uses "
        "the preset as it is."
    )
    assert response.status_code == 200, response.text
    assert warning in response.json()["warnings"]
    assert "process_overrides" not in json.loads(sliced.calls.last.request.content)


@respx.mock
def test_an_empty_stored_list_counts_as_the_preset_not_having_the_setting(
    client: TestClient,
) -> None:
    """An empty list has no per-variant shape to copy: sending ``[]`` would change
    nothing and the dialog would claim it had."""
    configure(client)
    h2c_presets()
    preset_values({"outer_wall_speed": []})

    (silk,) = client.post(
        "/api/v1/print/filament-profiles/preview",
        json={"choices": {"nozzles": [{"size": "0.2"}], "tier": "fine"}, "profiles": ["silk-pla"]},
    ).json()

    assert silk["applicable"] is False
    assert silk["reason"] == "the preset has no outer wall speed (outer_wall_speed)"
    assert silk["changes"] == []


@respx.mock
def test_the_presets_values_are_read_once_for_every_profile(client: TestClient) -> None:
    configure(client)
    h2c_presets()
    values = preset_values()

    shown = client.post(
        "/api/v1/print/filament-profiles/preview",
        json={
            "choices": {"nozzles": [{"size": "0.2"}], "tier": "fine"},
            "profiles": ["silk-pla", "silk-pla"],
        },
    ).json()

    # A repeated id is shown once (#2804 review), and the values are read once.
    assert [entry["applicable"] for entry in shown] == [True]
    assert values.call_count == 1
