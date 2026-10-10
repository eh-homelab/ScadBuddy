"""Who owns an assistant session (spec 2026-10-09 §7)."""

import uuid

import pytest

from scadbuddy.activity.visibility import session_owner
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres


async def test_missing_table_is_none(pg_pool: PgPool) -> None:
    assert await session_owner(pg_pool, str(uuid.uuid4())) is None


async def test_non_uuid_is_none(pg_pool: PgPool) -> None:
    assert await session_owner(pg_pool, "not-a-uuid") is None


async def test_owners(pg_pool: PgPool) -> None:
    browser, bearer = str(uuid.uuid4()), str(uuid.uuid4())
    with pg_pool.connection() as conn:
        conn.execute(
            "CREATE TABLE ai_sessions (id uuid PRIMARY KEY, owner_kind text, owner_id text)"
        )
        conn.execute(
            "INSERT INTO ai_sessions VALUES (%s, 'browser', 'x'), (%s, 'token', 'token:t1')",
            (browser, bearer),
        )
    assert await session_owner(pg_pool, browser) == "browser"
    assert await session_owner(pg_pool, bearer) == "token:t1"
    assert await session_owner(pg_pool, str(uuid.uuid4())) is None
