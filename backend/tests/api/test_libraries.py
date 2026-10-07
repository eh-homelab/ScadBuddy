"""The library routes (#93): the catalogue, pinning a library to one model, and
that model's pins reaching its renders.

The upstream is a local bare repository, so the clone is real and offline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import httpx
import psycopg
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import libraries as libraries_api
from scadbuddy.api import library_pins
from scadbuddy.api import operations as operations_api
from scadbuddy.api.deps import (
    DEPENDENCY_CHECK_CONCURRENCY,
    STATE_ATTR,
    AppState,
    get_fonts,
    get_libraries,
)
from scadbuddy.core.components import Components
from scadbuddy.core.config import INSTALL_CONCURRENCY
from scadbuddy.core.paths import DataPaths, model_path
from scadbuddy.core.problems import ApiError
from scadbuddy.library import operations as library_operations
from scadbuddy.library import url_import
from scadbuddy.library.catalogue import InvalidModelMetaError
from scadbuddy.library.history import GIT, GitError, ModelHistory, git_env
from scadbuddy.library.includes import resolve_dependencies
from scadbuddy.library.libraries import (
    CLONE_TIMEOUT,
    STAGING_PREFIX,
    CatalogueLibrary,
    CheckoutGate,
    CheckoutLeases,
    LibraryDeclarationError,
    LibraryNotInstalledError,
    LibraryStore,
    ModelLibrary,
)
from scadbuddy.library.scad import check_source
from scadbuddy.main import sweep_library_checkouts
from scadbuddy.workflows.commands import start_command
from tests.api.conftest import set_fake_env
from tests.conftest import make_library_upstream, open_pg_pool
from tests.support.operations import press
from tests.test_library_processes import _age

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
    # The pin commands run on the library worker, which reads the state, not the route's
    # dependencies.
    state.libraries = store
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
    response = client.put(f"/api/v1/models/{slug}/libraries/{name}", json=body, headers=press())
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


def test_two_models_render_one_library_at_two_refs(
    lib_client: TestClient,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    tmp_path: Path,
) -> None:
    _, commits = upstream
    create_model(lib_client)
    create_model(lib_client, "gadget")
    pin(lib_client, "BOSL2")
    pin(lib_client, "BOSL2", "gadget", ref="v2")
    log = tmp_path / "openscadpath.log"
    set_fake_env(tmp_path, "FAKE_OPENSCAD_PATH_LOG", str(log))

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

    removed = lib_client.delete(f"/api/v1/models/{SLUG}/libraries/BOSL2", headers=press())
    again = lib_client.delete(f"/api/v1/models/{SLUG}/libraries/BOSL2", headers=press())

    assert removed.status_code == 200, removed.text
    assert removed.json()["libraries"] == []
    assert again.status_code == 404


def test_a_built_in_takes_no_pins(lib_client: TestClient, libraries_app: FastAPI) -> None:
    store = libraries_app.dependency_overrides[get_libraries]()
    with patch.object(store, "resolve", side_effect=AssertionError("reached the service")):
        response = lib_client.put(
            "/api/v1/models/builtin:anything/libraries/BOSL2", json={}, headers=press()
        )

    assert response.status_code == 403


def test_pinning_to_a_model_that_does_not_exist_is_a_404_without_a_clone(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    store = libraries_app.dependency_overrides[get_libraries]()
    with patch.object(store, "resolve", side_effect=AssertionError("reached the service")):
        response = lib_client.put(
            f"/api/v1/models/{SLUG}/libraries/BOSL2", json={}, headers=press()
        )

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
                    lib_client.put(
                        f"/api/v1/models/{slug}/libraries/BOSL2", json={}, headers=press()
                    ).status_code
                ),
                slugs,
            )
        )

    assert codes == [200] * len(slugs)
    assert most == INSTALL_CONCURRENCY


def test_an_unknown_library_without_a_url_is_a_404(lib_client: TestClient) -> None:
    create_model(lib_client)
    response = lib_client.put(f"/api/v1/models/{SLUG}/libraries/nothing", json={}, headers=press())
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert "catalogue" in response.json()["detail"]


def test_a_url_on_a_transport_that_is_not_allowed_is_a_422(lib_client: TestClient) -> None:
    create_model(lib_client)
    response = lib_client.put(
        f"/api/v1/models/{SLUG}/libraries/mylib",
        json={"url": "ext::sh -c touch% /tmp/x", "ref": "v1"},
        headers=press(),
    )
    assert response.status_code == 422


def test_a_name_that_is_not_a_directory_name_is_a_422(lib_client: TestClient) -> None:
    create_model(lib_client)
    response = lib_client.put(f"/api/v1/models/{SLUG}/libraries/.hidden", json={}, headers=press())
    assert response.status_code == 422


def test_a_ref_with_dot_dot_is_refused_by_validation(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    create_model(lib_client)
    store = libraries_app.dependency_overrides[get_libraries]()
    with patch.object(store, "resolve", side_effect=AssertionError("reached the service")):
        response = lib_client.put(
            f"/api/v1/models/{SLUG}/libraries/BOSL2", json={"ref": "a..b"}, headers=press()
        )

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


def test_a_model_lists_its_invalid_library_entries_and_they_can_be_removed(
    lib_client: TestClient, paths: DataPaths
) -> None:
    """#217: an entry that is not a pin is left out of `libraries` but listed in
    `invalid_libraries`, with the render's 409 detail, and removed by its name."""
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    meta_path = paths.model_meta(SLUG)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    good = meta["libraries"][0]
    meta["libraries"] = [
        "threads",
        good,
        {"name": "gears", "url": good["url"], "ref": "v1"},
        42,
        {**good, "name": "bad name"},
    ]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    model = lib_client.get(f"/api/v1/models/{SLUG}").json()
    render = lib_client.post(f"/api/v1/models/{SLUG}/render", json={"params": {}})

    assert [lib["name"] for lib in model["libraries"]] == ["BOSL2"]
    invalid = model["invalid_libraries"]
    # Only the names DELETE can take; the rest are named in the problem alone.
    assert [entry["name"] for entry in invalid] == ["threads", "gears", None, None]
    # Their positions in model.json's `libraries`, past the valid pin at 1.
    assert [entry["index"] for entry in invalid] == [0, 2, 3, 4]
    assert render.status_code == 409
    assert invalid[0]["problem"] == render.json()["detail"]
    assert "'gears' is not valid: commit:" in invalid[1]["problem"]
    assert "an entry is not valid" in invalid[2]["problem"]
    assert "'bad name' is not valid: name:" in invalid[3]["problem"]

    for name in ("threads", "gears"):
        removed = lib_client.delete(f"/api/v1/models/{SLUG}/libraries/{name}", headers=press())
        assert removed.status_code == 200, removed.text
    assert [entry["name"] for entry in removed.json()["invalid_libraries"]] == [None, None]
    assert [lib["name"] for lib in removed.json()["libraries"]] == ["BOSL2"]


