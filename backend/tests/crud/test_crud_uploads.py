"""The print worker's uploads, slices, slice starts (#5298 Task 25, inventory Q15, Q17),
background slices, project targets, folderless projects and destination touches (Task
26, Q16, Q18) through its CRUD routes: `RemoteUploadStore` answers as
`BambuddyUploadStore` does, every natural write sent twice leaves what one left, a slice
start and a background slice are recorded once per ``Idempotency-Key``, a claim won
answers true again to its resend, and the methods only the API calls are refused on the
worker."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.bambuddy.uploads import (
    BambuddyUploadStore,
    LibraryCopy,
    PreviewSlice,
    ProjectTarget,
    SlicedCopy,
)
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudClient, CrudRefusedError
from scadbuddy.crud.idempotency import KEY_PROBLEM, KEY_REQUIRED_PROBLEM
from scadbuddy.crud.print import API_ONLY, BASE, ApiOnlyError, RemoteUploadStore
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.print_library.ids import NATIVE_ID_BASE
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import served_print_crud, threaded_print_crud

pytestmark = pytest.mark.requires_postgres

OUTPUT = "a" * 32
OTHER = "b" * 32
#: What the print worker still runs on SQL: nothing, since the copy lease (#5298 Task 27).
STILL_SQL: set[str] = set()


def copy(
    file_id: int, folder_id: int | None = 2, target_key: str = "H2D", **more: Any
) -> LibraryCopy:
    return LibraryCopy(id=file_id, folder_id=folder_id, target_key=target_key, **more)


@pytest.fixture
def store(tmp_path: Path, pg_conninfo: str) -> Iterator[SettingsStore]:
    opened = SettingsStore(
        Settings(
            data_dir=tmp_path,
            database_url=pg_conninfo,
            temporal_address=UNUSED_TEMPORAL_ADDRESS,
            auth_mode="enforce",
        )
    )
    opened.open()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def sql(store: SettingsStore) -> BambuddyUploadStore:
    """The API's own store, on the same schema."""
    return BambuddyUploadStore(store.pool)


@pytest.fixture
async def crud(store: SettingsStore) -> AsyncIterator[CrudClient]:
    async with served_print_crud(store) as client:
        yield client


@pytest.fixture
def remote(crud: CrudClient, store: SettingsStore) -> RemoteUploadStore:
    return RemoteUploadStore(crud)


def _rows(store: SettingsStore, table: str) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        return conn.execute(f"SELECT * FROM {table}").fetchall()


def test_the_worker_waits_for_the_uploads_area() -> None:
    assert "uploads" in PRINT_CRUD_AREAS


def test_every_method_is_remote_refused_or_named() -> None:
    """No `BambuddyUploadStore` method reaches the worker's SQL unnoticed: each is
    overridden over the routes, refused as the API's, or still on SQL by name."""
    public = {
        name
        for name, member in inspect.getmembers(BambuddyUploadStore, inspect.isfunction)
        if not name.startswith("_") and not name.endswith("_in")
    }
    remote = {name for name in public if name in vars(RemoteUploadStore)}
    assert set(API_ONLY) <= remote
    assert public - remote == STILL_SQL


