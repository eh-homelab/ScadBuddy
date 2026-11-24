"""`OutputRows`: every saved output as one row of ``outputs`` (#7255, outputs-in-Postgres
spec 2026-11-23 §4.2), and the log of the copy runs that fill it (``output_copy_runs``).

Phase 2 (#7256, spec §4.2, §4.3): the row also names the blobs that hold the output's
bytes (``model_blob``, ``preview_blob``, ``thumbnail_blob``, and ``output_files`` for
``files/``), keeps what readers derive from the model (``output_derived``), and logs the
byte copy's runs (``output_byte_runs``). Each blob is held in ``blob_refs`` by
``(OUTPUT_HOLDER, output_id)``; every write of a key writes its hold in the same
transaction, and every delete of a row releases its byte holds. The files under
``data/outputs/<slug>/<id>/`` stay the fallback until #7257.

It is synchronous, like `OutputIndex`: callers on the loop use a thread. It imports
`OutputMeta` only inside a function, because `outputs.py` imports this module.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, get_args

import psycopg
from psycopg import Connection
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.store.content_models import BlobRef
from scadbuddy.store.index import BlobIndex

if TYPE_CHECKING:
    from scadbuddy.library.outputs import OutputMeta

logger = logging.getLogger(__name__)

#: `blob_refs.holder_kind` for a saved output: its Parts and its byte blobs live as long
#: as it does.
OUTPUT_HOLDER = "output"

#: An output's byte roles that have a column of their own (spec §4.3).
Role = Literal["model", "preview", "thumbnail"]
ROLES: tuple[Role, ...] = get_args(Role)


def output_blob_key(output_id: str, role: Role) -> str:
    """The blob key of the output's ``role`` (spec §4.3)."""
    return f"output-{output_id}-{role}"


def staged_thumbnail_key(output_id: str, sha256: str) -> str:
    """The key a thumbnail upload's blob is put under before its step names it as
    `output_blob_key` ``thumbnail`` (`OutputRows.keep_thumbnail`). Nothing holds it, so
    the sweep removes its row after the grace."""
    return f"output-{output_id}-thumbnail-{sha256}"


def plate_cover_key(output_id: str) -> str:
    """The blob key of the output's plate cover: the 3MF's ``plate_1.png``, put once on
    its first read (`OutputRows.keep_plate_cover`) and held by the output like its other
    blobs, so a model delete releases it with them."""
    return f"output-{output_id}-plate_cover"


def thumbnail_lock_key(output_id: str) -> str:
    """The text whose ``hashtextextended(…, 0)`` keys the output's thumbnail lock: a
    Postgres advisory transaction lock, so it holds across the API's replicas. Every
    writer of the thumbnail key or file in a process with a database takes it
    (`lock_thumbnail`): a thumbnail upload's step (`OutputRows.keep_thumbnail`), a drawn
    thumbnail's step (`OutputRows.draw_thumbnail`), a write of the file alone
    (`OutputRows.write_thumbnail_file`) and the byte copy (Task 9). With no database
    (tests and tools) there is no blob and no other replica, and the file is written with
    no lock."""
    return f"output-thumbnail:{output_id}"


def lock_thumbnail(conn: Connection[DictRow], output_id: str) -> None:
    """Take the output's thumbnail lock until ``conn``'s transaction ends."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (thumbnail_lock_key(output_id),)
    )


def output_file_key(output_id: str, n: int) -> str:
    """The blob key of the output's ``files/`` entry ``n``: the 0-based index of its name
    in the sorted names (spec §4.3)."""
    return f"output-{output_id}-file-{n}"


@dataclass(frozen=True)
class OutputFileBlob:
    name: str
    blob_key: str
    size: int


@dataclass(frozen=True)
class OutputBlobs:
    """The blob keys of an output's bytes. ``None``: the role has no blob yet."""

    model: str | None = None
    preview: str | None = None
    thumbnail: str | None = None
    files: tuple[OutputFileBlob, ...] = ()

    def keys(self) -> list[str]:
        """Every key, the roles first, then the files in their order."""
        roles = [key for key in (self.model, self.preview, self.thumbnail) if key is not None]
        return roles + [item.blob_key for item in self.files]


@dataclass(frozen=True)
class ByteWrite:
    """One output's result of the byte copy."""

    output_id: str
    blobs: OutputBlobs
    #: The copy's stamp of the byte files it read.
    stamp: str
    #: ``[{role, error}]`` for the roles whose file was missing; ``None`` when none.
    damaged: list[dict[str, Any]] | None


