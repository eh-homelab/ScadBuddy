"""The tool-call gate's writes through the agent's CRUD routes (#5299 step 10, area ``gate``,
`api/crud_agent_gate.py`): only the ``agent`` credential reaches them; each is the agent's
``gate/store.ts`` writer as one transaction (`agent_data/gate.py`), its rows, events, status,
audit row and ``scadbuddy_input`` NOTIFY committed together; a run grant's approval only
while the backend's record of its use holds; a response decided by the gate's validator on
the API; and two resolutions of one request, of which exactly one writes.

The agent's tables are made from its own migration files, as the API applies them at its
start (#5299 D6). ``agent/test/gateBackend.pg.test.ts`` checks that these routes write the
same rows and events as the agent's own SQL."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.agent_data import gate_validate
from scadbuddy.agent_data.gate_canonical import response_hash
from scadbuddy.api import crud_agent_gate, crud_areas
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
ME = {"kind": "browser", "id": "browser", "label": "You"}
SYSTEM = {"kind": "system", "id": "scadbuddy", "label": "ScadBuddy"}
HASH = "a" * 64


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
    app.include_router(crud_agent_gate.router, prefix="/api/v1")
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
    """The agent's calls, with its credential; each answer's JSON, checked 200."""

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

    def status(self, path: str, body: Any) -> int:
        response = self.client.post(f"{BASE}{path}", json=body, headers=self.headers)
        return int(response.status_code)


def _sql(pool: PgPool, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(query.encode(), params)
        return list(cur.fetchall()) if cur.description else []


def _session(pool: PgPool, status: str = "running") -> str:
    session = str(uuid.uuid4())
    _sql(
        pool,
        "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
        " creator_id, status, max_turns, budget_usd) VALUES (%s, 'chat', 'browser', 'browser',"
        " 'You', 'browser', 'browser', %s, 10, 1)",
        (session, status),
    )
    return session


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


@pytest.fixture
def app(tables: PgPool) -> FastAPI:
    return _app(tables)


@pytest.fixture
def agent(app: FastAPI) -> Iterator[Agent]:
    with TestClient(app) as client:
        yield Agent(client, _token(app))


def _rid(session: str, run: str = "run-1", tool_use: str = "toolu_1") -> str:
    return f"durable:{session}:{run}:{tool_use}"


def _open(
    session: str,
    run: str = "run-1",
    kind: str = "approval",
    tool_use: str = "toolu_1",
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": _rid(session, run, tool_use),
        "session_id": session,
        "workflow_id": f"session-{session}",
        "workflow_run_id": run,
        "kind": kind,
        "tool_use_id": tool_use,
        "requested_by": ME,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    }
    if kind == "approval":
        body.update(
            tool="print_output",
            summary='{"output":"box"}',
            input_hash=HASH,
            prompt="",
            responders=["browser", "grant"],
        )
    else:
        body.update(
            tool="ask_user",
            summary="",
            input_hash=None,
            prompt="Which colour?",
            responders=["browser"],
            questions=[
                {"question": "Which colour?", "header": "C", "multiSelect": False, "options": []}
            ],
        )
    return {**body, **extra}


def _record(body: dict[str, Any]) -> dict[str, Any]:
    """The entry's record as the agent's turn writes it (`GateRecord`)."""
    record: dict[str, Any] = {
        "id": body["request_id"],
        "kind": body["kind"],
        "inputHash": body["input_hash"],
        "requestedBy": {"kind": "bearer", "id": "agent-1"},
        "sessionOwner": {"kind": "browser", "id": "browser"},
        "sessionCreator": {"kind": "browser", "id": "browser"},
    }
    if body["kind"] == "answer":
        record["questions"] = [{"question": "Which colour?", "multiSelect": False}]
    return record


def _types(pool: PgPool, session: str) -> list[str]:
    rows = _sql(
        pool,
        "SELECT event::jsonb->>'type' AS t FROM ai_session_events WHERE session_id = %s"
        " ORDER BY seq",
        (session,),
    )
    return [r["t"] for r in rows]


def _status(pool: PgPool, session: str) -> str:
    return str(_sql(pool, "SELECT status FROM ai_sessions WHERE id = %s", (session,))[0]["status"])


# -- the guard -------------------------------------------------------------------------------


def test_only_the_agent_credential_reaches_the_routes(app: FastAPI, tables: PgPool) -> None:
    session = _session(tables)
    with TestClient(app) as client:
        for caller in ("print", "projects", "render"):
            refused = client.post(
                f"{BASE}/gate/open", json=_open(session), headers=_token(app, caller)
            )
            assert refused.status_code == 403, refused.text
        assert client.post(f"{BASE}/gate/open", json=_open(session)).status_code == 401
        assert client.get(f"{BASE}/gate/response", params={"request_id": "x"}).status_code == 401
    assert _sql(tables, "SELECT count(*) AS n FROM ai_pending_input")[0]["n"] == 0


def test_the_areas_list_the_gate(agent: Agent) -> None:
    assert "gate" in agent.get("/areas")["areas"]


def test_a_body_out_of_bounds_is_refused_before_any_sql(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    for body in (
        _open(session, summary="x" * (crud_agent_gate.TEXT_MAX + 1)),
        _open(session, session_id="../not-a-session"),
        _open(session, request_id="d" * (crud_agent_gate.REQUEST_ID_MAX + 1)),
        _open(session, responders=["browser", "grant", "browser"]),
        _open(session, gate={"big": "x" * crud_agent_gate.CARD_JSON_MAX}),
        _open(session, extra=1),
    ):
        assert agent.status("/gate/open", body) == 422
    big = {"kind": "answer", "text": "x" * (crud_agent_gate.RESPONSE_JSON_MAX + 1)}
    resolve = {"request_id": _rid(session), "outcome": "answered", "responder": ME}
    assert agent.status("/gate/resolve", {**resolve, "response": big}) == 422
    assert agent.status("/gate/resolve", {**resolve, "response_hash": "../x"}) == 422
    assert _sql(tables, "SELECT count(*) AS n FROM ai_pending_input")[0]["n"] == 0


# -- open and resolve ------------------------------------------------------------------------


def test_an_open_writes_the_entry_its_events_and_the_status_once(
    agent: Agent, tables: PgPool
) -> None:
    session = _session(tables)
    first = agent.post("/gate/open", _open(session))
    assert first["opened"] is True
    [batch] = first["logged"]
    assert batch["session_id"] == session
    assert [e["type"] for e in batch["events"]] == [
        "approval.required",
        "input.requested",
        "session.status",
    ]
    assert batch["seqs"] == [1, 2, 3]
    assert batch["events"][0] == {
        "v": 1,
        "type": "approval.required",
        "sessionId": session,
        "id": _rid(session),
        "tool": "toolu_1",
        "summary": 'print_output {"output":"box"}',
        "risk": "outward",
    }
    assert _status(tables, session) == "waiting_approval"
    # A retried activity adds nothing.
    assert agent.post("/gate/open", _open(session)) == {"opened": False, "logged": []}
    assert _types(tables, session) == ["approval.required", "input.requested", "session.status"]


def test_a_resolve_writes_once_with_its_audit_and_notify(
    agent: Agent, tables: PgPool, pg_conninfo: str
) -> None:
    session = _session(tables)
    agent.post("/gate/open", _open(session))
    with psycopg.connect(pg_conninfo, autocommit=True) as listener:
        listener.execute("LISTEN scadbuddy_input")
        body = {"request_id": _rid(session), "outcome": "approved", "responder": ME}
        resolved = agent.post("/gate/resolve", body)
        assert [n.payload for n in listener.notifies(timeout=0.5, stop_after=1)] == [_rid(session)]
    assert resolved["resolved"] is True
    assert [e["type"] for e in resolved["logged"][0]["events"]] == [
        "approval.resolved",
        "input.resolved",
        "session.status",
    ]
    assert _status(tables, session) == "running"
    [row] = _sql(tables, "SELECT outcome, responder, tool FROM ai_input_responses")
    assert row == {"outcome": "approved", "responder": ME, "tool": "print_output"}
    [audit] = _sql(tables, "SELECT kind, action, outcome, tool_use_id FROM ai_audit")
    assert audit == {
        "kind": "approval",
        "action": "approved",
        "outcome": "ok",
        "tool_use_id": "toolu_1",
    }
    assert agent.post("/gate/resolve", body) == {"resolved": False, "logged": []}
    # An open after its request resolved opens nothing.
    assert agent.post("/gate/open", _open(session))["opened"] is False


def test_two_resolves_of_one_request_write_once(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    agent.post("/gate/open", _open(session))
    results: list[bool] = []
    start = threading.Barrier(2)

    def resolve(outcome: str) -> Callable[[], None]:
        def run() -> None:
            start.wait()
            body = {"request_id": _rid(session), "outcome": outcome, "responder": ME}
            results.append(agent.post("/gate/resolve", body)["resolved"])

        return run

    threads = [threading.Thread(target=resolve(o)) for o in ("approved", "denied")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [False, True]
    assert _sql(tables, "SELECT count(*) AS n FROM ai_input_responses")[0]["n"] == 1
    assert _types(tables, session).count("input.resolved") == 1
    assert _sql(tables, "SELECT count(*) AS n FROM ai_audit")[0]["n"] == 1


def test_an_open_of_a_new_run_cancels_the_old_runs_entries(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    agent.post("/gate/open", _open(session, "run-1"))
    opened = agent.post("/gate/open", _open(session, "run-2", tool_use="toolu_2"))
    assert opened["opened"] is True
    reset, fresh = opened["logged"]
    assert [e["type"] for e in reset["events"]] == [
        "approval.resolved",
        "input.resolved",
        "session.status",
    ]
    assert reset["events"][1]["reason"] == "reset"
    assert fresh["seqs"][0] == reset["seqs"][-1] + 1
    rows = _sql(tables, "SELECT request_id, outcome, reason FROM ai_input_responses")
    assert rows == [
        {"request_id": _rid(session, "run-1"), "outcome": "cancelled", "reason": "reset"}
    ]


def test_an_unanswered_question_is_audited_and_an_attention_timer_says_why(
    agent: Agent, tables: PgPool
) -> None:
    session = _session(tables)
    body = _open(session, kind="answer", attention={"reason": "done", "on_timeout": None})
    opened = agent.post("/gate/open", body)
    asked = opened["logged"][0]["events"][0]
    assert asked["attention"]["onTimeout"] is None and asked["attention"]["expiresAt"]
    resolved = agent.post(
        "/gate/resolve", {"request_id": _rid(session), "outcome": "timed_out", "responder": SYSTEM}
    )
    reason = "nobody replied in time (on_timeout: proceed)"
    assert resolved["logged"][0]["events"][0]["reason"] == reason
    [audit] = _sql(tables, "SELECT kind, action, outcome FROM ai_audit")
    assert audit == {"kind": "question", "action": "timed_out", "outcome": "refused"}


def test_a_tab_wait_ended_by_the_tab_says_reconnected(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    agent.post("/gate/open", _open(session, kind="answer"))
    resolved = agent.post(
        "/gate/resolve",
        {
            "request_id": _rid(session),
            "outcome": "cancelled",
            "responder": SYSTEM,
            "response": {"reconnected": True},
            "reason": "tab back",
        },
    )
    assert resolved["logged"][0]["events"][0]["reconnected"] is True


# -- decide ----------------------------------------------------------------------------------


def _decide(request_id: str, response: Any, role: str = "browser", **extra: Any) -> Any:
    return {"request_id": request_id, "response": response, "responder": ME, "role": role, **extra}


APPROVE = {"kind": "approval", "decision": "approve"}


def test_a_decide_validates_on_the_api_and_writes_once(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    body = _open(session)
    agent.post("/gate/open", {**body, "gate": _record(body)})
    decided = agent.post("/gate/decide", _decide(_rid(session), APPROVE))
    assert decided["decided"] == {"kind": "approval", "outcome": "approved"}
    assert [e["type"] for e in decided["logged"][0]["events"]][:2] == [
        "approval.resolved",
        "input.resolved",
    ]
    [row] = _sql(tables, "SELECT outcome, response, response_hash FROM ai_input_responses")
    assert row["outcome"] == "approved" and row["response"] == {"decision": "approve"}
    assert row["response_hash"] == response_hash(APPROVE)
    record = agent.get("/gate/response", request_id=_rid(session))["record"]
    assert record == {
        "outcome": "approved",
        "response": {"decision": "approve"},
        "responder": ME,
        "reason": None,
    }
    again = agent.post("/gate/decide", _decide(_rid(session), APPROVE))
    assert again["refused"]["code"] == "stale"


def test_a_decide_refuses_what_the_sql_path_refused(agent: Agent, tables: PgPool) -> None:
    """A role, a responder or a body the validator refuses writes nothing."""
    session = _session(tables)
    body = _open(session)
    agent.post("/gate/open", {**body, "gate": _record(body)})
    own = {"kind": "bearer", "id": "agent-1", "label": "Agent"}
    for request, code in (
        (_decide(_rid(session), APPROVE, "run_grant"), "forbidden"),
        (_decide(_rid(session), APPROVE, "parent"), "forbidden"),
        ({**_decide(_rid(session), APPROVE, "grant"), "responder": own}, "self"),
        (_decide(_rid(session), {"kind": "approval", "decision": "maybe"}), "malformed"),
        (_decide(_rid(session), {**APPROVE, "input_hash": "b" * 64}), "input_mismatch"),
        (_decide(_rid(session), {"kind": "answer", "text": "hi"}), "malformed"),
    ):
        assert agent.post("/gate/decide", request)["refused"]["code"] == code
    assert _sql(tables, "SELECT count(*) AS n FROM ai_input_responses")[0]["n"] == 0
    assert _sql(tables, "SELECT count(*) AS n FROM ai_pending_input")[0]["n"] == 1
    # A row past its timer is resolving, as the workflow is about to write it.
    _sql(tables, "UPDATE ai_pending_input SET expires_at = now() - interval '1 second'")
    assert agent.post("/gate/decide", _decide(_rid(session), APPROVE))["refused"]["code"] == (
        "resolving"
    )


def test_a_response_too_large_in_bytes_is_the_validators_refusal(
    agent: Agent, tables: PgPool
) -> None:
    """A response is bounded by its UTF-8 bytes, as the agent's body limit counts them: one past
    the validator's cap is refused ``too_large``, as on the SQL path, never a 422, however many
    characters its escapes would take."""
    session = _session(tables)
    body = _open(session, kind="answer")
    agent.post("/gate/open", {**body, "gate": _record(body)})
    # 4 UTF-8 bytes each, 12 characters each as an escaped JSON string.
    emoji = "\U0001f600" * (gate_validate.RESPONSE_MAX // 4 + 1)
    request = _decide(_rid(session), {"kind": "answer", "answers": [emoji]})
    assert agent.post("/gate/decide", request)["refused"]["code"] == "too_large"


def test_a_decide_stores_the_agents_hash_of_the_raw_response(agent: Agent, tables: PgPool) -> None:
    """The agent hashes the response as the person sent it (#3724), as a resolve does."""
    session = _session(tables)
    body = _open(session)
    agent.post("/gate/open", {**body, "gate": _record(body)})
    digest = "c" * 64
    request = _decide(_rid(session), APPROVE, response_hash=digest)
    assert agent.post("/gate/decide", request)["decided"]["outcome"] == "approved"
    [row] = _sql(tables, "SELECT response_hash FROM ai_input_responses")
    assert row["response_hash"] == digest
    assert (
        agent.status("/gate/decide", _decide(_rid(session), APPROVE, response_hash="../x")) == 422
    )


def test_a_decide_of_a_workflows_entry_or_a_malformed_id_writes_nothing(
    agent: Agent, tables: PgPool
) -> None:
    session = _session(tables)
    agent.post("/gate/open", _open(session))
    assert agent.post("/gate/decide", _decide(_rid(session), APPROVE))["workflow"] is True
    for rid in ("../../areas", "durable:not-a-uuid", "x"):
        assert agent.post("/gate/decide", _decide(rid, APPROVE))["refused"]["code"] == "stale"
        assert agent.get("/gate/response", request_id=rid) == {"record": None}
    assert _sql(tables, "SELECT count(*) AS n FROM ai_input_responses")[0]["n"] == 0


def test_a_decide_waits_for_the_agent_to_find_the_run_live(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    body = _open(session)
    agent.post("/gate/open", {**body, "gate": _record(body)})
    first = agent.post("/gate/decide", _decide(_rid(session), APPROVE, check_live=True))
    assert first["live_check"] == {"workflow_id": f"session-{session}", "run_id": "run-1"}
    assert _sql(tables, "SELECT count(*) AS n FROM ai_pending_input")[0]["n"] == 1
    # A run other than the row's writes nothing either.
    other = _decide(_rid(session), APPROVE, check_live=True, live_run="run-0")
    assert agent.post("/gate/decide", other)["live_check"] is not None
    live = _decide(_rid(session), APPROVE, check_live=True, live_run="run-1")
    assert agent.post("/gate/decide", live)["decided"]["outcome"] == "approved"


def test_two_decides_of_one_request_write_once(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    body = _open(session)
    agent.post("/gate/open", {**body, "gate": _record(body)})
    answers: list[Any] = []
    start = threading.Barrier(2)

    def decide(decision: str) -> Callable[[], None]:
        def run() -> None:
            start.wait()
            response = {"kind": "approval", "decision": decision}
            answers.append(agent.post("/gate/decide", _decide(_rid(session), response)))

        return run

    threads = [threading.Thread(target=decide(d)) for d in ("approve", "deny")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    won = [a for a in answers if a["decided"] is not None]
    lost = [a for a in answers if a["refused"] is not None]
    assert len(won) == 1 and [a["refused"]["code"] for a in lost] == ["stale"]
    assert _sql(tables, "SELECT count(*) AS n FROM ai_input_responses")[0]["n"] == 1


def test_an_answer_is_decided_into_its_answers(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    body = _open(session, kind="answer")
    agent.post("/gate/open", {**body, "gate": _record(body)})
    response = {"kind": "answer", "answers": {"Which colour?": "Red"}}
    decided = agent.post("/gate/decide", _decide(_rid(session), response))
    assert decided["decided"] == {"kind": "answer", "outcome": "answered"}
    resolved = decided["logged"][0]["events"][0]
    assert resolved["answers"] == ["Red"] and resolved["by"] == ME


# -- grant and deny --------------------------------------------------------------------------


def _grant_row(
    pool: PgPool, grant: str, *, status: str = "approved", expires: timedelta = timedelta(hours=1)
) -> None:
    _sql(
        pool,
        "INSERT INTO flow_grants (id, run_id, status, spec, requested_by, expires_at)"
        " VALUES (%s, %s, %s, '{}'::jsonb, '{}'::jsonb, now() + %s)",
        (grant, f"run-{grant}", status, expires),
    )


def _use(pool: PgPool, grant: str, request_id: str, session: str, digest: str = HASH) -> None:
    _sql(
        pool,
        "INSERT INTO flow_grant_uses (request_id, grant_id, run_id, fn, input_hash, rule_index,"
        " session_id) VALUES (%s, %s, %s, 'tool', %s, 0, %s)",
        (request_id, grant, f"run-{grant}", digest, session),
    )


def _grant(session: str, request_id: str, grant: str, digest: str = HASH) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "session_id": session,
        "tool": "print_output",
        "summary": "box",
        "input_hash": digest,
        "grant_id": grant,
        "requested_by": ME,
    }


def test_a_grant_approves_only_while_the_backends_record_holds(
    agent: Agent, tables: PgPool
) -> None:
    session = _session(tables)
    rid = _rid(session)
    _grant_row(tables, "g1")
    _use(tables, "g1", rid, session)
    # Nothing of the verdict is taken on trust: another hash, request, session or grant.
    other = _session(tables)
    for body in (
        _grant(session, rid, "g1", "d" * 64),
        _grant(session, _rid(session, tool_use="toolu_9"), "g1"),
        _grant(other, rid, "g1"),
        _grant(session, rid, "g2"),
        _grant(session, rid, "../g1"),
    ):
        assert agent.post("/gate/grant", body) == {"granted": False}
    assert _sql(tables, "SELECT count(*) AS n FROM ai_input_responses")[0]["n"] == 0
    assert agent.post("/gate/grant", _grant(session, rid, "g1")) == {"granted": True}
    # A retried activity: the same grant's approval is there already.
    assert agent.post("/gate/grant", _grant(session, rid, "g1")) == {"granted": True}
    [row] = _sql(tables, "SELECT outcome, responder FROM ai_input_responses")
    assert row == {
        "outcome": "approved",
        "responder": {"kind": "run_grant", "id": "g1", "label": "Run grant"},
    }
    assert _sql(tables, "SELECT count(*) AS n FROM ai_audit")[0]["n"] == 1


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE flow_grants SET revoked_at = now()",
        "UPDATE flow_grants SET status = 'declined'",
        "UPDATE flow_grants SET expires_at = now() - interval '1 second'",
    ],
)
def test_a_grant_not_in_force_approves_nothing(agent: Agent, tables: PgPool, change: str) -> None:
    session = _session(tables)
    rid = _rid(session)
    _grant_row(tables, "g1")
    _use(tables, "g1", rid, session)
    _sql(tables, change)
    assert agent.post("/gate/grant", _grant(session, rid, "g1")) == {"granted": False}
    assert _sql(tables, "SELECT count(*) AS n FROM ai_input_responses")[0]["n"] == 0


def test_a_revocation_waits_for_a_grant_under_its_share_lock(
    agent: Agent, tables: PgPool, pg_conninfo: str
) -> None:
    """A revocation lands wholly before or after the grant's transaction."""
    session = _session(tables)
    rid = _rid(session)
    _grant_row(tables, "g1")
    _use(tables, "g1", rid, session)
    with psycopg.connect(pg_conninfo) as revoker:
        revoker.execute("UPDATE flow_grants SET revoked_at = now() WHERE id = 'g1'")
        result: list[Any] = []
        thread = threading.Thread(
            target=lambda: result.append(agent.post("/gate/grant", _grant(session, rid, "g1")))
        )
        thread.start()
        thread.join(0.5)
        assert thread.is_alive()  # waiting on the revocation's row lock
        revoker.commit()
        thread.join()
    assert result == [{"granted": False}]


def test_a_deny_writes_once(agent: Agent, tables: PgPool) -> None:
    session = _session(tables)
    body = {
        "request_id": _rid(session),
        "session_id": session,
        "run_id": "flow-run-1",
        "tool": "print_output",
        "summary": "box",
        "input_hash": HASH,
        "reason": "outside",
        "requested_by": ME,
    }
    assert agent.post("/gate/deny", body) == {"denied": True}
    assert agent.post("/gate/deny", body) == {"denied": False}
    [row] = _sql(tables, "SELECT outcome, reason, hops FROM ai_input_responses")
    assert row["outcome"] == "denied" and row["reason"] == "outside"
    assert row["hops"][0]["actor"] == "flow"
    [audit] = _sql(tables, "SELECT action, outcome FROM ai_audit")
    assert audit == {"action": "denied", "outcome": "denied"}
