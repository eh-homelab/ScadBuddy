"""Template UI modules (spec 2026-09-27 §4.1, §9)."""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths

UI = {"module": "ui/index.js", "slot": "panel", "api": 1}


def _with_ui(paths: DataPaths, slug: str, files: dict[str, bytes], ui: object = UI) -> None:
    directory = paths.model_dir(slug)
    for name, body in files.items():
        target = directory / "ui" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    meta = json.loads(paths.model_meta(slug).read_text(encoding="utf-8"))
    meta["ui"] = ui
    paths.model_meta(slug).write_text(json.dumps(meta), encoding="utf-8")


def test_the_record_carries_the_declaration(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    _with_ui(paths, model, {"index.js": b"export function mount() {}\n"})
    record = client.get(f"/api/v1/models/{model}").json()
    assert record["ui"] == UI
    assert record["ui_error"] is None


def test_a_module_is_served_as_javascript(client: TestClient, model: str, paths: DataPaths) -> None:
    _with_ui(
        paths, model, {"index.js": b"export function mount() {}\n", "lib/a.js": b"export {}\n"}
    )
    response = client.get(f"/api/v1/models/{model}/ui/index.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    assert response.headers["cache-control"] == "no-cache"
    assert response.text == "export function mount() {}\n"
    assert client.get(f"/api/v1/models/{model}/ui/lib/a.js").status_code == 200
    again = client.get(
        f"/api/v1/models/{model}/ui/index.js", headers={"If-None-Match": response.headers["etag"]}
    )
    assert again.status_code == 304


# httpx normalises `../model.scad` before sending (it arrives as `/models/demo/model.scad`, a
# 404 of its own); the encoded forms and the symlink are what reach `_ui_file`.
@pytest.mark.parametrize(
    "path",
    [
        "../model.scad",
        "%2e%2e/model.scad",
        "..%2fmodel.scad",
        "page.html",
        "missing.js",
        ".hidden.js",
        # A NUL byte makes `Path.resolve` raise, and a segment past NAME_MAX makes the
        # stat raise: each the same 404, not a 500.
        "%00.js",
        "a%00.js",
        "x" * 290 + ".js",
    ],
)
def test_ui_paths_never_leave_ui(
    client: TestClient, model: str, paths: DataPaths, path: str
) -> None:
    _with_ui(
        paths,
        model,
        {"index.js": b"export function mount() {}\n", "page.html": b"<script>alert(1)</script>"},
    )
    os.symlink(paths.model_source(model), paths.model_dir(model) / "ui" / "link.js")
    response = client.get(f"/api/v1/models/{model}/ui/{path}")
    assert response.status_code == 404
    assert b"width = 10" not in response.content
    assert client.get(f"/api/v1/models/{model}/ui/link.js").status_code == 404


def test_a_model_without_ui_has_no_module(client: TestClient, model: str) -> None:
    assert client.get(f"/api/v1/models/{model}").json()["ui"] is None
    assert client.get(f"/api/v1/models/{model}/ui/index.js").status_code == 404


def test_a_malformed_ui_costs_only_the_ui(client: TestClient, model: str, paths: DataPaths) -> None:
    _with_ui(paths, model, {}, ui={"module": "../model.scad", "slot": "sidebar", "api": "one"})
    record = client.get(f"/api/v1/models/{model}").json()
    assert record["ui"] is None
    assert "ui" in record["ui_error"] and "slot" in record["ui_error"]
    assert any(m["slug"] == model for m in client.get("/api/v1/models").json())
    assert client.get(f"/api/v1/models/{model}/schema").status_code == 200


def test_a_patch_keeps_ui(client: TestClient, model: str, paths: DataPaths) -> None:
    _with_ui(paths, model, {"index.js": b"export function mount() {}\n"})
    assert client.patch(f"/api/v1/models/{model}", json={"name": "Renamed"}).status_code == 200
    assert json.loads(paths.model_meta(model).read_text(encoding="utf-8"))["ui"] == UI
    assert "ui_error" not in json.loads(paths.model_meta(model).read_text(encoding="utf-8"))


def test_an_uploaded_unreadable_ui_is_kept_and_reported(
    client: TestClient, paths: DataPaths
) -> None:
    declared = {"module": "x.txt"}
    response = client.post(
        "/api/v1/models",
        files={
            "file": ("uploaded.scad", b"cube(1);\n", "application/octet-stream"),
            "meta": ("model.json", json.dumps({"ui": declared}).encode(), "application/json"),
        },
    )
    assert response.status_code == 201, response.text
    slug = response.json()["slug"]
    record = client.get(f"/api/v1/models/{slug}").json()
    assert record["ui"] is None
    assert record["ui_error"].startswith("model.json's ui is not valid")
    written = json.loads(paths.model_meta(slug).read_text(encoding="utf-8"))
    assert written["ui"] == declared
    assert "ui_error" not in written


def _commit(paths: DataPaths, message: str) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(paths.models), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("add", "-A")
    git("commit", "-qm", message)
    return git("rev-parse", "HEAD")


@pytest.mark.requires_git
def test_a_pinned_revision_serves_its_own_module_graph(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    _with_ui(paths, model, {"index.js": b"import './a.js'\n", "a.js": b"// one\n"})
    first = _commit(paths, "ui one")
    _with_ui(paths, model, {"a.js": b"// two\n"})
    head = _commit(paths, "ui two")
    pinned = client.get(f"/api/v1/models/{model}/versions/{first}/ui/a.js")
    assert pinned.status_code == 200
    assert pinned.text == "// one\n"
    assert pinned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get(f"/api/v1/models/{model}/ui/a.js").text == "// two\n"
    # The current revision is answered from the live directory, which an uncommitted
    # edit can change: never cached as immutable under the commit's URL.
    current = client.get(f"/api/v1/models/{model}/versions/{head}/ui/a.js")
    assert current.status_code == 200
    assert current.text == "// two\n"
    assert current.headers["cache-control"] == "no-cache"


@pytest.mark.requires_git
def test_a_missing_library_checkout_does_not_stop_the_ui(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """The UI's files need no library: a pin whose checkout is missing, and cannot be
    fetched (no route to the git host), must not stop the pinned route serving them."""
    _with_ui(paths, model, {"index.js": b"// one\n"})
    meta = json.loads(paths.model_meta(model).read_text(encoding="utf-8"))
    meta["libraries"] = [
        {"name": "Gone", "url": "https://git.invalid/gone.git", "ref": "main", "commit": "0" * 40}
    ]
    paths.model_meta(model).write_text(json.dumps(meta), encoding="utf-8")
    first = _commit(paths, "ui one")
    _with_ui(paths, model, {"index.js": b"// two\n"})
    head = _commit(paths, "ui two")
    for commit, body in ((first, "// one\n"), (head, "// two\n")):
        response = client.get(f"/api/v1/models/{model}/versions/{commit}/ui/index.js")
        assert response.status_code == 200, response.text
        assert response.text == body
    assert not (paths.libraries / "Gone").exists()


def test_the_page_carries_the_csp() -> None:
    from pathlib import Path

    from scadbuddy.api.static import PAGE_CSP

    assert "script-src 'self';" in PAGE_CSP
    assert "connect-src 'self';" in PAGE_CSP
    assert "https://fonts.googleapis.com" in PAGE_CSP  # the font picker's previews
    assert "https://fonts.gstatic.com" in PAGE_CSP
    shared = Path(__file__).parents[3] / "frontend" / "page-csp.txt"
    if shared.is_file():  # absent in the image's test stage, which has no frontend tree
        assert shared.read_text(encoding="utf-8").strip() == PAGE_CSP


def test_the_spa_sends_the_csp_on_the_document_and_client_routes(tmp_path: object) -> None:
    from pathlib import Path

    from starlette.applications import Starlette
    from starlette.routing import Mount

    from scadbuddy.api.static import PAGE_CSP, SPAStaticFiles

    bundle = Path(str(tmp_path))
    (bundle / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (bundle / "assets").mkdir()
    (bundle / "assets" / "x.js").write_text("export {}\n", encoding="utf-8")
    app = Starlette(routes=[Mount("/", SPAStaticFiles(bundle))])
    with TestClient(app) as spa:
        for url in ("/", "/m/demo", "/assets/x.js"):
            response = spa.get(url)
            assert response.status_code == 200
            assert response.headers["content-security-policy"] == PAGE_CSP
        etag = spa.get("/").headers["etag"]
        unchanged = spa.get("/", headers={"If-None-Match": etag})
        assert unchanged.status_code == 304
        assert unchanged.headers["content-security-policy"] == PAGE_CSP


@pytest.mark.requires_git
def test_a_revisions_schema_carries_that_revisions_ui(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """ "Customize this version" mounts the revision's own interface, or none."""
    before = _commit(paths, "no ui yet")
    _with_ui(paths, model, {"index.js": b"export function mount() {}\n"})
    head = _commit(paths, "ui")
    _with_ui(paths, model, {}, ui={"module": "ui/x.txt", "api": 1})
    broken = _commit(paths, "bad ui")
    old = client.get(f"/api/v1/models/{model}/versions/{before}/schema")
    assert old.status_code == 200, old.text
    assert old.json()["ui"] is None and old.json()["ui_error"] is None
    assert old.json()["parameters"]
    assert client.get(f"/api/v1/models/{model}/versions/{head}/schema").json()["ui"] == UI
    bad = client.get(f"/api/v1/models/{model}/versions/{broken}/schema").json()
    assert bad["ui"] is None and bad["ui_error"].startswith("model.json's ui is not valid")
