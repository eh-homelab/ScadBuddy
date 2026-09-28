from __future__ import annotations

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
    with pytest.raises(ValueError):
        store.dir_for("../escape")


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
        import os

        os.utime(store.dir_for("stale"), (old, old))
        os.utime(store.dir_for("kept"), (old, old))
        assert sweep_blobs(store, refs, grace=3600) == ["stale"]
        assert store.exists("kept") and store.exists("fresh")
        refs.drop_holder("job", "j1")
        assert sweep_blobs(store, refs, grace=3600) == ["kept"]
    finally:
        projection.close()
