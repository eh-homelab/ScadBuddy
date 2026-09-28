"""Issue #316 — one library copy per (folder, target); a project's file is never moved
or deleted.

Driven through the real app and the spool-first run route (spec 2026-09-27 §4). The
folder a send lands in is the upload's ``folder_id`` query parameter, and a change of
target is a change of nozzle (the plate stays the H2C's), so that is what these vary.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import psycopg
import pytest
import respx
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.outputs import OutputStore
from scadbuddy.render.plate import DEFAULT_PLATE
from tests.api.test_print import run_body
from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import allow_reprints, body, run_print, run_routes
from tests.api.test_send import BASE, configure, make_output

# Every test here reads or writes an output's upload records, which live in Postgres.
pytestmark = pytest.mark.requires_postgres

API = f"{BASE}/api/v1"
INBOX = 2
#: project id -> the id of its library folder.
PROJECT_FOLDERS = {7: 9, 8: 10}
#: The two targets these tests switch between: the same H2C plate, another nozzle.
FINE = {"nozzles": [{"size": "0.2"}], "tier": "fine"}
STANDARD = {"nozzles": [{"size": "0.4"}], "tier": "standard"}


def uploads(*ids: int) -> respx.Route:
    """``POST /library/files`` answering with each id in turn, and each one readable."""
    for file_id in ids:
        exists(file_id)
    return respx.post(f"{API}/library/files").mock(
        side_effect=[
            httpx.Response(200, json={"id": file_id, "filename": f"demo-{file_id}.3mf"})
            for file_id in ids
        ]
    )


def exists(file_id: int, folder_id: int | None = INBOX) -> respx.Route:
    return respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(
            200,
            json={"id": file_id, "filename": f"demo-{file_id}.3mf", "folder_id": folder_id},
        )
    )


def deletes() -> respx.Route:
    return respx.route(method="DELETE", path__regex=r"/api/v1/library/files/\d+$").mock(
        return_value=httpx.Response(200, json={})
    )


def project_routes() -> None:
    for project_id, folder_id in PROJECT_FOLDERS.items():
        respx.get(f"{API}/library/folders/by-project/{project_id}").mock(
            return_value=httpx.Response(
                200, json=[{"id": folder_id, "name": f"p{project_id}", "project_id": project_id}]
            )
        )


def never_moved() -> respx.Route:
    return respx.post(f"{API}/library/files/move").mock(return_value=httpx.Response(200, json={}))


def run(
    client: TestClient,
    output_id: str,
    choices: dict[str, Any] = FINE,
    project_id: int | None = None,
) -> dict[str, Any]:
    request = body(**choices)
    if project_id is not None:
        request["project_id"] = project_id
    response = run_print(client, output_id, json=request)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def upload_store(client: TestClient) -> BambuddyUploadStore:
    uploads: BambuddyUploadStore = getattr(client.app.state, STATE_ATTR).uploads  # type: ignore[attr-defined]
    return uploads


def copies(client: TestClient, output_id: str) -> list[tuple[int, int | None]]:
    rows = client.get(f"/api/v1/outputs/{output_id}").json()["library_files"]
    return [(row["id"], row["folder_id"]) for row in rows]


def folders_uploaded_to(route: respx.Route) -> list[str | None]:
    return [call.request.url.params.get("folder_id") for call in route.calls]


def set_up(client: TestClient, model: str) -> str:
    configure(client, library_folder_id=INBOX)
    run_routes()
    project_routes()
    slice_routes()
    queue_route()
    allow_reprints(client)
    return make_output(client, model)


@respx.mock
def test_a_send_to_project_a_then_b_leaves_a_file_in_each(client: TestClient, model: str) -> None:
    output_id = set_up(client, model)
    upload = uploads(41, 42)
    delete = deletes()
    moved = never_moved()

    assert run(client, output_id, project_id=7)["library_file_id"] == 41
    body = run(client, output_id, project_id=8)

    assert (body["library_file_id"], body["folder_id"]) == (42, 10)
    assert folders_uploaded_to(upload) == ["9", "10"]
    assert not moved.called, "project A's file was moved into project B"
    assert not delete.called, "project A's file was deleted"
    assert copies(client, output_id) == [(41, 9), (42, 10)]

    # Back to A: its own copy is still there, and reused.
    assert run(client, output_id, project_id=7)["library_file_id"] == 41
    assert upload.call_count == 2


@respx.mock
def test_a_project_without_a_folder_gets_one_and_its_copy_is_never_superseded(
    client: TestClient, model: str
) -> None:
    """The picker offers projects that have no folder yet. Such a send must not land in
    the inbox, where the next inbox upload would delete it: the folder is created and
    linked first, as the picker promises."""
    output_id = set_up(client, model)
    respx.get(f"{API}/library/folders/by-project/11").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.get(f"{API}/projects/11").mock(
        return_value=httpx.Response(200, json={"id": 11, "name": "Kids", "status": "active"})
    )
    created = respx.post(f"{API}/library/folders/").mock(
        return_value=httpx.Response(200, json={"id": 12, "name": "Kids", "project_id": 11})
    )
    upload = uploads(41, 42)
    delete = deletes()

    body = run(client, output_id, project_id=11)
    assert (body["library_file_id"], body["folder_id"]) == (41, 12)
    assert created.called
    assert json.loads(created.calls[0].request.content)["project_id"] == 11

    # A send with no project, for another nozzle, supersedes inbox copies only.
    run(client, output_id, STANDARD)
    assert folders_uploaded_to(upload) == ["12", str(INBOX)]
    assert not delete.called, "the project's copy was deleted as if it were in the inbox"
    assert copies(client, output_id) == [(41, 12), (42, INBOX)]


@respx.mock
def test_a_printer_change_keeps_the_old_file_in_a_project_folder(
    client: TestClient, model: str
) -> None:
    output_id = set_up(client, model)
    upload = uploads(41, 42)
    delete = deletes()

    run(client, output_id, project_id=7)
    # A 0.4 nozzle: the same plate, but a different target.
    body = run(client, output_id, STANDARD, project_id=7)

    assert body["library_file_id"] == 42
    assert folders_uploaded_to(upload) == ["9", "9"]
    assert not delete.called, "the file the project printed from was deleted"
    assert copies(client, output_id) == [(41, 9), (42, 9)]


@respx.mock
def test_the_inbox_copy_is_replaced_on_a_printer_change(client: TestClient, model: str) -> None:
    output_id = set_up(client, model)
    upload = uploads(41, 42)
    delete = deletes()

    run(client, output_id)
    run(client, output_id, STANDARD)

    assert folders_uploaded_to(upload) == ["2", "2"]
    assert [call.request.url.path for call in delete.calls] == ["/api/v1/library/files/41"]
    assert copies(client, output_id) == [(42, INBOX)]


@respx.mock
def test_a_superseded_inbox_copy_that_cannot_be_deleted_does_not_fail_the_send(
    client: TestClient, model: str
) -> None:
    """The new copy is already uploaded by then, so the print goes ahead; the old one
    stays recorded, so nothing is left in Bambuddy that ScadBuddy has lost track of."""
    output_id = set_up(client, model)
    uploads(41, 42)
    respx.delete(f"{API}/library/files/41").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )

    run(client, output_id)
    assert run(client, output_id, STANDARD)["library_file_id"] == 42

    assert copies(client, output_id) == [(41, INBOX), (42, INBOX)]


@respx.mock
def test_a_copy_deleted_in_bambuddy_is_dropped_and_uploaded_again(
    client: TestClient, model: str
) -> None:
    output_id = set_up(client, model)
    upload = uploads(41, 42)
    delete = deletes()
    run(client, output_id, project_id=7)

    respx.get(f"{API}/library/files/41").mock(
        return_value=httpx.Response(404, json={"detail": "Not found"})
    )
    body = run(client, output_id, project_id=7)

    assert body["library_file_id"] == 42
    assert upload.call_count == 2
    assert not delete.called
    assert copies(client, output_id) == [(42, 9)]


@respx.mock
def test_the_same_folder_and_target_reuses_the_copy(client: TestClient, model: str) -> None:
    output_id = set_up(client, model)
    upload = uploads(41)

    run(client, output_id)
    run(client, output_id)

    assert upload.call_count == 1


# --- the filament step reads any copy (#457) -------------------------------------------


def open_dialog(client: TestClient, output_id: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/print/outputs/{output_id}/filaments?printer_id=1")
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


@respx.mock
def test_a_dialog_open_after_a_run_does_not_upload_again(client: TestClient, model: str) -> None:
    """The first open has nothing to read and uploads; the run needs its own layout and
    uploads again, superseding the first. From then on the dialog reads the run's copy."""
    output_id = set_up(client, model)
    upload = uploads(41, 42)
    deletes()

    open_dialog(client, output_id)
    run(client, output_id)
    assert upload.call_count == 2
    open_dialog(client, output_id)
    open_dialog(client, output_id)
    run(client, output_id)

    assert upload.call_count == 2


