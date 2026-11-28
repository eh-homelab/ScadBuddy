"""The agent's settings, Claude credentials and payload data keys through CRUD routes
(#5299 step 4, D5; #5342): only the ``agent`` credential reaches them, the API seals and
opens with the KEK, a reveal and a release each write an ``ai_audit`` row, a settings
compare-and-set holds the table's lock, and a payload key is made under the subject's
lock and never for a forgotten subject.

The agent's tables are made from copies of its migrations
(``tests/fixtures/agent-secret-migrations/``, kept equal by
``.github/scripts/lint-codec-copies.sh``); the credential vectors are a copy of
``agent/test/fixtures/secret-vectors.json``, so a row the agent sealed opens here."""

from __future__ import annotations

import base64
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

from scadbuddy.agent_data import audit, payload_keys, rotation
from scadbuddy.agent_data import credentials as store
from scadbuddy.agent_data.rotation import rotate
from scadbuddy.api import crud_agent_secrets, crud_areas
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
from scadbuddy.core.secrets import (
    Envelope,
    Kek,
    kek_from_base64,
    open_bytes,
    open_secret,
    seal_bytes,
    seal_secret,
)
from scadbuddy.library.settings_store import settings_kek
from scadbuddy.render.agent_migrations import migrate_agent
from scadbuddy.workflows.payload_codec import payload_key_context, payload_key_lock
from tests.conftest import PgPool
from tests.crud.crud_support import _NoSessions, _Noted

pytestmark = pytest.mark.requires_postgres

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
VECTORS = FIXTURES / "secret-vectors.json"
BASE = "/api/v1/internal/crud/agent"
KEK_B64 = base64.b64encode(b"\x31" * 32).decode()
OLD_B64 = base64.b64encode(b"\x32" * 32).decode()
KEK = kek_from_base64(KEK_B64)
OLD = kek_from_base64(OLD_B64)


def _agent_tables(pool: PgPool) -> None:
    # The agent's own migration files, as the API applies them at its start (#5299 D6).
    with pool.connection() as conn:
        migrate_agent(conn)


def _app(pool: PgPool, tmp_path: Path, *, kek: str | None, previous: str | None = None) -> FastAPI:
    def key_file(name: str, text: str | None) -> Path | None:
        if text is None:
            return None
        path = tmp_path / name
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
    app = FastAPI(dependencies=[Depends(auth_check)])
    app.state.scadbuddy = SimpleNamespace(
        settings=SimpleNamespace(
            auth_mode="enforce",
            secret_key_file=key_file(f"kek-{uuid.uuid4().hex}", kek),
            secret_key_previous_file=key_file(f"old-{uuid.uuid4().hex}", previous),
        ),
        components=SimpleNamespace(get=lambda key: parts[key]),
        settings_store=SimpleNamespace(pool=pool),
    )
    app.include_router(crud_agent_secrets.router, prefix="/api/v1")
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
    """The agent's calls, with its credential."""

    def __init__(self, client: TestClient, headers: dict[str, str]) -> None:
        self.client = client
        self.headers = headers

    def get(self, path: str, **params: Any) -> Any:
        return self.client.get(f"{BASE}{path}", params=params, headers=self.headers)

    def post(self, path: str, body: Any) -> Any:
        return self.client.post(f"{BASE}{path}", json=body, headers=self.headers)


@pytest.fixture
def tables(pg_pool: PgPool) -> PgPool:
    _agent_tables(pg_pool)
    return pg_pool


@pytest.fixture
def app(tables: PgPool, tmp_path: Path) -> FastAPI:
    return _app(tables, tmp_path, kek=KEK_B64)


@pytest.fixture
def agent(app: FastAPI) -> Iterator[Agent]:
    with TestClient(app) as client:
        yield Agent(client, _token(app))


def _audit(pool: PgPool) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        return list(
            conn.execute(
                "SELECT kind, action, outcome, detail, principal_kind, principal_id,"
                " session_id::text AS session_id FROM ai_audit ORDER BY id"
            ).fetchall()
        )


