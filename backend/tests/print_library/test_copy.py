"""Copies between Bambuddy's library and ScadBuddy's own (#5355): the plan's walk,
the batch's skip-by-hash, folder making and per-file results, both ways, against the
fake Bambuddy library and a native library over S3."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.models import FolderCreate
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.print_library.copy import copy_batch, plan_copy
from scadbuddy.print_library.copy_models import LibraryCopyBatchInput, LibraryCopyRequest
from scadbuddy.print_library.copy_store import LibraryCopyStore
from scadbuddy.print_library.native import NativeLibrary
from scadbuddy.store.content_models import StoreFullError
from scadbuddy.store.index import Pool
from tests.print_library.conftest import upload
from tests.support.fake_bambuddy_library import build_app

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
async def bambuddy() -> AsyncIterator[BambuddyClient]:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport) as http:
        yield BambuddyClient(BambuddyConfig(base_url="http://fake"), http=http)


class _NoUploads:
    """`upload_raw`'s unrecorded-upload check asks which ids an output records: none."""

    async def recorded(self, ids: Any) -> set[int]:
        return set()


def _uploads() -> BambuddyUploadStore:
    return cast(BambuddyUploadStore, _NoUploads())


async def _run(
    store: LibraryCopyStore,
    request: LibraryCopyRequest,
    *,
    bambuddy: BambuddyClient,
    native: NativeLibrary,
    copy_id: str = "library-copy-test",
) -> dict[int, tuple[str, str | None]]:
    """Plan and copy ``request`` to the end; each source id's (status, reason)."""
    store.start(copy_id, request)
    source = bambuddy if request.direction == "bb_to_sb" else native
    store.plan(copy_id, await plan_copy(request, source))
    beats: list[str] = []
    while True:
        batch = await copy_batch(
            store,
            LibraryCopyBatchInput(copy_id=copy_id, request=request, limit=2),
            client=bambuddy,
            native=native,
            uploads=_uploads(),
            heartbeat=beats.append,
        )
        if batch.remaining == 0:
            break
    assert beats, "each file heartbeats while it copies"
    return {f.source_id: (f.status, f.reason) for f in store.files(copy_id)}


async def _bb_tree(bambuddy: BambuddyClient) -> dict[str, int]:
    """Every Bambuddy folder by its path."""
    paths: dict[str, int] = {}

    def walk(folders: Any, above: str) -> None:
        for folder in folders:
            path = f"{above}/{folder.name}" if above else folder.name
            paths[path] = folder.id
            walk(folder.children, path)

    walk(await bambuddy.folders(), "")
    return paths


async def _native_tree(native: NativeLibrary) -> dict[str, int]:
    paths: dict[str, int] = {}

    def walk(folders: Any, above: str) -> None:
        for folder in folders:
            path = f"{above}/{folder.name}" if above else folder.name
            paths[path] = folder.id
            walk(folder.children, path)

    walk(await native.folders(), "")
    return paths


async def test_bambuddy_to_native_keeps_the_folder_path_and_skips_what_landed(
    pool: Pool, native: NativeLibrary, bambuddy: BambuddyClient, three_mf: Path
) -> None:
    dinos = await bambuddy.create_folder(FolderCreate(name="Dinos"))
    rex = await bambuddy.create_folder(FolderCreate(name="Rex", parent_id=dinos.id))
    payload = three_mf.read_bytes()
    top = await bambuddy.upload_library_file("top.3mf", payload, folder_id=dinos.id)
    deep = await bambuddy.upload_library_file("deep.3mf", b"stl-ish bytes", folder_id=rex.id)
    loose = await bambuddy.upload_library_file("loose.3mf", b"loose bytes")
    store = LibraryCopyStore(pool)
    target = await native.create_folder("From Bambuddy", None)
    request = LibraryCopyRequest(
        direction="bb_to_sb",
        file_ids=[loose.id],
        folder_ids=[dinos.id],
        target_folder_id=target.id,
    )

    results = await _run(store, request, bambuddy=bambuddy, native=native)
    assert results == {
        top.id: ("copied", None),
        deep.id: ("copied", None),
        loose.id: ("copied", None),
    }
    tree = await _native_tree(native)
    assert {"From Bambuddy", "From Bambuddy/Dinos", "From Bambuddy/Dinos/Rex"} <= set(tree)
    by_folder = {
        (row.folder_id, row.filename): row
        for row in await native.library_listing(folder_id=None, every_folder=True)
    }
    assert (tree["From Bambuddy/Dinos"], "top.3mf") in by_folder
    assert (tree["From Bambuddy/Dinos/Rex"], "deep.3mf") in by_folder
    assert (target.id, "loose.3mf") in by_folder
    copied = by_folder[(tree["From Bambuddy/Dinos"], "top.3mf")]
    assert b"".join([c async for c in native.download_library_file(copied.id)]) == payload
    # The copy is a library upload like any other: its plates were read.
    assert (await native.library_plates(copied.id)).plates
    summary = store.summary("library-copy-test")
    assert summary is not None
    assert (summary.total, summary.copied, summary.pending) == (3, 3, 0)

    # A rerun lands nothing twice: every file's bytes are at the target already.
    again = await _run(store, request, bambuddy=bambuddy, native=native)
    assert {status for status, _ in again.values()} == {"skipped_same_hash"}
    assert len(await native.library_listing(folder_id=None, every_folder=True)) == 3
    # No folder was made twice either.
    assert sorted(f.name for f in native.rows.folders()) == ["Dinos", "From Bambuddy", "Rex"]


