"""A durable turn's writes of its session's row through CRUD routes (#6449, #5299 step 9,
`api/crud_agent_turns.py`): only the ``agent`` credential reaches them, each route is the
agent's SQL as one transaction (`agent_data/turns.py`), with its events, and each fence
refuses a stale caller: a placement's claim once the row runs another turn or none, a turn's end
once it was ended or replaced, a lost turn's end once the row moved on. The agent's tables are
made from its own migration files, as the API applies them at its start (#5299 D6)."""

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

from scadbuddy.agent_data import turns
from scadbuddy.api import crud_agent_turns, crud_areas
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
BROWSER = {"kind": "browser", "id": "browser"}


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
    app.include_router(crud_agent_turns.router, prefix="/api/v1")
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

    def post(self, path: str, body: Any = None) -> Any:
        response = self.client.post(f"{BASE}{path}", json=body or {}, headers=self.headers)
        assert response.status_code == 200, response.text
        return response.json()

    def status(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        return self.client.request(
            method, f"{BASE}{path}", json=body, params=params, headers=self.headers
        )


def _sql(pool: PgPool, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(query.encode(), params)
        return list(cur.fetchall()) if cur.description else []


def _session(pool: PgPool, *, status: str = "idle", turn: str | None = None, **cols: Any) -> str:
    sid = str(uuid.uuid4())
    _sql(
        pool,
        "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
        " creator_id, status, max_turns, budget_usd, cost_usd, durable_turn_id, turn_attempt,"
        " runtime) VALUES (%s, 'chat', %s, %s, 'You', 'browser', 'browser', %s, 10, %s, %s, %s,"
        " %s, %s)",
        (
            sid,
            cols.get("owner_kind", "browser"),
            cols.get("owner_id", "browser"),
            status,
            cols.get("budget", 1.0),
            cols.get("cost", 0.0),
            turn,
            cols.get("attempt"),
            cols.get("runtime", "unified"),
        ),
    )
    return sid


def _row(pool: PgPool, sid: str) -> dict[str, Any]:
    return _sql(pool, "SELECT * FROM ai_sessions WHERE id = %s", (sid,))[0]


def _events(pool: PgPool, sid: str) -> list[dict[str, Any]]:
    rows = _sql(
        pool, "SELECT event FROM ai_session_events WHERE session_id = %s ORDER BY seq", (sid,)
    )
    return [json.loads(r["event"]) for r in rows]


def _status(sid: str, status: str) -> str:
    return json.dumps({"v": 1, "type": "session.status", "sessionId": sid, "status": status})


def _tail(sid: str, status: str = "idle") -> list[str]:
    result = {"v": 1, "type": "session.result", "sessionId": sid, "costUsd": 0, "turns": 0}
    return [json.dumps({**result, "budgetUsd": 0}), _status(sid, status)]


def _next(sid: str, turn: str, **extra: Any) -> dict[str, Any]:
    user_turn = {"v": 1, "type": "user.turn", "sessionId": sid, "turnId": turn, "text": "hi"}
    return {
        "turn_id": turn,
        "author": extra.pop("author", BROWSER),
        "bare": False,
        "started": [json.dumps(user_turn), _status(sid, "running")],
        **extra,
    }


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


def test_only_the_agent_credential_reaches_the_routes(tables: PgPool) -> None:
    app = _app(tables)
    sid = _session(tables)
    with TestClient(app) as client:
        assert client.get(f"{BASE}/sessions/{sid}/turn").status_code == 401
        for caller in ("render", "print", "projects", "intake", "storefront"):
            wrong = client.get(f"{BASE}/sessions/{sid}/turn", headers=_token(app, caller))
            assert wrong.status_code == 403
            claim = {"author": BROWSER, "turn_id": str(uuid.uuid4()), "bare": False}
            refused = client.post(
                f"{BASE}/sessions/{sid}/turn/send-claim", json=claim, headers=_token(app, caller)
            )
            assert refused.status_code == 403
        assert _row(tables, sid)["status"] == "idle"
        listed = client.get(f"{BASE}/areas", headers=_token(app)).json()["areas"]
        assert "session-turns" in listed


def test_a_send_claims_a_free_row_and_gives_it_back(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables, status="failed", runtime=None)
    turn = str(uuid.uuid4())
    body = {"author": BROWSER, "turn_id": turn, "tiers": ["read"], "bare": True}
    claimed = agent.post(f"/sessions/{sid}/turn/send-claim", body)["claimed"]
    assert claimed == {"prior_status": "failed", "runtime": "unified"}
    row = _row(tables, sid)
    assert (row["status"], str(row["durable_turn_id"]), row["turn_flow"]) == (
        "running",
        turn,
        True,
    )
    assert row["turn_tiers"] == {"by": BROWSER, "tiers": ["read"]}
    # A second claim finds the row running.
    again = {**body, "turn_id": str(uuid.uuid4())}
    assert agent.post(f"/sessions/{sid}/turn/send-claim", again)["claimed"] is None
    logged = agent.post(f"/sessions/{sid}/turn/give-back", {"prior": "failed"})["logged"]
    assert logged["seqs"] == [1]
    assert json.loads(logged["events"][0])["status"] == "failed"
    assert _events(tables, sid) == [json.loads(_status(sid, "failed"))]
    assert _row(tables, sid)["durable_turn_id"] is None
    # Given back once: the row no longer runs.
    assert agent.post(f"/sessions/{sid}/turn/give-back", {"prior": "idle"})["logged"] is None


@pytest.mark.parametrize(
    ("cols", "author"),
    [
        ({"status": "done"}, BROWSER),
        ({"status": "running"}, BROWSER),
        ({"cost": 1.0}, BROWSER),
        ({"owner_kind": "bearer", "owner_id": "a"}, {"kind": "bearer", "id": "b"}),
    ],
)
def test_a_send_claim_refuses_a_row_it_cannot_take(
    agent: Agent, tables: PgPool, cols: dict[str, Any], author: dict[str, str]
) -> None:
    sid = _session(tables, **cols)
    body = {"author": author, "turn_id": str(uuid.uuid4()), "bare": False}
    assert agent.post(f"/sessions/{sid}/turn/send-claim", body)["claimed"] is None
    archived = _session(tables)
    _sql(tables, "UPDATE ai_sessions SET archived_at = now() WHERE id = %s", (archived,))
    assert agent.post(f"/sessions/{archived}/turn/send-claim", body)["claimed"] is None


def test_a_send_may_queue_behind_a_running_turn(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables, status="running", turn=str(uuid.uuid4()))
    _sql(tables, "UPDATE ai_sessions SET event_seq = 7 WHERE id = %s", (sid,))
    assert agent.post(f"/sessions/{sid}/turn/queueable", {"author": BROWSER}) == {"seq": 7}
    idle = _session(tables)
    assert agent.post(f"/sessions/{idle}/turn/queueable", {"author": BROWSER}) == {"seq": None}


def test_the_turns_state_and_a_stops_flag(agent: Agent, tables: PgPool) -> None:
    turn = str(uuid.uuid4())
    sid = _session(tables, status="running", turn=turn, attempt="run:1")
    state = agent.get(f"/sessions/{sid}/turn", grace_ms=60_000)["turn"]
    assert state["durable_turn_id"] == turn and state["turn_attempt"] == "run:1"
    assert (state["interrupt_requested"], state["settled"], state["event_seq"]) == (
        False,
        False,
        "0",
    )
    assert agent.post(f"/sessions/{sid}/turn/interrupt") == {"changed": True}
    assert agent.get(f"/sessions/{sid}/turn")["turn"]["interrupt_requested"] is True
    idle = _session(tables)
    assert agent.post(f"/sessions/{idle}/turn/interrupt") == {"changed": False}
    assert agent.get(f"/sessions/{uuid.uuid4()}/turn") == {"turn": None}


def test_a_placement_takes_the_turn_only_while_the_row_runs_it(
    agent: Agent, tables: PgPool
) -> None:
    turn = str(uuid.uuid4())
    sid = _session(tables, status="running", turn=turn)
    path = f"/sessions/{sid}/turn/placement"
    first = {"turn_id": turn, "attempt": "run:1", "mark_key": turn}
    assert agent.post(path, first) == {"mark": 0}
    _sql(
        tables,
        "INSERT INTO ai_session_entries (project_key, session_id, subpath, entry)"
        " VALUES ('', %s, '', '{}')",
        (sid,),
    )
    # A later placement of the same turn takes it over and keeps the mark.
    moved = {**first, "attempt": "run:2"}
    assert agent.post(path, moved) == {"mark": 0}
    assert _row(tables, sid)["turn_attempt"] == "run:2"
    # A continuation's own key marks again.
    assert agent.post(path, {**moved, "mark_key": f"{turn}/r1"})["mark"] > 0
    # Replaced: the row runs another turn; ended: it runs none. Neither writes turn_attempt.
    other = str(uuid.uuid4())
    _sql(tables, "UPDATE ai_sessions SET durable_turn_id = %s WHERE id = %s", (other, sid))
    assert agent.post(path, {**first, "attempt": "run:3"}) == {"mark": None}
    _sql(
        tables,
        "UPDATE ai_sessions SET status = 'idle', durable_turn_id = NULL WHERE id = %s",
        (sid,),
    )
    assert agent.post(path, {**first, "attempt": "run:4"}) == {"mark": None}
    assert _row(tables, sid)["turn_attempt"] == "run:2"


def test_a_placements_spend_is_added_whoever_holds_the_row(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables, cost=0.5)
    body = {"key": "run:1:0", "cost_usd": 0.25, "unpriced_usd": 0.1, "turns": 2}
    assert agent.post(f"/sessions/{sid}/turn/spend", body) == {"written": True}
    row = _row(tables, sid)
    assert (row["cost_usd"], row["unpriced_cost_usd"], row["turns"]) == (0.75, 0.1, 2)
    assert agent.post(f"/sessions/{uuid.uuid4()}/turn/spend", body) == {"written": False}


def test_a_spend_sent_again_under_its_key_is_added_once(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    body = {"key": "run:1:0", "cost_usd": 0.25, "unpriced_usd": 0, "turns": 1}
    # The answer to the first was lost: the placement sends the same write again.
    assert agent.post(f"/sessions/{sid}/turn/spend", body) == {"written": True}
    assert agent.post(f"/sessions/{sid}/turn/spend", body) == {"written": True}
    assert (_row(tables, sid)["cost_usd"], _row(tables, sid)["turns"]) == (0.25, 1)
    assert agent.post(f"/sessions/{sid}/turn/spend", {**body, "key": "run:1:1"})
    assert (_row(tables, sid)["cost_usd"], _row(tables, sid)["turns"]) == (0.5, 2)
    # A key older than the hour is let go at the session's next spend.
    _sql(
        tables,
        "UPDATE ai_turn_spend_keys SET created_at = now() - interval '2 hours'"
        " WHERE session_id = %s AND key = 'run:1:0'",
        (sid,),
    )
    agent.post(f"/sessions/{sid}/turn/spend", {**body, "key": "run:2:0"})
    keys = _sql(tables, "SELECT key FROM ai_turn_spend_keys WHERE session_id = %s", (sid,))
    assert sorted(k["key"] for k in keys) == ["run:1:1", "run:2:0"]


def test_a_write_that_waits_past_the_lock_timeout_is_refused(
    agent: Agent, tables: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(turns, "LOCK_TIMEOUT_MS", 200)
    turn = str(uuid.uuid4())
    sid = _session(tables, status="running", turn=turn)
    with tables.connection() as conn, conn.transaction():
        conn.execute("SELECT 1 FROM ai_sessions WHERE id = %s FOR UPDATE", (sid,))
        refused = agent.status(
            "POST", f"/sessions/{sid}/turn/finish", {"turn_id": turn, "tail": _tail(sid)}
        )
    assert refused.status_code == 503
    assert refused.json()["reason"] == "row_busy"
    assert _row(tables, sid)["status"] == "running"
    assert _events(tables, sid) == []


def test_a_turn_id_is_read_lowercase(agent: Agent, tables: PgPool) -> None:
    turn = str(uuid.uuid4())
    sid = _session(tables, status="running", turn=turn)
    placed = {"turn_id": turn.upper(), "attempt": "run:1", "mark_key": turn}
    assert agent.post(f"/sessions/{sid}/turn/placement", placed) == {"mark": 0}
    ended = agent.post(
        f"/sessions/{sid}/turn/finish", {"turn_id": turn.upper(), "tail": _tail(sid)}
    )
    assert ended["ended"] is not None


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0.0, "0"),
        (-0.0, "0"),
        (5.0, "5"),
        (0.1, "0.1"),
        (0.30000000000000004, "0.30000000000000004"),
        (1e-7, "1e-7"),
        (1.5e-7, "1.5e-7"),
        (1.234e-8, "1.234e-8"),
        (0.000001, "0.000001"),
        (1e20, "100000000000000000000"),
        (123456789012345680000.0, "123456789012345680000"),
        (1e21, "1e+21"),
        (1.5e300, "1.5e+300"),
        (-2.5e-10, "-2.5e-10"),
        (float(2**53 + 2), "9007199254740994"),
        (float("nan"), "null"),
        (float("inf"), "null"),
    ],
)
def test_numbers_are_written_as_javascript_writes_them(value: float, text: str) -> None:
    assert turns.js_number(value) == text
    assert turns.dumps({"type": "x", "n": value, "l": [value]}) == (
        f'{{"type":"x","n":{text},"l":[{text}]}}'
    )


def test_a_turns_end_is_written_once_and_never_over_another_turn(
    agent: Agent, tables: PgPool
) -> None:
    turn = str(uuid.uuid4())
    sid = _session(tables, status="running", turn=turn, cost=0.125, budget=2.0)
    _sql(tables, "UPDATE ai_sessions SET turns = 3 WHERE id = %s", (sid,))
    path = f"/sessions/{sid}/turn/finish"
    budget = {"v": 1, "type": "error", "sessionId": sid, "code": "error_max_budget_usd"}
    tail = [_tail(sid)[0], json.dumps({**budget, "message": "x"}), _status(sid, "idle")]
    ended = agent.post(path, {"turn_id": turn, "tail": tail})
    assert ended["next"] is None and ended["ended"]["seqs"] == [1, 2, 3]
    written = [json.loads(e) for e in ended["ended"]["events"]]
    assert written[0] == {
        "v": 1,
        "type": "session.result",
        "sessionId": sid,
        "costUsd": 0.125,
        "turns": 3,
        "budgetUsd": 2,
    }
    # JSON.stringify's text: an integral number has no fraction; JS's toFixed rounds a tie up.
    assert '"budgetUsd":2}' in ended["ended"]["events"][0]
    assert written[1]["message"] == "this chat used its $2.00 budget ($0.13 spent)"
    row = _row(tables, sid)
    assert (row["status"], row["durable_turn_id"]) == ("idle", None)
    # Ended: a retried end writes nothing.
    assert agent.post(path, {"turn_id": turn, "tail": _tail(sid)}) == {"ended": None, "next": None}
    # Replaced: a later turn holds the row; the old turn's end never ends it.
    later = str(uuid.uuid4())
    _sql(
        tables,
        "UPDATE ai_sessions SET status = 'running', durable_turn_id = %s WHERE id = %s",
        (later, sid),
    )
    assert agent.post(path, {"turn_id": turn, "tail": _tail(sid)})["ended"] is None
    assert _row(tables, sid)["status"] == "running"
    assert len(_events(tables, sid)) == 3


def test_a_turns_end_claims_the_queues_head_in_its_transaction(
    agent: Agent, tables: PgPool
) -> None:
    turn, head = str(uuid.uuid4()), str(uuid.uuid4())
    sid = _session(tables, status="running", turn=turn)
    body = {"turn_id": turn, "tail": _tail(sid), "next": _next(sid, head, tiers=["read"])}
    done = agent.post(f"/sessions/{sid}/turn/finish", body)
    assert done["next"]["outcome"] == "started" and done["next"]["logged"]["seqs"] == [3, 4]
    row = _row(tables, sid)
    assert (row["status"], str(row["durable_turn_id"])) == ("running", head)
    assert row["turn_tiers"] == {"by": BROWSER, "tiers": ["read"]}
    assert [e["type"] for e in _events(tables, sid)] == [
        "session.result",
        "session.status",
        "user.turn",
        "session.status",
    ]
    # A retry: the end is written already and the head holds the row.
    again = agent.post(f"/sessions/{sid}/turn/finish", body)
    assert again == {"ended": None, "next": {"outcome": "started", "logged": None}}
    # Begun while another turn runs: busy.
    other = _next(sid, str(uuid.uuid4()))
    assert agent.post(f"/sessions/{sid}/turn/begin", {"next": other}) == {
        "outcome": "busy",
        "logged": None,
    }


def test_a_queued_message_the_session_cannot_run_is_dropped_with_why(
    agent: Agent, tables: PgPool
) -> None:
    sid = _session(tables, owner_kind="bearer", owner_id="b")
    head = _next(sid, str(uuid.uuid4()), author={"kind": "bearer", "id": "a"})
    begun = agent.post(f"/sessions/{sid}/turn/begin", {"next": head})
    assert begun["outcome"] == "dropped"
    (error,) = [json.loads(e) for e in begun["logged"]["events"]]
    assert error == {
        "v": 1,
        "type": "error",
        "sessionId": sid,
        "code": turns.QUEUED_DROPPED,
        "message": "A queued message was not sent: the session was handed off.",
    }
    assert _row(tables, sid)["status"] == "idle"
    gone = agent.post(f"/sessions/{uuid.uuid4()}/turn/begin", {"next": head})
    assert gone == {"outcome": "dropped", "logged": None}


def test_the_queue_event_is_written_under_the_rows_lock_and_read_back(
    agent: Agent, tables: PgPool
) -> None:
    sid = _session(tables)
    assert agent.get(f"/sessions/{sid}/turn/queue") == {"event": None}
    queued = {"v": 1, "type": "session.queue", "sessionId": sid, "version": 2, "queue": []}
    written = agent.post(f"/sessions/{sid}/turn/queue", {"event": json.dumps(queued)})
    assert written["logged"]["seqs"] == [1]
    assert json.loads(agent.get(f"/sessions/{sid}/turn/queue")["event"]) == queued
    gone = agent.post(f"/sessions/{uuid.uuid4()}/turn/queue", {"event": json.dumps(queued)})
    assert gone == {"logged": None}
    not_queue = {"event": _status(sid, "idle")}
    assert agent.status("POST", f"/sessions/{sid}/turn/queue", not_queue).status_code == 422


def test_the_sweep_ends_a_lost_turn_only_while_the_row_is_as_it_read_it(
    agent: Agent, tables: PgPool
) -> None:
    sid = _session(tables, status="waiting_input", turn=str(uuid.uuid4()))
    _sql(
        tables,
        "UPDATE ai_sessions SET updated_at = now() - interval '1 hour' WHERE id = %s",
        (sid,),
    )
    fresh = _session(tables, status="running", turn=str(uuid.uuid4()))
    rows = agent.get("/turns/stale", grace_ms=60_000, limit=10)["rows"]
    assert {"id": sid, "event_seq": "0"} in rows
    assert all(r["id"] != fresh for r in rows)
    lost = {"v": 1, "type": "error", "sessionId": sid, "code": "turn_failed", "message": "gone"}
    tail = [json.dumps(lost), _status(sid, "idle")]
    # Moved on since the sweep read it (an event was appended): never ended.
    _sql(tables, "UPDATE ai_sessions SET event_seq = 1 WHERE id = %s", (sid,))
    stale = agent.post(f"/sessions/{sid}/turn/end-lost", {"event_seq": 0, "tail": tail})
    assert stale == {"logged": None}
    assert _row(tables, sid)["status"] == "waiting_input"
    ended = agent.post(f"/sessions/{sid}/turn/end-lost", {"event_seq": 1, "tail": tail})
    assert ended["logged"]["seqs"] == [2, 3]
    assert _row(tables, sid)["status"] == "idle"


def test_a_turns_end_waits_for_a_claim_holding_the_row(agent: Agent, tables: PgPool) -> None:
    turn = str(uuid.uuid4())
    sid = _session(tables, status="running", turn=turn)
    result: dict[str, Any] = {}
    with tables.connection() as conn, conn.transaction():
        conn.execute("SELECT 1 FROM ai_sessions WHERE id = %s FOR UPDATE", (sid,))
        thread = threading.Thread(
            target=lambda: result.update(
                agent.post(f"/sessions/{sid}/turn/finish", {"turn_id": turn, "tail": _tail(sid)})
            )
        )
        thread.start()
        thread.join(0.5)
        assert thread.is_alive()
        conn.execute(
            "UPDATE ai_sessions SET durable_turn_id = %s WHERE id = %s",
            (str(uuid.uuid4()), sid),
        )
    thread.join(10)
    # The end read the row after the other writer committed: the turn was replaced.
    assert result == {"ended": None, "next": None}


def test_the_bodies_and_ids_are_bounded(agent: Agent, tables: PgPool) -> None:
    turn = str(uuid.uuid4())
    sid = _session(tables, status="running", turn=turn)
    base = f"/sessions/{sid}/turn"
    bad: list[tuple[str, dict[str, Any]]] = [
        ("/send-claim", {"author": BROWSER, "turn_id": "t", "bare": False}),
        ("/send-claim", {"author": {"kind": "x", "id": "a"}, "turn_id": turn, "bare": False}),
        ("/send-claim", {"author": BROWSER, "turn_id": turn, "bare": False, "extra": 1}),
        (
            "/send-claim",
            {"author": BROWSER, "turn_id": turn, "bare": False, "tiers": ["r"] * 17},
        ),
        ("/give-back", {"prior": "sideways"}),
        ("/placement", {"turn_id": turn, "attempt": "", "mark_key": turn}),
        ("/placement", {"turn_id": "../x", "attempt": "a", "mark_key": turn}),
        ("/spend", {"key": "k", "cost_usd": float("1e9"), "unpriced_usd": 0, "turns": 0}),
        ("/spend", {"cost_usd": 0, "unpriced_usd": 0, "turns": 0}),
        ("/spend", {"key": "k" * 600, "cost_usd": 0, "unpriced_usd": 0, "turns": 0}),
        ("/queue", {"event": _status(sid, "idle")}),
        ("/finish", {"turn_id": turn, "tail": []}),
        ("/finish", {"turn_id": turn, "tail": ["not json"]}),
        ("/finish", {"turn_id": turn, "tail": [_tail(sid)[0]]}),
        ("/finish", {"turn_id": turn, "tail": [_status(sid, "running")]}),
        ("/finish", {"turn_id": turn, "tail": _tail(sid) * 6}),
        ("/begin", {"next": {**_next(sid, turn), "started": []}}),
        ("/end-lost", {"event_seq": -1, "tail": _tail(sid)}),
    ]
    for tail, body in bad:
        assert agent.status("POST", f"{base}{tail}", body).status_code == 422, (tail, body)
    huge = agent.client.post(
        f"{BASE}{base}/finish",
        content=b'{"x":"' + b"y" * crud_agent_turns.BODY_BYTES_MAX + b'"}',
        headers={**agent.headers, "content-type": "application/json"},
    )
    assert huge.status_code == 413
    assert (
        agent.status("GET", "/turns/stale", limit=crud_agent_turns.STALE_MAX + 1).status_code == 422
    )
    for bad_id in ("not-a-uuid", "..", "%2E%2E", sid.upper(), f"{sid}%2F..%2F"):
        assert agent.status("GET", f"/sessions/{bad_id}/turn").status_code in (404, 422)
        claim = {"author": BROWSER, "turn_id": turn, "bare": False}
        refused = agent.status("POST", f"/sessions/{bad_id}/turn/send-claim", claim)
        assert refused.status_code in (404, 422)
    row = _row(tables, sid)
    assert (row["status"], str(row["durable_turn_id"]), row["turn_attempt"]) == (
        "running",
        turn,
        None,
    )
    assert _events(tables, sid) == []
