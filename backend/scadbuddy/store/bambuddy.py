"""The Bambuddy backend of the blob store (spec 2026-09-27 §6.3).

Layout (#316, #317): the Settings `library_folder_id` folder is ScadBuddy's inbox and
the only place it deletes from. Under it, one folder per template named by its title,
and under that `Work/`, where pieces, snapshots and assets live; fonts, which belong to
no template, live in `Shared/Work/`. A project's folder is the user's record: an output
may be written there (`folder="output"` with a `project_id`), nothing there is moved or
deleted. No dot-named folders: Bambuddy shows them.

Every folder ScadBuddy makes or adopts is recorded in `store_folders`, under the
Bambuddy instance (its base URL) and inbox it was found on, and a delete is refused
unless the file sits in one recorded as `work` there (#683). `FolderFinder` finds,
records and forgets them over SQL: the API's, and the render worker's through the API's
CRUD routes (`crud/render.py` `RemoteStoreFolders`, #5298), both `KeepsFolders`. What
each file is lives in `store_blobs`, so a fetch is by file id, never a folder scan.

Known limits (#682): a folder is adopted by `(parent, name)`, so two templates whose
titles clean to the same name share one folder pair, and a template titled "Shared"
shares the fonts' folder. A retitled template keeps its old folder (rows are keyed by
slug). Deletes stay safe: they are by file id and go through the `Work` check.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.commits import (
    FOLDER_NOT_FOUND,
    PARENT_FOLDER_NOT_FOUND,
    refused,
    upload_into,
    visible,
)
from scadbuddy.bambuddy.errors import not_configured
from scadbuddy.bambuddy.models import FolderCreate
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import RenderStoreSettings, load_render_store_settings
from scadbuddy.store.content_models import BlobData, BlobKind, BlobMissingError, BlobScope
from scadbuddy.store.content_models import RefusedDeleteError as RefusedDeleteError
from scadbuddy.store.index import BlobIndex, IndexesBlobs, Pool

logger = logging.getLogger(__name__)

WORK = "Work"
SHARED_TITLE = "Shared"
_MEDIA_TYPES = {
    ".zip": "application/zip",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".3mf": "model/3mf",
}


@dataclass(frozen=True)
class BambuddyTarget:
    config: BambuddyConfig
    inbox_id: int

    @property
    def instance(self) -> str:
        """Which Bambuddy a recorded folder id belongs to: its base URL (#683),
        normalised (`instance_key`) so a respelling of the same URL keeps its folders."""
        return instance_key(self.config.base_url)


_DEFAULT_PORTS = {"http": 80, "https": 443}


def instance_key(url: str) -> str:
    """A Bambuddy base URL as `store_folders` keys it (#1431): scheme and host
    lowercased, a default port dropped, trailing slashes stripped. `HTTP://Host:80/`
    and `http://host` are one instance; another host, port or path is another."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"  # an IPv6 literal keeps its brackets
    port = parts.port
    netloc = host if port is None or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"
    return f"{scheme}://{netloc}{parts.path.rstrip('/')}"


@dataclass(frozen=True)
class Inbox:
    """Where this call's folders are: one inbox on one Bambuddy instance."""

    instance: str
    id: int


def folder_name(title: str) -> str:
    """A Bambuddy folder name from a template title: no separators, no leading dots."""
    cleaned = re.sub(r"\s+", " ", re.sub(r"[\\/\x00-\x1f]", " ", title)).strip()
    cleaned = cleaned.lstrip(".").strip()
    return cleaned[:100] or "Template"


class StoreSettingsSource:
    """The render key and the inbox, re-read at most every ``ttl`` seconds, so a key
    rotated in Settings reaches every worker without a restart (spec §9, §10). A subclass
    says where they are read from (`_read`): the API's database here, the API's CRUD route
    on a render worker (`crud/render.py`, #5298)."""

    def __init__(
        self,
        *,
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl = ttl
        self._clock = clock
        self._cached: RenderStoreSettings | None = None
        self._at = 0.0
        #: False while a re-read fails and `current` answers the last good settings.
        self.fresh = True

    async def current(self) -> RenderStoreSettings:
        """The settings, re-read once ``ttl`` has passed. A re-read that fails keeps the
        last good ones (and clears `fresh`) rather than failing `/healthz` and the
        renders with the database or the API; only a first read that fails raises."""
        now = self._clock()
        if self._cached is None or now - self._at >= self.ttl:
            try:
                self._cached = await self._read()
            except Exception:
                if self._cached is None:
                    raise
                logger.warning("could not re-read the store settings; keeping the last good")
                self.fresh = False
            else:
                self.fresh = True
            self._at = now
        return self._cached

    async def _read(self) -> RenderStoreSettings:
        raise NotImplementedError

    def seed(self, current: RenderStoreSettings) -> None:
        """Start from the settings the process read at start, so a source that is
        down at the first re-read leaves these in place rather than nothing."""
        self._cached, self._at = current, self._clock()

    def invalidate(self) -> None:
        """Re-read on the next call: the API calls this after its own settings write."""
        self._at = float("-inf")

    async def target(self) -> BambuddyTarget:
        return render_target(await self.current())


def render_target(current: RenderStoreSettings) -> BambuddyTarget:
    """The Bambuddy and inbox the store's settings name, with the render key."""
    if not current.bambuddy_url or current.library_folder_id is None:
        raise not_configured(
            "the Bambuddy store needs a Bambuddy URL and a library folder in Settings"
        )
    return BambuddyTarget(
        config=BambuddyConfig(base_url=current.bambuddy_url.rstrip("/"), api_key=current.api_key),
        inbox_id=current.library_folder_id,
    )


class RenderSettingsSource(StoreSettingsSource):
    """The store settings read from the database: the API's (`load_render_store_settings`)."""

    def __init__(
        self,
        pool: Pool,
        defaults: Settings,
        *,
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(ttl=ttl, clock=clock)
        self._pool = pool
        self._defaults = defaults

    async def _read(self) -> RenderStoreSettings:
        return await asyncio.to_thread(load_render_store_settings, self._pool, self._defaults)


#: How long a find waits for another finder's folder lock before it fails. The same in
#: the API and in an older worker that still finds over SQL, so the two exclude each
#: other during a roll (#5298).
FOLDER_LOCK_SECONDS = 30
FOLDER_LOCK_TIMEOUT = f"{FOLDER_LOCK_SECONDS}s"

#: A recorded folder's role: a template's own folder, or the `Work/` under it.
FolderRole = Literal["template", "work"]


def folder_lock_key(instance: str, inbox: int, slug: str, role: str) -> int:
    """The advisory-lock key for finding one folder, the same in every process."""
    parts = ("store-folder", instance, inbox, slug, role)
    return int.from_bytes(hashlib.sha256(repr(parts).encode()).digest()[:8], "big", signed=True)


class BambuddyContentBackend:
    backend = "bambuddy"

    def __init__(
        self,
        target: Callable[[], Awaitable[BambuddyTarget]],
        pool: Pool,
        *,
        http: httpx.AsyncClient | None = None,
        index: IndexesBlobs | None = None,
        folders: KeepsFolders | None = None,
    ) -> None:
        """``index`` says whether a row names an uploaded file (`sweep_unrecorded`);
        ``folders`` finds, records and forgets the store's folders. Each is SQL on
        ``pool`` unless given (the render worker's are its CRUD routes', #5298)."""
        self._target = target
        self._index: IndexesBlobs = index if index is not None else BlobIndex(pool)
        self._kept: KeepsFolders = folders if folders is not None else FolderFinder(pool)
        self._http = http or httpx.AsyncClient()
        self._owns_http = http is None
        self._folders: dict[tuple[str, int, str, str], int] = {}
        #: One per folder being found or made, so this process's other callers for it
        #: wait in the event loop rather than each holding a thread and a connection.
        self._finding: dict[tuple[str, int, str, str], asyncio.Lock] = {}

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[tuple[BambuddyClient, Inbox]]:
        target = await self._target()
        inbox = Inbox(target.instance, target.inbox_id)
        async with BambuddyClient(target.config, http=self._http) as client:
            yield client, inbox

    # --- ContentBackend ------------------------------------------------------

    async def upload(self, kind: BlobKind, data: BlobData, *, name: str, scope: BlobScope) -> str:
        media = _MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")
        async with self._client() as (client, inbox):
            for attempt in (1, 2):
                try:
                    folder_id = await self._folder_for(client, inbox, scope)
                    # A folder made moments ago is waited for, not made again (#2263).
                    uploaded = await upload_into(
                        client, name, data, folder_id=folder_id, media_type=media
                    )
                except ApiError as error:
                    # The folder (or its template folder) was deleted in Bambuddy's UI,
                    # or never kept: forget it, make it again.
                    if error.status != 404 or attempt == 2:
                        raise
                    await self._forget(inbox, scope.slug or "")
                    continue
                return str(uploaded.id)
        raise AssertionError("unreachable")

    async def download(self, backend_id: str) -> AsyncGenerator[bytes]:
        async with self._client() as (client, _):
            try:
                async with aclosing(client.download_library_file(int(backend_id))) as chunks:
                    async for chunk in chunks:
                        yield chunk
            except ApiError as error:
                if error.status == 404:
                    raise BlobMissingError(backend_id) from None
                raise

    async def exists(self, backend_id: str) -> bool:
        async with self._client() as (client, _):
            try:
                await client.library_file(int(backend_id))
            except ApiError as error:
                if error.status == 404:
                    return False
                raise
        return True

    async def remove(self, backend_id: str) -> None:
        async with self._client() as (client, inbox):
            # #1272: the file's folder is the last thing read before the DELETE, so a
            # move in Bambuddy's UI has one round trip to slip through, not two reads.
            work = await self._work_folders(inbox)
            try:
                file = await client.library_file(int(backend_id))
            except ApiError as error:
                if error.status == 404:
                    return
                raise
            if file.folder_id not in work:
                raise RefusedDeleteError(
                    f"library file {backend_id} is in folder {file.folder_id}, not a ScadBuddy"
                    " Work folder; it is left alone"
                )
            try:
                await client.delete_library_file(int(backend_id))
            except ApiError as error:
                if error.status != 404:
                    raise

    async def sweep_unrecorded(
        self, *, cutoff: datetime, recorded: Callable[[str], bool] | None = None
    ) -> list[str]:
        """Delete the files in this Bambuddy's recorded `Work/` folders that no
        `store_blobs` row names and that were added before ``cutoff`` (#701): an upload
        whose process died before `ContentStore.put` wrote its row. A newer one may be
        an upload whose row is about to be written, so it stays. Each file's folder is
        read again right before its DELETE, as `remove` does (#1272): the listing can be
        many round trips old by then, and a file moved out of `Work/` meanwhile is left
        alone (#2115). ``recorded`` says whether a row names an id (by default, one of
        this backend's). Returns the deleted ids."""
        is_recorded = self._recorded_id if recorded is None else recorded
        removed: list[str] = []
        async with self._client() as (client, inbox):
            for folder_id in sorted(await self._work_folders(inbox)):
                try:
                    files = await client.library_listing(folder_id=folder_id)
                except ApiError as error:
                    if error.status == 404:  # deleted in Bambuddy's UI; made again on use
                        continue
                    raise
                for file in files:
                    added = file.created_at
                    if added is None or added.replace(tzinfo=UTC) > cutoff:
                        continue
                    if await asyncio.to_thread(is_recorded, str(file.id)):
                        continue
                    try:
                        still = await client.library_file(file.id)
                    except ApiError as error:
                        if error.status == 404:
                            continue
                        raise
                    if still.folder_id != folder_id:
                        continue
                    try:
                        await client.delete_library_file(file.id)
                    except ApiError as error:
                        if error.status != 404:
                            raise
                    removed.append(str(file.id))
        return removed

    def _recorded_id(self, backend_id: str) -> bool:
        return self._index.recorded(self.backend, backend_id)

    # --- folders -------------------------------------------------------------

    async def _folder_for(self, client: BambuddyClient, inbox: Inbox, scope: BlobScope) -> int:
        if scope.folder == "output" and scope.project_id is not None:
            folders = await client.folders_by_project(scope.project_id)
            if not folders:
                raise not_configured(
                    f"project {scope.project_id} has no library folder in Bambuddy"
                )
            return folders[0].id
        slug = scope.slug or ""
        title = scope.title or scope.slug or SHARED_TITLE
        role: FolderRole = "template" if scope.folder == "output" else "work"
        cache_key = (inbox.instance, inbox.id, slug, role)
        found = self._folders.get(cache_key)
        if found is not None:
            return found
        # This process's other callers for the folder wait in the event loop.
        async with self._finding.setdefault(cache_key, asyncio.Lock()):
            found = self._folders.get(cache_key)
            if found is None:
                found = await self._kept.find(client, inbox, slug, role, title)
                self._folders[cache_key] = found
            return found

    async def _forget(self, inbox: Inbox, slug: str) -> None:
        await self._kept.forget(inbox, slug)
        for role in ("template", "work"):
            self._folders.pop((inbox.instance, inbox.id, slug, role), None)

    async def _work_folders(self, inbox: Inbox) -> set[int]:
        return await self._kept.work_folders(inbox)


class KeepsFolders(Protocol):
    """Finds, records and forgets the store's folders on one Bambuddy inbox: SQL
    (`FolderFinder`), or the render worker's CRUD routes (`crud/render.py`
    `RemoteStoreFolders`, #5298), where the API finds them with its own client and target.

    `find` answers the folder of ``slug`` in ``role``, made under ``inbox`` (named
    ``title``, cleaned by `folder_name`) or under the template's folder (`Work/`) when
    none is recorded, and records it. `forget` drops the template's rows, `work_folders`
    lists the `Work/` folders recorded under ``inbox``."""

    async def find(
        self, client: BambuddyClient, inbox: Inbox, slug: str, role: FolderRole, title: str
    ) -> int: ...

    async def forget(self, inbox: Inbox, slug: str) -> None: ...

    async def work_folders(self, inbox: Inbox) -> set[int]: ...


class FolderFinder:
    """`KeepsFolders` over SQL on ``pool``: the API's. A find takes the folder's
    advisory lock (`folder_lock_key`) across its Bambuddy calls, so two finders never
    both make a folder."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def find(
        self, client: BambuddyClient, inbox: Inbox, slug: str, role: FolderRole, title: str
    ) -> int:
        template = await self._ensure(client, inbox, slug, "template", folder_name(title), inbox.id)
        if role == "template":
            return template
        return await self._ensure(client, inbox, slug, "work", WORK, template)

    async def _ensure(
        self,
        client: BambuddyClient,
        inbox: Inbox,
        slug: str,
        role: FolderRole,
        name: str,
        parent_id: int,
    ) -> int:
        # Across processes: the lock's own connection, used for the lookup and the
        # record too, so a find takes no pooled connection and no thread.
        async with self._locked(folder_lock_key(inbox.instance, inbox.id, slug, role)) as conn:
            found = await self._recorded(conn, inbox, slug, role)
            if found is None:
                found = await self._adopt_or_create(client, name, parent_id)
                await self._record(conn, inbox, slug, role, found)
        return found

    async def _adopt_or_create(self, client: BambuddyClient, name: str, parent_id: int) -> int:
        for root in await client.folders():
            for folder in root.walk():
                if folder.parent_id == parent_id and folder.name == name:
                    return folder.id
        create = FolderCreate(name=name, parent_id=parent_id)
        try:
            return (await client.create_folder(create)).id
        except ApiError as error:
            # The parent is the template folder this find made a moment ago, and Bambuddy
            # has not committed it yet (#2263): wait for it rather than failing the upload
            # into a forget that would make a second one.
            if not refused(error, PARENT_FOLDER_NOT_FOUND) or not await visible(
                lambda: client.folder(parent_id), FOLDER_NOT_FOUND
            ):
                raise
        return (await client.create_folder(create)).id

    @asynccontextmanager
    async def _locked(self, key: int) -> AsyncIterator[AsyncConnection[DictRow]]:
        """A transaction-scoped advisory lock, so two finders never both create a folder,
        on a connection of its own: the wait is in the event loop, not a thread, and a
        find never holds a pooled connection. A path that leaves without the COMMIT
        (a cancellation) closes the connection, and the rollback releases the lock.
        Yields the connection, for the work done under the lock."""
        conninfo = self._pool.conninfo
        conn = await AsyncConnection.connect(
            conninfo() if callable(conninfo) else conninfo, autocommit=True, row_factory=dict_row
        )
        try:
            await conn.execute("BEGIN")
            await conn.execute(f"SET LOCAL lock_timeout = '{FOLDER_LOCK_TIMEOUT}'")
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            yield conn
            await conn.execute("COMMIT")
        finally:
            await conn.close()

    @staticmethod
    async def _recorded(
        conn: AsyncConnection[DictRow], inbox: Inbox, slug: str, role: str
    ) -> int | None:
        cursor = await conn.execute(
            "SELECT folder_id FROM store_folders"
            " WHERE instance = %s AND inbox_id = %s AND slug = %s AND role = %s",
            (inbox.instance, inbox.id, slug, role),
        )
        row: dict[str, Any] | None = await cursor.fetchone()
        return int(row["folder_id"]) if row is not None else None

    @staticmethod
    async def _record(
        conn: AsyncConnection[DictRow], inbox: Inbox, slug: str, role: str, folder_id: int
    ) -> None:
        await conn.execute(
            "INSERT INTO store_folders (instance, inbox_id, slug, role, folder_id)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT DO NOTHING",
            (inbox.instance, inbox.id, slug, role, folder_id),
        )

    async def forget(self, inbox: Inbox, slug: str) -> None:
        await asyncio.to_thread(self._forget, inbox, slug)

    def _forget(self, inbox: Inbox, slug: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM store_folders WHERE instance = %s AND inbox_id = %s AND slug = %s",
                (inbox.instance, inbox.id, slug),
            )

    async def work_folders(self, inbox: Inbox) -> set[int]:
        """The `Work/` folders recorded under the configured inbox on the configured
        Bambuddy. One recorded under an inbox Settings no longer names, or on another
        instance (whose ids mean other folders here, #683), is not ScadBuddy's to
        delete from."""
        return await asyncio.to_thread(self._work_folders, inbox)

    def _work_folders(self, inbox: Inbox) -> set[int]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT folder_id FROM store_folders"
                " WHERE role = 'work' AND instance = %s AND inbox_id = %s",
                (inbox.instance, inbox.id),
            ).fetchall()
        return {int(row["folder_id"]) for row in rows}
