"""A marketplace feed's packages for the agent (#6933, spec 2026-11-21 §6.4, area
``plugin-feeds``, `api/crud_agent_packages.py`): the migration's table, and the CRUD read the
agent serves ``marketplace.json`` and the version zips from."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.api.test_crud_agent_cost import token

pytestmark = [pytest.mark.requires_postgres, pytest.mark.agent_migrations]

CRUD = "/api/v1/internal/crud/agent/plugin-feeds"
HOSTED = "/api/v1/hosted-plugins"
C1 = "a" * 40
H1 = "sha256:" + "1" * 64
SKILL = b"---\nname: hello\ndescription: Says hello\n---\n\nSay hello.\n"


@pytest.fixture
def db(pg_conninfo: str) -> Any:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        yield conn


def _package(
    db: Any,
    name: str,
    *,
    kind: str = "git",
    approved: bool = True,
    fetch_path: str = "",
    content_hash: str = H1,
    description: str | None = "A plugin",
    version: int | None = None,
) -> None:
    """A hosted package is pinned to its `version`; every other kind to the commit C1."""
    review = {"name": name, "description": description, "version": None}
    db.execute(
        "INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref,"
        " marketplace_entry, fetch_url, fetch_path, pin_kind, commit_sha, version, content_hash,"
        " files, review, approved_at)"
        " VALUES (%s, %s, %s, 'main', NULL, %s, %s, %s, %s, %s, %s, '{}', %s, %s)",
        (
            name,
            kind,
            f"https://git.test/{name}.git",
            f"https://git.test/{name}.git",
            fetch_path,
            "version" if version is not None else "commit",
            None if version is not None else C1,
            version,
            content_hash,
            json.dumps(review),
            "2026-11-22T00:00:00Z" if approved else None,
        ),
    )


def _list(db: Any, *names: str, feed: str = "default") -> None:
    for name in names:
        db.execute(
            "INSERT INTO ai_plugin_feed_packages (feed_id, package_name)"
            " SELECT id, %s FROM ai_plugin_feeds WHERE slug = %s",
            (name, feed),
        )


def test_the_default_feed_and_its_packages_are_read(client: TestClient, db: Any) -> None:
    agent = token(client)
    _package(db, "kept", fetch_path="plugins/kept")
    _package(db, "waiting", approved=False)
    _package(db, "unlisted")
    _list(db, "waiting", "kept")

    answer = client.get(f"{CRUD}/default", headers=agent)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["feed"] == {"slug": "default", "name": "scadbuddy", "owner": "ScadBuddy"}
    assert [p["name"] for p in body["packages"]] == ["kept", "waiting"]
    kept, waiting = body["packages"]
    assert kept == {
        "name": "kept",
        "source_kind": "git",
        "fetch_url": "https://git.test/kept.git",
        "fetch_path": "plugins/kept",
        "commit_sha": C1,
        "content_hash": H1,
        "pin_kind": "commit",
        "bytes_sha256": None,
        "approved": True,
        "allow_refused": False,
        "description": "A plugin",
        "hosted": None,
    }
    assert waiting["approved"] is False


def test_an_unknown_feed_is_none(client: TestClient) -> None:
    answer = client.get(f"{CRUD}/nowhere", headers=token(client))
    assert answer.status_code == 200
    assert answer.json() == {"feed": None, "packages": []}


def test_only_the_agent_reads_a_feed(client: TestClient) -> None:
    assert client.get(f"{CRUD}/default").status_code in (401, 403)
    assert client.get(f"{CRUD}/default", headers=token(client, "render")).status_code == 403


def test_a_deleted_package_leaves_the_feed(client: TestClient, db: Any) -> None:
    agent = token(client)
    _package(db, "gone")
    _list(db, "gone")
    assert client.post(
        "/api/v1/internal/crud/agent/plugin-packages/gone/delete", headers=agent
    ).json() == {"done": True}
    assert client.get(f"{CRUD}/default", headers=agent).json()["packages"] == []


def test_a_hosted_package_names_the_version_its_pin_is(client: TestClient, db: Any) -> None:
    assert client.post(HOSTED, json={"name": "greeter", "description": "Hi"}).status_code == 201
    put = client.put(f"{HOSTED}/greeter/files/skills/hello/SKILL.md", content=SKILL)
    assert put.status_code == 200, put.text
    v1 = client.post(f"{HOSTED}/greeter/publish").json()
    client.put(f"{HOSTED}/greeter/files/commands/wave.md", content=b"Wave.\n")
    v2 = client.post(f"{HOSTED}/greeter/publish").json()
    assert v2["version"] == 2
    # The approved pin is version 1; version 2 is newer but not the pin.
    _package(db, "greeter", kind="hosted", content_hash=v1["content_hash"], version=1)
    _list(db, "greeter")

    [entry] = client.get(f"{CRUD}/default", headers=token(client)).json()["packages"]
    assert entry["source_kind"] == "hosted"
    assert entry["hosted"] == {"version": 1, "zip_sha256": v1["zip_sha256"]}


def test_a_hosted_plugin_published_back_to_old_files_is_listed_once(
    client: TestClient, db: Any
) -> None:
    """Publish A, B, then A again: versions 1 and 3 have the same draft files. Each version's
    plugin.json names its own number, so their content hashes differ, and the pin names one
    version; the feed lists the package once, at that version, and its zip is served."""
    agent = token(client)
    assert client.post(HOSTED, json={"name": "greeter", "description": "Hi"}).status_code == 201
    assert client.put(f"{HOSTED}/greeter/files/skills/hello/SKILL.md", content=SKILL).is_success
    v1 = client.post(f"{HOSTED}/greeter/publish").json()
    assert client.put(f"{HOSTED}/greeter/files/commands/wave.md", content=b"Wave.\n").is_success
    assert client.post(f"{HOSTED}/greeter/publish").json()["version"] == 2
    assert client.delete(f"{HOSTED}/greeter/files/commands/wave.md").status_code == 204
    v3 = client.post(f"{HOSTED}/greeter/publish").json()
    assert v3["version"] == 3
    assert v3["content_hash"] != v1["content_hash"]
    _package(db, "greeter", kind="hosted", content_hash=v3["content_hash"], version=3)
    _list(db, "greeter")

    [entry] = client.get(f"{CRUD}/default", headers=agent).json()["packages"]
    assert entry["hosted"] == {"version": 3, "zip_sha256": v3["zip_sha256"]}
    zipped = client.get(
        "/api/v1/internal/crud/agent/hosted-plugins/greeter/versions/3.zip", headers=agent
    )
    assert zipped.status_code == 200
    assert hashlib.sha256(zipped.content).hexdigest() == v3["zip_sha256"]

    # The schema does not make a content hash unique per plugin: a second version row with the
    # pin's hash still gives one entry, the newest such version.
    db.execute(
        "INSERT INTO ai_hosted_plugin_versions (plugin_id, version, zip_sha256, content_hash,"
        " files, published_by) SELECT plugin_id, 4, zip_sha256, content_hash, files, published_by"
        " FROM ai_hosted_plugin_versions WHERE version = 3"
    )
    [entry] = client.get(f"{CRUD}/default", headers=agent).json()["packages"]
    assert entry["hosted"]["version"] == 4


def test_a_feed_read_can_name_one_package(client: TestClient, db: Any) -> None:
    agent = token(client)
    _package(db, "one")
    _package(db, "two")
    _list(db, "one", "two")
    answer = client.get(f"{CRUD}/default", params={"package": "two"}, headers=agent).json()
    assert [p["name"] for p in answer["packages"]] == ["two"]
    assert answer["feed"]["slug"] == "default"
    none = client.get(f"{CRUD}/default", params={"package": "three"}, headers=agent).json()
    assert none["packages"] == [] and none["feed"] is not None


def test_the_table_refuses_a_package_that_is_not_installed(client: TestClient, db: Any) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _list(db, "nothing")


def test_a_bytes_pin_names_its_file(client: TestClient, db: Any) -> None:
    """#6936: an archive's pin is the sha256 of its file, which the agent serves from the mirror."""
    sha = "b" * 64
    db.execute(
        "INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref,"
        " fetch_url, fetch_path, pin_kind, bytes_sha256, content_hash, files, review,"
        " approved_at, allow_refused)"
        " VALUES ('zipped', 'archive', 'https://files.test/z.zip', '', 'https://files.test/z.zip',"
        " '', 'bytes', %s, %s, '{}', '{}', now(), true)",
        (sha, H1),
    )
    _list(db, "zipped")
    [entry] = client.get(f"{CRUD}/default", headers=token(client)).json()["packages"]
    assert entry["pin_kind"] == "bytes"
    assert entry["bytes_sha256"] == sha
    assert entry["commit_sha"] is None
    assert entry["allow_refused"] is True


