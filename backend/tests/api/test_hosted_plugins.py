"""Hosted plugins (#6932, spec 2026-11-21 §6.1 to §6.3, `api/hosted_plugins.py`): the draft's
routes, path rules and caps, the publish, the immutable versions, and the agent's CRUD reads
(area ``hosted-plugins``, `api/crud_agent_packages.py`)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from scadbuddy.api import hosted_plugins as routes
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.hosted_plugins import bundle, rules
from tests.api.test_crud_agent_cost import token

pytestmark = [pytest.mark.requires_postgres, pytest.mark.agent_migrations]

BASE = "/api/v1/hosted-plugins"
CRUD = "/api/v1/internal/crud/agent/hosted-plugins"
SKILL = b"---\nname: hello\ndescription: Says hello\n---\n\nSay hello.\n"


@pytest.fixture
def db(pg_conninfo: str) -> Any:
    with psycopg.connect(pg_conninfo, autocommit=True) as conn:
        yield conn


def _create(client: TestClient, name: str = "greeter", **body: Any) -> Any:
    return client.post(BASE, json={"name": name, "description": "Says hello", **body})


def _put(client: TestClient, path: str, data: bytes, name: str = "greeter", **params: Any) -> Any:
    return client.put(f"{BASE}/{name}/files/{path}", content=data, params=params)


def _ready(client: TestClient, name: str = "greeter") -> None:
    assert _create(client, name).status_code == 201
    assert _put(client, "skills/hello/SKILL.md", SKILL, name).status_code == 200


def _holds(db: Any) -> set[str]:
    rows = db.execute("SELECT key FROM blob_refs WHERE holder_kind = 'hosted_plugin'").fetchall()
    return {r[0] for r in rows}


def test_a_plugin_is_created_listed_read_and_deleted(client: TestClient) -> None:
    made = _create(client, category="tools", tags=["fun"])
    assert made.status_code == 201, made.text
    view = made.json()
    assert view["name"] == "greeter"
    assert view["latest_version"] is None
    assert (view["category"], view["tags"], view["file_count"]) == ("tools", ["fun"], 0)
    assert [p["name"] for p in client.get(BASE).json()] == ["greeter"]
    assert client.get(f"{BASE}/greeter").json()["description"] == "Says hello"

    again = _create(client)
    assert (again.status_code, again.json()["reason"]) == (409, "hosted-plugin-exists")

    assert client.delete(f"{BASE}/greeter").status_code == 204
    missing = client.get(f"{BASE}/greeter")
    assert (missing.status_code, missing.json()["reason"]) == (404, "hosted-plugin-not-found")
    assert client.delete(f"{BASE}/greeter").status_code == 404


@pytest.mark.parametrize("name", ["Greeter", "a--b", "claude", "x"])
def test_a_name_the_package_rules_refuse_is_refused(client: TestClient, name: str) -> None:
    refused = _create(client, name)
    assert (refused.status_code, refused.json()["reason"]) == (422, "name-refused"), refused.text


def test_a_name_an_installed_or_built_in_package_has_is_taken(client: TestClient, db: Any) -> None:
    built_in = _create(client, "scadbuddy")
    assert (built_in.status_code, built_in.json()["reason"]) == (409, "package-name-taken")
    db.execute(
        "INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref, fetch_url,"
        " commit_sha, content_hash, files, review) VALUES ('greeter', 'git', 'https://g.test',"
        " 'HEAD', 'https://g.test', %s, %s, '{}', '{}')",
        ("a" * 40, "sha256:" + "1" * 64),
    )
    taken = _create(client)
    assert (taken.status_code, taken.json()["reason"]) == (409, "package-name-taken")


def test_a_file_is_written_read_listed_moved_and_deleted(client: TestClient, db: Any) -> None:
    assert _create(client).status_code == 201
    wrote = _put(client, "skills/hello/SKILL.md", SKILL)
    assert wrote.status_code == 200, wrote.text
    assert wrote.json()["sha256"] == hashlib.sha256(SKILL).hexdigest()
    script = _put(client, "skills/hello/run.sh", b"#!/bin/sh\n", executable="true")
    assert script.json()["executable"] is True

    read = client.get(f"{BASE}/greeter/files/skills/hello/SKILL.md")
    assert read.content == SKILL
    assert read.headers["content-type"] == "application/octet-stream"
    assert read.headers["x-content-type-options"] == "nosniff"

    listed = client.get(f"{BASE}/greeter/files").json()
    assert [f["path"] for f in listed["files"]] == ["skills/hello/SKILL.md", "skills/hello/run.sh"]
    assert listed["total_bytes"] == len(SKILL) + 10
    assert client.get(f"{BASE}/greeter").json()["file_count"] == 2

    # An overwrite replaces the bytes and drops the old blob's hold.
    _put(client, "skills/hello/SKILL.md", SKILL + b"More.\n")
    assert f"attachment-{hashlib.sha256(SKILL).hexdigest()}" not in _holds(db)
    assert len(_holds(db)) == 2

    moved = client.post(
        f"{BASE}/greeter/files/moves",
        json={"from": "skills/hello/run.sh", "to": "skills/hello/go.sh"},
    )
    assert moved.status_code == 200, moved.text
    assert (moved.json()["path"], moved.json()["executable"]) == ("skills/hello/go.sh", True)
    assert client.get(f"{BASE}/greeter/files/skills/hello/run.sh").status_code == 404

    assert client.delete(f"{BASE}/greeter/files/skills/hello/go.sh").status_code == 204
    gone = client.delete(f"{BASE}/greeter/files/skills/hello/go.sh")
    assert (gone.status_code, gone.json()["reason"]) == (404, "file-not-found")
    assert len(_holds(db)) == 1


@pytest.mark.parametrize(
    "path",
    [
        ".claude-plugin/plugin.json",
        "hooks/hooks.json",
        ".mcp.json",
        "skills/a/.env",
        "README.md",
        "skills/a%5Cb.md",
        "skills/a%00.md",
    ],
)
def test_a_path_outside_the_rules_is_refused(client: TestClient, path: str) -> None:
    assert _create(client).status_code == 201
    refused = _put(client, path, b"x")
    assert (refused.status_code, refused.json()["reason"]) == (422, "path-refused"), refused.text


def test_only_a_skill_may_hold_an_executable(client: TestClient) -> None:
    assert _create(client).status_code == 201
    refused = _put(client, "commands/go.md", b"x", executable="true")
    assert (refused.status_code, refused.json()["reason"]) == (422, "path-refused")
    _put(client, "skills/a/go.sh", b"x", executable="true")
    out = client.post(
        f"{BASE}/greeter/files/moves", json={"from": "skills/a/go.sh", "to": "agents/go.md"}
    )
    assert (out.status_code, out.json()["reason"]) == (422, "path-refused")


def test_a_path_that_clashes_is_refused(client: TestClient) -> None:
    assert _create(client).status_code == 201
    _put(client, "skills/a/SKILL.md", SKILL)
    _put(client, "skills/b/SKILL.md", SKILL)
    for path in ("skills/a", "skills/A/SKILL.md", "skills/a/SKILL.md/x"):
        refused = _put(client, path, b"x")
        assert (refused.status_code, refused.json()["reason"]) == (409, "path-clash"), path
    onto = client.post(
        f"{BASE}/greeter/files/moves", json={"from": "skills/a/SKILL.md", "to": "skills/b/SKILL.md"}
    )
    assert (onto.status_code, onto.json()["reason"]) == (409, "file-exists")


def test_the_caps_are_kept(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _create(client).status_code == 201
    big = _put(client, "skills/a/big.bin", b"\0" * (rules.MAX_FILE_BYTES + 1))
    assert (big.status_code, big.json()["reason"]) == (413, "file-too-large")
    assert _put(client, "skills/a/max.bin", b"\0" * rules.MAX_FILE_BYTES).status_code == 200

    monkeypatch.setattr(rules, "MAX_FILES", 2)
    assert _put(client, "skills/a/two.md", b"2").status_code == 200
    many = _put(client, "skills/a/three.md", b"3")
    assert (many.status_code, many.json()["reason"]) == (413, "too-many-files")
    assert _put(client, "skills/a/two.md", b"again").status_code == 200  # an overwrite

    monkeypatch.setattr(rules, "MAX_TOTAL_BYTES", rules.MAX_FILE_BYTES + 5)
    total = _put(client, "skills/a/two.md", b"123456")
    assert (total.status_code, total.json()["reason"]) == (413, "draft-too-large")


def test_a_draft_the_checks_refuse_is_not_published(client: TestClient) -> None:
    assert _create(client).status_code == 201
    empty = client.post(f"{BASE}/greeter/publish")
    assert (empty.status_code, empty.json()["reason"]) == (422, "draft-invalid")
    _put(client, "skills/hello/SKILL.md", b"---\nname: hello\n---\n")
    _put(client, "agents/a.md", b"---\nname: a\ndescription: d\nhooks: x\n---\n")
    refused = client.post(f"{BASE}/greeter/publish")
    assert refused.status_code == 422
    problems = refused.json()["problems"]
    assert any("description" in p for p in problems), problems
    assert any("hooks" in p for p in problems), problems
    assert client.get(f"{BASE}/greeter").json()["latest_version"] is None


def test_a_publish_makes_immutable_numbered_versions(client: TestClient, db: Any) -> None:
    _ready(client)
    diff = client.get(f"{BASE}/greeter/draft-diff").json()
    assert diff == {
        "base_version": None,
        "added": ["skills/hello/SKILL.md"],
        "removed": [],
        "changed": [],
        "manifest_changed": False,
    }

    first = client.post(f"{BASE}/greeter/publish")
    assert first.status_code == 200, first.text
    v1 = first.json()
    assert v1["version"] == 1
    assert set(v1["files"]) == {".claude-plugin/plugin.json", "skills/hello/SKILL.md"}
    assert v1["content_hash"] == bundle.content_hash(v1["files"])
    assert client.get(f"{BASE}/greeter").json()["latest_version"] == 1
    assert f"attachment-{v1['zip_sha256']}" in _holds(db)

    same = client.post(f"{BASE}/greeter/publish")
    assert (same.status_code, same.json()["reason"]) == (409, "nothing-to-publish")
    assert client.get(f"{BASE}/greeter/draft-diff").json()["added"] == []

    _put(client, "commands/wave.md", b"Wave.\n")
    _put(client, "skills/hello/SKILL.md", SKILL + b"Again.\n")
    diff = client.get(f"{BASE}/greeter/draft-diff").json()
    assert diff == {
        "base_version": 1,
        "added": ["commands/wave.md"],
        "removed": [],
        "changed": ["skills/hello/SKILL.md"],
        "manifest_changed": False,
    }
    v2 = client.post(f"{BASE}/greeter/publish").json()
    assert v2["version"] == 2
    assert v2["zip_sha256"] != v1["zip_sha256"]
    # The editor's version list (#6937): every version, newest first, as publish answered it.
    assert client.get(f"{BASE}/greeter/versions").json() == [v2, v1]
    missing = client.get(f"{BASE}/nobody/versions")
    assert (missing.status_code, missing.json()["reason"]) == (404, "hosted-plugin-not-found")

    with pytest.raises(psycopg.errors.RestrictViolation):
        db.execute("UPDATE ai_hosted_plugin_versions SET published_by = 'x' WHERE version = 1")
    with pytest.raises(psycopg.errors.RestrictViolation):
        db.execute("DELETE FROM ai_hosted_plugin_versions WHERE version = 1")

    # Deleting the plugin takes its versions and every hold.
    assert client.delete(f"{BASE}/greeter").status_code == 204
    assert db.execute("SELECT count(*) FROM ai_hosted_plugin_versions").fetchone()[0] == 0
    assert _holds(db) == set()


def test_the_same_draft_publishes_the_same_zip_bytes(client: TestClient) -> None:
    _ready(client)
    _put(client, "skills/hello/run.sh", b"#!/bin/sh\necho hi\n", executable="true")
    first = client.post(f"{BASE}/greeter/publish").json()
    zip_bytes = client.get(f"{CRUD}/greeter/versions/1.zip", headers=token(client)).content
    assert client.delete(f"{BASE}/greeter").status_code == 204

    _ready(client)
    _put(client, "skills/hello/run.sh", b"#!/bin/sh\necho hi\n", executable="true")
    second = client.post(f"{BASE}/greeter/publish").json()
    assert (second["zip_sha256"], second["content_hash"]) == (
        first["zip_sha256"],
        first["content_hash"],
    )
    assert client.get(f"{CRUD}/greeter/versions/1.zip", headers=token(client)).content == zip_bytes


def test_the_agent_reads_the_latest_version_and_its_zip(client: TestClient) -> None:
    agent = token(client)
    assert client.get(f"{CRUD}/greeter", headers=agent).json() == {"plugin": None}
    _ready(client)
    _put(client, "skills/hello/run.sh", b"#!/bin/sh\n", executable="true")
    unpublished = client.get(f"{CRUD}/greeter", headers=agent).json()["plugin"]
    assert (unpublished["latest_version"], unpublished["latest"]) == (None, None)
    v1 = client.post(f"{BASE}/greeter/publish").json()

    plugin = client.get(f"{CRUD}/greeter", headers=agent).json()["plugin"]
    assert plugin["latest_version"] == 1
    assert plugin["latest"]["content_hash"] == v1["content_hash"]
    assert plugin["latest"]["files"] == v1["files"]

    got = client.get(f"{CRUD}/greeter/versions/1.zip", headers=agent)
    assert got.status_code == 200
    assert got.headers["content-type"] == "application/zip"
    assert got.headers["x-scadbuddy-crud"] == "1"
    assert (
        hashlib.sha256(got.content).hexdigest()
        == v1["zip_sha256"]
        == got.headers["x-scadbuddy-zip-sha256"]
    )
    with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
        tree = {
            i.filename: (zf.read(i), bool((i.external_attr >> 16) & 0o111)) for i in zf.infolist()
        }
    assert tree[".claude-plugin/plugin.json"][0] == bundle.plugin_json("greeter", "Says hello", 1)
    assert bundle.content_hash(bundle.file_list(tree)) == v1["content_hash"]

    missing = client.get(f"{CRUD}/greeter/versions/2.zip", headers=agent)
    assert (missing.status_code, missing.json()["reason"]) == (404, "hosted-version-not-found")


def test_only_the_agent_credential_reaches_the_crud_routes(client: TestClient) -> None:
    for caller in ("print", "projects"):
        assert client.get(f"{CRUD}/greeter", headers=token(client, caller)).status_code == 403
    assert client.get(f"{CRUD}/greeter").status_code == 401
    assert (
        "hosted-plugins"
        in client.get("/api/v1/internal/crud/agent/areas", headers=token(client)).json()["areas"]
    )


def test_a_draft_over_the_packages_cap_with_its_manifest_is_not_published(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(client)
    monkeypatch.setattr(routes, "PACKAGE_MAX_TOTAL_BYTES", len(SKILL) + 10)
    refused = client.post(f"{BASE}/greeter/publish")
    assert (refused.status_code, refused.json()["reason"]) == (422, "draft-invalid")


def test_a_plugin_is_edited_with_a_patch(client: TestClient) -> None:
    assert _create(client, category="tools", tags=["fun"]).status_code == 201
    only = client.patch(f"{BASE}/greeter", json={"description": "Waves"})
    assert only.status_code == 200, only.text
    assert (only.json()["description"], only.json()["category"], only.json()["tags"]) == (
        "Waves",
        "tools",
        ["fun"],
    )
    both = client.patch(f"{BASE}/greeter", json={"category": None, "tags": ["a", "b"]})
    assert (both.json()["description"], both.json()["category"], both.json()["tags"]) == (
        "Waves",
        None,
        ["a", "b"],
    )
    assert client.get(f"{BASE}/greeter").json()["tags"] == ["a", "b"]
    assert client.patch(f"{BASE}/greeter", json={}).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"tags": ["Bad Tag"]},
        {"category": "Bad Category"},
        {"description": "x" * 1025},
        {"tags": [str(i) for i in range(21)]},
        {"name": "other"},
        {"description": None},
        {"tags": None},
    ],
)
def test_a_patch_is_validated_as_a_create_is(client: TestClient, body: dict[str, Any]) -> None:
    assert _create(client).status_code == 201
    assert client.patch(f"{BASE}/greeter", json=body).status_code == 422


def test_a_patch_of_a_missing_plugin_is_404(client: TestClient) -> None:
    gone = client.patch(f"{BASE}/nope", json={"description": "x"})
    assert (gone.status_code, gone.json()["reason"]) == (404, "hosted-plugin-not-found")


def test_a_new_description_reaches_the_next_version_only(client: TestClient) -> None:
    _ready(client)
    v1 = client.post(f"{BASE}/greeter/publish").json()
    client.patch(f"{BASE}/greeter", json={"description": "Waves"})
    again = client.get(f"{BASE}/greeter").json()
    assert again["description"] == "Waves"
    assert again["latest_version"] == v1["version"]

    # Only the generated plugin.json differs, and that is still a change to publish.
    diff = client.get(f"{BASE}/greeter/draft-diff").json()
    assert diff == {
        "base_version": 1,
        "added": [],
        "removed": [],
        "changed": [],
        "manifest_changed": True,
    }
    second = client.post(f"{BASE}/greeter/publish")
    assert second.status_code == 200, second.text
    v2 = second.json()
    assert v2["version"] == 2
    got = client.get(f"{CRUD}/greeter/versions/2.zip", headers=token(client))
    with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
        manifest = zf.read(rules.MANIFEST_PATH)
    assert manifest == bundle.plugin_json("greeter", "Waves", 2)
    assert client.get(f"{BASE}/greeter/draft-diff").json()["manifest_changed"] is False
    same = client.post(f"{BASE}/greeter/publish")
    assert (same.status_code, same.json()["reason"]) == (409, "nothing-to-publish")


def test_a_description_changed_while_it_is_published_is_refused(
    client: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(client)
    build = bundle.build_zip

    def changed_midway(files: Any) -> bytes:
        db.execute("UPDATE ai_hosted_plugins SET description = 'Waves' WHERE name = 'greeter'")
        return build(files)

    monkeypatch.setattr(bundle, "build_zip", changed_midway)
    refused = client.post(f"{BASE}/greeter/publish")
    assert (refused.status_code, refused.json()["reason"]) == (409, "draft-changed"), refused.text
    assert client.get(f"{BASE}/greeter").json()["latest_version"] is None


def test_a_put_to_a_missing_plugin_is_404(client: TestClient) -> None:
    gone = _put(client, "skills/a/SKILL.md", SKILL, name="nope")
    assert (gone.status_code, gone.json()["reason"]) == (404, "hosted-plugin-not-found")


def test_a_draft_changed_while_it_is_published_is_refused(
    client: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(client)
    build = bundle.build_zip

    def changed_midway(files: Any) -> bytes:
        db.execute(
            "UPDATE ai_hosted_plugin_files SET sha256 = %s WHERE path = 'skills/hello/SKILL.md'",
            ("0" * 64,),
        )
        return build(files)

    monkeypatch.setattr(bundle, "build_zip", changed_midway)
    refused = client.post(f"{BASE}/greeter/publish")
    assert (refused.status_code, refused.json()["reason"]) == (409, "draft-changed"), refused.text
    assert client.get(f"{BASE}/greeter").json()["latest_version"] is None
    assert db.execute("SELECT count(*) FROM ai_hosted_plugin_versions").fetchone()[0] == 0


def _lose_blob(client: TestClient, sha256: str) -> None:
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    state.store.blobs.remove(f"attachment-{sha256}")


def test_a_blob_the_store_lost_is_a_500_with_a_reason(client: TestClient) -> None:
    _ready(client)
    _lose_blob(client, hashlib.sha256(SKILL).hexdigest())
    read = client.get(f"{BASE}/greeter/files/skills/hello/SKILL.md")
    assert (read.status_code, read.json()["reason"]) == (500, "blob-missing")
    publish = client.post(f"{BASE}/greeter/publish")
    assert (publish.status_code, publish.json()["reason"]) == (500, "blob-missing")


def test_a_version_zip_the_store_lost_is_a_500_for_the_agent(client: TestClient) -> None:
    _ready(client)
    v1 = client.post(f"{BASE}/greeter/publish").json()
    _lose_blob(client, v1["zip_sha256"])
    got = client.get(f"{CRUD}/greeter/versions/1.zip", headers=token(client))
    assert (got.status_code, got.json()["reason"]) == (500, "blob-missing")


def test_the_trigger_refuses_to_update_or_delete_a_version_row(client: TestClient, db: Any) -> None:
    _ready(client)
    client.post(f"{BASE}/greeter/publish")
    for sql in (
        "UPDATE ai_hosted_plugin_versions SET published_by = 'x' WHERE version = 1",
        "UPDATE ai_hosted_plugin_versions SET zip_sha256 = %s WHERE version = 1",
        "DELETE FROM ai_hosted_plugin_versions WHERE version = 1",
    ):
        args = ("f" * 64,) if "%s" in sql else ()
        with pytest.raises(psycopg.errors.RestrictViolation):
            db.execute(sql, args)
    row = db.execute("SELECT published_by FROM ai_hosted_plugin_versions").fetchone()
    assert row is not None and row[0] != "x"
