"""Service credentials (#4975 phase 4, spec 2026-11-01 §7.1, §7.2): issue and rotation,
verification by hash, the sealed hand-off through each caller's view, revocation, and
the gate's bearer lookup."""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from scadbuddy.auth.gate import AuthGate, caller_of
from scadbuddy.auth.models import ANONYMOUS, Service
from scadbuddy.auth.service_credentials import (
    LIFETIME,
    ROTATE_BEFORE,
    SCOPES,
    ServiceCredentialStore,
    TokenReader,
    aad,
    grant_caller_views,
)
from scadbuddy.core.secrets import Kek
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

KEK = Kek("k1", os.urandom(32))


@pytest.fixture
def store(pg_pool: PgPool) -> ServiceCredentialStore:
    return ServiceCredentialStore(pg_pool)


def _token(pg_conninfo: str, caller: str = "agent", kek: Kek = KEK) -> str:
    token = TokenReader(caller, pg_conninfo, kek).token()  # type: ignore[arg-type]
    assert token is not None
    return token


def test_rotation_issues_one_per_caller_and_again_only_when_due(
    store: ServiceCredentialStore,
) -> None:
    now = datetime.now(UTC)
    assert len(store.rotate_due(KEK, now=now)) == 3
    assert store.rotate_due(KEK, now=now + LIFETIME - ROTATE_BEFORE - timedelta(hours=1)) == []
    assert len(store.rotate_due(KEK, now=now + LIFETIME - ROTATE_BEFORE + timedelta(hours=1))) == 3
    # A new KEK: the callers could not open the old ones, so new ones at once.
    assert len(store.rotate_due(Kek("k2", os.urandom(32)), now=now)) == 3


def test_a_caller_reads_its_token_and_the_api_verifies_it(
    store: ServiceCredentialStore, pg_conninfo: str, pg_pool: PgPool
) -> None:
    store.rotate_due(KEK)
    token = _token(pg_conninfo, "print")
    assert token.startswith("sbsvc_")
    assert store.resolve(token) == Service("print", SCOPES["print"])
    # Stored as its hash; the sealed copy opens only under its own row's AAD.
    with pg_pool.connection() as conn:
        rows = conn.execute("SELECT * FROM service_credentials").fetchall()
    assert all(token not in {str(v) for v in row.values()} for row in rows)
    assert aad("x", "print", ["b", "a"]) == "service_credentials|x|print|a,b"
    assert store.resolve(token.replace("sbsvc_", "sbsvc_0", 1)[:-1]) is None
    assert TokenReader("print", pg_conninfo, None).token() is None


def test_revoked_or_expired_is_refused(store: ServiceCredentialStore, pg_conninfo: str) -> None:
    store.rotate_due(KEK)
    token = _token(pg_conninfo)
    assert store.resolve(token, now=datetime.now(UTC) + LIFETIME + timedelta(seconds=1)) is None
    (agent,) = [c for c in store.list() if c.caller == "agent"]
    assert store.revoke(agent.id) == "agent"
    assert store.resolve(token) is None
    assert store.revoke(agent.id) is None
    assert not store.has_live("agent")


def _after(start: threading.Barrier, run: Callable[[], object]) -> None:
    start.wait()
    run()


def test_a_revoke_and_the_rotation_replace_once(store: ServiceCredentialStore) -> None:
    """#5393: the prune Schedule's rotation and a revoke's replacement, at once, leave
    one live credential, not one each."""
    store.rotate_due(KEK)
    for _ in range(20):
        for credential in store.list():
            if credential.caller == "agent":
                store.revoke(credential.id)
        start = threading.Barrier(2)
        threads = [
            threading.Thread(target=_after, args=(start, lambda: store.rotate_due(KEK))),
            threading.Thread(
                target=_after, args=(start, lambda: store.replace_if_none(KEK, "agent"))
            ),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        live = [c for c in store.list() if c.caller == "agent" and c.revoked_at is None]
        assert len(live) == 1


def test_last_used_is_recorded(store: ServiceCredentialStore, pg_conninfo: str) -> None:
    store.rotate_due(KEK)
    store.resolve(_token(pg_conninfo, "projects"))
    (projects,) = [c for c in store.list() if c.caller == "projects"]
    assert projects.last_used_at is not None


def test_each_role_is_granted_its_own_view_only(pg_pool: PgPool, pg_conninfo: str) -> None:
    role = "scadbuddy_print"
    with pg_pool.connection() as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
            with contextlib.suppress(psycopg.errors.DuplicateObject):
                conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
        assert role in grant_caller_views(conn)
        privileges = conn.execute(
            "SELECT table_name FROM information_schema.role_table_grants"
            " WHERE grantee = %s AND table_schema = current_schema()",
            (role,),
        ).fetchall()
    assert {row["table_name"] for row in privileges} == {"service_credentials_print"}


async def test_the_gate_resolves_a_bearer_credential(
    store: ServiceCredentialStore, pg_conninfo: str
) -> None:
    store.rotate_due(KEK)
    token = _token(pg_conninfo, "print")
    seen = []

    async def app(scope: dict[str, object], receive: object, send: object) -> None:
        seen.append(caller_of(scope))

    gate = AuthGate(app, sessions=lambda: None, credentials=lambda: store)  # type: ignore[arg-type,return-value]
    for header in (f"Bearer {token}", "Bearer sbsvc_0000000000000000." + "a" * 43):
        scope = {"type": "http", "headers": [(b"authorization", header.encode())]}
        await gate(scope, None, None)  # type: ignore[arg-type]
    assert seen == [Service("print", SCOPES["print"]), ANONYMOUS]
