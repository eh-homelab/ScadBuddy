"""A session's transcript, the Agent SDK's ``SessionStore`` rows (#5299 step 9, part b), for
``api/crud_agent_transcripts.py`` area ``transcripts``: ``ai_session_entries``.

Each function is the SQL of ``agent/src/sessions/store.ts`` (`PgTranscriptRows`) statement for
statement, a transaction as one transaction, with the sealing moved here (D5): the agent sends
and gets each entry's JSON text, and the API seals and opens it under the session's data key
(``ai_payload_keys``, subject ``session-<id>``), in the agent's format (``core/secrets.py``,
the vectors ``tests/fixtures/secret-vectors.json``) and AAD
``ai_session_entries:<session_id>:<subpath>:<uuid or #id>``. A row written before sealing
holds plaintext ``entry`` and is read as it is.

An append takes the subject's payload-key lock shared, as the agent's does, before it reads
the tombstone, so a forget either went first (and refuses it) or waits for it and deletes its
rows. A fork's main transcript is its parent's as the parent loads it, cut, then its own
(copy-on-write, #4212): ``segments`` reads the chain and every row a load takes in one
statement, one snapshot, and only rows up to each ancestor's least cut (``LEAST(bound, cut)``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from psycopg import Connection

from scadbuddy.agent_data.payload_keys import key_in
from scadbuddy.agent_data.workspace import Keks
from scadbuddy.core.secrets import open_bytes, seal_bytes
from scadbuddy.workflows.payload_codec import payload_key_lock

#: ``entry_type`` is plaintext, so only a short token-like type is stored (``ENTRY_TYPE``).
ENTRY_TYPE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
#: How many of a session's newest user and assistant rows ``last_cwd`` opens first.
LAST_CWD_ROWS = 20
#: How often a load is read again when an ancestor was forgotten while it read.
TRANSCRIPT_TRIES = 3


class SubjectForgottenError(Exception):
    """The session's key is gone (forgotten), or it was never made and is not to be."""


class KeyUnavailableError(Exception):
    """The API holds no KEK to seal or open with."""


@dataclass(frozen=True)
class Fence:
    """A turn placement's hold, or the session's last placement between turns
    (``turnHost.ts`` ``holdsOrLast``)."""

    attempt: str
    turn_id: str


def entry_context(session_id: str, subpath: str, bound: str) -> str:
    return f"ai_session_entries:{session_id}:{subpath}:{bound}"


def _parsed(text: str) -> dict[str, Any]:
    value = json.loads(text)
    return value if isinstance(value, dict) else {}


def entry_type_of(entry: dict[str, Any]) -> str | None:
    kind = entry.get("type")
    return kind if isinstance(kind, str) and ENTRY_TYPE.match(kind) else None


def _key(conn: Connection[Any], session_id: str, keks: Keks, *, create: bool) -> bytes:
    if keks.current is None:
        raise KeyUnavailableError("the API has no SCADBUDDY_SECRET_KEY_FILE")
    with conn.transaction():
        dek, _made = key_in(
            conn,
            f"session-{session_id}",
            create=create,
            standing=False,
            kek=keks.current,
            previous=keks.previous,
        )
    if dek is None:
        raise SubjectForgottenError(f"session-{session_id} was forgotten")
    return dek


