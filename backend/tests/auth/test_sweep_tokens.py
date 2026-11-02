"""#4975 phase 6: the sessions sweep forgets personal access tokens dead for 30 days and
delegation codes past a day, and keeps every other row."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scadbuddy.auth import delegation
from scadbuddy.auth.personal_tokens import KEEP, PersonalTokenStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
ALICE = "user:https://idp.test#alice"


def test_only_tokens_dead_past_the_cutoff_are_forgotten(pg_pool: PgPool) -> None:
    store = PersonalTokenStore(pg_pool)
    now = T0 + timedelta(days=400)
    cutoff = now - KEEP
    # Expired: just past the cutoff, and just inside it.
    _, expired_old = store.mint(
        ALICE, "old", "read", timedelta(days=1), now=cutoff - timedelta(days=2)
    )
    _, expired_new = store.mint(ALICE, "recent", "read", timedelta(days=1), now=cutoff)
    # Revoked: past the cutoff, and inside it, both still within their lifetime.
    _, revoked_old = store.mint(
        ALICE, "revoked-old", "read", timedelta(days=365), now=cutoff - timedelta(days=5)
    )
    assert store.revoke(ALICE, revoked_old.id, now=cutoff - timedelta(days=1))
    _, revoked_new = store.mint(
        ALICE, "revoked-new", "read", timedelta(days=365), now=cutoff - timedelta(days=5)
    )
    assert store.revoke(ALICE, revoked_new.id, now=cutoff + timedelta(days=1))
    _, live = store.mint(ALICE, "live", "write", timedelta(days=90), now=now - timedelta(days=1))

    assert store.delete_expired(now=now) == 2
    with pg_pool.connection() as conn:
        left = {row["name"] for row in conn.execute("SELECT name FROM personal_access_tokens")}
    assert left == {"recent", "revoked-new", "live"}
    assert {expired_old.id, revoked_old.id}.isdisjoint({expired_new.id, revoked_new.id, live.id})


def test_only_delegation_codes_past_a_day_are_forgotten(pg_pool: PgPool) -> None:
    old = delegation.mint(pg_pool, ALICE, timedelta(hours=1), now=T0)
    recent = delegation.mint(pg_pool, ALICE, timedelta(hours=1), now=T0 + timedelta(hours=23))
    assert old != recent
    # The old code expired a minute after T0, more than KEEP before this; the recent one
    # expired within it.
    assert delegation.delete_spent(pg_pool, now=T0 + delegation.KEEP + timedelta(hours=1)) == 1
    with pg_pool.connection() as conn:
        rows = conn.execute("SELECT created_at FROM auth_delegation_codes").fetchall()
    assert [row["created_at"] for row in rows] == [T0 + timedelta(hours=23)]
