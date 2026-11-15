"""The agent's remote plugins and their sign-ins behind its CRUD routes (#5299 step 5, area
``plugins``, `api/crud_agent_plugins.py`): the API seals and opens them with the KEK under
the agent's own AADs, so the agent needs none, each write sends the warm eviction in its
transaction, and a token refresh is kept only under its lease."""

from __future__ import annotations

import base64
import json
import time
import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.agent_data import leases
from scadbuddy.agent_data.rotation import rotate
from scadbuddy.core.secrets import Envelope, kek_from_base64, open_secret, seal_secret
from scadbuddy.core.settings import Settings
from tests.api.test_crud_agent_cost import token

pytestmark = [pytest.mark.requires_postgres, pytest.mark.agent_migrations]

BASE = "/api/v1/internal/crud/agent"
KEY_TEXT = base64.b64encode(b"\x42" * 32).decode()
KEK = kek_from_base64(KEY_TEXT)
URL = "https://mcp.example/mcp/"
#: The AAD exactly as the agent's `pluginAad` writes it (JSON.stringify, no spaces).
PLUGIN_AAD = 'ai_plugins:{"name":"docs","url":"https://mcp.example/mcp/","auth_header":"X-Key"}'
H = "ab" * 32
META = {
    "issuer": "https://auth.example",
    "authorization_endpoint": "https://auth.example/authorize",
    "token_endpoint": "https://auth.example/token",
    "iss_supported": True,
    "resource": URL,
    "scope": "read",
    "client_id": "client-1",
    "auth_method": "none",
    "registration": "dynamic",
    "redirect_uri": "https://scadbuddy.example/api/v1/ai/plugin-oauth/callback",
}


@pytest.fixture
def settings(settings: Settings, tmp_path: Path) -> Settings:
    key = tmp_path / "kek"
    key.write_text(KEY_TEXT)
    return settings.model_copy(update={"secret_key_file": key})


@pytest.fixture
def agent(client: TestClient) -> dict[str, str]:
    return token(client)


@pytest.fixture
def db(pg_conninfo: str) -> Any:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        yield conn


@pytest.fixture
def listener(pg_conninfo: str) -> Any:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        conn.execute("LISTEN scadbuddy_warm_evict")
        yield conn


def _heard(listener: Any) -> list[str]:
    return [n.payload for n in listener.notifies(timeout=0.5)]


def _envelope(row: Any) -> Envelope:
    return Envelope(secret_sealed=bytes(row[0]), dek_sealed=bytes(row[1]), kek_id=row[2])


def _create(client: TestClient, agent: dict[str, str], **extra: Any) -> Any:
    body = {"name": "docs", "url": URL, "by": "system", **extra}
    return client.post(f"{BASE}/plugins", json=body, headers=agent)


def test_only_the_agent_credential_reaches_the_routes(client: TestClient) -> None:
    refused = client.get(f"{BASE}/plugins", headers=token(client, "print"))
    assert refused.status_code == 403, refused.text
    assert client.get(f"{BASE}/plugins").status_code == 401


def test_a_plugin_is_made_disabled_and_its_secret_sealed_as_the_agent_seals_it(
    client: TestClient, agent: dict[str, str], db: Any, listener: Any
) -> None:
    made = _create(
        client,
        agent,
        auth_header="X-Key",
        secret="sk-secret-value-1234",
        tools={"search": "read", "drop": "off"},
    )
    assert made.status_code == 201, made.text
    view = made.json()
    assert (view["last4"], view["opens"], view["auth_header"]) == ("1234", True, "X-Key")
    rows = {(r["item"], r["access"]) for r in view["access_rows"]}
    assert rows == {("*", "off"), ("search", "read"), ("drop", "off")}
    assert "*" in _heard(listener)
    sealed = db.execute(
        "SELECT secret_sealed, dek_sealed, kek_id FROM ai_plugins WHERE name = 'docs'"
    ).fetchone()
    assert open_secret(KEK, _envelope(sealed), PLUGIN_AAD) == "sk-secret-value-1234"
    again = _create(client, agent)
    assert (again.status_code, again.json()["reason"]) == (409, "plugin-refused")
    assert client.get(f"{BASE}/plugins/nope", headers=agent).json() == {"plugin": None}
    assert [p["name"] for p in client.get(f"{BASE}/plugins", headers=agent).json()["plugins"]] == [
        "docs"
    ]


