"""A native run's laid-out per-print copy in ScadBuddy's own library (#3818 tier A Task 4,
plan 2026-10-18-hot-path-without-bambuddy; spec 2026-10-16 §5.4).

What `send.ensure_uploaded` does for Bambuddy, against `NativeCopies`: the 3MF laid out
for the printer (`send._laid_out_for`, unchanged) goes into the native library as a file
of its own (``filed=False``, no ``output_id``: a print's copy, never the output's filed
one, `filing.py`), recorded in `output_bambuddy_uploads` under the run subject. Its id is
native (`ids.NATIVE_ID_BASE` and up), which is how every reader of that table keeps to
one library (`BambuddyUploadStore.for_output`'s ``native``).

Reuse is by (subject, folder, `Target.key`) only: none of Bambuddy's project rules
(Generate's uncoloured file, `send._reusable`) apply to a native folder. A recorded copy
is reused while its row is live and still in the folder: a native id is never handed to
another file (an identity column, LIB Ruling 1) and a file's bytes are never rewritten
in place (`NativeLibraryStore` has no update of ``sha256``), so the row is the check
Bambuddy's ``file_hash`` comparison is there. Nothing here deletes: a superseded copy
stays until the library's own rules remove it (none yet; the copy's lifetime is a
follow-up, plan Task 14).
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging

from fastapi import status

from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.bambuddy.send import Printable, Target, _copy_lock, _hinted, _laid_out_for
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy
from scadbuddy.core.problems import ApiError
from scadbuddy.core.tracing import span
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.reads import LibraryListingSource, LibraryReads, NativeCopies
from scadbuddy.store.bambuddy import folder_name

logger = logging.getLogger(__name__)

#: Prefixes a native copy's lock key, so it never shares one with a Bambuddy folder's.
NATIVE_LOCK = "native:"


async def left_unrecorded(
    native: NativeCopies,
    uploads: BambuddyUploadStore,
    folder_id: int | None,
    filename: str,
    size: int,
    digest: str,
    *,
    besides: int | None = None,
) -> LibraryFile | None:
    """The file an earlier attempt uploaded into ``folder_id`` and died before recording
    (`send._left_unrecorded`'s rule, #1145, on the native library). Only a per-print file
    (`NativeCopies.per_print_files`: no output named, not filed) is a candidate, never the
    output's filed copy or a person's copy of it, which can share the name and the bytes
    (#4312 review M2). Named ``filename``,
    of ``size`` bytes hashing to ``digest`` (a sha256), that no subject records as a
    copy or a slice, and not ``besides`` (the file the upload is made from). Newest first;
    ``None`` when there is none."""
    name = filename.casefold()
    rows = [
        row
        for row in await native.per_print_files(folder_id)
        if row.id != besides
        and is_native_id(row.id)
        and row.file_size == size
        and row.filename.casefold() == name
    ]
    if not rows:
        return None
    taken = await uploads.recorded_files([row.id for row in rows])
    for row in sorted(rows, key=lambda row: row.id, reverse=True):
        if row.id in taken:
            continue
        try:
            found = await native.library_file(row.id)
        except ApiError:
            continue
        if found.file_hash == digest:
            logger.info(
                "an earlier attempt's native upload was never recorded; taking it",
                extra={"library_file_id": found.id},
            )
            return found
    return None


async def _still_there(
    native: LibraryReads, uploads: BambuddyUploadStore, key: str, copy: LibraryCopy
) -> bool:
    """Whether the recorded native ``copy`` is live where it was recorded. A 404 (trashed,
    purged) is forgotten; a copy a person moved is recorded where it is now, and is not
    this folder's any more. Any other failure raises: the native library is ScadBuddy's
    own, and an upload past an unreadable row would only hide the error."""
    try:
        found = await native.library_file(copy.id)
    except ApiError as error:
        if error.status != status.HTTP_404_NOT_FOUND:
            raise
        await uploads.forget(key, copy.id)
        return False
    if found.folder_id != copy.folder_id:
        await uploads.relocate([copy.id], found.folder_id)
        return False
    return True


async def ensure_native_copy(
    native: NativeCopies,
    uploads: BambuddyUploadStore,
    printable: Printable,
    *,
    target: Target,
    folder_id: int | None,
    source_id: int | None = None,
) -> int:
    """The native library file a native run slices and prints: a recorded copy of
    ``printable`` laid out for ``target`` in ``folder_id`` (``None``, the top level),
    else a new one uploaded and recorded. Held under the copy's lock (in this process,
    then across replicas), as `send.ensure_uploaded` is, so two runs of one subject upload
    one copy. ``source_id`` is the native library file the 3MF came from, never taken
    for an earlier attempt's copy."""
    target_key = _hinted(target.key, len(printable.colours))
    lock = (f"{NATIVE_LOCK}{printable.key}", folder_id)
    async with _copy_lock(lock), uploads.copy_lock(f"{lock[0]}:{folder_id}"):
        for copy in await uploads.for_output(printable.key, native=True):
            if copy.folder_id != folder_id or copy.target_key != target_key:
                continue
            if await _still_there(native, uploads, printable.key, copy):
                return copy.id
        with span("upload.fetch", attributes={"scadbuddy.subject": printable.key}):
            fetched = await printable.fetch()
        with span("upload.layout", attributes={"scadbuddy.subject": printable.key}):
            payload = await asyncio.to_thread(_laid_out_for, fetched, target)
        digest = hashlib.sha256(payload).hexdigest()
        earlier = await left_unrecorded(
            native,
            uploads,
            folder_id,
            printable.inbox_name,
            len(payload),
            digest,
            besides=source_id,
        )
        uploaded = (
            earlier.id
            if earlier is not None
            else (await native.upload(printable.inbox_name, io.BytesIO(payload), folder_id)).id
        )
        await uploads.record(
            printable.key, LibraryCopy(id=uploaded, folder_id=folder_id, target_key=target_key)
        )
    return uploaded


#: D6: what a native run answers a request naming a project (422, at ``print_check``).
NATIVE_PROJECT_REFUSAL = (
    "A project is Bambuddy's: a print on the native path is filed in ScadBuddy's library."
    " Pick a folder, or print through Bambuddy."
)


async def require_native_folder(native: LibraryListingSource, folder_id: int) -> None:
    """The 422 for a folder the native library does not have: a Bambuddy-range id, or a
    native one that is gone (D6, as `projects.require_destination_folder` refuses one the
    picker would not offer). Every live native folder is a destination: the native
    library has no projects, inbox or external folders to leave out."""
    known = is_native_id(folder_id) and any(
        f.id == folder_id for top in await native.folders() for f in top.walk()
    )
    if not known:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"folder {folder_id} is not a folder of ScadBuddy's library a print can be"
            " filed in: pick one from GET /api/v1/print/projects?native=true",
        )


async def native_destination(
    native: NativeCopies, *, folder_id: int | None, title: str | None
) -> int | None:
    """Where a native run files its copy (D6): ``folder_id`` when the request names one
    (checked), else the output's ``<Template title>/`` folder the filing uses
    (`filing.file_output`, `folder_name`), found or made under its advisory lock, else
    the top level (``None``) for a library file, which has no template."""
    if folder_id is not None:
        await require_native_folder(native, folder_id)
        return folder_id
    if title is None:
        return None
    return (await native.template_folder(folder_name(title))).id


async def trash_copies(native: NativeLibrary, uploads: BambuddyUploadStore, key: str) -> list[int]:
    """Trash ``key``'s native per-print copies and the slices recorded on them, before an
    output delete forgets their rows (#4312 review I1): they name no output (Ruling 6),
    so those rows are all that leads to them. A file already gone is passed over. Their
    ids."""
    ids = [
        file_id
        for copy in await uploads.for_output(key, native=True)
        for file_id in (copy.id, *(s.id for s in copy.sliced))
    ]
    return [f.id for f in await native.trash(ids)] if ids else []
