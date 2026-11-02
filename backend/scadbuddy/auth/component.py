"""``AUTH``: the sessions store, the login configuration and the OIDC client (#4975, spec
2026-11-01 §5), discovered as every component is.

Building it does no I/O: the pool opens on first use, as ``orders/component.py``'s
does, since ``Components.build_all`` runs before the lifespan opens anything; the first
use is under a lock, since the gate's lookups run on worker threads. ``AuthGate``
(``main.create_app``) reaches ``sessions`` only for a request carrying a session cookie.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends
from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool
from starlette.requests import HTTPConnection

from scadbuddy.api.components import component_dep
from scadbuddy.auth.config import LoginConfigStore
from scadbuddy.auth.gate import caller_of
from scadbuddy.auth.models import Caller
from scadbuddy.auth.oidc import OidcClient
from scadbuddy.auth.store import SessionStore
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE
from scadbuddy.core.secrets import Kek

#: One short statement per request that carries a cookie.
POOL_SIZE = 2
#: As ``orders/component.py``: an unreachable database fails a lookup within about this.
CONNECT_TIMEOUT = 5.0


class AuthComponent:
    def __init__(self, core: Core) -> None:
        self.core = core
        self.oidc = OidcClient()
        self._lock = threading.Lock()
        self._opened = False
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
        self._sessions = SessionStore(self._pool)
        self._config = LoginConfigStore(self._pool)

    def _open(self) -> None:
        with self._lock:
            if not self._opened:
                self._pool.open(wait=False)
                self._opened = True

    @property
    def sessions(self) -> SessionStore:
        self._open()
        return self._sessions

    @property
    def config(self) -> LoginConfigStore:
        self._open()
        return self._config

    @property
    def kek(self) -> Kek | None:
        """The KEK the settings are sealed under: the login needs it, for the client
        secret and the login cookie."""
        return self.core.settings_store.kek

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

AuthDep = Annotated[AuthComponent, component_dep(AUTH)]


def _caller(connection: HTTPConnection) -> Caller:
    return caller_of(connection.scope)


#: The request's caller, as ``AuthGate`` resolved it.
CallerDep = Annotated[Caller, Depends(_caller)]