def append(
    conn: Connection[Any],
    session_id: str,
    project_key: str,
    subpath: str,
    texts: list[str],
    fence: Fence | None,
    keks: Keks,
) -> bool:
    """``appendIf``: the entries sealed and inserted in one transaction, under the subject's
    lock shared, refused once it is forgotten, and only while ``fence`` holds (checked
    ``FOR SHARE`` after the lock). Whether it was written."""
    if not texts:
        return True
    if keks.current is None:
        raise KeyUnavailableError("the API has no SCADBUDDY_SECRET_KEY_FILE")
    entries = [_parsed(t) for t in texts]
    subject = f"session-{session_id}"
    # One transaction, the fewest round trips (#5299: the hot path): the key (made first under
    # the subject's lock when it has none), the rows' ids, then under the lock shared the
    # tombstone and the fence in one statement, and the insert.
    with conn.transaction():
        dek, _made = key_in(
            conn, subject, create=True, standing=False, kek=keks.current, previous=keks.previous
        )
        if dek is None:
            raise SubjectForgottenError(f"{subject} was forgotten: its transcript is not written")
        # Every row's id is reserved first, in order, so a batch keeps its order and a row
        # without a uuid is bound by its own id (a refused append only skips sequence values).
        ids = [
            int(r["id"])
            for r in conn.execute(
                "SELECT nextval(pg_get_serial_sequence('ai_session_entries', 'id')) AS id"
                " FROM generate_series(1, %s)",
                (len(texts),),
            ).fetchall()
        ]
        rows = []
        for row_id, text, entry in zip(ids, texts, entries, strict=True):
            uuid = entry.get("uuid") if isinstance(entry.get("uuid"), str) else None
            bound = uuid if uuid is not None else f"#{row_id}"
            sealed = seal_bytes(dek, text.encode(), entry_context(session_id, subpath, bound))
            rows.append(
                (row_id, project_key, session_id, subpath, uuid, entry_type_of(entry), sealed)
            )
        conn.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtext(%s))", (payload_key_lock(subject),)
        )
        check = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM ai_forgotten_subjects WHERE subject = %(subject)s)"
            " AS gone, (%(attempt)s::text IS NULL OR EXISTS (SELECT 1 FROM ("
            "SELECT 1 FROM ai_sessions WHERE id::text = %(session)s"
            " AND turn_attempt = %(attempt)s"
            " AND (durable_turn_id::text = %(turn)s OR durable_turn_id IS NULL) FOR SHARE"
            ") held)) AS holds",
            {
                "subject": subject,
                "session": session_id,
                "attempt": fence.attempt if fence is not None else None,
                "turn": fence.turn_id if fence is not None else None,
            },
        ).fetchone()
        assert check is not None
        if check["gone"]:
            raise SubjectForgottenError(f"{subject} was forgotten: its transcript is not written")
        if not check["holds"]:
            return False
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ai_session_entries"
                " (id, project_key, session_id, subpath, uuid, entry_type, sealed)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (session_id, subpath, uuid) WHERE uuid IS NOT NULL DO NOTHING",
                rows,
            )
    return True


def _open(
    conn: Connection[Any], owner: str, subpath: str, rows: list[dict[str, Any]], keks: Keks
) -> list[str]:
    """The rows' JSON texts, a sealed row opened under ``owner``'s key (never made to read)."""
    dek = _key(conn, owner, keks, create=False) if any(r["sealed"] for r in rows) else None
    out = []
    for row in rows:
        if row["sealed"] is None:
            out.append(row["entry"])
            continue
        assert dek is not None
        bound = row["uuid"] if row["uuid"] is not None else f"#{row['id']}"
        out.append(
            open_bytes(dek, bytes(row["sealed"]), entry_context(owner, subpath, bound)).decode()
        )
    return out


#: A session's fork chain and the main-transcript rows a load takes, in one statement
#: (``store.ts`` ``chainRows``).
_CHAIN = (
    "WITH RECURSIVE chain (id, parent, cut, depth, bound) AS ("
    " SELECT id, fork_parent_id, fork_cut, 0, NULL::bigint FROM ai_sessions WHERE id = %s::uuid"
    " UNION ALL"
    " SELECT s.id, s.fork_parent_id, s.fork_cut, c.depth + 1, LEAST(c.bound, c.cut)"
    " FROM ai_sessions s JOIN chain c ON s.id = c.parent)"
    " SELECT c.id::text AS owner, c.cut, c.depth, e.id, e.uuid, e.entry, e.sealed, e.inherited"
    " FROM chain c LEFT JOIN LATERAL ("
    " SELECT id, uuid, entry, sealed, inherited FROM ai_session_entries"
    " WHERE session_id = c.id::text AND subpath = '' ORDER BY inherited DESC, id LIMIT c.bound"
    ") e ON true ORDER BY c.depth, e.inherited DESC, e.id"
)