_COLUMNS = (
    "output_id, slug, meta, params, inputs, record, manifest, bom, arranged_from,"
    " backfill, has_plate_cover, copied_at, files_stamp, model_blob, preview_blob,"
    " thumbnail_blob, bytes_at"
)

#: Release the byte holds of the rows ``holder_id = ANY(%s)``: only keys of the output's
#: own (`output_blob_key`, `output_file_key`), never its Parts (a piece key) or a render
#: job's output key.
_RELEASE_BYTES = (
    "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = ANY(%s)"
    " AND starts_with(key, 'output-' || holder_id || '-')"
)

#: Hold ``key`` for the output, only while its row exists.
_HOLD = (
    "INSERT INTO blob_refs (key, holder_kind, holder_id)"
    " SELECT %s, %s, %s WHERE EXISTS (SELECT 1 FROM outputs WHERE output_id = %s)"
    " ON CONFLICT DO NOTHING"
)

_INSERT_FILE = "INSERT INTO output_files (output_id, name, blob_key, size) VALUES (%s, %s, %s, %s)"

_BYTE_RUN_COLUMNS = (
    "id, started_at, finished_at, incomplete, refused, copied, verified, skipped,"
    " needed_bytes, needed_count, failed, damaged"
)


@dataclass(frozen=True)
class OutputRow:
    output_id: str
    slug: str
    meta: OutputMeta
    params: dict[str, Any]
    #: ``None`` is a record from before inputs.
    inputs: dict[str, Any] | None
    record: dict[str, Any] | None
    manifest: list[Any] | None
    bom: list[Any] | None
    arranged_from: list[str] | None
    backfill: dict[str, Any] | None
    has_plate_cover: bool
    #: When a writer last wrote the row. The store sets it, so the value given is ignored.
    copied_at: datetime
    #: The copy's stamp of the record files it read; ``None`` for a row a save wrote.
    files_stamp: str | None = None
    #: The blob keys of its bytes. `OutputRows.get` leaves ``files`` empty: read them with
    #: `OutputRows.files`.
    blobs: OutputBlobs = field(default_factory=OutputBlobs)
    #: When a save or the byte copy put its blobs; ``None`` while the files hold them.
    #: The store sets it, so the value given is ignored.
    bytes_at: datetime | None = None


def _json(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value)


def _meta_json(meta: OutputMeta) -> Jsonb:
    return Jsonb(meta.model_dump(mode="json"))


def _row(data: DictRow) -> OutputRow:
    from scadbuddy.library.outputs import OutputMeta

    return OutputRow(
        output_id=data["output_id"],
        slug=data["slug"],
        meta=OutputMeta.model_validate(data["meta"]),
        params=data["params"],
        inputs=data["inputs"],
        record=data["record"],
        manifest=data["manifest"],
        bom=data["bom"],
        arranged_from=data["arranged_from"],
        backfill=data["backfill"],
        has_plate_cover=data["has_plate_cover"],
        copied_at=data["copied_at"],
        files_stamp=data["files_stamp"],
        blobs=OutputBlobs(
            model=data["model_blob"],
            preview=data["preview_blob"],
            thumbnail=data["thumbnail_blob"],
        ),
        bytes_at=data["bytes_at"],
    )


def _file(data: DictRow) -> OutputFileBlob:
    return OutputFileBlob(name=data["name"], blob_key=data["blob_key"], size=int(data["size"]))


def _values(row: OutputRow) -> tuple[Any, ...]:
    return (
        row.output_id,
        row.slug,
        _meta_json(row.meta),
        Jsonb(row.params),
        _json(row.inputs),
        _json(row.record),
        _json(row.manifest),
        _json(row.bom),
        _json(row.arranged_from),
        _json(row.backfill),
        row.has_plate_cover,
        row.meta.created_at,
    )


#: A save's row, with its blob keys. A save that put its model's blob has its bytes now
#: (``bytes_at``, by the database's clock).
_INSERT = (
    "INSERT INTO outputs (output_id, slug, meta, params, inputs, record, manifest, bom,"
    " arranged_from, backfill, has_plate_cover, created_at, model_blob, preview_blob,"
    " thumbnail_blob, bytes_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
    " CASE WHEN %s THEN clock_timestamp() END)"
)

#: A save's row replaces a row the copy wrote (it has a stamp) of the same folder: the
#: copy may have read the save's folder before the save wrote its row. A row of another
#: slug stays, so a second save of an id (`orders/quote.py` makes them with uuid5) under
#: another model still fails on the key.
_DROP_COPIED = (
    "DELETE FROM outputs WHERE output_id = %s AND slug = %s AND files_stamp IS NOT NULL"
    " RETURNING output_id"
)


def _ids(found: list[DictRow]) -> list[str]:
    return [str(item["output_id"]) for item in found]


