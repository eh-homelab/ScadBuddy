"""``AUTH``: the sessions store (#4975, spec 2026-11-01 §5.2), discovered as every
component is.

Building it does no I/O: the pool opens on first use, as ``orders/component.py``'s
does, since ``Components.build_all`` runs before the lifespan opens anything. ``AuthGate``
(``main.create_app``) reaches ``sessions`` only for a request carrying a session cookie.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool
from starlette.requests import HTTPConnection

from scadbuddy.auth.gate import caller_of
from scadbuddy.auth.models import Caller
from scadbuddy.auth.store import SessionStore
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE

#: One short statement per request that carries a cookie.
POOL_SIZE = 2
#: As ``orders/component.py``: an unreachable database fails a lookup within about this.
CONNECT_TIMEOUT = 5.0


class AuthComponent:
    def __init__(self, core: Core) -> None:
        self._store: SessionStore | None = None
        self._pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            core.settings.database_url,
            min_size=1,
            max_size=POOL_SIZE,
            open=False,
            timeout=CONNECT_TIMEOUT,
            connection_class=Connection[DictRow],
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "connect_timeout": max(1, int(CONNECT_TIMEOUT)),
                **TCP_KEEPALIVE,
            },
            name="scadbuddy-auth",
        )

    @property
    def sessions(self) -> SessionStore:
        if self._store is None:
            self._pool.open(wait=False)
            self._store = SessionStore(self._pool)
        return self._store

    def close(self) -> None:
        self._pool.close()


AUTH: Key[AuthComponent] = Key("auth")


def _build(core: Core, components: Components) -> AuthComponent:
    return AuthComponent(core)


@asynccontextmanager
async def _run(auth: AuthComponent) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await asyncio.to_thread(auth.close)


COMPONENT = Component(AUTH, build=_build, run=_run)


def _caller(connection: HTTPConnection) -> Caller:
    return caller_of(connection.scope)


#: The request's caller, as ``AuthGate`` resolved it.
CallerDep = Annotated[Caller, Depends(_caller)]
