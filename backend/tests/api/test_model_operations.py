"""A model's lifecycle as commands on ``library`` (#1054, phase 3c): create, import,
patch, duplicate and delete run as operations, with the request's large bytes carried
by claim check (``operations/claims.py``)."""

from __future__ import annotations

import asyncio
import subprocess
import time
import uuid
from datetime import timedelta
from functools import partial
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from temporalio.client import Client

from scadbuddy.api import models as models_api
from scadbuddy.api import operations as operations_api
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.library.history import GIT, git_env
from scadbuddy.workflows.commands import start_command

SOURCE = "cube(10);\n"


def _state(app: FastAPI) -> AppState:
    state: AppState = getattr(app.state, STATE_ATTR)
    return state


def _commits(app: FastAPI) -> int:
    counted = subprocess.run(
        [GIT, "-C", str(_state(app).paths.models), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        env=git_env(),
    )
    return int(counted.stdout)


def _workflow_ids(app: FastAPI, kind: str) -> list[str]:
    pool = _state(app).operations.store._require()
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT workflow_id FROM operations WHERE kind = %s ORDER BY created_at", (kind,)
        ).fetchall()
    return [row["workflow_id"] for row in rows]


def _history_bytes(app: FastAPI, workflow_id: str) -> int:
    settings = _state(app).settings

    async def fetch() -> int:
        client = await Client.connect(settings.temporal_address, namespace="default")
        history = await client.get_workflow_handle(workflow_id).fetch_history()
        return len(history.to_json())

    return asyncio.run(fetch())


def test_a_repeated_create_makes_one_model(client: TestClient, app: FastAPI) -> None:
    """§4.2: a re-send of the same create (a lost answer) never commits twice."""
    key = uuid.uuid4().hex
    body = {"name": "Once", "source": SOURCE}
    first = client.post("/api/v1/models", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 201, first.text
    commits = _commits(app)
    again = client.post("/api/v1/models", json=body, headers={"Idempotency-Key": key})
    assert again.status_code == 201, again.text
    assert again.json() == first.json()
    assert _commits(app) == commits
    assert _workflow_ids(app, "model_create")


def test_a_large_source_goes_by_claim_not_in_history(client: TestClient, app: FastAPI) -> None:
    """Past Temporal's 512 KB payload warning: the source is a claim, never a payload."""
    source = "// " + "x" * 700_000 + "\ncube(1);\n"
    created = client.post(
        "/api/v1/models?force=true",
        content=source.encode(),
        headers={"Content-Type": "text/plain", "X-Model-Name": "Large"},
    )
    assert created.status_code == 201, created.text
    assert client.get("/api/v1/models/large/source").text == source
    (workflow_id,) = _workflow_ids(app, "model_create")
    assert _history_bytes(app, workflow_id) < 100_000


def test_a_thumbnail_goes_by_claim(client: TestClient, app: FastAPI) -> None:
    png = models_api.PNG_MAGIC + b"\0" * 600_000
    created = client.post(
        "/api/v1/models?force=true",
        files={
            "file": ("thumbed.scad", SOURCE.encode(), "application/octet-stream"),
            "thumbnail": ("t.png", png, "image/png"),
        },
    )
    assert created.status_code == 201, created.text
    assert client.get("/api/v1/models/thumbed/thumbnail").content == png
    (workflow_id,) = _workflow_ids(app, "model_create")
    assert _history_bytes(app, workflow_id) < 100_000


def test_a_slow_create_answers_202_and_its_operation_ends_with_the_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        operations_api, "start_command", partial(start_command, deadline=timedelta(seconds=1))
    )
    create = models_api._create

    async def slowly(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(3)
        return await create(*args, **kwargs)

    monkeypatch.setattr(models_api, "_create", slowly)
    started = client.post("/api/v1/models", json={"name": "Slow", "source": SOURCE})
    assert started.status_code == 202, started.text
    op = started.json()
    deadline = time.monotonic() + 60
    while op["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.2)
        op = client.get(f"/api/v1/operations/{op['id']}").json()
    assert op["status"] == "succeeded", op
    assert op["result"]["slug"] == "slow"


def test_a_create_whose_slug_is_taken_is_refused_by_its_check(
    client: TestClient, app: FastAPI
) -> None:
    assert (
        client.post("/api/v1/models", json={"name": "Taken", "source": SOURCE}).status_code == 201
    )
    again = client.post("/api/v1/models", json={"name": "Taken", "source": SOURCE})
    assert again.status_code == 409, again.text
    assert len(_workflow_ids(app, "model_create")) == 1


def test_patch_duplicate_and_delete_are_operations(client: TestClient, app: FastAPI) -> None:
    assert client.post("/api/v1/models", json={"name": "Base", "source": SOURCE}).status_code == 201
    patched = client.patch("/api/v1/models/base", json={"description": "d"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["description"] == "d"
    copied = client.post("/api/v1/models/base/duplicate", json={"name": "Copy"})
    assert copied.status_code == 201, copied.text
    refused = client.delete("/api/v1/models/base")
    assert refused.status_code == 409, refused.text
    assert refused.json()["slugs"] == ["copy"]
    assert client.delete("/api/v1/models/copy").status_code == 204
    assert client.delete("/api/v1/models/base").status_code == 204
    for kind in ("model_patch", "model_duplicate", "model_delete"):
        assert _workflow_ids(app, kind), kind
