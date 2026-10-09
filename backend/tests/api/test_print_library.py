"""#313 — printing a file already in Bambuddy's library through the Print dialog, as
any print is (#1752): only how its 3MF is obtained differs from an output's."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import trimesh
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.uploads import LibraryCopy, ProjectTarget
from scadbuddy.render.bambu3mf import MAX_SETTINGS_BYTES, layout_of, write_bambu_3mf
from scadbuddy.render.split import ColourPart
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import body, follow_run, run_routes
from tests.api.test_print_runs import Gate, gated_slice_routes
from tests.api.test_print_runs import gate as gate  # the fixture, shared
from tests.api.test_send import BASE, _uploaded_3mf, configure
from tests.api.test_settings_runtime import _state
from tests.bambuddy.conftest import recording
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

API = f"{BASE}/api/v1"


#: The project settings ScadBuddy's own 3MF carries, as library file 615's did (#484): no
#: flow and no nozzle stats, so Bambuddy's slicer falls back to the H2C's Standard.
STUB_SETTINGS = {
    "filament_colour": ["#43A047"],
    "printer_settings_id": "ScadBuddy",
    "print_settings_id": "ScadBuddy",
    "filament_settings_id": ["ScadBuddy"],
    "nozzle_diameter": ["0.4"],
    "printable_height": "325",
    "wipe_tower_x": ["145"],
    "wipe_tower_y": ["5"],
}


def library_3mf(*, sliced: bool = False) -> bytes:
    """A 3MF as Bambuddy's library holds it: unsliced, or with a plate's gcode in it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("3D/3dmodel.model", "<model/>")
        archive.writestr("Metadata/project_settings.config", json.dumps(STUB_SETTINGS))
        if sliced:
            archive.writestr("Metadata/plate_1.gcode", "; sliced\n")
    return buffer.getvalue()


def library_file(
    file_id: int = 89,
    *,
    file_type: str = "3mf",
    plates: str = "library-plates-single.json",
    content: bytes | None = None,
) -> respx.Route:
    """The file, its plates and its bytes (an unsliced 3MF unless ``content`` says
    otherwise); returns the download's route."""
    respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(
            200,
            json={"id": file_id, "filename": f"file-{file_id}.{file_type}", "file_type": file_type},
        )
    )
    respx.get(f"{API}/library/files/{file_id}/plates").mock(
        return_value=httpx.Response(200, json={**recording(plates), "file_id": file_id})
    )
    return respx.get(f"{API}/library/files/{file_id}/download").mock(
        return_value=httpx.Response(200, content=library_3mf() if content is None else content)
    )


def one_color(file_id: int) -> None:
    """The recorded requirements with slot 1 alone, so a run on the rack's differing
    nozzles is not refused as multi-color (#469). Registered before ``run_routes``,
    whose two-filament regex it then wins over."""
    answer = recording("filament-requirements.json")
    respx.get(f"{API}/library/files/{file_id}/filament-requirements").mock(
        return_value=httpx.Response(
            200, json={**answer, "file_id": file_id, "filaments": answer["filaments"][:1]}
        )
    )


def run_library(client: TestClient, file_id: int, *, json: dict[str, Any]) -> httpx.Response:
    """``POST /print/library/{id}/run``, followed to its end (#742), answered as the
    synchronous route was: a refusal before the 202 as it is, a run that succeeded as
    its ``result`` with a 200, one that failed as its ``error``."""
    started: httpx.Response = client.post(f"/api/v1/print/library/{file_id}/run", json=json)
    if started.status_code not in (200, 202):
        return started
    run = follow_run(client, started.json()["id"])
    if run["status"] == "succeeded":
        return httpx.Response(200, json=run["result"])
    return httpx.Response(run["error"]["status"], json=run["error"])


def listing_routes(files: Any) -> respx.Route:
    respx.get(f"{API}/library/folders").mock(
        return_value=httpx.Response(200, json=recording("library-folders-nested.json"))
    )
    return respx.get(f"{API}/library/files/").mock(return_value=httpx.Response(200, json=files))


