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
from tests.api.conftest import FAIL_WIDTH, job_file, wait_for_job
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
        job_file(paths, job_id, "preview.glb"),
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
            diagnostics_dropped=3,
        )
    )

    response = client.get(f"/api/v1/models/{model}/diagnostics")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == "f" * 32
    assert body["status"] == "failed"
    assert body["error"] == "openscad exited with 1"
    assert body["diagnostics"] == [ERROR.model_dump(mode="json")]
    assert body["diagnostics_dropped"] == 3
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


# ── per-colour breakdown ─────────────────────────────────────────────────────


def _three_colours(paths: DataPaths, job_id: str) -> None:
    """Three boxes side by side along X, red, green and blue."""
    parts = [
        ColourPart(
            index + 1,
            f"Color {index + 1}",
            colour,
            trimesh.creation.box(
                extents=(10, 10, 10),
                transform=trimesh.transformations.translation_matrix((index * 20, 0, 0)),
            ),
        )
        for index, colour in enumerate(("#FF0000", "#00FF00", "#0000FF"))
    ]
    write_glb(parts, job_file(paths, job_id, "preview.glb"))


def test_a_breakdown_has_one_tile_per_colour_named_in_order(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    job_id = _render(client, model)
    _three_colours(paths, job_id)

    response = client.get(f"/api/v1/jobs/{job_id}/colours.png", params={"view": "top", "size": 64})

    assert response.status_code == 200, response.text
    named = response.headers["x-scadbuddy-colours"].split(",")
    assert response.headers["x-scadbuddy-colour-columns"] == "2"
    # The stub job's `colors` (extruder order) lists red, so it comes first.
    assert named[0] == "#FF0000"
    assert sorted(named) == ["#0000FF", "#00FF00", "#FF0000"]
    channels = [{"#FF0000": 0, "#00FF00": 1, "#0000FF": 2}[colour] for colour in named]
    image = read_png(response.content)
    # Three tiles in a 2x2 grid; the fourth is empty.
    assert image.shape == (128, 128, 4)
    assert not image[64:, 64:, 3].any()
    for tile, (row, column), channel in zip(
        range(3), [(0, 0), (0, 1), (1, 0)], channels, strict=True
    ):
        pixels = image[row * 64 : (row + 1) * 64, column * 64 : (column + 1) * 64]
        solid = pixels[pixels[..., 3] == 255][:, :3].astype(int)
        # Its own colour is on the tile; the other boxes are grey (all channels equal).
        coloured = solid[(solid.max(axis=1) - solid.min(axis=1)) > 60]
        assert len(coloured) > 0, tile
        assert (coloured.argmax(axis=1) == channel).all(), tile
        greys = solid[(solid.max(axis=1) - solid.min(axis=1)) <= 1]
        assert len(greys) > len(coloured), tile


def test_a_breakdown_of_a_failed_job_is_a_404(client: TestClient, model: str) -> None:
    job_id = _render(client, model, FAIL_WIDTH)
    assert client.get(f"/api/v1/jobs/{job_id}/colours.png").status_code == 404


def test_a_breakdown_tile_past_its_cap_is_a_422(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    job_id = _render(client, model)
    _real_preview(paths, job_id)
    assert (
        client.get(f"/api/v1/jobs/{job_id}/colours.png", params={"size": 1024}).status_code == 422
    )
