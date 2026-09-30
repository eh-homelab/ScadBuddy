"""#313 — printing a file already in Bambuddy's library through the Print dialog."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import body, run_routes
from tests.api.test_send import BASE, configure
from tests.bambuddy.conftest import recording

pytestmark = pytest.mark.requires_postgres

API = f"{BASE}/api/v1"


def library_file(
    file_id: int = 89, *, file_type: str = "3mf", plates: str = "library-plates-single.json"
) -> None:
    respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(
            200,
            json={"id": file_id, "filename": f"file-{file_id}.{file_type}", "file_type": file_type},
        )
    )
    respx.get(f"{API}/library/files/{file_id}/plates").mock(
        return_value=httpx.Response(200, json={**recording(plates), "file_id": file_id})
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
def test_an_stl_is_listed_under_advanced_with_print(client: TestClient) -> None:
    configure(client)
    listing_routes(recording("library-files-folder.json"))

    every = client.get("/api/v1/print/library", params={"folder_id": 4, "all": "true"}).json()

    stls = [row for row in every["files"] if row["file_type"] == "stl"]
    assert stls and all(row["printable"] for row in stls)


@respx.mock
def test_an_stl_slices_as_one_plate(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/library/files/46/filament-requirements").mock(
        return_value=httpx.Response(200, json=recording("filament-requirements-stl.json"))
    )
    library_file(46, file_type="stl", plates="library-plates-stl.json")
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = client.post(
        "/api/v1/print/library/46/run",
        json={**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}},
    )

    assert response.status_code == 200, response.text
    assert json.loads(sliced.calls.last.request.content)["plate"] == 1


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
def test_a_library_file_is_sliced_as_it_stands_and_queued(client: TestClient) -> None:
    configure(client)
    one_color(89)
    library_file(89)
    run_routes()
    sliced = slice_routes()
    queued = queue_route()
    upload = respx.post(f"{API}/library/files")

    response = client.post(
        "/api/v1/print/library/89/run",
        json={**body(), "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["library_file_id"] == 89
    assert response.json()["folder_id"] is None
    assert "/library/files/89/slice" in str(sliced.calls.last.request.url)
    assert json.loads(queued.calls.last.request.content)["printer_id"] == 1
    assert not upload.called


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
    library_file(70, plates="library-plates-stl.json")
    run_routes()
    sliced = slice_routes()
    queue_route()

    response = client.post(
        "/api/v1/print/library/70/run",
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
    assert check.json() == {"errors": [], "warnings": []}
    assert not sliced.called
