"""The camera recording's models: the camera URL a printer's report names."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from scadbuddy.camera.models import SegmentIn, rtsp_url

REPORT = Path(__file__).parent / "fixtures" / "h2c-report.json"


def test_rtsp_url_from_h2c_report() -> None:
    """The fixture is an H2C's stored ``print`` object (copied from the
    ``h2c-print-from-scadbuddy`` skill's boys1 eval report)."""
    report = json.loads(REPORT.read_text())
    assert rtsp_url(report) == "rtsps://172.16.0.187:322/streaming/live/1"


@pytest.mark.parametrize(
    "report",
    [
        {"ipcam": {"rtsp_url": "rtsp://172.16.0.187/streaming/live/1"}},
        {"ipcam": {"rtsp_url": ""}},
        {"ipcam": {}},
        {"ipcam": None},
        {},
    ],
)
def test_rtsp_url_none_for_rtsp_or_missing(report: dict[str, Any]) -> None:
    assert rtsp_url(report) is None


def test_a_segment_needs_positive_sizes_and_an_aware_start() -> None:
    good: dict[str, Any] = {
        "started_at": "2026-11-24T00:00:00+00:00",
        "duration_ms": 4000,
        "object_key": "camera/p/2026/11/24/000000.000000.mp4",
        "bytes": 10,
        "init_bytes": 5,
        "init_sha256": "ab" * 32,
        "discontinuity": False,
    }
    SegmentIn.model_validate(good)
    for bad in (
        {"duration_ms": 0},
        {"bytes": 0},
        {"init_bytes": 0},
        {"init_sha256": "xyz"},
        {"started_at": "2026-11-24T00:00:00"},
    ):
        with pytest.raises(ValidationError):
            SegmentIn.model_validate({**good, **bad})
