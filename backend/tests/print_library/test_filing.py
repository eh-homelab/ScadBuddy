"""Filing a saved output into the native library (plan 2026-10-17-native-print-library,
Task 8): one top-level folder per template title, one copy per output."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.core.problems import ApiError
from scadbuddy.library.output_rows import ByteWrite
from scadbuddy.library.outputs import MODEL_NAME, OutputBytesLostError, OutputMeta
from scadbuddy.print_library.filing import (
    file_output,
    put_in_folder,
    put_saved_in_folder,
    trash_filed,
)
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.print_library.store import NativeFile, NativeLibraryStore
from scadbuddy.render.glb import BoundingBox
from scadbuddy.rows.component import CONNECT_TIMEOUT
from scadbuddy.rows.component import POOL_SIZE as ROW_POOL_SIZE
from scadbuddy.store.content import ContentStore
from scadbuddy.store.index import Pool
from tests.support.output_blobs import blob_output

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


async def test_a_retried_filing_after_the_copy_was_trashed_files_none(
    native: NativeLibrary, three_mf: Path
) -> None:
    """A trashed filed copy still counts as filed, so a retry after the user trashed it
    does not file it again; the trashed copy is the answer (#4120). So the caller gets
    back a row with ``deleted_at`` set, and that is intended, not a bug: the user threw
    the copy away, and a retry must not put it back."""
    meta = _meta("f" * 32)
    first = await file_output(native, meta, three_mf, "Name Sign")
    assert [f.id for f in await native.trash([first.id])] == [first.id]
    again = await file_output(native, meta, three_mf, "Name Sign")  # the retry
    assert again.id == first.id and again.deleted_at is not None
    assert native.rows.filed_for_output(meta.id) == []
    assert [f.id for f in native.rows.rows_for_output(meta.id)] == [first.id]


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


async def test_filing_uploads_blob_bytes(
    native: NativeLibrary, tmp_path: Path, pg_conninfo: str
) -> None:
    """#7256: a saved output's put in a chosen folder reads its 3MF from the model blob,
    with the NFS file gone. A lost blob raises `OutputBytesLostError` (#7257, plan D9);
    a row that names no model blob is the 404."""
    async with blob_output(tmp_path / "output", pg_conninfo) as output:
        model = output.directory / MODEL_NAME
        data = model.read_bytes()
        model.unlink()
        gifts = native.rows.create_folder("Gifts", None).id
        placed, created = await put_saved_in_folder(
            native, output.store, output.meta, gifts, "Demo"
        )
        assert created and placed.sha256 == hashlib.sha256(data).hexdigest()
        rows = output.store.rows
        rows.set_blob(output.meta.id, "model", "output-gone-model")
        with pytest.raises(OutputBytesLostError):
            await put_saved_in_folder(
                native, output.store, output.meta, native.rows.create_folder("A", None).id, "x"
            )
        row = rows.get(output.meta.id)
        assert row is not None
        blobs = dataclasses.replace(row.blobs, model=None)
        rows.write_byte_copy([ByteWrite(output.meta.id, blobs, "stamp", [])])
        with pytest.raises(ApiError) as refused:
            await put_saved_in_folder(
                native, output.store, output.meta, native.rows.create_folder("B", None).id, "x"
            )
        assert refused.value.status == 404


async def test_filing_again_reads_no_3mf(
    native: NativeLibrary, tmp_path: Path, pg_conninfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7256 review B-M1: a put in a folder that holds its copy answers the copy with no
    download of the 3MF."""
    async with blob_output(tmp_path / "output", pg_conninfo) as output:
        gifts = native.rows.create_folder("Gifts", None).id
        placed, _ = await put_saved_in_folder(native, output.store, output.meta, gifts, "Demo")
        asked: list[str] = []
        real = output.store.local_role

        def counting(output_id: str, role: Any) -> Any:
            asked.append(role)
            return real(output_id, role)

        monkeypatch.setattr(output.store, "local_role", counting)
        there, created = await put_saved_in_folder(native, output.store, output.meta, gifts, "Demo")
        assert (there.id, created) == (placed.id, False)
        assert asked == []


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


async def test_overlapping_puts_of_two_outputs_take_two_names(
    native: NativeLibrary, three_mf: Path
) -> None:
    """#4508 review: the per-output lock alone let two outputs of one stem both take
    `Sign.3mf`; the folder's lock around the name pick keeps them apart."""
    gifts = native.rows.create_folder("Gifts", None).id
    placed = await asyncio.gather(
        *(
            put_in_folder(native, _meta(f"{i + 0xA0:032x}"), three_mf, gifts, "Sign")
            for i in range(4)
        )
    )
    assert sorted(row.name for row, _ in placed) == [
        "Sign (2).3mf",
        "Sign (3).3mf",
        "Sign (4).3mf",
        "Sign.3mf",
    ]


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