Segment = tuple[str, list[dict[str, Any]]]


def _first_rows(segments: list[Segment], n: int) -> list[Segment]:
    out: list[Segment] = []
    left = n
    for owner, rows in segments:
        if left <= 0:
            break
        kept = rows[:left]
        left -= len(kept)
        out.append((owner, kept))
    return out


def segments(conn: Connection[Any], session_id: str) -> list[Segment]:
    """``segmentsOf``: the rows of the main transcript in the order it loads, each run with
    its owner: inherited rows, then the parent's through the cut, then its own."""
    joined = conn.execute(_CHAIN, (session_id,)).fetchall()
    chain: list[tuple[str, int | None]] = []
    for row in joined:
        if not chain or chain[-1][0] != row["owner"]:
            chain.append((row["owner"], row["cut"]))
    rows = [r for r in joined if r["id"] is not None]
    if not chain:
        # No session row: the plain rows of the key, as before forks.
        chain = [(session_id, None)]
        rows = [
            {**r, "owner": session_id}
            for r in conn.execute(
                "SELECT id, uuid, entry, sealed, inherited FROM ai_session_entries"
                " WHERE session_id = %s AND subpath = '' ORDER BY inherited DESC, id",
                (session_id,),
            ).fetchall()
        ]
    through: list[Segment] = []
    for owner, cut in reversed(chain):
        mine = [r for r in rows if r["owner"] == owner]
        built: list[Segment] = [
            (owner, [r for r in mine if r["inherited"]]),
            *([] if cut is None else _first_rows(through, cut)),
            (owner, [r for r in mine if not r["inherited"]]),
        ]
        through = [s for s in built if s[1]]
    return through


def transcript(conn: Connection[Any], session_id: str, keys: Keks) -> list[str]:
    """``transcriptOf``: the main transcript as it loads, each run opened under its owner's
    key, read again when an ancestor's key went after the snapshot (its range is then the
    child's own)."""
    attempt = 1
    while True:
        segs = segments(conn, session_id)
        try:
            out: list[str] = []
            for owner, rows in segs:
                out.extend(_open(conn, owner, "", rows, keys))
            return out
        except SubjectForgottenError:
            ancestor_gone = any(owner != session_id for owner, _rows in segs)
            if not ancestor_gone or attempt >= TRANSCRIPT_TRIES:
                raise
            attempt += 1


_ROW = "SELECT id, uuid, entry, sealed FROM ai_session_entries"


def subpath_entries(conn: Connection[Any], session_id: str, subpath: str, keks: Keks) -> list[str]:
    rows = conn.execute(
        f"{_ROW} WHERE session_id = %s AND subpath = %s ORDER BY id", (session_id, subpath)
    ).fetchall()
    return _open(conn, session_id, subpath, rows, keks)


def last_of_type(
    conn: Connection[Any], session_id: str, subpath: str, kind: str, keks: Keks
) -> str | None:
    """The newest row without a uuid of this ``type``, by its plaintext ``entry_type``, or a
    row from before sealing whose text names it."""
    rows = conn.execute(
        f"{_ROW} WHERE session_id = %s AND subpath = %s AND uuid IS NULL AND NOT inherited"
        " AND (entry_type = %s OR (entry_type IS NULL AND entry LIKE %s))"
        " ORDER BY id DESC LIMIT 1",
        (session_id, subpath, kind, f'%"{kind}"%'),
    ).fetchall()
    return _open(conn, session_id, subpath, rows, keks)[0] if rows else None


def _cwd_of(texts: list[str]) -> str | None:
    for text in reversed(texts):
        try:
            cwd = _parsed(text).get("cwd")
        except ValueError:
            continue
        if isinstance(cwd, str) and cwd != "":
            return cwd
    return None


