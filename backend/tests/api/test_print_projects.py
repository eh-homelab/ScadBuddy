"""Issue #79 — projects through the real app, including the send that lands in one."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import run_print, run_request, run_routes
from tests.api.test_send import BASE, configure, make_output
from tests.bambuddy.conftest import recording
from tests.support.operations import press

API = f"{BASE}/api/v1"


def upload_route(file_id: int = 41) -> respx.Route:
    """The folder is a **query parameter** on this route, so the assertions below read
    it off the request URL rather than out of a body."""
    folder_listing()
    return respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(
            200, json={"id": file_id, "filename": "demo-elan.3mf", "file_type": "3mf"}
        )
    )


def folder_listing() -> respx.Route:
    """What the project folder already holds, which its copy's name must not collide
    with (#317)."""
    return respx.get(f"{API}/library/files").mock(return_value=httpx.Response(200, json=[]))


@respx.mock
def test_projects_are_listed_with_their_folders(client: TestClient, model: str) -> None:
    configure(client)
    respx.get(f"{API}/projects/").mock(
        return_value=httpx.Response(200, json=recording("projects.json"))
    )
    respx.get(f"{API}/library/folders").mock(
        return_value=httpx.Response(200, json=recording("library-folders.json"))
    )
    body = client.get("/api/v1/print/projects").json()
    assert body["projects"][0]["folder_name"] == "Raegan"


@respx.mock
def test_creating_a_project_pairs_it_with_a_folder(client: TestClient, model: str) -> None:
    configure(client)
    respx.post(f"{API}/projects/").mock(
        return_value=httpx.Response(
            200, json={"id": 7, "name": "Reagan keychains", "status": "active"}
        )
    )
    respx.get(f"{API}/library/folders/by-project/7").mock(return_value=httpx.Response(200, json=[]))
    folder = respx.post(f"{API}/library/folders/").mock(
        return_value=httpx.Response(
            200, json={"id": 9, "name": "Reagan keychains", "project_id": 7}
        )
    )
    body = client.post(
        "/api/v1/print/projects", json={"name": "Reagan keychains"}, headers=press()
    ).json()
    assert (body["id"], body["folder_id"]) == (7, 9)
    assert json.loads(folder.calls.last.request.content)["project_id"] == 7


def test_scadbuddy_keeps_no_project_membership(app: FastAPI) -> None:
    """ScadBuddy renders and sends; Bambuddy owns projects. Which prints and files belong
    to a project is on the project's own page, so no ScadBuddy route answers it from a
    second copy that would go stale.

    What ScadBuddy does keep is a *default*: the project Generate last filed a model
    (and preset) into, so the picker opens there (#1660), forgettable in Settings like
    ``last_project_id``. That is a preference about the next filing, not a record of
    what the project holds. ``GET /farm/archives?project_id=`` is the one read scoped
    to a project, and it is Bambuddy's own archive list passed through."""
    schema = app.openapi()
    scoped = {
        path
        for path, item in schema["paths"].items()
        if "get" in item
        and any("project" in param["name"] for param in item["get"].get("parameters", []))
    }
    assert scoped == {"/api/v1/farm/archives"}

    # The remembered default names a project and nothing it holds.
    remembered = schema["components"]["schemas"]["RememberedModelProject"]
    assert set(remembered["properties"]) == {"preset_id", "preset_name", "project_id"}


@pytest.mark.requires_postgres
@respx.mock
def test_a_send_to_a_project_uploads_into_that_projects_folder(
    client: TestClient, model: str
) -> None:
    """Putting the 3MF in a folder that carries ``project_id`` is what makes Bambuddy's
    project page list it."""
    configure(client, library_folder_id=2)
    output_id = make_output(client, model)
    uploaded = upload_route()
    run_routes()
    slice_routes()
    queue_route()
    respx.get(f"{API}/library/folders/by-project/7").mock(
        return_value=httpx.Response(200, json=[{"id": 9, "name": "Reagan", "project_id": 7}])
    )

    body = run_print(client, output_id, json=run_request(project_id=7)).json()
    assert body["project_id"] == 7
    assert body["folder_id"] == 9
    # The folder is a query parameter on the upload, not part of the body.
    assert uploaded.calls.last.request.url.params["folder_id"] == "9"


@pytest.mark.requires_postgres
@respx.mock
def test_an_already_uploaded_output_gets_a_copy_of_its_own_in_the_project_folder(
    client: TestClient, model: str
) -> None:
    """The second run is the one that bites. An output uploaded before a project was
    chosen has a copy in the inbox, and the project gets a copy of its own rather than
    that one being moved (#316): a moved file would leave whatever printed from it
    pointing at a folder it is no longer in. Without the upload the response would
    report a folder the file is not in."""
    configure(client, library_folder_id=2)
    output_id = make_output(client, model)
    folder_listing()
    uploaded = respx.post(f"{API}/library/files").mock(
        side_effect=[
            httpx.Response(200, json={"id": file_id, "filename": "demo-elan.3mf"})
            for file_id in (41, 42)
        ]
    )
    run_routes()
    slice_routes()
    queue_route()
    respx.get(f"{API}/library/folders/by-project/7").mock(
        return_value=httpx.Response(200, json=[{"id": 9, "name": "Reagan", "project_id": 7}])
    )
    moved = respx.post(f"{API}/library/files/move").mock(
        return_value=httpx.Response(200, json={"moved": 1, "skipped": []})
    )

    # First run, no project: the 3MF lands in the folder from Settings.
    run_print(client, output_id, json=run_request())
    assert uploaded.call_count == 1

    # Second run, this time filed under a project.
    body = run_print(client, output_id, json=run_request(project_id=7)).json()
    assert (body["library_file_id"], body["folder_id"]) == (42, 9)
    assert uploaded.calls.last.request.url.params["folder_id"] == "9"
    assert not moved.called


@pytest.mark.requires_postgres
@respx.mock
def test_the_queue_route_files_the_item_under_the_project_with_no_race(
    client: TestClient, model: str
) -> None:
    """``PrintQueueItemCreate`` carries ``project_id``, so there is no window in which
    the entry exists unfiled."""
    configure(client)
    output_id = make_output(client, model)
    upload_route()
    respx.get(f"{API}/library/folders/by-project/7").mock(
        return_value=httpx.Response(200, json=[{"id": 9, "name": "Reagan", "project_id": 7}])
    )
    run_routes()
    slice_routes()
    queued = queue_route()

    run_print(client, output_id, json=run_request(project_id=7))
    sent: dict[str, Any] = json.loads(queued.calls.last.request.content)
    assert sent["project_id"] == 7


@respx.mock
def test_filing_the_results_attaches_the_entries_and_their_archives(
    client: TestClient, model: str
) -> None:
    configure(client)
    output_id = make_output(client, model)
    respx.get(f"{API}/queue/71").mock(
        return_value=httpx.Response(200, json={"id": 71, "status": "completed", "archive_id": 88})
    )
    queue = respx.post(f"{API}/projects/7/add-queue").mock(return_value=httpx.Response(200))
    archives = respx.post(f"{API}/projects/7/add-archives").mock(return_value=httpx.Response(200))

    body = client.post(
        f"/api/v1/print/outputs/{output_id}/project",
        json={"project_id": 7, "queue_item_ids": [71]},
        headers=press(),
    ).json()
    assert body == {"project_id": 7, "queue_item_ids": [71], "archive_ids": [88]}
    assert queue.called and archives.called


def test_filing_an_output_with_no_project_says_so(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    response = client.post(f"/api/v1/print/outputs/{output_id}/project", json={}, headers=press())
    assert response.status_code == 409
    assert "no project" in response.json()["detail"]


def _projects_and_folders() -> None:
    """Project 7 owns folder 9; folder 30, "Home", is no project's."""
    respx.get(f"{API}/projects/").mock(
        return_value=httpx.Response(200, json=[{"id": 7, "name": "Reagan", "status": "active"}])
    )
    respx.get(f"{API}/library/folders").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 9, "name": "Reagan", "project_id": 7, "children": []},
                {"id": 30, "name": "Home", "children": []},
            ],
        )
    )


