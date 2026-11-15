"""Forgetting a durable subject's key, and the deletes of keys nothing forgets (#5299 step
6): the SQL ``agent/src/sessions/forget.ts`` and ``agent/src/temporal/payloadCodec.ts`` ran
themselves before, each as one transaction, for ``api/crud_agent_forget.py``.

``forget`` is forgetSubject's key transaction (spec 2026-10-01 §6.5, plan 5c Ruling 12): under
the subject's advisory lock (``payload_key_lock``, which a key's creation also takes,
``payload_keys.py``), the tombstone in ``ai_forgotten_subjects``, a flow run's images
(``ai_flow_blobs``, #2477) and the key's row, so no key is ever made for the subject again and
every copy of its payloads is undecryptable from the commit on. A session that has forks is
refused (``ForksTakeRangesError``): its forks take their ranges of its transcript in that same
transaction (the agent's ``copyForkRanges``, which opens and seals with the session keys), and
that stays the agent's until its sessions move here (#5299 step 9). The check runs under the
lock, which a fork's insert takes shared (the agent's ``manager.ts``), so a fork made after it
sees the tombstone.

``flows/forget.py`` (``DELETE /workflow-runs/{id}``) runs the same statements for a flow run.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import tuple_row

from scadbuddy.workflows.payload_codec import payload_key_lock

LOCK = "SELECT pg_advisory_xact_lock(hashtext(%s))"
TOMBSTONE = (
    "INSERT INTO ai_forgotten_subjects (subject) VALUES (%s) ON CONFLICT (subject) DO NOTHING"
)
DELETE_FLOW_BLOBS = "DELETE FROM ai_flow_blobs WHERE subject = %s"
DELETE_KEY = "DELETE FROM ai_payload_keys WHERE subject = %s"

#: How old a browser call's key is before the sweep deletes it: far past the longest call
#: (the agent's ``BRIDGE_KEY_MAX_AGE_S``).
BRIDGE_KEY_MAX_AGE_S = 3600
#: How old a session's key is before the sweep looks at it: well past any insert it could
#: race (the agent's ``ORPHAN_KEY_MIN_AGE_S``).
ORPHAN_KEY_MIN_AGE_S = 24 * 3600
#: The most orphaned session keys one sweep deletes.
ORPHAN_SWEEP_LIMIT = 100


class ForksTakeRangesError(Exception):
    """The session has forks, which take their ranges of its transcript before it goes."""


def forget(conn: Connection[Any], subject: str) -> bool:
    """The subject's tombstone, its flow images and its key, in one transaction under its
    lock; whether it had a key. ForksTakeRangesError for a session with forks."""
    with conn.transaction():
        cur = conn.cursor(row_factory=tuple_row)
        cur.execute(LOCK, (payload_key_lock(subject),))
        if subject.startswith("session-"):
            fork = cur.execute(
                "SELECT 1 FROM ai_sessions WHERE fork_parent_id = %s::uuid LIMIT 1",
                (subject.removeprefix("session-"),),
            ).fetchone()
            if fork is not None:
                raise ForksTakeRangesError(subject)
        cur.execute(TOMBSTONE, (subject,))
        if subject.startswith("flow-"):
            cur.execute(DELETE_FLOW_BLOBS, (subject,))
        return cur.execute(DELETE_KEY, (subject,)).rowcount > 0


def drop_bridge_key(conn: Connection[Any], subject: str) -> bool:
    """A browser call's key deleted once its caller has the answer (the agent's
    ``dropPayloadKey``), under the subject's lock; no tombstone, as nothing takes the id
    again. Whether there was one."""
    with conn.transaction():
        cur = conn.cursor(row_factory=tuple_row)
        cur.execute(LOCK, (payload_key_lock(subject),))
        return cur.execute(DELETE_KEY, (subject,)).rowcount > 0


def sweep(
    conn: Connection[Any],
    *,
    orphan_min_age_s: int = ORPHAN_KEY_MIN_AGE_S,
    bridge_max_age_s: int = BRIDGE_KEY_MAX_AGE_S,
) -> tuple[int, int]:
    """The keys no forget deletes (the agent's ``sweepOrphanedSessionKeys`` and
    ``sweepBridgeKeys``): a session's whose ``ai_sessions`` row is gone (a durable fork whose
    workflow never started, #3317), older than ``orphan_min_age_s`` and checked again under
    its lock, one transaction each; and a browser call's older than ``bridge_max_age_s``
    (#4780). Not a forget: no tombstone. Answers (orphaned, bridge) deleted."""
    cur = conn.cursor(row_factory=tuple_row)
    with conn.transaction():
        subjects = [
            row[0]
            for row in cur.execute(
                "SELECT k.subject FROM ai_payload_keys k"
                " WHERE k.subject LIKE 'session-%%'"
                " AND k.created_at < now() - make_interval(secs => %s)"
                " AND NOT EXISTS (SELECT 1 FROM ai_sessions s"
                " WHERE s.id::text = substr(k.subject, 9))"
                " LIMIT %s",
                (orphan_min_age_s, ORPHAN_SWEEP_LIMIT),
            ).fetchall()
        ]
    orphaned = 0
    for subject in subjects:
        session = subject.removeprefix("session-")
        with conn.transaction():
            cur.execute(LOCK, (payload_key_lock(subject),))
            orphaned += cur.execute(
                "DELETE FROM ai_payload_keys WHERE subject = %s"
                " AND NOT EXISTS (SELECT 1 FROM ai_sessions WHERE id::text = %s)"
                " AND NOT EXISTS (SELECT 1 FROM ai_session_entries WHERE session_id::text = %s)",
                (subject, session, session),
            ).rowcount
    with conn.transaction():
        bridges = cur.execute(
            "DELETE FROM ai_payload_keys WHERE subject LIKE 'bridge-%%'"
            " AND created_at < now() - make_interval(secs => %s)",
            (bridge_max_age_s,),
        ).rowcount
    return orphaned, bridges
