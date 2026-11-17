"""The farm's context, read from Bambuddy (#1912, #251's farm-context tier).

What the agent's analyzers need beyond a single print: every archive with its outcome.
The spool inventory is ScadBuddy's own since #6262 Task 9 (``filament/inventory.py``).
Every read here is ``can_read_status`` and none of them writes to Bambuddy.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from scadbuddy.bambuddy.models import ArchiveDetail


class ArchiveOutcome(BaseModel):
    """One Bambuddy archive (a past or current print, or an upload that never printed)
    and how it went. Any archive, not only ScadBuddy's own (``GET /prints`` is those)."""

    id: int
    print_name: str | None = None
    status: str | None = None
    printer_id: int | None = None
    project_id: int | None = None
    plate_id: int | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    print_time_seconds: int | None = None
    actual_time_seconds: int | None = None
    filament_type: str | None = None
    filament_color: str | None = None
    filament_used_grams: float | None = None
    cost: float | None = None
    failure_reason: str | None = None
    quantity: int = 1
    run_count: int = 0
    successful_run_count: int = 0
    failed_run_count: int = 0
    last_run_at: datetime | None = None

    @classmethod
    def of(cls, archive: ArchiveDetail) -> ArchiveOutcome:
        return cls.model_validate(archive.model_dump(include=set(cls.model_fields)))