def test_a_libraries_that_is_not_a_list_is_one_invalid_entry(
    lib_client: TestClient, paths: DataPaths
) -> None:
    create_model(lib_client)
    meta_path = paths.model_meta(SLUG)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["libraries"] = {"BOSL2": "v1"}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    model = lib_client.get(f"/api/v1/models/{SLUG}").json()

    assert model["libraries"] == []
    assert model["invalid_libraries"] == [
        {"name": None, "index": None, "problem": "`libraries` in model.json is not a list"}
    ]


def _write_libraries(paths: DataPaths, entries: list[Any]) -> None:
    meta_path = paths.model_meta(SLUG)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["libraries"] = entries
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def _raw_libraries(paths: DataPaths) -> Any:
    return json.loads(paths.model_meta(SLUG).read_text(encoding="utf-8"))["libraries"]


def test_removing_an_invalid_entry_by_index_keeps_a_pin_of_the_same_name(
    lib_client: TestClient, paths: DataPaths
) -> None:
    """#217: Remove on an invalid entry takes that entry alone, not the valid pin
    that shares its name."""
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    good = _raw_libraries(paths)[0]
    broken = {"name": "BOSL2", "url": good["url"], "ref": "v1"}
    _write_libraries(paths, [good, broken])
    [entry] = lib_client.get(f"/api/v1/models/{SLUG}").json()["invalid_libraries"]
    assert (entry["name"], entry["index"]) == ("BOSL2", 1)

    removed = lib_client.delete(
        f"/api/v1/models/{SLUG}/libraries/BOSL2", params={"index": 1}, headers=press()
    )

    assert removed.status_code == 200, removed.text
    assert removed.json()["invalid_libraries"] == []
    assert _raw_libraries(paths) == [good]


def test_removing_one_of_two_invalid_entries_of_a_name_keeps_the_other(
    lib_client: TestClient, paths: DataPaths
) -> None:
    create_model(lib_client)
    first = {"name": "threads", "url": "https://example.invalid/a.git", "ref": "v1"}
    _write_libraries(paths, ["threads", first])

    removed = lib_client.delete(
        f"/api/v1/models/{SLUG}/libraries/threads", params={"index": 0}, headers=press()
    )

    assert removed.status_code == 200, removed.text
    assert _raw_libraries(paths) == [first]
    assert [(e["name"], e["index"]) for e in removed.json()["invalid_libraries"]] == [
        ("threads", 0)
    ]


def test_removing_by_index_refuses_an_entry_that_is_not_that_invalid_one(
    lib_client: TestClient, paths: DataPaths
) -> None:
    """The entry at `index` must still be an invalid one of that name: a valid pin,
    another name or a position past the end is a 409, and nothing is removed."""
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    good = _raw_libraries(paths)[0]
    _write_libraries(paths, [good, "threads"])

    for name, index in (("BOSL2", 0), ("BOSL2", 1), ("threads", 0), ("threads", 2)):
        refused = lib_client.delete(
            f"/api/v1/models/{SLUG}/libraries/{name}", params={"index": index}, headers=press()
        )
        assert refused.status_code == 409, (name, index, refused.text)
    assert _raw_libraries(paths) == [good, "threads"]
    # Without `index`, every entry of the name goes, as before.
    removed = lib_client.delete(f"/api/v1/models/{SLUG}/libraries/threads", headers=press())
    assert removed.status_code == 200, removed.text
    assert _raw_libraries(paths) == [good]


def test_boot_sweeps_staging_clones_a_killed_install_left(app: FastAPI, paths: DataPaths) -> None:
    staging = paths.libraries / f"{STAGING_PREFIX}0123abcd" / "BOSL2"
    staging.mkdir(parents=True)
    (staging / "std.scad").write_text("cube(1);\n", encoding="utf-8")
    _age(staging.parent)
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
        response = lib_client.put(
            f"/api/v1/models/{SLUG}/libraries/BOSL2", json={"ref": "v9"}, headers=press()
        )

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

    response = lib_client.put(f"/api/v1/models/{SLUG}/libraries/BOSL2", json={}, headers=press())

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
        headers=press(),
    )

    assert response.status_code == 422
    assert "public" in response.json()["detail"]


