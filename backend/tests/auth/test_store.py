"""``auth_sessions`` (#4975, spec 2026-11-01 §5.2): hashed tokens, the idle and absolute
deadlines, the once-a-minute slide, revocation and the expired rows' delete."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from scadbuddy.auth.models import ABSOLUTE_LIFETIME, IDLE_TIMEOUT
from scadbuddy.auth.store import SessionStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)
ISSUER = "https://idp.example"


@pytest.fixture
def store(pg_pool: PgPool) -> SessionStore:
    return SessionStore(pg_pool)


def _create(store: SessionStore, subject: str = "alice") -> str:
    token, _ = store.create(issuer=ISSUER, subject=subject, kind="login", now=T0)
    return token


def test_the_token_is_stored_only_as_its_hash(store: SessionStore, pg_pool: PgPool) -> None:
    token, session = store.create(
        issuer=ISSUER, subject="alice", kind="login", name="Alice", now=T0
    )
    assert session.id_hash == hashlib.sha256(token.encode()).digest()
    assert session.principal == f"user:{ISSUER}#alice"
    assert session.idle_expires_at == T0 + IDLE_TIMEOUT
    assert session.absolute_expires_at == T0 + ABSOLUTE_LIFETIME
    with pg_pool.connection() as conn:
        rows = conn.execute("SELECT * FROM auth_sessions").fetchall()
    assert len(rows) == 1
    assert token not in {str(value) for value in rows[0].values()}


def test_resolve_finds_the_session(store: SessionStore) -> None:
    token = _create(store)
    session = store.resolve(token, now=T0 + timedelta(seconds=1))
    assert session is not None
    assert session.subject == "alice"


@pytest.mark.parametrize("token", ["", "short", "x" * 43 + "!"])
def test_a_malformed_token_is_no_session(store: SessionStore, token: str) -> None:
    assert store.resolve(token, now=T0) is None


def test_idle_expiry(store: SessionStore) -> None:
    token = _create(store)
    assert store.resolve(token, now=T0 + IDLE_TIMEOUT) is None


def test_the_idle_deadline_slides_at_most_once_a_minute(store: SessionStore) -> None:
    token = _create(store)
    early = store.resolve(token, now=T0 + timedelta(seconds=30))
    assert early is not None
    assert early.last_seen_at == T0
    later = T0 + timedelta(minutes=2)
    slid = store.resolve(token, now=later)
    assert slid is not None
    assert slid.last_seen_at == later
    assert slid.idle_expires_at == later + IDLE_TIMEOUT


def test_the_slide_never_passes_the_absolute_deadline(store: SessionStore) -> None:
    token = _create(store)
    for day in (13, 26):
        assert store.resolve(token, now=T0 + timedelta(days=day)) is not None
    session = store.resolve(token, now=T0 + timedelta(days=29))
    assert session is not None
    assert session.idle_expires_at == T0 + ABSOLUTE_LIFETIME
    assert store.resolve(token, now=T0 + ABSOLUTE_LIFETIME) is None


def test_a_short_session_caps_its_idle_deadline(store: SessionStore) -> None:
    token, session = store.create(
        issuer=ISSUER, subject="alice", kind="break_glass", lifetime=timedelta(hours=1), now=T0
    )
    assert session.idle_expires_at == session.absolute_expires_at == T0 + timedelta(hours=1)
    assert store.resolve(token, now=T0 + timedelta(minutes=59)) is not None
    assert store.resolve(token, now=T0 + timedelta(hours=1)) is None


def test_revoke(store: SessionStore) -> None:
    token, session = store.create(issuer=ISSUER, subject="alice", kind="login", now=T0)
    assert store.revoke(session.id_hash, now=T0) is True
    assert store.revoke(session.id_hash, now=T0) is False
    assert store.resolve(token, now=T0) is None


def test_revoke_all_is_one_principal(store: SessionStore) -> None:
    first = _create(store)
    second = _create(store)
    other = _create(store, subject="bob")
    assert store.revoke_all(f"user:{ISSUER}#alice", now=T0) == 2
    assert store.resolve(first, now=T0) is None
    assert store.resolve(second, now=T0) is None
    assert store.resolve(other, now=T0) is not None


def test_delete_expired_keeps_only_live_rows(store: SessionStore, pg_pool: PgPool) -> None:
    live = _create(store)
    _, revoked = store.create(issuer=ISSUER, subject="r", kind="login", now=T0)
    store.revoke(revoked.id_hash, now=T0)
    store.create(
        issuer=ISSUER, subject="short", kind="delegated", lifetime=timedelta(hours=1), now=T0
    )
    assert store.delete_expired(now=T0 + timedelta(hours=2)) == 2
    with pg_pool.connection() as conn:
        left = conn.execute("SELECT subject FROM auth_sessions").fetchall()
    assert [row["subject"] for row in left] == ["alice"]
    assert store.resolve(live, now=T0 + timedelta(hours=2)) is not None