def _sql(pool: PgPool, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        cur = conn.execute(query.encode(), params)
        return list(cur.fetchall()) if cur.description else []


# --- scope ------------------------------------------------------------------------------


def test_the_areas_are_listed() -> None:
    for area in ("settings", "credentials", "payload-keys"):
        assert area in crud_areas.CRUD_AREAS["agent"]


def test_only_the_agent_credential_reaches_them(app: FastAPI, tables: PgPool) -> None:
    subject = f"session-{uuid.uuid4()}"
    with TestClient(app) as client:
        for headers, code in (
            ({}, 401),
            (_token(app, "print"), 403),
            (_token(app, "projects"), 403),
        ):
            assert (
                client.get(f"{BASE}/settings", params={"key": "model"}, headers=headers).status_code
                == code
            )
            assert client.get(f"{BASE}/credentials", headers=headers).status_code == code
            reveal = client.post(
                f"{BASE}/credentials/reveal", json={"all": True, "purpose": "turn"}, headers=headers
            )
            assert reveal.status_code == code
            key = client.post(
                f"{BASE}/payload-keys/{subject}", json={"create": True}, headers=headers
            )
            assert key.status_code == code
    assert _sql(tables, "SELECT subject FROM ai_payload_keys") == []
    assert _audit(tables) == []


# --- settings ---------------------------------------------------------------------------


def test_settings_read_and_write(agent: Agent) -> None:
    assert agent.get("/settings", key=["model", "absent"]).json() == {"values": {}}
    written = agent.post("/settings", {"values": {"model": "opus", "limits": {"a": [1, None]}}})
    assert written.status_code == 200, written.text
    assert written.headers["x-scadbuddy-crud"] == "1"
    assert written.json() == {"written": True}
    got = agent.get("/settings", key=["model", "limits", "absent"]).json()
    assert got == {"values": {"model": "opus", "limits": {"a": [1, None]}}}


def test_settings_refuse_a_bad_key_or_body(agent: Agent) -> None:
    assert agent.get("/settings", key=["has space"]).status_code == 422
    assert agent.post("/settings", {"values": {}}).status_code == 422
    assert agent.post("/settings", {"values": {"k": 1}, "extra": 1}).status_code == 422
    over = "x" * (crud_agent_secrets.SETTING_VALUE_MAX + 1)
    big = {"values": {"k": over}}
    assert agent.post("/settings", big).status_code == 422
    big_expect = {"values": {"k": 1}, "expect": [{"key": "k", "present": True, "value": over}]}
    assert agent.post("/settings", big_expect).status_code == 422


def test_a_compare_and_set_writes_only_what_was_read(agent: Agent) -> None:
    agent.post("/settings", {"values": {"mode": "bearer"}})
    stale = {
        "values": {"mode": "disabled"},
        "expect": [{"key": "mode", "present": True, "value": "none"}],
    }
    assert agent.post("/settings", stale).json() == {"written": False}
    absent = {"values": {"mode": "disabled"}, "expect": [{"key": "mode", "present": False}]}
    assert agent.post("/settings", absent).json() == {"written": False}
    fresh = {
        "values": {"mode": "disabled", "cap": 3},
        "expect": [
            {"key": "mode", "present": True, "value": "bearer"},
            {"key": "cap", "present": False},
        ],
    }
    assert agent.post("/settings", fresh).json() == {"written": True}
    assert agent.get("/settings", key=["mode", "cap"]).json() == {
        "values": {"mode": "disabled", "cap": 3}
    }
    # jsonb equality: key order and 1 against 1.0 do not matter.
    agent.post("/settings", {"values": {"obj": {"a": 1, "b": 2}}})
    same = {
        "values": {"obj": 0},
        "expect": [{"key": "obj", "present": True, "value": {"b": 2, "a": 1.0}}],
    }
    assert agent.post("/settings", same).json() == {"written": True}


def test_a_compare_and_set_waits_for_the_tables_lock(
    agent: Agent, tables: PgPool, pg_conninfo: str
) -> None:
    agent.post("/settings", {"values": {"mode": "bearer"}})
    answers: list[Any] = []
    with psycopg.connect(pg_conninfo) as holder:
        holder.execute("LOCK TABLE ai_settings IN SHARE ROW EXCLUSIVE MODE")
        body = {
            "values": {"mode": "disabled"},
            "expect": [{"key": "mode", "present": True, "value": "bearer"}],
        }
        thread = threading.Thread(
            target=lambda: answers.append(agent.post("/settings", body).json())
        )
        thread.start()
        time.sleep(0.5)
        # Blocked behind the other writer; a read is not.
        assert answers == []
        assert agent.get("/settings", key=["mode"]).json() == {"values": {"mode": "bearer"}}
        holder.execute("UPDATE ai_settings SET value = '\"other\"' WHERE key = 'mode'")
        holder.commit()
    thread.join(10)
    # It compared what was committed once it held the lock.
    assert answers == [{"written": False}]


def test_a_write_notifies_in_its_transaction(agent: Agent, pg_conninfo: str) -> None:
    with psycopg.connect(pg_conninfo, autocommit=True) as listener:
        listener.execute("LISTEN scadbuddy_warm_evict")
        listener.execute("LISTEN scadbuddy_settings")
        agent.post("/settings", {"values": {"quiet": 1}})
        agent.post("/settings", {"values": {"model": "x"}, "notify": {"warm_evict": True}})
        agent.post("/settings", {"values": {"warm_idle_seconds": 90}, "notify": {"runtime": True}})
        refused = {
            "values": {"model": "y"},
            "expect": [{"key": "model", "present": False}],
            "notify": {"warm_evict": True},
        }
        agent.post("/settings", refused)
        heard = [(n.channel, n.payload) for n in listener.notifies(timeout=1.0)]
    assert heard == [("scadbuddy_warm_evict", "*"), ("scadbuddy_settings", "session-runtime")]


# --- credentials ------------------------------------------------------------------------

API_KEY = "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaa1234"


def test_a_credential_is_sealed_here_and_never_answered(agent: Agent, tables: PgPool) -> None:
    created = agent.post(
        "/credentials", {"kind": "anthropic_api_key", "secret": API_KEY, "name": "Work"}
    )
    assert created.status_code == 201, created.text
    row = created.json()["credential"]
    assert (row["kind"], row["name"], row["last4"], row["priority"]) == (
        "anthropic_api_key",
        "Work",
        "1234",
        0,
    )
    assert (row["kek_id"], row["legacy_format"], row["status"], row["epoch"]) == (
        KEK.id,
        False,
        "active",
        0,
    )
    assert API_KEY not in created.text
    listed = agent.get("/credentials")
    assert API_KEY not in listed.text
    assert [c["id"] for c in listed.json()["credentials"]] == [row["id"]]
    sealed = _sql(tables, "SELECT secret_sealed, kek_id FROM ai_credentials")
    assert API_KEY.encode() not in bytes(sealed[0]["secret_sealed"])
    assert agent.get("/credentials/one").json()["credential"]["id"] == row["id"]
    assert agent.get("/credentials/one", id="nope").json() == {"credential": None}
    # Nothing was revealed, so nothing is audited.
    assert _audit(tables) == []


def test_reveal_opens_and_audits_each_time(agent: Agent, tables: PgPool) -> None:
    first = agent.post("/credentials", {"kind": "anthropic_api_key", "secret": API_KEY}).json()[
        "credential"
    ]
    gateway = {
        "kind": "gateway",
        "base_url": "https://gw.example.com/v1",
        "secret": "gw-token-0123456789",
    }
    second = agent.post("/credentials", gateway).json()["credential"]
    session = str(uuid.uuid4())
    every = agent.post(
        "/credentials/reveal", {"all": True, "purpose": "turn", "session_id": session}
    )
    assert every.status_code == 200, every.text
    rows = every.json()["credentials"]
    assert [r["stored"]["id"] for r in rows] == [first["id"], second["id"]]
    assert rows[0]["credential"] == {
        "kind": "anthropic_api_key",
        "base_url": None,
        "secret": API_KEY,
    }
    assert rows[1]["credential"] == {
        "kind": "gateway",
        "base_url": "https://gw.example.com/v1",
        "secret": "gw-token-0123456789",
    }
    one = agent.post("/credentials/reveal", {"id": second["id"], "purpose": "test"}).json()
    assert one["credential"]["secret"] == "gw-token-0123456789"
    assert (
        agent.post("/credentials/reveal", {"id": "gone", "purpose": "test"}).json()["credential"]
        is None
    )
    audit = _audit(tables)
    assert [(a["kind"], a["action"], a["outcome"]) for a in audit] == [
        ("credential", "reveal.turn", "ok"),
        ("credential", "reveal.test", "ok"),
        ("credential", "reveal.test", "refused"),
    ]
    assert audit[0]["detail"] == "opened 2 of 2 credential(s)"
    assert audit[0]["session_id"] == session
    assert audit[1]["detail"] == "credential 2 (gateway https://gw.example.com/v1 …6789)"
    assert {(a["principal_kind"], a["principal_id"]) for a in audit} == {("service", "agent")}
    for a in audit:
        assert API_KEY not in (a["detail"] or "")


def test_a_row_another_key_sealed_is_not_opened(agent: Agent, tables: PgPool) -> None:
    created = agent.post("/credentials", {"kind": "anthropic_api_key", "secret": API_KEY}).json()[
        "credential"
    ]
    _sql(tables, "UPDATE ai_credentials SET kek_id = 'elsewhere'")
    every = agent.post("/credentials/reveal", {"all": True, "purpose": "redact"}).json()
    assert every["credentials"][0]["credential"] is None
    refused = agent.post("/credentials/reveal", {"id": created["id"], "purpose": "test"})
    assert (refused.status_code, refused.json()["reason"]) == (409, "credential-sealed")
    assert [(a["action"], a["outcome"]) for a in _audit(tables)] == [
        ("reveal.redact", "ok"),
        ("reveal.test", "error"),
    ]
    # Redaction reads at tool-call rate: one row a window (REDACT_AUDIT_WINDOW).
    agent.post("/credentials/reveal", {"all": True, "purpose": "redact"})
    assert [a["action"] for a in _audit(tables)].count("reveal.redact") == 1


def test_the_agents_own_rows_open_here(tables: PgPool, tmp_path: Path) -> None:
    """A credential the agent sealed (its vectors) opens with the API's copy of the KEK."""
    vectors = json.loads(VECTORS.read_text())
    app = _app(tables, tmp_path, kek=vectors["kek_b64"])
    with tables.connection() as conn:
        for v in vectors["credentials"]:
            # A claude_login is the fleet's (#7983): its scope is fleet, and the agent's
            # reveal skips it.
            conn.execute(
                "INSERT INTO ai_credentials (id, priority, kind, base_url, secret_sealed,"
                " dek_sealed, kek_id, last4, scope)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, '', %s)",
                (
                    v["id"],
                    v["priority"],
                    v["kind"],
                    v["base_url"],
                    base64.b64decode(v["secret_sealed_b64"]),
                    base64.b64decode(v["dek_sealed_b64"]),
                    v["kek_id"],
                    "fleet" if v["kind"] == "claude_login" else "agent",
                ),
            )
    with TestClient(app) as client:
        agent = Agent(client, _token(app))
        rows = agent.post("/credentials/reveal", {"all": True, "purpose": "turn"}).json()[
            "credentials"
        ]
    by_id = {r["stored"]["id"]: r for r in rows}
    for v in vectors["credentials"]:
        if v["kind"] == "claude_login":
            assert v["id"] not in by_id, v["name"]
            continue
        opened = by_id[v["id"]]["credential"]
        if v["opens"]:
            assert opened is not None and opened["secret"] == v["plaintext"], v["name"]
        else:
            assert opened is None, v["name"]


def test_saves_keep_the_agents_rules(agent: Agent) -> None:
    def refused(path: str, body: Any) -> tuple[int, str, str]:
        r = agent.post(path, body)
        return r.status_code, r.json()["reason"], r.json()["detail"]

    assert refused("/credentials", {"kind": "gateway", "secret": "abcdefgh"})[:2] == (
        400,
        "credential-refused",
    )
    assert (
        refused(
            "/credentials", {"kind": "anthropic_api_key", "base_url": "https://x", "secret": "s"}
        )[0]
        == 400
    )
    assert refused("/credentials", {"kind": "anthropic_api_key", "secret": "has space"})[2] == (
        "secret must not contain whitespace"
    )
    assert (
        refused("/credentials", {"kind": "gateway", "base_url": "https://u:p@x", "secret": "s"})[0]
        == 400
    )
    assert (
        refused("/credentials", {"kind": "anthropic_api_key", "secret": "s", "name": " padded "})[0]
        == 400
    )
    first = agent.post("/credentials", {"kind": "anthropic_api_key", "secret": API_KEY}).json()[
        "credential"
    ]
    # A save without the secret keeps it, but never to a new destination.
    kept = agent.post(
        "/credentials/save", {"id": first["id"], "update": {"kind": "anthropic_api_key"}}
    )
    assert kept.json()["credential"]["epoch"] == first["epoch"]
    moved = {"id": first["id"], "update": {"kind": "gateway", "base_url": "https://gw.example.com"}}
    assert refused("/credentials/save", moved)[0] == 409
    assert (
        refused(
            "/credentials/save",
            {"id": "nope", "update": {"kind": "anthropic_api_key", "secret": "s"}},
        )[0]
        == 404
    )
    new_secret = {
        "id": first["id"],
        "update": {"kind": "anthropic_api_key", "secret": API_KEY + "x"},
    }
    saved = agent.post("/credentials/save", new_secret).json()["credential"]
    assert (saved["epoch"], saved["last4"]) == (first["epoch"] + 1, "234x")


def test_order_delete_reset_rename_and_events(agent: Agent) -> None:
    ids = [
        agent.post("/credentials", {"kind": "anthropic_api_key", "secret": f"{API_KEY}{n}"}).json()[
            "credential"
        ]["id"]
        for n in range(3)
    ]
    assert agent.post("/credentials/order", {"ids": ids[:2]}).status_code == 409
    order = agent.post("/credentials/order", {"ids": list(reversed(ids))}).json()["credentials"]
    assert [c["id"] for c in order] == list(reversed(ids))
    assert agent.post("/credentials/delete", {"id": ids[2]}).json() == {"deleted": True}
    assert [c["priority"] for c in agent.get("/credentials").json()["credentials"]] == [0, 1]
    assert agent.post("/credentials/delete", {"id": ids[2]}).json() == {"deleted": False}
    renamed = agent.post("/credentials/name", {"id": ids[0], "name": "Home"}).json()["credential"]
    assert renamed["name"] == "Home"
    assert agent.post("/credentials/name", {"id": "nope", "name": None}).json() == {
        "credential": None
    }
    until = "2099-01-01T00:00:00+00:00"
    cool = {
        "id": ids[0],
        "epoch": 0,
        "event": {"kind": "cooling_down", "until": until, "reason": "429"},
    }
    assert agent.post("/credentials/events", cool).json()["transition"] == {
        "before": "active",
        "after": "cooling_down",
        "recovered": False,
    }
    stale = {**cool, "epoch": 7}
    assert agent.post("/credentials/events", stale).json() == {"transition": None}
    disabled = {"id": ids[0], "epoch": 0, "event": {"kind": "disabled", "reason": "401"}}
    assert agent.post("/credentials/events", disabled).json()["transition"]["after"] == "disabled"
    # A rate limit never downgrades a disable.
    assert agent.post("/credentials/events", cool).json()["transition"]["after"] == "disabled"
    reset = agent.post("/credentials/reset", {"id": ids[0]}).json()["credential"]
    assert (reset["status"], reset["epoch"], reset["last_error"]) == ("active", 1, None)
    assert agent.post("/credentials/events", {**disabled, "event": {"kind": "used"}}).json() == {
        "transition": None
    }


def test_too_many_credentials_are_refused(agent: Agent, tables: PgPool) -> None:
    with tables.connection() as conn:
        for n in range(store.MAX_CREDENTIALS):
            conn.execute(
                "INSERT INTO ai_credentials (id, priority, kind, secret_sealed, dek_sealed,"
                " kek_id, last4) VALUES (%s, %s, 'anthropic_api_key', '\\x02', '\\x02', 'k', '')",
                (f"c{n}", n),
            )
    too_many = agent.post("/credentials", {"kind": "anthropic_api_key", "secret": API_KEY})
    assert (too_many.status_code, too_many.json()["detail"]) == (409, store.TOO_MANY_MESSAGE)


def test_without_a_kek_nothing_is_sealed_or_opened(tables: PgPool, tmp_path: Path) -> None:
    app = _app(tables, tmp_path, kek=None)
    with TestClient(app) as client:
        agent = Agent(client, _token(app))
        assert agent.get("/credentials/key").json() == {
            "ok": False,
            "kek_id": None,
            "reason": "the API has no key-encryption key (SCADBUDDY_SECRET_KEY_FILE)",
        }
        saved = agent.post("/credentials", {"kind": "anthropic_api_key", "secret": API_KEY})
        assert (saved.status_code, saved.json()["detail"]) == (503, store.NO_KEK_MESSAGE)
        reveal = agent.post("/credentials/reveal", {"all": True, "purpose": "turn"})
        assert (reveal.status_code, reveal.json()["reason"]) == (503, "kek-missing")
        key = agent.post(f"/payload-keys/session-{uuid.uuid4()}", {"create": True})
        assert (key.status_code, key.json()["reason"]) == (503, "kek-missing")
    assert _audit(tables) == []


def test_the_key_route_names_the_apis_key(agent: Agent) -> None:
    assert agent.get("/credentials/key").json() == {"ok": True, "kek_id": KEK.id, "reason": None}


def test_a_key_file_that_failed_to_read_is_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = [1000.0]
    monkeypatch.setattr(rotation, "_clock", lambda: now[0])
    reads: list[Path | None] = []

    def counted(path: Path | None) -> Kek | None:
        reads.append(path)
        return settings_kek(path)

    monkeypatch.setattr("scadbuddy.agent_data.rotation.settings_kek", counted)
    path = tmp_path / f"kek-{uuid.uuid4().hex}"
    with caplog.at_level("WARNING", logger="scadbuddy.agent_data.rotation"):
        assert rotation.kek_at(path) is None
        # Inside RETRY_AFTER_S a failed read stands: no request reads the file again.
        now[0] += 1
        assert rotation.kek_at(path) is None
        assert len(reads) == 1
        # Past it the file is read again, but the warning is not logged again so soon.
        now[0] += rotation.RETRY_AFTER_S
        assert rotation.kek_at(path) is None
        assert len(reads) == 2
    assert sum("cannot be read" in r.getMessage() for r in caplog.records) == 1
    path.write_text(KEK_B64)
    now[0] += rotation.RETRY_AFTER_S
    assert rotation.kek_at(path) == KEK
    path.unlink()
    # Kept once read: no request reads it again.
    assert rotation.kek_at(path) == KEK
    assert len(reads) == 3


def test_a_window_row_never_waits_and_never_stands_for_an_uncommitted_one(
    tables: PgPool,
) -> None:
    args: tuple[audit.Kind, tuple[str, ...], str] = ("credential", ("reveal.redact",), "10 minutes")
    with tables.connection() as holder, tables.connection() as other:
        holder.autocommit = False
        assert audit.recorded_lately(holder, *args) is False
        audit.record(holder, "credential", "reveal.redact", "ok", "opened 1 of 1 credential(s)")
        # The holder's row is not committed and its lock is taken: the other caller does not
        # wait for the lock, and writes its own row.
        other.execute("SET lock_timeout = '1s'")
        started = time.monotonic()
        assert audit.recorded_lately(other, *args) is False
        assert time.monotonic() - started < 1
        audit.record(other, "credential", "reveal.redact", "ok", "opened 1 of 1 credential(s)")
        # The holder rolls back: the reveal is still audited, by the other caller's row.
        holder.rollback()
        holder.autocommit = True
    assert [a["action"] for a in _audit(tables)] == ["reveal.redact"]
    with tables.connection() as conn:
        assert audit.recorded_lately(conn, *args) is True


def test_a_window_lock_held_by_a_caller_in_a_transaction(tables: PgPool) -> None:
    """As the reveal route calls it: both callers inside ``conn.transaction()``, so the
    other's try-lock, when it gets one, holds to its commit."""
    args: tuple[audit.Kind, tuple[str, ...], str] = ("credential", ("reveal.redact",), "10 minutes")
    with (
        tables.connection() as holder,
        tables.connection() as other,
        holder.transaction(force_rollback=True),
    ):
        assert audit.recorded_lately(holder, *args) is False
        audit.record(holder, "credential", "reveal.redact", "ok", "opened 1 of 1 credential(s)")
        other.execute("SET lock_timeout = '1s'")
        with other.transaction():
            started = time.monotonic()
            assert audit.recorded_lately(other, *args) is False
            assert time.monotonic() - started < 1
            audit.record(other, "credential", "reveal.redact", "ok", "opened 1 of 1 credential(s)")
    assert [a["action"] for a in _audit(tables)] == ["reveal.redact"]
    # Once one is committed, a caller in a transaction reads it with no lock.
    with tables.connection() as conn, conn.transaction():
        assert audit.recorded_lately(conn, *args) is True


def test_the_key_files_are_read_once_not_per_request(
    tables: PgPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[Path | None] = []
    real = settings_kek

    def counted(path: Path | None) -> Kek | None:
        reads.append(path)
        return real(path)

    monkeypatch.setattr("scadbuddy.agent_data.rotation.settings_kek", counted)
    app = _app(tables, tmp_path, kek=KEK_B64, previous=OLD_B64)
    subject = f"flow-{uuid.uuid4()}"
    with TestClient(app) as client:
        agent = Agent(client, _token(app))
        for _ in range(3):
            assert agent.get("/credentials/key").status_code == 200
            assert agent.post(f"/payload-keys/{subject}", {"create": True}).status_code == 200
    settings = app.state.scadbuddy.settings
    assert sorted(map(str, reads)) == sorted(
        [str(settings.secret_key_file), str(settings.secret_key_previous_file)]
    )


# --- payload keys -----------------------------------------------------------------------


def _key(answer: Any) -> bytes:
    assert answer.status_code == 200, answer.text
    return base64.b64decode(answer.json()["data_key"])


def test_a_key_is_made_once_and_released_audited_once_a_window(
    agent: Agent, tables: PgPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = f"session-{uuid.uuid4()}"
    missing = agent.post(f"/payload-keys/{subject}", {})
    assert (missing.status_code, missing.json()["reason"]) == (410, "subject-forgotten")
    made = _key(agent.post(f"/payload-keys/{subject}", {"create": True}))
    again = _key(agent.post(f"/payload-keys/{subject}", {"create": True}))
    read = _key(agent.post(f"/payload-keys/{subject}", {}))
    assert made == again == read and len(made) == 32
    (row,) = _sql(
        tables, "SELECT dek_sealed, kek_id FROM ai_payload_keys WHERE subject = %s", (subject,)
    )
    assert row["kek_id"] == KEK.id
    assert open_bytes(KEK.key, bytes(row["dek_sealed"]), payload_key_context(subject)) == made
    # Its own kind; the made key's row stands for the releases inside the window.
    audit = _audit(tables)
    assert [(a["kind"], a["action"], a["outcome"], a["detail"]) for a in audit] == [
        ("payload_key", "payload_key.release", "refused", f"{subject}: no key"),
        ("payload_key", "payload_key.create", "ok", subject),
    ]
    assert {a["session_id"] for a in audit} == {subject.removeprefix("session-")}
    # Past the window (none here: ai_audit is append-only, so no row can be made older)
    # each release writes its row again.
    monkeypatch.setattr(payload_keys, "RELEASE_AUDIT_WINDOW", "0 seconds")
    _key(agent.post(f"/payload-keys/{subject}", {}))
    _key(agent.post(f"/payload-keys/{subject}", {}))
    assert [(a["action"], a["outcome"]) for a in _audit(tables)][2:] == [
        ("payload_key.release", "ok"),
        ("payload_key.release", "ok"),
    ]


def test_flow_and_bridge_subjects_and_bad_ones(agent: Agent) -> None:
    for kind in ("flow", "bridge"):
        assert len(_key(agent.post(f"/payload-keys/{kind}-{uuid.uuid4()}", {"create": True}))) == 32
    assert agent.post("/payload-keys/thing-1", {"create": True}).status_code == 422
    assert agent.post(f"/payload-keys/session-{uuid.uuid4()}x", {}).status_code == 422
    flow = f"flow-{uuid.uuid4()}"
    assert (
        agent.post(
            f"/payload-keys/{flow}", {"create": True, "only_for_standing_session": True}
        ).status_code
        == 422
    )


def test_a_forgotten_subject_never_gets_a_key(agent: Agent, tables: PgPool) -> None:
    subject = f"session-{uuid.uuid4()}"
    _key(agent.post(f"/payload-keys/{subject}", {"create": True}))
    _sql(tables, "INSERT INTO ai_forgotten_subjects (subject) VALUES (%s)", (subject,))
    _sql(tables, "DELETE FROM ai_payload_keys WHERE subject = %s", (subject,))
    for body in ({"create": True}, {}):
        gone = agent.post(f"/payload-keys/{subject}", body)
        assert (gone.status_code, gone.json()["reason"]) == (410, "subject-forgotten")
    assert _sql(tables, "SELECT 1 FROM ai_payload_keys WHERE subject = %s", (subject,)) == []


def test_a_create_waits_for_a_forget_that_is_committing(
    agent: Agent, tables: PgPool, pg_conninfo: str
) -> None:
    """forgetSubject takes the subject's lock, writes the tombstone and deletes the key; a
    create that started meanwhile reads the tombstone once the forget commits."""
    subject = f"session-{uuid.uuid4()}"
    answers: list[Any] = []
    with psycopg.connect(pg_conninfo) as forget:
        forget.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (payload_key_lock(subject),))
        forget.execute("INSERT INTO ai_forgotten_subjects (subject) VALUES (%s)", (subject,))
        thread = threading.Thread(
            target=lambda: answers.append(agent.post(f"/payload-keys/{subject}", {"create": True}))
        )
        thread.start()
        time.sleep(0.5)
        assert answers == []
        forget.commit()
    thread.join(10)
    assert answers[0].status_code == 410
    assert _sql(tables, "SELECT 1 FROM ai_payload_keys WHERE subject = %s", (subject,)) == []


def test_a_standing_session_key_needs_its_row(agent: Agent, tables: PgPool) -> None:
    session = uuid.uuid4()
    subject = f"session-{session}"
    standing = {"create": True, "only_for_standing_session": True}
    assert agent.post(f"/payload-keys/{subject}", standing).status_code == 410
    _sql(
        tables,
        "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind,"
        " creator_id, status, max_turns, budget_usd) VALUES (%s, 'chat', 'browser', 'browser',"
        " 'You', 'browser', 'browser', 'running', 10, 1)",
        (session,),
    )
    assert len(_key(agent.post(f"/payload-keys/{subject}", standing))) == 32


def test_a_key_under_the_previous_kek_still_opens(tables: PgPool, tmp_path: Path) -> None:
    subject = f"flow-{uuid.uuid4()}"
    dek = b"\x44" * 32
    _sql(
        tables,
        "INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES (%s, %s, %s)",
        (subject, seal_bytes(OLD.key, dek, payload_key_context(subject)), OLD.id),
    )
    for previous, code in ((None, 409), (OLD_B64, 200)):
        app = _app(tables, tmp_path, kek=KEK_B64, previous=previous)
        with TestClient(app) as client:
            answer = Agent(client, _token(app)).post(f"/payload-keys/{subject}", {})
        assert answer.status_code == code, answer.text
    assert base64.b64decode(answer.json()["data_key"]) == dek


def test_two_creates_at_once_make_one_key(app: FastAPI) -> None:
    subject = f"flow-{uuid.uuid4()}"
    keys: list[bytes] = []
    with TestClient(app) as client:
        agent = Agent(client, _token(app))

        def ask() -> None:
            keys.append(_key(agent.post(f"/payload-keys/{subject}", {"create": True})))

        threads = [threading.Thread(target=ask) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert len(keys) == 4 and len(set(keys)) == 1


# --- rotation ---------------------------------------------------------------------------


def _seal_credential(kek: Kek, pool: PgPool, priority: int = 0) -> str:
    row_id = str(uuid.uuid4())
    envelope = seal_secret(kek, API_KEY, store.credential_aad(row_id, "anthropic_api_key", None))
    _sql(
        pool,
        "INSERT INTO ai_credentials (id, priority, kind, secret_sealed, dek_sealed, kek_id, last4)"
        " VALUES (%s, %s, 'anthropic_api_key', %s, %s, %s, '1234')",
        (row_id, priority, envelope.secret_sealed, envelope.dek_sealed, envelope.kek_id),
    )
    return row_id


def test_rotation_rewraps_credentials_and_payload_keys(tables: PgPool, tmp_path: Path) -> None:
    _seal_credential(OLD, tables)
    # A v1 row (#354) is left as it is, and counted.
    legacy = _seal_credential(OLD, tables, priority=1)
    _sql(
        tables,
        "UPDATE ai_credentials SET secret_sealed = set_byte(secret_sealed, 0, 1) WHERE id = %s",
        (legacy,),
    )
    subject = f"session-{uuid.uuid4()}"
    dek = b"\x55" * 32
    _sql(
        tables,
        "INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES (%s, %s, %s)",
        (subject, seal_bytes(OLD.key, dek, payload_key_context(subject)), OLD.id),
    )
    _sql(
        tables,
        "INSERT INTO ai_payload_keys (subject, dek_sealed, kek_id) VALUES (%s, '\\x0200', %s)",
        (f"flow-{uuid.uuid4()}", OLD.id),
    )
    with tables.connection() as conn:
        assert rotate(conn, OLD, KEK) == {
            "credentials": (1, 1),
            "payload_keys": (1, 1),
            "notify": (0, 0),
            "plugins": (0, 0),
            "session_files": (0, 0),
        }
        assert rotate(conn, OLD, KEK) == {
            "credentials": (0, 1),
            "payload_keys": (0, 1),
            "notify": (0, 0),
            "plugins": (0, 0),
            "session_files": (0, 0),
        }
        assert rotate(conn, KEK, KEK) == {}
    app = _app(tables, tmp_path, kek=KEK_B64)
    with TestClient(app) as client:
        agent = Agent(client, _token(app))
        rows = agent.post("/credentials/reveal", {"all": True, "purpose": "turn"}).json()[
            "credentials"
        ]
        assert rows[0]["credential"]["secret"] == API_KEY
        assert _key(agent.post(f"/payload-keys/{subject}", {})) == dek


# --- #7983: the claude_login kind, the scope and the machine lease -------------------------
# Core's internal/secrets/claude_login_test.go holds the Go copy of the routes to the same cases.

ACCESS = "sk-ant-oat01-access-token-vector-AAAA"
REFRESH = "sk-ant-ort01-refresh-token-vector-BBBB"
#: A claudeAiOauth object as Claude Code writes it, with a space and a newline in it.
LOGIN = (
    f'{{"accessToken": "{ACCESS}", "refreshToken": "{REFRESH}",\n'
    ' "expiresAt": 1795000000000, "refreshTokenExpiresAt": null,'
    ' "accountUuid": "8d2c3a4e-0000-4000-8000-00000000000a",'
    ' "organizationUuid": "8d2c3a4e-0000-4000-8000-00000000000b",'
    ' "rateLimitTier": "default_claude_max_20x", "scopes": ["user:inference"],'
    ' "subscriptionType": "max", "subscriptionTypeSource": "account"}'
)


def _created(agent: Agent, body: dict[str, Any]) -> dict[str, Any]:
    r = agent.post("/credentials", body)
    assert r.status_code == 201, r.text
    row: dict[str, Any] = r.json()["credential"]
    return row


def _fleet(tables: PgPool, body: dict[str, Any], row_id: str | None = None) -> dict[str, Any]:
    """A create (or, with ``row_id``, a save) by a caller other than the agent's: the agent's
    routes refuse a claude_login, a fleet row and a row whose scope is both (#8587), so the
    fleet's rows are written here."""
    update = store.Update(**body)
    with tables.connection() as conn:
        row = (
            store.create(conn, update, KEK, agent=False)
            if row_id is None
            else store.put(conn, update, KEK, row_id, agent=False)
        )
    return store.stored(row)


def _fleet_refused(tables: PgPool, body: dict[str, Any], row_id: str | None = None) -> int:
    with pytest.raises(store.CredentialRefusedError) as refused:
        _fleet(tables, body, row_id)
    status: int = refused.value.status
    return status


def test_a_claude_login_is_sealed_with_its_account_in_clear(agent: Agent, tables: PgPool) -> None:
    row = _fleet(tables, {"kind": "claude_login", "secret": LOGIN, "email": "ops@example.com"})
    assert (row["kind"], row["scope"], row["last4"], row["base_url"]) == (
        "claude_login",
        "fleet",
        "AAAA",
        None,
    )
    assert [
        row[k]
        for k in (
            "account_uuid",
            "organization_uuid",
            "subscription_type",
            "rate_limit_tier",
            "email",
        )
    ] == [
        "8d2c3a4e-0000-4000-8000-00000000000a",
        "8d2c3a4e-0000-4000-8000-00000000000b",
        "max",
        "default_claude_max_20x",
        "ops@example.com",
    ]
    assert (row["leased_to"], row["leased_at"], row["lease_epoch"]) == (None, None, 0)
    listed = agent.get("/credentials")
    one = agent.get("/credentials/one", id=row["id"])
    for raw in (listed.text, one.text):
        assert ACCESS not in raw and REFRESH not in raw
    assert listed.json()["credentials"][0]["scope"] == "fleet"
    assert one.json()["credential"]["email"] == "ops@example.com"
    # The agent's reveal never opens a fleet row: as for an unknown id.
    opened = agent.post("/credentials/reveal", {"id": row["id"], "purpose": "test"})
    assert opened.json() == {"credential": None, "credentials": None}
    with tables.connection() as conn:
        sealed = store.sealed_rows(conn, None)
        raw = conn.execute(
            "SELECT secret_sealed, dek_sealed, kek_id FROM ai_credentials WHERE id = %s",
            (row["id"],),
        ).fetchone()
    assert sealed == []
    assert raw is not None
    envelope = Envelope(
        secret_sealed=bytes(raw["secret_sealed"]),
        dek_sealed=bytes(raw["dek_sealed"]),
        kek_id=raw["kek_id"],
    )
    aad = store.credential_aad(row["id"], "claude_login", None)
    assert open_secret(KEK, envelope, aad) == LOGIN


def test_the_agents_reveal_never_opens_a_fleet_row(agent: Agent, tables: PgPool) -> None:
    login = _fleet(tables, {"kind": "claude_login", "secret": LOGIN})
    fleet = _fleet(tables, {"kind": "anthropic_api_key", "secret": API_KEY + "f", "scope": "fleet"})
    own = _created(agent, {"kind": "anthropic_api_key", "secret": API_KEY})
    both = _fleet(tables, {"kind": "anthropic_api_key", "secret": API_KEY + "b", "scope": "both"})
    every = agent.post("/credentials/reveal", {"all": True, "purpose": "turn"})
    assert [r["stored"]["id"] for r in every.json()["credentials"]] == [own["id"], both["id"]]
    assert REFRESH not in every.text and API_KEY + "f" not in every.text
    for row in (login, fleet):
        one = agent.post("/credentials/reveal", {"id": row["id"], "purpose": "test"})
        assert (one.status_code, one.json()) == (200, {"credential": None, "credentials": None})
    first = agent.post("/credentials/reveal", {"purpose": "test"}).json()["credential"]
    assert first["secret"] == API_KEY
    assert len(agent.get("/credentials").json()["credentials"]) == 4


def _secrets(tables: PgPool) -> dict[str, str]:
    """Every row's secret, opened with the KEK: what a save left stored."""
    with tables.connection() as conn:
        found = conn.execute(
            "SELECT id, secret_sealed, dek_sealed, kek_id, kind, base_url FROM ai_credentials"
        ).fetchall()
    return {
        r["id"]: open_secret(
            KEK,
            Envelope(
                secret_sealed=bytes(r["secret_sealed"]),
                dek_sealed=bytes(r["dek_sealed"]),
                kek_id=r["kek_id"],
            ),
            store.credential_aad(r["id"], r["kind"], r["base_url"]),
        )
        for r in found
    }


def test_the_agents_caller_never_writes_a_fleet_row(agent: Agent, tables: PgPool) -> None:
    """A fleet machine installs a claude_login, so the agent's caller may not plant one, nor
    replace (or change) a row whose scope is fleet, nor make a row the fleet's."""
    for body in (
        {"kind": "claude_login", "secret": LOGIN},
        {"kind": "claude_login", "secret": LOGIN, "scope": "fleet"},
        {"kind": "anthropic_api_key", "secret": API_KEY, "scope": "fleet"},
    ):
        r = agent.post("/credentials", body)
        assert (r.status_code, r.json()["reason"]) == (409, "credential-refused"), r.text
        assert r.json()["detail"] == store.FLEET_ROW_MESSAGE
    login = _fleet(tables, {"kind": "claude_login", "secret": LOGIN})
    fleet = _fleet(tables, {"kind": "anthropic_api_key", "secret": API_KEY + "f", "scope": "fleet"})
    own = _created(agent, {"kind": "anthropic_api_key", "secret": API_KEY})
    for row_id, update in (
        (login["id"], {"kind": "claude_login", "secret": LOGIN.replace("AAAA", "CCCC")}),
        (login["id"], {"kind": "claude_login", "email": "other@example.com"}),
        (login["id"], {"kind": "anthropic_api_key", "secret": API_KEY}),
        (fleet["id"], {"kind": "anthropic_api_key", "secret": API_KEY + "x"}),
        (fleet["id"], {"kind": "anthropic_api_key", "secret": API_KEY + "x", "scope": "both"}),
        (own["id"], {"kind": "claude_login", "secret": LOGIN}),
        (own["id"], {"kind": "anthropic_api_key", "scope": "fleet"}),
    ):
        r = agent.post("/credentials/save", {"id": row_id, "update": update})
        assert (r.status_code, r.json()["reason"]) == (409, "credential-refused"), (update, r.text)
    listed = {c["id"]: c for c in agent.get("/credentials").json()["credentials"]}
    assert len(listed) == 3
    assert [listed[i]["epoch"] for i in (login["id"], fleet["id"], own["id"])] == [0, 0, 0]
    assert (listed[login["id"]]["kind"], listed[login["id"]]["email"]) == ("claude_login", None)
    assert listed[own["id"]]["scope"] == "agent"
    assert _secrets(tables) == {
        login["id"]: LOGIN,
        fleet["id"]: API_KEY + "f",
        own["id"]: API_KEY,
    }


def _state(agent: Agent) -> dict[str, tuple[Any, ...]]:
    """Every row's priority, name, status, epoch and last error, by id."""
    return {
        c["id"]: (c["priority"], c["name"], c["status"], c["epoch"], c["last_error"])
        for c in agent.get("/credentials").json()["credentials"]
    }


def test_the_agents_caller_changes_no_fleet_row(agent: Agent, tables: PgPool) -> None:
    """A compromised agent pod may not delete, rename, reset, cool down, disable or reorder a
    fleet login (#7983): each is the save's 409, and the row is as it was."""
    login = _fleet(tables, {"kind": "claude_login", "secret": LOGIN})
    own = _created(agent, {"kind": "anthropic_api_key", "secret": API_KEY})
    fleet = _fleet(tables, {"kind": "anthropic_api_key", "secret": API_KEY + "f", "scope": "fleet"})
    both = _fleet(tables, {"kind": "anthropic_api_key", "secret": API_KEY + "b", "scope": "both"})
    before = _state(agent)
    for row in (login, fleet):
        for path, body in (
            ("/credentials/delete", {"id": row["id"]}),
            ("/credentials/reset", {"id": row["id"]}),
            ("/credentials/name", {"id": row["id"], "name": "Mine"}),
            ("/credentials/events", {"id": row["id"], "epoch": 0, "event": {"kind": "used"}}),
            (
                "/credentials/events",
                {"id": row["id"], "epoch": 0, "event": {"kind": "disabled", "reason": "401"}},
            ),
            (
                "/credentials/events",
                {
                    "id": row["id"],
                    "epoch": 0,
                    "event": {
                        "kind": "cooling_down",
                        "until": "2099-01-01T00:00:00Z",
                        "reason": "429",
                    },
                },
            ),
        ):
            r = agent.post(path, body)
            assert (r.status_code, r.json().get("reason")) == (409, "credential-refused"), (
                path,
                r.text,
            )
            assert r.json()["detail"] == store.FLEET_ROW_MESSAGE
    # An order that names a fleet row, or every row, is refused; the agent's rows alone move.
    for ids in (
        [both["id"], own["id"], login["id"], fleet["id"]],
        [login["id"], both["id"], own["id"]],
    ):
        r = agent.post("/credentials/order", {"ids": ids})
        assert (r.status_code, r.json()["detail"]) == (409, store.FLEET_ROW_MESSAGE), r.text
    assert _state(agent) == before
    order = agent.post("/credentials/order", {"ids": [both["id"], own["id"]]})
    assert order.status_code == 200, order.text
    assert [c["id"] for c in order.json()["credentials"]] == [
        login["id"],
        both["id"],
        fleet["id"],
        own["id"],
    ]
    priorities = {i: p for i, (p, *_) in _state(agent).items()}
    assert (priorities[login["id"]], priorities[fleet["id"]]) == (0, 2)
    # With no id, a read, a save and a delete go to the first row the agent uses, never the
    # fleet's login before it. That row's scope is both, so the save and the delete are
    # refused (#8587).
    first = agent.get("/credentials/one").json()["credential"]
    assert first["id"] == both["id"]
    saved = agent.post(
        "/credentials/save", {"update": {"kind": "anthropic_api_key", "secret": "k"}}
    )
    assert (saved.status_code, saved.json()["detail"]) == (409, store.SHARED_ROW_MESSAGE), (
        saved.text
    )
    gone = agent.post("/credentials/delete", {})
    assert (gone.status_code, gone.json()["detail"]) == (409, store.SHARED_ROW_MESSAGE), gone.text
    assert agent.post("/credentials/delete", {"id": own["id"]}).json() == {"deleted": True}
    left = _state(agent)
    assert set(left) == {login["id"], both["id"], fleet["id"]}
    # The rest close up; the fleet's rows are otherwise as they were.
    assert left[login["id"]] == before[login["id"]]
    assert left[fleet["id"]] == before[fleet["id"]]
    assert _secrets(tables)[login["id"]] == LOGIN
    assert _secrets(tables)[both["id"]] == API_KEY + "b"


def _shared_refused(r: Any) -> None:
    assert (r.status_code, r.json().get("reason"), r.json().get("detail")) == (
        409,
        "credential-refused",
        store.SHARED_ROW_MESSAGE,
    ), r.text


def test_the_agents_caller_never_widens_a_row_to_the_fleet(agent: Agent, tables: PgPool) -> None:
    """The fleet's rotator reads a row whose scope is both (#8587), so the agent's caller may
    not make one, nor move its own row to both."""
    _shared_refused(
        agent.post(
            "/credentials", {"kind": "anthropic_api_key", "secret": API_KEY, "scope": "both"}
        )
    )
    _shared_refused(
        agent.post(
            "/credentials",
            {
                "kind": "gateway",
                "base_url": "https://gw.example.com",
                "secret": API_KEY,
                "scope": "both",
            },
        )
    )
    own = _created(agent, {"kind": "anthropic_api_key", "secret": API_KEY, "scope": "agent"})
    assert own["scope"] == "agent"
    for update in (
        {"kind": "anthropic_api_key", "scope": "both"},
        {"kind": "anthropic_api_key", "secret": API_KEY + "x", "scope": "both"},
    ):
        _shared_refused(agent.post("/credentials/save", {"id": own["id"], "update": update}))
    # With no id, the save goes to the agent's first row, and is refused the same way.
    _shared_refused(
        agent.post("/credentials/save", {"update": {"kind": "anthropic_api_key", "scope": "both"}})
    )
    listed = agent.get("/credentials").json()["credentials"]
    assert [(c["id"], c["scope"], c["epoch"]) for c in listed] == [(own["id"], "agent", 0)]
    assert _secrets(tables) == {own["id"]: API_KEY}


def test_the_agents_caller_never_rewrites_a_shared_row(agent: Agent, tables: PgPool) -> None:
    """A row whose scope is both (#8587): the agent's caller may not change its secret, kind,
    base URL or scope, delete it or disable it. Each is a 409, and the row is as it was."""
    shared = _fleet(tables, {"kind": "anthropic_api_key", "secret": API_KEY + "b", "scope": "both"})
    gateway = _fleet(
        tables,
        {
            "kind": "gateway",
            "base_url": "https://gw.example.com",
            "secret": API_KEY + "g",
            "scope": "both",
        },
    )
    before = _state(agent)
    for row_id, update in (
        (shared["id"], {"kind": "anthropic_api_key", "secret": API_KEY + "x"}),
        (shared["id"], {"kind": "anthropic_api_key", "secret": API_KEY + "x", "scope": "agent"}),
        (shared["id"], {"kind": "anthropic_api_key", "scope": "agent"}),
        (shared["id"], {"kind": "claude_oauth_token", "secret": "sk-ant-oat01-x"}),
        (
            shared["id"],
            {"kind": "gateway", "base_url": "https://evil.example.com", "secret": API_KEY},
        ),
        (
            gateway["id"],
            {"kind": "gateway", "base_url": "https://evil.example.com", "secret": API_KEY},
        ),
        (gateway["id"], {"kind": "gateway", "base_url": "https://gw.example.com"}),
    ):
        _shared_refused(agent.post("/credentials/save", {"id": row_id, "update": update}))
    for row in (shared, gateway):
        _shared_refused(agent.post("/credentials/delete", {"id": row["id"]}))
        disabled = {"id": row["id"], "epoch": 0, "event": {"kind": "disabled", "reason": "401"}}
        _shared_refused(agent.post("/credentials/events", disabled))
    _shared_refused(agent.post("/credentials/delete", {}))
    assert _state(agent) == before
    listed = {c["id"]: c for c in agent.get("/credentials").json()["credentials"]}
    assert [(listed[r["id"]]["kind"], listed[r["id"]]["base_url"]) for r in (shared, gateway)] == [
        ("anthropic_api_key", None),
        ("gateway", "https://gw.example.com"),
    ]
    assert _secrets(tables) == {shared["id"]: API_KEY + "b", gateway["id"]: API_KEY + "g"}


def test_the_agent_still_uses_a_shared_row(agent: Agent, tables: PgPool) -> None:
    """The agent uses a row whose scope is both (#8587): it reads and opens it, renames, resets
    and reorders it, and records its use, a rate limit and a transient error on it."""
    shared = _fleet(tables, {"kind": "anthropic_api_key", "secret": API_KEY + "b", "scope": "both"})
    own = _created(agent, {"kind": "anthropic_api_key", "secret": API_KEY})
    opened = agent.post("/credentials/reveal", {"id": shared["id"], "purpose": "test"})
    assert opened.json()["credential"]["secret"] == API_KEY + "b"
    used = {"id": shared["id"], "epoch": 0, "event": {"kind": "used"}}
    assert agent.post("/credentials/events", used).json()["transition"]["after"] == "active"
    transient = {"id": shared["id"], "epoch": 0, "event": {"kind": "transient", "reason": "503"}}
    assert agent.post("/credentials/events", transient).json()["transition"]["after"] == "active"
    cool = {
        "id": shared["id"],
        "epoch": 0,
        "event": {"kind": "cooling_down", "until": "2099-01-01T00:00:00Z", "reason": "429"},
    }
    assert agent.post("/credentials/events", cool).json()["transition"]["after"] == "cooling_down"
    # The agent's cooldown of a shared row ends within the hour; of its own row, when it says.
    own_cool = {**cool, "id": own["id"]}
    cooled = agent.post("/credentials/events", own_cool).json()
    assert cooled["transition"]["after"] == "cooling_down"
    with tables.connection() as conn:
        found = conn.execute(
            "SELECT id, cooldown_until <= now() + interval '1 hour' AS soon FROM ai_credentials"
            " WHERE id = ANY(%s)",
            ([shared["id"], own["id"]],),
        ).fetchall()
    ends = {r["id"]: r["soon"] for r in found}
    assert ends == {shared["id"]: True, own["id"]: False}
    reset = agent.post("/credentials/reset", {"id": shared["id"]}).json()["credential"]
    assert (reset["status"], reset["epoch"]) == ("active", 1)
    renamed = agent.post("/credentials/name", {"id": shared["id"], "name": "Shared"})
    assert renamed.json()["credential"]["name"] == "Shared"
    ordered = agent.post("/credentials/order", {"ids": [own["id"], shared["id"]]})
    assert [c["id"] for c in ordered.json()["credentials"]] == [own["id"], shared["id"]]
    row = agent.get("/credentials/one", id=shared["id"]).json()["credential"]
    assert (row["scope"], row["kind"]) == ("both", "anthropic_api_key")
    assert _secrets(tables)[shared["id"]] == API_KEY + "b"


def test_the_fleets_caller_still_changes_its_rows(agent: Agent, tables: PgPool) -> None:
    """``agent=False`` (the fleet's own caller, #7984) still deletes, renames, resets, records
    and reorders a fleet row."""
    login = _fleet(tables, {"kind": "claude_login", "secret": LOGIN})
    fleet = _fleet(tables, {"kind": "anthropic_api_key", "secret": API_KEY + "f", "scope": "fleet"})
    own = _created(agent, {"kind": "anthropic_api_key", "secret": API_KEY})
    event = store.Event(kind="disabled", reason="401")
    with tables.connection() as conn:
        assert store.rename(conn, login["id"], "Ops", agent=False) is not None
        recorded = store.record_event(conn, login["id"], 0, event, agent=False)
        assert recorded is not None and recorded["after"] == "disabled"
        reset = store.reset(conn, login["id"], agent=False)
        assert reset is not None and (reset["status"], reset["epoch"]) == ("active", 1)
        ordered = store.reorder(conn, [own["id"], fleet["id"], login["id"]], agent=False)
        assert [r["id"] for r in ordered] == [own["id"], fleet["id"], login["id"]]
        assert store.get(conn, None, agent=False)["id"] == own["id"]  # type: ignore[index]
        assert store.delete(conn, fleet["id"], agent=False)
        assert store.delete(conn, None, agent=False)
    assert [c["id"] for c in agent.get("/credentials").json()["credentials"]] == [login["id"]]
    assert agent.get("/credentials").json()["credentials"][0]["name"] == "Ops"


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "claude_login", "secret": "not json"},
        {"kind": "claude_login", "secret": '["a"]'},
        {"kind": "claude_login", "secret": '{"accessToken": "x", "expiresAt": 1}'},
        {
            "kind": "claude_login",
            "secret": '{"accessToken": "x", "refreshToken": "y", "expiresAt": "soon"}',
        },
        {
            "kind": "claude_login",
            "secret": '{"accessToken": "x", "refreshToken": "y", "expiresAt": 1, "accountUuid": 7}',
        },
        {"kind": "claude_login", "secret": LOGIN, "base_url": "https://gw.example.com"},
        {"kind": "anthropic_api_key", "secret": API_KEY, "email": "ops@example.com"},
        {"kind": "claude_login", "secret": LOGIN, "email": "two words@example.com"},
    ],
)
def test_a_claude_login_keeps_the_kinds_rules(tables: PgPool, body: dict[str, Any]) -> None:
    assert _fleet_refused(tables, body) == 400


