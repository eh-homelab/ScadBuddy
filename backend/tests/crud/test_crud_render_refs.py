"""The render worker's blob refs and piece geometry through the CRUD routes (#5298 Task 15,
inventory R11, R12): `RemoteBlobRefs` and `RemoteGeometryIndex` over `api/crud_render.py`,
on the API's own `BlobRefs` and `GeometryIndex`. Every write is natural: sent twice, it
leaves what it left once. A ref holds a blob for a render job only."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.crud.render import BASE, RemoteBlobRefs, RemoteGeometryIndex
from scadbuddy.render.job_models import Job
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.geometry import GeometryIndex, GeometryRow
from scadbuddy.workflows.pipelines import PROJECT_TIMEOUT
from tests.crud.crud_support import served_render_refs

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def projection(pg_conninfo: str) -> Iterator[JobProjection]:
    store = JobProjection(pg_conninfo, pool_size=4)
    store.open()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
async def remote(
    projection: JobProjection,
) -> AsyncIterator[tuple[RemoteBlobRefs, RemoteGeometryIndex]]:
    async with served_render_refs(projection, unavailable_budget=0.5) as served:
        yield served


@pytest.fixture
def refs(remote: tuple[RemoteBlobRefs, RemoteGeometryIndex]) -> RemoteBlobRefs:
    return remote[0]


@pytest.fixture
def geometry(remote: tuple[RemoteBlobRefs, RemoteGeometryIndex]) -> RemoteGeometryIndex:
    return remote[1]


def _job(projection: JobProjection) -> Job:
    job = Job(
        id=uuid.uuid4().hex,
        slug="demo",
        params={"w": 12},
        inputs={"params": {"w": 12}},
        model_version="abc123",
        created_at=datetime.now(UTC),
    )
    key = uuid.uuid4().hex
    return projection.accept(job, key, workflow_id=f"render-{key}", run_id="run-1")


def _refs(projection: JobProjection) -> list[tuple[str, str, str]]:
    with projection.pool.connection() as conn:
        rows = conn.execute(
            "SELECT key, holder_kind, holder_id FROM blob_refs ORDER BY key"
        ).fetchall()
    return [(row["key"], row["holder_kind"], row["holder_id"]) for row in rows]


# -- blob refs (R11) -----------------------------------------------------------


async def test_a_ref_sent_twice_writes_one_row(
    refs: RemoteBlobRefs, projection: JobProjection
) -> None:
    job = _job(projection)
    await asyncio.to_thread(refs.add, "piece-1", "job", job.id)
    await asyncio.to_thread(refs.add, "piece-1", "job", job.id)
    await asyncio.to_thread(refs.add, "piece-2", "job", job.id)
    assert _refs(projection) == [("piece-1", "job", job.id), ("piece-2", "job", job.id)]


@pytest.mark.parametrize("holder_kind", ["output", "preset", "slice", "attempt"])
async def test_a_ref_for_any_holder_but_a_job_is_a_403(
    refs: RemoteBlobRefs,
    projection: JobProjection,
    holder_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A render credential pins blobs for its jobs only, and the refusal is final: no
    re-exchange, no retry inside the budget."""
    job = _job(projection)
    await asyncio.to_thread(refs.add, "piece-0", "job", job.id)
    # #5655: the refusal is judged by what the client did, not by the wall time, which a
    # loaded runner stretched past a 0.5 s budget. One request, no wait before another,
    # no credential refresh.
    client = refs._client
    sent: list[str] = []
    waits: list[float] = []
    refreshes: list[object] = []
    request = client._http.request

    def counted(method: str, path: str, **kwargs: Any) -> httpx.Response:
        sent.append(path)
        return request(method, path, **kwargs)

    monkeypatch.setattr(client._http, "request", counted)
    monkeypatch.setattr(client, "_sleep", waits.append)
    monkeypatch.setattr(client._auth, "refresh", refreshes.append)
    with pytest.raises(CrudRefusedError) as refused:
        await asyncio.to_thread(refs.add, "piece-1", holder_kind, job.id)
    assert len(sent) == 1, sent
    assert waits == []
    assert refreshes == []
    assert refused.value.status == 403
    assert refused.value.problem.get("reason") == "holder-kind"
    assert _refs(projection) == [("piece-0", "job", job.id)]


