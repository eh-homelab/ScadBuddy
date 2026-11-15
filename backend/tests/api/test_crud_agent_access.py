"""The agent's tool access, tool listings, leases and plugin sessions behind its CRUD routes
(#5299 step 5, `api/crud_agent_access.py`): only the ``agent`` service credential reaches
them, each runs what the agent's SQL ran, an access write sends the warm eviction in its
transaction, and a listing is stored only while the caller's lease holds."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.api.test_crud_agent_cost import token

pytestmark = [pytest.mark.requires_postgres, pytest.mark.agent_migrations]

BASE = "/api/v1/internal/crud/agent"
TOOLS = [{"raw": {"name": "search"}, "omitted": []}]
HASH = "sha256:" + "a" * 64
COMMIT = "b" * 40


@pytest.fixture
def agent(client: TestClient) -> dict[str, str]:
    return token(client)


@pytest.fixture
def db(pg_conninfo: str) -> Any:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        yield conn


def _plugin(db: Any, name: str = "files", url: str = "https://mcp.example/") -> None:
    db.execute(
        "INSERT INTO ai_plugins (name, kind, url) VALUES (%s, 'remote_mcp', %s)", (name, url)
    )


def _package(db: Any, name: str = "pkg") -> None:
    db.execute(
        "INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref, fetch_url,"
        " commit_sha, content_hash, files, review) VALUES (%s, 'git', 'https://g.example/r',"
        " 'main', 'https://g.example/r', %s, %s, '{}', '{}')",
        (name, COMMIT, HASH),
    )


def _notifies(listener: Any) -> list[str]:
    return [n.payload for n in listener.notifies(timeout=0.5, stop_after=10)]


def test_only_the_agent_credential_reaches_the_routes(client: TestClient) -> None:
    body = {"sources": ["remote:files"]}
    for caller in ("print", "projects"):
        refused = client.post(f"{BASE}/access/rows", json=body, headers=token(client, caller))
        assert refused.status_code == 403, refused.text
    assert client.post(f"{BASE}/access/rows", json=body).status_code == 401


def test_the_areas_list_the_plugin_stores(client: TestClient, agent: dict[str, str]) -> None:
    areas = client.get(f"{BASE}/areas", headers=agent).json()["areas"]
    for area in ("access", "plugin-sessions", "plugins", "plugin-packages"):
        assert area in areas


# -- access --------------------------------------------------------------------------------


def test_an_access_write_evicts_and_writes_in_one_transaction(
    client: TestClient, agent: dict[str, str], db: Any, pg_conninfo: str
) -> None:
    with psycopg.connect(pg_conninfo, autocommit=True) as listener:
        listener.execute("LISTEN scadbuddy_warm_evict")
        body = {
            "by": "browser:browser",
            "edits": [
                {"source": "remote:files", "kind": "items", "items": {"*": "on"}},
                {"source": "remote:files", "kind": "replace", "tools": {"a": "read", "b": "off"}},
            ],
        }
        answer = client.post(f"{BASE}/access/write", json=body, headers=agent)
        assert answer.status_code == 200, answer.text
        assert answer.json() == {"applied": [0, 1]}
        assert _notifies(listener) == ["*"]
    rows = client.post(
        f"{BASE}/access/rows", json={"sources": ["remote:files"]}, headers=agent
    ).json()["rows"]
    assert [(r["item"], r["access"], r["updated_by"]) for r in rows] == [
        ("*", "on", "browser:browser"),
        ("a", "read", "browser:browser"),
        ("b", "off", "browser:browser"),
    ]
    one = client.get(
        f"{BASE}/access/row", params={"source": "remote:files", "item": "a"}, headers=agent
    )
    assert one.json()["row"]["access"] == "read"
    # A null deletes the row; a forget drops the source's listing too.
    db.execute("INSERT INTO ai_tool_listings (source, tools) VALUES ('remote:files', '[]'::jsonb)")
    edits = [{"source": "remote:files", "kind": "items", "items": {"a": None}}]
    client.post(f"{BASE}/access/write", json={"by": "system", "edits": edits}, headers=agent)
    assert db.execute("SELECT count(*) FROM ai_access WHERE item = 'a'").fetchone() == (0,)
    forget = [{"source": "remote:files", "kind": "forget"}]
    client.post(f"{BASE}/access/write", json={"by": "system", "edits": forget}, headers=agent)
    assert db.execute("SELECT count(*) FROM ai_access").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM ai_tool_listings").fetchone() == (0,)


def test_skip_unchanged_drops_an_edit_that_changes_nothing(
    client: TestClient, agent: dict[str, str], pg_conninfo: str
) -> None:
    first = {"source": "package:p/mcp:s", "kind": "replace", "tools": {"t": "write"}}
    other = {"source": "package:q/mcp:s", "kind": "replace", "tools": {"t": "read"}}
    client.post(f"{BASE}/access/write", json={"by": "a", "edits": [first]}, headers=agent)
    with psycopg.connect(pg_conninfo, autocommit=True) as listener:
        listener.execute("LISTEN scadbuddy_warm_evict")
        again = {"by": "b", "edits": [first, other], "skip_unchanged": True}
        assert client.post(f"{BASE}/access/write", json=again, headers=agent).json() == {
            "applied": [1]
        }
        assert _notifies(listener) == ["*"]
        same = {"by": "b", "edits": [first], "skip_unchanged": True}
        assert client.post(f"{BASE}/access/write", json=same, headers=agent).json() == {
            "applied": []
        }
        # Nothing to write sends nothing.
        assert _notifies(listener) == []


def test_an_access_write_out_of_the_model_is_refused(
    client: TestClient, agent: dict[str, str]
) -> None:
    for edit in (
        {"source": "other:x", "kind": "forget"},
        {"source": "remote:files", "kind": "items", "items": {"a": "always"}},
        {"source": "remote:files", "kind": "replace", "tools": {"*": "read"}},
        {"source": "remote:files", "kind": "forget", "extra": 1},
    ):
        answer = client.post(
            f"{BASE}/access/write", json={"by": "x", "edits": [edit]}, headers=agent
        )
        assert answer.status_code == 422, answer.text
    # The table's own rule: an item is `on` only in a package or the own plugin.
    on = {"source": "remote:files", "kind": "items", "items": {"a": "on"}}
    answer = client.post(f"{BASE}/access/write", json={"by": "x", "edits": [on]}, headers=agent)
    assert answer.status_code == 422, answer.text


# -- leases and listings ---------------------------------------------------------------------


def _lease(client: TestClient, agent: dict[str, str], key: str, tok: str) -> Any:
    return client.post(f"{BASE}/leases/{key}", json={"token": tok, "ttl_s": 60}, headers=agent)


def test_a_lease_is_busy_for_another_holder_and_natural_for_its_own(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    key = "tool-listing:remote:files"
    mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
    assert _lease(client, agent, key, mine).status_code == 200
    assert _lease(client, agent, key, mine).status_code == 200
    busy = _lease(client, agent, key, theirs)
    assert (busy.status_code, busy.json()["reason"]) == (409, "busy")
    renewed = client.put(f"{BASE}/leases/{key}", json={"token": mine}, headers=agent)
    assert renewed.status_code == 200
    lost = client.put(f"{BASE}/leases/{key}", json={"token": theirs}, headers=agent)
    assert (lost.status_code, lost.json()["reason"]) == (409, "lease-lost")
    gone = client.delete(f"{BASE}/leases/{key}", params={"token": mine}, headers=agent)
    assert gone.status_code == 204
    assert _lease(client, agent, key, theirs).status_code == 200
    # A lease past its expiry is taken over.
    db.execute("UPDATE agent_leases SET expires_at = now() - interval '1 second'")
    assert _lease(client, agent, key, mine).status_code == 200
    bad = _lease(client, agent, "something-else", mine)
    assert bad.status_code == 422


def test_a_lease_is_busy_while_an_sql_pod_holds_the_advisory_lock(
    client: TestClient, agent: dict[str, str], pg_conninfo: str
) -> None:
    """The rollout: an agent pod on the SQL path still holds the session advisory lock."""
    with psycopg.connect(pg_conninfo, autocommit=True) as old_pod:
        old_pod.execute(
            "SELECT pg_advisory_lock(hashtextextended('scadbuddy:ai_tool_listings:test:files', 0))"
        )
        busy = _lease(client, agent, "tool-listing:remote:files", str(uuid.uuid4()))
        assert (busy.status_code, busy.json()["reason"]) == (409, "busy")
        server = "tool-listing:package:pkg/mcp:srv"
        old_pod.execute(
            "SELECT pg_advisory_lock(hashtextextended("
            "'scadbuddy:ai_tool_listings:test:package:pkg/mcp:srv', 0))"
        )
        assert _lease(client, agent, server, str(uuid.uuid4())).status_code == 409
        old_pod.execute("SELECT pg_advisory_unlock_all()")
        assert _lease(client, agent, server, str(uuid.uuid4())).status_code == 200


def test_a_listing_is_stored_only_while_the_lease_holds_and_the_plugin_stands(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    _plugin(db)
    tok = str(uuid.uuid4())
    body = {"name": "files", "url": "https://mcp.example/", "tools": TOOLS, "lease_token": tok}
    lost = client.post(f"{BASE}/tool-listings/plugin", json=body, headers=agent)
    assert (lost.status_code, lost.json()["reason"]) == (409, "lease-lost")
    assert _lease(client, agent, "tool-listing:remote:files", tok).status_code == 200
    stored = client.post(f"{BASE}/tool-listings/plugin", json=body, headers=agent)
    assert stored.json() == {"stored": True}
    moved = {**body, "url": "https://elsewhere.example/"}
    assert client.post(f"{BASE}/tool-listings/plugin", json=moved, headers=agent).json() == {
        "stored": False
    }
    read = client.post(
        f"{BASE}/tool-listings/read", json={"sources": ["remote:files"]}, headers=agent
    ).json()["listings"]
    assert [(r["source"], r["tools"], r["truncated"]) for r in read] == [
        ("remote:files", TOOLS, False)
    ]
    # An expired lease is lost too.
    db.execute("UPDATE agent_leases SET expires_at = now() - interval '1 second'")
    late = client.post(f"{BASE}/tool-listings/plugin", json=body, headers=agent)
    assert late.json()["reason"] == "lease-lost"


def test_a_package_servers_listing_needs_its_pin(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    _package(db)
    tok = str(uuid.uuid4())
    assert _lease(client, agent, "tool-listing:package:pkg/mcp:srv", tok).status_code == 200
    body = {
        "pkg": "pkg",
        "server": "srv",
        "content_hash": HASH,
        "tools": TOOLS,
        "truncated": True,
        "lease_token": tok,
    }
    answer = client.post(f"{BASE}/tool-listings/package-server", json=body, headers=agent)
    assert answer.json() == {"stored": True}
    repinned = {**body, "content_hash": "sha256:" + "c" * 64}
    answer = client.post(f"{BASE}/tool-listings/package-server", json=repinned, headers=agent)
    assert answer.json() == {"stored": False}
    assert db.execute("SELECT source, truncated FROM ai_tool_listings").fetchall() == [
        ("package:pkg/mcp:srv", True)
    ]
    nameless = {**body, "tools": [{"raw": {"title": "x"}, "omitted": []}]}
    answer = client.post(f"{BASE}/tool-listings/package-server", json=nameless, headers=agent)
    assert answer.status_code == 422


# -- plugin sessions ---------------------------------------------------------------------------


def test_plugin_sessions_are_recorded_and_swept_by_replica(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    def seen(plugin: str, sid: str, replica: str) -> None:
        body = {"plugin": plugin, "id": sid, "replica": replica}
        assert client.post(f"{BASE}/plugin-sessions/seen", json=body, headers=agent).json() == {
            "ok": True
        }

    seen("files", "s1", "dead-host")
    seen("files", "s1", "dead-host")
    seen("pkg:srv", "s2", "live-host")
    seen("files", "s3", "me")
    db.execute("UPDATE ai_plugin_sessions SET created_at = now() - interval '10 minutes'")
    dead = client.post(
        f"{BASE}/plugin-sessions/dead",
        json={"live": ["live-host", "me"], "grace_ms": 300_000, "give_up_ms": 86_400_000},
        headers=agent,
    ).json()["rows"]
    assert dead == [{"plugin_id": "files", "upstream_session_id": "s1", "old": False}]
    mark = client.get(f"{BASE}/plugin-sessions/mark", headers=agent).json()["mark"]
    seen("files", "s4", "me")
    own = client.post(
        f"{BASE}/plugin-sessions/own", json={"replica": "me", "mark": mark}, headers=agent
    ).json()["rows"]
    assert own == [{"plugin_id": "files", "upstream_session_id": "s3", "old": False}]
    client.post(
        f"{BASE}/plugin-sessions/ended", json={"plugin": "files", "id": "s1"}, headers=agent
    )
    assert db.execute("SELECT count(*) FROM ai_plugin_sessions").fetchone() == (3,)
    bad = client.post(
        f"{BASE}/plugin-sessions/own", json={"replica": "me", "mark": "not a time!"}, headers=agent
    )
    assert bad.status_code == 422
