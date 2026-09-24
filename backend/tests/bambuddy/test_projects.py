"""Issue #79 — a ScadBuddy project is one of Bambuddy's, plus its library folder."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.projects import (
    ProjectRequest,
    attach_results,
    describe_projects,
    ensure_project,
    folder_for,
)
from scadbuddy.core.problems import ApiError
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"


def projects_route() -> respx.Route:
    return respx.get(f"{API}/projects/").mock(
        return_value=httpx.Response(200, json=recording("projects.json"))
    )


def folders_route() -> respx.Route:
    return respx.get(f"{API}/library/folders").mock(
        return_value=httpx.Response(200, json=recording("library-folders.json"))
    )


@respx.mock
async def test_each_project_is_listed_with_the_folder_that_belongs_to_it(
    bambuddy: BambuddyClient,
) -> None:
    """One read of the flat folder list rather than one by-project call per project —
    the list already carries ``project_id``."""
    projects_route()
    by_project = respx.get(url__regex=rf"{API}/library/folders/by-project/\d+").mock(
        return_value=httpx.Response(200, json=[])
    )
    folders_route()

    choices = await describe_projects(bambuddy)
    assert [(view.id, view.folder_id, view.folder_name) for view in choices.projects] == [
        (1, 2, "Raegan")
    ]
    assert not by_project.called


@respx.mock
async def test_creating_a_project_also_creates_its_folder(bambuddy: BambuddyClient) -> None:
    """A folder carries ``project_id``; without one Bambuddy's project page has no files
    to show, which is the whole point of the pairing."""
    created = respx.post(f"{API}/projects/").mock(
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

    view = await ensure_project(bambuddy, ProjectRequest(name="Reagan keychains"))
    assert (view.id, view.folder_id) == (7, 9)
    assert json.loads(created.calls.last.request.content)["name"] == "Reagan keychains"
    assert json.loads(folder.calls.last.request.content)["project_id"] == 7


@respx.mock
async def test_only_the_fields_scadbuddy_has_an_opinion_about_are_sent(
    bambuddy: BambuddyClient,
) -> None:
    """``ProjectCreate`` also carries ``target_count``, ``due_date`` and ``budget``;
    inventing values would put numbers on Bambuddy's project page nobody chose."""
    created = respx.post(f"{API}/projects/").mock(
        return_value=httpx.Response(200, json={"id": 7, "name": "x", "status": "active"})
    )
    respx.get(f"{API}/library/folders/by-project/7").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/library/folders/").mock(
        return_value=httpx.Response(200, json={"id": 9, "name": "x", "project_id": 7})
    )

    await ensure_project(bambuddy, ProjectRequest(name="x"))
    assert set(json.loads(created.calls.last.request.content)) == {"name", "priority"}


@respx.mock
async def test_linking_a_project_that_already_has_a_folder_does_not_make_another(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/projects/1").mock(
        return_value=httpx.Response(200, json=recording("projects.json")[0])
    )
    respx.get(f"{API}/library/folders/by-project/1").mock(
        return_value=httpx.Response(200, json=recording("folders-by-project.json"))
    )
    made = respx.post(f"{API}/library/folders/").mock(return_value=httpx.Response(500))

    view = await ensure_project(bambuddy, ProjectRequest(project_id=1))
    assert view.folder_id == 2
    assert not made.called


@respx.mock
async def test_a_new_project_needs_a_name(bambuddy: BambuddyClient) -> None:
    with pytest.raises(ApiError):
        await ensure_project(bambuddy, ProjectRequest())


@respx.mock
async def test_a_project_with_no_folder_falls_back_rather_than_failing(
    bambuddy: BambuddyClient,
) -> None:
    """``None`` means "use the folder from Settings", which is where every send went
    before this issue."""
    respx.get(f"{API}/library/folders/by-project/4").mock(return_value=httpx.Response(200, json=[]))
    assert await folder_for(bambuddy, 4) is None


@respx.mock
async def test_archives_are_attached_from_the_queue_entries_that_produced_them(
    bambuddy: BambuddyClient,
) -> None:
    """An archive exists only after a print, so it cannot be attached at run time; this
    reads the entries back and forwards whatever they have produced by now."""
    respx.get(f"{API}/queue/51").mock(
        return_value=httpx.Response(200, json={"id": 51, "status": "completed", "archive_id": 88})
    )
    respx.get(f"{API}/queue/52").mock(
        return_value=httpx.Response(200, json={"id": 52, "status": "pending", "archive_id": None})
    )
    queue = respx.post(f"{API}/projects/7/add-queue").mock(return_value=httpx.Response(200))
    archives = respx.post(f"{API}/projects/7/add-archives").mock(return_value=httpx.Response(200))

    result = await attach_results(bambuddy, 7, queue_item_ids=[51, 52])
    assert result.queue_item_ids == [51, 52]
    assert result.archive_ids == [88]
    assert json.loads(queue.calls.last.request.content) == {"queue_item_ids": [51, 52]}
    assert json.loads(archives.calls.last.request.content) == {"archive_ids": [88]}


@respx.mock
async def test_a_queue_entry_bambuddy_has_dropped_does_not_fail_the_attach(
    bambuddy: BambuddyClient,
) -> None:
    respx.get(f"{API}/queue/51").mock(return_value=httpx.Response(404, json={"detail": "gone"}))
    queue = respx.post(f"{API}/projects/7/add-queue").mock(return_value=httpx.Response(200))
    archives = respx.post(f"{API}/projects/7/add-archives").mock(return_value=httpx.Response(200))

    result = await attach_results(bambuddy, 7, queue_item_ids=[51])
    assert result.archive_ids == []
    assert queue.called
    assert not archives.called
