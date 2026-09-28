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
import re

from scadbuddy.core.paths import DataPaths, model_path
from scadbuddy.library.history import ModelHistory
from scadbuddy.render.jobs import export_revision
from scadbuddy.store.archive import pack_dir, unpack_dir
from scadbuddy.store.content import (
    BlobCorruptError,
    BlobMissingError,
    BlobScope,
    ContentStore,
    template_title,
)
from scadbuddy.store.locks import KeyLocks


def snapshot_key(slug: str, revision: str) -> str:
    return f"src-{re.sub(r'[^A-Za-z0-9._-]', '_', slug)}-{revision}"


class SnapshotUnavailableError(RuntimeError):
    """No snapshot is stored and this process has no git history to make one."""


class SnapshotStore:
    def __init__(
        self,
        content: ContentStore,
        paths: DataPaths,
        history: ModelHistory | None,
        *,
        locks: KeyLocks | None = None,
    ) -> None:
        self.content = content
        self.paths = paths
        self.history = history
        #: Shared with the worker's `FontMirror` (Task 8 passes one `KeyLocks`).
        self.locks = locks or KeyLocks()

    async def pin(self, slug: str, revision: str | None) -> str | None:
        """The revision a render uses, with its snapshot stored. An unpinned request
        renders the template's last commit; None when there is no history at all."""
        if revision is None:
            if self.history is None or not self.history.available:
                return None
            revision = await asyncio.to_thread(self.history.last_commit, model_path(slug))
            if revision is None:
                return None
        await self.ensure(slug, revision)
        return revision

    async def ensure(self, slug: str, revision: str) -> str:
        key = snapshot_key(slug, revision)
        if await asyncio.to_thread(self.content.index.get, key) is not None:
            await self.content.touch(key)
            return key
        directory = self.paths.model_revision_dir(slug, revision)
        if not directory.is_dir():
            if self.history is None:
                raise SnapshotUnavailableError(f"no snapshot of {slug}@{revision} and no history")
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

    async def materialize(self, slug: str, revision: str) -> bool:
        directory = self.paths.model_revision_dir(slug, revision)
        if directory.is_dir():
            return True
        key = snapshot_key(slug, revision)
        # One unpack per key in this process: a second one, arriving after the first
        # finished, would swap the export out from under a render reading it.
        async with self.locks.hold(key):
            if directory.is_dir():
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