@respx.mock
def test_the_root_lists_unsliced_3mfs_and_advanced_lists_every_file(client: TestClient) -> None:
    configure(client)
    listing_routes(recording("library-files-root.json"))

    plain = client.get("/api/v1/print/library").json()
    every = client.get("/api/v1/print/library", params={"all": "true"}).json()

    assert plain["files"] and {row["file_type"] for row in plain["files"]} == {"3mf"}
    assert all(row["printable"] for row in plain["files"])
    sliced = [row for row in every["files"] if row["file_type"] == "gcode.3mf"]
    assert sliced and not any(row["printable"] for row in sliced)
    assert plain["hidden"] == len(every["files"]) - len(plain["files"])
    # The folder tree arrives flattened, each with its depth.
    assert {"Supplies": 0, "Storage": 1}.items() <= {
        row["name"]: row["depth"] for row in plain["folders"]
    }.items()


@respx.mock
def test_a_file_scadbuddy_uploaded_names_its_output(client: TestClient, app: FastAPI) -> None:
    """#1864: what Arrange reads a generated library file's objects through."""
    configure(client)
    listing_routes(recording("library-files-root.json"))
    first = client.get("/api/v1/print/library").json()["files"][0]["id"]
    state: AppState = getattr(app.state, STATE_ATTR)
    asyncio.run(
        state.uploads.record("a" * 32, LibraryCopy(id=first, folder_id=None, target_key="k"))
    )

    files = client.get("/api/v1/print/library").json()["files"]

    assert {row["id"]: row["output_id"] for row in files if row["output_id"]} == {first: "a" * 32}
    assert len(files) > 1


@respx.mock
def test_a_library_print_s_copy_names_no_output(client: TestClient, app: FastAPI) -> None:
    """A library print records its copy under its subject, ``library:<file id>``
    (`send.upload_copy`), which is no output: Arrange must not read it as one."""
    configure(client)
    listing_routes(recording("library-files-root.json"))
    first = client.get("/api/v1/print/library").json()["files"][0]["id"]
    state: AppState = getattr(app.state, STATE_ATTR)
    asyncio.run(
        state.uploads.record("library:46", LibraryCopy(id=first, folder_id=None, target_key="k"))
    )

    files = client.get("/api/v1/print/library").json()["files"]

    assert [row["output_id"] for row in files if row["id"] == first] == [None]


@respx.mock
def test_an_stl_is_listed_under_advanced_with_print(client: TestClient) -> None:
    configure(client)
    listing_routes(recording("library-files-folder.json"))

    every = client.get("/api/v1/print/library", params={"folder_id": 4, "all": "true"}).json()

    stls = [row for row in every["files"] if row["file_type"] == "stl"]
    assert stls and all(row["printable"] for row in stls)


@respx.mock
def test_an_stl_slices_as_one_plate(client: TestClient) -> None:
    """Wrapped in a 3MF of ScadBuddy's own, it is laid out for the printer as a render is."""
    configure(client)
    respx.get(f"{API}/library/files/46/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements-stl.json"))
    )
    upload = flow_copy_routes()
    stl = trimesh.creation.box(extents=(20, 10, 5)).export(file_type="stl")
    library_file(46, file_type="stl", plates="library-plates-stl.json", content=stl)
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = run_library(
        client,
        46,
        json={**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}},
    )

    assert response.status_code == 200, response.text
    assert json.loads(sliced.calls.last.request.content)["plate"] == 1
    assert "/library/files/141/slice" in str(sliced.calls.last.request.url)
    assert uploaded_name(upload) == "file-46 (ScadBuddy).3mf"
    assert layout_of(_uploaded_3mf(upload)) == "scadbuddy"


@respx.mock
def test_a_folder_of_hundreds_of_files_is_one_read(client: TestClient) -> None:
    configure(client)
    rows = [
        {
            "id": 1000 + n,
            "filename": f"part-{n}.{'3mf' if n % 2 else 'gcode.3mf'}",
            "file_type": "3mf" if n % 2 else "gcode.3mf",
            "folder_id": 9,
            "file_size": 1,
            "print_count": 0,
            "created_at": "2026-09-28T00:00:00",
        }
        for n in range(500)
    ]
    files = listing_routes(rows)

    plain = client.get("/api/v1/print/library", params={"folder_id": 9}).json()
    every = client.get("/api/v1/print/library", params={"folder_id": 9, "all": "true"}).json()

    assert (len(plain["files"]), plain["hidden"], len(every["files"])) == (250, 250, 500)
    assert files.call_count == 2  # one read per listing, never one per file
    assert files.calls.last.request.url.params["folder_id"] == "9"


