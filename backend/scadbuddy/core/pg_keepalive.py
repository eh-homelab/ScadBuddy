"""libpq connection parameters that bound a silent Postgres peer (#1226).

``statement_timeout`` is enforced by the server, so it cannot catch a half-open
connection (the host is gone, a partition, a failover with no RST): the client's ``recv``
then blocks until the kernel gives up retransmitting, about 15 minutes on Linux
defaults. These make the client notice on its own.

``tcp_user_timeout`` fires only on data the peer has not acknowledged, so a statement
the server is merely slow on -- a lock wait, say -- is never cut off by it; the
keepalives cover a connection idle in the pool. Either way a dead peer is found in
about 30 s.
"""

from __future__ import annotations

TCP_KEEPALIVE: dict[str, int] = {
    "keepalives": 1,
    "keepalives_idle": 10,
    "keepalives_interval": 5,
    "keepalives_count": 3,
    "tcp_user_timeout": 30_000,  # milliseconds
}
