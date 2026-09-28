"""The upload store's caps, usage and sweep (#296)."""

from __future__ import annotations

import json
import os
import threading
import time
import zipfile
from pathlib import Path

import pytest
import trimesh

from scadbuddy.core.paths import DataPaths
from scadbuddy.library import assets as assets_module
from scadbuddy.library.assets import (
    AssetMeta,
    AssetNotFoundError,
    AssetQuotaError,
    AssetStore,
    file_assets,
    referenced_asset_ids,
)
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.provenance import ROOT_MODEL, Provenance, stamp
from scadbuddy.render.provenance import read as read_provenance
from scadbuddy.render.schema import CustomizerSchema, Parameter, ParamValue
from scadbuddy.render.split import ColourPart

DAY = 86400.0
GRACE = 7 * DAY


def svg(n: int) -> bytes:
    """A distinct SVG per ``n``, so each is its own asset."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{n + 1}" height="10">'
        f'<path d="M0 0 L{n + 1} 0 L0 10 Z"/></svg>'
    ).encode()


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path / "data")
    data.ensure()
    return data


@pytest.fixture
def store(paths: DataPaths) -> AssetStore:
    return AssetStore(paths.assets)


def age(store: AssetStore, meta: AssetMeta, seconds: float) -> None:
    """Make the asset look last used ``seconds`` ago."""
    then = time.time() - seconds
    for path in (store.blob_path(meta), store.root / f"{meta.id}.json"):
        os.utime(path, (then, then))


def exists(store: AssetStore, meta: AssetMeta) -> bool:
    try:
        store.get(meta.id)
    except AssetNotFoundError:
        return False
    return True


# -- caps and usage ---------------------------------------------------------------


def test_usage_counts_the_stored_files_and_their_bytes(store: AssetStore) -> None:
    assert store.usage().count == 0
    first = store.put(svg(1), "a.svg")
    second = store.put(svg(2), "b.svg")
    usage = store.usage()
    assert usage.count == 2
    assert usage.bytes == first.size + second.size
    # The lock lives beside the store, never in it.
    assert not any(path.name.startswith(".") for path in store.root.iterdir())


def test_an_upload_past_the_count_cap_is_refused(paths: DataPaths) -> None:
    store = AssetStore(paths.assets, max_count=1)
    store.put(svg(1), "a.svg")
    with pytest.raises(AssetQuotaError, match="SCADBUDDY_ASSET_MAX_COUNT") as refused:
        store.put(svg(2), "b.svg")
    assert refused.value.usage.count == 1
    assert store.usage().count == 1


def test_an_upload_past_the_byte_cap_is_refused(paths: DataPaths) -> None:
    size = AssetStore(paths.assets / "probe").put(svg(1), "a.svg").size
    store = AssetStore(paths.assets, max_total_bytes=size + 10)
    store.put(svg(1), "a.svg")
    with pytest.raises(AssetQuotaError, match="SCADBUDDY_ASSET_MAX_TOTAL_BYTES"):
        store.put(svg(2), "b.svg")
    assert store.usage().count == 1


def test_content_already_stored_is_taken_at_the_cap(paths: DataPaths) -> None:
    store = AssetStore(paths.assets, max_count=1, max_total_bytes=1)
    unlimited = AssetStore(paths.assets)
    meta = unlimited.put(svg(1), "a.svg")
    # Over both caps already; the same bytes again still cost nothing.
    assert store.put(svg(1), "again.svg").id == meta.id


def test_zero_caps_are_no_limit(store: AssetStore) -> None:
    for n in range(5):
        store.put(svg(n), f"{n}.svg")
    assert store.usage().count == 5


def test_concurrent_uploads_cannot_both_take_the_last_slot(paths: DataPaths) -> None:
    store = AssetStore(paths.assets, max_count=3)
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


# -- what counts as a reference ---------------------------------------------------


def write_3mf(path: Path, params: dict[str, ParamValue]) -> None:
    write_bambu_3mf(
        [ColourPart(1, "Color 1", "#FF0000", trimesh.creation.box(extents=(10, 10, 5)))],
        path,
        thumbnails=None,
        model_name="demo",
    )
    stamp(path, Provenance(model="demo", version="v", output="o", params=params))


def test_every_reference_source_is_read(paths: DataPaths, store: AssetStore) -> None:
    ids = [store.put(svg(n), f"{n}.svg").id for n in range(7)]
    output = paths.output_dir("demo", "a" * 32)
    output.mkdir(parents=True)
    (output / "params.json").write_text(json.dumps({"label": ids[0]}))
    # An output whose params.json is gone still names its asset in the 3MF stamp.
    bare = paths.output_dir("builtin:demo", "b" * 32)
    bare.mkdir(parents=True)
    write_3mf(bare / "model.3mf", {"label": ids[1]})
    paths.model_presets("demo").write_text(
        json.dumps({"presets": [{"id": "p", "name": "P", "params": {"label": ids[2]}}]})
    )
    paths.model_dir("demo").mkdir(parents=True)
    (paths.model_dir("demo") / "presets.json").write_text(json.dumps([{"label": ids[3]}]))
    paths.model_dir("builtin:demo").mkdir(parents=True)
    (paths.model_dir("builtin:demo") / "model.json").write_text(json.dumps({"x": ids[4]}))

    found = referenced_asset_ids(paths, [{"label": ids[5]}])

    assert found >= set(ids[:6])
    assert ids[6] not in found


def test_a_damaged_record_still_keeps_its_asset(paths: DataPaths, store: AssetStore) -> None:
    meta = store.put(svg(1), "a.svg")
    output = paths.output_dir("demo", "a" * 32)
    output.mkdir(parents=True)
    (output / "params.json").write_text('{"label": "' + meta.id + '", truncated')
    assert meta.id in referenced_asset_ids(paths)


def test_an_unreadable_reference_source_fails_the_collection(paths: DataPaths) -> None:
    output = paths.output_dir("demo", "a" * 32)
    # A directory where a record should be: reading it is an OSError, not "no refs".
    (output / "params.json").mkdir(parents=True)
    with pytest.raises(OSError):
        referenced_asset_ids(paths)


def test_a_stamp_provenance_cannot_parse_still_keeps_its_asset(
    paths: DataPaths, store: AssetStore
) -> None:
    """`provenance.read` answers None for a stamp it cannot validate; the sweep must
    not read that as "names nothing" when params.json is gone too."""
    meta = store.put(svg(1), "a.svg")
    output = paths.output_dir("demo", "a" * 32)
    output.mkdir(parents=True)
    archive = output / "model.3mf"
    with zipfile.ZipFile(archive, "w") as written:
        written.writestr(
            ROOT_MODEL,
            f'<model><metadata name="ScadBuddy:Provenance">{{"future": {{"label": '
            f'"{meta.id}"}}</metadata></model>',
        )
    assert read_provenance(archive) is None
    assert meta.id in referenced_asset_ids(paths)


def test_a_3mf_that_cannot_be_opened_fails_the_collection(paths: DataPaths) -> None:
    output = paths.output_dir("demo", "a" * 32)
    output.mkdir(parents=True)
    (output / "model.3mf").write_bytes(b"PK\x03\x04 truncated")
    with pytest.raises(OSError, match=r"model\.3mf"):
        referenced_asset_ids(paths)


# -- the sweep --------------------------------------------------------------------


def test_the_sweep_removes_an_old_asset_nothing_references(
    paths: DataPaths, store: AssetStore
) -> None:
    meta = store.put(svg(1), "a.svg")
    age(store, meta, GRACE + DAY)

    assert store.sweep(referenced_asset_ids(paths), grace=GRACE) == [meta.id]
    assert not exists(store, meta)
    assert list(store.root.iterdir()) == []


def test_the_sweep_never_removes_a_referenced_asset(paths: DataPaths, store: AssetStore) -> None:
    kept = [store.put(svg(n), f"{n}.svg") for n in range(4)]
    swept = store.put(svg(9), "gone.svg")
    for meta in (*kept, swept):
        age(store, meta, 10 * GRACE)
    output = paths.output_dir("demo", "a" * 32)
    output.mkdir(parents=True)
    (output / "params.json").write_text(json.dumps({"label": kept[0].id}))
    paths.model_presets("demo").write_text(
        json.dumps({"presets": [{"id": "p", "name": "P", "params": {"label": kept[1].id}}]})
    )
    bare = paths.output_dir("demo", "c" * 32)
    bare.mkdir(parents=True)
    write_3mf(bare / "model.3mf", {"label": kept[2].id})
    jobs = [{"label": kept[3].id}]

    removed = store.sweep(referenced_asset_ids(paths, jobs), grace=GRACE)

    assert removed == [swept.id]
    assert all(exists(store, meta) for meta in kept)


def test_the_sweep_keeps_an_unreferenced_asset_inside_its_grace(
    paths: DataPaths, store: AssetStore
) -> None:
    meta = store.put(svg(1), "a.svg")
    age(store, meta, GRACE - DAY)
    assert store.sweep(referenced_asset_ids(paths), grace=GRACE) == []
    assert exists(store, meta)


def test_an_asset_used_after_the_references_were_read_is_kept(
    paths: DataPaths, store: AssetStore, tmp_path: Path
) -> None:
    """A render submitted, or a preset saved, while the sweep runs: its reference is
    not in the set the sweep read, but validating it marked the asset used."""
    meta = store.put(svg(1), "a.svg")
    age(store, meta, 10 * GRACE)
    referenced = referenced_asset_ids(paths)  # before the render exists
    schema = CustomizerSchema(
        parameters=[Parameter(name="label", type="file", initial="", accept=["svg"])]
    )
    file_assets(schema, {"label": meta.id}, store, tmp_path)

    assert store.sweep(referenced, grace=GRACE) == []
    assert exists(store, meta)


def test_an_asset_uploaded_again_after_the_references_were_read_is_kept(
    paths: DataPaths, store: AssetStore
) -> None:
    meta = store.put(svg(1), "a.svg")
    age(store, meta, 10 * GRACE)
    referenced = referenced_asset_ids(paths)
    store.put(svg(1), "again.svg")

    assert store.sweep(referenced, grace=GRACE) == []
    assert exists(store, meta)


def test_a_use_after_the_sweep_removed_the_asset_is_a_not_found(
    paths: DataPaths, store: AssetStore
) -> None:
    meta = store.put(svg(1), "a.svg")
    age(store, meta, 10 * GRACE)
    store.sweep(referenced_asset_ids(paths), grace=GRACE)
    with pytest.raises(AssetNotFoundError):
        store.use(meta.id)


def test_use_marks_the_asset_used_now(store: AssetStore) -> None:
    meta = store.put(svg(1), "a.svg")
    age(store, meta, 10 * GRACE)
    store.use(meta.id)
    assert time.time() - (store.root / f"{meta.id}.json").stat().st_mtime < 60


# -- the running total (#390) -----------------------------------------------------


def no_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail anything that lists the store: usage must come from the ledger."""

    def refuse(self: AssetStore) -> dict[str, Path]:
        raise AssertionError("the store was scanned")

    monkeypatch.setattr(AssetStore, "_blobs", refuse)


