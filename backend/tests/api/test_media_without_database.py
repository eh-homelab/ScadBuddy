"""Template media (#274) with no rows yet: the list is the legacy ``thumbnail.png``,
or a built-in's bundled media. (The no-database mode these tests once covered is
gone: #467 requires ``SCADBUDDY_DATABASE_URL``.)"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from tests.api.conftest import PNG_BYTES

pytestmark = pytest.mark.requires_git

BUILTIN = "builtin:keychain"


@pytest.fixture
def client(app: FastAPI, seed_dir: Path) -> Iterator[TestClient]:
    directory = seed_dir / "keychain"
    (directory / "media").mkdir(parents=True)
    (directory / "model.scad").write_text("width = 10;\n", encoding="utf-8")
    (directory / "media" / "front.png").write_bytes(PNG_BYTES)
    (directory / "model.json").write_text(
        json.dumps(
            {"name": "Keychain", "media": [{"id": "front", "file": "front.png", "kind": "image"}]}
        ),
        encoding="utf-8",
    )
    with TestClient(app) as test_client:
        yield test_client


def test_the_legacy_thumbnail_is_listed(client: TestClient, model: str, paths: DataPaths) -> None:
    paths.model_dir(model).joinpath("thumbnail.png").write_bytes(PNG_BYTES)

    [item] = client.get(f"/api/v1/models/{model}").json()["media"]

    assert item["id"] == "thumbnail"
    assert client.get(f"/api/v1/models/{model}/media/thumbnail").content == PNG_BYTES


def test_a_built_ins_bundled_media_is_listed(client: TestClient) -> None:
    [item] = client.get(f"/api/v1/models/{BUILTIN}").json()["media"]

    assert item["id"] == "front"
    assert client.get(f"/api/v1/models/{BUILTIN}/media/front").content == PNG_BYTES


def test_the_thumbnail_routes_still_work(client: TestClient, model: str) -> None:
    put = client.put(
        f"/api/v1/models/{model}/thumbnail", files={"file": ("t.png", PNG_BYTES, "image/png")}
    )
    assert put.status_code == 200, put.text
    assert [item["id"] for item in put.json()["media"]] == ["thumbnail"]
    assert client.delete(f"/api/v1/models/{model}/thumbnail").json()["media"] == []
