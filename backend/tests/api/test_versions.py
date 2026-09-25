"""The history, diff and restore routes, against a real git repository."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from scadbuddy.library.history import GitTimeoutError, ModelHistory
from tests.api.conftest import wait_for_job

pytestmark = pytest.mark.requires_git

SLUG = "keychain"
FIRST = 'width = 10;\nlabel = "hi";\n'
SECOND = 'width = 20;\nlabel = "hi";\n'
THIRD = 'width = 30;\nlabel = "bye";\n'


def upload(client: TestClient, source: str = FIRST) -> dict[str, Any]:
    response = client.post(
        "/api/v1/models",
        files={"file": (f"{SLUG}.scad", source.encode(), "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def put_source(client: TestClient, source: str, message: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"source": source}
    if message is not None:
        body["message"] = message
    response: httpx.Response = client.put(f"/api/v1/models/{SLUG}/source", json=body)
    return response


def versions(client: TestClient) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/models/{SLUG}/versions")
    assert response.status_code == 200, response.text
    listed: list[dict[str, Any]] = response.json()
    return listed


def test_an_upload_is_the_models_first_revision(client: TestClient) -> None:
    record = upload(client)

    listed = versions(client)
    assert [entry["message"] for entry in listed] == [f"Add {SLUG}"]
    assert listed[0]["commit"] == record["version"]
    assert listed[0]["short"] == record["version"][:7]
    assert listed[0]["current"] is True
    assert listed[0]["author"] == "ScadBuddy"
    # Paths are relative to the model's own directory, not the repository root.
    assert sorted(change["path"] for change in listed[0]["files"]) == [
        "model.json",
        "model.scad",
    ]


def test_upload_then_two_edits_gives_three_revisions(client: TestClient) -> None:
    """#90's acceptance: upload a .scad, edit it twice, see three revisions."""
    upload(client)
    assert put_source(client, SECOND).status_code == 200
    assert put_source(client, THIRD, "Rename the tag").status_code == 200

    listed = versions(client)
    assert [entry["message"] for entry in listed] == [
        "Rename the tag",
        f"Edit {SLUG} source",
        f"Add {SLUG}",
    ]
    assert [entry["current"] for entry in listed] == [True, False, False]


def test_a_metadata_change_is_its_own_revision(client: TestClient) -> None:
    upload(client)
    assert client.patch(f"/api/v1/models/{SLUG}", json={"description": "nicer"}).status_code == 200

    assert [entry["message"] for entry in versions(client)] == [
        f"Update {SLUG} metadata",
        f"Add {SLUG}",
    ]


def test_source_of_an_old_revision_is_still_readable(client: TestClient) -> None:
    first = upload(client)["version"]
    put_source(client, SECOND)

    response = client.get(f"/api/v1/models/{SLUG}/versions/{first}/source")

    assert response.status_code == 200
    assert response.text == FIRST
    assert client.get(f"/api/v1/models/{SLUG}/source").text == SECOND


def test_an_abbreviated_commit_id_resolves(client: TestClient) -> None:
    first = upload(client)["version"]

    response = client.get(f"/api/v1/models/{SLUG}/versions/{first[:8]}/source")

    assert response.status_code == 200
    assert response.text == FIRST


def test_an_unknown_revision_is_a_404(client: TestClient) -> None:
    upload(client)

    response = client.get(f"/api/v1/models/{SLUG}/versions/{'0' * 40}/source")

    assert response.status_code == 404
    assert "no revision" in response.json()["detail"]


def test_a_commit_is_a_snapshot_of_the_whole_repository(client: TestClient) -> None:
    """A commit that touched another model still names a state of this one.

    That is git, not a leak: every commit is a tree of the whole repository, so
    asking for a model's source at any revision answers what it was then. The
    model's own version LIST is scoped to the commits that touched it, which is
    what the UI offers -- these two are deliberately different questions.
    """
    upload(client)
    other = client.post(
        "/api/v1/models",
        files={"file": ("plate.scad", FIRST.encode(), "application/octet-stream")},
    )
    assert other.status_code == 201

    response = client.get(f"/api/v1/models/{SLUG}/versions/{other.json()['version']}/source")

    assert response.status_code == 200
    assert response.text == FIRST
    assert [entry["message"] for entry in versions(client)] == [f"Add {SLUG}"]


