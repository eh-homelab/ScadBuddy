"""The outputs routes in the steady state (#7255): the rows answer every read and a
missing row is a missing output. Release B retired the copy (#7257), and #7887 dropped
its run tables."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.library.outputs import OutputStore
from tests.api.conftest import wait_for_job
from tests.support.operations import press

pytestmark = pytest.mark.requires_postgres


def _store(client: TestClient) -> OutputStore:
    store: OutputStore = getattr(client.app.state, STATE_ATTR).outputs  # type: ignore[attr-defined]
    return store


def _save(client: TestClient, model: str, width: float, name: str | None = None) -> str:
    job_id = client.post(
        f"/api/v1/models/{model}/render", json={"params": {"width": width}}
    ).json()["job_id"]
    wait_for_job(client, job_id)
    body: dict[str, object] = {"job_id": job_id}
    if name is not None:
        body["name"] = name
    created = client.post(f"/api/v1/models/{model}/outputs", json=body, headers=press())
    assert created.status_code == 201, created.text
    output_id: str = created.json()["id"]
    return output_id


@pytest.fixture
def first(client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """One output saved; after it, nothing globs the outputs for a record."""
    output_id = _save(client, model, 1, "first")
    real_glob = Path.glob

    def no_meta_glob(self: Path, pattern: str, **kwargs: object) -> Iterator[Path]:
        if pattern.endswith("meta.json"):
            raise AssertionError(f"globbed {self} for {pattern}")
        return real_glob(self, pattern, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "glob", no_meta_glob)
    yield output_id


def test_the_list_is_newest_first(client: TestClient, model: str, first: str) -> None:
    second = _save(client, model, 2)
    listed = client.get(f"/api/v1/models/{model}/outputs").json()
    assert [row["id"] for row in listed] == [second, first]
    assert listed[1]["name"] == "first"
    assert listed[1]["record"] is not None and listed[1]["manifest"]


def test_an_output_reads_by_id(client: TestClient, model: str, first: str) -> None:
    fetched = client.get(f"/api/v1/outputs/{first}")
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert (body["id"], body["slug"], body["name"]) == (first, model, "first")
    assert body["params"]["width"] == 1


def test_a_rename_reaches_the_row(client: TestClient, model: str, first: str) -> None:
    renamed = client.patch(f"/api/v1/outputs/{first}", json={"name": "renamed"}, headers=press())
    assert renamed.status_code == 200, renamed.text
    assert client.get(f"/api/v1/outputs/{first}").json()["name"] == "renamed"
    [listed] = client.get(f"/api/v1/models/{model}/outputs").json()
    assert listed["name"] == "renamed"


def test_a_delete_removes_the_row(client: TestClient, model: str, first: str) -> None:
    assert client.delete(f"/api/v1/outputs/{first}", headers=press()).status_code == 204
    assert client.get(f"/api/v1/outputs/{first}").status_code == 404
    assert client.get(f"/api/v1/models/{model}/outputs").json() == []
    store = _store(client)
    assert store.rows is not None and store.rows.get(first) is None


def test_an_unknown_output_is_a_404_with_no_glob(client: TestClient, first: str) -> None:
    assert client.get("/api/v1/outputs/" + "0" * 32).status_code == 404
