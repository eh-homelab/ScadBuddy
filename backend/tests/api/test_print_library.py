"""#313 — printing a file already in Bambuddy's library through the Print dialog, as
any print is (#1752): only how its 3MF is obtained differs from an output's."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import logging
import time
import zipfile
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
from typing import Any, cast

import anyio
import httpx
import pytest
import respx
import trimesh
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.activity.index import ActivityIndex
from scadbuddy.api import library_print
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.library_print import _kept_as_sent
from scadbuddy.bambuddy import catalogue, clearance, print_run, print_source
from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.filaments import MAX_PLATES
from scadbuddy.bambuddy.filaments3mf import SlicedProfile
from scadbuddy.bambuddy.models import NozzleInfo, Printer, PrinterStatus
from scadbuddy.bambuddy.print_run import PreparedPlates, PrintRunRequest
from scadbuddy.bambuddy.print_source import LibrarySource, ReadOutOfMemoryError
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy, ProjectTarget
from scadbuddy.render.bambu3mf import (
    MAX_SETTINGS_BYTES,
    laid_out_plates,
    layout_of,
    write_bambu_3mf,
)
from scadbuddy.render.split import ColourPart
from scadbuddy.store.download_cache import DownloadCache, TooLargeError
from tests.api.test_print_filaments import queue_route, slice_kept, slice_routes
from tests.api.test_print_progress import watched as watched  # the fixture, shared
from tests.api.test_print_run_choices import body, follow_run, run_routes
from tests.api.test_print_runs import Gate, gated_slice_routes
from tests.api.test_print_runs import gate as gate  # the fixture, shared
from tests.api.test_send import BASE, _uploaded_3mf, configure, make_output
from tests.api.test_settings_runtime import _state
from tests.bambuddy.conftest import recording
from tests.support.operations import press
from tests.test_clearance import two_objects_3mf

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


def library_3mf(*, sliced: bool = False, settings: dict[str, Any] | None = None) -> bytes:
    """A 3MF as Bambuddy's library holds it: unsliced, or with a plate's gcode in it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("3D/3dmodel.model", "<model/>")
        archive.writestr("Metadata/project_settings.config", json.dumps(settings or STUB_SETTINGS))
        if sliced:
            archive.writestr("Metadata/plate_1.gcode", "; sliced\n")
    return buffer.getvalue()


def library_file(
    file_id: int = 89,
    *,
    file_type: str = "3mf",
    plates: str = "library-plates-single.json",
    content: bytes | None = None,
    file_hash: str | None = None,
) -> respx.Route:
    """The file, its plates and its bytes (an unsliced 3MF unless ``content`` says
    otherwise); returns the download's route. Bambuddy states no hash for it unless
    ``file_hash`` names one."""
    respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": file_id,
                "filename": f"file-{file_id}.{file_type}",
                "file_type": file_type,
                "file_hash": file_hash,
            },
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
    # #2671: a sliced file prints as it is, and says it is sliced (so it is never arranged).
    assert sliced and all(row["printable"] and row["sliced"] for row in sliced)
    assert plain["hidden"] == len(every["files"]) - len(plain["files"])
    # The folder tree arrives flattened, each with its depth.
    assert {"Supplies": 0, "Storage": 1}.items() <= {
        row["name"]: row["depth"] for row in plain["folders"]
    }.items()
    # #2724: a folder linked to a Bambuddy project names it; that folder is the
    # project's file list, which a batch print can take. Any other folder names none.
    by_name = {row["name"]: row for row in plain["folders"]}
    assert (by_name["Raegan"]["project_id"], by_name["Raegan"]["project_name"]) == (
        1,
        "Reagan Keychain",
    )
    assert by_name["Supplies"]["project_id"] is None


@respx.mock
def test_a_search_finds_files_in_every_folder_from_one_kept_read(client: TestClient) -> None:
    """#3630: the whole library in one read, kept; ``fresh`` reads it again."""
    configure(client)
    files = listing_routes(recording("library-files-folder.json"))

    found = client.get("/api/v1/print/library/search", params={"q": "dry pods"}).json()
    again = client.get(
        "/api/v1/print/library/search", params={"q": "desiccant", "all": "true"}
    ).json()

    assert files.call_count == 1
    assert files.calls.last.request.url.params["include_root"] == "false"
    assert [hit["filename"] for hit in found["files"]] == [
        "AMS_2_Pro_Dry_Pods_-_ABS.3mf",
        "AMS_2_Pro_Dry_Pods_-_PETG.3mf",
        "AMS_2_Pro_Dry_Pods_-_PLA.3mf",
    ]
    assert found["hidden"] == 1  # the sliced Dry Pods file, only under Advanced
    assert {tuple(hit["folder_path"]) for hit in found["files"]} == {("Supplies", "Storage")}
    assert "Desiccant_Box.stl" in [hit["filename"] for hit in again["files"]]

    # ``fresh`` shares a read that started within 2 s of it (`test_library_search` pins
    # the read it starts past that), so a burst of fresh searches is one read.
    client.get("/api/v1/print/library/search", params={"q": "pods", "fresh": "true"})

    assert files.call_count == 1


@respx.mock
def test_a_search_hit_scadbuddy_uploaded_names_its_output(
    client: TestClient, app: FastAPI, model: str
) -> None:
    """As the listing does (#1864): Arrange reads a found file's objects through it."""
    configure(client)
    output_id = make_output(client, model)
    listing_routes(recording("library-files-folder.json"))
    state: AppState = getattr(app.state, STATE_ATTR)
    asyncio.run(state.uploads.record(output_id, LibraryCopy(id=48, folder_id=4, target_key="k")))

    hits = client.get("/api/v1/print/library/search", params={"q": "dry pods"}).json()["files"]

    assert {hit["id"]: hit["output_id"] for hit in hits} == {59: None, 52: None, 48: output_id}


@respx.mock
def test_a_search_answers_as_the_listing_does_when_bambuddy_is_unset_or_down(
    client: TestClient,
) -> None:
    """The search's read runs in a shielded task; its problem is still the listing's."""
    unset_search = client.get("/api/v1/print/library/search", params={"q": "pods"})
    unset_listing = client.get("/api/v1/print/library")
    assert (unset_search.status_code, unset_search.json()["type"]) == (
        unset_listing.status_code,
        unset_listing.json()["type"],
    )
    assert unset_search.status_code == 409

    configure(client)
    respx.get(f"{API}/library/folders").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(f"{API}/library/files/").mock(side_effect=httpx.ConnectError("refused"))

    down_search = client.get("/api/v1/print/library/search", params={"q": "pods"})
    down_listing = client.get("/api/v1/print/library")

    assert down_search.status_code == down_listing.status_code == 502
    assert down_search.json()["type"] == down_listing.json()["type"]


@respx.mock
def test_a_file_scadbuddy_uploaded_names_its_output(
    client: TestClient, app: FastAPI, model: str
) -> None:
    """#1864: what Arrange reads a generated library file's objects through."""
    configure(client)
    output_id = make_output(client, model)
    listing_routes(recording("library-files-root.json"))
    first = client.get("/api/v1/print/library").json()["files"][0]["id"]
    state: AppState = getattr(app.state, STATE_ATTR)
    asyncio.run(
        state.uploads.record(output_id, LibraryCopy(id=first, folder_id=None, target_key="k"))
    )

    files = client.get("/api/v1/print/library").json()["files"]

    assert {row["id"]: row["output_id"] for row in files if row["output_id"]} == {first: output_id}
    assert len(files) > 1


@respx.mock
def test_a_file_whose_output_was_deleted_names_no_output(client: TestClient, app: FastAPI) -> None:
    """#2138: the upload row outlives its output. Named, the file would be read as an
    output's copy, and Arrange would 404 it rather than read it as a plain file."""
    configure(client)
    listing_routes(recording("library-files-root.json"))
    first = client.get("/api/v1/print/library").json()["files"][0]["id"]
    state: AppState = getattr(app.state, STATE_ATTR)
    asyncio.run(
        state.uploads.record("a" * 32, LibraryCopy(id=first, folder_id=None, target_key="k"))
    )

    files = client.get("/api/v1/print/library").json()["files"]

    assert [row["output_id"] for row in files if row["id"] == first] == [None]


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
    # One plate is sliced as the whole file (#2180).
    assert json.loads(sliced.calls.last.request.content)["plate"] == 0
    assert "/library/files/141/slice" in str(sliced.calls.last.request.url)
    assert uploaded_name(upload) == "file-46 (ScadBuddy).3mf"
    assert layout_of(_uploaded_3mf(upload)) == "scadbuddy"
    # Laying out never adds a plate, so slicing the copy whole slices exactly the one
    # plate the library file has (#2227).
    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        assert len(laid_out_plates(archive)) == 1


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
def test_a_library_run_reads_the_project_list_once(client: TestClient) -> None:
    """#4008 review — a library file's check resolves the default project too, and the
    plan reuses it: a Settings default Bambuddy no longer has is one read, then the
    inbox."""
    configure(client)
    saved = client.put("/api/v1/settings", json={"last_project_id": 99})
    assert saved.status_code == 200, saved.text
    projects = respx.get(f"{API}/projects/").mock(return_value=httpx.Response(200, json=[]))
    one_color(89)
    upload = flow_copy_routes()
    library_file(89)
    run_routes()
    slice_routes()
    queue_route()

    response = run_library(
        client,
        89,
        json={**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}},
    )

    assert response.status_code == 200, response.text
    assert upload.calls.last.request.url.params["folder_id"] == "2"
    assert projects.call_count == 1


