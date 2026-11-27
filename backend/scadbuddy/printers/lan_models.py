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
    #: When the report that showed its end was seen (#8321), set with ``ended``. An
    #: order attempt reads its seconds by it (`orders/archives.py` `lan_facts`). ``None``
    #: while ``ended`` is, and on a start from before it.
    ended_at: datetime | None = None
    #: The plate's sliced grams per filament slot (#8321), from its upload
    #: (`LanUploaded.grams_by_slot`), for every slot whatever its tray holds. Empty on a
    #: start from before it, or for a file that does not say.
    grams_by_slot: dict[int, float] = Field(default_factory=dict)
    #: The run that started it (#4543): its rack picks are keyed by the run and
    #: ``subtask_name``, and settled by ``lan_wait`` as it waits it out. ``None`` on a
    #: start from before it.
    run_id: str | None = None


class LanStopped(BaseModel):
    """Why a run started none of the plates it still had to start over the LAN (#4541):
    a start or a wait that ``failed``, or a cancel."""

    stage: Literal["failed", "cancelled"]
    message: str


#: The ``gcode_state``s in which the printer holds a job and refuses a ``project_file``
#: ``ERROR STATE`` (#6871). ``RUNNING`` was measured on the H2C on 2026-11-21: the
#: refusal came with HMS ``0500_4004`` "Device is busy and cannot start new task".
#: ``PREPARE``, ``SLICING`` and ``PAUSE`` hold a job the same way (OpenBambuAPI
#: ``mqtt.md``); not measured.
BUSY_STATES = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE"})
#: The ended states whose plate may still hold the print (#6871): a run waits for a
#: cleared plate here only when ``lan_require_plate_clear`` asks it to.
ENDED_STATES = frozenset({"FINISH", "FAILED"})
#: A printer with no print at all (#6871): neither busy nor holding an ended one.
IDLE_STATES = frozenset({"IDLE"})


class LanQueued(BaseModel):
    """Why a native run waits before its first print (#6871): the printer was busy when
    the run read it, before any upload. It runs another print (``job``, which the run
    waits out), or holds an ended one whose plate must be cleared (``job`` ``None``). The
    run starts once the printer is free and someone says the plate is clear."""

    #: The printer's ``gcode_state`` the run read: one of `BUSY_STATES` while it prints,
    #: one of `ENDED_STATES` while its plate waits.
    state: str
    #: The print the run waits for, as the report named it when it was read.
    job: LanStarted | None = None
    #: Its place among ScadBuddy's runs queued on the printer, from 1, when it was
    #: read; ``None`` when that could not be read.
    place: int | None = None


def busy_with(
    report: dict[str, Any], received_at: datetime, *, plate_clear: bool
) -> LanQueued | None:
    """Whether a printer reporting ``report`` cannot take a print now (#6871): it runs
    one (`BUSY_STATES`), or, with ``plate_clear``, holds an ended one whose plate must be
    cleared first (`ENDED_STATES`). ``None`` when it can."""
    state = report.get("gcode_state")
    if not isinstance(state, str):
        return None
    if state in BUSY_STATES:
        name = report.get("subtask_name")
        job = LanStarted(subtask_name=name if isinstance(name, str) else "", started_at=received_at)
        return LanQueued(state=state, job=job)
    if plate_clear and state in ENDED_STATES:
        return LanQueued(state=state)
    return None
