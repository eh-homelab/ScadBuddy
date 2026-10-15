"""#2349 — library files' names by id, for a page that labels a few of them."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.api.test_send import BASE, configure

pytestmark = pytest.mark.requires_postgres

API = f"{BASE}/api/v1"


def _file(file_id: int, filename: str, folder_id: int) -> httpx.Response:
    return httpx.Response(200, json={"id": file_id, "filename": filename, "folder_id": folder_id})


@respx.mock
def test_names_each_file_and_marks_one_bambuddy_no_longer_has(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/library/files/719").mock(return_value=_file(719, "sign.3mf", 4))
    respx.get(f"{API}/library/files/89").mock(return_value=_file(89, "bin.3mf", 2))
    gone = httpx.Response(404, json={"detail": "gone"})
    respx.get(f"{API}/library/files/1633").mock(return_value=gone)

    response = client.get("/api/v1/print/library/names?ids=719&ids=1633&ids=89&ids=719")

    assert response.status_code == 200, response.text
    assert response.json() == [
        {"id": 719, "name": "sign.3mf", "folder_id": 4},
        {"id": 1633, "name": None, "folder_id": None},
        {"id": 89, "name": "bin.3mf", "folder_id": 2},
    ]


@respx.mock
def test_any_other_failure_is_the_routes(client: TestClient) -> None:
    configure(client)
    respx.get(f"{API}/library/files/719").mock(return_value=httpx.Response(401, json={}))

    response = client.get("/api/v1/print/library/names?ids=719")

    assert response.status_code == 409
    assert "API key" in response.json()["detail"]


@respx.mock
def test_a_failure_cancels_the_other_reads_before_the_client_closes(client: TestClient) -> None:
    """A read still waiting when another fails is cancelled, not left to fail later
    against a closed client with an exception nothing retrieves."""
    configure(client)
    outcome: list[str] = []

    async def slow(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        outcome.append("finished")
        return _file(720, "late.3mf", 1)

    respx.get(f"{API}/library/files/720").mock(side_effect=slow)
    respx.get(f"{API}/library/files/719").mock(return_value=httpx.Response(401, json={}))

    response = client.get("/api/v1/print/library/names?ids=720&ids=719")

    assert response.status_code == 409
    assert outcome == ["cancelled"]


@respx.mock
def test_every_file_missing_is_a_misrouted_bambuddy_not_every_file_deleted(
    client: TestClient,
) -> None:
    configure(client)
    gone = httpx.Response(404, json={"detail": "Not Found"})
    respx.get(f"{API}/library/files/719").mock(return_value=gone)
    respx.get(f"{API}/library/files/89").mock(return_value=gone)

    response = client.get("/api/v1/print/library/names?ids=719&ids=89")

    assert response.status_code == 502
    assert "URL" in response.json()["detail"]
    # One file asked and gone is just that file deleted.
    single = client.get("/api/v1/print/library/names?ids=719")
    assert single.status_code == 200
    assert single.json() == [{"id": 719, "name": None, "folder_id": None}]


def test_takes_one_to_a_hundred_ids(client: TestClient) -> None:
    configure(client)
    assert client.get("/api/v1/print/library/names").status_code == 422
    many = "&".join(f"ids={i}" for i in range(1, 102))
    assert client.get(f"/api/v1/print/library/names?{many}").status_code == 422
    assert client.get("/api/v1/print/library/names?ids=0").status_code == 422