def test_a_revision_that_predates_the_model_is_a_404(client: TestClient) -> None:
    other = client.post(
        "/api/v1/models",
        files={"file": ("plate.scad", FIRST.encode(), "application/octet-stream")},
    )
    assert other.status_code == 201
    upload(client)

    response = client.get(f"/api/v1/models/{SLUG}/versions/{other.json()['version']}/source")

    assert response.status_code == 404


def test_a_revisions_schema_comes_from_that_revision(client: TestClient) -> None:
    first = upload(client)["version"]
    put_source(client, SECOND)

    response = client.get(f"/api/v1/models/{SLUG}/versions/{first}/schema")

    assert response.status_code == 200
    assert response.json()["title"] == "Fake"


def test_diff_defaults_to_the_previous_revision(client: TestClient) -> None:
    upload(client)
    second = put_source(client, SECOND).json()["version"]

    response = client.get(f"/api/v1/models/{SLUG}/versions/{second}/diff")

    assert response.status_code == 200
    body = response.json()
    assert body["head"] == second
    assert "-width = 10;" in body["patch"]
    assert "+width = 20;" in body["patch"]
    assert [change["path"] for change in body["files"]] == ["model.scad"]


def test_diff_between_two_named_revisions(client: TestClient) -> None:
    first = upload(client)["version"]
    put_source(client, SECOND)
    third = put_source(client, THIRD).json()["version"]

    response = client.get(f"/api/v1/models/{SLUG}/versions/{third}/diff?base={first}")

    assert response.status_code == 200
    body = response.json()
    assert (body["base"], body["head"]) == (first, third)
    assert "-width = 10;" in body["patch"]
    assert "+width = 30;" in body["patch"]


def test_diff_of_the_first_revision_shows_it_being_added(client: TestClient) -> None:
    first = upload(client)["version"]

    body = client.get(f"/api/v1/models/{SLUG}/versions/{first}/diff").json()

    assert "+width = 10;" in body["patch"]
    assert {change["status"] for change in body["files"]} == {"A"}


def test_restore_is_a_new_revision_and_never_a_rewrite(client: TestClient) -> None:
    first = upload(client)["version"]
    put_source(client, SECOND)

    response = client.post(f"/api/v1/models/{SLUG}/versions/{first}/restore")

    assert response.status_code == 200
    restored = response.json()
    assert restored["message"] == f"Restore {SLUG} to {first[:7]}"
    assert restored["current"] is True
    assert client.get(f"/api/v1/models/{SLUG}/source").text == FIRST
    # Four entries: add, edit, restore -- and the edit is still there.
    assert [entry["message"] for entry in versions(client)] == [
        f"Restore {SLUG} to {first[:7]}",
        f"Edit {SLUG} source",
        f"Add {SLUG}",
    ]
    assert client.get(f"/api/v1/models/{SLUG}").json()["version"] == restored["commit"]


def test_restoring_an_unknown_revision_is_a_404(client: TestClient) -> None:
    upload(client)

    response = client.post(f"/api/v1/models/{SLUG}/versions/{'0' * 40}/restore")

    assert response.status_code == 404


def test_source_that_does_not_parse_is_rejected_and_records_nothing(
    client: TestClient,
) -> None:
    upload(client)
    before = versions(client)

    response = put_source(client, "%%FAIL%%\n")

    assert response.status_code == 422
    assert versions(client) == before
    assert client.get(f"/api/v1/models/{SLUG}/source").text == FIRST


def test_rendering_does_not_dirty_the_repository(client: TestClient) -> None:
    """A render derives the schema, and that write must not land in `models/`.

    It happens outside any commit, so if it did the repository would sit
    permanently dirty and the next metadata commit would carry a cache blob.
    """
    upload(client)
    job = client.post(f"/api/v1/models/{SLUG}/render", json={"params": {}}).json()
    assert wait_for_job(client, job["job_id"])["status"] == "done"

    before = versions(client)
    assert client.patch(f"/api/v1/models/{SLUG}", json={"description": "nicer"}).status_code == 200

    added = versions(client)[0]
    assert added["message"] == f"Update {SLUG} metadata"
    assert [change["path"] for change in added["files"]] == ["model.json"]
    assert len(added["files"]) == 1
    assert len(versions(client)) == len(before) + 1


