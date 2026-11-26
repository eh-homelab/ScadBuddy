"""Forgetting a durable subject's key (#5299 step 6): the SQL ``agent/src/sessions/forget.ts``
ran itself before, as one transaction, for ``api/crud_agent_forget.py``. A browser call's key
dropped and the sweep of the keys nothing forgets are Go's (``tools/go/core/internal/forget``,
#7667).

``forget`` is forgetSubject's key transaction (spec 2026-10-01 §6.5, plan 5c Ruling 12): under
the subject's advisory lock (``payload_key_lock``, which a key's creation also takes,
``payload_keys.py``), the tombstone in ``ai_forgotten_subjects``, a flow run's images
(``ai_flow_blobs``, #2477) and the key's row, so no key is ever made for the subject again and
every copy of its payloads is undecryptable from the commit on. A session that has forks has
them take their ranges of its transcript, its events and the images those name in that same
transaction (``transcripts.py`` ``copy_fork_ranges``, the agent's ``copyForkRanges``, #5299 step
9), opened and sealed with the session keys under the KEK; with no KEK, a session whose forks take
transcript entries is refused (``ForksTakeRangesError``), and the agent copies them itself. The
copy runs under the lock, which a fork's insert takes shared (the agent's ``manager.ts``), so a
fork made after it sees the tombstone.

``flows/forget.py`` (``DELETE /workflow-runs/{id}``) runs the same statements for a flow run.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import tuple_row

from scadbuddy.agent_data import transcripts
from scadbuddy.agent_data.workspace import Keks
from scadbuddy.workflows.payload_codec import payload_key_lock

LOCK = "SELECT pg_advisory_xact_lock(hashtext(%s))"
TOMBSTONE = (
    "INSERT INTO ai_forgotten_subjects (subject) VALUES (%s) ON CONFLICT (subject) DO NOTHING"
)
DELETE_FLOW_BLOBS = "DELETE FROM ai_flow_blobs WHERE subject = %s"
DELETE_KEY = "DELETE FROM ai_payload_keys WHERE subject = %s"


class ForksTakeRangesError(Exception):
    """The session's forks take transcript entries, and the API has no KEK to copy them."""


def forget(conn: Connection[Any], subject: str, keys: Keks) -> bool:
    """The subject's tombstone, its flow images and its key, in one transaction under its
    lock, a session's forks first taking their ranges (``copy_fork_ranges``); whether it had a
    key. ForksTakeRangesError when a fork takes transcript entries and there is no KEK."""
    with conn.transaction():
        cur = conn.cursor(row_factory=tuple_row)
        cur.execute(LOCK, (payload_key_lock(subject),))
        if subject.startswith("session-"):
            try:
                transcripts.copy_fork_ranges(conn, subject.removeprefix("session-"), keys)
            except transcripts.KeyUnavailableError:
                raise ForksTakeRangesError(subject) from None
        cur.execute(TOMBSTONE, (subject,))
        if subject.startswith("flow-"):
            cur.execute(DELETE_FLOW_BLOBS, (subject,))
        return cur.execute(DELETE_KEY, (subject,)).rowcount > 0
