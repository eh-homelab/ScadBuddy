"""The agent's small stores behind its CRUD routes (#5299 step 5, `api/crud_agent_stores.py`):
only the ``agent`` service credential reaches them, each runs what the agent's SQL ran, and
the bounds, locks and expiries hold on the server."""

from __future__ import annotations

import hashlib
import threading
import uuid
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.api.test_crud_agent_cost import token

pytestmark = [pytest.mark.requires_postgres, pytest.mark.agent_migrations]

BASE = "/api/v1/internal/crud/agent"
SESSION = "0b0e3c55-6f1e-4a39-9d0e-8a1c2b3d4e5f"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PNG_NAME = hashlib.sha256(PNG).hexdigest() + ".png"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def agent(client: TestClient) -> dict[str, str]:
    return token(client)


@pytest.fixture
def db(pg_conninfo: str) -> Any:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        yield conn


def _session(db: Any, session: str = SESSION, title: str = "Chat") -> None:
    db.execute(
        "INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label,"
        " creator_kind, creator_id, status, max_turns, budget_usd, title)"
        " VALUES (%s, 'chat', 'browser', 'browser', 'You', 'browser', 'browser',"
        " 'idle', 10, 1, %s)",
        (session, title),
    )


def test_only_the_agent_credential_reaches_the_routes(client: TestClient) -> None:
    for caller in ("print", "projects"):
        refused = client.get(f"{BASE}/mcp-tokens", headers=token(client, caller))
        assert refused.status_code == 403, refused.text
        assert refused.headers["x-scadbuddy-crud"] == "1"
    assert client.get(f"{BASE}/mcp-tokens").status_code == 401


def test_the_areas_list_the_small_stores(client: TestClient, agent: dict[str, str]) -> None:
    areas = client.get(f"{BASE}/areas", headers=agent).json()["areas"]
    for area in (
        "activity-kinds",
        "browser-origins",
        "bridge-tabs",
        "pairings",
        "mcp-tokens",
        "notify",
    ):
        assert area in areas


# -- activity kinds ------------------------------------------------------------------------