@respx.mock
def test_a_library_file_is_laid_out_sliced_and_queued_as_an_output_is(
    client: TestClient,
) -> None:
    """#1752 (B1, B2): its copy, laid out for the choices, is what is sliced; the
    user's own file is only read."""
    configure(client)
    one_color(89)
    upload = flow_copy_routes()
    library_file(89)
    run_routes()
    sliced = slice_routes()
    queued = queue_route()

    response = run_library(
        client,
        89,
        json={**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["library_file_id"] == 141
    assert response.json()["folder_id"] is None
    assert "/library/files/141/slice" in str(sliced.calls.last.request.url)
    assert json.loads(queued.calls.last.request.content)["printer_id"] == 1
    assert upload.call_count == 1
    assert upload.calls.last.request.url.params["folder_id"] == "2"
    assert not [
        call
        for call in respx.calls
        if "/library/files/89" in call.request.url.path and call.request.method != "GET"
    ]


@respx.mock
def test_a_file_deleted_in_bambuddy_is_a_404_with_nothing_sliced(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/library/files/89").mock(
        return_value=httpx.Response(404, json={"detail": "File not found"})
    )
    run_routes()
    sliced = slice_routes()
    queued = queue_route()

    response = client.post("/api/v1/print/library/89/run", json=body())

    assert response.status_code == 404, response.text
    assert response.json()["type"].endswith("/bambuddy-not-found")
    assert not sliced.called and not queued.called


@respx.mock
def test_a_sliced_file_is_a_422_before_anything_is_sliced(client: TestClient) -> None:
    configure(client)
    library_file(104, file_type="gcode.3mf")
    run_routes()
    sliced = slice_routes()

    run = client.post("/api/v1/print/library/104/run", json=body())
    choices = client.get("/api/v1/print/library/104/choices")
    filaments = client.get("/api/v1/print/library/104/filaments")

    for response in (run, choices, filaments):
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == (
            "file-104.gcode.3mf is sliced already. Print it from Bambuddy."
        )
    assert not sliced.called


@respx.mock
def test_a_file_with_no_plate_metadata_prints_plate_one(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/library/files/70/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements-stl.json"))
    )
    flow_copy_routes()
    library_file(70, plates="library-plates-stl.json")
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = run_library(
        client,
        70,
        json={
            **body(),
            "all_plates": True,
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
        },
    )

    assert response.status_code == 200, response.text
    assert sliced.call_count == 1
    sent = json.loads(sliced.calls.last.request.content)
    assert sent["plate"] == 1 and len(sent["filament_presets"]) == 1
    assert client.get("/api/v1/print/library/70/plates").json() == []


@respx.mock
def test_a_library_file_s_plates_carry_their_names(client: TestClient) -> None:
    """#929: the dialog labels a plate by what it holds, so Bambuddy's name comes through."""
    configure(client)
    library_file(67, plates="library-plates-multi.json")

    plates = client.get("/api/v1/print/library/67/plates").json()

    assert [(plate["index"], plate["name"]) for plate in plates] == [
        (1, "makerlab"),
        (2, "quant_1_A"),
    ]


@respx.mock
def test_the_choices_are_remembered_per_library_file(client: TestClient) -> None:
    configure(client)
    library_file(89)
    library_file(67, plates="library-plates-multi.json")
    run_routes()
    remembered = {
        "printer_id": 1,
        "filament_plan": [{"slot_id": 1, "spool_id": 9}],
        "nozzles": [{"size": "0.2"}],
        "tier": "fine",
    }

    put = client.put("/api/v1/print/library/89/choices", json=remembered)
    own = client.get("/api/v1/print/library/89/choices").json()["model_choices"]
    other = client.get("/api/v1/print/library/67/choices").json()["model_choices"]

    assert put.status_code == 200, put.text
    assert own["tier"] == "fine" and [n["size"] for n in own["nozzles"]] == ["0.2", "0.2"]
    assert other["tier"] is None and other["nozzles"] == []


@respx.mock
def test_a_remembered_printer_that_is_gone_falls_through(client: TestClient) -> None:
    configure(client)
    library_file(89)
    run_routes()
    put = client.put("/api/v1/print/library/89/choices", json={"printer_id": 99})

    choices = client.get("/api/v1/print/library/89/choices")

    assert put.status_code == 200, put.text
    assert put.json()["printer_id"] == 99
    assert choices.json()["model_choices"]["printer_id"] == 99

    assert choices.status_code == 200, choices.text
    assert choices.json()["printer_id"] == 1


@respx.mock
def test_the_check_refuses_a_library_file_nothing_on_the_mounted_nozzles(
    client: TestClient,
) -> None:
    """#768 on a library file: two filaments, a 0.2 on the right and a 0.4 on the left,
    which #755's check used to refuse. The run no longer judges the mounted nozzles, so
    the check says nothing, and slices nothing."""
    configure(client)
    library_file(89)
    run_routes()
    sliced = slice_routes()
    request = {
        **body(),
        "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}, {"slot_id": 2, "spool_id": 10}]},
    }

    check = client.post("/api/v1/print/library/89/check", json=request)

    assert check.status_code == 200, check.text
    assert (check.json()["errors"], check.json()["warnings"]) == ([], [])
    assert not sliced.called


@respx.mock
def test_a_library_run_answers_202_before_the_slice_finishes_and_a_retry_is_its_run(
    client: TestClient, gate: Gate
) -> None:
    """#742: the run is followed like an output's, so a proxy's timeout cannot make a
    retry queue the print twice."""
    configure(client)
    one_color(89)
    flow_copy_routes()
    library_file(89)
    run_routes()
    gated_slice_routes(gate)
    queued = queue_route()
    request = {
        **body(),
        "request_id": "press-1",
        "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
    }

    response = client.post("/api/v1/print/library/89/run", json=request)

    assert response.status_code == 202, response.text
    run = response.json()
    assert run["status"] == "running" and run["output_id"] == "library:89"
    assert not queued.called
    again = client.post("/api/v1/print/library/89/run", json=request)
    assert again.status_code == 200
    assert again.json()["id"] == run["id"] and again.json()["repeated"] is True

    gate.open()
    ended = follow_run(client, run["id"])

    assert ended["status"] == "succeeded"
    assert ended["result"]["library_file_id"] == 141
    assert queued.call_count == 1


@respx.mock
def test_a_library_file_s_print_is_in_the_history_once_bambuddy_archives_it(
    client: TestClient,
) -> None:
    """#976: a library-file run has no output, and its archive was never listed."""
    configure(client)
    one_color(89)
    flow_copy_routes()
    library_file(89)
    run_routes()
    slice_routes()
    queued_again = queue_route(item_id=51)
    response = run_library(
        client,
        89,
        json={**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}},
    )
    assert response.status_code == 200, response.text
    # The scheduler dispatched the item: Bambuddy made archive 90 and named it on the item.
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 51,
                "printer_id": 1,
                "archive_id": 90,
                "library_file_id": 89,
                "library_file_name": "spoollock.gcode.3mf",
                "status": "printing",
                "plate_id": 1,
            },
        )
    )
    respx.get(f"{API}/archives/90").mock(
        return_value=httpx.Response(
            200,
            json={
                **recording("archive-detail.json"),
                "id": 90,
                "status": "printing",
                "print_name": "Bambu Spool Lock or shim UPDATED",
                "photos": [],
                "finish_photo": None,
                "timelapse_path": None,
            },
        )
    )
    respx.get(f"{API}/archives/90/runs").mock(
        return_value=httpx.Response(200, json=recording("archive-runs.json"))
    )

    listed = client.get("/api/v1/prints", params={"status": "printing"})

    assert listed.status_code == 200, listed.text
    [summary] = listed.json()["items"]
    assert summary["archive_id"] == 90
    assert summary["library_file_id"] == 89
    assert summary["output_id"] is None and summary["slug"] is None
    assert summary["output_name"] == "Bambu Spool Lock or shim UPDATED"
    assert summary["status"] == "printing"
    assert summary["params_diff"] is None
    assert summary["cover"] == {"kind": "thumbnail", "url": "/api/v1/prints/90/thumbnail"}

    # It opens, and its media is served, like an output's print.
    detail = client.get("/api/v1/prints/90")
    assert detail.status_code == 200, detail.text
    assert detail.json()["provenance"] is None
    assert detail.json()["links"]["customize_url"] is None
    assert {file["kind"] for file in detail.json()["files"]} == {
        "library_file",
        "preview_glb",
        "sliced",
        "source",
    }
    respx.get(f"{API}/archives/90/thumbnail").mock(
        return_value=httpx.Response(200, content=b"png", headers={"content-type": "image/png"})
    )
    assert client.get("/api/v1/prints/90/thumbnail").status_code == 200

    # Once Bambuddy drops the queue item, the print stays listed.
    respx.get(f"{API}/queue/51").mock(return_value=httpx.Response(404, json={"detail": "gone"}))
    assert [item["archive_id"] for item in client.get("/api/v1/prints").json()["items"]] == [90]

    # And it prints again, on the printer and plate it printed on.
    reprint = client.post("/api/v1/prints/90/reprint", headers=press())
    assert reprint.status_code == 201, reprint.text
    sent = json.loads(queued_again.calls.last.request.content)
    assert sent["archive_id"] == 90 and sent["printer_id"] == 1


