"""The assistant's workspace files (#2329), ``ai_session_files``, kept by the API for the
agent's CRUD routes (#5299 step 7, ``api/crud_agent_workspace.py``): what the agent's
``workspace/store.ts`` ``SessionFiles`` ran over SQL, with the file keys sealed and
opened here, since the KEK is only in the API (#5299 D5).

A workspace is a session's (``session:<id>``, deleted with its session) or a principal's
own outside any session (``principal:<kind>:<id>``, swept a day after its last write). A
file's bytes are, on a backend with a content store, a blob ``workspace-<sha256 of the
sealed bytes>`` sealed under a file key of its own in segments (``seal_file``, the
agent's ``workspace/sealing.ts`` format byte for byte, so either side opens what the
other sealed); the row keeps the file key wrapped in the context
``workspace-file-key:<workspace>:<path>:<blob key>``, under the session's payload data key
or, for a principal's file, under the KEK (``key_kek_id``). On ``store_backend`` local
(no content store) the bytes stay in Postgres: one ``ai_session_file_bytes`` row per
sha256, which every file with those bytes names, so a fork shares them (#4212).

A blob is kept while a row names it: its hold ``(ai_session_file, <workspace>/<path>)`` in
``blob_refs`` is written and dropped here in the transaction that writes or deletes the
row, so a hold and its row commit together. ``reconcile`` releases what a session's
cascade, a forget or an older agent's failed release left, and holds a row no hold names.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from scadbuddy.agent_data import settings as agent_settings
from scadbuddy.agent_data.payload_keys import KeyNotHeldError, key_in
from scadbuddy.core.secrets import KEK_BYTES, Kek, SealError, open_bytes, seal_bytes

#: The plaintext of one sealed segment (``workspace/sealing.ts`` ``SEGMENT_BYTES``).
SEGMENT_BYTES = 1024 * 1024
#: What ``seal_bytes`` adds to a segment: the version byte, the IV and the tag.
SEAL_OVERHEAD = 1 + 12 + 16
_SEALED_SEGMENT = SEGMENT_BYTES + SEAL_OVERHEAD

#: ``ai_settings`` keys of the caps, and their defaults (``workspace/store.ts``).
FILE_MAX_SETTING = "workspace_file_max_bytes"
WORKSPACE_MAX_SETTING = "workspace_max_bytes"
DEFAULT_FILE_MAX_BYTES = 25 * 1024 * 1024
DEFAULT_WORKSPACE_MAX_BYTES = 200 * 1024 * 1024
#: The largest file the API takes, whatever the setting says: the most the Settings form
#: can save (``routes/workspaceSettings.ts`` ``MAX_FILE_BYTES``).
FILE_BYTES_CEILING = 100 * 1024 * 1024
#: The most files one workspace holds (``WORKSPACE_MAX_FILES``).
MAX_FILES = 5_000
#: How long a principal's file lives after its last write.
TTL_MS = 24 * 60 * 60 * 1000
#: The lock one reconcile at a time takes: the agent's ``RECONCILE_LOCK``, so an agent pod
#: still on SQL (a session lock on the same key) and the API never reconcile at once.
RECONCILE_LOCK = "scadbuddy:ai_session_files:reconcile"
#: The ``blob_refs.holder_kind`` of a workspace file.
HOLDER_KIND = "ai_session_file"

_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
#: A path: one to four segments (the table's CHECK).
PATH_PATTERN = rf"^{_SEGMENT}(/{_SEGMENT}){{0,3}}$"
PATH_MAX = 512
_PATH = re.compile(PATH_PATTERN)
UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
#: A workspace: a session's, or a principal's of one of the owner kinds
#: (``sessions/protocol.ts`` ``Owner``).
WORKSPACE_PATTERN = (
    rf"^(session:{UUID_PATTERN}"
    r"|principal:(browser|bearer|oidc|anonymous|flow):[^\x00-\x1f\x7f]{1,600})$"
)
WORKSPACE_MAX = 700
_WORKSPACE = re.compile(WORKSPACE_PATTERN)
SHA256_PATTERN = r"^[0-9a-f]{64}$"
#: The events a write or a delete may log in its transaction, and the largest.
EVENTS_MAX = 4
EVENT_BYTES_MAX = 4096
#: The event type a workspace change logs (``sessions/protocol.ts``).
EVENT_TYPE = "workspace.file"

Code = Literal["invalid_path", "not_found", "conflict", "too_large", "quota", "unavailable"]


class WorkspaceRefusedError(Exception):
    """A refusal the agent shows as it is (``WorkspaceError``): never a file's bytes."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code: Code = code
        self.message = message


def unavailable(why: str) -> WorkspaceRefusedError:
    return WorkspaceRefusedError("unavailable", f"{why}; nothing was written")


@dataclass(frozen=True)
class Keks:
    """The API's KEK and, during a rotation, the one before it."""

    current: Kek | None
    previous: Kek | None