@respx.mock
def test_a_library_print_queues_with_the_options_remembered_for_that_file(
    client: TestClient,
) -> None:
    """B6 (#1754): a library file's own options scope, ``library:<file id>``, is merged
    over the printer's as a model's is; another file's is not."""
    configure(client)
    one_color(89)
    flow_copy_routes()
    library_file(89)
    run_routes()
    slice_routes()
    queued = queue_route()
    options = "/api/v1/settings/print-options"
    for remembered in (
        {"scope": "printer", "key": "1", "options": {"timelapse": True, "use_ams": False}},
        {"scope": "model", "key": "library:89", "options": {"timelapse": False}},
        {"scope": "model", "key": "library:67", "options": {"use_ams": True}},
    ):
        assert client.put(options, json=remembered).status_code == 200

    response = run_library(
        client,
        89,
        json={**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}},
    )

    assert response.status_code == 200, response.text
    sent = json.loads(queued.calls.last.request.content)
    assert sent["timelapse"] is False
    assert sent["use_ams"] is False


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
def test_a_deleted_files_choices_are_its_404_whatever_else_fails(client: TestClient) -> None:
    """#2509 review: the file, the printers and the presets are read together now, and
    the file's 404 still beats the printers' failure, as when it was read first."""
    configure(client)
    respx.get(f"{API}/library/files/89").mock(
        return_value=httpx.Response(404, json={"detail": "File not found"})
    )
    respx.get(f"{API}/printers/").mock(return_value=httpx.Response(500))
    respx.get(f"{API}/slicer/presets").mock(return_value=httpx.Response(500))
    respx.get(f"{API}/local-presets/").mock(return_value=httpx.Response(500))

    response = client.get("/api/v1/print/library/89/choices")

    assert response.status_code == 404, response.text
    assert response.json()["type"].endswith("/bambuddy-not-found")


def sliced_3mf(
    *,
    printer_model: str = "Bambu Lab H2C",
    nozzle: str = "0.4",
    plates: tuple[int, ...] = (1,),
) -> bytes:
    """A sliced file as Bambu Studio exports one (#2671): G-code for ``plates``, and
    ``slice_info.config`` saying what each was sliced for and which filament it uses."""
    info = "".join(
        f"""<plate>
          <metadata key="index" value="{index}"/>
          <metadata key="nozzle_diameters" value="{nozzle}"/>
          <metadata key="prediction" value="{1800 * index}"/>
          <metadata key="weight" value="{4.5 * index}"/>
          <filament id="1" type="PLA" color="#0047BB" used_m="1.2" used_g="{4.5 * index}"/>
        </plate>"""
        for index in plates
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("3D/3dmodel.model", "<model/>")
        archive.writestr(
            "Metadata/project_settings.config",
            json.dumps(
                {
                    "printer_model": printer_model,
                    "printer_settings_id": f"{printer_model} {nozzle} nozzle",
                    "print_settings_id": "0.20mm Standard @BBL H2C",
                    "curr_bed_type": "Textured PEI Plate",
                    "nozzle_diameter": [nozzle, nozzle],
                    "filament_colour": ["#0047BB"],
                    "filament_type": ["PLA"],
                }
            ),
        )
        archive.writestr("Metadata/slice_info.config", f"<config>{info}</config>")
        for index in plates:
            archive.writestr(f"Metadata/plate_{index}.gcode", "; sliced\n")
    return buffer.getvalue()


def sliced_file(file_id: int = 104, content: bytes | None = None) -> respx.Route:
    """A sliced library file with a hash, so its slots and profile are read from it."""
    return library_file(
        file_id,
        file_type="gcode.3mf",
        content=sliced_3mf() if content is None else content,
        file_hash=hashlib.sha256(f"sliced-{file_id}".encode()).hexdigest(),
    )


SLICED_RUN = {
    "printer_id": 1,
    "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
    "choices": {"nozzles": [{"size": "0.4"}], "tier": "standard"},
}


@respx.mock
def test_a_sliced_run_of_two_copies_queues_a_quantity_of_two(client: TestClient) -> None:
    """#3437: a sliced file's copies come from the request through the print options
    (``request_scope``), as any run's do."""
    configure(client)
    sliced_file(104)
    run_routes()
    queued = queue_route()

    response = run_library(client, 104, json={**SLICED_RUN, "copies": 2})

    assert response.status_code == 200, response.text
    assert response.json()["copies"] == 2
    assert json.loads(queued.calls.last.request.content)["quantity"] == 2


@respx.mock
def test_an_unprintable_files_choices_are_its_422_whatever_else_fails(client: TestClient) -> None:
    """#2532: as the 404 above, an unprintable file's 422 beats the printers' and the
    presets' failures, though they are read beside it."""
    configure(client)
    library_file(105, file_type="step")
    respx.get(f"{API}/printers/").mock(return_value=httpx.Response(500))
    respx.get(f"{API}/slicer/presets").mock(return_value=httpx.Response(500))
    respx.get(f"{API}/local-presets/").mock(return_value=httpx.Response(500))

    response = client.get("/api/v1/print/library/105/choices")

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "ScadBuddy prints only 3MF and STL files from the library, and file-105.step is a step."
    )


@respx.mock
def test_a_sliced_file_is_queued_as_it_is_with_nothing_sliced_or_uploaded(
    client: TestClient,
) -> None:
    """#2671: the file itself is queued, with the spools chosen for its slots and the
    options remembered for it (a manual start among them); nothing is uploaded or
    sliced, and the user's file is only read."""
    configure(client)
    upload = flow_copy_routes()
    sliced_file(104)
    run_routes()
    sliced = slice_routes()
    queued = queue_route()
    assert (
        client.put(
            "/api/v1/settings/print-options",
            json={"scope": "model", "key": "library:104", "options": {"manual_start": True}},
        ).status_code
        == 200
    )

    response = run_library(client, 104, json=SLICED_RUN)

    assert response.status_code == 200, response.text
    assert response.json()["library_file_id"] == 104
    assert response.json()["sliced_library_file_id"] == 104
    assert response.json()["slice_job_id"] is None
    assert not sliced.called and not upload.called
    sent = json.loads(queued.calls.last.request.content)
    assert sent["library_file_id"] == 104
    assert sent["plate_id"] == 1
    assert sent["manual_start"] is True
    assert sent["filament_overrides"][0]["slot_id"] == 1
    assert not [
        call
        for call in respx.calls
        if "/library/files/104" in call.request.url.path and call.request.method != "GET"
    ]


@respx.mock
def test_a_sliced_files_choices_say_what_it_was_sliced_for(client: TestClient) -> None:
    """#2671: the dialog shows the baked-in slicer choices read-only, and its slots are
    the ones the file was sliced with."""
    configure(client)
    sliced_file(104, sliced_3mf(plates=(1, 3)))
    run_routes()

    choices = client.get("/api/v1/print/library/104/choices")

    assert choices.status_code == 200, choices.text
    profile = choices.json()["sliced"]
    assert profile["printer_model"] == "Bambu Lab H2C"
    assert profile["nozzle_diameters"] == ["0.4"]
    assert profile["process"] == "0.20mm Standard @BBL H2C"
    assert profile["bed_type"] == "Textured PEI Plate"
    assert [(p["index"], p["print_seconds"], p["weight_g"]) for p in profile["plates"]] == [
        (1, 1800, 4.5),
        (3, 5400, 13.5),
    ]
    assert [slot["slot_id"] for slot in choices.json()["filaments"]["slots"]] == [1]


@respx.mock
@pytest.mark.parametrize(
    ("content", "plate_id", "kind", "warning", "queued_plate", "held"),
    [
        pytest.param(
            sliced_3mf(printer_model="Bambu Lab X1 Carbon"),
            1,
            "sliced-other-model",
            "Sliced for a Bambu Lab X1 Carbon; 3DP-31B-598 is a H2C. Held for a manual start.",
            1,
            True,
            id="another-model",
        ),
        pytest.param(
            sliced_3mf(nozzle="0.6"),
            1,
            "sliced-nozzle-missing",
            "Sliced for a 0.6 mm nozzle; 0.2, 0.4 mm is mounted on 3DP-31B-598."
            " Held for a manual start: swap it before you start the print.",
            1,
            True,
            id="another-nozzle",
        ),
        pytest.param(
            sliced_3mf(plates=(1,)),
            2,
            "sliced-plate-missing",
            "This file holds no plate 2, so plate 1 is queued instead. Held for a manual start.",
            1,
            True,  # another plate than asked for never starts unattended (#3436)
            id="no-such-plate",
        ),
        pytest.param(
            b"not a 3mf",
            1,
            "sliced-unchecked",
            "ScadBuddy could not read this sliced file's settings, so its printer model and"
            " nozzle were not checked. Held for a manual start: check them before you start"
            " the print.",
            1,
            True,  # nothing was checked, so it is held (#3436)
            id="unreadable-profile",
        ),
    ],
)
def test_a_sliced_file_that_does_not_fit_still_queues_with_a_warning(
    client: TestClient,
    content: bytes,
    plate_id: int,
    kind: str,
    warning: str,
    queued_plate: int,
    held: bool,
) -> None:
    """#2671: a sliced file is never refused. A mismatch is a warning in the check and
    on the run, so the user sets it right before starting the print. #2954: one sliced
    for another model or nozzle is held for a manual start even though the options
    remembered for it say to start at once, so it cannot start before anyone reads the
    warning."""
    configure(client)
    sliced_file(104, content)
    run_routes()
    queued = queue_route()
    assert (
        client.put(
            "/api/v1/settings/print-options",
            json={"scope": "model", "key": "library:104", "options": {"manual_start": False}},
        ).status_code
        == 200
    )

    check = client.post(
        "/api/v1/print/library/104/check", json={**SLICED_RUN, "plate_id": plate_id}
    )
    run = run_library(client, 104, json={**SLICED_RUN, "plate_id": plate_id})

    assert check.status_code == 200, check.text
    assert check.json()["errors"] == []
    assert {"kind": kind, "slot_id": None, "message": warning} in check.json()["warnings"]
    assert run.status_code == 200, run.text
    assert any(w["kind"] == kind and w["message"] == warning for w in run.json()["warnings"])
    assert queued.call_count == 1
    sent = json.loads(queued.calls.last.request.content)
    assert sent["plate_id"] == queued_plate
    assert sent["manual_start"] is held


