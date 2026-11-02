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
from scadbuddy.auth.config import LoginConfig, LoginConfigStore
from scadbuddy.auth.gate import caller_of
from scadbuddy.auth.models import Caller
from scadbuddy.auth.observe import WouldRefuse
from scadbuddy.auth.oidc import OidcClient
from scadbuddy.auth.personal_tokens import PersonalTokenStore
from scadbuddy.auth.run_tokens import run_token_key
from scadbuddy.auth.service_credentials import ServiceCredentialStore
from scadbuddy.auth.store import Pool, SessionStore
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.core.pg_keepalive import TCP_KEEPALIVE
from scadbuddy.core.secrets import Envelope, Kek
from scadbuddy.library.url_import import public_transport

#: One short statement per request that carries a cookie.
POOL_SIZE = 2
#: As ``orders/component.py``: an unreachable database fails a lookup within about this.
CONNECT_TIMEOUT = 5.0


class AuthComponent:
    def __init__(self, core: Core) -> None:
        self.core = core
        settings = core.settings
        # Off a private network, every fetch goes to a vetted public address only.
        self.oidc = OidcClient(
            transport=None if settings.auth_oidc_private_network else public_transport()
        )
        self.would_refuse = WouldRefuse(core.metrics)
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
        self._credentials = ServiceCredentialStore(self._pool)
        self._tokens = PersonalTokenStore(self._pool)
        self._run_key: tuple[str, bytes] | None = None

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
    def pool(self) -> Pool:
        self._open()
        return self._pool

    @property
    def credentials(self) -> ServiceCredentialStore:
        self._open()
        return self._credentials

    @property
    def tokens(self) -> PersonalTokenStore:
        self._open()
        return self._tokens

    @property
    def config(self) -> LoginConfigStore:
        self._open()
        return self._config

    def load_config(self) -> tuple[LoginConfig, Envelope | None]:
        """The stored configuration under the deployment's issuer and client id
        (``SCADBUDDY_AUTH_OIDC_*``). A secret sealed for another pair is no secret."""
        stored, envelope = self.config.load()
        settings = self.core.settings
        config = stored.model_copy(
            update={
                "issuer": settings.auth_oidc_issuer.strip(),
                "client_id": settings.auth_oidc_client_id.strip(),
            }
        )
        if (stored.issuer, stored.client_id) != (config.issuer, config.client_id):
            envelope = None
        return config, envelope

    @property
    def kek(self) -> Kek | None:
        """The KEK the settings are sealed under: the login needs it, for the client
        secret and the login cookie."""
        return self.core.settings_store.kek

    def run_token_key(self) -> bytes | None:
        """The key a flow run's token is MACed under (`auth/run_tokens.py`), derived once
        per KEK; None without one."""
        kek = self.kek
        if kek is None:
            return None
        cached = self._run_key
        if cached is None or cached[0] != kek.id:
            cached = (kek.id, run_token_key(kek))
            self._run_key = cached
        return cached[1]

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
