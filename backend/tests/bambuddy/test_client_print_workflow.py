"""Issue #85 — the print-workflow surface, against recorded 1.2.5.5 bodies.

Bodies for the GET routes are recorded (see ``recordings/README.md``); the POST
responses are hand-built from Bambuddy's ``openapi.json``, because posting to the live
instance was out of bounds.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import Scope
from scadbuddy.bambuddy.models import (
    FolderCreate,
    PresetRef,
    ProjectCreate,
    QueueItemCreate,
)
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"


def sent(route: respx.Route) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(route.calls.last.request.read())
    return body


# --- printers ----------------------------------------------------------------------


@respx.mock
async def test_a_single_printer_has_the_same_shape_as_the_list_row(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/printers/1").mock(
        return_value=httpx.Response(200, json=recording("printer.json"))
    )

    printer = await bambuddy.printer(1)

    assert (printer.id, printer.name, printer.model) == (1, "3DP-31B-598", "H2C")
    assert printer.nozzle_count == 2


@respx.mock
async def test_the_status_exposes_the_nozzles_ams_trays_and_switch_inlet(
    bambuddy: BambuddyClient,
) -> None:
    """The four live things a print decision needs, off one recorded H2C."""
    respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=recording("printer-status.json"))
    )

    status = await bambuddy.printer_status(1)

    assert (status.id, status.name, status.connected) == (1, "3DP-31B-598", True)
    # One entry per extruder; the diameters are STRINGS on this route.
    assert [nozzle.nozzle_diameter for nozzle in status.nozzles] == ["0.2", "0.4"]
    assert status.nozzles[status.active_extruder].nozzle_diameter == "0.2"
    # Four AMS units, UNSORTED, and ``id`` is the printer's numbering rather than a
    # list index — the single-slot AMS-HT is id 128 and sits third.
    assert [unit.id for unit in status.ams] == [0, 1, 128, 2]
    units = {unit.id: unit for unit in status.ams}
    assert [tray.id for tray in units[0].tray] == [0, 1, 2, 3]
    assert units[0].tray[0].tray_type == "PETG"
    assert units[1].tray[1].tray_color == "0047BBFF"
    assert (units[128].is_ams_ht, len(units[128].tray)) == (True, 1)
    # The external spool is not an AMS at all; it arrives on vt_tray.
    assert [tray.id for tray in status.vt_tray] == [254, 255]
    # Keyed by STRINGIFIED ams id — JSON has no integer keys.
    assert status.ams_switch_inlet == {"0": "B", "1": "B", "2": "A", "128": "A"}
    assert status.nozzle_rack[0].filament_colour == "0047BBFF"


@respx.mock
async def test_an_untagged_spool_reports_minus_one_rather_than_zero(
    bambuddy: BambuddyClient,
) -> None:
    """``remain: -1`` means "unknown", not "empty" — a 0 would read as a dead spool."""
    respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=recording("printer-status.json"))
    )

    status = await bambuddy.printer_status(1)

    untagged = status.ams[0].tray[2]
    assert untagged.remain == -1
    assert untagged.tray_id_name == ""


@respx.mock
async def test_available_filaments_requires_the_model_and_returns_a_bare_list(
    bambuddy: BambuddyClient,
) -> None:
    route = respx.get(f"{API}/printers/available-filaments").mock(
        return_value=httpx.Response(200, json=recording("available-filaments.json"))
    )

    filaments = await bambuddy.available_filaments("H2C")

    assert route.calls.last.request.url.params["model"] == "H2C"
    assert "location" not in route.calls.last.request.url.params
    assert [f.type for f in filaments[:2]] == ["PETG", "PETG"]
    # This route spells the colour WITH a leading '#'; the AMS tray does not.
    assert filaments[0].color == "#0086D6FF"
    assert filaments[0].tray_info_idx == "GFG00"


@respx.mock
async def test_available_filaments_passes_a_location_when_given_one(
    bambuddy: BambuddyClient,
) -> None:
    route = respx.get(f"{API}/printers/available-filaments").mock(
        return_value=httpx.Response(200, json=recording("available-filaments.json"))
    )

    await bambuddy.available_filaments("H2C", location="Garage")

    assert route.calls.last.request.url.params["location"] == "Garage"


# --- presets -----------------------------------------------------------------------


@respx.mock
async def test_local_presets_are_grouped_by_type_and_keep_their_json_strings(
    bambuddy: BambuddyClient,
) -> None:
    """``compatible_printers`` is a JSON-encoded STRING here, unlike on the cloud tier."""
    respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=recording("local-presets.json"))
    )

    presets = await bambuddy.local_presets()

    assert (presets.printer, presets.process) == ([], [])
    filament = presets.filament[0]
    assert filament.preset_type == "filament"
    assert filament.source == "orcaslicer"
    assert filament.filament_type == "PETG"
    assert filament.compatible_printers == '["Bambu Lab H2C 0.4 nozzle"]'
    assert filament.default_filament_colour == '["#3400AD"]'


@respx.mock
async def test_a_local_preset_refs_itself_by_stringified_row_id(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/local-presets/").mock(
        return_value=httpx.Response(200, json=recording("local-presets.json"))
    )

    presets = await bambuddy.local_presets()

    assert presets.filament[0].ref() == PresetRef(source="local", id="2")


# --- projects and folders ----------------------------------------------------------


@respx.mock
async def test_projects_are_a_bare_list_with_their_rollup_counters(
    bambuddy: BambuddyClient,
) -> None:
    route = respx.get(f"{API}/projects/").mock(
        return_value=httpx.Response(200, json=recording("projects.json"))
    )

    [project] = await bambuddy.projects()

    assert (project.id, project.name, project.status) == (1, "Reagan Keychain", "active")
    assert (project.archive_count, project.total_items, project.completed_count) == (1, 1, 1)
    assert "status" not in route.calls.last.request.url.params


@respx.mock
async def test_a_status_filter_becomes_a_query_parameter(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/projects/").mock(
        return_value=httpx.Response(200, json=recording("projects.json"))
    )

    await bambuddy.projects(status_filter="active")

    assert route.calls.last.request.url.params["status"] == "active"


@respx.mock
async def test_creating_a_project_sends_tags_as_a_string(bambuddy: BambuddyClient) -> None:
    route = respx.post(f"{API}/projects/").mock(
        return_value=httpx.Response(200, json=recording("projects.json")[0])
    )

    project = await bambuddy.create_project(
        ProjectCreate(name="Keychains", tags="scadbuddy,keychain")
    )

    assert project.id == 1
    assert sent(route) == {
        "name": "Keychains",
        "tags": "scadbuddy,keychain",
        "priority": "normal",
    }


@respx.mock
async def test_add_archives_and_add_queue_post_their_id_lists(
    bambuddy: BambuddyClient,
) -> None:
    archives = respx.post(f"{API}/projects/1/add-archives").mock(
        return_value=httpx.Response(200, json={})
    )
    queue = respx.post(f"{API}/projects/1/add-queue").mock(
        return_value=httpx.Response(200, json={})
    )

    await bambuddy.add_archives_to_project(1, [4, 2])
    await bambuddy.add_queue_items_to_project(1, [9])

    assert sent(archives) == {"archive_ids": [4, 2]}
    assert sent(queue) == {"queue_item_ids": [9]}


@respx.mock
async def test_folders_by_project_carries_the_project_link(bambuddy: BambuddyClient) -> None:
    respx.get(f"{API}/library/folders/by-project/1").mock(
        return_value=httpx.Response(200, json=recording("folders-by-project.json"))
    )

    [folder] = await bambuddy.folders_by_project(1)

    assert (folder.id, folder.name) == (2, "Raegan")
    assert (folder.project_id, folder.project_name) == (1, "Reagan Keychain")
    assert folder.file_count == 6


@respx.mock
async def test_creating_a_folder_posts_to_the_trailing_slash_with_the_project_id(
    bambuddy: BambuddyClient,
) -> None:
    """The write route is ``/library/folders/``; ``folders()`` reads the slashless one."""
    route = respx.post(f"{API}/library/folders/").mock(
        return_value=httpx.Response(200, json=recording("folders-by-project.json")[0])
    )

    folder = await bambuddy.create_folder(FolderCreate(name="Raegan", project_id=1))

    assert folder.project_id == 1
    assert sent(route) == {"name": "Raegan", "project_id": 1}


# --- the full queue item -----------------------------------------------------------


@respx.mock
async def test_the_queue_create_defaults_are_bambuddys_own(bambuddy: BambuddyClient) -> None:
    """An omitted field and an unset one must mean the same thing to Bambuddy."""
    route = respx.post(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue-item.json"))
    )

    await bambuddy.enqueue(QueueItemCreate(printer_id=1, library_file_id=52))

    body = sent(route)
    assert body["bed_levelling"] == "auto"
    assert body["flow_cali"] == "auto"
    assert body["nozzle_offset_cali"] == "auto"
    assert body["preheat_override"] == "inherit"
    assert body["vibration_cali"] is True
    assert body["layer_inspect"] is False
    assert body["timelapse"] is False
    assert body["use_ams"] is True
    assert body["quantity"] == 1


@respx.mock
async def test_the_queue_create_sends_every_print_option_it_is_given(
    bambuddy: BambuddyClient,
) -> None:
    route = respx.post(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue-item.json"))
    )

    item = await bambuddy.enqueue(
        QueueItemCreate(
            printer_id=1,
            library_file_id=52,
            project_id=3,
            plate_id=2,
            quantity=4,
            ams_mapping=[1, 0],
            required_filament_types=["PLA", "PETG"],
            filament_overrides=[{"slot": 0, "colour": "#0047BB"}],
            nozzle_rack_choice={"0": 1},
            bed_levelling="on",
            flow_cali="off",
            timelapse=True,
            layer_inspect=True,
            use_ams=False,
            manual_start=True,
            insert_at_top=True,
        )
    )

    assert item.id == 9
    body = sent(route)
    assert body["ams_mapping"] == [1, 0]
    assert body["required_filament_types"] == ["PLA", "PETG"]
    assert body["filament_overrides"] == [{"slot": 0, "colour": "#0047BB"}]
    assert body["nozzle_rack_choice"] == {"0": 1}
    assert (body["project_id"], body["plate_id"], body["quantity"]) == (3, 2, 4)
    assert (body["manual_start"], body["insert_at_top"]) == (True, True)
    assert body["use_ams"] is False


@respx.mock
async def test_an_unset_optional_is_omitted_rather_than_sent_as_null(
    bambuddy: BambuddyClient,
) -> None:
    route = respx.post(f"{API}/queue/").mock(
        return_value=httpx.Response(200, json=recording("queue-item.json"))
    )

    await bambuddy.enqueue(QueueItemCreate(target_model="H2C", quantity=2))

    body = sent(route)
    assert body["target_model"] == "H2C"
    for absent in ("printer_id", "ams_mapping", "project_id", "plate_id", "batch_id"):
        assert absent not in body


@pytest.mark.parametrize("bad", ["yes", True, 1])
def test_a_bool_is_not_a_calibration_mode(bad: object) -> None:
    """``bed_levelling`` is ``off|on|auto``; the bool the design spec assumed 422s."""
    with pytest.raises(ValueError):
        QueueItemCreate(printer_id=1, bed_levelling=bad)  # type: ignore[arg-type]


# --- scopes ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("call", "route", "method", "scope"),
    [
        (lambda c: c.printer(1), "/printers/1", "get", Scope.READ_STATUS),
        (lambda c: c.printer_status(1), "/printers/1/status", "get", Scope.READ_STATUS),
        (
            lambda c: c.available_filaments("H2C"),
            "/printers/available-filaments",
            "get",
            Scope.READ_STATUS,
        ),
        (lambda c: c.local_presets(), "/local-presets/", "get", Scope.MANAGE_LIBRARY),
        (
            lambda c: c.folders_by_project(1),
            "/library/folders/by-project/1",
            "get",
            Scope.MANAGE_LIBRARY,
        ),
        (
            lambda c: c.create_folder(FolderCreate(name="x")),
            "/library/folders/",
            "post",
            Scope.MANAGE_LIBRARY,
        ),
        (lambda c: c.projects(), "/projects/", "get", Scope.MANAGE_PROJECTS),
        (
            lambda c: c.create_project(ProjectCreate(name="x")),
            "/projects/",
            "post",
            Scope.MANAGE_PROJECTS,
        ),
        (
            lambda c: c.add_archives_to_project(1, [1]),
            "/projects/1/add-archives",
            "post",
            Scope.MANAGE_PROJECTS,
        ),
        (
            lambda c: c.add_queue_items_to_project(1, [1]),
            "/projects/1/add-queue",
            "post",
            Scope.MANAGE_PROJECTS,
        ),
    ],
)
@respx.mock
async def test_every_new_call_names_the_scope_its_refusal_needs(
    bambuddy: BambuddyClient,
    call: Callable[[BambuddyClient], Awaitable[object]],
    route: str,
    method: str,
    scope: Scope,
) -> None:
    """A 403 must say which API-key scope to tick, not just "forbidden"."""
    getattr(respx, method)(f"{API}{route}").mock(
        return_value=httpx.Response(403, json={"detail": "Forbidden"})
    )

    with pytest.raises(ApiError) as caught:
        await call(bambuddy)

    assert caught.value.status == 409
    assert caught.value.extensions["required_scope"] == scope.value
    assert scope.value in caught.value.detail