# --- #484: the flow chosen for a library file Bambuddy slices -----------------------------
#
# Library file 615, a ScadBuddy 3MF, printed with High Flow chosen for both sides of a
# printer with an HH01 0.4 on each, and paused at the first layer ("the left nozzle is
# not matched"): its settings stated no flow, so Bambuddy's slicer sliced it Standard.

HIGH_FLOW_04 = {"nozzle_type": "HH01", "nozzle_diameter": "0.4"}


def both_sides_high_flow() -> None:
    """Printer 1 with a 0.4 High Flow mounted on each side, the rack's two mounted
    hotends saying the same (library file 615's printer)."""
    status = recording("printer-status-rack.json")
    status["nozzles"] = [HIGH_FLOW_04, HIGH_FLOW_04]
    for entry in status["nozzle_rack"]:
        if entry["id"] in (0, 1):
            entry.update(HIGH_FLOW_04)
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(200, json=status))


def high_flow_run() -> dict[str, Any]:
    return {
        **body(nozzles=[{"size": "0.4", "flow": "high_flow"}], tier="standard"),
        "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
    }


def flow_copy_routes(listed: list[dict[str, Any]] | None = None) -> respx.Route:
    """The inbox (folder 2, from Settings) as listed before the copy is uploaded, and
    the upload, which Bambuddy answers as file 141 of one filament. Registered before
    ``run_routes``, as :func:`one_color` is."""
    respx.get(f"{API}/library/files", params={"folder_id": 2}).mock(
        return_value=httpx.Response(200, json=listed or [])
    )
    one_color(141)
    return respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(
            200, json={"id": 141, "filename": "file-89.3mf", "file_type": "3mf"}
        )
    )


