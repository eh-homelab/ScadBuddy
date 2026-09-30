"""The blob store (spec 2026-09-27 §6). Phase 1 ships the interface directory-shaped
over the data volume: a blob is the directory a piece rendered into, referenced by the
jobs (and later outputs) that need it, swept when nothing does. Phase 3 (#426) adds
`fetch`/`publish`, which `store/cache.py` implements over a remote `ContentStore`."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol

from scadbuddy.store.content_models import BlobScope
from scadbuddy.store.refs import BlobRefs

logger = logging.getLogger(__name__)


class PieceStateLostError(LookupError):
    """A stage found nothing to continue from: the store no longer holds the piece an
    earlier stage published (deleted in the backend between two stages)."""


class BlobStore(Protocol):
    backend: str

    def dir_for(self, key: str) -> Path:
        """The blob's directory, created if missing. Every call, for a blob that
        already exists too, refreshes its `touched_at`: a claimant calls this before
        `BlobRefs.add`, and that is what keeps `sweep_blobs` from taking a blob
        claimed between its `referenced()` snapshot and its pass over the keys."""
        ...

    def exists(self, key: str) -> bool:
        """Synchronous (it may read the index): call it from a thread, not the loop."""
        ...

    def remove(self, key: str) -> None: ...
    def keys(self) -> list[str]: ...
    def touched_at(self, key: str) -> float: ...

    async def fetch(self, key: str) -> bool:
        """Make ``dir_for(key)`` hold the stored blob; False when there is none."""
        ...

    async def checkout(self, key: str) -> str | None:
        """`fetch` for a stage about to write into ``dir_for(key)``: the directory stops
        being a hit until that stage publishes. Returns the sha it was fetched at, the
        baseline for `publish_fresh`; raises `PieceStateLostError` when nothing is stored."""
        ...

    async def checkout_fresh(self, key: str) -> str | None:
        """For a stage that renders from nothing (`render_main`): the directory stops
        being a hit, and the baseline is what the index holds now."""
        ...

    async def publish(self, key: str, *, scope: BlobScope) -> None:
        """Store ``dir_for(key)`` as it now is, for any other process to fetch; refused
        if the store moved on since this directory was fetched."""
        ...

    async def indexed_sha(self, key: str) -> str | None:
        """The sha the store holds for ``key`` now; None when it holds nothing."""
        ...

    async def publish_fresh(self, key: str, *, scope: BlobScope, expected: str | None) -> None:
        """`publish` for a directory built from nothing (`render_main`): the swap is
        against ``expected``, read with `indexed_sha` when the activity started, not
        against a marker the directory never had."""
        ...


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
        try:
            store.remove(key)
        except OSError:
            # One blob that cannot go (EACCES, EBUSY) must not keep every other one.
            logger.exception("could not remove a blob", extra={"key": key})
            continue
        removed.append(key)
    return removed


__all__ = ["BlobRefs", "BlobStore", "sweep_blobs"]
