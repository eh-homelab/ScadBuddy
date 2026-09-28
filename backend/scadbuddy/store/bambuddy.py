"""The Bambuddy backend of the blob store (spec 2026-09-27 §6.3).

Layout (#316, #317): the Settings `library_folder_id` folder is ScadBuddy's inbox and
the only place it deletes from. Under it, one folder per template named by its title,
and under that `Work/`, where pieces, snapshots and assets live; fonts, which belong to
no template, live in `Shared/Work/`. A project's folder is the user's record: an output
may be written there (`folder="output"` with a `project_id`), nothing there is moved or
deleted. No dot-named folders: Bambuddy shows them.

Every folder ScadBuddy makes or adopts is recorded in `store_folders`, and a delete is
refused unless the file sits in one recorded as `work`. What each file is lives in
`store_blobs`, so a fetch is by file id, never a folder scan.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.errors import not_configured
from scadbuddy.bambuddy.models import FolderCreate
from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.library.settings_store import RenderStoreSettings, load_render_store_settings
from scadbuddy.store.content_models import BlobKind, BlobMissingError, BlobScope
from scadbuddy.store.content_models import RefusedDeleteError as RefusedDeleteError
from scadbuddy.store.index import Pool

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

    async def current(self) -> RenderStoreSettings:
        now = self._clock()
        if self._cached is None or now - self._at >= self.ttl:
            self._cached = await asyncio.to_thread(
                load_render_store_settings, self._pool, self._defaults
            )
            self._at = now
        return self._cached

    def invalidate(self) -> None:
        """Re-read on the next call: the API calls this after its own settings write."""
        self._cached = None

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


class BambuddyContentBackend:
    backend = "bambuddy"

    def __init__(
        self,
        target: Callable[[], Awaitable[BambuddyTarget]],
        pool: Pool,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._target = target
        self._pool = pool
        self._http = http or httpx.AsyncClient()
        self._owns_http = http is None
        self._folders: dict[tuple[int, str, str], int] = {}
        #: One per folder being found or made, so this process's other callers for it
        #: wait in the event loop rather than each holding a thread and a connection.
        self._finding: dict[tuple[int, str, str], asyncio.Lock] = {}

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[tuple[BambuddyClient, int]]:
        target = await self._target()
        async with BambuddyClient(target.config, http=self._http) as client:
            yield client, target.inbox_id

    # --- ContentBackend ------------------------------------------------------

    async def upload(self, kind: BlobKind, data: bytes, *, name: str, scope: BlobScope) -> str:
        media = _MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")
        async with self._client() as (client, inbox):
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

    async def download(self, backend_id: str) -> AsyncIterator[bytes]:
        async with self._client() as (client, _):
            try:
                async for chunk in client.download_library_file(int(backend_id)):
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
        async with self._client() as (client, _):
            try:
                file = await client.library_file(int(backend_id))
            except ApiError as error:
                if error.status == 404:
                    return
                raise
            work = await asyncio.to_thread(self._work_folders)
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

    async def _folder_for(self, client: BambuddyClient, inbox: int, scope: BlobScope) -> int:
        if scope.folder == "output" and scope.project_id is not None:
            folders = await client.folders_by_project(scope.project_id)
            if not folders:
                raise not_configured(
                    f"project {scope.project_id} has no library folder in Bambuddy"
                )
            return folders[0].id
        slug = scope.slug or ""
        title = folder_name(scope.title or scope.slug or SHARED_TITLE)
        template = await self._ensure(client, inbox, slug, "template", title, inbox)
        if scope.folder == "output":
            return template
        return await self._ensure(client, inbox, slug, "work", WORK, template)

    async def _ensure(
        self, client: BambuddyClient, inbox: int, slug: str, role: str, name: str, parent_id: int
    ) -> int:
        cache_key = (inbox, slug, role)
        found = self._folders.get(cache_key)
        if found is not None:
            return found
        async with self._finding.setdefault(cache_key, asyncio.Lock()):
            found = self._folders.get(cache_key)
            if found is not None:
                return found
            # Across processes: one connection, held for the lock and used for the
            # lookup and the record, so a find never needs a second one from the pool.
            async with self._locked("store-folder", inbox, slug, role) as conn:
                found = await asyncio.to_thread(self._recorded, conn, inbox, slug, role)
                if found is None:
                    found = await self._adopt_or_create(client, name, parent_id)
                    await asyncio.to_thread(self._record, conn, inbox, slug, role, found)
            self._folders[cache_key] = found
            return found

    async def _adopt_or_create(self, client: BambuddyClient, name: str, parent_id: int) -> int:
        for root in await client.folders():
            for folder in root.walk():
                if folder.parent_id == parent_id and folder.name == name:
                    return folder.id
        return (await client.create_folder(FolderCreate(name=name, parent_id=parent_id))).id

    @asynccontextmanager
    async def _locked(self, *parts: object) -> AsyncIterator[Connection[DictRow]]:
        """A Postgres advisory lock, so two workers never both create a folder. Yields
        the connection that holds it, for the work done under it."""
        key = int.from_bytes(hashlib.sha256(repr(parts).encode()).digest()[:8], "big", signed=True)
        conn = await asyncio.to_thread(self._pool.getconn)
        try:
            await asyncio.to_thread(conn.execute, "SELECT pg_advisory_lock(%s)", (key,))
            try:
                yield conn
            finally:
                await asyncio.to_thread(conn.execute, "SELECT pg_advisory_unlock(%s)", (key,))
        finally:
            await asyncio.to_thread(self._pool.putconn, conn)

    @staticmethod
    def _recorded(conn: Connection[DictRow], inbox: int, slug: str, role: str) -> int | None:
        row: dict[str, Any] | None = conn.execute(
            "SELECT folder_id FROM store_folders WHERE inbox_id = %s AND slug = %s AND role = %s",
            (inbox, slug, role),
        ).fetchone()
        return int(row["folder_id"]) if row is not None else None

    @staticmethod
    def _record(
        conn: Connection[DictRow], inbox: int, slug: str, role: str, folder_id: int
    ) -> None:
        conn.execute(
            "INSERT INTO store_folders (inbox_id, slug, role, folder_id)"
            " VALUES (%s, %s, %s, %s)"
            " ON CONFLICT DO NOTHING",
            (inbox, slug, role, folder_id),
        )

    def _forget(self, inbox: int, slug: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM store_folders WHERE inbox_id = %s AND slug = %s", (inbox, slug)
            )
        for role in ("template", "work"):
            self._folders.pop((inbox, slug, role), None)

    def _work_folders(self) -> set[int]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT folder_id FROM store_folders WHERE role = 'work'"
            ).fetchall()
        return {int(row["folder_id"]) for row in rows}
