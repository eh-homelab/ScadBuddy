"""Order numbers (#4249, plan 2026-10-19 Ruling 5): ``ORD-<yyyy>-<n>``.

The count comes from a per-year counter row (``order_numbers``) read under ``FOR
UPDATE`` in the order's own insert transaction, so a rolled-back insert gives its number
back and two replicas never take the same one. A Postgres sequence would skip on a
rollback (https://www.postgresql.org/docs/17/functions-sequence.html: "nextval ... is
never rolled back"), and gapless numbers are the point. The lock serialises every order
create of a year for the length of one insert transaction, which a shop's few orders a
day never notice; it is never to be made a sequence.
"""

from __future__ import annotations

from psycopg import Connection
from psycopg.pq import TransactionStatus
from psycopg.rows import DictRow


def next_number(conn: Connection[DictRow], year: int) -> str:
    """The next number of ``year``, taken under the year's row lock.

    ``conn`` must be inside a transaction: the lock lives until it ends, and a caller
    that rolls back gives the number back. Outside one this raises ``RuntimeError``
    rather than hand out a number nothing protects.
    """
    if conn.info.transaction_status not in (TransactionStatus.INTRANS, TransactionStatus.ACTIVE):
        raise RuntimeError("next_number needs a transaction: the number's lock lives in it")
    conn.execute(
        "INSERT INTO order_numbers (year, last) VALUES (%s, 0) ON CONFLICT (year) DO NOTHING",
        (year,),
    )
    row = conn.execute(
        "SELECT last FROM order_numbers WHERE year = %s FOR UPDATE", (year,)
    ).fetchone()
    assert row is not None  # inserted above, and never deleted
    taken = int(row["last"]) + 1
    conn.execute("UPDATE order_numbers SET last = %s WHERE year = %s", (taken, year))
    return f"ORD-{year}-{taken}"
