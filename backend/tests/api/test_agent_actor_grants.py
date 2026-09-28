"""The agent-actor gate's grant (#349, AI spec §5.3): "unless an approved, unconsumed
outward action for that session authorises that request; the backend consumes it once".

The agent writes a grant only after a human approved that exact method and path
(``agent/src/harness/headlessGrants.ts``); the agent's own test runs :data:`GRANT_SQL`
against its real schema (``agent/test/headlessGrants.pg.test.ts``). Here: the gate's use
of a grant check, and :class:`PostgresGrants` against a cut-down copy of that schema.
"""

from __future__ import annotations

import asyncio
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from scadbuddy.api.agent_actor import (
    AGENT_ACTOR_HEADER,
    GRANT_CONCURRENCY,
    GRANT_POOL_MAX,
    AgentActorGate,
    GrantCheck,
    PostgresGrants,
)

SESSION = "0b0e5bd7-1f38-4c1e-9a55-3c1b1f2a9d10"
MARKED = {AGENT_ACTOR_HEADER: SESSION}
RUN = "/api/v1/print/outputs/out-1/run"


def _echo_app() -> Starlette:
    async def ok(request: Request) -> PlainTextResponse:
        return PlainTextResponse(f"reached {request.method} {request.url.path}")

    return Starlette(
        routes=[Route("/{path:path}", ok, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])]
    )


def _gated(grants: GrantCheck | None) -> TestClient:
    return TestClient(AgentActorGate(_echo_app(), grants=grants))


def test_a_granted_outward_request_reaches_its_route() -> None:
    asked: list[tuple[str, str, str]] = []

    async def grants(session: str, method: str, path: str) -> bool:
        asked.append((session, method, path))
        return True

    response = _gated(grants).post(RUN, headers=MARKED)
    assert response.status_code == 200
    assert response.text == f"reached POST {RUN}"
    assert asked == [(SESSION, "POST", RUN)]


def test_an_ungranted_outward_request_is_refused_and_says_how_to_ask() -> None:
    async def grants(session: str, method: str, path: str) -> bool:
        return False

    response = _gated(grants).post(RUN, headers=MARKED)
    assert response.status_code == 403
    assert "mcp__scadbuddy_browser__authorize_request" in response.json()["detail"]


def test_without_a_grant_store_every_marked_outward_request_is_refused() -> None:
    assert _gated(None).post(RUN, headers=MARKED).status_code == 403


def test_a_marker_that_is_not_a_session_id_never_reaches_the_grant_store() -> None:
    asked: list[str] = []

    async def grants(session: str, method: str, path: str) -> bool:
        asked.append(session)
        return True

    response = _gated(grants).post(RUN, headers={AGENT_ACTOR_HEADER: "' OR 1=1 --"})
    assert response.status_code == 403
    assert asked == []


def test_the_grant_store_gets_the_canonical_session_id() -> None:
    asked: list[str] = []

    async def grants(session: str, method: str, path: str) -> bool:
        asked.append(session)
        return False

    _gated(grants).post(RUN, headers={AGENT_ACTOR_HEADER: SESSION.upper()})
    assert asked == [SESSION]


def test_allowed_and_unmarked_requests_never_consult_the_grant_store() -> None:
    asked: list[str] = []

    async def grants(session: str, method: str, path: str) -> bool:
        asked.append(path)
        return False

    client = _gated(grants)
    assert client.get("/api/v1/models", headers=MARKED).status_code == 200
    assert client.post("/api/v1/models/demo/render", headers=MARKED).status_code == 200
    assert client.post(RUN).status_code == 200
    assert asked == []


# The agent's schema, cut down to the columns GRANT_SQL reads.
MINI_SCHEMA = """
CREATE TABLE ai_sessions (id uuid PRIMARY KEY, turn_id uuid, lease_until timestamptz);
CREATE TABLE ai_approvals (id uuid PRIMARY KEY, session_id uuid, decision text,
                           consumed_at timestamptz, revoked_at timestamptz);
CREATE TABLE ai_headless_grants (
  id uuid PRIMARY KEY, session_id uuid NOT NULL, turn_id uuid NOT NULL,
  approval_id uuid NOT NULL, method text NOT NULL, path text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL,
  used_at timestamptz);
"""


def _seed(
    conninfo: str, *, lease: str = "1 minute", expires: str = "1 minute", decision: str = "approved"
) -> None:
    turn = str(uuid.uuid4())
    approval = str(uuid.uuid4())
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(MINI_SCHEMA.encode())
        conn.execute(
            "INSERT INTO ai_sessions VALUES (%s, %s, now() + %s::interval)", (SESSION, turn, lease)
        )
        conn.execute(
            "INSERT INTO ai_approvals (id, session_id, decision, consumed_at)"
            " VALUES (%s, %s, %s, now())",
            (approval, SESSION, decision),
        )
        conn.execute(
            "INSERT INTO ai_headless_grants (id, session_id, turn_id, approval_id, method, path,"
            " expires_at) VALUES (%s, %s, %s, %s, 'POST', %s, now() + %s::interval)",
            (str(uuid.uuid4()), SESSION, turn, approval, RUN, expires),
        )