def valid_workspace(workspace: str) -> bool:
    return len(workspace) <= WORKSPACE_MAX and _WORKSPACE.match(workspace) is not None


def session_of(workspace: str) -> str | None:
    return workspace[len("session:") :] if workspace.startswith("session:") else None


def holder(workspace: str, path: str) -> str:
    return f"{workspace}/{path}"


def check_path(path: str) -> str:
    """``path`` as stored (a leading ``./`` dropped), or ``invalid_path``."""
    clean = re.sub(r"^(\./)+", "", path)
    if not clean or len(clean) > PATH_MAX or _PATH.match(clean) is None:
        raise WorkspaceRefusedError(
            "invalid_path",
            f'{json.dumps(path[:200])} is not a workspace path: use up to 4 "/"-separated'
            ' parts, each starting with a letter, digit or "_" and holding only letters,'
            ' digits, "_", "." and "-" (e.g. "downloads/part.3mf")',
        )
    return clean


# --- sealing ---------------------------------------------------------------------------


def _segment_context(digest: str, index: int, last: bool) -> str:
    return f"workspace-file:{digest}:{index}:{'end' if last else 'more'}"


def file_key_context(workspace: str, path: str, blob_key: str) -> str:
    return f"workspace-file-key:{workspace}:{path}:{blob_key}"


def seal_file(file_key: bytes, data: bytes, digest: str) -> bytes:
    """``data`` sealed under ``file_key`` segment by segment (``sealing.ts`` ``sealFile``)."""
    parts: list[bytes] = []
    offset = index = 0
    while True:
        end = min(offset + SEGMENT_BYTES, len(data))
        last = end >= len(data)
        parts.append(seal_bytes(file_key, data[offset:end], _segment_context(digest, index, last)))
        if last:
            return b"".join(parts)
        offset, index = end, index + 1


async def open_file(
    file_key: bytes, sealed: AsyncIterator[bytes], digest: str
) -> AsyncIterator[bytes]:
    """The plaintext of a blob ``seal_file`` (or the agent) wrote, a segment at a time as
    it arrives; SealError when a segment does not open (wrong key, altered, reordered,
    cut short)."""
    held = bytearray()
    index = 0
    async for chunk in sealed:
        held += chunk
        # A full segment with bytes after it is not the last.
        while len(held) > _SEALED_SEGMENT:
            yield open_bytes(
                file_key, bytes(held[:_SEALED_SEGMENT]), _segment_context(digest, index, False)
            )
            del held[:_SEALED_SEGMENT]
            index += 1
    if len(held) < SEAL_OVERHEAD:
        raise SealError("a sealed workspace file is cut short")
    tail = open_bytes(file_key, bytes(held), _segment_context(digest, index, True))
    if tail:
        yield tail


def new_file_key() -> bytes:
    return os.urandom(KEK_BYTES)


def _kek_of(keks: Keks, kek_id: str | None) -> Kek | None:
    for kek in (keks.current, keks.previous):
        if kek is not None and kek.id == kek_id:
            return kek
    return None


def _session_key(conn: Connection[Any], session: str, keks: Keks, *, create: bool) -> bytes:
    if keks.current is None:
        raise WorkspaceRefusedError("unavailable", "the API has no SCADBUDDY_SECRET_KEY_FILE")
    try:
        with conn.transaction():
            dek, _made = key_in(
                conn,
                f"session-{session}",
                create=create,
                # A key is made only for a session that stands: a write racing its
                # deletion or its forget never leaves a key nothing deletes.
                standing=True,
                kek=keks.current,
                previous=keks.previous,
            )
    except KeyNotHeldError as err:
        raise WorkspaceRefusedError("unavailable", str(err)) from None
    if dek is None:
        raise WorkspaceRefusedError("unavailable", f"session {session} was deleted or forgotten")
    return dek


def wrap_key(
    conn: Connection[Any], workspace: str, path: str, blob_key: str, file_key: bytes, keks: Keks
) -> tuple[bytes, str | None]:
    """``file_key`` wrapped for the row, and the KEK's id for a principal's file."""
    context = file_key_context(workspace, path, blob_key)
    session = session_of(workspace)
    if session is None:
        if keks.current is None:
            raise WorkspaceRefusedError("unavailable", "the API has no SCADBUDDY_SECRET_KEY_FILE")
        return seal_bytes(keks.current.key, file_key, context), keks.current.id
    dek = _session_key(conn, session, keks, create=True)
    return seal_bytes(dek, file_key, context), None


def unwrap_key(
    conn: Connection[Any],
    workspace: str,
    path: str,
    blob_key: str,
    sealed: bytes,
    kek_id: str | None,
    keks: Keks,
) -> bytes:
    """The row's file key; ``unavailable`` when no key here opens it."""
    context = file_key_context(workspace, path, blob_key)
    session = session_of(workspace)
    try:
        if session is None:
            kek = _kek_of(keks, kek_id)
            if kek is None:
                raise WorkspaceRefusedError(
                    "unavailable",
                    f"a workspace file's key was sealed with key {kek_id}, which the API"
                    " does not hold",
                )
            return open_bytes(kek.key, sealed, context)
        return open_bytes(_session_key(conn, session, keks, create=False), sealed, context)
    except SealError:
        raise WorkspaceRefusedError("unavailable", f"the key of {path} does not open") from None


