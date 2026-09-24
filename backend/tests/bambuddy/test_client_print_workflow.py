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
    EligibilityRequest,
    FolderCreate,
    PipelineCreate,
    PipelineRunRequest,
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


# --- pipelines ---------------------------------------------------------------------


@respx.mock
async def test_a_configured_pipeline_carries_its_target_and_fanout(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/slicer-pipelines/").mock(
        return_value=httpx.Response(200, json=recording("slicer-pipelines-configured.json"))
    )

    [pipeline] = await bambuddy.pipelines()

    assert (pipeline.id, pipeline.name) == (1, "Default")
    assert pipeline.target_kind == "specific_printer"
    assert pipeline.target_printer_id == 1
    assert pipeline.fanout_strategy == "max_parallel"
    assert pipeline.printer_preset == PresetRef(source="cloud", id="GM041")
    assert pipeline.bed_type == "Textured PEI Plate"


@respx.mock
async def test_creating_a_pipeline_sends_only_the_create_schemas_fields(
    bambuddy: BambuddyClient,
) -> None:
    """``SlicerPipelineCreate`` has no target or fanout — those are response-only."""
    route = respx.post(f"{API}/slicer-pipelines/").mock(
        return_value=httpx.Response(
            200, json=recording("slicer-pipelines-configured.json")["pipelines"][0]
        )
    )

    pipeline = await bambuddy.create_pipeline(
        PipelineCreate(
            name="ScadBuddy",
            printer_preset=PresetRef(source="cloud", id="GM041"),
            process_preset=PresetRef(source="cloud", id="GP243"),
            filament_presets=[PresetRef(source="cloud", id="GFSG00_23")],
            bed_type="Textured PEI Plate",
        )
    )

    assert pipeline.id == 1
    body = sent(route)
    assert body["filament_presets"] == [{"source": "cloud", "id": "GFSG00_23"}]
    assert "target_kind" not in body
    assert "fanout_strategy" not in body
    # description was never set, so it is absent rather than null.
    assert "description" not in body


@respx.mock
async def test_check_eligibility_returns_the_report_rather_than_raising(
    bambuddy: BambuddyClient,
) -> None:
    """An ineligible answer here is a 200 — only ``run`` turns it into a 409."""
    report = {
        "ok": False,
        "target_kind": "specific_printer",
        "target_printer_id": 1,
        "target_printer_name": "3DP-31B-598",
        "issues": [
            {
                "kind": "filament_type_mismatch",
                "slot_index": 0,
                "expected": "PLA",
                "actual": "PETG",
            }
        ],
        "printer_reports": [],
    }
    route = respx.post(f"{API}/slicer-pipelines/1/check-eligibility").mock(
        return_value=httpx.Response(200, json=report)
    )

    result = await bambuddy.check_eligibility(1, EligibilityRequest(source_library_file_id=41))

    assert result.ok is False
    assert [(i.kind, i.slot_index, i.expected, i.actual) for i in result.issues] == [
        ("filament_type_mismatch", 0, "PLA", "PETG")
    ]
    assert sent(route) == {"source_library_file_id": 41, "force": False}


@respx.mock
async def test_a_class_targeted_report_keeps_the_per_printer_detail(
    bambuddy: BambuddyClient,
) -> None:
    """Under ``printer_class`` ``ok`` means "at least one passes", and the per-printer
    reasons live in ``printer_reports``, not ``issues``."""
    respx.post(f"{API}/slicer-pipelines/1/check-eligibility").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "target_kind": "printer_class",
                "target_model_class": "H2C",
                "issues": [],
                "printer_reports": [
                    {"printer_id": 1, "printer_name": "3DP-31B-598", "ok": True, "issues": []},
                    {
                        "printer_id": 2,
                        "printer_name": "3DP-99Z-000",
                        "ok": False,
                        "issues": [{"kind": "printer_offline"}],
                    },
                ],
            },
        )
    )

    result = await bambuddy.check_eligibility(1, EligibilityRequest(source_archive_id=4))

    assert result.ok is True
    assert result.issues == []
    assert [(r.printer_id, r.ok) for r in result.printer_reports] == [(1, True), (2, False)]
    assert result.printer_reports[1].issues[0].kind == "printer_offline"


