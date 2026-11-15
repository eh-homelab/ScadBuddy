"""The assistant's session images, attachments and saved HTTP bodies through the agent's
CRUD routes (#5299 step 7, areas ``session-blobs``, ``attachments``, ``http-bodies``): only
the ``agent`` credential reaches them; a name is its bytes' sha256 and an image's signature
its type's, checked again here; a tool call's images replace its earlier attempt's; a
fenced write stores nothing once the turn moved; a fork reads an ancestor's image only
when an event it reads names it; an attachment is its owner's alone, under the owner's
caps; a saved body is read by range and only the newest ten are kept."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import crud_agent_blobs, crud_areas
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
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 24
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x02" * 16


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
    app.include_router(crud_agent_blobs.router, prefix="/api/v1")
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


def _sql(
    pool: PgPool, query: str, params: tuple[Any, ...] | dict[str, Any] = ()
) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(query.encode(), params)
        return list(cur.fetchall()) if cur.description else []


def _session(pool: PgPool, *, parent: str | None = None, event_cut: int | None = None) -> str:
    sid = str(uuid.uuid4())
    _sql(
        pool,
        "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
        " creator_id, status, max_turns, budget_usd, fork_parent_id, fork_cut, fork_event_cut)"
        " VALUES (%s, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'running', 10,"
        " 1, %s, %s, %s)",
        (sid, parent, None if parent is None else 1, event_cut),
    )
    return sid


def _event(pool: PgPool, session: str, seq: int, event: dict[str, Any]) -> None:
    _sql(
        pool,
        "INSERT INTO ai_session_events (session_id, seq, event) VALUES (%s, %s, %s)",
        (session, seq, json.dumps(event)),
    )


def _name(data: bytes, ext: str) -> str:
    return f"{hashlib.sha256(data).hexdigest()}.{ext}"


class Agent:
    def __init__(self, client: TestClient, headers: dict[str, str]) -> None:
        self.client = client
        self.headers = headers

    def get(self, route: str, **params: Any) -> Any:
        return self.client.get(f"{BASE}{route}", params=params, headers=self.headers)

    def post(self, route: str, body: Any) -> Any:
        return self.client.post(f"{BASE}{route}", json=body, headers=self.headers)

    def blobs(
        self,
        session: str,
        images: list[tuple[str, bytes]],
        *,
        names: list[str] | None = None,
        **meta: Any,
    ) -> Any:
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
        body = {
            "session_id": session,
            "images": [
                {
                    "name": (names or [])[i] if names else _name(data, ext[media]),
                    "media_type": media,
                }
                for i, (media, data) in enumerate(images)
            ],
            **meta,
        }
        return self.client.post(
            f"{BASE}/session-blobs",
            headers=self.headers,
            data={"meta": json.dumps(body)},
            files={
                f"image{i}": (f"image{i}", data, "application/octet-stream")
                for i, (_, data) in enumerate(images)
            },
        )

    def attach(
        self, owner: dict[str, str], image: bytes = PNG, preview: bytes = WEBP, **meta: Any
    ) -> Any:
        body = {
            "owner": owner,
            "media_type": "image/png",
            "preview_media_type": "image/webp",
            **meta,
        }
        return self.client.post(
            f"{BASE}/attachments",
            headers=self.headers,
            data={"meta": json.dumps(body)},
            files={
                "image": ("image", image, "application/octet-stream"),
                "preview": ("preview", preview, "application/octet-stream"),
            },
        )

    def save_body(self, session: str, body_id: str, data: bytes, **meta: Any) -> Any:
        fields = {
            "session_id": session,
            "id": body_id,
            "url": "https://example.com/x",
            "content_type": "text/plain",
            "size_bytes": len(data),
            "complete": True,
            **meta,
        }
        return self.client.post(
            f"{BASE}/http-bodies",
            headers=self.headers,
            data={"meta": json.dumps(fields)},
            files={"body": ("body", data, "application/octet-stream")},
        )


@pytest.fixture
def agent(tables: PgPool) -> Iterator[Agent]:
    app = _app(tables)
    with TestClient(app) as client:
        yield Agent(client, _token(app))


# --- the guard -----------------------------------------------------------------------


def test_only_the_agent_credential_reaches_the_routes(tables: PgPool) -> None:
    app = _app(tables)
    sid = str(uuid.uuid4())
    with TestClient(app) as client:
        route = f"{BASE}/session-blobs"
        params = {"session_id": sid, "name": _name(PNG, "png")}
        assert client.get(route, params=params).status_code == 401
        assert client.get(route, params=params, headers=_token(app, "render")).status_code == 403
        assert (
            client.post(f"{BASE}/attachments/sweeps", headers=_token(app, "print")).status_code
            == 403
        )
        body = {"session_id": sid, "id": sid}
        assert client.get(f"{BASE}/http-bodies/meta", params=body).status_code == 401
        areas = client.get(f"{BASE}/areas", headers=_token(app)).json()["areas"]
        assert {"session-blobs", "attachments", "http-bodies"} <= set(areas)


def test_a_name_or_id_that_climbs_is_refused(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    for name in ("../x.png", f"{'a' * 64}.png/../x", "x.png", f"{'A' * 64}.png"):
        assert agent.get("/session-blobs", session_id=sid, name=name).status_code == 422, name
    assert agent.get("/session-blobs", session_id="../x", name=_name(PNG, "png")).status_code == 422
    owner = {"owner_kind": "browser", "owner_id": "browser"}
    for bad in ("..", "..%2Fx", "not-a-uuid"):
        answer = agent.client.get(
            f"{BASE}/attachments/{bad}/image", params=owner, headers=agent.headers
        )
        assert answer.status_code in (404, 422), bad
    assert agent.get("/http-bodies/bytes", session_id=sid, id="../x").status_code == 422


# --- session blobs -------------------------------------------------------------------


def test_images_are_stored_once_by_name_and_read_back(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    assert agent.blobs(sid, [("image/png", PNG), ("image/jpeg", JPEG)]).json() == {"stored": True}
    assert agent.blobs(sid, [("image/png", PNG)]).json() == {"stored": True}
    rows = _sql(tables, "SELECT name FROM ai_session_blobs WHERE session_id = %s", (sid,))
    assert sorted(r["name"] for r in rows) == sorted([_name(PNG, "png"), _name(JPEG, "jpg")])
    read = agent.get("/session-blobs", session_id=sid, name=_name(JPEG, "jpg"))
    assert read.status_code == 200
    assert read.content == JPEG
    assert read.headers["content-type"] == "image/jpeg"
    missing = agent.get("/session-blobs", session_id=sid, name=_name(b"other", "png"))
    assert missing.status_code == 404
    gone = agent.blobs(str(uuid.uuid4()), [("image/png", PNG)])
    assert gone.status_code == 404


def test_an_image_is_checked_against_its_name_type_and_caps(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    wrong = agent.blobs(sid, [("image/png", PNG)], names=[_name(JPEG, "png")])
    assert wrong.status_code == 422
    not_png = agent.blobs(sid, [("image/png", JPEG)], names=[_name(JPEG, "png")])
    assert not_png.status_code == 422
    many = agent.blobs(sid, [("image/png", PNG + bytes([i])) for i in range(9)])
    assert many.status_code == 422
    big = PNG + b"\x00" * crud_agent_blobs.IMAGE_BYTES_MAX
    assert agent.blobs(sid, [("image/png", big)]).status_code == 413
    assert _sql(tables, "SELECT 1 FROM ai_session_blobs") == []


def test_a_tool_calls_images_replace_its_earlier_attempts(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    first, second = PNG + b"1", PNG + b"2"
    assert agent.blobs(sid, [("image/png", first)], tool_use_id="toolu_1").status_code == 200
    assert agent.blobs(sid, [("image/png", second)], tool_use_id="toolu_1").status_code == 200
    rows = _sql(
        tables,
        "SELECT ord, name FROM ai_session_tool_images WHERE session_id = %s ORDER BY ord",
        (sid,),
    )
    assert rows == [{"ord": 0, "name": _name(second, "png")}]


def test_a_fenced_write_stores_nothing_once_the_turn_moved(agent: Agent, tables: PgPool) -> None:
    sid = _session(tables)
    turn = str(uuid.uuid4())
    _sql(
        tables,
        "UPDATE ai_sessions SET turn_attempt = 'run:1', durable_turn_id = %s WHERE id = %s",
        (turn, sid),
    )
    moved = agent.blobs(sid, [("image/png", PNG)], fence={"attempt": "run:2", "turn_id": turn})
    assert moved.json() == {"stored": False}
    assert _sql(tables, "SELECT 1 FROM ai_session_blobs") == []
    held = agent.blobs(sid, [("image/png", PNG)], fence={"attempt": "run:1", "turn_id": turn})
    assert held.json() == {"stored": True}
    both = agent.blobs(
        sid, [("image/png", PNG)], tool_use_id="t", fence={"attempt": "run:1", "turn_id": turn}
    )
    assert both.status_code == 422


def test_a_fork_reads_only_the_ancestors_images_its_events_name(
    agent: Agent, tables: PgPool
) -> None:
    parent = _session(tables)
    named, later = PNG + b"named", PNG + b"later"
    agent.blobs(parent, [("image/png", named), ("image/png", later)])
    _event(tables, parent, 1, {"type": "user.turn", "images": [{"name": _name(named, "png")}]})
    _event(tables, parent, 2, {"type": "user.turn", "images": [{"name": _name(later, "png")}]})
    fork = _session(tables, parent=parent, event_cut=1)
    assert agent.get("/session-blobs", session_id=fork, name=_name(named, "png")).content == named
    past_cut = agent.get("/session-blobs", session_id=fork, name=_name(later, "png"))
    assert past_cut.status_code == 404


# --- attachments ---------------------------------------------------------------------


BROWSER = {"kind": "browser", "id": "browser"}
OTHER = {"kind": "bearer", "id": "token:other"}


def test_an_attachment_is_staged_resolved_read_and_deleted_by_its_owner(
    agent: Agent, tables: PgPool
) -> None:
    made = agent.attach(BROWSER)
    assert made.status_code == 201
    view = made.json()
    assert view["name"] == _name(PNG, "png")
    assert view["preview"] == {"name": _name(WEBP, "webp"), "media_type": "image/webp"}
    ids = {"owner": BROWSER, "ids": [view["id"]]}
    resolved = agent.post("/attachments/resolve", ids).json()["attachments"]
    assert [r["id"] for r in resolved] == [view["id"]]
    owner = {"owner_kind": "browser", "owner_id": "browser"}
    assert agent.get(f"/attachments/{view['id']}/image", **owner).content == PNG
    assert agent.get(f"/attachments/{view['id']}/preview", **owner).content == WEBP
    # Another owner's reads as unknown, and deletes nothing.
    theirs = {"owner_kind": "bearer", "owner_id": "token:other"}
    assert agent.get(f"/attachments/{view['id']}/image", **theirs).status_code == 404
    assert agent.post("/attachments/resolve", {"owner": OTHER, "ids": [view["id"]]}).json() == {
        "attachments": []
    }
    assert agent.post("/attachments/deletes", {"owner": OTHER, "ids": [view["id"]]}).json() == {
        "deleted": 0
    }
    assert agent.post("/attachments/deletes", ids).json() == {"deleted": 1}
    assert agent.get(f"/attachments/{view['id']}/image", **owner).status_code == 404


def test_an_attachment_is_checked_and_refused_past_the_owners_caps(
    agent: Agent, tables: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert agent.attach(BROWSER, image=JPEG).status_code == 422
    assert agent.attach(BROWSER, preview=PNG).status_code == 422
    big_preview = WEBP + b"\x00" * crud_agent_blobs.PREVIEW_BYTES_MAX
    assert agent.attach(BROWSER, preview=big_preview).status_code == 413
    monkeypatch.setattr(crud_agent_blobs, "OWNER_ROWS_MAX", 2)
    assert agent.attach(BROWSER).status_code == 201
    assert agent.attach(BROWSER).status_code == 201
    full = agent.attach(BROWSER)
    assert full.status_code == 409
    assert full.json()["reason"] == "quota"
    assert agent.attach(OTHER).status_code == 201


def test_the_sweep_deletes_expired_attachments(agent: Agent, tables: PgPool) -> None:
    kept = agent.attach(BROWSER).json()["id"]
    gone = agent.attach(BROWSER).json()["id"]
    _sql(
        tables,
        "UPDATE ai_attachments SET expires_at = now() - interval '1 s' WHERE id = %s",
        (gone,),
    )
    owner = {"owner_kind": "browser", "owner_id": "browser"}
    assert agent.get(f"/attachments/{gone}/image", **owner).status_code == 404
    assert agent.post("/attachments/sweeps", None).json() == {"deleted": 1}
    assert [r["id"] for r in _sql(tables, "SELECT id::text FROM ai_attachments")] == [kept]


# --- saved HTTP bodies ---------------------------------------------------------------


def test_a_saved_body_is_read_by_range_and_the_newest_ten_are_kept(
    agent: Agent, tables: PgPool
) -> None:
    sid = _session(tables)
    first = str(uuid.uuid4())
    assert agent.save_body(sid, first, b"0123456789").json() == {"stored": True}
    meta = agent.get("/http-bodies/meta", session_id=sid, id=first).json()
    assert {k: meta[k] for k in ("url", "content_type", "size_bytes", "complete")} == {
        "url": "https://example.com/x",
        "content_type": "text/plain",
        "size_bytes": 10,
        "complete": True,
    }
    assert (
        agent.get("/http-bodies/bytes", session_id=sid, id=first, offset=2, length=3).content
        == b"234"
    )
    assert (
        agent.get("/http-bodies/bytes", session_id=sid, id=first, offset=8, length=9).content
        == b"89"
    )
    assert (
        agent.get("/http-bodies/bytes", session_id=sid, id=first, offset=20, length=1).content
        == b""
    )
    # Far past any body, as http_response_read may ask: nothing, never a refusal.
    far = agent.get("/http-bodies/bytes", session_id=sid, id=first, offset=2**31 - 2, length=4)
    assert (far.status_code, far.content) == (200, b"")
    assert agent.get("/http-bodies/bytes", session_id=sid, id=first).content == b"0123456789"
    assert agent.save_body(sid, first, b"again").status_code == 409
    for _ in range(10):
        agent.save_body(sid, str(uuid.uuid4()), b"x")
    assert agent.get("/http-bodies/meta", session_id=sid, id=first).status_code == 404
    assert agent.get("/http-bodies/bytes", session_id=sid, id=first).status_code == 404
    rows = _sql(
        tables,
        "SELECT count(*)::int AS n FROM ai_session_http_bodies WHERE session_id = %s",
        (sid,),
    )
    assert rows == [{"n": 10}]
    assert agent.save_body(str(uuid.uuid4()), str(uuid.uuid4()), b"x").status_code == 404


def test_a_body_past_the_cap_is_refused(
    agent: Agent, tables: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _session(tables)
    monkeypatch.setattr(crud_agent_blobs, "HTTP_BODY_BYTES_MAX", 8)
    assert agent.save_body(sid, str(uuid.uuid4()), b"x" * 9).status_code == 413
    assert _sql(tables, "SELECT 1 FROM ai_session_http_bodies") == []
