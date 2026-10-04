"""A model's edits as commands on ``library`` (#1054, phase 3d): source saves, sidecars,
sibling files, restore and upstream actions run as operations, with large bytes carried
by claim check."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import models as models_api
from scadbuddy.api import operations as operations_api
from scadbuddy.workflows.commands import start_command
from tests.api.test_model_operations import _commits, _history_bytes, _workflow_ids

SOURCE = "cube(10);\n"


def _model(client: TestClient, name: str) -> tuple[str, str]:
    """A new model's slug and version."""
    created = client.post("/api/v1/models", json={"name": name, "source": SOURCE})
    assert created.status_code == 201, created.text
    return created.json()["slug"], created.json()["version"]


def test_a_repeated_source_save_makes_one_revision(client: TestClient, app: FastAPI) -> None:
    slug, _ = _model(client, "Saved")
    key = uuid.uuid4().hex
    body = {"source": "cube(11);\n"}
    first = client.put(f"/api/v1/models/{slug}/source", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 200, first.text
    commits = _commits(app)
    again = client.put(f"/api/v1/models/{slug}/source", json=body, headers={"Idempotency-Key": key})
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert _commits(app) == commits
    assert _workflow_ids(app, "model_source_put")


def test_a_large_source_save_goes_by_claim(client: TestClient, app: FastAPI) -> None:
    slug, _ = _model(client, "Big")
    source = "// " + "x" * 900_000 + "\ncube(1);\n"
    saved = client.put(f"/api/v1/models/{slug}/source?force=true", json={"source": source})
    assert saved.status_code == 200, saved.text
    assert client.get(f"/api/v1/models/{slug}/source").text == source
    (workflow_id,) = _workflow_ids(app, "model_source_put")
    assert _history_bytes(app, workflow_id) < 100_000


def test_a_stale_base_is_a_409_with_current(client: TestClient) -> None:
    slug, version = _model(client, "Stale")
    moved = client.put(f"/api/v1/models/{slug}/source", json={"source": "cube(2);\n"})
    assert moved.status_code == 200, moved.text
    stale = client.put(
        f"/api/v1/models/{slug}/source", json={"source": "cube(3);\n", "base": version}
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["base"] == version
    assert stale.json()["current"] == moved.json()["version"]


def test_a_patch_with_large_edits_goes_by_claim(client: TestClient, app: FastAPI) -> None:
    slug, version = _model(client, "Patched")
    replace = "cube(10); // " + "y" * 300_000
    patched = client.post(
        f"/api/v1/models/{slug}/source/patch",
        json={
            "base": version,
            "edits": [{"search": "cube(10);", "replace": replace}],
            "force": True,
        },
    )
    assert patched.status_code == 200, patched.text
    assert client.get(f"/api/v1/models/{slug}/source").text == replace + "\n"
    (workflow_id,) = _workflow_ids(app, "model_source_patch")
    assert _history_bytes(app, workflow_id) < 100_000


def test_a_slow_save_answers_202_and_its_operation_ends_with_the_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    slug, _ = _model(client, "Slow Save")
    monkeypatch.setattr(
        operations_api, "start_command", partial(start_command, deadline=timedelta(seconds=1))
    )
    save = models_api._save_source

    async def slowly(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3)
        return await save(*args, **kwargs)

    monkeypatch.setattr(models_api, "_save_source", slowly)
    started = client.put(f"/api/v1/models/{slug}/source", json={"source": "cube(4);\n"})
    assert started.status_code == 202, started.text
    op = started.json()
    deadline = time.monotonic() + 60
    while op["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.2)
        op = client.get(f"/api/v1/operations/{op['id']}").json()
    assert op["status"] == "succeeded", op
    assert op["result"]["slug"] == slug


def test_sidecar_and_file_edits_are_operations(client: TestClient, app: FastAPI) -> None:
    slug, _ = _model(client, "Sidecars")
    png = models_api.PNG_MAGIC + b"\0" * 64
    steps: list[tuple[str, Callable[[], Any]]] = [
        (
            "model_thumbnail_put",
            lambda: client.put(
                f"/api/v1/models/{slug}/thumbnail", files={"file": ("t.png", png, "image/png")}
            ),
        ),
        ("model_thumbnail_delete", lambda: client.delete(f"/api/v1/models/{slug}/thumbnail")),
        (
            "model_readme_put",
            lambda: client.put(f"/api/v1/models/{slug}/readme", json={"content": "# Hi\n"}),
        ),
        ("model_readme_delete", lambda: client.delete(f"/api/v1/models/{slug}/readme")),
        (
            "model_file_put",
            lambda: client.put(
                f"/api/v1/models/{slug}/files/part.scad", json={"content": "module p() {}\n"}
            ),
        ),
        ("model_file_delete", lambda: client.delete(f"/api/v1/models/{slug}/files/part.scad")),
    ]
    for kind, call in steps:
        answered = call()
        assert answered.status_code == 200, (kind, answered.text)
        assert answered.json()["slug"] == slug
        assert _workflow_ids(app, kind), kind


def test_removing_a_missing_readme_is_still_404(client: TestClient) -> None:
    slug, _ = _model(client, "No Readme")
    response = client.delete(f"/api/v1/models/{slug}/readme")
    assert response.status_code == 404, response.text
    assert "has no README" in response.json()["detail"]
