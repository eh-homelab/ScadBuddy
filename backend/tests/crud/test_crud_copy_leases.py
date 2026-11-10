"""The print worker's copy lease through its CRUD routes (#5298 Task 27): the worker's
`RemoteUploadStore.copy_lock` and the API's exclude each other, a record under a lease
another holder took is 409 ``lease-lost`` and `LeaseLostError` on the worker, a held old
advisory lock makes the route's acquire busy, and the store holds no pool."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest

from scadbuddy.api.crud_areas import CRUD_AREAS
from scadbuddy.bambuddy.copy_leases import COPY_LOCK_PREFIX, CopyLease, CopyLeases, LeaseLostError
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy
from scadbuddy.core.settings import Settings
from scadbuddy.crud.client import CrudClient, CrudRefusedError
from scadbuddy.crud.print import BASE, ApiOnlyError, RemoteCopyLeases, RemoteUploadStore
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.worker import PRINT_CRUD_AREAS
from tests.conftest import UNUSED_TEMPORAL_ADDRESS
from tests.crud.crud_support import served_print_crud

pytestmark = pytest.mark.requires_postgres

KEY = "native:library:12:None"
OUTPUT = "a" * 32
URL = f"{BASE}/copy-leases/{KEY}"


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
def remote(crud: CrudClient) -> RemoteUploadStore:
    return RemoteUploadStore(crud)


def test_the_worker_waits_for_the_copy_leases_area() -> None:
    assert "copy-leases" in PRINT_CRUD_AREAS
    assert "copy-leases" in CRUD_AREAS["print"]


def test_the_worker_store_holds_no_pool(crud: CrudClient) -> None:
    with pytest.raises(ApiOnlyError):
        _ = RemoteUploadStore(crud).pool


async def test_the_workers_lease_and_the_apis_exclude_each_other(
    crud: CrudClient, remote: RemoteUploadStore, sql: BambuddyUploadStore
) -> None:
    entered, leave = asyncio.Event(), asyncio.Event()

    async def api_holds() -> None:
        async with sql.copy_lock(KEY):
            entered.set()
            await leave.wait()

    async with remote.copy_lock(KEY):
        api = asyncio.create_task(api_holds())
        await asyncio.sleep(1)
        assert not entered.is_set()
    await asyncio.wait_for(entered.wait(), 5)
    # The API holds it now: the worker's acquire waits.
    assert await RemoteCopyLeases(crud).acquire(KEY, str(uuid.uuid4()), 60, 0.3) is None
    leave.set()
    await api
    assert await CopyLeases(sql.pool).held_by(KEY) is None


async def test_a_record_under_a_lease_another_took_is_refused(
    remote: RemoteUploadStore, store: SettingsStore
) -> None:
    leases = CopyLeases(store.pool)
    stale = CopyLease(key=KEY, token=str(uuid.uuid4()))
    assert await leases.acquire(KEY, stale.token, 0.2, 0) is not None
    await asyncio.sleep(0.5)
    assert await leases.acquire(KEY, str(uuid.uuid4()), 60, 0) is not None

    with pytest.raises(LeaseLostError):
        await remote.record(OUTPUT, LibraryCopy(id=11, folder_id=None, target_key="H2D"), stale)
    assert await remote.for_output(OUTPUT) == []


async def test_a_record_under_the_workers_lease_is_written(remote: RemoteUploadStore) -> None:
    async with remote.copy_lock(KEY) as lease:
        await remote.record(OUTPUT, LibraryCopy(id=11, folder_id=None, target_key="H2D"), lease)
    assert [copy.id for copy in await remote.for_output(OUTPUT)] == [11]


async def test_the_routes_answer_busy_lost_and_natural(crud: CrudClient) -> None:
    holder, other = str(uuid.uuid4()), str(uuid.uuid4())
    held = await crud.request("POST", URL, json={"token": holder, "ttl_s": 60})
    assert held.json()["token"] == holder
    # Sent again by its holder: held again.
    await crud.request("POST", URL, json={"token": holder, "ttl_s": 60})

    with pytest.raises(CrudRefusedError) as busy:
        await crud.request("POST", URL, json={"token": other, "ttl_s": 60, "wait_s": 0.2})
    assert (busy.value.status, busy.value.problem.get("reason")) == (409, "busy")
    with pytest.raises(CrudRefusedError) as lost:
        await crud.request("PUT", URL, json={"token": other})
    assert (lost.value.status, lost.value.problem.get("reason")) == (409, "lease-lost")

    await crud.request("PUT", URL, json={"token": holder})
    await crud.request("DELETE", f"{URL}?token={holder}")
    await crud.request("DELETE", f"{URL}?token={holder}")
    await crud.request("POST", URL, json={"token": other, "ttl_s": 60})


@pytest.mark.parametrize(
    "body", [{"ttl_s": 301}, {"ttl_s": 60, "wait_s": 26}, {"ttl_s": 0}, {"ttl_s": 60, "token": "x"}]
)
async def test_the_acquire_bounds_its_lease_and_wait(
    crud: CrudClient, body: dict[str, object]
) -> None:
    with pytest.raises(CrudRefusedError) as refused:
        await crud.request("POST", URL, json={"token": str(uuid.uuid4()), **body})
    assert refused.value.status == 422


async def test_an_old_holder_of_the_advisory_lock_makes_the_acquire_busy(
    crud: CrudClient, pg_conninfo: str
) -> None:
    leases = RemoteCopyLeases(crud)
    with psycopg.connect(pg_conninfo, autocommit=True) as old_pod:
        old_pod.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))", (COPY_LOCK_PREFIX + KEY,)
        )
        assert await leases.acquire(KEY, str(uuid.uuid4()), 60, 0.3) is None
    assert await leases.acquire(KEY, str(uuid.uuid4()), 60, 0) is not None
