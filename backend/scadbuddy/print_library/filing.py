"""Filing a saved output into the native library (plan 2026-10-17-native-print-library,
Task 8, Ruling 8): its ``model.3mf`` goes under a top-level folder named for the
template, tagged with the output so a delete can find the copies nobody moved."""

from __future__ import annotations

import asyncio
from pathlib import Path

from scadbuddy.library.outputs import OutputMeta, download_filename
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.store import NativeFile
from scadbuddy.store.bambuddy import folder_name


async def file_output(
    native: NativeLibrary, meta: OutputMeta, model_3mf: Path, title: str
) -> NativeFile:
    """The output's filed copy. Idempotent per output (I5): an activity retry finds the
    row the first attempt made and uploads nothing. The upload holds no lock and no row
    connection; the check, the folder and the insert are one short transaction
    (`NativeLibraryStore.file_for_output`), and an attempt that overlapped one that filed
    first releases its own upload."""
    existing = await asyncio.to_thread(native.rows.filed_for_output, meta.id)
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


async def trash_filed(native: NativeLibrary, output_id: str) -> list[int]:
    """Trash the output's copies still ``filed`` (a move clears it); their ids."""
    filed = await asyncio.to_thread(native.rows.filed_for_output, output_id)
    trashed = await native.trash([f.id for f in filed])
    return [f.id for f in trashed]
