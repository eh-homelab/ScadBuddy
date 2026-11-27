"""`OutputRows`: every saved output as one row of ``outputs`` (#7255, outputs-in-Postgres
spec 2026-11-23 §4.2).

Phase 2 (#7256, spec §4.2, §4.3): the row also names the blobs that hold the output's
bytes (``model_blob``, ``preview_blob``, ``thumbnail_blob``, and ``output_files`` for
``files/``), and keeps what readers derive from the model (``output_derived``). Each
blob is held in ``blob_refs`` by ``(OUTPUT_HOLDER, output_id)``; every write of a key
writes its hold in the same transaction, and every delete of a row releases its byte
holds. Since #7257 the rows and the blobs are the only record: nothing reads the files
under ``data/outputs/``.

It is synchronous: callers on the loop use a thread. It imports
`OutputMeta` only inside a function, because `outputs.py` imports this module.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, get_args

from psycopg import Connection, Rollback
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.library.output_query import OutputQuery, query_ids
from scadbuddy.store.content_models import BlobRef
from scadbuddy.store.index import BlobIndex

if TYPE_CHECKING:
    from pathlib import Path

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


def plate_cover_prefix(output_id: str) -> str:
    """What every key of the output's plate cover starts with (`plate_cover_key`)."""
    return f"output-{output_id}-plate_cover-"


def plate_cover_key(output_id: str, sha256: str) -> str:
    """The blob key of the output's plate cover of ``sha256``: the 3MF's ``plate_1.png``,
    put once on its first read (`OutputRows.keep_plate_cover`) and held by the output
    like its other blobs, so a model delete releases it with them. The key names the
    cover's own sha256, so a reader of an old model that puts its cover late never
    changes the bytes of the key that the row names."""
    return f"{plate_cover_prefix(output_id)}{sha256}"


def thumbnail_lock_key(output_id: str) -> str:
    """The text whose ``hashtextextended(…, 0)`` keys the output's thumbnail lock: a
    Postgres advisory transaction lock, so it holds across the API's replicas. Every
    writer of the thumbnail key takes it (`lock_thumbnail`): a thumbnail upload's step
    (`OutputRows.keep_thumbnail`) and a drawn thumbnail's step
    (`OutputRows.draw_thumbnail`)."""
    return f"output-thumbnail:{output_id}"


def lock_thumbnail(conn: Connection[DictRow], output_id: str) -> None:
    """Take the output's thumbnail lock until ``conn``'s transaction ends."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (thumbnail_lock_key(output_id),)
    )


def output_file_key(output_id: str, name: str) -> str:
    """The blob key of the output's ``files/`` entry ``name``: a hash of the name (spec
    §4.3), so a name added or gone never moves the key of another name, and a reader of
    an older ``output_files`` row never gets another file's bytes (#7478 review)."""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
    return f"output-{output_id}-file-{digest}"


@dataclass(frozen=True)
class OutputFileBlob:
    name: str
    blob_key: str
    size: int


@dataclass(frozen=True)
class StagedBlob:
    """A save's put: the object ``ref``, uploaded with no index row
    (`ContentStore.stage`), that the output's ``key`` takes in the row's transaction.
    ``reused``: an existing object another row names, which that transaction holds
    (`BlobIndex.put_in` ``reuse``). ``path`` is the file it was read from, for the save's
    own copy when the sweep freed a reused object first (`OutputBytes.own_copies`)."""

    key: str
    ref: BlobRef
    reused: bool
    path: Path


@dataclass(frozen=True)
class OutputBlobs:
    """The blob keys of an output's bytes. ``None``: the role has no blob yet."""

    model: str | None = None
    preview: str | None = None
    thumbnail: str | None = None
    files: tuple[OutputFileBlob, ...] = ()
    #: A save's puts (`OutputBytes.put_folder`). Each key takes its object only in the
    #: transaction that writes the row (`OutputRows.insert`), after the row's insert, so
    #: a save that fails on the id never changes the bytes of the output that holds it.
    staged: tuple[StagedBlob, ...] = ()

    def keys(self) -> list[str]:
        """Every key, the roles first, then the files in their order."""
        roles = [key for key in (self.model, self.preview, self.thumbnail) if key is not None]
        return roles + [item.blob_key for item in self.files]