def _release_bytes(conn: Connection[DictRow], output_ids: Sequence[str]) -> None:
    """Release the byte holds of the rows ``output_ids`` deleted, in ``conn``'s
    transaction."""
    if output_ids:
        conn.execute(_RELEASE_BYTES, (OUTPUT_HOLDER, list(output_ids)))


def _drop_copied(conn: Connection[DictRow], output_id: str, slug: str) -> None:
    _release_bytes(conn, _ids(conn.execute(_DROP_COPIED, (output_id, slug)).fetchall()))


def _write_files(
    conn: Connection[DictRow], output_id: str, files: Sequence[OutputFileBlob]
) -> None:
    if files:
        with conn.cursor() as cursor:
            cursor.executemany(
                _INSERT_FILE, [(output_id, item.name, item.blob_key, item.size) for item in files]
            )


def _hold_keys(conn: Connection[DictRow], output_id: str, keys: Sequence[str]) -> None:
    if keys:
        with conn.cursor() as cursor:
            cursor.executemany(_HOLD, [(key, OUTPUT_HOLDER, output_id, output_id) for key in keys])


def _set_blob(conn: Connection[DictRow], output_id: str, role: Role, key: str) -> bool:
    cursor = conn.execute(
        f"UPDATE outputs SET {role}_blob = %s, bytes_at = CASE WHEN bytes_at IS NOT"
        " NULL THEN clock_timestamp() END WHERE output_id = %s",
        (key, output_id),
    )
    if not cursor.rowcount:
        return False
    _hold_keys(conn, output_id, [key])
    return True


#: What a thumbnail step runs again after, as `BlobIndex.retrying` runs an index write
#: again: Postgres broke a deadlock with it (`BlobIndex.put_in` can still take a hold out
#: of key order), or a serialization failure.
_RETRIED: tuple[type[Exception], ...] = (DeadlockDetected, SerializationFailure)
_ATTEMPTS = 3


def _retrying[T](
    pool: ConnectionPool[Connection[DictRow]],
    output_id: str,
    step: Callable[[Connection[DictRow]], T],
) -> T:
    """Run ``step`` (one transaction of its own) on one pooled connection, again when
    Postgres picks it as a deadlock's victim or fails it to serialize. The last
    attempt's error propagates."""
    with pool.connection() as conn:
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                return step(conn)
            except _RETRIED:
                if attempt == _ATTEMPTS:
                    raise
                logger.warning(
                    "retrying a thumbnail step Postgres picked as a deadlock's victim",
                    extra={"output_id": output_id, "attempt": attempt},
                )
    raise AssertionError("unreachable")


class _FileThereError(Exception):
    """A drawn thumbnail's file write found a file there: its step rolls back."""


def _insert(conn: Connection[DictRow], row: OutputRow) -> None:
    blobs = row.blobs
    _drop_copied(conn, row.output_id, row.slug)
    conn.execute(
        _INSERT,
        (
            *_values(row),
            blobs.model,
            blobs.preview,
            blobs.thumbnail,
            blobs.model is not None,
        ),
    )
    _write_files(conn, row.output_id, blobs.files)
    _hold_keys(conn, row.output_id, blobs.keys())


#: The copy's new row. A row a save wrote meanwhile wins.
_COPY_INSERT = (
    "INSERT INTO outputs (output_id, slug, meta, params, inputs, record, manifest, bom,"
    " arranged_from, backfill, has_plate_cover, created_at, files_stamp, copied_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    " ON CONFLICT (output_id) DO NOTHING"
)

#: The copy's refresh: only a row no writer touched since the mark. A row deleted
#: meanwhile stays deleted.
_COPY_REFRESH = (
    "UPDATE outputs SET slug = %s, meta = %s, params = %s, inputs = %s, record = %s,"
    " manifest = %s, bom = %s, arranged_from = %s, backfill = %s, has_plate_cover = %s,"
    " created_at = %s, files_stamp = %s, copied_at = %s"
    " WHERE output_id = %s AND copied_at < %s"
)


