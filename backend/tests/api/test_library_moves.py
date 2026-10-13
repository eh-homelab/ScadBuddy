"""#2670 — copying, moving and renaming files from the Library page or an agent, through
Bambuddy's API, keeping what ScadBuddy has keyed to each file."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.bambuddy.uploads import LibraryCopy, SlicedCopy
from tests.api.test_send import BASE, configure
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

API = f"{BASE}/api/v1"
OUTPUT = "b" * 32

#: Top-level "Inbox" (2), "Projects" (10) holding "Lamp" (11, project 7) and "Desk"
#: (12, project 8), and two top-level "Lamp"s (13, 14) that no project links.
TREE: list[dict[str, Any]] = [
    {"id": 2, "name": "Inbox", "parent_id": None, "children": []},
    {
        "id": 10,
        "name": "Projects",
        "parent_id": None,
        "children": [
            {
                "id": 11,
                "name": "Lamp",
                "parent_id": 10,
                "project_id": 7,
                "project_name": "Lamp",
                "children": [],
            },
            {
                "id": 12,
                "name": "Desk",
                "parent_id": 10,
                "project_id": 8,
                "project_name": "Desk",
                "children": [],
            },
        ],
    },
    {"id": 13, "name": "Lamp", "parent_id": None, "children": []},
    {"id": 14, "name": "Lamp", "parent_id": None, "children": []},
]


def folders() -> respx.Route:
    return respx.get(f"{API}/library/folders").mock(return_value=httpx.Response(200, json=TREE))


def file(file_id: int, folder_id: int | None, **extra: Any) -> respx.Route:
    row = {
        "id": file_id,
        "filename": f"file-{file_id}.3mf",
        "file_type": "3mf",
        "folder_id": folder_id,
    }
    row.update(extra)
    return respx.get(f"{API}/library/files/{file_id}").mock(
        return_value=httpx.Response(200, json=row)
    )


def post(client: TestClient, action: str, body: dict[str, Any]) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/api/v1/print/library/{action}", json=body, headers=press()
    )
    return response


def state_of(app: FastAPI) -> AppState:
    state: AppState = getattr(app.state, STATE_ATTR)
    return state


# --- move ----------------------------------------------------------------------------


@respx.mock
def test_a_move_keeps_the_id_and_moves_the_upload_rows_with_it(
    client: TestClient, app: FastAPI
) -> None:
    """An inbox copy moved into a project's folder must stop being an inbox copy, or the
    next send would replace (delete) it; its slice stays recorded under the same id."""
    configure(client)
    state = state_of(app)
    asyncio.run(state.uploads.record(OUTPUT, LibraryCopy(id=89, folder_id=2, target_key="k")))
    asyncio.run(state.uploads.record_sliced(OUTPUT, 89, SlicedCopy(id=90, preset_key="p")))
    folders()
    file(89, 2)
    moved = respx.post(f"{API}/library/files/move").mock(
        return_value=httpx.Response(
            200, json={"status": "success", "moved": 1, "skipped": 0, "skipped_reasons": []}
        )
    )

    response = post(client, "move", {"file_ids": [89], "folder_id": 11})

    assert response.status_code == 200, response.text
    assert json.loads(moved.calls.last.request.content) == {"file_ids": [89], "folder_id": 11}
    assert response.json() == {
        "folder_id": 11,
        "moved": [{"id": 89, "filename": "file-89.3mf", "left_project": None}],
        "skipped": [],
    }
    [copy] = asyncio.run(state.uploads.for_output(OUTPUT))
    assert (copy.id, copy.folder_id, [s.id for s in copy.sliced]) == (89, 11, [90])


@respx.mock
def test_a_move_out_of_a_project_folder_names_the_project_it_left(client: TestClient) -> None:
    configure(client)
    folders()
    file(89, 11)  # in project 7's folder
    file(91, 11)
    file(92, 12)  # in project 8's folder
    respx.post(f"{API}/library/files/move").mock(
        return_value=httpx.Response(200, json={"moved": 2, "skipped": 0, "skipped_reasons": []})
    )

    body = post(client, "move", {"file_ids": [89, 92], "folder_path": "Projects/Desk"}).json()
    assert body["folder_id"] == 12
    assert {row["id"]: row["left_project"] for row in body["moved"]} == {
        89: {"id": 7, "name": "Lamp"},
        92: None,  # stays in project 8
    }

    body = post(client, "move", {"file_ids": [91]}).json()
    assert body["folder_id"] is None
    assert body["moved"][0]["left_project"] == {"id": 7, "name": "Lamp"}


@respx.mock
def test_a_file_bambuddy_skips_is_reported_and_its_rows_stay(
    client: TestClient, app: FastAPI
) -> None:
    configure(client)
    state = state_of(app)
    asyncio.run(state.uploads.record(OUTPUT, LibraryCopy(id=91, folder_id=2, target_key="k")))
    folders()
    file(89, 2)
    file(91, 2)
    respx.post(f"{API}/library/files/move").mock(
        return_value=httpx.Response(
            200,
            json={
                "moved": 1,
                "skipped": 1,
                "skipped_reasons": [
                    {"file_id": 91, "code": "not_owner", "reason": "not the file owner"}
                ],
            },
        )
    )

    body = post(client, "move", {"file_ids": [89, 91], "folder_id": 13}).json()

    assert [row["id"] for row in body["moved"]] == [89]
    assert body["skipped"] == [
        {
            "id": 91,
            "filename": "file-91.3mf",
            "reason": "Bambuddy did not move it: not the file owner",
        }
    ]
    [copy] = asyncio.run(state.uploads.for_output(OUTPUT))
    assert copy.folder_id == 2


@respx.mock
def test_a_folder_path_shared_by_two_folders_or_by_none_is_refused_before_anything_moves(
    client: TestClient,
) -> None:
    configure(client)
    folders()
    file(89, 2)
    moved = respx.post(f"{API}/library/files/move")

    shared = post(client, "move", {"file_ids": [89], "folder_path": "Lamp"})
    nowhere = post(client, "move", {"file_ids": [89], "folder_path": "Projects/Chair"})
    unknown = post(client, "move", {"file_ids": [89], "folder_id": 99})

    assert shared.status_code == 409, shared.text
    assert "2 library folders are at 'Lamp'" in shared.json()["detail"]
    assert nowhere.status_code == 404
    assert unknown.status_code == 404
    assert not moved.called


@respx.mock
def test_a_nested_folder_path_is_found_with_stray_slashes(client: TestClient) -> None:
    configure(client)
    folders()
    file(89, 2)
    moved = respx.post(f"{API}/library/files/move").mock(
        return_value=httpx.Response(200, json={"moved": 1, "skipped": 0, "skipped_reasons": []})
    )

    assert (
        post(client, "move", {"file_ids": [89], "folder_path": "/Projects/Lamp/"}).json()[
            "folder_id"
        ]
        == 11
    )
    assert json.loads(moved.calls.last.request.content)["folder_id"] == 11


@respx.mock
def test_a_file_bambuddy_no_longer_has_is_a_404(client: TestClient) -> None:
    configure(client)
    folders()
    respx.get(f"{API}/library/files/404").mock(
        return_value=httpx.Response(404, json={"detail": "File not found"})
    )
    moved = respx.post(f"{API}/library/files/move")

    response = post(client, "move", {"file_ids": [404], "folder_id": 2})

    assert response.status_code == 404
    assert "no longer in Bambuddy's library" in response.json()["detail"]
    assert not moved.called


def test_a_move_names_its_folder_once_and_needs_a_file(client: TestClient) -> None:
    configure(client)
    both = post(client, "move", {"file_ids": [1], "folder_id": 2, "folder_path": "Inbox"})
    assert both.status_code == 422
    assert post(client, "move", {"file_ids": []}).status_code == 422
    assert client.post("/api/v1/print/library/move", json={"file_ids": [1]}).status_code == 428


# --- copy ----------------------------------------------------------------------------


@respx.mock
def test_a_copy_is_a_download_and_an_upload_with_its_notes_and_upload_rows(
    client: TestClient, app: FastAPI
) -> None:
    configure(client)
    state = state_of(app)
    asyncio.run(state.uploads.record(OUTPUT, LibraryCopy(id=89, folder_id=2, target_key="k")))
    folders()
    file(89, 2, notes="Edit in ScadBuddy: https://scadbuddy.test/o/b")
    respx.get(f"{API}/library/files/89/download").mock(
        return_value=httpx.Response(200, content=b"PK3mf-bytes")
    )
    uploaded = respx.post(f"{API}/library/files").mock(
        return_value=httpx.Response(200, json={"id": 140, "filename": "file-89.3mf"})
    )
    annotated = respx.put(f"{API}/library/files/140").mock(
        return_value=httpx.Response(200, json={"id": 140, "filename": "file-89.3mf"})
    )

    response = post(client, "copy", {"file_ids": [89], "folder_path": "Projects/Lamp"})

    assert response.status_code == 200, response.text
    assert response.json() == {
        "folder_id": 11,
        "copied": [{"source_id": 89, "id": 140, "filename": "file-89.3mf"}],
        "skipped": [],
    }
    request = uploaded.calls.last.request
    assert request.url.params["folder_id"] == "11"
    assert b"PK3mf-bytes" in request.content
    assert b'filename="file-89.3mf"' in request.content
    assert json.loads(annotated.calls.last.request.content) == {
        "notes": "Edit in ScadBuddy: https://scadbuddy.test/o/b"
    }
    copies = asyncio.run(state.uploads.for_output(OUTPUT))
    assert [(copy.id, copy.folder_id, copy.target_key) for copy in copies] == [
        (89, 2, "k"),
        (140, 11, "k"),
    ]


@respx.mock
def test_a_file_too_large_to_copy_is_skipped(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scadbuddy.library import library_file_moves

    monkeypatch.setattr(library_file_moves, "MAX_COPY_BYTES", 4)
    configure(client)
    folders()
    file(89, 2)
    respx.get(f"{API}/library/files/89/download").mock(
        return_value=httpx.Response(200, content=b"too many bytes")
    )
    uploaded = respx.post(f"{API}/library/files")

    body = post(client, "copy", {"file_ids": [89]}).json()

    assert body["copied"] == []
    assert [row["id"] for row in body["skipped"]] == [89]
    assert "larger than" in body["skipped"][0]["reason"]
    assert not uploaded.called


# --- rename --------------------------------------------------------------------------


@respx.mock
def test_a_rename_keeps_the_id_and_sends_the_name_alone(client: TestClient) -> None:
    configure(client)
    file(89, 2)
    renamed = respx.put(f"{API}/library/files/89").mock(
        return_value=httpx.Response(200, json={"id": 89, "filename": "Desk lamp.3mf"})
    )

    response = post(client, "rename", {"file_id": 89, "filename": " Desk lamp.3mf "})

    assert response.status_code == 200, response.text
    assert response.json() == {"id": 89, "filename": "Desk lamp.3mf", "previous": "file-89.3mf"}
    assert json.loads(renamed.calls.last.request.content) == {"filename": "Desk lamp.3mf"}


@respx.mock
def test_a_rename_keeps_the_extension_of_a_sliced_file(client: TestClient) -> None:
    configure(client)
    file(89, 2, filename="lamp.gcode.3mf", file_type="gcode.3mf")
    renamed = respx.put(f"{API}/library/files/89")

    dropped = post(client, "rename", {"file_id": 89, "filename": "lamp.3mf"})
    folder = post(client, "rename", {"file_id": 89, "filename": "a/b.gcode.3mf"})

    assert dropped.status_code == 422, dropped.text
    assert dropped.json()["extension"] == ".gcode.3mf"
    assert folder.status_code == 422
    assert not renamed.called


@respx.mock
def test_a_name_bambuddy_refuses_is_its_400(client: TestClient) -> None:
    configure(client)
    file(89, 2)
    respx.put(f"{API}/library/files/89").mock(
        return_value=httpx.Response(400, json={"detail": "Filename contains invalid characters"})
    )

    response = post(client, "rename", {"file_id": 89, "filename": "lamp?.3mf"})

    assert response.status_code >= 400
    assert "invalid characters" in response.text


# --- listing -------------------------------------------------------------------------


@respx.mock
def test_the_listing_names_each_folder_s_project(client: TestClient) -> None:
    configure(client)
    folders()
    respx.get(f"{API}/library/files/").mock(return_value=httpx.Response(200, json=[]))

    listed = client.get("/api/v1/print/library").json()["folders"]

    assert {row["id"]: (row["project_id"], row["project_name"]) for row in listed} == {
        2: (None, None),
        10: (None, None),
        11: (7, "Lamp"),
        12: (8, "Desk"),
        13: (None, None),
        14: (None, None),
    }
