"""#902: POST /outputs/{id}/backfill, and Arrange's typed refusal that asks for it."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.api import jobs as jobs_api
from scadbuddy.api import outputs as outputs_api
from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.api.outputs import NEEDS_BACKFILL_PROBLEM
from scadbuddy.core.problems import ApiError
from scadbuddy.library import backfill as backfill_module
from scadbuddy.library.outputs import OUTPUT_HOLDER, BackfillState, OutputStore, release_parts
from scadbuddy.render.job_models import Job
from scadbuddy.store.refs import BlobRefs
from tests.api.conftest import wait_for_job
from tests.support.operations import press
from tests.support.store import store_pool

Pool = ConnectionPool[Connection[DictRow]]


@pytest.fixture
def pool(app: FastAPI, pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        state: AppState = getattr(app.state, STATE_ATTR)
        state.refs = BlobRefs(opened)
        yield opened


def _state(app: FastAPI) -> AppState:
    state: AppState = getattr(app.state, STATE_ATTR)
    return state


def held(pool: Pool, output_id: str) -> set[str]:
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT key FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
            (OUTPUT_HOLDER, output_id),
        ).fetchall()
    return {row["key"] for row in rows}


def legacy_output(client: TestClient, app: FastAPI, slug: str = "pasted") -> str:
    """A saved output of a pasted template, made to look like one saved before phase 5:
    no manifest.json and no Parts held."""
    if client.get(f"/api/v1/models/{slug}").status_code == 404:
        created = client.post(
            "/api/v1/models", json={"name": slug, "source": "cube(10);\n"}, headers=press()
        )
        assert created.status_code == 201, created.text
    queued = client.post(f"/api/v1/models/{slug}/render", json={"inputs": {"params": {}}})
    assert queued.status_code == 202, queued.text
    assert wait_for_job(client, queued.json()["job_id"])["status"] == "done"
    saved = client.post(f"/api/v1/models/{slug}/outputs", json={"job_id": queued.json()["job_id"]})
    assert saved.status_code == 201, saved.text
    output_id: str = saved.json()["id"]
    state = _state(app)
    (state.outputs.directory(output_id) / "manifest.json").unlink()
    release_parts(state.refs, output_id)
    return output_id


def attached(client: TestClient, output_id: str, timeout: float = 30) -> dict[str, Any]:
    """The output once the API has attached its re-render, on the job's event."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail: dict[str, Any] = client.get(f"/api/v1/outputs/{output_id}").json()
        if detail["manifest"] or (detail["backfill"] or {}).get("error"):
            return detail
        time.sleep(0.1)
    raise AssertionError(f"output {output_id} was never attached")


def _rewrite_meta(store: OutputStore, output_id: str, **update: object) -> None:
    path = store.directory(output_id) / "meta.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **update}), encoding="utf-8")


def test_a_backfill_rerenders_the_output_and_gives_it_a_manifest(
    client: TestClient, app: FastAPI, pool: Pool
) -> None:
    output_id = legacy_output(client, app)
    before = client.get(f"/api/v1/outputs/{output_id}").json()
    assert before["manifest"] == [] and before["backfill"] is None

    queued = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["id"]
    pending = client.get(f"/api/v1/outputs/{output_id}").json()
    # Unless the attach already finished it.
    assert pending["backfill"] in ({"job_id": job_id, "error": None}, None)
    assert wait_for_job(client, job_id)["status"] == "done"

    after = attached(client, output_id)
    assert after["manifest"] and after["backfill"] is None
    assert (after["id"], after["name"], after["created_at"]) == (
        before["id"],
        before["name"],
        before["created_at"],
    )
    assert held(pool, output_id) == {m["part"] for m in after["manifest"]}
    assert [o["id"] for o in client.get("/api/v1/models/pasted/outputs").json()] == [output_id]


def test_an_output_that_has_a_manifest_is_a_409(
    client: TestClient, app: FastAPI, pool: Pool
) -> None:
    output_id = legacy_output(client, app)
    queued = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert wait_for_job(client, queued.json()["id"])["status"] == "done"
    assert attached(client, output_id)["manifest"]
    again = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert again.status_code == 409, again.text
    assert "already records its objects" in again.json()["detail"]
    # Arrange's needs_backfill is a 409 as well: the code tells a client which (#1007).
    assert again.json()["code"] == "already_backfilled"


@pytest.mark.parametrize(
    ("version", "why"),
    [(None, "records no revision"), ("0" * 40, "no revision '" + "0" * 40)],
)
def test_an_output_whose_revision_cannot_be_rendered_again_is_a_422(
    client: TestClient, app: FastAPI, pool: Pool, version: str | None, why: str
) -> None:
    output_id = legacy_output(client, app)
    _rewrite_meta(_state(app).outputs, output_id, model_version=version)
    refused = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert refused.status_code == 422, refused.text
    assert why in refused.json()["detail"]
    assert _state(app).outputs.backfill(output_id) is None  # nothing queued


def test_an_arranged_output_is_a_422(client: TestClient, app: FastAPI, pool: Pool) -> None:
    output_id = legacy_output(client, app)
    store = _state(app).outputs
    (store.directory(output_id) / "arranged_from.json").write_text('["x"]', encoding="utf-8")
    refused = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert refused.status_code == 422, refused.text
    assert "arranged" in refused.json()["detail"]


def test_an_unknown_output_is_a_404(client: TestClient) -> None:
    assert client.post(f"/api/v1/outputs/{'0' * 32}/backfill").status_code == 404


