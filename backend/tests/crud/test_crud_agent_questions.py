"""The agent's questions and `done` summaries through its CRUD routes (#5299 step 10, area
``questions``, `api/crud_agent_questions.py`): only the ``agent`` credential reaches them; a
turn's `done` post lands only while its placement holds the session (the turn fence), at most
three a turn, replacing what it replaces, with its events and audit rows; a dismissal is written
once; the attention count is read only under its lease; and a malformed id names nothing."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.agent_data import leases
from scadbuddy.api import crud_agent_access, crud_agent_questions
from scadbuddy.render.agent_migrations import migrate_agent
from tests.conftest import PgPool
from tests.crud.test_crud_agent_gate import (
    BASE,
    ME,
    Agent,
    _app,
    _session,
    _sql,
    _token,
    _types,
)

pytestmark = pytest.mark.requires_postgres

CARD = [{"question": "Done", "header": "Done", "multiSelect": False, "options": []}]


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


@pytest.fixture
def app(tables: PgPool) -> FastAPI:
    app = _app(tables)
    app.include_router(crud_agent_questions.router, prefix="/api/v1")
    app.include_router(crud_agent_access.router, prefix="/api/v1")
    return app


@pytest.fixture
def agent(app: FastAPI) -> Iterator[Agent]:
    with TestClient(app) as client:
        yield Agent(client, _token(app))


def _placed(tables: PgPool, session: str, attempt: str = "run-1/1") -> str:
    turn = str(uuid.uuid4())
    _sql(
        tables,
        "UPDATE ai_sessions SET turn_attempt = %s, durable_turn_id = %s WHERE id = %s",
        (attempt, turn, session),
    )
    return turn


def _done(session: str, turn: str, attempt: str = "run-1/1", **extra: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "session_id": session,
        "turn_id": turn,
        "attempt": attempt,
        "tool": "mcp__scadbuddy_questions__request_user_attention",
        "tool_use_id": "toolu_1",
        "questions": CARD,
        "summary": "Rendered the box.",
        "unattended": False,
        **extra,
    }


def test_only_the_agent_credential_reaches_the_routes(app: FastAPI, tables: PgPool) -> None:
    session = _session(tables)
    turn = _placed(tables, session)
    with TestClient(app) as client:
        for caller in ("print", "projects", "render"):
            refused = client.post(
                f"{BASE}/questions/done", json=_done(session, turn), headers=_token(app, caller)
            )
            assert refused.status_code == 403, refused.text
        assert client.post(f"{BASE}/questions/pending", json=ME).status_code == 401
    assert _sql(tables, "SELECT count(*) AS n FROM ai_questions")[0]["n"] == 0


def test_the_areas_list_questions(agent: Agent) -> None:
    assert "questions" in agent.get("/areas")["areas"]


def test_a_body_out_of_bounds_is_refused(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    turn = _placed(tables, session)
    for body in (
        _done(session, turn, id="../x"),
        _done(session, turn, summary="s" * (64 * 1024 + 1)),
        _done(session, turn, questions=[{"big": "x" * (1024 * 1024)}]),
        _done(session, turn, extra=1),
    ):
        assert agent.status("/questions/done", body) == 422
    too_many = {"id": str(uuid.uuid4()), "session_id": session, "answers": ["a"] * 17, "by": ME}
    assert agent.status("/questions/answer", too_many) == 422


def test_a_done_post_lands_only_for_the_placement_that_holds_the_turn(
    agent: Agent, tables: PgPool
) -> None:
    session = _session(tables)
    turn = _placed(tables, session)
    for refused in (
        _done(session, turn, attempt="run-1/2"),
        _done(session, str(uuid.uuid4())),
        _done(session, "not-a-uuid"),
        _done("../x", turn),
    ):
        assert agent.post("/questions/done", refused) == {
            "asked": False,
            "limited": None,
            "logged": [],
        }
    # Not the browser user's any more (a handoff mid-turn): nothing is posted to the new owner.
    _sql(tables, "UPDATE ai_sessions SET owner_kind = 'bearer' WHERE id = %s", (session,))
    assert agent.post("/questions/done", _done(session, turn))["asked"] is False
    assert _sql(tables, "SELECT count(*) AS n FROM ai_questions")[0]["n"] == 0


def test_a_done_post_replaces_the_last_and_is_capped(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    turn = _placed(tables, session)
    first = _done(session, turn)
    posted = agent.post("/questions/done", first)
    assert posted["asked"] is True and posted["limited"] is None
    [batch] = posted["logged"]
    assert [e["type"] for e in batch["events"]] == ["question.asked", "input.requested"]
    assert batch["events"][0] == {
        "v": 1,
        "type": "question.asked",
        "sessionId": session,
        "id": first["id"],
        "tool": "toolu_1",
        "questions": CARD,
        "attention": {"reason": "done", "summary": "Rendered the box."},
    }
    entry = batch["events"][1]["entry"]
    assert entry["id"] == f"question:{first['id']}" and entry["prompt"] == "Done"
    attention = {"reason": "done", "on_timeout": None, "summary": "Rendered the box."}
    assert entry["attention"] == attention
    second = agent.post("/questions/done", _done(session, turn))
    assert [e["type"] for e in second["logged"][0]["events"]] == [
        "question.resolved",
        "input.resolved",
        "question.asked",
        "input.requested",
    ]
    [cancelled] = _sql(tables, "SELECT action, outcome FROM ai_audit WHERE kind = 'question'")
    assert cancelled == {"action": "cancelled", "outcome": "refused"}
    agent.post("/questions/done", _done(session, turn))
    limited = agent.post("/questions/done", _done(session, turn))
    assert limited["asked"] is False and "already posted 3" in limited["limited"]
    rows = _sql(tables, "SELECT action FROM ai_audit WHERE action = 'rate_limited'")
    assert rows == [{"action": "rate_limited"}]


def test_a_summary_is_listed_and_dismissed_once(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    turn = _placed(tables, session)
    body = _done(session, turn)
    agent.post("/questions/done", body)
    [listed] = agent.post("/questions/pending", {"kind": "browser", "id": "browser"})["questions"]
    assert listed["id"] == body["id"] and listed["summary"] == "Rendered the box."
    entry = agent.get("/questions/entry", id=body["id"])["entry"]
    assert entry["outcome"] is None and entry["session_id"] == session
    dismiss = {
        "id": body["id"],
        "session_id": session,
        "answers": ["ok"],
        "by": ME,
        "input_hash": "a" * 64,
    }
    written = agent.post("/questions/answer", dismiss)
    assert [e["type"] for e in written["logged"][0]["events"]] == [
        "question.resolved",
        "input.resolved",
    ]
    [audit] = _sql(
        tables, "SELECT action, input_hash, detail FROM ai_audit WHERE action = 'answered'"
    )
    assert audit["input_hash"] == "a" * 64 and audit["detail"].endswith(": 1 answer")
    again = agent.client.post(f"{BASE}/questions/answer", json=dismiss, headers=agent.headers)
    assert again.status_code == 409 and again.json()["reason"] == "conflict"
    wrong = {**dismiss, "answers": ["a", "b"]}
    agent.post("/questions/done", {**body, "id": str(uuid.uuid4())})
    assert agent.post("/questions/pending", {"kind": "browser", "id": "browser"})["questions"]
    for missing, reason in ((str(uuid.uuid4()), "not_found"), ("../x", "not_found")):
        refused = agent.client.post(
            f"{BASE}/questions/answer", json={**wrong, "id": missing}, headers=agent.headers
        )
        assert refused.status_code == 409 and refused.json()["reason"] == reason
    assert _types(tables, session)[-2:] == ["question.asked", "input.requested"]


def test_the_attention_count_is_read_only_under_its_lease(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    me = str(uuid.uuid4())
    for n in range(2):
        _sql(
            tables,
            "INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder,"
            " tool, requested_at) VALUES (%s, %s, 'answer', 'timed_out', '{}'::jsonb,"
            " 'wait_for_user', now())",
            (f"durable:{session}:run-1:toolu_{n}", session),
        )
    _sql(
        tables,
        "INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder, tool,"
        " requested_at) VALUES (%s, %s, 'answer', 'cancelled', '{}'::jsonb, 'wait_for_user',"
        " now())",
        (f"durable:{session}:run-1:tab:toolu_9", session),
    )
    for token in ("../x", str(uuid.uuid4())):
        refused = agent.client.post(
            f"{BASE}/questions/attention-count",
            json={"own": True, "lease_token": token},
            headers=agent.headers,
        )
        assert refused.status_code == 409 and refused.json()["reason"] == "lease-lost"
    with tables.connection() as conn, conn.transaction():
        assert leases.acquire(conn, "attention-rate", me, 60) is not None
    assert agent.post("/questions/attention-count", {"own": True, "lease_token": me}) == {"n": 2}
    assert agent.post("/questions/attention-count", {"own": False, "lease_token": me}) == {"n": 1}


def test_the_lease_is_busy_while_an_sql_pod_holds_the_rate_lock(
    tables: PgPool, pg_conninfo: str
) -> None:
    import psycopg

    with psycopg.connect(pg_conninfo) as sql_pod:
        sql_pod.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('scadbuddy:attention-rate', 0))"
        )
        with tables.connection() as conn, conn.transaction():
            assert leases.acquire(conn, "attention-rate", str(uuid.uuid4()), 60) is None
        sql_pod.rollback()
    with tables.connection() as conn, conn.transaction():
        assert leases.acquire(conn, "attention-rate", str(uuid.uuid4()), 60) is not None


def test_a_turns_done_facts_are_read(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    turn = _placed(tables, session)
    since = _sql(tables, "SELECT now()::text AS t")[0]["t"]
    _sql(
        tables,
        "INSERT INTO ai_session_resources (session_id, tool, resource_type, resource_id, action)"
        " VALUES (%s, 'render_model', 'model', 'box', 'created')",
        (session,),
    )
    facts = agent.post(
        "/questions/done-facts", {"session_id": session, "turn_id": turn, "since": since}
    )
    assert facts["windows"] == []
    [touch] = facts["touches"]
    assert touch["in_turn"] is True and touch["tool"] == "render_model"
    missing = {"session_id": "../x", "turn_id": turn, "since": since}
    assert agent.post("/questions/done-facts", missing) == {"windows": [], "touches": []}