# --- rows ------------------------------------------------------------------------------

_INFO = "path, media_type, size, sha256, source, created_at, updated_at"


def info_of(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": row["path"],
        "media_type": row["media_type"],
        "size": row["size"],
        "sha256": row["sha256"],
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _cur(conn: Connection[Any]) -> Any:
    return conn.cursor(row_factory=dict_row)


def _positive(value: Any, fallback: int) -> int:
    ok = isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**53
    return value if ok else fallback


def limits(conn: Connection[Any]) -> tuple[int, int]:
    """(the largest file, the most one workspace holds), from the settings or the defaults."""
    stored = agent_settings.read(conn, [FILE_MAX_SETTING, WORKSPACE_MAX_SETTING])
    return (
        _positive(stored.get(FILE_MAX_SETTING), DEFAULT_FILE_MAX_BYTES),
        _positive(stored.get(WORKSPACE_MAX_SETTING), DEFAULT_WORKSPACE_MAX_BYTES),
    )


def list_files(conn: Connection[Any], workspace: str, prefix: str | None) -> list[dict[str, Any]]:
    under = None if prefix is None else f"{check_path(prefix.rstrip('/'))}/"
    rows = (
        _cur(conn)
        .execute(
            f"SELECT {_INFO} FROM ai_session_files WHERE workspace = %s"
            " AND (%s::text IS NULL OR starts_with(path, %s)) ORDER BY path",
            (workspace, under, under),
        )
        .fetchall()
    )
    return [info_of(r) for r in rows]


def usage(conn: Connection[Any], workspace: str) -> tuple[int, int]:
    row = (
        _cur(conn)
        .execute(
            "SELECT count(*)::int AS files, coalesce(sum(size), 0)::bigint AS bytes"
            " FROM ai_session_files WHERE workspace = %s",
            (workspace,),
        )
        .fetchone()
    )
    return (row["files"], int(row["bytes"])) if row else (0, 0)


def list_sessions(conn: Connection[Any], session_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    if not session_ids:
        return out
    rows = (
        _cur(conn)
        .execute(
            f"SELECT workspace, {_INFO}, blob_key IS NOT NULL AS sealed FROM ai_session_files"
            " WHERE workspace = ANY(%s::text[]) ORDER BY workspace, path",
            ([f"session:{i}" for i in session_ids],),
        )
        .fetchall()
    )
    for row in rows:
        out.setdefault(row["workspace"][len("session:") :], []).append(
            {**info_of(row), "sealed": row["sealed"]}
        )
    return out


def stat(conn: Connection[Any], workspace: str, path: str) -> dict[str, Any] | None:
    row = (
        _cur(conn)
        .execute(
            f"SELECT {_INFO} FROM ai_session_files WHERE workspace = %s AND path = %s",
            (workspace, path),
        )
        .fetchone()
    )
    return None if row is None else info_of(row)


def blob_rows(conn: Connection[Any]) -> int:
    row = conn.execute(
        "SELECT count(*)::int FROM ai_session_files WHERE blob_key IS NOT NULL"
    ).fetchone()
    return int(_first(row)) if row else 0


@dataclass(frozen=True)
class Stored:
    """Where a row's bytes are, and what they are."""

    sha256: str
    size: int
    media_type: str
    blob_key: str | None
    key_sealed: bytes | None
    key_kek_id: str | None


def stored(conn: Connection[Any], workspace: str, path: str) -> Stored | None:
    row = (
        _cur(conn)
        .execute(
            "SELECT sha256, size, media_type, blob_key, key_sealed, key_kek_id"
            " FROM ai_session_files WHERE workspace = %s AND path = %s",
            (workspace, path),
        )
        .fetchone()
    )
    if row is None:
        return None
    sealed = row["key_sealed"]
    return Stored(
        row["sha256"],
        row["size"],
        row["media_type"],
        row["blob_key"],
        None if sealed is None else bytes(sealed),
        row["key_kek_id"],
    )


def sha_now(conn: Connection[Any], workspace: str, path: str) -> str | None:
    row = conn.execute(
        "SELECT sha256 FROM ai_session_files WHERE workspace = %s AND path = %s",
        (workspace, path),
    ).fetchone()
    return None if row is None else str(_first(row))


def slice_of(
    conn: Connection[Any], workspace: str, path: str, offset: int, length: int
) -> tuple[bytes | None, int, str] | None:
    """One slice of a row kept in Postgres (``substring``, stored uncompressed for it): the
    part (None once the row names a blob), the size and the sha256; None with no row."""
    row = (
        _cur(conn)
        .execute(
            "SELECT substring(coalesce(f.data, b.data) FROM %s::int FOR %s::int) AS part,"
            " f.size, f.sha256"
            " FROM ai_session_files f LEFT JOIN ai_session_file_bytes b"
            " ON b.sha256 = f.bytes_sha256 WHERE f.workspace = %s AND f.path = %s",
            (offset + 1, length, workspace, path),
        )
        .fetchone()
    )
    if row is None:
        return None
    part = row["part"]
    return (None if part is None else bytes(part)), row["size"], row["sha256"]


Expect = Literal["any", "absent", "sha256"]


def _check_expected(path: str, current: str | None, expect: Expect, expected: str | None) -> None:
    if expect == "absent" and current is not None:
        raise WorkspaceRefusedError(
            "conflict", f"{path} already exists (sha256 {current}); nothing was written"
        )
    if expect == "sha256" and current != expected:
        raise WorkspaceRefusedError(
            "conflict",
            f"{path} changed since it was read: its sha256 is {current}, not {expected};"
            " read it again. Nothing was written"
            if current is not None
            else f"{path} does not exist any more; nothing was written",
        )


def _check_room(held_bytes: int, size: int, held_files: int, workspace_max: int) -> None:
    total = held_bytes + size
    if total > workspace_max:
        raise WorkspaceRefusedError(
            "quota",
            f"the workspace would hold {total} bytes; it holds at most {workspace_max}."
            " Delete files with workspace_delete first",
        )
    if held_files + 1 > MAX_FILES:
        raise WorkspaceRefusedError(
            "quota",
            f"the workspace holds {held_files} files; it holds at most {MAX_FILES}."
            " Delete files with workspace_delete first",
        )


def precheck(
    conn: Connection[Any],
    workspace: str,
    path: str,
    size: int,
    workspace_max: int,
    expect: Expect,
    expected: str | None,
) -> None:
    """The write's conflict and quota checks without the lock, before its upload, so a
    refused write stores nothing; the checks under the lock are the ones that hold."""
    row = (
        _cur(conn)
        .execute(
            "SELECT (SELECT sha256 FROM ai_session_files WHERE workspace = %(w)s AND path = %(p)s)"
            " AS current, (SELECT coalesce(sum(size), 0)::bigint FROM ai_session_files"
            " WHERE workspace = %(w)s AND path <> %(p)s) AS held,"
            " (SELECT count(*)::int FROM ai_session_files"
            " WHERE workspace = %(w)s AND path <> %(p)s) AS files",
            {"w": workspace, "p": path},
        )
        .fetchone()
    )
    assert row is not None
    _check_expected(path, row["current"], expect, expected)
    _check_room(int(row["held"]), size, row["files"], workspace_max)


def check_events(workspace: str, events: list[str]) -> list[str]:
    """The events as given, each a ``workspace.file`` event of the workspace's own session;
    ValueError otherwise. Only a session's workspace logs any."""
    if not events:
        return []
    session = session_of(workspace)
    if session is None:
        raise ValueError("only a session's workspace logs events")
    if len(events) > EVENTS_MAX:
        raise ValueError(f"at most {EVENTS_MAX} events")
    for text in events:
        if len(text.encode()) > EVENT_BYTES_MAX:
            raise ValueError(f"an event is at most {EVENT_BYTES_MAX} bytes")
        parsed = json.loads(text)
        if (
            not isinstance(parsed, dict)
            or parsed.get("type") != EVENT_TYPE
            or parsed.get("sessionId") != session
        ):
            raise ValueError(f"each event is a {EVENT_TYPE} event of session {session}")
    return events


def _lock_session(conn: Connection[Any], session: str | None) -> None:
    """The session's row first, as a session delete takes it before it cascades to the
    files: both take the locks in one order and never deadlock."""
    if session is not None:
        conn.execute("SELECT 1 FROM ai_sessions WHERE id = %s FOR NO KEY UPDATE", (session,))


def _append_events(conn: Connection[Any], session: str, events: list[str]) -> list[int]:
    """``eventLog.ts`` ``append`` in the caller's transaction: the seqs, oldest first."""
    rows = conn.execute(
        "WITH s AS (UPDATE ai_sessions SET event_seq = event_seq + %(n)s, updated_at = now()"
        " WHERE id = %(id)s RETURNING event_seq - %(n)s AS base)"
        " INSERT INTO ai_session_events (session_id, seq, event)"
        " SELECT %(id)s, s.base + e.ord, e.event"
        " FROM s, unnest(%(events)s::text[]) WITH ORDINALITY AS e(event, ord) RETURNING seq",
        {"n": len(events), "id": session, "events": events},
    ).fetchall()
    if len(rows) != len(events):
        raise WorkspaceRefusedError("not_found", f"session {session} does not exist")
    return sorted(int(_first(r)) for r in rows)


def _hold(conn: Connection[Any], blob_key: str, workspace: str, path: str) -> None:
    conn.execute(
        "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES (%s, %s, %s)"
        " ON CONFLICT (key, holder_kind, holder_id) DO UPDATE SET created_at = now()",
        (blob_key, HOLDER_KIND, holder(workspace, path)),
    )


def _release(conn: Connection[Any], blob_key: str, workspace: str, path: str) -> None:
    conn.execute(
        "DELETE FROM blob_refs WHERE key = %s AND holder_kind = %s AND holder_id = %s",
        (blob_key, HOLDER_KIND, holder(workspace, path)),
    )


def _put_bytes(conn: Connection[Any], digest: str, data: bytes) -> None:
    """Locks the bytes row of ``digest`` ``FOR KEY SHARE`` (which ``drop_unnamed_bytes``'s
    ``SKIP LOCKED`` skips), putting it first when it is not there (``store.ts`` ``putBytes``)."""
    while True:
        held = conn.execute(
            "SELECT 1 FROM ai_session_file_bytes WHERE sha256 = %s FOR KEY SHARE", (digest,)
        ).fetchone()
        if held is not None:
            return
        conn.execute(
            "INSERT INTO ai_session_file_bytes (sha256, data) VALUES (%s, %s)"
            " ON CONFLICT (sha256) DO NOTHING",
            (digest, data),
        )


def drop_unnamed_bytes(conn: Connection[Any], shas: list[str] | None = None) -> int:
    """Deletes the ``ai_session_file_bytes`` rows no file names, of ``shas`` or all; a row a
    write or a fork holds ``FOR KEY SHARE`` is skipped."""
    if shas is not None and not shas:
        return 0
    gone = conn.execute(
        "DELETE FROM ai_session_file_bytes WHERE sha256 IN ("
        " SELECT b.sha256 FROM ai_session_file_bytes b"
        " WHERE (%(all)s OR b.sha256 = ANY(%(shas)s::text[]))"
        " AND NOT EXISTS (SELECT 1 FROM ai_session_files f WHERE f.bytes_sha256 = b.sha256)"
        " FOR UPDATE SKIP LOCKED)",
        {"all": shas is None, "shas": shas or []},
    )
    return gone.rowcount


@dataclass(frozen=True)
class Placed:
    """A write's bytes as the row takes them: in Postgres, or a held blob and its key."""

    data: bytes | None = None
    blob_key: str | None = None
    key_sealed: bytes | None = None
    key_kek_id: str | None = None


@dataclass(frozen=True)
class Write:
    workspace: str
    path: str
    media_type: str
    sha256: str
    size: int
    source: dict[str, Any] | None
    expect: Expect
    expected: str | None
    events: list[str]


def write_row(
    conn: Connection[Any], write: Write, placed: Placed, workspace_max: int
) -> tuple[dict[str, Any], list[int], str | None]:
    """The write's transaction: the checks under the workspace's lock, the row, its blob's
    hold (and the old blob's release) and its events. (the file as stored, the events'
    seqs, the bytes row the file named before, which may now be unnamed)."""
    workspace, path = write.workspace, write.path
    session = session_of(workspace)
    with conn.transaction():
        cur = _cur(conn)
        if write.events:
            _lock_session(conn, session)
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"scadbuddy:ai_session_files:{workspace}",),
        )
        current = cur.execute(
            "SELECT sha256, blob_key, bytes_sha256 FROM ai_session_files"
            " WHERE workspace = %s AND path = %s FOR UPDATE",
            (workspace, path),
        ).fetchone()
        _check_expected(
            path, None if current is None else current["sha256"], write.expect, write.expected
        )
        held = cur.execute(
            "SELECT coalesce(sum(size), 0)::bigint AS bytes, count(*)::int AS files"
            " FROM ai_session_files WHERE workspace = %s AND path <> %s",
            (workspace, path),
        ).fetchone()
        assert held is not None
        _check_room(int(held["bytes"]), write.size, held["files"], workspace_max)
        # A file and a directory never share a name (``store.ts`` ``write``).
        segments = path.split("/")
        parents = ["/".join(segments[: i + 1]) for i in range(len(segments) - 1)]
        clash = cur.execute(
            "SELECT path FROM ai_session_files WHERE workspace = %s"
            " AND (path = ANY(%s::text[]) OR starts_with(path, %s)) LIMIT 1",
            (workspace, parents, f"{path}/"),
        ).fetchone()
        if clash is not None:
            other = clash["path"]
            raise WorkspaceRefusedError(
                "conflict",
                f"{other} is a file, so {path} cannot be under it; nothing was written"
                if len(other) < len(path)
                else f"{path} is a directory (it holds {other}), so it cannot be a file too;"
                " nothing was written",
            )
        bytes_sha = None
        if placed.data is not None:
            _put_bytes(conn, write.sha256, placed.data)
            bytes_sha = write.sha256
        row = cur.execute(
            "INSERT INTO ai_session_files (workspace, session_id, path, media_type, data,"
            " bytes_sha256, blob_key, key_sealed, key_kek_id, sha256, size, source)"
            " VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (workspace, path) DO UPDATE SET media_type = EXCLUDED.media_type,"
            " data = NULL, bytes_sha256 = EXCLUDED.bytes_sha256, blob_key = EXCLUDED.blob_key,"
            " key_sealed = EXCLUDED.key_sealed, key_kek_id = EXCLUDED.key_kek_id,"
            " sha256 = EXCLUDED.sha256, size = EXCLUDED.size, source = EXCLUDED.source,"
            f" updated_at = now() RETURNING {_INFO}",
            (
                workspace,
                session,
                path,
                write.media_type,
                bytes_sha,
                placed.blob_key,
                placed.key_sealed,
                placed.key_kek_id,
                write.sha256,
                write.size,
                None if write.source is None else Jsonb(write.source),
            ),
        ).fetchone()
        assert row is not None
        replaced_blob = None if current is None else current["blob_key"]
        if replaced_blob is not None and replaced_blob != placed.blob_key:
            _release(conn, replaced_blob, workspace, path)
        if placed.blob_key is not None:
            _hold(conn, placed.blob_key, workspace, path)
        seqs = _append_events(conn, session, write.events) if write.events and session else []
        replaced_bytes = None if current is None else current["bytes_sha256"]
    return info_of(row), seqs, replaced_bytes if replaced_bytes != bytes_sha else None