@respx.mock
@pytest.mark.parametrize(
    ("plate_ids", "warning", "queued_plates"),
    [
        ([3, 2, 1], "This file holds no plate 2, so it is left out.", [1, 3]),
        ([1, 2, 4], "This file holds no plates 2 and 4, so they are left out.", [1]),
        # One chosen plate is `plate_id` (#3768 review): the file's first, said the same.
        ([2], "This file holds no plate 2, so plate 1 is queued instead.", [1]),
    ],
)
def test_a_sliced_file_leaves_out_chosen_plates_it_does_not_hold(
    client: TestClient, plate_ids: list[int], warning: str, queued_plates: list[int]
) -> None:
    """#3762 review: of several chosen plates, the ones a sliced file holds are queued
    and the rest are named in a warning, never refused (#2671). Like every sliced
    mismatch, each is held for a manual start (#2954)."""
    warning += " Held for a manual start."
    configure(client)
    sliced_file(104, sliced_3mf(plates=(1, 3)))
    run_routes()
    queued = queue_route()
    request = {**SLICED_RUN, "plate_ids": plate_ids}

    check = client.post("/api/v1/print/library/104/check", json=request)
    run = run_library(client, 104, json=request)

    assert check.status_code == 200, check.text
    assert check.json()["errors"] == []
    assert {"kind": "sliced-plate-missing", "slot_id": None, "message": warning} in check.json()[
        "warnings"
    ]
    assert run.status_code == 200, run.text
    assert any(w["message"] == warning for w in run.json()["warnings"])
    sent = [json.loads(call.request.content) for call in queued.calls]
    assert sorted(body["plate_id"] for body in sent) == queued_plates
    assert all(body["manual_start"] is True for body in sent)


@respx.mock
def test_a_sliced_file_that_fits_checks_clean(client: TestClient) -> None:
    """#2671: an H2C file for the 0.4 the printer has, on a plate it holds: nothing to
    warn of, though the printer is not one ScadBuddy chooses presets for by name."""
    configure(client)
    sliced_file(104)
    run_routes()

    check = client.post("/api/v1/print/library/104/check", json=SLICED_RUN)

    assert check.status_code == 200, check.text
    assert check.json()["errors"] == []
    assert check.json()["warnings"] == []


@respx.mock
def test_a_sliced_files_checks_say_a_tower_choice_is_not_applied(client: TestClient) -> None:
    """#3648 review: a sliced file queues as it is, so a prime tower chosen for it is
    said not to apply rather than silently ignored."""
    configure(client)
    sliced_file(104)
    run_routes()

    request = {**SLICED_RUN, "prime_tower": {"enabled": False}}
    check = client.post("/api/v1/print/library/104/check", json=request)

    assert check.status_code == 200, check.text
    [warning] = check.json()["warnings"]
    assert warning["kind"] == "prime-tower-not-applied"
    assert "already sliced" in warning["message"]


@respx.mock
def test_a_sliced_file_is_never_preview_sliced(client: TestClient) -> None:
    configure(client)
    sliced_file(104)
    run_routes()
    sliced = slice_routes()

    started = client.post(
        "/api/v1/print/library/104/preview-slice", json=SLICED_RUN, headers=press()
    )

    # #2700: the route answers 202 once the operation is recorded; the refusal is the
    # operation's failure, so it is followed until it settles.
    assert started.status_code == 202, started.text
    op = started.json()
    deadline = time.monotonic() + 30
    while op["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
        op = client.get(f"/api/v1/operations/{op['id']}").json()
    assert op["status"] == "failed", op
    assert "sliced already" in json.dumps(op["error"])
    assert not sliced.called


@respx.mock
def test_the_dialogs_background_slice_looks_up_no_earlier_slice(client: TestClient) -> None:
    """#2691 review: the background slice planned the run, which looked up the recorded
    slices (GETs) and then sliced anyway, as it slices to show the toolpath."""
    configure(client)
    one_color(89)
    upload = flow_copy_routes()
    library_file(89)
    run_routes()
    sliced = slice_routes(sliced_id=77)
    queue_route()
    request = {**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}}
    first = run_library(client, 89, json={**request, "request_id": "first"})
    assert first.status_code == 200, first.text
    copy_kept(upload)
    kept = slice_kept()
    before = kept.call_count

    started = client.post("/api/v1/print/library/89/preview-slice", json=request, headers=press())

    assert started.status_code == 202, started.text
    op = started.json()
    deadline = time.monotonic() + 30
    while op["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
        op = client.get(f"/api/v1/operations/{op['id']}").json()
    assert op["status"] == "succeeded", op
    assert sliced.call_count == 2
    assert kept.call_count == before


@respx.mock
def test_a_preview_of_a_plate_with_no_slice_plan_fails_saying_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2691 review: a plate with nothing to slice is refused with a message, not an
    assert python -O would strip into an AttributeError."""
    from scadbuddy.bambuddy.print_run import plan_run as real

    async def planless(*args: Any, **kwargs: Any) -> Any:
        planned = await real(*args, **kwargs)
        return planned.model_copy(
            update={"plates": [p.model_copy(update={"plan": None}) for p in planned.plates]}
        )

    monkeypatch.setattr("scadbuddy.bambuddy.preview.plan_run", planless)
    configure(client)
    one_color(89)
    flow_copy_routes()
    library_file(89)
    run_routes()
    sliced = slice_routes()
    request = {**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}}

    started = client.post("/api/v1/print/library/89/preview-slice", json=request, headers=press())

    assert started.status_code == 202, started.text
    op = started.json()
    deadline = time.monotonic() + 30
    while op["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
        op = client.get(f"/api/v1/operations/{op['id']}").json()
    assert op["status"] == "failed", op
    assert "nothing to slice" in json.dumps(op["error"])
    assert not sliced.called


@respx.mock
def test_a_second_print_of_the_same_choices_queues_scadbuddys_own_slice(
    client: TestClient,
) -> None:
    """#2671: ScadBuddy recorded the slice of its copy with these presets, and Bambuddy
    still has it, so printing the same again queues that slice rather than slicing."""
    configure(client)
    one_color(89)
    upload = flow_copy_routes()
    library_file(89)
    run_routes()
    sliced = slice_routes(sliced_id=77)
    queued = queue_route()
    slice_kept()
    request = {**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}}

    first = run_library(client, 89, json={**request, "request_id": "first"})
    copy_kept(upload)
    second = run_library(client, 89, json={**request, "request_id": "second"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert sliced.call_count == 1
    assert second.json()["sliced_library_file_id"] == 77
    assert second.json()["slice_job_id"] is None
    assert [json.loads(call.request.content)["library_file_id"] for call in queued.calls] == [
        77,
        77,
    ]
    # #2955: said, so no slice job does not read as a slice skipped by mistake.
    reused = [w for w in second.json()["warnings"] if w["kind"] == "slice-reused"]
    assert reused == [
        {
            "kind": "slice-reused",
            "slot_id": None,
            "message": "Plate 1 queues ScadBuddy's earlier slice of the same choices;"
            " nothing was sliced again.",
        }
    ]
    assert not [w for w in first.json()["warnings"] if w["kind"] == "slice-reused"]


@respx.mock
def test_a_recorded_slice_whose_id_now_holds_another_file_is_sliced_again(
    client: TestClient,
) -> None:
    """#2691 review (Blocking): Bambuddy hands a deleted file's id to the next file, so a
    slice matched by id alone could queue another model's G-code. A reuse needs the
    file at the id to have the hash the run recorded; another file there is sliced
    again."""
    configure(client)
    one_color(89)
    upload = flow_copy_routes()
    library_file(89)
    run_routes()
    sliced = slice_routes(sliced_id=77)
    queued = queue_route()
    slice_kept(file_hash="a" * 64)
    request = {**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}}
    first = run_library(client, 89, json={**request, "request_id": "first"})
    assert first.status_code == 200, first.text
    copy_kept(upload)
    # The slice was deleted, and its id went to another file: still a gcode.3mf.
    slice_kept(file_hash="b" * 64)

    second = run_library(client, 89, json={**request, "request_id": "second"})

    assert second.status_code == 200, second.text
    assert sliced.call_count == 2
    assert second.json()["slice_job_id"] is not None
    assert not [w for w in second.json()["warnings"] if w["kind"] == "slice-reused"]
    assert queued.call_count == 2


@respx.mock
def test_a_slice_whose_hash_cannot_be_read_still_queues_and_is_never_reused(
    client: TestClient,
) -> None:
    """#2691 review: the hash is read once the plate is queued, so a failed read must
    not fail the run; the slice is then only never reused."""
    configure(client)
    one_color(89)
    upload = flow_copy_routes()
    library_file(89)
    run_routes()
    sliced = slice_routes(sliced_id=77)
    queue_route()
    respx.get(f"{API}/library/files/77").mock(return_value=httpx.Response(500))
    request = {**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}}
    first = run_library(client, 89, json={**request, "request_id": "first"})
    assert first.status_code == 200, first.text
    copy_kept(upload)
    slice_kept()

    second = run_library(client, 89, json={**request, "request_id": "second"})

    assert second.status_code == 200, second.text
    assert sliced.call_count == 2


@respx.mock
def test_a_reprint_looks_up_every_plates_recorded_slice_at_once(client: TestClient) -> None:
    """#2691 review: each plate's recorded slices are a GET each; a reprint of many
    plates reads them together rather than one plate after another."""
    configure(client)
    one_color(67)
    upload = flow_copy_routes()
    library_file(67, plates="library-plates-multi.json")
    run_routes()
    # Each plate's slice its own job and its own file, so each records its own slice.
    jobs = iter([9, 10])
    sliced = respx.route(method="POST", path__regex=r"/api/v1/library/files/\d+/slice").mock(
        side_effect=lambda request: httpx.Response(
            200, json={"job_id": next(jobs), "status": "pending"}
        )
    )
    for job_id, file_id in ((9, 77), (10, 78)):
        respx.get(f"{API}/slice-jobs/{job_id}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": job_id,
                    "status": "completed",
                    "result": {"library_file_id": file_id, "filament_used_g": 12.0},
                },
            )
        )
    queue_route()
    request = {
        **body(),
        "all_plates": True,
        "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
    }
    in_flight, most = 0, 0

    async def kept(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, most
        in_flight += 1
        most = max(most, in_flight)
        await asyncio.sleep(0.2)
        in_flight -= 1
        file_id = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            200,
            json={
                "id": file_id,
                "filename": "sliced.gcode.3mf",
                "file_type": "gcode.3mf",
                "file_hash": f"{file_id:064x}",
            },
        )

    # Read by the first run for each slice's hash (#2691 review), then by the reprint.
    respx.get(url__regex=rf"{API}/library/files/7[78]$").mock(side_effect=kept)
    first = run_library(client, 67, json={**request, "request_id": "first"})
    copy_kept(upload)
    most = 0

    second = run_library(client, 67, json={**request, "request_id": "second"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert sliced.call_count == 2  # the first run's two plates; the second slices none
    assert most == 2


@respx.mock
def test_a_recorded_slice_bambuddy_no_longer_has_is_sliced_again(client: TestClient) -> None:
    configure(client)
    one_color(89)
    upload = flow_copy_routes()
    library_file(89)
    run_routes()
    sliced = slice_routes(sliced_id=77)
    queue_route()
    respx.get(f"{API}/library/files/77").mock(
        return_value=httpx.Response(404, json={"detail": "File not found"})
    )
    request = {**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}}

    first = run_library(client, 89, json={**request, "request_id": "first"})
    copy_kept(upload)
    second = run_library(client, 89, json={**request, "request_id": "second"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert sliced.call_count == 2


@respx.mock
@pytest.mark.parametrize("prune_fails", [False, True], ids=["pruned", "prune-fails"])
def test_a_reused_slice_deleted_before_it_is_queued_is_sliced_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, prune_fails: bool
) -> None:
    """#2955: the recorded slice was still in Bambuddy when the reprint was planned, and
    gone by its POST /queue/, which Bambuddy answers 400 "Library file not found"
    (``print_queue.py`` at maziggy/bambuddy@943f3587). Read back, it is a 404, so the
    plate is sliced again and that slice queued; the run does not fail on it, even when
    forgetting the gone slice fails (#3495 review)."""
    if prune_fails:

        async def down(*_: object) -> None:
            raise RuntimeError("the database is down")

        monkeypatch.setattr(BambuddyUploadStore, "forget_gone_slice", down)
    configure(client)
    one_color(89)
    upload = flow_copy_routes()
    library_file(89)
    run_routes()
    sliced = slice_routes(sliced_id=77, job_id=9)
    queued = queue_route()
    request = {**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}}
    first = run_library(client, 89, json={**request, "request_id": "first"})
    assert first.status_code == 200, first.text
    copy_kept(upload)

    kept = {"id": 77, "filename": "sliced.gcode.3mf", "file_type": "gcode.3mf"}
    respx.get(f"{API}/library/files/77").mock(
        side_effect=[
            httpx.Response(200, json={**kept, "file_hash": f"{77:064x}"}),  # the plan
            httpx.Response(404, json={"detail": "File not found"}),  # the read-back
        ]
    )
    sliced.mock(return_value=httpx.Response(200, json={"job_id": 10, "status": "pending"}))
    respx.get(f"{API}/slice-jobs/10").mock(
        return_value=httpx.Response(
            200,
            json={"id": 10, "status": "completed", "result": {"library_file_id": 78}},
        )
    )
    slice_kept(78)
    accepted = queued.return_value
    assert accepted is not None
    queued.mock(
        side_effect=[httpx.Response(400, json={"detail": "Library file not found"}), accepted]
    )
    second = run_library(client, 89, json={**request, "request_id": "second"})

    assert second.status_code == 200, second.text
    assert second.json()["sliced_library_file_id"] == 78
    assert second.json()["slice_job_id"] == 10
    sent = [json.loads(call.request.content)["library_file_id"] for call in queued.calls]
    assert sent == [77, 77, 78]
    assert sliced.call_count == 2
    # Forgotten as soon as it is found gone, not on the next reprint (#3495 review).
    copies = asyncio.run(_state(client).uploads.for_output("library:89"))
    assert [s.id for c in copies for s in c.sliced] == ([77, 78] if prune_fails else [78])


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
    # Its one plate, sliced as the whole file (#2180).
    assert sent["plate"] == 0 and len(sent["filament_presets"]) == 1
    assert client.get("/api/v1/print/library/70/plates").json() == []


@respx.mock
def test_a_one_plate_library_file_is_sliced_as_the_whole_file(client: TestClient) -> None:
    """#2180: Bambuddy repaints a slot it thinks the plate leaves unused whenever the
    slice names a plate, and it cannot see colour painted inline on a mesh, so queue 268's
    two-colour file sliced all green. ``plate: 0`` skips that substitution."""
    configure(client)
    one_color(89)
    flow_copy_routes()
    library_file(89, plates="library-plates-single.json")
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = run_library(
        client,
        89,
        json={**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}},
    )

    assert response.status_code == 200, response.text
    assert json.loads(sliced.calls.last.request.content)["plate"] == 0


