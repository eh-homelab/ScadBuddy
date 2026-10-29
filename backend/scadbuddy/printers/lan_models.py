"""What the LAN client sends and reads (#3823, plan 2026-10-16, Task 4). Field names are
OpenBambuAPI's `mqtt.md` (https://github.com/Doridian/OpenBambuAPI/blob/main/mqtt.md,
read 2026-10-18)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.filaments import FilamentWarning
from scadbuddy.bambuddy.stages import Stage


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
    #: One physical nozzle id per filament slot on a rack printer
    #: (`printers.nozzle_mapping`); ``None`` sends no field, and the firmware picks.
    nozzle_mapping: list[int] | None = None


class LanStarted(BaseModel):
    """A plate the printer took over the LAN (plan 2026-10-16 Task 9): the
    ``subtask_name`` its reports carry while it runs it, which the native follow reads
    its progress by, and when the start was seen."""

    subtask_name: str
    started_at: datetime
    #: The rack pick's advisories for this plate (#4545): what was sent, or why the
    #: printer picks the hotends itself. Never a serial. A start an earlier attempt took
    #: carries none, as no pick is made again.
    warnings: list[FilamentWarning] = Field(default_factory=list)
    #: The plate it printed (#4541); ``None`` on a start from before a run could start
    #: more than one.
    plate_id: int | None = None
    #: How it ended, once a later start of the same run waited for it (`lan_wait`): the
    #: printer has moved on to that start since, so its report no longer says. ``None``
    #: while it is the printer's print, read from the report.
    ended: Stage | None = None
    ended_message: str | None = None


class LanStopped(BaseModel):
    """Why a run started none of the plates it still had to start over the LAN (#4541):
    a start or a wait that ``failed``, or a cancel."""

    stage: Literal["failed", "cancelled"]
    message: str
