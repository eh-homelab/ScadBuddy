"""A save through `POST /outputs` puts the output's bytes in the blob store (#7256,
outputs-in-Postgres spec 2026-11-23 §6.6), and a store with no room fails the save with
507 and leaves nothing behind (Ruling R2)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import scadbuddy.library.output_operations as output_operations
from scadbuddy.api import outputs as outputs_api
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.output_bytes import OutputBytes
from scadbuddy.library.output_rows import OUTPUT_HOLDER
from scadbuddy.library.outputs import hold_parts
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
from scadbuddy.print_library.component import PRINT_LIBRARY
from scadbuddy.store.content_models import StoreFullError
from scadbuddy.store.index import Pool
from tests.api.conftest import PNG_BYTES, wait_for_job
from tests.support.operations import press
from tests.support.store import record_reads, store_pool

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def s3_client(settings: Settings, s3: S3Settings) -> Iterator[TestClient]:
    on_s3 = settings.model_copy(
        update={
            "store_backend": "s3",
            "store_s3_endpoint": s3.endpoint,
            "store_s3_bucket": s3.bucket,
            "store_s3_prefix": s3.prefix,
            "store_s3_access_key_id": s3.access_key_id,
            "store_s3_secret_access_key": s3.secret_access_key,
        }
    )
    with TestClient(create_app(on_s3)) as client:
        yield client


@pytest.fixture
def native_s3_client(settings: Settings, s3: S3Settings) -> Iterator[TestClient]:
    on_s3 = settings.model_copy(
        update={
            "store_backend": "s3",
            "store_s3_endpoint": s3.endpoint,
            "store_s3_bucket": s3.bucket,
            "store_s3_prefix": s3.prefix,
            "store_s3_access_key_id": s3.access_key_id,
            "store_s3_secret_access_key": s3.secret_access_key,
            "library_backend": "native",
        }
    )
    with TestClient(create_app(on_s3)) as client:
        yield client


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened


def _state(client: TestClient) -> AppState:
    app = client.app
    assert isinstance(app, FastAPI)
    state: AppState = getattr(app.state, STATE_ATTR)
    return state


@pytest.fixture
def model(s3_client: TestClient) -> str:
    """A committed template: the S3 store renders from a git snapshot."""
    created = s3_client.post(
        "/api/v1/models", json={"name": "Widget", "source": "width = 10;\n"}, headers=press()
    )
    assert created.status_code == 201, created.text
    slug: str = created.json()["slug"]
    return slug


def _finished_job(client: TestClient, slug: str) -> str:
    queued = client.post(f"/api/v1/models/{slug}/render", json={"params": {"width": 12}})
    assert queued.status_code == 202, queued.text
    job_id: str = queued.json()["job_id"]
    wait_for_job(client, job_id)
    return job_id


def _output_holds(pool: Pool) -> list[tuple[str, str]]:
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT key, holder_id FROM blob_refs WHERE holder_kind = %s", (OUTPUT_HOLDER,)
        ).fetchall()
    return [(str(row["key"]), str(row["holder_id"])) for row in found]


def test_save_on_s3_names_its_blobs(s3_client: TestClient, model: str, pool: Pool) -> None:
    saved = s3_client.post(
        f"/api/v1/models/{model}/outputs",
        json={"job_id": _finished_job(s3_client, model)},
        headers=press(),
    )
    assert saved.status_code == 201, saved.text
    output_id = saved.json()["id"]
    rows = _state(s3_client).outputs.rows
    assert rows is not None
    row = rows.get(output_id)
    assert row is not None and row.bytes_at is not None
    assert row.blobs.model == f"output-{output_id}-model"
    assert row.blobs.preview == f"output-{output_id}-preview"
    held = {key for key, holder in _output_holds(pool) if holder == output_id}
    assert {row.blobs.model, row.blobs.preview} <= held


def test_local_store_puts_output_bytes(client: TestClient, paths: DataPaths, pool: Pool) -> None:
    """#7257 plan D7: on the `local` store the outputs have a blob store of their own,
    under `data/output-blobs/`, so a save names its blobs there as on `s3`."""
    assert _state(client).store.backend == "local"
    created = client.post(
        "/api/v1/models", json={"name": "Widget", "source": "width = 10;\n"}, headers=press()
    )
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    saved = client.post(
        f"/api/v1/models/{slug}/outputs",
        json={"job_id": _finished_job(client, slug)},
        headers=press(),
    )
    assert saved.status_code == 201, saved.text
    output_id = saved.json()["id"]
    rows = _state(client).outputs.rows
    assert rows is not None
    row = rows.get(output_id)
    assert row is not None and row.bytes_at is not None
    assert row.blobs.model == f"output-{output_id}-model"
    held = {key for key, holder in _output_holds(pool) if holder == output_id}
    assert {row.blobs.model, row.blobs.preview} <= held
    assert any((paths.output_blobs / "output").iterdir())


def test_output_blobs_folder_only_on_local(
    client: TestClient, paths: DataPaths, tmp_path: Path
) -> None:
    """#7257 A3 review M2: `ensure` makes `data/output-blobs/` only for `local`'s output
    store, never on the share of an `s3` or `bambuddy` store."""
    assert _state(client).store.backend == "local"
    assert paths.output_blobs.is_dir()
    other = DataPaths(root=tmp_path / "other-data")
    other.ensure()
    assert other.outputs.is_dir()
    assert not other.output_blobs.exists()
    other.ensure(output_blobs=True)
    assert other.output_blobs.is_dir()


def test_s3_start_makes_no_output_blobs_folder(s3_client: TestClient) -> None:
    """#7257 A3 review M2: an app on `s3` makes no `data/output-blobs/`."""
    state = _state(s3_client)
    assert state.store.backend == "s3"
    assert state.paths.outputs.is_dir()
    assert not state.paths.output_blobs.exists()


