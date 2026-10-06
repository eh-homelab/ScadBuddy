from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.render.inputs import MAX_INPUTS_BYTES
from scadbuddy.render.job_models import Job, QueueFullError
from scadbuddy.render.jobs import SnapshotPendingError
from scadbuddy.render.schema import ParamValue
from scadbuddy.render.submit import RenderService
from scadbuddy.store.content import StoreFullError
from tests.api.conftest import FAIL_WIDTH, FAILED_WARNING, set_fake_env, wait_for_job


def test_render_is_accepted_and_the_job_completes(
    client: TestClient, model: str, settings: Settings
) -> None:
    logged = [
        "rendered fine",
        'ECHO: "NOTE: a note"',
        "WARNING: The file 'logo.svg' couldn't be opened",
    ]
    set_fake_env(Path(settings.openscad).parent, "FAKE_STDERR", logged)
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status_url"] == f"/api/v1/jobs/{accepted['job_id']}"

    job = wait_for_job(client, accepted["job_id"])
    assert job["status"] == "done"
    assert job["slug"] == model
    assert job["params"] == {"width": 12}
    assert job["colors"] == ["#FF0000"]
    assert job["warnings"] == ["OpenSCAD could not open logo.svg; the model rendered without it"]
    assert job["notes"] == ["a note"]
    assert job["bbox_mm"]["size"] == [10.0, 10.0, 5.0]
    assert job["parts"][0]["extruder"] == 1
    assert job["log_tail"] == logged
    assert job["preview_url"] == f"/api/v1/jobs/{accepted['job_id']}/preview.glb"


def test_render_with_no_params_uses_the_model_defaults(client: TestClient, model: str) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={})
    assert response.status_code == 202
    assert wait_for_job(client, response.json()["job_id"])["params"] == {}


def test_an_unknown_parameter_is_rejected_by_name(client: TestClient, model: str) -> None:
    response = client.post(
        f"/api/v1/models/{model}/render", json={"params": {"width": 1, "nope": 2, "also": 3}}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["parameters"] == ["also", "nope"]
    assert "also, nope" in body["detail"]
    assert response.headers["content-type"] == "application/problem+json"


def test_a_parameter_of_the_wrong_type_is_rejected(client: TestClient, model: str) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": "wide"}})
    assert response.status_code == 422
    assert "expects a number" in response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [{"params": {"label": "N\x00L"}}, {"inputs": {"params": {"label": "N\x00L"}}}],
)
def test_a_nul_in_a_text_parameter_is_rejected_by_name(
    client: TestClient, model: str, body: dict[str, Any]
) -> None:
    """#965: Postgres cannot hold a NUL, so it was a 500 echoing the driver's error."""
    response = client.post(f"/api/v1/models/{model}/render", json=body)
    assert response.status_code == 422, response.text
    assert "'label' contains a NUL byte" in response.json()["detail"]


def test_a_nul_in_the_template_ui_state_is_rejected(client: TestClient, model: str) -> None:
    response = client.post(
        f"/api/v1/models/{model}/render", json={"inputs": {"params": {}, "ui": {"a\x00": 1}}}
    )
    assert response.status_code == 422, response.text
    assert "NUL byte" in response.json()["detail"]


def test_a_text_parameter_holding_a_path_is_rejected(client: TestClient, model: str) -> None:
    """#281: a template may hand any string to import()/surface(), so a value that
    would reach outside the model's directory never reaches openscad."""
    response = client.post(
        f"/api/v1/models/{model}/render", json={"params": {"label": "/proc/self/environ"}}
    )
    assert response.status_code == 422
    assert "looks like a file path" in response.json()["detail"]


def test_rendering_an_unknown_model_is_a_404(client: TestClient) -> None:
    assert client.post("/api/v1/models/missing/render", json={}).status_code == 404


def test_a_failed_render_carries_the_log_tail(client: TestClient, model: str) -> None:
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": FAIL_WIDTH}})
    job = wait_for_job(client, response.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"] == "openscad exited with 1"
    assert job["log_tail"] == [
        "WARNING: The file 'pic.svg' couldn't be opened",
        "ERROR: something broke",
    ]
    # A failed render has no result, and still says what it could (#408).
    assert job["warnings"] == [FAILED_WARNING]
    assert job["preview_url"] is None
    assert job["bbox_mm"] is None


def test_the_preview_glb_is_served_with_the_gltf_media_type(client: TestClient, model: str) -> None:
    job_id = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 1}}).json()[
        "job_id"
    ]
    wait_for_job(client, job_id)

    response = client.get(f"/api/v1/jobs/{job_id}/preview.glb")
    assert response.status_code == 200
    assert response.headers["content-type"] == "model/gltf-binary"
    assert response.content.startswith(b"glTF")