def test_activity_kinds_upsert_and_retire_in_one_call(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    first = [
        {"key": "agent.a", "label": "A", "facets": []},
        {"key": "agent.b", "label": "B", "facets": [{"name": "n", "type": "number", "label": "N"}]},
    ]
    assert (
        client.post(f"{BASE}/activity-kinds", json={"kinds": first}, headers=agent).status_code
        == 200
    )
    again = [{"key": "agent.a", "label": "A2", "facets": []}]
    assert (
        client.post(f"{BASE}/activity-kinds", json={"kinds": again}, headers=agent).status_code
        == 200
    )
    rows = db.execute(
        "SELECT key, label, retired_at IS NOT NULL FROM ai_activity_kinds ORDER BY key"
    ).fetchall()
    assert rows == [("agent.a", "A2", False), ("agent.b", "B", True)]
    # Declared again, a retired kind comes back.
    assert (
        client.post(f"{BASE}/activity-kinds", json={"kinds": first}, headers=agent).status_code
        == 200
    )
    assert db.execute(
        "SELECT retired_at FROM ai_activity_kinds WHERE key = 'agent.b'"
    ).fetchone() == (None,)


def test_an_activity_kind_outside_agent_or_an_extra_field_is_refused(
    client: TestClient, agent: dict[str, str]
) -> None:
    for kind in (
        {"key": "render.x", "label": "x", "facets": []},
        {"key": "agent.x", "label": "x", "facets": [], "extra": 1},
    ):
        answer = client.post(f"{BASE}/activity-kinds", json={"kinds": [kind]}, headers=agent)
        assert answer.status_code == 422, answer.text


# -- browser origins ----------------------------------------------------------------------


def test_an_approval_never_narrows_and_a_revoke_withdraws_it(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    _session(db)
    origin = "https://example.com"

    def approve(scope: str, session: str = SESSION) -> bool:
        body = {"session_id": session, "origin": origin, "scope": scope}
        answer = client.post(f"{BASE}/browser-origins/approve", json=body, headers=agent)
        assert answer.status_code == 200, answer.text
        return bool(answer.json()["done"])

    assert approve("session")
    assert approve("once")
    listed = client.get(f"{BASE}/browser-origins", params={"session_id": SESSION}, headers=agent)
    assert listed.json() == {"origins": [{"origin": origin, "scope": "session"}]}
    # No such session: nothing written.
    assert not approve("once", str(uuid.uuid4()))

    approved = client.get(f"{BASE}/browser-origins/approved", headers=agent).json()["approved"]
    assert [(a["session_id"], a["title"], a["scope"]) for a in approved] == [
        (SESSION, "Chat", "session")
    ]
    one = client.get(
        f"{BASE}/browser-origins/approved", params={"session_id": str(uuid.uuid4())}, headers=agent
    )
    assert one.json() == {"approved": []}

    body = {"session_id": SESSION, "origin": origin}
    assert client.post(f"{BASE}/browser-origins/revoke", json=body, headers=agent).json() == {
        "done": True
    }
    assert client.post(f"{BASE}/browser-origins/revoke", json=body, headers=agent).json() == {
        "done": False
    }


def test_an_origin_with_a_path_is_refused(client: TestClient, agent: dict[str, str]) -> None:
    body = {"session_id": SESSION, "origin": "https://example.com/x", "scope": "once"}
    assert (
        client.post(f"{BASE}/browser-origins/approve", json=body, headers=agent).status_code == 422
    )


# -- session tabs, tab owners, tab images -------------------------------------------------


def test_a_conditional_tab_write_lands_only_on_the_tab_it_saw(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    _session(db)

    def write(tab: str, **extra: Any) -> bool:
        body = {"session_id": SESSION, "tab_id": tab, **extra}
        answer = client.post(f"{BASE}/session-tabs", json=body, headers=agent)
        assert answer.status_code == 200, answer.text
        return bool(answer.json()["written"])

    def read() -> Any:
        return client.get(
            f"{BASE}/session-tabs", params={"session_id": SESSION}, headers=agent
        ).json()["tab_id"]

    assert read() is None
    assert write("tab-a", conditional=True)  # no row: inserts
    assert not write("tab-b", conditional=True)  # a row, `seen` none: nothing
    assert not write("tab-b", conditional=True, seen="tab-x")
    assert read() == "tab-a"
    assert write("tab-b", conditional=True, seen="tab-a")
    assert write("tab-c")
    assert read() == "tab-c"
    missing = client.post(
        f"{BASE}/session-tabs", json={"session_id": str(uuid.uuid4()), "tab_id": "t"}, headers=agent
    )
    assert missing.status_code == 404


def test_tab_owners_keep_the_latest_claim(client: TestClient, agent: dict[str, str]) -> None:
    def owner(tab: str) -> Any:
        return client.get(f"{BASE}/tab-owners", params={"tab_id": tab}, headers=agent).json()[
            "owner"
        ]

    def post(path: str, body: dict[str, Any]) -> None:
        answer = client.post(f"{BASE}/tab-owners/{path}", json=body, headers=agent)
        assert answer.status_code == 200, answer.text

    post("claim", {"tab_id": "t1", "queue": "agent-tools.a"})
    post("claim", {"tab_id": "t1", "queue": "agent-tools.b"})
    first = owner("t1")
    assert first["queue"] == "agent-tools.b"
    # A refresh by another queue leaves the claim alone, and makes a missing row again.
    post("refresh", {"tab_ids": ["t1", "t2"], "queue": "agent-tools.a"})
    assert owner("t1")["queue"] == "agent-tools.b"
    assert owner("t2")["queue"] == "agent-tools.a"
    # A release by another queue, or of a row claimed after the moment named, is a no-op.
    post("release", {"tab_id": "t1", "queue": "agent-tools.a"})
    post("refresh", {"tab_ids": ["t1"], "queue": "agent-tools.b"})
    post(
        "release", {"tab_id": "t1", "queue": "agent-tools.b", "claimed_before": first["claimed_at"]}
    )
    assert owner("t1") is not None
    post("release", {"tab_id": "t1", "queue": "agent-tools.b"})
    assert owner("t1") is None
    post("release-all", {"queue": "agent-tools.a"})
    assert owner("t2") is None
    bad = client.post(
        f"{BASE}/tab-owners/release",
        json={"tab_id": "t1", "queue": "q", "claimed_before": "not a time"},
        headers=agent,
    )
    assert bad.status_code == 422


def test_a_tab_image_is_taken_once_and_expires(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    put = client.put(
        f"{BASE}/tab-images/{PNG_NAME}",
        content=PNG,
        headers={**agent, "content-type": "image/png"},
    )
    assert put.status_code == 204, put.text
    taken = client.post(f"{BASE}/tab-images/{PNG_NAME}/take", headers=agent)
    assert taken.status_code == 200
    assert (taken.content, taken.headers["content-type"]) == (PNG, "image/png")
    assert taken.headers["x-scadbuddy-crud"] == "1"
    assert client.post(f"{BASE}/tab-images/{PNG_NAME}/take", headers=agent).status_code == 204

    client.put(
        f"{BASE}/tab-images/{PNG_NAME}", content=PNG, headers={**agent, "content-type": "image/png"}
    )
    db.execute("UPDATE ai_tab_images SET stored_at = now() - interval '11 minutes'")
    assert client.post(f"{BASE}/tab-images/{PNG_NAME}/take", headers=agent).status_code == 204
    assert db.execute("SELECT count(*) FROM ai_tab_images").fetchone() == (0,)


def test_a_tab_image_must_be_its_name_and_type(client: TestClient, agent: dict[str, str]) -> None:
    wrong_bytes = client.put(
        f"{BASE}/tab-images/{PNG_NAME}",
        content=PNG + b"x",
        headers={**agent, "content-type": "image/png"},
    )
    assert wrong_bytes.status_code == 422
    wrong_type = client.put(
        f"{BASE}/tab-images/{PNG_NAME}", content=PNG, headers={**agent, "content-type": "image/gif"}
    )
    assert wrong_type.status_code == 422
    bad_name = client.put(
        f"{BASE}/tab-images/..%2Fx.png", content=PNG, headers={**agent, "content-type": "image/png"}
    )
    assert bad_name.status_code in (404, 422)
    big = b"\x00" * (5 * 1024 * 1024 // 4 * 3 + 1)
    too_big = client.put(
        f"{BASE}/tab-images/{hashlib.sha256(big).hexdigest()}.png",
        content=big,
        headers={**agent, "content-type": "image/png"},
    )
    assert too_big.status_code == 413


# -- pairings -----------------------------------------------------------------------------


def _ask(client: TestClient, agent: dict[str, str], who: str = "token:x", code: str = "C") -> Any:
    return client.post(
        f"{BASE}/pairings",
        json={
            "principal_kind": "bearer",
            "principal_id": who,
            "fallback_label": f"bearer {who}",
            "code_hash": _sha(code),
        },
        headers=agent,
    )


def test_a_pairing_is_named_after_its_token_and_accepted_once(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    token_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO ai_mcp_tokens (id, name, tier, token_hash) VALUES (%s, 'laptop', 'write', %s)",
        (token_id, _sha("t")),
    )
    asked = _ask(client, agent, f"token:{token_id}", "ABCD-EFGH")
    assert asked.status_code == 201, asked.text
    request = asked.json()
    assert request["label"] == "MCP token “laptop”"
    stored = db.execute("SELECT code_hash FROM ai_browser_pairings").fetchone()
    assert stored == (_sha("ABCD-EFGH"),)
    pending = client.get(f"{BASE}/pairings/pending", headers=agent).json()["pairings"]
    assert [p["id"] for p in pending] == [request["id"]]

    wrong = client.post(
        f"{BASE}/pairings/{request['id']}/accept",
        json={"code_hash": _sha("nope"), "tab_id": "tab-1"},
        headers=agent,
    ).json()
    assert (wrong["result"], wrong["attempts_left"]) == ("wrong_code", 4)
    right = client.post(
        f"{BASE}/pairings/{request['id']}/accept",
        json={"code_hash": _sha("ABCD-EFGH"), "tab_id": "tab-1"},
        headers=agent,
    ).json()
    assert right["result"] == "ok"
    assert (right["principal_kind"], right["principal_id"]) == ("bearer", f"token:{token_id}")
    again = client.post(
        f"{BASE}/pairings/{request['id']}/accept",
        json={"code_hash": _sha("ABCD-EFGH"), "tab_id": "tab-2"},
        headers=agent,
    ).json()
    assert again["result"] == "gone"

    paired = client.get(
        f"{BASE}/pairings/paired-tab",
        params={"principal_kind": "bearer", "principal_id": f"token:{token_id}"},
        headers=agent,
    ).json()["pairing"]
    assert (paired["id"], paired["tab_id"]) == (request["id"], "tab-1")
    with_tab = client.post(
        f"{BASE}/pairings/paired-with", json={"tab_ids": ["tab-1", "tab-9"]}, headers=agent
    ).json()["pairings"]
    assert [p["tab_id"] for p in with_tab] == ["tab-1"]
    end = client.post(
        f"{BASE}/pairings/{request['id']}/end", json={"tab_id": "tab-2"}, headers=agent
    )
    assert end.json() == {"done": False}
    end = client.post(
        f"{BASE}/pairings/{request['id']}/end", json={"tab_id": "tab-1"}, headers=agent
    )
    assert end.json() == {"done": True}


def test_too_many_wrong_codes_deny_and_deny_ends_a_request(
    client: TestClient, agent: dict[str, str]
) -> None:
    request = _ask(client, agent).json()
    for left in (4, 3, 2, 1):
        answer = client.post(
            f"{BASE}/pairings/{request['id']}/accept",
            json={"code_hash": _sha("x"), "tab_id": "t"},
            headers=agent,
        ).json()
        assert answer["attempts_left"] == left
    last = client.post(
        f"{BASE}/pairings/{request['id']}/accept",
        json={"code_hash": _sha("x"), "tab_id": "t"},
        headers=agent,
    ).json()
    assert last["result"] == "gone"
    other = _ask(client, agent, "token:y").json()
    assert client.post(f"{BASE}/pairings/{other['id']}/deny", headers=agent).json() == {
        "done": True
    }
    assert client.post(f"{BASE}/pairings/{other['id']}/deny", headers=agent).json() == {
        "done": False
    }


def test_concurrent_requests_never_pass_the_cap(client: TestClient, agent: dict[str, str]) -> None:
    """The cap check and insert run under the transaction's advisory lock: ten requests at
    once from one principal leave exactly three pending."""
    answers: list[int] = []
    lock = threading.Lock()

    def ask() -> None:
        status = _ask(client, agent, "token:same").status_code
        with lock:
            answers.append(status)

    threads = [threading.Thread(target=ask) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(answers) == [201] * 3 + [409] * 7
    refused = _ask(client, agent, "token:same")
    assert refused.json()["reason"] == "pairing-refused"


def test_a_long_principal_id_still_pairs(client: TestClient, agent: dict[str, str]) -> None:
    # The agent's fallback label holds the principal's id, which may be 500 characters.
    who = "o" * 500
    answer = client.post(
        f"{BASE}/pairings",
        json={
            "principal_kind": "oidc",
            "principal_id": who,
            "fallback_label": f"OIDC user {who}",
            "code_hash": _sha("ABCD-EFGH"),
        },
        headers=agent,
    )
    assert answer.status_code == 201, answer.text
    assert answer.json()["label"] == f"OIDC user {who}"


def test_a_bad_code_hash_is_refused(client: TestClient, agent: dict[str, str]) -> None:
    answer = client.post(
        f"{BASE}/pairings",
        json={
            "principal_kind": "bearer",
            "principal_id": "token:x",
            "fallback_label": "x",
            "code_hash": "ABCD-EFGH",
        },
        headers=agent,
    )
    assert answer.status_code == 422


# -- MCP tokens ---------------------------------------------------------------------------


def test_a_token_verifies_by_its_hash_until_revoked(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    hashed = _sha("sbmcp_secret")
    minted = client.post(
        f"{BASE}/mcp-tokens",
        json={"name": "laptop", "tier": "outward", "token_hash": hashed, "approval_grant": True},
        headers=agent,
    )
    assert minted.status_code == 201, minted.text
    record = minted.json()["token"]
    assert (record["tier"], record["approval_grant"], record["last_used_at"]) == (
        "outward",
        True,
        None,
    )
    # Only the hash is stored, and only a hash is accepted.
    assert db.execute("SELECT token_hash FROM ai_mcp_tokens").fetchone() == (hashed,)
    raw = client.post(
        f"{BASE}/mcp-tokens/verify", json={"token_hash": "sbmcp_secret"}, headers=agent
    )
    assert raw.status_code == 422

    verified = client.post(f"{BASE}/mcp-tokens/verify", json={"token_hash": hashed}, headers=agent)
    assert verified.json() == {"token": {"id": record["id"], "tier": "outward"}}
    listed = client.get(f"{BASE}/mcp-tokens", headers=agent).json()["tokens"]
    assert listed[0]["last_used_at"] is not None
    live = client.get(f"{BASE}/mcp-tokens/{record['id']}/live", headers=agent).json()
    assert live == {"tier": "outward", "approval_grant": True}

    assert client.post(f"{BASE}/mcp-tokens/{record['id']}/revoke", headers=agent).json() == {
        "done": True
    }
    assert client.post(f"{BASE}/mcp-tokens/{record['id']}/revoke", headers=agent).json() == {
        "done": False
    }
    gone = client.post(f"{BASE}/mcp-tokens/verify", json={"token_hash": hashed}, headers=agent)
    assert gone.json() == {"token": None}
    assert client.get(f"{BASE}/mcp-tokens/{record['id']}/live", headers=agent).json() == {
        "tier": None,
        "approval_grant": False,
    }


def test_an_expired_token_does_not_verify(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    hashed = _sha("old")
    client.post(
        f"{BASE}/mcp-tokens",
        json={
            "name": "old",
            "tier": "read",
            "token_hash": hashed,
            "expires_at": "2020-01-01T00:00:00Z",
        },
        headers=agent,
    )
    assert client.post(
        f"{BASE}/mcp-tokens/verify", json={"token_hash": hashed}, headers=agent
    ).json() == {"token": None}


def test_a_grant_needs_an_outward_token_and_a_hash_is_unique(
    client: TestClient, agent: dict[str, str]
) -> None:
    body = {"name": "n", "tier": "write", "token_hash": _sha("a"), "approval_grant": True}
    assert client.post(f"{BASE}/mcp-tokens", json=body, headers=agent).status_code == 422
    body = {"name": "n", "tier": "write", "token_hash": _sha("a")}
    assert client.post(f"{BASE}/mcp-tokens", json=body, headers=agent).status_code == 201
    assert client.post(f"{BASE}/mcp-tokens", json=body, headers=agent).status_code == 409
