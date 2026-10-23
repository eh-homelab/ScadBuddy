"""Filing a saved output into the native library (plan 2026-10-17-native-print-library,
Task 8, Ruling 8): its ``model.3mf`` goes under a top-level folder named for the
template, tagged with the output so a delete can find the copies nobody moved."""

from __future__ import annotations

import asyncio
from pathlib import Path

from psycopg.errors import ForeignKeyViolation

from scadbuddy.library.outputs import OutputMeta, download_filename
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.store import FolderGoneError, NativeFile
from scadbuddy.store.bambuddy import folder_name


async def file_output(
    native: NativeLibrary, meta: OutputMeta, model_3mf: Path, title: str
) -> NativeFile:
    """The output's filed copy. Idempotent per output (I5): an activity retry finds the
    row the first attempt made and uploads nothing. The upload holds no lock and no row
    connection; the check, the folder and the insert are one short transaction
    (`NativeLibraryStore.file_for_output`), and an attempt that overlapped one that filed
    first releases its own upload. Only a filed copy counts: one put in a folder of the
    user's choosing (`put_in_folder`, #4507) or moved (``filed`` cleared) is the user's,
    so it never stands in for the template folder's."""
    existing = await asyncio.to_thread(native.rows.filed_rows_for_output, meta.id)
    if existing:
        return existing[0]
    with model_3mf.open("rb") as data:
        row = await native.stage(download_filename(meta), data, None, output_id=meta.id, filed=True)
    try:
        filed, inserted = await asyncio.to_thread(
            native.rows.file_for_output, row, folder_name(title)
        )
    except BaseException:
        await native.content.delete(row.blob_key)
        raise
    if not inserted:
        await native.content.delete(row.blob_key)
    return filed


async def put_in_folder(
    native: NativeLibrary, meta: OutputMeta, model_3mf: Path, folder_id: int, stem: str
) -> tuple[NativeFile, bool]:
    """The output's copy in ``folder_id``, a folder chosen at Generate (#4507), and whether
    this call put it there. Not ``filed``: as a file in a Bambuddy folder does, it outlives
    the output. The bytes are staged with no lock held; the check and the insert are one
    transaction under the output's lock (`NativeLibraryStore.put_for_output`), and a call
    that finds a copy already there releases its own upload. A folder deleted meanwhile is
    the 404."""
    # Lock-free, so it only saves an upload in the common case; `put_for_output`'s locked
    # check is the one that counts.
    there = [
        f
        for f in await asyncio.to_thread(native.rows.rows_for_output, meta.id)
        if f.folder_id == folder_id and f.deleted_at is None
    ]
    if there:
        return there[0], False
    with model_3mf.open("rb") as data:
        row = await native.stage(f"{stem}.3mf", data, folder_id, output_id=meta.id)
    try:
        placed, inserted = await asyncio.to_thread(native.rows.put_for_output, row, folder_id, stem)
    except (FolderGoneError, ForeignKeyViolation):
        await native.content.delete(row.blob_key)
        await native.folder(folder_id)  # the 404 naming it
        raise
    except BaseException:
        await native.content.delete(row.blob_key)
        raise
    if not inserted:
        await native.content.delete(row.blob_key)
    return placed, inserted


async def trash_filed(native: NativeLibrary, output_id: str) -> list[int]:
    """Trash the output's copies still ``filed`` (a move clears it); their ids."""
    filed = await asyncio.to_thread(native.rows.filed_for_output, output_id)
    trashed = await native.trash([f.id for f in filed])
    return [f.id for f in trashed]
