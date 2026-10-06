"""The Bambuddy backend of the blob store (spec 2026-09-27 §6.3).

Layout (#316, #317): the Settings `library_folder_id` folder is ScadBuddy's inbox and
the only place it deletes from. Under it, one folder per template named by its title,
and under that `Work/`, where pieces, snapshots and assets live; fonts, which belong to
no template, live in `Shared/Work/`. A project's folder is the user's record: an output
may be written there (`folder="output"` with a `project_id`), nothing there is moved or
deleted. No dot-named folders: Bambuddy shows them.

Every folder ScadBuddy makes or adopts is recorded in `store_folders`, under the
Bambuddy instance (its base URL) and inbox it was found on, and a delete is refused
unless the file sits in one recorded as `work` (or `retired`, #1437) there (#683).
What each file is lives in `store_blobs`, so a fetch is by file id, never a folder
scan.

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
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from psycopg import AsyncConnection, Connection
from psycopg.errors import UniqueViolation
from psycopg.rows import DictRow, dict_row

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.errors import not_configured
from scadbuddy.bambuddy.models import FolderCreate
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import RenderStoreSettings, load_render_store_settings
from scadbuddy.store.content_models import BlobKind, BlobMissingError, BlobScope
from scadbuddy.store.content_models import RefusedDeleteError as RefusedDeleteError
from scadbuddy.store.index import Pool

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
class _LegacyFolder:
    """A `store_folders` row recorded before rows carried their instance (#683)."""

    inbox_id: int
    slug: str
    role: str
    folder_id: int


@dataclass(frozen=True)
class _Inbox:
    """Where this call's folders are: one inbox on one Bambuddy instance."""

    instance: str
    id: int


def folder_name(title: str) -> str:
    """A Bambuddy folder name from a template title: no separators, no leading dots."""
    cleaned = re.sub(r"\s+", " ", re.sub(r"[\\/\x00-\x1f]", " ", title)).strip()
    cleaned = cleaned.lstrip(".").strip()
    return cleaned[:100] or "Template"


class RenderSettingsSource:
    """The render key and the inbox, re-read at most every ``ttl`` seconds, so a key
    rotated in Settings reaches every worker without a restart (spec §9, §10)."""

    def __init__(
        self,
        pool: Pool,
        defaults: Settings,
        *,
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self._defaults = defaults
        self.ttl = ttl
        self._clock = clock
        self._cached: RenderStoreSettings | None = None
        self._at = 0.0
        #: False while a re-read fails and `current` answers the last good settings.
        self.fresh = True

    async def current(self) -> RenderStoreSettings:
        """The settings, re-read once ``ttl`` has passed. A re-read that fails keeps the
        last good ones (and clears `fresh`) rather than failing `/healthz` and the
        renders with the database; only a first read that fails raises."""
        now = self._clock()
        if self._cached is None or now - self._at >= self.ttl:
            try:
                self._cached = await asyncio.to_thread(
                    load_render_store_settings, self._pool, self._defaults
                )
            except Exception:
                if self._cached is None:
                    raise
                logger.warning("could not re-read the store settings; keeping the last good")
                self.fresh = False
            else:
                self.fresh = True
            self._at = now
        return self._cached

    def seed(self, current: RenderStoreSettings) -> None:
        """Start from the settings the process read at start, so a database that is
        down at the first re-read leaves these in place rather than nothing."""
        self._cached, self._at = current, self._clock()

    def invalidate(self) -> None:
        """Re-read on the next call: the API calls this after its own settings write."""
        self._at = float("-inf")

    async def target(self) -> BambuddyTarget:
        current = await self.current()
        if not current.bambuddy_url or current.library_folder_id is None:
            raise not_configured(
                "the Bambuddy store needs a Bambuddy URL and a library folder in Settings"
            )
        return BambuddyTarget(
            config=BambuddyConfig(
                base_url=current.bambuddy_url.rstrip("/"), api_key=current.api_key
            ),
            inbox_id=current.library_folder_id,
        )


#: How long a find waits for another worker's folder lock before it fails.
FOLDER_LOCK_TIMEOUT = "30s"


def folder_lock_key(instance: str, inbox: int, slug: str, role: str) -> int:
    """The advisory-lock key for finding one folder, the same in every process."""
    return _lock_key(("store-folder", instance, inbox, slug, role))


def legacy_folder_lock_key(inbox: int, slug: str, role: str) -> int:
    """The key a release from before #683 locks the same find on. A find takes it as
    well as `folder_lock_key` while such a worker may still be draining (#1437), so the
    two never both create the folder; it goes with `store_folders.instance`'s DEFAULT
    (#1429)."""
    return _lock_key(("store-folder", inbox, slug, role))


def _lock_key(parts: tuple[object, ...]) -> int:
    return int.from_bytes(hashlib.sha256(repr(parts).encode()).digest()[:8], "big", signed=True)


#: How long after a failed settle of pre-#683 rows the next one is tried (#1428).
SETTLE_BACKOFF = 30.0


class LegacyFoldersUnsettledError(RuntimeError):
    """A delete needs the pre-#683 rows settled and the last try failed within
    `SETTLE_BACKOFF`. Retryable, unlike `RefusedDeleteError` (#1437)."""


class BambuddyContentBackend:
    backend = "bambuddy"

    def __init__(
        self,
        target: Callable[[], Awaitable[BambuddyTarget]],
        pool: Pool,
        *,
        http: httpx.AsyncClient | None = None,
        settle_backoff: float = SETTLE_BACKOFF,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._target = target
        self._pool = pool
        self._http = http or httpx.AsyncClient()
        self._owns_http = http is None
        self._folders: dict[tuple[str, int, str, str], int] = {}
        #: One per folder being found or made, so this process's other callers for it
        #: wait in the event loop rather than each holding a thread and a connection.
        self._finding: dict[tuple[str, int, str, str], asyncio.Lock] = {}
        #: The instances whose pre-#683 rows this process has settled (`_claim_legacy`).
        self._claimed: set[str] = set()
        #: One per instance, so concurrent first calls settle once (#1428).
        self._settling: dict[str, asyncio.Lock] = {}
        #: When each instance's last settle failed, for the backoff.
        self._settle_failed: dict[str, float] = {}
        self._settle_backoff = settle_backoff
        self._clock = clock

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @asynccontextmanager
    async def _client(
        self, *, settle: bool = False
    ) -> AsyncIterator[tuple[BambuddyClient, _Inbox]]:
        """A client on the configured Bambuddy. With ``settle`` (the paths that use
        folder records: upload, remove), the pre-#683 rows are settled first, best
        effort: any failure is logged and leaves them for a later call. A download or
        an exists never touches them, so nothing there can fail it (#1432)."""
        target = await self._target()
        inbox = _Inbox(target.instance, target.inbox_id)
        async with BambuddyClient(target.config, http=self._http) as client:
            if settle and inbox.instance not in self._claimed:
                try:
                    await self._settle(client, inbox.instance)
                except LegacyFoldersUnsettledError:
                    pass  # backing off; a delete that needs them retries (`remove`)
                except Exception:
                    logger.exception("could not settle pre-instance folder records")
            yield client, inbox

    async def _settle(self, client: BambuddyClient, instance: str, *, again: bool = False) -> None:
        """`_claim_legacy` once per instance at a time (#1428), not before
        ``settle_backoff`` has passed since one failed: then it raises
        `LegacyFoldersUnsettledError` without a request. ``again`` re-reads an instance
        already settled (an older release may have recorded a row since). A failure
        raises."""
        async with self._settling.setdefault(instance, asyncio.Lock()):
            if instance in self._claimed and not again:
                return
            failed = self._settle_failed.get(instance)
            if failed is not None and self._clock() - failed < self._settle_backoff:
                raise LegacyFoldersUnsettledError(
                    "the pre-instance folder records could not be settled; retry later"
                )
            try:
                await self._claim_legacy(client, instance)
            except Exception:
                self._settle_failed[instance] = self._clock()
                raise
            self._settle_failed.pop(instance, None)

    async def _claim_legacy(self, client: BambuddyClient, instance: str) -> None:
        """Settle the rows recorded before folders were per instance (``''``), before
        this instance's first find or delete. A slug's rows are claimed only as a pair
        that sits where ScadBuddy put it on THIS instance: the template folder directly
        under its inbox, with the recorded `Work` folder directly under it. One id that
        happens to be some folder under the inbox is not enough (that would send uploads
        into a folder the user made). Anything
        else (ids from an instance the URL was repointed away from, #683; a folder moved
        or deleted since) is dropped, never trusted, so the next find adopts or makes the
        folder again. A folder listing that fails raises and leaves the rows for the
        next call."""
        legacy = await asyncio.to_thread(self._legacy_rows)
        if not legacy:
            self._claimed.add(instance)
            return
        folders = {f.id: f for root in await client.folders() for f in root.walk()}
        templates = {
            (row.inbox_id, row.slug): row.folder_id
            for row in legacy
            if row.role == "template"
            and (folder := folders.get(row.folder_id)) is not None
            and folder.parent_id == row.inbox_id
        }
        # The slots whose recorded Work folder sits under their recorded template folder.
        paired = {
            (row.inbox_id, row.slug)
            for row in legacy
            if row.role == "work"
            and (folder := folders.get(row.folder_id)) is not None
            and folder.name == WORK
            and folder.parent_id == templates.get((row.inbox_id, row.slug))
        }

        def placed(row: _LegacyFolder) -> bool:
            return (row.inbox_id, row.slug) in paired

        await asyncio.to_thread(
            self._settle_legacy, instance, [row for row in legacy if placed(row)], legacy
        )
        self._claimed.add(instance)

    def _legacy_rows(self) -> list[_LegacyFolder]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT inbox_id, slug, role, folder_id FROM store_folders WHERE instance = ''"
            ).fetchall()
        return [
            _LegacyFolder(int(r["inbox_id"]), r["slug"], r["role"], int(r["folder_id"]))
            for r in rows
        ]

    def _settle_legacy(
        self, instance: str, claimed: list[_LegacyFolder], seen: list[_LegacyFolder]
    ) -> None:
        """Claim ``claimed`` for ``instance`` (unless it already records that folder or
        that slot), then drop whatever of ``seen`` is still unclaimed. A Work folder
        whose slot the instance already records with another folder is kept as
        `retired` (#1437): it is ScadBuddy's and may hold files, so it stays deletable
        though nothing uploads into it. A row an older release inserts after ``seen``
        was read is left for the next process."""
        with self._pool.connection() as conn, conn.transaction():
            for row in claimed:
                try:
                    # A savepoint each: a find in another process may record the same
                    # slot meanwhile (its row is not visible to NOT EXISTS until it
                    # commits). Then this row is not claimed.
                    with conn.transaction():
                        won = self._claim_row(conn, instance, row)
                except UniqueViolation:
                    won = False
                if not won and row.role == "work":
                    self._retire_row(conn, instance, row)
            for row in seen:
                conn.execute(
                    "DELETE FROM store_folders WHERE instance = '' AND inbox_id = %s"
                    " AND slug = %s AND role = %s AND folder_id = %s",
                    (row.inbox_id, row.slug, row.role, row.folder_id),
                )

    @staticmethod
    def _claim_row(conn: Connection[DictRow], instance: str, row: _LegacyFolder) -> bool:
        """Whether the row is now ``instance``'s."""
        cursor = conn.execute(
            "UPDATE store_folders AS legacy SET instance = %s WHERE legacy.instance = ''"
            " AND inbox_id = %s AND slug = %s AND role = %s AND folder_id = %s"
            " AND NOT EXISTS (SELECT 1 FROM store_folders AS own WHERE own.instance = %s"
            " AND (own.folder_id = legacy.folder_id OR (own.inbox_id, own.slug, own.role)"
            " = (legacy.inbox_id, legacy.slug, legacy.role)))",
            (instance, row.inbox_id, row.slug, row.role, row.folder_id, instance),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _retire_row(conn: Connection[DictRow], instance: str, row: _LegacyFolder) -> None:
        """Record ``row``'s Work folder as `retired` for ``instance``: nothing if the
        instance already records that folder (it is deletable already)."""
        conn.execute(
            "INSERT INTO store_folders (instance, inbox_id, slug, role, folder_id)"
            " VALUES (%s, %s, %s, 'retired', %s) ON CONFLICT DO NOTHING",
            (instance, row.inbox_id, row.slug, row.folder_id),
        )

    # --- ContentBackend ------------------------------------------------------

    async def upload(self, kind: BlobKind, data: bytes, *, name: str, scope: BlobScope) -> str:
        media = _MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")
        async with self._client(settle=True) as (client, inbox):
            for attempt in (1, 2):
                folder_id = await self._folder_for(client, inbox, scope)
                try:
                    uploaded = await client.upload_library_file(
                        name, data, folder_id=folder_id, media_type=media
                    )
                except ApiError as error:
                    # The folder was deleted in Bambuddy's UI: forget it, make it again.
                    if error.status != 404 or attempt == 2:
                        raise
                    await asyncio.to_thread(self._forget, inbox, scope.slug or "")
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
        async with self._client(settle=True) as (client, inbox):
            try:
                file = await client.library_file(int(backend_id))
            except ApiError as error:
                if error.status == 404:
                    return
                raise
            work = await asyncio.to_thread(self._work_folders, inbox)
            if file.folder_id not in work and await asyncio.to_thread(
                self._legacy_work_folder, inbox, file.folder_id
            ):
                # A pre-#683 row names the folder and is not settled yet (a listing
                # failed, or an older release recorded it since). Settle now: a failure
                # (or the backoff after one) raises, which the caller retries, where a
                # refusal would untrack the file for good (#1437).
                await self._settle(client, inbox.instance, again=True)
                work = await asyncio.to_thread(self._work_folders, inbox)
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

    # --- folders -------------------------------------------------------------

    async def _folder_for(self, client: BambuddyClient, inbox: _Inbox, scope: BlobScope) -> int:
        if scope.folder == "output" and scope.project_id is not None:
            folders = await client.folders_by_project(scope.project_id)
            if not folders:
                raise not_configured(
                    f"project {scope.project_id} has no library folder in Bambuddy"
                )
            return folders[0].id
        slug = scope.slug or ""
        title = folder_name(scope.title or scope.slug or SHARED_TITLE)
        template = await self._ensure(client, inbox, slug, "template", title, inbox.id)
        if scope.folder == "output":
            return template
        return await self._ensure(client, inbox, slug, "work", WORK, template)

    async def _ensure(
        self,
        client: BambuddyClient,
        inbox: _Inbox,
        slug: str,
        role: str,
        name: str,
        parent_id: int,
    ) -> int:
        cache_key = (inbox.instance, inbox.id, slug, role)
        found = self._folders.get(cache_key)
        if found is not None:
            return found
        async with self._finding.setdefault(cache_key, asyncio.Lock()):
            found = self._folders.get(cache_key)
            if found is not None:
                return found
            # Across processes: the lock's own connection, used for the lookup and the
            # record too, so a find takes no pooled connection and no thread. The legacy
            # key first, always, so two finds never take the pair in opposite orders.
            keys = (
                legacy_folder_lock_key(inbox.id, slug, role),
                folder_lock_key(inbox.instance, inbox.id, slug, role),
            )
            async with self._locked(*keys) as conn:
                found = await self._recorded(conn, inbox, slug, role)
                if found is None:
                    found = await self._adopt_or_create(client, name, parent_id)
                    await self._record(conn, inbox, slug, role, found)
            self._folders[cache_key] = found
            return found

    async def _adopt_or_create(self, client: BambuddyClient, name: str, parent_id: int) -> int:
        for root in await client.folders():
            for folder in root.walk():
                if folder.parent_id == parent_id and folder.name == name:
                    return folder.id
        return (await client.create_folder(FolderCreate(name=name, parent_id=parent_id))).id

    @asynccontextmanager
    async def _locked(self, *keys: int) -> AsyncIterator[AsyncConnection[DictRow]]:
        """Transaction-scoped advisory locks, so two workers never both create a folder,
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
            for key in keys:
                await conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            yield conn
            await conn.execute("COMMIT")
        finally:
            await conn.close()

    @staticmethod
    async def _recorded(
        conn: AsyncConnection[DictRow], inbox: _Inbox, slug: str, role: str
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
        conn: AsyncConnection[DictRow], inbox: _Inbox, slug: str, role: str, folder_id: int
    ) -> None:
        await conn.execute(
            "INSERT INTO store_folders (instance, inbox_id, slug, role, folder_id)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT DO NOTHING",
            (inbox.instance, inbox.id, slug, role, folder_id),
        )

    def _forget(self, inbox: _Inbox, slug: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM store_folders WHERE instance = %s AND inbox_id = %s AND slug = %s"
                " AND role IN ('template', 'work')",
                (inbox.instance, inbox.id, slug),
            )
        for role in ("template", "work"):
            self._folders.pop((inbox.instance, inbox.id, slug, role), None)

    def _work_folders(self, inbox: _Inbox) -> set[int]:
        """The `Work/` folders recorded under the configured inbox on the configured
        Bambuddy, retired ones included (#1437). One recorded under an inbox Settings no
        longer names, or on another instance (whose ids mean other folders here, #683),
        is not ScadBuddy's to delete from."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT folder_id FROM store_folders"
                " WHERE role IN ('work', 'retired') AND instance = %s AND inbox_id = %s",
                (inbox.instance, inbox.id),
            ).fetchall()
        return {int(row["folder_id"]) for row in rows}

    def _legacy_work_folder(self, inbox: _Inbox, folder_id: int | None) -> bool:
        """Whether a pre-#683 row still records ``folder_id`` as a Work folder of the
        configured inbox."""
        if folder_id is None:
            return False
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM store_folders WHERE instance = '' AND role = 'work'"
                " AND inbox_id = %s AND folder_id = %s",
                (inbox.id, folder_id),
            ).fetchone()
        return row is not None
