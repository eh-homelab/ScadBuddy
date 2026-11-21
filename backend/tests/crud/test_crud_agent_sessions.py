"""A session's row and what its tool calls touched through CRUD routes (#5299 step 9, part a,
`api/crud_agent_sessions.py`): only the ``agent`` credential reaches them, each route is the
agent's SQL as one transaction (`agent_data/sessions.py`), an insert is counted under the
owner's start lock and a fork's checked under its parent's, a handoff and a raise apply only
while the row is as the agent read it, and every body is bounded.

The agent's tables are made from its own migration files, as the API applies them at its
start (#5299 D6)."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.agent_data import sessions
from scadbuddy.api import crud_agent_sessions, crud_areas
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
YOU = {"kind": "browser", "id": "browser", "label": "You"}
TOKEN = {"kind": "bearer", "id": "token:42", "label": "CI token"}
OTHER = {"kind": "bearer", "id": "token:7", "label": "Other"}


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
    app.include_router(crud_agent_sessions.router, prefix="/api/v1")
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

    def post(self, path: str, body: Any = None) -> Any:
        response = self.client.post(f"{BASE}{path}", json=body, headers=self.headers)
        assert response.status_code == 200, response.text
        return response.json()

    def refused(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        response = self.client.request(
            method, f"{BASE}{path}", json=body, params=params, headers=self.headers
        )
        assert response.status_code >= 400, response.text
        return response


def _sql(pool: PgPool, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(query.encode(), params)
        return list(cur.fetchall()) if cur.description else []


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


def _new(owner: dict[str, str] = YOU, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "origin": "chat",
        "owner": owner,
        "title": "A chat",
        "tags": ["t"],
        "scope": {"model": "m"},
        "runtime": "unified",
        "max_turns": 20,
        "budget_usd": 5,
        **extra,
    }


def _start(agent: Agent, owner: dict[str, str] = YOU, **extra: Any) -> dict[str, Any]:
    session: dict[str, Any] = agent.post("/sessions", _new(owner, **extra))["session"]
    return session


# --- auth --------------------------------------------------------------------------------


def test_only_the_agent_credential_reaches_the_routes(app: FastAPI) -> None:
    with TestClient(app) as client:
        some = str(uuid.uuid4())
        assert client.get(f"{BASE}/sessions/{some}").status_code == 401
        for caller in ("render", "print", "projects"):
            wrong = client.get(f"{BASE}/sessions/{some}", headers=_token(app, caller))
            assert wrong.status_code == 403
            assert wrong.headers["x-scadbuddy-crud"] == "1"
        assert client.get(f"{BASE}/sessions/{some}", headers=_token(app)).json() == {
            "session": None
        }


def test_the_areas_list_sessions_and_resources(agent: Agent) -> None:
    areas = agent.get("/areas")["areas"]
    assert "sessions" in areas and "session-resources" in areas


def test_an_id_in_the_path_is_a_uuid(agent: Agent) -> None:
    for bad in ("not-a-uuid", "..", "%2E%2E%2Fsettings", str(uuid.uuid4()).upper()):
        response = agent.client.get(f"{BASE}/sessions/{bad}", headers=agent.headers)
        assert response.status_code in (404, 422), (bad, response.status_code)
    # A `../` names another route, never a session: nothing outside the session routes answers.
    traversal = agent.client.get(f"{BASE}/sessions/../areas", headers=agent.headers)
    assert traversal.status_code in (404, 422) or "areas" in traversal.json()
    assert agent.refused("POST", "/sessions/x/resources", {"rows": []}).status_code == 422


# --- the row -----------------------------------------------------------------------------


def test_a_session_is_inserted_and_read_whole(agent: Agent) -> None:
    body = _new(TOKEN)
    row = agent.post("/sessions", body)["session"]
    assert row["id"] == body["id"]
    assert row["owner_kind"] == "bearer" and row["creator_id"] == "token:42"
    assert row["status"] == "idle" and row["title"] == "A chat" and row["tags"] == ["t"]
    assert row["scope"] == {"model": "m"} and row["runtime"] == "unified"
    assert row["budget_usd"] == 5 and row["pool_cost_usd"] == 0 and row["turns"] == 0
    assert row["offer_kind"] is None and row["archived_at"] is None
    assert row["background_tools"] is True and row["turn_flow"] is False
    assert row["created_at"].endswith("Z") and len(row["created_at"]) == 24
    assert row["claimed_at"].endswith("Z") and len(row["claimed_at"]) == 27
    assert agent.get(f"/sessions/{body['id']}")["session"] == row
    # A repeat of the same insert is refused, never a second row.
    assert agent.refused("POST", "/sessions", body).status_code == 409


def test_a_fork_and_a_child_spend_from_the_lineage(agent: Agent, tables: PgPool) -> None:
    parent = _start(agent)
    _sql(tables, "UPDATE ai_sessions SET cost_usd = 2 WHERE id = %s", (parent["id"],))
    child = _start(
        agent,
        parent_id=parent["id"],
        budget_of=parent["id"],
        budget_usd=parent["budget_usd"],
        fork_of={"parent_id": parent["id"], "cut": 3},
    )
    assert child["parent_id"] == parent["id"] and child["pool_cost_usd"] == 2
    row = _sql(
        tables,
        "SELECT budget_root_id::text AS root, fork_parent_id::text AS fp, fork_cut"
        " FROM ai_sessions WHERE id = %s",
        (child["id"],),
    )[0]
    assert row == {"root": parent["id"], "fp": parent["id"], "fork_cut": 3}
    # A grandchild's root is still the lineage's.
    grand = _start(agent, parent_id=child["id"], budget_of=child["id"])
    assert _sql(
        tables, "SELECT budget_root_id::text AS r FROM ai_sessions WHERE id = %s", (grand["id"],)
    ) == [{"r": parent["id"]}]


def test_background_tools_come_from_the_parent_unless_refused(agent: Agent) -> None:
    flow = _start(agent, background_tools=False)
    assert flow["background_tools"] is False
    assert _start(agent, parent_id=flow["id"])["background_tools"] is False
    plain = _start(agent)
    assert _start(agent, parent_id=plain["id"])["background_tools"] is True
    assert _start(agent, parent_id=plain["id"], background_tools=False)["background_tools"] is False
    assert agent.refused("POST", "/sessions", _new(background_tools=True)).status_code == 422


def test_the_new_session_limit_holds_under_the_owner_lock(agent: Agent, tables: PgPool) -> None:
    limit = {"max": 2, "window_ms": 60_000}
    _start(agent, TOKEN, limit=limit)
    _start(agent, TOKEN, limit=limit)
    refused = agent.refused("POST", "/sessions", _new(TOKEN, limit=limit))
    assert refused.status_code == 429 and refused.json()["reason"] == "rate_limited"
    # Another owner counts on its own; without a limit an insert is not counted.
    _start(agent, OTHER, limit=limit)
    _start(agent, TOKEN)
    assert agent.get(
        "/session-starts", owner_kind="bearer", owner_id="token:42", window_ms=60_000
    ) == {"count": 3}
    # The lock is the agent's own: a held lock makes a concurrent insert wait.
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with tables.connection() as conn, conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("ai_sessions.start:bearer:token:7",),
            )
            held.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    held.wait(10)
    done: list[int] = []

    def insert() -> None:
        response = agent.client.post(
            f"{BASE}/sessions", json=_new(OTHER, limit=limit), headers=agent.headers
        )
        done.append(response.status_code)

    other = threading.Thread(target=insert)
    other.start()
    other.join(0.5)
    assert done == []
    release.set()
    thread.join()
    other.join(10)
    assert done == [200]


def test_a_fork_of_a_forgotten_parent_is_refused(agent: Agent, tables: PgPool) -> None:
    parent = _start(agent)
    _sql(
        tables,
        "INSERT INTO ai_forgotten_subjects (subject) VALUES (%s)",
        (f"session-{parent['id']}",),
    )
    body = _new(parent_id=parent["id"], fork_of={"parent_id": parent["id"], "cut": 1})
    refused = agent.refused("POST", "/sessions", body)
    assert refused.status_code == 404 and refused.json()["reason"] == "forgotten"
    assert agent.get(f"/sessions/{body['id']}") == {"session": None}


def test_a_fork_waits_for_a_forget_of_its_parent(agent: Agent, tables: PgPool) -> None:
    parent = _start(agent)
    held = threading.Event()
    release = threading.Event()

    def forget() -> None:
        with tables.connection() as conn, conn.transaction():
            subject = f"session-{parent['id']}"
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (payload_key_lock(subject),))
            conn.execute("INSERT INTO ai_forgotten_subjects (subject) VALUES (%s)", (subject,))
            held.set()
            release.wait(10)

    thread = threading.Thread(target=forget)
    thread.start()
    held.wait(10)
    answers: list[Any] = []

    def fork() -> None:
        body = _new(parent_id=parent["id"], fork_of={"parent_id": parent["id"], "cut": 1})
        answers.append(
            agent.client.post(f"{BASE}/sessions", json=body, headers=agent.headers).json()
        )

    forking = threading.Thread(target=fork)
    forking.start()
    forking.join(0.5)
    assert answers == []
    release.set()
    thread.join()
    forking.join(10)
    assert answers[0]["reason"] == "forgotten"


def test_an_insert_body_is_bounded(agent: Agent) -> None:
    cases: list[dict[str, Any]] = [
        _new(title="x" * (crud_agent_sessions.TITLE_MAX + 1)),
        _new(tags=["t"] * (crud_agent_sessions.TAGS_MAX + 1)),
        _new(tags=["t" * (crud_agent_sessions.TAG_MAX + 1)]),
        _new(scope={"k": "x" * crud_agent_sessions.SCOPE_BYTES_MAX}),
        _new(owner={**YOU, "id": "x" * 513}),
        _new(owner={"kind": "root", "id": "x", "label": "x"}),
        _new(budget_usd=0),
        _new(budget_usd=101),
        _new(max_turns=0),
        _new(origin="web"),
        _new(id="../x"),
        _new(fork_of={"parent_id": "x", "cut": 1}),
        _new(extra="x"),
    ]
    for body in cases:
        assert agent.refused("POST", "/sessions", body).status_code == 422
    huge = agent.client.post(
        f"{BASE}/sessions",
        content=b'{"x":"' + b"y" * crud_agent_sessions.BODY_BYTES_MAX + b'"}',
        headers={**agent.headers, "content-type": "application/json"},
    )
    assert huge.status_code == 413


def test_the_agent_settings_are_set(agent: Agent) -> None:
    session = _start(agent)
    agent.post(
        f"/sessions/{session['id']}/agent", {"model": "claude-x", "effort": "high", "type": "a-b"}
    )
    row = agent.get(f"/sessions/{session['id']}")["session"]
    assert (row["agent_model"], row["agent_effort"], row["agent_type"]) == (
        "claude-x",
        "high",
        "a-b",
    )
    bad = agent.refused("POST", f"/sessions/{session['id']}/agent", {"model": "a b"})
    assert bad.status_code == 422


# --- the list ----------------------------------------------------------------------------


def test_a_principal_lists_what_it_owns_made_or_is_offered(agent: Agent, tables: PgPool) -> None:
    mine = _start(agent, TOKEN)
    other = _start(agent, OTHER)
    offered = _start(agent, OTHER)
    agent.post(
        f"/sessions/{offered['id']}/offer",
        {"owner": {"kind": "bearer", "id": "token:7"}, "to": TOKEN, "ttl_ms": 60_000},
    )
    listed = agent.post("/sessions/list", {"principal": {"kind": "bearer", "id": "token:42"}})
    assert {s["id"] for s in listed["sessions"]} == {mine["id"], offered["id"]}
    everything = agent.post("/sessions/list", {"principal": None})["sessions"]
    assert {mine["id"], other["id"], offered["id"]} <= {s["id"] for s in everything}
    # Newest first.
    _sql(
        tables,
        "UPDATE ai_sessions SET updated_at = now() + interval '1 hour' WHERE id = %s",
        (mine["id"],),
    )
    assert agent.post("/sessions/list", {"principal": None})["sessions"][0]["id"] == mine["id"]


def test_the_list_filters(agent: Agent, tables: PgPool) -> None:
    a = _start(agent, title="Bracket 50% off")
    b = _start(agent, title="Other")
    _sql(tables, "UPDATE ai_sessions SET archived_at = now() WHERE id = %s", (b["id"],))
    _sql(
        tables,
        "INSERT INTO ai_session_resources (session_id, tool, resource_type, resource_id, action,"
        " model_slug) VALUES (%s, 'render_model', 'output', 'out-1', 'created', 'gear')",
        (a["id"],),
    )
    _sql(
        tables,
        "INSERT INTO ai_session_files (workspace, session_id, path, media_type, data, sha256, size)"
        " VALUES (%s, %s, 'cad/part.stl', 'model/stl', 'x', %s, 1)",
        (f"session:{b['id']}", b["id"], "0" * 64),
    )

    def ids(**filters: Any) -> set[str]:
        return {
            s["id"]
            for s in agent.post("/sessions/list", {"principal": None, **filters})["sessions"]
        }

    assert a["id"] in ids() and b["id"] not in ids()
    assert ids(archived="only") == {b["id"]}
    assert {a["id"], b["id"]} <= ids(archived="include")
    assert ids(resource={"type": "model", "id": "gear"}) == {a["id"]}
    assert ids(resource={"type": "output", "id": "out-1"}) == {a["id"]}
    assert ids(search="50%") == {a["id"]}
    assert ids(search="0_") == set()
    assert ids(archived="include", search="part") == {b["id"]}
    assert ids(archived="include", with_file=True, accept=[".stl"]) == {b["id"]}
    assert ids(archived="include", with_file=True, accept=["model/*"]) == {b["id"]}
    assert ids(archived="include", with_file=True, accept=["image/png"]) == set()
    # As many entries as a picker's 1000-character ``accept`` can hold (``.a,`` each).
    many = [f".{n}" for n in range(333)] + [".stl"]
    assert ids(archived="include", with_file=True, accept=many) == {b["id"]}
    assert ids(status="running") & {a["id"], b["id"]} == set()
    assert ids(origin="mcp") & {a["id"], b["id"]} == set()
    assert (
        len(
            agent.post("/sessions/list", {"principal": None, "archived": "include", "limit": 1})[
                "sessions"
            ]
        )
        == 1
    )
    for bad in ({"limit": 501}, {"search": "x" * 501}, {"accept": ["x"] * 501}, {"status": "lost"}):
        assert (
            agent.refused("POST", "/sessions/list", {"principal": None, **bad}).status_code == 422
        )


def test_the_list_query_matches_the_agents(tables: PgPool) -> None:
    text, params = sessions.list_query(
        ("bearer", "token:42"), search="a", accept=[".stl", "image/*", "model/stl"], with_file=True
    )
    assert params["limit"] == 100 and params["search"] == "%a%"
    assert params["accept0"] == "%.stl" and params["accept1"] == "image/%"
    with tables.connection() as conn:
        conn.execute(f"EXPLAIN {text}".encode(), params)


# --- the handoff -------------------------------------------------------------------------


def test_an_offer_applies_only_while_the_owner_holds(agent: Agent) -> None:
    s = _start(agent, TOKEN)
    path = f"/sessions/{s['id']}"
    stale = agent.post(
        f"{path}/offer", {"owner": {"kind": "bearer", "id": "token:7"}, "to": OTHER, "ttl_ms": 1000}
    )
    assert stale == {"changed": False}
    made = agent.post(
        f"{path}/offer",
        {"owner": {"kind": "bearer", "id": "token:42"}, "to": OTHER, "ttl_ms": 60_000},
    )
    assert made == {"changed": True}
    row = agent.get(path)["session"]
    assert (row["offer_kind"], row["offer_id"], row["offer_label"]) == (
        "bearer",
        "token:7",
        "Other",
    )
    assert row["offer_until"].endswith("Z")
    # Only the live offer to that principal is cancelled.
    assert agent.post(f"{path}/offer/cancel", {"to": {"kind": "bearer", "id": "token:42"}}) == {
        "changed": False
    }
    assert agent.post(f"{path}/offer/cancel", {"to": {"kind": "bearer", "id": "token:7"}}) == {
        "changed": True
    }
    assert agent.get(path)["session"]["offer_kind"] is None


def test_an_expired_offer_reads_as_none_and_cannot_be_accepted(
    agent: Agent, tables: PgPool
) -> None:
    s = _start(agent, TOKEN)
    path = f"/sessions/{s['id']}"
    agent.post(
        f"{path}/offer",
        {"owner": {"kind": "bearer", "id": "token:42"}, "to": OTHER, "ttl_ms": 60_000},
    )
    _sql(
        tables,
        "UPDATE ai_sessions SET pending_owner_until = now() - interval '1 s' WHERE id = %s",
        (s["id"],),
    )
    assert agent.get(path)["session"]["offer_kind"] is None
    accept = {"owner": {"kind": "bearer", "id": "token:42"}, "to": OTHER, "accepting": True}
    assert agent.post(f"{path}/owner", accept) == {"changed": False}


def test_a_transfer_applies_once(agent: Agent) -> None:
    s = _start(agent, TOKEN)
    path = f"/sessions/{s['id']}"
    agent.post(
        f"{path}/offer",
        {"owner": {"kind": "bearer", "id": "token:42"}, "to": OTHER, "ttl_ms": 60_000},
    )
    accept = {"owner": {"kind": "bearer", "id": "token:42"}, "to": OTHER, "accepting": True}
    assert agent.post(f"{path}/owner", accept) == {"changed": True}
    assert agent.post(f"{path}/owner", accept) == {"changed": False}
    row = agent.get(path)["session"]
    assert (row["owner_kind"], row["owner_id"], row["owner_label"], row["offer_kind"]) == (
        "bearer",
        "token:7",
        "Other",
        None,
    )
    # A plain transfer needs no offer, only the owner it read.
    back = {"owner": {"kind": "bearer", "id": "token:7"}, "to": YOU}
    assert agent.post(f"{path}/owner", back) == {"changed": True}


# --- the budget --------------------------------------------------------------------------


def test_a_raise_goes_to_the_lineages_root_and_stops_at_the_cap(
    agent: Agent, tables: PgPool
) -> None:
    root = _start(agent)
    fork = _start(agent, parent_id=root["id"], budget_of=root["id"])
    by = {"kind": "browser", "id": "browser"}
    raised = agent.post(
        f"/sessions/{fork['id']}/budget", {"by": by, "add_usd": 2.5, "max_usd": 100}
    )
    assert raised == {"budget_usd": 7.5}
    assert _sql(tables, "SELECT budget_usd FROM ai_sessions WHERE id = %s", (root["id"],)) == [
        {"budget_usd": 7.5}
    ]
    assert agent.get(f"/sessions/{fork['id']}")["session"]["budget_usd"] == 7.5
    capped = agent.post(f"/sessions/{fork['id']}/budget", {"by": by, "add_usd": 93, "max_usd": 100})
    assert capped == {"budget_usd": None}
    stranger = _start(agent, TOKEN)
    refused = agent.post(
        f"/sessions/{stranger['id']}/budget", {"by": by, "add_usd": 1, "max_usd": 100}
    )
    assert refused == {"budget_usd": None}
    for bad in ({"by": by, "add_usd": 0, "max_usd": 100}, {"by": by, "add_usd": 1, "max_usd": 101}):
        assert agent.refused("POST", f"/sessions/{root['id']}/budget", bad).status_code == 422


# --- a failed fork -----------------------------------------------------------------------


def test_only_a_session_that_logged_nothing_is_discarded(agent: Agent, tables: PgPool) -> None:
    fork = _start(agent)
    sid = fork["id"]
    _sql(
        tables,
        "INSERT INTO ai_session_entries (project_key, session_id, subpath, entry)"
        " VALUES ('', %s, '', '{}')",
        (sid,),
    )
    _sql(
        tables,
        "INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES (%s, '\\x00', 'k')",
        (f"session-{sid}",),
    )
    assert agent.post(f"/sessions/{sid}/discard") == {"discarded": True}
    assert agent.get(f"/sessions/{sid}") == {"session": None}
    assert _sql(tables, "SELECT 1 FROM ai_session_entries WHERE session_id = %s", (sid,)) == []
    assert (
        _sql(tables, "SELECT 1 FROM ai_payload_keys WHERE subject = %s", (f"session-{sid}",)) == []
    )
    logged = _start(agent)
    _sql(tables, "UPDATE ai_sessions SET event_seq = 1 WHERE id = %s", (logged["id"],))
    assert agent.post(f"/sessions/{logged['id']}/discard") == {"discarded": False}
    assert agent.get(f"/sessions/{logged['id']}")["session"] is not None
    assert agent.post(f"/sessions/{uuid.uuid4()}/discard") == {"discarded": False}


# --- resources ---------------------------------------------------------------------------


def _touch(**extra: Any) -> dict[str, Any]:
    return {
        "tool": "render_model",
        "resource_type": "output",
        "resource_id": "out-1",
        "action": "created",
        "model_slug": "gear",
        "before_id": None,
        "after_id": None,
        **extra,
    }


def test_resources_are_recorded_and_listed_oldest_first(agent: Agent) -> None:
    s = _start(agent)
    path = f"/sessions/{s['id']}/resources"
    rows = [_touch(), _touch(resource_type="unclassified", resource_id=None, action="modified")]
    assert agent.post(path, {"rows": rows}) == {"recorded": True}
    listed = agent.get(path)["resources"]
    assert [(r["resource_type"], r["resource_id"]) for r in listed] == [
        ("output", "out-1"),
        ("unclassified", None),
    ]
    assert listed[0]["at"].endswith("Z") and listed[0]["tool"] == "render_model"
    assert agent.post(f"/sessions/{uuid.uuid4()}/resources", {"rows": [_touch()]}) == {
        "recorded": False
    }
    assert agent.get(f"/sessions/{uuid.uuid4()}/resources") == {"resources": []}


def test_a_resource_write_is_bounded(agent: Agent) -> None:
    s = _start(agent)
    path = f"/sessions/{s['id']}/resources"
    cases: list[dict[str, Any]] = [
        {"rows": []},
        {"rows": [_touch()] * (crud_agent_sessions.RESOURCES_MAX + 1)},
        {"rows": [_touch(resource_id="x" * 301)]},
        {"rows": [_touch(resource_type="secret")]},
        {"rows": [_touch(resource_type="unclassified")]},
        {"rows": [_touch(resource_id=None)]},
        {"rows": [_touch(action="read")]},
    ]
    for body in cases:
        assert agent.refused("POST", path, body).status_code == 422, body
    assert agent.get(path) == {"resources": []}