@respx.mock
def test_the_dialog_reads_a_projects_copy_without_moving_it(client: TestClient, model: str) -> None:
    output_id = set_up(client, model)
    upload = uploads(41)
    moved = never_moved()
    run(client, output_id, project_id=7)

    open_dialog(client, output_id)

    assert upload.call_count == 1
    assert not moved.called
    assert copies(client, output_id) == [(41, 9)]


@respx.mock
def test_a_copy_deleted_in_bambuddy_is_not_read(client: TestClient, model: str) -> None:
    output_id = set_up(client, model)
    upload = uploads(41, 42)
    deletes()
    run(client, output_id)
    respx.get(f"{API}/library/files/41").mock(return_value=httpx.Response(404, json={}))

    open_dialog(client, output_id)

    assert upload.call_count == 2
    assert copies(client, output_id) == [(42, INBOX)]


@respx.mock
def test_a_recolored_copy_still_shows_the_models_colors(client: TestClient, model: str) -> None:
    """The run's copy is in the spools' colors (#476); the dialog shows the model's."""
    output_id = set_up(client, model)
    uploads(41)
    run(client, output_id)

    slots = open_dialog(client, output_id)["slots"]

    # The test model is one red filament; the recorded file reports blue and pink.
    assert [slot["colour"] for slot in slots] == ["#FF0000", "#FF1493"]