def delete_row(
    conn: Connection[Any], workspace: str, path: str, expected: str | None, events: list[str]
) -> tuple[bool, list[int], str | None]:
    """Deletes a file, its blob's hold and logs its events in one transaction: (whether
    there was one, the seqs, the bytes row it named). ``conflict`` when ``expected`` does
    not hold."""
    session = session_of(workspace)
    with conn.transaction():
        cur = _cur(conn)
        if events:
            _lock_session(conn, session)
        row = cur.execute(
            "SELECT sha256 FROM ai_session_files WHERE workspace = %s AND path = %s FOR UPDATE",
            (workspace, path),
        ).fetchone()
        if row is None:
            return False, [], None
        if expected is not None and row["sha256"] != expected:
            raise WorkspaceRefusedError(
                "conflict",
                f"{path} changed since it was read: its sha256 is {row['sha256']};"
                " nothing was deleted",
            )
        gone = cur.execute(
            "DELETE FROM ai_session_files WHERE workspace = %s AND path = %s"
            " RETURNING blob_key, bytes_sha256",
            (workspace, path),
        ).fetchone()
        assert gone is not None
        if gone["blob_key"] is not None:
            _release(conn, gone["blob_key"], workspace, path)
        seqs = _append_events(conn, session, events) if events and session else []
    return True, seqs, gone["bytes_sha256"]


