"""A MakerWorld import into the library ScadBuddy serves (#6119).

Bambuddy downloads the 3MF into its own library (``POST /makerworld/import``). On
``library_backend`` native the file is then copied into ScadBuddy's own library as a
``bb_to_sb`` copy files it (`print_library/copy.py`): skipped when a live native file holds
the same bytes. So the answer names the library file the order and print tools take on
either backend, and a repeat downloads and stores nothing twice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import status

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.core.problems import ApiError
from scadbuddy.makerworld.models import MakerWorldImported, MakerWorldImportRequest
from scadbuddy.print_library.copy import STORE_FULL, copy_file_to_native
from scadbuddy.print_library.copy_store import TargetLocks
from scadbuddy.print_library.native import NativeLibrary

#: Where an import goes in ScadBuddy's own library without a `folder_id`: the name
#: Bambuddy gives its own default folder (``ModelProvider.default_folder_name``).
DEFAULT_FOLDER = "MakerWorld"

FOLDER_ON_BAMBUDDY = (
    "`folder_id` names a folder of ScadBuddy's own library, and library_backend is bambuddy:"
    " give `bambuddy_folder_id` (a Bambuddy library folder, such as a print project's"
    " `folder_id`) instead"
)


async def import_model(
    request: MakerWorldImportRequest,
    *,
    client: BambuddyClient,
    native: NativeLibrary | None,
    locks: Callable[[], TargetLocks],
) -> MakerWorldImported:
    imported = await client.makerworld_import(
        request.model_id, profile_id=request.profile_id, folder_id=request.bambuddy_folder_id
    )
    if native is None:
        return MakerWorldImported(
            library="bambuddy",
            library_file_id=imported.library_file_id,
            folder_id=imported.folder_id,
            filename=imported.filename,
            profile_id=imported.profile_id,
            bambuddy_library_file_id=imported.library_file_id,
            bambuddy_folder_id=imported.folder_id,
            was_existing=imported.was_existing,
            bambuddy_was_existing=imported.was_existing,
            copied=False,
        )
    held = locks()
    try:
        result, file_id, _, reason, _ = await copy_file_to_native(
            client,
            native,
            imported.library_file_id,
            root_id=request.folder_id,
            path="" if request.folder_id is not None else DEFAULT_FOLDER,
            locks=held,
        )
    finally:
        await asyncio.to_thread(held.release_all)
        await asyncio.to_thread(held.close)
    if result == "failed" or file_id is None:
        raise _copy_failed(imported.library_file_id, reason)
    row = await native.library_file(file_id)
    return MakerWorldImported(
        library="native",
        library_file_id=row.id,
        folder_id=row.folder_id,
        filename=row.filename,
        profile_id=imported.profile_id,
        bambuddy_library_file_id=imported.library_file_id,
        bambuddy_folder_id=imported.folder_id,
        was_existing=imported.was_existing and result == "skipped_same_hash",
        bambuddy_was_existing=imported.was_existing,
        copied=result == "copied",
    )


def _copy_failed(bambuddy_id: int, reason: str | None) -> ApiError:
    detail = (
        f"Bambuddy imported the model as its library file {bambuddy_id}, but copying it"
        f" into ScadBuddy's library failed: {reason or 'no reason given'}. Importing again"
        " downloads nothing twice"
    )
    if reason is not None and reason.startswith(STORE_FULL):
        return ApiError(status.HTTP_507_INSUFFICIENT_STORAGE, detail)
    # Not `UNAVAILABLE_PROBLEM`: a copy that ended "failed" (the file gone from Bambuddy,
    # bytes that do not match its hash) fails the same way again, so the run does not
    # try it again (`operations._try_again`). A transient Bambuddy failure raises its
    # own error from the copy instead.
    return ApiError(status.HTTP_502_BAD_GATEWAY, detail)
