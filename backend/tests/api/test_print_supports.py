"""#1899: support structures, as a template declares them and as a print chooses them,
sent as process overrides in the shape the print's process preset stores each one. A
value whose shape cannot be read is left out with a warning; the print goes ahead."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from tests.api.test_filament_profiles import preset_values
from tests.api.test_print_filaments import prepared, queue_route, slice_routes
from tests.api.test_print_run_choices import body, declare_print_settings, run_print, run_routes
from tests.api.test_send import BASE, upload_route

pytestmark = pytest.mark.requires_postgres

#: The support settings as ``0.20mm Standard @BBL H2C`` resolves them, with the angle
#: as an extruder-variant list, so the shape is what decides the override's.
PRESET: dict[str, Any] = {
    "enable_support": "0",
    "support_type": "tree(auto)",
    "support_threshold_angle": ["30", "30"],
    "support_on_build_plate_only": "0",
}


def overrides(sliced: respx.Route) -> dict[str, Any]:
    # An empty map is not sent at all.
    return dict(json.loads(sliced.calls.last.request.content).get("process_overrides", {}))


def routes() -> respx.Route:
    upload_route()
    run_routes()
    sliced = slice_routes()
    queue_route()
    return sliced


@respx.mock
def test_the_prints_supports_go_out_in_the_presets_shape(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    sliced = routes()
    preset_values(PRESET)
    supports = {
        "enabled": True,
        "type": "normal",
        "threshold_angle": 40,
        "on_build_plate_only": True,
    }

    response = run_print(client, output_id, json={**body(), "supports": supports})

    assert response.status_code == 200, response.text
    assert overrides(sliced) == {
        "enable_support": "1",
        "support_type": "normal(auto)",
        # Every extruder variant alike, as a list: a bare "40" would collapse it.
        "support_threshold_angle": ["40", "40"],
        "support_on_build_plate_only": "1",
    }
    assert not [w for w in response.json()["warnings"] if w["kind"] == "supports-not-applied"]


@respx.mock
def test_a_templates_supports_apply_and_the_prints_choice_wins(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = prepared(client, model)
    declare_print_settings(
        paths,
        model,
        {"enable_support": "1", "support_type": "tree(auto)", "support_threshold_angle": "25"},
    )
    sliced = routes()
    preset_values(PRESET)

    assert run_print(client, output_id, json=body()).status_code == 200
    assert overrides(sliced) == {
        "enable_support": "1",
        "support_type": "tree(auto)",
        "support_threshold_angle": ["25", "25"],
    }

    # The template's angle stays under a choice that names none.
    choice = {"enabled": True, "type": "normal"}
    assert run_print(client, output_id, json={**body(), "supports": choice}).status_code == 200
    assert overrides(sliced) == {
        "enable_support": "1",
        "support_type": "normal(auto)",
        "support_threshold_angle": ["25", "25"],
        "support_on_build_plate_only": "0",
    }

    off = {**body(), "supports": {"enabled": False}}
    assert run_print(client, output_id, json=off).status_code == 200
    assert overrides(sliced) == {"enable_support": "0"}


@respx.mock
def test_unreadable_preset_values_print_without_supports_and_say_so(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    output_id = prepared(client, model)
    declare_print_settings(paths, model, {"enable_prime_tower": "1", "enable_support": "1"})
    sliced = routes()
    preset_values(resolved=False, values={}, reason="sidecar_unavailable")

    response = run_print(client, output_id, json={**body(), "supports": {"enabled": True}})

    assert response.status_code == 200, response.text
    # Never refused: the template's other settings still go, the supports do not.
    assert overrides(sliced) == {"enable_prime_tower": "1"}
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "supports-not-applied"]
    assert "Printing without the support settings" in warning["message"]
    assert "sidecar_unavailable" in warning["message"]


@respx.mock
def test_bambuddy_failing_the_read_prints_without_supports(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    sliced = routes()
    respx.get(f"{BASE}/api/v1/slicer/preset-values").mock(
        return_value=httpx.Response(502, json={"detail": "bad gateway"})
    )

    response = run_print(client, output_id, json={**body(), "supports": {"enabled": True}})

    assert response.status_code == 200, response.text
    assert overrides(sliced) == {}
    assert [w["kind"] for w in response.json()["warnings"]].count("supports-not-applied") == 1


@respx.mock
def test_a_setting_the_preset_lacks_is_left_out_and_named(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    sliced = routes()
    preset_values({"enable_support": "0", "support_type": "tree(auto)"})
    supports = {"enabled": True, "threshold_angle": 40}

    response = run_print(client, output_id, json={**body(), "supports": supports})

    assert response.status_code == 200, response.text
    assert overrides(sliced) == {"enable_support": "1", "support_type": "tree(auto)"}
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "supports-not-applied"]
    assert "support_threshold_angle" in warning["message"]
    assert "support_on_build_plate_only" in warning["message"]


@pytest.mark.parametrize(
    "supports",
    [{"enabled": True, "type": "organic"}, {"enabled": True, "threshold_angle": 91}, {}],
)
def test_a_support_choice_it_cannot_send_is_refused(
    client: TestClient, model: str, supports: dict[str, Any]
) -> None:
    output_id = prepared(client, model)
    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json={**body(), "supports": supports}
    )
    assert response.status_code == 422
