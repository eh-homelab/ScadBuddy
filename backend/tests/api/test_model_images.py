"""Images beside a model, for its README's relative `![](thumbnail.png)` (#951)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api import lsp
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.paths import DataPaths, model_path

PNG = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + b"\1" * 32
OTHER_PNG = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + b"\2" * 32


def test_an_image_beside_the_model_is_served_with_its_type(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    (paths.model_dir(model) / "images").mkdir()
    (paths.model_dir(model) / "images" / "badge.png").write_bytes(PNG)

    response = client.get(f"/api/v1/models/{model}/images/images/badge.png")

    assert response.status_code == 200, response.text
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    # The working tree can change under the same URL: revalidated, never kept.
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"].startswith('"')


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("a.png", "image/png"),
        ("a.PNG", "image/png"),
        ("a.jpg", "image/jpeg"),
        ("a.jpeg", "image/jpeg"),
        ("a.gif", "image/gif"),
        ("a.webp", "image/webp"),
        ("a.svg", "image/svg+xml"),
    ],
)
def test_each_image_extension_has_its_content_type(
    client: TestClient, model: str, paths: DataPaths, name: str, content_type: str
) -> None:
    (paths.model_dir(model) / name).write_bytes(PNG)
    response = client.get(f"/api/v1/models/{model}/images/{name}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == content_type


@pytest.mark.parametrize("name", ["model.scad", "model.json", "README.md", "noext", "a.png.txt"])
def test_a_file_that_is_not_an_image_is_a_415(
    client: TestClient, model: str, paths: DataPaths, name: str
) -> None:
    target = paths.model_dir(model) / name
    if not target.exists():
        target.write_bytes(PNG)
    assert client.get(f"/api/v1/models/{model}/images/{name}").status_code == 415


@pytest.mark.parametrize(
    "path",
    [
        "%2E%2E/%2E%2E/secret.png",
        "images/%2E%2E/%2E%2E/secret.png",
        "%2E/thumbnail.png",
        ".renders/key/plate.png",
        "images/.hidden.png",
        "%2Fetc%2Fsecret.png",
        "images//badge.png",
        "images%5Cbadge.png",
    ],
)
def test_a_path_that_is_not_plain_is_refused(
    client: TestClient, model: str, paths: DataPaths, path: str
) -> None:
    (paths.root / "secret.png").write_bytes(b"TOP SECRET")
    response = client.get(f"/api/v1/models/{model}/images/{path}")
    assert response.status_code == 422, (path, response.text)
    assert b"TOP SECRET" not in response.content


def test_a_symlink_out_of_the_directory_a_missing_file_or_model_is_a_404(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    outside = paths.root / "secret.png"
    outside.write_bytes(b"TOP SECRET")
    (paths.model_dir(model) / "link.png").symlink_to(outside)
    (paths.model_dir(model) / "dir.png").mkdir()

    for path in ("link.png", "nope.png", "dir.png"):
        response = client.get(f"/api/v1/models/{model}/images/{path}")
        assert response.status_code == 404, (path, response.text)
        assert b"TOP SECRET" not in response.content
    assert client.get("/api/v1/models/nobody/images/thumbnail.png").status_code == 404


def test_an_image_over_the_cap_is_a_413(
    client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lsp, "MAX_MODEL_IMAGE_BYTES", len(PNG))
    (paths.model_dir(model) / "at-cap.png").write_bytes(PNG)
    (paths.model_dir(model) / "big.png").write_bytes(PNG + b"x")

    assert client.get(f"/api/v1/models/{model}/images/at-cap.png").status_code == 200
    assert client.get(f"/api/v1/models/{model}/images/big.png").status_code == 413


def test_a_matching_if_none_match_is_a_304(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    (paths.model_dir(model) / "thumbnail.png").write_bytes(PNG)
    first = client.get(f"/api/v1/models/{model}/images/thumbnail.png")

    again = client.get(
        f"/api/v1/models/{model}/images/thumbnail.png",
        headers={"If-None-Match": first.headers["etag"]},
    )

    assert again.status_code == 304
    assert again.content == b""


SLUG = "keychain"


def _upload(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/v1/models",
        files={"file": (f"{SLUG}.scad", b"cube(1);\n", "application/octet-stream")},
        data={"force": "true"},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _commit_image(client: TestClient, paths: DataPaths, name: str, data: bytes) -> str:
    (paths.model_dir(SLUG) / name).write_bytes(data)
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    commit = state.history.commit(f"Set {name}", f"{model_path(SLUG)}/{name}")
    assert commit is not None
    return commit


@pytest.mark.requires_git
def test_a_revision_serves_the_image_as_it_was_then_and_is_immutable(
    client: TestClient, paths: DataPaths
) -> None:
    _upload(client)
    first = _commit_image(client, paths, "thumbnail.png", PNG)
    _commit_image(client, paths, "thumbnail.png", OTHER_PNG)

    old = client.get(f"/api/v1/models/{SLUG}/images/thumbnail.png", params={"commit": first})
    short = client.get(f"/api/v1/models/{SLUG}/images/thumbnail.png", params={"commit": first[:7]})
    now = client.get(f"/api/v1/models/{SLUG}/images/thumbnail.png")

    assert old.status_code == 200, old.text
    assert old.content == PNG
    assert old.headers["content-type"] == "image/png"
    assert "immutable" in old.headers["cache-control"]
    assert short.content == PNG
    assert now.content == OTHER_PNG


@pytest.mark.requires_git
def test_a_revision_without_the_file_or_an_unknown_one_is_a_404(
    client: TestClient, paths: DataPaths
) -> None:
    created = _upload(client)["version"]
    _commit_image(client, paths, "thumbnail.png", PNG)
    url = f"/api/v1/models/{SLUG}/images/thumbnail.png"

    assert client.get(url, params={"commit": created}).status_code == 404
    assert client.get(url, params={"commit": "0" * 40}).status_code == 404
    assert client.get(url, params={"commit": "not-a-commit"}).status_code == 422
    # The path rules hold for a revision too, and so does the extension.
    assert (
        client.get(
            f"/api/v1/models/{SLUG}/images/%2E%2E/x.png", params={"commit": created}
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/v1/models/{SLUG}/images/model.scad", params={"commit": created}
        ).status_code
        == 415
    )


@pytest.mark.requires_git
def test_a_revision_image_over_the_cap_is_a_413(
    client: TestClient, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    _upload(client)
    commit = _commit_image(client, paths, "big.png", PNG + b"x")
    monkeypatch.setattr(lsp, "MAX_MODEL_IMAGE_BYTES", len(PNG))

    response = client.get(f"/api/v1/models/{SLUG}/images/big.png", params={"commit": commit})

    assert response.status_code == 413


@pytest.mark.requires_git
def test_a_directory_named_like_an_image_at_a_revision_is_a_404(
    client: TestClient, paths: DataPaths
) -> None:
    _upload(client)
    (paths.model_dir(SLUG) / "dir.png").mkdir()
    (paths.model_dir(SLUG) / "dir.png" / "inner.png").write_bytes(PNG)
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    commit = state.history.commit("Add a directory", f"{model_path(SLUG)}/dir.png")
    assert commit is not None

    response = client.get(f"/api/v1/models/{SLUG}/images/dir.png", params={"commit": commit})

    assert response.status_code == 404


@pytest.mark.requires_git
def test_a_committed_symlink_at_a_revision_is_a_404_not_its_target_path(
    client: TestClient, paths: DataPaths
) -> None:
    _upload(client)
    (paths.model_dir(SLUG) / "link.png").symlink_to("/etc/passwd")
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    commit = state.history.commit("Add a symlink", f"{model_path(SLUG)}/link.png")
    assert commit is not None

    response = client.get(f"/api/v1/models/{SLUG}/images/link.png", params={"commit": commit})

    assert response.status_code == 404
    assert b"/etc/passwd" not in response.content


def test_a_symlink_inside_the_directory_is_followed(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    (paths.model_dir(model) / "images").mkdir()
    (paths.model_dir(model) / "images" / "real.png").write_bytes(PNG)
    (paths.model_dir(model) / "cover.png").symlink_to("images/real.png")

    response = client.get(f"/api/v1/models/{model}/images/cover.png")

    assert response.status_code == 200, response.text
    assert response.content == PNG


@pytest.mark.requires_git
def test_a_symlink_inside_the_directory_is_followed_at_a_revision_too(
    client: TestClient, paths: DataPaths
) -> None:
    _upload(client)
    (paths.model_dir(SLUG) / "real.png").write_bytes(PNG)
    (paths.model_dir(SLUG) / "cover.png").symlink_to("real.png")
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    commit = state.history.commit("Add a cover link", model_path(SLUG))
    assert commit is not None

    live = client.get(f"/api/v1/models/{SLUG}/images/cover.png")
    pinned = client.get(f"/api/v1/models/{SLUG}/images/cover.png", params={"commit": commit})

    assert live.content == PNG
    assert pinned.status_code == 200, pinned.text
    assert pinned.content == PNG
