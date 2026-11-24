"""The camera recording's wire models (plan 2026-11-23 camera recording, phase 1)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, SecretStr

RTSPS = "rtsps://"


def rtsp_url(report: Mapping[str, Any]) -> str | None:
    """The printer's camera URL from its stored ``print`` report: ``ipcam.rtsp_url`` when it
    starts with ``rtsps://``, else ``None`` (an ``rtsp://`` or empty value is no camera)."""
    ipcam = report.get("ipcam")
    if not isinstance(ipcam, Mapping):
        return None
    url = ipcam.get("rtsp_url")
    if isinstance(url, str) and url.startswith(RTSPS):
        return url
    return None


class CameraSource(BaseModel):
    """Where to pull one printer's camera. Holds no access code."""

    printer_id: str
    host: str
    port: int
    path: str
    cert_sha256: str


class CameraSourceWithCode(CameraSource):
    access_code: SecretStr


class SegmentIn(BaseModel):
    started_at: AwareDatetime
    duration_ms: int = Field(gt=0)
    object_key: str = Field(min_length=1)
    bytes: int = Field(gt=0)
    init_bytes: int = Field(gt=0)
    init_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    discontinuity: bool = False


class LeaseAnswer(BaseModel):
    held: bool
    holder: str | None = None
    expires_at: datetime | None = None
    indexed_until: datetime | None = None


FeedState = Literal["pulling", "retrying", "stalled", "stopped"]


class FeedReport(BaseModel):
    part: Literal["relay", "recorder"]
    state: FeedState
    error: str | None = None
    recorder: str | None = None
    last_segment_at: datetime | None = None
    upload_lag_s: float | None = None
    buffered_bytes: int | None = None
