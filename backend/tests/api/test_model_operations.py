"""A model's lifecycle as commands on ``library`` (#1054, phase 3c): create, import,
patch, duplicate and delete run as operations, with the request's large bytes carried
by claim check (``operations/claims.py``)."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
from scadbuddy.library import scad
from scadbuddy.library.catalogue import MAX_DESCRIPTION_CHARS, MAX_TAG_CHARS, MAX_TAGS
from scadbuddy.library.history import GIT, git_env
from scadbuddy.library.presets import MAX_PRESETS
from scadbuddy.operations.component import OPERATIONS
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
    pool = _state(app).components.get(OPERATIONS).store._require()
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


def test_a_repeated_upload_answers_the_model_it_made(client: TestClient, app: FastAPI) -> None:
    """Review 3c I1: the multipart re-send of a create whose answer was lost gets that
    model, not a 409 for the slug it took."""
    key = uuid.uuid4().hex

    def upload() -> Any:
        return client.post(
            "/api/v1/models",
            files={"file": ("uploaded_once.scad", SOURCE.encode(), "text/plain")},
            headers={"Idempotency-Key": key},
        )

    first = upload()
    assert first.status_code == 201, first.text
    commits = _commits(app)
    again = upload()
    assert again.status_code == 201, again.text
    assert again.json() == first.json()
    assert _commits(app) == commits


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


def test_a_request_too_large_for_history_is_refused_before_any_operation(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 3c I2: a field that is not a claim is still bounded, so Temporal never
    refuses the input as a 503 that a retry repeats."""
    created = client.post("/api/v1/models", json={"name": "Wordy", "source": SOURCE})
    assert created.status_code == 201, created.text
    slug = created.json()["slug"]
    monkeypatch.setattr(operations_api, "MAX_REQUEST_BYTES", 1000)
    response = client.patch(f"/api/v1/models/{slug}", json={"description": "x" * 2000})
    assert response.status_code == 413, response.text
    assert _workflow_ids(app, "model_patch") == []


@pytest.mark.parametrize(
    "body",
    [
        {"description": "x" * (MAX_DESCRIPTION_CHARS + 1)},
        {"tags": ["t"] * (MAX_TAGS + 1)},
        {"tags": ["x" * (MAX_TAG_CHARS + 1)]},
    ],
)
def test_a_description_or_tags_past_their_caps_are_a_422_naming_the_field(
    client: TestClient, model: str, body: dict[str, Any]
) -> None:
    """Review #1126 1.4: inside the inline cap, so the 413 is never what they meet."""
    (field,) = body
    capped = {"name": "Capped", "source": SOURCE, **body}
    responses = [
        client.patch(f"/api/v1/models/{model}", json=body),
        client.post("/api/v1/models", json=capped),
        client.post(
            "/api/v1/models",
            files={"file": ("capped.scad", SOURCE.encode(), "text/plain")},
            data={field: body[field] if field == "description" else json.dumps(body[field])},
        ),
    ]
    for response in responses:
        assert response.status_code == 422, response.text
        assert field in response.text
    worst = {
        "name": "n" * 200,
        "description": "\u00e9" * MAX_DESCRIPTION_CHARS,
        "tags": ["\u00e9" * MAX_TAG_CHARS] * MAX_TAGS,
    }
    assert len(json.dumps(worst).encode()) < operations_api.MAX_REQUEST_BYTES


def _claims(app: FastAPI) -> set[str]:
    root = _state(app).paths.claims
    return {path.name for path in root.iterdir()} if root.exists() else set()


def test_a_full_list_of_large_presets_is_patched_by_claim(
    client: TestClient, model: str, app: FastAPI
) -> None:
    """Review 3c 1.1: MAX_PRESETS presets with a few text values each are far past the
    inline cap, and still a valid edit."""
    presets = [
        {
            "name": f"Preset {index}",
            "params": {"width": index, "label": f"{index} " + "x" * 500},
            "description": "d" * 150,
            "tags": ["tag"],
        }
        for index in range(MAX_PRESETS)
    ]
    assert len(json.dumps(presets)) > operations_api.MAX_REQUEST_BYTES
    response = client.patch(f"/api/v1/models/{model}", json={"presets": presets})
    assert response.status_code == 200, response.text
    written = json.loads(_state(app).paths.model_meta(model).read_text(encoding="utf-8"))
    assert len(written["presets"]) == MAX_PRESETS


