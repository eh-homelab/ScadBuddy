"""The agent's sweeps and a forget's own deletes through CRUD routes (#6501,
`api/crud_agent_sweeps.py`): only the ``agent`` credential reaches them, the live read answers
the ids with a row, and a forget's rows go only for a session already forgotten, with its
unnamed workspace bytes and its warm-evict NOTIFY."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import crud_agent_sweeps, crud_areas
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
    app.include_router(crud_agent_sweeps.router, prefix="/api/v1")
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


def _sql(pool: PgPool, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(query.encode(), params)
        return list(cur.fetchall()) if cur.description else []


def _session(pool: PgPool) -> str:
    sid = str(uuid.uuid4())
    _sql(
        pool,
        "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
        " creator_id, status, max_turns, budget_usd) VALUES (%s, 'chat', 'browser', 'browser',"
        " 'You', 'browser', 'browser', 'idle', 10, 1)",
        (sid,),
    )
    return sid


def _file(pool: PgPool, sid: str, path: str, data: bytes) -> str:
    sha = hashlib.sha256(data).hexdigest()
    _sql(
        pool,
        "INSERT INTO ai_session_file_bytes (sha256, data) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (sha, data),
    )
    _sql(
        pool,
        "INSERT INTO ai_session_files (workspace, path, session_id, media_type, size, sha256,"
        " bytes_sha256)"
        " VALUES (%s, %s, %s, 'text/plain', %s, %s, %s)",
        (f"session:{sid}", path, sid, len(data), sha, sha),
    )
    return sha


def test_only_the_agent_credential_reaches_the_routes(tables: PgPool) -> None:
    app = _app(tables)
    sid = str(uuid.uuid4())
    with TestClient(app) as c:
        routes = [("/sessions/live", {"ids": [sid]}), (f"/sessions/{sid}/forget-rows", None)]
        for path, body in routes:
            assert c.post(f"{BASE}{path}", json=body).status_code == 401
            for caller in ("render", "print", "projects"):
                wrong = c.post(f"{BASE}{path}", json=body, headers=_token(app, caller))
                assert wrong.status_code == 403
        listed = c.get(f"{BASE}/areas", headers=_token(app)).json()["areas"]
        assert {"session-sweeps", "forget-rows"} <= set(listed)


def test_the_live_ids_are_those_with_a_row(
    client: tuple[TestClient, dict[str, str]], tables: PgPool
) -> None:
    c, headers = client
    here, gone = _session(tables), str(uuid.uuid4())
    got = c.post(f"{BASE}/sessions/live", json={"ids": [here, gone]}, headers=headers)
    assert got.status_code == 200, got.text
    assert got.json() == {"ids": [here]}
    assert c.post(f"{BASE}/sessions/live", json={"ids": []}, headers=headers).json() == {"ids": []}


@pytest.mark.parametrize(
    "ids", [["../areas"], ["not-a-uuid"], [str(uuid.uuid4()).upper()], ["0" * 36]]
)
def test_a_malformed_id_is_refused(
    client: tuple[TestClient, dict[str, str]], ids: list[str]
) -> None:
    c, headers = client
    assert c.post(f"{BASE}/sessions/live", json={"ids": ids}, headers=headers).status_code == 422


def test_a_live_read_is_bounded(client: tuple[TestClient, dict[str, str]]) -> None:
    c, headers = client
    many = {"ids": [str(uuid.uuid4()) for _ in range(1001)]}
    assert c.post(f"{BASE}/sessions/live", json=many, headers=headers).status_code == 422
    # Past the byte cap, the route refuses before it parses the body.
    big = b'{"ids": [], "pad": "' + b"x" * (40 * 1024 * 1024) + b'"}'
    refused = c.post(
        f"{BASE}/sessions/live",
        content=big,
        headers={**headers, "content-type": "application/json"},
    )
    assert refused.status_code == 413


def test_a_session_not_forgotten_keeps_its_rows(
    client: tuple[TestClient, dict[str, str]], tables: PgPool
) -> None:
    c, headers = client
    sid = _session(tables)
    refused = c.post(f"{BASE}/sessions/{sid}/forget-rows", headers=headers)
    assert (refused.status_code, refused.json()["reason"]) == (409, "not-forgotten")
    assert _sql(tables, "SELECT 1 FROM ai_sessions WHERE id = %s", (sid,))


def test_a_forgotten_sessions_rows_go_with_its_unnamed_bytes(
    client: tuple[TestClient, dict[str, str]], tables: PgPool, pg_conninfo: str
) -> None:
    c, headers = client
    sid, other = _session(tables), _session(tables)
    own = _file(tables, sid, "a.txt", b"only mine")
    shared = _file(tables, sid, "b.txt", b"shared")
    _file(tables, other, "b.txt", b"shared")
    _sql(
        tables,
        "INSERT INTO ai_session_entries (project_key, session_id, subpath, entry)"
        " VALUES ('p', %s, '', '{}')",
        (sid,),
    )
    _sql(tables, "INSERT INTO ai_forgotten_subjects (subject) VALUES (%s)", (f"session-{sid}",))
    with psycopg.connect(pg_conninfo, autocommit=True) as listener:
        listener.execute("LISTEN scadbuddy_warm_evict")
        got = c.post(f"{BASE}/sessions/{sid}/forget-rows", headers=headers)
        assert got.status_code == 200, got.text
        heard = [n.payload for n in listener.notifies(timeout=1.0, stop_after=1)]
    assert got.json() == {"rows": 2}
    assert heard == [sid]
    assert not _sql(tables, "SELECT 1 FROM ai_sessions WHERE id = %s", (sid,))
    assert not _sql(tables, "SELECT 1 FROM ai_session_entries WHERE session_id = %s", (sid,))
    kept = {r["sha256"] for r in _sql(tables, "SELECT sha256 FROM ai_session_file_bytes")}
    assert shared in kept and own not in kept
    # Again: nothing left, and still a success.
    assert c.post(f"{BASE}/sessions/{sid}/forget-rows", headers=headers).json() == {"rows": 0}


def test_a_path_id_is_a_uuid(client: tuple[TestClient, dict[str, str]]) -> None:
    c, headers = client
    for sid in ("not-a-uuid", "..%2Fareas", str(uuid.uuid4()).upper()):
        got = c.post(f"{BASE}/sessions/{sid}/forget-rows", headers=headers)
        assert got.status_code in (404, 422)
