"""Copies between Bambuddy's library and ScadBuddy's own (#5355): the plan's walk,
the batch's skip-by-hash, folder making and per-file results, both ways, against the
fake Bambuddy library and a native library over S3."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM, Scope, map_response
from scadbuddy.bambuddy.models import FolderCreate
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.print_library.copy import (
    FILE_ATTEMPTS,
    GAVE_UP,
    GONE_FROM_SOURCE,
    MAX_FILE_TRIES,
    NO_REASON,
    TARGET_FOLDER_GONE,
    CopyRefusedError,
    _BambuddyTarget,
    copy_batch,
    plan_copy,
)
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
    bb_to_sb = request.direction == "bb_to_sb"
    source, target = (bambuddy, native) if bb_to_sb else (native, bambuddy)
    store.plan(copy_id, await plan_copy(request, source, target))
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


def _batch(
    store: LibraryCopyStore,
    request: LibraryCopyRequest,
    *,
    bambuddy: BambuddyClient,
    native: NativeLibrary,
    copy_id: str = "library-copy-test",
    limit: int = 10,
) -> Any:
    return copy_batch(
        store,
        LibraryCopyBatchInput(copy_id=copy_id, request=request, limit=limit),
        client=bambuddy,
        native=native,
        uploads=_uploads(),
        heartbeat=lambda _: None,
    )


async def _planned(
    store: LibraryCopyStore,
    request: LibraryCopyRequest,
    *,
    bambuddy: BambuddyClient,
    native: NativeLibrary,
    copy_id: str = "library-copy-test",
) -> None:
    store.start(copy_id, request)
    bb_to_sb = request.direction == "bb_to_sb"
    source, target = (bambuddy, native) if bb_to_sb else (native, bambuddy)
    store.plan(copy_id, await plan_copy(request, source, target))


def _rows(store: LibraryCopyStore, copy_id: str = "library-copy-test") -> dict[int, Any]:
    return {f.source_id: f for f in store.files(copy_id)}


async def test_a_file_the_library_always_500s_on_fails_alone_once_its_tries_ran_out(
    pool: Pool, native: NativeLibrary, bambuddy: BambuddyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 5xx is retried, since it is as likely the library as the file. The file counts
    its own tries (#5424): on its `FILE_ATTEMPTS`th it is the file's own failure, and
    the same batch goes on to copy the rest. While it is retried, the pending row says
    why."""
    bad = await bambuddy.upload_library_file("bad.3mf", b"bad bytes")
    good = await bambuddy.upload_library_file("good.3mf", b"good bytes")
    download = BambuddyClient.download_library_file

    def failing(self: BambuddyClient, file_id: int) -> Any:
        if file_id == bad.id:
            raise ApiError(
                502,
                "Bambuddy answered 500 for the download",
                type_=UNAVAILABLE_PROBLEM,
                bambuddy_status=500,
            )
        return download(self, file_id)

    monkeypatch.setattr(BambuddyClient, "download_library_file", failing)
    request = LibraryCopyRequest(direction="bb_to_sb", file_ids=[bad.id, good.id])
    store = LibraryCopyStore(pool)
    await _planned(store, request, bambuddy=bambuddy, native=native)

    for tries in range(1, FILE_ATTEMPTS):
        with pytest.raises(ApiError) as retried:
            await _batch(store, request, bambuddy=bambuddy, native=native)
        assert retried.value.status == 502
        rows = _rows(store)
        assert (rows[bad.id].status, rows[bad.id].attempts) == ("pending", tries)
        assert "500" in (rows[bad.id].reason or "")
        assert rows[bad.id].reason_try == tries  # the try that failed, said with it
        assert (rows[good.id].status, rows[good.id].attempts) == ("pending", 0)
    last = await _batch(store, request, bambuddy=bambuddy, native=native)
    assert (last.copied, last.failed, last.remaining) == (1, 1, 0)
    rows = _rows(store)
    assert rows[good.id].status == "copied" and rows[good.id].reason is None
    assert rows[bad.id].status == "failed"
    assert rows[bad.id].attempts == FILE_ATTEMPTS
    assert "500" in (rows[bad.id].reason or "")
    assert rows[bad.id].reason_try is None