def test_a_scope_outside_the_three_is_invalid(agent: Agent, tables: PgPool) -> None:
    r = agent.post("/credentials", {"kind": "anthropic_api_key", "secret": API_KEY, "scope": "x"})
    assert r.status_code == 422
    for scope in ("agent", "both"):
        body = {"kind": "claude_login", "secret": LOGIN, "scope": scope}
        assert _fleet_refused(tables, body) == 422
    fleet = _fleet(tables, {"kind": "claude_login", "secret": LOGIN, "scope": "fleet"})
    assert (fleet["scope"], fleet["email"]) == ("fleet", None)


def test_the_scope_defaults_to_the_agent_and_a_save_moves_it(agent: Agent, tables: PgPool) -> None:
    def save(row_id: str, update: dict[str, Any]) -> dict[str, Any]:
        r = agent.post("/credentials/save", {"id": row_id, "update": update})
        assert r.status_code == 200, r.text
        out: dict[str, Any] = r.json()["credential"]
        return out

    key = _created(agent, {"kind": "anthropic_api_key", "secret": API_KEY})
    assert (key["scope"], key["account_uuid"], key["email"]) == ("agent", None, None)
    assert save(key["id"], {"kind": "anthropic_api_key", "scope": "agent"})["scope"] == "agent"
    # Only a caller other than the agent's widens a row to both (#8587).
    moved = _fleet(tables, {"kind": "anthropic_api_key", "scope": "both"}, key["id"])
    assert (moved["scope"], moved["epoch"]) == ("both", 0)
    kept = _fleet(tables, {"kind": "anthropic_api_key", "secret": API_KEY + "y"}, key["id"])
    assert (kept["scope"], kept["epoch"]) == ("both", 1)
    # The fleet's caller turns it into a login, and back.
    as_login = _fleet(
        tables,
        {"kind": "claude_login", "secret": LOGIN, "email": "ops@example.com"},
        key["id"],
    )
    assert (as_login["scope"], as_login["subscription_type"], as_login["email"]) == (
        "fleet",
        "max",
        "ops@example.com",
    )
    back = _fleet(tables, {"kind": "anthropic_api_key", "secret": API_KEY}, key["id"])
    assert (back["account_uuid"], back["email"]) == (None, None)
    agent_key = _created(agent, {"kind": "anthropic_api_key", "secret": API_KEY + "z"})
    login = _fleet(tables, {"kind": "claude_login", "secret": LOGIN}, agent_key["id"])
    assert login["scope"] == "fleet"
    mailed = _fleet(tables, {"kind": "claude_login", "email": "new@example.com"}, agent_key["id"])
    assert (mailed["email"], mailed["epoch"]) == ("new@example.com", login["epoch"])


