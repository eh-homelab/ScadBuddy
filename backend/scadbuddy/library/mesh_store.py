"""The API's `MeshStore` (#7313, `library/mesh_blobs.py`): a mesh's bytes as the blob
``attachment-<sha256>`` of ScadBuddy's own store, held in ``blob_refs`` by `HOLDER`.

Apart from `mesh_blobs.py`, which `library/history.py` imports: `orders/attachments.py`
reaches `library/libraries.py`, which imports the history."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from scadbuddy.core.gitrepo import GitError
from scadbuddy.library.mesh_blobs import (
    HOLDER,
    MESH_TIMEOUT,
    MeshBlobMissingError,
    MeshPointer,
    copy_verified,
)
from scadbuddy.orders.attachments import blob_key, blob_path, keep_file
from scadbuddy.store import BlobStore
from scadbuddy.store.refs import HoldsBlobs


class LoopMeshStore:
    """`MeshStore` over the API's `BlobStore` and `blob_refs`, called from the threads
    the history runs on: the store's coroutines run on ``loop``, the API's own.

    Only the API commits to the models repository, and only it holds the database
    (root CLAUDE.md "Database access"): the hold is a row it writes itself."""

    def __init__(
        self,
        blobs: BlobStore,
        refs: HoldsBlobs,
        loop: asyncio.AbstractEventLoop,
        *,
        timeout: float = MESH_TIMEOUT,
    ) -> None:
        self.blobs = blobs
        self.refs = refs
        self.loop = loop
        self.timeout = timeout

    def put(self, path: Path) -> MeshPointer:
        # One handle: what is hashed is what is kept, whatever replaces the file.
        with path.open("rb") as source:
            sha256 = hashlib.file_digest(source, "sha256").hexdigest()
            size = source.tell()
            # Kept first, then held: `keep_file` touches the blob, which keeps it from
            # the sweeps until the hold is written (`BlobStore.claim`).
            key = self._run(keep_file(self.blobs, source, sha256))
        try:
            self.refs.add(key, HOLDER, sha256)
        except Exception as error:
            raise GitError(f"could not hold a mesh in the blob store: {error}") from error
        return MeshPointer(sha256=sha256, size=size)

    def get(self, sha256: str, dest: Path) -> None:
        found = self._run(blob_path(self.blobs, blob_key(sha256)))
        if found is None:
            raise MeshBlobMissingError(f"the blob store holds no mesh {sha256}")
        copy_verified(found, dest, sha256)

    def _run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            coro.close()
            raise RuntimeError("the mesh store is called from a thread, never its own loop")
        if not self.loop.is_running():
            coro.close()
            raise GitError("the blob store's event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(self.timeout)
        except Exception as error:
            # Whatever the store raised, as the error a commit undoes its change on.
            future.cancel()
            raise GitError(f"the blob store failed: {error}") from error