async def test_a_ref_for_a_missing_job_is_a_404_and_writes_nothing(
    refs: RemoteBlobRefs, projection: JobProjection
) -> None:
    """The route refuses a holder that is no render job, so a stolen credential cannot
    pin blobs under made-up holders."""
    with pytest.raises(CrudRefusedError) as refused:
        await asyncio.to_thread(
            refs._client.request,
            "POST",
            f"{BASE}/blob-refs",
            json={"key": "piece-1", "holder_kind": "job", "holder_id": "absent"},
        )
    assert refused.value.status == 404
    assert _refs(projection) == []


async def test_the_workers_ref_for_a_gone_job_is_dropped_not_raised(
    refs: RemoteBlobRefs, projection: JobProjection
) -> None:
    """The SQL write held a blob for any holder id, a pruned job's too, and the sweep
    then dropped the orphan ref. A ref for a job that is gone holds nothing, so the
    worker's render goes on without it."""
    await asyncio.to_thread(refs.add, "piece-1", "job", "absent")
    assert _refs(projection) == []


async def test_a_slow_route_makes_a_ref_raise_inside_the_project_attempt(
    refs: RemoteBlobRefs, projection: JobProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`project` holds a done job's blobs inside its 10 s attempt, as its row writes."""
    job = _job(projection)

    def slow(*_: object) -> None:
        time.sleep(6)

    monkeypatch.setattr(projection, "read", slow)
    started = time.monotonic()
    with pytest.raises(CrudUnavailableError):
        await asyncio.to_thread(refs.add, "piece-1", "job", job.id)
    assert time.monotonic() - started < PROJECT_TIMEOUT.total_seconds()


# -- piece geometry (R12) ------------------------------------------------------


async def test_geometry_round_trips_and_a_put_sent_twice_writes_one_row(
    geometry: RemoteGeometryIndex, projection: JobProjection
) -> None:
    assert await asyncio.to_thread(geometry.get, "geo-1") is None
    main = {"warnings": [], "nested": {"a": 1}}
    await asyncio.to_thread(geometry.put, "geo-1", "piece-1", ["c0", "c1"], main)
    await asyncio.to_thread(geometry.put, "geo-1", "piece-1", ["c0", "c1"], main)
    assert await asyncio.to_thread(geometry.get, "geo-1") == GeometryRow(
        "piece-1", ["c0", "c1"], main
    )
    # The newest finished piece wins.
    await asyncio.to_thread(geometry.put, "geo-1", "piece-2", ["c0"], {})
    assert await asyncio.to_thread(geometry.get, "geo-1") == GeometryRow("piece-2", ["c0"], {})
    assert GeometryIndex(projection.pool).get("geo-1") == GeometryRow("piece-2", ["c0"], {})


async def test_a_forget_keeps_a_replaced_row_and_sent_twice_succeeds(
    geometry: RemoteGeometryIndex, projection: JobProjection
) -> None:
    await asyncio.to_thread(geometry.put, "geo-1", "piece-2", ["c0"], {})
    # Read as piece-1 before piece-2 replaced it: piece-2's row stays.
    await asyncio.to_thread(geometry.forget, "geo-1", "piece-1")
    assert await asyncio.to_thread(geometry.get, "geo-1") is not None
    await asyncio.to_thread(geometry.forget, "geo-1", "piece-2")
    await asyncio.to_thread(geometry.forget, "geo-1", "piece-2")
    assert await asyncio.to_thread(geometry.get, "geo-1") is None
