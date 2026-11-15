"""A session's transcript through CRUD routes (#5299 step 9, part b,
`api/crud_agent_transcripts.py`): only the ``agent`` credential reaches them, each route is the
agent's SQL as one transaction (`agent_data/transcripts.py`), the API seals and opens every
entry under the session's data key with the agent's AAD, an append is refused once the session
is forgotten and fenced on the turn's placement, and a fork's transcript is read through its
chain in one snapshot, never past an ancestor's cut.

The agent's tables are made from its own migration files, as the API applies them at its
start (#5299 D6)."""

from __future__ import annotations

import base64
import json
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.agent_data import transcripts
from scadbuddy.agent_data.payload_keys import key_in
from scadbuddy.api import crud_agent_transcripts, crud_areas
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
from scadbuddy.core.secrets import kek_from_base64, open_bytes, seal_bytes
from scadbuddy.render.agent_migrations import migrate_agent
from scadbuddy.workflows.payload_codec import payload_key_lock
from tests.conftest import PgPool
from tests.crud.crud_support import _NoSessions, _Noted

pytestmark = pytest.mark.requires_postgres

BASE = "/api/v1/internal/crud/agent"
KEK_B64 = base64.b64encode(b"\x41" * 32).decode()
KEK = kek_from_base64(KEK_B64)


def _app(pool: PgPool, tmp_path: Path, *, kek: str | None = KEK_B64) -> FastAPI:
    credentials = ServiceCredentialStore(pool)
    parts: dict[Any, Any] = {
        AUTH: SimpleNamespace(
            would_refuse=_Noted(),
            verifiers={},
            credentials=credentials,
            core=SimpleNamespace(metrics=Metrics()),
        ),
    }
    key_file = None
    if kek is not None:
        key_file = tmp_path / f"kek-{uuid.uuid4().hex}"
        key_file.write_text(kek)
    app = FastAPI(dependencies=[Depends(auth_check)])
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(
            auth_mode="enforce", secret_key_file=key_file, secret_key_previous_file=None
        ),
        components=SimpleNamespace(get=lambda key: parts[key]),
        settings_store=SimpleNamespace(pool=pool),
    )
    app.include_router(crud_agent_transcripts.router, prefix="/api/v1")
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

    def append(self, sid: str, entries: list[Any], subpath: str = "", **extra: Any) -> Any:
        texts = [json.dumps(e) for e in entries]
        return self.post(
            f"/sessions/{sid}/entries",
            {"project_key": "p", "subpath": subpath, "entries": texts, **extra},
        )

    def load(self, sid: str) -> list[Any] | None:
        entries = self.get(f"/sessions/{sid}/transcript")["entries"]
        return None if entries is None else [json.loads(t) for t in entries]


