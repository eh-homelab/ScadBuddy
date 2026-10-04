"""Saving, re-covering and deleting an output as ``library`` operations (#1054)."""

from __future__ import annotations

import uuid

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.operations import STILL_ACCEPTING_PROBLEM, _prelude
from scadbuddy.core.paths import DataPaths
from scadbuddy.operations.component import OPERATIONS
from scadbuddy.workflows.operation_models import PreludeStep
from tests.api.conftest import FAIL_WIDTH, PNG_BYTES, wait_for_job
from tests.api.test_library_copies import API, run, set_up, uploads
from tests.api.test_model_operations import _workflow_ids

pytestmark = [pytest.mark.requires_git, pytest.mark.requires_postgres]


def _job(client: TestClient, slug: str, width: float = 12) -> str:
    job_id: str = client.post(
        f"/api/v1/models/{slug}/render", json={"params": {"width": width}}
    ).json()["job_id"]
    wait_for_job(client, job_id)
    return job_id


def _outputs(paths: DataPaths, slug: str) -> list[str]:
    root = paths.output_dir(slug, "x").parent
    return sorted(path.name for path in root.iterdir()) if root.exists() else []


def test_a_repeated_output_create_makes_one_output(
    client: TestClient, app: FastAPI, model: str, paths: DataPaths
) -> None:
    job_id = _job(client, model)
    key = uuid.uuid4().hex
    body = {"job_id": job_id, "name": "Once"}
    headers = {"Idempotency-Key": key}
    first = client.post(f"/api/v1/models/{model}/outputs", json=body, headers=headers)
    assert first.status_code == 201, first.text
    again = client.post(f"/api/v1/models/{model}/outputs", json=body, headers=headers)
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert _outputs(paths, model) == [first.json()["id"]]
    assert len(_workflow_ids(app, "output_create")) == 1


def test_output_writes_are_operations(client: TestClient, app: FastAPI, model: str) -> None:
    created = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": _job(client, model)})
    assert created.status_code == 201, created.text
    output_id = created.json()["id"]
    covered = client.put(
        f"/api/v1/outputs/{output_id}/thumbnail",
        files={"file": ("t.png", PNG_BYTES, "image/png")},
    )
    assert covered.status_code == 204, covered.text
    assert client.get(f"/api/v1/outputs/{output_id}/thumbnail").content == PNG_BYTES
    assert client.delete(f"/api/v1/outputs/{output_id}").status_code == 204
    assert client.get(f"/api/v1/outputs/{output_id}").status_code == 404
    for kind in ("output_create", "output_thumbnail", "output_delete"):
        assert _workflow_ids(app, kind), kind


def test_a_job_not_done_is_still_409_without_an_operation_record(
    client: TestClient, app: FastAPI, model: str
) -> None:
    failed = _job(client, model, FAIL_WIDTH)
    response = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": failed})
    assert response.status_code == 409, response.text
    assert "failed" in response.json()["detail"]
    assert _workflow_ids(app, "output_create") == []


@respx.mock
def test_an_inbox_copy_that_fails_to_delete_keeps_the_output(
    client: TestClient, app: FastAPI, model: str, paths: DataPaths
) -> None:
    output_id = set_up(client, model)
    uploads(41)
    run(client, output_id)
    respx.delete(f"{API}/library/files/41").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )
    response = client.delete(f"/api/v1/outputs/{output_id}?delete_inbox_copies=true")
    assert response.status_code >= 500, response.text
    # Bambuddy's failure, recorded: not a 503 from before the operation started.
    assert response.json()["type"] != STILL_ACCEPTING_PROBLEM, response.text
    assert len(_workflow_ids(app, "output_delete")) == 1
    assert output_id in _outputs(paths, model)
    assert client.get(f"/api/v1/outputs/{output_id}").status_code == 200


def test_only_a_delete_of_the_inbox_copies_waits_on_the_print_worker(client: TestClient) -> None:
    """#1060: the inbox copies go on the `bambuddy` queue, as `output_delete`'s prelude;
    a plain delete names none, so it never waits on the print worker."""
    app_state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    ops = app_state.components.get(OPERATIONS)
    kind = ops.kinds["output_delete"]
    request = {"output_id": "a" * 32, "delete_inbox_copies": False}

    assert _prelude(ops, kind, request) is None
    assert _prelude(ops, kind, {**request, "delete_inbox_copies": True}) == PreludeStep(
        kind="output_inbox_delete", task_queue=ops.queues["bambuddy"], run_attempts=3
    )
    assert ops.kinds["output_inbox_delete"].queue == "bambuddy"
