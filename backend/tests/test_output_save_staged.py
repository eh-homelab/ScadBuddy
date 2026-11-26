"""Release B's writes (#7257, task B4): a save stages its files on the pod's spool, never
on the share, and an edit writes the row and the blob only. The outputs root is a plain
file in these tests, so any read or write under it raises."""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.library.output_rows import output_blob_key
from scadbuddy.library.outputs import (
    ORPHAN_HOLD_GRACE,
    BackfillState,
    OutputStore,
)
from scadbuddy.render.jobs import Job
from scadbuddy.store.content_models import StoreFullError
from scadbuddy.store.index import Pool
from tests.support.arrange import finished_job
from tests.support.row_outputs import output_store
from tests.support.store import store_pool

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened


def _block_share(store: OutputStore) -> Path:
    """The outputs root replaced by a plain file: any access under it raises."""
    root = store.paths.root / "outputs"
    if root.is_dir():
        shutil.rmtree(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_bytes(b"not a folder")
    return root


async def _store_and_job(tmp_path: Path, pool: Pool) -> tuple[OutputStore, Job, Path]:
    paths, job, written = await finished_job(tmp_path)
    files = tmp_path / "extra" / "files"
    files.mkdir(parents=True)
    (files / "a.stl").write_bytes(b"a stl")
    job = job.model_copy(update={"outputs": [written.model_copy(update={"files": ["a.stl"]})]})
    store = output_store(tmp_path, pool, paths=paths)
    _block_share(store)
    return store, job, files


def _spooled(store: OutputStore) -> list[str]:
    assert store.bytes is not None
    spool = store.bytes.spool
    return sorted(os.listdir(spool)) if spool.is_dir() else []


async def test_save_writes_no_output_folder(tmp_path: Path, pool: Pool) -> None:
    store, job, files = await _store_and_job(tmp_path, pool)
    meta = await store.save(job, name="first", files_dir=files)
    row = store.rows.get(meta.id)
    assert row is not None and row.bytes_at is not None
    assert row.blobs.model is not None and row.blobs.preview is not None
    assert [item.name for item in store.rows.files(meta.id)] == ["a.stl"]
    assert store.bytes is not None
    assert await store.bytes.read(row.blobs.model)
    assert (store.paths.root / "outputs").read_bytes() == b"not a folder"


async def test_save_stages_on_spool_and_cleans_up(
    tmp_path: Path, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, job, files = await _store_and_job(tmp_path, pool)
    staged: list[Path] = []
    assert store.bytes is not None
    put_folder = store.bytes.put_folder

    async def seen(directory: Path, *args: Any, **kwargs: Any) -> Any:
        staged.append(directory)
        return await put_folder(directory, *args, **kwargs)

    monkeypatch.setattr(store.bytes, "put_folder", seen)
    await store.save(job, name="first", files_dir=files)
    assert len(staged) == 1 and staged[0].parent == store.bytes.spool
    assert _spooled(store) == []

    async def refused(directory: Path, *args: Any, **kwargs: Any) -> Any:
        staged.append(directory)
        raise StoreFullError("no room")

    monkeypatch.setattr(store.bytes, "put_folder", refused)
    with pytest.raises(StoreFullError):
        await store.save(job, name="second", files_dir=files)
    assert staged[1].parent == store.bytes.spool
    assert _spooled(store) == []


async def test_saving_yields_the_staged_3mf(tmp_path: Path, pool: Pool) -> None:
    store, job, files = await _store_and_job(tmp_path, pool)
    async with store.saving(job, name="first", files_dir=files) as saved:
        assert saved.model_3mf.is_file()
        data = saved.model_3mf.read_bytes()
        row = store.rows.get(saved.meta.id)
        assert row is not None and row.blobs.model is not None
    assert store.bytes is not None
    assert await store.bytes.read(row.blobs.model) == data
    assert not saved.model_3mf.exists()
    assert _spooled(store) == []


async def test_rename_and_backfill_write_rows_only(tmp_path: Path, pool: Pool) -> None:
    store, job, files = await _store_and_job(tmp_path, pool)
    meta = await store.save(job, name="first", files_dir=files)
    assert store.rename(meta.id, "renamed").name == "renamed"
    store.start_backfill(meta.id, "j2")
    assert store.backfill(meta.id) == BackfillState(job_id="j2")
    store.fail_backfill(meta.id, "j2", "it broke")
    assert store.backfill(meta.id) == BackfillState(job_id="j2", error="it broke")
    store.clear_backfill(meta.id)
    assert store.backfill(meta.id) is None
    assert job.outputs
    store.attach_backfill(meta.id, job.outputs[0])
    assert store.record(meta.id) is not None
    store.delete(meta.id)
    assert store.rows.get(meta.id) is None
    assert (store.paths.root / "outputs").read_bytes() == b"not a folder"


async def test_put_thumbnail_writes_no_file(tmp_path: Path, pool: Pool) -> None:
    store, job, files = await _store_and_job(tmp_path, pool)
    meta = await store.save(job, name="first", files_dir=files)
    await store.put_thumbnail(meta.id, b"uploaded png")
    assert store.bytes is not None
    assert await store.bytes.read(output_blob_key(meta.id, "thumbnail")) == b"uploaded png"
    # A drawn thumbnail never replaces an upload.
    assert await store.put_drawn_thumbnail(meta.id, b"drawn png") is False
    assert await store.bytes.read(output_blob_key(meta.id, "thumbnail")) == b"uploaded png"


async def test_put_drawn_thumbnail_without_one_keeps_it(tmp_path: Path, pool: Pool) -> None:
    store, job, files = await _store_and_job(tmp_path, pool)
    meta = await store.save(job, name="first", files_dir=files)
    row = store.rows.get(meta.id)
    assert row is not None and row.blobs.thumbnail is None
    assert await store.put_drawn_thumbnail(meta.id, b"drawn png") is True
    assert store.bytes is not None
    assert await store.bytes.read(output_blob_key(meta.id, "thumbnail")) == b"drawn png"


async def test_claim_writes_no_folder(tmp_path: Path, pool: Pool) -> None:
    store, job, files = await _store_and_job(tmp_path, pool)
    output_id = "a" * 32
    assert store.claim(job.slug, output_id) is True
    assert store.claim(job.slug, output_id) is False
    meta = await store.save(job, files_dir=files, output_id=output_id)
    assert meta.id == output_id
    assert store.claim(job.slug, output_id) is False


async def test_live_save_keeps_its_claim(
    tmp_path: Path, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4 review M1: a save that runs longer than `ORPHAN_HOLD_GRACE` keeps its claim. It
    refreshes the claim while it runs, so the prune leaves it and another attempt cannot
    take the id."""
    store, job, files = await _store_and_job(tmp_path, pool)
    output_id = "b" * 32
    assert store.claim(job.slug, output_id)
    store.claim_refresh_seconds = 0.05
    assert store.bytes is not None
    put_folder = store.bytes.put_folder
    entered = asyncio.Event()
    go_on = asyncio.Event()

    async def slow(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await go_on.wait()
        return await put_folder(*args, **kwargs)

    def age() -> None:
        with pool.connection() as conn:
            conn.execute(
                "UPDATE output_claims SET claimed_at = clock_timestamp() - %s WHERE output_id = %s",
                (ORPHAN_HOLD_GRACE + timedelta(hours=1), output_id),
            )

    def claimed_young() -> bool:
        with pool.connection() as conn:
            found = conn.execute(
                "SELECT claimed_at > clock_timestamp() - %s AS young FROM output_claims"
                " WHERE output_id = %s",
                (ORPHAN_HOLD_GRACE, output_id),
            ).fetchone()
        return found is not None and bool(found["young"])

    monkeypatch.setattr(store.bytes, "put_folder", slow)
    task = asyncio.create_task(store.save(job, files_dir=files, output_id=output_id, claimed=True))
    try:
        await asyncio.wait_for(entered.wait(), 30)
        age()
        for _ in range(200):
            if claimed_young():
                break
            await asyncio.sleep(0.05)
        assert claimed_young()
        assert store.rows.prune_claims(ORPHAN_HOLD_GRACE) == 0
        assert store.claim(job.slug, output_id) is False
    finally:
        go_on.set()
    meta = await task
    assert meta.id == output_id


async def test_save_cancelled_during_puts_leaves_the_spool_empty(
    tmp_path: Path, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4 review: a save cancelled while it uploads leaves nothing staged on the spool."""
    store, job, files = await _store_and_job(tmp_path, pool)
    assert store.bytes is not None
    entered = asyncio.Event()

    async def stalled(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(store.bytes, "put_folder", stalled)
    task = asyncio.create_task(store.save(job, name="cancelled", files_dir=files))
    await asyncio.wait_for(entered.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _spooled(store) == []


async def test_save_cancelled_during_prepare_leaves_the_spool_empty(
    tmp_path: Path, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4 review: a cancel cannot stop `prepare`'s thread, so the save waits for it to end
    before it removes the staged folder; nothing it wrote stays on the spool. The thread
    stops halfway, before it copies the extra files."""
    store, job, files = await _store_and_job(tmp_path, pool)
    entered = threading.Event()
    go_on = threading.Event()
    ended = threading.Event()
    copytree = shutil.copytree

    def slow(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        go_on.wait(10)
        try:
            return copytree(*args, **kwargs)
        finally:
            ended.set()

    monkeypatch.setattr(shutil, "copytree", slow)
    task = asyncio.create_task(store.save(job, name="cancelled", files_dir=files))
    assert await asyncio.to_thread(entered.wait, 30)
    task.cancel()
    await asyncio.sleep(0.2)
    go_on.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(ended.wait, 30)
    assert _spooled(store) == []


async def test_unclaimed_save_refreshes_no_claim(
    tmp_path: Path, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B4 review: a save under an id it did not claim (a fresh id) keeps no claim young."""
    store, job, files = await _store_and_job(tmp_path, pool)
    store.claim_refresh_seconds = 0.01
    refreshed: list[str] = []
    monkeypatch.setattr(store.rows, "refresh_claim", refreshed.append)
    assert store.bytes is not None
    put_folder = store.bytes.put_folder

    async def slow(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.2)
        return await put_folder(*args, **kwargs)

    monkeypatch.setattr(store.bytes, "put_folder", slow)
    await store.save(job, files_dir=files, output_id="c" * 32)
    assert refreshed == []