async def test_the_reads_answer_as_the_sql_store(
    remote: RemoteUploadStore, sql: BambuddyUploadStore
) -> None:
    await remote.record(OUTPUT, copy(11, folder_id=9, content_key="c1"))
    await remote.record(OUTPUT, copy(12, folder_id=None, target_key="A1"))
    await remote.record(OTHER, copy(11, folder_id=9, content_key="c1"))
    await remote.record(OUTPUT, copy(NATIVE_ID_BASE + 1))
    await remote.record_sliced(OUTPUT, 11, SlicedCopy(id=21, preset_key="p"))
    await remote.record_slice_hash(OUTPUT, 21, "h21")

    assert await remote.for_output(OUTPUT) == await sql.for_output(OUTPUT)
    assert (await remote.for_output(OUTPUT))[0].sliced == [
        SlicedCopy(id=21, preset_key="p", file_hash="h21")
    ]
    assert await remote.for_output(OUTPUT, native=True) == await sql.for_output(OUTPUT, native=True)
    assert await remote.for_outputs([OUTPUT, OTHER, "c" * 32]) == await sql.for_outputs(
        [OUTPUT, OTHER, "c" * 32]
    )
    assert await remote.for_outputs([]) == {}
    assert await remote.same_content(9, "H2D", "c1") == await sql.same_content(9, "H2D", "c1")
    assert len(await remote.same_content(9, "H2D", "c1")) == 2
    assert await remote.same_content(None, "A1", "c1") == []
    assert await remote.shared([11, 12], OUTPUT) == await sql.shared([11, 12], OUTPUT) == {11}
    assert await remote.shared([], OUTPUT) == set()
    assert await remote.recorded([11, 12, 99]) == await sql.recorded([11, 12, 99]) == {11, 12}
    assert await remote.recorded([]) == set()
    assert await remote.recorded_files([11, 21, 99]) == await sql.recorded_files([11, 21, 99])
    assert await remote.recorded_files([11, 21, 99]) == {11, 21}
    assert await remote.recorded_files([]) == set()
    assert await remote.slices_of(OUTPUT, 11, "p") == await sql.slices_of(OUTPUT, 11, "p")
    assert await remote.slices_of(OUTPUT, 11, "other") == []
    assert await remote.sent_between(OUTPUT) == await sql.sent_between(OUTPUT)
    assert await remote.sent_between(OUTPUT) is not None
    assert await remote.sent_between(OTHER) is None


async def test_each_write_sent_twice_leaves_what_one_left(
    remote: RemoteUploadStore, store: SettingsStore
) -> None:
    """Natural writes (plan D5): a resend after a lost answer changes nothing."""

    async def twice(write: Any) -> list[dict[str, Any]]:
        await write()
        once = _rows(store, "output_bambuddy_uploads"), _rows(store, "output_bambuddy_slices")
        await write()
        again = _rows(store, "output_bambuddy_uploads"), _rows(store, "output_bambuddy_slices")
        # `touch` sets `used_at` to the time of the write: compare all else.
        for before, after in zip(once, again, strict=True):
            assert [{**r, "used_at": None} for r in before] == [
                {**r, "used_at": None} for r in after
            ]
        return again[0]

    rows = await twice(lambda: remote.record(OUTPUT, copy(11, content_key="c1")))
    assert len(rows) == 1
    await twice(lambda: remote.record(OUTPUT, copy(12)))
    await twice(lambda: remote.record_sliced(OUTPUT, 11, SlicedCopy(id=21, preset_key="p")))
    await twice(lambda: remote.record_sliced(OUTPUT, 12, SlicedCopy(id=22, preset_key="p")))
    await twice(lambda: remote.record_sliced(OUTPUT, 12, SlicedCopy(id=23, preset_key="p")))
    await twice(lambda: remote.record_slice_hash(OUTPUT, 21, "h"))
    rows = await twice(lambda: remote.touch(OUTPUT, 11))
    assert next(row for row in rows if row["library_file_id"] == 11)["used_at"] is not None
    await twice(lambda: remote.forget_slice(OUTPUT, 22))
    await twice(lambda: remote.forget_gone_slice(23))
    assert {row["sliced_library_file_id"] for row in _rows(store, "output_bambuddy_slices")} == {21}
    await twice(lambda: remote.forget(OUTPUT, 12))
    rows = await twice(lambda: remote.forget_files([11]))
    assert rows == [] and _rows(store, "output_bambuddy_slices") == []
    await remote.forget_files([])