async def _check(
    conninfo: str, session: str = SESSION, method: str = "POST", path: str = RUN
) -> bool:
    grants = PostgresGrants(conninfo)
    try:
        return await grants(session, method, path)
    finally:
        await grants.aclose()


@pytest.mark.requires_postgres
async def test_a_postgres_grant_is_used_once_for_its_exact_request(pg_conninfo: str) -> None:
    _seed(pg_conninfo)
    grants = PostgresGrants(pg_conninfo)
    try:
        assert not await grants(SESSION, "POST", RUN + "/x")
        assert not await grants(SESSION, "PUT", RUN)
        assert not await grants(str(uuid.uuid4()), "POST", RUN)
        assert await grants(SESSION, "POST", RUN)
        assert not await grants(SESSION, "POST", RUN), "a grant is used at most once"
    finally:
        await grants.aclose()


@pytest.mark.requires_postgres
@pytest.mark.parametrize(
    ("lease", "expires", "decision"),
    [
        ("-1 second", "1 minute", "approved"),
        ("1 minute", "-1 second", "approved"),
        ("1 minute", "1 minute", "denied"),
    ],
)
async def test_a_postgres_grant_needs_a_live_turn_time_and_an_approval(
    pg_conninfo: str, lease: str, expires: str, decision: str
) -> None:
    _seed(pg_conninfo, lease=lease, expires=expires, decision=decision)
    assert not await _check(pg_conninfo)


@pytest.mark.requires_postgres
async def test_a_postgres_grant_from_another_turn_is_refused(pg_conninfo: str) -> None:
    _seed(pg_conninfo)
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute("UPDATE ai_sessions SET turn_id = %s", (str(uuid.uuid4()),))
    assert not await _check(pg_conninfo)


@pytest.mark.requires_postgres
async def test_a_postgres_grant_whose_approval_was_revoked_is_refused(pg_conninfo: str) -> None:
    _seed(pg_conninfo)
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute("UPDATE ai_approvals SET revoked_at = now()")
    assert not await _check(pg_conninfo)


@pytest.mark.requires_postgres
async def test_a_database_without_the_agent_tables_refuses(pg_conninfo: str) -> None:
    assert not await _check(pg_conninfo)


async def test_an_unreachable_database_refuses() -> None:
    grants = PostgresGrants("postgresql://nobody@127.0.0.1:1/none", timeout=1.0)
    try:
        assert not await grants(SESSION, "POST", RUN)
    finally:
        await grants.aclose()


# -- a flood of bogus markers (review of #518: one connection per request was a DoS) --


@pytest.mark.requires_postgres
async def test_a_flood_of_bogus_markers_opens_at_most_the_pool_and_a_real_grant_still_works(
    pg_conninfo: str,
) -> None:
    _seed(pg_conninfo)
    grants = PostgresGrants(pg_conninfo)
    try:
        flood = [grants(str(uuid.uuid4()), "DELETE", f"/api/v1/x/{i}") for i in range(200)]
        assert not any(await asyncio.gather(*flood))
        stats = grants.pool.get_stats()
        # Never more connections than the pool holds, however many requests came.
        assert stats.get("connections_num", 0) <= GRANT_POOL_MAX
        # Past the concurrency cap, requests were refused without touching the database.
        assert grants.refused_busy >= 200 - GRANT_CONCURRENCY
        assert await grants(SESSION, "POST", RUN)
    finally:
        await grants.aclose()


@pytest.mark.requires_postgres
async def test_checks_past_the_cap_are_refused_at_once_not_queued(pg_conninfo: str) -> None:
    grants = PostgresGrants(pg_conninfo, concurrency=1)
    try:
        results = await asyncio.gather(*(grants(str(uuid.uuid4()), "POST", RUN) for _ in range(10)))
        assert results == [False] * 10
        assert grants.refused_busy == 9
        assert grants.in_flight == 0
    finally:
        await grants.aclose()


def test_the_cheap_rejects_need_no_database() -> None:
    asked: list[str] = []

    async def grants(session: str, method: str, path: str) -> bool:
        asked.append(path)
        return True

    client = _gated(grants)
    # A path no grant can name (the agent only grants /api/v1/... paths).
    assert client.post("/not-api/x", headers=MARKED).status_code == 403
    assert client.delete("/", headers=MARKED).status_code == 403
    assert asked == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/api/v1/settings"),
        ("PUT", "/api/v1/settings/"),
        ("PUT", "/api/v1/settings/print-options"),
        ("POST", "/api/v1/settings/register-sidebar"),
    ],
)
def test_settings_are_never_grantable(method: str, path: str) -> None:
    # A grant covers method and path, not the body: a granted PUT /api/v1/settings could
    # point bambuddy_url anywhere and leak the API key (review of #518).
    asked: list[str] = []

    async def grants(session: str, method: str, path: str) -> bool:
        asked.append(path)
        return True

    response = _gated(grants).request(method, path, headers=MARKED)
    assert response.status_code == 403
    assert "no approval can grant" in response.json()["detail"]
    assert asked == []


def test_a_path_that_only_starts_like_settings_is_still_grantable() -> None:
    async def grants(session: str, method: str, path: str) -> bool:
        return True

    assert _gated(grants).post("/api/v1/settingsx", headers=MARKED).status_code == 200