@respx.mock
def test_a_plate_of_a_file_with_several_is_sliced_by_its_number(client: TestClient) -> None:
    """Plate 0 slices every plate, so a file with more than one keeps naming its plate."""
    configure(client)
    one_color(67)
    flow_copy_routes()
    library_file(67, plates="library-plates-multi.json")
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = run_library(
        client,
        67,
        json={
            **body(),
            "plate_id": 2,
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
        },
    )

    assert response.status_code == 200, response.text
    assert json.loads(sliced.calls.last.request.content)["plate"] == 2


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
def test_a_library_files_check_reads_the_dialogs_kept_catalogue(client: TestClient) -> None:
    """#4196 on a library file: only the first check reads ``/slicer/presets``."""
    configure(client)
    library_file(89)
    run_routes()
    presets = respx.get(f"{API}/slicer/presets").mock(
        return_value=httpx.Response(200, json=recording("slicer-presets-h2c.json"))
    )

    checks = [client.post("/api/v1/print/library/89/check", json=body()) for _ in "ab"]

    assert [check.status_code for check in checks] == [200, 200], checks[-1].text
    assert presets.call_count == 1


@respx.mock
def test_a_library_files_check_fetches_the_file_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4489: the check fetches the 3MF beside `prepare_run`; every read of it, the slot
    read's too (#4495), shares that one fetch, even one that starts before it ends."""
    configure(client)
    download = library_file(89)
    run_routes()
    fetches: list[int] = []
    fetch_3mf = LibrarySource.fetch_3mf

    async def counted(self: LibrarySource, bambuddy: BambuddyClient | None) -> bytes | None:
        fetches.append(self.file_id)
        return await fetch_3mf(self, bambuddy)

    monkeypatch.setattr(LibrarySource, "fetch_3mf", counted)
    request = {
        **body(),
        "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}, {"slot_id": 2, "spool_id": 10}]},
    }

    check = client.post("/api/v1/print/library/89/check", json=request)

    assert check.status_code == 200, check.text
    assert check.json()["errors"] == []
    assert fetches == [89]
    # #4495: the slot read reads this fetch's bytes rather than spooling its own.
    assert download.call_count == 1


@respx.mock
def test_a_checks_slots_are_cached_so_a_check_refused_early_downloads_nothing(
    client: TestClient,
) -> None:
    """#4553 review: the slots read from the check's fetch are cached, and a later check
    reads them there, so one that ends before it fetches (no printer) downloads nothing,
    as before #4495."""
    configure(client)
    content = library_3mf()
    download = library_file(89, content=content, file_hash=hashlib.sha256(content).hexdigest())
    run_routes()

    first = client.post("/api/v1/print/library/89/check", json=body())
    assert first.status_code == 200, first.text
    assert download.call_count == 1

    unprinted = client.post("/api/v1/print/library/89/check", json={**body(), "printer_id": None})
    assert unprinted.status_code == 200, unprinted.text
    assert download.call_count == 1


@respx.mock
def test_a_check_whose_fetch_runs_out_of_memory_is_refused_saying_how_to_bound_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#4562 review: the check's own fetch (its layout read) running out of memory is the
    check's refusal, as a library file's read is, never an unhandled 500. #4571 review:
    the slot read, which made that fetch first and read the slots from disk instead, left
    the layout read to fetch the file again; the one fetch's failure answers both."""
    configure(client)
    library_file(89)
    run_routes()
    fetches: list[int] = []

    async def out_of_memory(self: LibrarySource, bambuddy: BambuddyClient | None) -> bytes:
        fetches.append(self.file_id)
        raise MemoryError

    monkeypatch.setattr(LibrarySource, "fetch_3mf", out_of_memory)

    with caplog.at_level(logging.WARNING, logger=print_run.logger.name):
        check = client.post("/api/v1/print/library/89/check", json=body())

    assert check.status_code == 200, check.text
    assert check.json()["errors"] == [str(ReadOutOfMemoryError())]
    assert fetches == [89]
    [logged] = [r for r in caplog.records if "ran out of memory" in r.getMessage()]
    assert logged.exc_info is not None and logged.exc_info[0] is MemoryError


