"""The blob store (spec 2026-09-27 §6). Phase 1 ships the interface directory-shaped
over the data volume: a blob is the directory a piece rendered into, referenced by the
jobs (and later outputs) that need it, swept when nothing does. Phase 3 (#426) adds the
Bambuddy backend and the byte-stream calls."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

from scadbuddy.store.refs import BlobRefs


class BlobStore(Protocol):
    backend: str

    def dir_for(self, key: str) -> Path:
        """The blob's directory, created if missing. Every call, for a blob that
        already exists too, refreshes its `touched_at`: a claimant calls this before
        `BlobRefs.add`, and that is what keeps `sweep_blobs` from taking a blob
        claimed between its `referenced()` snapshot and its pass over the keys."""
        ...

    def exists(self, key: str) -> bool: ...
    def remove(self, key: str) -> None: ...
    def keys(self) -> list[str]: ...
    def touched_at(self, key: str) -> float: ...


def sweep_blobs(
    store: BlobStore, refs: BlobRefs, *, grace: float, now: float | None = None
) -> list[str]:
    """Remove every blob nothing references that has not been touched for ``grace``.

    A claimant calls `dir_for` (which touches the blob's mtime) before `refs.add`, so
    a blob claimed between this sweep's `referenced()` snapshot and its `keys()` loop
    is still within the grace window and survives."""
    cutoff = (now if now is not None else time.time()) - grace
    kept = refs.referenced()
    removed: list[str] = []
    blob_keys = store.keys()
    for key in blob_keys:
        if key in kept:
            continue
        try:
            touched = store.touched_at(key)
        except FileNotFoundError:
            continue  # gone since `keys()`: another sweep took it
        if touched > cutoff:
            continue
        store.remove(key)
        removed.append(key)
    return removed


__all__ = ["BlobRefs", "BlobStore", "sweep_blobs"]
