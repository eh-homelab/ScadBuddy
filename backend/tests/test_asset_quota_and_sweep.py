"""The upload store's caps, usage and sweep (#296), kept in Postgres (#591)."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import threading
import time
import zipfile
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import psycopg
import pytest
from PIL import Image
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import PoolClosed, PoolTimeout

from scadbuddy.core.paths import DataPaths
from scadbuddy.library import assets as assets_module
from scadbuddy.library.assets import (
    ASSET_LOCK_KEY,
    STALE_ROWS,
    AssetMeta,
    AssetNotFoundError,
    AssetQuotaError,
    AssetStore,
    AssetStoreUnavailableError,
    asset_lock_key,
    file_assets,
    referenced_asset_ids,
    sanitise_svg,
)
from scadbuddy.library.output_rows import ByteWrite
from scadbuddy.library.outputs import OutputStore
from scadbuddy.render.provenance import ROOT_MODEL
from scadbuddy.render.schema import CustomizerSchema, Parameter
from tests.conftest import PgPool, open_pg_pool
from tests.support.output_blobs import blob_output, put_folder_at_keys
from tests.support.row_outputs import output_store
from tests.support.saved_outputs import rewrite_stored_model

DAY = 86400.0
GRACE = 7 * DAY
LOCK = "SELECT pg_advisory_lock(hashtextextended(%s, 0))"
UNLOCK = "SELECT pg_advisory_unlock(hashtextextended(%s, 0))"


def _referenced(outputs: OutputStore, records: Any = ()) -> set[str]:
    """`referenced_asset_ids` over the outputs' rows, from a test with no loop."""
    return asyncio.run(referenced_asset_ids(outputs.paths, records, outputs))


def svg(n: int) -> bytes:
    """A distinct SVG per ``n``, so each is its own asset."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{n + 1}" height="10">'
        f'<path d="M0 0 L{n + 1} 0 L0 10 Z"/></svg>'
    ).encode()


def svg_id(n: int) -> str:
    """The id `svg(n)` is stored under: the hash of what is kept."""
    return hashlib.sha256(sanitise_svg(svg(n))).hexdigest()


def png() -> bytes:
    out = io.BytesIO()
    Image.new("L", (4, 4)).save(out, format="PNG")
    return out.getvalue()


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path / "data")
    data.ensure()
    return data


@pytest.fixture
def store(paths: DataPaths, pg_pool: PgPool) -> AssetStore:
    return AssetStore(paths.assets, pg_pool)


@pytest.fixture
def outputs(tmp_path: Path, paths: DataPaths, pg_pool: PgPool) -> OutputStore:
    """The outputs' rows and blobs, which hold no output until a test saves one."""
    return output_store(tmp_path, pg_pool, paths=paths)


def age(pool: PgPool, meta: AssetMeta, seconds: float) -> None:
    """Make the asset look last used ``seconds`` ago."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE assets SET last_used_at = %s WHERE id = %s",
            (datetime.now(UTC) - timedelta(seconds=seconds), meta.id),
        )


def age_file(path: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


def last_used(pool: PgPool, asset_id: str) -> datetime:
    with pool.connection() as conn:
        row = conn.execute("SELECT last_used_at FROM assets WHERE id = %s", (asset_id,)).fetchone()
    assert row is not None
    stamp: datetime = row["last_used_at"]
    return stamp


def exists(store: AssetStore, meta: AssetMeta) -> bool:
    try:
        store.get(meta.id)
    except AssetNotFoundError:
        return False
    return True


def orphan(store: AssetStore, asset_id: str, seconds: float = 0.0) -> Path:
    """A blob on the volume with no row: an insert that failed, or one from before #591."""
    blob = store.root / f"{asset_id}.svg"
    blob.write_bytes(svg(0))
    if seconds:
        age_file(blob, seconds)
    return blob


# -- caps and usage ---------------------------------------------------------------


def test_usage_counts_the_stored_files_and_their_bytes(store: AssetStore) -> None:
    assert store.usage().count == 0
    first = store.put(svg(1), "a.svg")
    second = store.put(svg(2), "b.svg")
    usage = store.usage()
    assert usage.count == 2
    assert usage.bytes == first.size + second.size


