"""The agent's NOTIFYs and its last reads of a session's events through CRUD routes (#6501,
`api/crud_agent_publish.py`): only the ``agent`` credential reaches them, a publish NOTIFYs
on the channel the agent's own SQL used with a payload checked field by field, and the reads
answer what the agent's statements read (``activityProjection.ts`` ``openCalls``,
``queue.ts`` ``childReasoning``)."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import crud_agent_publish, crud_areas
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
    app.include_router(crud_agent_publish.router, prefix="/api/v1")
    app.include_router(crud_areas.router, prefix="/api/v1")
    install_problem_handlers(app)
    app.add_middleware(AuthGate, sessions=lambda: _NoSessions(), credentials=lambda: credentials)
    return app


def _token(app: FastAPI, caller: ServiceCaller = "agent") -> dict[str, str]:
    credentials = app.state.scadbuddy.components.get(AUTH).credentials
    minted = InProcessProvider(caller, lambda: credentials).token()
    assert minted is not None
    return {"authorization": f"Bearer {minted}"}


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


@pytest.fixture
def client(tables: PgPool) -> Iterator[tuple[TestClient, dict[str, str]]]:
    app = _app(tables)
    with TestClient(app) as c:
        yield c, _token(app)


def _session(pool: PgPool) -> str:
    sid = str(uuid.uuid4())
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
            " creator_id, status, max_turns, budget_usd) VALUES (%s, 'chat', 'browser',"
            " 'browser', 'You', 'browser', 'browser', 'idle', 10, 1)",
            (sid,),
        )
    return sid


def _log(pool: PgPool, sid: str, events: list[dict[str, Any]]) -> None:
    with pool.connection() as conn:
        for seq, event in enumerate(events, start=1):
            conn.execute(
                "INSERT INTO ai_session_events (session_id, seq, event) VALUES (%s, %s, %s)",
                (sid, seq, json.dumps(event)),
            )


def _event(sid: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "at": "2026-11-16T00:00:00.000Z",
        "kind": "session.message",
        "session_id": sid,
        "seq": 3,
        "replica": "agent-0",
        **extra,
    }


def test_only_the_agent_credential_reaches_the_routes(tables: PgPool) -> None:
    app = _app(tables)
    sid = str(uuid.uuid4())
    with TestClient(app) as c:
        routes = [
            ("POST", "/publish/session-event", _event(sid)),
            ("POST", "/publish/warm-evict", {"target": "*"}),
            ("GET", f"/sessions/{sid}/open-calls", None),
            ("GET", f"/sessions/{sid}/reasoning?tool_use_id=t", None),
        ]
        for method, path, body in routes:
            assert c.request(method, f"{BASE}{path}", json=body).status_code == 401
            for caller in ("render", "print", "projects"):
                wrong = c.request(method, f"{BASE}{path}", json=body, headers=_token(app, caller))
                assert wrong.status_code == 403
        listed = c.get(f"{BASE}/areas", headers=_token(app)).json()["areas"]
        assert {"publish", "session-reads"} <= set(listed)


def test_a_session_event_is_notified_as_the_agent_sent_it(
    client: tuple[TestClient, dict[str, str]], pg_conninfo: str
) -> None:
    c, headers = client
    sid = str(uuid.uuid4())
    plain, waiting = _event(sid), _event(sid, kind="session.waiting", status="waiting_input")
    with psycopg.connect(pg_conninfo, autocommit=True) as listener:
        listener.execute("LISTEN scadbuddy_events")
        for body in (plain, waiting):
            answer = c.post(f"{BASE}/publish/session-event", json=body, headers=headers)
            assert answer.json() == {"sent": True}
        heard = [json.loads(n.payload) for n in listener.notifies(timeout=1.0, stop_after=2)]
    assert heard == [plain, waiting]


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "session.other"},
        {"session_id": "../areas"},
        {"session_id": "not-a-uuid"},
        {"seq": -1},
        {"status": "Not A Status"},
        {"id": "x" * 32},
        {"replica": "r" * 201},
        {"extra": 1},
    ],
)
def test_a_malformed_session_event_is_refused(
    client: tuple[TestClient, dict[str, str]], change: dict[str, Any]
) -> None:
    c, headers = client
    body = {**_event(str(uuid.uuid4())), **change}
    assert c.post(f"{BASE}/publish/session-event", json=body, headers=headers).status_code == 422


def test_a_publish_names_its_length_and_stays_small(
    client: tuple[TestClient, dict[str, str]],
) -> None:
    c, headers = client
    big = json.dumps({"target": "*", "pad": "x" * (40 * 1024 * 1024)})
    refused = c.post(
        f"{BASE}/publish/warm-evict",
        content=big,
        headers={**headers, "content-type": "application/json"},
    )
    assert refused.status_code == 413


def test_a_warm_eviction_is_notified_for_a_session_or_every_one(
    client: tuple[TestClient, dict[str, str]], pg_conninfo: str
) -> None:
    c, headers = client
    sid = str(uuid.uuid4())
    with psycopg.connect(pg_conninfo, autocommit=True) as listener:
        listener.execute("LISTEN scadbuddy_warm_evict")
        for target in (sid, "*"):
            answer = c.post(f"{BASE}/publish/warm-evict", json={"target": target}, headers=headers)
            assert answer.json() == {"sent": True}
        for target in ("../x", "", "**", sid + "x"):
            refused = c.post(f"{BASE}/publish/warm-evict", json={"target": target}, headers=headers)
            assert refused.status_code == 422
        heard = [n.payload for n in listener.notifies(timeout=1.0, stop_after=2)]
    assert heard == [sid, "*"]


def test_a_path_id_is_a_uuid(client: tuple[TestClient, dict[str, str]]) -> None:
    c, headers = client
    for sid in ("not-a-uuid", "..%2Fareas", str(uuid.uuid4()).upper()):
        for tail in ("open-calls", "reasoning?tool_use_id=t"):
            got = c.get(f"{BASE}/sessions/{sid}/{tail}", headers=headers)
            assert got.status_code in (404, 422)


def test_the_open_calls_are_those_with_no_result(
    client: tuple[TestClient, dict[str, str]], tables: PgPool
) -> None:
    c, headers = client
    sid = _session(tables)
    _log(
        tables,
        sid,
        [
            {"type": "user.turn", "turnId": "t1"},
            {"type": "tool.call", "id": "a", "name": "read"},
            {"type": "tool.call", "id": "b", "name": "write"},
            {"type": "tool.result", "id": "a"},
            {"type": "user.turn", "turnId": "t2"},
            {"type": "tool.call", "id": "c"},
        ],
    )

    def calls(**params: str) -> Any:
        got = c.get(f"{BASE}/sessions/{sid}/open-calls", params=params, headers=headers)
        assert got.status_code == 200, got.text
        return got.json()["calls"]

    assert calls() == [{"id": "b", "name": "write"}, {"id": "c", "name": ""}]
    assert calls(turn_id="t2") == [{"id": "c", "name": ""}]
    # A turn not logged reads from the start, as the agent's SQL did.
    assert calls(turn_id="t9") == calls()
    other = str(uuid.uuid4())
    assert c.get(f"{BASE}/sessions/{other}/open-calls", headers=headers).json() == {"calls": []}


def test_the_reasoning_is_the_deltas_since_the_turn_before_the_call(
    client: tuple[TestClient, dict[str, str]], tables: PgPool
) -> None:
    c, headers = client
    sid = _session(tables)
    _log(
        tables,
        sid,
        [
            {"type": "assistant.text.delta", "messageId": "m0", "delta": "old"},
            {"type": "user.turn", "turnId": "t1"},
            {"type": "assistant.text.delta", "messageId": "m1", "delta": "a"},
            {"type": "assistant.text.delta", "messageId": "m1", "delta": "b"},
            {"type": "tool.call", "id": "toolu_1", "name": "x"},
            {"type": "assistant.text.delta", "messageId": "m2", "delta": "after"},
        ],
    )

    def read(**params: Any) -> Any:
        got = c.get(f"{BASE}/sessions/{sid}/reasoning", params=params, headers=headers)
        assert got.status_code == 200, got.text
        return got.json()

    assert read(tool_use_id="toolu_1") == {
        "logged": True,
        "deltas": [{"message": "m1", "delta": "a"}, {"message": "m1", "delta": "b"}],
    }
    assert read(tool_use_id="toolu_9") == {"logged": False, "deltas": []}
    # Past the scanned window the call is not logged.
    assert read(tool_use_id="toolu_1", scan=1) == {"logged": False, "deltas": []}
    too_long = c.get(
        f"{BASE}/sessions/{sid}/reasoning",
        params={"tool_use_id": "t" * 501},
        headers=headers,
    )
    assert too_long.status_code == 422