@respx.mock
def test_a_check_that_runs_out_of_memory_in_its_preparation_is_refused_too(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#4576 review: a read in `prepare_run` that runs out of memory is refused as the
    layout's own read is, not re-raised as a 500 by its `except BaseException`. It is
    logged with its traceback (#4587 review): the allocation may not have been a read.
    #4598: the read that fails is one `prepare_run` really makes, the kept preset
    catalogue's, not a stub of `prepare_run` itself."""
    configure(client)
    library_file(89)
    run_routes()
    loads: list[str] = []

    def out_of_memory(path: Path) -> None:
        loads.append(path.name)
        raise MemoryError

    monkeypatch.setattr(catalogue, "_load", out_of_memory)

    with caplog.at_level(logging.WARNING, logger=print_run.logger.name):
        check = client.post("/api/v1/print/library/89/check", json=body())

    assert check.status_code == 200, check.text
    assert check.json()["errors"] == [str(ReadOutOfMemoryError())]
    assert loads, "the catalogue's read was never reached"
    [logged] = [r for r in caplog.records if "ran out of memory" in r.getMessage()]
    assert logged.getMessage() == "print check ran out of memory preparing the run"
    assert logged.exc_info is not None and logged.exc_info[0] is MemoryError


@respx.mock
def test_a_check_that_runs_out_of_memory_again_logs_its_traceback_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#4632: the dialog checks again on every change, so a file too large to hold here
    logged the same stack each time. Within the interval it is one line; past it, and
    for another file, the traceback again."""
    configure(client)
    library_file(89)
    library_file(90)
    run_routes()
    clock = [1000.0]
    monkeypatch.setattr(print_run, "_clock", lambda: clock[0])

    async def out_of_memory(self: LibrarySource, bambuddy: BambuddyClient | None) -> bytes:
        raise MemoryError

    monkeypatch.setattr(LibrarySource, "fetch_3mf", out_of_memory)
    steps = [
        (0.0, 89),
        (1.0, 89),
        (1.0, 90),
        (print_run.OUT_OF_MEMORY_TRACEBACK_INTERVAL, 89),
    ]

    with caplog.at_level(logging.WARNING, logger=print_run.logger.name):
        for step, file_id in steps:
            clock[0] += step
            check = client.post(f"/api/v1/print/library/{file_id}/check", json=body())
            assert check.json()["errors"] == [str(ReadOutOfMemoryError())]

    logged = [r for r in caplog.records if "ran out of memory" in r.getMessage()]
    assert [(r.__dict__["subject"], bool(r.exc_info)) for r in logged] == [
        ("library:89", True),
        ("library:89", False),
        ("library:90", True),
        ("library:89", True),
    ]
    # Past the interval, 90's entry went when 89's traceback was logged again.
    assert set(print_run._out_of_memory_logged) == {"library:89"}


@respx.mock
def test_a_check_of_a_file_too_large_to_fetch_still_reads_its_slots_from_the_file(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4495: the check reads the slots from the fetch it makes anyway, so a file the
    fetch does not hold (past `MAX_DOWNLOAD_BYTES`) is read from disk as before, and
    Bambuddy's filament-requirements, which a large 3MF outlasts (#2550), is not asked."""
    configure(client)
    download = library_file(89)
    run_routes()
    asked = respx.get(f"{API}/library/files/89/filament-requirements").mock(
        return_value=httpx.Response(500)
    )
    monkeypatch.setattr(print_source, "MAX_DOWNLOAD_BYTES", 16)

    check = client.post("/api/v1/print/library/89/check", json=body())

    assert check.status_code == 200, check.text
    assert not asked.called
    # The fetch, stopped at the limit, and the spool that read the slots.
    assert download.call_count == 2


@respx.mock
def test_a_library_files_check_passes_a_preset_gone_since_it_was_kept_and_the_run_refuses_it(
    client: TestClient,
) -> None:
    """#4196 review, on a library file: the check reads the copy the dialog kept, the
    run reads fresh, so a preset Bambuddy has lost since is refused only by the run."""
    configure(client)
    library_file(89)
    run_routes()
    sliced = slice_routes()
    queue_route()
    kept = client.post("/api/v1/print/library/89/check", json=body())
    no_presets: dict[str, dict[str, list[Any]]] = {
        tier: {"printer": [], "process": [], "filament": []}
        for tier in ("cloud", "standard", "local", "orca_cloud")
    }
    respx.get(f"{API}/slicer/presets").mock(return_value=httpx.Response(200, json=no_presets))

    check = client.post("/api/v1/print/library/89/check", json=body())
    ran = run_library(client, 89, json=body())

    assert (kept.status_code, kept.json()["errors"]) == (200, []), kept.text
    assert (check.status_code, check.json()["errors"]) == (200, []), check.text
    assert ran.status_code == 422, ran.text
    assert not sliced.called


@respx.mock
def test_a_library_run_is_attributed_to_who_started_it(
    client: TestClient, gate: Gate, activity_index: ActivityIndex
) -> None:
    configure(client)
    one_color(89)
    flow_copy_routes()
    library_file(89)
    run_routes()
    gated_slice_routes(gate)
    queue_route()
    request = {**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}}

    response = client.post(
        "/api/v1/print/library/89/run",
        json=request,
        headers={"X-ScadBuddy-Agent-Author": "token:t9"},
    )

    assert response.status_code == 202, response.text
    got = asyncio.run(activity_index.get(f"print_run:{response.json()['id']}"))
    gate.open()
    assert got is not None and got.principal == "token:t9"


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
    assert summary["queue_item_id"] == 51
    assert summary["output_id"] is None and summary["slug"] is None
    assert summary["output_name"] == "Bambu Spool Lock or shim UPDATED"
    assert summary["status"] == "printing"
    assert summary["params_diff"] is None
    assert summary["cover"] == {
        "kind": "thumbnail",
        "url": "/api/v1/prints/90/thumbnail",
        "full_url": "/api/v1/prints/90/thumbnail",
    }

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


# --- #1751: a library print's progress, as an output's -------------------------------------


def queue_item_answer(item_id: int, status: str) -> respx.Route:
    return respx.get(f"{API}/queue/{item_id}").mock(
        return_value=httpx.Response(
            200, json={"id": item_id, "printer_id": 1, "printer_name": "H2C", "status": status}
        )
    )


@respx.mock
def test_a_library_file_never_printed_has_no_progress(
    client: TestClient, watched: list[str]
) -> None:
    configure(client)
    response = client.get("/api/v1/print/library/89/progress")
    assert response.status_code == 200, response.text
    assert response.json() is None
    assert watched == []
    # Nor is there anything to file under a project.
    attached = client.post(
        "/api/v1/print/library/89/project", json={"project_id": 7}, headers=press()
    )
    assert attached.status_code == 409, attached.text


@respx.mock
def test_a_library_run_that_failed_before_queueing_is_its_progress(client: TestClient) -> None:
    """As an output's (#1049): a run refused after its 202 queued nothing, and its
    failure is the file's progress rather than "never printed"."""
    configure(client)
    one_color(89)
    flow_copy_routes()
    library_file(89)
    run_routes()

    started = client.post(
        "/api/v1/print/library/89/run", json={**body(), "filament_plan": {"slots": []}}
    )
    assert started.status_code == 202, started.text
    run = follow_run(client, started.json()["id"])
    assert run["status"] == "failed"

    answer = client.get("/api/v1/print/library/89/progress").json()
    assert answer["route"] == "run" and answer["stage"] == "failed" and answer["settled"]
    assert answer["error_message"] == run["error"]["detail"]


@respx.mock
def test_a_library_print_s_progress_is_its_newest_run_s_and_is_followed(
    client: TestClient, watched: list[str]
) -> None:
    """R10, and the #1947 leftover: a second run of the file is read on its own, so the
    first run's queue item that never settles holds nothing open."""
    configure(client)
    one_color(89)
    upload = flow_copy_routes()
    library_file(89)
    run_routes()
    slice_routes()
    plan = {"filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}}
    queue_item_answer(51, "pending")
    queue_item_answer(52, "printing")
    queue_route(item_id=51)
    first = run_library(client, 89, json={**body(), **plan, "request_id": "first"})
    assert first.status_code == 200, first.text
    # The second run slices the copy the first uploaded, as `…_is_reused` does.
    payload = _uploaded_3mf(upload)
    row = {"id": 141, "filename": uploaded_name(upload), "file_type": "3mf"}
    flow_copy_routes([{**row, "file_size": len(payload)}])
    respx.get(f"{API}/library/files/141").mock(
        return_value=httpx.Response(
            200, json={**row, "file_hash": hashlib.sha256(payload).hexdigest()}
        )
    )
    slice_kept()
    queue_route(item_id=52)
    second = run_library(client, 89, json={**body(), **plan, "request_id": "second"})
    assert second.status_code == 200, second.text

    progress = client.get("/api/v1/print/library/89/progress")

    assert progress.status_code == 200, progress.text
    answer = progress.json()
    assert answer["queue_item_id"] == 52 and answer["stage"] == "running"
    assert [copy["queue_entry_id"] for copy in answer["copies_detail"]] == [52]
    # Still moving, so the read makes sure it is followed, in the background.
    deadline = time.monotonic() + 5
    while not watched and time.monotonic() < deadline:
        time.sleep(0.02)
    assert watched == ["library:89"]

    queue_item_answer(52, "completed")
    done = client.get("/api/v1/print/library/89/progress").json()
    assert done["settled"] and done["stage"] == "done"

    # F2: filed under a project as an output's print is, its newest run by default.
    added = respx.post(f"{API}/projects/7/add-queue").mock(return_value=httpx.Response(200))
    attached = client.post(
        "/api/v1/print/library/89/project", json={"project_id": 7}, headers=press()
    )
    assert attached.status_code == 200, attached.text
    assert attached.json()["queue_item_ids"] == [52]
    assert json.loads(added.calls.last.request.content) == {"queue_item_ids": [52]}


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


@respx.mock
def test_queue_268_a_one_extruder_library_file_gets_both_nozzles_and_one_slice(
    client: TestClient,
) -> None:
    """#2181, #2227: a library file saved for one extruder (an A1's stats) printed on a
    printer with the chosen 0.4 High Flow on both sides. The file's own one-side stats
    are no longer kept (before, both colours went onto the left): the nozzle plan
    (#2166) names the side for its one filament, and the copy offers that side alone,
    in a Manual map. Its one plate is sliced whole (#2180)."""
    configure(client)
    one_color(89)
    upload = flow_copy_routes()
    one_side = {
        **STUB_SETTINGS,
        "extruder_nozzle_stats": ["Standard#1"],
        "extruder_nozzle_stats_new": ["Standard#1"],
    }
    library_file(89, content=library_3mf(settings=one_side))
    run_routes()
    both_sides_high_flow()
    sliced = slice_routes()
    queue_route()

    response = run_library(client, 89, json=high_flow_run())

    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert settings["filament_map_mode"] == "Manual"
    assert settings["filament_map"] == ["2"]
    assert settings["extruder_nozzle_stats"] == ["High Flow#0", "High Flow#1"]
    assert settings["extruder_nozzle_stats_new"] == ["High Flow#0", "High Flow#1"]
    assert json.loads(sliced.calls.last.request.content)["plate"] == 0


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


def copy_kept(upload: respx.Route) -> None:
    """The copy the first run uploaded, still in the inbox, so a second run takes it."""
    payload = _uploaded_3mf(upload)
    row = {"id": 141, "filename": uploaded_name(upload), "file_type": "3mf"}
    flow_copy_routes([{**row, "file_size": len(payload)}])
    respx.get(f"{API}/library/files/141").mock(
        return_value=httpx.Response(
            200, json={**row, "file_hash": hashlib.sha256(payload).hexdigest()}
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
    """The same bytes under the same name in the inbox are not uploaded again, so
    printing a file twice leaves one copy; its slice is queued again too (#2671)."""
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

    slice_kept()

    # Two copies, so it is a second run rather than the first one's answer repeated.
    response = run_library(client, 89, json={**high_flow_run(), "copies": 2})

    assert response.status_code == 200, response.text
    assert sliced.call_count == 1
    assert "/library/files/141/slice" in str(sliced.calls.last.request.url)
    assert first.call_count == 1


def one_extruder_3mf() -> bytes:
    """A two-colour library file saved for a one-extruder printer (an A1), as queue item
    268's MakerWorld file was: its stats offer the slicer one extruder."""
    settings = {
        **STUB_SETTINGS,
        "filament_colour": ["#3F8E43", "#27272C"],
        "extruder_nozzle_stats": ["Standard#1"],
        "extruder_nozzle_stats_new": ["Standard#1"],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("3D/3dmodel.model", "<model/>")
        archive.writestr("Metadata/project_settings.config", json.dumps(settings))
    return buffer.getvalue()


@respx.mock
def test_a_two_colour_library_file_offers_both_sides_and_slices_every_plate(
    client: TestClient,
) -> None:
    """Queue item 268, on a printer with a 0.4 High Flow on each side.

    #2181: the file's one-extruder stats offered the slicer only the left, so both
    colours went there; the copy offers both sides. #2180: a one-plate file is sliced as
    ``plate: 0``, so Bambuddy does not repaint the second slot (which it reads as unused
    in an inline-painted mesh) with the first's colour."""
    configure(client)
    respx.get(f"{API}/library/files", params={"folder_id": 2}).mock(
        return_value=httpx.Response(200, json=[])
    )
    upload = respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(
            200, json={"id": 141, "filename": "file-89.3mf", "file_type": "3mf"}
        )
    )
    library_file(89, content=one_extruder_3mf())
    run_routes()
    both_sides_high_flow()
    sliced = slice_routes()
    queue_route()

    response = run_library(
        client,
        89,
        json={
            **body(nozzles=[{"size": "0.4", "flow": "high_flow"}], tier="standard"),
        },
    )

    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(_uploaded_3mf(upload))) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert settings["filament_map_mode"] == "Manual"
    assert settings["filament_map"] == ["2", "1"]
    assert settings["extruder_nozzle_stats"] == ["High Flow#1", "High Flow#1"]
    assert settings["extruder_nozzle_stats_new"] == ["High Flow#1", "High Flow#1"]
    request = json.loads(sliced.calls.last.request.content)
    assert request["plate"] == 0
    assert len(request["filament_colours"]) == 2


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
    # Recorded by the real print, not seeded: the listing must not read that copy as an
    # output's, whatever key `send.upload_copy` filed it under.
    root = recording("library-files-root.json")
    listing_routes([{**root[0], "id": 141}, {**root[0], "id": 89}])
    listed = client.get("/api/v1/print/library").json()["files"]
    assert {row["id"]: row["output_id"] for row in listed} == {141: None, 89: None}
    remembered = asyncio.run(uploads.project_target(7))
    assert remembered is not None
    assert remembered.model_copy(update={"nozzle_plan": None}) == ProjectTarget(
        printer_id=1, nozzle_diameter="0.2"
    )
    # The run's nozzle plan too, which the next Generate lays the file out with (#2166).
    assert remembered.nozzle_plan is not None
    assert [slot["side"] for slot in remembered.nozzle_plan["slots"]] == ["R"]


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
@pytest.mark.parametrize("hashed", [False, True], ids=["streamed", "from-the-cache"])
def test_the_library_file_is_always_a_download_never_a_page(
    client: TestClient, hashed: bool
) -> None:
    """Whatever type Bambuddy names, the file is never rendered on ScadBuddy's origin,
    streamed from Bambuddy or served from the download cache (#2788)."""
    configure(client)
    content = b"<svg onload='alert(1)'/>"
    respx.get(f"{API}/library/files/89").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 89,
                "filename": "x.svg",
                "file_type": "svg",
                "file_hash": hashlib.sha256(content).hexdigest() if hashed else None,
            },
        )
    )
    respx.get(f"{API}/library/files/89/download").mock(
        return_value=httpx.Response(
            200,
            content=content,
            headers={"content-type": "image/svg+xml", "content-disposition": "inline"},
        )
    )

    served = client.get("/api/v1/print/library/89/file")

    assert served.status_code == 200, served.text
    assert served.content == content
    assert served.headers["content-type"] == "application/octet-stream"
    assert served.headers["content-disposition"].startswith("attachment")
    assert served.headers["x-content-type-options"] == "nosniff"


