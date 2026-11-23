"""A hosted plugin as an installed package (#6934, spec 2026-11-21 §6.3 step 5 and §6.5,
`api/crud_agent_packages.py`): pinned to a published version, stored only when that version
has the content hash sent, approved by its version, re-pinned into ``pending_*``; the row
carries the hosted plugin's latest version; a package of another kind cannot take a hosted
plugin's name."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.test_crud_agent_cost import token

pytestmark = [pytest.mark.requires_postgres, pytest.mark.agent_migrations]

BASE = "/api/v1/internal/crud/agent/plugin-packages"
HOSTED = "/api/v1/hosted-plugins"
C1 = "a" * 40
H1 = "sha256:" + "1" * 64
SKILL = b"---\nname: hello\ndescription: Says hello\n---\n\nSay hello.\n"


@pytest.fixture
def agent(client: TestClient) -> dict[str, str]:
    return token(client)


def _review(name: str = "greeter") -> dict[str, Any]:
    return {
        "name": name,
        "description": "Hi",
        "version": None,
        "skills": [f"{name}:hello"],
        "commands": [],
        "agents": [],
        "hooks": [],
        "mcp_servers": [],
        "local_servers": [],
        "files": [],
        "refused": [],
        "builtin_tools": [],
        "described": {},
    }


def _publish(client: TestClient, name: str = "greeter", extra: bytes | None = None) -> Any:
    if client.get(f"{HOSTED}/{name}").status_code == 404:
        assert client.post(HOSTED, json={"name": name, "description": "Hi"}).status_code == 201
        assert client.put(f"{HOSTED}/{name}/files/skills/hello/SKILL.md", content=SKILL).is_success
    if extra is not None:
        assert client.put(f"{HOSTED}/{name}/files/commands/wave.md", content=extra).is_success
    published = client.post(f"{HOSTED}/{name}/publish")
    assert published.is_success, published.text
    return published.json()


def _pin(version: int, content_hash: str, name: str = "greeter") -> dict[str, Any]:
    return {
        "fetch_url": f"hosted:{name}",
        "fetch_path": "",
        "pin_kind": "version",
        "version": version,
        "content_hash": content_hash,
        "files": {},
        "review": _review(name),
    }


def _install(version: int, content_hash: str, name: str = "greeter") -> dict[str, Any]:
    return {
        "name": name,
        "source_kind": "hosted",
        "source_url": f"hosted:{name}",
        "source_ref": "",
        **_pin(version, content_hash, name),
    }


def test_a_hosted_install_is_its_version_then_a_repin_waits_for_approval(
    client: TestClient, agent: dict[str, str]
) -> None:
    v1 = _publish(client)
    made = client.post(BASE, json=_install(1, v1["content_hash"]), headers=agent)
    assert made.status_code == 201, made.text
    row = made.json()
    assert (row["source_kind"], row["pin_kind"], row["version"], row["commit_sha"]) == (
        "hosted",
        "version",
        1,
        None,
    )
    assert (row["approved_at"], row["enabled"], row["hosted_latest_version"]) == (None, False, 1)

    ok = client.post(
        f"{BASE}/greeter/approve",
        json={"version": 1, "content_hash": v1["content_hash"]},
        headers=agent,
    )
    assert ok.status_code == 200, ok.text
    assert client.post(f"{BASE}/greeter/enabled", json={"enabled": True}, headers=agent).is_success

    v2 = _publish(client, extra=b"Wave.\n")
    # Version 2 is published and no pin names it yet: the row says so.
    listed = client.get(BASE, headers=agent).json()["packages"]
    assert [(p["version"], p["hosted_latest_version"]) for p in listed] == [(1, 2)]

    again = client.post(
        f"{BASE}/greeter/pending", json={"ref": "", **_pin(1, v1["content_hash"])}, headers=agent
    )
    assert (again.status_code, again.json()["reason"]) == (409, "package-pinned")
    staged = client.post(
        f"{BASE}/greeter/pending", json={"ref": "", **_pin(2, v2["content_hash"])}, headers=agent
    )
    assert staged.status_code == 200, staged.text
    row = staged.json()
    # The current pin keeps loading until the re-pin is approved.
    assert (row["version"], row["pending_pin_kind"], row["pending_version"]) == (1, "version", 2)
    assert row["enabled"] is True

    wrong = client.post(
        f"{BASE}/greeter/approve",
        json={"version": 2, "content_hash": v1["content_hash"]},
        headers=agent,
    )
    assert (wrong.status_code, wrong.json()["reason"]) == (409, "pin-mismatch")
    approved = client.post(
        f"{BASE}/greeter/approve",
        json={"version": 2, "content_hash": v2["content_hash"]},
        headers=agent,
    )
    assert approved.status_code == 200, approved.text
    row = approved.json()
    assert (row["version"], row["content_hash"], row["pending_version"]) == (
        2,
        v2["content_hash"],
        None,
    )
    # A new version of the same hosted plugin keeps the package enabled.
    assert row["enabled"] is True


def test_a_hosted_pin_must_be_a_published_version_with_its_hash(
    client: TestClient, agent: dict[str, str]
) -> None:
    v1 = _publish(client)
    for body in (_install(1, H1), _install(2, v1["content_hash"])):
        refused = client.post(BASE, json=body, headers=agent)
        assert (refused.status_code, refused.json()["reason"]) == (409, "hosted-version-mismatch")
    nobody = client.post(BASE, json=_install(1, v1["content_hash"], name="nobody"), headers=agent)
    assert (nobody.status_code, nobody.json()["reason"]) == (409, "hosted-version-mismatch")
    a_commit = {**_install(1, v1["content_hash"]), "pin_kind": "commit", "commit_sha": C1}
    del a_commit["version"]
    assert client.post(BASE, json=a_commit, headers=agent).status_code == 422
    two_pins = {**_install(1, v1["content_hash"]), "commit_sha": C1}
    assert client.post(BASE, json=two_pins, headers=agent).status_code == 422


def test_another_kind_cannot_take_a_hosted_plugins_name(
    client: TestClient, agent: dict[str, str]
) -> None:
    _publish(client)
    git = {
        "name": "greeter",
        "source_kind": "git",
        "source_url": "https://git.test/greeter.git",
        "source_ref": "HEAD",
        "marketplace_entry": None,
        "fetch_url": "https://git.test/greeter.git",
        "fetch_path": "",
        "commit_sha": C1,
        "content_hash": H1,
        "files": {},
        "review": _review(),
    }
    refused = client.post(BASE, json=git, headers=agent)
    assert (refused.status_code, refused.json()["reason"]) == (409, "hosted-name-taken")
    # A draft that was never published holds its name too.
    assert client.post(HOSTED, json={"name": "drafty", "description": "Hi"}).status_code == 201
    drafty = {**git, "name": "drafty", "review": _review("drafty")}
    refused = client.post(BASE, json=drafty, headers=agent)
    assert (refused.status_code, refused.json()["reason"]) == (409, "hosted-name-taken")
    other = {**git, "name": "other", "review": _review("other")}
    made = client.post(BASE, json=other, headers=agent)
    assert made.status_code == 201, made.text
    assert made.json()["hosted_latest_version"] is None


def test_deleting_the_hosted_plugin_deletes_its_package(
    client: TestClient, agent: dict[str, str]
) -> None:
    v1 = _publish(client)
    assert client.post(BASE, json=_install(1, v1["content_hash"]), headers=agent).status_code == 201
    assert client.delete(f"{HOSTED}/greeter").status_code == 204
    assert client.get(f"{BASE}/greeter", headers=agent).json() == {"package": None}
