"""The `slice_plate` payloads the Python side and the Rust slicer worker exchange
(spec §5.2). `workers/rust/vectors/slice_plate.json` pins the two languages together."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

#: The ApplicationError type the worker raises for a failed slice.
SLICER_ERROR = "SlicerError"
#: Bambu Studio's exit code for a rejected input.
VALIDATION_EXIT = 192


class TransferUrl(BaseModel):
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    #: Aware UTC. Dumped with "+00:00"; the Rust side parses either form.
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        return value


class SlicePlateInput(BaseModel):
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