def uploaded_name(route: respx.Route) -> str:
    content: bytes = route.calls.last.request.content
    return content.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()


def hf_mounted(warnings: list[dict[str, Any]]) -> list[str]:
    return [warning["message"] for warning in warnings if warning["kind"] == "hf-mounted"]


@respx.mock
def test_a_library_file_bambuddy_slices_is_sliced_for_the_high_flow_chosen(
    client: TestClient,
) -> None:
    """What reaches the slicer is a copy stating the flow chosen for each side, so the
    slice matches the High Flow nozzles mounted and nothing warns of them. The user's
    own file is only read."""
    configure(client)
    one_color(89)
    upload = flow_copy_routes()
    library_file(89)
    run_routes()
    both_sides_high_flow()
    sliced = slice_routes()
    queue_route()

    check = client.post("/api/v1/print/library/89/check", json=high_flow_run())
    response = run_library(client, 89, json=high_flow_run())

    assert check.status_code == 200, check.text
    assert hf_mounted(check.json()["warnings"]) == []
    assert response.status_code == 200, response.text
    assert hf_mounted(response.json()["warnings"]) == []
    assert "/library/files/141/slice" in str(sliced.calls.last.request.url)
    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert "3D/3dmodel.model" in archive.namelist()
    assert settings["nozzle_volume_type"] == ["High Flow", "High Flow"]
    # Its author's settings are kept, but for the spool's colour (#476).
    assert settings["filament_colour"] == ["#688197"]
    assert {key: settings[key] for key in STUB_SETTINGS if key != "filament_colour"} == {
        key: value for key, value in STUB_SETTINGS.items() if key != "filament_colour"
    }
    assert uploaded_name(upload) == "file-89 (ScadBuddy).3mf"
    assert upload.calls.last.request.url.params["folder_id"] == "2"
    assert not [
        call
        for call in respx.calls
        if "/library/files/89" in call.request.url.path and call.request.method != "GET"
    ]


