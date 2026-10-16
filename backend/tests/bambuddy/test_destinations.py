"""Issue #3651 — the destination picker: projects and library folders, their order, and
the default rule (the content's own project, else the Settings one)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import respx

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.destinations import default_destination
from scadbuddy.bambuddy.projects import PATH_SEPARATOR, describe_projects
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.render.glb import BoundingBox
from tests.bambuddy.conftest import BASE_URL, recording

API = f"{BASE_URL}/api/v1"


def folder(
    folder_id: int,
    name: str,
    *,
    parent_id: int | None = None,
    project_id: int | None = None,
    children: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": folder_id,
        "name": name,
        "parent_id": parent_id,
        "project_id": project_id,
        "file_count": 1,
        "latest_activity_at": "2026-10-01T10:00:00",
        "children": children or [],
        **extra,
    }


def projects(*rows: tuple[int, str]) -> respx.Route:
    return respx.get(f"{API}/projects/").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": project_id, "name": name, "status": "active", "created_at": None}
                for project_id, name in rows
            ],
        )
    )


def folders(rows: list[dict[str, Any]]) -> respx.Route:
    return respx.get(f"{API}/library/folders").mock(return_value=httpx.Response(200, json=rows))


@respx.mock
async def test_folders_of_no_project_are_offered_with_their_paths(
    bambuddy: BambuddyClient,
) -> None:
    """A project's folder is the project, so it is not offered twice; the inbox is "No
    project" and what is under it is ScadBuddy's; an external folder is not written to."""
    projects((1, "Keychains"))
    folders(
        [
            folder(2, "Keychains", project_id=1, children=[folder(5, "Media", project_id=1)]),
            folder(3, "Home", children=[folder(4, "Kitchen", parent_id=3)]),
            folder(10, "Inbox", children=[folder(11, "Work", parent_id=10)]),
            folder(12, "NAS", is_external=True),
        ]
    )

    choices = await describe_projects(bambuddy, inbox_id=10)

    assert [(view.id, view.path) for view in choices.folders] == [
        (3, "Home"),
        (4, f"Home{PATH_SEPARATOR}Kitchen"),
    ]
    assert [view.folder_id for view in choices.projects] == [2]


@respx.mock
async def test_touched_is_the_later_of_scadbuddys_touch_and_bambuddys_activity(
    bambuddy: BambuddyClient,
) -> None:
    projects((1, "Keychains"), (2, "Signs"))
    folders(
        [
            folder(20, "Keychains", project_id=1),
            folder(3, "Home", latest_activity_at="2026-10-05T08:00:00"),
        ]
    )
    touched = datetime(2026, 10, 3, tzinfo=UTC)

    choices = await describe_projects(
        bambuddy,
        touches={("project", 2): touched, ("folder", 3): touched},
        default_project_id=2,
        sort="name",
    )

    by_id = {view.id: view for view in choices.projects}
    activity = datetime(2026, 10, 1, 10, tzinfo=UTC)
    assert (by_id[1].modified_at, by_id[1].touched_at) == (activity, activity)
    # No folder yet and no creation time: only the touch.
    assert (by_id[2].modified_at, by_id[2].touched_at) == (None, touched)
    # Bambuddy's activity is later than the touch.
    assert choices.folders[0].touched_at == datetime(2026, 10, 5, 8, tzinfo=UTC)
    assert (choices.default_project_id, choices.sort) == (2, "name")


class _Copies:
    def __init__(self, copies: dict[str, list[LibraryCopy]]) -> None:
        self.copies = copies

    async def for_outputs(self, output_ids: Iterable[str]) -> dict[str, list[LibraryCopy]]:
        return {output_id: self.copies.get(output_id, []) for output_id in output_ids}


def uploads(copies: dict[str, list[LibraryCopy]] | None = None) -> BambuddyUploadStore:
    return cast(BambuddyUploadStore, _Copies(copies or {}))


def output(letter: str, *, project_id: int | None = None) -> OutputMeta:
    return OutputMeta(
        id=letter * 32,
        slug="demo",
        job_id="d" * 32,
        created_at=datetime(2026, 9, 28, tzinfo=UTC),
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        project_id=project_id,
    )


def copy(file_id: int, folder_id: int | None) -> LibraryCopy:
    return LibraryCopy(id=file_id, folder_id=folder_id, target_key="t")


def library_file(file_id: int, folder_id: int | None) -> None:
    respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(
            200, json={"id": file_id, "filename": f"{file_id}.3mf", "folder_id": folder_id}
        )
    )


def tree() -> None:
    projects((1, "Keychains"), (2, "Signs"))
    folders([folder(20, "Keychains", project_id=1), folder(30, "Signs", project_id=2)])


@respx.mock
async def test_content_all_in_one_project_defaults_to_it(bambuddy: BambuddyClient) -> None:
    """An output filed there, one last printed there, and a library file in its folder."""
    tree()
    library_file(7, 20)
    store = uploads({"a" * 32: [copy(100, None), copy(101, 20)]})

    chosen = await default_destination(
        bambuddy,
        store,
        StoredSettings(default_project_id=2),
        outputs=[output("a"), output("b", project_id=1)],
        library_file_ids=[7],
    )

    assert (chosen.project_id, chosen.reason) == (1, "content")


@respx.mock
async def test_content_in_two_projects_takes_the_settings_default(
    bambuddy: BambuddyClient,
) -> None:
    tree()
    library_file(7, 20)
    library_file(8, 30)

    chosen = await default_destination(
        bambuddy, uploads(), StoredSettings(default_project_id=2), library_file_ids=[7, 8]
    )

    assert (chosen.project_id, chosen.reason) == (2, "settings")


@respx.mock
async def test_content_partly_in_no_project_is_not_in_one(bambuddy: BambuddyClient) -> None:
    """A file in the project and one in the inbox: not "all of it in one project"."""
    tree()
    library_file(7, 20)
    library_file(8, None)

    chosen = await default_destination(
        bambuddy, uploads(), StoredSettings(last_project_id=2), library_file_ids=[7, 8]
    )

    assert (chosen.project_id, chosen.reason) == (2, "last")


@respx.mock
async def test_a_project_gone_from_bambuddy_is_never_the_default(
    bambuddy: BambuddyClient,
) -> None:
    tree()
    respx.get(f"{API}/library/files/9").mock(return_value=httpx.Response(404, json={}))

    chosen = await default_destination(
        bambuddy,
        uploads(),
        StoredSettings(default_project_id=99, last_project_id=98),
        outputs=[output("c", project_id=97)],
        library_file_ids=[9],
    )

    assert (chosen.project_id, chosen.reason) == (None, "none")


@respx.mock
async def test_the_recorded_folders_are_read_as_bambuddy_answers_them(
    bambuddy: BambuddyClient,
) -> None:
    """The recordings' project (1) owns folder 2, so a file there is in it."""
    respx.get(f"{API}/projects/").mock(
        return_value=httpx.Response(200, json=recording("projects.json"))
    )
    respx.get(f"{API}/library/folders").mock(
        return_value=httpx.Response(200, json=recording("library-folders-nested.json"))
    )
    library_file(7, 2)

    chosen = await default_destination(bambuddy, uploads(), StoredSettings(), library_file_ids=[7])

    assert (chosen.project_id, chosen.reason) == (1, "content")