def test_a_scope_or_email_save_moves_updated_at(agent: Agent, tables: PgPool) -> None:
    key = _created(agent, {"kind": "anthropic_api_key", "secret": API_KEY})
    login = _fleet(tables, {"kind": "claude_login", "secret": LOGIN})
    with tables.connection() as conn:
        conn.execute("UPDATE ai_credentials SET updated_at = '2026-01-01T00:00:00Z'")
    moved = _fleet(tables, {"kind": "anthropic_api_key", "scope": "both"}, key["id"])
    mailed = _fleet(tables, {"kind": "claude_login", "email": "ops@example.com"}, login["id"])
    for row in (moved, mailed):
        assert row["epoch"] == 0
        assert not row["updated_at"].startswith("2026-01-01"), row


def test_widening_a_fleet_row_to_the_agent_needs_its_secret_again(
    agent: Agent, tables: PgPool
) -> None:
    fleet = _fleet(tables, {"kind": "anthropic_api_key", "secret": API_KEY + "f", "scope": "fleet"})
    for scope in ("agent", "both"):
        update = {"kind": "anthropic_api_key", "scope": scope}
        with pytest.raises(store.CredentialRefusedError) as refused:
            _fleet(tables, update, fleet["id"])
        assert (refused.value.status, str(refused.value)) == (422, store.WIDENING_MESSAGE)
        row = agent.get("/credentials/one", id=fleet["id"]).json()["credential"]
        assert (row["scope"], row["epoch"]) == ("fleet", 0)
        one = agent.post("/credentials/reveal", {"id": fleet["id"], "purpose": "test"})
        assert one.json() == {"credential": None, "credentials": None}
    # With its secret in the same save, the row widens and its epoch moves.
    widened = _fleet(
        tables,
        {"kind": "anthropic_api_key", "secret": API_KEY + "w", "scope": "both"},
        fleet["id"],
    )
    assert (widened["scope"], widened["epoch"]) == ("both", 1)
    opened = agent.post("/credentials/reveal", {"id": fleet["id"], "purpose": "test"})
    assert opened.json()["credential"]["secret"] == API_KEY + "w"
    # A narrowing needs no secret and keeps the epoch.
    narrowed = _fleet(tables, {"kind": "anthropic_api_key", "scope": "fleet"}, fleet["id"])
    assert (narrowed["scope"], narrowed["epoch"]) == ("fleet", 1)


