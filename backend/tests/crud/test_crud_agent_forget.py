"""Forgetting a durable subject, the key deletes nothing forgets, and a flow run's images
through the agent's CRUD routes (#5299 step 6, area ``forget``): only the ``agent``
credential reaches them; a forget writes the tombstone and deletes the key and a run's
images in one transaction under the subject's lock, a session's forks first taking their
ranges of its transcript, events and images (#5299 step 9, part c), refused only when a fork
takes transcript entries and the API has no KEK; a flow image put is refused for a
forgotten run and keeps at most the run's cap; the bytes travel as they are, never base64."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from scadbuddy.agent_data import events, flow_blobs, transcripts
from scadbuddy.agent_data.workspace import Keks
from scadbuddy.api import crud_agent_forget, crud_areas
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
from scadbuddy.core.secrets import kek_from_base64
from scadbuddy.render.agent_migrations import migrate_agent
from scadbuddy.workflows.payload_codec import payload_key_lock
from tests.conftest import PgPool
from tests.crud.crud_support import _NoSessions, _Noted

pytestmark = pytest.mark.requires_postgres

BASE = "/api/v1/internal/crud/agent"
PNG = b"\x89PNG\r\n\x1a\n" + b"sealed by the agent" * 4
KEK_B64 = base64.b64encode(b"\x42" * 32).decode()
KEYS = Keks(kek_from_base64(KEK_B64), None)


def _app(pool: PgPool, key_file: Path | None = None) -> FastAPI:
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
        settings=SimpleNamespace(
            auth_mode="enforce", secret_key_file=key_file, secret_key_previous_file=None
        ),
        components=SimpleNamespace(get=lambda key: parts[key]),
        settings_store=SimpleNamespace(pool=pool),
    )
    app.include_router(crud_agent_forget.router, prefix="/api/v1")
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


@pytest.fixture
def app(tables: PgPool) -> FastAPI:
    return _app(tables)


class Agent:
    def __init__(self, client: TestClient, headers: dict[str, str]) -> None:
        self.client = client
        self.headers = headers

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        if body is not None:
            kwargs["json"] = body
        return self.client.post(f"{BASE}{path}", headers=self.headers, **kwargs)

    def get(self, path: str) -> Any:
        return self.client.get(f"{BASE}{path}", headers=self.headers)

    def put_images(self, subject: str, images: list[tuple[str, str, bytes]]) -> Any:
        meta = [{"name": n, "media_type": t, "size": len(d)} for n, t, d in images]
        files = [("blob", (n, d, "application/octet-stream")) for n, _, d in images]
        return self.post(f"/flow-blobs/{subject}", data={"images": json.dumps(meta)}, files=files)


@pytest.fixture
def agent(app: FastAPI) -> Iterator[Agent]:
    with TestClient(app) as client:
        yield Agent(client, _token(app))


@pytest.fixture
def keyed(tables: PgPool, tmp_path: Path) -> Iterator[Agent]:
    key_file = tmp_path / "kek"
    key_file.write_text(KEK_B64)
    app = _app(tables, key_file)
    with TestClient(app) as client:
        yield Agent(client, _token(app))


def _sql(pool: PgPool, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(query.encode(), params)
        return list(cur.fetchall()) if cur.description else []


def _key(pool: PgPool, subject: str, *, age_s: int = 0) -> None:
    _sql(
        pool,
        "INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id, created_at)"
        " VALUES (%s, %s, 'k1', now() - make_interval(secs => %s))",
        (subject, b"\x00" * 60, age_s),
    )


def _keys(pool: PgPool) -> set[str]:
    return {r["subject"] for r in _sql(pool, "SELECT subject FROM ai_payload_keys")}


def _tombstones(pool: PgPool) -> set[str]:
    return {r["subject"] for r in _sql(pool, "SELECT subject FROM ai_forgotten_subjects")}


def _session(
    pool: PgPool,
    session: uuid.UUID,
    parent: uuid.UUID | None = None,
    cut: int = 0,
    event_cut: int | None = None,
) -> None:
    _sql(
        pool,
        "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
        " creator_id, status, max_turns, budget_usd, fork_parent_id, fork_cut, fork_event_cut)"
        " VALUES (%s, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'running', 10,"
        " 1, %s, %s, %s)",
        (session, parent, None if parent is None else cut, event_cut),
    )


def _link(pool: PgPool, session: uuid.UUID) -> dict[str, Any]:
    return _sql(
        pool,
        "SELECT fork_parent_id, fork_cut, fork_event_cut FROM ai_sessions WHERE id = %s",
        (session,),
    )[0]


def _image(data: bytes = PNG, ext: str = "png", media: str = "image/png") -> tuple[str, str, bytes]:
    return f"{hashlib.sha256(data).hexdigest()}.{ext}", media, data


def _flow() -> str:
    return f"flow-{uuid.uuid4()}"


# --- scope ------------------------------------------------------------------------------


def test_the_area_is_listed() -> None:
    assert "forget" in crud_areas.CRUD_AREAS["agent"]


def test_only_the_agent_credential_reaches_them(app: FastAPI, tables: PgPool) -> None:
    flow, bridge = _flow(), f"bridge-{uuid.uuid4()}"
    _key(tables, flow)
    _key(tables, bridge, age_s=7200)
    name, media, data = _image()
    with TestClient(app) as client:
        for headers, code in (
            ({}, 401),
            (_token(app, "print"), 403),
            (_token(app, "projects"), 403),
        ):
            calls = (
                client.post(f"{BASE}/forgotten-subjects/{flow}", headers=headers),
                client.post(f"{BASE}/payload-keys/{bridge}/drop", headers=headers),
                client.post(f"{BASE}/payload-key-sweeps", json={}, headers=headers),
                client.post(
                    f"{BASE}/flow-blobs/{flow}",
                    data={"images": json.dumps([{"name": name, "media_type": media, "size": 1}])},
                    files=[("blob", (name, data, "application/octet-stream"))],
                    headers=headers,
                ),
                client.get(f"{BASE}/flow-blobs/{flow}/{name}", headers=headers),
            )
            assert [c.status_code for c in calls] == [code] * len(calls)
            assert all(c.headers["x-scadbuddy-crud"] == "1" for c in calls)
    assert _keys(tables) == {flow, bridge}
    assert _tombstones(tables) == set()
    assert _sql(tables, "SELECT 1 FROM ai_flow_blobs") == []


# --- forget -----------------------------------------------------------------------------


def test_a_flow_run_is_forgotten_with_its_images(agent: Agent, tables: PgPool) -> None:
    flow, other = _flow(), _flow()
    _key(tables, flow)
    _key(tables, other)
    assert agent.put_images(flow, [_image()]).status_code == 200
    assert agent.put_images(other, [_image()]).status_code == 200
    gone = agent.post(f"/forgotten-subjects/{flow}")
    assert (gone.status_code, gone.json()) == (200, {"key": True})
    assert _keys(tables) == {other}
    assert _tombstones(tables) == {flow}
    assert [r["subject"] for r in _sql(tables, "SELECT subject FROM ai_flow_blobs")] == [other]
    # Again: nothing left, and still answered.
    assert agent.post(f"/forgotten-subjects/{flow}").json() == {"key": False}
    # A forgotten run keeps no image, whatever key its caller still caches.
    refused = agent.put_images(flow, [_image()])
    assert (refused.status_code, refused.json()["reason"]) == (410, "subject-forgotten")
    assert _sql(tables, "SELECT 1 FROM ai_flow_blobs WHERE subject = %s", (flow,)) == []


def test_without_a_kek_a_fork_that_takes_entries_refuses_and_one_that_takes_none_goes(
    agent: Agent, tables: PgPool
) -> None:
    parent, taker, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _session(tables, parent)
    _session(tables, taker, parent, cut=2)
    for s in (parent, taker):
        _key(tables, f"session-{s}")
    # It never fails open: no tombstone, no key gone, the link kept.
    refused = agent.post(f"/forgotten-subjects/session-{parent}")
    assert (refused.status_code, refused.json()["reason"]) == (409, "forks-take-ranges")
    assert f"session-{parent}" in _keys(tables) and _tombstones(tables) == set()
    assert _link(tables, taker)["fork_parent_id"] == parent
    # A fork of a closed DurableSession takes no entries (fork_cut 0): no key is needed.
    other_parent = uuid.uuid4()
    _session(tables, other_parent)
    _session(tables, other, other_parent, cut=0)
    _key(tables, f"session-{other_parent}")
    assert agent.post(f"/forgotten-subjects/session-{other_parent}").json() == {"key": True}
    assert _link(tables, other) == {
        "fork_parent_id": None,
        "fork_cut": None,
        "fork_event_cut": None,
    }
    assert _tombstones(tables) == {f"session-{other_parent}"}


def test_forks_take_their_ranges_before_the_parent_goes(keyed: Agent, tables: PgPool) -> None:
    parent, child, grand, gone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _session(tables, parent)
    image = _image()
    other = _image(PNG + b"other")
    entries = [json.dumps({"type": "user", "uuid": f"p-{i}", "n": i}) for i in range(4)]
    logged = [
        json.dumps({"type": "session.started", "sessionId": str(parent)}),
        json.dumps({"type": "user.turn", "sessionId": str(parent), "text": "hi"}),
        json.dumps(
            {
                "type": "tool.result",
                "sessionId": str(parent),
                "images": [{"name": image[0]}, {"name": other[0]}],
            }
        ),
        json.dumps({"type": "user.turn", "sessionId": str(parent), "text": "late"}),
    ]
    with tables.connection() as conn:
        assert transcripts.append(conn, str(parent), "p", "", entries, None, KEYS)
        with conn.transaction():
            events.append_in(conn, str(parent), logged)
    _sql(
        tables,
        "INSERT INTO ai_session_blobs (session_id, name, media_type, data, created_at)"
        " VALUES (%s, %s, %s, %s, now() - interval '1 day')",
        (parent, *image),
    )
    _session(tables, child, parent, cut=3, event_cut=3)
    # An image the fork already holds, as a queued message's write in it left it (#6413).
    for owner in (parent, child):
        _sql(
            tables,
            "INSERT INTO ai_session_blobs (session_id, name, media_type, data, created_at)"
            " VALUES (%s, %s, %s, %s, now() - interval '2 days')",
            (owner, *other),
        )
    _session(tables, grand, child, cut=1, event_cut=1)
    _session(tables, gone, parent, cut=2, event_cut=3)
    with tables.connection() as conn:
        assert transcripts.append(
            conn, str(child), "p", "", [json.dumps({"type": "user", "uuid": "c-0"})], None, KEYS
        )
    _sql(tables, "INSERT INTO ai_forgotten_subjects (subject) VALUES (%s)", (f"session-{gone}",))

    assert keyed.post(f"/forgotten-subjects/session-{parent}").json() == {"key": True}

    with tables.connection() as conn:
        # The child's range of the transcript is its own now, sealed under its key, read first.
        loaded = [json.loads(t)["uuid"] for t in transcripts.transcript(conn, str(child), KEYS)]
        assert loaded == ["p-0", "p-1", "p-2", "c-0"]
        # Its own fork reads through it, and the cut it had still holds.
        assert [json.loads(t)["uuid"] for t in transcripts.transcript(conn, str(grand), KEYS)] == [
            "p-0"
        ]
        read = events.read(conn, str(child), 0, 100)
    assert [(r["seq"], json.loads(r["event"])["type"], r["inherited"]) for r in read] == [
        (2, "user.turn", False),
        (3, "tool.result", False),
    ]
    assert all(json.loads(r["event"])["sessionId"] == str(child) for r in read)
    rows = _sql(
        tables, "SELECT inherited FROM ai_session_entries WHERE session_id = %s", (str(child),)
    )
    assert sorted(r["inherited"] for r in rows) == [False, True, True, True]
    blobs = _sql(
        tables,
        "SELECT name, data, created_at < now() - interval '1 hour' AS old"
        " FROM ai_session_blobs WHERE session_id = %s",
        (child,),
    )
    # A copied image keeps the parent's time; one the fork held is stamped by this write.
    assert sorted((b["name"], b["data"], b["old"]) for b in blobs) == sorted(
        [(image[0], image[2], True), (other[0], other[2], False)]
    )
    for fork in (child, gone):
        assert _link(tables, fork) == {
            "fork_parent_id": None,
            "fork_cut": None,
            "fork_event_cut": None,
        }
    # A fork forgotten itself takes nothing, and gets no key made for it.
    assert (
        _sql(tables, "SELECT 1 FROM ai_session_entries WHERE session_id = %s", (str(gone),)) == []
    )
    assert _sql(tables, "SELECT 1 FROM ai_session_events WHERE session_id = %s", (gone,)) == []
    assert f"session-{gone}" not in _keys(tables)
    assert _link(tables, grand)["fork_parent_id"] == child
    assert f"session-{parent}" in _tombstones(tables) and f"session-{parent}" not in _keys(tables)


def test_bad_subjects_are_refused(agent: Agent) -> None:
    for subject in ("thing-1", f"session-{uuid.uuid4()}x", f"bridge-{uuid.uuid4()}"):
        assert agent.post(f"/forgotten-subjects/{subject}").status_code == 422
    for subject in (f"session-{uuid.uuid4()}", _flow(), "bridge-1"):
        assert agent.post(f"/payload-keys/{subject}/drop").status_code == 422


def test_a_forget_waits_for_a_key_creation_under_the_lock(
    agent: Agent, tables: PgPool, pg_conninfo: str
) -> None:
    """A key's creation takes the subject's lock; a forget meanwhile deletes the key it
    committed, once it commits."""
    flow = _flow()
    answers: list[Any] = []
    with psycopg.connect(pg_conninfo) as create:
        create.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (payload_key_lock(flow),))
        create.execute(
            "INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES (%s, %s, 'k1')",
            (flow, b"\x00" * 60),
        )
        thread = threading.Thread(
            target=lambda: answers.append(agent.post(f"/forgotten-subjects/{flow}"))
        )
        thread.start()
        time.sleep(0.5)
        assert answers == []
        create.commit()
    thread.join(10)
    assert answers[0].json() == {"key": True}
    assert _keys(tables) == set()


def test_a_bridge_key_is_dropped_with_no_tombstone(agent: Agent, tables: PgPool) -> None:
    bridge = f"bridge-{uuid.uuid4()}"
    _key(tables, bridge)
    assert agent.post(f"/payload-keys/{bridge}/drop").json() == {"dropped": True}
    assert agent.post(f"/payload-keys/{bridge}/drop").json() == {"dropped": False}
    assert _keys(tables) == set() and _tombstones(tables) == set()


def test_the_sweep_deletes_only_the_keys_nothing_holds(agent: Agent, tables: PgPool) -> None:
    standing, entries, orphan, young = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _session(tables, standing)
    _key(tables, f"session-{standing}", age_s=2 * 86400)
    _key(tables, f"session-{entries}", age_s=2 * 86400)
    _sql(
        tables,
        "INSERT INTO ai_session_entries (project_key, session_id, subpath, entry_type, sealed)"
        " VALUES ('', %s, '', 'user', %s)",
        (entries, b"\x00"),
    )
    _key(tables, f"session-{orphan}", age_s=2 * 86400)
    _key(tables, f"session-{young}", age_s=60)
    old_bridge, new_bridge = f"bridge-{uuid.uuid4()}", f"bridge-{uuid.uuid4()}"
    _key(tables, old_bridge, age_s=7200)
    _key(tables, new_bridge, age_s=10)
    flow = _flow()
    _key(tables, flow, age_s=30 * 86400)
    swept = agent.post("/payload-key-sweeps", {})
    assert swept.json() == {"orphaned": 1, "bridge": 1}
    assert _keys(tables) == {
        f"session-{standing}",
        f"session-{entries}",
        f"session-{young}",
        new_bridge,
        flow,
    }
    assert _tombstones(tables) == set()
    assert agent.post("/payload-key-sweeps", {"orphan_min_age_s": 1}).status_code == 422


# --- flow images ------------------------------------------------------------------------


def test_images_are_kept_once_and_read_back_as_sent(agent: Agent, tables: PgPool) -> None:
    flow = _flow()
    png = _image()
    jpg = _image(b"\xff\xd8\xff" + b"another" * 3, "jpg", "image/jpeg")
    kept = agent.put_images(flow, [png, jpg, png])
    assert (kept.status_code, kept.json()) == (200, {"names": [png[0], jpg[0]]})
    assert agent.put_images(flow, [png]).json() == {"names": [png[0]]}
    rows = _sql(tables, "SELECT name, media_type, size, data FROM ai_flow_blobs ORDER BY name")
    assert sorted(
        (r["name"], r["media_type"], r["size"], bytes(r["data"])) for r in rows
    ) == sorted([(png[0], "image/png", len(PNG), PNG), (jpg[0], "image/jpeg", len(jpg[2]), jpg[2])])
    got = agent.get(f"/flow-blobs/{flow}/{jpg[0]}")
    assert got.status_code == 200
    assert got.content == jpg[2]
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["x-scadbuddy-media-type"] == "image/jpeg"
    assert got.headers["x-scadbuddy-crud"] == "1"
    missing = agent.get(f"/flow-blobs/{_flow()}/{jpg[0]}")
    assert (missing.status_code, missing.json()["reason"]) == (404, "no-such-image")
    assert missing.headers["x-scadbuddy-crud"] == "1"
    assert agent.get(f"/flow-blobs/{flow}/x.png").status_code == 422


def test_a_run_keeps_at_most_its_cap(
    agent: Agent, tables: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(flow_blobs, "FLOW_IMAGES_MAX", 1)
    flow = _flow()
    first, second = _image(), _image(PNG + b"\x00")
    assert agent.put_images(flow, [first]).json() == {"names": [first[0]]}
    assert agent.put_images(flow, [second, first]).json() == {"names": [first[0]]}
    assert [r["name"] for r in _sql(tables, "SELECT name FROM ai_flow_blobs")] == [first[0]]
    # Another run has its own room.
    assert agent.put_images(_flow(), [second]).json() == {"names": [second[0]]}


def test_a_put_waits_for_a_forget_that_is_committing(
    agent: Agent, tables: PgPool, pg_conninfo: str
) -> None:
    flow = _flow()
    answers: list[Any] = []
    with psycopg.connect(pg_conninfo) as forget:
        forget.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (payload_key_lock(flow),))
        forget.execute("INSERT INTO ai_forgotten_subjects (subject) VALUES (%s)", (flow,))
        thread = threading.Thread(target=lambda: answers.append(agent.put_images(flow, [_image()])))
        thread.start()
        time.sleep(0.5)
        assert answers == []
        forget.commit()
    thread.join(10)
    assert answers[0].status_code == 410
    assert _sql(tables, "SELECT 1 FROM ai_flow_blobs") == []


def test_bad_puts_are_refused(agent: Agent, tables: PgPool) -> None:
    flow = _flow()
    name, media, data = _image()
    meta = json.dumps([{"name": name, "media_type": media, "size": len(data)}])
    blob = [("blob", (name, data, "application/octet-stream"))]
    refusals = (
        # Not multipart: the bytes never go as JSON.
        agent.post(f"/flow-blobs/{flow}", {"images": [{"name": name}]}),
        agent.post(f"/flow-blobs/{flow}", data={"images": meta}),
        agent.post(f"/flow-blobs/{flow}", data={"images": "[]"}, files=blob),
        agent.post(f"/flow-blobs/{flow}", data={"images": "not json"}, files=blob),
        agent.post(f"/flow-blobs/{flow}", files=blob, data={"other": "x"}),
        agent.put_images(flow, [(name, "image/jpeg", data)]),
        agent.put_images(flow, [(name, "image/svg+xml", data)]),
        agent.put_images(flow, [("../x.png", media, data)]),
        agent.put_images(flow, [(name, media, b"")]),
        agent.put_images(f"session-{uuid.uuid4()}", [(name, media, data)]),
        agent.put_images(flow, [(name, media, data)] * (crud_agent_forget.PUT_IMAGES_MAX + 1)),
    )
    assert [r.status_code for r in refusals] == [422] * len(refusals)
    assert _sql(tables, "SELECT 1 FROM ai_flow_blobs") == []