def test_a_message_with_control_bytes_still_lists(client: TestClient) -> None:
    upload(client)

    assert put_source(client, SECOND, "one\x1etwo\x1fthree").status_code == 200

    listed = versions(client)
    assert [entry["message"] for entry in listed] == ["one two three", f"Add {SLUG}"]


def test_an_over_long_message_is_rejected(client: TestClient) -> None:
    upload(client)

    response = put_source(client, SECOND, "x" * 5000)

    assert response.status_code == 422
    assert [entry["message"] for entry in versions(client)] == [f"Add {SLUG}"]


def test_versions_of_a_missing_model_are_a_404(client: TestClient) -> None:
    assert client.get("/api/v1/models/nope/versions").status_code == 404


def test_an_output_records_the_revision_it_was_rendered_from(client: TestClient) -> None:
    first = upload(client)["version"]
    second = put_source(client, SECOND).json()["version"]

    job = client.post(f"/api/v1/models/{SLUG}/render", json={"params": {}}).json()
    finished = wait_for_job(client, job["job_id"])
    assert finished["status"] == "done"
    assert finished["model_version"] == second

    output = client.post(
        f"/api/v1/models/{SLUG}/outputs", json={"job_id": job["job_id"], "name": "now"}
    )
    assert output.status_code == 201
    assert output.json()["model_version"] == second
    assert output.json()["model_version"] != first


def test_rendering_an_old_revision_does_not_restore_it(client: TestClient) -> None:
    """ "Customize this version": the render reads the old source, the model stays put."""
    first = upload(client)["version"]
    second = put_source(client, SECOND).json()["version"]

    job = client.post(f"/api/v1/models/{SLUG}/render", json={"params": {}, "version": first}).json()
    finished = wait_for_job(client, job["job_id"])

    assert finished["status"] == "done"
    assert finished["model_version"] == first
    assert client.get(f"/api/v1/models/{SLUG}/source").text == SECOND
    assert client.get(f"/api/v1/models/{SLUG}").json()["version"] == second
    assert [entry["message"] for entry in versions(client)] == [
        f"Edit {SLUG} source",
        f"Add {SLUG}",
    ]


def test_rendering_an_unknown_revision_is_a_404(client: TestClient) -> None:
    upload(client)

    response = client.post(
        f"/api/v1/models/{SLUG}/render", json={"params": {}, "version": "0" * 40}
    )

    assert response.status_code == 404


def test_deleting_a_model_records_the_deletion(client: TestClient) -> None:
    upload(client)

    assert client.delete(f"/api/v1/models/{SLUG}").status_code == 204

    # The model is gone, so its history is only reachable through the repository --
    # which is the point of not inventing a store: the commit is still there.
    assert client.get(f"/api/v1/models/{SLUG}/versions").status_code == 404


def _stalled(*_: object, **__: object) -> None:
    raise GitTimeoutError("git timed out after 30s")


@pytest.mark.parametrize(
    ("method", "call"),
    [
        (
            "show",
            lambda client, first: client.get(f"/api/v1/models/{SLUG}/versions/{first}/source"),
        ),
        (
            "export",
            lambda client, first: client.get(f"/api/v1/models/{SLUG}/versions/{first}/schema"),
        ),
        (
            "export",
            lambda client, first: client.post(
                f"/api/v1/models/{SLUG}/render", json={"params": {}, "version": first}
            ),
        ),
        (
            "resolve",
            lambda client, first: client.post(
                f"/api/v1/models/{SLUG}/render", json={"params": {}, "version": first}
            ),
        ),
    ],
    ids=["source", "schema", "render-export", "render-resolve"],
)
def test_a_git_failure_is_a_clean_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, call: Any
) -> None:
    """Every git call is bounded (#132): a stalled one must answer like its siblings do,
    not through the catch-all handler."""
    first = upload(client)["version"]
    put_source(client, SECOND)
    monkeypatch.setattr(ModelHistory, method, _stalled)

    response = call(client, first)

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == "git timed out after 30s"
