"""The blob store (spec 2026-09-27 §6). Phase 1 ships the interface directory-shaped
over the data volume: a blob is the directory a piece rendered into, referenced by the
jobs (and later outputs) that need it, swept when nothing does. Phase 3 (#426) adds the
Bambuddy backend and the byte-stream calls."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol

from scadbuddy.store.refs import BlobRefs

logger = logging.getLogger(__name__)


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


def _touched_at(store: BlobStore, key: str) -> float | None:
    """The blob's `touched_at`, or None when there is none to read: gone since
    `keys()` (another sweep took it), or failing (EACCES, ESTALE; logged)."""
    try:
        return store.touched_at(key)
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("could not read an unreferenced blob's touch", extra={"key": key})
        return None


def sweep_blobs(
    store: BlobStore, refs: BlobRefs, *, grace: float, now: float | None = None
) -> list[str]:
    """Remove every blob nothing references that has not been touched for ``grace``.

    What protects a blob being claimed is the grace window, not exclusion. A claimant
    calls `dir_for` (which touches the blob) before `refs.add`, and the sweep re-reads
    the touch right before it removes a blob, so a claim landing after its
    `referenced()` snapshot or after its first read survives. A same-key claim that
    lands between that re-read and the removal (about one syscall wide) can still lose
    its directory; `dir_for` then recreates it empty."""
    cutoff = (now if now is not None else time.time()) - grace
    kept = refs.referenced()
    removed: list[str] = []
    blob_keys = store.keys()
    for key in blob_keys:
        if key in kept:
            continue
        touched = _touched_at(store, key)
        if touched is None or touched > cutoff:
            continue
        touched = _touched_at(store, key)  # a claimant's `dir_for` touches first
        if touched is None or touched > cutoff:
            continue
        try:
            store.remove(key)
        except OSError:
            # Logged and skipped, like the other sweeps: one blob that cannot go
            # (EACCES, EBUSY) must not keep every one after it.
            logger.exception("could not remove an unreferenced blob", extra={"key": key})
            continue
        removed.append(key)
    return removed


__all__ = ["BlobRefs", "BlobStore", "sweep_blobs"]
