"""ScadBuddy's own print library: `library_files` rows over the content store (plan
2026-10-17-native-print-library, Task 3). It answers Bambuddy's models field for field,
so the readers written against `BambuddyClient` (`library_listing.py`, Arrange's cache,
the download cache, all keyed on `file_hash`) work on it unchanged."""

from __future__ import annotations

import asyncio
import mimetypes
import tempfile
import zipfile
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree

from psycopg.errors import ForeignKeyViolation

from scadbuddy.bambuddy.models import (
    FilamentRequirements,
    Folder,
    LibraryFile,
    LibraryListRow,
    LibraryPlate,
    LibraryPlates,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.store import (
    FolderCycleError,
    NativeFile,
    NativeFolder,
    NativeLibraryStore,
    Unset,
    UnsetType,
    file_type_of,
)
from scadbuddy.render.bambu3mf import MODEL_SETTINGS_NAME, plates_of
from scadbuddy.store.content import BlobScope, ContentStore

#: `thumbnail_path` of a file that has a cover: `library_listing._entry` reads only
#: whether it is set (Bambuddy's is a path on its own disk, never fetched).
THUMBNAIL_MARKER = "native"

_MEDIA_TYPES = {"3mf": "model/3mf", "gcode.3mf": "model/3mf", "stl": "model/stl"}


def blob_key(file_id: int) -> str:
    return f"library-file-{file_id}"


def _naive_utc(at: datetime) -> datetime:
    """Bambuddy's times are naive UTC (`LibraryListRow.created_at`); so are these."""
    return at.astimezone(UTC).replace(tzinfo=None)


def _media_type(name: str, file_type: str) -> str:
    return (
        _MEDIA_TYPES.get(file_type) or mimetypes.guess_type(name)[0] or "application/octet-stream"
    )


#: An uploaded file is untrusted: an entry declaring more than its cap is not read.
#: `zipfile` stops an entry at its declared size, so a header that understates one is a
#: CRC failure (`BadZipFile`), never a bomb (`render/bambu3mf.py` `nozzles_statable`).
#: Bambu Studio's `model_settings.config` is kilobytes; covers are PNGs of a few hundred.
MAX_PLATE_SETTINGS_BYTES = 16 * 1024 * 1024
MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024


#: A 3MF's own cover, beside or instead of its plates' (`thumbnail(plate=None)`).
COVER_NAME = "Metadata/thumbnail.png"


def _read_upload(data: BinaryIO) -> tuple[list[LibraryPlate], bool]:
    """Ruling 9: the plates and whether there is a cover, read once, at upload. A file
    that will not read has neither; one with no plate metadata may still have a cover."""
    try:
        with zipfile.ZipFile(data) as archive:
            names = set(archive.namelist())
            settings = (
                archive.getinfo(MODEL_SETTINGS_NAME) if MODEL_SETTINGS_NAME in names else None
            )
            cover = (
                COVER_NAME in names and archive.getinfo(COVER_NAME).file_size <= MAX_THUMBNAIL_BYTES
            )
        if settings is None or settings.file_size > MAX_PLATE_SETTINGS_BYTES:
            return [], cover
        data.seek(0)
        entries = plates_of(data)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, ValueError, OSError):
        return [], False
    finally:
        data.seek(0)
    plates = [
        LibraryPlate(index=p.index, name=p.name, has_thumbnail=p.thumbnail is not None)
        for p in entries
    ]
    return plates, cover or any(p.has_thumbnail for p in plates)


def _not_found(file_id: int) -> ApiError:
    return ApiError(404, f"library file {file_id} not found", file_id=file_id)


def _folder_not_found(folder_id: int | None) -> ApiError:
    """Bambuddy answers a missing folder, target or parent, with 404 too."""
    return ApiError(404, f"library folder {folder_id} not found", folder_id=folder_id)


#: `FolderCycleError`'s answer.
FOLDER_CYCLE = "a folder cannot move into itself or a folder under it"


