"""A saved output holds its Parts; deleting it, or its model, lets them go (spec
2026-09-27 §7, Review Focus 3)."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.api.deps import STATE_ATTR, AppState, get_render
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OUTPUT_HOLDER, THUMBNAIL_NAME, OutputStore, _cover_from_3mf
from scadbuddy.operations.component import OPERATIONS
from scadbuddy.render.bambu3mf import PLATE_THUMBNAIL
from scadbuddy.render.job_models import Job, JobNotFoundError
from scadbuddy.store.refs import BlobRefs
from scadbuddy.tools.export_openapi import export
from scadbuddy.workflows.arrange import part_of
from scadbuddy.workflows.models import ArrangeInputs, PackItem, PlateSize
from tests.support.arrange import finished_job
from tests.support.operations import press
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


def answer_for(app: FastAPI, job: Job) -> None:
    """Make the app's render service answer for ``job``: the routes through `get_render`,
    the output operations' checks through the app's own (#1054)."""
    app.dependency_overrides[get_render] = lambda: OneJob(job)
    state: AppState = getattr(app.state, STATE_ATTR)
    setattr(state.render.store, "read", OneJob(job).read)  # noqa: B010


def finished(app: FastAPI, tmp_path: Path) -> tuple[str, list[str]]:
    """A done job the app's render service answers for: its id and its output's Parts."""
    _, job, written = asyncio.run(finished_job(tmp_path))
    answer_for(app, job)
    return job.id, [m.part for m in written.manifest]


def save(client: TestClient, job_id: str) -> str:
    response = client.post("/api/v1/models/demo/outputs", json={"job_id": job_id}, headers=press())
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
    assert client.delete(f"/api/v1/outputs/{output_id}", headers=press()).status_code == 204
    assert held(pool) == set()


def test_deleting_the_model_releases_every_output_s_parts(
    client: TestClient, app: FastAPI, pool: Pool, tmp_path: Path
) -> None:
    job_id, _ = finished(app, tmp_path)
    first, second = save(client, job_id), save(client, job_id)
    assert {holder for _, holder in held(pool)} == {first, second}
    assert client.delete("/api/v1/models/demo", headers=press()).status_code == 204
    assert held(pool) == set()


class _FirstReleaseFails(BlobRefs):
    """`blob_refs` with Postgres gone for the first output the delete releases."""

    failed: str | None = None

    def drop_holder(self, holder_kind: str, holder_id: str) -> None:
        if self.failed is None:
            self.failed = holder_id
            raise psycopg.OperationalError("the server closed the connection")
        super().drop_holder(holder_kind, holder_id)


def test_deleting_the_model_releases_the_other_outputs_when_one_release_fails(
    client: TestClient, app: FastAPI, pg_conninfo: str, tmp_path: Path
) -> None:
    """#1782: one output's failed release costs only that output; the rest are released."""
    job_id, _ = finished(app, tmp_path)
    with store_pool(pg_conninfo) as opened:
        state: AppState = getattr(app.state, STATE_ATTR)
        refs = _FirstReleaseFails(opened)
        state.refs = refs
        saved = {save(client, job_id), save(client, job_id), save(client, job_id)}
        assert client.delete("/api/v1/models/demo", headers=press()).status_code == 204
        assert refs.failed in saved
        assert {holder for _, holder in held(opened)} == {refs.failed}


class _HoldFails(BlobRefs):
    """`blob_refs` with Postgres gone while the save holds its Parts."""

    def add(
        self, key: str, holder_kind: str, holder_id: str, *, budget: float | None = None
    ) -> None:
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
        response = client.post(
            "/api/v1/models/demo/outputs", json={"job_id": job_id}, headers=press()
        )
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
    # The base's answer for a copy that fails (#427), raised after the release.
    response = client.post("/api/v1/models/demo/outputs", json={"job_id": job_id}, headers=press())
    assert response.status_code == 404
    assert "is gone" in response.json()["detail"]
    assert held(pool) == set()


class _HoldLandsThenFails(BlobRefs):
    """The hold's INSERT committed, then the connection dropped before it answered."""

    def add(
        self, key: str, holder_kind: str, holder_id: str, *, budget: float | None = None
    ) -> None:
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
        response = client.post(
            "/api/v1/models/demo/outputs", json={"job_id": job_id}, headers=press()
        )
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
        client.post("/api/v1/models/demo/outputs", json={"job_id": job_id}, headers=press())
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
    answer_for(app, arranged)
    output_id = save(client, "arr-1")
    detail = client.get(f"/api/v1/outputs/{output_id}").json()
    assert detail["arranged_from"] == sources
    assert held(pool) == {(m.part, output_id) for m in written.manifest}


@pytest.mark.parametrize("plates", [1, 3])
def test_an_arranged_output_records_its_plates_and_a_cover(
    client: TestClient, app: FastAPI, pool: Pool, tmp_path: Path, plates: int
) -> None:
    """#2281: the saved record says what the arrange job produced, its plates and a
    cover, though no viewer captured one."""
    _, job, written = asyncio.run(finished_job(tmp_path, job_id="arr-3", plates=plates))
    assert job.result is not None and len(job.result.plates) == (plates if plates > 1 else 0)
    inputs = ArrangeInputs(
        items=[PackItem(part=part_of(written.manifest[0]), count=2 * plates)],
        plate=PlateSize(key="default", width=256.0, depth=256.0),
        sources=["a" * 32],
    )
    arranged = job.model_copy(update={"kind": "arrange", "inputs": inputs.model_dump(mode="json")})
    answer_for(app, arranged)
    output_id = save(client, "arr-3")
    detail = client.get(f"/api/v1/outputs/{output_id}").json()
    assert detail["has_thumbnail"] is True
    cover = client.get(f"/api/v1/outputs/{output_id}/thumbnail")
    assert cover.status_code == 200 and cover.content.startswith(b"\x89PNG")
    recorded = [(p["index"], p["bbox_mm"], p["colors"]) for p in detail["layout_plates"]]
    expected = [(p.index, p.bbox_mm.model_dump(mode="json"), p.colors) for p in job.result.plates]
    assert recorded == expected
    listed = client.get(f"/api/v1/outputs/{output_id}/plates").json()
    assert len(listed) == plates and all(p["has_thumbnail"] for p in listed)


def test_the_edit_link_of_an_arranged_output_says_it_was_arranged(
    client: TestClient, app: FastAPI, pool: Pool, tmp_path: Path
) -> None:
    """An arranged output has no one template state to reopen: the deep link says where
    its objects came from, so the page can say so rather than open empty inputs."""
    _, job, written = asyncio.run(finished_job(tmp_path, job_id="arr-2"))
    sources = ["a" * 32]
    inputs = ArrangeInputs(
        items=[PackItem(part=part_of(written.manifest[0]), count=1)],
        plate=PlateSize(key="default", width=256.0, depth=256.0),
        sources=sources,
    )
    arranged = job.model_copy(update={"kind": "arrange", "inputs": inputs.model_dump(mode="json")})
    answer_for(app, arranged)
    output_id = save(client, "arr-2")
    target = client.get(f"/api/v1/outputs/{output_id}/edit").json()
    assert target["arranged_from"] == sources


def _3mf_with_cover(path: Path, cover: bytes) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(PLATE_THUMBNAIL, cover)
    return path


def test_the_cover_from_a_3mf_lands_whole_and_leaves_no_temporary(tmp_path: Path) -> None:
    """#2556: the cover is written to a sibling and renamed into place."""
    cover = b"\x89PNG" + bytes(range(256)) * 64
    model = _3mf_with_cover(tmp_path / "model.3mf", cover)
    directory = tmp_path / "out"
    directory.mkdir()
    _cover_from_3mf(model, directory / THUMBNAIL_NAME)
    assert (directory / THUMBNAIL_NAME).read_bytes() == cover
    assert [p.name for p in directory.iterdir()] == [THUMBNAIL_NAME]


def test_a_cover_write_cut_short_leaves_no_thumbnail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2556: a write killed halfway never shows as a truncated ``thumbnail.png``, which
    ``has_thumbnail`` would report and the UI would serve broken."""
    model = _3mf_with_cover(tmp_path / "model.3mf", b"\x89PNG" + b"x" * 4096)
    directory = tmp_path / "out"
    directory.mkdir()
    real_write = Path.write_bytes

    def half_then_fail(self: Path, data: bytes) -> int:
        real_write(self, data[: len(data) // 2])
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", half_then_fail)
    with pytest.raises(OSError, match="disk full"):
        _cover_from_3mf(model, directory / THUMBNAIL_NAME)
    assert not (directory / THUMBNAIL_NAME).exists()


def test_layout_plates_is_documented_in_the_api(tmp_path: Path) -> None:
    """#2557: nothing in the UI reads ``layout_plates`` yet, so the published schema says
    what it is for and how it differs from ``plates``."""
    schema = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))
    carrying = {
        name: body["properties"]["layout_plates"].get("description", "")
        for name, body in schema["components"]["schemas"].items()
        if "layout_plates" in body.get("properties", {})
    }
    assert "OutputDetail" in carrying
    for description in carrying.values():
        assert "without opening the 3MF" in description and "`plates`" in description


@pytest.mark.parametrize("paused", ["before_the_write", "after_the_write"])
def test_overlapping_saves_under_one_id_never_touch_each_others_output(
    client: TestClient,
    app: FastAPI,
    pool: Pool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paused: str,
) -> None:
    """#5390 review: two attempts of one quote save under the operation's output id at
    once. The first is in the middle of its save when the second starts, and the
    second's write fails. The first claimed the id, so the second saves under an id of
    its own: its failure removes only its own directory and releases only its own
    Parts, and the first output and its holds survive."""
    job_id, parts = finished(app, tmp_path)
    state: AppState = getattr(app.state, STATE_ATTR)
    kind = state.components.get(OPERATIONS).kinds["output_create"]
    real = state.outputs.create
    inside = threading.Event()
    go = threading.Event()
    calls: list[str | None] = []

    def create(job: Job, **kwargs: Any) -> Any:
        calls.append(kwargs.get("output_id"))
        if len(calls) == 1:
            inside.set()
            if paused == "before_the_write":
                assert go.wait(30)
                return real(job, **kwargs)
            meta = real(job, **kwargs)
            assert go.wait(30)
            return meta
        # The second save's copy fails part way: the job's preview is gone.
        [chosen] = job.outputs
        gone = chosen.result.model_copy(update={"preview_glb": "gone/preview.glb"})
        broken = job.model_copy(update={"outputs": [chosen.model_copy(update={"result": gone})]})
        return real(broken, **kwargs)

    monkeypatch.setattr(state.outputs, "create", create)
    shared = uuid.uuid4().hex
    body = {
        "slug": "demo",
        "job_id": job_id,
        "index": 0,
        "inputs": None,
        "name": None,
        "output_id": shared,
    }
    assert client.portal is not None
    first = client.portal.start_task_soon(kind.run, body, {})
    assert inside.wait(30)
    with pytest.raises(ApiError):
        client.portal.call(kind.run, body, {})
    go.set()
    saved = first.result(30)

    assert saved["id"] == shared
    assert client.get(f"/api/v1/outputs/{shared}").status_code == 200
    assert held(pool) == {(part, shared) for part in parts}
    assert calls[0] == shared and calls[1] != shared
