"""The agent's plugin packages behind its CRUD routes (#5299 step 5, area
``plugin-packages``, `api/crud_agent_packages.py`): only the ``agent`` credential reaches
them, each runs what ``agent/src/plugins/packages/store.ts`` ran over SQL in one
transaction, and the prune of stale rows (`agent_data/packages.py`) keeps what the agent's
tests (``agent/test/pluginPackages.pg.test.ts``) say it keeps."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.agent_data import packages
from tests.api.test_crud_agent_cost import token

pytestmark = [pytest.mark.requires_postgres, pytest.mark.agent_migrations]

BASE = "/api/v1/internal/crud/agent/plugin-packages"
C1 = "a" * 40
C2 = "b" * 40
H1 = "sha256:" + "1" * 64
H2 = "sha256:" + "2" * 64
HOOK = "hook:Stop:0123456789abcdef"


@pytest.fixture
def agent(client: TestClient) -> dict[str, str]:
    return token(client)


@pytest.fixture
def db(pg_conninfo: str) -> Any:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        yield conn


def _review(name: str = "greeter", **extra: Any) -> dict[str, Any]:
    review: dict[str, Any] = {
        "name": name,
        "description": None,
        "version": None,
        "skills": [f"{name}:hello"],
        "commands": [],
        "agents": [],
        "hooks": [{"event": "Stop", "type": "prompt", "id": "0123456789abcdef"}],
        "mcp_servers": [{"name": "mem", "type": "http", "url": "https://mcp.example/mcp/"}],
        "local_servers": [],
        "files": [],
        "refused": [],
        "builtin_tools": [],
        "described": {"skill:hello": None},
    }
    review.update(extra)
    return review


def _install(name: str = "greeter", **review: Any) -> dict[str, Any]:
    return {
        "name": name,
        "source_kind": "git",
        "source_url": f"https://git.test/{name}.git",
        "source_ref": "HEAD",
        "marketplace_entry": None,
        "fetch_url": f"https://git.test/{name}.git",
        "fetch_path": "",
        "commit_sha": C1,
        "content_hash": H1,
        "files": {"README.md": {"sha256": "0" * 64, "size": 1}},
        "review": _review(name, **review),
    }


def _pending(name: str = "greeter", **changes: Any) -> dict[str, Any]:
    body = {
        "ref": "main",
        "fetch_url": f"https://git.test/{name}.git",
        "fetch_path": "",
        "commit_sha": C2,
        "content_hash": H2,
        "files": {"README.md": {"sha256": "9" * 64, "size": 2}},
        "review": _review(name),
    }
    body.update(changes)
    return body


def _seed(db: Any, source: str, rows: dict[str, str]) -> None:
    for item, access in rows.items():
        db.execute(
            "INSERT INTO ai_access (source, item, access, updated_by) VALUES (%s, %s, %s, 'test')",
            (source, item, access),
        )


def _list(db: Any, source: str) -> None:
    db.execute("INSERT INTO ai_tool_listings (source, tools) VALUES (%s, '[]')", (source,))


def _access(db: Any) -> list[str]:
    # Sorted here, by code point: ORDER BY follows the database's collation, which differs
    # between machines (C locally, en_US in CI).
    rows = db.execute("SELECT source, item, access FROM ai_access").fetchall()
    return [f"{s} {i}={a}" for s, i, a in sorted(rows)]


def _listings(db: Any) -> list[str]:
    return sorted(r[0] for r in db.execute("SELECT source FROM ai_tool_listings"))


def test_only_the_agent_credential_reaches_the_routes(client: TestClient) -> None:
    for caller in ("print", "projects"):
        refused = client.get(BASE, headers=token(client, caller))
        assert refused.status_code == 403, refused.text
    assert client.get(BASE).status_code == 401


def test_an_install_is_stored_unapproved_and_a_second_one_is_refused(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    # What an older replica's remove left behind: a re-install starts at the defaults.
    _seed(db, "package:greeter", {"*": "off", "skill:hello": "off"})
    _list(db, "package:greeter/mcp:mem")
    made = client.post(BASE, json=_install(), headers=agent)
    assert made.status_code == 201, made.text
    row = made.json()
    assert (row["name"], row["approved_at"], row["enabled"]) == ("greeter", None, False)
    assert row["files"] == {"README.md": {"sha256": "0" * 64, "size": 1}}
    assert _access(db) == []
    assert _listings(db) == []

    _seed(db, "package:greeter", {"skill:hello": "off"})
    again = client.post(BASE, json=_install(), headers=agent)
    assert (again.status_code, again.json()["reason"]) == (409, "package-exists")
    assert "is already installed" in again.json()["detail"]
    # A refused install forgets nothing.
    assert _access(db) == ["package:greeter skill:hello=off"]

    assert [p["name"] for p in client.get(BASE, headers=agent).json()["packages"]] == ["greeter"]
    assert client.get(f"{BASE}/greeter", headers=agent).json()["package"]["commit_sha"] == C1
    assert client.get(f"{BASE}/other", headers=agent).json() == {"package": None}


def test_an_install_whose_name_is_not_its_reviews_is_refused(
    client: TestClient, agent: dict[str, str]
) -> None:
    body = _install()
    body["review"]["name"] = "other"
    assert client.post(BASE, json=body, headers=agent).status_code == 422
    assert client.post(BASE, json={**_install(), "extra": 1}, headers=agent).status_code == 422
    assert (
        client.post(BASE, json={**_install(), "commit_sha": "HEAD"}, headers=agent).status_code
        == 422
    )


def test_approve_only_what_the_review_showed_then_enable(
    client: TestClient, agent: dict[str, str]
) -> None:
    client.post(BASE, json=_install(), headers=agent)
    early = client.post(f"{BASE}/greeter/enabled", json={"enabled": True}, headers=agent)
    assert (early.status_code, early.json()["reason"]) == (409, "package-unapproved")

    wrong = {"commit_sha": C2, "content_hash": H1}
    refused = client.post(f"{BASE}/greeter/approve", json=wrong, headers=agent)
    assert (refused.status_code, refused.json()["reason"]) == (409, "pin-mismatch")

    approved = client.post(
        f"{BASE}/greeter/approve", json={"commit_sha": C1, "content_hash": H1}, headers=agent
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["approved_at"] is not None
    assert approved.json()["allow_refused"] is False
    # A second approval of the same pin changes nothing.
    again = client.post(
        f"{BASE}/greeter/approve", json={"commit_sha": C1, "content_hash": H1}, headers=agent
    )
    assert again.json()["approved_at"] == approved.json()["approved_at"]

    on = client.post(f"{BASE}/greeter/enabled", json={"enabled": True}, headers=agent)
    assert on.json()["enabled"] is True
    pins = client.get(BASE, params={"enabled": "true"}, headers=agent).json()["packages"]
    assert [p["name"] for p in pins] == ["greeter"]

    missing = client.post(f"{BASE}/nope/enabled", json={"enabled": True}, headers=agent)
    assert (missing.status_code, missing.json()["reason"]) == (404, "package-not-found")
    assert missing.json()["detail"] == 'no plugin package named "nope"'


def test_a_review_with_refusals_needs_allow_refused(
    client: TestClient, agent: dict[str, str]
) -> None:
    client.post(BASE, json=_install(refused=["a command hook"]), headers=agent)
    pin = {"commit_sha": C1, "content_hash": H1}
    refused = client.post(f"{BASE}/greeter/approve", json=pin, headers=agent)
    assert (refused.status_code, refused.json()["reason"]) == (409, "refused-needs-allow")
    allowed = client.post(
        f"{BASE}/greeter/approve", json={**pin, "allow_refused": True}, headers=agent
    )
    assert allowed.json()["allow_refused"] is True


def test_a_repin_from_another_place_is_approved_disabled(
    client: TestClient, agent: dict[str, str]
) -> None:
    client.post(BASE, json=_install(), headers=agent)
    client.post(
        f"{BASE}/greeter/approve", json={"commit_sha": C1, "content_hash": H1}, headers=agent
    )
    client.post(f"{BASE}/greeter/enabled", json={"enabled": True}, headers=agent)

    same = client.post(
        f"{BASE}/greeter/pending", json=_pending(commit_sha=C1, content_hash=H1), headers=agent
    )
    assert (same.status_code, same.json()["reason"]) == (409, "package-pinned")
    named = client.post(
        f"{BASE}/greeter/pending", json=_pending(review=_review("other")), headers=agent
    )
    assert (named.status_code, named.json()["reason"]) == (409, "package-name-differs")

    # From the same place: approved, it keeps `enabled`.
    pending = client.post(f"{BASE}/greeter/pending", json=_pending(), headers=agent)
    assert pending.json()["pending_commit_sha"] == C2
    kept = client.post(
        f"{BASE}/greeter/approve", json={"commit_sha": C2, "content_hash": H2}, headers=agent
    )
    assert (kept.json()["commit_sha"], kept.json()["enabled"]) == (C2, True)
    assert kept.json()["pending_commit_sha"] is None
    assert kept.json()["source_ref"] == "main"

    # From another repository: approved disabled.
    moved = _pending(fetch_url="https://git.test/moved.git", commit_sha=C1, content_hash=H1)
    client.post(f"{BASE}/greeter/pending", json=moved, headers=agent)
    off = client.post(
        f"{BASE}/greeter/approve", json={"commit_sha": C1, "content_hash": H1}, headers=agent
    )
    assert (off.json()["fetch_url"], off.json()["enabled"]) == ("https://git.test/moved.git", False)

    client.post(f"{BASE}/greeter/pending", json=_pending(), headers=agent)
    dropped = client.post(f"{BASE}/greeter/discard-pending", headers=agent)
    assert dropped.json()["pending_review"] is None
    assert client.post(f"{BASE}/nope/discard-pending", headers=agent).status_code == 404


def test_a_write_prunes_what_the_review_no_longer_has(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    client.post(BASE, json=_install(), headers=agent)
    client.post(
        f"{BASE}/greeter/approve", json={"commit_sha": C1, "content_hash": H1}, headers=agent
    )
    _seed(
        db,
        "package:greeter",
        {
            "skill:hello": "off",
            "skill:gone": "off",
            "subagent:gone": "off",
            "builtin:Bash": "off",
            HOOK: "off",
            "hook:Stop:0000000000000000": "off",
        },
    )
    _seed(db, "package:greeter/mcp:mem", {"recall": "read"})
    _seed(db, "package:greeter/mcp:old", {"*": "off"})
    _list(db, "package:greeter/mcp:old")
    client.post(f"{BASE}/greeter/enabled", json={"enabled": True}, headers=agent)
    assert _access(db) == [
        f"package:greeter {HOOK}=off",
        "package:greeter skill:hello=off",
        "package:greeter/mcp:mem recall=read",
    ]
    assert _listings(db) == []


def test_a_review_that_cannot_tell_keeps_the_rows(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    # An older vet's review: no `described`, no `local_servers`, no `builtin_tools`.
    old = _review()
    for key in ("described", "local_servers", "builtin_tools"):
        del old[key]
    body = _install()
    body["review"] = old
    client.post(BASE, json=body, headers=agent)
    client.post(
        f"{BASE}/greeter/approve", json={"commit_sha": C1, "content_hash": H1}, headers=agent
    )
    _seed(db, "package:greeter", {"skill:gone": "off", "builtin:Bash": "off"})
    _seed(db, "package:greeter/mcp:box", {"*": "off"})
    _list(db, "package:greeter/mcp:box")
    client.post(f"{BASE}/greeter/enabled", json={"enabled": True}, headers=agent)
    assert _access(db) == [
        "package:greeter builtin:Bash=off",
        "package:greeter skill:gone=off",
        "package:greeter/mcp:box *=off",
    ]
    assert _listings(db) == ["package:greeter/mcp:box"]


def test_a_null_builtin_tools_prunes_the_builtins_as_the_agent_does(
    client: TestClient, db: Any
) -> None:
    # `store.ts` `pruneStaleRows` keeps the built-ins only when the key is absent; a stored
    # null reads as an empty list there, so it prunes them.
    _seed(db, "package:greeter", {"skill:hello": "off", "builtin:Bash": "off"})
    review = _review(builtin_tools=None)
    with db.transaction():
        assert packages.prune_stale_rows(db, "greeter", review) is True
    assert _access(db) == ["package:greeter skill:hello=off"]


def test_a_delete_forgets_by_prefix_never_by_like(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    for name in ("a.b", "a_b"):
        client.post(BASE, json=_install(name), headers=agent)
    for name in ("a.b", "a_b", "aXb", "a.b.c"):
        _seed(db, f"package:{name}", {"skill:s": "off"})
        _seed(db, f"package:{name}/mcp:m", {"t": "read"})
        _list(db, f"package:{name}/mcp:m")
    assert client.post(f"{BASE}/a.b/delete", headers=agent).json() == {"done": True}
    assert client.post(f"{BASE}/a.b/delete", headers=agent).json() == {"done": False}
    assert _access(db) == [
        "package:a.b.c skill:s=off",
        "package:a.b.c/mcp:m t=read",
        "package:aXb skill:s=off",
        "package:aXb/mcp:m t=read",
        "package:a_b skill:s=off",
        "package:a_b/mcp:m t=read",
    ]
    assert _listings(db) == ["package:a.b.c/mcp:m", "package:aXb/mcp:m", "package:a_b/mcp:m"]


def test_each_write_tells_the_warm_caches_and_a_refusal_does_not(
    client: TestClient, agent: dict[str, str], pg_conninfo: str
) -> None:
    with psycopg.connect(pg_conninfo, autocommit=True) as listener:
        listener.execute("LISTEN scadbuddy_warm_evict")
        client.post(BASE, json=_install(), headers=agent)
        client.post(BASE, json=_install(), headers=agent)
        client.post(
            f"{BASE}/greeter/approve", json={"commit_sha": C2, "content_hash": H2}, headers=agent
        )
        client.post(
            f"{BASE}/greeter/approve", json={"commit_sha": C1, "content_hash": H1}, headers=agent
        )
        client.post(f"{BASE}/greeter/delete", headers=agent)
        heard = [(n.channel, n.payload) for n in listener.notifies(timeout=1.0)]
    assert heard == [("scadbuddy_warm_evict", "*")] * 3