# --- a fork's copy, the move to blobs, the sweep, reconcile, rotation ---------------------


def copy(conn: Connection[Any], from_session: str, to_session: str, keks: Keks) -> int:
    """A fork's copy of its parent's files, copy-on-write (#4212): a blob's row names the
    parent's blob, its file key wrapped again for the child, held in the same
    transaction; a row kept in Postgres names the parent's bytes row. Every file key is
    wrapped first (what can fail: no KEK, a forgotten parent), then every row is written in
    one transaction under the parent's write lock, once the parent's blob rows are still
    the ones wrapped: a write to the parent between the two is wrapped again, at most
    ``COPY_ATTEMPTS`` times, then refused ``conflict``. How many rows the child got."""
    source = f"session:{from_session}"
    wrapped: dict[str, tuple[dict[str, Any], bytes]] = {}
    for _ in range(COPY_ATTEMPTS):
        rows = _blob_rows_of(conn, source)
        for row in rows:
            kept = wrapped.get(row["path"])
            if kept is not None and kept[0]["blob_key"] == row["blob_key"]:
                continue
            file_key = unwrap_key(
                conn, source, row["path"], row["blob_key"], bytes(row["key_sealed"]), None, keks
            )
            sealed, _ = wrap_key(
                conn, f"session:{to_session}", row["path"], row["blob_key"], file_key, keks
            )
            wrapped[row["path"]] = (row, sealed)
        for gone in set(wrapped) - {r["path"] for r in rows}:
            del wrapped[gone]
        written = _copy_rows(conn, from_session, to_session, wrapped)
        if written is not None:
            return written
    raise WorkspaceRefusedError(
        "conflict", "the parent's files kept changing while the fork copied them; try again"
    )