_COLUMNS = (
    "output_id, slug, meta, params, inputs, record, manifest, bom, arranged_from,"
    " backfill, has_plate_cover, model_blob, preview_blob,"
    " thumbnail_blob, bytes_at, params_text"
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
    #: The blob keys of its bytes. `OutputRows.get` leaves ``files`` empty: read them with
    #: `OutputRows.files`.
    blobs: OutputBlobs = field(default_factory=OutputBlobs)
    #: When a save or the byte copy put its blobs; ``None`` while the files hold them.
    #: The store sets it, so the value given is ignored.
    bytes_at: datetime | None = None
    #: The parameters that differ from the template's defaults, for the list's ``q``
    #: (`output_query.params_text`, #7257). NOT NULL since release B's migration: a row
    #: with no `output_index` text holds '' and matches `q` by its name only.
    params_text: str = ""


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
        blobs=OutputBlobs(
            model=data["model_blob"],
            preview=data["preview_blob"],
            thumbnail=data["thumbnail_blob"],
        ),
        bytes_at=data["bytes_at"],
        params_text=data["params_text"],
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
    " thumbnail_blob, params_text, bytes_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
    " CASE WHEN %s THEN clock_timestamp() END)"
)


def _ids(found: list[DictRow]) -> list[str]:
    return [str(item["output_id"]) for item in found]


def _release_bytes(conn: Connection[DictRow], output_ids: Sequence[str]) -> None:
    """Release the byte holds of the rows ``output_ids`` deleted, in ``conn``'s
    transaction."""
    if output_ids:
        conn.execute(_RELEASE_BYTES, (OUTPUT_HOLDER, list(output_ids)))


def _write_files(
    conn: Connection[DictRow], output_id: str, files: Sequence[OutputFileBlob]
) -> None:
    if files:
        with conn.cursor() as cursor:
            cursor.executemany(
                _INSERT_FILE, [(output_id, item.name, item.blob_key, item.size) for item in files]
            )


#: A derive write's check, on the row ``o``, that the model it read is the one the row
#: names now: the files hold the bytes (``bytes_at`` NULL), or the index's sha256 of the
#: row's model key is the one read. Put in with ``%``; a file read gives NULL, which
#: matches no blob.
_MODEL_READ = (
    "(o.bytes_at IS NULL OR EXISTS (SELECT 1 FROM store_blobs b"
    " WHERE b.key = o.model_blob AND b.sha256 = %s::text))"
)


def _lock_for_derive(conn: Connection[DictRow], output_ids: Sequence[str]) -> None:
    """Lock the rows FOR SHARE, in output id order, in the caller's transaction, before a
    derive write checks the model's sha256 in a statement of its own. A writer that moves
    the model key and drops what was derived (the tests' `drop_derived`, #7887) takes the
    row FOR UPDATE: so the check sees the key the drop followed, or the drop waits for the
    write's commit and deletes what it wrote."""
    conn.execute(
        "SELECT 1 FROM outputs WHERE output_id = ANY(%s) ORDER BY output_id FOR SHARE",
        (list(output_ids),),
    ).fetchall()


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


#: What a thumbnail step or a save's row runs again after, as `BlobIndex.retrying` runs an
#: index write again: Postgres broke a deadlock with it (`BlobIndex.put_in` can still take
#: a hold out of key order), or a serialization failure.
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
                    "retrying an output step Postgres picked as a deadlock's victim",
                    extra={"output_id": output_id, "attempt": attempt},
                )
    raise AssertionError("unreachable")


