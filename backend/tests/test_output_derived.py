"""What readers derive of an output's bytes lives in ``output_derived`` (#7256): the
geometry analysis and its attributed form are rows, read before the old file caches,
and a miss reads the 3MF from the blob store through a temporary file. The plate cover
of a model with no picture of its own is read out of the output's model blob."""

from __future__ import annotations

import zipfile
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any

import pytest

import scadbuddy.library.outputs as outputs_module
from scadbuddy.library.output_bytes import OutputBytes
from scadbuddy.library.output_rows import ByteWrite, OutputRows
from scadbuddy.library.outputs import (
    GEOMETRY_NAME,
    MODEL_NAME,
    OutputMeta,
    OutputStore,
)
from scadbuddy.render.bambu3mf import PLATE_THUMBNAIL
from scadbuddy.render.geometry import ANALYSIS_VERSION, GeometryAnalysis, analyze_3mf
from scadbuddy.render.projection import JobProjection
from scadbuddy.store.content import ContentStore
from scadbuddy.store.index import Pool
from tests.support.arrange import finished_job
from tests.support.store import local_content, store_pool

pytestmark = pytest.mark.requires_postgres

COVER_PNG = b"\x89PNG\r\n\x1a\nplate cover"


@pytest.fixture
def projection(pg_conninfo: str) -> Iterator[JobProjection]:
    jobs = JobProjection(pg_conninfo, pool_size=2)
    jobs.open()
    try:
        yield jobs
    finally:
        jobs.close()


@pytest.fixture
def pool(pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        yield opened


@pytest.fixture
async def content(tmp_path: Path, pool: Pool) -> AsyncGenerator[ContentStore]:
    store = local_content(tmp_path / "remote", pool)
    store.spool = tmp_path / "spool"
    try:
        yield store
    finally:
        await store.aclose()


async def _copied(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> tuple[OutputStore, OutputMeta]:
    """A saved output whose bytes the byte copy put in the blob store."""
    paths, job, _ = await finished_job(tmp_path / "data")
    store = OutputStore(paths)
    store.rows = OutputRows(projection.pool)
    store.bytes = OutputBytes(content)
    meta = store.create(job, name="first")
    blobs = await store.bytes.put_folder(store.directory(meta.id), meta.id, meta.slug)
    store.rows.write_bytes([ByteWrite(meta.id, blobs, "stamp", None)])
    return store, meta


def _counting(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every path `analyze_3mf` is called on."""
    calls: list[Path] = []
    real = analyze_3mf

    def analyze(path: Path, **kwargs: Any) -> GeometryAnalysis:
        calls.append(path)
        return real(path, **kwargs)

    monkeypatch.setattr(outputs_module, "analyze_3mf", analyze)
    return calls


async def test_geometry_cached_in_output_derived(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = await _copied(tmp_path, projection, content)
    directory = store.directory(meta.id)
    # Only the blob holds the 3MF: the analysis reads it through a temporary file.
    (directory / MODEL_NAME).unlink()
    calls = _counting(monkeypatch)
    analysis = await store.geometry(meta.id)
    assert len(calls) == 1 and calls[0].parent != directory
    assert not calls[0].exists()  # the temporary file is gone
    assert store.rows is not None
    found = store.rows.derived(meta.id, "geometry", 1)
    assert found is not None
    assert found[0] == str(ANALYSIS_VERSION)
    assert GeometryAnalysis.model_validate(found[1]) == analysis
    # The row answers, not the file cache.
    (directory / GEOMETRY_NAME).unlink()
    assert await store.geometry(meta.id) == analysis
    assert len(calls) == 1


async def test_geometry_version_mismatch_recomputes(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = await _copied(tmp_path, projection, content)
    assert store.rows is not None
    first = await store.geometry(meta.id)
    stale = first.model_copy(update={"height_mm": 42.0, "version": ANALYSIS_VERSION - 1})
    store.rows.write_derived(
        meta.id, "geometry", 1, str(ANALYSIS_VERSION - 1), stale.model_dump(mode="json")
    )
    (store.directory(meta.id) / GEOMETRY_NAME).unlink()
    calls = _counting(monkeypatch)
    again = await store.geometry(meta.id)
    assert len(calls) == 1
    assert again == first
    found = store.rows.derived(meta.id, "geometry", 1)
    assert found is not None and found[0] == str(ANALYSIS_VERSION)
    assert GeometryAnalysis.model_validate(found[1]) == first


async def test_geometry_without_3mf_is_file_not_found(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> None:
    store, meta = await _copied(tmp_path, projection, content)
    assert store.rows is not None
    store.rows.set_blob(meta.id, "model", "output-gone-model")
    (store.directory(meta.id) / MODEL_NAME).unlink()
    with pytest.raises(FileNotFoundError):
        await store.geometry(meta.id)


async def test_attributed_round_trip_in_rows(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> None:
    store, meta = await _copied(tmp_path, projection, content)
    analysis = await store.geometry(meta.id, 1)
    named = analysis.model_copy(update={"height_mm": 7.0})
    store.write_attributed(meta.id, 1, "a" * 40, named)
    assert store.rows is not None
    found = store.rows.derived(meta.id, "attributed", 1)
    assert found is not None and found[0] == str(ANALYSIS_VERSION)
    assert found[1]["model_version"] == "a" * 40
    # The row answers with the file cache gone.
    (store.directory(meta.id) / "geometry-attributed.json").unlink()
    assert store.attributed(meta.id, 1, "a" * 40) == named
    assert store.attributed(meta.id, 1, "b" * 40) is None
    # A row of another analysis version is a miss.
    store.rows.write_derived(meta.id, "attributed", 1, str(ANALYSIS_VERSION - 1), found[1])
    assert store.attributed(meta.id, 1, "a" * 40) is None


def _with_cover(model: Path) -> None:
    """``model`` rewritten with a plate cover image."""
    with zipfile.ZipFile(model) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members[PLATE_THUMBNAIL] = COVER_PNG
    with zipfile.ZipFile(model, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


async def test_plate_cover_from_model_blob(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> None:
    paths, job, _ = await finished_job(tmp_path / "data")
    store = OutputStore(paths)
    rows = OutputRows(projection.pool)
    store.rows = rows
    store.bytes = OutputBytes(content)
    meta = store.create(job, name="first")
    directory = store.directory(meta.id)
    _with_cover(directory / MODEL_NAME)
    with projection.pool.connection() as conn:
        conn.execute("UPDATE outputs SET has_plate_cover = true WHERE output_id = %s", (meta.id,))
    run = rows.start_run()
    rows.finish_run(run, copied=1, refreshed=0, dropped=0, not_copied=[], damaged=[])
    blobs = await store.bytes.put_folder(directory, meta.id, meta.slug)
    rows.write_bytes([ByteWrite(meta.id, blobs, "stamp", None)])
    (directory / MODEL_NAME).unlink()
    assert await store.read_plate_cover(meta.slug) == COVER_PNG
    # The sync form reads the NFS file only, which is gone.
    assert store.plate_cover(meta.slug) is None


async def test_plate_cover_from_file_before_the_copy(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> None:
    paths, job, _ = await finished_job(tmp_path / "data")
    store = OutputStore(paths)
    store.rows = OutputRows(projection.pool)
    store.bytes = OutputBytes(content)
    meta = store.create(job, name="first")
    _with_cover(store.directory(meta.id) / MODEL_NAME)
    store.forget_plate_cover(meta.slug)
    assert await store.read_plate_cover(meta.slug) == COVER_PNG
    assert await store.read_plate_cover("other") is None
