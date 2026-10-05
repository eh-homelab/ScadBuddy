"""A template's source at a revision, as one blob (spec 2026-09-27 §6.1).

The API makes it (it has git); a worker unpacks it into the same revision-export
directory `prepare_source` reads, so a populated export is found and git is never
needed on the worker. A snapshot is a cache of git: nothing references it, it is swept
after the grace once no render touches it, and `ensure` makes it again on demand.

`pack_dir` leaves out dot-named files and symlinks, so a template whose tree holds
either renders on a worker without them: such a template renders differently there
than on the API's volume.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from pathlib import Path

from scadbuddy.core.paths import DataPaths, model_path
from scadbuddy.library.history import ModelHistory
from scadbuddy.render.jobs import (
    SnapshotPendingError as SnapshotPendingError,
)
from scadbuddy.render.jobs import (
    SnapshotUnavailableError as SnapshotUnavailableError,  # defined beside resolve_source
)
from scadbuddy.render.jobs import export_revision, touch_export
from scadbuddy.store.archive import pack_dir, unpack_dir
from scadbuddy.store.content import (
    BlobCorruptError,
    BlobMissingError,
    BlobScope,
    ContentStore,
    template_title,
)
from scadbuddy.store.locks import KeyLocks

logger = logging.getLogger(__name__)

#: How long a submit or preview waits for a revision's snapshot to be stored (#686).
#: Only a revision's first pin exports and uploads; past this the caller gets a 503
#: with Retry-After rather than hanging on the upload's own 180 s, and the store
#: carries on behind it.
PIN_TIMEOUT = 30.0
#: How long shutdown lets a store `pin` stopped waiting for finish before cancelling it.
SHUTDOWN_GRACE = 10.0


def snapshot_key(slug: str, revision: str) -> str:
    return f"src-{re.sub(r'[^A-Za-z0-9._-]', '_', slug)}-{revision}"


class SnapshotStore:
    def __init__(
        self,
        content: ContentStore,
        paths: DataPaths,
        history: ModelHistory | None,
        *,
        locks: KeyLocks | None = None,
        pin_timeout: float = PIN_TIMEOUT,
    ) -> None:
        self.content = content
        self.paths = paths
        self.history = history
        #: Shared with the worker's `FontMirror` (Task 8 passes one `KeyLocks`).
        self.locks = locks or KeyLocks()
        self.pin_timeout = pin_timeout
        #: The stores `pin` stopped waiting for, held so they run to the end.
        self._storing: set[asyncio.Task[str]] = set()

    async def pin(self, slug: str, revision: str | None) -> str | None:
        """The revision a render uses, with its snapshot stored. An unpinned request
        renders the template's last commit; None when there is no history at all."""
        if revision is None:
            if self.history is None or not self.history.available:
                return None
            revision = await asyncio.to_thread(self.history.last_commit, model_path(slug))
            if revision is None:
                return None
        # Shielded: a caller that stops waiting (the timeout, or a client gone) leaves
        # the store running, so the retry finds it done or joins it under the key's lock.
        storing = asyncio.create_task(self.ensure(slug, revision))
        try:
            await asyncio.wait_for(asyncio.shield(storing), self.pin_timeout)
        except TimeoutError:
            if storing.done():
                # It finished as the wait ran out: its own outcome, not "pending".
                storing.result()
                return revision
            self._behind(storing)
            raise SnapshotPendingError(
                f"the snapshot of {slug}@{revision[:12]} is still being stored; try again"
                f" in {math.ceil(self.pin_timeout)} s",
                retry_after=max(1, math.ceil(self.pin_timeout)),
            ) from None
        except asyncio.CancelledError:
            if storing.done() and not storing.cancelled() and storing.exception() is not None:
                logger.warning(
                    "storing a snapshot failed as its caller went", exc_info=storing.exception()
                )
            self._behind(storing)
            raise
        return revision

    async def aclose(self, *, grace: float = SHUTDOWN_GRACE) -> None:
        """At shutdown: let the stores no request waits for any more finish within
        ``grace``, then cancel the rest, so none is left pending on a closed loop."""
        if not self._storing:
            return
        _, pending = await asyncio.wait(set(self._storing), timeout=grace)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    def _behind(self, storing: asyncio.Task[str]) -> None:
        """Keep a store no caller waits for any more, and log how it ends."""
        if storing.done():
            return
        self._storing.add(storing)

        def settled(task: asyncio.Task[str]) -> None:
            self._storing.discard(task)
            if not task.cancelled() and (error := task.exception()) is not None:
                logger.warning("storing a snapshot no request waited for failed", exc_info=error)

        storing.add_done_callback(settled)

    async def ensure(self, slug: str, revision: str) -> str:
        key = snapshot_key(slug, revision)
        if await self._stored(key):
            return key
        # One export and upload per key in this process: a revision's first submits
        # (debounced, at once) wait for the first rather than each storing it (#686).
        async with self.locks.hold(key):
            if await self._stored(key):
                return key
            directory = self.paths.model_revision_dir(slug, revision)
            # Marked used before it is packed, so the prune (whose TTL an old revision's
            # export is past) does not take it mid-pack; one it already took is exported
            # again.
            if not (directory.is_dir() and await _used(directory)):
                if self.history is None:
                    raise SnapshotUnavailableError(
                        f"no snapshot of {slug}@{revision} and no history"
                    )
                await asyncio.to_thread(export_revision, self.history, slug, revision, directory)
            data = await asyncio.to_thread(pack_dir, directory)
            await self.content.put(
                "snapshot",
                data,
                name=f"src-{revision[:12]}.zip",
                scope=BlobScope(slug=slug, title=template_title(directory, slug)),
                key=key,
            )
        return key

    async def _stored(self, key: str) -> bool:
        """Whether the index holds ``key``; a hit is touched, as a use."""
        if await asyncio.to_thread(self.content.index.get, key) is None:
            return False
        await self.content.touch(key)
        return True

    async def materialize(self, slug: str, revision: str) -> bool:
        directory = self.paths.model_revision_dir(slug, revision)
        if directory.is_dir() and await _used(directory):
            return True
        key = snapshot_key(slug, revision)
        # One unpack per key in this process: a second one, arriving after the first
        # finished, would swap the export out from under a render reading it.
        async with self.locks.hold(key):
            if directory.is_dir() and await _used(directory):
                return True
            stat = await asyncio.to_thread(self.content.index.get, key)
            # This backend's row only: `read` downloads from this backend.
            if stat is None or stat.ref.backend != self.content.name:
                return False
            try:
                data = await self.content.read(stat.ref)
            except (BlobMissingError, BlobCorruptError):
                # Gone or altered in the backend: forget it, so the next `ensure` (the
                # API's `pin`, which has git) stores it again.
                await self.content.forget(key)
                return False
            await asyncio.to_thread(unpack_dir, data, directory)
        await self.content.touch(key)
        return True


async def _used(directory: Path) -> bool:
    """Mark an export used, so the prune's TTL runs from the last render that read it.
    False when the prune took it first: the caller brings it in again, or says it
    cannot, rather than handing on a directory that is gone."""
    await asyncio.to_thread(touch_export, directory)
    return directory.is_dir()
