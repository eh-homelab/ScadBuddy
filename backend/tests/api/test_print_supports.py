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
    }

    off = {**body(), "supports": {"enabled": False}}
    assert run_print(client, output_id, json=off).status_code == 200
    assert overrides(sliced) == {"enable_support": "0"}


@respx.mock
def test_unreadable_preset_values_still_send_a_templates_supports(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Review of #2835: a template's `enable_support` went out as it is before #1899,
    and still does when the preset cannot be read. Only the angle, the one setting whose
    shape is in doubt, is left out, with a warning naming why."""
    output_id = prepared(client, model)
    declare_print_settings(
        paths,
        model,
        {"enable_prime_tower": "1", "enable_support": "1", "support_threshold_angle": "25"},
    )
    sliced = routes()
    preset_values(resolved=False, values={}, reason="sidecar_unavailable")

    response = run_print(client, output_id, json=body())

    assert response.status_code == 200, response.text
    assert overrides(sliced) == {"enable_prime_tower": "1", "enable_support": "1"}
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "supports-not-applied"]
    assert "Printing without support_threshold_angle" in warning["message"]
    assert "sidecar_unavailable" in warning["message"]


@respx.mock
def test_bambuddy_failing_the_read_still_sends_the_prints_choice(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    sliced = routes()
    respx.get(f"{BASE}/api/v1/slicer/preset-values").mock(
        return_value=httpx.Response(502, json={"detail": "bad gateway"})
    )

    response = run_print(client, output_id, json={**body(), "supports": {"enabled": True}})

    assert response.status_code == 200, response.text
    assert overrides(sliced) == {"enable_support": "1", "support_type": "tree(auto)"}
    assert "supports-not-applied" not in [w["kind"] for w in response.json()["warnings"]]


@respx.mock
def test_a_failed_read_names_a_short_cause(client: TestClient, model: str) -> None:
    """Review of #2835: the warning gives Bambuddy's answer, not the client's whole
    error text, which would repeat what the sentence already says."""
    output_id = prepared(client, model)
    routes()
    respx.get(f"{BASE}/api/v1/slicer/preset-values").mock(
        return_value=httpx.Response(502, json={"detail": "bad gateway"})
    )

    response = run_print(
        client, output_id, json={**body(), "supports": {"enabled": True, "threshold_angle": 40}}
    )

    assert response.status_code == 200, response.text
    [warning] = [w for w in response.json()["warnings"] if w["kind"] == "supports-not-applied"]
    assert warning["message"].endswith(": Bambuddy failed the read (HTTP 502).")
    assert "read the values of process preset" not in warning["message"]


@respx.mock
def test_a_choice_that_leaves_a_field_out_keeps_the_templates(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Review of #2835: `{"enabled": true}` keeps a template's type and build-plate
    setting, as it keeps its angle."""
    output_id = prepared(client, model)
    declare_print_settings(
        paths,
        model,
        {"support_type": "normal(auto)", "support_on_build_plate_only": "1"},
    )
    sliced = routes()
    preset_values(PRESET)

    response = run_print(client, output_id, json={**body(), "supports": {"enabled": True}})

    assert response.status_code == 200, response.text
    assert overrides(sliced) == {
        "enable_support": "1",
        "support_type": "normal(auto)",
        "support_on_build_plate_only": "1",
    }


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
    assert "support_threshold_angle (the process preset has no value" in warning["message"]


@respx.mock
def test_a_scalar_setting_the_preset_lacks_still_goes(client: TestClient, model: str) -> None:
    """A preset read that lacks ``support_on_build_plate_only`` sends it as one value, as
    a failed read does: a sparse preset never prints with less than an unreadable one."""
    output_id = prepared(client, model)
    sliced = routes()
    preset_values({"enable_support": "0", "support_type": "tree(auto)"})
    supports = {"enabled": True, "on_build_plate_only": True}

    response = run_print(client, output_id, json={**body(), "supports": supports})

    assert response.status_code == 200, response.text
    assert overrides(sliced) == {
        "enable_support": "1",
        "support_type": "tree(auto)",
        "support_on_build_plate_only": "1",
    }
    assert not [w for w in response.json()["warnings"] if w["kind"] == "supports-not-applied"]


@respx.mock
@pytest.mark.parametrize(
    ("key", "sent"),
    [
        ("enable_support", "1"),
        ("support_type", "normal(auto)"),
        ("support_on_build_plate_only", "1"),
        ("support_threshold_angle", "40"),
    ],
)
def test_every_setting_a_preset_stores_as_a_list_goes_as_one(
    client: TestClient, model: str, key: str, sent: str
) -> None:
    output_id = prepared(client, model)
    sliced = routes()
    preset_values({**PRESET, key: ["x", "x"]})
    supports = {
        "enabled": True,
        "type": "normal",
        "threshold_angle": 40,
        "on_build_plate_only": True,
    }

    response = run_print(client, output_id, json={**body(), "supports": supports})

    assert response.status_code == 200, response.text
    assert overrides(sliced)[key] == [sent, sent]


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
