"""`POST /models/{slug}/source/patch`, `PUT /source` with a `base`, and agent authorship
of the commits both make (#252)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from scadbuddy.core.authorship import (
    AGENT_AUTHOR_NAME,
    AUTHOR_HEADER,
    AUTHOR_SESSION_HEADER,
    PRINCIPAL_TRAILER,
    SESSION_TRAILER,
    InvalidAuthorError,
    author_from,
)
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.history import git_env

pytestmark = pytest.mark.requires_git

SLUG = "keychain"
FIRST = 'width = 10;\nlabel = "hi";\ncube(width);\n'
AGENT = {AUTHOR_HEADER: "token:abc123", AUTHOR_SESSION_HEADER: "0f9e2d3c-session"}


def upload(client: TestClient, source: str = FIRST) -> dict[str, Any]:
    response = client.post(
        "/api/v1/models",
        files={"file": (f"{SLUG}.scad", source.encode(), "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def patch(client: TestClient, body: dict[str, Any], **headers: str) -> Any:
    return client.post(f"/api/v1/models/{SLUG}/source/patch", json=body, headers=headers)


def source(client: TestClient) -> str:
    text: str = client.get(f"/api/v1/models/{SLUG}/source").text
    return text


def test_a_unified_diff_against_the_current_revision_is_one_revision(client: TestClient) -> None:
    base = upload(client)["version"]
    diff = "--- a/model.scad\n+++ b/model.scad\n@@ -1 +1 @@\n-width = 10;\n+width = 25;\n"

    response = patch(client, {"base": base, "patch": diff, "message": "Make it wider"})

    assert response.status_code == 200, response.text
    assert response.json()["version"] != base
    assert source(client) == FIRST.replace("10", "25")
    listed = client.get(f"/api/v1/models/{SLUG}/versions").json()
    assert [v["message"] for v in listed] == ["Make it wider", f"Add {SLUG}"]


def test_search_replace_edits_apply_against_a_short_base(client: TestClient) -> None:
    base = upload(client)["version"][:7]
    edits = [{"search": '"hi"', "replace": '"hello"'}]

    response = patch(client, {"base": base, "edits": edits})

    assert response.status_code == 200, response.text
    assert '"hello"' in source(client)


def test_a_stale_base_is_a_409_naming_the_current_revision(client: TestClient) -> None:
    base = upload(client)["version"]
    moved = client.put(f"/api/v1/models/{SLUG}/source", json={"source": FIRST + "sphere(1);\n"})
    current = moved.json()["version"]

    response = patch(client, {"base": base, "edits": [{"search": "10", "replace": "11"}]})

    assert response.status_code == 409
    problem = response.json()
    assert (problem["base"], problem["current"]) == (base, current)
    assert "moved on" in problem["detail"]
    assert "sphere(1);" in source(client) and "11" not in source(client)


def test_a_hunk_that_does_not_apply_is_a_422_and_writes_nothing(client: TestClient) -> None:
    base = upload(client)["version"]

    response = patch(client, {"base": base, "patch": "@@ -1 +1 @@\n-depth = 1;\n+depth = 2;\n"})

    assert response.status_code == 422
    assert "hunk 1" in response.json()["detail"]
    assert source(client) == FIRST
    assert len(client.get(f"/api/v1/models/{SLUG}/versions").json()) == 1


def test_exactly_one_of_patch_and_edits(client: TestClient) -> None:
    base = upload(client)["version"]
    assert patch(client, {"base": base}).status_code == 422
    both = {
        "base": base,
        "patch": "@@ -1 +1 @@\n-a\n+b\n",
        "edits": [{"search": "a", "replace": "b"}],
    }
    assert patch(client, both).status_code == 422


def test_a_patched_source_that_does_not_parse_is_refused_unless_forced(
    client: TestClient,
) -> None:
    base = upload(client)["version"]
    edits = [{"search": "cube(width);", "replace": "%%FAIL%%"}]

    assert patch(client, {"base": base, "edits": edits}).status_code == 422
    assert source(client) == FIRST
    assert patch(client, {"base": base, "edits": edits, "force": True}).status_code == 200


def test_a_built_in_cannot_be_patched(client: TestClient) -> None:
    response = client.post(
        "/api/v1/models/builtin:keychain/source/patch",
        json={"base": "0" * 40, "edits": [{"search": "a", "replace": "b"}]},
    )
    assert response.status_code in (403, 404)


def test_put_source_with_a_stale_base_is_refused(client: TestClient) -> None:
    base = upload(client)["version"]
    assert (
        client.put(f"/api/v1/models/{SLUG}/source", json={"source": "a = 1;\n"}).status_code == 200
    )

    stale = client.put(f"/api/v1/models/{SLUG}/source", json={"source": "a = 2;\n", "base": base})

    assert stale.status_code == 409
    assert source(client) == "a = 1;\n"
    current = stale.json()["current"]
    fresh = client.put(
        f"/api/v1/models/{SLUG}/source", json={"source": "a = 2;\n", "base": current}
    )
    assert fresh.status_code == 200


def _last_commit(paths: DataPaths) -> str:
    return subprocess.run(
        ["git", "-c", f"safe.directory={paths.models}", "log", "-1", "--format=%an%n%cn%n%B"],
        cwd=paths.models,
        env=git_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_an_agent_edit_is_authored_as_the_agent_with_trailers(
    client: TestClient, paths: DataPaths
) -> None:
    base = upload(client)["version"]

    response = patch(client, {"base": base, "edits": [{"search": "10", "replace": "12"}]}, **AGENT)

    assert response.status_code == 200, response.text
    author, committer, *message = _last_commit(paths).splitlines()
    assert (author, committer) == (AGENT_AUTHOR_NAME, "ScadBuddy")
    assert f"{PRINCIPAL_TRAILER}: token:abc123" in message
    assert f"{SESSION_TRAILER}: 0f9e2d3c-session" in message
    latest, first = client.get(f"/api/v1/models/{SLUG}/versions").json()
    assert latest["author"] == AGENT_AUTHOR_NAME
    assert latest["agent"] == {"principal": "token:abc123", "session": "0f9e2d3c-session"}
    assert latest["message"] == f"Edit {SLUG} source"
    assert (first["author"], first["agent"]) == ("ScadBuddy", None)


def test_a_restore_in_a_threadpool_route_is_the_agents_too(
    client: TestClient, paths: DataPaths
) -> None:
    """`restore_version` is a plain `def` route: Starlette runs it in its threadpool,
    which must still see the request's author."""
    base = upload(client)["version"]
    client.put(f"/api/v1/models/{SLUG}/source", json={"source": "a = 1;\n"})

    restored = client.post(
        f"/api/v1/models/{SLUG}/versions/{base}/restore", headers={AUTHOR_HEADER: "browser"}
    )

    assert restored.status_code == 200, restored.text
    assert restored.json()["agent"] == {"principal": "browser", "session": None}