def _sql(pool: PgPool, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(query.encode(), params)
        return list(cur.fetchall()) if cur.description else []


def _session(
    pool: PgPool, *, parent: str | None = None, cut: int | None = None, **cols: Any
) -> str:
    sid = str(uuid.uuid4())
    _sql(
        pool,
        "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
        " creator_id, status, max_turns, budget_usd, fork_parent_id, fork_cut, turn_attempt,"
        " durable_turn_id) VALUES (%s, 'chat', 'browser', 'browser', 'You', 'browser',"
        " 'browser', 'idle', 10, 1, %s, %s, %s, %s)",
        (sid, parent, cut, cols.get("attempt"), cols.get("turn")),
    )
    return sid


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


@pytest.fixture
def agent(tables: PgPool, tmp_path: Path) -> Iterator[Agent]:
    app = _app(tables, tmp_path)
    with TestClient(app) as client:
        yield Agent(client, _token(app))


def _entries(n: int, tag: str) -> list[dict[str, Any]]:
    return [{"type": "user", "uuid": f"{tag}-{i}", "message": f"{tag} {i}"} for i in range(n)]


def test_only_the_agent_credential_reaches_the_routes(tables: PgPool, tmp_path: Path) -> None:
    app = _app(tables, tmp_path)
    sid = _session(tables)
    with TestClient(app) as client:
        assert client.get(f"{BASE}/sessions/{sid}/transcript").status_code == 401
        for caller in ("render", "print", "projects"):
            wrong = client.get(f"{BASE}/sessions/{sid}/transcript", headers=_token(app, caller))
            assert wrong.status_code == 403
        listed = client.get(f"{BASE}/areas", headers=_token(app)).json()["areas"]
        assert "transcripts" in listed


def test_an_append_is_sealed_with_the_agents_aad_and_loads_deep_equal(
    agent: Agent, tables: PgPool
) -> None:
    sid = _session(tables)
    entries = [
        {"type": "user", "uuid": "u1", "cwd": "/w/a", "message": "nul \u0000 and é and 🙂"},
        {"type": "last-prompt", "text": "no uuid"},
        {"type": "Not A Token", "uuid": "u2"},
    ]
    assert agent.append(sid, entries) == {"written": True}
    assert agent.load(sid) == entries
    rows = _sql(
        tables,
        "SELECT id, uuid, entry, entry_type, sealed FROM ai_session_entries"
        " WHERE session_id = %s ORDER BY id",
        (sid,),
    )
    assert [r["entry_type"] for r in rows] == ["user", "last-prompt", None]
    assert all(r["entry"] is None and r["sealed"] is not None for r in rows)
    with tables.connection() as conn:
        dek, _made = key_in(
            conn, f"session-{sid}", create=False, standing=False, kek=KEK, previous=None
        )
    assert dek is not None
    first = open_bytes(dek, bytes(rows[0]["sealed"]), f"ai_session_entries:{sid}::u1")
    assert json.loads(first) == entries[0]
    second = rows[1]
    assert (
        json.loads(
            open_bytes(dek, bytes(second["sealed"]), f"ai_session_entries:{sid}::#{second['id']}")
        )
        == entries[1]
    )


def test_a_uuid_is_written_once_and_a_row_without_one_each_time(
    agent: Agent, tables: PgPool
) -> None:
    sid = _session(tables)
    agent.append(sid, [{"type": "user", "uuid": "u1"}, {"type": "x"}])
    agent.append(sid, [{"type": "user", "uuid": "u1"}, {"type": "x"}])
    assert agent.load(sid) == [{"type": "user", "uuid": "u1"}, {"type": "x"}, {"type": "x"}]


def test_rows_written_by_the_agent_and_before_sealing_load_too(
    agent: Agent, tables: PgPool
) -> None:
    sid = _session(tables)
    agent.append(sid, [{"type": "user", "uuid": "a"}])
    _sql(
        tables,
        "INSERT INTO ai_session_entries (project_key, session_id, subpath, uuid, entry)"
        " VALUES ('p', %s, '', 'legacy', %s)",
        (sid, json.dumps({"type": "user", "uuid": "legacy"})),
    )
    with tables.connection() as conn:
        dek, _made = key_in(
            conn, f"session-{sid}", create=False, standing=False, kek=KEK, previous=None
        )
    assert dek is not None
    text = json.dumps({"type": "assistant", "uuid": "agent"})
    _sql(
        tables,
        "INSERT INTO ai_session_entries (project_key, session_id, subpath, uuid, entry_type,"
        " sealed) VALUES ('p', %s, '', 'agent', 'assistant', %s)",
        (sid, seal_bytes(dek, text.encode(), f"ai_session_entries:{sid}::agent")),
    )
    assert [e["uuid"] for e in agent.load(sid) or []] == ["a", "legacy", "agent"]


def test_the_fence_holds_the_append_to_its_placement(agent: Agent, tables: PgPool) -> None:
    turn = str(uuid.uuid4())
    sid = _session(tables, attempt="run:1", turn=turn)
    fence = {"attempt": "run:1", "turn_id": turn}
    assert agent.append(sid, [{"type": "user", "uuid": "a"}], fence=fence) == {"written": True}
    stale = {"attempt": "run:0", "turn_id": turn}
    assert agent.append(sid, [{"type": "user", "uuid": "b"}], fence=stale) == {"written": False}
    other = {"attempt": "run:1", "turn_id": str(uuid.uuid4())}
    assert agent.append(sid, [{"type": "user", "uuid": "c"}], fence=other) == {"written": False}
    # Between turns the last placement still writes (what Claude Code writes as it exits).
    _sql(tables, "UPDATE ai_sessions SET durable_turn_id = NULL WHERE id = %s", (sid,))
    assert agent.append(sid, [{"type": "x", "uuid": "d"}], fence=other) == {"written": True}
    assert [e["uuid"] for e in agent.load(sid) or []] == ["a", "d"]


def test_a_forgotten_session_is_never_written_and_reads_as_forgotten(
    agent: Agent, tables: PgPool
) -> None:
    sid = _session(tables)
    agent.append(sid, [{"type": "user", "uuid": "a"}])
    _sql(tables, "INSERT INTO ai_forgotten_subjects (subject) VALUES (%s)", (f"session-{sid}",))
    refused = agent.status(
        "POST",
        f"/sessions/{sid}/entries",
        {"project_key": "p", "subpath": "", "entries": [json.dumps({"type": "x"})]},
    )
    assert refused.status_code == 410 and refused.json()["reason"] == "forgotten"
    _sql(tables, "DELETE FROM ai_payload_keys WHERE subject = %s", (f"session-{sid}",))
    gone = agent.status("GET", f"/sessions/{sid}/transcript")
    assert gone.status_code == 410
    assert len(_sql(tables, "SELECT 1 FROM ai_session_entries WHERE session_id = %s", (sid,))) == 1


def test_an_append_waits_for_a_forget_in_flight(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    agent.append(sid, [{"type": "user", "uuid": "a"}])
    held = threading.Event()
    release = threading.Event()

    def forget() -> None:
        with tables.connection() as conn, conn.transaction():
            subject = f"session-{sid}"
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (payload_key_lock(subject),))
            conn.execute("INSERT INTO ai_forgotten_subjects (subject) VALUES (%s)", (subject,))
            held.set()
            release.wait(10)

    thread = threading.Thread(target=forget)
    thread.start()
    held.wait(10)
    answers: list[int] = []

    def append() -> None:
        body = {"project_key": "p", "subpath": "", "entries": [json.dumps({"type": "x"})]}
        answers.append(agent.status("POST", f"/sessions/{sid}/entries", body).status_code)

    appending = threading.Thread(target=append)
    appending.start()
    appending.join(0.5)
    assert answers == []
    release.set()
    thread.join()
    appending.join(10)
    assert answers == [410]


def test_no_kek_is_unavailable(tables: PgPool, tmp_path: Path) -> None:
    app = _app(tables, tmp_path, kek=None)
    sid = _session(tables)
    with TestClient(app) as client:
        body = {"project_key": "p", "subpath": "", "entries": [json.dumps({"type": "x"})]}
        refused = client.post(f"{BASE}/sessions/{sid}/entries", json=body, headers=_token(app))
        assert refused.status_code == 503 and refused.json()["reason"] == "unavailable"


def test_a_fork_reads_its_parents_transcript_through_the_cut(agent: Agent, tables: PgPool) -> None:
    parent = _session(tables)
    agent.append(parent, _entries(5, "p"))
    child = _session(tables, parent=parent, cut=3)
    agent.append(child, _entries(2, "c"))
    grand = _session(tables, parent=child, cut=4)
    agent.append(grand, _entries(1, "g"))

    def uuids(sid: str) -> list[str]:
        return [e["uuid"] for e in agent.load(sid) or []]

    assert uuids(child) == ["p-0", "p-1", "p-2", "c-0", "c-1"]
    assert uuids(grand) == ["p-0", "p-1", "p-2", "c-0", "g-0"]
    # The parent's rows written past the cut are never the child's.
    agent.append(parent, _entries(1, "late"))
    assert uuids(child) == ["p-0", "p-1", "p-2", "c-0", "c-1"]
    with tables.connection() as conn:
        rows = transcripts.segments(conn, child)
    assert [len(r) for _owner, r in rows] == [3, 2]
    # A fork has a transcript to resume, its own and its parent's.
    head = agent.get(f"/sessions/{child}/transcript/head")
    assert head["exists"] is True


def test_rows_a_forget_copied_in_are_read_first(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    agent.append(sid, [{"type": "user", "uuid": "own"}])
    with tables.connection() as conn:
        dek, _made = key_in(
            conn, f"session-{sid}", create=False, standing=False, kek=KEK, previous=None
        )
    assert dek is not None
    text = json.dumps({"type": "user", "uuid": "copied"})
    _sql(
        tables,
        "INSERT INTO ai_session_entries (project_key, session_id, subpath, uuid, entry_type,"
        " sealed, inherited) VALUES ('p', %s, '', 'copied', 'user', %s, true)",
        (sid, seal_bytes(dek, text.encode(), f"ai_session_entries:{sid}::copied")),
    )
    assert [e["uuid"] for e in agent.load(sid) or []] == ["copied", "own"]


def test_an_ancestor_forgotten_mid_read_is_read_again(agent: Agent, tables: PgPool) -> None:
    parent = _session(tables)
    agent.append(parent, _entries(2, "p"))
    child = _session(tables, parent=parent, cut=2)
    _sql(tables, "DELETE FROM ai_payload_keys WHERE subject = %s", (f"session-{parent}",))
    assert agent.status("GET", f"/sessions/{child}/transcript").status_code == 410


def test_the_reads(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    agent.append(
        sid,
        [
            {"type": "user", "uuid": "a", "cwd": "/w/one"},
            {"type": "cost-state", "usd": 1},
            {"type": "assistant", "uuid": "b", "cwd": "/w/two"},
            {"type": "cost-state", "usd": 2},
        ],
    )
    agent.append(sid, [{"type": "user", "uuid": "s1"}], subpath="subagents/agent-1")
    assert agent.get(f"/sessions/{sid}/entries/last", type="cost-state") == {
        "entry": json.dumps({"type": "cost-state", "usd": 2})
    }
    assert agent.get(f"/sessions/{sid}/entries/last", type="nothing") == {"entry": None}
    assert agent.get(f"/sessions/{sid}/cwd") == {"cwd": "/w/two"}
    assert agent.get(f"/sessions/{sid}/subkeys") == {"subkeys": ["subagents/agent-1"]}
    sub = agent.get(f"/sessions/{sid}/entries", subpath="subagents/agent-1")["entries"]
    assert [json.loads(t)["uuid"] for t in sub] == ["s1"]
    assert agent.get(f"/sessions/{sid}/entries", subpath="none") == {"entries": None}
    listed = agent.get("/transcripts", project_key="p")["sessions"]
    assert [s["session_id"] for s in listed] == [sid] and listed[0]["mtime"] > 0
    head = agent.get(f"/sessions/{sid}/transcript/head")
    assert head["exists"] is True and head["since"] is not None
    since = head["since"]
    assert agent.post(f"/sessions/{sid}/transcript/moved", {"since": since, "own": []}) == {
        "moved": False
    }
    agent.append(sid, [{"type": "user", "uuid": "other"}])
    moved = agent.post(f"/sessions/{sid}/transcript/moved", {"since": since, "own": []})
    assert moved == {"moved": True}
    mine = agent.post(f"/sessions/{sid}/transcript/moved", {"since": since, "own": ["other"]})
    assert mine == {"moved": False}
    assert agent.get(f"/sessions/{sid}/entries/landed", after=int(since)) == {"landed": True}
    last = _sql(tables, "SELECT max(id) AS m FROM ai_session_entries WHERE session_id = %s", (sid,))
    assert agent.get(f"/sessions/{sid}/entries/landed", after=last[0]["m"]) == {"landed": False}
    empty = _session(tables)
    assert agent.get(f"/sessions/{empty}/transcript") == {"entries": None}
    assert agent.get(f"/sessions/{empty}/transcript/head") == {"exists": False, "since": None}
    assert agent.get(f"/sessions/{empty}/cwd") == {"cwd": None}


def test_a_fork_with_no_rows_of_its_own_takes_its_cwd_through_the_chain(
    agent: Agent, tables: PgPool
) -> None:
    parent = _session(tables)
    agent.append(parent, [{"type": "user", "uuid": "a", "cwd": "/w/parent"}])
    child = _session(tables, parent=parent, cut=1)
    assert agent.get(f"/sessions/{child}/cwd") == {"cwd": "/w/parent"}
    assert agent.get(f"/sessions/{child}/transcript/head") == {"exists": True, "since": None}


def test_a_delete_takes_a_subpath_or_everything(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    agent.append(sid, _entries(1, "m"))
    agent.append(sid, _entries(1, "s"), subpath="subagents/a")
    agent.post(f"/sessions/{sid}/entries/delete", {"subpath": "subagents/a"})
    assert agent.get(f"/sessions/{sid}/subkeys") == {"subkeys": []}
    assert agent.load(sid) is not None
    agent.post(f"/sessions/{sid}/entries/delete", {"subpath": None})
    assert agent.load(sid) is None


def test_the_bodies_and_ids_are_bounded(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    path = f"/sessions/{sid}/entries"

    def body(**extra: Any) -> dict[str, Any]:
        return {"project_key": "p", "subpath": "", "entries": ['{"type":"x"}'], **extra}

    for bad in (
        body(entries=[]),
        body(entries=['{"type":"x"}'] * (crud_agent_transcripts.ENTRIES_MAX + 1)),
        body(entries=["not json"]),
        body(subpath="x" * (crud_agent_transcripts.SUBPATH_MAX + 1)),
        body(fence={"attempt": "", "turn_id": "t"}),
        body(extra=1),
    ):
        assert agent.status("POST", path, bad).status_code == 422, bad
    huge = agent.client.post(
        f"{BASE}{path}",
        content=b'{"x":"' + b"y" * crud_agent_transcripts.BODY_BYTES_MAX + b'"}',
        headers={**agent.headers, "content-type": "application/json"},
    )
    assert huge.status_code == 413
    too_many = {"since": None, "own": ["u"] * (crud_agent_transcripts.OWN_MAX + 1)}
    assert agent.status("POST", f"/sessions/{sid}/transcript/moved", too_many).status_code == 422
    assert agent.status("GET", f"/sessions/{sid}/entries/last", type="Bad Type").status_code == 422
    for bad_id in ("not-a-uuid", "..", "%2E%2E"):
        assert agent.status("GET", f"/sessions/{bad_id}/transcript").status_code in (404, 422)
    assert _sql(tables, "SELECT 1 FROM ai_session_entries WHERE session_id = %s", (sid,)) == []