#: How many times a fork's copy wraps again what changed in its parent meanwhile.
COPY_ATTEMPTS = 3


def _blob_rows_of(conn: Connection[Any], workspace: str) -> list[dict[str, Any]]:
    return list(
        _cur(conn)
        .execute(
            f"SELECT {_INFO}, blob_key, key_sealed FROM ai_session_files"
            " WHERE workspace = %s AND blob_key IS NOT NULL",
            (workspace,),
        )
        .fetchall()
    )


def _copy_rows(
    conn: Connection[Any],
    from_session: str,
    to_session: str,
    wrapped: dict[str, tuple[dict[str, Any], bytes]],
) -> int | None:
    """The fork's rows, in one transaction under the parent's write lock; None, and nothing
    written, when the parent's blob rows are no longer those ``wrapped`` names."""
    source, target = f"session:{from_session}", f"session:{to_session}"
    written: int = 0
    with conn.transaction():
        cur = _cur(conn)
        # The parent's write lock, so a write to the parent waits for the fork.
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"scadbuddy:ai_session_files:{source}",),
        )
        now = {r["path"]: r["blob_key"] for r in _blob_rows_of(conn, source)}
        if now != {path: row["blob_key"] for path, (row, _) in wrapped.items()}:
            return None
        # Every bytes row the child will name is locked FOR KEY SHARE until this commits
        # (``store.ts`` ``copy``): lock what is there, put the bytes of the parent's inline
        # rows that are not, and lock again until all are held.
        while True:
            inline = cur.execute(
                "SELECT path, sha256 FROM ai_session_files WHERE workspace = %s"
                " AND data IS NOT NULL AND sha256 = encode(sha256(data), 'hex')",
                (source,),
            ).fetchall()
            have = {
                r["sha256"]
                for r in cur.execute(
                    "SELECT sha256 FROM ai_session_file_bytes WHERE sha256 IN"
                    " (SELECT bytes_sha256 FROM ai_session_files WHERE workspace = %s"
                    " AND bytes_sha256 IS NOT NULL) OR sha256 = ANY(%s::text[]) FOR KEY SHARE",
                    (source, list({r["sha256"] for r in inline})),
                ).fetchall()
            }
            missing = [r["path"] for r in inline if r["sha256"] not in have]
            if not missing:
                break
            cur.execute(
                "INSERT INTO ai_session_file_bytes (sha256, data)"
                " SELECT DISTINCT ON (sha256) sha256, data FROM ai_session_files"
                " WHERE workspace = %s AND path = ANY(%s::text[]) AND data IS NOT NULL"
                " ORDER BY sha256 ON CONFLICT (sha256) DO NOTHING",
                (source, missing),
            )
        cur.execute(
            "UPDATE ai_session_files SET bytes_sha256 = sha256, data = NULL"
            " WHERE workspace = %s AND path = ANY(%s::text[]) AND data IS NOT NULL",
            (source, [r["path"] for r in inline]),
        )
        written += cur.execute(
            "INSERT INTO ai_session_files (workspace, session_id, path, media_type, data,"
            " bytes_sha256, sha256, size, source, created_at, updated_at)"
            " SELECT %s, %s, path, media_type, data, bytes_sha256, sha256, size, source,"
            " created_at, updated_at FROM ai_session_files WHERE workspace = %s"
            " AND (data IS NOT NULL OR bytes_sha256 IS NOT NULL)"
            " ON CONFLICT (workspace, path) DO NOTHING",
            (target, to_session, source),
        ).rowcount
        for row, sealed in wrapped.values():
            inserted = cur.execute(
                "INSERT INTO ai_session_files (workspace, session_id, path, media_type,"
                " blob_key, key_sealed, sha256, size, source, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (workspace, path) DO NOTHING",
                (
                    target,
                    to_session,
                    row["path"],
                    row["media_type"],
                    row["blob_key"],
                    sealed,
                    row["sha256"],
                    row["size"],
                    None if row["source"] is None else Jsonb(row["source"]),
                    row["created_at"],
                    row["updated_at"],
                ),
            ).rowcount
            if inserted:
                _hold(conn, row["blob_key"], target, row["path"])
            written += int(inserted)
    return written