async def test_a_slice_start_is_recorded_once_per_key(
    remote: RemoteUploadStore, crud: CrudClient, store: SettingsStore
) -> None:
    await remote.record_slice_start(11, 5)
    await remote.record_slice_start(11, None)
    assert sorted(
        (r["library_file_id"], r["job_id"] or 0) for r in _rows(store, "print_slice_starts")
    ) == [
        (11, 0),
        (11, 5),
    ]
    # A resend under its key, as the client sends one after a lost answer.
    body = {"library_file_id": 12, "job_id": None}
    for _ in range(2):
        await crud.request("POST", f"{BASE}/slice-starts", json=body, idempotency_key="k1")
    assert [r["library_file_id"] for r in _rows(store, "print_slice_starts")].count(12) == 1
    with pytest.raises(CrudRefusedError) as reused:
        await crud.request(
            "POST", f"{BASE}/slice-starts", json={**body, "job_id": 6}, idempotency_key="k1"
        )
    assert reused.value.status == 422 and reused.value.problem["type"] == KEY_PROBLEM
    with pytest.raises(CrudRefusedError) as keyless:
        await crud.request("POST", f"{BASE}/slice-starts", json=body)
    assert keyless.value.status == 428 and keyless.value.problem["type"] == KEY_REQUIRED_PROBLEM
    assert len(_rows(store, "print_slice_starts")) == 3


@pytest.mark.parametrize("name", API_ONLY)
def test_a_method_only_the_api_calls_is_refused(remote: RemoteUploadStore, name: str) -> None:
    with pytest.raises(ApiOnlyError):
        getattr(remote, name)([])


def test_another_callers_credential_is_refused(store: SettingsStore) -> None:
    with threaded_print_crud(store, caller="projects") as client:
        for method, path in (
            ("GET", f"{BASE}/uploads?output_id=x"),
            ("POST", f"{BASE}/uploads"),
            ("GET", f"{BASE}/uploads/recorded?id=1"),
            ("GET", f"{BASE}/uploads/recorded-files?id=1"),
            ("POST", f"{BASE}/uploads/forget-files"),
            ("POST", f"{BASE}/slices"),
            ("PUT", f"{BASE}/slices/1/hash"),
            ("POST", f"{BASE}/slices/1/gone"),
            ("POST", f"{BASE}/slice-starts"),
            ("POST", f"{BASE}/previews"),
            ("GET", f"{BASE}/previews/find?job_id=1&subject=x"),
            ("POST", f"{BASE}/previews/1/claim"),
            ("POST", f"{BASE}/previews/1/retire"),
            ("PUT", f"{BASE}/project-targets/1"),
            ("GET", f"{BASE}/folderless?name=x"),
            ("DELETE", f"{BASE}/folderless/1"),
            ("POST", f"{BASE}/destination-touches"),
        ):
            with pytest.raises(CrudRefusedError) as refused:
                client.request(method, path, json={})
            assert refused.value.status == 403, path
            assert refused.value.problem["reason"] == "wrong_caller", path


def preview(job_id: int, subject: str = OUTPUT, library_file_id: int = 11) -> PreviewSlice:
    return PreviewSlice(
        job_id=job_id,
        subject=subject,
        library_file_id=library_file_id,
        preset_key="p",
        plate_id=1,
        job_created="2026-11-07T10:00:00",
    )


async def test_background_slices_answer_as_the_sql_store(
    remote: RemoteUploadStore, sql: BambuddyUploadStore
) -> None:
    await remote.record_preview(preview(5))
    await remote.record_preview(preview(6))
    await remote.record_preview(preview(7, subject=OTHER, library_file_id=12))
    found = await remote.preview(5, OUTPUT)
    assert found is not None and found == await sql.preview(5, OUTPUT)
    assert found.id is not None and found.job_created == "2026-11-07T10:00:00"
    assert await remote.preview(5, OTHER) is None
    assert await remote.previews_for(11, "p") == await sql.previews_for(11, "p")
    assert [row.job_id for row in await remote.previews_for(11, "p")] == [6, 5]
    for _ in range(2):
        await remote.preview_sliced(found.id, 31, "plate.gcode.3mf")
    sliced = await remote.preview(5, OUTPUT)
    assert sliced is not None and (sliced.sliced_file_id, sliced.sliced_name) == (
        31,
        "plate.gcode.3mf",
    )
    assert await remote.claim_preview(found.id)
    claimed = await sql.preview(5, OUTPUT)
    assert claimed is not None and claimed.printed
    for _ in range(2):
        await remote.retire_preview(found.id)
    assert await remote.preview(5, OUTPUT) is None
    assert [row.job_id for row in await remote.previews_for(11, "p")] == [6]