@respx.mock
def test_a_library_file_with_a_hash_is_downloaded_from_bambuddy_once(
    client: TestClient, app: FastAPI
) -> None:
    """#2788: the second whole-file ask is served from the download cache; a ranged ask
    is still Bambuddy's to answer."""
    configure(client)
    content = library_3mf()
    download = library_file(89, content=content, file_hash=hashlib.sha256(content).hexdigest())

    first = client.get("/api/v1/print/library/89/file")
    second = client.get("/api/v1/print/library/89/file")
    ranged = client.get("/api/v1/print/library/89/file", headers={"range": "bytes=0-3"})

    assert first.content == second.content == content
    assert second.headers["content-length"] == str(len(content))
    # Served whole from the cache, a resume is still a ranged ask Bambuddy answers.
    assert second.headers["accept-ranges"] == "bytes"
    # Its hash names the bytes, so a browser can revalidate it (#2821 review).
    assert second.headers["etag"] == f'"{hashlib.sha256(content).hexdigest()}"'
    assert download.call_count == 2
    assert download.calls[-1].request.headers["range"] == "bytes=0-3"
    assert ranged.status_code == 200
    state: AppState = getattr(app.state, STATE_ATTR)
    downloads = state.store.downloads
    assert downloads is not None and downloads.has(hashlib.sha256(content).hexdigest())


@respx.mock
def test_a_cached_library_file_that_is_not_its_hash_is_never_served(
    client: TestClient, app: FastAPI
) -> None:
    """Bytes planted under a file's hash in the shared directory are dropped, and the
    file is served as Bambuddy has it (#2788). Each served copy is the request's own,
    removed once sent."""
    configure(client)
    content = library_3mf()
    sha = hashlib.sha256(content).hexdigest()
    download = library_file(89, content=content, file_hash=sha)
    state: AppState = getattr(app.state, STATE_ATTR)
    downloads = state.store.downloads
    assert downloads is not None
    downloads.root.mkdir(parents=True, exist_ok=True)
    downloads.path(sha).write_bytes(b"planted")

    assert client.get("/api/v1/print/library/89/file").content == content
    assert download.call_count == 1
    # The planted entry was dropped, and what was sent kept in its place.
    assert downloads.path(sha).read_bytes() == content
    assert client.get("/api/v1/print/library/89/file").content == content
    assert download.call_count == 1
    spool = state.paths.cache / "library-spool"
    assert not spool.exists() or list(spool.iterdir()) == []


@respx.mock
def test_a_library_file_larger_than_its_stated_size_is_not_kept(
    client: TestClient, app: FastAPI
) -> None:
    """A size Bambuddy does not state, or understates, cannot fill the volume: the fill
    stops at the cache's limit and keeps nothing, while the one download streams on."""
    configure(client)
    content = library_3mf()
    download = library_file(89, content=content, file_hash=hashlib.sha256(content).hexdigest())
    state: AppState = getattr(app.state, STATE_ATTR)
    downloads = state.store.downloads
    assert downloads is not None
    downloads.max_bytes = 4 * (len(content) - 1)

    assert client.get("/api/v1/print/library/89/file").content == content
    assert download.call_count == 1
    assert list(downloads.root.iterdir()) == []


@respx.mock
def test_a_library_file_not_yet_kept_is_streamed_while_it_is_kept(
    client: TestClient, app: FastAPI
) -> None:
    """A miss is not downloaded whole before its first byte is sent (#2788 review): one
    download is both what is sent and what is kept, with Bambuddy's headers."""
    configure(client)
    content = library_3mf()
    sha = hashlib.sha256(content).hexdigest()
    respx.get(f"{API}/library/files/89").mock(
        return_value=httpx.Response(
            200, json={"id": 89, "filename": "a.3mf", "file_type": "3mf", "file_hash": sha}
        )
    )
    download = respx.get(f"{API}/library/files/89/download").mock(
        return_value=httpx.Response(
            200,
            content=content,
            headers={"accept-ranges": "bytes", "etag": '"bambuddy"'},
        )
    )
    state: AppState = getattr(app.state, STATE_ATTR)
    downloads = state.store.downloads
    assert downloads is not None

    served = client.get("/api/v1/print/library/89/file")

    assert served.content == content
    assert served.headers["etag"] == '"bambuddy"'
    assert download.call_count == 1
    assert downloads.path(sha).read_bytes() == content


