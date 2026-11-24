"""`library/mesh_store.py` `LoopMeshStore` (#7313): a mesh's bytes kept as a blob of
ScadBuddy's own store, held by `model_mesh` so no sweep ever takes them."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from scadbuddy.library.mesh_blobs import HOLDER, MeshBlobMissingError
from scadbuddy.library.mesh_store import LoopMeshStore
from scadbuddy.orders.attachments import BLOB_FILE, blob_key
from scadbuddy.store import BlobRefs, sweep_blobs
from scadbuddy.store.local import LocalBlobStore
from tests.conftest import PgPool

MESH = b"PK\x03\x04a part mesh" * 1000


async def test_a_mesh_is_kept_held_and_never_swept(tmp_path: Path, pg_pool: PgPool) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    refs = BlobRefs(pg_pool)
    meshes = LoopMeshStore(blobs, refs, asyncio.get_running_loop())
    source = tmp_path / "o1-p1.3mf"
    source.write_bytes(MESH)
    sha = hashlib.sha256(MESH).hexdigest()

    await asyncio.to_thread(meshes.put, source, sha)
    assert refs.holds(blob_key(sha), HOLDER, sha)
    # No grace: an unheld blob would go now.
    assert sweep_blobs(blobs, refs, grace=0, now=1e12) == []

    dest = tmp_path / "out.3mf"
    await asyncio.to_thread(meshes.get, sha, dest)
    assert dest.read_bytes() == MESH


async def test_a_mesh_whose_bytes_changed_in_the_store_is_refused(
    tmp_path: Path, pg_pool: PgPool
) -> None:
    blobs = LocalBlobStore(tmp_path / "blobs")
    meshes = LoopMeshStore(blobs, BlobRefs(pg_pool), asyncio.get_running_loop())
    source = tmp_path / "o1-p1.3mf"
    source.write_bytes(MESH)
    sha = hashlib.sha256(MESH).hexdigest()
    await asyncio.to_thread(meshes.put, source, sha)
    (blobs.dir_for(blob_key(sha)) / BLOB_FILE).write_bytes(b"not the mesh")

    dest = tmp_path / "out.3mf"
    with pytest.raises(MeshBlobMissingError):
        await asyncio.to_thread(meshes.get, sha, dest)
    assert not dest.exists()


async def test_a_missing_mesh_is_refused(tmp_path: Path, pg_pool: PgPool) -> None:
    meshes = LoopMeshStore(
        LocalBlobStore(tmp_path / "blobs"), BlobRefs(pg_pool), asyncio.get_running_loop()
    )
    with pytest.raises(MeshBlobMissingError):
        await asyncio.to_thread(meshes.get, "ab" * 32, tmp_path / "out.3mf")


async def test_the_store_refuses_a_call_from_its_own_loop(tmp_path: Path, pg_pool: PgPool) -> None:
    meshes = LoopMeshStore(
        LocalBlobStore(tmp_path / "blobs"), BlobRefs(pg_pool), asyncio.get_running_loop()
    )
    with pytest.raises(RuntimeError, match="thread"):
        meshes.get("ab" * 32, tmp_path / "out.3mf")
