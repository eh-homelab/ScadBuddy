"""Who a property write is by, for the history triggers (#6949, K1): `set_actor` names the
actor in ``scadbuddy.actor`` for the rest of the transaction. A property remove (and an
entity delete that takes keys) is refused without it.

The triggers publish through the event log, whose lock is held to commit. `set_actor`
takes that lock first (`hold_event_log`), so a writer that calls it before any row lock
keeps the order of `core/pg_events.py` ("Lock order"): the log, then rows. Without it a
printer merge (which deletes an entity row) and a property write on that entity could
each hold what the other waits for."""

from __future__ import annotations

from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.core.pg_events import hold_event_log
from scadbuddy.orders.models import Actor

Conn = Connection[DictRow]

#: The orphan sweep's actor (`entities/sweep.py`).
SWEEP_ACTOR = Actor(kind="provider", principal="entities_sweep")


def set_actor(conn: Conn, by: Actor) -> None:
    """Take the event log's lock, then name ``by`` as ``scadbuddy.actor`` until the
    transaction ends. Call it in a transaction, before any row lock.

    The lock is held to commit (`hold_event_log`), so a write with properties runs one at
    a time with every other event publisher. That is the cost of a log whose rows commit
    in ``seq`` order, which the history subscriber's cursor needs (`core/pg_events.py`).
    It is accepted (#8116): each such transaction is short, and the longest, a spool
    `bulk_on` create, is capped at `BULK_MAX` spools."""
    hold_event_log(conn)
    conn.execute("SELECT set_config('scadbuddy.actor', %s, true)", (by.model_dump_json(),))
