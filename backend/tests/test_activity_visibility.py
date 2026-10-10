"""Who owns an assistant session (spec 2026-10-09 §7)."""

import uuid

import pytest

from scadbuddy.activity.visibility import session_owner
from scadbuddy.core.authorship import principal_header
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


@pytest.mark.parametrize(
    ("owner_id", "header"),
    [
        # agent/test/authoring.test.ts, "percent-encodes what the backend would refuse".
        ("oidc:https://idp.example#ünï code", "oidc:https://idp.example#%C3%BCn%C3%AF%20code"),
        ("token:t1", "token:t1"),
        ("x" * 400, "x" * 300),
        # "cuts a long principal id between escapes, never inside one".
        ("x" * 298 + "ü", "x" * 298),
        ("x" * 294 + "ü", "x" * 294 + "%C3%BC"),
        ("x" * 290 + "😀", "x" * 290),
        ("x" * 288 + "😀", "x" * 288 + "%F0%9F%98%80"),
        # "encodes a lone surrogate instead of throwing".
        ("a\ud800b", "a%EF%BF%BDb"),
        ("", "unknown"),
    ],
)
def test_principal_header_mirrors_the_agents(owner_id: str, header: str) -> None:
    assert principal_header(owner_id) == header


async def test_an_owner_is_named_as_its_tools_name_it(pg_pool: PgPool) -> None:
    session = str(uuid.uuid4())
    with pg_pool.connection() as conn:
        conn.execute(
            "CREATE TABLE ai_sessions (id uuid PRIMARY KEY, owner_kind text, owner_id text)"
        )
        conn.execute(
            "INSERT INTO ai_sessions VALUES (%s, 'oidc', 'oidc:https://idp.example#ünï code')",
            (session,),
        )
    owner = await session_owner(pg_pool, session)
    assert owner == "oidc:https://idp.example#%C3%BCn%C3%AF%20code"