def test_a_package_is_added_to_the_feed_and_removed(client: TestClient, db: Any) -> None:
    agent = token(client)
    _package(db, "added")
    url = f"{CRUD}/default/packages/added"
    assert client.put(url, headers=agent).json() == {"done": True}
    assert client.put(url, headers=agent).json() == {"done": False}
    names = [p["name"] for p in client.get(f"{CRUD}/default", headers=agent).json()["packages"]]
    assert names == ["added"]
    assert client.delete(url, headers=agent).json() == {"done": True}
    assert client.delete(url, headers=agent).json() == {"done": False}
    assert client.get(f"{CRUD}/default", headers=agent).json()["packages"] == []


def test_adding_needs_the_feed_and_the_package(client: TestClient, db: Any) -> None:
    agent = token(client)
    _package(db, "here")
    no_feed = client.put(f"{CRUD}/nowhere/packages/here", headers=agent)
    assert no_feed.status_code == 404
    assert no_feed.json()["reason"] == "feed-not-found"
    no_package = client.put(f"{CRUD}/default/packages/missing", headers=agent)
    assert no_package.status_code == 404
    assert no_package.json()["reason"] == "package-not-found"
    assert client.delete(f"{CRUD}/nowhere/packages/here", headers=agent).status_code == 404


def test_only_the_agent_changes_a_feed(client: TestClient, db: Any) -> None:
    _package(db, "guarded")
    url = f"{CRUD}/default/packages/guarded"
    assert client.put(url).status_code in (401, 403)
    assert client.put(url, headers=token(client, "render")).status_code == 403
    assert client.delete(url, headers=token(client, "render")).status_code == 403
