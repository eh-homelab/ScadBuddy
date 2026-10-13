"""Renaming and moving library files, and making, changing and deleting folders
(#2686), through ScadBuddy's operations."""

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_library_delete import API, files, queue
from tests.api.test_send import configure
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres


def folders(*tree: dict[str, object]) -> None:
    respx.get(f"{API}/library/folders").mock(return_value=httpx.Response(200, json=list(tree)))


def folder_files(folder_id: int, *rows: tuple[int, bool]) -> None:
    """The folder's files on the delete's check; every later listing is empty, as it is
    once they are deleted."""
    listed = [
        {
            "id": i,
            "filename": f"file-{i}.3mf",
            "file_type": "3mf",
            "folder_id": folder_id,
            "is_external": ext,
        }
        for i, ext in rows
    ]
    calls = iter([listed])
    respx.get(f"{API}/library/files", params={"folder_id": folder_id}).mock(
        side_effect=lambda request: httpx.Response(200, json=next(calls, []))
    )


@respx.mock
def test_rename_and_move_send_only_what_was_given(client: TestClient) -> None:
    configure(client)
    files((7, False))
    put = respx.put(f"{API}/library/files/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "filename": "b.3mf", "folder_id": 3})
    )
    response = client.patch(
        "/api/v1/print/library/7", json={"filename": "b.3mf", "folder_id": 3}, headers=press()
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 7, "filename": "b.3mf", "folder_id": 3}
    assert json.loads(put.calls.last.request.content) == {"filename": "b.3mf", "folder_id": 3}


def test_a_filename_with_a_slash_is_refused(client: TestClient) -> None:
    configure(client)
    response = client.patch(
        "/api/v1/print/library/7", json={"filename": "a/b.3mf"}, headers=press()
    )
    assert response.status_code == 422


@respx.mock
def test_mkdir_and_folder_rename(client: TestClient) -> None:
    configure(client)
    made = respx.post(f"{API}/library/folders/").mock(
        return_value=httpx.Response(200, json={"id": 9, "name": "Parts", "parent_id": 2})
    )
    response = client.post(
        "/api/v1/print/library/folders", json={"name": "Parts", "parent_id": 2}, headers=press()
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"id": 9, "name": "Parts", "parent_id": 2}
    assert json.loads(made.calls.last.request.content) == {"name": "Parts", "parent_id": 2}

    respx.put(f"{API}/library/folders/9").mock(
        return_value=httpx.Response(200, json={"id": 9, "name": "Bits", "parent_id": 2})
    )
    renamed = client.patch(
        "/api/v1/print/library/folders/9", json={"name": "Bits"}, headers=press()
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Bits"


@respx.mock
def test_folder_delete_takes_its_files_and_subfolders(client: TestClient) -> None:
    configure(client)
    folders(
        {
            "id": 4,
            "name": "Top",
            "children": [{"id": 5, "name": "Sub", "parent_id": 4, "children": []}],
        }
    )
    folder_files(4, (10, False))
    folder_files(5, (11, True))
    files((10, False), (11, True))
    queue()
    respx.post(f"{API}/library/bulk-delete").mock(
        return_value=httpx.Response(200, json={"deleted_files": 2})
    )
    gone5 = respx.delete(f"{API}/library/folders/5").mock(return_value=httpx.Response(200, json={}))
    gone4 = respx.delete(f"{API}/library/folders/4").mock(return_value=httpx.Response(200, json={}))

    response = client.post("/api/v1/print/library/folders/4/delete", headers=press())

    assert response.status_code == 200, response.text
    body = response.json()
    assert sorted((f["id"], f["trashed"]) for f in body["deleted"]) == [(10, True), (11, False)]
    assert body["folders"] == [5, 4]
    assert gone5.called and gone4.called


@respx.mock
def test_folder_delete_keeps_a_folder_whose_file_was_skipped(client: TestClient) -> None:
    configure(client)
    folders(
        {
            "id": 4,
            "name": "Top",
            "children": [{"id": 5, "name": "Sub", "parent_id": 4, "children": []}],
        }
    )
    folder_files(4, (10, False))
    folder_files(5, (11, False))
    queue()
    respx.post(f"{API}/library/bulk-delete").mock(
        return_value=httpx.Response(200, json={"deleted_files": 1})
    )
    # File 10 reads as gone once deleted; 11 is still listed, so Bambuddy skipped it.
    respx.get(f"{API}/library/files/10").mock(
        side_effect=[
            httpx.Response(200, json={"id": 10, "filename": "file-10.3mf", "file_type": "3mf"}),
            httpx.Response(404, json={"detail": "File not found"}),
        ]
    )
    respx.get(f"{API}/library/files/11").mock(
        return_value=httpx.Response(
            200, json={"id": 11, "filename": "file-11.3mf", "file_type": "3mf"}
        )
    )
    gone5 = respx.delete(f"{API}/library/folders/5").mock(return_value=httpx.Response(200, json={}))
    gone4 = respx.delete(f"{API}/library/folders/4").mock(return_value=httpx.Response(200, json={}))

    response = client.post("/api/v1/print/library/folders/4/delete", headers=press())

    assert response.status_code == 200, response.text
    body = response.json()
    assert [f["id"] for f in body["deleted"]] == [10]
    assert [f["id"] for f in body["skipped"]] == [11]
    assert body["folders"] == []
    assert not gone5.called and not gone4.called


@respx.mock
def test_folder_delete_refuses_before_anything_when_a_print_waits(client: TestClient) -> None:
    configure(client)
    folders({"id": 4, "name": "Top", "children": []})
    folder_files(4, (10, False))
    files((10, False))
    queue({"id": 1, "status": "pending", "library_file_id": 10})
    bulk = respx.post(f"{API}/library/bulk-delete")
    gone = respx.delete(f"{API}/library/folders/4")

    response = client.post("/api/v1/print/library/folders/4/delete", headers=press())

    assert response.status_code == 409
    assert not bulk.called and not gone.called


@respx.mock
def test_an_empty_folder_delete_drops_only_the_folder(client: TestClient) -> None:
    configure(client)
    folders({"id": 4, "name": "Top", "children": []})
    folder_files(4)
    queue()
    gone = respx.delete(f"{API}/library/folders/4").mock(return_value=httpx.Response(200, json={}))
    response = client.post("/api/v1/print/library/folders/4/delete", headers=press())
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": [], "skipped": [], "folders": [4]}
    assert gone.called


@respx.mock
def test_a_file_added_after_the_check_keeps_its_folder(client: TestClient) -> None:
    configure(client)
    folders({"id": 4, "name": "Top", "children": []})

    def row(i: int) -> dict[str, object]:
        return {"id": i, "filename": f"file-{i}.3mf", "file_type": "3mf", "folder_id": 4}

    respx.get(f"{API}/library/files", params={"folder_id": 4}).mock(
        side_effect=[
            httpx.Response(200, json=[row(10)]),
            httpx.Response(200, json=[row(10), row(12)]),
        ]
    )
    files((10, False))
    queue()
    respx.delete(f"{API}/library/files/10").mock(
        return_value=httpx.Response(200, json={"status": "success", "trashed": True})
    )
    gone = respx.delete(f"{API}/library/folders/4").mock(return_value=httpx.Response(200, json={}))

    response = client.post("/api/v1/print/library/folders/4/delete", headers=press())

    assert response.status_code == 200, response.text
    body = response.json()
    assert [f["id"] for f in body["deleted"]] == [10]
    assert [f["id"] for f in body["skipped"]] == [12]
    assert body["folders"] == []
    assert not gone.called
