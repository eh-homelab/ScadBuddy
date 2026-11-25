"""The agent's plugin packages behind its CRUD routes (#5299 step 5, area
``plugin-packages``, `api/crud_agent_packages.py`): only the ``agent`` credential reaches
them, each runs what ``agent/src/plugins/packages/store.ts`` ran over SQL in one
transaction, and the prune of stale rows (`agent_data/packages.py`) keeps what the agent's
tests (``agent/test/pluginPackages.pg.test.ts``) say it keeps."""

from __future__ import annotations

import hashlib
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from scadbuddy.agent_data import packages
from scadbuddy.render.agent_migrations import AGENT_MIGRATIONS, migrate_agent
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


def test_every_package_write_tells_tools_and_plugins(
    client: TestClient, agent: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#7460: an install, a re-pin, its discard, an approval, a switch and a remove each
    publish ``plugin_packages.changed`` once committed, so Settings → Tools & plugins reads
    the packages again wherever the write came from; a refused write publishes nothing."""
    seen: list[Any] = []
    monkeypatch.setattr(
        "scadbuddy.api.crud_agent_packages.emit", lambda _bus, event: seen.append(event)
    )

    def told() -> list[tuple[str, str]]:
        found = [(e.kind, e.name) for e in seen]
        seen.clear()
        return found

    changed = [("plugin_packages.changed", "greeter")]
    assert client.post(BASE, json=_install(), headers=agent).status_code == 201
    assert told() == changed
    assert client.post(BASE, json=_install(), headers=agent).status_code == 409
    assert told() == []
    assert client.post(f"{BASE}/greeter/pending", json=_pending(), headers=agent).status_code == 200
    assert told() == changed
    assert client.post(f"{BASE}/greeter/discard-pending", headers=agent).status_code == 200
    assert told() == changed
    pin = {"commit_sha": C1, "content_hash": H1}
    assert client.post(f"{BASE}/greeter/approve", json=pin, headers=agent).status_code == 200
    assert told() == changed
    on = client.post(f"{BASE}/greeter/enabled", json={"enabled": True}, headers=agent)
    assert on.status_code == 200
    assert told() == changed
    assert client.post(f"{BASE}/greeter/delete", headers=agent).json() == {"done": True}
    assert told() == changed
    assert client.post(f"{BASE}/greeter/delete", headers=agent).json() == {"done": False}
    assert told() == []


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


# Bytes pins and their mirrors (#6935, spec 2026-11-21 §6.5, §6.6).
MIRRORS = "/api/v1/internal/crud/agent/package-mirrors"
ZIP = b"PK\x05\x06" + b"\0" * 18
ZIP_SHA = hashlib.sha256(ZIP).hexdigest()
OTHER = b"another archive"
OTHER_SHA = hashlib.sha256(OTHER).hexdigest()
OCTETS = {"content-type": "application/octet-stream"}


def _mirror(client: TestClient, agent: dict[str, str], data: bytes, name: str = "greeter") -> str:
    sha = hashlib.sha256(data).hexdigest()
    sent = client.post(f"{MIRRORS}/{name}", content=data, headers={**agent, **OCTETS})
    assert sent.status_code == 200, sent.text
    assert sent.json() == {"bytes_sha256": sha}
    return sha


def _archive(name: str = "greeter", sha: str = ZIP_SHA) -> dict[str, Any]:
    body = _install(name)
    body.update(
        source_kind="archive",
        source_url=f"https://files.test/{name}.zip",
        source_ref="",
        fetch_url=f"https://files.test/{name}.zip",
        fetch_path="greeter-1.0",
        pin_kind="bytes",
        bytes_sha256=sha,
    )
    del body["commit_sha"]
    return body


def _held(db: Any) -> list[str]:
    rows = db.execute(
        "SELECT holder_id FROM blob_refs WHERE holder_kind = 'plugin_package_mirror'"
    ).fetchall()
    return sorted(r[0] for r in rows)


def _age_holds(db: Any) -> None:
    db.execute(
        "UPDATE blob_refs SET created_at = now() - interval '2 days'"
        " WHERE holder_kind = 'plugin_package_mirror'"
    )


def test_a_mirror_is_read_back_only_by_a_package_that_names_it(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    _mirror(client, agent, ZIP)
    assert _held(db) == [f"greeter/{ZIP_SHA}"]
    # Kept, but no package names it yet.
    unnamed = client.get(f"{MIRRORS}/greeter/{ZIP_SHA}", headers=agent)
    assert (unnamed.status_code, unnamed.json()["reason"]) == (404, "mirror-not-found")
    client.post(BASE, json=_archive(), headers=agent)
    got = client.get(f"{MIRRORS}/greeter/{ZIP_SHA}", headers=agent)
    assert (got.status_code, got.content) == (200, ZIP)
    # Another package's name, or bytes the package does not name, are not served.
    assert client.get(f"{MIRRORS}/other/{ZIP_SHA}", headers=agent).status_code == 404
    _mirror(client, agent, OTHER)
    assert client.get(f"{MIRRORS}/greeter/{OTHER_SHA}", headers=agent).status_code == 404
    assert (
        client.post(f"{MIRRORS}/-bad", content=ZIP, headers={**agent, **OCTETS}).status_code == 422
    )
    for caller in ("print", "projects"):
        other = token(client, caller)
        assert client.get(f"{MIRRORS}/greeter/{ZIP_SHA}", headers=other).status_code == 403
        sent = client.post(f"{MIRRORS}/greeter", content=ZIP, headers={**other, **OCTETS})
        assert sent.status_code == 403
    # Gone with the package, though the blob may still be in the store.
    client.post(f"{BASE}/greeter/delete", headers=agent)
    assert client.get(f"{MIRRORS}/greeter/{ZIP_SHA}", headers=agent).status_code == 404


def test_a_pending_repin_reads_its_mirror(client: TestClient, agent: dict[str, str]) -> None:
    _mirror(client, agent, ZIP)
    client.post(BASE, json=_archive(), headers=agent)
    client.post(
        f"{BASE}/greeter/approve",
        json={"bytes_sha256": ZIP_SHA, "content_hash": H1},
        headers=agent,
    )
    _mirror(client, agent, OTHER)
    pending = {
        **_pending(),
        "ref": "",
        "fetch_url": "https://files.test/greeter.zip",
        "fetch_path": "greeter-1.0",
        "pin_kind": "bytes",
        "bytes_sha256": OTHER_SHA,
    }
    del pending["commit_sha"]
    staged = client.post(f"{BASE}/greeter/pending", json=pending, headers=agent)
    assert staged.status_code == 200, staged.text
    for sha, data in ((ZIP_SHA, ZIP), (OTHER_SHA, OTHER)):
        got = client.get(f"{MIRRORS}/greeter/{sha}", headers=agent)
        assert (got.status_code, got.content) == (200, data)


def test_a_bytes_pin_is_stored_only_with_its_mirror(
    client: TestClient, agent: dict[str, str]
) -> None:
    missing = client.post(BASE, json=_archive(), headers=agent)
    assert (missing.status_code, missing.json()["reason"]) == (409, "mirror-missing")
    _mirror(client, agent, ZIP)
    made = client.post(BASE, json=_archive(), headers=agent)
    assert made.status_code == 201, made.text
    row = made.json()
    assert (row["pin_kind"], row["commit_sha"], row["bytes_sha256"]) == ("bytes", None, ZIP_SHA)
    assert (row["source_kind"], row["source_ref"], row["fetch_path"]) == (
        "archive",
        "",
        "greeter-1.0",
    )
    # A repository install is still a commit pin.
    made = client.post(BASE, json=_install("other"), headers=agent)
    assert (made.json()["pin_kind"], made.json()["bytes_sha256"]) == ("commit", None)


def test_a_pin_must_fit_its_source(client: TestClient, agent: dict[str, str]) -> None:
    _mirror(client, agent, ZIP)
    repo_bytes = {**_archive(), "source_kind": "git", "source_ref": "HEAD"}
    assert client.post(BASE, json=repo_bytes, headers=agent).status_code == 422
    archive_commit = {**_install(), "source_kind": "archive", "source_ref": ""}
    assert client.post(BASE, json=archive_commit, headers=agent).status_code == 422
    both = {**_archive(), "commit_sha": C1}
    assert client.post(BASE, json=both, headers=agent).status_code == 422
    no_ref = {**_install(), "source_ref": ""}
    assert client.post(BASE, json=no_ref, headers=agent).status_code == 422
    # A marketplace-url entry may be either kind, and names its entry.
    url_entry = {
        **_archive(),
        "source_kind": "marketplace-url",
        "source_url": "https://plugins.test/marketplace.json",
        "marketplace_entry": "greeter",
    }
    made = client.post(BASE, json=url_entry, headers=agent)
    assert made.status_code == 201, made.text
    assert made.json()["marketplace_entry"] == "greeter"


def test_a_bytes_repin_is_approved_by_its_sha256(client: TestClient, agent: dict[str, str]) -> None:
    _mirror(client, agent, ZIP)
    client.post(BASE, json=_archive(), headers=agent)
    by_commit = client.post(
        f"{BASE}/greeter/approve", json={"commit_sha": C1, "content_hash": H1}, headers=agent
    )
    assert (by_commit.status_code, by_commit.json()["reason"]) == (409, "pin-mismatch")
    ok = client.post(
        f"{BASE}/greeter/approve",
        json={"bytes_sha256": ZIP_SHA, "content_hash": H1},
        headers=agent,
    )
    assert ok.status_code == 200, ok.text
    client.post(f"{BASE}/greeter/enabled", json={"enabled": True}, headers=agent)

    pending = {
        **_pending(),
        "ref": "",
        "fetch_url": "https://files.test/greeter.zip",
        "fetch_path": "greeter-1.0",
        "pin_kind": "bytes",
        "bytes_sha256": OTHER_SHA,
    }
    del pending["commit_sha"]
    unheld = client.post(f"{BASE}/greeter/pending", json=pending, headers=agent)
    assert (unheld.status_code, unheld.json()["reason"]) == (409, "mirror-missing")
    _mirror(client, agent, OTHER)
    staged = client.post(f"{BASE}/greeter/pending", json=pending, headers=agent)
    assert staged.status_code == 200, staged.text
    assert (staged.json()["pending_pin_kind"], staged.json()["pending_bytes_sha256"]) == (
        "bytes",
        OTHER_SHA,
    )
    # The current pin is the one that loads until the re-pin is approved.
    assert staged.json()["bytes_sha256"] == ZIP_SHA
    same = {**pending, "bytes_sha256": ZIP_SHA, "content_hash": H1}
    again = client.post(f"{BASE}/greeter/pending", json=same, headers=agent)
    assert (again.status_code, again.json()["reason"]) == (409, "package-pinned")
    a_commit = _pending()
    refused = client.post(f"{BASE}/greeter/pending", json=a_commit, headers=agent)
    assert refused.status_code == 422, refused.text

    approved = client.post(
        f"{BASE}/greeter/approve",
        json={"bytes_sha256": OTHER_SHA, "content_hash": H2},
        headers=agent,
    )
    assert approved.status_code == 200, approved.text
    row = approved.json()
    assert (row["bytes_sha256"], row["pending_bytes_sha256"], row["pending_pin_kind"]) == (
        OTHER_SHA,
        None,
        None,
    )
    # From the same place: still enabled.
    assert row["enabled"] is True
    neither = client.post(f"{BASE}/greeter/approve", json={"content_hash": H2}, headers=agent)
    assert neither.status_code == 422


def test_a_mirror_no_pin_names_is_released_after_its_grace(
    client: TestClient, agent: dict[str, str], db: Any
) -> None:
    _mirror(client, agent, ZIP)
    _mirror(client, agent, OTHER)
    client.post(BASE, json=_archive(), headers=agent)
    _age_holds(db)
    client.post(BASE, json=_install("other"), headers=agent)
    assert _held(db) == [f"greeter/{ZIP_SHA}"]
    # Released once the package that named it is gone.
    client.post(f"{BASE}/greeter/delete", headers=agent)
    _age_holds(db)
    client.post(f"{BASE}/other/delete", headers=agent)
    assert _held(db) == []


def test_a_mirror_is_held_for_the_package_it_was_sent_for(
    client: TestClient, agent: dict[str, str]
) -> None:
    # Sent for "other": "greeter" cannot pin it until it is sent for "greeter" too.
    _mirror(client, agent, ZIP, name="other")
    refused = client.post(BASE, json=_archive(), headers=agent)
    assert (refused.status_code, refused.json()["reason"]) == (409, "mirror-missing")
    _mirror(client, agent, ZIP)
    assert client.post(BASE, json=_archive(), headers=agent).status_code == 201


# #7191: the strict pending CHECKs of 20261123T1756Z_plugin_package_pending_kind_strict.sql,
# and its backfill of the rows that the looser CHECKs of 20261122T0323Z let in.
STRICT = "20261123T1756Z_plugin_package_pending_kind_strict"
Z1 = "c" * 64
Z2 = "d" * 64
PENDING_COLUMNS = (
    "pending_ref",
    "pending_fetch_url",
    "pending_fetch_path",
    "pending_pin_kind",
    "pending_commit_sha",
    "pending_bytes_sha256",
    "pending_version",
    "pending_content_hash",
    "pending_files",
    "pending_review",
)


def _insert(conn: Any, name: str, source_kind: str = "git", **pending: Any) -> None:
    """A commit-pinned row of ``source_kind``. ``pending`` sets re-pin columns (without the
    ``pending_`` prefix); with a ``content_hash`` the re-pin's place, files and review too."""
    columns: dict[str, Any] = {
        "name": name,
        "source_kind": source_kind,
        "source_url": f"https://git.test/{name}.git",
        "source_ref": "HEAD",
        "marketplace_entry": name if source_kind.startswith("marketplace") else None,
        "fetch_url": f"https://git.test/{name}.git",
        "fetch_path": "",
        "pin_kind": "commit",
        "commit_sha": C1,
        "content_hash": H1,
        "files": Jsonb({}),
        "review": Jsonb(_review(name)),
    }
    if "content_hash" in pending:
        columns.update(
            pending_ref="main",
            pending_fetch_url=f"https://git.test/{name}.git",
            pending_fetch_path="",
            pending_files=Jsonb({}),
            pending_review=Jsonb(_review(name)),
        )
    columns.update({f"pending_{k}": v for k, v in pending.items()})
    names = ", ".join(columns)
    marks = ", ".join(["%s"] * len(columns))
    conn.execute(
        f"INSERT INTO ai_plugin_packages ({names}) VALUES ({marks})".encode(),
        tuple(columns.values()),
    )


def _pending_of(conn: Any, name: str) -> dict[str, Any]:
    cols = ", ".join(PENDING_COLUMNS)
    row = conn.execute(
        f"SELECT {cols} FROM ai_plugin_packages WHERE name = %s".encode(), (name,)
    ).fetchone()
    return dict(zip(PENDING_COLUMNS, row, strict=True))


def test_the_backfill_makes_every_loose_row_strict(pg_conninfo: str) -> None:
    before = tuple(m for m in AGENT_MIGRATIONS if m.id != STRICT)
    assert len(before) == len(AGENT_MIGRATIONS) - 1
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate_agent(conn, before)
        # What the looser CHECKs let in: an old pod's commit re-pin with no kind, and a
        # kind that it left behind with no re-pin.
        _insert(conn, "old-repin", commit_sha=C2, content_hash=H2)
        _insert(conn, "stale-kind", pin_kind="bytes")
        # Rows that are strict already.
        _insert(
            conn, "strict", "marketplace-url", pin_kind="bytes", bytes_sha256=Z1, content_hash=H2
        )
        _insert(conn, "plain")
        # Re-pins that neither CHECK lets in: written only with both CHECKs off.
        conn.execute(
            "ALTER TABLE ai_plugin_packages"
            " DROP CONSTRAINT ai_plugin_packages_pending_check,"
            " DROP CONSTRAINT ai_plugin_packages_pending_source_pin_check"
        )
        _insert(conn, "no-kind", content_hash=H2)
        _insert(
            conn, "two-pins", pin_kind="commit", commit_sha=C2, bytes_sha256=Z2, content_hash=H2
        )
        _insert(conn, "wrong-source", pin_kind="bytes", bytes_sha256=Z2, content_hash=H2)
        conn.execute(
            "ALTER TABLE ai_plugin_packages"
            " ADD CONSTRAINT ai_plugin_packages_pending_check CHECK (true),"
            " ADD CONSTRAINT ai_plugin_packages_pending_source_pin_check CHECK (true)"
        )
        assert migrate_agent(conn) == [STRICT]

        old = _pending_of(conn, "old-repin")
        assert (old["pending_pin_kind"], old["pending_commit_sha"]) == ("commit", C2)
        assert old["pending_content_hash"] == H2
        assert set(_pending_of(conn, "stale-kind").values()) == {None}
        strict = _pending_of(conn, "strict")
        assert (strict["pending_pin_kind"], strict["pending_bytes_sha256"]) == ("bytes", Z1)
        assert set(_pending_of(conn, "plain").values()) == {None}
        # A proposal that the strict CHECKs refuse is discarded whole; the pin stays.
        for name in ("no-kind", "two-pins", "wrong-source"):
            assert set(_pending_of(conn, name).values()) == {None}, name
            pin = conn.execute(
                "SELECT pin_kind, commit_sha FROM ai_plugin_packages WHERE name = %s", (name,)
            ).fetchone()
            assert pin == ("commit", C1), name


@pytest.mark.parametrize(
    ("change", "constraint"),
    [
        # A re-pin with no kind: the previous build's commit re-pin.
        ("pending_pin_kind = NULL", "ai_plugin_packages_pending_check"),
        # A kind with no re-pin: the previous build's discard and approve.
        (
            "pending_commit_sha = NULL, pending_content_hash = NULL, pending_files = NULL,"
            " pending_review = NULL, pending_ref = NULL, pending_fetch_url = NULL,"
            " pending_fetch_path = NULL",
            "ai_plugin_packages_pending_check",
        ),
        # A pin column that is not the kind's, or a second one.
        ("pending_pin_kind = 'bytes'", "ai_plugin_packages_pending_check"),
        (f"pending_bytes_sha256 = '{Z1}'", "ai_plugin_packages_pending_check"),
        # A kind that the source cannot have.
        (
            f"pending_pin_kind = 'bytes', pending_commit_sha = NULL, pending_bytes_sha256 = '{Z1}'",
            "ai_plugin_packages_pending_source_pin_check",
        ),
    ],
)
def test_the_strict_checks_refuse_the_loose_forms(
    pg_conninfo: str, change: str, constraint: str
) -> None:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        migrate_agent(conn)
        _insert(conn, "greeter", pin_kind="commit", commit_sha=C2, content_hash=H2)
        with pytest.raises(psycopg.errors.CheckViolation) as refused:
            conn.execute(f"UPDATE ai_plugin_packages SET {change} WHERE name = 'greeter'".encode())
        assert refused.value.diag.constraint_name == constraint