@respx.mock
def test_a_library_files_flow_copy_in_the_inbox_is_reused(client: TestClient) -> None:
    """The same bytes under the same name in the inbox are sliced again, not uploaded
    again, so printing a file twice leaves one copy."""
    configure(client)
    one_color(89)
    first = flow_copy_routes()
    library_file(89)
    run_routes()
    both_sides_high_flow()
    sliced = slice_routes()
    queue_route()
    assert run_library(client, 89, json=high_flow_run()).status_code == 200
    payload = _uploaded_3mf(first)
    row = {"id": 141, "filename": uploaded_name(first), "file_type": "3mf"}
    flow_copy_routes([{**row, "file_size": len(payload)}])
    respx.get(f"{API}/library/files/141").mock(
        return_value=httpx.Response(
            200, json={**row, "file_hash": hashlib.sha256(payload).hexdigest()}
        )
    )

    # Two copies, so it is a second run rather than the first one's answer repeated.
    response = run_library(client, 89, json={**high_flow_run(), "copies": 2})

    assert response.status_code == 200, response.text
    assert sliced.call_count == 2
    assert "/library/files/141/slice" in str(sliced.calls.last.request.url)
    assert first.call_count == 1


def _settings_bomb() -> bytes:
    """A 3MF whose settings inflate past the cap from a few KB (#484 review)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("3D/3dmodel.model", "<model/>")
        archive.writestr(
            "Metadata/project_settings.config", b"{" + b" " * (MAX_SETTINGS_BYTES * 8) + b"}"
        )
    return buffer.getvalue()


@respx.mock
@pytest.mark.parametrize(
    "content",
    [
        pytest.param(library_3mf(sliced=True), id="sliced"),
        pytest.param(_settings_bomb(), id="settings-past-the-cap"),
    ],
)
def test_an_already_sliced_library_file_prints_as_it_is_and_warns(
    client: TestClient, content: bytes
) -> None:
    """A file with gcode in it cannot state a flow, nor can one whose settings inflate
    past the cap: it is sliced as it stands, taken as Standard on both sides, so each
    High Flow nozzle mounted is warned of."""
    configure(client)
    one_color(89)
    upload = flow_copy_routes()
    library_file(89, content=content)
    run_routes()
    both_sides_high_flow()
    sliced = slice_routes()
    queue_route()

    response = run_library(client, 89, json=high_flow_run())

    assert response.status_code == 200, response.text
    assert "/library/files/89/slice" in str(sliced.calls.last.request.url)
    assert not upload.called
    assert len(hf_mounted(response.json()["warnings"])) == 2


@respx.mock
def test_a_standard_library_print_is_laid_out_too(client: TestClient) -> None:
    """B3: Standard is laid out like any other choice, so the slice states the side
    offered and the flow on both sides rather than leaving them to the slicer."""
    configure(client)
    one_color(89)
    upload = flow_copy_routes()
    download = library_file(89)
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = run_library(
        client,
        89,
        json={
            **body(nozzles=[{"size": "0.4"}], tier="standard"),
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
        },
    )

    assert response.status_code == 200, response.text
    assert "/library/files/141/slice" in str(sliced.calls.last.request.url)
    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert settings["nozzle_volume_type"] == ["Standard", "Standard"]
    assert "extruder_nozzle_stats" in settings
    assert download.call_count == 1


def scadbuddy_3mf(tmp_path: Path) -> bytes:
    """A 3MF ScadBuddy rendered, as someone saved one into the library."""
    out = tmp_path / "critter.3mf"
    write_bambu_3mf(
        [ColourPart(1, "Color 1", "#43A047", trimesh.creation.box(extents=(10, 10, 4)))],
        out,
        thumbnails=None,
        model_name="critter",
    )
    return out.read_bytes()


@respx.mock
def test_a_library_print_into_a_project_is_filed_there_and_remembered(
    client: TestClient, tmp_path: Path
) -> None:
    """#1752 (B2, R3): the copy is replated for the printer and nozzle chosen, filed in
    the project's folder (#79) under the file's own name, recorded under the file, and
    the project remembers what it printed on (#317), exactly as an output's print."""
    configure(client)
    one_color(89)
    respx.get(f"{API}/library/folders/by-project/7").mock(
        return_value=httpx.Response(200, json=[{"id": 9, "name": "Kids' room", "project_id": 7}])
    )
    respx.get(f"{API}/library/files", params={"folder_id": "9"}).mock(
        return_value=httpx.Response(200, json=[])
    )
    upload = flow_copy_routes()
    library_file(89, content=scadbuddy_3mf(tmp_path))
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = run_library(
        client,
        89,
        json={
            **body(),
            "project_id": 7,
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["folder_id"] == 9
    assert upload.calls.last.request.url.params["folder_id"] == "9"
    assert uploaded_name(upload) == "file-89.3mf"
    assert "/library/files/141/slice" in str(sliced.calls.last.request.url)
    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert settings["nozzle_diameter"] == ["0.2"]
    assert settings["filament_colour"] == ["#688197"]
    uploads = _state(client).uploads
    assert [copy.id for copy in asyncio.run(uploads.for_output("library:89"))] == [141]
    assert asyncio.run(uploads.project_target(7)) == ProjectTarget(
        printer_id=1, nozzle_diameter="0.2"
    )


# --- #1753: viewing a library file as an output is viewed ---------------------------------


@respx.mock
def test_a_library_files_plate_is_served_as_a_preview_mesh(
    client: TestClient, tmp_path: Path
) -> None:
    configure(client)
    library_file(89, content=scadbuddy_3mf(tmp_path))

    preview = client.get("/api/v1/print/library/89/preview.glb", params={"plate": 1})
    missing = client.get("/api/v1/print/library/89/preview.glb", params={"plate": 3})

    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"] == "model/gltf-binary"
    assert preview.content[:4] == b"glTF"
    assert missing.status_code == 404, missing.text


@respx.mock
def test_a_library_file_with_no_mesh_to_read_is_a_422_saying_why(client: TestClient) -> None:
    configure(client)
    library_file(89, file_type="gcode.3mf", content=library_3mf(sliced=True))

    preview = client.get("/api/v1/print/library/89/preview.glb")

    assert preview.status_code == 422, preview.text
    assert "sliced already" in preview.json()["detail"]


@respx.mock
def test_the_library_file_itself_is_served(client: TestClient) -> None:
    configure(client)
    library_file(89, content=b"the file's bytes")

    served = client.get("/api/v1/print/library/89/file")

    assert served.status_code == 200, served.text
    assert served.content == b"the file's bytes"


@respx.mock
def test_the_library_file_is_always_a_download_never_a_page(client: TestClient) -> None:
    """Whatever type Bambuddy names, the file is never rendered on ScadBuddy's origin."""
    configure(client)
    respx.get(f"{API}/library/files/89/download").mock(
        return_value=httpx.Response(
            200,
            content=b"<svg onload='alert(1)'/>",
            headers={"content-type": "image/svg+xml", "content-disposition": "inline"},
        )
    )

    served = client.get("/api/v1/print/library/89/file")

    assert served.status_code == 200, served.text
    assert served.headers["content-type"] == "application/octet-stream"
    assert served.headers["content-disposition"].startswith("attachment")
    assert served.headers["x-content-type-options"] == "nosniff"
