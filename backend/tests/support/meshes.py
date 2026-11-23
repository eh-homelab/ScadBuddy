"""A mesh store in memory (#7313), for history tests that need no blob store or
Postgres: what `library/mesh_blobs.py` `MeshStore` stores, by sha256."""

from __future__ import annotations

import hashlib
from pathlib import Path

from scadbuddy.library.mesh_blobs import MeshBlobMissingError


class MemoryMeshStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.puts = 0

    def put(self, path: Path, sha256: str) -> None:
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == sha256
        self.puts += 1
        self.blobs[sha256] = data

    def get(self, sha256: str, dest: Path) -> None:
        if sha256 not in self.blobs:
            raise MeshBlobMissingError(f"no mesh {sha256}")
        dest.write_bytes(self.blobs[sha256])