def test_save_on_s3_reads_its_files_off_the_loop(
    s3_client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _finished_job(s3_client, model)
    outputs = _state(s3_client).outputs
    assert outputs.bytes is not None
    # The save stages its files on the spool (#7257, plan D8).
    reads = record_reads(monkeypatch, outputs.bytes.spool)
    saved = s3_client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": job_id}, headers=press()
    )
    monkeypatch.undo()
    assert saved.status_code == 201, saved.text
    assert reads
    # No read of the output's files ran on the loop's thread.
    assert not any(reads)


def test_create_run_files_from_staged_3mf(
    native_s3_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The save files the 3MF it staged and put, with no download of the model blob
    (final review M4, #7257 task B4): no `OutputBytes.local_blob` while it runs."""
    client = native_s3_client
    created = client.post(
        "/api/v1/models", json={"name": "Widget", "source": "width = 10;\n"}, headers=press()
    )
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    job_id = _finished_job(client, slug)
    downloads: list[str] = []
    local_blob = OutputBytes.local_blob

    def spy(self: OutputBytes, key: str, suffix: str) -> Any:
        downloads.append(key)
        return local_blob(self, key, suffix)

    monkeypatch.setattr(OutputBytes, "local_blob", spy)
    saved = client.post(f"/api/v1/models/{slug}/outputs", json={"job_id": job_id}, headers=press())
    monkeypatch.undo()
    assert saved.status_code == 201, saved.text
    output_id = saved.json()["id"]
    native = _state(client).components.get(PRINT_LIBRARY).native
    assert native is not None
    assert [row.output_id for row in native.rows.filed_rows_for_output(output_id)] == [output_id]
    assert downloads == []
    assert not (_state(client).paths.root / "outputs" / slug).exists()


def test_thumbnail_store_full_is_507_and_changes_nothing(
    s3_client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = s3_client.post(
        f"/api/v1/models/{model}/outputs",
        json={"job_id": _finished_job(s3_client, model)},
        headers=press(),
    )
    assert saved.status_code == 201, saved.text
    output_id = saved.json()["id"]
    state = _state(s3_client)
    assert state.store.content is not None

    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise StoreFullError("no room")

    monkeypatch.setattr(state.store.content, "put", refused)
    response = s3_client.put(
        f"/api/v1/outputs/{output_id}/thumbnail",
        files={"file": ("capture.png", PNG_BYTES, "image/png")},
        headers=press(),
    )
    assert response.status_code == 507, response.text
    assert "the blob store has no room for this thumbnail" in response.json()["detail"]
    row = state.outputs.rows.get(output_id)
    assert row is not None and row.blobs.thumbnail is None


def test_save_store_full_is_507_and_cleans_up(
    s3_client: TestClient,
    model: str,
    paths: DataPaths,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = _finished_job(s3_client, model)
    state = _state(s3_client)
    held_before: list[list[tuple[str, str]]] = []

    def recorded(*args: Any, **kwargs: Any) -> None:
        hold_parts(*args, **kwargs)
        held_before.append(_output_holds(pool))

    monkeypatch.setattr(output_operations, "hold_parts", recorded)
    assert state.outputs.bytes is not None and state.store.content is not None
    # Every put past this cap is refused; the render's own blobs are in already.
    state.store.content.max_total_bytes = 1
    response = s3_client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": job_id}, headers=press()
    )
    assert response.status_code == 507, response.text
    assert "the blob store has no room for this output" in response.json()["detail"]
    # Nothing on the share, and nothing left staged on the spool (#7257).
    assert not (paths.root / "outputs" / model).exists()
    spool = state.outputs.bytes.spool
    assert not spool.is_dir() or [p for p in spool.iterdir() if p.is_dir()] == []
    assert state.outputs.rows is not None
    assert state.outputs.rows.for_slug(model) == []
    # The save held its Parts before the puts; neither they nor any byte hold is left.
    assert len(held_before) == 1 and held_before[0] != []
    assert _output_holds(pool) == []


def test_save_reads_its_detail_off_the_loop(
    s3_client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The detail reads the blob index (`thumbnail_version`, `has_role`): never on the loop.
    job_id = _finished_job(s3_client, model)
    on_loop: list[bool] = []
    detail = outputs_api.detail

    def probed(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_loop.append(False)
        else:
            on_loop.append(True)
        return detail(*args, **kwargs)

    monkeypatch.setattr(outputs_api, "detail", probed)
    saved = s3_client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": job_id}, headers=press()
    )
    assert saved.status_code == 201, saved.text
    assert on_loop
    assert not any(on_loop)
