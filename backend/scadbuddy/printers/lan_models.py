"""What the LAN client sends and reads (#3823, plan 2026-10-16, Task 4). Field names are
OpenBambuAPI's `mqtt.md` (https://github.com/Doridian/OpenBambuAPI/blob/main/mqtt.md,
read 2026-10-18)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class LanReport(BaseModel):
    """A printer's state: the ``print`` object of its reports on
    ``device/<serial>/report``, each merged over the one before (`lan.merge_report`),
    because the P1 series sends only what changed (`mqtt.md`, "pushing.pushall")."""

    serial: str = Field(repr=False)
    received_at: datetime
    print: dict[str, Any] = Field(default_factory=dict)


class ProjectFile(BaseModel):
    """``print.project_file``'s arguments (`mqtt.md`, "print.project_file")."""

    url: str
    param: str
    subtask_name: str
    use_ams: bool
    ams_mapping: list[int]
    bed_type: str = "auto"
    flow_cali: bool = False
    timelapse: bool = False
    layer_inspect: bool = True
    md5: str


class LanStarted(BaseModel):
    """A plate the printer took over the LAN (plan 2026-10-16 Task 9): the
    ``subtask_name`` its reports carry while it runs it, which the native follow reads
    its progress by, and when the start was seen."""

    subtask_name: str
    started_at: datetime
