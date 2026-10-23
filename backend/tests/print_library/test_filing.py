"""Filing a saved output into the native library (plan 2026-10-17-native-print-library,
Task 8): one top-level folder per template title, one copy per output."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.print_library.component import CONNECT_TIMEOUT, ROW_POOL_SIZE
from scadbuddy.print_library.filing import file_output, put_in_folder, trash_filed
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.store import NativeFile, NativeLibraryStore
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


async def test_a_retried_filing_after_the_copy_moved_files_a_second_copy(
    native: NativeLibrary, three_mf: Path
) -> None:
    """The trade-off #4507 made: a moved copy (`filed` cleared) cannot be told from one
    put in a chosen folder, and only a filed copy counts, so an `output_create` retried
    after the user moved its copy files a second one into the template folder."""
    meta = _meta("d" * 32)
    first = await file_output(native, meta, three_mf, "Name Sign")
    template_folder = first.folder_id
    await native.update_file(first.id, folder_id=None, filed=False)
    again = await file_output(native, meta, three_mf, "Name Sign")  # the retry
    assert again.id != first.id and again.filed
    assert again.folder_id == template_folder
    assert [f.id for f in native.rows.files(template_folder)] == [again.id]
    moved = native.rows.file(first.id)
    assert moved is not None and moved.folder_id is None and not moved.filed


async def test_a_chosen_folder_copy_never_suppresses_the_filing(
    native: NativeLibrary, three_mf: Path
) -> None:
    """#4507: the save's filing failed, Generate put a copy in a chosen folder, and a
    later filing still files into the template's folder."""
    meta = _meta("e" * 32)
    gifts = native.rows.create_folder("Gifts", None).id
    chosen, created = await put_in_folder(native, meta, three_mf, gifts, "Sign")
    assert created and not chosen.filed and chosen.folder_id == gifts
    filed = await file_output(native, meta, three_mf, "Name Sign")
    assert filed.id != chosen.id and filed.filed
    folder = native.rows.folder(filed.folder_id or 0)
    assert folder is not None and folder.name == "Name Sign"


async def test_overlapping_puts_in_one_folder_put_one_copy(
    native: NativeLibrary, three_mf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4507: a double Generate, or the agent and the page at once, with different keys."""
    meta = _meta("f" * 32)
    gifts = native.rows.create_folder("Gifts", None).id
    staged: list[str] = []
    stage = native.stage

    async def recorded(*args: Any, **kwargs: Any) -> NativeFile:
        row = await stage(*args, **kwargs)
        staged.append(row.blob_key)
        return row

    monkeypatch.setattr(native, "stage", recorded)
    placed = await asyncio.gather(
        *(put_in_folder(native, meta, three_mf, gifts, "Sign") for _ in range(4))
    )
    assert len({row.id for row, _ in placed}) == 1
    assert sorted(created for _, created in placed) == [False, False, False, True]
    assert [f.name for f in native.rows.files(gifts)] == ["Sign.3mf"]
    # Every upload but the kept one's was released.
    kept = placed[0][0].blob_key
    assert kept in staged
    for key in staged:
        assert (await native.content.stat(key) is None) == (key != kept)


async def test_a_put_into_a_folder_deleted_since_its_check_is_404(
    native: NativeLibrary, three_mf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4507 review: the folder is checked live inside the locked insert; the staged
    upload is released."""
    meta = _meta("9" * 32)
    gifts = native.rows.create_folder("Gifts", None).id
    staged: list[str] = []
    stage = native.stage

    async def then_delete(*args: Any, **kwargs: Any) -> NativeFile:
        row = await stage(*args, **kwargs)
        staged.append(row.blob_key)
        await native.delete_folder(gifts)  # between the check and the insert
        return row

    monkeypatch.setattr(native, "stage", then_delete)
    with pytest.raises(ApiError) as gone:
        await put_in_folder(native, meta, three_mf, gifts, "Sign")
    assert gone.value.status == 404
    assert native.rows.rows_for_output(meta.id) == []
    assert [await native.content.stat(key) for key in staged] == [None]


async def test_a_put_names_its_copy_unique_in_the_folder(
    native: NativeLibrary, three_mf: Path
) -> None:
    gifts = native.rows.create_folder("Gifts", None).id
    a, _ = await put_in_folder(native, _meta("1" * 32), three_mf, gifts, "Sign")
    b, _ = await put_in_folder(native, _meta("2" * 32), three_mf, gifts, "Sign")
    assert (a.name, b.name) == ("Sign.3mf", "Sign (2).3mf")


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