@respx.mock
def test_a_library_file_bambuddy_sends_encoded_is_never_kept(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body sent with a Content-Encoding is not the bytes the hash names (#2821
    review): it is passed on as Bambuddy sent it, and no fill is started for it."""
    configure(client)
    content = library_3mf()
    sha = hashlib.sha256(content).hexdigest()
    respx.get(f"{API}/library/files/89").mock(
        return_value=httpx.Response(
            200, json={"id": 89, "filename": "a.3mf", "file_type": "3mf", "file_hash": sha}
        )
    )
    encoded = gzip.compress(content)
    respx.get(f"{API}/library/files/89/download").mock(
        return_value=httpx.Response(200, content=encoded, headers={"content-encoding": "gzip"})
    )
    state: AppState = getattr(app.state, STATE_ATTR)
    downloads = state.store.downloads
    assert downloads is not None
    fills: list[str] = []
    real_fill = downloads.fill

    def fill(sha256: str) -> Any:
        fills.append(sha256)
        return real_fill(sha256)

    monkeypatch.setattr(downloads, "fill", fill)

    served = client.get("/api/v1/print/library/89/file")

    assert served.status_code == 200, served.text
    assert served.headers["content-encoding"] == "gzip"
    assert served.content == content
    assert fills == []
    assert not downloads.has(sha)


@respx.mock
def test_a_kept_file_too_large_to_copy_first_is_streamed_from_bambuddy(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hit is copied whole before its first byte (#2821 review): past
    CACHED_COPY_MAX_BYTES the file streams from Bambuddy instead, and is not kept again."""
    configure(client)
    content = library_3mf()
    sha = hashlib.sha256(content).hexdigest()
    download = library_file(89, content=content, file_hash=sha)
    state: AppState = getattr(app.state, STATE_ATTR)
    downloads = state.store.downloads
    assert downloads is not None
    downloads.put(sha, content)
    assert downloads.has(sha)
    monkeypatch.setattr(library_print, "CACHED_COPY_MAX_BYTES", len(content) - 1)
    fills: list[str] = []
    real_fill = downloads.fill

    def fill(sha256: str) -> Any:
        fills.append(sha256)
        return real_fill(sha256)

    monkeypatch.setattr(downloads, "fill", fill)

    served = client.get("/api/v1/print/library/89/file")

    assert served.content == content
    assert download.call_count == 1
    assert fills == []


@respx.mock
def test_the_hash_read_before_a_download_is_its_own_span(
    client: TestClient, spans: InMemorySpanExporter
) -> None:
    """The extra call to Bambuddy before every whole-file download shows in a trace,
    with whether it found a hash to keep the file under (#2821 review)."""
    configure(client)
    content = library_3mf()
    library_file(89, content=content, file_hash=hashlib.sha256(content).hexdigest())

    assert client.get("/api/v1/print/library/89/file").status_code == 200

    (read,) = [s for s in spans.get_finished_spans() if s.name == "library.file_hash"]
    assert read.attributes is not None
    assert read.attributes["scadbuddy.library.file_id"] == 89
    assert read.attributes["scadbuddy.cache.cacheable"] is True


@respx.mock
def test_a_failed_read_of_the_files_hash_is_a_miss_and_the_file_still_served(
    client: TestClient, app: FastAPI
) -> None:
    """The hash read is only for the cache (#2821 review): when it fails, the download
    is still tried, not failed before it starts."""
    configure(client)
    content = library_3mf()
    respx.get(f"{API}/library/files/89").mock(return_value=httpx.Response(503))
    download = respx.get(f"{API}/library/files/89/download").mock(
        return_value=httpx.Response(200, content=content)
    )

    served = client.get("/api/v1/print/library/89/file")

    assert served.status_code == 200, served.text
    assert served.content == content
    assert download.call_count == 1


async def test_a_streamed_file_sent_as_memoryviews_is_kept_whole(tmp_path: Path) -> None:
    """Every chunk goes into the fill, not only `bytes` ones (#2821 review): a
    memoryview left out would leave it short of its hash, and nothing kept."""
    downloads = DownloadCache(tmp_path / "downloads", max_bytes=1 << 20)
    content = b"first half|second half"
    sha = hashlib.sha256(content).hexdigest()

    async def chunks() -> AsyncIterator[str | bytes | memoryview]:
        yield memoryview(content[:10])
        yield content[10:]

    sent = [
        chunk.encode() if isinstance(chunk, str) else bytes(chunk)
        async for chunk in _kept_as_sent(downloads, sha, chunks())
    ]

    assert b"".join(sent) == content
    assert downloads.path(sha).read_bytes() == content


async def test_a_fill_given_up_is_closed_once_even_when_the_send_is_cancelled() -> None:
    """#2821 review: the fill was closed in a thread after a chunk past the limit, and
    `fill` was still set while that ran; a cancel then made the `finally` close it again
    with the thread still inside it. It is cleared first and closed once."""
    exits: list[float] = []

    class TooFull:
        def write(self, chunk: bytes) -> int:
            raise TooLargeError

    class SlowClose:
        def __enter__(self) -> TooFull:
            return TooFull()

        def __exit__(self, *_: object) -> None:
            exits.append(time.monotonic())
            time.sleep(0.3)

    class Downloads:
        def fill(self, sha256: str) -> SlowClose:
            return SlowClose()

    async def chunks() -> AsyncIterator[bytes]:
        yield b"too much"
        await anyio.sleep(10)

    async def send() -> None:
        sent = _kept_as_sent(cast(DownloadCache, Downloads()), "0" * 64, chunks())
        async with aclosing(sent):
            async for _ in sent:
                pass

    async with anyio.create_task_group() as group:
        group.start_soon(send)
        await anyio.sleep(0.1)
        group.cancel_scope.cancel()

    assert len(exits) == 1


async def test_a_browser_gone_midway_leaves_no_partial_behind(tmp_path: Path) -> None:
    """A dropped download is a cancelled scope, where every await raises again (#2821
    review): the fill is closed before anything is awaited, so its partial goes with it
    rather than waiting for a prune."""
    downloads = DownloadCache(tmp_path / "downloads", max_bytes=1 << 20)
    content = b"first half|second half"
    sha = hashlib.sha256(content).hexdigest()

    async def chunks() -> AsyncIterator[bytes]:
        try:
            yield content[:10]
            yield content[10:]
        finally:
            # Closing Bambuddy's response is awaited, and is where the cancel lands.
            await asyncio.sleep(0)

    with anyio.CancelScope() as scope:
        async with aclosing(_kept_as_sent(downloads, sha, chunks())) as sent:
            async for _ in sent:
                scope.cancel()
                break

    assert scope.cancelled_caught
    assert list(downloads.root.iterdir()) == []


@respx.mock
def test_a_download_cache_that_cannot_be_used_still_serves_the_file(
    client: TestClient, app: FastAPI
) -> None:
    """The cache is best effort: an unwritable directory, or a full volume, is the file
    streamed from Bambuddy, never a 500 (#2788 review)."""
    configure(client)
    content = library_3mf()
    library_file(89, content=content, file_hash=hashlib.sha256(content).hexdigest())
    state: AppState = getattr(app.state, STATE_ATTR)
    downloads = state.store.downloads
    assert downloads is not None
    downloads.root.parent.mkdir(parents=True, exist_ok=True)
    downloads.root.write_bytes(b"not a directory")

    for _ in range(2):
        served = client.get("/api/v1/print/library/89/file")
        assert served.status_code == 200, served.text
        assert served.content == content


@respx.mock
def test_a_library_file_too_large_to_keep_is_streamed_as_before(
    client: TestClient, app: FastAPI
) -> None:
    configure(client)
    content = library_3mf()
    respx.get(f"{API}/library/files/89").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 89,
                "filename": "big.3mf",
                "file_type": "3mf",
                "file_size": len(content),
                "file_hash": hashlib.sha256(content).hexdigest(),
            },
        )
    )
    download = respx.get(f"{API}/library/files/89/download").mock(
        return_value=httpx.Response(200, content=content)
    )
    state: AppState = getattr(app.state, STATE_ATTR)
    downloads = state.store.downloads
    assert downloads is not None
    # A quarter of the cap is the most one file may take of it.
    downloads.max_bytes = 4 * len(content) - 4

    assert client.get("/api/v1/print/library/89/file").content == content
    assert client.get("/api/v1/print/library/89/file").content == content
    assert download.call_count == 2


def two_plate_3mf() -> bytes:
    """A Bambu Studio project of three filaments over two plates, never sliced: plate 1
    places an object in slot 1 with a part in slot 3, plate 2 one in slot 2 (#2550)."""
    settings = {
        "filament_colour": ["#FF0000", "#0000FF", "#FFFFFF"],
        "filament_type": ["PLA", "PETG", "PLA"],
    }
    model_settings = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="1">
    <metadata key="name" value="body"/>
    <metadata key="extruder" value="1"/>
    <part id="1" subtype="normal_part"><metadata key="extruder" value="1"/></part>
    <part id="2" subtype="normal_part"><metadata key="extruder" value="3"/></part>
  </object>
  <object id="2">
    <metadata key="name" value="lid"/>
    <metadata key="extruder" value="2"/>
    <part id="3" subtype="normal_part"/>
  </object>
  <plate>
    <metadata key="plater_id" value="1"/>
    <model_instance><metadata key="object_id" value="1"/></model_instance>
  </plate>
  <plate>
    <metadata key="plater_id" value="2"/>
    <model_instance><metadata key="object_id" value="2"/></model_instance>
  </plate>
</config>
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("3D/3dmodel.model", "<model/>")
        archive.writestr("Metadata/project_settings.config", json.dumps(settings))
        archive.writestr("Metadata/model_settings.config", model_settings)
    return buffer.getvalue()


def _slots(options: dict[str, Any]) -> list[tuple[int, str | None, str | None]]:
    return [(slot["slot_id"], slot["material"], slot["colour"]) for slot in options["slots"]]


@respx.mock
def test_a_library_3mfs_slots_are_read_from_the_file_never_from_bambuddy(
    client: TestClient,
) -> None:
    """#2550: Bambuddy's filament-requirements read of a large unsliced 3MF outlasts the
    request, and failed the whole dialog. The slots are read from the 3MF itself, the
    plate's own slots from its parts' extruders, so Bambuddy is never asked."""
    configure(client)
    download = library_file(
        67, plates="library-plates-multi.json", content=two_plate_3mf(), file_hash="a" * 64
    )
    asked = respx.get(path__regex=r"/api/v1/library/files/67/filament-requirements").mock(
        side_effect=httpx.ReadTimeout("Bambuddy is slicing a preview")
    )
    run_routes()

    choices = client.get("/api/v1/print/library/67/choices")
    plate_2 = client.get("/api/v1/print/library/67/filaments", params={"plate_id": 2})
    every = client.get("/api/v1/print/library/67/filaments", params={"all_plates": "true"})

    assert choices.status_code == 200, choices.text
    assert _slots(choices.json()["filaments"]) == [(1, "PLA", "#FF0000"), (3, "PLA", "#FFFFFF")]
    assert plate_2.status_code == 200, plate_2.text
    assert _slots(plate_2.json()) == [(2, "PETG", "#0000FF")]
    assert [slot["slot_id"] for slot in every.json()["slots"]] == [1, 2, 3]
    assert not asked.called
    # Read once, then from the cache under the file's hash.
    assert download.call_count == 1


@respx.mock
def test_a_library_3mfs_chosen_plates_offer_just_their_slots(client: TestClient) -> None:
    """#3652: a repeated ``plate_ids`` on a library file answers for those plates only.
    Plate 2 alone is its own slot 2; plates 2 and 1 together are every slot either uses.
    Past ``MAX_PLATES`` entries the read is refused."""
    configure(client)
    library_file(
        67, plates="library-plates-multi.json", content=two_plate_3mf(), file_hash="a" * 64
    )
    run_routes()
    url = "/api/v1/print/library/67/filaments"

    plate_2 = client.get(url, params=[("plate_ids", 2)])
    both = client.get(url, params=[("plate_ids", 2), ("plate_ids", 1)])
    too_many = client.get(url, params=[("plate_ids", 1)] * (MAX_PLATES + 1))

    assert plate_2.status_code == 200, plate_2.text
    assert [slot["slot_id"] for slot in plate_2.json()["slots"]] == [2]
    assert both.status_code == 200, both.text
    assert [slot["slot_id"] for slot in both.json()["slots"]] == [1, 2, 3]
    assert too_many.status_code == 422, too_many.text