def test_a_url_whose_lookup_times_out_is_a_503_to_try_again(
    lib_client: TestClient, libraries_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#205: a resolver that did not answer in time is not the SSRF refusal."""
    create_model(lib_client)

    async def hangs(host: str, port: int) -> list[str]:
        await asyncio.sleep(5)
        return ["10.0.0.7"]

    monkeypatch.setattr(url_import, "resolve_host", hangs)
    monkeypatch.setattr(url_import, "RESOLVE_TIMEOUT", 0.05)
    store = libraries_app.dependency_overrides[get_libraries]()
    monkeypatch.setattr(store, "protocols", ("https",))
    monkeypatch.setattr(store, "_git", lambda *args: pytest.fail(f"git ran: {args}"))

    response = lib_client.put(
        f"/api/v1/models/{SLUG}/libraries/mylib",
        json={"url": "https://git.example/o/r.git", "ref": "v1"},
        headers=press(),
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"] == "could not resolve git.example just now; try again"
    assert lib_client.get(f"/api/v1/models/{SLUG}").json()["libraries"] == []


@respx.mock
@pytest.mark.usefixtures("fake_dns")
def test_an_imported_model_takes_pins_like_any_other(
    lib_client: TestClient,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    tmp_path: Path,
) -> None:
    """#153's URL import goes through the same create path, so its model takes a
    pin and renders with exactly that library, keeping where it came from."""
    _, commits = upstream
    raw = "https://raw.githubusercontent.com/someone/models/main/widget.scad"
    respx.get(raw).mock(return_value=httpx.Response(200, text=SOURCE))
    imported = lib_client.post("/api/v1/models/import", json={"url": raw})
    assert imported.status_code == 201, imported.text
    log = tmp_path / "openscadpath.log"
    set_fake_env(tmp_path, "FAKE_OPENSCAD_PATH_LOG", str(log))

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
) -> None:
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    log = tmp_path / "openscadpath.log"
    set_fake_env(tmp_path, "FAKE_OPENSCAD_PATH_LOG", str(log))

    checked = lib_client.post("/api/v1/models/check", json={"source": SOURCE, "slug": SLUG})
    saved = lib_client.put(f"/api/v1/models/{SLUG}/source", json={"source": SOURCE + "\n"})

    assert checked.status_code == 200, checked.text
    assert saved.status_code == 200, saved.text
    expected = str(paths.libraries / "BOSL2" / commits["v1"])
    assert log.read_text(encoding="utf-8").splitlines() == [expected, expected]


def _unreachable(url: str) -> None:
    """Take the upstream away, so fetching a missing checkout again fails."""
    bare = Path(url.removeprefix("file://"))
    bare.rename(bare.with_name("moved.git"))


def test_a_pinned_library_whose_checkout_is_gone_is_fetched_again(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    """Re-cloned at the pinned commit from the pin's own URL (#169), rather than a
    409 asking for the library to be pinned again."""
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    checkout = paths.libraries / "BOSL2" / commits["v1"]
    shutil.rmtree(checkout)

    schema = lib_client.get(f"/api/v1/models/{SLUG}/schema")
    render = lib_client.post(f"/api/v1/models/{SLUG}/render", json={"params": {}})

    assert schema.status_code == 200, schema.text
    assert render.status_code == 202, render.text
    assert (checkout / "BOSL2" / "std.scad").read_text(encoding="utf-8") == (
        "module marker() cube(1);\n"
    )
    assert not [e for e in paths.libraries.iterdir() if e.name.startswith(STAGING_PREFIX)]


def test_a_pinned_library_whose_checkout_is_gone_is_a_409_when_it_cannot_be_fetched(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    checkout = paths.libraries / "BOSL2" / commits["v1"]
    checkout.rename(paths.root / "elsewhere")
    _unreachable(url)

    schema = lib_client.get(f"/api/v1/models/{SLUG}/schema")
    render = lib_client.post(f"/api/v1/models/{SLUG}/render", json={"params": {}})

    assert schema.status_code == 409
    assert "BOSL2" in schema.json()["detail"]
    assert "fetching it again failed" in schema.json()["detail"]
    assert render.status_code == 409
    assert not checkout.exists()


def test_a_missing_checkout_is_fetched_at_its_commit_when_the_ref_has_moved(
    lib_client: TestClient,
    libraries_app: FastAPI,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    """A branch pin: the branch is at v2 now, but the model is pinned to v1."""
    url, commits = upstream
    create_model(lib_client)
    state: AppState = getattr(libraries_app.state, STATE_ATTR)
    state.catalogue.pin_library(
        SLUG, ModelLibrary(name="BOSL2", url=url, ref="main", commit=commits["v1"])
    )

    assert lib_client.get(f"/api/v1/models/{SLUG}/schema").status_code == 200

    assert [commit for _, commit in state.libraries.installed()] == [commits["v1"]]
    fetched = paths.libraries / "BOSL2" / commits["v1"] / "BOSL2" / "std.scad"
    assert fetched.read_text(encoding="utf-8") == "module marker() cube(1);\n"


def test_a_missing_checkout_is_a_409_when_the_upstream_refuses_its_commit(
    lib_client: TestClient,
    libraries_app: FastAPI,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    tmp_path: Path,
) -> None:
    """#459: the upstream answers -- the ref clones -- but refuses `fetch <commit>`
    for the commit the pin records (a branch force-pushed and collected since): the
    re-fetch in `_check_out` fails, and the user is told which commit and why."""
    url, _ = upstream
    # A real commit, from a repository this upstream has never had it from.
    _, elsewhere = make_library_upstream(tmp_path / "elsewhere", {"v1": "module other();\n"})
    gone = elsewhere["v1"]
    create_model(lib_client)
    state: AppState = getattr(libraries_app.state, STATE_ATTR)
    state.catalogue.pin_library(SLUG, ModelLibrary(name="BOSL2", url=url, ref="main", commit=gone))

    schema = lib_client.get(f"/api/v1/models/{SLUG}/schema")

    assert schema.status_code == 409
    detail = schema.json()["detail"]
    assert detail == (
        f"'BOSL2' is pinned to {gone[:7]}, which is not on this volume, and fetching it "
        f"again failed (could not fetch {gone[:7]} from {url}: no such ref, or the "
        "repository could not be reached); pin it to this model again"
    )
    assert not (paths.libraries / "BOSL2" / gone).exists()
    assert not [e for e in paths.libraries.iterdir() if e.name.startswith(STAGING_PREFIX)]


def test_a_missing_checkout_is_held_to_the_size_cap_when_fetched_again(
    lib_client: TestClient,
    libraries_app: FastAPI,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    shutil.rmtree(paths.libraries / "BOSL2" / commits["v1"])
    libraries_app.dependency_overrides[get_libraries]().max_bytes = 1

    schema = lib_client.get(f"/api/v1/models/{SLUG}/schema")

    assert schema.status_code == 409
    assert "a library may take" in schema.json()["detail"]
    assert not (paths.libraries / "BOSL2" / commits["v1"]).exists()


def test_an_old_revision_whose_checkout_is_gone_is_fetched_again(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    create_model(lib_client)
    written_against = pin(lib_client, "BOSL2")["version"]
    pin(lib_client, "BOSL2", ref="v2")
    removed = lib_client.delete(
        "/api/v1/libraries/BOSL2", params={"commit": commits["v1"]}, headers=press()
    )
    assert removed.status_code == 204, removed.text

    schema = lib_client.get(f"/api/v1/models/{SLUG}/versions/{written_against}/schema")

    assert schema.status_code == 200, schema.text
    assert (paths.libraries / "BOSL2" / commits["v1"] / "BOSL2").is_dir()


def test_the_editor_check_and_save_fetch_a_checkout_that_is_gone(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    checkout = paths.libraries / "BOSL2" / commits["v1"]
    shutil.rmtree(checkout)

    checked = lib_client.post("/api/v1/models/check", json={"source": SOURCE, "slug": SLUG})
    assert checked.status_code == 200, checked.text
    assert (checkout / "BOSL2").is_dir()
    shutil.rmtree(checkout)
    saved = lib_client.put(f"/api/v1/models/{SLUG}/source", json={"source": SOURCE + "\n"})
    assert saved.status_code == 200, saved.text
    assert (checkout / "BOSL2").is_dir()


def test_a_preset_save_fetches_a_checkout_that_is_gone(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    """Create, update and duplicate check the values against the schema, which reads
    the pins: each fetches a missing checkout again, as a render does."""
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    checkout = paths.libraries / "BOSL2" / commits["v1"]
    presets = f"/api/v1/models/{SLUG}/presets"

    shutil.rmtree(checkout)
    created = lib_client.post(presets, json={"name": "Small", "params": {}})
    assert created.status_code == 201, created.text
    assert (checkout / "BOSL2").is_dir()
    preset = f"{presets}/{created.json()['id']}"
    shutil.rmtree(checkout)
    updated = lib_client.patch(preset, json={"params": {}})
    assert updated.status_code == 200, updated.text
    assert (checkout / "BOSL2").is_dir()
    shutil.rmtree(checkout)
    duplicated = lib_client.post(f"{preset}/duplicate", json={"name": "Copy"})
    assert duplicated.status_code == 201, duplicated.text
    assert (checkout / "BOSL2").is_dir()


def test_the_editor_check_and_save_are_a_409_when_a_checkout_is_gone(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    """The check takes its source from the body, so it wires the library path up on
    its own rather than through `resolve_source` -- and must fail the same way."""
    url, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    (paths.libraries / "BOSL2" / commits["v1"]).rename(paths.root / "elsewhere")
    _unreachable(url)

    checked = lib_client.post("/api/v1/models/check", json={"source": SOURCE, "slug": SLUG})
    saved = lib_client.put(f"/api/v1/models/{SLUG}/source", json={"source": SOURCE + "\n"})

    assert checked.status_code == 409
    assert checked.headers["content-type"] == "application/problem+json"
    assert "BOSL2" in checked.json()["detail"]
    assert saved.status_code == 409


def _schema_runs(log: Path) -> int:
    return len(log.read_text(encoding="utf-8").splitlines()) if log.is_file() else 0


def test_a_new_pin_re_derives_the_schema(lib_client: TestClient, tmp_path: Path) -> None:
    """Same source, other library code: the cached schema is keyed on the pins too."""
    create_model(lib_client)
    log = tmp_path / "invocations.log"
    set_fake_env(tmp_path, "FAKE_OPENSCAD_LOG", str(log))
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
    """A bare name is not a pin: nothing is created."""
    refused = _upload_with_meta(lib_client, {"name": "Widget", "libraries": ["BOSL2"]})

    assert refused.status_code == 422
    assert "'BOSL2' without a pin" in refused.json()["detail"]
    assert lib_client.get(f"/api/v1/models/{SLUG}").status_code == 404


def test_a_dropped_model_json_carries_its_pins_and_is_checked_with_them(
    lib_client: TestClient,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    tmp_path: Path,
) -> None:
    url, commits = upstream
    # Fetched onto this volume by another model's pin.
    create_model(lib_client, "gadget")
    pin(lib_client, "BOSL2", "gadget")
    pinned = {"name": "BOSL2", "url": url, "ref": "v1", "commit": commits["v1"]}
    log = tmp_path / "openscadpath.log"
    set_fake_env(tmp_path, "FAKE_OPENSCAD_PATH_LOG", str(log))

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
    """One that cannot be fetched -- here, a transport this store does not allow --
    is the 409 every render of it would be, and nothing is created."""
    pinned = {"name": "BOSL2", "url": "https://x.invalid/b.git", "ref": "v1", "commit": "a" * 40}

    refused = _upload_with_meta(lib_client, {"name": "Widget", "libraries": [pinned]})

    assert refused.status_code == 409, refused.text
    assert "not on this volume" in refused.json()["detail"]
    assert "is not a file URL" in refused.json()["detail"]
    assert lib_client.get(f"/api/v1/models/{SLUG}").status_code == 404


def test_a_dropped_model_json_whose_checkout_is_not_here_fetches_it(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    """A model.json from another instance brings its pins; the checkouts follow."""
    url, commits = upstream
    pinned = {"name": "BOSL2", "url": url, "ref": "v2", "commit": commits["v2"]}

    created = _upload_with_meta(lib_client, {"name": "Widget", "libraries": [pinned]})

    assert created.status_code == 201, created.text
    assert created.json()["libraries"] == [pinned]
    assert (paths.libraries / "BOSL2" / commits["v2"] / "BOSL2").is_dir()


def test_a_dropped_model_json_with_a_malformed_pin_is_a_422(lib_client: TestClient) -> None:
    broken = {"name": "BOSL2", "url": "https://x.invalid/b.git", "ref": "v1", "commit": "HEAD"}

    refused = _upload_with_meta(lib_client, {"name": "Widget", "libraries": [broken]})

    assert refused.status_code == 422, refused.text
    assert "'BOSL2' is not valid: commit:" in refused.json()["detail"]
    assert lib_client.get(f"/api/v1/models/{SLUG}").status_code == 404


# ── re-pinning (#253) ────────────────────────────────────────────────────────


def repin(client: TestClient, name: str, slug: str = SLUG, **body: Any) -> httpx.Response:
    response: httpx.Response = client.patch(
        f"/api/v1/models/{slug}/libraries/{name}", json=body, headers=press()
    )
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

    refused = lib_client.delete("/api/v1/libraries/BOSL2", headers=press())
    one = lib_client.delete(
        "/api/v1/libraries/BOSL2", params={"commit": commits["v1"]}, headers=press()
    )

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

    removed = lib_client.delete(
        "/api/v1/libraries/BOSL2", params={"commit": commits["v1"]}, headers=press()
    )

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
    assert (
        lib_client.delete(f"/api/v1/models/{SLUG}/libraries/BOSL2", headers=press()).status_code
        == 200
    )

    removed = lib_client.delete("/api/v1/libraries/BOSL2", headers=press())
    again = lib_client.delete("/api/v1/libraries/BOSL2", headers=press())

    assert removed.status_code == 204, removed.text
    assert not (paths.libraries / "BOSL2").exists()
    assert again.status_code == 404
    assert lib_client.get("/api/v1/libraries/installed").json() == []


def test_a_name_only_declaration_counts_as_a_pin_of_every_commit(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    """A hand-edited entry with no commit, or an unreadable model.json, cannot say
    which checkout it needs, so it keeps them all."""
    _, commits = upstream
    create_model(lib_client)
    create_model(lib_client, "edited")
    create_model(lib_client, "broken")
    pin(lib_client, "BOSL2")
    assert (
        lib_client.delete(f"/api/v1/models/{SLUG}/libraries/BOSL2", headers=press()).status_code
        == 200
    )
    meta = json.loads(paths.model_meta("edited").read_text(encoding="utf-8"))
    paths.model_meta("edited").write_text(
        json.dumps({**meta, "libraries": [{"name": "BOSL2"}]}), encoding="utf-8"
    )
    paths.model_meta("broken").write_text('{"libraries": ["BOSL2"', encoding="utf-8")

    refused = lib_client.delete(
        "/api/v1/libraries/BOSL2", params={"commit": commits["v1"]}, headers=press()
    )

    assert refused.status_code == 409
    assert refused.json()["models"] == ["broken", "edited"]


def test_a_removal_refuses_what_is_not_a_checkout(lib_client: TestClient) -> None:
    assert lib_client.delete("/api/v1/libraries/BOSL2", headers=press()).status_code == 404
    assert lib_client.delete("/api/v1/libraries/.git", headers=press()).status_code == 422
    assert lib_client.delete(
        "/api/v1/libraries/BOSL2", params={"commit": "HEAD"}, headers=press()
    ).status_code == (422)


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


# ── review follow-ups: races (#324) ──────────────────────────────────────────


def _during_clone(libraries_app: FastAPI, meanwhile: Any) -> Any:
    """``resolve`` that runs ``meanwhile`` first: another request landing while this
    one's clone is in flight, at exactly that point."""
    store = libraries_app.dependency_overrides[get_libraries]()
    real = store.resolve

    def resolve(name: str, **kwargs: Any) -> Any:
        meanwhile()
        return real(name, **kwargs)

    return patch.object(store, "resolve", side_effect=resolve)


def test_a_repin_does_not_bring_back_a_library_unpinned_while_it_cloned(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    state: AppState = getattr(libraries_app.state, STATE_ATTR)

    with _during_clone(libraries_app, lambda: state.catalogue.unpin_library(SLUG, "BOSL2")):
        response = repin(lib_client, "BOSL2", ref="v2")

    assert response.status_code == 409, response.text
    assert "changed or removed" in response.json()["detail"]
    assert lib_client.get(f"/api/v1/models/{SLUG}").json()["libraries"] == []


def test_a_repin_does_not_overwrite_a_pin_moved_while_it_cloned(
    lib_client: TestClient, libraries_app: FastAPI, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    state: AppState = getattr(libraries_app.state, STATE_ATTR)
    moved = ModelLibrary(name="BOSL2", url=url, ref="main", commit=commits["v2"])

    with _during_clone(libraries_app, lambda: state.catalogue.pin_library(SLUG, moved)):
        response = repin(lib_client, "BOSL2", ref="v2")

    assert response.status_code == 409, response.text
    assert lib_client.get(f"/api/v1/models/{SLUG}").json()["libraries"][0]["ref"] == "main"


def test_a_checkout_a_render_is_reading_is_not_removed(
    lib_client: TestClient,
    libraries_app: FastAPI,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    """The model's pin is already gone, but a render that resolved it still runs."""
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    checkout = paths.libraries / "BOSL2" / commits["v1"]
    state: AppState = getattr(libraries_app.state, STATE_ATTR)
    job_id = "a" * 32
    lease = state.checkouts.hold(job_id, [checkout])
    assert (
        lib_client.delete(f"/api/v1/models/{SLUG}/libraries/BOSL2", headers=press()).status_code
        == 200
    )

    whole = lib_client.delete("/api/v1/libraries/BOSL2", headers=press())
    one = lib_client.delete(
        "/api/v1/libraries/BOSL2", params={"commit": commits["v1"]}, headers=press()
    )
    state.checkouts.release(lease)
    after = lib_client.delete("/api/v1/libraries/BOSL2", headers=press())

    for response in (whole, one):
        assert response.status_code == 409, response.text
        assert response.json()["jobs"] == [job_id]
    assert after.status_code == 204, after.text
    assert not checkout.exists()


def test_a_removal_refused_for_a_lease_leaves_no_operation(
    lib_client: TestClient,
    libraries_app: FastAPI,
    paths: DataPaths,
    pg_conninfo: str,
    upstream: tuple[str, dict[str, str]],
) -> None:
    """Review #1119 2-2: the removal's refusals are its check, so "try again once the
    render has finished" is not recorded as a failed operation."""
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    state: AppState = getattr(libraries_app.state, STATE_ATTR)
    state.checkouts.hold("a" * 32, [paths.libraries / "BOSL2" / commits["v1"]])
    with psycopg.connect(pg_conninfo) as conn:
        before = conn.execute("SELECT count(*) FROM operations").fetchone()

    leased = lib_client.delete("/api/v1/libraries/BOSL2", headers=press())
    unknown = lib_client.delete("/api/v1/libraries/nothing", headers=press())

    assert leased.status_code == 409, leased.text
    assert unknown.status_code == 404, unknown.text
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM operations").fetchone() == before


def test_a_pin_refused_before_its_clone_leaves_no_operation(
    lib_client: TestClient, libraries_app: FastAPI, pg_conninfo: str
) -> None:
    """Review #1119 2-2: an unknown catalogue name, and an address literal that is not
    public, are refused by the pin's check."""
    create_model(lib_client)
    store = libraries_app.dependency_overrides[get_libraries]()
    store.protocols = ("https",)

    unknown = lib_client.put(f"/api/v1/models/{SLUG}/libraries/nothing", json={}, headers=press())
    private = lib_client.put(
        f"/api/v1/models/{SLUG}/libraries/mylib",
        json={"url": "https://10.0.0.7/o/r.git", "ref": "v1"},
        headers=press(),
    )

    assert unknown.status_code == 404, unknown.text
    assert private.status_code == 422, private.text
    assert "public" in private.json()["detail"]
    with psycopg.connect(pg_conninfo) as conn:
        assert conn.execute("SELECT count(*) FROM operations").fetchone() == (0,)


def test_a_checkout_the_render_worker_is_reading_is_not_removed(
    lib_client: TestClient,
    paths: DataPaths,
    pg_conninfo: str,
    upstream: tuple[str, dict[str, str]],
) -> None:
    """#872: the worker's lease, taken on its own gate and pool, refuses the API's
    removal."""
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    checkout = paths.libraries / "BOSL2" / commits["v1"]
    assert (
        lib_client.delete(f"/api/v1/models/{SLUG}/libraries/BOSL2", headers=press()).status_code
        == 200
    )
    worker_pool = open_pg_pool(pg_conninfo, size=1)
    try:
        worker = CheckoutLeases(worker_pool, paths.libraries)
        job_id = "c" * 32
        token = worker.take(job_id, [checkout])
        refused = lib_client.delete("/api/v1/libraries/BOSL2", headers=press())
        worker.drop(token)
    finally:
        worker_pool.close()
    after = lib_client.delete("/api/v1/libraries/BOSL2", headers=press())

    assert refused.status_code == 409, refused.text
    assert refused.json()["jobs"] == [job_id]
    assert after.status_code == 204, after.text
    assert not checkout.exists()


def test_a_lease_elsewhere_does_not_block_a_removal(
    lib_client: TestClient,
    libraries_app: FastAPI,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
) -> None:
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    pin(lib_client, "BOSL2", ref="v2")
    state: AppState = getattr(libraries_app.state, STATE_ATTR)
    state.checkouts.hold("b" * 32, [paths.libraries / "BOSL2" / commits["v2"]])

    removed = lib_client.delete(
        "/api/v1/libraries/BOSL2", params={"commit": commits["v1"]}, headers=press()
    )

    assert removed.status_code == 204, removed.text


# ── sweeping checkouts nothing pins (#271) ────────────────────────────────────


def _fake_checkout(paths: DataPaths, name: str, commit: str, *, old: bool = True) -> Path:
    checkout = paths.libraries / name / commit
    (checkout / name).mkdir(parents=True)
    if old:
        _age(checkout)
    return checkout


def test_the_sweep_keeps_every_checkout_any_revision_pins(
    lib_client: TestClient,
    libraries_app: FastAPI,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    tmp_path: Path,
) -> None:
    _, commits = upstream
    other_url, other = make_library_upstream(tmp_path / "other", {"x1": "cube(1);\n"})
    create_model(lib_client)
    create_model(lib_client, "gadget")
    old_revision = pin(lib_client, "BOSL2")["version"]  # v1: only in the history after
    pin(lib_client, "BOSL2", ref="v2")  # v2: pinned live
    # MCAD: pinned only by a model that has since been deleted.
    pin(lib_client, "MCAD", "gadget", url=other_url, ref="x1")
    assert lib_client.delete("/api/v1/models/gadget").status_code == 204
    for entry in lib_client.get("/api/v1/libraries/installed").json():
        _age(paths.libraries / entry["name"] / entry["commit"])
    unpinned = _fake_checkout(paths, "BOSL2", "c" * 40)
    just_cloned = _fake_checkout(paths, "BOSL2", "d" * 40, old=False)
    state: AppState = getattr(libraries_app.state, STATE_ATTR)

    # A gate of its own: the app's is bound to the TestClient's loop.
    removed = asyncio.run(sweep_library_checkouts(replace(state, checkouts=CheckoutGate())))

    assert removed == [f"BOSL2@{'c' * 40}"]
    assert not unpinned.exists()
    assert just_cloned.is_dir()
    assert sorted(commit for _, commit in state.libraries.installed()) == sorted(
        [commits["v1"], commits["v2"], other["x1"], "d" * 40]
    )
    # Restoring the old revision needs nothing fetched: its checkout was kept.
    restored = lib_client.post(f"/api/v1/models/{SLUG}/versions/{old_revision}/restore")
    assert restored.status_code == 200, restored.text


def test_the_sweep_keeps_a_checkout_a_live_edit_names(
    lib_client: TestClient,
    libraries_app: FastAPI,
    paths: DataPaths,
) -> None:
    """Named by a hand-edited entry with no commit, uncommitted: every checkout of
    it stays."""
    create_model(lib_client)
    meta = json.loads(paths.model_meta(SLUG).read_text(encoding="utf-8"))
    paths.model_meta(SLUG).write_text(
        json.dumps({**meta, "libraries": [{"name": "BOSL2"}]}), encoding="utf-8"
    )
    kept = _fake_checkout(paths, "BOSL2", "c" * 40)
    state: AppState = getattr(libraries_app.state, STATE_ATTR)

    removed = asyncio.run(sweep_library_checkouts(replace(state, checkouts=CheckoutGate())))

    assert removed == []
    assert kept.is_dir()


def test_boot_sweeps_checkouts_no_revision_pins(app: FastAPI, paths: DataPaths) -> None:
    history: ModelHistory = getattr(app.state, STATE_ATTR).history
    pinned = {"name": "BOSL2", "url": "https://x.invalid/b.git", "ref": "v1", "commit": "a" * 40}
    paths.model_dir(SLUG).mkdir(parents=True)
    paths.model_source(SLUG).write_text(SOURCE, encoding="utf-8")
    paths.model_meta(SLUG).write_text(
        json.dumps({"name": "Widget", "libraries": [pinned]}), encoding="utf-8"
    )
    history.ensure_repo()  # records it as the first revision
    paths.model_meta(SLUG).write_text(json.dumps({"name": "Widget"}), encoding="utf-8")
    assert history.commit("unpin", SLUG) is not None
    in_history = _fake_checkout(paths, "BOSL2", "a" * 40)
    unpinned = _fake_checkout(paths, "BOSL2", "b" * 40)

    with TestClient(app):
        pass

    assert in_history.is_dir()
    assert not unpinned.exists()


def test_the_sweep_keeps_a_checkout_an_old_builtin_revision_pins(
    app: FastAPI, paths: DataPaths
) -> None:
    """A built-in's model.json is two levels deep (`_builtin/<slug>/model.json`);
    restoring its old revision must not need a checkout the sweep removed."""
    state: AppState = getattr(app.state, STATE_ATTR)
    builtin = "builtin:gizmo"
    pinned = {"name": "BOSL2", "url": "https://x.invalid/b.git", "ref": "v1", "commit": "a" * 40}
    paths.model_dir(builtin).mkdir(parents=True)
    paths.model_source(builtin).write_text(SOURCE, encoding="utf-8")
    paths.model_meta(builtin).write_text(
        json.dumps({"name": "Gizmo", "libraries": [pinned]}), encoding="utf-8"
    )
    state.history.ensure_repo()  # records it as the first revision
    paths.model_meta(builtin).write_text(json.dumps({"name": "Gizmo"}), encoding="utf-8")
    assert state.history.commit("unpin", model_path(builtin)) is not None
    in_history = _fake_checkout(paths, "BOSL2", "a" * 40)
    unpinned = _fake_checkout(paths, "BOSL2", "b" * 40)

    removed = asyncio.run(sweep_library_checkouts(replace(state, checkouts=CheckoutGate())))

    assert removed == [f"BOSL2@{'b' * 40}"]
    assert in_history.is_dir()
    assert not unpinned.exists()


def test_a_sweep_that_cannot_read_the_history_removes_nothing(
    app: FastAPI, paths: DataPaths, caplog: pytest.LogCaptureFixture
) -> None:
    unpinned = _fake_checkout(paths, "BOSL2", "b" * 40)

    with (
        patch.object(ModelHistory, "object_ids_in", side_effect=GitError("git log failed")),
        TestClient(app) as client,
    ):
        assert client.get("/healthz").status_code == 200

    assert unpinned.is_dir()
    assert "could not sweep library checkouts" in caplog.text


# ── the upgrade flow's building blocks (#169) ────────────────────────────────


def test_a_librarys_users_are_the_models_that_pin_it_now(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]], tmp_path: Path
) -> None:
    url, commits = upstream
    other, other_commits = make_library_upstream(tmp_path / "other", {"v1": "sphere(1);\n"})
    for slug in (SLUG, "gadget", "plain", "elsewhere", "broken", "garbled"):
        create_model(lib_client, slug)
    pin(lib_client, "BOSL2")
    pin(lib_client, "BOSL2", "gadget", ref="v1")
    pin(lib_client, "BOSL2", "gadget", ref="v2")  # only the current pin counts
    pin(lib_client, "Other", "elsewhere", url=other, ref="v1")
    pin(lib_client, "BOSL2", "broken")
    meta_path = paths.model_meta("broken")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["libraries"][0]["commit"] = "HEAD"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    paths.model_meta("garbled").write_text('{"libraries": ["BOSL2"', encoding="utf-8")

    users = lib_client.get("/api/v1/libraries/BOSL2/users")
    others = lib_client.get("/api/v1/libraries/Other/users")
    nobody = lib_client.get("/api/v1/libraries/MCAD/users")

    assert users.status_code == 200, users.text
    assert sorted(users.json(), key=lambda user: user["slug"]) == [
        # A hand-edited entry is still a user, as a removal would count it.
        {"slug": "broken", "url": None, "ref": None, "commit": None},
        {"slug": "gadget", "url": url, "ref": "v2", "commit": commits["v2"]},
        # So is a model.json that is not JSON but names the library.
        {"slug": "garbled", "url": None, "ref": None, "commit": None},
        {"slug": SLUG, "url": url, "ref": "v1", "commit": commits["v1"]},
    ]
    assert others.json() == [
        {"slug": "elsewhere", "url": other, "ref": "v1", "commit": other_commits["v1"]}
    ]
    assert nobody.status_code == 200
    assert nobody.json() == []


def check_candidate(client: TestClient, name: str, slug: str = SLUG, **body: Any) -> httpx.Response:
    response: httpx.Response = client.post(
        f"/api/v1/models/{slug}/libraries/{name}/check", json=body
    )
    return response


def test_a_candidate_is_checked_without_moving_the_pin(
    lib_client: TestClient,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    tmp_path: Path,
) -> None:
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    before = lib_client.get(f"/api/v1/models/{SLUG}").json()
    meta_before = paths.model_meta(SLUG).read_bytes()
    log = tmp_path / "openscadpath.log"
    set_fake_env(tmp_path, "FAKE_OPENSCAD_PATH_LOG", str(log))

    response = check_candidate(lib_client, "BOSL2", ref="v2")

    assert response.status_code == 200, response.text
    checked = response.json()
    assert (checked["ref"], checked["commit"]) == ("v2", commits["v2"])
    assert checked["ok"] is True
    assert set(checked) >= {"ok", "checked", "timed_out", "diagnostics", "log_tail", "parameters"}
    # OpenSCAD saw the candidate, not the pin.
    assert log.read_text(encoding="utf-8").splitlines() == [
        str(paths.libraries / "BOSL2" / commits["v2"])
    ]
    # Nothing recorded: the same pin, the same revision, the same model.json.
    assert lib_client.get(f"/api/v1/models/{SLUG}").json() == before
    assert paths.model_meta(SLUG).read_bytes() == meta_before


def test_a_candidate_defaults_to_the_catalogue_ref(
    lib_client: TestClient, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream
    create_model(lib_client)
    pin(lib_client, "BOSL2", ref="v2")

    response = lib_client.post(f"/api/v1/models/{SLUG}/libraries/BOSL2/check")

    assert response.status_code == 200, response.text
    assert (response.json()["ref"], response.json()["commit"]) == ("v1", commits["v1"])
    assert lib_client.get(f"/api/v1/models/{SLUG}").json()["libraries"][0]["ref"] == "v2"


def test_a_candidate_ref_that_does_not_exist_is_the_pin_routes_502(
    lib_client: TestClient,
) -> None:
    create_model(lib_client)
    pin(lib_client, "BOSL2")

    checked = check_candidate(lib_client, "BOSL2", ref="v9")
    pinned = lib_client.put(
        f"/api/v1/models/{SLUG}/libraries/BOSL2", json={"ref": "v9"}, headers=press()
    )

    assert checked.status_code == pinned.status_code == 502
    assert checked.json()["detail"] == pinned.json()["detail"]
    assert lib_client.get(f"/api/v1/models/{SLUG}").json()["libraries"][0]["ref"] == "v1"


def test_a_candidate_check_is_refused_before_a_clone(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    create_model(lib_client)
    store = libraries_app.dependency_overrides[get_libraries]()
    with patch.object(store, "resolve", side_effect=AssertionError("reached the service")):
        unknown = check_candidate(lib_client, "BOSL2", "nothing-here", ref="v2")
        undeclared = check_candidate(lib_client, "BOSL2", ref="v2")
        dot_dot = check_candidate(lib_client, "BOSL2", ref="v1..v2")

    assert unknown.status_code == 404
    assert "no model named" in unknown.json()["detail"]
    assert undeclared.status_code == 404
    assert "does not declare" in undeclared.json()["detail"]
    assert dot_dot.status_code == 422


def test_a_built_in_can_be_checked_against_a_candidate(
    lib_client: TestClient, paths: DataPaths, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream
    builtin = "builtin:gizmo"
    pinned = {"name": "BOSL2", "url": url, "ref": "v1", "commit": commits["v1"]}
    paths.model_dir(builtin).mkdir(parents=True)
    paths.model_source(builtin).write_text(SOURCE, encoding="utf-8")
    meta = json.dumps({"name": "Gizmo", "libraries": [pinned]})
    paths.model_meta(builtin).write_text(meta, encoding="utf-8")

    response = check_candidate(lib_client, "BOSL2", builtin, ref="v2")

    assert response.status_code == 200, response.text
    assert response.json()["commit"] == commits["v2"]
    assert paths.model_meta(builtin).read_text(encoding="utf-8") == meta


def test_a_candidate_check_holds_off_removals(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    state: AppState = getattr(libraries_app.state, STATE_ATTR)
    held: list[int] = []
    real = check_source

    async def spy(*args: Any, **kwargs: Any) -> Any:
        held.append(state.checkouts._pins)
        return await real(*args, **kwargs)

    with patch.object(libraries_api, "check_source", spy):
        response = check_candidate(lib_client, "BOSL2", ref="v2")

    assert response.status_code == 200, response.text
    assert held == [1]
    assert state.checkouts._pins == 0


# ── include/use resolution (#253) ────────────────────────────────────────────────


def dependencies(client: TestClient, slug: str = SLUG, **body: Any) -> dict[str, Any]:
    response = client.post(f"/api/v1/models/{slug}/dependencies", json=body or None)
    assert response.status_code == 200, response.text
    report: dict[str, Any] = response.json()
    return report


def test_an_unpinned_library_is_unresolved_with_the_catalogue_one_suggested(
    lib_client: TestClient, upstream: tuple[str, dict[str, str]]
) -> None:
    url, _ = upstream
    create_model(lib_client)

    report = dependencies(lib_client)

    [entry] = report["includes"]
    assert (entry["kind"], entry["target"], entry["status"]) == (
        "use",
        "BOSL2/std.scad",
        "unresolved",
    )
    assert entry["suggestion"]["name"] == "BOSL2"
    assert entry["suggestion"]["source"] == "catalogue"
    assert entry["suggestion"]["url"] == url
    assert entry["suggestion"]["ref"] == "v1"
    assert report["unresolved"] == 1


def test_once_pinned_the_library_resolves_into_its_checkout(lib_client: TestClient) -> None:
    create_model(lib_client)
    pin(lib_client, "BOSL2")

    report = dependencies(lib_client)

    [entry] = report["includes"]
    assert (entry["status"], entry["path"], entry["library"]) == (
        "resolved",
        "BOSL2/std.scad",
        "BOSL2",
    )
    assert report["unresolved"] == 0
    assert report["missing_checkouts"] == []


def test_an_unsaved_source_is_read_against_the_models_directory_and_pins(
    lib_client: TestClient, paths: DataPaths
) -> None:
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    paths.model_dir(SLUG).joinpath("helper.scad").write_text("", encoding="utf-8")

    report = dependencies(
        lib_client,
        source="include <helper.scad>\ninclude <BOSL2/nope.scad>\nuse <other/x.scad>\n",
    )

    helper, nope, other = report["includes"]
    assert (helper["status"], helper["path"]) == ("resolved", "helper.scad")
    assert nope["status"] == "unresolved"
    assert "has no nope.scad" in nope["reason"]
    assert other["status"] == "unresolved"
    assert other["suggestion"] is None
    # The saved source is untouched.
    assert paths.model_source(SLUG).read_text(encoding="utf-8") == SOURCE


def test_a_font_the_image_does_not_have_is_reported(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    class Fonts:
        def resolvable(self) -> set[str]:
            return {"dejavusans"}

    libraries_app.dependency_overrides[get_fonts] = lambda: Fonts()
    create_model(lib_client)

    report = dependencies(
        lib_client, source='text("a", font="DejaVu Sans");\ntext("b", font="Pacifico");\n'
    )

    assert [(f["font"], f["missing"]) for f in report["fonts"]] == [
        ("DejaVu Sans", []),
        ("Pacifico", ["Pacifico"]),
    ]
    assert report["fonts_checked"] is True


def test_a_target_outside_the_models_directory_says_nothing_about_the_filesystem(
    lib_client: TestClient, paths: DataPaths
) -> None:
    """Review of #740: a `../` target that exists and one that does not answer alike."""
    create_model(lib_client)
    (paths.models / "neighbour.scad").write_text("", encoding="utf-8")

    report = dependencies(
        lib_client, source="include <../neighbour.scad>\ninclude <../nothing.scad>\n"
    )

    present, absent = report["includes"]
    assert present["status"] == absent["status"] == "unresolved"
    assert present["path"] is absent["path"] is None
    assert present["reason"] == absent["reason"]


def test_the_pin_index_answers_as_library_pins_does(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    catalogue = getattr(libraries_app.state, STATE_ATTR).catalogue

    index = catalogue.library_pin_index()

    assert index["BOSL2"] == [
        (slug, found) for slug, found in catalogue.library_pins("BOSL2") if found is not None
    ]


def test_dependency_reports_wait_for_a_permit(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    """Review of #740: a report runs in a worker thread only while it holds a permit."""
    create_model(lib_client)
    permits = getattr(libraries_app.state, STATE_ATTR).dependency_checks
    held: list[int] = []
    real = resolve_dependencies

    def spy(*args: Any, **kwargs: Any) -> Any:
        held.append(permits._value)
        return real(*args, **kwargs)

    with patch.object(libraries_api, "resolve_dependencies", spy):
        dependencies(lib_client)

    assert held == [DEPENDENCY_CHECK_CONCURRENCY - 1]
    assert permits._value == DEPENDENCY_CHECK_CONCURRENCY


def test_resolving_the_dependencies_of_a_model_that_does_not_exist_is_a_404(
    lib_client: TestClient,
) -> None:
    assert lib_client.post("/api/v1/models/nope/dependencies").status_code == 404


def _commits(app: FastAPI, slug: str = SLUG) -> int:
    state: AppState = getattr(app.state, STATE_ATTR)
    counted = subprocess.run(
        [GIT, "-C", str(state.paths.model_dir(slug)), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        env=git_env(),
    )
    return int(counted.stdout)


def test_a_repeated_pin_answers_the_recorded_model_without_a_second_commit(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    """§4.2: a re-send of the same press (a lost answer) never commits twice."""
    create_model(lib_client)
    key = uuid.uuid4().hex
    first = lib_client.put(
        f"/api/v1/models/{SLUG}/libraries/BOSL2", json={}, headers={"Idempotency-Key": key}
    )
    commits = _commits(libraries_app)
    again = lib_client.put(
        f"/api/v1/models/{SLUG}/libraries/BOSL2", json={}, headers={"Idempotency-Key": key}
    )
    assert first.status_code == again.status_code == 200, again.text
    assert again.json() == first.json()
    assert _commits(libraries_app) == commits


def test_a_refused_removal_keeps_its_models_extension(lib_client: TestClient) -> None:
    create_model(lib_client)
    pin(lib_client, "BOSL2")
    refused = lib_client.delete("/api/v1/libraries/BOSL2", headers=press())
    assert refused.status_code == 409, refused.text
    assert refused.json()["models"] == [SLUG]


def test_a_slow_pin_answers_202_and_its_operation_ends_with_the_model(
    lib_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clone past the answer deadline never holds the request (§4.2 step 4)."""
    create_model(lib_client)
    monkeypatch.setattr(
        operations_api, "start_command", partial(start_command, deadline=timedelta(seconds=1))
    )
    resolve = library_pins.resolve_pin

    async def slowly(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3)
        return await resolve(*args, **kwargs)

    monkeypatch.setattr(library_operations, "resolve_pin", slowly)
    started = lib_client.put(f"/api/v1/models/{SLUG}/libraries/BOSL2", json={}, headers=press())
    assert started.status_code == 202, started.text
    deadline = time.monotonic() + 60
    op = started.json()
    while op["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.2)
        op = lib_client.get(f"/api/v1/operations/{op['id']}").json()
    assert op["status"] == "succeeded", op
    assert [entry["name"] for entry in op["result"]["libraries"]] == ["BOSL2"]


def test_a_pin_on_a_broken_model_json_is_its_409_not_an_unexpected_500(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    """Review I1: the run answers a model.json it cannot read as every route does."""
    create_model(lib_client)
    state: AppState = getattr(libraries_app.state, STATE_ATTR)
    (state.paths.model_dir(SLUG) / "model.json").write_text("{not json", encoding="utf-8")
    refused = lib_client.put(f"/api/v1/models/{SLUG}/libraries/BOSL2", json={}, headers=press())
    assert refused.status_code == 409, refused.text
    assert refused.json()["title"] == "Invalid Model Metadata"


#: The library kinds read no component.
_NO_COMPONENTS = cast(Components, SimpleNamespace())


def test_a_pin_may_run_longer_than_its_clone() -> None:
    """Review I2: the run outlives the clone's own limit, so a slow clone is never
    recorded failed while it goes on to commit."""
    kinds = library_operations.library_kinds(cast(AppState, SimpleNamespace()), _NO_COMPONENTS)
    for name in ("library_pin", "library_repin"):
        timeout = next(kind for kind in kinds if kind.name == name).run_timeout
        assert timeout is not None and timeout.total_seconds() > CLONE_TIMEOUT


def _raising_state(error: Exception) -> Any:
    def raise_it(*args: Any, **kwargs: Any) -> Any:
        raise error

    return SimpleNamespace(
        catalogue=SimpleNamespace(unpin_library=raise_it, library_users=raise_it),
        libraries=SimpleNamespace(paths=SimpleNamespace(libraries=Path("/nowhere"))),
        checkouts=CheckoutGate(),
    )


@pytest.mark.parametrize(
    ("error", "title"),
    [
        (InvalidModelMetaError("w", "not JSON"), "Invalid Model Metadata"),
        (LibraryDeclarationError("bad entry"), "Invalid Library Declaration"),
        (LibraryNotInstalledError("not on the volume"), "Conflict"),
    ],
)
@pytest.mark.parametrize("kind", ["library_unpin", "library_remove"])
async def test_a_runs_unreadable_declaration_is_the_routes_409(
    kind: str, error: Exception, title: str
) -> None:
    """Review #1119 3: what the app's handlers answered 409 under the routes, the
    runs answer 409 too, not the operation's unexpected 500."""
    kinds = library_operations.library_kinds(_raising_state(error), _NO_COMPONENTS)
    run = next(each for each in kinds if each.name == kind).run
    request = {"slug": "w", "name": "BOSL2", "index": 0, "commit": None}
    with pytest.raises(ApiError) as raised:
        await run(request, {})
    assert (raised.value.status, raised.value.title) == (409, title)


def test_a_pin_on_a_model_with_a_thumbnail_answers_its_record(lib_client: TestClient) -> None:
    """The run's record is read back as the route's answer: a model with media must
    survive that (its items are views, not model.json entries)."""
    create_model(lib_client)
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    put = lib_client.put(
        f"/api/v1/models/{SLUG}/thumbnail", files={"file": ("t.png", png, "image/png")}
    )
    assert put.status_code == 200, put.text
    pinned = pin(lib_client, "BOSL2")
    assert [item["id"] for item in pinned["media"]] == ["thumbnail"]
