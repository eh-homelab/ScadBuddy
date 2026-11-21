"""The agent's /mcp session relay through its CRUD routes (#5299 step 11, area ``mcp-relay``,
`api/crud_agent_mcp.py`): only the ``agent`` credential reaches them; the directory is written,
counted, looked up and removed as the agent's ``mcp/sessionRelay.ts`` did over SQL; a message's
body row and its ``scadbuddy_mcp`` NOTIFY commit together; a row is taken once; and a malformed
id, an oversized or ill-shaped header names nothing and NOTIFYs nothing."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import crud_agent_mcp
from scadbuddy.render.agent_migrations import migrate_agent
from tests.conftest import PgPool
from tests.crud.test_crud_agent_gate import BASE, Agent, _app, _sql, _token

pytestmark = pytest.mark.requires_postgres

HASH = "b" * 64
REPLICA = str(uuid.uuid4())


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


@pytest.fixture
def app(tables: PgPool) -> FastAPI:
    app = _app(tables)
    app.include_router(crud_agent_mcp.router, prefix="/api/v1")
    return app


@pytest.fixture
def agent(app: FastAPI) -> Iterator[Agent]:
    with TestClient(app) as client:
        yield Agent(client, _token(app))


@pytest.fixture
def heard(pg_conninfo: str) -> Iterator[psycopg.Connection[Any]]:
    """A connection LISTENing on the relay's channel, as an agent replica's did."""
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute("LISTEN scadbuddy_mcp")
        yield conn


def _notified(conn: psycopg.Connection[Any], timeout: float = 0.5) -> list[str]:
    return [n.payload for n in conn.notifies(timeout=timeout, stop_after=10)]


def test_only_the_agent_credential_reaches_the_routes(app: FastAPI, agent: Agent) -> None:
    for caller in ("print", "projects"):
        refused = agent.client.post(
            f"{BASE}/mcp-relay/sessions/owner", json={"hash": HASH}, headers=_token(app, caller)
        )
        assert refused.status_code == 403, refused.text
        assert refused.headers["x-scadbuddy-crud"] == "1"
    assert (
        agent.client.post(f"{BASE}/mcp-relay/sessions/owner", json={"hash": HASH}).status_code
        == 401
    )
    assert "mcp-relay" in agent.get("/areas")["areas"]


def test_the_directory_is_written_counted_and_removed(agent: Agent, tables: PgPool) -> None:
    agent.post("/mcp-relay/sessions", {"hash": HASH, "replica": REPLICA, "caller_key": "token:1"})
    other = "c" * 64
    agent.post("/mcp-relay/sessions", {"hash": other, "replica": REPLICA, "caller_key": "token:2"})
    assert agent.post("/mcp-relay/sessions/owner", {"hash": HASH}) == {"replica": REPLICA}
    assert agent.post("/mcp-relay/counts", {"caller_key": "token:1", "idle_seconds": 60}) == {
        "total": 2,
        "caller": 1,
    }
    # Registered again by another replica: the row moves, as the SQL's ON CONFLICT did.
    moved = str(uuid.uuid4())
    agent.post("/mcp-relay/sessions", {"hash": HASH, "replica": moved, "caller_key": "token:1"})
    # A gone owner's removal never takes the new owner's row.
    assert agent.post("/mcp-relay/sessions/remove", {"hash": HASH, "replica": REPLICA}) == {
        "done": False
    }
    assert agent.post("/mcp-relay/sessions/owner", {"hash": HASH}) == {"replica": moved}
    _sql(tables, "UPDATE ai_mcp_sessions SET last_seen = now() - interval '2 hours'")
    agent.post("/mcp-relay/sessions/touch", {"hash": HASH})
    assert (
        agent.post("/mcp-relay/counts", {"caller_key": "token:1", "idle_seconds": 60})["total"] == 1
    )
    # The sweep drops the idle row only.
    agent.post("/mcp-relay/sweep", {"idle_seconds": 60})
    assert [r["id_hash"] for r in _sql(tables, "SELECT id_hash FROM ai_mcp_sessions")] == [HASH]
    assert agent.post("/mcp-relay/sessions/remove", {"hash": HASH}) == {"done": True}
    assert agent.post("/mcp-relay/sessions/owner", {"hash": HASH}) == {"replica": None}


def test_a_request_row_and_its_notify_commit_together(
    agent: Agent, tables: PgPool, heard: psycopg.Connection[Any]
) -> None:
    req, row, to = str(uuid.uuid4()), str(uuid.uuid4()), REPLICA
    body = {"from": str(uuid.uuid4()), "hash": HASH, "principal": {"id": "x"}, "request": {}}
    agent.post(
        "/mcp-relay/send", {"header": {"t": "req", "req": req, "to": to, "row": row}, "body": body}
    )
    [payload] = _notified(heard)
    assert json.loads(payload) == {"t": "req", "req": req, "to": to, "row": row}
    # The row was readable when the header was heard, and is taken once.
    assert agent.post("/mcp-relay/rows/take", {"row": row}) == {"body": body}
    assert agent.post("/mcp-relay/rows/take", {"row": row}) == {"body": None}
    assert agent.post("/mcp-relay/rows/withdraw", {"row": row}) == {"done": False}


def test_an_inline_chunk_rides_in_the_notify(agent: Agent, heard: psycopg.Connection[Any]) -> None:
    req = str(uuid.uuid4())
    agent.post(
        "/mcp-relay/send",
        {"header": {"t": "chunk", "req": req, "to": REPLICA, "text": "data: 1\n\n"}},
    )
    agent.post(
        "/mcp-relay/send",
        {
            "header": {
                "t": "head",
                "req": req,
                "to": REPLICA,
                "status": 200,
                "headers": [["a", "b"]],
            }
        },
    )
    agent.post(
        "/mcp-relay/send", {"header": {"t": "end", "req": req, "to": REPLICA, "error": True}}
    )
    payloads = [json.loads(p) for p in _notified(heard)]
    assert [p["t"] for p in payloads] == ["chunk", "head", "end"]
    assert payloads[0]["text"] == "data: 1\n\n"
    assert payloads[1]["headers"] == [["a", "b"]]
    assert payloads[2]["error"] is True


def test_a_non_ascii_chunk_the_agent_sized_rides_in_the_notify(
    agent: Agent, heard: psycopg.Connection[Any]
) -> None:
    # The agent inlines a chunk whose JSON is at most 7 500 UTF-8 bytes (sessionRelay.ts
    # INLINE_MAX_BYTES); written as \uXXXX escapes this one would pass 7 999.
    text = "data: " + "漢°—" * 900 + "\n\n"
    header = {"t": "chunk", "req": str(uuid.uuid4()), "to": REPLICA, "text": text}
    assert len(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode()) <= 7500
    assert len(json.dumps(header, separators=(",", ":")).encode()) > 7999
    agent.post("/mcp-relay/send", {"header": header})
    [payload] = [json.loads(p) for p in _notified(heard)]
    assert payload["text"] == text


def test_a_withdrawn_request_is_gone(agent: Agent) -> None:
    row = str(uuid.uuid4())
    header = {"t": "req", "req": str(uuid.uuid4()), "to": REPLICA, "row": row}
    agent.post("/mcp-relay/send", {"header": header, "body": {"x": 1}})
    assert agent.post("/mcp-relay/rows/withdraw", {"row": row}) == {"done": True}
    assert agent.post("/mcp-relay/rows/take", {"row": row}) == {"body": None}
    # The same row again is refused, never a second row under one id.
    agent.post("/mcp-relay/send", {"header": header, "body": {"x": 1}})
    assert agent.status("/mcp-relay/send", {"header": header, "body": {"x": 2}}) == 409


@pytest.mark.parametrize(
    "send",
    [
        # A malformed id, a kind the relay never sends, an unknown field.
        {"header": {"t": "ack", "req": "../x", "to": REPLICA}},
        {"header": {"t": "nope", "req": str(uuid.uuid4()), "to": REPLICA}},
        {"header": {"t": "ack", "req": str(uuid.uuid4()), "to": REPLICA, "extra": 1}},
        # A body with no row to put it in, a row with no body.
        {"header": {"t": "ack", "req": str(uuid.uuid4()), "to": REPLICA}, "body": {"x": 1}},
        {"header": {"t": "req", "req": str(uuid.uuid4()), "to": REPLICA, "row": str(uuid.uuid4())}},
        # Past what a NOTIFY carries.
        {"header": {"t": "chunk", "req": str(uuid.uuid4()), "to": REPLICA, "text": "x" * 8000}},
    ],
)
def test_a_malformed_send_notifies_nothing(
    agent: Agent, tables: PgPool, heard: psycopg.Connection[Any], send: dict[str, Any]
) -> None:
    assert agent.status("/mcp-relay/send", send) == 422
    assert _notified(heard, timeout=0.2) == []
    assert _sql(tables, "SELECT id FROM ai_mcp_relay_messages") == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/mcp-relay/sessions/owner", {"hash": "B" * 64}),
        ("/mcp-relay/sessions/owner", {"hash": "../" + "b" * 61}),
        ("/mcp-relay/sessions", {"hash": HASH, "replica": "x", "caller_key": "k"}),
        ("/mcp-relay/sessions", {"hash": HASH, "replica": REPLICA, "caller_key": "k" * 1025}),
        ("/mcp-relay/counts", {"caller_key": "k", "idle_seconds": 0}),
        ("/mcp-relay/sweep", {"idle_seconds": 10**9}),
        ("/mcp-relay/rows/take", {"row": "1"}),
    ],
)
def test_a_malformed_id_or_bound_is_refused(agent: Agent, path: str, body: dict[str, Any]) -> None:
    assert agent.status(path, body) == 422
