from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.render.job_models import QueueFullError
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
