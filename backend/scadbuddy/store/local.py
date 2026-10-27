from __future__ import annotations

import asyncio
import os
import re
import shutil
import uuid
from collections.abc import AsyncGenerator
from contextlib import suppress
from pathlib import Path
from typing import get_args

from scadbuddy.store import PieceStateLostError
from scadbuddy.store.content_models import (
    BlobData,
    BlobKind,
    BlobMissingError,
    BlobScope,
    blob_digest,
)

_KEY = re.compile(r"[A-Za-z0-9._-]{1,128}")


class LocalBlobStore:
    backend = "local"

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        # fullmatch: `$` would also accept a key ending in a newline.
        if not _KEY.fullmatch(key) or key in (".", ".."):
            raise ValueError(f"not a blob key: {key!r}")
        return self.root / key

    def dir_for(self, key: str) -> Path:
        path = self._path(key)
        path.mkdir(parents=True, exist_ok=True)
        os.utime(path, None)
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).is_dir()

    def remove(self, key: str) -> None:
        """Already gone is fine; any other failure raises, so a sweep never counts a
        blob still on disk as removed."""
        with suppress(FileNotFoundError):
            shutil.rmtree(self._path(key))

    def keys(self) -> list[str]:
        if not self.root.is_dir():
            return []
        # Only names `_path` accepts: a stray directory (`lost+found`) is no blob, and
        # its name would fail the sweep's every call on it.
        return sorted(
            p.name
            for p in self.root.iterdir()
            if p.is_dir() and _KEY.fullmatch(p.name) and p.name not in (".", "..")
        )

    def touched_at(self, key: str) -> float:
        return self._path(key).stat().st_mtime

    async def fetch(self, key: str) -> bool:
        # One volume: the directory is the blob.
        return self.exists(key)

    async def claim(self, key: str) -> bool:
        return await asyncio.to_thread(self._claim, key)

    def _claim(self, key: str) -> bool:
        # One volume: the directory is the blob. One with nothing visible in it was
        # made by `dir_for` for a write that never finished, and holds no blob; a
        # file at the key's path is none either, as for `exists`. The touch comes first
        # (a sweep must not take a blob between the check and the hold), so a
        # half-written directory looks fresh too; `keep` writes it again at once.
        path = self._path(key)
        try:
            os.utime(path, None)
            return any(not entry.name.startswith(".") for entry in path.iterdir())
        except (FileNotFoundError, NotADirectoryError):
            return False

    async def checkout(self, key: str) -> str | None:
        if not self.exists(key):
            raise PieceStateLostError(key)
        return None

    async def checkout_fresh(self, key: str) -> str | None:
        return None

    async def publish(self, key: str, *, scope: BlobScope) -> None:
        return None

    async def indexed_sha(self, key: str) -> str | None:
        return None

    async def publish_fresh(self, key: str, *, scope: BlobScope, expected: str | None) -> None:
        return None


_OBJECT = re.compile(
    "(" + "|".join(re.escape(kind) for kind in get_args(BlobKind)) + ")/[0-9a-f]{64}"
)
_CHUNK = 1 << 20


class LocalContentBackend:
    """Objects under a directory, one file per sha256: tests, `verify.sh`, and the
    stand-in for a remote backend in the worker-cache tests."""

    backend = "local"

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, backend_id: str) -> Path:
        # fullmatch: `$` would also accept an id ending in a newline.
        if not _OBJECT.fullmatch(backend_id):
            raise ValueError(f"not a local object id: {backend_id!r}")
        return self.root / backend_id

    async def upload(self, kind: BlobKind, data: BlobData, *, name: str, scope: BlobScope) -> str:
        def write() -> str:
            backend_id = f"{kind}/{blob_digest(data)[0]}"
            path = self._path(backend_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            staging = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
            if isinstance(data, bytes):
                staging.write_bytes(data)
            else:
                with staging.open("wb") as out:
                    shutil.copyfileobj(data, out)
                data.seek(0)
            os.replace(staging, path)
            return backend_id

        return await asyncio.to_thread(write)

    async def download(self, backend_id: str) -> AsyncGenerator[bytes]:
        try:
            data = await asyncio.to_thread(self._path(backend_id).read_bytes)
        except FileNotFoundError:
            raise BlobMissingError(backend_id) from None
        for start in range(0, len(data), _CHUNK):
            yield data[start : start + _CHUNK]

    async def exists(self, backend_id: str) -> bool:
        return await asyncio.to_thread(self._path(backend_id).is_file)

    async def remove(self, backend_id: str) -> None:
        await asyncio.to_thread(self._path(backend_id).unlink, missing_ok=True)
