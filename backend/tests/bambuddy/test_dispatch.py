"""Slicing a :class:`SlicePlan` and queueing the result against one printer.

The route-level behaviour is exercised end-to-end in
``tests/api/test_print_run_choices.py``; this pins what goes on the wire from the plan
alone: every preset and the plate type on the slice, and the printer, plate and
remembered options on the queue item, with no class target.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.dispatch import (
    RackChoice,
    SlicePlan,
    enqueue_plate,
    slice_and_queue,
    start_slice,
    wait_slice,
)
from scadbuddy.bambuddy.filaments import QueueFilaments
from scadbuddy.bambuddy.models import PresetRef
from scadbuddy.bambuddy.options import PrintOptions
from scadbuddy.core.problems import ApiError
from scadbuddy.rack.usage import PickedHotend
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
async def test_process_overrides_go_on_the_slice_and_into_its_key(
    bambuddy: BambuddyClient,
) -> None:
    """A template's print settings (#770) ride on the slice as Bambuddy's
    ``process_overrides``, and a slice made with them is not the plain preset's."""
    sliced, _ = routes()
    plan = PLAN.model_copy(update={"process_overrides": {"enable_prime_tower": "1"}})

    outcome = await slice_and_queue(bambuddy, library_file_id=41, plan=plan, printer_id=1)
    plain = await slice_and_queue(bambuddy, library_file_id=41, plan=PLAN, printer_id=1)

    sent = json.loads(sliced.calls[0].request.read())
    assert sent["process_overrides"] == {"enable_prime_tower": "1"}
    assert "process_overrides" not in json.loads(sliced.calls[1].request.read())
    assert outcome.preset_key == f"{plain.preset_key}/enable_prime_tower=1"


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


# The pieces `PrintRun` runs as activities (#1052, spec 2026-10-01 §5.3).


@respx.mock
async def test_start_slice_posts_once_and_returns_the_job(bambuddy: BambuddyClient) -> None:
    sliced, queued = routes()
    started = await start_slice(bambuddy, library_file_id=41, plan=PLAN, plate_id=2)
    assert (started.job_id, sliced.call_count, queued.called) == (9, 1, False)
    assert started.preset_key


@respx.mock
async def test_wait_slice_returns_the_sliced_file(bambuddy: BambuddyClient) -> None:
    routes()
    assert await wait_slice(bambuddy, 9) == 52


@respx.mock
async def test_wait_slice_raises_bambuddys_words_on_failure(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/slice-jobs/9").mock(
        return_value=httpx.Response(
            200, json={"id": 9, "status": "failed", "error": "object floats above the bed"}
        )
    )
    with pytest.raises(ApiError) as raised:
        await wait_slice(bambuddy, 9)
    assert raised.value.status == 502
    assert "object floats above the bed" in raised.value.detail
    assert raised.value.extensions == {"slice_job_id": 9}


@respx.mock
async def test_enqueue_plate_sends_the_item_once(bambuddy: BambuddyClient) -> None:
    _, queued = routes()
    item = await enqueue_plate(
        bambuddy, sliced=52, printer_id=1, plate_id=2, copies=3, project_id=None, options=None
    )
    sent = json.loads(queued.calls.last.request.read())
    assert (item, queued.call_count) == (recording("queue-item.json")["id"], 1)
    assert (sent["library_file_id"], sent["plate_id"], sent["quantity"]) == (52, 2, 3)


@respx.mock
async def test_the_rack_choice_rides_on_the_queue_item(bambuddy: BambuddyClient) -> None:
    _, queued = routes()
    asked: list[int] = []

    async def choose(sliced: int) -> RackChoice | None:
        asked.append(sliced)
        return RackChoice(
            nozzle_rack_choice={"0": 4},
            picks=[PickedHotend(group_id=0, position=4, serial="TEST-HOTEND-19")],
        )

    outcome = await slice_and_queue(
        bambuddy, library_file_id=41, plan=PLAN, printer_id=1, choose_rack=choose
    )

    assert asked == [52]
    assert json.loads(queued.calls.last.request.read())["nozzle_rack_choice"] == {"0": 4}
    assert [(p.group_id, p.position) for p in outcome.rack_picks] == [(0, 4)]
    assert "TEST-HOTEND-19" not in repr(outcome)


@respx.mock
async def test_no_rack_choice_sends_no_field(bambuddy: BambuddyClient) -> None:
    _, queued = routes()

    async def choose(sliced: int) -> RackChoice | None:
        return None

    outcome = await slice_and_queue(
        bambuddy, library_file_id=41, plan=PLAN, printer_id=1, choose_rack=choose
    )

    assert "nozzle_rack_choice" not in json.loads(queued.calls.last.request.read())
    assert outcome.rack_picks == []


@respx.mock
async def test_the_rack_is_chosen_after_the_slice_and_before_before_enqueue(
    bambuddy: BambuddyClient,
) -> None:
    sliced, _ = routes()
    order: list[str] = []

    async def choose(file_id: int) -> RackChoice | None:
        order.append(f"choose after {sliced.call_count} slice")
        return None

    async def before() -> None:
        order.append("before_enqueue")

    await slice_and_queue(
        bambuddy,
        library_file_id=41,
        plan=PLAN,
        printer_id=1,
        choose_rack=choose,
        before_enqueue=before,
    )

    assert order == ["choose after 1 slice", "before_enqueue"]


@respx.mock
async def test_a_choose_rack_that_raises_fails_the_plate_with_nothing_queued(
    bambuddy: BambuddyClient,
) -> None:
    """Spec section 5: awaited bare, like ``before_enqueue``. The callback the run builds
    never raises (Task 9 tests that); this pins that ``slice_and_queue`` adds no ``try``."""
    _, queued = routes()

    async def choose(sliced: int) -> RackChoice | None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await slice_and_queue(
            bambuddy, library_file_id=41, plan=PLAN, printer_id=1, choose_rack=choose
        )
    assert not queued.called
