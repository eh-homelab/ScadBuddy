"""The library routes (#93): the catalogue, pinning a library to one model, and
that model's pins reaching its renders.

The upstream is a local bare repository, so the clone is real and offline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import (
    INSTALL_CONCURRENCY,
    STATE_ATTR,
    AppState,
    CheckoutGate,
    get_libraries,
)
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.history import GIT, ModelHistory, git_env
from scadbuddy.library.libraries import (
    LOCKFILE_NAME,
    STAGING_PREFIX,
    CatalogueLibrary,
    LibraryStore,
)
from tests.conftest import make_library_upstream

pytestmark = pytest.mark.requires_git

SLUG = "widget"
SOURCE = 'use <BOSL2/std.scad>\nwidth = 10;\nlabel = "hi";\n'


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[str, dict[str, str]]:
    return make_library_upstream(
        tmp_path, {"v1": "module marker() cube(1);\n", "v2": "module marker() cube(2);\n"}
    )


@pytest.fixture
def libraries_app(app: FastAPI, upstream: tuple[str, dict[str, str]]) -> FastAPI:
    """The real store, with a catalogue whose one entry is the local upstream."""
    url, _ = upstream
    state: AppState = getattr(app.state, STATE_ATTR)
    store = LibraryStore(
        state.paths,
        catalogue=(
            CatalogueLibrary(
                name="BOSL2",
                url=url,
                ref="v1",
                licence="BSD-2-Clause",
                homepage="https://example.invalid/bosl2",
            ),
        ),
        protocols=("file",),
    )
    app.dependency_overrides[get_libraries] = lambda: store
    return app


@pytest.fixture
def lib_client(libraries_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(libraries_app) as test_client:
        yield test_client


def create_model(client: TestClient, name: str = SLUG) -> dict[str, Any]:
    response = client.post("/api/v1/models", json={"name": name, "source": SOURCE})
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def pin(client: TestClient, name: str, slug: str = SLUG, **body: Any) -> dict[str, Any]:
    response = client.put(f"/api/v1/models/{slug}/libraries/{name}", json=body)
    assert response.status_code == 200, response.text
    record: dict[str, Any] = response.json()
    return record


def test_the_catalogue_lists_what_can_be_pinned(lib_client: TestClient) -> None:
    listed = lib_client.get("/api/v1/libraries").json()

    assert listed == [
        {
            "name": "BOSL2",
            "url": listed[0]["url"],
            "ref": "v1",
            "licence": "BSD-2-Clause",
            "homepage": "https://example.invalid/bosl2",
        }
    ]


def test_pinning_a_library_records_it_in_that_model_alone(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream
    create_model(lib_client)
    create_model(lib_client, "gadget")

    record = pin(lib_client, "BOSL2")

    pinned = {"name": "BOSL2", "url": url, "ref": "v1", "commit": commits["v1"]}
    assert record["libraries"] == [pinned]
    assert lib_client.get(f"/api/v1/models/{SLUG}").json()["libraries"] == [pinned]
    assert lib_client.get("/api/v1/models/gadget").json()["libraries"] == []
    assert not (paths.models / LOCKFILE_NAME).exists()


def test_two_models_render_one_library_at_two_refs(
    lib_client: TestClient,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, commits = upstream
    create_model(lib_client)
    create_model(lib_client, "gadget")
    pin(lib_client, "BOSL2")
    pin(lib_client, "BOSL2", "gadget", ref="v2")
    log = tmp_path / "openscadpath.log"
    monkeypatch.setenv("FAKE_OPENSCAD_PATH_LOG", str(log))

    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200
    assert lib_client.get("/api/v1/models/gadget/schema").status_code == 200

    assert log.read_text(encoding="utf-8").splitlines() == [
        str(paths.libraries / "BOSL2" / commits["v1"]),
        str(paths.libraries / "BOSL2" / commits["v2"]),
    ]


def test_a_library_is_pinned_by_url_under_any_name(
    lib_client: TestClient, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream
    create_model(lib_client)

    record = pin(lib_client, "mylib", url=url, ref="v2")

    assert [(lib["name"], lib["commit"]) for lib in record["libraries"]] == [
        ("mylib", commits["v2"])
    ]


def test_a_curated_name_can_be_pinned_from_a_fork(lib_client: TestClient, tmp_path: Path) -> None:
    """One model's fork swaps nothing out from under any other."""
    elsewhere, commits = make_library_upstream(tmp_path / "elsewhere", {"v1": "sphere(1);\n"})
    create_model(lib_client)

    record = pin(lib_client, "BOSL2", url=elsewhere, ref="v1")

    assert record["libraries"][0]["url"] == elsewhere
    assert record["libraries"][0]["commit"] == commits["v1"]