def _insert(
    conn: Connection[DictRow], pool: ConnectionPool[Connection[DictRow]], row: OutputRow
) -> list[tuple[str, BlobRef]]:
    blobs = row.blobs
    values = (
        *_values(row),
        blobs.model,
        blobs.preview,
        blobs.thumbnail,
        row.params_text,
        blobs.model is not None,
    )
    # Another row of the id: the insert raises its `UniqueViolation`.
    conn.execute(_INSERT, values)
    # Only now, with the row the save's, do its keys take the objects it put: their index
    # rows are written here, in this one transaction (#7477), in key order as `mark` locks
    # them. A key named another object before is released by the caller after the
    # commit. `ReuseLostError`: a reused object was freed first; the transaction rolls
    # back whole and the caller puts its own copies.
    replaced: list[tuple[str, BlobRef]] = []
    index = BlobIndex(pool)
    for staged in sorted(blobs.staged, key=lambda item: item.key):
        previous = index.put_in(
            conn, staged.key, staged.ref, slug=row.slug, meta={}, reuse=staged.reused
        )
        if previous is not None and previous.backend_id != staged.ref.backend_id:
            replaced.append((staged.key, previous))
    _write_files(conn, row.output_id, blobs.files)
    _hold_keys(conn, row.output_id, blobs.keys())
    # The id is saved now: its claim (`OutputRows.claim`) goes with the save (#7257).
    conn.execute("DELETE FROM output_claims WHERE output_id = %s", (row.output_id,))
    return replaced


#: What a retired copy activity logs: it does nothing, the outputs are cut over (plan D4).
CUT_OVER_NOTHING_TO_COPY = "the outputs are cut over (#7257); nothing to copy"


