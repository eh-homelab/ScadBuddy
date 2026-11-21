"""A durable subject's payload data key released to the agent (#5299 D5, #5342): opened
here with the KEK, never the KEK itself.

A subject (``session-<uuid>``, ``flow-<uuid>`` or a browser call's ``bridge-<uuid>``) has
one data key, a row of ``ai_payload_keys`` sealed under the KEK in the context
``dek:ai_payload_keys:<subject>``. ``release`` reads it and, with ``create``, makes it
first, in one transaction: under the subject's advisory lock, which ``forgetSubject``
also takes, and never for a subject in ``ai_forgotten_subjects``, so the tombstone check
never reads from before a forget that is committing and a forgotten subject never gets a
key again. With ``standing`` a session's key is made only while its ``ai_sessions`` row
stands (the workspace's sealing, #4789). A release writes its ``ai_audit`` row (kind
``payload_key``, ``audit.py``) in the same transaction: a key made, or none to give, every
time; a key released, once per subject in ``RELEASE_AUDIT_WINDOW``, since the agent asks at
transcript-read rate (its 30 s cache aside) and a row per read would bury the log.

The agent keeps what it is given for 30 s at most (``agent/src/temporal/payloadCodec.ts``
``CrudPayloadKeys``), which is how long a forgotten subject can still be opened there.
"""

from __future__ import annotations

import os
import re
from typing import Any

from psycopg import Connection

from scadbuddy.agent_data import audit
from scadbuddy.core.secrets import KEK_BYTES, Kek, SealError, open_bytes, seal_bytes
from scadbuddy.workflows.payload_codec import payload_key_context, payload_key_lock

#: A subject the agent may ask a key for, matched whole.
SUBJECT_PATTERN = (
    r"^(session|flow|bridge)-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SESSION = re.compile(r"^session-(.+)$")
#: How long one ``payload_key.release`` row stands for every later release of its subject.
RELEASE_AUDIT_WINDOW = "10 minutes"


class KeyNotHeldError(Exception):
    """The subject's key is sealed under a KEK this API does not hold."""


def _row(conn: Connection[Any], subject: str) -> tuple[bytes, str] | None:
    found = conn.execute(
        "SELECT dek_sealed, kek_id FROM ai_payload_keys WHERE subject = %s", (subject,)
    ).fetchone()
    if found is None:
        return None
    if isinstance(found, dict):
        return bytes(found["dek_sealed"]), found["kek_id"]
    return bytes(found[0]), found[1]


def _create(conn: Connection[Any], subject: str, kek: Kek, standing: bool) -> bool:
    """Makes the key unless the subject has one, was forgotten, or (``standing``) its
    session is gone; whether this call made it."""
    sealed = seal_bytes(kek.key, os.urandom(KEK_BYTES), payload_key_context(subject))
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (payload_key_lock(subject),))
    made = conn.execute(
        "INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id)"
        " SELECT %(subject)s, %(sealed)s, %(kek_id)s"
        " WHERE NOT EXISTS (SELECT 1 FROM ai_forgotten_subjects WHERE subject = %(subject)s)"
        " AND (NOT %(standing)s OR EXISTS"
        " (SELECT 1 FROM ai_sessions WHERE id::text = substr(%(subject)s, 9)))"
        " ON CONFLICT (subject) DO NOTHING",
        {"subject": subject, "sealed": sealed, "kek_id": kek.id, "standing": standing},
    )
    return made.rowcount == 1


def release(
    conn: Connection[Any],
    subject: str,
    *,
    create: bool,
    standing: bool,
    kek: Kek,
    previous: Kek | None,
) -> bytes | None:
    """The subject's data key, made first with ``create``; None when it has none (it was
    forgotten, or never made). One transaction, with its audit row."""
    session = _SESSION.match(subject)
    session_id = session.group(1) if session else None
    with conn.transaction():
        key, made = key_in(
            conn, subject, create=create, standing=standing, kek=kek, previous=previous
        )
        if key is None:
            audit.record(
                conn,
                "payload_key",
                "payload_key.release",
                "refused",
                f"{subject}: no key",
                session_id=session_id,
            )
            return None
        if made or not _released_lately(conn, subject):
            audit.record(
                conn,
                "payload_key",
                "payload_key.create" if made else "payload_key.release",
                "ok",
                subject,
                session_id=session_id,
            )
    return key


def key_in(
    conn: Connection[Any],
    subject: str,
    *,
    create: bool,
    standing: bool,
    kek: Kek,
    previous: Kek | None,
) -> tuple[bytes | None, bool]:
    """The subject's data key (made first with ``create``) and whether this call made it,
    in the caller's transaction, with no audit row: for ``release``, and for the API's own
    use of a key it never hands out (the workspace's file keys, ``workspace.py``). None
    when it has none."""
    row = _row(conn, subject)
    made = False
    if row is None and create:
        made = _create(conn, subject, kek, standing)
        # Whoever inserted first, this is the key every encoder of the subject uses.
        row = _row(conn, subject)
    if row is None:
        return None, False
    dek_sealed, kek_id = row
    opener = kek if kek_id == kek.id else previous if previous and kek_id == previous.id else None
    if opener is None:
        raise KeyNotHeldError(
            f"the payload key of {subject} was sealed with key {kek_id}, which the API"
            " does not hold"
        )
    try:
        return open_bytes(opener.key, dek_sealed, payload_key_context(subject)), made
    except SealError:
        raise KeyNotHeldError(f"the payload key of {subject} does not open") from None


def _released_lately(conn: Connection[Any], subject: str) -> bool:
    """Whether the subject's key was released (or made) inside the window; never under the
    subject's own lock, which a forget holds."""
    return audit.recorded_lately(
        conn,
        "payload_key",
        ("payload_key.release", "payload_key.create"),
        RELEASE_AUDIT_WINDOW,
        detail=subject,
    )


def rewrap(conn: Connection[Any], previous: Kek, current: Kek) -> tuple[int, int]:
    """Key rotation: each key sealed under ``previous`` sealed again under ``current``,
    each update conditional on ``kek_id``. Answers (re-wrapped, failed)."""
    if previous.id == current.id:
        return 0, 0
    rows = conn.execute(
        "SELECT subject, dek_sealed FROM ai_payload_keys WHERE kek_id = %s", (previous.id,)
    ).fetchall()
    rewrapped = failed = 0
    for found in rows:
        subject, sealed = (
            (found["subject"], found["dek_sealed"]) if isinstance(found, dict) else found
        )
        try:
            dek = open_bytes(previous.key, bytes(sealed), payload_key_context(subject))
        except SealError:
            failed += 1
            continue
        updated = conn.execute(
            "UPDATE ai_payload_keys SET dek_sealed = %s, kek_id = %s"
            " WHERE subject = %s AND kek_id = %s",
            (
                seal_bytes(current.key, dek, payload_key_context(subject)),
                current.id,
                subject,
                previous.id,
            ),
        )
        rewrapped += updated.rowcount
    return rewrapped, failed