def test_removing_a_library_takes_it_off_the_model(lib_client: TestClient) -> None:
    create_model(lib_client)
    pin(lib_client, "BOSL2")

    removed = lib_client.delete(f"/api/v1/models/{SLUG}/libraries/BOSL2")
    again = lib_client.delete(f"/api/v1/models/{SLUG}/libraries/BOSL2")

    assert removed.status_code == 200, removed.text
    assert removed.json()["libraries"] == []
    assert again.status_code == 404


def test_a_built_in_takes_no_pins(lib_client: TestClient, libraries_app: FastAPI) -> None:
    store = libraries_app.dependency_overrides[get_libraries]()
    with patch.object(store, "resolve", side_effect=AssertionError("reached the service")):
        response = lib_client.put("/api/v1/models/builtin:anything/libraries/BOSL2", json={})

    assert response.status_code == 403


def test_pinning_to_a_model_that_does_not_exist_is_a_404_without_a_clone(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    store = libraries_app.dependency_overrides[get_libraries]()
    with patch.object(store, "resolve", side_effect=AssertionError("reached the service")):
        response = lib_client.put(f"/api/v1/models/{SLUG}/libraries/BOSL2", json={})

    assert response.status_code == 404


def test_the_metadata_patch_takes_no_libraries(lib_client: TestClient) -> None:
    """A pin is a fetched commit; it is set by the pin route, never typed in."""
    create_model(lib_client)

    patched = lib_client.patch(f"/api/v1/models/{SLUG}", json={"libraries": ["BOSL2"]})

    assert patched.status_code == 200
    assert patched.json()["libraries"] == []


def test_installs_in_flight_are_capped(
    lib_client: TestClient, libraries_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each clone holds a worker thread for up to the git timeout; a burst of them
    must not take the executor every other route shares."""
    store = libraries_app.dependency_overrides[get_libraries]()
    lock = threading.Lock()
    running, most = 0, 0
    real_resolve = store.resolve

    def resolve(name: str, **kwargs: Any) -> Any:
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        time.sleep(0.2)
        with lock:
            running -= 1
        return real_resolve(name, **kwargs)

    monkeypatch.setattr(store, "resolve", resolve)
    slugs = [f"model-{index}" for index in range(INSTALL_CONCURRENCY + 2)]
    for slug in slugs:
        create_model(lib_client, slug)
    with ThreadPoolExecutor(len(slugs)) as pool:
        codes = list(
            pool.map(
                lambda slug: (
                    lib_client.put(f"/api/v1/models/{slug}/libraries/BOSL2", json={}).status_code
                ),
                slugs,
            )
        )

    assert codes == [200] * len(slugs)
    assert most == INSTALL_CONCURRENCY


def test_an_unknown_library_without_a_url_is_a_404(lib_client: TestClient) -> None:
    create_model(lib_client)
    response = lib_client.put(f"/api/v1/models/{SLUG}/libraries/nothing", json={})
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert "catalogue" in response.json()["detail"]


def test_a_url_on_a_transport_that_is_not_allowed_is_a_422(lib_client: TestClient) -> None:
    create_model(lib_client)
    response = lib_client.put(
        f"/api/v1/models/{SLUG}/libraries/mylib",
        json={"url": "ext::sh -c touch% /tmp/x", "ref": "v1"},
    )
    assert response.status_code == 422


def test_a_name_that_is_not_a_directory_name_is_a_422(lib_client: TestClient) -> None:
    create_model(lib_client)
    response = lib_client.put(f"/api/v1/models/{SLUG}/libraries/.hidden", json={})
    assert response.status_code == 422


def test_a_ref_with_dot_dot_is_refused_by_validation(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    create_model(lib_client)
    store = libraries_app.dependency_overrides[get_libraries]()
    with patch.object(store, "resolve", side_effect=AssertionError("reached the service")):
        response = lib_client.put(f"/api/v1/models/{SLUG}/libraries/BOSL2", json={"ref": "a..b"})

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["errors"]] == [["body", "ref"]]


def test_the_schema_states_that_a_ref_has_no_dot_dot(lib_client: TestClient) -> None:
    schema = lib_client.get("/openapi.json").json()["components"]["schemas"]
    ref = schema["LibraryPinRequest"]["properties"]["ref"]["anyOf"][0]

    assert "(?!.*\\.\\.)" in ref["pattern"]


def test_a_bad_entry_in_model_json_is_a_409_for_that_model_only(
    lib_client: TestClient, paths: DataPaths
) -> None:
    create_model(lib_client)
    create_model(lib_client, "plain")
    pin(lib_client, "BOSL2")
    meta_path = paths.model_meta(SLUG)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["libraries"][0]["commit"] = "HEAD"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    assert lib_client.get("/api/v1/models/plain/schema").status_code == 200
    # The listing still reads; the render is what refuses.
    assert lib_client.get(f"/api/v1/models/{SLUG}").status_code == 200
    schema = lib_client.get(f"/api/v1/models/{SLUG}/schema")
    render = lib_client.post(f"/api/v1/models/{SLUG}/render", json={"params": {}})

    for refused in (schema, render):
        assert refused.status_code == 409
        assert refused.headers["content-type"] == "application/problem+json"
        assert refused.json()["title"] == "Invalid Library Declaration"
        assert "library 'BOSL2' is not valid: commit:" in refused.json()["detail"]
    # Pinning it again is the fix.
    pin(lib_client, "BOSL2")
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200


def test_boot_sweeps_staging_clones_a_killed_install_left(app: FastAPI, paths: DataPaths) -> None:
    staging = paths.libraries / f"{STAGING_PREFIX}0123abcd" / "BOSL2"
    staging.mkdir(parents=True)
    (staging / "std.scad").write_text("cube(1);\n", encoding="utf-8")
    checkout = paths.libraries / "BOSL2" / ("a" * 40) / "BOSL2"
    checkout.mkdir(parents=True)

    with TestClient(app):
        pass

    assert [entry.name for entry in paths.libraries.iterdir()] == ["BOSL2"]
    assert checkout.is_dir()


def test_a_failed_staging_sweep_does_not_stop_the_boot(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch.object(LibraryStore, "sweep_staging", side_effect=OSError("EIO")),
        TestClient(app) as client,
    ):
        assert client.get("/healthz").status_code == 200
    assert "could not sweep library staging clones" in caplog.text


def test_a_ref_that_does_not_exist_upstream_is_a_502(
    lib_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    create_model(lib_client)
    with caplog.at_level(logging.WARNING, logger="scadbuddy.library.libraries"):
        response = lib_client.put(f"/api/v1/models/{SLUG}/libraries/BOSL2", json={"ref": "v9"})

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "v9" in detail
    # git's own words go to the log, never to the client.
    (logged,) = [record for record in caplog.records if record.message == "git failed"]
    stderr: str = logged.git_stderr  # type: ignore[attr-defined]
    assert "upstream origin" in stderr
    assert stderr not in detail
    assert "fatal" not in detail
    assert lib_client.get(f"/api/v1/models/{SLUG}").json()["libraries"] == []


def test_a_clone_over_the_size_cap_is_a_422(libraries_app: FastAPI, lib_client: TestClient) -> None:
    store: LibraryStore = libraries_app.dependency_overrides[get_libraries]()
    store.max_bytes = 1
    create_model(lib_client)

    response = lib_client.put(f"/api/v1/models/{SLUG}/libraries/BOSL2", json={})

    assert response.status_code == 422
    assert ", over the 1 bytes" in response.json()["detail"]
    assert lib_client.get(f"/api/v1/models/{SLUG}").json()["libraries"] == []


def test_a_url_on_the_cluster_network_is_a_422_without_a_clone(
    lib_client: TestClient,
    libraries_app: FastAPI,
    fake_dns: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_model(lib_client)
    fake_dns["git.internal.example"] = ["10.0.0.7"]
    store = libraries_app.dependency_overrides[get_libraries]()
    monkeypatch.setattr(store, "protocols", ("https",))
    monkeypatch.setattr(store, "_git", lambda *args: pytest.fail(f"git ran: {args}"))

    response = lib_client.put(
        f"/api/v1/models/{SLUG}/libraries/mylib",
        json={"url": "https://git.internal.example/o/r.git", "ref": "v1"},
    )

    assert response.status_code == 422
    assert "public" in response.json()["detail"]


@respx.mock
@pytest.mark.usefixtures("fake_dns")
def test_an_imported_model_takes_pins_like_any_other(
    lib_client: TestClient,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#153's URL import goes through the same create path, so its model takes a
    pin and renders with exactly that library, keeping where it came from."""
    _, commits = upstream
    raw = "https://raw.githubusercontent.com/someone/models/main/widget.scad"
    respx.get(raw).mock(return_value=httpx.Response(200, text=SOURCE))
    imported = lib_client.post("/api/v1/models/import", json={"url": raw})
    assert imported.status_code == 201, imported.text
    log = tmp_path / "openscadpath.log"
    monkeypatch.setenv("FAKE_OPENSCAD_PATH_LOG", str(log))

    record = pin(lib_client, "BOSL2")

    assert ([lib["name"] for lib in record["libraries"]], record["origin_url"]) == (["BOSL2"], raw)
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200
    assert log.read_text(encoding="utf-8").splitlines() == [
        str(paths.libraries / "BOSL2" / commits["v1"])
    ]


def test_the_editor_check_sees_the_models_libraries(
    lib_client: TestClient,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    log = tmp_path / "openscadpath.log"
    monkeypatch.setenv("FAKE_OPENSCAD_PATH_LOG", str(log))

    checked = lib_client.post("/api/v1/models/check", json={"source": SOURCE, "slug": SLUG})
    saved = lib_client.put(f"/api/v1/models/{SLUG}/source", json={"source": SOURCE + "\n"})

    assert checked.status_code == 200, checked.text
    assert saved.status_code == 200, saved.text
    expected = str(paths.libraries / "BOSL2" / commits["v1"])
    assert log.read_text(encoding="utf-8").splitlines() == [expected, expected]


def test_a_pinned_library_whose_checkout_is_gone_is_a_409(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    checkout = paths.libraries / "BOSL2" / commits["v1"]
    checkout.rename(paths.root / "elsewhere")

    schema = lib_client.get(f"/api/v1/models/{SLUG}/schema")
    render = lib_client.post(f"/api/v1/models/{SLUG}/render", json={"params": {}})

    assert schema.status_code == 409
    assert "BOSL2" in schema.json()["detail"]
    assert render.status_code == 409


def test_the_editor_check_and_save_are_a_409_when_a_checkout_is_gone(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    """The check takes its source from the body, so it wires the library path up on
    its own rather than through `resolve_source` -- and must fail the same way."""
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    (paths.libraries / "BOSL2" / commits["v1"]).rename(paths.root / "elsewhere")

    checked = lib_client.post("/api/v1/models/check", json={"source": SOURCE, "slug": SLUG})
    saved = lib_client.put(f"/api/v1/models/{SLUG}/source", json={"source": SOURCE + "\n"})

    assert checked.status_code == 409
    assert checked.headers["content-type"] == "application/problem+json"
    assert "BOSL2" in checked.json()["detail"]
    assert saved.status_code == 409


def _schema_runs(log: Path) -> int:
    return len(log.read_text(encoding="utf-8").splitlines()) if log.is_file() else 0


def test_a_new_pin_re_derives_the_schema(
    lib_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same source, other library code: the cached schema is keyed on the pins too."""
    create_model(lib_client)
    log = tmp_path / "invocations.log"
    monkeypatch.setenv("FAKE_OPENSCAD_LOG", str(log))
    # The create's own check derived it, so this one is served from the cache.
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200
    assert _schema_runs(log) == 0

    pin(lib_client, "BOSL2")
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200
    assert _schema_runs(log) == 1

    pin(lib_client, "BOSL2", ref="v2")
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200
    assert _schema_runs(log) == 2


def test_restoring_a_revision_restores_its_pins_and_no_others(
    lib_client: TestClient, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    create_model(lib_client)
    create_model(lib_client, "gadget")
    pin(lib_client, "BOSL2", "gadget", ref="v2")
    written_against = pin(lib_client, "BOSL2")["version"]
    edited = lib_client.put(f"/api/v1/models/{SLUG}/source", json={"source": SOURCE + "cube(1);\n"})
    assert edited.status_code == 200, edited.text
    pin(lib_client, "BOSL2", ref="v2")

    restored = lib_client.post(f"/api/v1/models/{SLUG}/versions/{written_against}/restore")

    assert restored.status_code == 200, restored.text
    assert sorted(change["path"] for change in restored.json()["files"]) == [
        "model.json",
        "model.scad",
    ]
    model = lib_client.get(f"/api/v1/models/{SLUG}").json()
    assert [lib["commit"] for lib in model["libraries"]] == [commits["v1"]]
    gadget = lib_client.get("/api/v1/models/gadget").json()
    assert [lib["commit"] for lib in gadget["libraries"]] == [commits["v2"]]


# ── pins from before they moved into each model ───────────────────────────────


def test_boot_moves_the_lockfile_into_the_models(
    app: FastAPI, paths: DataPaths, tmp_path: Path
) -> None:
    history: ModelHistory = getattr(app.state, STATE_ATTR).history
    url, commits = make_library_upstream(tmp_path / "legacy", {"v1": "cube(1);\n"})
    lock = {"BOSL2": {"url": url, "ref": "v1", "commit": commits["v1"]}}
    (paths.model_dir(SLUG)).mkdir(parents=True)
    (paths.model_source(SLUG)).write_text(SOURCE, encoding="utf-8")
    paths.model_meta(SLUG).write_text(
        json.dumps({"name": "Widget", "libraries": ["BOSL2"]}), encoding="utf-8"
    )
    history.ensure_repo()
    (paths.models / LOCKFILE_NAME).write_text(json.dumps(lock), encoding="utf-8")
    assert history.commit("legacy", LOCKFILE_NAME, SLUG) is not None

    with TestClient(app) as client:
        record = client.get(f"/api/v1/models/{SLUG}").json()

    assert record["libraries"] == [{"name": "BOSL2", **lock["BOSL2"]}]
    assert not (paths.models / LOCKFILE_NAME).exists()


def test_a_failed_migration_does_not_stop_the_boot(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    with (
        patch("scadbuddy.main.migrate_lockfile", side_effect=OSError("EIO")),
        TestClient(app) as client,
    ):
        assert client.get("/healthz").status_code == 200
    assert "could not migrate the library lockfile" in caplog.text


# ── a dropped model.json's pins (#179) ────────────────────────────────────────


def _upload_with_meta(client: TestClient, meta: dict[str, Any]) -> httpx.Response:
    response: httpx.Response = client.post(
        "/api/v1/models",
        files={
            "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
            "meta": ("model.json", json.dumps(meta).encode(), "application/json"),
        },
    )
    return response


def test_a_dropped_model_json_cannot_name_a_library_without_a_pin(
    lib_client: TestClient,
) -> None:
    """A bare name has no shared lockfile left to resolve it; nothing is created."""
    refused = _upload_with_meta(lib_client, {"name": "Widget", "libraries": ["BOSL2"]})

    assert refused.status_code == 422
    assert refused.json()["libraries"] == ["BOSL2"]
    assert lib_client.get(f"/api/v1/models/{SLUG}").status_code == 404


def test_a_dropped_model_json_carries_its_pins_and_is_checked_with_them(
    lib_client: TestClient,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, commits = upstream
    # Fetched onto this volume by another model's pin.
    create_model(lib_client, "gadget")
    pin(lib_client, "BOSL2", "gadget")
    pinned = {"name": "BOSL2", "url": url, "ref": "v1", "commit": commits["v1"]}
    log = tmp_path / "openscadpath.log"
    monkeypatch.setenv("FAKE_OPENSCAD_PATH_LOG", str(log))

    created = _upload_with_meta(lib_client, {"name": "Widget", "libraries": [pinned, pinned]})

    assert created.status_code == 201, created.text
    assert created.json()["libraries"] == [pinned]
    # The parse check ran with the pinned checkout, as every render will.
    assert log.read_text(encoding="utf-8").splitlines() == [
        str(paths.libraries / "BOSL2" / commits["v1"])
    ]


def test_a_dropped_model_json_whose_checkout_is_not_here_is_a_409(
    lib_client: TestClient,
) -> None:
    """The 409 every render of it would be, and nothing is created."""
    pinned = {"name": "BOSL2", "url": "https://x.invalid/b.git", "ref": "v1", "commit": "a" * 40}

    refused = _upload_with_meta(lib_client, {"name": "Widget", "libraries": [pinned]})

    assert refused.status_code == 409, refused.text
    assert "not on this volume" in refused.json()["detail"]
    assert lib_client.get(f"/api/v1/models/{SLUG}").status_code == 404


def test_a_dropped_model_json_with_a_malformed_pin_is_a_422(lib_client: TestClient) -> None:
    broken = {"name": "BOSL2", "url": "https://x.invalid/b.git", "ref": "v1", "commit": "HEAD"}

    refused = _upload_with_meta(lib_client, {"name": "Widget", "libraries": [broken]})

    assert refused.status_code == 422, refused.text
    assert "'BOSL2' is not valid: commit:" in refused.json()["detail"]
    assert lib_client.get(f"/api/v1/models/{SLUG}").status_code == 404


# ── re-pinning (#253) ────────────────────────────────────────────────────────


def repin(client: TestClient, name: str, slug: str = SLUG, **body: Any) -> httpx.Response:
    response: httpx.Response = client.patch(f"/api/v1/models/{slug}/libraries/{name}", json=body)
    return response


def test_a_repin_moves_the_model_to_another_ref(
    lib_client: TestClient, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")

    response = repin(lib_client, "BOSL2", ref="v2")

    assert response.status_code == 200, response.text
    assert response.json()["libraries"] == [
        {"name": "BOSL2", "url": url, "ref": "v2", "commit": commits["v2"]}
    ]


def test_a_repin_keeps_the_fork_the_model_pins(lib_client: TestClient, tmp_path: Path) -> None:
    """PUT without a url would go back to the catalogue's upstream; PATCH must not."""
    elsewhere, commits = make_library_upstream(
        tmp_path / "elsewhere", {"v1": "sphere(1);\n", "v2": "sphere(2);\n"}
    )
    create_model(lib_client)
    pin(lib_client, "BOSL2", url=elsewhere, ref="v1")

    response = repin(lib_client, "BOSL2", ref="v2")

    assert response.status_code == 200, response.text
    [library] = response.json()["libraries"]
    assert (library["url"], library["commit"]) == (elsewhere, commits["v2"])


def test_a_repin_without_a_ref_follows_the_branch(
    lib_client: TestClient, upstream: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    _, commits = upstream
    create_model(lib_client)
    first = pin(lib_client, "BOSL2", ref="main")["libraries"][0]["commit"]
    assert first == commits["v2"]
    work = tmp_path / "upstream-work"
    env = git_env()
    (work / "std.scad").write_text("module marker() cube(3);\n", encoding="utf-8")
    for args in (
        ("commit", "-am", "later"),
        ("push", "--quiet", str(tmp_path / "upstream.git"), "main"),
    ):
        subprocess.run([GIT, *args], cwd=work, env=env, check=True, capture_output=True)

    response = repin(lib_client, "BOSL2")

    assert response.status_code == 200, response.text
    [library] = response.json()["libraries"]
    assert library["ref"] == "main"
    assert library["commit"] not in (first, commits["v1"])


def test_a_repin_of_a_library_the_model_does_not_declare_is_a_404(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    create_model(lib_client)
    store = libraries_app.dependency_overrides[get_libraries]()
    with patch.object(store, "resolve", side_effect=AssertionError("reached the service")):
        response = repin(lib_client, "BOSL2", ref="v2")

    assert response.status_code == 404
    assert "does not declare" in response.json()["detail"]


def test_a_repin_is_refused_like_a_pin(lib_client: TestClient, libraries_app: FastAPI) -> None:
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    store = libraries_app.dependency_overrides[get_libraries]()

    with patch.object(store, "resolve", side_effect=AssertionError("reached the service")):
        dot_dot = repin(lib_client, "BOSL2", ref="v1..v2")
        built_in = repin(lib_client, "BOSL2", "builtin:anything", ref="v2")
    missing_ref = repin(lib_client, "BOSL2", ref="v9")

    assert dot_dot.status_code == 422
    assert built_in.status_code == 403
    assert missing_ref.status_code == 502
    assert lib_client.get(f"/api/v1/models/{SLUG}").json()["libraries"][0]["ref"] == "v1"


# ── checkouts on the volume (#253) ───────────────────────────────────────────


def test_the_installed_checkouts_name_the_models_that_pin_them(
    lib_client: TestClient, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    create_model(lib_client)
    create_model(lib_client, "gadget")
    pin(lib_client, "BOSL2")
    pin(lib_client, "BOSL2", "gadget", ref="v2")
    pin(lib_client, "BOSL2", "gadget", ref="v1")

    installed = lib_client.get("/api/v1/libraries/installed").json()

    assert sorted((entry["commit"], entry["used_by"]) for entry in installed) == sorted(
        [(commits["v1"], ["gadget", SLUG]), (commits["v2"], [])]
    )


def test_a_library_a_model_still_pins_is_not_removed(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")

    refused = lib_client.delete("/api/v1/libraries/BOSL2")
    one = lib_client.delete("/api/v1/libraries/BOSL2", params={"commit": commits["v1"]})

    for response in (refused, one):
        assert response.status_code == 409, response.text
        assert response.json()["models"] == [SLUG]
        assert SLUG in response.json()["detail"]
    assert (paths.libraries / "BOSL2" / commits["v1"] / "BOSL2").is_dir()


def test_a_checkout_no_model_pins_is_removed(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    pin(lib_client, "BOSL2", ref="v2")  # v1's checkout stays, pinned by nothing live

    removed = lib_client.delete("/api/v1/libraries/BOSL2", params={"commit": commits["v1"]})

    assert removed.status_code == 204, removed.text
    assert not (paths.libraries / "BOSL2" / commits["v1"]).exists()
    assert (paths.libraries / "BOSL2" / commits["v2"] / "BOSL2").is_dir()
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200


def test_every_checkout_goes_once_nothing_pins_the_library(
    lib_client: TestClient, paths: DataPaths
) -> None:
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    pin(lib_client, "BOSL2", ref="v2")
    assert lib_client.delete(f"/api/v1/models/{SLUG}/libraries/BOSL2").status_code == 200

    removed = lib_client.delete("/api/v1/libraries/BOSL2")
    again = lib_client.delete("/api/v1/libraries/BOSL2")

    assert removed.status_code == 204, removed.text
    assert not (paths.libraries / "BOSL2").exists()
    assert again.status_code == 404
    assert lib_client.get("/api/v1/libraries/installed").json() == []


def test_a_name_only_declaration_counts_as_a_pin_of_every_commit(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    """A bare name from before per-model pins, or an unreadable model.json, cannot say
    which checkout it needs, so it keeps them all."""
    _, commits = upstream
    create_model(lib_client)
    create_model(lib_client, "legacy")
    create_model(lib_client, "broken")
    pin(lib_client, "BOSL2")
    assert lib_client.delete(f"/api/v1/models/{SLUG}/libraries/BOSL2").status_code == 200
    meta = json.loads(paths.model_meta("legacy").read_text(encoding="utf-8"))
    paths.model_meta("legacy").write_text(
        json.dumps({**meta, "libraries": ["BOSL2"]}), encoding="utf-8"
    )
    paths.model_meta("broken").write_text('{"libraries": ["BOSL2"', encoding="utf-8")

    refused = lib_client.delete("/api/v1/libraries/BOSL2", params={"commit": commits["v1"]})

    assert refused.status_code == 409
    assert refused.json()["models"] == ["broken", "legacy"]


def test_a_removal_refuses_what_is_not_a_checkout(lib_client: TestClient) -> None:
    assert lib_client.delete("/api/v1/libraries/BOSL2").status_code == 404
    assert lib_client.delete("/api/v1/libraries/.git").status_code == 422
    assert lib_client.delete("/api/v1/libraries/BOSL2", params={"commit": "HEAD"}).status_code == (
        422
    )


async def test_a_removal_waits_for_pins_in_flight() -> None:
    gate = CheckoutGate()
    order: list[str] = []
    pinning = asyncio.Event()
    finish_pin = asyncio.Event()

    async def pin_one() -> None:
        async with gate.pinning():
            pinning.set()
            await finish_pin.wait()
            order.append("pinned")

    async def remove_one() -> None:
        await pinning.wait()
        async with gate.removing():
            order.append("removed")

    pin_task = asyncio.create_task(pin_one())
    remove_task = asyncio.create_task(remove_one())
    await asyncio.sleep(0.01)
    assert order == []
    finish_pin.set()
    await asyncio.gather(pin_task, remove_task)

    assert order == ["pinned", "removed"]