def kept_in_postgres(conn: Connection[Any], batch: int) -> list[tuple[str, str, str]]:
    """Up to ``batch`` of each kind of row still kept in Postgres: (workspace, path, sha256)."""
    rows = conn.execute(
        "(SELECT workspace, path, sha256 FROM ai_session_files WHERE data IS NOT NULL"
        " ORDER BY workspace, path LIMIT %(n)s) UNION ALL"
        " (SELECT workspace, path, sha256 FROM ai_session_files WHERE bytes_sha256 IS NOT NULL"
        " ORDER BY workspace, path LIMIT %(n)s)",
        {"n": batch},
    ).fetchall()
    out: list[tuple[str, str, str]] = []
    for r in rows:
        values = list(r.values()) if isinstance(r, dict) else list(r)
        out.append((str(values[0]), str(values[1]), str(values[2])))
    return out


def inline_bytes(
    conn: Connection[Any], workspace: str, path: str, digest: str
) -> tuple[bytes, str | None] | None:
    """A row's bytes still in Postgres, at ``digest``, and the bytes row it names."""
    row = (
        _cur(conn)
        .execute(
            "SELECT coalesce(f.data, b.data) AS data, f.bytes_sha256 FROM ai_session_files f"
            " LEFT JOIN ai_session_file_bytes b ON b.sha256 = f.bytes_sha256"
            " WHERE f.workspace = %s AND f.path = %s AND f.sha256 = %s"
            " AND (f.data IS NOT NULL OR f.bytes_sha256 IS NOT NULL)",
            (workspace, path, digest),
        )
        .fetchone()
    )
    return None if row is None else (bytes(row["data"]), row["bytes_sha256"])


