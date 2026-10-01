"""A saved output holds its Parts; deleting it, or its model, lets them go (spec
2026-09-27 §7, Review Focus 3)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.api.deps import STATE_ATTR, AppState, get_render
from scadbuddy.library.outputs import OUTPUT_HOLDER, OutputStore
from scadbuddy.render.job_models import Job, JobNotFoundError
from scadbuddy.store.refs import BlobRefs
from scadbuddy.workflows.arrange import part_of
from scadbuddy.workflows.models import ArrangeInputs, PackItem, PlateSize
from tests.support.arrange import finished_job
from tests.support.store import store_pool

Pool = ConnectionPool[Connection[DictRow]]


class OneJob:
    """A render service that knows one finished job: all `require_job`
    (`render.store.read`) and `_delete_model` (`render.store.has_unfinished`) ask of it."""

    def __init__(self, job: Job) -> None:
        self.store = self
        self.job = job

    def read(self, job_id: str) -> Job:
        if job_id != self.job.id:
            raise JobNotFoundError(job_id)
        return self.job

    def has_unfinished(self, slug: str) -> bool:
        return False


@pytest.fixture
def pool(app: FastAPI, pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        state: AppState = getattr(app.state, STATE_ATTR)
        state.refs = BlobRefs(opened)
        yield opened


def held(pool: Pool) -> set[tuple[str, str]]:
    """Every (Part, output) pair `blob_refs` holds for an output."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT key, holder_id FROM blob_refs WHERE holder_kind = %s", (OUTPUT_HOLDER,)
        ).fetchall()
    return {(row["key"], row["holder_id"]) for row in rows}


def finished(app: FastAPI, tmp_path: Path) -> tuple[str, list[str]]:
    """A done job the app's render service answers for: its id and its output's Parts."""
    _, job, written = asyncio.run(finished_job(tmp_path))
    app.dependency_overrides[get_render] = lambda: OneJob(job)
    return job.id, [m.part for m in written.manifest]


def save(client: TestClient, job_id: str) -> str:
    response = client.post("/api/v1/models/demo/outputs", json={"job_id": job_id})
    assert response.status_code == 201, response.text
    output_id: str = response.json()["id"]
    return output_id


def test_saving_an_output_holds_each_of_its_parts(
    client: TestClient, app: FastAPI, pool: Pool, tmp_path: Path
) -> None:
    job_id, parts = finished(app, tmp_path)
    output_id = save(client, job_id)
    assert held(pool) == {(part, output_id) for part in parts}
    detail = client.get(f"/api/v1/outputs/{output_id}").json()
    assert [m["part"] for m in detail["manifest"]] == parts
    assert detail["arranged_from"] == []


def test_deleting_an_output_releases_its_parts(
    client: TestClient, app: FastAPI, pool: Pool, tmp_path: Path
) -> None:
    job_id, _ = finished(app, tmp_path)
    output_id = save(client, job_id)
    assert client.delete(f"/api/v1/outputs/{output_id}").status_code == 204
    assert held(pool) == set()


def test_deleting_the_model_releases_every_output_s_parts(
    client: TestClient, app: FastAPI, pool: Pool, tmp_path: Path
) -> None:
    job_id, _ = finished(app, tmp_path)
    first, second = save(client, job_id), save(client, job_id)
    assert {holder for _, holder in held(pool)} == {first, second}
    assert client.delete("/api/v1/models/demo").status_code == 204
    assert held(pool) == set()


class _HoldFails(BlobRefs):
    """`blob_refs` with Postgres gone while the save holds its Parts."""

    def add(self, key: str, holder_kind: str, holder_id: str) -> None:
        raise psycopg.OperationalError("the server closed the connection")


def test_a_save_whose_hold_fails_saves_nothing(
    app: FastAPI, pg_conninfo: str, tmp_path: Path
) -> None:
    """Held first: a failed hold is a 500 with nothing on disk, so a retry is safe."""
    job_id, _ = finished(app, tmp_path)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        store_pool(pg_conninfo) as opened,
    ):
        state: AppState = getattr(app.state, STATE_ATTR)
        state.refs = _HoldFails(opened)
        response = client.post("/api/v1/models/demo/outputs", json={"job_id": job_id})
        assert response.status_code == 500
        assert client.get("/api/v1/models/demo/outputs").json() == []


def test_a_save_whose_write_fails_holds_nothing(
    client: TestClient,
    app: FastAPI,
    pool: Pool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id, _ = finished(app, tmp_path)

    def full(*_: object, **__: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(OutputStore, "create", full)
    with pytest.raises(OSError, match="No space left"):
        client.post("/api/v1/models/demo/outputs", json={"job_id": job_id})
    assert held(pool) == set()


class _HoldLandsThenFails(BlobRefs):
    """The hold's INSERT committed, then the connection dropped before it answered."""

    def add(self, key: str, holder_kind: str, holder_id: str) -> None:
        super().add(key, holder_kind, holder_id)
        raise psycopg.OperationalError("the server closed the connection")


def test_a_hold_that_fails_part_way_is_released(
    app: FastAPI, pg_conninfo: str, tmp_path: Path
) -> None:
    job_id, _ = finished(app, tmp_path)
    with (
        TestClient(app, raise_server_exceptions=False) as client,
        store_pool(pg_conninfo) as opened,
    ):
        state: AppState = getattr(app.state, STATE_ATTR)
        state.refs = _HoldLandsThenFails(opened)
        response = client.post("/api/v1/models/demo/outputs", json={"job_id": job_id})
        assert response.status_code == 500
        assert held(opened) == set()


def test_a_save_cancelled_mid_write_keeps_its_holds(
    app: FastAPI, pool: Pool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write's thread cannot be stopped and may still finish: a cancelled save keeps
    its holds rather than leave a written output unheld."""
    job_id, parts = finished(app, tmp_path)

    def cancelled(*_: object, **__: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(OutputStore, "create", cancelled)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/api/v1/models/demo/outputs", json={"job_id": job_id})
    assert {part for part, _ in held(pool)} == set(parts)


def test_saving_an_arrange_job_records_its_sources_and_holds_its_parts(
    client: TestClient, app: FastAPI, pool: Pool, tmp_path: Path
) -> None:
    _, job, written = asyncio.run(finished_job(tmp_path, job_id="arr-1"))
    sources = ["a" * 32, "c" * 32]
    inputs = ArrangeInputs(
        items=[PackItem(part=part_of(written.manifest[0]), count=2)],
        plate=PlateSize(key="default", width=256.0, depth=256.0),
        sources=sources,
    )
    arranged = job.model_copy(update={"kind": "arrange", "inputs": inputs.model_dump(mode="json")})
    app.dependency_overrides[get_render] = lambda: OneJob(arranged)
    output_id = save(client, "arr-1")
    detail = client.get(f"/api/v1/outputs/{output_id}").json()
    assert detail["arranged_from"] == sources
    assert held(pool) == {(m.part, output_id) for m in written.manifest}