def last_cwd(conn: Connection[Any], session_id: str, keks: Keks) -> str | None:
    """``lastCwd``: the newest ``cwd`` the session's own user and assistant rows name, else
    its transcript's as it loads (a fork before its first turn)."""
    rows = conn.execute(
        f"{_ROW} WHERE session_id = %s AND subpath = '' AND NOT inherited"
        " AND (entry_type IN ('user', 'assistant') OR entry_type IS NULL)"
        " ORDER BY id DESC LIMIT %s",
        (session_id, LAST_CWD_ROWS),
    ).fetchall()
    if rows:
        own = _cwd_of(list(reversed(_open(conn, session_id, "", rows, keks))))
        if own is not None:
            return own
    return _cwd_of(transcript(conn, session_id, keks))


def list_sessions(conn: Connection[Any], project_key: str) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            "SELECT session_id, floor(extract(epoch FROM max(created_at)) * 1000)::bigint AS mtime"
            " FROM ai_session_entries WHERE project_key = %s AND subpath = ''"
            " GROUP BY session_id",
            (project_key,),
        ).fetchall()
    )


def delete(conn: Connection[Any], session_id: str, subpath: str | None) -> None:
    """The main transcript takes its subagent transcripts with it."""
    if subpath is None:
        conn.execute("DELETE FROM ai_session_entries WHERE session_id = %s", (session_id,))
    else:
        conn.execute(
            "DELETE FROM ai_session_entries WHERE session_id = %s AND subpath = %s",
            (session_id, subpath),
        )


def subkeys(conn: Connection[Any], session_id: str) -> list[str]:
    return [
        r["subpath"]
        for r in conn.execute(
            "SELECT DISTINCT subpath FROM ai_session_entries"
            " WHERE session_id = %s AND subpath <> '' ORDER BY subpath",
            (session_id,),
        ).fetchall()
    ]


def head(conn: Connection[Any], session_id: str) -> dict[str, Any]:
    """Whether there is a main transcript to resume (rows of its own, or a fork's range,
    ``transcriptExists``), and its own newest row with a uuid (``warm.ts`` ``spawn``)."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM ai_session_entries WHERE session_id = %(id)s"
        " AND subpath = '')"
        " OR EXISTS (SELECT 1 FROM ai_sessions WHERE id::text = %(id)s"
        " AND fork_parent_id IS NOT NULL AND fork_cut > 0) AS found,"
        " (SELECT max(id) FROM ai_session_entries WHERE session_id = %(id)s AND subpath = ''"
        " AND uuid IS NOT NULL AND NOT inherited)::text AS since",
        {"id": session_id},
    ).fetchone()
    assert row is not None
    return {"exists": bool(row["found"]), "since": row["since"]}


def moved(conn: Connection[Any], session_id: str, since: int | None, own: list[str]) -> bool:
    """``warm.ts`` ``tailMoved``: whether a row with a uuid the process did not write landed
    in the main transcript after ``since``."""
    row = conn.execute(
        "SELECT 1 FROM ai_session_entries WHERE session_id = %s AND subpath = ''"
        " AND uuid IS NOT NULL AND NOT inherited AND id > %s AND NOT (uuid = ANY(%s::text[]))"
        " LIMIT 1",
        (session_id, since or 0, own),
    ).fetchone()
    return row is not None


def user_landed(conn: Connection[Any], session_id: str, mark: int) -> bool:
    """``store.ts`` ``userLanded`` (for ``turnHost.ts``): whether a user entry landed in the
    main transcript after row ``mark``; a legacy row's own top-level ``type`` is read."""
    rows = conn.execute(
        "SELECT entry_type = 'user' AS typed, entry FROM ai_session_entries"
        " WHERE session_id = %s AND subpath = '' AND id > %s AND NOT inherited"
        " AND (entry_type = 'user' OR (entry_type IS NULL AND entry LIKE '%%\"user\"%%'))"
        " ORDER BY id",
        (session_id, mark),
    ).fetchall()
    for row in rows:
        if row["typed"]:
            return True
        try:
            if _parsed(row["entry"] or "").get("type") == "user":
                return True
        except ValueError:
            continue
    return False
