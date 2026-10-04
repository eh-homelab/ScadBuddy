"""A model's edits as commands on ``library`` (#1054, phase 3d): source saves, sidecars,
sibling files, restore and upstream actions run as operations, with large bytes carried
by claim check."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import model_files
from scadbuddy.api import models as models_api
from scadbuddy.api import operations as operations_api
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.upstream import MergeConflictError, MergePlan, MergePreview
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.component import OPERATIONS
from scadbuddy.workflows.commands import start_command
from tests.api.test_model_operations import (
    _claims,
    _commits,
    _history_bytes,
    _state,
    _workflow_ids,
)

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


def test_an_edits_final_answer_drops_its_claim(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #1130 1: every edit that goes by claim drops it once its answer is final,
    a refusal included."""
    slug, version = _model(client, "Claims")
    png = models_api.PNG_MAGIC + b"\2" * 64
    answers = [
        (200, client.put(f"/api/v1/models/{slug}/source", json={"source": "cube(5);\n"})),
        (
            409,
            client.put(
                f"/api/v1/models/{slug}/source", json={"source": "cube(6);\n", "base": version}
            ),
        ),
        (
            409,
            client.post(
                f"/api/v1/models/{slug}/source/patch",
                json={"base": version, "edits": [{"search": "cube", "replace": "sphere"}]},
            ),
        ),
        (
            200,
            client.put(
                f"/api/v1/models/{slug}/thumbnail", files={"file": ("t.png", png, "image/png")}
            ),
        ),
        (200, client.put(f"/api/v1/models/{slug}/readme", json={"content": "# Hi\n"})),
        (200, client.put(f"/api/v1/models/{slug}/files/a.scad", json={"content": "a = 1;\n"})),
    ]
    current = client.get(f"/api/v1/models/{slug}").json()["version"]
    answers.append(
        (
            200,
            client.post(
                f"/api/v1/models/{slug}/source/patch",
                json={"base": current, "edits": [{"search": "cube", "replace": "sphere"}]},
            ),
        )
    )
    monkeypatch.setattr(model_files, "MAX_SOURCE_FILES", 1)
    answers.append(
        (422, client.put(f"/api/v1/models/{slug}/files/b.scad", json={"content": "b = 1;\n"}))
    )
    for status, answered in answers:
        assert answered.status_code == status, answered.text
    assert _claims(app) == set()


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


def _operation_ids(app: FastAPI, kind: str) -> list[str]:
    pool = _state(app).components.get(OPERATIONS).store._require()
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id FROM operations WHERE kind = %s ORDER BY created_at", (kind,)
        ).fetchall()
    return [str(row["id"]) for row in rows]