class OutputRows:
    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

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

    def ids_for_slug(self, slug: str) -> list[str]:
        """The id of every output of ``slug``, oldest first: one query (#7257)."""
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT output_id FROM outputs WHERE slug = %s ORDER BY created_at, output_id",
                (slug,),
            ).fetchall()
        return [str(item["output_id"]) for item in found]

    def slugs(self) -> set[str]:
        """Every slug that has an output: one query (#7257). The orphan sweep drops the
        rows of a slug whose model is gone (`Catalogue.sweep_orphans`)."""
        with self._pool.connection() as conn:
            found = conn.execute("SELECT DISTINCT slug FROM outputs").fetchall()
        return {str(item["slug"]) for item in found}

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

    def insert(
        self, row: OutputRow, conn: Connection[DictRow] | None = None
    ) -> list[tuple[str, BlobRef]]:
        """Add a save's row, with its blob keys, its ``output_files`` rows and a hold of
        each key; each key takes its staged object (`OutputBlobs.staged`) after the row's
        insert. A row that already holds the id is a `UniqueViolation`, and then nothing
        is written and no key moves. With ``conn``, inside its transaction.

        Without ``conn``, a deadlock or a serialization failure runs the transaction again
        whole (`_retrying`): its index rows are the save's, all in it (#7477). The last
        attempt's error propagates, and no key moved.

        Returns each key that named another object before and that object, which the
        caller releases after the commit (`OutputBytes.release_replaced`)."""
        if conn is not None:
            return _insert(conn, self._pool, row)

        def step(own: Connection[DictRow]) -> list[tuple[str, BlobRef]]:
            with own.transaction():
                return _insert(own, self._pool, row)

        return _retrying(self._pool, row.output_id, step)

    def keep_thumbnail(
        self, output_id: str, key: str, staged: BlobRef, *, slug: str
    ) -> tuple[bool, BlobRef | None]:
        """A thumbnail upload's step, in one transaction under the output's thumbnail
        lock (`lock_thumbnail`): ``key`` takes the object of ``staged`` (a put under
        `staged_thumbnail_key`), then the row names ``key`` and holds it. No file is
        written (#7257). A deadlock or a serialization failure runs the step again
        (`_retrying`).

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
                _set_blob(conn, output_id, "thumbnail", key)
            return True, previous

        return _retrying(self._pool, output_id, step)

    def draw_thumbnail(self, output_id: str, key: str, staged: BlobRef, *, slug: str) -> bool:
        """A drawn thumbnail's step (#4775), in one transaction under the output's
        thumbnail lock: only while ``key`` has no index row, ``key`` takes the object of
        ``staged`` and the row names ``key`` and holds it. False, and nothing written, when
        the output has no row or ``key`` has an index row (an upload's, or a blob the
        store lost: a draw never replaces it, #7257). Blocking."""

        def step(conn: Connection[DictRow]) -> bool:
            with conn.transaction():
                lock_thumbnail(conn, output_id)
                found = conn.execute(
                    "SELECT 1 FROM outputs WHERE output_id = %s FOR UPDATE", (output_id,)
                ).fetchone()
                if found is None:
                    return False
                there = conn.execute("SELECT 1 FROM store_blobs WHERE key = %s", (key,)).fetchone()
                if there is not None:
                    return False
                BlobIndex(self._pool).put_in(conn, key, staged, slug=slug, meta={}, reuse=True)
                _set_blob(conn, output_id, "thumbnail", key)
                return True

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

    def write_derived(
        self,
        output_id: str,
        kind: str,
        plate: int,
        version: str,
        data: Any,
        *,
        model_sha: str | None,
    ) -> None:
        """Keep what a reader derived from the model of ``model_sha`` (``None``: the
        file); it replaces the plate's earlier data. Nothing is written when the row is
        gone, or when the model key names other bytes now (`_MODEL_READ`)."""
        self.write_derived_many(kind, plate, version, [(output_id, data, model_sha)])

    def write_derived_many(
        self,
        kind: str,
        plate: int,
        version: str,
        items: Sequence[tuple[str, Any, str | None]],
    ) -> None:
        """`write_derived` of ``(output_id, data, model_sha)`` for many outputs, in one
        transaction (prod commits take 1-2 s each). A row gone meanwhile, or one whose
        model key names other bytes now, is left out. The rows are locked first
        (`_lock_for_derive`), so the check of the model's sha256 runs after
        a drop of a moved key's derived rows, or before it starts."""
        if not items:
            return
        ids = [output_id for output_id, _, _ in items]
        data = [Jsonb(value) for _, value, _ in items]
        shas = [sha for _, _, sha in items]
        with self._pool.connection() as conn, conn.transaction():
            _lock_for_derive(conn, ids)
            conn.execute(
                "INSERT INTO output_derived (output_id, kind, plate, version, data)"
                " SELECT item.output_id, %s, %s, %s, item.data"
                " FROM unnest(%s::text[], %s::jsonb[], %s::text[])"
                " AS item (output_id, data, model_sha)"
                f" JOIN outputs o USING (output_id) WHERE {_MODEL_READ % 'item.model_sha'}"
                " ON CONFLICT (output_id, kind, plate)"
                " DO UPDATE SET version = EXCLUDED.version, data = EXCLUDED.data",
                (kind, plate, version, ids, data, shas),
            )

    def keep_plate_cover(
        self, output_id: str, version: str, key: str | None, *, model_sha: str | None
    ) -> None:
        """Keep the output's plate cover (`output_derived` kind ``plate_cover``, plate 0),
        read from the model of ``model_sha`` (``None``: the file): ``key``, a blob already
        put under `plate_cover_key`, held by the output in the same transaction; ``None``
        when the 3MF has no cover. The output's holds of other cover keys are released
        in the same transaction, so the sweep removes the replaced cover after its grace
        (a reader that read its key before this commit can still read it). Nothing is
        written when the row is gone, or when the model key names other bytes now; the
        blob is then held by nothing, so the sweep removes it."""
        with self._pool.connection() as conn, conn.transaction():
            _lock_for_derive(conn, [output_id])
            found = conn.execute(
                f"SELECT 1 FROM outputs o WHERE o.output_id = %s AND {_MODEL_READ % '%s'}",
                (output_id, model_sha),
            ).fetchone()
            if found is None:
                return
            conn.execute(
                "INSERT INTO output_derived (output_id, kind, plate, version, data)"
                " VALUES (%s, 'plate_cover', 0, %s, %s) ON CONFLICT (output_id, kind, plate)"
                " DO UPDATE SET version = EXCLUDED.version, data = EXCLUDED.data",
                (output_id, version, Jsonb({"key": key})),
            )
            conn.execute(
                "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s"
                " AND starts_with(key, %s) AND key IS DISTINCT FROM %s",
                (OUTPUT_HOLDER, output_id, plate_cover_prefix(output_id), key),
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

    def update_meta(self, output_id: str, meta: OutputMeta) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE outputs SET meta = %s WHERE output_id = %s",
                (_meta_json(meta), output_id),
            )

    def set_backfill(self, output_id: str, backfill: dict[str, Any] | None) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE outputs SET backfill = %s WHERE output_id = %s",
                (_json(backfill), output_id),
            )

    def attach_backfill(self, output_id: str, record: dict[str, Any], manifest: list[Any]) -> bool:
        """A backfill's attach in one statement: the record it has stays, the manifest
        is the re-render's, and the backfill is cleared. False when the row is gone."""
        with self._pool.connection() as conn:
            done = conn.execute(
                "UPDATE outputs SET record = COALESCE(record, %s), manifest = %s,"
                " backfill = NULL WHERE output_id = %s",
                (_json(record), _json(manifest), output_id),
            )
            return done.rowcount == 1

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

    def plate_covers_for(self, slugs: Sequence[str]) -> dict[str, str]:
        """Each slug's oldest output with a plate cover, as `oldest_with_plate_cover`
        answers it for one, in one query (#7317). A slug with none is left out."""
        with self._pool.connection() as conn:
            found = conn.execute(
                "SELECT DISTINCT ON (slug) slug, output_id FROM outputs"
                " WHERE slug = ANY(%s) AND has_plate_cover"
                " ORDER BY slug, created_at, output_id",
                (list(slugs),),
            ).fetchall()
        return {str(item["slug"]): str(item["output_id"]) for item in found}

    def query(self, query: OutputQuery) -> tuple[list[str], int]:
        """The outputs list's page (#4777, #7257): its output ids, newest first, and how
        many the filters select. ``after`` must be an id the filters select, else
        `AfterNotListedError`."""
        with self._pool.connection() as conn:
            return query_ids(conn, query)

    def claim(self, output_id: str, slug: str) -> bool:
        """Claim ``output_id`` for a save of ``slug`` (#7257, plan D6): True when this
        call claimed it. False when a claim of it stands, or an output of that id is
        saved. The save's row deletes the claim (`insert`).

        A save that holds the claim inserts its row and deletes its claim in one
        transaction. A claim made before that commits waits on the deleted claim row and
        then inserts, since its snapshot saw no ``outputs`` row. So a second statement in
        the claim's transaction looks again: under READ COMMITTED it takes a new snapshot,
        which holds the save's row, and the claim is rolled back. (An advisory lock per id
        would need every writer of ``outputs`` to take it, and phase 2 pods do not.)"""
        with self._pool.connection() as conn, conn.transaction() as tx:
            inserted = (
                conn.execute(
                    "INSERT INTO output_claims (output_id, slug) SELECT %s, %s"
                    " WHERE NOT EXISTS (SELECT 1 FROM outputs WHERE output_id = %s)"
                    " ON CONFLICT DO NOTHING",
                    (output_id, slug, output_id),
                ).rowcount
                == 1
            )
            if not inserted:
                return False
            saved = conn.execute(
                "SELECT 1 FROM outputs WHERE output_id = %s", (output_id,)
            ).fetchone()
            if saved is not None:
                raise Rollback(tx)
        return saved is None

    def release_claim(self, output_id: str) -> None:
        """Free a claim whose save failed, so another attempt can claim the id."""
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM output_claims WHERE output_id = %s", (output_id,))

    def refresh_claim(self, output_id: str) -> None:
        """A live save's claim, made young again (A4 review M1): the prune
        (`prune_claims`) never takes a claim whose save still runs. Nothing when the save's
        row has deleted it, or the prune took it first (logged)."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "UPDATE output_claims SET claimed_at = clock_timestamp() WHERE output_id = %s",
                (output_id,),
            )
            if cursor.rowcount == 0:
                logger.info("a running save's claim was gone at refresh", extra={"id": output_id})

    def prune_claims(self, grace: timedelta) -> int:
        """Delete the claims older than ``grace``, by the database's clock: a save that
        died between its claim and its row. How many went."""
        with self._pool.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM output_claims WHERE claimed_at < clock_timestamp() - %s", (grace,)
            )
        return cursor.rowcount
