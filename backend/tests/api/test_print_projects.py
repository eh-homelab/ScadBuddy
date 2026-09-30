"""Issue #79 — projects through the real app, including the send that lands in one."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_print_filaments import queue_route, slice_routes
from tests.api.test_print_run_choices import run_print, run_request, run_routes
from tests.api.test_send import BASE, configure, make_output
from tests.bambuddy.conftest import recording

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
    body = client.post("/api/v1/print/projects", json={"name": "Reagan keychains"}).json()
    assert (body["id"], body["folder_id"]) == (7, 9)
    assert json.loads(folder.calls.last.request.content)["project_id"] == 7


def test_scadbuddy_keeps_no_model_to_project_relationship(client: TestClient) -> None:
    """ScadBuddy renders and sends; Bambuddy owns projects. Which prints belong to a
    project is on the project's own page, so there is no per-model route to store a
    second answer that would go stale."""
    configure(client)
    assert client.put(
        "/api/v1/print/models/anything/project", json={"project_id": 7}
    ).status_code in (404, 405)


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
    ).json()
    assert body == {"project_id": 7, "queue_item_ids": [71], "archive_ids": [88]}
    assert queue.called and archives.called


def test_filing_an_output_with_no_project_says_so(client: TestClient, model: str) -> None:
    configure(client)
    output_id = make_output(client, model)
    response = client.post(f"/api/v1/print/outputs/{output_id}/project", json={})
    assert response.status_code == 409
    assert "no project" in response.json()["detail"]