async def test_a_background_slice_is_recorded_once_per_key(
    crud: CrudClient, store: SettingsStore
) -> None:
    body = preview(5).model_dump(mode="json")
    for _ in range(2):
        await crud.request("POST", f"{BASE}/previews", json=body, idempotency_key="p1")
    assert len(_rows(store, "print_preview_slices")) == 1
    with pytest.raises(CrudRefusedError) as keyless:
        await crud.request("POST", f"{BASE}/previews", json=body)
    assert keyless.value.status == 428
    assert len(_rows(store, "print_preview_slices")) == 1


async def test_a_claim_retried_with_its_key_answers_true_once_won(
    remote: RemoteUploadStore,
    crud: CrudClient,
    sql: BambuddyUploadStore,
    store: SettingsStore,
) -> None:
    """A lost answer must not turn a won claim into a lost one: the slice retired after
    the claim, its resend under the same key still answers true."""
    await sql.record_preview(preview(5))
    row = await sql.preview(5, OUTPUT)
    assert row is not None and row.id is not None
    url = f"{BASE}/previews/{row.id}/claim"
    first = await crud.request("POST", url, idempotency_key="c1")
    assert first.json() == {"claimed": True}
    await sql.retire_preview(row.id)
    again = await crud.request("POST", url, idempotency_key="c1")
    assert again.json() == {"claimed": True}
    # A new claim is new: the sweep has the slice now.
    other = await crud.request("POST", url, idempotency_key="c2")
    assert other.json() == {"claimed": False}
    with pytest.raises(CrudRefusedError) as keyless:
        await crud.request("POST", url)
    assert keyless.value.status == 428


async def test_a_retried_activity_claims_under_the_same_key(
    crud: CrudClient, sql: BambuddyUploadStore, store: SettingsStore
) -> None:
    """Inside an activity a claim's key is the activity's (`WriteKeys`): its retry, on
    a fresh store, answers what the attempt that lost its answer won."""
    await sql.record_preview(preview(5))
    await sql.record_preview(preview(6))
    first, second = (await sql.previews_for(11, "p"))[::-1]
    assert first.id is not None and second.id is not None

    async def claim(row_id: int) -> bool:
        return await RemoteUploadStore(crud).claim_preview(row_id)

    assert await ActivityEnvironment().run(claim, first.id)
    await sql.retire_preview(first.id)
    assert await ActivityEnvironment().run(claim, first.id)
    # The retry claims another slice under that ordinal: a claim of its own.
    assert await ActivityEnvironment().run(claim, second.id)
    claimed = await sql.preview(6, OUTPUT)
    assert claimed is not None and claimed.printed


async def test_project_records_answer_as_the_sql_store(
    remote: RemoteUploadStore, sql: BambuddyUploadStore, store: SettingsStore
) -> None:
    target = ProjectTarget(printer_id=3, nozzle_diameter="0.4", nozzle_plan={"left": "0.4"})
    assert await remote.project_target(8) is None
    for _ in range(2):
        await remote.remember_project_target(8, target)
    assert await remote.project_target(8) == await sql.project_target(8) == target
    assert len(_rows(store, "project_print_targets")) == 1

    for _ in range(2):
        await remote.remember_folderless(41, "Keychains", None)
        await remote.remember_folderless(42, "Keychains", 7)
        await remote.remember_folderless(43, "Keychains", None)
    assert await remote.folderless("Keychains", None) == await sql.folderless("Keychains", None)
    assert set(await remote.folderless("Keychains", None)) == {41, 43}
    assert await remote.folderless("Keychains", 7) == [42]
    for _ in range(2):
        await remote.forget_folderless(41)
    assert await remote.folderless("Keychains", None) == [43]

    for _ in range(2):
        await remote.touch_destination("project", 8)
        await remote.touch_destination("folder", 8)
    assert set(await sql.destination_touches()) == {("project", 8), ("folder", 8)}
