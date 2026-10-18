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
    row the first attempt made and uploads nothing."""
    existing = await asyncio.to_thread(native.rows.filed_for_output, meta.id)
    if existing:
        return existing[0]
    folder = await asyncio.to_thread(native.rows.template_folder, folder_name(title))
    with model_3mf.open("rb") as data:
        return await native.upload(
            download_filename(meta), data, folder.id, output_id=meta.id, filed=True
        )


async def trash_filed(native: NativeLibrary, output_id: str) -> list[int]:
    """Trash the output's copies still ``filed`` (a move clears it); their ids."""
    filed = await asyncio.to_thread(native.rows.filed_for_output, output_id)
    trashed = await native.trash([f.id for f in filed])
    return [f.id for f in trashed]
