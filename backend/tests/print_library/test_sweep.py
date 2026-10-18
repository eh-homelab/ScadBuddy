"""`sweep_library` (plan 2026-10-17-native-print-library, Task 6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.sweep import LIBRARY_TRASH_TTL, sweep_library
from scadbuddy.store.content import BlobScope
from tests.print_library.conftest import upload

pytestmark = pytest.mark.requires_postgres


def utcnow() -> datetime:
    return datetime.now(UTC)


def trash_at(native: NativeLibrary, file_id: int, at: datetime) -> None:
    native.rows.trash([file_id])
    with native.rows._pool.connection() as conn:
        conn.execute("UPDATE library_files SET deleted_at = %s WHERE id = %s", (at, file_id))


async def test_old_trash_is_purged_and_new_trash_kept(
    native: NativeLibrary, three_mf: Path
) -> None:
    now = utcnow()
    old = await upload(native, three_mf)
    new = await upload(native, three_mf)
    trash_at(native, old.id, now - timedelta(days=31))
    trash_at(native, new.id, now - timedelta(days=1))
    assert (await sweep_library(native, now=now)).purged == 1
    assert native.rows.file(old.id, trashed=True) is None
    assert native.rows.file(new.id, trashed=True) is not None


async def test_a_restore_between_list_and_purge_keeps_the_file(
    native: NativeLibrary, three_mf: Path
) -> None:
    now = utcnow()
    f = await upload(native, three_mf)
    trash_at(native, f.id, now - timedelta(days=31))
    native.rows.restore([f.id])  # after the sweep's list, before its purge
    assert await native.purge(f.id, now - LIBRARY_TRASH_TTL) is False
    data = b"".join([c async for c in native.download_library_file(f.id)])
    assert data == three_mf.read_bytes()


async def test_an_orphan_library_blob_goes_after_grace(native: NativeLibrary) -> None:
    await native.content.put(
        "library", b"o", name="o", scope=BlobScope(), key="library-file-1000000099"
    )
    assert (await sweep_library(native, now=utcnow())).orphans == 0
    assert (await sweep_library(native, now=utcnow() + timedelta(hours=2))).orphans == 1


async def test_a_live_files_blob_is_never_an_orphan(native: NativeLibrary, three_mf: Path) -> None:
    f = await upload(native, three_mf)
    assert (await sweep_library(native, now=utcnow() + timedelta(days=5))).orphans == 0
    assert native.rows.file(f.id) is not None
