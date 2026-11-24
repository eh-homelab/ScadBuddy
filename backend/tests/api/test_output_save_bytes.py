"""A save through `POST /outputs` puts the output's bytes in the blob store (#7256,
outputs-in-Postgres spec 2026-11-23 §6.6), and a store with no room fails the save with
507 and leaves nothing behind (Ruling R2)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import scadbuddy.library.output_operations as output_operations
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.output_rows import OUTPUT_HOLDER
from scadbuddy.library.outputs import THUMBNAIL_NAME, hold_parts
from scadbuddy.library.settings_store import S3Settings
from scadbuddy.main import create_app
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


def test_save_on_s3_reads_its_files_off_the_loop(
    s3_client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _finished_job(s3_client, model)
    reads = record_reads(monkeypatch, paths.outputs)
    saved = s3_client.post(
        f"/api/v1/models/{model}/outputs", json={"job_id": job_id}, headers=press()
    )
    monkeypatch.undo()
    assert saved.status_code == 201, saved.text
    assert reads
    # No read of the output's files ran on the loop's thread.
    assert not any(reads)


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
    assert not (paths.output_dir(model, output_id) / THUMBNAIL_NAME).exists()


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
    outputs = paths.outputs / model
    assert not outputs.is_dir() or list(outputs.iterdir()) == []
    assert state.outputs.rows is not None
    assert state.outputs.rows.for_slug(model) == []
    # The save held its Parts before the puts; neither they nor any byte hold is left.
    assert len(held_before) == 1 and held_before[0] != []
    assert _output_holds(pool) == []
