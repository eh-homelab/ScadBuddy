"""#2998: every value of the process preset a print resolves to, for the dialog's
"Slicer settings" editor and the agent, and a run's process overrides as the slice and
the check see them."""

from __future__ import annotations

import json

import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_filament_profiles import H2C_OUTER_WALL, preset_values
from tests.api.test_print_choices import h2c_presets
from tests.api.test_print_filaments import prepared, queue_route, slice_routes
from tests.api.test_print_run_choices import body, run_print, run_routes
from tests.api.test_print_supports import routes
from tests.api.test_send import configure, upload_route

pytestmark = pytest.mark.requires_postgres

CHOICES = {"nozzles": [{"size": "0.2"}], "tier": "fine"}


@respx.mock
def test_every_value_of_the_resolved_process_is_answered(client: TestClient) -> None:
    configure(client)
    h2c_presets()
    preset_values({"outer_wall_speed": H2C_OUTER_WALL, "skirt_loops": "1"})

    response = client.post("/api/v1/print/process-values", json={"choices": CHOICES})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "process": {"source": "cloud", "id": "GP243"},
        "resolved": True,
        "reason": None,
        "values": {"outer_wall_speed": H2C_OUTER_WALL, "skirt_loops": "1"},
    }


@respx.mock
def test_values_that_cannot_be_read_are_said_not_raised(client: TestClient) -> None:
    configure(client)
    h2c_presets()
    preset_values(resolved=False, values={}, reason="sidecar_unavailable")

    response = client.post("/api/v1/print/process-values", json={"choices": CHOICES})

    assert response.status_code == 200, response.text
    assert response.json()["resolved"] is False
    assert response.json()["reason"] == "sidecar_unavailable"


@respx.mock
def test_a_key_the_process_lacks_is_a_warning_and_still_sent(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    preset_values({"skirt_loops": "1"})
    sliced = slice_routes()
    queue_route()
    request = {**body(), "process_overrides": {"skirt_loops": 0, "no_such_key": "x"}}

    response = run_print(client, output_id, json=request)

    assert response.status_code == 200, response.text
    sent = json.loads(sliced.calls.last.request.content)["process_overrides"]
    assert sent == {"no_such_key": "x", "skirt_loops": "0"}
    warnings = response.json()["warnings"]
    unchecked = [w for w in warnings if w["kind"] == "process-override-unchecked"]
    assert len(unchecked) == 1 and "no_such_key" in unchecked[0]["message"]


@respx.mock
def test_the_check_warns_of_a_key_the_process_lacks(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    routes()
    preset_values({"skirt_loops": "1"})

    check = client.post(
        f"/api/v1/print/outputs/{output_id}/check",
        json={**body(), "process_overrides": {"no_such_key": "x"}},
    )

    assert check.status_code == 200, check.text
    kinds = [warning["kind"] for warning in check.json()["warnings"]]
    assert "process-override-unchecked" in kinds
