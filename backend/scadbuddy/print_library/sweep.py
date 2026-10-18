"""The native library's housekeeping sweep (plan 2026-10-17-native-print-library, Task 6):
trash past its retention is purged, and a `library` blob no row names is removed. The
generic blob sweep never touches the `library` kind, so this is the only collector of an
orphan."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from pydantic import BaseModel

from scadbuddy.print_library.native import NativeLibrary

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