def test_only_the_blobs_are_written_to_the_volume(store: AssetStore, pg_pool: PgPool) -> None:
    """No metadata sidecar, no running total, no lock file (#591): they are rows now."""
    first = store.put(svg(1), "a.svg")
    second = store.put(png(), "b.png")
    store.put(svg(1), "again.svg")
    store.use(first.id)
    age(pg_pool, first, GRACE + DAY)
    store.sweep({first.id}, grace=GRACE)

    assert sorted(p.name for p in store.root.iterdir()) == sorted(
        [f"{first.id}.svg", f"{second.id}.png"]
    )
    assert [p.name for p in store.root.parent.iterdir() if p.name.startswith(".")] == []


def test_an_upload_past_the_count_cap_is_refused(paths: DataPaths, pg_pool: PgPool) -> None:
    store = AssetStore(paths.assets, pg_pool, max_count=1)
    store.put(svg(1), "a.svg")
    with pytest.raises(AssetQuotaError, match="SCADBUDDY_ASSET_MAX_COUNT") as refused:
        store.put(svg(2), "b.svg")
    assert refused.value.usage.count == 1
    assert store.usage().count == 1


def test_an_upload_past_the_byte_cap_is_refused(paths: DataPaths, pg_pool: PgPool) -> None:
    size = len(sanitise_svg(svg(1)))
    store = AssetStore(paths.assets, pg_pool, max_total_bytes=size + 10)
    assert store.put(svg(1), "a.svg").size == size
    with pytest.raises(AssetQuotaError, match="SCADBUDDY_ASSET_MAX_TOTAL_BYTES"):
        store.put(svg(2), "b.svg")
    assert store.usage().count == 1


def test_content_already_stored_is_taken_at_the_cap(paths: DataPaths, pg_pool: PgPool) -> None:
    store = AssetStore(paths.assets, pg_pool, max_count=1, max_total_bytes=1)
    unlimited = AssetStore(paths.assets, pg_pool)
    meta = unlimited.put(svg(1), "a.svg")
    # Over both caps already; the same bytes again still cost nothing.
    assert store.put(svg(1), "again.svg").id == meta.id


def test_zero_caps_are_no_limit(store: AssetStore) -> None:
    for n in range(5):
        store.put(svg(n), f"{n}.svg")
    assert store.usage().count == 5


