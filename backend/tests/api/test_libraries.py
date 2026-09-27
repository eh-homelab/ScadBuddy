"""The library routes (#93), and a model's libraries reaching its renders.

The upstream is a local bare repository, so the clone is real and offline.
"""

from __future__ import annotations

import json
import logging
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

from scadbuddy.api.deps import INSTALL_CONCURRENCY, STATE_ATTR, AppState, get_libraries
from scadbuddy.core.paths import DataPaths
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
        state.history,
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


def add(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post("/api/v1/libraries", json=body)
    assert response.status_code == 200, response.text
    added: dict[str, Any] = response.json()
    return added


def create_model(client: TestClient) -> None:
    response = client.post("/api/v1/models", json={"name": SLUG, "source": SOURCE})
    assert response.status_code == 201, response.text


def declare(client: TestClient, *names: str) -> dict[str, Any]:
    response = client.patch(f"/api/v1/models/{SLUG}", json={"libraries": list(names)})
    assert response.status_code == 200, response.text
    record: dict[str, Any] = response.json()
    return record


def test_the_catalogue_lists_what_can_be_added(lib_client: TestClient) -> None:
    listed = lib_client.get("/api/v1/libraries").json()

    assert [(entry["name"], entry["ref"], entry["curated"]) for entry in listed] == [
        ("BOSL2", "v1", True)
    ]
    assert listed[0]["pin"] is None
    assert listed[0]["licence"] == "BSD-2-Clause"


def test_adding_a_library_pins_it_in_the_models_repository(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream

    added = add(lib_client, name="BOSL2")

    assert added["pin"] == {"url": url, "ref": "v1", "commit": commits["v1"]}
    lock = json.loads((paths.models / LOCKFILE_NAME).read_text(encoding="utf-8"))
    assert lock["BOSL2"]["commit"] == commits["v1"]
    assert lib_client.get("/api/v1/libraries").json()[0]["pin"]["commit"] == commits["v1"]


def test_bumping_the_ref_writes_the_new_commit_to_the_lock(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    add(lib_client, name="BOSL2")

    bumped = add(lib_client, name="BOSL2", ref="v2")

    assert bumped["pin"]["commit"] == commits["v2"]
    lock = json.loads((paths.models / LOCKFILE_NAME).read_text(encoding="utf-8"))
    assert lock["BOSL2"]["ref"] == "v2"


def test_a_user_added_library_is_listed_after_the_catalogue(
    lib_client: TestClient, upstream: tuple[str, dict[str, str]]
) -> None:
    url, _ = upstream

    add(lib_client, name="mylib", url=url, ref="v2")

    listed = lib_client.get("/api/v1/libraries").json()
    assert [(entry["name"], entry["curated"]) for entry in listed] == [
        ("BOSL2", True),
        ("mylib", False),
    ]


def test_installs_in_flight_are_capped(
    lib_client: TestClient, libraries_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each clone holds a worker thread for up to the git timeout; a burst of them
    must not take the executor every other route shares."""
    store = libraries_app.dependency_overrides[get_libraries]()
    lock = threading.Lock()
    running, most = 0, 0
    real_install = store.install

    def install(name: str, **kwargs: Any) -> Any:
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        time.sleep(0.2)
        with lock:
            running -= 1
        return real_install(name, **kwargs)

    monkeypatch.setattr(store, "install", install)
    with ThreadPoolExecutor(INSTALL_CONCURRENCY + 2) as pool:
        codes = list(
            pool.map(
                lambda _: lib_client.post("/api/v1/libraries", json={"name": "BOSL2"}).status_code,
                range(INSTALL_CONCURRENCY + 2),
            )
        )

    assert codes == [200] * (INSTALL_CONCURRENCY + 2)
    assert most == INSTALL_CONCURRENCY


def test_an_unknown_library_without_a_url_is_a_404(lib_client: TestClient) -> None:
    response = lib_client.post("/api/v1/libraries", json={"name": "nothing"})
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


def test_a_url_on_a_transport_that_is_not_allowed_is_a_422(lib_client: TestClient) -> None:
    response = lib_client.post(
        "/api/v1/libraries", json={"name": "mylib", "url": "ext::sh -c touch% /tmp/x", "ref": "v1"}
    )
    assert response.status_code == 422


def test_a_curated_name_with_another_url_is_a_422(
    lib_client: TestClient, paths: DataPaths, tmp_path: Path
) -> None:
    elsewhere, _ = make_library_upstream(tmp_path / "elsewhere", {"v1": "sphere(1);\n"})

    response = lib_client.post(
        "/api/v1/libraries", json={"name": "BOSL2", "url": elsewhere, "ref": "v1"}
    )

    assert response.status_code == 422
    assert "BOSL2" in response.json()["detail"]
    assert not (paths.models / LOCKFILE_NAME).exists()


def test_a_name_with_a_path_in_it_is_a_422(lib_client: TestClient) -> None:
    response = lib_client.post("/api/v1/libraries", json={"name": "../up"})
    assert response.status_code == 422


def test_a_ref_with_dot_dot_is_refused_by_validation(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    store = libraries_app.dependency_overrides[get_libraries]()
    with patch.object(store, "install", side_effect=AssertionError("reached the service")):
        response = lib_client.post("/api/v1/libraries", json={"name": "BOSL2", "ref": "a..b"})

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["errors"]] == [["body", "ref"]]


def test_the_schema_states_that_a_ref_has_no_dot_dot(lib_client: TestClient) -> None:
    schema = lib_client.get("/openapi.json").json()["components"]["schemas"]
    ref = schema["LibraryAdd"]["properties"]["ref"]["anyOf"][0]

    assert "(?!.*\\.\\.)" in ref["pattern"]


def test_a_hand_edited_pin_with_a_bad_commit_is_a_clear_500(
    libraries_app: FastAPI, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    url, _ = upstream
    (paths.models / LOCKFILE_NAME).write_text(
        json.dumps({"BOSL2": {"url": url, "ref": "v1", "commit": "HEAD"}}), encoding="utf-8"
    )

    with TestClient(libraries_app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/libraries")

    assert response.status_code == 500
    assert "libraries.lock entry 'BOSL2' is not valid: commit:" in response.json()["detail"]


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
    with caplog.at_level(logging.WARNING, logger="scadbuddy.library.libraries"):
        response = lib_client.post("/api/v1/libraries", json={"name": "BOSL2", "ref": "v9"})

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "v9" in detail
    # git's own words go to the log, never to the client.
    (logged,) = [record for record in caplog.records if record.message == "git failed"]
    stderr: str = logged.git_stderr  # type: ignore[attr-defined]
    assert "upstream origin" in stderr
    assert stderr not in detail
    assert "fatal" not in detail


def test_a_url_on_the_cluster_network_is_a_422_without_a_clone(
    lib_client: TestClient,
    libraries_app: FastAPI,
    paths: DataPaths,
    fake_dns: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_dns["git.internal.example"] = ["10.0.0.7"]
    store = libraries_app.dependency_overrides[get_libraries]()
    monkeypatch.setattr(store, "protocols", ("https",))
    monkeypatch.setattr(store, "_git", lambda *args: pytest.fail(f"git ran: {args}"))

    response = lib_client.post(
        "/api/v1/libraries",
        json={"name": "mylib", "url": "https://git.internal.example/o/r.git", "ref": "v1"},
    )

    assert response.status_code == 422
    assert "public" in response.json()["detail"]
    assert not (paths.models / LOCKFILE_NAME).exists()


def test_a_model_can_declare_only_a_library_that_is_pinned(lib_client: TestClient) -> None:
    create_model(lib_client)

    refused = lib_client.patch(f"/api/v1/models/{SLUG}", json={"libraries": ["BOSL2"]})
    assert refused.status_code == 422
    assert "BOSL2" in refused.json()["detail"]

    add(lib_client, name="BOSL2")
    assert declare(lib_client, "BOSL2")["libraries"] == ["BOSL2"]
    assert lib_client.get(f"/api/v1/models/{SLUG}").json()["libraries"] == ["BOSL2"]


def test_the_schema_is_derived_with_only_the_declared_libraries(
    lib_client: TestClient,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, commits = upstream
    add(lib_client, name="BOSL2")
    add(lib_client, name="other", url=url, ref="v2")
    create_model(lib_client)
    declare(lib_client, "BOSL2")
    log = tmp_path / "openscadpath.log"
    monkeypatch.setenv("FAKE_OPENSCAD_PATH_LOG", str(log))

    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200

    assert log.read_text(encoding="utf-8").splitlines() == [
        str(paths.libraries / "BOSL2" / commits["v1"])
    ]


@respx.mock
@pytest.mark.usefixtures("fake_dns")
def test_an_imported_model_declares_libraries_like_any_other(
    lib_client: TestClient,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#153's URL import goes through the same create path, so its model takes a
    declaration and renders with exactly that library, keeping where it came from."""
    _, commits = upstream
    raw = "https://raw.githubusercontent.com/someone/models/main/widget.scad"
    respx.get(raw).mock(return_value=httpx.Response(200, text=SOURCE))
    add(lib_client, name="BOSL2")
    imported = lib_client.post("/api/v1/models/import", json={"url": raw})
    assert imported.status_code == 201, imported.text
    log = tmp_path / "openscadpath.log"
    monkeypatch.setenv("FAKE_OPENSCAD_PATH_LOG", str(log))

    record = declare(lib_client, "BOSL2")

    assert (record["libraries"], record["origin_url"]) == (["BOSL2"], raw)
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
    add(lib_client, name="BOSL2")
    create_model(lib_client)
    declare(lib_client, "BOSL2")
    log = tmp_path / "openscadpath.log"
    monkeypatch.setenv("FAKE_OPENSCAD_PATH_LOG", str(log))

    checked = lib_client.post("/api/v1/models/check", json={"source": SOURCE, "slug": SLUG})
    saved = lib_client.put(f"/api/v1/models/{SLUG}/source", json={"source": SOURCE + "\n"})

    assert checked.status_code == 200, checked.text
    assert saved.status_code == 200, saved.text
    expected = str(paths.libraries / "BOSL2" / commits["v1"])
    assert log.read_text(encoding="utf-8").splitlines() == [expected, expected]


def test_a_declared_library_whose_checkout_is_gone_is_a_409(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    add(lib_client, name="BOSL2")
    create_model(lib_client)
    declare(lib_client, "BOSL2")
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
    add(lib_client, name="BOSL2")
    create_model(lib_client)
    declare(lib_client, "BOSL2")
    (paths.libraries / "BOSL2" / commits["v1"]).rename(paths.root / "elsewhere")

    checked = lib_client.post("/api/v1/models/check", json={"source": SOURCE, "slug": SLUG})
    saved = lib_client.put(f"/api/v1/models/{SLUG}/source", json={"source": SOURCE + "\n"})

    assert checked.status_code == 409
    assert checked.headers["content-type"] == "application/problem+json"
    assert "BOSL2" in checked.json()["detail"]
    assert saved.status_code == 409


def _schema_runs(log: Path) -> int:
    return len(log.read_text(encoding="utf-8").splitlines()) if log.is_file() else 0


def test_a_new_pin_re_derives_the_schema_of_models_that_declare_it(
    lib_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same source, other library code: the cached schema is keyed on the pins too."""
    add(lib_client, name="BOSL2")
    create_model(lib_client)
    declare(lib_client, "BOSL2")
    log = tmp_path / "invocations.log"
    monkeypatch.setenv("FAKE_OPENSCAD_LOG", str(log))
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200
    assert _schema_runs(log) == 1

    add(lib_client, name="BOSL2", ref="v2")
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200

    assert _schema_runs(log) == 2


def test_changing_the_declaration_re_derives_the_schema(
    lib_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(lib_client, name="BOSL2")
    create_model(lib_client)
    log = tmp_path / "invocations.log"
    monkeypatch.setenv("FAKE_OPENSCAD_LOG", str(log))
    # The create's own check derived it, so this one is served from the cache.
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200
    assert _schema_runs(log) == 0

    declare(lib_client, "BOSL2")
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200

    assert _schema_runs(log) == 1


def test_restoring_old_pins_re_derives_the_schema(
    lib_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(lib_client, name="BOSL2")
    create_model(lib_client)
    current = declare(lib_client, "BOSL2")["version"]
    add(lib_client, name="BOSL2", ref="v2")
    log = tmp_path / "invocations.log"
    monkeypatch.setenv("FAKE_OPENSCAD_LOG", str(log))
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200

    restored = lib_client.post(f"/api/v1/models/{SLUG}/versions/{current}/restore")
    assert restored.status_code == 200, restored.text
    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200

    assert _schema_runs(log) == 2


def test_restoring_a_revision_restores_its_pins(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    add(lib_client, name="BOSL2")
    create_model(lib_client)
    written_against = declare(lib_client, "BOSL2")["version"]
    edited = lib_client.put(f"/api/v1/models/{SLUG}/source", json={"source": SOURCE + "cube(1);\n"})
    assert edited.status_code == 200, edited.text
    add(lib_client, name="BOSL2", ref="v2")

    restored = lib_client.post(f"/api/v1/models/{SLUG}/versions/{written_against}/restore")

    assert restored.status_code == 200, restored.text
    assert [change["path"] for change in restored.json()["files"]] == ["model.scad"]
    lock = json.loads((paths.models / LOCKFILE_NAME).read_text(encoding="utf-8"))
    assert lock["BOSL2"]["commit"] == commits["v1"]


def test_restoring_the_current_revision_still_restores_its_pins(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    """The model is unchanged, so the revision touches only the lockfile -- and the
    answer is still the revision the model is at, not a 500."""
    _, commits = upstream
    add(lib_client, name="BOSL2")
    create_model(lib_client)
    current = declare(lib_client, "BOSL2")["version"]
    add(lib_client, name="BOSL2", ref="v2")

    restored = lib_client.post(f"/api/v1/models/{SLUG}/versions/{current}/restore")

    assert restored.status_code == 200, restored.text
    assert restored.json()["commit"] == current
    lock = json.loads((paths.models / LOCKFILE_NAME).read_text(encoding="utf-8"))
    assert lock["BOSL2"]["commit"] == commits["v1"]