def moved_row(conn: Connection[Any], workspace: str, path: str, digest: str, placed: Placed) -> int:
    """A row still at ``digest`` and still in Postgres, now naming its blob, held in the
    same transaction; a row written meanwhile is left as its writer left it."""
    with conn.transaction():
        updated = conn.execute(
            "UPDATE ai_session_files SET data = NULL, bytes_sha256 = NULL, blob_key = %s,"
            " key_sealed = %s, key_kek_id = %s WHERE workspace = %s AND path = %s"
            " AND sha256 = %s AND (data IS NOT NULL OR bytes_sha256 IS NOT NULL)",
            (placed.blob_key, placed.key_sealed, placed.key_kek_id, workspace, path, digest),
        ).rowcount
        if updated and placed.blob_key is not None:
            _hold(conn, placed.blob_key, workspace, path)
    return updated


def sweep(conn: Connection[Any], ttl_ms: int) -> int:
    """Deletes principals' files last written more than ``ttl_ms`` ago with their blobs'
    holds, then every bytes row no file names; how many files went."""
    with conn.transaction():
        rows = (
            _cur(conn)
            .execute(
                "DELETE FROM ai_session_files WHERE session_id IS NULL"
                " AND updated_at < now() - (%s * interval '1 millisecond')"
                " RETURNING workspace, path, blob_key",
                (ttl_ms,),
            )
            .fetchall()
        )
        for row in rows:
            if row["blob_key"] is not None:
                _release(conn, row["blob_key"], row["workspace"], row["path"])
    drop_unnamed_bytes(conn)
    return len(rows)


def reconcile(conn: Connection[Any], grace_s: int) -> tuple[int, int] | None:
    """Makes the holds match the rows in one transaction: releases every hold older than
    ``grace_s`` whose row is gone or names another blob (what a session's cascade, a forget,
    a crash or an older agent's failed release left), and holds every row no hold names.
    None, doing nothing, while another reconcile holds ``RECONCILE_LOCK``: an agent pod still
    on SQL takes it as a session lock across its own run."""
    with conn.transaction():
        locked = conn.execute(
            "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", (RECONCILE_LOCK,)
        ).fetchone()
        if locked is None or not _first(locked):
            return None
        released = conn.execute(
            "DELETE FROM blob_refs r WHERE r.holder_kind = %(kind)s"
            " AND r.created_at < now() - make_interval(secs => %(grace)s)"
            " AND NOT EXISTS (SELECT 1 FROM ai_session_files f WHERE f.blob_key = r.key"
            " AND f.workspace || '/' || f.path = r.holder_id)",
            {"kind": HOLDER_KIND, "grace": grace_s},
        ).rowcount
        held = conn.execute(
            "INSERT INTO blob_refs (key, holder_kind, holder_id)"
            " SELECT f.blob_key, %(kind)s, f.workspace || '/' || f.path FROM ai_session_files f"
            " WHERE f.blob_key IS NOT NULL AND NOT EXISTS (SELECT 1 FROM blob_refs r"
            " WHERE r.key = f.blob_key AND r.holder_kind = %(kind)s"
            " AND r.holder_id = f.workspace || '/' || f.path)"
            " ON CONFLICT (key, holder_kind, holder_id) DO NOTHING",
            {"kind": HOLDER_KIND},
        ).rowcount
    return released, held


def rewrap(conn: Connection[Any], previous: Kek, current: Kek) -> tuple[int, int]:
    """Key rotation: every principal file key sealed under ``previous`` sealed again under
    ``current``, as the agent did at its start before #5299 step 7. (re-wrapped, failed)."""
    if previous.id == current.id:
        return 0, 0
    rows = (
        _cur(conn)
        .execute(
            "SELECT workspace, path, blob_key, key_sealed FROM ai_session_files"
            " WHERE key_kek_id = %s",
            (previous.id,),
        )
        .fetchall()
    )
    rewrapped = failed = 0
    for row in rows:
        context = file_key_context(row["workspace"], row["path"], row["blob_key"])
        try:
            file_key = open_bytes(previous.key, bytes(row["key_sealed"]), context)
        except SealError:
            failed += 1
            continue
        rewrapped += conn.execute(
            "UPDATE ai_session_files SET key_sealed = %s, key_kek_id = %s"
            " WHERE workspace = %s AND path = %s AND blob_key = %s AND key_kek_id = %s",
            (
                seal_bytes(current.key, file_key, context),
                current.id,
                row["workspace"],
                row["path"],
                row["blob_key"],
                previous.id,
            ),
        ).rowcount
    return rewrapped, failed


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iso(value: datetime) -> str:
    return value.isoformat()


def _first(row: Any) -> Any:
    return next(iter(row.values())) if isinstance(row, dict) else row[0]
