"""`store_blobs`: which blob a key names, where its bytes are, when it was last wanted."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from datetime import datetime
from typing import Any, Protocol

from psycopg import Connection
from psycopg.errors import DeadlockDetected, LockNotAvailable
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.store.content_models import BlobRef, BlobStat, ReuseLostError

logger = logging.getLogger(__name__)
Pool = ConnectionPool[Connection[DictRow]]
_COLUMNS = "key, sha256, kind, backend, backend_id, size, slug, meta, touched_at"
#: A write that points a row at another object drops its migrated copy, now stale, and
#: the error of a copy that failed (spec 2026-10-16 §4.3), and the API's check of its
#: bytes (`verified_at`, #6233); one that names the same object keeps them. Its
#: parameters are `_kept_copy`'s.
_MIGRATED = ("migrated_backend", "migrated_backend_id", "migrated_error", "migrated_error_at")
_KEPT_WITH_OBJECT = (*_MIGRATED, "verified_at")
_KEEP_COPY = ", ".join(
    f"{column} = CASE WHEN (sha256, backend, backend_id) = (%s, %s, %s) THEN {column} END"
    for column in _KEPT_WITH_OBJECT
)
#: Attempts at an index write that Postgres picks as a deadlock's victim (`retrying`).
_DEADLOCK_ATTEMPTS = 3
#: `BlobIndex.copied_refs`: read in order from `store_blobs_backend_migrated_touched`
#: (#4117), not sorted.
COPIED_REFS_SQL = (
    f"SELECT {_COLUMNS} FROM store_blobs WHERE backend = %s AND migrated_backend = %s"
    " ORDER BY touched_at DESC LIMIT %s"
)


#: How many saves may be in their upload phase at once on one pod, each with one
#: connection of the API's stage-lock pool (`StageLocks`, #7477 review I2).
STAGE_LOCK_SLOTS = 3
#: The stage-lock pool's connections kept for removals' exclusive locks
#: (`StageLocks.claim_connection`, #7756 review): a removal holds one through the
#: backend's remove, a round trip, so it is never the API's shared pool's.
CLAIM_LOCK_SLOTS = 1
#: The size of the stage-lock pool (`scadbuddy-stage-locks`): every save slot and every
#: removal slot has a connection of its own, so neither waits for the other's.
STAGE_LOCK_POOL_SIZE = STAGE_LOCK_SLOTS + CLAIM_LOCK_SLOTS


def _drop_or_discard(conn: Connection[DictRow], query: str, params: tuple[str, ...]) -> None:
    """Run ``query``, an advisory unlock. When it fails, close ``conn``: a pool discards a
    closed connection, and a closed session holds no lock. Given back alive, it would keep
    its locks for its next user (#7477 review M2)."""
    try:
        conn.execute(query, params).fetchone()
    except Exception:
        logger.warning("could not drop an object's advisory lock", exc_info=True)
        conn.close()


#: How long a stage hold waits for one object's lock, a try, while a removal's claim
#: holds it (#7756 review): a claim lasts one backend remove, so a hung remove must not
#: pin a save's slot. `StageHold.take` tries `STAGE_LOCK_TRIES` times, then gives up.
STAGE_LOCK_TIMEOUT_MS = 5000
STAGE_LOCK_TRIES = 6


class StageLockBusyError(Exception):
    """A removal held an object's lock for every try of `StageHold.take`: the save fails,
    and may be sent again."""


def _lock_shared(conn: Connection[DictRow], name: str) -> None:
    """Take the session lock ``name`` shared on ``conn``, waiting `STAGE_LOCK_TIMEOUT_MS`
    a try, `STAGE_LOCK_TRIES` tries. Each try is a transaction whose ``SET LOCAL`` ends
    with it, so the pooled connection keeps no timeout; the session lock outlives the
    commit."""
    for _ in range(STAGE_LOCK_TRIES):
        try:
            with conn.transaction():
                conn.execute(
                    "SELECT set_config('lock_timeout', %s, true)", (f"{STAGE_LOCK_TIMEOUT_MS}ms",)
                )
                conn.execute(
                    "SELECT pg_advisory_lock_shared(hashtextextended(%s, 0))", (name,)
                ).fetchone()
            return
        except LockNotAvailable:
            logger.info("an object's stage lock is claimed by a removal; trying again")
    raise StageLockBusyError(f"a removal held {name} for {STAGE_LOCK_TRIES} tries")


def object_lock_name(backend: str, sha256: str) -> str:
    """The text whose ``hashtextextended(…, 0)`` keys the session advisory lock of the
    objects of ``sha256`` on ``backend`` (#7477). A stager holds it shared from before its
    upload until its rows commit (`StageHold`); a release removes such an object only
    while it holds it exclusively (`BlobIndex.claim_unnamed`). The kind is left out, so
    two kinds of the same bytes share one lock: that only makes a release wait for a
    later pass. Content-addressed backends (S3, local) name the object by its sha256."""
    return f"blob-object:{backend}:{sha256}"


class StageHold:
    """The shared locks a stager holds on the objects it uploaded (#7477), on one pooled
    connection of its own (`StageLocks`' pool), from before each upload until `close`. A
    session lock, so no transaction stays open and nothing commits. A process that dies
    drops its connection and so its locks. Blocking; `close` is idempotent and
    thread-safe."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool
        self._conn: Connection[DictRow] | None = None
        self._held: set[str] = set()
        self._closed = False
        self._lock = threading.Lock()

    def take(self, backend: str, sha256: str) -> None:
        """Hold the objects of ``sha256`` on ``backend`` shared; it waits only while a
        release holds them (`BlobIndex.claim_unnamed`), which never waits itself, and at
        most `STAGE_LOCK_TRIES` tries of `STAGE_LOCK_TIMEOUT_MS`: then
        `StageLockBusyError`, and the caller closes the hold as on any failure."""
        name = object_lock_name(backend, sha256)
        with self._lock:
            if self._closed:
                raise RuntimeError("this stage hold is closed")
            if name in self._held:
                return
            if self._conn is None:
                self._conn = self._pool.getconn()
            _lock_shared(self._conn, name)
            self._held.add(name)

    def close(self) -> None:
        """Drop every lock and give the connection back."""
        with self._lock:
            self._closed = True
            conn, self._conn = self._conn, None
            self._held.clear()
        if conn is None:
            return
        try:
            _drop_or_discard(conn, "SELECT pg_advisory_unlock_all()", ())
        finally:
            self._pool.putconn(conn)


class StageLocks:
    """Where stage holds and removals take their lock connection, and how many of each
    may hold one at once (#7477 review I2, #7756 review). The pool is small and of its
    own, not the API's shared one: a save holds its connection from its first upload to
    its commit and meanwhile needs the shared pool for its index reads and its rows, and
    a removal holds its exclusive lock through the backend's remove, so locks taken there
    could take every connection and stall every route. The pool holds ``slots`` plus
    ``claim_slots`` connections: a save waits for one of ``slots`` before its first
    upload (`hold`), and a removal for one of ``claim_slots`` (`claim_connection`), so
    each one's connection is always free when its turn comes, and a save never waits
    mid-save for a second one. A removal takes its lock without waiting and needs no
    other connection, so a removal slot is never held for longer than one remove."""

    def __init__(self, pool: Pool, slots: int, claim_slots: int = CLAIM_LOCK_SLOTS) -> None:
        self.pool = pool
        self.slots = slots
        self._free = asyncio.Semaphore(slots)
        self._claims = threading.BoundedSemaphore(claim_slots)

    @contextlib.contextmanager
    def claim_connection(self) -> Iterator[Connection[DictRow]]:
        """A connection for a removal's exclusive lock, once a removal slot is free.
        Blocking."""
        with self._claims, self.pool.connection() as conn:
            yield conn

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncIterator[StageHold]:
        """A `StageHold`, once a slot is free; closed, and its slot freed, at the end."""
        async with self._free:
            stage = StageHold(self.pool)
            try:
                yield stage
            finally:
                # Shielded: a cancel must not leave the locks held on a pooled connection.
                await asyncio.shield(asyncio.to_thread(stage.close))


def _kept_copy(ref: BlobRef) -> tuple[str, ...]:
    return (ref.sha256, ref.backend, ref.backend_id) * len(_KEPT_WITH_OBJECT)


def _stat(row: DictRow) -> BlobStat:
    return BlobStat(
        key=row["key"],
        ref=BlobRef(
            sha256=row["sha256"],
            kind=row["kind"],
            backend=row["backend"],
            backend_id=row["backend_id"],
            size=row["size"],
        ),
        slug=row["slug"],
        meta=row["meta"],
        touched_at=row["touched_at"],
    )


class IndexesBlobs(Protocol):
    """What `ContentStore` and its callers ask of the blob index: `BlobIndex` over SQL
    in the API, `crud/render.py` `RemoteBlobIndex` over the API's routes in the render
    worker (#5298). Each answers as `BlobIndex` does."""

    def get(self, key: str) -> BlobStat | None: ...
    def by_sha(self, sha256: str, kind: str, backend: str) -> BlobRef | None: ...
    def put(
        self,
        key: str,
        ref: BlobRef,
        *,
        slug: str | None,
        meta: dict[str, Any],
        reuse: bool = False,
    ) -> BlobRef | None: ...
    def swap(
        self,
        key: str,
        ref: BlobRef,
        *,
        expected: str | None,
        slug: str | None,
        meta: dict[str, Any],
        reuse: bool = False,
    ) -> bool: ...
    def now(self) -> datetime: ...
    def mark(self, keys: Sequence[str], *, backend: str, cutoff: datetime, **meta: Any) -> None: ...
    def touch(self, key: str) -> None: ...
    def delete(self, key: str, *, backend: str | None = None) -> BlobStat | None: ...
    def delete_if_stale(
        self, key: str, cutoff: datetime, *, backend: str | None = None
    ) -> BlobStat | None: ...
    def shares_backend_id(self, backend: str, backend_id: str) -> bool: ...
    def recorded(self, backend: str, backend_id: str) -> bool: ...
    def stats(
        self,
        kinds: Sequence[str] | None,
        slug: str | None = None,
        *,
        backend: str | None = None,
    ) -> list[BlobStat]: ...
    def usage(self, backend: str) -> tuple[int, int, dict[str, int]]: ...


class BlobIndex:
    """Each write has a ``*_in`` form that runs on the caller's connection, in its
    transaction when it has one (its own transactions are then savepoints), so the API's
    CRUD route commits its idempotency ledger row with the write (#5298, plan D5);
    `retrying` runs either form again when Postgres picks it as a deadlock's victim."""

    def __init__(self, pool: Pool, *, stage_locks: StageLocks | None = None) -> None:
        """``stage_locks``: where `stage_hold` and `claim_unnamed` take their lock
        connections. The API passes its own small pool (`SettingsStore.stage_lock_pool`);
        without it they come from ``pool``, bounded to `STAGE_LOCK_SLOTS` saves and
        `CLAIM_LOCK_SLOTS` removals (tests, tools)."""
        self._pool = pool
        self._stage_locks = stage_locks or StageLocks(pool, STAGE_LOCK_SLOTS)

    def get(self, key: str) -> BlobStat | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM store_blobs WHERE key = %s", (key,)
            ).fetchone()
        return _stat(row) if row is not None else None

    def by_sha(self, sha256: str, kind: str, backend: str) -> BlobRef | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM store_blobs WHERE sha256 = %s AND kind = %s"
                " AND backend = %s LIMIT 1",
                (sha256, kind, backend),
            ).fetchone()
        return _stat(row).ref if row is not None else None

    @staticmethod
    def _locked(
        conn: Connection[DictRow], key: str, ref: BlobRef, *, reuse: bool, columns: str
    ) -> DictRow | None:
        """Lock ``key``'s row FOR UPDATE, in the caller's transaction, and return it.

        With ``reuse``, ``ref`` names an existing object, and this also holds a row
        that names it, so a put or delete that would free that row waits for this
        transaction, and its `shares_backend_id` check afterwards sees the new row. If
        ``key``'s own row names the object, its lock is the hold. Otherwise another
        row is locked FOR SHARE. If the object was freed first, no row matches, and this
        raises so the caller uploads its own copy. A false miss (the one matched row
        moved on under READ COMMITTED) only costs an upload.

        Rows are locked in key order, as `mark` locks them: a row naming the object
        below ``key`` first, then ``key``'s, then one above it. Two puts swapping
        objects between two keys then both start at the lower key, and one waits for
        the other instead of each holding its own key and waiting for the other's
        (#1605). That deadlock was retried, but the victim's retry could re-take its
        key before the winner took it and deadlock again. Only a hold that moved on
        between the two steps is taken out of order, and that rare deadlock is what
        `retrying` is still for. The hold is never ``key``'s own row FOR SHARE, so two
        re-puts of one key cannot both share it and then deadlock on the upgrade.

        On the local backend the caller's own copy is the same path (`kind/sha`), so a
        release that already missed `shares_backend_id` can still unlink that re-upload
        before its row lands. No production path uses the local `ContentBackend` for
        this: the store builds a `ContentStore` only over Bambuddy, whose every upload
        is a new file."""
        where = "backend = %s AND backend_id = %s"
        held = reuse and (
            conn.execute(
                f"SELECT 1 FROM store_blobs WHERE {where} AND key < %s"
                " ORDER BY key LIMIT 1 FOR SHARE",
                (ref.backend, ref.backend_id, key),
            ).fetchone()
            is not None
        )
        own = conn.execute(
            f"SELECT {columns} FROM store_blobs WHERE key = %s FOR UPDATE", (key,)
        ).fetchone()
        if not reuse or held:
            return own
        if own is not None and (own["backend"], own["backend_id"]) == (ref.backend, ref.backend_id):
            return own
        above = conn.execute(
            f"SELECT 1 FROM store_blobs WHERE {where} AND key <> %s"
            " ORDER BY key DESC LIMIT 1 FOR SHARE",
            (ref.backend, ref.backend_id, key),
        ).fetchone()
        if above is None:
            raise ReuseLostError(f"{ref.backend}:{ref.backend_id} was freed while being reused")
        return own

    def retrying[T](self, key: str, write: Callable[[Connection[DictRow]], T]) -> T:
        """Run ``write`` (its own transactions) on one pooled connection, again when
        Postgres breaks a deadlock with it. `_locked` takes its rows in key order, so
        only a hold it had to take out of order can deadlock. The last attempt's
        `DeadlockDetected` propagates. A ``write`` that opens one transaction around
        all it does (a CRUD route's, with its ledger row) is retried whole."""
        with self._pool.connection() as conn:
            for attempt in range(1, _DEADLOCK_ATTEMPTS + 1):
                try:
                    return write(conn)
                except DeadlockDetected:
                    if attempt == _DEADLOCK_ATTEMPTS:
                        raise
                    logger.warning(
                        "retrying an index write Postgres picked as a deadlock's victim",
                        extra={"key": key, "attempt": attempt},
                    )
        raise AssertionError("unreachable")

    def put(
        self,
        key: str,
        ref: BlobRef,
        *,
        slug: str | None,
        meta: dict[str, Any],
        reuse: bool = False,
    ) -> BlobRef | None:
        """Point ``key`` at ``ref`` and return what it named before, read under the
        row's lock in the same transaction, so two racing puts each release exactly the
        object the other one replaced. A key another put created meanwhile is retried
        as an update: a single upsert would not see that row's object. With ``reuse``,
        ``ref`` names an existing object (`_locked`)."""
        return self.retrying(
            key, lambda conn: self.put_in(conn, key, ref, slug=slug, meta=meta, reuse=reuse)
        )

    def put_in(
        self,
        conn: Connection[DictRow],
        key: str,
        ref: BlobRef,
        *,
        slug: str | None,
        meta: dict[str, Any],
        reuse: bool = False,
    ) -> BlobRef | None:
        """`put` on ``conn``. It writes ``meta`` whole, except that a row's ``output`` mark
        (`slicing/transfer.py` `SLICED_OUTPUT_META`) stays: a slicer's input of the same
        bytes as a marked output shares its key, and must not unmark it (#5298 Task 32)."""
        values = (ref.sha256, ref.kind, ref.backend, ref.backend_id, ref.size, slug, Jsonb(meta))
        while True:
            with conn.transaction():
                row = self._locked(conn, key, ref, reuse=reuse, columns=_COLUMNS)
                if row is not None:
                    conn.execute(
                        f"UPDATE store_blobs SET {_KEEP_COPY}, sha256 = %s, kind = %s,"
                        " backend = %s, backend_id = %s, size = %s, slug = %s,"
                        " meta = %s || jsonb_strip_nulls("
                        "jsonb_build_object('output', store_blobs.meta -> 'output')),"
                        " touched_at = now() WHERE key = %s",
                        (*_kept_copy(ref), *values, key),
                    )
                    return _stat(row).ref
                inserted = conn.execute(
                    "INSERT INTO store_blobs"
                    " (sha256, kind, backend, backend_id, size, slug, meta, key)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
                    (*values, key),
                )
                if inserted.rowcount == 1:
                    return None

    def swap(
        self,
        key: str,
        ref: BlobRef,
        *,
        expected: str | None,
        slug: str | None,
        meta: dict[str, Any],
        reuse: bool = False,
    ) -> bool:
        """Point ``key`` at ``ref`` only if it still names ``expected`` (None: no row).
        With ``reuse``, ``ref`` names an existing object (`_locked`)."""
        return self.retrying(
            key,
            lambda conn: self.swap_in(
                conn, key, ref, expected=expected, slug=slug, meta=meta, reuse=reuse
            ),
        )

    def swap_in(
        self,
        conn: Connection[DictRow],
        key: str,
        ref: BlobRef,
        *,
        expected: str | None,
        slug: str | None,
        meta: dict[str, Any],
        reuse: bool = False,
        only_kind: str | None = None,
    ) -> bool:
        """`swap` on ``conn``. With ``only_kind``, a row of another kind is not rewritten
        (the swap answers false): the render worker's CRUD route passes ``piece``, checked
        under the row's lock."""
        values = (ref.sha256, ref.kind, ref.backend, ref.backend_id, ref.size, slug, Jsonb(meta))
        with conn.transaction():
            self._locked(conn, key, ref, reuse=reuse, columns="backend, backend_id")
            if expected is None:
                cursor = conn.execute(
                    "INSERT INTO store_blobs"
                    " (sha256, kind, backend, backend_id, size, slug, meta, key)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
                    (*values, key),
                )
            else:
                cursor = conn.execute(
                    f"UPDATE store_blobs SET {_KEEP_COPY}, sha256 = %s, kind = %s,"
                    " backend = %s, backend_id = %s, size = %s, slug = %s, meta = %s,"
                    " touched_at = now() WHERE key = %s AND sha256 = %s"
                    " AND (%s::text IS NULL OR kind = %s)",
                    (*_kept_copy(ref), *values, key, expected, only_kind, only_kind),
                )
            return cursor.rowcount == 1

    def now(self) -> datetime:
        """The database's clock, which every `touched_at` is stamped with."""
        with self._pool.connection() as conn:
            row = conn.execute("SELECT now() AS now").fetchone()
        assert row is not None
        now: datetime = row["now"]
        return now

    def mark(self, keys: Sequence[str], *, backend: str, cutoff: datetime, **meta: Any) -> None:
        """Merge ``meta`` into the rows of ``keys`` on ``backend`` not touched since
        ``cutoff``. A later `put` rewrites `meta`, which clears the mark (all but an
        ``output`` mark, which `put_in` keeps)."""
        self.retrying(
            ",".join(sorted(keys)),
            lambda conn: self.mark_in(conn, keys, backend=backend, cutoff=cutoff, meta=meta),
        )

    @staticmethod
    def mark_in(
        conn: Connection[DictRow],
        keys: Sequence[str],
        *,
        backend: str,
        cutoff: datetime,
        meta: dict[str, Any],
    ) -> None:
        """`mark` on ``conn``."""
        where = "key = ANY(%s) AND backend = %s AND touched_at <= %s"
        args = (list(keys), backend, cutoff)
        with conn.transaction():
            # Keys first, in key order, as every other index write locks them; an
            # UPDATE over many rows would take them in scan order.
            conn.execute(f"SELECT 1 FROM store_blobs WHERE {where} ORDER BY key FOR UPDATE", args)
            conn.execute(
                f"UPDATE store_blobs SET meta = meta || %s WHERE {where}", (Jsonb(meta), *args)
            )

    def touch(self, key: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("UPDATE store_blobs SET touched_at = now() WHERE key = %s", (key,))

    def delete(self, key: str, *, backend: str | None = None) -> BlobStat | None:
        """Drop ``key``'s row; with ``backend``, only a row on that backend."""
        with self._pool.connection() as conn:
            return self.delete_in(conn, key, backend=backend)

    @staticmethod
    def delete_in(
        conn: Connection[DictRow], key: str, *, backend: str | None = None
    ) -> BlobStat | None:
        """`delete` on ``conn``."""
        row = conn.execute(
            f"DELETE FROM store_blobs WHERE key = %s"
            f" AND (%s::text IS NULL OR backend = %s) RETURNING {_COLUMNS}",
            (key, backend, backend),
        ).fetchone()
        return _stat(row) if row is not None else None

    def delete_if_names(self, key: str, ref: BlobRef) -> BlobStat | None:
        """Drop ``key``'s row only while it still names ``ref`` (its object and bytes), in
        one statement: a row stored again since ``ref`` was read stays. The row taken, or
        None."""
        with self._pool.connection() as conn:
            row = conn.execute(
                f"DELETE FROM store_blobs WHERE key = %s AND sha256 = %s AND kind = %s"
                f" AND backend = %s AND backend_id = %s RETURNING {_COLUMNS}",
                (key, ref.sha256, ref.kind, ref.backend, ref.backend_id),
            ).fetchone()
        return _stat(row) if row is not None else None

    def delete_if_stale(
        self, key: str, cutoff: datetime, *, backend: str | None = None
    ) -> BlobStat | None:
        """Atomic against a concurrent `touch`: a blob claimed after the sweep's snapshot
        has a fresh `touched_at` and is not matched. With ``backend``, only a row on it."""
        with self._pool.connection() as conn:
            return self.delete_if_stale_in(conn, key, cutoff, backend=backend)

    @staticmethod
    def delete_if_stale_in(
        conn: Connection[DictRow], key: str, cutoff: datetime, *, backend: str | None = None
    ) -> BlobStat | None:
        """`delete_if_stale` on ``conn``."""
        row = conn.execute(
            f"DELETE FROM store_blobs WHERE key = %s AND touched_at <= %s"
            f" AND (%s::text IS NULL OR backend = %s) RETURNING {_COLUMNS}",
            (key, cutoff, backend, backend),
        ).fetchone()
        return _stat(row) if row is not None else None

    def shares_backend_id(self, backend: str, backend_id: str) -> bool:
        """Whether a row names the object, as its own or as its migrated copy: a copy
        still running across the flip is recorded only beside its row."""
        with self._pool.connection() as conn:
            return self._names(conn, backend, backend_id)

    def stage_hold(self) -> contextlib.AbstractAsyncContextManager[StageHold]:
        """A new `StageHold` (`StageLocks.hold`): it waits for a free slot first."""
        return self._stage_locks.hold()

    @contextlib.contextmanager
    def claim_unnamed(self, backend: str, backend_id: str, sha256: str) -> Iterator[bool]:
        """Whether the object may be removed now: True only when this call holds its lock
        exclusively (`object_lock_name`, never waiting for it) and no row names it,
        checked under that lock. The lock is held until the block ends, so a stager that
        comes meanwhile waits for the remove and then uploads again. False: a stager holds
        it (a save between its upload and its commit), or a row names it; leave it."""
        name = object_lock_name(backend, sha256)
        # The stage-lock pool's removal slot, not the shared pool: the lock is held
        # through the caller's remove, a backend round trip (#7756 review).
        with self._stage_locks.claim_connection() as conn:
            row = conn.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS got", (name,)
            ).fetchone()
            if not (row and row["got"]):
                yield False
                return
            try:
                yield not self._names(conn, backend, backend_id)
            finally:
                _drop_or_discard(
                    conn, "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (name,)
                )

    @staticmethod
    def _names(conn: Connection[DictRow], backend: str, backend_id: str) -> bool:
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM store_blobs"
            " WHERE (backend, backend_id) = (%s, %s)"
            " OR (migrated_backend, migrated_backend_id) = (%s, %s)) AS shared",
            (backend, backend_id, backend, backend_id),
        ).fetchone()
        return bool(row and row["shared"])

    def recorded(self, backend: str, backend_id: str) -> bool:
        """Whether a row names the object as its own: not a migrated copy
        (`BambuddyContentBackend.sweep_unrecorded`'s default)."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM store_blobs WHERE backend = %s AND backend_id = %s)"
                " AS recorded",
                (backend, backend_id),
            ).fetchone()
        return bool(row and row["recorded"])

    def vouched(self, ref: BlobRef) -> tuple[bool, bool]:
        """Of the rows that name ``ref``'s object: whether one the API verified names it as
        it is (the same kind, SHA-256 and size, `verified_at` set, #6233), and whether one
        names it as another kind."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT coalesce(bool_or(kind = %s AND sha256 = %s AND size = %s"
                " AND verified_at IS NOT NULL), false) AS verified,"
                " coalesce(bool_or(kind <> %s), false) AS foreign"
                " FROM store_blobs WHERE backend = %s AND backend_id = %s",
                (ref.kind, ref.sha256, ref.size, ref.kind, ref.backend, ref.backend_id),
            ).fetchone()
        return (bool(row and row["verified"]), bool(row and row["foreign"]))

    @staticmethod
    def mark_verified_in(conn: Connection[DictRow], key: str, ref: BlobRef) -> None:
        """On ``conn``: note that the API read ``ref``'s object and found its SHA-256 and
        size, when ``key``'s row names it (`vouched`)."""
        conn.execute(
            "UPDATE store_blobs SET verified_at = now() WHERE key = %s AND sha256 = %s"
            " AND kind = %s AND backend = %s AND backend_id = %s AND size = %s",
            (key, ref.sha256, ref.kind, ref.backend, ref.backend_id, ref.size),
        )

    def stats(
        self,
        kinds: Sequence[str] | None,
        slug: str | None = None,
        *,
        backend: str | None = None,
    ) -> list[BlobStat]:
        """Rows by kind, template and backend; None matches any."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM store_blobs"
                " WHERE (%s::text[] IS NULL OR kind = ANY(%s::text[]))"
                " AND (%s::text IS NULL OR slug = %s)"
                " AND (%s::text IS NULL OR backend = %s) ORDER BY key",
                (list(kinds) if kinds is not None else None,) * 2 + (slug, slug, backend, backend),
            ).fetchall()
        return [_stat(row) for row in rows]

    def unmigrated(
        self, source: str, target: str, *, limit: int, started: datetime
    ) -> list[BlobStat]:
        """Up to ``limit`` rows on ``source`` with no copy on ``target``, least recently
        touched first; a row whose copy failed only if it failed before ``started``."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM store_blobs WHERE backend = %s"
                " AND migrated_backend IS DISTINCT FROM %s"
                " AND (migrated_error IS NULL OR migrated_error_at < %s)"
                " ORDER BY touched_at, key LIMIT %s",
                (source, target, started, limit),
            ).fetchall()
        return [_stat(row) for row in rows]

    def migration_counts(
        self, source: str, target: str, *, started: datetime | None = None
    ) -> tuple[int, int, int]:
        """Rows on ``source`` still to copy to ``target``, those whose copy failed, and
        those copied. With ``started``, a copy that failed before it is still to copy."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FILTER (WHERE NOT failed) AS remaining,"
                " count(*) FILTER (WHERE failed) AS failed,"
                " (SELECT count(*) FROM store_blobs WHERE backend = %s"
                "  AND migrated_backend = %s) AS copied"
                " FROM (SELECT migrated_error IS NOT NULL"
                "  AND (%s::timestamptz IS NULL OR migrated_error_at >= %s) AS failed"
                "  FROM store_blobs WHERE backend = %s"
                "  AND migrated_backend IS DISTINCT FROM %s) AS pending",
                (source, target, started, started, source, target),
            ).fetchone()
        assert row is not None
        return int(row["remaining"]), int(row["failed"]), int(row["copied"])

    def migration_failures(self, source: str, target: str, *, limit: int) -> list[tuple[str, str]]:
        """Up to ``limit`` rows on ``source`` whose copy to ``target`` failed, and why."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT key, migrated_error FROM store_blobs WHERE backend = %s"
                " AND migrated_backend IS DISTINCT FROM %s AND migrated_error IS NOT NULL"
                " ORDER BY key LIMIT %s",
                (source, target, limit),
            ).fetchall()
        return [(row["key"], row["migrated_error"]) for row in rows]

    def mark_migrated(self, key: str, ref: BlobRef, *, backend: str, backend_id: str) -> bool:
        """Record ``key``'s copy on ``backend`` only if the row still names ``ref``'s
        bytes on ``ref``'s backend; a row that moved on is left for the next pass."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "UPDATE store_blobs SET migrated_backend = %s, migrated_backend_id = %s,"
                " migrated_error = NULL, migrated_error_at = NULL"
                " WHERE key = %s AND sha256 = %s AND backend = %s",
                (backend, backend_id, key, ref.sha256, ref.backend),
            )
        return cursor.rowcount == 1

    def mark_migration_failed(self, key: str, ref: BlobRef, error: str) -> None:
        """Record why ``key``'s copy failed, if the row still names ``ref``."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE store_blobs SET migrated_error = %s, migrated_error_at = now()"
                " WHERE key = %s AND sha256 = %s AND backend = %s",
                (error, key, ref.sha256, ref.backend),
            )

    def copied_refs(self, source: str, target: str, *, limit: int) -> list[BlobRef]:
        """Up to ``limit`` rows on ``source`` already copied to ``target``, most recently
        touched first: objects the source served this migration, to ask it about again.
        A copy takes the least recently touched rows first, so these are the latest
        copies, and the ones in use, the least likely to have been deleted since. Run
        only for a batch of misses alone (#4114), and read off an index (#4117)."""
        with self._pool.connection() as conn:
            rows = conn.execute(COPIED_REFS_SQL, (source, target, limit)).fetchall()
        return [_stat(row).ref for row in rows]

    def forget_missing(self, key: str, ref: BlobRef) -> bool:
        """Drop ``key``'s row if it still names ``ref``, object and all (#4084): one its
        backend no longer has. A row re-put meanwhile names another object and stays."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM store_blobs"
                " WHERE key = %s AND sha256 = %s AND backend = %s AND backend_id = %s",
                (key, ref.sha256, ref.backend, ref.backend_id),
            )
        return cursor.rowcount > 0

    def switch_migrated(self, backend: str) -> int:
        """Point every row copied to ``backend`` at its copy, in one statement: the
        migration's switch, run when a process starts on ``backend``."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "UPDATE store_blobs SET backend = migrated_backend,"
                " backend_id = migrated_backend_id, migrated_backend = NULL,"
                " migrated_backend_id = NULL, migrated_error = NULL, migrated_error_at = NULL,"
                " verified_at = NULL WHERE migrated_backend = %s",
                (backend,),
            )
        return cursor.rowcount

    def usage(self, backend: str) -> tuple[int, int, dict[str, int]]:
        """Distinct stored objects and their bytes, in total and per kind."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT kind, count(*) AS n, coalesce(sum(size), 0) AS b FROM ("
                " SELECT DISTINCT ON (backend_id) backend_id, kind, size FROM store_blobs"
                " WHERE backend = %s) AS objects GROUP BY kind",
                (backend,),
            ).fetchall()
        by_kind = {row["kind"]: int(row["b"]) for row in rows}
        return sum(int(row["n"]) for row in rows), sum(by_kind.values()), by_kind