def test_arrange_names_every_output_that_needs_a_backfill(
    client: TestClient, app: FastAPI, pool: Pool
) -> None:
    first, second = legacy_output(client, app), legacy_output(client, app)
    refused = client.post(
        "/api/v1/outputs/arrange",
        json={
            "objects": [
                {"output_id": first, "part": "p", "count": 1},
                {"output_id": second, "part": "q", "count": 1},
                {"output_id": first, "part": "r", "count": 1},
            ]
        },
    )
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["type"] == NEEDS_BACKFILL_PROBLEM
    assert body["code"] == "needs_backfill"
    assert body["output_ids"] == [first, second]
    assert "saved before Arrange existed" in body["detail"]


def test_a_second_post_while_the_rerender_is_in_flight_answers_that_job(
    client: TestClient, app: FastAPI, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History and Print can both POST, and a double click does: one re-render."""
    output_id = legacy_output(client, app)
    store = _state(app).render.store
    read = store.read

    def still_running(job_id: str) -> Job:
        return read(job_id).model_copy(update={"state": "running", "result": None, "outputs": []})

    monkeypatch.setattr(store, "read", still_running)
    first = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert first.status_code == 202, first.text

    def no_render(*args: object, **kwargs: object) -> None:
        raise AssertionError("a second re-render was queued")

    monkeypatch.setattr(outputs_api, "render_model", no_render)
    again = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert again.status_code == 202, again.text
    assert again.json()["id"] == first.json()["id"]
    assert _state(app).outputs.backfill(output_id) == BackfillState(job_id=first.json()["id"])


def test_a_second_post_after_the_rerender_finished_but_before_its_attach_answers_that_job(
    client: TestClient, app: FastAPI, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1007: the guard covered only a job in flight; one done and not yet attached
    queued a second, wasted render."""
    # Nothing attaches in this test: the window between done and attached stays open.
    monkeypatch.setattr(backfill_module, "_attach", lambda *args, **kwargs: False)
    output_id = legacy_output(client, app)
    store = _state(app).render.store
    read = store.read

    def finished(job_id: str) -> Job:
        return read(job_id).model_copy(update={"state": "done", "finished_at": datetime.now(UTC)})

    monkeypatch.setattr(store, "read", finished)
    first = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert first.status_code == 202, first.text

    def no_render(*args: object, **kwargs: object) -> None:
        raise AssertionError("a second re-render was queued")

    monkeypatch.setattr(outputs_api, "render_model", no_render)
    again = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert again.status_code == 202, again.text
    assert again.json()["id"] == first.json()["id"]


def test_a_post_long_after_the_rerender_finished_unattached_queues_a_new_one(
    client: TestClient, app: FastAPI, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1849: a re-render done well past the attach grace is not being attached (the
    attach keeps failing). Answering it forever left every Arrange waiting out its whole
    wait; a POST re-queues instead."""
    monkeypatch.setattr(backfill_module, "_attach", lambda *args, **kwargs: False)
    output_id = legacy_output(client, app)
    store = _state(app).render.store
    read = store.read
    long_ago = datetime.now(UTC) - outputs_api.BACKFILL_ATTACH_GRACE - timedelta(seconds=1)

    def finished(job_id: str) -> Job:
        return read(job_id).model_copy(update={"state": "done", "finished_at": long_ago})

    monkeypatch.setattr(store, "read", finished)
    first = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert first.status_code == 202, first.text
    submitted: list[object] = []
    real_render = jobs_api.render_model

    async def counting(*args: Any, **kwargs: Any) -> Any:
        submitted.append(args)
        return await real_render(*args, **kwargs)

    monkeypatch.setattr(outputs_api, "render_model", counting)
    again = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert again.status_code == 202, again.text
    assert len(submitted) == 1  # re-queued, not answered with the stale done job


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (ApiError(404, "no library 'gone' for template 'pasted'"), None),
        (ApiError(409, "no snapshot of pasted@abc and no history"), "snapshot_unavailable"),
    ],
)
def test_a_render_that_cannot_start_is_a_422_that_says_why(
    client: TestClient,
    app: FastAPI,
    pool: Pool,
    monkeypatch: pytest.MonkeyPatch,
    raised: ApiError,
    code: str | None,
) -> None:
    """#1007: the render's own detail, not always "revision gone"; and never a 409, which
    here means the output already records its objects."""
    output_id = legacy_output(client, app)

    async def refuse(*args: object, **kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(outputs_api, "render_model", refuse)
    refused = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert refused.status_code == 422, refused.text
    body = refused.json()
    assert raised.detail in body["detail"]
    assert "no longer in the template's history" not in body["detail"]
    assert body.get("code") == code
    assert _state(app).outputs.backfill(output_id) is None  # nothing queued


def test_a_post_whose_pending_job_does_not_validate_queues_a_new_one(
    client: TestClient, app: FastAPI, pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1803 review: the attach retries a job row that does not validate, so the guard
    must not 500 on it either, or the output could never be re-queued."""
    monkeypatch.setattr(backfill_module, "_attach", lambda *args, **kwargs: False)
    output_id = legacy_output(client, app)
    store = _state(app).render.store
    first = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert first.status_code == 202, first.text
    read = store.read
    bad = [first.json()["id"]]

    def unreadable(job_id: str) -> Job:
        if job_id in bad:  # the guard's read only: the render that follows may share the id
            bad.remove(job_id)
            Job.model_validate({"id": job_id})  # a ValidationError, a ValueError
        return read(job_id)

    monkeypatch.setattr(store, "read", unreadable)
    again = client.post(f"/api/v1/outputs/{output_id}/backfill")
    assert again.status_code == 202, again.text
    assert bad == []  # the guard did read the bad row, and re-queued instead of a 500
