"""What readers derive of an output's bytes lives in ``output_derived`` (#7256): the
geometry analysis and its attributed form are rows, read before the old file caches,
and a miss reads the 3MF from the blob store through a temporary file. The plate cover
of a model with no picture of its own is read out of the output's model blob."""

from __future__ import annotations

import hashlib
import logging
import shutil
import zipfile
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

import scadbuddy.library.outputs as outputs_module
from scadbuddy.library.output_bytes import OutputBytes
from scadbuddy.library.output_rows import (
    OUTPUT_HOLDER,
    ByteWrite,
    OutputRows,
    plate_cover_key,
)
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
from tests.support.saved_outputs import save_files
from tests.support.store import local_content, store_pool

pytestmark = pytest.mark.requires_postgres

COVER_PNG = b"\x89PNG\r\n\x1a\nplate cover"
COVER_SHA = hashlib.sha256(COVER_PNG).hexdigest()


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
    meta = save_files(store, job, name="first")
    blobs = await store.bytes.put_folder(store.directory(meta.id), meta.id, meta.slug)
    store.rows.write_byte_copy([ByteWrite(meta.id, blobs, "stamp", [])])
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


async def test_geometry_answers_when_its_file_cache_cannot_be_written(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> None:
    """#7256 review B-M7: an output whose bytes are only blobs has no folder for the file
    cache; the analysis is still the answer, and its row is kept."""
    store, meta = await _copied(tmp_path, projection, content)
    shutil.rmtree(store.directory(meta.id))
    analysis = await store.geometry(meta.id)
    assert store.rows is not None
    found = store.rows.derived(meta.id, "geometry", 1)
    assert found is not None and GeometryAnalysis.model_validate(found[1]) == analysis


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
        meta.id,
        "geometry",
        1,
        str(ANALYSIS_VERSION - 1),
        stale.model_dump(mode="json"),
        model_sha=store.model_sha(meta.id),
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
    store.write_attributed(meta.id, 1, "a" * 40, named, model_sha=store.model_sha(meta.id))
    assert store.rows is not None
    found = store.rows.derived(meta.id, "attributed", 1)
    assert found is not None and found[0] == str(ANALYSIS_VERSION)
    assert found[1]["model_version"] == "a" * 40
    # The row answers with the file cache gone.
    (store.directory(meta.id) / "geometry-attributed.json").unlink()
    assert store.attributed(meta.id, 1, "a" * 40) == named
    assert store.attributed(meta.id, 1, "b" * 40) is None
    # A row of another analysis version is a miss.
    store.rows.write_derived(
        meta.id,
        "attributed",
        1,
        str(ANALYSIS_VERSION - 1),
        found[1],
        model_sha=store.model_sha(meta.id),
    )
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
    meta = save_files(store, job, name="first")
    directory = store.directory(meta.id)
    _with_cover(directory / MODEL_NAME)
    with projection.pool.connection() as conn:
        conn.execute("UPDATE outputs SET has_plate_cover = true WHERE output_id = %s", (meta.id,))
    run = rows.start_run()
    rows.finish_run(run, copied=1, refreshed=0, dropped=0, not_copied=[], damaged=[])
    blobs = await store.bytes.put_folder(directory, meta.id, meta.slug)
    rows.write_byte_copy([ByteWrite(meta.id, blobs, "stamp", [])])
    (directory / MODEL_NAME).unlink()
    assert await store.read_plate_cover(meta.slug) == COVER_PNG


async def test_plate_cover_from_file_before_the_copy(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> None:
    paths, job, _ = await finished_job(tmp_path / "data")
    store = OutputStore(paths)
    store.rows = OutputRows(projection.pool)
    store.bytes = OutputBytes(content)
    meta = save_files(store, job, name="first")
    _with_cover(store.directory(meta.id) / MODEL_NAME)
    store.forget_plate_cover(meta.slug)
    assert await store.read_plate_cover(meta.slug) == COVER_PNG
    assert await store.read_plate_cover("other") is None


def _without_cover(model: Path) -> None:
    """``model`` rewritten with no plate cover image."""
    with zipfile.ZipFile(model) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members.pop(PLATE_THUMBNAIL, None)
    with zipfile.ZipFile(model, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


async def _covered(
    tmp_path: Path, projection: JobProjection, content: ContentStore, *, cover: bool = True
) -> tuple[OutputStore, OutputMeta]:
    """A copied output whose 3MF holds a plate cover (or none), its NFS 3MF removed."""
    paths, job, _ = await finished_job(tmp_path / "data")
    store = OutputStore(paths)
    rows = OutputRows(projection.pool)
    store.rows = rows
    store.bytes = OutputBytes(content)
    meta = save_files(store, job, name="first")
    directory = store.directory(meta.id)
    if cover:
        _with_cover(directory / MODEL_NAME)
    else:
        _without_cover(directory / MODEL_NAME)
    with projection.pool.connection() as conn:
        conn.execute("UPDATE outputs SET has_plate_cover = true WHERE output_id = %s", (meta.id,))
    run = rows.start_run()
    rows.finish_run(run, copied=1, refreshed=0, dropped=0, not_copied=[], damaged=[])
    blobs = await store.bytes.put_folder(directory, meta.id, meta.slug)
    rows.write_byte_copy([ByteWrite(meta.id, blobs, "stamp", [])])
    (directory / MODEL_NAME).unlink()
    return store, meta


def _model_reads(store: OutputStore, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every role `read_role` is asked for."""
    asked: list[str] = []
    real = store.read_role

    def counting(output_id: str, role: Any) -> Any:
        asked.append(role)
        return real(output_id, role)

    monkeypatch.setattr(store, "read_role", counting)
    return asked


async def test_plate_cover_is_kept_as_a_blob(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7256 review B-I2: the first read keeps the cover as its own blob, held by the
    output; a second read reads that blob and no 3MF."""
    store, meta = await _covered(tmp_path, projection, content)
    asked = _model_reads(store, monkeypatch)
    assert await store.read_plate_cover(meta.slug) == COVER_PNG
    assert asked == ["model"]
    key = plate_cover_key(meta.id, COVER_SHA)
    assert store.rows is not None
    assert store.rows.derived(meta.id, "plate_cover", 0) == ("1", {"key": key})
    with pool.connection() as conn:
        held = conn.execute(
            "SELECT holder_kind, holder_id FROM blob_refs WHERE key = %s", (key,)
        ).fetchall()
    assert [(row["holder_kind"], row["holder_id"]) for row in held] == [(OUTPUT_HOLDER, meta.id)]
    assert await store.read_plate_cover(meta.slug) == COVER_PNG
    assert asked == ["model"]  # the second read read no 3MF


async def test_no_plate_cover_is_kept_as_a_row(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = await _covered(tmp_path, projection, content, cover=False)
    asked = _model_reads(store, monkeypatch)
    assert await store.read_plate_cover(meta.slug) is None
    assert store.rows is not None
    assert store.rows.derived(meta.id, "plate_cover", 0) == ("1", {"key": None})
    assert await store.read_plate_cover(meta.slug) is None
    assert asked == ["model"]


async def test_lost_plate_cover_blob_is_derived_again(
    tmp_path: Path, projection: JobProjection, content: ContentStore, pool: Pool
) -> None:
    store, meta = await _covered(tmp_path, projection, content)
    assert await store.read_plate_cover(meta.slug) == COVER_PNG
    with pool.connection() as conn:
        conn.execute(
            "DELETE FROM store_blobs WHERE key = %s", (plate_cover_key(meta.id, COVER_SHA),)
        )
    assert await store.read_plate_cover(meta.slug) == COVER_PNG


def _cover_holds(pool: Pool, output_id: str) -> list[str]:
    """The output's holds of plate cover keys."""
    with pool.connection() as conn:
        found = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = %s AND holder_id = %s"
            " AND key LIKE '%%plate_cover%%' ORDER BY key",
            (OUTPUT_HOLDER, output_id),
        ).fetchall()
    return [str(row["key"]) for row in found]


@pytest.mark.parametrize("old_first", [True, False])
async def test_plate_cover_of_the_old_model_never_replaces_the_kept_one(
    tmp_path: Path, projection: JobProjection, content: ContentStore, pool: Pool, old_first: bool
) -> None:
    """#7256 Task 9 fix concern 2b: a reader of the old model that keeps its cover after a
    reader of the new model, or before it, never leaves the old cover's bytes under the
    key the row names. Each cover has a key of its own (its sha256)."""
    store, meta = await _covered(tmp_path, projection, content)
    assert store.rows is not None and store.bytes is not None
    new_sha = store.model_sha(meta.id)
    assert new_sha is not None
    old = (b"\x89PNG\r\n\x1a\nthe old model's cover", "0" * 64)
    keeps = [old, (COVER_PNG, new_sha)]
    for png, model_sha in keeps if old_first else reversed(keeps):
        await store._keep_plate_cover(meta.slug, meta.id, png, model_sha)
    kept = store.rows.derived(meta.id, "plate_cover", 0)
    assert kept is not None
    key = kept[1]["key"]
    assert await store.bytes.read(key) == COVER_PNG
    assert _cover_holds(pool, meta.id) == [key]
    assert await store.read_plate_cover(meta.slug) == COVER_PNG


async def test_a_new_plate_cover_releases_the_replaced_one(
    tmp_path: Path, projection: JobProjection, content: ContentStore, pool: Pool
) -> None:
    """A cover kept again with other bytes names its own key; the output no longer holds
    the replaced key, so the sweep removes it."""
    store, meta = await _covered(tmp_path, projection, content)
    assert store.rows is not None
    model_sha = store.model_sha(meta.id)
    await store._keep_plate_cover(meta.slug, meta.id, b"first cover", model_sha)
    first = _cover_holds(pool, meta.id)
    await store._keep_plate_cover(meta.slug, meta.id, COVER_PNG, model_sha)
    second = _cover_holds(pool, meta.id)
    assert len(first) == 1 and len(second) == 1 and first != second
    assert store.rows.derived(meta.id, "plate_cover", 0) == ("1", {"key": second[0]})
    await store._keep_plate_cover(meta.slug, meta.id, None, model_sha)
    assert _cover_holds(pool, meta.id) == []
    assert store.rows.derived(meta.id, "plate_cover", 0) == ("1", {"key": None})


def _client_error(status: int, code: str | None = None) -> ClientError:
    response: Any = {
        "Error": {"Code": code or str(status)},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return ClientError(response, "GetObject")


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(lambda: _client_error(404), id="404"),
        pytest.param(lambda: _client_error(404, "NoSuchKey"), id="NoSuchKey"),
    ],
)
async def test_plate_cover_missing_object_is_no_cover_once(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Any,
) -> None:
    """#7256 Task 8 amendment: only a missing object (404, ``NoSuchKey``) is kept as no
    cover, so the next ask does not read again. A guard: no backend raises these past
    `local_role` (see the local backend test below)."""
    store, meta = await _covered(tmp_path, projection, content)
    gets: list[Any] = []

    def missing(ref: Any, *args: Any, **kwargs: Any) -> Any:
        gets.append(ref)
        raise error()

    monkeypatch.setattr(content, "get", missing)
    with caplog.at_level(logging.WARNING, logger="scadbuddy.library.outputs"):
        assert await store.read_plate_cover(meta.slug) is None
    warned = [r for r in caplog.records if "plate cover" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING
    assert await store.read_plate_cover(meta.slug) is None
    assert len(gets) == 1
    assert store.rows is not None
    assert store.rows.derived(meta.id, "plate_cover", 0) == ("1", {"key": None})


async def test_plate_cover_missing_object_in_local_backend_keeps_nothing(
    tmp_path: Path, projection: JobProjection, content: ContentStore
) -> None:
    """#7256 Task 8 review B-M1: through the real local backend, a missing object is a
    lost blob. With no file either there is no cover for now, and nothing is kept."""
    store, meta = await _covered(tmp_path, projection, content)
    objects = [path for path in (tmp_path / "remote").rglob("*") if path.is_file()]
    assert objects
    for path in objects:
        path.unlink()
    assert await store.read_plate_cover(meta.slug) is None
    assert store.rows is not None
    assert store.rows.derived(meta.id, "plate_cover", 0) is None


async def test_plate_cover_disk_error_is_not_kept(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7256 Task 8 review B-M1: a ``FileNotFoundError`` of the local disk (the spool
    removed during the read) is transient. It is not kept as "no cover"."""
    store, meta = await _covered(tmp_path, projection, content)
    assert store.bytes is not None
    real = store.bytes._download_to

    async def spool_gone(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("spool removed")

    monkeypatch.setattr(store.bytes, "_download_to", spool_gone)
    assert await store.read_plate_cover(meta.slug) is None
    assert store.rows is not None
    assert store.rows.derived(meta.id, "plate_cover", 0) is None
    monkeypatch.setattr(store.bytes, "_download_to", real)
    assert await store.read_plate_cover(meta.slug) == COVER_PNG


@pytest.mark.parametrize("status", [401, 403])
async def test_kept_plate_cover_refused_read_is_no_cover(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    """#7256 Task 8 review A-M1: a refused read of a kept cover blob answers no cover
    (as the derive path does), logs one warning and keeps nothing new, never a 500."""
    store, meta = await _covered(tmp_path, projection, content)
    assert await store.read_plate_cover(meta.slug) == COVER_PNG
    kept = ("1", {"key": plate_cover_key(meta.id, COVER_SHA)})
    assert store.rows is not None
    assert store.rows.derived(meta.id, "plate_cover", 0) == kept
    real = content.get

    def refused(ref: Any, *args: Any, **kwargs: Any) -> Any:
        raise _client_error(status)

    monkeypatch.setattr(content, "get", refused)
    with caplog.at_level(logging.WARNING, logger="scadbuddy.library.outputs"):
        assert await store.read_plate_cover(meta.slug) is None
    warned = [r for r in caplog.records if "plate cover" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING
    assert store.rows.derived(meta.id, "plate_cover", 0) == kept
    monkeypatch.setattr(content, "get", real)
    assert await store.read_plate_cover(meta.slug) == COVER_PNG


@pytest.mark.parametrize("status", [401, 403, 400])
async def test_plate_cover_refused_read_is_not_kept(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    """#7256 Task 8 amendment: a 401, 403 or other 4xx is logged once at warning level and
    not kept, so the cover comes back once the key is fixed (a key rotation heals)."""
    store, meta = await _covered(tmp_path, projection, content)
    real = content.get

    def refused(ref: Any, *args: Any, **kwargs: Any) -> Any:
        raise _client_error(status)

    monkeypatch.setattr(content, "get", refused)
    with caplog.at_level(logging.WARNING, logger="scadbuddy.library.outputs"):
        assert await store.read_plate_cover(meta.slug) is None
    warned = [r for r in caplog.records if "plate cover" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING
    assert store.rows is not None
    assert store.rows.derived(meta.id, "plate_cover", 0) is None
    monkeypatch.setattr(content, "get", real)
    assert await store.read_plate_cover(meta.slug) == COVER_PNG


async def test_plate_cover_5xx_is_asked_again(
    tmp_path: Path,
    projection: JobProjection,
    content: ContentStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, meta = await _covered(tmp_path, projection, content)
    real = content.get

    def failing(ref: Any, *args: Any, **kwargs: Any) -> Any:
        raise _client_error(503)

    monkeypatch.setattr(content, "get", failing)
    # A 5xx is a lost blob: the file is read, and it is gone, so no cover for now.
    assert await store.read_plate_cover(meta.slug) is None
    assert store.rows is not None
    assert store.rows.derived(meta.id, "plate_cover", 0) is None
    monkeypatch.setattr(content, "get", real)
    assert await store.read_plate_cover(meta.slug) == COVER_PNG
