from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from scadbuddy.store import sweep_blobs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs


def test_dir_for_creates_and_finds_a_key(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    directory = store.dir_for("abc")
    assert directory.is_dir() and directory == tmp_path / "blobs" / "abc"
    assert store.exists("abc") and store.keys() == ["abc"]
    store.remove("abc")
    assert not store.exists("abc")


def test_keys_are_confined_to_the_root(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    for key in ("../escape", "piece\n", "\n", "\x00\x1b"):
        with pytest.raises(ValueError):
            store.dir_for(key)
    assert not (tmp_path / "blobs").exists() or store.keys() == []


def test_removing_a_blob_already_gone_is_fine(tmp_path: Path) -> None:
    LocalBlobStore(tmp_path / "blobs").remove("never-made")


def test_a_remove_that_fails_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    store.dir_for("stuck")

    def refuse(path: object, ignore_errors: bool = False) -> None:
        # As `shutil.rmtree` does: the error is raised unless it is ignored.
        if not ignore_errors:
            raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(shutil, "rmtree", refuse)
    with pytest.raises(PermissionError):
        store.remove("stuck")
    assert store.exists("stuck")


@pytest.mark.requires_postgres
def test_dir_for_touches_an_existing_blob_so_a_claim_survives_the_sweep(
    tmp_path: Path, pg_conninfo: str
) -> None:
    from scadbuddy.render.projection import JobProjection

    projection = JobProjection(pg_conninfo, pool_size=2)
    projection.open()
    try:
        refs = BlobRefs(projection.pool)
        store = LocalBlobStore(tmp_path / "blobs")
        old = time.time() - 7200
        os.utime(store.dir_for("claimed"), (old, old))
        store.dir_for("claimed")  # a new holder claims it, about to add its ref
        assert sweep_blobs(store, refs, grace=3600) == []
    finally:
        projection.close()


@pytest.mark.requires_postgres
def test_sweep_removes_only_unreferenced_blobs_past_grace(tmp_path: Path, pg_conninfo: str) -> None:
    from scadbuddy.render.projection import JobProjection

    projection = JobProjection(pg_conninfo, pool_size=2)
    projection.open()
    try:
        refs = BlobRefs(projection.pool)
        store = LocalBlobStore(tmp_path / "blobs")
        for key in ("kept", "fresh", "stale"):
            (store.dir_for(key) / "model.3mf").write_bytes(b"x")
        refs.add("kept", "job", "j1")
        old = time.time() - 7200
        os.utime(store.dir_for("stale"), (old, old))
        os.utime(store.dir_for("kept"), (old, old))
        assert sweep_blobs(store, refs, grace=3600) == ["stale"]
        assert store.exists("kept") and store.exists("fresh")
        refs.drop_holder("job", "j1")
        assert sweep_blobs(store, refs, grace=3600) == ["kept"]
    finally:
        projection.close()


class _RacedStore(LocalBlobStore):
    """A blob another sweep removes between this sweep's `keys()` and `touched_at()`."""

    def __init__(self, root: Path, raced: str) -> None:
        super().__init__(root)
        self.raced = raced

    def touched_at(self, key: str) -> float:
        if key == self.raced:
            shutil.rmtree(self.root / key)
        return super().touched_at(key)


@pytest.mark.requires_postgres
def test_a_blob_that_vanishes_mid_sweep_does_not_stop_it(tmp_path: Path, pg_conninfo: str) -> None:
    from scadbuddy.render.projection import JobProjection

    projection = JobProjection(pg_conninfo, pool_size=2)
    projection.open()
    try:
        refs = BlobRefs(projection.pool)
        store = _RacedStore(tmp_path / "blobs", raced="a-gone")
        old = time.time() - 7200
        for key in ("a-gone", "b-stale"):
            os.utime(store.dir_for(key), (old, old))
        assert sweep_blobs(store, refs, grace=3600) == ["b-stale"]
        assert store.keys() == []
    finally:
        projection.close()


@pytest.mark.requires_postgres
def test_a_blob_that_cannot_be_removed_is_not_counted_and_the_sweep_goes_on(
    tmp_path: Path, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scadbuddy.render.projection import JobProjection

    projection = JobProjection(pg_conninfo, pool_size=2)
    projection.open()
    try:
        refs = BlobRefs(projection.pool)
        store = LocalBlobStore(tmp_path / "blobs")
        old = time.time() - 7200
        for key in ("a-stuck", "b-stale"):
            os.utime(store.dir_for(key), (old, old))
        rmtree = shutil.rmtree

        def refuse_one(path: Path, ignore_errors: bool = False) -> None:
            if Path(path).name == "a-stuck" and not ignore_errors:
                raise PermissionError(13, "Permission denied", str(path))
            rmtree(path, ignore_errors=ignore_errors)

        monkeypatch.setattr(shutil, "rmtree", refuse_one)
        assert sweep_blobs(store, refs, grace=3600) == ["b-stale"]
        assert store.keys() == ["a-stuck"]
    finally:
        projection.close()