@respx.mock
async def test_an_unknown_issue_kind_still_parses(bambuddy: BambuddyClient) -> None:
    """Bambuddy adds issue kinds between releases; one must not 502 the whole report."""
    respx.post(f"{API}/slicer-pipelines/1/check-eligibility").mock(
        return_value=httpx.Response(
            200, json={"ok": False, "issues": [{"kind": "some_kind_shipped_later"}]}
        )
    )

    result = await bambuddy.check_eligibility(1, EligibilityRequest(source_library_file_id=41))

    assert result.issues[0].kind == "some_kind_shipped_later"


@respx.mock
async def test_a_run_reports_the_queue_entry_and_printer_of_every_copy(
    bambuddy: BambuddyClient,
) -> None:
    route = respx.post(f"{API}/slicer-pipelines/1/run").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": 12,
                "pipeline_id": 1,
                "pipeline_name": "Default",
                "source_library_file_id": 41,
                "copies": 2,
                "copies_completed": 0,
                "copies_in_progress": 1,
                "status": "dispatching",
                "slice_job_id": 9,
                "sliced_library_file_id": 52,
                "eligibility_overridden": True,
                "created_by": None,
                "created_at": "2026-09-23T01:00:00Z",
                "started_at": "2026-09-23T01:00:05Z",
                "completed_at": None,
                "target_kind": "specific_printer",
                "target_printer_id": 1,
                "fanout_strategy": "max_parallel",
                "jobs": [
                    {
                        "id": 30,
                        "pipeline_run_id": 12,
                        "copy_index": 0,
                        "assigned_printer_id": 1,
                        "assigned_printer_name": "3DP-31B-598",
                        "queue_entry_id": 90,
                        "status": "queued",
                    },
                    {
                        "id": 31,
                        "pipeline_run_id": 12,
                        "copy_index": 1,
                        "assigned_printer_id": None,
                        "assigned_printer_name": None,
                        "queue_entry_id": None,
                        "status": "awaiting_printer",
                    },
                ],
            },
        )
    )

    run = await bambuddy.run_pipeline(
        1, PipelineRunRequest(source_library_file_id=41, copies=2, force=True)
    )

    assert (run.id, run.status, run.copies) == (12, "dispatching", 2)
    assert run.eligibility_overridden is True
    assert [(j.copy_index, j.queue_entry_id, j.assigned_printer_id) for j in run.jobs] == [
        (0, 90, 1),
        (1, None, None),
    ]
    assert sent(route) == {"source_library_file_id": 41, "copies": 2, "force": True}


@respx.mock
async def test_the_runs_list_is_wrapped_and_carries_a_total(bambuddy: BambuddyClient) -> None:
    route = respx.get(f"{API}/slicer-pipelines/1/runs").mock(
        return_value=httpx.Response(200, json=recording("pipeline-runs.json"))
    )

    runs = await bambuddy.pipeline_runs(1, limit=25)

    assert (runs.runs, runs.total) == ([], 0)
    assert route.calls.last.request.url.params["limit"] == "25"


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
        (
            lambda c: c.check_eligibility(1, EligibilityRequest(source_archive_id=1)),
            "/slicer-pipelines/1/check-eligibility",
            "post",
            Scope.MANAGE_QUEUE,
        ),
        (
            lambda c: c.pipeline_runs(1),
            "/slicer-pipelines/1/runs",
            "get",
            Scope.MANAGE_QUEUE,
        ),
        (
            lambda c: c.create_pipeline(
                PipelineCreate(
                    name="x",
                    printer_preset=PresetRef(source="cloud", id="a"),
                    process_preset=PresetRef(source="cloud", id="b"),
                    filament_presets=[PresetRef(source="cloud", id="c")],
                )
            ),
            "/slicer-pipelines/",
            "post",
            Scope.MANAGE_QUEUE,
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
