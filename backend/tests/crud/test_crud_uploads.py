"""The print worker's uploads, slices and slice starts through its CRUD routes (#5298
Task 25, inventory Q15, Q17): `RemoteUploadStore` answers as `BambuddyUploadStore` does,
every natural write sent twice leaves what one left, a slice start is recorded once per
``Idempotency-Key``, and the methods only the API calls are refused on the worker."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy, SlicedCopy
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
#: The methods the print worker still runs on its pool until #5298 Task 26 (Q16, Q18),
#: and the copy lock until Task 27 (Q19).
STILL_SQL = {
    "copy_lock",
    "record_preview",
    "preview",
    "previews_for",
    "preview_sliced",
    "retire_preview",
    "claim_preview",
    "previews_to_sweep",
    "take_preview",
    "forget_preview",
    "project_target",
    "remember_project_target",
    "project_targets",
    "forget_project_target",
    "forget_all_project_targets",
    "folderless",
    "remember_folderless",
    "forget_folderless",
    "touch_destination",
    "destination_touches",
}


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
    return RemoteUploadStore(crud, store.pool)


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
            ("POST", f"{BASE}/uploads/forget-files"),
            ("POST", f"{BASE}/slices"),
            ("PUT", f"{BASE}/slices/1/hash"),
            ("POST", f"{BASE}/slices/1/gone"),
            ("POST", f"{BASE}/slice-starts"),
        ):
            with pytest.raises(CrudRefusedError) as refused:
                client.request(method, path, json={})
            assert refused.value.status == 403, path
            assert refused.value.problem["reason"] == "wrong_caller", path
