"""Every API replica follows the models repository's HEAD over Postgres (#2507).

The replicas share ``/data`` over NFS, which tells a client nothing about what
another client changed: inotify sees local changes only, and the Linux client has no
NFSv4.1 directory notifications. So a writer announces each HEAD it records
(`repo_lock.HEAD_CHANNEL`, in the transaction that records it, under the write lock),
and each replica, on hearing it:

1. has its NFS client revalidate ``.git``, ``models/`` and the directories of the
   templates the commit touched, so the next read here sees the commit rather than
   what was cached before it; and
2. drops what the catalogue caches about those templates' revisions.

That is what makes the realtime events about a commit safe to act on. The writer
publishes them on the bus as before -- ``version.committed`` for every commit
(`api.deps.announce_commits`), ``source.changed`` from the routes that change a
source -- and they are NOTIFYed after the HEAD is, which commits under the write lock
first. Postgres delivers a session's notifications in commit order, both channels
share this process's listener, and the listener awaits the refresh before it reads
the next notification (the NFS work itself runs on a thread, off the event loop), so a
Customize or editor page that hears "changed elsewhere" and reads again is answered
by a replica that already sees the change, whichever one it lands on. Publishing a
second copy from each replica would only send every page each event twice.

A NOTIFY sent while the LISTEN connection was down reached nobody here, so a
reconnect refreshes every template. The channel is registered on the process's
shared `PgListener` before the event bus's, so the refresh lands before the bus
tells subscribers to resync, and a commit's announcement is heard before the
events the writer publishes after it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from scadbuddy.core.paths import DataPaths, revalidate_dirs
from scadbuddy.core.pg_listener import PgListener
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.repo_lock import HEAD_CHANNEL, decode_change

logger = logging.getLogger(__name__)


class ModelsHeadFollower:
    """This process's view of the models repository, kept current by the writers'
    announcements. :attr:`catalogue` is bound once built: the channel has to be
    registered before the event bus registers its own."""

    def __init__(self, paths: DataPaths, history: ModelHistory, listener: PgListener) -> None:
        self.paths = paths
        self.history = history
        self.catalogue: Catalogue | None = None
        #: The last HEAD announced here.
        self.head: str | None = None
        listener.listen(HEAD_CHANNEL, on_notify=self._received, on_connect=self._connected)

    async def _received(self, payload: str) -> None:
        try:
            change = decode_change(payload)
        except ValueError:
            logger.warning("ignored an undecodable models HEAD payload")
            return
        await self.refresh(change.models)
        # Only once this replica sees it (#2617): whatever waits on `head` reads it then.
        self.head = change.commit

    async def _connected(self, reconnected: bool) -> None:
        if reconnected:
            logger.warning("the models HEAD listener reconnected; refreshing every template")
            await self.refresh(None)

    async def refresh(self, model_ids: list[str] | None) -> None:
        """Revalidate the repository and the templates ``model_ids`` -- every one for
        None -- and forget what is cached about them. The revalidation walks and opens
        directories over NFS, so it runs on a thread (#2617): a large library or a slow
        mount holds up this channel, never the event loop."""
        await asyncio.to_thread(self.revalidate, model_ids)
        if self.catalogue is not None:
            self.catalogue.forget(model_ids)

    def revalidate(self, model_ids: list[str] | None) -> None:
        """Have the NFS client revalidate the repository and those templates."""
        self.history.revalidate()
        directories: list[Path] = [self.paths.models, self.paths.builtins]
        if model_ids is None:
            for parent in (self.paths.models, self.paths.builtins):
                directories += _template_dirs(parent)
        else:
            directories += [self.paths.model_dir(model_id) for model_id in model_ids]
        revalidate_dirs(*directories)


def _template_dirs(parent: Path) -> list[Path]:
    try:
        with os.scandir(parent) as entries:
            return [
                Path(entry.path)
                for entry in entries
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False)
            ]
    except OSError:
        return []