# --- records written before #455 ----------------------------------------------------


@respx.mock
def test_an_old_records_copy_is_not_reused_and_the_next_send_uploads_afresh(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """No data migration (#455): the keys a ``meta.json`` carried for its uploads, before
    they moved to Postgres, load without error and mean no recorded copy."""
    output_id = set_up(client, model)
    path = paths.output_dir(model, output_id) / "meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.update(
        library_file_id=41,
        library_file_plate=DEFAULT_PLATE.key,
        library_files=[{"id": 41, "folder_id": INBOX, "target_key": DEFAULT_PLATE.key}],
    )
    path.write_text(json.dumps(meta), encoding="utf-8")
    upload = uploads(42)

    response = client.post(f"/api/v1/outputs/{output_id}/send", json={"mode": "library"})

    assert response.json()["library_file_id"] == 42
    assert upload.call_count == 1
    assert copies(client, output_id) == [(42, INBOX)]


# --- sliced files --------------------------------------------------------------------


def sliced(client: TestClient, output_id: str) -> list[list[dict[str, Any]]]:
    rows = client.get(f"/api/v1/outputs/{output_id}").json()["library_files"]
    return [row["sliced"] for row in rows]


@respx.mock
def test_each_run_records_its_sliced_file_against_its_copy(client: TestClient, model: str) -> None:
    output_id = set_up(client, model)
    uploads(41)

    run(client, output_id)

    [[row]] = sliced(client, output_id)
    assert row["id"] == 77
    assert row["preset_key"]