def test_the_headless_browsers_marker_names_the_session(client: TestClient) -> None:
    upload(client)
    response = client.put(
        f"/api/v1/models/{SLUG}/source",
        json={"source": "a = 3;\n"},
        headers={"X-ScadBuddy-Agent-Session": "headless-1"},
    )
    assert response.status_code == 200, response.text
    latest = client.get(f"/api/v1/models/{SLUG}/versions").json()[0]
    assert latest["agent"] == {"principal": None, "session": "headless-1"}


@pytest.mark.parametrize(
    "headers",
    [{AUTHOR_HEADER: "has space"}, {AUTHOR_SESSION_HEADER: "a\tb"}, {AUTHOR_HEADER: "x" * 301}],
)
def test_a_malformed_author_is_a_400(client: TestClient, headers: dict[str, str]) -> None:
    upload(client)
    response = client.put(
        f"/api/v1/models/{SLUG}/source", json={"source": "a = 4;\n"}, headers=headers
    )
    assert response.status_code == 400
    assert source(client) == FIRST


@pytest.mark.parametrize("header", [AUTHOR_HEADER, AUTHOR_SESSION_HEADER])
def test_a_trailing_newline_is_not_a_valid_author(header: str) -> None:
    # `$` matches before a final newline; the patterns end at \Z (review of #741).
    with pytest.raises(InvalidAuthorError):
        author_from(Headers({header: "token-abc\n"}))