async def test_a_source_file_that_is_gone_fails_alone(
    pool: Pool, native: NativeLibrary, bambuddy: BambuddyClient
) -> None:
    kept = await bambuddy.upload_library_file("kept.3mf", b"kept")
    store = LibraryCopyStore(pool)
    request = LibraryCopyRequest(direction="bb_to_sb", file_ids=[kept.id, 4242])
    results = await _run(store, request, bambuddy=bambuddy, native=native)
    assert results[kept.id] == ("copied", None)
    assert results[4242][0] == "failed"
    assert "no longer in the source" in (results[4242][1] or "")


async def test_a_full_store_is_a_failed_row_with_the_507(
    pool: Pool, native: NativeLibrary, bambuddy: BambuddyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = await bambuddy.upload_library_file("big.3mf", b"big")

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise StoreFullError("the store holds its 10 MB")

    monkeypatch.setattr(native, "upload", refuse)
    store = LibraryCopyStore(pool)
    results = await _run(
        store,
        LibraryCopyRequest(direction="bb_to_sb", file_ids=[file.id]),
        bambuddy=bambuddy,
        native=native,
    )
    status, reason = results[file.id]
    assert status == "failed"
    assert reason is not None and "507" in reason and "10 MB" in reason


async def test_native_to_bambuddy_uploads_once_and_keeps_both_on_a_name_clash(
    pool: Pool, native: NativeLibrary, bambuddy: BambuddyClient, three_mf: Path
) -> None:
    parts = await native.create_folder("Parts", None)
    small = await native.create_folder("Small", parts.id)
    one = await upload(native, three_mf, parts.id)
    other = three_mf.with_name("other.3mf")
    other.write_bytes(b"other native bytes")
    two = await upload(native, other, small.id)
    # A Bambuddy file of the same name but other bytes: never overwritten.
    clash = await bambuddy.upload_library_file("bracket.3mf", b"other bytes")
    # ... and one of the same bytes, elsewhere: the copy is skipped for it.
    same = await bambuddy.upload_library_file("elsewhere.3mf", three_mf.read_bytes())
    store = LibraryCopyStore(pool)
    request = LibraryCopyRequest(direction="sb_to_bb", folder_ids=[parts.id])

    results = await _run(store, request, bambuddy=bambuddy, native=native)
    assert results[one.id] == ("skipped_same_hash", None)
    assert results[two.id] == ("copied", None)
    tree = await _bb_tree(bambuddy)
    assert {"Parts", "Parts/Small"} <= set(tree)
    rows = await bambuddy.library_listing(folder_id=None, every_folder=True)
    assert {(r.folder_id, r.filename) for r in rows if r.folder_id is not None} == {
        (tree["Parts/Small"], "other.3mf")
    }
    assert (await bambuddy.library_file(clash.id)).file_size == len(b"other bytes")
    assert (await bambuddy.library_file(same.id)).filename == "elsewhere.3mf"
    files = {f.source_id: f for f in store.files("library-copy-test")}
    assert files[one.id].target_id == same.id
    assert files[two.id].target_folder_id == tree["Parts/Small"]
    assert files[two.id].path == "Parts/Small"

    again = await _run(store, request, bambuddy=bambuddy, native=native)
    assert {status for status, _ in again.values()} == {"skipped_same_hash"}
    assert len(await bambuddy.library_listing(folder_id=None, every_folder=True)) == 3


async def test_the_plan_refuses_ids_on_the_wrong_side_and_a_missing_folder(
    native: NativeLibrary, bambuddy: BambuddyClient
) -> None:
    with pytest.raises(ApiError) as wrong:
        await plan_copy(LibraryCopyRequest(direction="bb_to_sb", file_ids=[1000000001]), bambuddy)
    assert wrong.value.status == 422
    with pytest.raises(ApiError) as target:
        await plan_copy(
            LibraryCopyRequest(
                direction="sb_to_bb", file_ids=[1000000001], target_folder_id=1000000002
            ),
            native,
        )
    assert target.value.status == 422
    with pytest.raises(ApiError) as missing:
        await plan_copy(LibraryCopyRequest(direction="bb_to_sb", folder_ids=[77]), bambuddy)
    assert missing.value.status == 404
    with pytest.raises(ValueError, match="at least one"):
        LibraryCopyRequest(direction="bb_to_sb")


async def test_a_file_in_a_selected_folder_is_planned_once_under_its_path(
    bambuddy: BambuddyClient,
) -> None:
    folder = await bambuddy.create_folder(FolderCreate(name="Both"))
    file = await bambuddy.upload_library_file("twice.3mf", b"x", folder_id=folder.id)
    planned = await plan_copy(
        LibraryCopyRequest(direction="bb_to_sb", file_ids=[file.id], folder_ids=[folder.id]),
        bambuddy,
    )
    assert [(p.source_id, p.path) for p in planned] == [(file.id, "Both"), (file.id, "")]
    # The store keeps the first of a source id: the folder's path wins.
