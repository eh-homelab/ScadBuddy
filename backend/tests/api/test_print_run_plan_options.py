"""Issue #141: a filament-plan run carries the remembered print options.

``run_for_output`` resolves the remembered options and hands them to ``slice_and_queue``
alongside the plan's filament mapping. Both must land on the one queue item: the options
must not displace the plan's ``filament_overrides``, nor the plan the options.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_options_picker import remember
from tests.api.test_print_run_choices import run_request, run_routes
from tests.api.test_send import configure, make_output, upload_route

pytestmark = pytest.mark.requires_postgres


@respx.mock
def test_a_plan_run_queues_the_remembered_options_and_the_plans_overrides(
    client: TestClient, model: str
) -> None:
    configure(client)
    remember(client, "global", {"timelapse": False, "bed_levelling": "off", "quantity": 4})
    remember(client, "printer", {"flow_cali": "on"}, key="1")
    remember(client, "model", {"manual_start": True}, key=model)
    output_id = make_output(client, model)
    upload_route()
    run_routes()
    slice_routes()
    queued = queue_route()

    # The plan pins spool 9 to slot 1 and spool 5 to slot 2; `copies` is left out so
    # the remembered quantity applies.
    response = client.post(f"/api/v1/print/outputs/{output_id}/run", json=run_request(copies=None))
    assert response.status_code == 200, response.text

    sent: dict[str, Any] = json.loads(queued.calls.last.request.content)
    # Every scope's remembered option rides on the item: global, per-printer, per-model.
    assert sent["timelapse"] is False
    assert sent["bed_levelling"] == "off"
    assert sent["flow_cali"] == "on"
    assert sent["manual_start"] is True
    assert sent["quantity"] == 4
    assert response.json()["copies"] == 4
    # And so does the plan's mapping, one override per planned slot.
    assert [override["slot_id"] for override in sent["filament_overrides"]] == [1, 2]
    assert sent["required_filament_types"] == ["PETG", "PLA"]
    assert sent["printer_id"] == 1
