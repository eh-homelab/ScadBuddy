"""The copy lease (#5298 Task 27): the lock one caller holds while it finds, uploads and
records a library copy (#317), as a row of ``copy_leases`` (migration
``20261110T1445Z_copy_leases``) instead of a session advisory lock, which a worker
without a database cannot take.

A holder names its lease by a token of its own and holds it until ``expires_at``; it
renews it every `COPY_LEASE_RENEW` seconds while its section runs (`hold_lease`) and
deletes it on the way out. A lease past its expiry is taken over by the next acquire, so
a holder that died frees the key within `COPY_LEASE_TTL`. Every call is natural on its
token: an acquire sent again by its holder answers it held, a renew or release sent again
renews or deletes what the first did.

The session advisory lock it replaced is gone (#5298 Task 35): no pod of a build before
the lease is left, so neither an acquire nor `uploads.py` `copy_lock` takes it.

`CopyLeases` is the API's SQL. The print worker's is `crud/print.py` `RemoteCopyLeases`,
over ``…/crud/print/copy-leases/{key}``; both are a `LeaseCalls`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Protocol

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, field_validator

logger = logging.getLogger(__name__)

#: How long a lease holds without a renew, in seconds.
COPY_LEASE_TTL = 120.0
#: How often a holder renews its lease, in seconds.
COPY_LEASE_RENEW = 30.0
#: The longest lease and the longest wait one acquire may ask for, in seconds. The wait
#: stays below the CRUD client's request timeout with room to spare.
MAX_TTL = 300.0
MAX_WAIT = 25.0
#: How often a waiting acquire tries again, in seconds.
ACQUIRE_POLL = 0.25


class CopyLease(BaseModel):
    """A lease held: its key and its holder's token, a UUID (the column's type)."""

    model_config = ConfigDict(frozen=True)

    key: str
    token: str

    @field_validator("token")
    @classmethod
    def _a_uuid(cls, token: str) -> str:
        return str(uuid.UUID(token))


class LeaseLostError(Exception):
    """The lease named is not held by its token: it expired and another holder took it,
    or it was released."""


class LeaseCalls(Protocol):
    async def acquire(self, key: str, token: str, ttl: float, wait: float) -> datetime | None:
        """The new expiry, or ``None`` when another holder kept it for all of ``wait``."""

    async def renew(self, key: str, token: str, ttl: float) -> datetime | None:
        """The new expiry, or ``None`` when ``token`` no longer holds ``key``."""

    async def release(self, key: str, token: str) -> None: ...


def held_in(conn: Connection[DictRow], lease: CopyLease) -> bool:
    """Whether ``lease`` holds, in the caller's transaction, locking the row so no
    acquire takes it over before that transaction ends."""
    row = conn.execute(
        "SELECT 1 FROM copy_leases WHERE key = %s AND token = %s AND expires_at > now() FOR SHARE",
        (lease.key, lease.token),
    ).fetchone()
    return row is not None


class CopyLeases:
    """``copy_leases`` on the API's pool. Each statement runs in a worker thread."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    async def acquire(self, key: str, token: str, ttl: float, wait: float) -> datetime | None:
        deadline = time.monotonic() + wait
        while True:
            expires = await asyncio.to_thread(self._try, key, token, ttl)
            left = deadline - time.monotonic()
            if expires is not None or left <= 0:
                return expires
            await asyncio.sleep(min(ACQUIRE_POLL, left))

    async def renew(self, key: str, token: str, ttl: float) -> datetime | None:
        return await asyncio.to_thread(self._renew, key, token, ttl)

    async def release(self, key: str, token: str) -> None:
        await asyncio.to_thread(self._release, key, token)

    async def held_by(self, key: str) -> str | None:
        """The token that holds ``key`` now, if any."""
        return await asyncio.to_thread(self._held_by, key)

    def _try(self, key: str, token: str, ttl: float) -> datetime | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "INSERT INTO copy_leases (key, token, expires_at)"
                " VALUES (%s, %s, now() + make_interval(secs => %s))"
                " ON CONFLICT (key) DO UPDATE"
                " SET token = EXCLUDED.token, expires_at = EXCLUDED.expires_at"
                " WHERE copy_leases.expires_at <= now() OR copy_leases.token = EXCLUDED.token"
                " RETURNING expires_at",
                (key, token, ttl),
            ).fetchone()
        return None if row is None else row["expires_at"]

    def _renew(self, key: str, token: str, ttl: float) -> datetime | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE copy_leases SET expires_at = now() + make_interval(secs => %s)"
                " WHERE key = %s AND token = %s RETURNING expires_at",
                (ttl, key, token),
            ).fetchone()
        return None if row is None else row["expires_at"]

    def _release(self, key: str, token: str) -> None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM copy_leases WHERE key = %s AND token = %s", (key, token))

    def _held_by(self, key: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT token FROM copy_leases WHERE key = %s AND expires_at > now()", (key,)
            ).fetchone()
        return None if row is None else str(row["token"])


async def _renewing(leases: LeaseCalls, lease: CopyLease, ttl: float, every: float) -> None:
    while True:
        await asyncio.sleep(every)
        try:
            renewed = await leases.renew(lease.key, lease.token, ttl)
        except Exception:
            logger.warning("could not renew a copy lease", exc_info=True)
            continue
        if renewed is None:
            # Taken over after it expired: the section's record refuses (`LeaseLostError`).
            logger.warning("a copy lease was lost", extra={"lease_key": lease.key})
            return


@contextlib.asynccontextmanager
async def hold_lease(
    leases: LeaseCalls,
    key: str,
    *,
    ttl: float = COPY_LEASE_TTL,
    renew_every: float = COPY_LEASE_RENEW,
) -> AsyncIterator[CopyLease]:
    """Hold ``key``'s lease for the section: wait for it as long as it takes, renew it
    every ``renew_every`` seconds, release it on the way out, however the section ends.
    A release that fails is logged; the lease then frees itself at its expiry.

    An acquire that is cancelled or fails may have been granted all the same (its answer
    lost on the way back), so it is released too: the token is this call's own, so the
    release deletes nothing another holder has."""
    lease = CopyLease(key=key, token=str(uuid.uuid4()))
    try:
        while await leases.acquire(key, lease.token, ttl, MAX_WAIT) is None:
            pass
    except BaseException:
        await _release_quietly(leases, lease)
        raise
    renewer = asyncio.create_task(_renewing(leases, lease, ttl, renew_every))
    try:
        yield lease
    finally:
        renewer.cancel()
        # `wait`, not `await renewer`: a cancel of this task still propagates.
        await asyncio.wait({renewer})
        await _release_quietly(leases, lease)


async def _release_quietly(leases: LeaseCalls, lease: CopyLease) -> None:
    """Release ``lease``, logging a failure rather than raising it over the error or
    cancellation that ends the section."""
    try:
        await leases.release(lease.key, lease.token)
    except Exception:
        logger.warning("could not release a copy lease", exc_info=True)