@respx.mock
def test_a_pipeline_runs_sliced_file_is_recorded_when_the_progress_read_sees_it(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Outputs sent before the spool-first run still follow their pipeline run; its
    sliced file appears only on a later read, and is recorded then."""
    output_id = set_up(client, model)
    asyncio.run(
        upload_store(client).record(
            output_id, LibraryCopy(id=41, folder_id=INBOX, target_key=DEFAULT_PLATE.key)
        )
    )
    OutputStore(paths).record_send(output_id, pipeline_run_id=12, print_route="pipeline")
    assert sliced(client, output_id) == [[]]

    respx.get(f"{API}/pipeline-runs/12").mock(return_value=httpx.Response(200, json=run_body()))
    client.get(f"/api/v1/print/outputs/{output_id}/progress")
    client.get(f"/api/v1/print/outputs/{output_id}/progress")

    assert sliced(client, output_id) == [[{"id": 52, "preset_key": "1", "file_hash": None}]]


# --- deleting an output --------------------------------------------------------------


@respx.mock
def test_deleting_an_output_can_take_its_inbox_copies_and_never_a_projects(
    client: TestClient, model: str
) -> None:
    output_id = set_up(client, model)
    uploads(41, 42)
    delete = deletes()
    run(client, output_id)
    run(client, output_id, project_id=7)

    response = client.delete(f"/api/v1/outputs/{output_id}?delete_inbox_copies=true")

    assert response.status_code == 204
    assert [call.request.url.path for call in delete.calls] == ["/api/v1/library/files/41"]
    assert client.get(f"/api/v1/outputs/{output_id}").status_code == 404
    # The project's copy stays in Bambuddy; the record of it goes with the output.
    assert asyncio.run(upload_store(client).for_output(output_id)) == []


@respx.mock
def test_deleting_an_output_leaves_bambuddy_alone_unless_asked(
    client: TestClient, model: str
) -> None:
    output_id = set_up(client, model)
    uploads(41)
    delete = deletes()
    run(client, output_id)

    assert client.delete(f"/api/v1/outputs/{output_id}").status_code == 204
    assert not delete.called


@respx.mock
def test_an_inbox_copy_that_cannot_be_deleted_keeps_the_output(
    client: TestClient, model: str
) -> None:
    """Deleting the record first would lose the only pointer to a file still in the
    inbox, so a failed delete stops before the output goes."""
    output_id = set_up(client, model)
    uploads(41)
    run(client, output_id)
    respx.delete(f"{API}/library/files/41").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )

    response = client.delete(f"/api/v1/outputs/{output_id}?delete_inbox_copies=true")

    assert response.status_code >= 500
    assert client.get(f"/api/v1/outputs/{output_id}").status_code == 200


@respx.mock
def test_deleting_a_model_forgets_its_outputs_upload_records(
    client: TestClient, model: str
) -> None:
    """The model's outputs go with it, and so do their records (#455). Bambuddy's files
    are left alone, as they are when one output is deleted without asking."""
    output_id = set_up(client, model)
    other = make_output(client, model)
    uploads(41, 42)
    delete = deletes()
    run(client, output_id)
    run(client, other)
    store = upload_store(client)
    assert asyncio.run(store.for_outputs([output_id, other])) != {output_id: [], other: []}

    assert client.delete(f"/api/v1/models/{model}").status_code == 204

    assert asyncio.run(store.for_outputs([output_id, other])) == {output_id: [], other: []}
    assert not delete.called


@respx.mock
def test_an_output_whose_records_cannot_be_forgotten_is_still_deleted(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The files are gone by then, so the delete answers 204 and the records are left
    for a later cleanup rather than a 500 that says the output survived (#522 review)."""
    output_id = set_up(client, model)

    async def fail(_ids: object) -> None:
        raise psycopg.OperationalError("the database went away")

    monkeypatch.setattr(upload_store(client), "delete_outputs", fail)
    links = getattr(client.app.state, STATE_ATTR).print_links  # type: ignore[attr-defined]
    forgotten: list[object] = []

    async def forget(ids: object) -> None:
        forgotten.append(ids)

    monkeypatch.setattr(links, "delete_outputs", forget)

    assert client.delete(f"/api/v1/outputs/{output_id}").status_code == 204
    assert client.get(f"/api/v1/outputs/{output_id}").status_code == 404
    # The upload records' failure does not keep the links serving its archives.
    assert forgotten == [[output_id]]
