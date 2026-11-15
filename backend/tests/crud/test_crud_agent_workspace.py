"""The assistant's workspace files through the agent's CRUD routes (#5299 step 7, area
``workspace``): only the ``agent`` credential reaches them; a path or workspace that is
not one (``../`` among them) is refused before any SQL; a write checks its conflict, caps
and directory clash under the workspace's lock and writes its row, its blob's hold and its
events in one transaction; the API seals each file under a key of its own, wrapped under
the session's payload key or the KEK, and opens it again; on ``store_backend`` local the
bytes stay in Postgres, shared by sha256; a fork's copy, the move to blobs, the sweep, the
reconcile (one at a time) and a key rotation."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.agent_data import rotation
from scadbuddy.agent_data import workspace as ws
from scadbuddy.api import crud_agent_workspace, crud_areas
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
from scadbuddy.core.secrets import kek_from_base64, open_bytes
from scadbuddy.render.agent_migrations import migrate_agent
from scadbuddy.store.content import ContentStore
from scadbuddy.store.index import BlobIndex
from scadbuddy.store.local import LocalContentBackend
from tests.conftest import PgPool
from tests.crud.crud_support import _NoSessions, _Noted

pytestmark = pytest.mark.requires_postgres

BASE = "/api/v1/internal/crud/agent"
KEK_B64 = base64.b64encode(b"\x41" * 32).decode()
OLD_B64 = base64.b64encode(b"\x42" * 32).decode()
KEK = kek_from_base64(KEK_B64)
OLD = kek_from_base64(OLD_B64)


def _app(
    pool: PgPool,
    tmp_path: Path,
    *,
    blobs: bool,
    kek: str | None = KEK_B64,
    previous: str | None = None,
) -> FastAPI:
    def key_file(name: str, text: str | None) -> Path | None:
        if text is None:
            return None
        path = tmp_path / f"{name}-{uuid.uuid4().hex}"
        path.write_text(text)
        return path

    credentials = ServiceCredentialStore(pool)
    parts: dict[Any, Any] = {
        AUTH: SimpleNamespace(
            would_refuse=_Noted(),
            verifiers={},
            credentials=credentials,
            core=SimpleNamespace(metrics=Metrics()),
        ),
    }
    content = (
        ContentStore(LocalContentBackend(tmp_path / "remote"), BlobIndex(pool)) if blobs else None
    )
    app = FastAPI(dependencies=[Depends(auth_check)])
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(
            auth_mode="enforce",
            secret_key_file=key_file("kek", kek),
            secret_key_previous_file=key_file("old", previous),
        ),
        components=SimpleNamespace(get=lambda key: parts[key]),
        settings_store=SimpleNamespace(pool=pool),
        store=SimpleNamespace(content=content, backend="s3" if blobs else "local"),
    )
    app.include_router(crud_agent_workspace.router, prefix="/api/v1")
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
    # The agent's own migration files, as the API applies them at its start (#5299 D6).
    with pg_pool.connection() as conn:
        migrate_agent(conn)
    return pg_pool


def _sql(
    pool: PgPool, query: str, params: tuple[Any, ...] | dict[str, Any] = ()
) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(query.encode(), params)
        return list(cur.fetchall()) if cur.description else []


def _session(pool: PgPool, session: uuid.UUID | None = None) -> str:
    sid = session or uuid.uuid4()
    _sql(
        pool,
        "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
        " creator_id, status, max_turns, budget_usd) VALUES (%s, 'chat', 'browser', 'browser',"
        " 'You', 'browser', 'browser', 'running', 10, 1)",
        (sid,),
    )
    return str(sid)


class Agent:
    def __init__(self, client: TestClient, headers: dict[str, str]) -> None:
        self.client = client
        self.headers = headers

    def get(self, route: str, **params: Any) -> Any:
        return self.client.get(f"{BASE}{route}", params=params, headers=self.headers)

    def post(self, path: str, body: Any) -> Any:
        return self.client.post(f"{BASE}{path}", json=body, headers=self.headers)

    def write(
        self,
        workspace: str,
        path: str,
        data: bytes,
        *,
        expect: str = "any",
        expected: str | None = None,
        events: list[dict[str, Any]] | None = None,
        media_type: str = "application/octet-stream",
        source: dict[str, Any] | None = None,
        **meta: Any,
    ) -> Any:
        body = {
            "workspace": workspace,
            "path": path,
            "media_type": media_type,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "source": source,
            "expect": expect,
            "expected_sha256": expected,
            "events": [json.dumps(e) for e in events or []],
            **meta,
        }
        return self.client.post(
            f"{BASE}/workspace/file-writes",
            headers=self.headers,
            data={"meta": json.dumps(body)},
            files={"file": ("file", data, "application/octet-stream")},
        )

    def read(self, workspace: str, path: str, expected: str | None = None) -> Any:
        params: dict[str, str] = {"workspace": workspace, "path": path}
        if expected is not None:
            params["expected_sha256"] = expected
        return self.client.get(f"{BASE}/workspace/file-bytes", params=params, headers=self.headers)


def _agent(app: FastAPI) -> Iterator[Agent]:
    with TestClient(app) as client:
        yield Agent(client, _token(app))


@pytest.fixture
def local(tables: PgPool, tmp_path: Path) -> Iterator[Agent]:
    yield from _agent(_app(tables, tmp_path, blobs=False))


@pytest.fixture
def blobs(tables: PgPool, tmp_path: Path) -> Iterator[Agent]:
    yield from _agent(_app(tables, tmp_path, blobs=True))


def _event(session: str, path: str, change: str = "uploaded") -> dict[str, Any]:
    return {
        "v": 1,
        "type": "workspace.file",
        "sessionId": session,
        "path": path,
        "change": change,
        "by": {"kind": "browser", "id": "browser", "label": "You"},
    }


def _holds(pool: PgPool) -> set[tuple[str, str]]:
    rows = _sql(pool, "SELECT key, holder_id FROM blob_refs WHERE holder_kind = 'ai_session_file'")
    return {(r["key"], r["holder_id"]) for r in rows}


# --- the guard -----------------------------------------------------------------------


def test_only_the_agent_credential_reaches_the_routes(tables: PgPool, tmp_path: Path) -> None:
    app = _app(tables, tmp_path, blobs=False)
    with TestClient(app) as client:
        assert client.get(f"{BASE}/workspace/status").status_code == 401
        refused = client.get(f"{BASE}/workspace/status", headers=_token(app, "render"))
        assert refused.status_code == 403
        write = client.post(
            f"{BASE}/workspace/file-writes",
            headers=_token(app, "print"),
            data={"meta": "{}"},
            files={"file": ("f", b"x", "application/octet-stream")},
        )
        assert write.status_code == 403
        areas = client.get(f"{BASE}/areas", headers=_token(app)).json()["areas"]
        assert "workspace" in areas


def test_a_path_or_workspace_that_climbs_is_refused(local: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    for path in ("../x", "a/../b", "/etc/passwd", ".hidden", "a//b"):
        answer = local.get("/workspace/file", workspace=f"session:{sid}", path=path)
        assert answer.status_code == 422, path
        assert answer.json()["reason"] == "invalid_path"
        assert local.write(f"session:{sid}", path, b"x").status_code == 422
    for workspace in ("session:../x", f"session:{sid}/..", "principal:root:x", "other"):
        assert local.get("/workspace/files", workspace=workspace).status_code == 422
    assert _sql(tables, "SELECT * FROM ai_session_files") == []


# --- Postgres (store_backend local) -----------------------------------------------------


def test_a_file_kept_in_postgres_is_written_read_listed_and_deleted(
    local: Agent, tables: PgPool
) -> None:
    sid = _session(tables)
    key = f"session:{sid}"
    written = local.write(
        key,
        "./notes/a.txt",
        b"hello",
        media_type="text/plain",
        source={"kind": "upload"},
        expect="absent",
        events=[_event(sid, "notes/a.txt")],
    )
    assert written.status_code == 200, written.text
    body = written.json()
    assert body["file"]["path"] == "notes/a.txt"
    assert body["file"]["source"] == {"kind": "upload"}
    assert len(body["seqs"]) == 1
    row = _sql(tables, "SELECT data, bytes_sha256, blob_key FROM ai_session_files")[0]
    assert row["data"] is None and row["blob_key"] is None
    assert row["bytes_sha256"] == hashlib.sha256(b"hello").hexdigest()
    logged = _sql(tables, "SELECT seq, event FROM ai_session_events WHERE session_id = %s", (sid,))
    assert [json.loads(r["event"])["path"] for r in logged] == ["notes/a.txt"]

    read = local.read(key, "notes/a.txt")
    assert read.status_code == 200 and read.content == b"hello"
    assert read.headers["x-scadbuddy-sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert [f["path"] for f in local.get("/workspace/files", workspace=key).json()["files"]] == [
        "notes/a.txt"
    ]
    assert local.get("/workspace/files", workspace=key, prefix="other").json()["files"] == []
    assert local.get("/workspace/usage", workspace=key).json() == {"files": 1, "bytes": 5}
    listed = local.post("/workspace/session-files", {"session_ids": [sid]}).json()["sessions"]
    assert listed[sid][0]["sealed"] is False
    assert local.read(key, "notes/a.txt", expected="0" * 64).status_code == 409

    gone = local.post(
        "/workspace/file-deletes",
        {
            "workspace": key,
            "path": "notes/a.txt",
            "events": [json.dumps(_event(sid, "notes/a.txt", "deleted"))],
        },
    ).json()
    assert gone["deleted"] is True and len(gone["seqs"]) == 1
    # Its bytes went with the last file that named them.
    assert _sql(tables, "SELECT * FROM ai_session_file_bytes") == []
    assert local.read(key, "notes/a.txt").status_code == 404
    again = local.post("/workspace/file-deletes", {"workspace": key, "path": "notes/a.txt"})
    assert again.json() == {"deleted": False, "seqs": []}


def test_a_write_is_refused_on_conflict_caps_and_a_directory_clash(
    local: Agent, tables: PgPool
) -> None:
    sid = _session(tables)
    key = f"session:{sid}"
    first = local.write(key, "a.txt", b"one").json()["file"]
    refused = local.write(key, "a.txt", b"two", expect="absent")
    assert (refused.status_code, refused.json()["reason"]) == (409, "conflict")
    stale = local.write(key, "a.txt", b"two", expect="sha256", expected="0" * 64)
    assert stale.json()["reason"] == "conflict"
    assert (
        local.write(key, "a.txt", b"two", expect="sha256", expected=first["sha256"]).status_code
        == 200
    )
    assert local.write(key, "a.txt/b", b"x").json()["reason"] == "conflict"
    assert local.write(key, "dir/b", b"x").status_code == 200
    assert local.write(key, "dir", b"x").json()["reason"] == "conflict"
    _sql(
        tables,
        "INSERT INTO ai_settings (key, value) VALUES ('workspace_file_max_bytes', '4'::jsonb),"
        " ('workspace_max_bytes', '6'::jsonb)",
    )
    too_large = local.write(key, "big", b"12345")
    assert (too_large.status_code, too_large.json()["reason"]) == (413, "too_large")
    quota = local.write(key, "more", b"1234")
    assert (quota.status_code, quota.json()["reason"]) == (409, "quota")
    # The sha256 the meta names is the bytes'.
    lying = local.write(key, "c", b"x", sha256="0" * 64)
    assert lying.status_code == 422


def test_an_event_of_another_session_or_kind_is_refused(local: Agent, tables: PgPool) -> None:
    sid, other = _session(tables), _session(tables)
    key = f"session:{sid}"
    assert local.write(key, "a", b"x", events=[_event(other, "a")]).status_code == 422
    assert (
        local.write(key, "a", b"x", events=[{**_event(sid, "a"), "type": "turn.done"}]).status_code
        == 422
    )
    principal = "principal:bearer:token:1"
    assert local.write(principal, "a", b"x", events=[_event(sid, "a")]).status_code == 422
    assert _sql(tables, "SELECT * FROM ai_session_events") == []


def test_the_body_is_bounded(local: Agent, tables: PgPool, monkeypatch: pytest.MonkeyPatch) -> None:
    sid = _session(tables)
    monkeypatch.setattr(ws, "FILE_BYTES_CEILING", 10)
    answer = local.write(f"session:{sid}", "a", b"x" * 20 * 1024)
    assert answer.status_code == 413
    assert _sql(tables, "SELECT * FROM ai_session_files") == []


def test_a_fork_shares_the_bytes_kept_in_postgres(local: Agent, tables: PgPool) -> None:
    parent, child = _session(tables), _session(tables)
    local.write(f"session:{parent}", "a.txt", b"shared")
    assert local.post(
        "/workspace/copies", {"from_session": parent, "to_session": child}
    ).json() == {"copied": 1}
    assert local.read(f"session:{child}", "a.txt").content == b"shared"
    assert len(_sql(tables, "SELECT * FROM ai_session_file_bytes")) == 1


def test_the_sweep_takes_principals_expired_files(local: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    local.write("principal:bearer:token:1", "old", b"x")
    local.write(f"session:{sid}", "kept", b"y")
    _sql(tables, "UPDATE ai_session_files SET updated_at = now() - interval '2 days'")
    assert local.post("/workspace/sweeps", {}).json() == {"swept": 1}
    assert [r["path"] for r in _sql(tables, "SELECT path FROM ai_session_files")] == ["kept"]


def test_status_says_postgres_on_store_backend_local(local: Agent) -> None:
    status = local.get("/workspace/status").json()
    assert status["store"] == "postgres"
    assert local.post("/workspace/blob-moves", {}).json() == {
        "moved": 0,
        "local": True,
        "more": False,
    }


# --- sealed blobs -----------------------------------------------------------------------


def test_a_session_file_is_sealed_held_and_opened_again(blobs: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    key = f"session:{sid}"
    # Past one segment, so it is sealed and opened in three.
    data = os.urandom(ws.SEGMENT_BYTES * 2 + 17)
    written = blobs.write(key, "big.bin", data)
    assert written.status_code == 200, written.text
    row = _sql(
        tables, "SELECT blob_key, key_sealed, key_kek_id, data, bytes_sha256 FROM ai_session_files"
    )[0]
    assert row["blob_key"].startswith("workspace-") and row["key_kek_id"] is None
    assert row["data"] is None and row["bytes_sha256"] is None
    assert _holds(tables) == {(row["blob_key"], f"{key}/big.bin")}
    # The file key is wrapped under the session's payload key, which the API made.
    assert _sql(tables, "SELECT subject FROM ai_payload_keys") == [{"subject": f"session-{sid}"}]
    assert blobs.read(key, "big.bin").content == data
    assert blobs.read(key, "big.bin", expected=hashlib.sha256(data).hexdigest()).content == data
    listed = blobs.post("/workspace/session-files", {"session_ids": [sid]}).json()["sessions"]
    assert listed[sid][0]["sealed"] is True

    replaced = blobs.write(key, "big.bin", b"small")
    assert replaced.status_code == 200
    new = _sql(tables, "SELECT blob_key FROM ai_session_files")[0]["blob_key"]
    assert _holds(tables) == {(new, f"{key}/big.bin")}
    blobs.post("/workspace/file-deletes", {"workspace": key, "path": "big.bin"})
    assert _holds(tables) == set()


def test_a_principal_file_is_wrapped_under_the_kek_and_rewrapped_at_rotation(
    tables: PgPool, tmp_path: Path
) -> None:
    principal = "principal:oidc:https://idp.example/#sub"
    for agent in _agent(_app(tables, tmp_path, blobs=True, kek=OLD_B64)):
        assert agent.write(principal, "a.txt", b"before").status_code == 200
    row = _sql(tables, "SELECT key_kek_id FROM ai_session_files")[0]
    assert row["key_kek_id"] == OLD.id
    with tables.connection() as conn:
        out = rotation.rotate(conn, OLD, KEK)
    assert out["session_files"] == (1, 0)
    for agent in _agent(_app(tables, tmp_path, blobs=True)):
        assert agent.read(principal, "a.txt").content == b"before"


def test_without_a_kek_a_write_is_refused_and_nothing_is_stored(
    tables: PgPool, tmp_path: Path
) -> None:
    for agent in _agent(_app(tables, tmp_path, blobs=True, kek=None)):
        sid = _session(tables)
        status = agent.get("/workspace/status").json()
        assert status["store"] == "refused" and "SCADBUDDY_SECRET_KEY_FILE" in status["reason"]
        refused = agent.write(f"session:{sid}", "a", b"x")
        assert (refused.status_code, refused.json()["reason"]) == (503, "unavailable")
    assert _sql(tables, "SELECT * FROM ai_session_files") == []


def test_a_fork_rewraps_the_parent_keys_and_holds_the_same_blob(
    blobs: Agent, tables: PgPool
) -> None:
    parent, child = _session(tables), _session(tables)
    blobs.write(f"session:{parent}", "a.bin", b"sealed")
    copied = blobs.post("/workspace/copies", {"from_session": parent, "to_session": child})
    assert copied.json() == {"copied": 1}
    rows = _sql(
        tables, "SELECT workspace, blob_key, key_sealed FROM ai_session_files ORDER BY workspace"
    )
    assert rows[0]["blob_key"] == rows[1]["blob_key"]
    assert rows[0]["key_sealed"] != rows[1]["key_sealed"]
    assert len(_holds(tables)) == 2
    assert blobs.read(f"session:{child}", "a.bin").content == b"sealed"


def test_a_fork_copies_the_parent_as_it_is_under_the_lock(
    blobs: Agent, tables: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent, child = _session(tables), _session(tables)
    blobs.write(f"session:{parent}", "a.bin", b"kept")
    blobs.write(f"session:{parent}", "b.bin", b"deleted meanwhile")
    wrap = ws.wrap_key
    calls: list[str] = []

    def wrap_then_delete(*args: Any, **kwargs: Any) -> Any:
        # The parent's b.bin goes between the wrap and the copy's lock, on another connection.
        if not calls:
            _sql(tables, "DELETE FROM ai_session_files WHERE path = 'b.bin'")
        calls.append(args[2])
        return wrap(*args, **kwargs)

    monkeypatch.setattr(ws, "wrap_key", wrap_then_delete)
    copied = blobs.post("/workspace/copies", {"from_session": parent, "to_session": child})
    assert copied.json() == {"copied": 1}
    rows = _sql(
        tables, "SELECT path FROM ai_session_files WHERE workspace = %s", (f"session:{child}",)
    )
    assert [r["path"] for r in rows] == ["a.bin"]
    assert {path for _, path in _holds(tables) if path.startswith(f"session:{child}")} == {
        f"session:{child}/a.bin"
    }


def test_a_session_gone_gets_no_key_and_no_file(blobs: Agent, tables: PgPool) -> None:
    sid = str(uuid.uuid4())
    refused = blobs.write(f"session:{sid}", "a", b"x")
    assert refused.status_code in (404, 503)
    assert _sql(tables, "SELECT * FROM ai_payload_keys") == []


def test_files_kept_in_postgres_move_to_blobs(tables: PgPool, tmp_path: Path) -> None:
    sid = _session(tables)
    for agent in _agent(_app(tables, tmp_path, blobs=False)):
        agent.write(f"session:{sid}", "a.txt", b"inline")
    for agent in _agent(_app(tables, tmp_path, blobs=True)):
        moved = agent.post("/workspace/blob-moves", {"batch": 10}).json()
        assert moved == {"moved": 1, "local": False, "more": True}
        assert agent.post("/workspace/blob-moves", {}).json() == {
            "moved": 0,
            "local": False,
            "more": False,
        }
        row = _sql(tables, "SELECT blob_key FROM ai_session_files")[0]
        assert _holds(tables) == {(row["blob_key"], f"session:{sid}/a.txt")}
        assert _sql(tables, "SELECT * FROM ai_session_file_bytes") == []
        assert agent.read(f"session:{sid}", "a.txt").content == b"inline"


def test_reconcile_releases_orphan_holds_and_holds_unheld_rows(
    blobs: Agent, tables: PgPool
) -> None:
    sid = _session(tables)
    blobs.write(f"session:{sid}", "a", b"x")
    blob = _sql(tables, "SELECT blob_key FROM ai_session_files")[0]["blob_key"]
    _sql(tables, "DELETE FROM blob_refs")
    orphan = "workspace-" + "1" * 64
    _sql(
        tables,
        "INSERT INTO blob_refs (key, holder_kind, holder_id, created_at)"
        " VALUES (%s, 'ai_session_file', 'session:gone/x', now() - interval '2 hours'),"
        " (%s, 'ai_session_file', 'session:fresh/x', now())",
        (orphan, orphan),
    )
    done = blobs.post("/workspace/reconciles", {"grace_seconds": 3600}).json()
    assert done == {"released": 1, "held": 1, "skipped": None}
    assert _holds(tables) == {(blob, f"session:{sid}/a"), (orphan, "session:fresh/x")}


def test_reconcile_skips_while_another_holds_the_lock(
    blobs: Agent, tables: PgPool, pg_conninfo: str
) -> None:
    held = threading.Event()
    with psycopg.connect(pg_conninfo, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (ws.RECONCILE_LOCK,))
        held.set()
        done = blobs.post("/workspace/reconciles", {}).json()
        assert done == {"released": 0, "held": 0, "skipped": "held"}


def test_seal_file_opens_segment_by_segment_and_refuses_a_cut(tables: PgPool) -> None:
    import asyncio

    key = ws.new_file_key()
    data = os.urandom(ws.SEGMENT_BYTES + 5)
    digest = hashlib.sha256(data).hexdigest()
    sealed = ws.seal_file(key, data, digest)

    async def opened(blob: bytes, size: int) -> bytes:
        async def chunks() -> Any:
            for i in range(0, len(blob), size):
                yield blob[i : i + size]

        return b"".join([p async for p in ws.open_file(key, chunks(), digest)])

    assert asyncio.run(opened(sealed, 4096)) == data
    with pytest.raises(Exception, match=r"authentication|cut short"):
        asyncio.run(opened(sealed[: ws.SEGMENT_BYTES + ws.SEAL_OVERHEAD], 4096))
    # The first segment is sealed in its own context, as the agent's sealing.ts does.
    first = sealed[: ws.SEGMENT_BYTES + ws.SEAL_OVERHEAD]
    assert open_bytes(key, first, f"workspace-file:{digest}:0:more") == data[: ws.SEGMENT_BYTES]
