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
from tests.bambuddy.conftest import BASE_URL, recorded_schema, recording

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
async def test_a_project_folder_nested_under_another_is_still_found(
    bambuddy: BambuddyClient,
) -> None:
    """``/library/folders`` answers with a **tree**: a sub-folder arrives inside its
    parent's ``children``, not alongside it. A scan of the top level alone would report
    a nested project folder as missing and then create a second one beside it."""
    nested = recording("library-folders-nested.json")
    # Move the project link onto a folder that only exists as a child.
    supplies = next(row for row in nested if row["children"])
    supplies["children"][0]["project_id"] = 1
    supplies["children"][0]["project_name"] = "Reagan Keychain"
    for row in nested:
        if row.get("project_id") == 1:
            row["project_id"] = None

    projects_route()
    respx.get(f"{API}/library/folders").mock(return_value=httpx.Response(200, json=nested))

    choices = await describe_projects(bambuddy)
    assert choices.projects[0].folder_id == supplies["children"][0]["id"]


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
async def test_a_new_project_can_be_put_under_a_parent(bambuddy: BambuddyClient) -> None:
    """``parent_id`` is the field Bambuddy's recorded ``ProjectCreate`` takes for the
    nesting (#930), and the listing reports it back so the picker can indent."""
    assert "parent_id" in recorded_schema("ProjectCreate")["properties"]
    created = respx.post(f"{API}/projects/").mock(
        return_value=httpx.Response(
            200, json={"id": 7, "name": "Tags", "status": "active", "parent_id": 1}
        )
    )
    respx.get(f"{API}/library/folders/by-project/7").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/library/folders/by-project/1").mock(
        return_value=httpx.Response(200, json=recording("folders-by-project.json"))
    )
    respx.post(f"{API}/library/folders/").mock(
        return_value=httpx.Response(200, json={"id": 9, "name": "Tags", "project_id": 7})
    )

    view = await ensure_project(bambuddy, ProjectRequest(name="Tags", parent_id=1))
    assert json.loads(created.calls.last.request.content)["parent_id"] == 1
    assert view.parent_id == 1


@respx.mock
async def test_a_child_projects_folder_is_made_inside_its_parents_folder(
    bambuddy: BambuddyClient,
) -> None:
    """The library mirrors the nesting: the new folder's ``parent_id`` is the parent
    project's own folder (``FolderCreate.parent_id``), not the library's top level."""
    assert "parent_id" in recorded_schema("FolderCreate")["properties"]
    respx.post(f"{API}/projects/").mock(
        return_value=httpx.Response(
            200, json={"id": 7, "name": "Tags", "status": "active", "parent_id": 1}
        )
    )
    respx.get(f"{API}/library/folders/by-project/7").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/library/folders/by-project/1").mock(
        return_value=httpx.Response(200, json=recording("folders-by-project.json"))
    )
    folder = respx.post(f"{API}/library/folders/").mock(
        return_value=httpx.Response(
            200, json={"id": 9, "name": "Tags", "project_id": 7, "parent_id": 2}
        )
    )

    await ensure_project(bambuddy, ProjectRequest(name="Tags", parent_id=1))
    assert json.loads(folder.calls.last.request.content) == {
        "name": "Tags",
        "project_id": 7,
        "parent_id": 2,
    }


@respx.mock
async def test_a_parent_without_a_folder_is_not_given_one(bambuddy: BambuddyClient) -> None:
    """Creating a child must not write to a project nobody touched: when the parent
    has no folder of its own, the child's folder goes at the top level."""
    respx.post(f"{API}/projects/").mock(
        return_value=httpx.Response(
            200, json={"id": 7, "name": "Tags", "status": "active", "parent_id": 4}
        )
    )
    respx.get(f"{API}/library/folders/by-project/7").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/library/folders/by-project/4").mock(return_value=httpx.Response(200, json=[]))
    folder = respx.post(f"{API}/library/folders/").mock(
        return_value=httpx.Response(200, json={"id": 9, "name": "Tags", "project_id": 7})
    )

    await ensure_project(bambuddy, ProjectRequest(name="Tags", parent_id=4))
    assert folder.call_count == 1
    assert json.loads(folder.calls.last.request.content) == {"name": "Tags", "project_id": 7}


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
async def test_a_parent_on_a_link_is_refused_rather_than_ignored(
    bambuddy: BambuddyClient,
) -> None:
    """Linking never re-parents an existing project, so a ``parent_id`` sent with
    ``project_id`` is a 400 the caller sees, not a nesting silently dropped (#930)."""
    with pytest.raises(ApiError) as raised:
        await ensure_project(bambuddy, ProjectRequest(project_id=1, parent_id=2))
    assert raised.value.status == 400


@respx.mock
async def test_a_parent_with_an_existing_folder_is_refused_before_anything_is_made(
    bambuddy: BambuddyClient,
) -> None:
    """``folder_id`` links a folder as it stands, wherever it is, so with ``parent_id``
    the project would say nested while its folder is not. Refused before Bambuddy is
    written to, rather than creating a project and then failing (#930)."""
    created = respx.post(f"{API}/projects/").mock(return_value=httpx.Response(500))
    with pytest.raises(ApiError) as raised:
        await ensure_project(bambuddy, ProjectRequest(name="Tags", parent_id=1, folder_id=2))
    assert raised.value.status == 400
    assert not created.called


@respx.mock
async def test_a_new_project_needs_a_name(bambuddy: BambuddyClient) -> None:
    with pytest.raises(ApiError):
        await ensure_project(bambuddy, ProjectRequest())


@respx.mock
async def test_a_project_with_no_folder_gets_one_created_and_linked(
    bambuddy: BambuddyClient,
) -> None:
    """Never a fallback to the Settings folder: that is the inbox, where the project's
    copy would be superseded and deleted by the next inbox upload (#316)."""
    respx.get(f"{API}/library/folders/by-project/4").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{API}/projects/4").mock(
        return_value=httpx.Response(200, json={"id": 4, "name": "Shelf", "status": "active"})
    )
    made = respx.post(f"{API}/library/folders/").mock(
        return_value=httpx.Response(200, json={"id": 44, "name": "Shelf", "project_id": 4})
    )
    assert await folder_for(bambuddy, 4) == 44
    assert json.loads(made.calls[0].request.content) == {"name": "Shelf", "project_id": 4}


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


def project_folders(*rows: dict[str, object]) -> respx.Route:
    return respx.get(f"{API}/library/folders/by-project/7").mock(
        return_value=httpx.Response(200, json=list(rows))
    )


PROJECT_FOLDER = {"id": 9, "name": "Kids' room", "project_id": 7, "parent_id": None}
MEDIA_FOLDER = {"id": 12, "name": "Media", "project_id": 7, "parent_id": 9}


@respx.mock
async def test_the_media_folder_is_never_mistaken_for_the_project_folder(
    bambuddy: BambuddyClient,
) -> None:
    """``Media/`` carries the project's id as well, so "the first folder of the
    project" could be it; a send must still land in the project folder itself."""
    project_folders(MEDIA_FOLDER, PROJECT_FOLDER)
    assert await folder_for(bambuddy, 7) == 9