def test_only_a_login_holds_a_lease_or_an_account(agent: Agent, tables: PgPool) -> None:
    key = _created(agent, {"kind": "anthropic_api_key", "secret": API_KEY})
    held = _fleet(tables, {"kind": "claude_login", "secret": LOGIN})
    # Two machines may each hold a login to one account.
    _fleet(tables, {"kind": "claude_login", "secret": LOGIN})
    with pytest.raises(psycopg.errors.CheckViolation), tables.connection() as conn:
        conn.execute("UPDATE ai_credentials SET scope = 'both' WHERE id = %s", (held["id"],))
    for sql in (
        "UPDATE ai_credentials SET leased_to = 'm1', leased_at = now() WHERE id = %s",
        "UPDATE ai_credentials SET email = 'ops@example.com' WHERE id = %s",
        "UPDATE ai_credentials SET scope = 'nobody' WHERE id = %s",
    ):
        with pytest.raises(psycopg.errors.CheckViolation), tables.connection() as conn:
            conn.execute(sql, (key["id"],))
    with pytest.raises(psycopg.errors.CheckViolation), tables.connection() as conn:
        conn.execute("UPDATE ai_credentials SET leased_to = 'm1' WHERE id = %s", (held["id"],))
    with tables.connection() as conn:
        conn.execute(
            "UPDATE ai_credentials SET leased_to = 'm1', leased_at = '2026-11-26T00:00:00Z',"
            " lease_epoch = 3 WHERE id = %s",
            (held["id"],),
        )
    leased = agent.get("/credentials/one", id=held["id"]).json()["credential"]
    assert (leased["leased_to"], leased["leased_at"], leased["lease_epoch"]) == (
        "m1",
        "2026-11-26T00:00:00+00:00",
        3,
    )


def test_a_kind_change_that_ends_a_lease_bumps_lease_epoch(tables: PgPool) -> None:
    held = _fleet(tables, {"kind": "claude_login", "secret": LOGIN})
    with tables.connection() as conn:
        conn.execute(
            "UPDATE ai_credentials SET leased_to = 'm1', leased_at = now(), lease_epoch = 3"
            " WHERE id = %s",
            (held["id"],),
        )
    # The lease ends with the kind, and each lease change bumps lease_epoch, so the machine
    # that held it cannot write back.
    ended = _fleet(tables, {"kind": "anthropic_api_key", "secret": API_KEY}, held["id"])
    assert (ended["leased_to"], ended["leased_at"], ended["lease_epoch"]) == (None, None, 4)
    # A kind change with no lease held changes no lease, so the epoch stays.
    _fleet(tables, {"kind": "claude_login", "secret": LOGIN}, held["id"])
    again = _fleet(tables, {"kind": "anthropic_api_key", "secret": API_KEY}, held["id"])
    assert again["lease_epoch"] == 4
