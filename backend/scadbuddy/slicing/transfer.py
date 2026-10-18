"""The `TransferUrl`s the slicer worker downloads its input and uploads its output by
(plan 2026-10-16-native-slicer-rust-worker Task 10, Ruling C3). The worker holds no
store credentials: a GET is S3's presigned URL of the object on `store_backend` s3, and
the internal blob route (`api/internal_blobs.py`) on any other backend; every PUT is
the internal route's, to a pending key the API stores the body under and records in
`transfer_puts`. A URL's header carries a bearer token: never log one."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict

from scadbuddy.slicing.models import TransferUrl
from scadbuddy.slicing.transfer_token import Method, TransferToken, sign
from scadbuddy.store.content import ContentStore
from scadbuddy.store.content_models import BlobMissingError
from scadbuddy.store.s3 import S3ContentBackend

TRANSFER_TTL = timedelta(hours=2)
TRANSFER_HEADER = "X-ScadBuddy-Transfer"
INTERNAL_BLOBS = "/api/v1/internal/blobs"
#: The internal PUT route takes only a key with this prefix: a token never writes a
#: stored key.
PENDING_PREFIX = "pending-"


class TransferUnavailableError(RuntimeError):
    """The API cannot mint an internal route's URL: no base URL, or no key to sign with."""


class TransferRecordError(RuntimeError):
    """`finish_put` of a pending key nothing was PUT to, or with a sha256 or size the
    PUT did not have."""


class PendingPut(BaseModel):
    #: Its URL's token stays out of a `ValidationError` (`TransferUrl` redacts its repr).
    model_config = ConfigDict(hide_input_in_errors=True)

    url: TransferUrl
    pending_key: str


class TransferPut(BaseModel):
    pending_key: str
    key: str
    sha256: str
    size: int
    created_at: datetime


class TransferPuts:
    """The `transfer_puts` table: what each pending key's PUT was stored as."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    def record(self, pending_key: str, *, key: str, sha256: str, size: int) -> None:
        """An upsert: a retried attempt PUTs to the same pending key again."""
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO transfer_puts (pending_key, key, sha256, size) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (pending_key) DO UPDATE SET key = EXCLUDED.key,"
                " sha256 = EXCLUDED.sha256, size = EXCLUDED.size, created_at = now()",
                (pending_key, key, sha256, size),
            )

    def get(self, pending_key: str) -> TransferPut | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT pending_key, key, sha256, size, created_at FROM transfer_puts"
                " WHERE pending_key = %s",
                (pending_key,),
            ).fetchone()
        return None if row is None else TransferPut.model_validate(row)

    def prune(self, older_than: timedelta) -> int:
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM transfer_puts WHERE created_at < now() - %s", (older_than,)
            )
            return cursor.rowcount


class Transfers:
    def __init__(
        self,
        content: ContentStore,
        puts: TransferPuts,
        *,
        internal_base_url: str | None,
        secret: bytes | None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """``secret`` is `transfer_token.transfer_key` of the KEK, None without one."""
        self.content = content
        self.puts = puts
        self.internal_base_url = internal_base_url.rstrip("/") if internal_base_url else None
        self.secret = secret
        self.now = now

    def _internal(self, method: Method, key: str) -> TransferUrl:
        if self.internal_base_url is None:
            raise TransferUnavailableError(
                "the native slicer needs SCADBUDDY_API_INTERNAL_URL: the worker reaches the"
                " API's internal blob routes there"
            )
        if self.secret is None:
            raise TransferUnavailableError(
                "the native slicer needs SCADBUDDY_SECRET_KEY_FILE: its transfer URLs are"
                " signed with a key derived from it"
            )
        # Whole seconds, as the token carries: the URL then says the instant the route
        # stops taking it, not up to a second after.
        expires_at = (self.now() + TRANSFER_TTL).replace(microsecond=0)
        token = TransferToken(method=method, blob_key=key, expires_at=expires_at)
        return TransferUrl(
            url=f"{self.internal_base_url}{INTERNAL_BLOBS}/{key}",
            headers={TRANSFER_HEADER: sign(self.secret, token)},
            expires_at=expires_at,
        )

    def internal_get(self, key: str) -> TransferUrl:
        """The internal GET route's URL of ``key``, whether or not it is stored, on any
        backend. Public for the route's tests, which run on moto's S3, where `mint_get`
        presigns instead. A caller that needs ``key`` stored (a `BlobMissingError` now,
        not a 404 from the worker later) uses `mint_get`."""
        return self._internal("GET", key)

    async def mint_get(self, key: str) -> TransferUrl:
        """A URL that reads the stored ``key``: `BlobMissingError` if it is not."""
        stat = await self.content.stat(key)
        if stat is None:
            raise BlobMissingError(key)
        backend = self.content.backend
        if isinstance(backend, S3ContentBackend):
            # botocore signs with its own clock (X-Amz-Date, whole seconds), which an
            # injected ``now`` cannot move; read before the presign and floored, this
            # ``expires_at`` is never later than the URL's real expiry on the real clock.
            expires_at = (self.now() + TRANSFER_TTL).replace(microsecond=0)
            url = await backend.presign_get(stat.ref.backend_id, TRANSFER_TTL)
            return TransferUrl(url=url, expires_at=expires_at)
        return self.internal_get(key)

    def mint_put(self) -> PendingPut:
        """The internal PUT route of a new pending key, on every backend."""
        pending_key = f"{PENDING_PREFIX}{uuid.uuid4().hex}"
        return PendingPut(url=self._internal("PUT", pending_key), pending_key=pending_key)

    async def finish_put(self, put: PendingPut, *, sha256: str, size: int) -> str:
        """The key the PUT to ``put`` was stored under, once the worker's reported
        sha256 and size are the ones the route stored and the key is stored still (the
        `slice` kind is swept, and nothing holds it until the caller's `BlobRefs.add`)."""
        row = await asyncio.to_thread(self.puts.get, put.pending_key)
        if row is None:
            raise TransferRecordError(f"nothing was PUT to {put.pending_key}")
        if row.sha256 != sha256 or row.size != size:
            raise TransferRecordError(
                f"{put.pending_key} was PUT as {row.size} bytes of sha256 {row.sha256},"
                f" not {size} bytes of {sha256}"
            )
        if await self.content.stat(row.key) is None:
            raise TransferRecordError(f"{put.pending_key}'s {row.key} is no longer stored")
        return row.key