def test_a_refused_keyed_upload_writes_no_claims(client: TestClient, app: FastAPI) -> None:
    """Review 3c 1.2, 1.3: a keyed upload over a taken slug is refused before its
    parts are claimed, and starts nothing."""
    taken = client.post("/api/v1/models", json={"name": "Taken", "source": SOURCE})
    assert taken.status_code == 201, taken.text
    before = _claims(app)
    refused = client.post(
        "/api/v1/models",
        files={
            "file": ("taken.scad", b"cube(2);\n", "text/plain"),
            "thumbnail": ("t.png", models_api.PNG_MAGIC + b"\0" * 1000, "image/png"),
        },
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert refused.status_code == 409, refused.text
    assert _claims(app) == before == set()
    assert len(_workflow_ids(app, "model_create")) == 1


def test_a_keyed_upload_over_a_taken_slug_is_refused_before_its_parts_are_read(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1126 1.3, 3.2: the browser keys every upload, so the early taken-slug
    refusal (#436) holds for a keyed one too, unless that key's create already ran."""
    taken = client.post("/api/v1/models", json={"name": "Taken", "source": SOURCE})
    assert taken.status_code == 201, taken.text
    decoded: list[bytes] = []
    decode = scad.decode_source

    def recording(raw: bytes) -> str:
        decoded.append(raw)
        return decode(raw)

    monkeypatch.setattr(models_api, "decode_source", recording)
    refused = client.post(
        "/api/v1/models",
        files={"file": ("taken.scad", b"cube(2);\n", "text/plain")},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert refused.status_code == 409, refused.text
    assert decoded == []
    assert _claims(app) == set()


def test_a_request_refused_by_the_cap_drops_its_claims(
    client: TestClient, model: str, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(operations_api, "MAX_REQUEST_BYTES", 1000)
    response = client.patch(
        f"/api/v1/models/{model}",
        json={"description": "x" * 2000, "presets": [{"name": "One"}]},
    )
    assert response.status_code == 413, response.text
    assert _claims(app) == set()


def test_a_finished_create_drops_its_claims_but_not_one_a_running_operation_names(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 3c 1.2: a final answer drops the request's claims, except the bytes an
    operation still running holds by the same name."""
    monkeypatch.setattr(
        operations_api, "start_command", partial(start_command, deadline=timedelta(seconds=1))
    )
    create = models_api._create

    async def slow_for_slow(*args: Any, **kwargs: Any) -> Any:
        if kwargs["slug"] == "slow":
            await asyncio.sleep(3)
        return await create(*args, **kwargs)

    monkeypatch.setattr(models_api, "_create", slow_for_slow)
    slow = client.post("/api/v1/models", json={"name": "Slow", "source": SOURCE})
    assert slow.status_code == 202, slow.text
    quick = client.post(
        "/api/v1/models",
        files={
            "file": ("quick.scad", SOURCE.encode(), "text/plain"),
            "thumbnail": ("t.png", models_api.PNG_MAGIC + b"\0" * 1000, "image/png"),
        },
    )
    assert quick.status_code == 201, quick.text
    assert _claims(app) == {hashlib.sha256(SOURCE.encode()).hexdigest()}
    op = slow.json()
    deadline = time.monotonic() + 60
    while op["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.2)
        op = client.get(f"/api/v1/operations/{op['id']}").json()
    assert op["status"] == "succeeded", op


def test_a_duplicate_made_between_the_check_and_the_run_refuses_the_delete(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 3c 3.1: the run refuses again just before the delete, and the problem it
    records keeps `duplicates` and `slugs`."""
    assert client.post("/api/v1/models", json={"name": "Base", "source": SOURCE}).status_code == 201
    refuse = models_api.refuse_delete
    calls = 0

    def duplicated_after_the_check(*args: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            refuse(*args)
            _state(app).catalogue.duplicate("base", "copy", "Copy")
            return
        refuse(*args)

    monkeypatch.setattr(models_api, "refuse_delete", duplicated_after_the_check)
    key = {"Idempotency-Key": uuid.uuid4().hex}
    refused = client.delete("/api/v1/models/base", headers=key)
    assert refused.status_code == 409, refused.text
    assert calls == 2
    assert refused.json()["slugs"] == ["copy"]
    assert refused.json()["duplicates"] == 1
    again = client.delete("/api/v1/models/base", headers=key)
    assert again.status_code == 409 and again.json() == refused.json()
    assert len(_workflow_ids(app, "model_delete")) == 1
    assert client.get("/api/v1/models/base").status_code == 200