def test_uploads_keep_the_total_without_scanning_the_store(
    store: AssetStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = store.put(svg(1), "a.svg")
    no_scan(monkeypatch)
    second = store.put(svg(2), "b.svg")
    store.put(svg(2), "again.svg")  # already stored: counted once
    usage = store.usage()
    assert (usage.count, usage.bytes) == (2, first.size + second.size)


def test_the_count_cap_is_enforced_without_scanning_the_store(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssetStore(paths.assets, max_count=1)
    store.put(svg(1), "a.svg")
    no_scan(monkeypatch)
    with pytest.raises(AssetQuotaError):
        store.put(svg(2), "b.svg")


def test_the_sweep_keeps_the_total_without_scanning_it_again(
    paths: DataPaths, store: AssetStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = store.put(svg(1), "old.svg")
    kept = store.put(svg(2), "kept.svg")
    age(store, old, GRACE + DAY)
    blobs = store._blobs()
    # The sweep lists the store once to find candidates; the total must not need more.
    calls: list[int] = []

    def listed_once(self: AssetStore) -> dict[str, Path]:
        calls.append(1)
        return dict(blobs)

    monkeypatch.setattr(AssetStore, "_blobs", listed_once)
    assert store.sweep(set(), grace=GRACE) == [old.id]
    usage = store.usage()
    assert (usage.count, usage.bytes) == (1, kept.size)
    assert len(calls) == 1


def test_a_new_instance_reads_the_total_the_last_one_left(
    paths: DataPaths, store: AssetStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = store.put(svg(1), "a.svg")
    second = store.put(svg(2), "b.svg")
    no_scan(monkeypatch)
    usage = AssetStore(paths.assets).usage()
    assert (usage.count, usage.bytes) == (2, first.size + second.size)


def test_rebuild_counts_files_changed_behind_the_stores_back(
    paths: DataPaths, store: AssetStore
) -> None:
    first = store.put(svg(1), "a.svg")
    second = store.put(svg(2), "b.svg")
    # Removed while the process was down: the ledger still counts it.
    store.blob_path(second).unlink()
    (store.root / f"{second.id}.json").unlink()
    restarted = AssetStore(paths.assets)
    usage = restarted.rebuild_usage()
    assert (usage.count, usage.bytes) == (1, first.size)
    assert restarted.usage().count == 1


def test_an_upload_that_dies_midway_is_recounted(
    store: AssetStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = store.put(svg(1), "a.svg")
    real = assets_module._write_atomically

    def crash_on_metadata(path: Path, payload: bytes) -> None:
        if path.suffix == ".json" and path.parent == store.root:
            raise OSError("disk went away")
        real(path, payload)

    monkeypatch.setattr(assets_module, "_write_atomically", crash_on_metadata)
    with pytest.raises(OSError):
        store.put(svg(2), "b.svg")
    monkeypatch.setattr(assets_module, "_write_atomically", real)
    # The blob landed; the ledger was left dirty, so the next read counts it.
    blob_size = sum(p.stat().st_size for p in store.root.glob("*.svg"))
    usage = store.usage()
    assert usage.count == 2
    assert usage.bytes == blob_size
    assert blob_size > first.size


def test_a_missing_or_damaged_ledger_is_recounted(store: AssetStore) -> None:
    meta = store.put(svg(1), "a.svg")
    store.ledger_path.write_text("{not json", encoding="utf-8")
    assert store.usage().count == 1
    store.ledger_path.unlink()
    usage = store.usage()
    assert (usage.count, usage.bytes) == (1, meta.size)
    assert json.loads(store.ledger_path.read_text(encoding="utf-8"))["dirty"] is False


def test_the_sweep_removes_the_metadata_of_a_blob_already_gone(
    store: AssetStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blob removed behind the store's back between the sweep's listing and its
    removal: the metadata must still go, and the total is recounted."""
    gone = store.put(svg(1), "gone.svg")
    kept = store.put(svg(2), "kept.svg")
    age(store, gone, GRACE + DAY)
    real_last_used = AssetStore._last_used

    def vanish_first(self: AssetStore, asset_id: str, blob: Path) -> float | None:
        stamp = real_last_used(self, asset_id, blob)
        if asset_id == gone.id:
            blob.unlink(missing_ok=True)
        return stamp

    monkeypatch.setattr(AssetStore, "_last_used", vanish_first)
    assert store.sweep(set(), grace=GRACE) == [gone.id]
    assert not (store.root / f"{gone.id}.json").exists()
    usage = store.usage()
    assert (usage.count, usage.bytes) == (1, kept.size)