@pytest.mark.requires_postgres
@respx.mock
def test_a_print_into_a_library_folder_uploads_there_under_no_project(
    client: TestClient, model: str
) -> None:
    """#3651 — a folder of no project is a destination of its own: no project is
    looked up, nothing is filed under one, and the folder is touched for the picker."""
    configure(client, library_folder_id=2)
    output_id = make_output(client, model)
    uploaded = upload_route()
    run_routes()
    slice_routes()
    queued = queue_route()
    by_project = respx.get(url__regex=rf"{API}/library/folders/by-project/\d+").mock(
        return_value=httpx.Response(200, json=[])
    )
    _projects_and_folders()

    body = run_print(client, output_id, json=run_request(folder_id=30)).json()

    assert (body["project_id"], body["folder_id"]) == (None, 30)
    assert uploaded.calls.last.request.url.params["folder_id"] == "30"
    assert json.loads(queued.calls.last.request.content).get("project_id") is None
    assert not by_project.called
    listed = client.get("/api/v1/print/projects").json()
    assert [(row["id"], row["path"]) for row in listed["folders"]] == [(30, "Home")]
    assert listed["folders"][0]["touched_at"] is not None


@pytest.mark.requires_postgres
@respx.mock
@pytest.mark.parametrize("folder_id", [2, 9, 404])
@pytest.mark.parametrize("null_project", [False, True])
def test_a_folder_the_picker_does_not_offer_is_refused(
    client: TestClient, model: str, folder_id: int, null_project: bool
) -> None:
    """#3651 review — the inbox (2), a project's folder (9) and a folder that is gone
    are not destinations: the run is refused before anything is uploaded, also when
    the body sends ``project_id: null`` beside the folder."""
    configure(client, library_folder_id=2)
    output_id = make_output(client, model)
    uploaded = upload_route()
    run_routes()
    _projects_and_folders()
    request = run_request(folder_id=folder_id)
    if null_project:
        request["project_id"] = None

    response = run_print(client, output_id, json=request)

    assert response.status_code == 422, response.text
    assert "not a library folder" in response.text
    assert not uploaded.called


