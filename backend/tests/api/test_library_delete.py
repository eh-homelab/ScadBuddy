"""#2167 — deleting files from the Library page goes through Bambuddy's API, to its
trash, and Undo restores them from it."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_send import BASE, configure
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres

API = f"{BASE}/api/v1"


def files(*rows: tuple[int, bool]) -> None:
    """Each file as Bambuddy reads it: its id and whether it is external."""
    for file_id, external in rows:
        respx.get(f"{API}/library/files/{file_id}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": file_id,
                    "filename": f"file-{file_id}.3mf",
                    "file_type": "3mf",
                    "is_external": external,
                },
            )
        )


def queue(*items: dict[str, Any]) -> respx.Route:
    return respx.get(f"{API}/queue/").mock(return_value=httpx.Response(200, json=list(items)))


def delete(client: TestClient, *ids: int) -> httpx.Response:
    return client.post(
        "/api/v1/print/library/delete", json={"file_ids": list(ids)}, headers=press()
    )


@respx.mock
def test_one_file_goes_to_bambuddy_s_trash(client: TestClient) -> None:
    configure(client)
    files((89, False))
    queue()
    gone = respx.delete(f"{API}/library/files/89").mock(
        return_value=httpx.Response(200, json={"status": "success", "trashed": True})
    )

    response = delete(client, 89)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "deleted": [{"id": 89, "filename": "file-89.3mf", "trashed": True}],
        "skipped": [],
    }
    assert gone.called
    assert gone.calls.last.request.headers["X-API-Key"] == "s3cret"


@respx.mock
def test_an_external_file_is_gone_for_good(client: TestClient) -> None:
    configure(client)
    files((7, True))
    queue()
    respx.delete(f"{API}/library/files/7").mock(
        return_value=httpx.Response(200, json={"status": "success", "trashed": False})
    )

    assert delete(client, 7).json()["deleted"] == [
        {"id": 7, "filename": "file-7.3mf", "trashed": False}
    ]


@respx.mock
def test_several_files_are_one_bulk_delete_and_a_skipped_one_is_reported(
    client: TestClient,
) -> None:
    configure(client)
    files((89, False), (91, False), (7, True))
    queue()
    bulk = respx.post(f"{API}/library/bulk-delete").mock(
        return_value=httpx.Response(200, json={"deleted_files": 1, "deleted_folders": 0})
    )
    # Once deleted, a file reads as Bambuddy's 404; the skipped one is still listed.
    respx.get(f"{API}/library/files/89").mock(
        side_effect=[
            httpx.Response(200, json={"id": 89, "filename": "file-89.3mf"}),
            httpx.Response(404, json={"detail": "File not found"}),
        ]
    )

    response = delete(client, 89, 91, 7, 89)

    assert response.status_code == 200, response.text
    assert json.loads(bulk.calls.last.request.content) == {
        "file_ids": [89, 91, 7],
        "folder_ids": [],
    }
    body = response.json()
    assert body["deleted"] == [{"id": 89, "filename": "file-89.3mf", "trashed": True}]
    assert [row["id"] for row in body["skipped"]] == [91, 7]
    assert "API key" in body["skipped"][0]["reason"]


@respx.mock
def test_a_file_a_waiting_print_names_is_refused_before_anything_is_deleted(
    client: TestClient,
) -> None:
    configure(client)
    files((89, False), (91, False))
    listed = queue(
        {"id": 5, "library_file_id": 91, "status": "pending"},
        {"id": 6, "library_file_id": 89, "status": "printing"},
    )
    bulk = respx.post(f"{API}/library/bulk-delete")

    response = delete(client, 89, 91)

    assert response.status_code == 409, response.text
    assert "file-91.3mf is waiting to print" in response.json()["detail"]
    assert response.json()["file_ids"] == [91]
    assert listed.calls.last.request.url.params["status"] == "pending"
    assert not bulk.called


@respx.mock
def test_a_file_bambuddy_no_longer_has_is_a_404(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/library/files/404").mock(
        return_value=httpx.Response(404, json={"detail": "File not found"})
    )
    queue()

    response = delete(client, 404)

    assert response.status_code == 404
    assert "no longer in Bambuddy's library" in response.json()["detail"]


@respx.mock
def test_a_key_without_manage_library_names_the_scope(client: TestClient) -> None:
    configure(client)
    files((89, False))
    queue()
    respx.delete(f"{API}/library/files/89").mock(
        return_value=httpx.Response(403, json={"detail": "Forbidden"})
    )

    response = delete(client, 89)

    assert response.status_code == 409
    assert response.json()["required_scope"] == "Manage Library"


@respx.mock
def test_undo_restores_from_the_trash_and_reports_what_was_not_there(
    client: TestClient,
) -> None:
    configure(client)
    restored = respx.post(f"{API}/library/trash/89/restore").mock(
        return_value=httpx.Response(200, json={"status": "success", "id": 89})
    )
    respx.post(f"{API}/library/trash/7/restore").mock(
        return_value=httpx.Response(404, json={"detail": "File not found in trash"})
    )

    response = client.post(
        "/api/v1/print/library/restore", json={"file_ids": [89, 7]}, headers=press()
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restored"] == [89]
    assert [row["id"] for row in body["skipped"]] == [7]
    assert restored.called


def test_a_delete_needs_an_idempotency_key_and_a_file(client: TestClient) -> None:
    configure(client)
    assert client.post("/api/v1/print/library/delete", json={"file_ids": [1]}).status_code == 428
    assert (
        client.post("/api/v1/print/library/delete", json={"file_ids": []}, headers=press())
    ).status_code == 422