def _follow(client: TestClient, op: dict[str, Any]) -> dict[str, Any]:
    deadline = time.monotonic() + 60
    while op["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.2)
        op = client.get(f"/api/v1/operations/{op['id']}").json()
    return op


@pytest.mark.parametrize("route", ["put", "patch"])
def test_a_stale_base_the_run_finds_after_a_202_carries_current(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """Review 1130 3: the run's refusal under the lock reaches the operation's record
    with `current` among its extensions."""
    slug, version = _model(client, "Held Save")
    monkeypatch.setattr(
        operations_api, "start_command", partial(start_command, deadline=timedelta(seconds=1))
    )
    release = threading.Event()
    save = models_api._save_source

    async def held(*args: Any, **kwargs: Any) -> Any:
        await asyncio.to_thread(release.wait, 60)
        return await save(*args, **kwargs)

    monkeypatch.setattr(models_api, "_save_source", held)
    try:
        if route == "put":
            started = client.put(
                f"/api/v1/models/{slug}/source", json={"source": "cube(6);\n", "base": version}
            )
        else:
            started = client.post(
                f"/api/v1/models/{slug}/source/patch",
                json={"base": version, "edits": [{"search": "10", "replace": "6"}], "force": True},
            )
        assert started.status_code == 202, started.text
        moved = _state(app).catalogue.write_source(slug, "cube(7);\n")
    finally:
        release.set()
    op = _follow(client, started.json())
    assert op["status"] == "failed", op
    assert op["error"]["status"] == 409
    assert op["error"]["extensions"]["current"] == moved.version


def test_a_merge_that_conflicts_only_in_the_run_is_a_retryable_409_without_merged(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 1130 4: the route found the merge clean, then the template moved; the
    run's conflict is the 409 of a merge that kept changing, and `merged` stays out."""
    slug, _ = _model(client, "Merged Late")
    clean = MergePlan(revision="0" * 40, preview=MergePreview.model_construct(), conflicts=0)
    monkeypatch.setattr(Catalogue, "merge_plan", lambda self, slug: (clean, "dismissed"))
    conflict = MergePlan(
        revision="1" * 40,
        preview=MergePreview.model_construct(merged="<<<<<<< the run's merge", taken=[], kept=[]),
        conflicts=1,
    )

    def conflicts(self: Catalogue, slug: str) -> Any:
        raise MergeConflictError(conflict, "dismissed")

    monkeypatch.setattr(Catalogue, "merge_upstream", conflicts)
    refused = client.post(f"/api/v1/models/{slug}/upstream/merge")
    assert refused.status_code == 409, refused.text
    assert refused.json()["state"] == "dismissed"
    assert "merged" not in refused.json()
    (op_id,) = _operation_ids(app, "model_upstream_merge")
    error = client.get(f"/api/v1/operations/{op_id}").json()["error"]
    assert error["status"] == 409
    assert error["extensions"] == {"state": "dismissed"}


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


def _refused_by_the_check(app: FastAPI, response: Any, status: int, kind: str) -> None:
    """A refusal the kind's check made: nothing was recorded (M1)."""
    assert response.status_code == status, response.text
    assert _operation_ids(app, kind) == []


def test_a_file_edits_volume_refusals_record_nothing(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 1130 5 and M1: the refusals that read the volume are made by the check."""
    slug, _ = _model(client, "Files Refused")
    own = client.delete(f"/api/v1/models/{slug}/files/model.scad")
    _refused_by_the_check(app, own, 409, "model_file_delete")
    missing = client.delete(f"/api/v1/models/{slug}/files/missing.scad")
    _refused_by_the_check(app, missing, 404, "model_file_delete")
    assert "has no file 'missing.scad'" in missing.json()["detail"]
    monkeypatch.setattr(model_files, "MAX_SOURCE_FILES", 1)
    too_many = client.put(f"/api/v1/models/{slug}/files/x.scad", json={"content": "x = 1;\n"})
    _refused_by_the_check(app, too_many, 422, "model_file_put")
    assert "already has 1 .scad files" in too_many.json()["detail"]


def test_a_sidecars_volume_refusals_record_nothing(client: TestClient, app: FastAPI) -> None:
    """M1: no README or thumbnail of its own to remove is refused by the check."""
    slug, _ = _model(client, "Sidecars Refused")
    readme = client.delete(f"/api/v1/models/{slug}/readme")
    _refused_by_the_check(app, readme, 404, "model_readme_delete")
    thumbnail = client.delete(f"/api/v1/models/{slug}/thumbnail")
    _refused_by_the_check(app, thumbnail, 404, "model_thumbnail_delete")
    assert "no thumbnail of its own" in thumbnail.json()["detail"]


def test_edits_of_a_missing_model_are_404s_that_claim_nothing(
    client: TestClient, app: FastAPI
) -> None:
    """Review 1130 5 and M4: the existence check comes before the claim is written."""
    claims = _state(app).paths.claims
    png = models_api.PNG_MAGIC + b"\1" * 64
    content = "// for a model that is not there\n"
    calls = [
        client.put("/api/v1/models/nope/files/part.scad", json={"content": content}),
        client.delete("/api/v1/models/nope/files/part.scad"),
        client.delete("/api/v1/models/nope/files/model.scad"),
        client.put("/api/v1/models/nope/readme", json={"content": content}),
        client.put("/api/v1/models/nope/source", json={"source": content}),
        client.post(
            "/api/v1/models/nope/source/patch",
            json={"base": "abc1234", "edits": [{"search": "a", "replace": "b"}]},
        ),
        client.put("/api/v1/models/nope/thumbnail", files={"file": ("t.png", png, "image/png")}),
    ]
    for answered in calls:
        assert answered.status_code == 404, answered.text
    for data in (content.encode(), png):
        assert not (claims / hashlib.sha256(data).hexdigest()).exists()


def test_a_nul_in_a_saved_source_is_refused_before_its_claim(
    client: TestClient, app: FastAPI
) -> None:
    """M4: PUT /source refuses binary in the route, as it did before it was an operation."""
    slug, _ = _model(client, "Binary")
    source = "cube(1);\x00\n"
    refused = client.put(f"/api/v1/models/{slug}/source", json={"source": source})
    assert refused.status_code == 422, refused.text
    assert _operation_ids(app, "model_source_put") == []
    claim = _state(app).paths.claims / hashlib.sha256(source.encode()).hexdigest()
    assert not claim.exists()


def test_a_swept_upload_asks_for_the_edit_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M5: a same-key re-send replays the failure, so the answer asks for a new edit."""
    slug, _ = _model(client, "Swept")

    def swept(self: ClaimStore, name: str) -> bytes:
        raise LookupError(name)

    monkeypatch.setattr(ClaimStore, "get", swept)
    refused = client.put(f"/api/v1/models/{slug}/readme", json={"content": "# Gone\n"})
    assert refused.status_code == 409, refused.text
    assert "start the edit again" in refused.json()["detail"]


def test_removing_a_missing_readme_is_still_404(client: TestClient) -> None:
    slug, _ = _model(client, "No Readme")
    response = client.delete(f"/api/v1/models/{slug}/readme")
    assert response.status_code == 404, response.text
    assert "has no README" in response.json()["detail"]


def test_a_restore_is_an_operation(client: TestClient, app: FastAPI) -> None:
    slug, first = _model(client, "Restored")
    moved = client.put(f"/api/v1/models/{slug}/source", json={"source": "cube(5);\n"})
    assert moved.status_code == 200, moved.text
    restored = client.post(f"/api/v1/models/{slug}/versions/{first}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["current"] is True
    assert client.get(f"/api/v1/models/{slug}/source").text == SOURCE
    assert _workflow_ids(app, "model_restore")