def test_concurrent_uploads_cannot_both_take_the_last_slot(
    paths: DataPaths, pg_pool: PgPool
) -> None:
    store = AssetStore(paths.assets, pg_pool, max_count=3)
    outcomes: list[str] = []

    def upload(n: int) -> None:
        try:
            store.put(svg(n), f"{n}.svg")
            outcomes.append("stored")
        except AssetQuotaError:
            outcomes.append("refused")

    threads = [threading.Thread(target=upload, args=(n,)) for n in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("stored") == 3
    assert store.usage().count == 3


def test_replicas_cannot_both_take_the_last_slot(paths: DataPaths, pg_conninfo: str) -> None:
    """Two processes sharing the volume and the database, each with its own pool: the
    quota check and the insert hold the database's lock, not a process's."""
    pools = [open_pg_pool(pg_conninfo, size=3) for _ in range(2)]
    try:
        replicas = [AssetStore(paths.assets, pool, max_count=3) for pool in pools]
        outcomes: list[str] = []

        def upload(n: int) -> None:
            try:
                replicas[n % 2].put(svg(n), f"{n}.svg")
                outcomes.append("stored")
            except AssetQuotaError:
                outcomes.append("refused")

        threads = [threading.Thread(target=upload, args=(n,)) for n in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert outcomes.count("stored") == 3
        assert all(replica.usage().count == 3 for replica in replicas)
        assert len(list(paths.assets.glob("*.svg"))) == 3
    finally:
        for pool in pools:
            pool.close()


def test_usage_needs_no_scan_of_the_store(
    store: AssetStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = store.put(svg(1), "a.svg")
    no_scan(monkeypatch)
    second = store.put(svg(2), "b.svg")
    store.put(svg(2), "again.svg")  # already stored: counted once
    usage = store.usage()
    assert (usage.count, usage.bytes) == (2, first.size + second.size)


def test_the_count_cap_is_enforced_without_scanning_the_store(
    paths: DataPaths, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssetStore(paths.assets, pg_pool, max_count=1)
    store.put(svg(1), "a.svg")
    no_scan(monkeypatch)
    with pytest.raises(AssetQuotaError):
        store.put(svg(2), "b.svg")


def test_a_new_instance_reads_the_same_usage(paths: DataPaths, pg_pool: PgPool) -> None:
    first = AssetStore(paths.assets, pg_pool).put(svg(1), "a.svg")
    second = AssetStore(paths.assets, pg_pool).put(svg(2), "b.svg")
    usage = AssetStore(paths.assets, pg_pool).usage()
    assert (usage.count, usage.bytes) == (2, first.size + second.size)


def test_a_blob_that_cannot_be_written_stores_no_row(
    store: AssetStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(path: Path, payload: bytes) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(assets_module, "_write_atomically", fail)
    with pytest.raises(OSError, match="disk went away"):
        store.put(svg(1), "a.svg")
    assert store.usage().count == 0
    assert list(store.root.iterdir()) == []


def test_an_insert_that_fails_leaves_only_an_orphan_the_sweep_removes(
    store: AssetStore, pg_pool: PgPool
) -> None:
    with pg_pool.connection() as conn:
        conn.execute(
            "CREATE FUNCTION refuse_asset() RETURNS trigger LANGUAGE plpgsql"
            " AS $$ BEGIN RAISE EXCEPTION 'refused'; END $$"
        )
        conn.execute(
            "CREATE TRIGGER refuse BEFORE INSERT ON assets"
            " FOR EACH ROW EXECUTE FUNCTION refuse_asset()"
        )
    with pytest.raises(psycopg.Error, match="refused"):
        store.put(svg(1), "a.svg")
    (blob,) = store.root.glob("*.svg")
    # The blob landed; with no row it is neither counted nor found.
    assert store.usage().count == 0
    with pytest.raises(AssetNotFoundError):
        store.get(blob.stem)
    assert store.sweep(set(), grace=GRACE) == []  # still inside its grace
    age_file(blob, GRACE + DAY)
    assert store.sweep(set(), grace=GRACE) == [blob.stem]
    assert not blob.exists()


def test_a_store_without_a_database_refuses_every_use(paths: DataPaths) -> None:
    store = AssetStore(paths.assets)
    with pytest.raises(AssetStoreUnavailableError):
        store.put(svg(1), "a.svg")
    with pytest.raises(AssetStoreUnavailableError):
        store.usage()
    assert list(paths.assets.iterdir()) == []


# -- what counts as a reference ---------------------------------------------------


def test_every_reference_source_is_read(
    paths: DataPaths, store: AssetStore, outputs: OutputStore
) -> None:
    ids = [store.put(svg(n), f"{n}.svg").id for n in range(5)]
    paths.model_dir("demo").mkdir(parents=True)
    (paths.model_dir("demo") / "presets.json").write_text(json.dumps([{"label": ids[0]}]))
    paths.model_dir("builtin:demo").mkdir(parents=True)
    (paths.model_dir("builtin:demo") / "model.json").write_text(json.dumps({"x": ids[1]}))

    # A saved preset's values (rows in Postgres) and a job's reach it as `params`.
    found = _referenced(outputs, [{"label": ids[2]}, {"label": ids[3]}])

    assert found >= set(ids[:4])
    assert ids[4] not in found


def test_an_unreadable_reference_source_fails_the_collection(
    paths: DataPaths, outputs: OutputStore
) -> None:
    # A directory where a template's presets should be: reading it is an OSError, not
    # "no refs".
    (paths.model_dir("demo") / "presets.json").mkdir(parents=True)
    with pytest.raises(OSError):
        _referenced(outputs)


@pytest.mark.requires_postgres
async def test_referenced_asset_ids_from_rows_and_blob_3mf(
    tmp_path: Path, pg_conninfo: str
) -> None:
    """#7256, #7257: the rows' JSON is scanned, never the folders, and the 3MF of a row
    whose ``params`` is empty is read from its model blob."""
    in_record, in_archive, in_files = "a" * 64, "b" * 64, "c" * 64
    async with blob_output(tmp_path / "o", pg_conninfo) as output:
        store, rows = output.store, output.store.rows
        assert rows is not None and store.bytes is not None
        model = output.directory / "model.3mf"
        with zipfile.ZipFile(model, "w") as written:
            written.writestr(ROOT_MODEL, f"<model>{in_archive}</model>")
        blobs = await put_folder_at_keys(
            store.bytes, output.directory, output.meta.id, output.meta.slug
        )
        rows.write_byte_copy([ByteWrite(output.meta.id, blobs, "stamp", [])])
        with output.pool.connection() as conn:
            conn.execute(
                "UPDATE outputs SET params = '{}', inputs = %s WHERE output_id = %s",
                (Jsonb({"label": in_record}), output.meta.id),
            )
        # Only the folder names this one; no reader reads the folder (#7257).
        (output.directory / "params.json").write_text(json.dumps({"label": in_files}))
        model.unlink()
        found = await referenced_asset_ids(store.paths, [{"x": "d" * 64}], store)
        assert {in_record, in_archive, "d" * 64} <= found
        assert in_files not in found


@pytest.mark.requires_postgres
async def test_referenced_asset_ids_read_each_3mf_once(
    tmp_path: Path, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7256 review B-I1: the ids a 3MF names are kept in ``output_derived`` on its first
    read, so the daily sweep reads no blob for an output it read before."""
    in_archive = "b" * 64
    async with blob_output(tmp_path / "o", pg_conninfo) as output:
        store, rows = output.store, output.store.rows
        assert rows is not None and store.bytes is not None
        model = output.directory / "model.3mf"
        with zipfile.ZipFile(model, "w") as written:
            written.writestr(ROOT_MODEL, f"<model>{in_archive}</model>")
        blobs = await put_folder_at_keys(
            store.bytes, output.directory, output.meta.id, output.meta.slug
        )
        rows.write_byte_copy([ByteWrite(output.meta.id, blobs, "stamp", [])])
        with output.pool.connection() as conn:
            conn.execute("UPDATE outputs SET params = '{}' WHERE output_id = %s", (output.meta.id,))
        model.unlink()
        gets: list[Any] = []
        real_get = output.content.get

        def counting_get(ref: Any, *args: Any, **kwargs: Any) -> Any:
            gets.append(ref)
            return real_get(ref, *args, **kwargs)

        monkeypatch.setattr(output.content, "get", counting_get)
        assert in_archive in await referenced_asset_ids(store.paths, [], store)
        assert len(gets) == 1
        found = rows.derived(output.meta.id, "asset_ids", 0)
        assert found == (assets_module.ASSET_IDS_VERSION, [in_archive])
        assert in_archive in await referenced_asset_ids(store.paths, [], store)
        assert len(gets) == 1  # the second sweep read no blob


async def _two_bare_outputs(output: Any, good: bytes, other: bytes) -> tuple[str, str]:
    """Two saved outputs with empty ``params``: the helper's, whose 3MF is ``good``, and a
    second one, whose 3MF is ``other``. Only the blobs hold them."""
    store = output.store
    second = await store.save(output.job, name="second")
    ids = (output.meta.id, second.id)
    for output_id, data in zip(ids, (good, other), strict=True):
        await rewrite_stored_model(store, output_id, partial(_write, data))
    with output.pool.connection() as conn:
        conn.execute("UPDATE outputs SET params = '{}' WHERE output_id = ANY(%s)", (list(ids),))
    return ids


def _write(data: bytes, path: Path) -> None:
    path.write_bytes(data)


def _archive_naming(asset_id: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as written:
        written.writestr(ROOT_MODEL, f"<model>{asset_id}</model>")
    return buffer.getvalue()


@pytest.mark.requires_postgres
async def test_an_unreadable_3mf_skips_only_its_output(
    tmp_path: Path,
    pg_conninfo: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7256 Task 8 amendment: a 3MF that cannot be read as a 3MF skips only its own
    output, logged. The pass goes on, and the ids it read are kept. Review B-M2: the
    damaged output keeps an empty ``asset_ids`` row, so the next sweep reads no blob for
    it and logs no warning again."""
    in_archive = "b" * 64
    async with blob_output(tmp_path / "o", pg_conninfo) as output:
        good, bad = await _two_bare_outputs(
            output, _archive_naming(in_archive), b"PK\x03\x04 truncated"
        )
        rows = output.store.rows
        assert rows is not None
        with caplog.at_level(logging.WARNING, logger="scadbuddy.library.assets"):
            found = await referenced_asset_ids(output.store.paths, [], output.store)
        assert in_archive in found
        assert rows.derived(good, "asset_ids", 0) == (
            assets_module.ASSET_IDS_VERSION,
            [in_archive],
        )
        assert rows.derived(bad, "asset_ids", 0) == (assets_module.ASSET_IDS_VERSION, [])
        skipped = [r for r in caplog.records if getattr(r, "output_id", None) == bad]
        assert len(skipped) == 1 and skipped[0].levelno == logging.WARNING
        gets: list[Any] = []
        real_get = output.content.get

        def counting_get(ref: Any, *args: Any, **kwargs: Any) -> Any:
            gets.append(ref)
            return real_get(ref, *args, **kwargs)

        monkeypatch.setattr(output.content, "get", counting_get)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="scadbuddy.library.assets"):
            assert in_archive in await referenced_asset_ids(output.store.paths, [], output.store)
        assert gets == []  # the second sweep read no 3MF
        assert not [r for r in caplog.records if getattr(r, "output_id", None) == bad]


@pytest.mark.requires_postgres
async def test_a_failed_model_read_keeps_the_ids_read_before_and_raises(
    tmp_path: Path, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7256 Task 8 amendment: a read the store fails (not a damaged 3MF) still raises, so
    the sweep deletes nothing; the ids read before it in the pass are kept."""
    async with blob_output(tmp_path / "o", pg_conninfo) as output:
        ids = await _two_bare_outputs(output, _archive_naming("b" * 64), _archive_naming("c" * 64))
        store, rows = output.store, output.store.rows
        assert rows is not None
        real = store.read_role
        seen: list[str] = []

        def failing_second(output_id: str, role: Any) -> Any:
            seen.append(output_id)
            if len(seen) == 2:
                raise OSError("store went away")
            return real(output_id, role)

        monkeypatch.setattr(store, "read_role", failing_second)
        with pytest.raises(OSError, match="store went away"):
            await referenced_asset_ids(store.paths, [], store)
        assert sorted(seen) == sorted(ids)
        first_ids = {ids[0]: "b" * 64, ids[1]: "c" * 64}
        assert rows.derived(seen[0], "asset_ids", 0) == (
            assets_module.ASSET_IDS_VERSION,
            [first_ids[seen[0]]],
        )
        assert rows.derived(seen[1], "asset_ids", 0) is None


async def test_sweep_assets_deletes_nothing_when_references_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#7256 Task 8 amendment: today's safety stays. When the referenced set cannot be
    read at all, `sweep_assets` removes nothing."""
    from types import SimpleNamespace

    from scadbuddy import main

    swept: list[Any] = []

    async def unreadable(*args: Any, **kwargs: Any) -> set[str]:
        raise OSError("cannot read the outputs")

    state = SimpleNamespace(
        paths=None, outputs=None, assets=SimpleNamespace(sweep=lambda *a, **k: swept.append(a))
    )
    monkeypatch.setattr(main, "_asset_records", lambda state: [])
    monkeypatch.setattr(main, "referenced_asset_ids", unreadable)
    with pytest.raises(OSError):
        await main.sweep_assets(state)  # type: ignore[arg-type]
    assert swept == []


# -- the sweep --------------------------------------------------------------------


def test_the_sweep_removes_an_old_asset_nothing_references(
    outputs: OutputStore, paths: DataPaths, store: AssetStore, pg_pool: PgPool
) -> None:
    meta = store.put(svg(1), "a.svg")
    age(pg_pool, meta, GRACE + DAY)

    assert store.sweep(_referenced(outputs), grace=GRACE) == [meta.id]
    assert not exists(store, meta)
    assert list(store.root.iterdir()) == []
    assert store.usage().count == 0


def test_a_stopped_sweep_removes_nothing_more(
    outputs: OutputStore, paths: DataPaths, store: AssetStore, pg_pool: PgPool
) -> None:
    """#2140: the library worker's stop ends the sweep before its next asset, so a
    shutdown's join is not spent on the rest; the next sweep takes them."""
    meta = store.put(svg(1), "a.svg")
    age(pg_pool, meta, GRACE + DAY)
    stop = threading.Event()
    stop.set()

    assert store.sweep(_referenced(outputs), grace=GRACE, stop=stop) == []
    assert exists(store, meta)
    assert store.sweep(_referenced(outputs), grace=GRACE) == [meta.id]


def test_the_sweep_never_removes_a_referenced_asset(
    outputs: OutputStore, paths: DataPaths, store: AssetStore, pg_pool: PgPool
) -> None:
    kept = [store.put(svg(n), f"{n}.svg") for n in range(4)]
    swept = store.put(svg(9), "gone.svg")
    for meta in (*kept, swept):
        age(pg_pool, meta, 10 * GRACE)
    # Saved presets' values and jobs'.
    params = [{"label": meta.id} for meta in kept]

    removed = store.sweep(_referenced(outputs, params), grace=GRACE)

    assert removed == [swept.id]
    assert all(exists(store, meta) for meta in kept)


def test_the_sweep_keeps_an_unreferenced_asset_inside_its_grace(
    outputs: OutputStore, paths: DataPaths, store: AssetStore, pg_pool: PgPool
) -> None:
    meta = store.put(svg(1), "a.svg")
    age(pg_pool, meta, GRACE - DAY)
    # The blob's mtime is not the last use any more: only the row's is.
    age_file(store.blob_path(meta), 10 * GRACE)
    assert store.sweep(_referenced(outputs), grace=GRACE) == []
    assert exists(store, meta)


def test_an_asset_used_after_the_references_were_read_is_kept(
    outputs: OutputStore, paths: DataPaths, store: AssetStore, pg_pool: PgPool, tmp_path: Path
) -> None:
    """A render submitted, or a preset saved, while the sweep runs: its reference is
    not in the set the sweep read, but validating it marked the asset used."""
    meta = store.put(svg(1), "a.svg")
    age(pg_pool, meta, 10 * GRACE)
    referenced = _referenced(outputs)  # before the render exists
    schema = CustomizerSchema(
        parameters=[Parameter(name="label", type="file", initial="", accept=["svg"])]
    )
    file_assets(schema, {"label": meta.id}, store, tmp_path)

    assert store.sweep(referenced, grace=GRACE) == []
    assert exists(store, meta)


def test_an_asset_uploaded_again_after_the_references_were_read_is_kept(
    outputs: OutputStore, paths: DataPaths, store: AssetStore, pg_pool: PgPool
) -> None:
    meta = store.put(svg(1), "a.svg")
    age(pg_pool, meta, 10 * GRACE)
    referenced = _referenced(outputs)
    store.put(svg(1), "again.svg")

    assert store.sweep(referenced, grace=GRACE) == []
    assert exists(store, meta)


def test_a_use_after_the_sweep_removed_the_asset_is_a_not_found(
    outputs: OutputStore, paths: DataPaths, store: AssetStore, pg_pool: PgPool
) -> None:
    meta = store.put(svg(1), "a.svg")
    age(pg_pool, meta, 10 * GRACE)
    store.sweep(_referenced(outputs), grace=GRACE)
    with pytest.raises(AssetNotFoundError):
        store.use(meta.id)


def test_use_marks_the_asset_used_now(store: AssetStore, pg_pool: PgPool) -> None:
    meta = store.put(svg(1), "a.svg")
    age(pg_pool, meta, 10 * GRACE)
    store.use(meta.id)
    assert datetime.now(UTC) - last_used(pg_pool, meta.id) < timedelta(seconds=60)
    assert store.sweep(set(), grace=GRACE) == []
    assert exists(store, meta)


def test_a_use_that_lands_during_the_sweeps_recheck_keeps_the_asset(
    store: AssetStore, pg_pool: PgPool, pg_conninfo: str
) -> None:
    """The sweep listed the asset as stale, then a use arrived before its removal: the
    removal re-checks the last use with the row locked, and sees the use."""
    meta = store.put(svg(1), "a.svg")
    age(pg_pool, meta, 10 * GRACE)
    removed: list[list[str]] = []
    with psycopg.connect(pg_conninfo) as use:  # a transaction: holds the row's lock
        use.execute(
            "UPDATE assets SET last_used_at = %s WHERE id = %s", (datetime.now(UTC), meta.id)
        )
        sweeper = threading.Thread(target=lambda: removed.append(store.sweep(set(), grace=GRACE)))
        sweeper.start()
        sweeper.join(0.5)
        assert sweeper.is_alive(), "the sweep did not wait for the row"
        use.commit()
        sweeper.join(10)
    assert removed == [[]]
    assert exists(store, meta)


def test_a_use_waits_for_a_removal_in_progress_and_then_finds_nothing(
    store: AssetStore, pg_conninfo: str
) -> None:
    meta = store.put(svg(1), "a.svg")
    outcome: list[str] = []

    def use() -> None:
        try:
            store.use(meta.id)
            outcome.append("found")
        except AssetNotFoundError:
            outcome.append("not found")

    with psycopg.connect(pg_conninfo) as sweep:  # the sweep's re-check, row locked
        sweep.execute("SELECT 1 FROM assets WHERE id = %s FOR UPDATE", (meta.id,))
        user = threading.Thread(target=use)
        user.start()
        user.join(0.5)
        assert outcome == []
        sweep.execute("DELETE FROM assets WHERE id = %s", (meta.id,))
        sweep.commit()
        user.join(10)
    assert outcome == ["not found"]


def test_an_upload_waits_until_a_removal_has_taken_the_blob(
    store: AssetStore, pg_conninfo: str
) -> None:
    """The sweep holds the asset's lock from its re-check until the blob is gone, past
    the commit of the row's delete; an upload of the same content, from any process,
    waits for it rather than inserting a row over a blob about to be removed."""
    stored: list[AssetMeta] = []
    key = asset_lock_key(svg_id(1))
    with psycopg.connect(pg_conninfo, autocommit=True) as sweep:
        sweep.execute(LOCK, (key,))
        uploader = threading.Thread(target=lambda: stored.append(store.put(svg(1), "a.svg")))
        uploader.start()
        uploader.join(0.5)
        assert uploader.is_alive(), "the upload did not wait for the asset's lock"
        assert store.usage().count == 0
        sweep.execute(UNLOCK, (key,))
        uploader.join(10)
    assert len(stored) == 1 and exists(store, stored[0])


def test_a_removal_in_progress_does_not_hold_up_an_upload_of_other_content(
    store: AssetStore, pg_conninfo: str
) -> None:
    """The sweep's removal locks only its own asset, so its unlinks never queue every
    other upload in the store behind them."""
    stored: list[AssetMeta] = []
    key = asset_lock_key(svg_id(1))
    with psycopg.connect(pg_conninfo, autocommit=True) as sweep:
        sweep.execute(LOCK, (key,))
        uploader = threading.Thread(target=lambda: stored.append(store.put(svg(2), "b.svg")))
        uploader.start()
        uploader.join(10)
        assert not uploader.is_alive(), "the upload waited for another asset's removal"
        sweep.execute(UNLOCK, (key,))
    assert len(stored) == 1 and exists(store, stored[0])


def test_a_reupload_does_not_wait_for_an_upload_of_new_content(
    store: AssetStore, pg_conninfo: str
) -> None:
    """Only content not yet stored needs the store's lock (its quota check); stored
    content costs nothing, so its re-upload takes only its own asset's lock."""
    first = store.put(svg(1), "a.svg")
    with psycopg.connect(pg_conninfo, autocommit=True) as other:
        other.execute(LOCK, (ASSET_LOCK_KEY,))  # a new upload mid-way through its check
        uploader = threading.Thread(target=lambda: store.put(svg(1), "again.svg"))
        uploader.start()
        uploader.join(10)
        assert not uploader.is_alive(), "the re-upload waited for the store's lock"
        other.execute(UNLOCK, (ASSET_LOCK_KEY,))
    assert store.get(first.id).name == "again.svg"


@pytest.mark.parametrize("error", [PoolTimeout, PoolClosed, psycopg.OperationalError])
def test_a_pool_that_cannot_give_one_removal_a_connection_does_not_stop_the_sweep(
    paths: DataPaths,
    pg_pool: PgPool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: type[psycopg.Error],
) -> None:
    """The pool's own failures -- exhausted, closed -- are skipped per candidate like
    any other database error, not raised out of the pass."""
    store = AssetStore(paths.assets, pg_pool)
    metas = sorted((store.put(svg(n), f"{n}.svg") for n in range(3)), key=lambda m: m.id)
    for meta in metas:
        age(pg_pool, meta, GRACE + DAY)
    real = pg_pool.connection
    calls: list[int] = []

    def flaky(*args: Any, **kwargs: Any) -> AbstractContextManager[Connection[DictRow]]:
        calls.append(1)
        if len(calls) == 2:  # the first candidate's (the first call lists the rows)
            raise error("no connection for you")
        return real(*args, **kwargs)

    monkeypatch.setattr(pg_pool, "connection", flaky)
    assert store.sweep(set(), grace=GRACE) == [meta.id for meta in metas[1:]]
    assert exists(store, metas[0])
    assert "could not remove an unused asset" in caplog.text


def test_one_removal_that_fails_in_the_database_does_not_stop_the_sweep(
    store: AssetStore, pg_pool: PgPool, pg_conninfo: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A database error on one candidate is logged and skipped, like a file error: the
    rest are still removed and counted, and that asset's lock is let go."""
    metas = sorted((store.put(svg(n), f"{n}.svg") for n in range(3)), key=lambda m: m.id)
    failing = metas[0]  # the first the sweep tries
    for meta in metas:
        age(pg_pool, meta, GRACE + DAY)
    with pg_pool.connection() as conn:
        conn.execute(
            "CREATE FUNCTION refuse_delete() RETURNS trigger LANGUAGE plpgsql"
            f" AS $$ BEGIN IF OLD.id = '{failing.id}' THEN RAISE EXCEPTION 'refused';"
            " END IF; RETURN OLD; END $$"
        )
        conn.execute(
            "CREATE TRIGGER refuse BEFORE DELETE ON assets"
            " FOR EACH ROW EXECUTE FUNCTION refuse_delete()"
        )

    assert store.sweep(set(), grace=GRACE) == [meta.id for meta in metas[1:]]
    assert exists(store, failing)
    assert "could not remove an unused asset" in caplog.text

    # The failed removal's lock was released: another session can take it at once (a
    # session of its own, since the pool's could be the one that holds it).
    with psycopg.connect(pg_conninfo, autocommit=True) as other:
        other.execute("DROP TRIGGER refuse ON assets")
        taken = other.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
            (asset_lock_key(failing.id),),
        ).fetchone()
        assert taken == (True,)
        other.execute(UNLOCK, (asset_lock_key(failing.id),))
    assert store.sweep(set(), grace=GRACE) == [failing.id]


def test_the_sweep_removes_a_row_whose_blob_is_gone(store: AssetStore, pg_pool: PgPool) -> None:
    """A blob removed behind the store's back: not found, not kept alive by a use,
    and its row goes with the sweep."""
    gone = store.put(svg(1), "gone.svg")
    kept = store.put(svg(2), "kept.svg")
    age(pg_pool, gone, GRACE + DAY)
    store.blob_path(gone).unlink()
    assert not exists(store, gone)
    with pytest.raises(AssetNotFoundError):
        store.use(gone.id)
    assert store.sweep(set(), grace=GRACE) == [gone.id]
    usage = store.usage()
    assert (usage.count, usage.bytes) == (1, kept.size)


def test_the_sweep_removes_an_old_orphan_blob_and_keeps_a_fresh_or_referenced_one(
    store: AssetStore,
) -> None:
    old = orphan(store, "a" * 64, GRACE + DAY)
    fresh = orphan(store, "b" * 64)
    named = orphan(store, "c" * 64, GRACE + DAY)
    assert store.usage().count == 0

    assert store.sweep({named.stem}, grace=GRACE) == [old.stem]
    assert sorted(p.name for p in store.root.iterdir()) == [fresh.name, named.name]


def test_the_sweep_removes_the_file_based_stores_leftovers(
    store: AssetStore, pg_pool: PgPool
) -> None:
    """No backfill (#591): the sidecars and the running total are ignored, then removed."""
    kept = store.put(svg(1), "a.svg")
    leftovers = [
        store.root / f"{kept.id}.json",
        store.root / f"{'e' * 64}.json",
        store.root.with_name(".assets.usage.json"),
        store.root.with_name(".assets.lock"),
    ]
    for path in leftovers:
        path.write_text(json.dumps({"count": 42, "bytes": 4242, "dirty": False}))
    assert store.usage().count == 1

    assert store.sweep(set(), grace=GRACE) == []
    assert not any(path.exists() for path in leftovers)
    assert exists(store, kept)


def test_only_the_first_sweep_looks_for_the_file_based_stores_leftovers(
    store: AssetStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing writes them any more, so one pass per process removes them for good;
    later sweeps do not scan the store for them again."""
    passes: list[int] = []
    real = AssetStore._remove_legacy_files

    def counted(self: AssetStore) -> None:
        passes.append(1)
        real(self)

    monkeypatch.setattr(AssetStore, "_remove_legacy_files", counted)
    for _ in range(3):
        store.sweep(set(), grace=GRACE)
    assert len(passes) == 1


def test_the_sweep_reads_the_stale_rows_through_the_last_use_index(pg_pool: PgPool) -> None:
    """The sweep reads only the rows past the grace, through `assets_last_used`, not
    the whole table."""
    with pg_pool.connection() as conn, conn.transaction():
        conn.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            str(next(iter(row.values())))
            for row in conn.execute(f"EXPLAIN {STALE_ROWS}", (datetime.now(UTC),))
        )
    assert "assets_last_used" in plan, plan


def no_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail anything that lists the store: usage comes from the rows."""

    def refuse(self: AssetStore) -> dict[str, Path]:
        raise AssertionError("the store was scanned")

    monkeypatch.setattr(AssetStore, "_blobs", refuse)


def test_the_sweep_lists_the_store_once(
    store: AssetStore, pg_pool: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = store.put(svg(1), "old.svg")
    kept = store.put(svg(2), "kept.svg")
    age(pg_pool, old, GRACE + DAY)
    blobs = store._blobs()
    calls: list[int] = []

    def listed_once(self: AssetStore) -> dict[str, Path]:
        calls.append(1)
        return dict(blobs)

    monkeypatch.setattr(AssetStore, "_blobs", listed_once)
    assert store.sweep(set(), grace=GRACE) == [old.id]
    usage = store.usage()
    assert (usage.count, usage.bytes) == (1, kept.size)
    assert len(calls) == 1
