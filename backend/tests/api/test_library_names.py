"""#2349 — library files' names by id, for a page that labels a few of them."""

from __future__ import annotations

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


def test_takes_one_to_a_hundred_ids(client: TestClient) -> None:
    configure(client)
    assert client.get("/api/v1/print/library/names").status_code == 422
    many = "&".join(f"ids={i}" for i in range(1, 102))
    assert client.get(f"/api/v1/print/library/names?{many}").status_code == 422
    assert client.get("/api/v1/print/library/names?ids=0").status_code == 422
