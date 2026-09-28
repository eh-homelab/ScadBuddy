"""piece_key and the Projection payload (spec 2026-09-27 §3.4)."""

from __future__ import annotations

import re
import uuid

from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import JobResult, PartInfo
from scadbuddy.render.job_store import render_key
from scadbuddy.workflows.models import Failure, Projection, piece_key

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _result() -> JobResult:
    return JobResult(
        model_3mf="blobs/k/model.3mf",
        preview_glb="blobs/k/preview.glb",
        parts=[PartInfo(name="Color 1", colour="#FF0000", extruder=1, watertight=True)],
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )


def test_piece_key_is_stable_under_key_order_and_differs_by_input() -> None:
    base = piece_key("demo", "abc123", "model.scad", {"width": 1, "height": 2})
    reordered = piece_key("demo", "abc123", "model.scad", {"height": 2, "width": 1})
    assert HEX64.match(base)
    assert base == reordered

    assert piece_key("other", "abc123", "model.scad", {"width": 1, "height": 2}) != base
    assert piece_key("demo", "def456", "model.scad", {"width": 1, "height": 2}) != base
    assert piece_key("demo", "abc123", "other.scad", {"width": 1, "height": 2}) != base
    assert piece_key("demo", "abc123", "model.scad", {"width": 9, "height": 2}) != base


def test_piece_key_differs_from_render_key_but_both_are_hex64() -> None:
    params = {"width": 1, "height": 2}
    piece = piece_key("demo", "abc123", "model.scad", params)
    render = render_key("demo", params, "abc123")

    assert HEX64.match(piece)
    assert HEX64.match(render)
    assert piece != render


def test_projection_round_trips_through_json() -> None:
    job_id = uuid.uuid4().hex
    projection = Projection(
        job_id=job_id,
        slug="demo",
        state="done",
        steps=None,
        result=_result(),
        failure=Failure(error="boom", log_tail=["ERROR: boom"]),
        blob_key="blobs/k",
    )

    dumped = projection.model_dump(mode="json")
    restored = Projection.model_validate(dumped)

    assert restored == projection
    assert restored.result is not None
    assert restored.result.model_3mf == "blobs/k/model.3mf"
    assert restored.failure is not None
    assert restored.failure.error == "boom"