@respx.mock
def test_the_preview_s_download_serves_the_choices_slots(
    client: TestClient, tmp_path: Path
) -> None:
    """#2550: the dialog's preview and its choices read the same 3MF, fetched once."""
    configure(client)
    download = library_file(89, content=scadbuddy_3mf(tmp_path), file_hash="b" * 64)
    asked = respx.get(path__regex=r"/api/v1/library/files/89/filament-requirements").mock(
        side_effect=httpx.ReadTimeout("Bambuddy is slicing a preview")
    )
    run_routes()

    preview = client.get("/api/v1/print/library/89/preview.glb", params={"plate": 1})
    choices = client.get("/api/v1/print/library/89/choices")

    assert preview.status_code == 200, preview.text
    assert choices.status_code == 200, choices.text
    assert [slot["slot_id"] for slot in choices.json()["filaments"]["slots"]] == [1]
    assert choices.json()["filaments"]["slots"][0]["colour"] == "#43A047"
    assert not asked.called
    assert download.call_count == 1


@respx.mock
def test_a_file_scadbuddy_cannot_read_its_slots_from_asks_bambuddy(client: TestClient) -> None:
    """#2550: a 3MF that is not a readable archive, and an STL, which holds no filament
    settings, keep Bambuddy's filament-requirements as their slots."""
    configure(client)
    library_file(89, content=b"not a zip archive")
    library_file(46, file_type="stl", plates="library-plates-stl.json")
    answer = recording("filament-requirements.json")
    asked = respx.get(path__regex=r"/api/v1/library/files/(89|46)/filament-requirements").mock(
        return_value=httpx.Response(200, json=answer)
    )
    run_routes()

    unreadable = client.get("/api/v1/print/library/89/choices")
    stl = client.get("/api/v1/print/library/46/choices")

    assert unreadable.status_code == 200, unreadable.text
    assert [slot["colour"] for slot in unreadable.json()["filaments"]["slots"]] == [
        "#0047BB",
        "#FF1493",
    ]
    assert stl.status_code == 200, stl.text
    asked_for = {call.request.url.path for call in asked.calls}
    assert asked_for == {
        "/api/v1/library/files/89/filament-requirements",
        "/api/v1/library/files/46/filament-requirements",
    }


@respx.mock
def test_a_library_print_runs_when_bambuddy_cannot_read_its_filaments(
    client: TestClient,
) -> None:
    """#2550: a print of library file 1922 got past the dialog and still had to read its
    slots, from the file and from the copy it uploads, through Bambuddy's
    filament-requirements, which a large 3MF outlasts. The route that starts the run
    reads them from the file, as the dialog does, and hands them to every activity, so
    it slices and queues with Bambuddy never asked."""
    configure(client)
    # Not run_routes' own pattern, which would replace this route rather than follow it.
    unreadable = respx.get(path__regex=r"/library/files/\d+/filament-requirements$").mock(
        side_effect=httpx.ReadTimeout("Bambuddy is slicing a preview")
    )
    flow_copy_routes()
    download = library_file(67, plates="library-plates-multi.json", file_hash="c" * 64)
    run_routes()
    sliced = slice_routes()
    queued = queue_route()

    response = run_library(
        client,
        67,
        json={
            **body(),
            "plate_id": 2,
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
        },
    )

    assert response.status_code == 200, response.text
    assert json.loads(sliced.calls.last.request.content)["plate"] == 2
    assert queued.called
    # Read once by the route, which caches the slots, and once to upload the copy.
    assert download.call_count == 2
    asked = {call.request.url.path for call in unreadable.calls}
    assert not asked & {
        "/api/v1/library/files/67/filament-requirements",
        "/api/v1/library/files/141/filament-requirements",
    }, asked


@respx.mock
@pytest.mark.parametrize(
    ("sequence", "warned"), [("by object", True), ("by layer", False), (None, False)]
)
def test_a_by_object_check_warns_when_the_toolhead_would_hit_an_object(
    client: TestClient, tmp_path: Path, sequence: str | None, warned: bool
) -> None:
    """#956: two objects 15 mm apart on an H2C, whose toolhead needs 49 mm. Printing by
    object, the check warns and refuses nothing; by layer it has nothing to say."""
    configure(client)
    download = library_file(89, content=two_objects_3mf(tmp_path / "bins.3mf", apart=15))
    run_routes()
    sliced = slice_routes()
    request = {**body(), **({"print_sequence": sequence} if sequence else {})}

    check = client.post("/api/v1/print/library/89/check", json=request)

    assert check.status_code == 200, check.text
    kinds = [warning["kind"] for warning in check.json()["warnings"]]
    assert check.json()["errors"] == []
    assert ("sequence-clearance" in kinds) is warned
    assert check.json()["objects"] == (2 if warned else None)
    assert not sliced.called
    # #2976: by object or not, the file is downloaded as often: once, for the nozzle
    # check, whose read the objects and (#4495) the slots come from.
    assert download.call_count == 1


@respx.mock
def test_the_by_object_pairs_are_compared_off_the_event_loop(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3251: the pairwise comparison is quadratic in a file's objects, and ran on the
    API's event loop, where a large one blocked every request until it finished."""
    configure(client)
    library_file(89, content=two_objects_3mf(tmp_path / "bins.3mf", apart=15))
    run_routes()
    slice_routes()
    real = clearance.clearance_warnings
    on_loop: list[bool] = []

    def recorded(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(*args, **kwargs)

    monkeypatch.setattr(clearance, "clearance_warnings", recorded)
    monkeypatch.setattr(print_run, "clearance_warnings", recorded, raising=False)

    check = client.post(
        "/api/v1/print/library/89/check", json={**body(), "print_sequence": "by object"}
    )

    assert check.status_code == 200, check.text
    assert "sequence-clearance" in [w["kind"] for w in check.json()["warnings"]]
    assert on_loop == [False]


@respx.mock
def test_a_by_object_check_of_every_plate_names_each_plate_and_counts_none(
    client: TestClient, tmp_path: Path
) -> None:
    """#2976: checking every plate at once, each plate's clash is named, and no one
    plate's object count stands for all of them."""
    configure(client)
    library_file(
        89,
        content=two_objects_3mf(tmp_path / "bins.3mf", apart=15, plates=2),
        plates="library-plates-multi.json",
    )
    run_routes()
    slice_routes()

    check = client.post(
        "/api/v1/print/library/89/check",
        json={**body(), "print_sequence": "by object", "all_plates": True},
    )

    assert check.status_code == 200, check.text
    clashes = [w["message"] for w in check.json()["warnings"] if w["kind"] == "sequence-clearance"]
    assert any("Plate 1" in message for message in clashes), clashes
    assert any("Plate 2" in message for message in clashes), clashes
    assert check.json()["objects"] is None


def _status(*sizes: str) -> PrinterStatus:
    return PrinterStatus(
        id=1, name="P", connected=True, nozzles=[NozzleInfo(nozzle_diameter=s) for s in sizes]
    )


def test_a_sliced_file_records_its_own_nozzle_else_the_printers() -> None:
    """#2691 review: with no diameter in the file, the run recorded the dialog's nozzle,
    which is hidden for a sliced file and says nothing of it."""
    request = PrintRunRequest.model_validate(
        SLICED_RUN | {"choices": {"nozzles": [{"size": "0.2"}]}}
    )
    stated = SlicedProfile(nozzle_diameters=["0.6"])

    def size(profile: SlicedProfile | None, status: PrinterStatus | None) -> str:
        prepared = PreparedPlates(plate_ids=[1], printer_id=1, printer_status=status)
        return print_run.sliced_nozzle_size(profile, prepared, request)

    assert size(stated, _status("0.4")) == "0.6"
    assert size(SlicedProfile(), _status("0.4")) == "0.4"
    assert size(None, None) == "0.2"


def test_a_printer_reporting_no_nozzle_leaves_a_sliced_files_nozzle_unchecked() -> None:
    """#2691 review: a printer that reports no nozzles said nothing, which reads as fine."""
    profile = SlicedProfile(printer_model="Bambu Lab H2C", nozzle_diameters=["0.6"])
    printer = Printer(id=1, name="P", model="H2C")

    [warning] = print_run.sliced_warnings(profile, printer, _status(), [1])

    assert warning.kind == "sliced-nozzle-missing"
    assert "could not be checked" in warning.message
    assert print_run.sliced_warnings(profile, printer, _status("0.6"), [1]) == []
    assert print_run.sliced_warnings(SlicedProfile(), printer, _status(), [1]) == []


def test_another_model_of_the_same_g_code_family_is_said_but_not_held() -> None:
    """#2956: an X1 Carbon slice on a P1S. Bambuddy dispatches G-code across the X1 and
    P1 series (``GCODE_COMPAT_FAMILIES``), so the run says so and is not held; a model
    outside the family still is."""
    profile = SlicedProfile(printer_model="Bambu Lab X1 Carbon")

    [said] = print_run.sliced_warnings(profile, Printer(id=1, name="P", model="P1S"), None, [1])
    [held] = print_run.sliced_warnings(profile, Printer(id=1, name="P", model="H2C"), None, [1])

    assert said.kind == "sliced-same-family"
    assert said.kind not in print_run.HELD_SLICED_KINDS
    assert held.kind == "sliced-other-model"
    assert held.kind in print_run.HELD_SLICED_KINDS


def test_a_same_family_printer_known_by_its_device_code_is_named_by_its_model() -> None:
    """#3495 review: Bambuddy may hold a printer's SSDP code ("C12"); the warning names
    the model it stands for."""
    profile = SlicedProfile(printer_model="Bambu Lab X1 Carbon")

    [said] = print_run.sliced_warnings(profile, Printer(id=1, name="P", model="C12"), None, [1])

    assert said.kind == "sliced-same-family"
    assert said.message.startswith("Sliced for a X1C; P is a P1S,")
    assert "C12" not in said.message
