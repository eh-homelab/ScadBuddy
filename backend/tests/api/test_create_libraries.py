"""Declaring libraries at create time (#169): a paste or upload naming curated
libraries has them pinned in its first revision and parse-checked against them.

The upstream is the same local bare repository as ``test_libraries.py``'s.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState, get_libraries
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.libraries import LibraryStore
from tests.api import test_libraries as shared
from tests.api.test_libraries import SLUG, SOURCE, create_model, pin

pytestmark = pytest.mark.requires_git

# The library tests' fixtures: the local upstream, and an app whose catalogue is it.
upstream = shared.upstream
libraries_app = shared.libraries_app
lib_client = shared.lib_client


def _versions(client: TestClient, slug: str = SLUG) -> int:
    response = client.get(f"/api/v1/models/{slug}/versions")
    assert response.status_code == 200, response.text
    return len(response.json())


def _store(app: FastAPI) -> LibraryStore:
    store: LibraryStore = app.dependency_overrides[get_libraries]()
    return store


def test_a_paste_pins_its_libraries_in_its_first_revision_and_checks_with_them(
    lib_client: TestClient,
    paths: DataPaths,
    upstream: tuple[str, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, commits = upstream
    log = tmp_path / "openscadpath.log"
    monkeypatch.setenv("FAKE_OPENSCAD_PATH_LOG", str(log))

    created = lib_client.post(
        "/api/v1/models",
        json={"name": "Widget", "source": SOURCE, "libraries": ["BOSL2", "BOSL2"]},
    )

    assert created.status_code == 201, created.text
    pinned = {"name": "BOSL2", "url": url, "ref": "v1", "commit": commits["v1"]}
    assert created.json()["libraries"] == [pinned]
    assert _versions(lib_client) == 1
    # The parse check ran with the checkout, as every render will.
    assert log.read_text(encoding="utf-8").splitlines() == [
        str(paths.libraries / "BOSL2" / commits["v1"])
    ]


def test_an_upload_pins_the_libraries_its_form_names(
    lib_client: TestClient, upstream: tuple[str, dict[str, str]]
) -> None:
    _, commits = upstream

    created = lib_client.post(
        "/api/v1/models",
        files={"file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream")},
        data={"libraries": ["BOSL2"]},
    )

    assert created.status_code == 201, created.text
    assert [(lib["name"], lib["commit"]) for lib in created.json()["libraries"]] == [
        ("BOSL2", commits["v1"])
    ]
    assert _versions(lib_client) == 1


def test_a_create_naming_a_library_outside_the_catalogue_clones_nothing(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    with patch.object(_store(libraries_app), "resolve") as resolve:
        refused = lib_client.post(
            "/api/v1/models",
            json={"name": "Widget", "source": SOURCE, "libraries": ["BOSL2", "NopeSCAD"]},
        )

    assert refused.status_code == 422, refused.text
    assert refused.json()["libraries"] == ["NopeSCAD"]
    resolve.assert_not_called()
    assert lib_client.get(f"/api/v1/models/{SLUG}").status_code == 404


def test_a_create_naming_something_that_is_not_a_library_name_is_a_422(
    lib_client: TestClient,
) -> None:
    refused = lib_client.post(
        "/api/v1/models",
        json={"name": "Widget", "source": SOURCE, "libraries": ["../etc"]},
    )

    assert refused.status_code == 422, refused.text
    assert lib_client.get(f"/api/v1/models/{SLUG}").status_code == 404


def test_a_create_whose_library_cannot_be_fetched_is_a_502_and_creates_nothing(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    store = _store(libraries_app)
    store.catalogue["BOSL2"] = store.catalogue["BOSL2"].model_copy(update={"ref": "v9"})

    refused = lib_client.post(
        "/api/v1/models", json={"name": "Widget", "source": SOURCE, "libraries": ["BOSL2"]}
    )

    assert refused.status_code == 502, refused.text
    assert lib_client.get(f"/api/v1/models/{SLUG}").status_code == 404


def test_a_dropped_model_json_pin_wins_over_a_named_library(
    lib_client: TestClient, libraries_app: FastAPI, upstream: tuple[str, dict[str, str]]
) -> None:
    url, commits = upstream
    # v2 on the volume, from another model's pin.
    create_model(lib_client, "gadget")
    pin(lib_client, "BOSL2", "gadget", ref="v2")
    pinned = {"name": "BOSL2", "url": url, "ref": "v2", "commit": commits["v2"]}

    with patch.object(_store(libraries_app), "resolve") as resolve:
        created = lib_client.post(
            "/api/v1/models",
            files={
                "file": (f"{SLUG}.scad", SOURCE.encode(), "application/octet-stream"),
                "meta": (
                    "model.json",
                    json.dumps({"libraries": [pinned]}).encode(),
                    "application/json",
                ),
            },
            data={"libraries": ["BOSL2"]},
        )

    assert created.status_code == 201, created.text
    assert created.json()["libraries"] == [pinned]
    resolve.assert_not_called()


def test_a_create_holds_the_checkout_gate_until_it_has_recorded_the_pin(
    lib_client: TestClient, libraries_app: FastAPI
) -> None:
    """No removal can delete the checkout between the clone and the create's commit."""
    state: AppState = getattr(libraries_app.state, STATE_ATTR)
    held: list[int] = []
    real_create = state.catalogue.create

    def create(*args: Any, **kwargs: Any) -> Any:
        held.append(state.checkouts._pins)
        return real_create(*args, **kwargs)

    with patch.object(state.catalogue, "create", side_effect=create):
        created = lib_client.post(
            "/api/v1/models", json={"name": "Widget", "source": SOURCE, "libraries": ["BOSL2"]}
        )

    assert created.status_code == 201, created.text
    assert held == [1]
    assert state.checkouts._pins == 0


def test_a_create_without_libraries_takes_no_pin(lib_client: TestClient) -> None:
    assert create_model(lib_client)["libraries"] == []