def test_a_failed_job_has_no_preview(client: TestClient, model: str) -> None:
    job_id = client.post(
        f"/api/v1/models/{model}/render", json={"params": {"width": FAIL_WIDTH}}
    ).json()["job_id"]
    wait_for_job(client, job_id)
    assert client.get(f"/api/v1/jobs/{job_id}/preview.glb").status_code == 404


def test_an_unknown_job_is_a_404(client: TestClient) -> None:
    assert client.get("/api/v1/jobs/" + "0" * 32).status_code == 404
    assert client.get("/api/v1/jobs/not-a-job-id").status_code == 422


def test_a_render_can_supersede_the_previous_one(client: TestClient, model: str) -> None:
    first = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 11}})
    second = client.post(
        f"/api/v1/models/{model}/render",
        json={"params": {"width": 12}, "supersedes": first.json()["job_id"]},
    )
    assert second.status_code == 202
    # The first is cancelled unless its render finished first; the newer one renders.
    assert wait_for_job(client, second.json()["job_id"])["status"] == "done"
    assert wait_for_job(client, first.json()["job_id"])["status"] in ("done", "cancelled")


def test_supersedes_must_be_a_job_id(client: TestClient, model: str) -> None:
    response = client.post(
        f"/api/v1/models/{model}/render", json={"params": {}, "supersedes": "../etc"}
    )
    assert response.status_code == 422


def test_a_full_render_queue_is_a_503_with_retry_after(client: TestClient, model: str) -> None:
    """Only with SCADBUDDY_RENDER_QUEUE_MAX set; by default nothing is refused."""
    full = mock.AsyncMock(side_effect=QueueFullError(depth=16, retry_after=7))
    with mock.patch.object(RenderService, "submit", full):
        response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "7"
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["retry_after"] == 7
    assert "queue is full" in body["detail"]


def test_a_render_whose_source_the_blob_store_has_no_room_for_is_a_507(
    client: TestClient, model: str
) -> None:
    """`submit` pins the template's snapshot in the store; a full store is a problem, not a 500."""
    full = mock.AsyncMock(side_effect=StoreFullError("past SCADBUDDY_STORE_MAX_TOTAL_BYTES (10)"))
    with mock.patch.object(RenderService, "submit", full):
        response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    assert response.status_code == 507
    assert response.headers["content-type"] == "application/problem+json"
    assert "SCADBUDDY_STORE_MAX_TOTAL_BYTES" in response.json()["detail"]


def test_a_render_whose_snapshot_is_still_uploading_is_a_coded_503(
    client: TestClient, model: str
) -> None:
    """#686: past `pin`'s wait, a 503 with Retry-After and a `code` that tells it apart
    from a full queue."""
    pending = mock.AsyncMock(side_effect=SnapshotPendingError("still uploading", retry_after=30))
    with mock.patch.object(RenderService, "submit", pending):
        response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["retry_after"] == 30
    assert body["code"] == "snapshot_pending"


class _NoCommit:
    """`SnapshotStore.pin` with no history, or a template with no commit yet."""

    async def pin(self, slug: str, revision: str | None) -> str | None:
        return None