class NativeLibrary:
    def __init__(self, rows: NativeLibraryStore, content: ContentStore, spool: Path) -> None:
        self.rows = rows
        self.content = content
        #: Where a thumbnail read makes its private copy of a file.
        self.spool = spool

    async def _file(self, file_id: int) -> NativeFile:
        if not is_native_id(file_id):
            raise _not_found(file_id)
        found = await asyncio.to_thread(self.rows.file, file_id)
        if found is None:
            raise _not_found(file_id)
        return found

    async def _folder_must_exist(self, folder_id: int | None) -> None:
        if folder_id is None:
            return
        if (
            not is_native_id(folder_id)
            or await asyncio.to_thread(self.rows.folder, folder_id) is None
        ):
            raise _folder_not_found(folder_id)

    # reads (`LibraryListingSource`, `LibraryReads`)

    async def folders(self) -> list[Folder]:
        folders = await asyncio.to_thread(self.rows.folders)
        files = await asyncio.to_thread(self.rows.files, None, every_folder=True)
        counts: dict[int, int] = {}
        latest: dict[int, datetime] = {}
        for f in files:
            if f.folder_id is None:
                continue
            counts[f.folder_id] = counts.get(f.folder_id, 0) + 1
            if f.folder_id not in latest or f.updated_at > latest[f.folder_id]:
                latest[f.folder_id] = f.updated_at
        children: dict[int | None, list[NativeFolder]] = {}
        for folder in folders:
            children.setdefault(folder.parent_id, []).append(folder)

        def shaped(folder: NativeFolder) -> Folder:
            changed = latest.get(folder.id)
            return Folder(
                id=folder.id,
                name=folder.name,
                parent_id=folder.parent_id,
                file_count=counts.get(folder.id, 0),
                project_id=None,
                latest_activity_at=_naive_utc(changed) if changed else None,
                children=[shaped(c) for c in children.get(folder.id, [])],
            )

        return [shaped(f) for f in children.get(None, [])]

    async def library_listing(
        self, *, folder_id: int | None, every_folder: bool = False
    ) -> list[LibraryListRow]:
        files = await asyncio.to_thread(self.rows.files, folder_id, every_folder=every_folder)
        return [
            LibraryListRow(
                id=f.id,
                filename=f.name,
                file_type=f.file_type,
                folder_id=f.folder_id,
                file_size=f.size,
                thumbnail_path=THUMBNAIL_MARKER if f.has_thumbnail else None,
                print_count=0,
                created_at=_naive_utc(f.created_at),
                is_external=False,
            )
            for f in files
        ]

    async def library_file(self, file_id: int) -> LibraryFile:
        f = await self._file(file_id)
        return LibraryFile(
            id=f.id,
            filename=f.name,
            file_type=f.file_type,
            file_size=f.size,
            thumbnail_path=THUMBNAIL_MARKER if f.has_thumbnail else None,
            folder_id=f.folder_id,
            file_hash=f.sha256,
            is_external=False,
        )

    async def library_plates(self, file_id: int) -> LibraryPlates:
        f = await self._file(file_id)
        return LibraryPlates(file_id=f.id, plates=f.plates, is_multi_plate=len(f.plates) > 1)

    async def download_library_file(self, file_id: int) -> AsyncGenerator[bytes]:
        f = await self._file(file_id)
        stat = await self.content.stat(f.blob_key)
        if stat is None:
            raise _not_found(file_id)
        async with aclosing(self.content.get(stat.ref)) as chunks:
            async for chunk in chunks:
                yield chunk

    async def filament_requirements(
        self, file_id: int, *, plate_id: int | None = None
    ) -> FilamentRequirements:
        """Empty: the slots are read from the file itself (Ruling 6)."""
        f = await self._file(file_id)
        return FilamentRequirements(file_id=f.id, filename=f.name, plate_id=plate_id)

    async def thumbnail(self, file_id: int, plate: int | None) -> bytes | None:
        """``Metadata/plate_<plate>.png``; with no plate, ``Metadata/thumbnail.png``,
        else plate 1's."""
        f = await self._file(file_id)
        if not f.has_thumbnail:
            return None
        names = (
            [f"Metadata/plate_{plate}.png"]
            if plate is not None
            else [COVER_NAME, "Metadata/plate_1.png"]
        )
        stat = await self.content.stat(f.blob_key)
        if stat is None:
            return None
        # The store's spool is made on its first use (`DownloadCache.private_copy`).
        await asyncio.to_thread(self.spool.mkdir, parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=self.spool) as copy:
            async with aclosing(self.content.get(stat.ref)) as chunks:
                async for chunk in chunks:
                    await asyncio.to_thread(copy.write, chunk)
            return await asyncio.to_thread(_member, copy, names)

    async def names(self, ids: list[int]) -> list[tuple[int, NativeFile | None]]:
        native = [i for i in ids if is_native_id(i)]
        found = {f.id: f for f in await asyncio.to_thread(self.rows.files_by_ids, native)}
        return [(i, found.get(i)) for i in ids]

    # writes

    async def upload(
        self,
        filename: str,
        data: BinaryIO,
        folder_id: int | None,
        *,
        output_id: str | None = None,
        filed: bool = False,
    ) -> NativeFile:
        await self._folder_must_exist(folder_id)
        row = await self.stage(filename, data, folder_id, output_id=output_id, filed=filed)
        return await self._insert(row, folder_id)

    async def stage(
        self,
        filename: str,
        data: BinaryIO,
        folder_id: int | None,
        *,
        output_id: str | None = None,
        filed: bool = False,
    ) -> NativeFile:
        """The bytes stored and the row that names them, not yet inserted. The caller
        inserts the row or deletes ``blob_key``: the generic sweep skips the `library` kind;
        `sweep_library` collects only the orphans a crash leaves."""
        file_id = await asyncio.to_thread(self.rows.next_file_id)
        file_type = file_type_of(filename)
        plates, has_thumbnail = (
            await asyncio.to_thread(_read_upload, data)
            if file_type in ("3mf", "gcode.3mf")
            else ([], False)
        )
        key = blob_key(file_id)
        ref = await self.content.put(
            "library", data, name=filename, scope=BlobScope(), key=key, meta={"file_id": file_id}
        )
        now = datetime.now(UTC)
        return NativeFile(
            id=file_id,
            folder_id=folder_id,
            name=filename,
            size=ref.size,
            sha256=ref.sha256,
            blob_key=key,
            media_type=_media_type(filename, file_type),
            plates=plates,
            has_thumbnail=has_thumbnail,
            output_id=output_id,
            filed=filed,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )

    async def _insert(self, row: NativeFile, folder_id: int | None) -> NativeFile:
        """The row for a blob just stored. Any failure releases the blob first: the generic
        sweep skips the `library` kind, and `sweep_library` (print_library/sweep.py) collects
        the orphans a crash leaves, only later. The folder deleted since its check is the
        FK's refusal, the 404."""
        try:
            return await asyncio.to_thread(self.rows.insert_file, row)
        except ForeignKeyViolation:
            await self.content.delete(row.blob_key)
            raise _folder_not_found(folder_id) from None
        except BaseException:
            await self.content.delete(row.blob_key)
            raise

    async def copy(self, file_id: int, folder_id: int | None) -> NativeFile:
        """A new file, the same bytes: the source's object indexed under the copy's key
        (`ContentStore.link`). `ReuseLostError` (the source freed mid-copy) propagates."""
        source = await self._file(file_id)
        await self._folder_must_exist(folder_id)
        stat = await self.content.stat(source.blob_key)
        if stat is None:
            raise _not_found(file_id)
        new_id = await asyncio.to_thread(self.rows.next_file_id)
        key = blob_key(new_id)
        await self.content.link(key, stat.ref, scope=BlobScope(), meta={"file_id": new_id})
        now = datetime.now(UTC)
        row = source.model_copy(
            update={
                "id": new_id,
                "folder_id": folder_id,
                "blob_key": key,
                "filed": False,
                "created_at": now,
                "updated_at": now,
                "deleted_at": None,
            }
        )
        return await self._insert(row, folder_id)

    async def update_file(
        self,
        file_id: int,
        *,
        name: str | None = None,
        folder_id: int | UnsetType | None = Unset,
        filed: bool | None = None,
    ) -> NativeFile:
        """A rename, a move (``None`` is the top level), or both; a missing file or
        target folder is the 404, ``file_id`` or ``folder_id`` in its extensions."""
        await self._file(file_id)
        if not isinstance(folder_id, UnsetType):
            await self._folder_must_exist(folder_id)
        try:
            row = await asyncio.to_thread(
                self.rows.update_file, file_id, name=name, folder_id=folder_id, filed=filed
            )
        except ForeignKeyViolation:
            raise _folder_not_found(
                None if isinstance(folder_id, UnsetType) else folder_id
            ) from None
        if row is None:
            raise _not_found(file_id)
        return row

    async def trash(self, ids: list[int]) -> list[NativeFile]:
        """The files moved to the trash; one already there or gone is left out."""
        native = [i for i in ids if is_native_id(i)]
        return await asyncio.to_thread(self.rows.trash, native) if native else []

    async def restore(self, ids: list[int]) -> list[int]:
        native = [i for i in ids if is_native_id(i)]
        return await asyncio.to_thread(self.rows.restore, native) if native else []

    async def folder(self, folder_id: int) -> NativeFolder:
        """The folder, or the 404 naming it."""
        found = (
            await asyncio.to_thread(self.rows.folder, folder_id)
            if is_native_id(folder_id)
            else None
        )
        if found is None:
            raise _folder_not_found(folder_id)
        return found

    async def template_folder(self, name: str) -> NativeFolder:
        """The top-level folder ``name`` the filing uses (`filing.file_output`), found or
        made once however many callers race (#3818 tier A Task 5)."""
        return await asyncio.to_thread(self.rows.template_folder, name)

    async def create_folder(self, name: str, parent_id: int | None) -> NativeFolder:
        await self._folder_must_exist(parent_id)
        try:
            return await asyncio.to_thread(self.rows.create_folder, name, parent_id)
        except ForeignKeyViolation:
            raise _folder_not_found(parent_id) from None

    async def update_folder(
        self, folder_id: int, *, name: str | None, parent_id: int | UnsetType | None = Unset
    ) -> NativeFolder:
        """A missing folder or parent is the 404; a move under itself the 422."""
        await self._folder_must_exist(folder_id)
        if not isinstance(parent_id, UnsetType):
            await self._folder_must_exist(parent_id)
        try:
            row = await asyncio.to_thread(
                self.rows.update_folder, folder_id, name=name, parent_id=parent_id
            )
        except FolderCycleError:
            raise ApiError(422, FOLDER_CYCLE, folder_id=folder_id) from None
        except ForeignKeyViolation:
            raise _folder_not_found(
                None if isinstance(parent_id, UnsetType) else parent_id
            ) from None
        if row is None:
            raise _folder_not_found(folder_id)
        return row

    async def delete_folder(self, folder_id: int) -> tuple[list[NativeFile], list[int]]:
        """Every file in the folder's subtree trashed, then the folders deleted, deepest
        first (`NativeLibraryStore.delete_folder_tree`)."""
        if not is_native_id(folder_id):
            raise _folder_not_found(folder_id)
        done = await asyncio.to_thread(self.rows.delete_folder_tree, folder_id)
        if done is None:
            raise _folder_not_found(folder_id)
        return done

    async def purge(self, file_id: int, cutoff: datetime) -> bool:
        """Delete a trashed file older than ``cutoff`` and release its blob; False when a
        restore got there first. The object stays while another key names it."""
        row = await asyncio.to_thread(self.rows.purge_trashed, file_id, cutoff)
        if row is None:
            return False
        await self.content.delete(row.blob_key)
        return True


def _member(archive: BinaryIO, names: list[str]) -> bytes | None:
    archive.seek(0)
    try:
        with zipfile.ZipFile(archive) as z:
            present = set(z.namelist())
            for name in names:
                if name in present:
                    if z.getinfo(name).file_size > MAX_THUMBNAIL_BYTES:
                        return None
                    return z.read(name)
    except zipfile.BadZipFile:
        return None
    return None