class OutputRows:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool
        self._copied = False
        self._lock = threading.Lock()

    def get(self, output_id: str) -> OutputRow | None:
        with self._pool.connection() as conn:
            found = conn.execute(
                f"SELECT {_COLUMNS} FROM outputs WHERE output_id = %s", (output_id,)
            ).fetchone()
        return None if found is None else _row(found)

    def slug_of(self, output_id: str) -> str | None:
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT slug FROM outputs WHERE output_id = %s", (output_id,)
            ).fetchone()
        return None if found is None else str(found["slug"])

    def existing(self, output_ids: Iterable[str]) -> set[str]:
        ids = list(output_ids)
        if not ids:
            return set()
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT output_id FROM outputs WHERE output_id = ANY(%s)", (ids,)
            ).fetchall()
        return {str(item["output_id"]) for item in found}

    def for_slug(self, slug: str) -> list[OutputRow]:
        """The model's outputs, newest first."""
        with self._pool.connection() as conn:
            found = conn.execute(
                f"SELECT {_COLUMNS} FROM outputs WHERE slug = %s"
                " ORDER BY created_at DESC, output_id DESC",
                (slug,),
            ).fetchall()
        return [_row(item) for item in found]

    def insert(self, row: OutputRow, conn: Connection[DictRow] | None = None) -> None:
        """Add a save's row, with its blob keys, its ``output_files`` rows and a hold of
        each key. It replaces a row the copy wrote of the same slug; any other row with
        that id is an error, and then nothing is written. With ``conn``, inside its
        transaction."""
        if conn is not None:
            _insert(conn, row)
            return
        with self._pool.connection() as own, own.transaction():
            _insert(own, row)

    def drop_copied(self, output_id: str, slug: str) -> None:
        """Delete the row the copy wrote of the folder ``<slug>/<output_id>``, and its byte
        holds; a row a save wrote (no stamp) stays. A failed save calls it after it
        removed its folder."""
        with self._pool.connection() as conn, conn.transaction():
            _drop_copied(conn, output_id, slug)

    def set_blob(self, output_id: str, role: Role, key: str) -> bool:
        """Name ``key`` as the blob of the output's ``role`` and hold it, in one
        transaction. False when the row is gone: then no hold is written. It moves
        ``bytes_at`` only on a row already in blobs, so the byte copy's guard skips it; on
        a row with no ``bytes_at`` the files still hold the bytes, and it stays NULL."""
        if role not in ROLES:
            raise ValueError(f"not a byte role: {role!r}")
        with self._pool.connection() as conn, conn.transaction():
            return _set_blob(conn, output_id, role, key)

    def keep_thumbnail(
        self,
        output_id: str,
        key: str,
        staged: BlobRef,
        *,
        slug: str,
        write: Callable[[], None],
    ) -> tuple[bool, BlobRef | None]:
        """A thumbnail upload's step, in one transaction under the output's thumbnail
        lock (`lock_thumbnail`): ``key`` takes the object of ``staged`` (a put under
        `staged_thumbnail_key`), then ``write`` writes the file, then the row names
        ``key`` and holds it. The key moves only when the transaction commits, so a
        ``write`` that raises leaves the key, the row and the old file as they were; the
        staged blob is then held by nothing, and the sweep removes it. A commit that
        fails after ``write`` (a lost connection) leaves the new file with the old key:
        that is the accepted cost of a file beside a row, until #7257 drops the file.
        A deadlock or a serialization failure runs the step again (`_retrying`); ``write``
        then writes the same bytes again.

        Returns whether the row was there (False: nothing written) and the object ``key``
        named before, which the caller releases when it is not ``staged``'s
        (`OutputBytes.release_replaced`). `ReuseLostError` when the staged object was
        freed first. Blocking."""

        def step(conn: Connection[DictRow]) -> tuple[bool, BlobRef | None]:
            with conn.transaction():
                lock_thumbnail(conn, output_id)
                found = conn.execute(
                    "SELECT 1 FROM outputs WHERE output_id = %s FOR UPDATE", (output_id,)
                ).fetchone()
                if found is None:
                    return False, None
                previous = BlobIndex(self._pool).put_in(
                    conn, key, staged, slug=slug, meta={}, reuse=True
                )
                write()
                _set_blob(conn, output_id, "thumbnail", key)
            return True, previous

        return _retrying(self._pool, output_id, step)

    def draw_thumbnail(
        self,
        output_id: str,
        key: str,
        staged: BlobRef,
        *,
        slug: str,
        write: Callable[[], bool],
    ) -> bool:
        """A drawn thumbnail's step (#4775), in one transaction under the output's
        thumbnail lock, only while the output has no thumbnail: no index row of ``key``
        and no file. ``key`` takes the object of ``staged``, then ``write`` writes the
        file only when there is none (False: an upload's file is there, and the key goes
        back), then the row names ``key`` and holds it. Whether this draw is the
        thumbnail now. Blocking."""

        def step(conn: Connection[DictRow]) -> bool:
            try:
                with conn.transaction():
                    lock_thumbnail(conn, output_id)
                    found = conn.execute(
                        "SELECT 1 FROM outputs WHERE output_id = %s FOR UPDATE", (output_id,)
                    ).fetchone()
                    if found is None:
                        return False
                    there = conn.execute(
                        "SELECT 1 FROM store_blobs WHERE key = %s", (key,)
                    ).fetchone()
                    if there is not None:
                        return False
                    BlobIndex(self._pool).put_in(conn, key, staged, slug=slug, meta={}, reuse=True)
                    if not write():
                        raise _FileThereError
                    _set_blob(conn, output_id, "thumbnail", key)
                    return True
            except _FileThereError:
                return False

        return _retrying(self._pool, output_id, step)

    def write_thumbnail_file[T](self, output_id: str, write: Callable[[], T]) -> T:
        """``write`` of the thumbnail file alone (no blob store, no row yet, or a drawn
        thumbnail whose put failed), under the output's thumbnail lock, so the byte copy
        puts the key from the file under the same lock (Task 9), never puts a file this
        write replaces. Blocking."""

        def step(conn: Connection[DictRow]) -> T:
            with conn.transaction():
                lock_thumbnail(conn, output_id)
                return write()

        return _retrying(self._pool, output_id, step)

    def files(self, output_id: str) -> list[OutputFileBlob]:
        """The output's ``files/`` blobs, by name."""
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT name, blob_key, size FROM output_files WHERE output_id = %s ORDER BY name",
                (output_id,),
            ).fetchall()
        return [_file(item) for item in found]

    def file(self, output_id: str, name: str) -> OutputFileBlob | None:
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT name, blob_key, size FROM output_files WHERE output_id = %s AND name = %s",
                (output_id, name),
            ).fetchone()
        return None if found is None else _file(found)

    def derived(self, output_id: str, kind: str, plate: int) -> tuple[str, Any] | None:
        """What a reader derived of the output's ``plate``: its version and data."""
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT version, data FROM output_derived"
                " WHERE output_id = %s AND kind = %s AND plate = %s",
                (output_id, kind, plate),
            ).fetchone()
        return None if found is None else (str(found["version"]), found["data"])

    def write_derived(self, output_id: str, kind: str, plate: int, version: str, data: Any) -> None:
        """Keep what a reader derived; it replaces the plate's earlier data. Nothing is
        written when the row is gone."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    "INSERT INTO output_derived (output_id, kind, plate, version, data)"
                    " SELECT %s, %s, %s, %s, %s"
                    " WHERE EXISTS (SELECT 1 FROM outputs WHERE output_id = %s)"
                    " ON CONFLICT (output_id, kind, plate)"
                    " DO UPDATE SET version = EXCLUDED.version, data = EXCLUDED.data",
                    (output_id, kind, plate, version, Jsonb(data), output_id),
                )
        except psycopg.errors.ForeignKeyViolation:
            # The row went between the check and the insert.
            return

    def write_derived_many(
        self, kind: str, plate: int, version: str, items: Sequence[tuple[str, Any]]
    ) -> None:
        """`write_derived` of ``(output_id, data)`` for many outputs, in one statement and
        one transaction (prod commits take 1-2 s each). A row gone meanwhile is left out."""
        if not items:
            return
        ids = [output_id for output_id, _ in items]
        data = [Jsonb(value) for _, value in items]
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                with self._pool.connection() as conn:
                    conn.execute(
                        "INSERT INTO output_derived (output_id, kind, plate, version, data)"
                        " SELECT item.output_id, %s, %s, %s, item.data"
                        " FROM unnest(%s::text[], %s::jsonb[]) AS item (output_id, data)"
                        " JOIN outputs USING (output_id)"
                        " ON CONFLICT (output_id, kind, plate)"
                        " DO UPDATE SET version = EXCLUDED.version, data = EXCLUDED.data",
                        (kind, plate, version, ids, data),
                    )
                return
            except psycopg.errors.ForeignKeyViolation:
                # A row went between the join and the insert: the next try leaves it out.
                if attempt == _ATTEMPTS:
                    raise

    def keep_plate_cover(self, output_id: str, version: str, key: str | None) -> None:
        """Keep the output's plate cover (`output_derived` kind ``plate_cover``, plate 0):
        ``key``, a blob already put under `plate_cover_key`, held by the output in the
        same transaction; ``None`` when the 3MF has no cover. Nothing is written when the
        row is gone, and the blob is then held by nothing, so the sweep removes it."""
        with self._pool.connection() as conn, conn.transaction():
            found = conn.execute(
                "SELECT 1 FROM outputs WHERE output_id = %s FOR SHARE", (output_id,)
            ).fetchone()
            if found is None:
                return
            conn.execute(
                "INSERT INTO output_derived (output_id, kind, plate, version, data)"
                " VALUES (%s, 'plate_cover', 0, %s, %s) ON CONFLICT (output_id, kind, plate)"
                " DO UPDATE SET version = EXCLUDED.version, data = EXCLUDED.data",
                (output_id, version, Jsonb({"key": key})),
            )
            if key is not None:
                _hold_keys(conn, output_id, [key])

    def blob_shas(self, output_ids: Sequence[str]) -> dict[tuple[str, Role], str]:
        """The sha256 of each ``(output_id, role)`` blob the rows name, for rows already
        in blobs (``bytes_at`` set), from the index: one query for a whole listing
        (`OutputStore.role_versions`). A role with no such blob is left out."""
        if not output_ids:
            return {}
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT o.output_id, role.name, b.sha256 FROM outputs o"
                " CROSS JOIN LATERAL (VALUES ('model', o.model_blob),"
                " ('preview', o.preview_blob), ('thumbnail', o.thumbnail_blob))"
                " AS role (name, key)"
                " JOIN store_blobs b ON b.key = role.key"
                " WHERE o.output_id = ANY(%s) AND o.bytes_at IS NOT NULL",
                (list(output_ids),),
            ).fetchall()
        return {(str(item["output_id"]), item["name"]): str(item["sha256"]) for item in found}

    def byte_states(self) -> dict[str, tuple[str, str | None, datetime | None]]:
        """Each row's slug, ``bytes_stamp`` and ``bytes_at``, by output id: what the byte
        copy compares the files with."""
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT output_id, slug, bytes_stamp, bytes_at FROM outputs"
            ).fetchall()
        return {
            str(item["output_id"]): (str(item["slug"]), item["bytes_stamp"], item["bytes_at"])
            for item in found
        }

    def write_bytes(self, writes: Sequence[ByteWrite]) -> set[str]:
        """One batch of the byte copy, in one transaction. Per write: the blob columns,
        ``bytes_stamp``, ``bytes_damaged`` and ``bytes_at``, its ``output_files`` rows
        replaced, a hold of each key, and the holds of byte keys it no longer names
        released. A row deleted meanwhile gets nothing, no hold either. The ids whose
        row still existed."""
        written: set[str] = set()
        if not writes:
            return written
        with self._pool.connection() as conn, conn.transaction():
            for write in writes:
                blobs = write.blobs
                cursor = conn.execute(
                    "UPDATE outputs SET model_blob = %s, preview_blob = %s,"
                    " thumbnail_blob = %s, bytes_stamp = %s, bytes_damaged = %s,"
                    " bytes_at = clock_timestamp() WHERE output_id = %s",
                    (
                        blobs.model,
                        blobs.preview,
                        blobs.thumbnail,
                        write.stamp,
                        _json(write.damaged),
                        write.output_id,
                    ),
                )
                if not cursor.rowcount:
                    continue
                written.add(write.output_id)
                conn.execute("DELETE FROM output_files WHERE output_id = %s", (write.output_id,))
                _write_files(conn, write.output_id, blobs.files)
                keys = blobs.keys()
                conn.execute(
                    f"{_RELEASE_BYTES} AND NOT key = ANY(%s)",
                    (OUTPUT_HOLDER, [write.output_id], keys),
                )
                _hold_keys(conn, write.output_id, keys)
        return written

    def pending_bytes(self) -> int:
        """How many rows the files still hold the bytes of."""
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT count(*) AS n FROM outputs WHERE bytes_at IS NULL"
            ).fetchone()
        assert found is not None
        return int(found["n"])

    def start_byte_run(self) -> int:
        with self._pool.connection() as conn:
            found = conn.execute(
                "INSERT INTO output_byte_runs DEFAULT VALUES RETURNING id"
            ).fetchone()
        assert found is not None
        return int(found["id"])

    def end_byte_run(
        self,
        run_id: int,
        *,
        finished: bool,
        incomplete: bool,
        refused: str | None,
        copied: int,
        verified: int,
        skipped: int,
        needed_bytes: int,
        needed_count: int,
        failed: list[dict[str, Any]],
        damaged: list[dict[str, Any]],
    ) -> None:
        """The run's counts. Only a ``finished`` run gets ``finished_at``."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE output_byte_runs SET"
                " finished_at = CASE WHEN %s THEN clock_timestamp() END, incomplete = %s,"
                " refused = %s, copied = %s, verified = %s, skipped = %s,"
                " needed_bytes = %s, needed_count = %s, failed = %s, damaged = %s"
                " WHERE id = %s",
                (
                    finished,
                    incomplete,
                    refused,
                    copied,
                    verified,
                    skipped,
                    needed_bytes,
                    needed_count,
                    Jsonb(failed),
                    Jsonb(damaged),
                    run_id,
                ),
            )

    def last_byte_run(self) -> dict[str, Any] | None:
        with self._pool.connection() as conn:
            found = conn.execute(
                f"SELECT {_BYTE_RUN_COLUMNS} FROM output_byte_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if found is None else dict(found)

    def clock(self) -> datetime:
        """The database's clock now, as every ``copied_at`` is: the copy's mark."""
        with self._pool.connection() as conn:
            found = conn.execute("SELECT clock_timestamp() AS now").fetchone()
        assert found is not None
        now: datetime = found["now"]
        return now

    def write_copy(
        self, new: Sequence[OutputRow], changed: Sequence[OutputRow], mark: datetime
    ) -> tuple[set[str], set[str]]:
        """One batch of the copy, in one transaction, with ``copied_at`` set to ``mark``
        (taken before the batch's folders were read). A ``new`` row is added unless a
        save added it meanwhile; a ``changed`` row is written only when no writer touched
        it since ``mark``, and never when it was deleted. The ids added and refreshed."""
        added: set[str] = set()
        refreshed: set[str] = set()
        if not new and not changed:
            return added, refreshed
        with self._pool.connection() as conn, conn.transaction():
            for row in new:
                cursor = conn.execute(_COPY_INSERT, (*_values(row), row.files_stamp, mark))
                if cursor.rowcount:
                    added.add(row.output_id)
            for row in changed:
                cursor = conn.execute(
                    _COPY_REFRESH,
                    (*_values(row)[1:], row.files_stamp, mark, row.output_id, mark),
                )
                if cursor.rowcount:
                    refreshed.add(row.output_id)
        return added, refreshed

    def undo_copy(self, output_ids: Iterable[str], mark: datetime) -> set[str]:
        """Delete the rows the copy wrote at ``mark`` whose folder went after its read,
        and their byte holds; a row a writer touched since stays. The ids deleted."""
        ids = list(output_ids)
        if not ids:
            return set()
        with self._pool.connection() as conn, conn.transaction():
            deleted = _ids(
                conn.execute(
                    "DELETE FROM outputs WHERE output_id = ANY(%s) AND copied_at = %s"
                    " RETURNING output_id",
                    (ids, mark),
                ).fetchall()
            )
            _release_bytes(conn, deleted)
        return set(deleted)

    def drop_unseen(self, output_ids: Iterable[str], started: datetime) -> int:
        """The copy's drop, in one transaction: the rows of folders the walk did not see,
        unless a writer touched them after the run started, and their byte holds. How
        many went."""
        ids = list(output_ids)
        if not ids:
            return 0
        with self._pool.connection() as conn, conn.transaction():
            deleted = _ids(
                conn.execute(
                    "DELETE FROM outputs WHERE output_id = ANY(%s) AND copied_at < %s"
                    " RETURNING output_id",
                    (ids, started),
                ).fetchall()
            )
            _release_bytes(conn, deleted)
        return len(deleted)

    def documents(self, asset_ids_version: str) -> list[tuple[str, bool, str, list[str] | None]]:
        """Each row's id, whether its ``params`` is ``{}``, the text of every JSON column
        it holds (its meta and records), and for such a row the asset ids its 3MF names,
        as kept (`output_derived` kind ``asset_ids`` of ``asset_ids_version``; ``None``
        when not kept yet): what the asset sweep scans for the ids an output names, as it
        scanned the folder's JSON files (#7256)."""
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT o.output_id, o.params = '{}'::jsonb AS bare,"
                " jsonb_build_array(o.meta, o.params, o.inputs, o.record, o.manifest, o.bom,"
                " o.arranged_from, o.backfill)::text AS text, d.data AS kept FROM outputs o"
                " LEFT JOIN output_derived d ON d.output_id = o.output_id"
                " AND d.kind = 'asset_ids' AND d.plate = 0 AND d.version = %s"
                " AND o.params = '{}'::jsonb",
                (asset_ids_version,),
            ).fetchall()
        return [
            (
                str(item["output_id"]),
                bool(item["bare"]),
                str(item["text"]),
                None if item["kept"] is None else [str(each) for each in item["kept"]],
            )
            for item in found
        ]

    def stamps(self) -> dict[str, str | None]:
        """Each row's ``files_stamp``, by output id: what the copy compares the files with."""
        with self._pool.connection() as conn:
            found = conn.execute("SELECT output_id, files_stamp FROM outputs").fetchall()
        return {str(item["output_id"]): item["files_stamp"] for item in found}

    def update_meta(self, output_id: str, meta: OutputMeta) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE outputs SET meta = %s, copied_at = clock_timestamp() WHERE output_id = %s",
                (_meta_json(meta), output_id),
            )

    def set_backfill(self, output_id: str, backfill: dict[str, Any] | None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE outputs SET backfill = %s, copied_at = clock_timestamp()"
                " WHERE output_id = %s",
                (_json(backfill), output_id),
            )

    def attach_backfill(self, output_id: str, record: dict[str, Any], manifest: list[Any]) -> None:
        """A backfill's attach in one statement: the record it has stays, the manifest
        is the re-render's, and the backfill is cleared."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE outputs SET record = COALESCE(record, %s), manifest = %s,"
                " backfill = NULL, copied_at = clock_timestamp() WHERE output_id = %s",
                (_json(record), _json(manifest), output_id),
            )

    def delete(self, output_id: str) -> None:
        """Delete the row and its byte holds; its Parts' holds stay (`release_parts`)."""
        with self._pool.connection() as conn, conn.transaction():
            deleted = _ids(
                conn.execute(
                    "DELETE FROM outputs WHERE output_id = %s RETURNING output_id", (output_id,)
                ).fetchall()
            )
            _release_bytes(conn, deleted)

    def delete_slug(self, slug: str) -> None:
        """A model's delete: its rows and their byte holds (Ruling R4). Their Parts'
        holds stay, as before."""
        with self._pool.connection() as conn, conn.transaction():
            deleted = _ids(
                conn.execute(
                    "DELETE FROM outputs WHERE slug = %s RETURNING output_id", (slug,)
                ).fetchall()
            )
            _release_bytes(conn, deleted)

    def pending_backfills(self) -> list[tuple[str, dict[str, Any]]]:
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT output_id, backfill FROM outputs WHERE backfill IS NOT NULL"
                " ORDER BY created_at, output_id"
            ).fetchall()
        return [(str(item["output_id"]), item["backfill"]) for item in found]

    def oldest_with_plate_cover(self, slug: str) -> str | None:
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT output_id FROM outputs WHERE slug = %s AND has_plate_cover"
                " ORDER BY created_at, output_id LIMIT 1",
                (slug,),
            ).fetchone()
        return None if found is None else str(found["output_id"])

    def ids_copied_before(self, before: datetime) -> set[str]:
        """The rows the running copy did not refresh: written before ``before``."""
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT output_id FROM outputs WHERE copied_at < %s", (before,)
            ).fetchall()
        return {str(item["output_id"]) for item in found}

    def copied(self) -> bool:
        """Whether a copy run has finished. Once true it stays true for this object."""
        with self._lock:
            if self._copied:
                return True
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT 1 FROM output_copy_runs WHERE finished_at IS NOT NULL LIMIT 1"
            ).fetchone()
        if found is None:
            return False
        with self._lock:
            self._copied = True
        return True

    def start_run(self) -> int:
        with self._pool.connection() as conn:
            found = conn.execute(
                "INSERT INTO output_copy_runs DEFAULT VALUES RETURNING id"
            ).fetchone()
        assert found is not None
        return int(found["id"])

    def run_started_at(self, run_id: int) -> datetime:
        """When the run started, by the database's clock, as every ``copied_at`` is."""
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT started_at FROM output_copy_runs WHERE id = %s", (run_id,)
            ).fetchone()
        if found is None:
            raise LookupError(f"no copy run {run_id}")
        started: datetime = found["started_at"]
        return started

    def finish_run(
        self,
        run_id: int,
        *,
        copied: int,
        refreshed: int,
        dropped: int,
        not_copied: list[str],
        damaged: list[dict[str, Any]],
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE output_copy_runs SET finished_at = clock_timestamp(), copied = %s,"
                " refreshed = %s, dropped = %s, not_copied = %s, damaged = %s WHERE id = %s",
                (copied, refreshed, dropped, Jsonb(not_copied), Jsonb(damaged), run_id),
            )

    def end_incomplete_run(
        self,
        run_id: int,
        *,
        copied: int,
        refreshed: int,
        dropped: int,
        not_copied: list[str],
        damaged: list[dict[str, Any]],
    ) -> None:
        """The end of a run whose walk may have missed folders: its counts and
        ``incomplete``, and no ``finished_at``, so it does not make `copied` true."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE output_copy_runs SET incomplete = true, copied = %s, refreshed = %s,"
                " dropped = %s, not_copied = %s, damaged = %s WHERE id = %s",
                (copied, refreshed, dropped, Jsonb(not_copied), Jsonb(damaged), run_id),
            )

    def last_run(self) -> dict[str, Any] | None:
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT id, started_at, finished_at, incomplete, copied, refreshed, dropped,"
                " not_copied, damaged FROM output_copy_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if found is None else dict(found)