def test_a_render_the_store_cannot_snapshot_is_a_409(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#674 gate: on the bambuddy store a render needs a commit to snapshot; none is a
    conflict the author resolves by committing, not a 500."""
    render = getattr(client.app.state, STATE_ATTR).render  # type: ignore[attr-defined]
    monkeypatch.setattr(render, "snapshots", _NoCommit())
    response = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert "never committed" in response.json()["detail"]


# -- the customizer's range and options (#432) -------------------------------------

RANGED_SOURCE = (
    "// %%RANGED%%\n"
    "width = 10; // [1:100]\n"
    'shape = "round"; // [round:Round, square:Square]\n'
    '// retired shape = "circle"\n'
)


@pytest.fixture
def ranged(paths: DataPaths, model: str) -> str:
    paths.model_source(model).write_text(RANGED_SOURCE, encoding="utf-8")
    return model


@pytest.mark.parametrize(
    ("params", "name", "detail"),
    [
        ({"width": 101}, "width", "'width' must be between 1 and 100, got 101"),
        ({"width": 0.5}, "width", "'width' must be between 1 and 100, got 0.5"),
        ({"shape": "hexagon"}, "shape", '\'shape\' must be one of "round", "square"'),
    ],
)
def test_a_value_outside_the_customizer_is_rejected_by_name(
    client: TestClient, ranged: str, params: dict[str, object], name: str, detail: str
) -> None:
    response = client.post(f"/api/v1/models/{ranged}/render", json={"params": params})
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["parameters"] == [name]
    assert detail in body["detail"]
    assert response.headers["content-type"] == "application/problem+json"


def test_the_customizer_bounds_and_a_retired_option_are_accepted(
    client: TestClient, ranged: str
) -> None:
    for params in ({"width": 1}, {"width": 100}, {"shape": "square"}, {"shape": "circle"}):
        response = client.post(f"/api/v1/models/{ranged}/render", json={"params": params})
        assert response.status_code == 202, (params, response.text)


def test_a_preset_outside_the_customizer_is_rejected(client: TestClient, ranged: str) -> None:
    url = f"/api/v1/models/{ranged}/presets"
    refused = client.post(url, json={"name": "Huge", "params": {"width": 1000}})
    assert refused.status_code == 422
    assert refused.json()["parameters"] == ["width"]
    # A preset saved before an option was renamed can be saved again.
    kept = client.post(url, json={"name": "Old", "params": {"shape": "circle"}})
    assert kept.status_code == 201, kept.text


def test_a_render_takes_inputs_and_the_job_reports_them(client: TestClient, model: str) -> None:
    body = {"inputs": {"params": {"width": 12}, "ui": {"tab": "lid"}}}
    accepted = client.post(f"/api/v1/models/{model}/render", json=body)
    assert accepted.status_code == 202
    job = client.get(accepted.json()["status_url"]).json()
    assert job["params"] == {"width": 12}
    assert job["inputs"] == {"params": {"width": 12}, "ui": {"tab": "lid"}, "v": 0}


def test_a_coalesced_submit_answers_with_the_callers_own_inputs(
    client: TestClient, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#706 gate: a submit that joins a waiting job (the same `params`) gets that job,
    whose row keeps the first submitter's inputs. The response carries the caller's own,
    so a UI never takes a stranger's state for its own."""
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    submit = state.render.submit
    jobs: list[Job] = []

    async def coalescing(slug: str, params: Mapping[str, ParamValue], **kwargs: Any) -> Job:
        # As the service answers a submit whose render key matches a pending job.
        if not jobs:
            jobs.append(await submit(slug, params, **kwargs))
        return jobs[0]

    monkeypatch.setattr(state.render, "submit", coalescing)
    url = f"/api/v1/models/{model}/render"
    first = client.post(url, json={"inputs": {"params": {"width": 5}, "ui": {"tab": "lid"}}})
    second = client.post(url, json={"inputs": {"params": {"width": 5}, "ui": {"tab": "base"}}})
    assert first.status_code == second.status_code == 202, second.text
    assert second.json()["job_id"] == first.json()["job_id"]
    assert first.json()["inputs"] == {"params": {"width": 5}, "ui": {"tab": "lid"}, "v": 0}
    assert second.json()["inputs"] == {"params": {"width": 5}, "ui": {"tab": "base"}, "v": 0}
    status = client.get(second.json()["status_url"]).json()
    assert status["inputs"]["ui"] == {"tab": "lid"}  # the submission that created it


def test_a_params_body_is_still_accepted_as_inputs(client: TestClient, model: str) -> None:
    accepted = client.post(f"/api/v1/models/{model}/render", json={"params": {"width": 12}})
    assert accepted.status_code == 202
    job = client.get(accepted.json()["status_url"]).json()
    assert job["inputs"] == {"params": {"width": 12}, "v": 0}


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"inputs": {"params": {"width": 1}}, "params": {"width": 2}}, "disagree"),
        ({"inputs": {"params": {"nope": 1}}}, "unknown parameters: nope"),
        ({"inputs": {"params": {"width": [1]}}}, "inputs.params.width must be a number"),
        ({"inputs": {"params": {}, "blob": "x" * 70000}}, f"at most {MAX_INPUTS_BYTES}"),
        ('{"inputs": {"params": {"width": NaN}}}', "no NaN or Infinity"),
        ('{"inputs": {"params": {}, "ui": {"zoom": Infinity}}}', "no NaN or Infinity"),
        # The params-only body takes the same checks (#706 gate): an integer
        # parameter at Infinity was a 500 from `int(float("inf"))`.
        ('{"params": {"width": Infinity}}', "no NaN or Infinity"),
        ('{"params": {"width": NaN}}', "no NaN or Infinity"),
        ({"params": {"label": "x" * 70000}}, f"at most {MAX_INPUTS_BYTES}"),
    ],
    ids=[
        "disagree",
        "unknown",
        "type",
        "size",
        "nan-param",
        "inf-nested",
        "legacy-inf",
        "legacy-nan",
        "legacy-size",
    ],
)
def test_bad_inputs_are_refused_before_a_job_exists(
    client: TestClient, model: str, body: dict[str, object] | str, detail: str
) -> None:
    url = f"/api/v1/models/{model}/render"
    if isinstance(body, str):
        # httpx will not encode NaN; send the bytes a client that does would send.
        refused = client.post(url, content=body, headers={"content-type": "application/json"})
    else:
        refused = client.post(url, json=body)
    assert refused.status_code == 422, refused.text
    assert detail in refused.json()["detail"]
    state: AppState = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    assert state.render.store.list_jobs() == []