def test_an_update_keeps_the_secret_only_where_it_was_bound(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    _create(client, agent, auth_header="X-Key", secret="sk-secret-value-1234")

    def update(**body: Any) -> Any:
        return client.post(f"{BASE}/plugins/docs/update", json=body, headers=agent)

    moved = update(url="https://other.example/mcp/")
    assert (moved.status_code, moved.json()["reason"]) == (409, "plugin-refused")
    assert "needs the secret again" in moved.json()["detail"]
    assert update(auth_header="X-New").status_code == 409
    assert update(secret=None, auth_header="X-Key").status_code == 400
    assert update(nope=1).status_code == 422
    missing = client.post(f"{BASE}/plugins/gone/update", json={}, headers=agent)
    assert missing.status_code == 404
    # A new secret at a new URL: sealed for the new URL, the sign-in and listing dropped.
    db.execute("INSERT INTO ai_tool_listings (source, tools) VALUES ('remote:docs', '[]'::jsonb)")
    done = update(url="https://other.example/mcp/", secret="sk-another-value-9876")
    assert done.status_code == 200, done.text
    assert done.json()["last4"] == "9876"
    assert db.execute("SELECT count(*) FROM ai_tool_listings").fetchone() == (0,)
    sealed = db.execute(
        "SELECT secret_sealed, dek_sealed, kek_id FROM ai_plugins WHERE name = 'docs'"
    ).fetchone()
    aad = 'ai_plugins:{"name":"docs","url":"https://other.example/mcp/","auth_header":"X-Key"}'
    assert open_secret(KEK, _envelope(sealed), aad) == "sk-another-value-9876"
    # Enabled and tiers: only the rows the patch names.
    on = update(enabled=True, tool_tiers={"search": "write"}, by="browser:browser")
    rows = {(r["item"], r["access"], r["updated_by"]) for r in on.json()["access_rows"]}
    assert rows == {("*", "on", "browser:browser"), ("search", "write", "browser:browser")}
    removed = update(secret=None)
    assert (removed.json()["auth_header"], removed.json()["last4"]) == (None, None)


def test_a_tool_access_write_keeps_the_other_rows_author(
    client: TestClient, agent: dict[str, str]
) -> None:
    _create(client, agent, tools={"a": "read"})
    first = client.post(
        f"{BASE}/plugins/docs/update",
        json={"tool_tiers": {"a": "read"}, "by": "browser:first"},
        headers=agent,
    )
    assert first.status_code == 200, first.text
    set_b = client.post(
        f"{BASE}/plugins/docs/tool-access",
        json={"tool": "b", "access": "off", "by": "browser:second"},
        headers=agent,
    )
    assert set_b.status_code == 200, set_b.text
    rows = {(r["item"], r["access"], r["updated_by"]) for r in set_b.json()["access_rows"]}
    assert ("a", "read", "browser:first") in rows
    assert ("b", "off", "browser:second") in rows
    dropped = client.post(
        f"{BASE}/plugins/docs/tool-access",
        json={"tool": "b", "access": None, "by": "browser:second"},
        headers=agent,
    )
    assert {r["item"] for r in dropped.json()["access_rows"]} == {"*", "a"}


def test_reveal_and_open_enabled_open_the_header_and_never_fail_on_a_bad_row(
    client: TestClient, agent: dict[str, str], db: Any, settings: Settings
) -> None:
    _create(client, agent, auth_header="X-Key", secret="sk-secret-value-1234")
    assert client.post(f"{BASE}/plugins/open-enabled", headers=agent).json() == {"plugins": []}
    client.post(f"{BASE}/plugins/docs/update", json={"enabled": True}, headers=agent)
    [opened] = client.post(f"{BASE}/plugins/open-enabled", headers=agent).json()["plugins"]
    assert opened["header"] == {"name": "X-Key", "value": "sk-secret-value-1234"}
    revealed = client.post(f"{BASE}/plugins/docs/reveal", headers=agent).json()["plugin"]
    assert revealed["header"]["value"] == "sk-secret-value-1234"
    assert client.post(f"{BASE}/plugins/nope/reveal", headers=agent).json() == {"plugin": None}
    # A row altered in the database fails to open: an answer, never a 500.
    db.execute("UPDATE ai_plugins SET url = 'https://evil.example/' WHERE name = 'docs'")
    bad = client.post(f"{BASE}/plugins/docs/reveal", headers=agent).json()["plugin"]
    assert bad["header"] is None and bad["seal_error"]
    # No KEK on the API: the plugin is listed, not usable, and its secret not opened.
    app: Any = client.app
    app.state.scadbuddy.settings = settings.model_copy(update={"secret_key_file": None})
    view = client.get(f"{BASE}/plugins/docs", headers=agent).json()["plugin"]
    assert view["opens"] is False
    no_key = client.post(f"{BASE}/plugins/docs/reveal", headers=agent).json()["plugin"]
    assert "no key-encryption key" in no_key["seal_error"]
    refused = _create(client, agent, name="other", auth_header="X-Key", secret="s" * 20)
    assert (refused.status_code, refused.json()["reason"]) == (503, "kek-missing")
    # A plugin with no secret needs no key.
    assert _create(client, agent, name="plain").status_code == 201


def test_a_sign_in_runs_start_authorize_take_and_store(
    client: TestClient, agent: dict[str, str], db: Any, listener: Any
) -> None:
    _create(client, agent)
    start = {
        "state_hash": H,
        "plugin": "docs",
        "plugin_url": URL,
        "ticket_hash": "cd" * 32,
        "meta": META,
        "secret": {"state": "st", "verifier": "ve"},
        "created_by": "browser",
        "ttl_ms": 600_000,
    }
    assert client.post(f"{BASE}/plugin-oauth/flows", json=start, headers=agent).status_code == 201
    gone = client.post(
        f"{BASE}/plugin-oauth/flows",
        json={**start, "plugin": "nope", "state_hash": "12" * 32, "ticket_hash": "34" * 32},
        headers=agent,
    )
    assert (gone.status_code, gone.json()["reason"]) == (409, "plugin-gone")
    ask = {"ticket_hash": "cd" * 32, "binding_hash": "ef" * 32}
    flow = client.post(f"{BASE}/plugin-oauth/flows/authorize", json=ask, headers=agent).json()
    assert flow["flow"]["secret"] == {"state": "st", "verifier": "ve", "client_secret": None}
    # A ticket is good once.
    spent = client.post(f"{BASE}/plugin-oauth/flows/authorize", json=ask, headers=agent).json()
    assert spent == {"flow": None}
    other = client.post(
        f"{BASE}/plugin-oauth/flows/take",
        json={"state_hash": H, "binding_hash": ""},
        headers=agent,
    ).json()
    assert other == {"flow": None, "held": True}
    taken = client.post(
        f"{BASE}/plugin-oauth/flows/take",
        json={"state_hash": H, "binding_hash": "ef" * 32},
        headers=agent,
    ).json()
    assert (taken["flow"]["secret"]["verifier"], taken["flow"]["expired"]) == ("ve", False)
    _heard(listener)
    grant = {
        "plugin": "docs",
        "plugin_url": URL,
        "resource": URL,
        "issuer": META["issuer"],
        "token_endpoint": META["token_endpoint"],
        "client_id": "client-1",
        "auth_method": "none",
        "registration": "dynamic",
        "scope": "read",
        "secret": {"access_token": "at-1", "refresh_token": "rt-1"},
        "has_refresh": True,
        "access_expires_at": None,
        "connected_by": "browser",
    }
    stored = client.post(f"{BASE}/plugin-oauth/grants", json=grant, headers=agent)
    assert stored.json() == {"stored": True}
    assert "*" in _heard(listener)
    # The URL changed during the sign-in: nothing is kept.
    moved = client.post(
        f"{BASE}/plugin-oauth/grants", json={**grant, "plugin_url": "https://x/"}, headers=agent
    )
    assert moved.json() == {"stored": False}
    row = db.execute(
        "SELECT secret_sealed, dek_sealed, kek_id, grant_id FROM ai_plugin_oauth"
    ).fetchone()
    aad = "ai_plugin_oauth:" + json.dumps(
        {
            "plugin": "docs",
            "plugin_url": URL,
            "resource": URL,
            "issuer": META["issuer"],
            "token_endpoint": META["token_endpoint"],
            "client_id": "client-1",
        },
        separators=(",", ":"),
    )
    assert json.loads(open_secret(KEK, _envelope(row), aad)) == {
        "access_token": "at-1",
        "refresh_token": "rt-1",
    }
    view = client.get(f"{BASE}/plugins/docs", headers=agent).json()["plugin"]
    assert view["oauth"]["grant_id"] == str(row[3])
    secrets = client.post(f"{BASE}/plugin-oauth/stored-secrets", headers=agent).json()
    assert secrets["secrets"][0]["refresh_token"] == "rt-1"


def _signed_in(client: TestClient, agent: dict[str, str], db: Any) -> str:
    _create(client, agent)
    sealed = seal_secret(
        KEK,
        '{"access_token":"at-1","refresh_token":"rt-1"}',
        "ai_plugin_oauth:"
        + json.dumps(
            {
                "plugin": "docs",
                "plugin_url": URL,
                "resource": URL,
                "issuer": "https://auth.example",
                "token_endpoint": "https://auth.example/token",
                "client_id": "c",
            },
            separators=(",", ":"),
        ),
    )
    grant = str(uuid.uuid4())
    db.execute(
        "INSERT INTO ai_plugin_oauth (plugin, plugin_url, resource, issuer, token_endpoint,"
        " client_id, auth_method, registration, secret_sealed, dek_sealed, kek_id, has_refresh,"
        " grant_id, connected_by) VALUES ('docs', %s, %s, 'https://auth.example',"
        " 'https://auth.example/token', 'c', 'none', 'dynamic', %s, %s, %s, true, %s, 'b')",
        (URL, URL, sealed.secret_sealed, sealed.dek_sealed, sealed.kek_id, grant),
    )
    return grant


def test_a_refresh_is_kept_only_under_its_lease(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    grant = _signed_in(client, agent, db)
    opened = client.post(
        f"{BASE}/plugin-oauth/grants/docs/open",
        json={"plugin_url": URL, "grant": grant},
        headers=agent,
    ).json()
    assert opened["grant"]["secret"]["refresh_token"] == "rt-1"
    stale = client.post(
        f"{BASE}/plugin-oauth/grants/docs/open",
        json={"plugin_url": URL, "grant": str(uuid.uuid4())},
        headers=agent,
    ).json()
    assert stale == {
        "grant": None,
        "fail": "it was signed in again or out since this run loaded it",
    }
    lease = str(uuid.uuid4())
    body = {
        "lease_token": lease,
        "grant": grant,
        "secret": {"access_token": "at-2", "refresh_token": "rt-2"},
        "has_refresh": True,
        "access_expires_at": "2030-01-01T00:00:00Z",
        "scope": None,
    }
    lost = client.post(f"{BASE}/plugin-oauth/grants/docs/refreshed", json=body, headers=agent)
    assert (lost.status_code, lost.json()["reason"]) == (409, "lease-lost")
    db.execute(
        "INSERT INTO agent_leases (key, token, expires_at)"
        " VALUES ('plugin-oauth:docs', %s, now() + interval '1 minute')",
        (lease,),
    )
    kept = client.post(f"{BASE}/plugin-oauth/grants/docs/refreshed", json=body, headers=agent)
    assert kept.json() == {"stored": True}
    again = client.post(
        f"{BASE}/plugin-oauth/grants/docs/open",
        json={"plugin_url": URL, "grant": grant},
        headers=agent,
    ).json()
    assert again["grant"]["secret"]["access_token"] == "at-2"
    assert again["grant"]["access_expires_at"].startswith("2030-01-01")


def test_a_refresh_lease_waits_for_an_sql_pods_row_lock_only_as_long_as_asked(
    client: TestClient, agent: dict[str, str], db: Any, pg_conninfo: str
) -> None:
    """The rollout: an SQL-path pod holds the grant's row across a refresh. The acquire
    waits for it no longer than ``wait_s``, then answers busy."""
    _signed_in(client, agent, db)
    ask = {"token": str(uuid.uuid4()), "ttl_s": 60, "wait_s": 0.5}
    with psycopg.connect(pg_conninfo) as old_pod:
        old_pod.execute("SELECT 1 FROM ai_plugin_oauth WHERE plugin = 'docs' FOR UPDATE")
        started = time.monotonic()
        busy = client.post(f"{BASE}/leases/plugin-oauth:docs", json=ask, headers=agent)
        assert time.monotonic() - started < 5
        assert (busy.status_code, busy.json()["reason"]) == (409, "busy")
        old_pod.rollback()
        assert db.execute("SELECT count(*) FROM agent_leases").fetchone()[0] == 0
    held = client.post(f"{BASE}/leases/plugin-oauth:docs", json=ask, headers=agent)
    assert held.status_code == 200


def test_a_refresh_lease_puts_back_the_lock_timeout_in_force(
    client: TestClient, agent: dict[str, str], db: Any, pg_conninfo: str
) -> None:
    """The wait's own lock_timeout ends with the row lock: the rest of the transaction keeps
    what was set before."""
    _signed_in(client, agent, db)
    with psycopg.connect(pg_conninfo) as conn, conn.transaction():
        conn.execute("SET LOCAL lock_timeout = '7s'")
        assert leases.acquire(conn, "plugin-oauth:docs", str(uuid.uuid4()), 60, 0.5) is not None
        assert conn.execute("SELECT current_setting('lock_timeout')").fetchone() == ("7s",)


def test_past_max_row_waits_an_acquire_does_not_wait_on_the_grant_row(
    client: TestClient, agent: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each wait holds a pool connection, so only MAX_ROW_WAITS acquires wait on the row."""
    from scadbuddy.api import crud_agent_access

    waits: list[float] = []
    real = leases.acquire

    def acquire(conn: Any, key: str, token: str, ttl: float, wait: float = 0.0) -> Any:
        waits.append(wait)
        return real(conn, key, token, ttl, wait)

    monkeypatch.setattr(leases, "acquire", acquire)
    ask = {"token": str(uuid.uuid4()), "ttl_s": 60, "wait_s": 5}
    held = client.post(f"{BASE}/leases/plugin-oauth:docs", json=ask, headers=agent)
    assert held.status_code == 200
    assert waits[-1] > 0
    monkeypatch.setattr(crud_agent_access, "_row_waits", crud_agent_access.MAX_ROW_WAITS)
    ask["token"] = str(uuid.uuid4())
    client.post(f"{BASE}/leases/plugin-oauth:other", json=ask, headers=agent)
    assert waits[-1] == 0


def test_a_mark_and_a_sign_out_tell_the_warm_caches(
    client: TestClient, agent: dict[str, str], db: Any, listener: Any
) -> None:
    grant = _signed_in(client, agent, db)
    _heard(listener)
    other = {"reason": "refused", "grant": str(uuid.uuid4())}
    assert client.post(
        f"{BASE}/plugin-oauth/grants/docs/mark", json=other, headers=agent
    ).json() == {"done": False}
    assert _heard(listener) == []
    marked = client.post(
        f"{BASE}/plugin-oauth/grants/docs/mark",
        json={"reason": "sign in again", "grant": grant},
        headers=agent,
    )
    assert marked.json() == {"done": True}
    assert _heard(listener) == ["*"]
    failed = client.post(
        f"{BASE}/plugin-oauth/grants/docs/open",
        json={"plugin_url": URL, "grant": grant},
        headers=agent,
    ).json()
    assert failed["fail"] == "sign in again"
    assert client.post(f"{BASE}/plugin-oauth/grants/docs/forget", headers=agent).json() == {
        "done": True
    }
    assert _heard(listener) == ["*"]
    assert client.post(f"{BASE}/plugin-oauth/grants/docs/forget", headers=agent).json() == {
        "done": False
    }
    assert client.post(f"{BASE}/plugins/docs/delete", headers=agent).json() == {"done": True}
    assert _heard(listener) == ["*"]


def test_a_key_rotation_rewraps_the_plugin_secrets_and_sign_ins(
    client: TestClient, db: Any
) -> None:
    """At the API's start (`agent_data/rotation.py`), as the agent did at its own."""
    old = kek_from_base64(base64.b64encode(b"\x07" * 32).decode())
    header = seal_secret(old, "sk-secret-value-1234", PLUGIN_AAD)
    db.execute(
        "INSERT INTO ai_plugins (name, kind, url, auth_header, secret_sealed, dek_sealed,"
        " kek_id, last4) VALUES ('docs', 'remote_mcp', %s, 'X-Key', %s, %s, %s, '1234')",
        (URL, header.secret_sealed, header.dek_sealed, header.kek_id),
    )
    grant_aad = "ai_plugin_oauth:" + json.dumps(
        {
            "plugin": "docs",
            "plugin_url": URL,
            "resource": URL,
            "issuer": "i",
            "token_endpoint": "t",
            "client_id": "c",
        },
        separators=(",", ":"),
    )
    sealed = seal_secret(old, '{"access_token":"at"}', grant_aad)
    db.execute(
        "INSERT INTO ai_plugin_oauth (plugin, plugin_url, resource, issuer, token_endpoint,"
        " client_id, auth_method, registration, secret_sealed, dek_sealed, kek_id, has_refresh,"
        " grant_id, connected_by) VALUES ('docs', %s, %s, 'i', 't', 'c', 'none', 'dynamic',"
        " %s, %s, %s, false, %s, 'b')",
        (URL, URL, sealed.secret_sealed, sealed.dek_sealed, sealed.kek_id, uuid.uuid4()),
    )
    db.execute(
        "INSERT INTO ai_plugin_oauth_flows (state_hash, plugin, plugin_url, meta, secret_sealed,"
        " dek_sealed, kek_id, created_by, expires_at) VALUES (%s, 'docs', %s, '{}'::jsonb,"
        " %s, %s, %s, 'b', now() + interval '5 minutes')",
        (H, URL, sealed.secret_sealed, sealed.dek_sealed, sealed.kek_id),
    )
    assert rotate(db, old, KEK)["plugins"] == (2, 0)
    assert rotate(db, old, KEK)["plugins"] == (0, 0)
    assert db.execute("SELECT count(*) FROM ai_plugin_oauth_flows").fetchone() == (0,)
    row = db.execute(
        "SELECT secret_sealed, dek_sealed, kek_id FROM ai_plugins WHERE name = 'docs'"
    ).fetchone()
    assert open_secret(KEK, _envelope(row), PLUGIN_AAD) == "sk-secret-value-1234"
    row = db.execute("SELECT secret_sealed, dek_sealed, kek_id FROM ai_plugin_oauth").fetchone()
    assert open_secret(KEK, _envelope(row), grant_aad) == '{"access_token":"at"}'
