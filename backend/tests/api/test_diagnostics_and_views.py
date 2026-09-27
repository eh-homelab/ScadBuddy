"""Render diagnostics and named preview views over the API (#252)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import trimesh
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths
from scadbuddy.render.diagnostics import Diagnostic
from scadbuddy.render.glb import write_glb
from scadbuddy.render.jobs import Job, JobStore
from scadbuddy.render.split import ColourPart
from tests.api.conftest import FAIL_WIDTH, wait_for_job
from tests.conftest import read_png

ERROR = Diagnostic(
    severity="error",
    message="Parser error: syntax error",
    file="model.scad",
    line=3,
)


def _render(client: TestClient, slug: str, width: float = 12) -> str:
    response = client.post(f"/api/v1/models/{slug}/render", json={"params": {"width": width}})
    assert response.status_code == 202, response.text
    job_id: str = response.json()["job_id"]
    wait_for_job(client, job_id)
    return job_id


def _real_preview(paths: DataPaths, job_id: str) -> None:
    """The stub render writes placeholder bytes; a view needs a mesh to draw."""
    write_glb(
        [ColourPart(1, "Color 1", "#FF0000", trimesh.creation.box(extents=(40, 10, 10)))],
        paths.job_work_dir(job_id) / "preview.glb",
    )


# ── diagnostics ──────────────────────────────────────────────────────────────


def test_a_job_reports_its_diagnostics(client: TestClient, model: str) -> None:
    job = client.get(f"/api/v1/jobs/{_render(client, model)}").json()

    assert job["diagnostics"] == []


def test_the_model_diagnostics_are_the_latest_settled_render(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    _render(client, model)
    store = JobStore(paths)
    now = datetime.now(UTC)
    store.write(
        Job(
            id="f" * 32,
            slug=model,
            state="failed",
            created_at=now,
            finished_at=now + timedelta(minutes=1),
            error="openscad exited with 1",
            diagnostics=[ERROR],
        )
    )

    response = client.get(f"/api/v1/models/{model}/diagnostics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == "f" * 32
    assert body["status"] == "failed"
    assert body["error"] == "openscad exited with 1"
    assert body["diagnostics"] == [ERROR.model_dump(mode="json")]
    # The job's own route says the same.
    assert client.get(f"/api/v1/jobs/{'f' * 32}").json()["diagnostics"] == body["diagnostics"]


def test_a_failed_render_is_a_settled_one(client: TestClient, model: str) -> None:
    job_id = _render(client, model, FAIL_WIDTH)

    body = client.get(f"/api/v1/models/{model}/diagnostics").json()

    assert body["job_id"] == job_id
    assert body["status"] == "failed"


def test_a_model_never_rendered_has_no_diagnostics(client: TestClient, model: str) -> None:
    assert client.get(f"/api/v1/models/{model}/diagnostics").status_code == 404
    assert client.get("/api/v1/models/missing/diagnostics").status_code == 404


# ── views ────────────────────────────────────────────────────────────────────


def test_a_job_preview_is_drawn_from_a_named_view(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    job_id = _render(client, model)
    _real_preview(paths, job_id)

    front = client.get(f"/api/v1/jobs/{job_id}/views/front.png", params={"size": 128})
    left = client.get(f"/api/v1/jobs/{job_id}/views/left.png", params={"size": 128})

    assert front.status_code == 200, front.text
    assert front.headers["content-type"] == "image/png"
    front_image, left_image = read_png(front.content), read_png(left.content)
    assert front_image.shape == (128, 128, 4)

    def width_over_height(image: np.ndarray) -> float:
        rows, cols = np.nonzero(image[..., 3] == 255)
        return float(cols.max() - cols.min() + 1) / float(rows.max() - rows.min() + 1)

    # A bar along X: long from the front, square end-on.
    assert width_over_height(front_image) > 3
    assert 0.9 < width_over_height(left_image) < 1.1


def test_a_view_that_is_not_one_is_a_422(client: TestClient, model: str, paths: DataPaths) -> None:
    job_id = _render(client, model)
    _real_preview(paths, job_id)

    assert client.get(f"/api/v1/jobs/{job_id}/views/sideways.png").status_code == 422
    too_big = client.get(f"/api/v1/jobs/{job_id}/views/iso.png", params={"size": 5000})
    assert too_big.status_code == 422


def test_a_failed_job_has_no_views(client: TestClient, model: str) -> None:
    job_id = _render(client, model, FAIL_WIDTH)

    assert client.get(f"/api/v1/jobs/{job_id}/views/iso.png").status_code == 404
    assert client.get(f"/api/v1/jobs/{'0' * 32}/views/iso.png").status_code == 404


def test_a_saved_output_is_drawn_from_a_named_view(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    job_id = _render(client, model)
    _real_preview(paths, job_id)
    created = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job_id})
    assert created.status_code == 201, created.text
    output_id = created.json()["id"]

    response = client.get(f"/api/v1/outputs/{output_id}/views/top.png")

    assert response.status_code == 200, response.text
    assert read_png(response.content).shape == (512, 512, 4)
    assert client.get(f"/api/v1/outputs/{'0' * 32}/views/top.png").status_code == 404
