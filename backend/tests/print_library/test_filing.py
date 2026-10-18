"""Filing a saved output into the native library (plan 2026-10-17-native-print-library,
Task 8): one top-level folder per template title, one copy per output."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.library.outputs import OutputMeta
from scadbuddy.print_library.component import CONNECT_TIMEOUT, ROW_POOL_SIZE
from scadbuddy.print_library.filing import file_output, trash_filed
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.store import NativeLibraryStore
from scadbuddy.render.glb import BoundingBox
from scadbuddy.store.content import ContentStore
from scadbuddy.store.index import Pool

pytestmark = pytest.mark.requires_postgres


def _meta(output_id: str, name: str = "Sign") -> OutputMeta:
    box = BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1))
    return OutputMeta(
        id=output_id, slug="demo", name=name, job_id="j", created_at=datetime.now(UTC), bbox_mm=box
    )


async def test_filing_twice_files_once(native: NativeLibrary, three_mf: Path) -> None:
    meta = _meta("a" * 32)
    a = await file_output(native, meta, three_mf, "Name Sign")
    b = await file_output(native, meta, three_mf, "Name Sign")  # an activity retry
    assert a.id == b.id
    assert len(native.rows.filed_for_output(meta.id)) == 1
    assert (a.name, a.output_id, a.filed) == ("demo-sign.3mf", meta.id, True)


async def test_two_outputs_of_one_template_share_a_folder_under_a_race(
    native: NativeLibrary, three_mf: Path
) -> None:
    filed = await asyncio.gather(
        *(file_output(native, _meta(f"{i:032x}", f"n{i}"), three_mf, "Name Sign") for i in range(4))
    )
    assert len({f.folder_id for f in filed}) == 1
    assert [f.name for f in native.rows.folders()] == ["Name Sign"]


async def test_trash_filed_takes_only_unmoved_copies(native: NativeLibrary, three_mf: Path) -> None:
    meta = _meta("b" * 32)
    kept = await file_output(native, meta, three_mf, "Name Sign")
    other = await file_output(native, _meta("c" * 32, "other"), three_mf, "Name Sign")
    await native.update_file(kept.id, folder_id=None, filed=False)
    assert await trash_filed(native, meta.id) == []
    assert await trash_filed(native, other.output_id or "") == [other.id]
    assert native.rows.file(kept.id) is not None
    assert native.rows.file(other.id) is None


async def test_overlapping_filings_of_one_output_file_one_copy(
    native: NativeLibrary, three_mf: Path
) -> None:
    meta = _meta("d" * 32)
    filed = await asyncio.gather(
        *(file_output(native, meta, three_mf, "Name Sign") for _ in range(3))
    )
    assert len({f.id for f in filed}) == 1
    assert len(native.rows.filed_for_output(meta.id)) == 1


async def test_filings_past_the_row_pool_all_file(
    pg_conninfo: str, content: ContentStore, three_mf: Path, tmp_path: Path
) -> None:
    """Twice the component's row pool of saves filing at once (an Arrange batch, the
    agent) each file their copy: a filing holds one row connection, never a second
    while it waits on the pool, so none times out and drops its filing."""
    rows: Pool = ConnectionPool(
        pg_conninfo,
        min_size=1,
        max_size=ROW_POOL_SIZE,
        timeout=CONNECT_TIMEOUT,
        connection_class=Connection[DictRow],
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    with rows:
        native = NativeLibrary(NativeLibraryStore(rows), content, tmp_path)
        metas = [_meta(f"{i + 0xE0:032x}", f"n{i}") for i in range(2 * ROW_POOL_SIZE)]
        filed = await asyncio.gather(
            *(file_output(native, meta, three_mf, "Name Sign") for meta in metas)
        )
        assert len({f.id for f in filed}) == len(metas)
        for meta in metas:
            assert len(native.rows.filed_for_output(meta.id)) == 1
