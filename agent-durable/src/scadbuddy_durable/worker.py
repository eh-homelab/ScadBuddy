"""What the sidecar runs: no Temporal worker (plan 2026-10-13 Step 8.1, spec §14.4).

The ``agent`` queue's worker ran the Python turn's session workflow. Every session is an
AgentSession since Step 6.3, and Step 7.2 moved the open Python sessions to it, so that
worker is gone. ``python -m scadbuddy_durable`` serves /healthz only and says why, until a
Python workload exists (decision Q9). The modules kept beside this one (``codec``,
``secrets``, ``credentials``, ``payload_store``, ``gate/``) stay tested against the agent's
vector files.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import psycopg

Connect = Callable[[], AbstractAsyncContextManager[psycopg.AsyncConnection[Any]]]

NO_WORKER = (
    "no worker: the Python turn is retired (spec 2026-10-13 §14.4), and agent-durable "
    "has no Temporal workload"
)


def connector(url: str, search_path: str | None = None) -> Connect:
    @asynccontextmanager
    async def connect() -> AsyncIterator[psycopg.AsyncConnection[Any]]:
        conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
        try:
            if search_path:
                await conn.execute(f"SET search_path TO {search_path}")
            yield conn
        finally:
            await conn.close()

    return connect
