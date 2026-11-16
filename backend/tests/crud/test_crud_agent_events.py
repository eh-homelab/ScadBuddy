"""A session's event log through CRUD routes (#5299 step 9, part b, `api/crud_agent_events.py`):
only the ``agent`` credential reaches them, each route is the agent's SQL as one transaction
(`agent_data/events.py`), seqs come without gaps from the session's row, a turn's events are
written only while the row names its placement, a fork reads its parent's conversation through
its chain in one snapshot, never past an ancestor's cut, and a new fork's log is written under its
parent's lock.

The agent's tables are made from its own migration files, as the API applies them at its
start (#5299 D6)."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import crud_agent_events, crud_areas
from scadbuddy.auth.check import auth_check
from scadbuddy.auth.component import AUTH
from scadbuddy.auth.gate import AuthGate
from scadbuddy.auth.service_credentials import (
    InProcessProvider,
    ServiceCaller,
    ServiceCredentialStore,
)
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.problems import install_problem_handlers
from scadbuddy.render.agent_migrations import migrate_agent
from scadbuddy.workflows.payload_codec import payload_key_lock
from tests.conftest import PgPool
from tests.crud.crud_support import _NoSessions, _Noted

pytestmark = pytest.mark.requires_postgres

BASE = "/api/v1/internal/crud/agent"


def _app(pool: PgPool) -> FastAPI:
    credentials = ServiceCredentialStore(pool)
    parts: dict[Any, Any] = {
        AUTH: SimpleNamespace(
            would_refuse=_Noted(),
            verifiers={},
            credentials=credentials,
            core=SimpleNamespace(metrics=Metrics()),
        ),
    }
    app = FastAPI(dependencies=[Depends(auth_check)])
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(auth_mode="enforce"),
        components=SimpleNamespace(get=lambda key: parts[key]),
        settings_store=SimpleNamespace(pool=pool),
    )
    app.include_router(crud_agent_events.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: _NoSessions(), credentials=lambda: credentials)
    return app


def _token(app: FastAPI, caller: ServiceCaller = "agent") -> dict[str, str]:
    credentials = app.state.scadbuddy.components.get(AUTH).credentials
    minted = InProcessProvider(caller, lambda: credentials).token()
    assert minted is not None
    return {"authorization": f"Bearer {minted}"}


class Agent:
    def __init__(self, client: TestClient, headers: dict[str, str]) -> None:
        self.client = client
        self.headers = headers

    def get(self, path: str, **params: Any) -> Any:
        response = self.client.get(f"{BASE}{path}", params=params, headers=self.headers)
        assert response.status_code == 200, response.text
        return response.json()

    def post(self, path: str, body: Any) -> Any:
        response = self.client.post(f"{BASE}{path}", json=body, headers=self.headers)
        assert response.status_code == 200, response.text
        return response.json()

    def status(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        return self.client.request(
            method, f"{BASE}{path}", json=body, params=params, headers=self.headers
        )

    def append(self, sid: str, events: list[dict[str, Any]], **extra: Any) -> Any:
        body = {"events": [json.dumps(e) for e in events], **extra}
        return self.post(f"/sessions/{sid}/events", body)["seqs"]

    def read(self, sid: str, after: int = 0) -> list[dict[str, Any]]:
        rows = self.get(f"/sessions/{sid}/events", after=after)["events"]
        return [{**r, "event": json.loads(r["event"])} for r in rows]


def _sql(pool: PgPool, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(query.encode(), params)
        return list(cur.fetchall()) if cur.description else []


def _session(pool: PgPool, *, parent: str | None = None, **cols: Any) -> str:
    sid = str(uuid.uuid4())
    _sql(
        pool,
        "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
        " creator_id, status, max_turns, budget_usd, fork_parent_id, fork_cut, turn_attempt,"
        " durable_turn_id) VALUES (%s, 'chat', 'browser', 'browser', 'You', 'browser',"
        " 'browser', 'idle', 10, 1, %s, %s, %s, %s)",
        (sid, parent, None if parent is None else 0, cols.get("attempt"), cols.get("turn")),
    )
    return sid


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


@pytest.fixture
def agent(tables: PgPool) -> Iterator[Agent]:
    app = _app(tables)
    with TestClient(app) as client:
        yield Agent(client, _token(app))


def _turn(sid: str, turn: str, text: str) -> dict[str, Any]:
    return {"type": "user.turn", "sessionId": sid, "turnId": turn, "text": text}


def _delta(sid: str, text: str) -> dict[str, Any]:
    return {"type": "assistant.text.delta", "sessionId": sid, "text": text}


def test_only_the_agent_credential_reaches_the_routes(tables: PgPool) -> None:
    app = _app(tables)
    sid = _session(tables)
    with TestClient(app) as client:
        assert client.get(f"{BASE}/sessions/{sid}/events").status_code == 401
        for caller in ("render", "print", "projects"):
            wrong = client.get(f"{BASE}/sessions/{sid}/events", headers=_token(app, caller))
            assert wrong.status_code == 403
        listed = client.get(f"{BASE}/areas", headers=_token(app)).json()["areas"]
        assert "session-events" in listed


def test_an_append_takes_the_next_seqs_and_reads_back_in_order(
    agent: Agent, tables: PgPool
) -> None:
    sid = _session(tables)
    first = [{"type": "session.started", "sessionId": sid, "title": "é 🙂"}]
    assert agent.append(sid, first) == [1]
    assert agent.append(sid, [_delta(sid, "a"), _delta(sid, "b")]) == [2, 3]
    assert [r["seq"] for r in agent.read(sid)] == [1, 2, 3]
    assert agent.read(sid)[0]["event"] == first[0]
    assert [r["seq"] for r in agent.read(sid, after=2)] == [3]
    assert agent.get(f"/sessions/{sid}/events/last-seq") == {"seq": 3}
    page = agent.get(f"/sessions/{sid}/events", after=0, limit=2)["events"]
    assert [r["seq"] for r in page] == [1, 2]
    assert all(r["inherited"] is False for r in page)


def test_a_session_with_no_row_is_gone(agent: Agent) -> None:
    missing = str(uuid.uuid4())
    body = {"events": [json.dumps({"type": "x"})]}
    refused = agent.status("POST", f"/sessions/{missing}/events", body)
    assert refused.status_code == 404 and refused.json()["reason"] == "session_gone"
    assert agent.get(f"/sessions/{missing}/events") == {"events": []}
    assert agent.get(f"/sessions/{missing}/events/last-seq") == {"seq": 0}


def test_the_fence_holds_a_turns_events_to_its_placement(agent: Agent, tables: PgPool) -> None:
    turn = str(uuid.uuid4())
    sid = _session(tables, attempt="run:1", turn=turn)
    fence = {"attempt": "run:1", "turn_id": turn}
    assert agent.append(sid, [_delta(sid, "a")], fence=fence) == [1]
    stale = {"attempt": "run:0", "turn_id": turn}
    assert agent.append(sid, [_delta(sid, "b")], fence=stale) is None
    # Unlike the transcript's, no turn running is no placement holding it.
    _sql(tables, "UPDATE ai_sessions SET durable_turn_id = NULL WHERE id = %s", (sid,))
    assert agent.append(sid, [_delta(sid, "c")], fence=fence) is None
    assert [r["seq"] for r in agent.read(sid)] == [1]


def test_the_fence_waits_for_a_writer_of_the_row(agent: Agent, tables: PgPool) -> None:
    turn = str(uuid.uuid4())
    sid = _session(tables, attempt="run:1", turn=turn)
    held = threading.Event()
    release = threading.Event()

    def take_over() -> None:
        with tables.connection() as conn, conn.transaction():
            conn.execute("SELECT 1 FROM ai_sessions WHERE id = %s FOR UPDATE", (sid,))
            conn.execute("UPDATE ai_sessions SET turn_attempt = 'run:2' WHERE id = %s", (sid,))
            held.set()
            release.wait(10)

    thread = threading.Thread(target=take_over)
    thread.start()
    held.wait(10)
    answers: list[Any] = []
    fence = {"attempt": "run:1", "turn_id": turn}
    appending = threading.Thread(
        target=lambda: answers.append(agent.append(sid, [_delta(sid, "a")], fence=fence))
    )
    appending.start()
    appending.join(0.5)
    assert answers == []
    release.set()
    thread.join()
    appending.join(10)
    assert answers == [None]


def test_a_fork_reads_its_parents_conversation_through_the_cut(
    agent: Agent, tables: PgPool
) -> None:
    parent = _session(tables)
    turn = str(uuid.uuid4())
    agent.append(parent, [{"type": "session.started", "sessionId": parent}])  # 1
    agent.append(parent, [_turn(parent, turn, "make a box"), _delta(parent, "ok")])  # 2, 3
    agent.append(parent, [{"type": "session.status", "sessionId": parent, "status": "idle"}])  # 4
    child = _session(tables, parent=parent)
    started = {"type": "session.started", "sessionId": child}
    idle = {"type": "session.status", "sessionId": child, "status": "idle"}
    seqs = agent.post(
        f"/sessions/{child}/fork-log",
        {
            "parent_id": parent,
            "cut": 4,
            "started": [json.dumps(started)],
            "then": [json.dumps(idle)],
        },
    )["seqs"]
    assert seqs == [1, 5]
    agent.append(parent, [_delta(parent, "late")])  # past the cut: never the child's
    rows = agent.read(child)
    assert [(r["seq"], r["event"]["type"], r["inherited"]) for r in rows] == [
        (1, "session.started", False),
        (2, "user.turn", True),
        (3, "assistant.text.delta", True),
        (5, "session.status", False),
    ]
    # Logged as the parent's: the agent addresses an inherited event to the child.
    assert rows[1]["event"]["sessionId"] == parent
    grand = _session(tables, parent=child)
    agent.post(
        f"/sessions/{grand}/fork-log",
        {"parent_id": child, "cut": 2, "started": [json.dumps(started)], "then": []},
    )
    assert [(r["seq"], r["inherited"]) for r in agent.read(grand)] == [(1, False), (2, True)]
    assert agent.get(f"/sessions/{grand}/events/last-seq") == {"seq": 2}


def test_a_fork_log_after_its_parent_was_forgotten_is_refused(agent: Agent, tables: PgPool) -> None:
    parent = _session(tables)
    child = _session(tables, parent=parent)
    _sql(
        tables,
        "UPDATE ai_sessions SET fork_parent_id = NULL, fork_cut = NULL WHERE id = %s",
        (child,),
    )
    body = {"parent_id": parent, "cut": 0, "started": [json.dumps({"type": "x"})], "then": []}
    refused = agent.status("POST", f"/sessions/{child}/fork-log", body)
    assert refused.status_code == 404 and refused.json()["reason"] == "parent_gone"
    assert agent.read(child) == []


def test_a_fork_log_waits_for_a_forget_of_the_parent(agent: Agent, tables: PgPool) -> None:
    parent = _session(tables)
    child = _session(tables, parent=parent)
    held = threading.Event()
    release = threading.Event()

    def forget() -> None:
        with tables.connection() as conn, conn.transaction():
            lock = payload_key_lock(f"session-{parent}")
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock,))
            conn.execute(
                "UPDATE ai_sessions SET fork_parent_id = NULL, fork_cut = NULL WHERE id = %s",
                (child,),
            )
            held.set()
            release.wait(10)

    thread = threading.Thread(target=forget)
    thread.start()
    held.wait(10)
    answers: list[int] = []
    body = {"parent_id": parent, "cut": 0, "started": [json.dumps({"type": "x"})], "then": []}
    logging = threading.Thread(
        target=lambda: answers.append(
            agent.status("POST", f"/sessions/{child}/fork-log", body).status_code
        )
    )
    logging.start()
    logging.join(0.5)
    assert answers == []
    release.set()
    thread.join()
    logging.join(10)
    assert answers == [404]


def test_a_turns_words_and_its_events(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    turn = str(uuid.uuid4())
    path = f"/sessions/{sid}/events"
    assert agent.get(f"{path}/user-turn", turn_id=turn) == {"text": None}
    assert agent.get(f"{path}/turn", turn_id=turn) == {"here": True, "after": None, "events": []}
    agent.append(sid, [_delta(sid, "before")])
    agent.append(sid, [_turn(sid, turn, "first")])
    agent.append(sid, [_turn(sid, turn, "again"), _delta(sid, "a")])
    assert agent.get(f"{path}/user-turn", turn_id=turn) == {"text": "again"}
    # From the last start logged with that id on, then past what was read.
    first = agent.get(f"{path}/turn", turn_id=turn)
    assert first["here"] is True and first["after"] == 4
    assert [json.loads(t)["text"] for t in first["events"]] == ["a"]
    agent.append(sid, [_delta(sid, "b")])
    more = agent.get(f"{path}/turn", turn_id=turn, after=4)
    assert more["after"] == 5 and [json.loads(t)["text"] for t in more["events"]] == ["b"]
    assert agent.get(f"{path}/turn", turn_id=turn, after=5) == {
        "here": True,
        "after": 5,
        "events": [],
    }
    gone = str(uuid.uuid4())
    answer = agent.get(f"/sessions/{gone}/events/turn", turn_id=turn)
    assert answer == {"here": False, "after": None, "events": []}


def test_the_bodies_and_ids_are_bounded(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    path = f"/sessions/{sid}/events"
    bodies: list[dict[str, Any]] = [
        {"events": []},
        {"events": ['{"type":"x"}'] * (crud_agent_events.EVENTS_MAX + 1)},
        {"events": ["not json"]},
        {"events": ["[1, 2]"]},
        {"events": ['{"no":"type"}']},
        {"events": ['{"type":"x"}'], "fence": {"attempt": "", "turn_id": "t"}},
        {"events": ['{"type":"x"}'], "extra": 1},
    ]
    for bad in bodies:
        assert agent.status("POST", path, bad).status_code == 422, bad
    huge = agent.client.post(
        f"{BASE}{path}",
        content=b'{"x":"' + b"y" * crud_agent_events.BODY_BYTES_MAX + b'"}',
        headers={**agent.headers, "content-type": "application/json"},
    )
    assert huge.status_code == 413
    limit = crud_agent_events.READ_MAX + 1
    assert agent.status("GET", path, limit=limit).status_code == 422
    assert agent.status("GET", path, after=-1).status_code == 422
    bad_fork = {"parent_id": "../x", "cut": 0, "started": ['{"type":"x"}'], "then": []}
    assert agent.status("POST", f"/sessions/{sid}/fork-log", bad_fork).status_code == 422
    for bad_id in ("not-a-uuid", "..", "%2E%2E", sid.upper()):
        assert agent.status("GET", f"/sessions/{bad_id}/events").status_code in (404, 422)
    assert _sql(tables, "SELECT 1 FROM ai_session_events WHERE session_id = %s", (sid,)) == []