@pytest.mark.requires_postgres
@respx.mock
def test_a_deleted_settings_default_falls_through_rather_than_failing_the_print(
    client: TestClient, model: str
) -> None:
    """#3651 review — a run that leaves the project out, with a Settings default that
    Bambuddy no longer has, files under the last send's project instead of failing on
    the deleted project's folder."""
    configure(client, library_folder_id=2)
    output_id = make_output(client, model)
    uploaded = upload_route()
    run_routes()
    slice_routes()
    queue_route()
    _projects_and_folders()
    respx.get(f"{API}/library/folders/by-project/7").mock(
        return_value=httpx.Response(200, json=[{"id": 9, "name": "Reagan", "project_id": 7}])
    )
    deleted = respx.get(f"{API}/projects/99").mock(return_value=httpx.Response(404))
    assert client.put("/api/v1/print/projects/last", json={"project_id": 7}).status_code == 200
    saved = client.put("/api/v1/settings", json={"default_project_id": 99})
    assert saved.status_code == 200, saved.text

    body = run_print(client, output_id, json=run_request()).json()

    assert (body["project_id"], body["folder_id"]) == (7, 9)
    assert uploaded.calls.last.request.url.params["folder_id"] == "9"
    assert not deleted.called


@pytest.mark.requires_postgres
def test_a_project_and_a_folder_together_are_refused(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    response = client.post(
        f"/api/v1/print/outputs/{output_id}/run", json=run_request(project_id=7, folder_id=30)
    )
    assert response.status_code == 422


@pytest.mark.requires_postgres
@respx.mock
def test_the_default_is_the_project_the_content_is_in_else_the_settings_one(
    client: TestClient, model: str
) -> None:
    """#3651 — a library file in project 7's folder opens on 7; one in "Home", on the
    project Settings names, else on the last send's."""
    configure(client)
    _projects_and_folders()
    for file_id, folder_id in ((61, 9), (62, 30)):
        respx.get(f"{API}/library/files/{file_id}").mock(
            return_value=httpx.Response(
                200, json={"id": file_id, "filename": "a.3mf", "folder_id": folder_id}
            )
        )

    def default(*file_ids: int) -> dict[str, Any]:
        response = client.get(
            "/api/v1/print/projects/default", params={"library_file_id": list(file_ids)}
        )
        assert response.status_code == 200, response.text
        answer: dict[str, Any] = response.json()
        return answer

    assert default(61) == {"project_id": 7, "reason": "content"}
    assert default(61, 62) == {"project_id": None, "reason": "none"}
    assert client.put("/api/v1/print/projects/last", json={"project_id": 7}).status_code == 200
    assert default(62) == {"project_id": 7, "reason": "last"}
    saved = client.put(
        "/api/v1/settings", json={"default_project_id": 7, "destination_sort": "name"}
    )
    assert saved.status_code == 200, saved.text
    assert (saved.json()["default_project_id"], saved.json()["destination_sort"]) == (7, "name")
    assert default(62) == {"project_id": 7, "reason": "settings"}
    listed = client.get("/api/v1/print/projects").json()
    assert (listed["default_project_id"], listed["sort"]) == (7, "name")
    # Choosing project 7 touched it.
    assert listed["projects"][0]["touched_at"] is not None
