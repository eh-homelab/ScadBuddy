"""A session's transcript, the Agent SDK's ``SessionStore`` rows (#5299 step 9, part b):
``ai_session_entries``. The ``transcripts`` routes are Go's (``tools/go/core/internal/transcripts``,
#7667); here is what a forget runs to copy a fork's range (``copy_fork_ranges``), with the
append and read its tests use.

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

from scadbuddy.agent_data import events
from scadbuddy.agent_data.payload_keys import key_in
from scadbuddy.agent_data.workspace import Keks
from scadbuddy.core.secrets import open_bytes, seal_bytes
from scadbuddy.workflows.payload_codec import payload_key_lock

#: ``entry_type`` is plaintext, so only a short token-like type is stored (``ENTRY_TYPE``).
ENTRY_TYPE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
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


#: At most this many rows in one insert of ``copy_fork_ranges``.
COPY_BATCH = 1000


def copy_fork_ranges(conn: Connection[Any], parent_id: str, keys: Keks) -> int:
    """``store.ts`` ``copyForkRanges``, in the forget's transaction under the parent's lock:
    each fork that reads through ``parent_id`` takes the range it references as rows of its own,
    ``inherited`` and sealed under its own key, with its events and the images they name, and
    its link is cleared. A fork forgotten itself gets no rows, only its link cleared. How many
    forks there were. KeyUnavailableError without a KEK when a fork takes transcript entries."""
    children = conn.execute(
        "SELECT id::text AS id, fork_cut AS cut FROM ai_sessions"
        " WHERE fork_parent_id = %s::uuid ORDER BY id FOR UPDATE",
        (parent_id,),
    ).fetchall()
    if not children:
        return 0
    takes_entries = any((c["cut"] or 0) > 0 for c in children)
    if takes_entries and keys.current is None:
        raise KeyUnavailableError("the API has no SCADBUDDY_SECRET_KEY_FILE")
    texts: list[str] = []
    if takes_entries:
        for owner, owned in segments(conn, parent_id):
            texts.extend(_open(conn, owner, "", owned, keys))
    for child in children:
        child_id = child["id"]
        subject = f"session-{child_id}"
        taken = texts[: child["cut"] or 0]
        dek = None
        if taken:
            assert keys.current is not None
            with conn.transaction():
                dek, _made = key_in(
                    conn,
                    subject,
                    create=True,
                    standing=False,
                    kek=keys.current,
                    previous=keys.previous,
                )
        # As an append does: a forget of the child either went first, and its tombstone is seen
        # here, or waits for this to commit and then deletes these rows with the rest.
        conn.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtext(%s))", (payload_key_lock(subject),)
        )
        forgotten = conn.execute(
            "SELECT 1 FROM ai_forgotten_subjects WHERE subject = %s", (subject,)
        ).fetchone()
        if dek is not None and forgotten is None:
            for at in range(0, len(taken), COPY_BATCH):
                batch = taken[at : at + COPY_BATCH]
                ids = [
                    int(r["id"])
                    for r in conn.execute(
                        "SELECT nextval(pg_get_serial_sequence('ai_session_entries', 'id')) AS id"
                        " FROM generate_series(1, %s)",
                        (len(batch),),
                    ).fetchall()
                ]
                inserts: list[tuple[Any, ...]] = []
                for row_id, text in zip(ids, batch, strict=True):
                    entry = _parsed(text)
                    uuid = entry.get("uuid") if isinstance(entry.get("uuid"), str) else None
                    bound = uuid if uuid is not None else f"#{row_id}"
                    sealed = seal_bytes(dek, text.encode(), entry_context(child_id, "", bound))
                    inserts.append((row_id, child_id, uuid, entry_type_of(entry), sealed))
                with conn.cursor() as cur:
                    # A uuid the fork already holds as its own row stays there.
                    cur.executemany(
                        "INSERT INTO ai_session_entries (id, project_key, session_id, subpath,"
                        " uuid, entry_type, sealed, inherited) VALUES (%s, '', %s, '', %s, %s, %s,"
                        " true) ON CONFLICT (session_id, subpath, uuid) WHERE uuid IS NOT NULL"
                        " DO NOTHING",
                        inserts,
                    )
        if forgotten is None:
            copied = events.copy_fork_events(conn, child_id)
            events.copy_fork_blobs(conn, child_id, events.blob_names(copied))
        conn.execute(
            "UPDATE ai_sessions SET fork_parent_id = NULL, fork_cut = NULL,"
            " fork_event_cut = NULL WHERE id = %s::uuid",
            (child_id,),
        )
    return len(children)
