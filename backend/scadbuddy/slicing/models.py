"""The `slice_plate` payloads the Python side and the Rust slicer worker exchange
(spec §5.2). `workers/rust/vectors/slice_plate.json` pins the two languages together."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The ApplicationError type the worker raises for a failed slice.
SLICER_ERROR = "SlicerError"
#: Bambu Studio's exit code for a rejected input.
VALIDATION_EXIT = 192


class TransferUrl(BaseModel):
    """A bearer credential: the path and query may carry a presigned signature, a header
    value the internal route's token. Its repr and str, and a `ValidationError` of it,
    show only the origin, the header names and ``expires_at``, as `sb-transfer`'s
    `Debug` does. A model that holds one sets ``hide_input_in_errors`` too: pydantic
    takes that from the model validated, not from the field's."""

    model_config = ConfigDict(hide_input_in_errors=True)

    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    #: Aware, normalised to UTC, so it always dumps with "Z".
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        return value.astimezone(UTC)

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:
        parts = urlsplit(self.url)
        origin = f"{parts.scheme}://{parts.hostname}" if parts.scheme and parts.hostname else None
        yield "origin", origin or "<redacted>"
        yield "headers", dict.fromkeys(self.headers, "<redacted>")
        yield "expires_at", self.expires_at


class SlicePlateInput(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    input_3mf: TransferUrl
    #: 0 = every plate.
    plate: int = Field(ge=0)
    machine_preset: TransferUrl
    process_preset: TransferUrl
    #: In slot order.
    filament_presets: list[TransferUrl]
    sliced_3mf: TransferUrl
    slicedata: TransferUrl | None = None
    arrange: Literal["no", "yes", "auto"] = "no"
    allow_newer_file: bool = True
    debug: int = Field(default=1, ge=0, le=5)


class SliceProgress(BaseModel):
    """One heartbeat's details."""

    plate_index: int
    plate_count: int
    plate_percent: int
    total_percent: int
    message: str = ""


class SlicePlateOutput(BaseModel):
    sliced_sha256: str
    sliced_size: int
    slicedata_sha256: str | None = None
    slicedata_size: int | None = None
    warnings: list[str] = Field(default_factory=list)
    exit_code: int
    stderr_tail: str = ""
    elapsed_seconds: float
