"""The native library's housekeeping sweeps.

`sweep_library` (plan 2026-10-17-native-print-library, Task 6): trash past its retention
is purged, and a `library` blob no row names is removed. The generic blob sweep never
touches the `library` kind, so this is the only collector of an orphan.

`sweep_print_copies` (#4337, #3818 tier A Task 14): the lifetime of what a native run
puts in the library, its laid-out per-print copies and their slices (``print_subject``).
They are ScadBuddy's own working files, as Bambuddy's inbox copies are on the Bambuddy
path, and they follow the inbox's rules:

* A folder holds the copy a subject used last (case 3). Bambuddy's inbox holds one copy per
  output: a new upload supersedes the output's others there, whatever they were laid out
  for (`send.upload_copy`). A native folder has no projects (plan 2026-10-18 D6), so the
  project folder's rule (a copy printed from is that project's record, #316) has nothing
  to keep. An older copy goes with its slices.
* A copy or slice nothing records goes (cases 1 and 2): its output was deleted without
  its copies (the History page lists only Bambuddy's copies, so a native output's delete
  never asks), or with its model, whose delete touches no file. Bambuddy keeps such a
  copy as an ordinary file in its inbox, which a person can see and clear there; a native
  copy has nothing else that ever would. `COPY_ORPHAN_GRACE` covers a run between its
  upload and its record.
* A copy a person moved, restored or copied is theirs (case 4): it carries no subject, so
  nothing here trashes it. A recorded copy found unmarked but live, or out of its folder,
  is released with its slices (`copies.release_moved`): a move the routes did not release,
  or one whose release never ran. A person's own file of the same name and bytes never
  carries one either (case 5).
* A subject a run still uses (`PrintRunStore.subjects_in_use`) is left alone.

"Goes" is the trash: the row, and with it the blob (`live_file_keys`), stays for
`LIBRARY_TRASH_TTL`, restorable, until `sweep_library` purges it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from pydantic import BaseModel

from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy
from scadbuddy.print_library.copies import release_moved
from scadbuddy.print_library.native import NativeLibrary

logger = logging.getLogger(__name__)

#: How long a trashed file stays restorable.
LIBRARY_TRASH_TTL = timedelta(days=30)
#: How long a blob no row names is left alone: an upload stores the blob before it
#: inserts the row.
ORPHAN_GRACE = timedelta(hours=1)
LIBRARY_KIND = "library"


class LibrarySweep(BaseModel):
    purged: int
    orphans: int


async def sweep_library(
    native: NativeLibrary,
    *,
    now: datetime,
    trash_ttl: timedelta = LIBRARY_TRASH_TTL,
    grace: timedelta = ORPHAN_GRACE,
) -> LibrarySweep:
    cutoff = now - trash_ttl
    purged = 0
    for row in await asyncio.to_thread(native.rows.trashed_before, cutoff):
        # Conditional: a file restored since the list is kept.
        if await native.purge(row.id, cutoff):
            purged += 1
    # The rows are read before the index, so a blob stored after this snapshot is young.
    live = await asyncio.to_thread(native.rows.live_file_keys)
    stale = now - grace
    orphans = 0
    for stat in await asyncio.to_thread(
        native.content.index.stats, [LIBRARY_KIND], backend=native.content.name
    ):
        if stat.key in live or stat.touched_at > stale:
            continue
        if await native.content.delete_if_stale(stat.key, stale):
            orphans += 1
    return LibrarySweep(purged=purged, orphans=orphans)


#: How old a per-print file nothing records must be before it goes: a run records its
#: copy and its slice moments after it uploads them, and its subject is in use until it
#: ends, so this only spares a run the sweep could not see.
COPY_ORPHAN_GRACE = timedelta(days=1)


class CopySweep(BaseModel):
    #: Files trashed as a folder's older copies of a subject, with their slices.
    superseded: int
    #: Files trashed that nothing records.
    orphans: int
    #: Recorded copies released to the person (moved or restored), each with its slices.
    released: int


async def sweep_print_copies(
    native: NativeLibrary,
    uploads: BambuddyUploadStore,
    subjects_in_use: Callable[[], Awaitable[set[str]]],
    *,
    now: datetime,
    grace: timedelta = COPY_ORPHAN_GRACE,
) -> CopySweep:
    """Trash the per-print copies and slices nothing keeps (module docstring). The marked
    files are read before the subjects in use, so a run that started before the read is
    seen; the subjects in use are read again just before the trash, and a subject a run
    took up in between keeps all its files. A file uploaded after the read waits for the
    next pass."""
    files = {f.id: f for f in await asyncio.to_thread(native.rows.per_print)}
    busy = await subjects_in_use()
    recorded = await uploads.recorded_files(list(files))
    subjects = sorted({f.print_subject for f in files.values() if f.print_subject} - busy)
    copies = await uploads.for_outputs(subjects, native=True)
    by_use = await uploads.copies_by_use(subjects)
    # The recorded copies that carry no mark: theirs if live (moved, restored), else gone.
    unmarked = [c.id for found in copies.values() for c in found if c.id not in files]
    live = {f.id: f for f in await asyncio.to_thread(native.rows.files_by_ids, unmarked)}
    superseded: dict[str, list[int]] = {}
    forget: list[tuple[str, int]] = []
    gone: list[tuple[str, int]] = []
    released: list[int] = []
    for subject in subjects:
        recorded_copies = {c.id: c for c in copies[subject]}
        kept: dict[int | None, LibraryCopy] = {}
        for copy_id in by_use[subject]:  # least recently used first
            copy = recorded_copies.get(copy_id)
            if copy is None:
                continue
            row = files.get(copy.id)
            if row is None and copy.id not in live:
                # Trashed, which a restore may undo; or purged, and then its slices have
                # nothing left to keep them: its record goes, and they are orphans.
                if await asyncio.to_thread(native.rows.file, copy.id, trashed=True) is None:
                    gone.append((subject, copy.id))
                continue
            if row is None or row.folder_id != copy.folder_id:
                # A person's: moved (outside the routes, or a move whose release never
                # ran) or restored. Theirs, with its slices.
                released.append(copy.id)
                continue
            if row.print_subject != subject:
                continue
            older = kept.get(copy.folder_id)
            kept[copy.folder_id] = copy
            if older is not None:
                superseded.setdefault(subject, []).extend(
                    [older.id, *(s.id for s in older.sliced if s.id in files)]
                )
                forget.append((subject, older.id))
    await release_moved(native, uploads, released)
    for subject, copy_id in gone:
        await uploads.forget(subject, copy_id)
    cutoff = now - grace
    orphans: dict[str, list[int]] = {}
    for f in files.values():
        if (
            f.print_subject is not None
            and f.print_subject not in busy
            and f.id not in recorded
            and f.created_at < cutoff
        ):
            orphans.setdefault(f.print_subject, []).append(f.id)
    # A run that started since the first read: its subject keeps everything.
    busy = await subjects_in_use()
    doomed = [i for subject, ids in superseded.items() if subject not in busy for i in ids]
    lost = [i for subject, ids in orphans.items() if subject not in busy for i in ids]
    # Only what is still marked: a file a person moved or restored since the read is theirs.
    trashed = {f.id for f in await native.trash([*doomed, *lost], per_print=True)}
    # After the trash, as a Bambuddy copy is forgotten only once its delete came back.
    for subject, copy_id in forget:
        if copy_id in trashed:
            await uploads.forget(subject, copy_id)
    done = CopySweep(
        superseded=len(trashed.intersection(doomed)),
        orphans=len(trashed.intersection(lost)),
        released=len(released),
    )
    if done.superseded or done.orphans or done.released:
        logger.info("swept the native per-print copies", extra=done.model_dump())
    return done