async def test_a_file_written_before_the_raise_stays_recorded(
    pool: Pool, native: NativeLibrary, bambuddy: BambuddyClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A batch that raises on its second file keeps its first file's row: the retry
    starts at the file that raised, and spends none of the first file's tries."""
    first = await bambuddy.upload_library_file("a.3mf", b"first bytes")
    second = await bambuddy.upload_library_file("b.3mf", b"second bytes")
    download = BambuddyClient.download_library_file

    def failing(self: BambuddyClient, file_id: int) -> Any:
        if file_id == second.id:
            raise ApiError(502, "could not reach Bambuddy", type_=UNAVAILABLE_PROBLEM)
        return download(self, file_id)

    monkeypatch.setattr(BambuddyClient, "download_library_file", failing)
    request = LibraryCopyRequest(direction="bb_to_sb", file_ids=[first.id, second.id])
    store = LibraryCopyStore(pool)
    await _planned(store, request, bambuddy=bambuddy, native=native)
    with pytest.raises(ApiError):
        await _batch(store, request, bambuddy=bambuddy, native=native)
    rows = _rows(store)
    assert (rows[first.id].status, rows[first.id].attempts) == ("copied", 1)
    assert (rows[second.id].status, rows[second.id].attempts) == ("pending", 1)
    monkeypatch.setattr(BambuddyClient, "download_library_file", download)
    done = await _batch(store, request, bambuddy=bambuddy, native=native)
    assert (done.copied, done.remaining) == (1, 0)
    assert _rows(store)[first.id].attempts == 1


@pytest.mark.parametrize("answer", ["unreachable", "429"])
async def test_a_library_that_does_not_answer_is_retried_until_the_file_tries_run_out(
    pool: Pool,
    native: NativeLibrary,
    bambuddy: BambuddyClient,
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    """A Bambuddy that cannot be reached (a restart, a gateway's 503), or that limits
    requests (429, #5424), is not the file's failure while the file has tries: past
    `FILE_ATTEMPTS` the batch still raises. After `MAX_FILE_TRIES` the file fails with
    the last reason, so a file that always times out cannot hold the job. The next file
    then has tries of its own."""
    one = await bambuddy.upload_library_file("one.3mf", b"one")
    two = await bambuddy.upload_library_file("two.3mf", b"two")

    def down(self: BambuddyClient, file_id: int) -> Any:
        if answer == "429":
            raise ApiError(
                502,
                "Bambuddy is limiting requests and refused to download the file",
                type_=UNAVAILABLE_PROBLEM,
                bambuddy_status=429,
            )
        raise ApiError(504, "could not reach Bambuddy: ReadTimeout", type_=UNAVAILABLE_PROBLEM)

    monkeypatch.setattr(BambuddyClient, "download_library_file", down)
    request = LibraryCopyRequest(direction="bb_to_sb", file_ids=[one.id, two.id])
    store = LibraryCopyStore(pool)
    await _planned(store, request, bambuddy=bambuddy, native=native)
    for _ in range(MAX_FILE_TRIES - 1):
        with pytest.raises(ApiError):
            await _batch(store, request, bambuddy=bambuddy, native=native)
    rows = _rows(store)
    assert {f.status for f in rows.values()} == {"pending"}
    assert rows[one.id].attempts == MAX_FILE_TRIES - 1 and rows[two.id].attempts == 0
    assert rows[one.id].reason_try == MAX_FILE_TRIES - 1
    assert ("limiting" if answer == "429" else "ReadTimeout") in (rows[one.id].reason or "")
    # The last try: the file fails with the reason, and the next file raises on its first.
    with pytest.raises(ApiError):
        await _batch(store, request, bambuddy=bambuddy, native=native)
    rows = _rows(store)
    assert rows[one.id].status == "failed"
    assert (rows[one.id].reason or "").startswith(f"gave up after {MAX_FILE_TRIES} tries: ")
    assert rows[one.id].attempts == MAX_FILE_TRIES
    assert (rows[two.id].status, rows[two.id].attempts) == ("pending", 1)


async def test_a_file_whose_tries_never_ended_fails_once_they_ran_out(
    pool: Pool, native: NativeLibrary, bambuddy: BambuddyClient
) -> None:
    """A batch that timed out, or whose worker stopped, records nothing, but its try
    was counted when it began. After `MAX_FILE_TRIES` such tries the file fails."""
    file = await bambuddy.upload_library_file("huge.3mf", b"huge")
    request = LibraryCopyRequest(direction="bb_to_sb", file_ids=[file.id])
    store = LibraryCopyStore(pool)
    await _planned(store, request, bambuddy=bambuddy, native=native)
    for _ in range(MAX_FILE_TRIES):
        store.begin("library-copy-test", file.id, limit=MAX_FILE_TRIES)
    batch = await _batch(store, request, bambuddy=bambuddy, native=native)
    assert (batch.failed, batch.remaining) == (1, 0)
    row = _rows(store)[file.id]
    assert row.reason == GAVE_UP.format(tries=MAX_FILE_TRIES, reason=NO_REASON)
    # A file that ran out of tries keeps its count: never more than the reason says.
    assert row.attempts == MAX_FILE_TRIES


@pytest.mark.parametrize("code", [401, 403])
async def test_bambuddy_refusing_the_key_fails_the_copy_not_each_file(
    pool: Pool,
    native: NativeLibrary,
    bambuddy: BambuddyClient,
    three_mf: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
) -> None:
    """A 401 or 403 on one ``file_hash`` read (`_BambuddyTarget.has`) would fail every
    file the same way (#5424). It raises `CopyRefusedError`, which fails the copy as a
    whole with Bambuddy's reason; no file is written off for it, so a rerun with a
    good key copies them all."""
    await bambuddy.upload_library_file("there.3mf", three_mf.read_bytes())
    one = await upload(native, three_mf)

    async def refused(self: BambuddyClient, file_id: int) -> Any:
        raise map_response(
            httpx.Response(code, json={"detail": "nope"}),
            scope=Scope.MANAGE_LIBRARY,
            what="read a library file",
        )

    monkeypatch.setattr(BambuddyClient, "library_file", refused)
    request = LibraryCopyRequest(direction="sb_to_bb", file_ids=[one.id])
    store = LibraryCopyStore(pool)
    await _planned(store, request, bambuddy=bambuddy, native=native)
    with pytest.raises(CopyRefusedError, match="API key"):
        await _batch(store, request, bambuddy=bambuddy, native=native)
    assert {f.status for f in store.files("library-copy-test")} == {"pending"}


async def test_two_copies_of_the_same_bytes_into_bambuddy_upload_them_once(
    pool: Pool, native: NativeLibrary, bambuddy: BambuddyClient, three_mf: Path
) -> None:
    """Two copies into one target at once (#5424): the lock on the bytes' SHA-256 in
    that library holds from the hash check until the row is recorded, and a copy whose
    listing is older than the other's put finds it through the rows. One uploads, the
    other skips."""
    one = await upload(native, three_mf)
    store = LibraryCopyStore(pool)
    first = LibraryCopyRequest(direction="sb_to_bb", file_ids=[one.id])
    second = LibraryCopyRequest(direction="sb_to_bb", file_ids=[one.id], target_folder_id=None)
    await _planned(store, first, bambuddy=bambuddy, native=native, copy_id="library-copy-a")
    await _planned(store, second, bambuddy=bambuddy, native=native, copy_id="library-copy-b")
    batches = await asyncio.gather(
        _batch(store, first, bambuddy=bambuddy, native=native, copy_id="library-copy-a"),
        _batch(store, second, bambuddy=bambuddy, native=native, copy_id="library-copy-b"),
    )
    assert sorted((b.copied, b.skipped) for b in batches) == [(0, 1), (1, 0)]
    assert len(await bambuddy.library_listing(folder_id=None)) == 1
    a, b = _rows(store, "library-copy-a")[one.id], _rows(store, "library-copy-b")[one.id]
    assert a.target_id == b.target_id


async def test_a_copy_whose_listing_is_stale_finds_another_copys_upload_by_its_row(
    pool: Pool, native: NativeLibrary, bambuddy: BambuddyClient, three_mf: Path
) -> None:
    """`_BambuddyTarget.has` reads Bambuddy's listing once per batch. A file another
    copy put after that read is found through that copy's row (`held`), and checked at
    Bambuddy first."""
    one = await upload(native, three_mf)
    store = LibraryCopyStore(pool)
    request = LibraryCopyRequest(direction="sb_to_bb", file_ids=[one.id])
    await _planned(store, request, bambuddy=bambuddy, native=native, copy_id="library-copy-a")
    target = _BambuddyTarget(bambuddy, _uploads(), None, store)
    assert await target.has("ab" * 32, None) is None  # the listing is read now: empty
    await _batch(store, request, bambuddy=bambuddy, native=native, copy_id="library-copy-a")
    landed = _rows(store, "library-copy-a")[one.id]
    assert landed.status == "copied"
    source = native.rows.file(one.id)
    assert source is not None and landed.target_id is not None
    assert await target.has(source.sha256, None) == landed.target_id
    await bambuddy.delete_library_file(landed.target_id)
    fresh = _BambuddyTarget(bambuddy, _uploads(), None, store)
    assert await fresh.has(source.sha256, None) is None


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
        await plan_copy(
            LibraryCopyRequest(direction="bb_to_sb", file_ids=[1000000001]), bambuddy, native
        )
    assert wrong.value.status == 422
    with pytest.raises(ApiError) as target:
        await plan_copy(
            LibraryCopyRequest(
                direction="sb_to_bb", file_ids=[1000000001], target_folder_id=1000000002
            ),
            native,
            bambuddy,
        )
    assert target.value.status == 422
    with pytest.raises(ApiError) as missing:
        await plan_copy(LibraryCopyRequest(direction="bb_to_sb", folder_ids=[77]), bambuddy, native)
    assert missing.value.status == 404
    # A target folder the target library does not have is its own 404, before any file.
    loose = await bambuddy.upload_library_file("loose.3mf", b"bytes")
    with pytest.raises(ApiError) as no_target:
        await plan_copy(
            LibraryCopyRequest(
                direction="bb_to_sb", file_ids=[loose.id], target_folder_id=1000000999
            ),
            bambuddy,
            native,
        )
    assert no_target.value.status == 404
    assert no_target.value.detail == TARGET_FOLDER_GONE.format(id=1000000999)
    with pytest.raises(ValueError, match="at least one"):
        LibraryCopyRequest(direction="bb_to_sb")


async def test_a_target_folder_deleted_after_the_plan_fails_each_file_with_its_own_reason(
    pool: Pool, native: NativeLibrary, bambuddy: BambuddyClient
) -> None:
    """The target's 404 is not the source's: no file is reported gone from the source."""
    one = await bambuddy.upload_library_file("one.3mf", b"one")
    two = await bambuddy.upload_library_file("two.3mf", b"two")
    target = await native.create_folder("Doomed", None)
    request = LibraryCopyRequest(
        direction="bb_to_sb", file_ids=[one.id, two.id], target_folder_id=target.id
    )
    store = LibraryCopyStore(pool)
    store.start("library-copy-test", request)
    store.plan("library-copy-test", await plan_copy(request, bambuddy, native))
    await native.delete_folder(target.id)
    batch = await copy_batch(
        store,
        LibraryCopyBatchInput(copy_id="library-copy-test", request=request, limit=10),
        client=bambuddy,
        native=native,
        uploads=_uploads(),
        heartbeat=lambda _: None,
    )
    assert (batch.copied, batch.failed, batch.remaining) == (0, 2, 0)
    for row in store.files("library-copy-test"):
        assert row.status == "failed"
        assert row.reason is not None and row.reason != GONE_FROM_SOURCE
        assert str(target.id) in row.reason
    assert await asyncio.to_thread(native.rows.files, None, every_folder=True) == []


async def test_a_file_in_a_selected_folder_is_planned_once_under_its_path(
    bambuddy: BambuddyClient, native: NativeLibrary
) -> None:
    folder = await bambuddy.create_folder(FolderCreate(name="Both"))
    file = await bambuddy.upload_library_file("twice.3mf", b"x", folder_id=folder.id)
    planned = await plan_copy(
        LibraryCopyRequest(direction="bb_to_sb", file_ids=[file.id], folder_ids=[folder.id]),
        bambuddy,
        native,
    )
    assert [(p.source_id, p.path) for p in planned] == [(file.id, "Both"), (file.id, "")]
    # The store keeps the first of a source id: the folder's path wins.


async def test_two_copies_of_the_same_bambuddy_file_into_native_upload_it_once(
    pool: Pool, native: NativeLibrary, bambuddy: BambuddyClient
) -> None:
    """The same race the other way: the lock on Bambuddy's ``file_hash`` is taken
    before the download, so no spool waits open on it, and one copy skips."""
    file = await bambuddy.upload_library_file("both.3mf", b"shared bytes")
    store = LibraryCopyStore(pool)
    request = LibraryCopyRequest(direction="bb_to_sb", file_ids=[file.id])
    for copy_id in ("library-copy-a", "library-copy-b"):
        await _planned(store, request, bambuddy=bambuddy, native=native, copy_id=copy_id)
    batches = await asyncio.gather(
        _batch(store, request, bambuddy=bambuddy, native=native, copy_id="library-copy-a"),
        _batch(store, request, bambuddy=bambuddy, native=native, copy_id="library-copy-b"),
    )
    assert sorted((b.copied, b.skipped) for b in batches) == [(0, 1), (1, 0)]
    assert len(await native.library_listing(folder_id=None)) == 1


async def test_a_file_that_already_ended_keeps_its_outcome(
    pool: Pool, native: NativeLibrary, bambuddy: BambuddyClient
) -> None:
    """A batch attempt that timed out but still runs, and its retry, can both end one
    file. The second end (a skip on the first one's upload) never overwrites the first."""
    file = await bambuddy.upload_library_file("one.3mf", b"one")
    store = LibraryCopyStore(pool)
    request = LibraryCopyRequest(direction="bb_to_sb", file_ids=[file.id])
    await _planned(store, request, bambuddy=bambuddy, native=native)
    store.record("library-copy-test", file.id, "copied", target_id=1000000007, sha256="ab" * 32)
    store.record("library-copy-test", file.id, "skipped_same_hash", target_id=1000000007)
    row = _rows(store)[file.id]
    assert (row.status, row.target_id) == ("copied", 1000000007)
    assert store.held("bb_to_sb", "ab" * 32) == [1000000007]
