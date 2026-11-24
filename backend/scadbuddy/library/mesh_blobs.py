"""Mesh files in the blob store, a pointer in git (#7313).

A mesh model's part meshes (``<slug>/meshes/o<N>-p<M>.3mf``, #2146) can each run to
over 100 MB, and GitHub refuses a push that holds one
(https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github#file-size-limits),
so no new mesh may enter the history the off-box push (#4974) sends. (The one already in
it is #7314: this alone does not make that push succeed.) The working tree still holds
every mesh's bytes, so every reader of the model directory is unchanged. In a repository
that keeps pointers (`ModelHistory._keeps_pointers`; no start turns that on until
`WRITE_POINTERS` is), ``.gitignore`` keeps the bytes out of git (`GITIGNORE_LINES`), and
git tracks a small pointer beside each one, so an off-box copy has pointers only and a
restore from it needs the blob store too. A pointer is
``<name>.3mf.blob`` (`MeshPointer`: the sha256 and size). The bytes are a blob of
ScadBuddy's own store, ``attachment-<sha256>`` (`orders/attachments.py` `keep_file`, on
the local store and a remote one alike), held in ``blob_refs`` by `HOLDER` with the
sha256 as holder id (`library/mesh_store.py` `LoopMeshStore`). Nothing ever drops that
hold: an old revision's pointer may name the blob for as long as the history stands.

`library/history.py` `ModelHistory` writes the pointers when it stages a commit and
fills the meshes back in wherever it writes a revision into a directory (export,
restore, an upstream merge). Commits made before this hold the meshes' own bytes; a
revision has one or the other, and both are read.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from scadbuddy.core.gitrepo import GitError
from scadbuddy.core.paths import BUILTIN_DIR

#: The directory of a model's meshes, and a mesh's and its pointer's suffixes.
MESHES_DIR = "meshes"
MESH_SUFFIX = ".3mf"
POINTER_SUFFIX = ".blob"
#: The ``blob_refs`` holder of a mesh's bytes; the holder id is the sha256.
HOLDER = "model_mesh"
#: The one commit that turns meshes still tracked as bytes into pointers.
CONVERT_MESSAGE = "Move mesh files to the blob store"
#: Whether a start turns pointers on and converts. Off in the release that first reads
#: them: a pod that does not know pointers exports a revision with no meshes into caches
#: keyed by revision alone, and puts the bytes back in git. Turn it on only in a release
#: after every pod runs one that reads them; a rollback past that release is not safe then.
WRITE_POINTERS = False
#: What ``.gitignore`` keeps out: exactly the paths `is_mesh_path` answers for.
GITIGNORE_LINES = (f"*/{MESHES_DIR}/*{MESH_SUFFIX}", f"{BUILTIN_DIR}/*/{MESHES_DIR}/*{MESH_SUFFIX}")
#: The pathspecs of `GITIGNORE_LINES`, for ``git ls-files``.
PATHSPECS = tuple(f":(glob){line}" for line in GITIGNORE_LINES)
#: How long a mesh's store or fetch may take: a mesh runs to hundreds of MB.
MESH_TIMEOUT = 600.0
_SHA256 = re.compile(r"[0-9a-f]{64}")


class MeshBlobMissingError(GitError):
    """The store holds no mesh under a pointer's sha256, or other bytes than it names."""


@dataclass(frozen=True)
class MeshPointer:
    sha256: str
    size: int

    def dumps(self) -> bytes:
        return (
            json.dumps({"sha256": self.sha256, "size": self.size}, sort_keys=True) + "\n"
        ).encode()

    @classmethod
    def loads(cls, data: bytes) -> MeshPointer:
        """The pointer in ``data``; ``ValueError`` when it is not one."""
        try:
            raw = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("not a mesh pointer") from None
        sha = raw.get("sha256") if isinstance(raw, dict) else None
        size = raw.get("size") if isinstance(raw, dict) else None
        if not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            raise ValueError("not a mesh pointer")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("not a mesh pointer")
        return cls(sha256=sha, size=size)


def is_mesh_name(name: str) -> bool:
    """Whether ``name``, relative to a model's directory, is one of its meshes."""
    parts = name.split("/")
    return len(parts) == 2 and parts[0] == MESHES_DIR and parts[1].endswith(MESH_SUFFIX)


def is_mesh_path(path: str) -> bool:
    """Whether ``path``, relative to the repository, is a mesh `GITIGNORE_LINES` ignores."""
    parts = path.split("/")
    if len(parts) == 4 and parts[0] == BUILTIN_DIR:
        return is_mesh_name("/".join(parts[2:]))
    return len(parts) == 3 and is_mesh_name("/".join(parts[1:]))


def is_pointer_path(path: str) -> bool:
    return path.endswith(POINTER_SUFFIX) and is_mesh_path(path.removesuffix(POINTER_SUFFIX))


def is_pointer_name(name: str) -> bool:
    return name.endswith(POINTER_SUFFIX) and is_mesh_name(name.removesuffix(POINTER_SUFFIX))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_verified(source: Path, dest: Path, sha256: str) -> None:
    """``source`` copied to ``dest`` under a name of its own, moved into place only when
    its bytes hash to ``sha256``; `MeshBlobMissingError` otherwise, with ``dest`` as it was."""
    staging = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as reader, staging.open("xb") as writer:
            for chunk in iter(lambda: reader.read(1 << 20), b""):
                digest.update(chunk)
                writer.write(chunk)
        if digest.hexdigest() != sha256:
            raise MeshBlobMissingError(f"the stored mesh {sha256} holds other bytes")
        os.replace(staging, dest)
    finally:
        with contextlib.suppress(FileNotFoundError):
            staging.unlink()


class MeshStore(Protocol):
    """Where `ModelHistory` keeps meshes' bytes. Sync: the history runs on a thread."""

    def put(self, path: Path) -> MeshPointer:
        """Keep the file at ``path``, held for good; the pointer that names the bytes
        kept. `GitError` when the store cannot."""
        ...

    def get(self, sha256: str, dest: Path) -> None:
        """Write the mesh ``sha256`` at ``dest``, its bytes checked;
        `MeshBlobMissingError` when the store has no such mesh."""
        ...
