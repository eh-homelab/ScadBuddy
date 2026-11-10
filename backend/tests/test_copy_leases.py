"""The copy lease (#5298 Task 27, `bambuddy/copy_leases.py`) on the API's SQL: two
holders exclude each other, an expired lease is taken over, a record under a lease
another holder took is refused, a renewed lease outlives its TTL, and a holder of the
old session advisory lock makes an acquire busy."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.copy_leases import (
    COPY_LOCK_PREFIX,
    CopyLease,
    CopyLeases,
    LeaseLostError,
    hold_lease,
)
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy

pytestmark = pytest.mark.requires_postgres

KEY = "native:output:abc:7"
OUTPUT = "a" * 32


def token() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def leases(pg_pool: ConnectionPool[Connection[DictRow]]) -> CopyLeases:
    return CopyLeases(pg_pool)


@pytest.fixture
def uploads(pg_pool: ConnectionPool[Connection[DictRow]]) -> BambuddyUploadStore:
    return BambuddyUploadStore(pg_pool)


@pytest.fixture
def old_pod(pg_conninfo: str) -> Iterator[Connection[DictRow]]:
    """A connection of its own, as an old print pod's `copy_lock` takes."""
    with psycopg.connect(pg_conninfo, autocommit=True, row_factory=dict_row) as conn:
        yield conn


async def test_two_holders_exclude_each_other(leases: CopyLeases) -> None:
    async with hold_lease(leases, KEY) as first:
        assert await leases.acquire(KEY, token(), 60, 0.3) is None
        assert await leases.held_by(KEY) == first.token
    assert await leases.held_by(KEY) is None
    assert await leases.acquire(KEY, token(), 60, 0) is not None


async def test_a_waiting_acquire_takes_the_lease_once_released(leases: CopyLeases) -> None:
    holder = token()
    assert await leases.acquire(KEY, holder, 60, 0) is not None
    waiting = asyncio.create_task(leases.acquire(KEY, token(), 60, 5))
    await asyncio.sleep(0.3)
    assert not waiting.done()
    await leases.release(KEY, holder)
    assert await waiting is not None


async def test_an_acquire_sent_again_by_its_holder_answers_it_held(leases: CopyLeases) -> None:
    holder = token()
    assert await leases.acquire(KEY, holder, 60, 0) is not None
    assert await leases.acquire(KEY, holder, 60, 0) is not None
    assert await leases.held_by(KEY) == holder


async def test_an_expired_lease_is_taken_over(leases: CopyLeases) -> None:
    dead = token()
    assert await leases.acquire(KEY, dead, 0.2, 0) is not None
    await asyncio.sleep(0.5)
    assert await leases.held_by(KEY) is None
    taker = token()
    assert await leases.acquire(KEY, taker, 60, 0) is not None
    assert await leases.held_by(KEY) == taker
    # The dead holder's renew and release change nothing.
    assert await leases.renew(KEY, dead, 60) is None
    await leases.release(KEY, dead)
    assert await leases.held_by(KEY) == taker


async def test_a_record_with_a_stale_token_is_refused(
    leases: CopyLeases, uploads: BambuddyUploadStore
) -> None:
    stale = CopyLease(key=KEY, token=token())
    assert await leases.acquire(KEY, stale.token, 0.2, 0) is not None
    await asyncio.sleep(0.5)
    assert await leases.acquire(KEY, token(), 60, 0) is not None

    with pytest.raises(LeaseLostError):
        await uploads.record(OUTPUT, LibraryCopy(id=11, folder_id=7, target_key="H2D"), stale)
    assert await uploads.for_output(OUTPUT) == []


async def test_a_record_under_its_held_lease_is_written(uploads: BambuddyUploadStore) -> None:
    async with uploads.copy_lock(KEY) as lease:
        await uploads.record(OUTPUT, LibraryCopy(id=11, folder_id=7, target_key="H2D"), lease)
    assert [copy.id for copy in await uploads.for_output(OUTPUT)] == [11]


async def test_renew_keeps_a_long_upload_held(leases: CopyLeases) -> None:
    """A 2-minute upload under the 120 s TTL renewed every 30 s, at a hundredth of the
    scale: held for 2.5 s on a 1 s TTL renewed every 0.25 s."""
    async with hold_lease(leases, KEY, ttl=1.0, renew_every=0.25) as lease:
        await asyncio.sleep(2.5)
        assert await leases.held_by(KEY) == lease.token
        assert await leases.acquire(KEY, token(), 60, 0) is None


async def test_an_old_holder_of_the_advisory_lock_makes_acquire_busy(
    leases: CopyLeases, old_pod: Connection[DictRow]
) -> None:
    old_pod.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (COPY_LOCK_PREFIX + KEY,))
    assert await leases.acquire(KEY, token(), 60, 0.3) is None
    old_pod.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (COPY_LOCK_PREFIX + KEY,))
    assert await leases.acquire(KEY, token(), 60, 0) is not None


async def test_the_apis_copy_lock_also_holds_the_advisory_lock(
    uploads: BambuddyUploadStore, old_pod: Connection[DictRow]
) -> None:
    """So an old print pod, which takes only that lock, waits for an API holder."""

    def old_pod_gets_it() -> bool:
        row = old_pod.execute(
            "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS got",
            (COPY_LOCK_PREFIX + KEY,),
        ).fetchone()
        return bool(row is not None and row["got"])

    async with uploads.copy_lock(KEY):
        assert not old_pod_gets_it()
    assert old_pod_gets_it()
