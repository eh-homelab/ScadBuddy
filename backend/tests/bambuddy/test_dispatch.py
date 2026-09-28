"""Slicing a :class:`SlicePlan` and queueing the result against one printer.

The route-level behaviour is exercised end-to-end in
``tests/api/test_print_run_choices.py``; this pins what goes on the wire from the plan
alone: every preset and the plate type on the slice, and the printer, plate and
remembered options on the queue item, with no class target.
"""

from __future__ import annotations

import json

import httpx
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import SlicePlan, slice_and_queue
from scadbuddy.bambuddy.filaments import QueueFilaments
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.bambuddy.options import PrintOptions
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"

PLAN = SlicePlan(
    printer_preset=PresetRef(source="cloud", id="GM042"),
    process_preset=PresetRef(source="cloud", id="GP243"),
    filament_presets=[PresetRef(source="cloud", id="GFG99")],
    filament_colours=["#688197"],
    bed_type="Supertack Plate",
)


def routes() -> tuple[respx.Route, respx.Route]:
    sliced = respx.post(f"{API}/library/files/41/slice").mock(
        return_value=httpx.Response(202, json={"job_id": 9, "status": "pending"})
    )
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(
            200, json={"id": 9, "status": "completed", "result": {"library_file_id": 52}}
        )
    )
    queued = respx.post(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue-item.json"))
    )
    return sliced, queued


@respx.mock
async def test_the_plan_is_what_is_sliced(bambuddy: BambuddyClient) -> None:
    sliced, _ = routes()

    await slice_and_queue(bambuddy, library_file_id=41, plan=PLAN, printer_id=1, plate_id=2)

    sent = json.loads(sliced.calls.last.request.read())
    sent.pop("use_embedded_settings")
    assert sent == {
        "printer_preset": {"source": "cloud", "id": "GM042"},
        "process_preset": {"source": "cloud", "id": "GP243"},
        "filament_presets": [{"source": "cloud", "id": "GFG99"}],
        "filament_colours": ["#688197"],
        "bed_type": "Supertack Plate",
        "plate": 2,
    }


@respx.mock
async def test_the_item_names_the_printer_and_carries_the_options(
    bambuddy: BambuddyClient,
) -> None:
    """One printer, never a class: the spools are loaded in that machine. ``copies``
    and ``project_id`` win over the quantity and project the options carry."""
    _, queued = routes()

    outcome = await slice_and_queue(
        bambuddy,
        library_file_id=41,
        plan=PLAN,
        printer_id=1,
        filaments=QueueFilaments(
            filament_overrides=[{"slot_id": 1, "type": "PETG", "color": "#688197"}],
            required_filament_types=["PETG"],
        ),
        plate_id=2,
        copies=3,
        project_id=7,
        options=PrintOptions(timelapse=False, quantity=5, project_id=4),
    )

    sent = json.loads(queued.calls.last.request.read())
    assert sent["printer_id"] == 1
    assert sent.get("target_model") is None
    assert (sent["library_file_id"], sent["plate_id"]) == (52, 2)
    assert (sent["quantity"], sent["project_id"], sent["timelapse"]) == (3, 7, False)
    assert sent["required_filament_types"] == ["PETG"]
    assert (outcome.slice_job_id, outcome.sliced_library_file_id) == (9, 52)
    assert outcome.printer_id == 1
