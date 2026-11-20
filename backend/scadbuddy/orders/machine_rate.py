"""A machine rate per registry printer (#4625, order operations spec 2026-11-07 §6).

``rate = sum tasks (minutes x labour rate / 60 + consumables) / interval_hours
       + sum wear (cost / life_hours) + watts / 1000 x kWh price + purchase / life_hours``

Every amount is in the shop's ``orders_currency``, as the price book's rates are. Each
task's, part's and term's figure per hour is rounded half up to 0.0001 once
(:data:`RATE_PLACES`), and every sum adds the rounded figures, so a breakdown always adds
up to its rate. A task counts only with an interval in print-hours: Bambuddy's calendar
tasks (``interval_type`` ``days``) and its disabled ones are listed, never counted.

The inputs are three tables of the CRUD service's own (``printer_cost_profiles``,
``printer_wear_parts``, ``printer_maintenance_costs``), keyed by the registry printer. A
printer linked to Bambuddy takes its tasks and their intervals from Bambuddy's
maintenance tracker, read through the orders seam (``integrations.py``), and keeps the
intervals of the tasks it counts (and clears the rest); when Bambuddy does not answer,
the rate uses those and says so (``intervals`` ``stored``), counting only the tasks the
last read counted, so it is the live rate as last read. A printer only the LAN reaches
has its intervals typed.
A printer with no profile is priced at the book's ``machine_rate_per_hour``.

Actuals (#5774, spec 2026-11-17-machine-rate-actuals): two more tables of the CRUD
service's own, ``printer_maintenance_events`` and ``printer_wear_replacements``, log what
really happened, each event with the printer's print-hour counter then (`hours_source`).
Each row computes an actual per hour over the last ``actuals_window`` usable gaps or
replacements (`task_actual`, `part_actual`) and a confidence; a row set to ``actual``
prices by it once it has that many, else by its estimate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Self

import psycopg
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.orders.integrations import (
    MaintenanceDone,
    MaintenanceTask,
    NotLinkedError,
    OrderIntegrations,
)
from scadbuddy.orders.models import (
    ActualWhy,
    HoursSource,
    IntervalSource,
    MachineRateBreakdown,
    MaintenancePart,
    NotCounted,
    PricedBy,
    SkippedGap,
    WearPartRate,
    _places,
)
from scadbuddy.printers import run_hours
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.store import PrinterStore, printer_uuid

logger = logging.getLogger(__name__)

#: Each figure per hour is rounded to this, once.
RATE_PLACES = Decimal("0.0001")
MINUTES_PER_HOUR = Decimal(60)
WATTS_PER_KW = Decimal(1000)
#: How many wear parts or tasks one printer takes.
MAX_ROWS = 50

#: ``numeric(14, 4)``: a price or a cost.
Amount = Annotated[str, _places(4, 10**10)]
#: ``numeric(10, 1)`` above 0: a life or an interval in print-hours.
Hours = Annotated[str, _places(1, 10**9, positive=True)]
#: ``numeric(8, 1)``: watts or minutes.
Tenths = Annotated[str, _places(1, 10**7)]
#: ``numeric(10, 4)``: the price of a kWh.
KwhPrice = Annotated[str, _places(4, 10**6)]
#: ``numeric(12, 4)``: a labour rate.
LabourRate = Annotated[str, _places(4, 10**8)]
#: ``numeric(10, 1)`` at or above 0: a counter reading, or the LAN counter's offset.
Reading = Annotated[str, _places(1, 10**9)]
#: #5774 §3.3: the window's default and its bounds (``printer_cost_profiles.actuals_window``).
DEFAULT_WINDOW = 3
MAX_WINDOW = 20
#: §3.6: the LAN counter's offset for a new profile, or a printer with none.
DEFAULT_LAN_OFFSET = "0"


class CostProfile(BaseModel):
    """What a printer cost and costs to run (§6.1). A null ``life_hours`` is no
    depreciation; a null ``labour_rate_per_hour`` is the shop's
    ``pricing_labour_rate_per_hour``."""

    model_config = ConfigDict(extra="forbid")

    purchase_price: Amount = "0"
    life_hours: Hours | None = None
    watts: Tenths = "0"
    kwh_price: KwhPrice = "0"
    labour_rate_per_hour: LabourRate | None = None
    #: #5774 (spec 2026-11-17-machine-rate-actuals §3.3): how many usable gaps or
    #: replacements a row needs before its actual prices. Left out of a `PUT`: kept as
    #: stored (``DEFAULT_WINDOW`` for a new profile), so a client that does not know it
    #: never resets it.
    actuals_window: int | None = Field(default=None, ge=1, le=MAX_WINDOW)
    #: §3.6: the hours a LAN-linked printer ran before ScadBuddy's counter watched it.
    #: Left out: kept as stored (``DEFAULT_LAN_OFFSET`` for a new profile).
    lan_hours_offset: Reading | None = None

    @property
    def window(self) -> int:
        return self.actuals_window or DEFAULT_WINDOW


#: A name the row had before this edit: its events move to the new name (§4).
RenamedFrom = Annotated[str, Field(min_length=1, max_length=100)]


class WearPart(BaseModel):
    """A part that wears out with print-hours: a nozzle, a plate, PTFE tube, a filter."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    cost: Amount
    life_hours: Hours
    #: #5774 §3.5: which figure prices it. Left out of a `PUT`: kept as stored (a new row
    #: starts at ``estimate``).
    priced_by: PricedBy | None = None
    renamed_from: RenamedFrom | None = None


class MaintenanceCost(BaseModel):
    """One occurrence of a maintenance task: the minutes it takes and its consumables.
    ``interval_hours`` is typed for a printer Bambuddy does not track. A linked one's
    tasks are priced at Bambuddy's intervals: a task read from it keeps the interval read,
    whatever is sent, and one not read (yet) is not counted (``not_read``)."""

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=100)
    minutes: Tenths = "0"
    consumables: Amount = "0"
    interval_hours: Hours | None = None
    #: As `WearPart.priced_by`.
    priced_by: PricedBy | None = None
    renamed_from: RenamedFrom | None = None


def _unique(names: Sequence[str], what: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"two {what} are named {name!r}")
        seen.add(name)


def _renames(rows: Sequence[tuple[str, str | None]], what: str) -> None:
    """Each ``renamed_from`` names one old row, and never a name a row has now: that
    row's events would move."""
    names = {name for name, _ in rows}
    seen: set[str] = set()
    for name, old in rows:
        if old is None or old == name:
            continue
        if old in names or old in seen:
            raise ValueError(f"renamed_from {old!r} of {what} is another row's name")
        seen.add(old)


class MachineRateInputs(BaseModel):
    """`PUT /printers/{id}/machine-rate`: a printer's inputs, replaced whole."""

    model_config = ConfigDict(extra="forbid")

    profile: CostProfile
    wear_parts: list[WearPart] = Field(default_factory=list, max_length=MAX_ROWS)
    maintenance: list[MaintenanceCost] = Field(default_factory=list, max_length=MAX_ROWS)

    @model_validator(mode="after")
    def _names(self) -> Self:
        _unique([part.name for part in self.wear_parts], "wear parts")
        _unique([cost.task for cost in self.maintenance], "maintenance tasks")
        _renames([(p.name, p.renamed_from) for p in self.wear_parts], "wear parts")
        _renames([(c.task, c.renamed_from) for c in self.maintenance], "maintenance tasks")
        return self


# -- logged events (#5774, spec 2026-11-17-machine-rate-actuals §4, §5) --------------------

EventKind = Literal["task", "part"]
EventOrigin = Literal["bambuddy", "scadbuddy"]
#: ``notes``' bound (``text CHECK (length(notes) <= 1000)``).
NOTES_MAX = 1000
#: A writer's key, so a retry writes once.
Source = Annotated[str, Field(min_length=1, max_length=200)]
EventName = Annotated[str, Field(min_length=1, max_length=100)]
#: An imported task event's ``source`` is this and its Bambuddy history id (§4): the
#: import's key, which no ScadBuddy event may take.
IMPORT_SOURCE = "bambuddy:"
#: An imported event's ``created_by``.
IMPORTED_BY = "bambuddy"
#: The bounds of an imported row's ``bambuddy_history_id integer``.
INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1
#: The least ``hours_at_maintenance`` that rounds to 1e9, past ``numeric(10, 1)``.
HOURS_AT_LIMIT = 999_999_999.95
#: #6663 review: the seconds the housekeeping sweep (:meth:`MachineRates.read_logs`) may
#: spend reading logs, well inside its activity's start-to-close
#: (`workflows.housekeeping.MAINTENANCE_IMPORT_TIMEOUT`, 10 min). It starts no printer
#: past it and cuts a read that runs past it; the printers left wait for the next tick.
READ_LOGS_BUDGET = 7 * 60.0


def storable_task(name: str) -> bool:
    """Whether a Bambuddy task name fits ``printer_maintenance_events.task`` (``CHECK
    (length(task) BETWEEN 1 AND 100)``) and can be sent as text: no NUL, and UTF-8 (a
    JSON reply can hold a lone surrogate). A row of any other name is skipped."""
    if not 1 <= len(name) <= 100 or "\x00" in name:
        return False
    try:
        name.encode()
    except UnicodeEncodeError:
        return False
    return True


def storable_notes(notes: str | None) -> str | None:
    """A Bambuddy note as ``notes`` can keep it: its NULs dropped, a lone surrogate as
    ``?``, then cut to `NOTES_MAX` (``CHECK (length(notes) <= 1000)``)."""
    if notes is None:
        return None
    return notes.replace("\x00", "").encode(errors="replace").decode()[:NOTES_MAX]


class MachineRateEvent(BaseModel):
    """A maintenance task done (``kind`` ``task``) or a wear part replaced (``part``).
    ``hours_at`` is the printer's counter then, on ``hours_source``'s scale; a task's
    null ``minutes`` or ``consumables`` is priced at the task's typed figure; a part's
    ``hours_used`` is typed (a correction), else derived from the replacement before."""

    id: str
    kind: EventKind
    name: str
    at: datetime
    hours_at: str | None = None
    hours_source: HoursSource | None = None
    minutes: str | None = None
    consumables: str | None = None
    cost: str | None = None
    hours_used: str | None = None
    notes: str | None = None
    origin: EventOrigin
    bambuddy_history_id: int | None = None
    source: str
    created_at: datetime
    created_by: str


class MachineRateEventCreate(BaseModel):
    """`POST /printers/{id}/machine-rate/events`: a ScadBuddy event. ``hours_at`` left
    out is filled from the printer's counter (§3.2); one sent is a correction on that
    counter. A part takes ``cost`` (and may take ``hours_used``); a task takes
    ``minutes`` and ``consumables`` (null: the typed ones)."""

    model_config = ConfigDict(extra="forbid")

    kind: EventKind
    name: EventName
    #: When it was done, with its zone; now when left out.
    at: AwareDatetime | None = None
    hours_at: Reading | None = None
    hours_used: Hours | None = None
    minutes: Tenths | None = None
    consumables: Amount | None = None
    cost: Amount | None = None
    notes: str | None = Field(default=None, max_length=NOTES_MAX)
    #: A retry with the same ``source`` returns the first event (200). Never
    #: ``bambuddy:…``, the import's key (§4).
    source: Source | None = None

    @field_validator("source")
    @classmethod
    def _not_imported(cls, value: str | None) -> str | None:
        if value is not None and value.lower().startswith(IMPORT_SOURCE):
            raise ValueError(f"a source starting {IMPORT_SOURCE!r} is the Bambuddy import's")
        return value

    @model_validator(mode="after")
    def _fields(self) -> Self:
        if self.kind == "part":
            if self.cost is None:
                raise ValueError("a replaced part needs its cost")
            if self.minutes is not None or self.consumables is not None:
                raise ValueError("a replaced part takes no minutes or consumables")
        elif self.cost is not None or self.hours_used is not None:
            raise ValueError("a task takes no cost or hours_used")
        return self


class MachineRateEventPatch(BaseModel):
    """`PATCH /printers/{id}/machine-rate/events/{event_id}`: the fields sent are set; a
    null clears one (a task's minutes or consumables then price at the typed ones). An
    imported event's time, counter source and Bambuddy id stay as read."""

    model_config = ConfigDict(extra="forbid")

    minutes: Tenths | None = None
    consumables: Amount | None = None
    cost: Amount | None = None
    hours_at: Reading | None = None
    hours_used: Hours | None = None
    notes: str | None = Field(default=None, max_length=NOTES_MAX)


class MachineRateEventPage(BaseModel):
    """`GET /printers/{id}/machine-rate/events`: newest first, ``next`` the cursor of
    the page after (null at the end)."""

    events: list[MachineRateEvent]
    next: str | None = None


#: A page of `MachineRateStore.list_events`, and its most.
EVENTS_PAGE = 50
EVENTS_PAGE_MAX = 200


class LanCount(BaseModel):
    """ScadBuddy's own counter for a LAN-linked printer (§3.6): ``hours`` is the reading
    events get, ``run_hours`` plus ``offset_hours``; ``unseen_hours`` were not counted (a
    gap that ended in another state or print)."""

    hours: str
    run_hours: str
    offset_hours: str
    unseen_hours: str
    counted_to: datetime
    started_at: datetime


class EventFieldError(ValueError):
    """A patch sets a field its event's kind has not (a task's cost, a part's minutes), or
    clears a part's cost."""

    def __init__(self, kind: EventKind, fields: list[str]) -> None:
        super().__init__(f"a {kind} event cannot set {', '.join(fields)}")
        self.fields = fields


class ImportedEventError(Exception):
    """An event imported from Bambuddy cannot be deleted (the next import would bring it
    back), nor its reading cleared (its counter source stays as read)."""


class EventSourceTakenError(Exception):
    """A ``source`` already names an event of the other kind on the printer."""


def _uuid(text: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    at, _, ident = cursor.rpartition("|")
    key = _uuid(ident)
    if key is None:
        raise ValueError("not a cursor this route gave")
    return datetime.fromisoformat(at), key


EVENTS_SQL = (
    "SELECT * FROM ("
    "SELECT id, printer, 'task' AS kind, task AS name, performed_at AS at, hours_at,"
    " hours_source, minutes, consumables, NULL::numeric AS cost, NULL::numeric AS hours_used,"
    " notes, origin, bambuddy_history_id, source, created_at, created_by"
    " FROM printer_maintenance_events"
    " UNION ALL "
    "SELECT id, printer, 'part', part, replaced_at, hours_at, hours_source, NULL, NULL, cost,"
    " hours_used, notes, 'scadbuddy', NULL, source, created_at, created_by"
    " FROM printer_wear_replacements"
    ") AS events"
)


_EVENT_TABLES: dict[EventKind, str] = {
    "task": "printer_maintenance_events",
    "part": "printer_wear_replacements",
}


def _locked_event(
    conn: Connection[DictRow], key: uuid.UUID, ident: uuid.UUID
) -> MachineRateEvent | None:
    """The event, its row locked until the transaction ends, so a check made on it
    holds for the write that follows."""
    for table in _EVENT_TABLES.values():
        conn.execute(
            f"SELECT 1 FROM {table} WHERE printer = %s AND id = %s FOR UPDATE", (key, ident)
        )
    row = conn.execute(f"{EVENTS_SQL} WHERE printer = %s AND id = %s", (key, ident)).fetchone()
    return None if row is None else _event(row)


def _event(row: DictRow) -> MachineRateEvent:
    return MachineRateEvent(
        id=str(row["id"]),
        kind=row["kind"],
        name=row["name"],
        at=row["at"],
        hours_at=_number(row["hours_at"]),
        hours_source=row["hours_source"],
        minutes=_number(row["minutes"]),
        consumables=_number(row["consumables"]),
        cost=_number(row["cost"]),
        hours_used=_number(row["hours_used"]),
        notes=row["notes"],
        origin=row["origin"],
        bambuddy_history_id=row["bambuddy_history_id"],
        source=row["source"],
        created_at=row["created_at"],
        created_by=row["created_by"],
    )


def _decimal(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def events_of(logged: Sequence[MachineRateEvent]) -> Events:
    """`compute`'s ``events`` from the stored rows."""
    events = Events()
    for e in logged:
        if e.kind == "task":
            events.tasks.setdefault(e.name, []).append(
                TaskEvent(
                    id=e.id,
                    at=e.at,
                    hours_at=_decimal(e.hours_at),
                    hours_source=e.hours_source,
                    minutes=_decimal(e.minutes),
                    consumables=_decimal(e.consumables),
                )
            )
        else:
            assert e.cost is not None
            events.parts.setdefault(e.name, []).append(
                Replacement(
                    id=e.id,
                    at=e.at,
                    hours_at=_decimal(e.hours_at),
                    hours_source=e.hours_source,
                    cost=Decimal(e.cost),
                    hours_used=_decimal(e.hours_used),
                )
            )
    return events


def _text(number: Decimal) -> str:
    """The shortest decimal string of ``number``: ``0.1200`` reads ``0.12``."""
    return format(number.normalize(), "f") if number else "0"


def _rounded(number: Decimal) -> Decimal:
    return number.quantize(RATE_PLACES, rounding=ROUND_HALF_UP)


#: A kept interval's places (``printer_maintenance_costs.interval_hours``, ``numeric(10, 1)``).
INTERVAL_PLACES = Decimal("0.1")


def _hours(value: float) -> str | None:
    """An interval Bambuddy gave as a float, to the tenth of an hour its row keeps (``str``
    once: never the float's binary expansion), so the live rate is the one stored. None
    for one that rounds to nothing: not counted."""
    hours = Decimal(str(value)).quantize(INTERVAL_PLACES, rounding=ROUND_HALF_UP)
    return _text(hours) if hours > 0 else None


def reading(value: float) -> Decimal:
    """A counter reading Bambuddy gave as a float (``current_hours``,
    ``hours_at_maintenance``), to the tenth its column keeps, rounded half up. Unlike
    `_hours` it keeps 0: a first event at 0.0 h has a known counter (#5774 §3.2)."""
    return Decimal(str(value)).quantize(INTERVAL_PLACES, rounding=ROUND_HALF_UP)


# -- actuals (#5774, spec 2026-11-17-machine-rate-actuals §3.3, §3.4) ---------------------


@dataclass(frozen=True)
class TaskEvent:
    """A maintenance task done (``printer_maintenance_events``), as `compute` reads it.
    Null ``minutes`` or ``consumables``: the task's typed figure prices it."""

    id: str
    at: datetime
    hours_at: Decimal | None
    hours_source: HoursSource | None
    minutes: Decimal | None = None
    consumables: Decimal | None = None


@dataclass(frozen=True)
class Replacement:
    """A wear part replaced (``printer_wear_replacements``): what the new part cost and,
    when typed, the hours the removed one ran."""

    id: str
    at: datetime
    hours_at: Decimal | None
    hours_source: HoursSource | None
    cost: Decimal
    hours_used: Decimal | None = None


@dataclass
class Events:
    """A printer's events, by task and by part name."""

    tasks: dict[str, list[TaskEvent]] = field(default_factory=dict)
    parts: dict[str, list[Replacement]] = field(default_factory=dict)


@dataclass
class Actual:
    """One row's actual over its window: the usable gaps' (or replacements') sums, the
    events walked (newest first, the skipped ones between them) and what was skipped."""

    need: int
    have: int = 0
    cost: Decimal = Decimal(0)
    hours: Decimal = Decimal(0)
    partly_typed: bool = False
    window: list[str] = field(default_factory=list)
    skipped: list[SkippedGap] = field(default_factory=list)

    @property
    def per_hour(self) -> Decimal | None:
        return _rounded(self.cost / self.hours) if self.have else None

    @property
    def confidence(self) -> int:
        """``floor(100 x min(have, N) / N)`` (§3.4)."""
        return 100 * min(self.have, self.need) // self.need

    @property
    def why(self) -> ActualWhy | None:
        if self.have >= self.need:
            return None
        return "no_events" if self.have == 0 else "too_few"

    @property
    def ready(self) -> bool:
        return self.have >= self.need


def _gap_problem(
    newer: HoursSource | None, older: HoursSource | None, hours: Decimal
) -> Literal["not_positive", "counter_changed"] | None:
    if newer != older:
        return "counter_changed"
    if hours <= 0:
        return "not_positive"
    return None


def task_actual(
    events: Sequence[TaskEvent], cost: MaintenanceCost | None, labour: Decimal, need: int
) -> Actual:
    """A task's actual (§3.3): over the last ``need`` usable gaps between two events with
    a reading, ordered by reading then time, ``sum(cost_i) / sum(gap_i)``, where each
    gap's cost is its newer event's own figures, else the typed ones."""
    typed_minutes = Decimal(cost.minutes) if cost else Decimal(0)
    typed_consumables = Decimal(cost.consumables) if cost else Decimal(0)
    read = sorted(
        (e for e in events if e.hours_at is not None),
        key=lambda e: (e.hours_at, e.at, e.id),
    )
    actual = Actual(need=need)
    for index in range(len(read) - 1, -1, -1):
        newer = read[index]
        actual.window.append(newer.id)
        if actual.have >= need or index == 0:
            break
        older = read[index - 1]
        assert newer.hours_at is not None and older.hours_at is not None
        gap = newer.hours_at - older.hours_at
        problem = _gap_problem(newer.hours_source, older.hours_source, gap)
        if problem is not None:
            actual.skipped.append(SkippedGap(event=newer.id, previous=older.id, reason=problem))
            continue
        minutes = typed_minutes if newer.minutes is None else newer.minutes
        consumables = typed_consumables if newer.consumables is None else newer.consumables
        if newer.minutes is None or newer.consumables is None:
            actual.partly_typed = True
        actual.cost += minutes * labour / MINUTES_PER_HOUR + consumables
        actual.hours += gap
        actual.have += 1
    return actual


def part_actual(replacements: Sequence[Replacement], need: int) -> Actual:
    """A wear part's actual (§3.3): over the last ``need`` replacements with a usable
    ``hours_used``, ``sum(cost_r) / sum(hours_used_r)``. Replacements are ordered by time
    (one may have no reading). ``hours_used`` is the typed one, else the reading less the
    replacement before's; a replacement with neither (the first, or one after a
    replacement with no reading) only marks where the next part's life starts.

    The window lists, as a task's does, every event a counted or skipped gap reads: the
    oldest counted replacement's base is listed too when its ``hours_used`` is derived."""
    ordered = sorted(replacements, key=lambda r: (r.at, r.hours_at or 0, r.id))
    actual = Actual(need=need)
    for index in range(len(ordered) - 1, -1, -1):
        newer = ordered[index]
        actual.window.append(newer.id)
        used = newer.hours_used
        base: Replacement | None = None
        if used is None:
            base = ordered[index - 1] if index > 0 else None
            if base is None or base.hours_at is None or newer.hours_at is None:
                continue
            used = newer.hours_at - base.hours_at
            problem = _gap_problem(newer.hours_source, base.hours_source, used)
            if problem is not None:
                actual.skipped.append(SkippedGap(event=newer.id, previous=base.id, reason=problem))
                continue
        actual.cost += newer.cost
        actual.hours += used
        actual.have += 1
        if actual.have >= need:
            if base is not None:
                actual.window.append(base.id)
            break
    return actual


def _task_part(
    task: str,
    interval: str | None,
    interval_type: Literal["hours", "days"],
    cost: MaintenanceCost | None,
    labour: Decimal,
    why: NotCounted | None,
    actual: Actual,
) -> MaintenancePart:
    minutes = Decimal(cost.minutes) if cost else Decimal(0)
    consumables = Decimal(cost.consumables) if cost else Decimal(0)
    priced_by: PricedBy = (cost.priced_by if cost else None) or "estimate"
    if why is None and interval is None:
        why = "no_interval"
    estimate: Decimal | None = None
    if why is None:
        assert interval is not None
        occurrence = minutes * labour / MINUTES_PER_HOUR + consumables
        estimate = _rounded(occurrence / Decimal(interval))
    # A task not counted never prices, by either figure (§3.3: `why` keeps its meaning).
    basis: PricedBy = (
        "actual" if priced_by == "actual" and why is None and actual.ready else "estimate"
    )
    actual_per_hour = actual.per_hour
    per_hour = actual_per_hour if basis == "actual" else estimate
    return MaintenancePart(
        task=task,
        interval_hours=interval,
        interval_type=interval_type,
        minutes=_text(minutes),
        consumables=_text(consumables),
        per_hour=_text(per_hour or Decimal(0)),
        counted=why is None,
        why=why,
        priced_by=priced_by,
        basis=basis,
        estimate_per_hour=_number(estimate),
        actual_per_hour=_number(actual_per_hour),
        have=actual.have,
        need=actual.need,
        confidence=actual.confidence,
        partly_typed=actual.partly_typed,
        actual_why=actual.why,
        skipped_gaps=actual.skipped,
        actual_interval_hours=(
            _text((actual.hours / actual.have).quantize(INTERVAL_PLACES, ROUND_HALF_UP))
            if actual.have
            else None
        ),
        actual_cost_per_occurrence=(
            _text(_rounded(actual.cost / actual.have)) if actual.have else None
        ),
    )


def _wear_part(part: WearPart, actual: Actual) -> WearPartRate:
    estimate = _rounded(Decimal(part.cost) / Decimal(part.life_hours))
    priced_by: PricedBy = part.priced_by or "estimate"
    basis: PricedBy = "actual" if priced_by == "actual" and actual.ready else "estimate"
    actual_per_hour = actual.per_hour
    per_hour = actual_per_hour if basis == "actual" and actual_per_hour is not None else estimate
    return WearPartRate(
        name=part.name,
        cost=_text(Decimal(part.cost)),
        life_hours=_text(Decimal(part.life_hours)),
        per_hour=_text(per_hour),
        priced_by=priced_by,
        basis=basis,
        estimate_per_hour=_text(estimate),
        actual_per_hour=_number(actual_per_hour),
        have=actual.have,
        need=actual.need,
        confidence=actual.confidence,
        actual_why=actual.why,
        skipped_gaps=actual.skipped,
        actual_hours_used=(
            _text((actual.hours / actual.have).quantize(INTERVAL_PLACES, ROUND_HALF_UP))
            if actual.have
            else None
        ),
    )


def compute(
    profile: CostProfile,
    wear: Sequence[WearPart],
    costs: Sequence[MaintenanceCost],
    tasks: Sequence[MaintenanceTask] | None,
    *,
    default_labour: str,
    currency: str,
    intervals: IntervalSource,
    printer: str | None = None,
    printer_name: str | None = None,
    intervals_read_at: datetime | None = None,
    read_tasks: Collection[str] | None = None,
    events: Events | None = None,
) -> MachineRateBreakdown:
    """The rate and its parts. ``tasks`` are Bambuddy's (a linked printer read now):
    the tasks are its, each with its interval, priced with the cost row of the same
    name. ``None`` prices every cost row with its own ``interval_hours``: typed, or, for
    a linked printer, the last read from Bambuddy, where ``read_tasks`` names the rows the
    last read counted and any other row is ``not_read``.

    ``events`` are the printer's logged ones (#5774): each row's actual over the
    profile's ``actuals_window`` (§3.3) prices a row set to ``actual`` once it has that
    many usable gaps or replacements; until then its estimate prices (§3.4)."""
    labour = Decimal(profile.labour_rate_per_hour or default_labour or "0")
    logged = events or Events()
    need = profile.window

    def actual_of(name: str, cost: MaintenanceCost | None) -> Actual:
        return task_actual(logged.tasks.get(name, ()), cost, labour, need)

    parts: list[MaintenancePart] = []
    if tasks is None:
        for cost in costs:
            unread = read_tasks is not None and cost.task not in read_tasks
            parts.append(
                _task_part(
                    cost.task,
                    cost.interval_hours,
                    "hours",
                    cost,
                    labour,
                    "not_read" if unread else None,
                    actual_of(cost.task, cost),
                )
            )
    else:
        by_task = {cost.task: cost for cost in costs}
        for task in tasks:
            why: NotCounted | None = None
            if not task.enabled:
                why = "disabled"
            elif task.interval_type == "days":
                why = "calendar"
            interval = _hours(task.interval_hours)
            if why is None and interval is None:
                why = "no_interval"
            row = by_task.pop(task.task, None)
            parts.append(
                _task_part(
                    task.task,
                    interval,
                    task.interval_type,
                    row,
                    labour,
                    why,
                    actual_of(task.task, row),
                )
            )
        for cost in by_task.values():
            parts.append(
                _task_part(
                    cost.task,
                    cost.interval_hours,
                    "hours",
                    cost,
                    labour,
                    "not_in_bambuddy",
                    actual_of(cost.task, cost),
                )
            )
    maintenance = sum((Decimal(part.per_hour) for part in parts), Decimal(0))

    wear_rates = [
        _wear_part(part, part_actual(logged.parts.get(part.name, ()), need)) for part in wear
    ]
    wear_total = sum((Decimal(part.per_hour) for part in wear_rates), Decimal(0))
    # §3.4: the lowest confidence of the rows a person set to `actual` that can price (a
    # task not counted cannot), or null when none is.
    trusted = [p.confidence for p in parts if p.priced_by == "actual" and p.counted] + [
        p.confidence for p in wear_rates if p.priced_by == "actual"
    ]
    confidences = [c for c in trusted if c is not None]
    power = _rounded(Decimal(profile.watts) / WATTS_PER_KW * Decimal(profile.kwh_price))
    depreciation = (
        _rounded(Decimal(profile.purchase_price) / Decimal(profile.life_hours))
        if profile.life_hours is not None
        else Decimal(0)
    )
    return MachineRateBreakdown(
        printer=printer,
        printer_name=printer_name,
        source="profile",
        rate_per_hour=_text(maintenance + wear_total + power + depreciation),
        currency=currency,
        labour_rate_per_hour=_text(labour),
        maintenance=parts,
        maintenance_per_hour=_text(maintenance),
        wear=wear_rates,
        wear_per_hour=_text(wear_total),
        watts=_text(Decimal(profile.watts)),
        kwh_price=_text(Decimal(profile.kwh_price)),
        power_per_hour=_text(power),
        purchase_price=_text(Decimal(profile.purchase_price)),
        life_hours=None if profile.life_hours is None else _text(Decimal(profile.life_hours)),
        depreciation_per_hour=_text(depreciation),
        intervals=intervals,
        intervals_read_at=intervals_read_at,
        actuals=any(p.basis == "actual" for p in [*parts, *wear_rates]),
        actuals_window=need,
        confidence=min(confidences) if confidences else None,
    )


def _number(value: Decimal | None) -> str | None:
    return None if value is None else _text(value)


@dataclass
class Stored:
    """A printer's rows: its profile (None for none), parts and task costs, when its
    intervals were last read from Bambuddy (the latest of its rows'), the tasks whose
    interval was read, and whether the printer is linked to Bambuddy."""

    profile: CostProfile | None
    wear: list[WearPart]
    costs: list[MaintenanceCost]
    read_at: datetime | None
    read_tasks: set[str] = field(default_factory=set)
    linked: bool = False
    serial: str | None = None
    #: #5774: the logged task events and part replacements each row's window needs (a
    #: task's with a reading only), oldest first: `events` prices as every event would.
    logged: list[MachineRateEvent] = field(default_factory=list)
    #: How many events each kind and name has, all of them.
    counts: dict[tuple[EventKind, str], int] = field(default_factory=dict)
    #: #5774 PR 3 (§3.7): when the printer's Bambuddy log was last read in full.
    events_read_at: datetime | None = None

    @property
    def window(self) -> int:
        return self.profile.window if self.profile else DEFAULT_WINDOW

    @property
    def stored_reads(self) -> set[str] | None:
        """`compute`'s ``read_tasks``: a linked printer's read tasks, else None."""
        return self.read_tasks if self.linked else None

    @property
    def events(self) -> Events:
        """`compute`'s ``events``."""
        return events_of(self.logged)


#: The events `task_actual` reads: a task's with a reading, every part's.
COUNTED = "(kind = 'part' OR hours_at IS NOT NULL)"

#: Each row's newest events (``COUNTED``), in the order `task_actual` (reading, time, id)
#: or `part_actual` (time, reading, id) walks them: `_fetched` of them per row.
WINDOWED_SQL = (
    "SELECT * FROM (SELECT e.*, row_number() OVER (PARTITION BY e.printer, e.kind, e.name"
    " ORDER BY CASE WHEN e.kind = 'task' THEN e.hours_at END DESC, e.at DESC,"
    " CASE WHEN e.kind = 'part' THEN coalesce(e.hours_at, 0) END DESC, e.id DESC) AS rank,"
    " 2 * coalesce(c.actuals_window, %s) + 1 AS fetched"
    f" FROM ({EVENTS_SQL}) AS e LEFT JOIN printer_cost_profiles AS c ON c.printer = e.printer"
    " WHERE (e.kind = 'part' OR e.hours_at IS NOT NULL){where}"
    ") AS ranked WHERE rank <= fetched ORDER BY at, id"
)


def _fetched(window: int) -> int:
    """How many of a row's newest events `WINDOWED_SQL` reads (its ``fetched``, the same
    sum): N + 1 make N gaps, and the rest leave room for skipped ones before a row must
    be read whole."""
    return 2 * window + 1


def _cut_short(held: Stored) -> list[tuple[EventKind, str]]:
    """The rows whose read stopped at `_fetched` events before their window was full: a
    walk past the oldest event read could have found a gap the read left out. A row that
    filled its window within the events read prices as every event would."""
    events, need = held.events, held.window
    limit = _fetched(need)
    short: list[tuple[EventKind, str]] = []
    for name, tasks in events.tasks.items():
        if len(tasks) >= limit and not task_actual(tasks, None, Decimal(0), need).ready:
            short.append(("task", name))
    for name, replacements in events.parts.items():
        if len(replacements) >= limit and not part_actual(replacements, need).ready:
            short.append(("part", name))
    return short


class MachineRateStore:
    """The machine rate's tables, plain CRUD (the CRUD service's own, #5295): the inputs
    and, since #5774, the logged events."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    def stored(self, printer: str) -> Stored:
        key = printer_uuid(printer)
        if key is None:
            return Stored(None, [], [], None)
        return self._read([key]).get(str(key), Stored(None, [], [], None))

    def stored_all(self) -> dict[str, Stored]:
        """Every profiled printer's rows, by registry id: one query per table."""
        return self._read(None)

    def _read(self, keys: list[uuid.UUID] | None) -> dict[str, Stored]:
        """The rows of ``keys``' printers (None: every printer with a profile), one query
        per table whatever their number."""
        where = "" if keys is None else " WHERE printer = ANY(%s)"
        args: tuple[object, ...] = () if keys is None else (keys,)
        with self._pool.connection() as conn:
            profiles = conn.execute(
                "SELECT c.printer, c.purchase_price, c.life_hours, c.watts, c.kwh_price,"
                " c.labour_rate_per_hour, c.actuals_window, c.lan_hours_offset,"
                " p.bambuddy_printer_id IS NOT NULL AS linked,"
                " p.serial"
                " FROM printer_cost_profiles AS c JOIN printers AS p ON p.id = c.printer"
                + ("" if keys is None else " WHERE c.printer = ANY(%s)"),
                args,
            ).fetchall()
            parts = conn.execute(
                "SELECT printer, name, cost, life_hours, priced_by"
                f" FROM printer_wear_parts{where}"
                " ORDER BY printer, position",
                args,
            ).fetchall()
            tasks = conn.execute(
                "SELECT printer, task, minutes, consumables, interval_hours, interval_read_at,"
                f" priced_by FROM printer_maintenance_costs{where} ORDER BY printer, task",
                args,
            ).fetchall()
            logged = conn.execute(
                WINDOWED_SQL.format(where="" if keys is None else " AND e.printer = ANY(%s)"),
                (DEFAULT_WINDOW, *args),
            ).fetchall()
            counts = conn.execute(
                f"SELECT printer, kind, name, count(*) AS events FROM ({EVENTS_SQL}) AS e"
                f"{where} GROUP BY printer, kind, name",
                args,
            ).fetchall()
            reads = conn.execute(
                f"SELECT printer, read_at FROM printer_maintenance_reads{where}", args
            ).fetchall()
            found = self._held(profiles, parts, tasks, logged, counts)
            for r in reads:
                held = found.setdefault(str(r["printer"]), Stored(None, [], [], None))
                held.events_read_at = r["read_at"]
            # A row whose skipped gaps walked past what was read is read whole.
            for printer, held in found.items():
                for kind, name in _cut_short(held):
                    rows = conn.execute(
                        f"{EVENTS_SQL} WHERE printer = %s AND kind = %s AND name = %s"
                        f" AND {COUNTED} ORDER BY at, id",
                        (uuid.UUID(printer), kind, name),
                    ).fetchall()
                    kept = [e for e in held.logged if (e.kind, e.name) != (kind, name)]
                    held.logged = sorted(
                        [*kept, *map(_event, rows)], key=lambda e: (e.at, uuid.UUID(e.id))
                    )
        return found

    @staticmethod
    def _held(
        profiles: list[DictRow],
        parts: list[DictRow],
        tasks: list[DictRow],
        logged: list[DictRow],
        counts: list[DictRow],
    ) -> dict[str, Stored]:
        found: dict[str, Stored] = {}
        for row in profiles:
            fields: dict[str, object] = {
                k: _number(v)
                for k, v in row.items()
                if k not in ("printer", "linked", "serial", "actuals_window")
            }
            fields["actuals_window"] = row["actuals_window"]
            found[str(row["printer"])] = Stored(
                CostProfile.model_validate(fields),
                [],
                [],
                None,
                linked=row["linked"],
                serial=row["serial"],
            )
        for r in parts:
            held = found.setdefault(str(r["printer"]), Stored(None, [], [], None))
            held.wear.append(
                WearPart(
                    name=r["name"],
                    cost=_text(r["cost"]),
                    life_hours=_text(r["life_hours"]),
                    priced_by=r["priced_by"],
                )
            )
        for r in tasks:
            held = found.setdefault(str(r["printer"]), Stored(None, [], [], None))
            held.costs.append(
                MaintenanceCost(
                    task=r["task"],
                    minutes=_text(r["minutes"]),
                    consumables=_text(r["consumables"]),
                    interval_hours=_number(r["interval_hours"]),
                    priced_by=r["priced_by"],
                )
            )
            read_at = r["interval_read_at"]
            if read_at is not None:
                held.read_tasks.add(r["task"])
                if held.read_at is None or read_at > held.read_at:
                    held.read_at = read_at
        for r in logged:
            held = found.setdefault(str(r["printer"]), Stored(None, [], [], None))
            held.logged.append(_event(r))
        for r in counts:
            held = found.setdefault(str(r["printer"]), Stored(None, [], [], None))
            held.counts[r["kind"], r["name"]] = r["events"]
        return found

    def replace(self, printer: str, body: MachineRateInputs) -> None:
        """The printer's inputs, replaced whole in one transaction. On a printer linked to
        Bambuddy, a task whose interval was read from Bambuddy keeps it (and when it was
        read): what is sent for it is ignored. On one unlinked since, what is sent is
        kept, as typed."""
        key = printer_uuid(printer)
        assert key is not None
        profile = body.profile
        with self._pool.connection() as conn, conn.transaction():
            # The profile row first: its lock orders this with remember_intervals, so the
            # intervals read below are not overtaken by a live read meanwhile.
            conn.execute(
                "INSERT INTO printer_cost_profiles (printer, purchase_price, life_hours, watts,"
                " kwh_price, labour_rate_per_hour, actuals_window, lan_hours_offset)"
                " VALUES (%s, %s, %s, %s, %s, %s,"
                " coalesce(%s::integer, %s), coalesce(%s::numeric, %s::numeric))"
                " ON CONFLICT (printer) DO UPDATE SET purchase_price = EXCLUDED.purchase_price,"
                " life_hours = EXCLUDED.life_hours, watts = EXCLUDED.watts,"
                " kwh_price = EXCLUDED.kwh_price,"
                " labour_rate_per_hour = EXCLUDED.labour_rate_per_hour,"
                " actuals_window = coalesce(%s::integer, printer_cost_profiles.actuals_window),"
                " lan_hours_offset = coalesce(%s::numeric, printer_cost_profiles.lan_hours_offset),"
                " updated_at = now()",
                (
                    key,
                    profile.purchase_price,
                    profile.life_hours,
                    profile.watts,
                    profile.kwh_price,
                    profile.labour_rate_per_hour,
                    profile.actuals_window,
                    DEFAULT_WINDOW,
                    profile.lan_hours_offset,
                    DEFAULT_LAN_OFFSET,
                    profile.actuals_window,
                    profile.lan_hours_offset,
                ),
            )
            # #5774 §3.5: a row sent with no `priced_by` keeps the stored choice (under
            # its old name when renamed); a new row starts at `estimate`.
            parts_chosen = {
                r["name"]: r["priced_by"]
                for r in conn.execute(
                    "SELECT name, priced_by FROM printer_wear_parts WHERE printer = %s", (key,)
                ).fetchall()
            }
            tasks_chosen = {
                r["task"]: r["priced_by"]
                for r in conn.execute(
                    "SELECT task, priced_by FROM printer_maintenance_costs WHERE printer = %s",
                    (key,),
                ).fetchall()
            }
            # #5774 §4: a renamed row's events move to its new name, so no history is lost.
            for part in body.wear_parts:
                if part.renamed_from is not None and part.renamed_from != part.name:
                    conn.execute(
                        "UPDATE printer_wear_replacements SET part = %s"
                        " WHERE printer = %s AND part = %s",
                        (part.name, key, part.renamed_from),
                    )
            for cost in body.maintenance:
                if cost.renamed_from is not None and cost.renamed_from != cost.task:
                    conn.execute(
                        "UPDATE printer_maintenance_events SET task = %s"
                        " WHERE printer = %s AND task = %s",
                        (cost.task, key, cost.renamed_from),
                    )
            # Bambuddy's intervals are kept only while it tracks the printer: once
            # unlinked, the intervals sent are typed ones and count.
            read = {
                r["task"]: (r["interval_hours"], r["interval_read_at"])
                for r in conn.execute(
                    "SELECT m.task, m.interval_hours, m.interval_read_at"
                    " FROM printer_maintenance_costs m JOIN printers p ON p.id = m.printer"
                    " WHERE m.printer = %s AND m.interval_read_at IS NOT NULL"
                    " AND p.bambuddy_printer_id IS NOT NULL",
                    (key,),
                ).fetchall()
            }
            conn.execute("DELETE FROM printer_wear_parts WHERE printer = %s", (key,))
            for position, part in enumerate(body.wear_parts):
                conn.execute(
                    "INSERT INTO printer_wear_parts (printer, position, name, cost, life_hours,"
                    " priced_by) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        key,
                        position,
                        part.name,
                        part.cost,
                        part.life_hours,
                        part.priced_by
                        or parts_chosen.get(part.renamed_from or part.name, "estimate"),
                    ),
                )
            conn.execute("DELETE FROM printer_maintenance_costs WHERE printer = %s", (key,))
            for cost in body.maintenance:
                interval, read_at = read.get(cost.task, (cost.interval_hours, None))
                conn.execute(
                    "INSERT INTO printer_maintenance_costs (printer, task, minutes, consumables,"
                    " interval_hours, interval_read_at, priced_by)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        key,
                        cost.task,
                        cost.minutes,
                        cost.consumables,
                        interval,
                        read_at,
                        cost.priced_by
                        or tasks_chosen.get(cost.renamed_from or cost.task, "estimate"),
                    ),
                )

    # -- #5774: logged events and ScadBuddy's counter ----------------------------------

    def add_event(
        self,
        printer: str,
        body: MachineRateEventCreate,
        *,
        hours_at: str | None,
        hours_source: HoursSource | None,
        created_by: str,
    ) -> tuple[MachineRateEvent, bool]:
        """A ScadBuddy event, and whether this call wrote it: a second write of the same
        printer and ``source`` returns the first as it was recorded and writes nothing.
        :class:`EventSourceTakenError` when the source names an event of the other kind:
        each table's ``UNIQUE (printer, source)`` does not span both, so the write looks
        the source up in both under the printer's lock, which two writes of one printer
        take in turn."""
        key = printer_uuid(printer)
        assert key is not None
        source = body.source or f"manual:{uuid.uuid4()}"
        at = body.at or datetime.now(UTC)
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('scadbuddy:machine-rate-events:'"
                " || %s, 0))",
                (str(key),),
            )
            first = conn.execute(
                f"{EVENTS_SQL} WHERE printer = %s AND source = %s", (key, source)
            ).fetchone()
            if first is not None:
                if first["kind"] != body.kind:
                    raise EventSourceTakenError(body.kind, source)
                return _event(first), False
            if body.kind == "task":
                written = conn.execute(
                    "INSERT INTO printer_maintenance_events (id, printer, task, performed_at,"
                    " hours_at, hours_source, minutes, consumables, notes, origin, source,"
                    " created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'scadbuddy',"
                    " %s, %s) ON CONFLICT (printer, source) DO NOTHING",
                    (
                        uuid.uuid4(),
                        key,
                        body.name,
                        at,
                        hours_at,
                        hours_source,
                        body.minutes,
                        body.consumables,
                        body.notes,
                        source,
                        created_by,
                    ),
                ).rowcount
            else:
                written = conn.execute(
                    "INSERT INTO printer_wear_replacements (id, printer, part, replaced_at,"
                    " hours_at, hours_source, hours_used, cost, notes, source, created_by)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (printer, source) DO NOTHING",
                    (
                        uuid.uuid4(),
                        key,
                        body.name,
                        at,
                        hours_at,
                        hours_source,
                        body.hours_used,
                        body.cost,
                        body.notes,
                        source,
                        created_by,
                    ),
                ).rowcount
            found = conn.execute(
                f"{EVENTS_SQL} WHERE printer = %s AND source = %s AND kind = %s",
                (key, source, body.kind),
            ).fetchone()
        assert found is not None
        return _event(found), written > 0

    def import_events(
        self,
        printer: str,
        bambuddy_printer_id: int,
        done: Sequence[tuple[str, MaintenanceDone]],
    ) -> int | None:
        """Bambuddy's log rows (task name, row) kept as imported task events, each once by
        its ``bambuddy:<history id>`` source (§3.7, §4), and the read stamped
        (``events_read_at``), in one transaction under the printer's event lock. A row
        read again, or whose source a ScadBuddy event holds, writes nothing, and one past
        the columns' bounds (its history id, its counter or its task name,
        `storable_task`) is skipped; a note is kept as `storable_notes` gives it. How many it
        wrote; None, writing nothing, once the printer no longer links
        ``bambuddy_printer_id`` (unlinked, or linked to another since the read began)."""
        key = printer_uuid(printer)
        if key is None:
            return None
        written = 0
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('scadbuddy:machine-rate-events:'"
                " || %s, 0))",
                (str(key),),
            )
            # Held while it writes, so an unlink waits for it, or it sees the unlink.
            linked = conn.execute(
                "SELECT bambuddy_printer_id FROM printers WHERE id = %s FOR SHARE", (key,)
            ).fetchone()
            if linked is None or linked["bambuddy_printer_id"] != bambuddy_printer_id:
                return None
            for task, row in done:
                if (
                    not storable_task(task)
                    or not INT32_MIN <= row.history_id <= INT32_MAX
                    or not math.isfinite(row.hours_at)
                    or row.hours_at >= HOURS_AT_LIMIT
                ):
                    # Past the columns' bounds (`task`'s length, `bambuddy_history_id
                    # integer`, `hours_at numeric(10, 1)`), or no number or text: skipped.
                    continue
                hours: Decimal | None = None
                if row.hours_at >= 0:
                    # A counter below 0 (a negative offset typed in Bambuddy) is no
                    # reading: the event opens and closes no gap.
                    hours = reading(row.hours_at)
                source = f"{IMPORT_SOURCE}{row.history_id}"
                written += conn.execute(
                    "INSERT INTO printer_maintenance_events (id, printer, task, performed_at,"
                    " hours_at, hours_source, notes, origin, bambuddy_history_id, source,"
                    " created_by) SELECT %s, %s, %s, %s, %s, %s, %s, 'bambuddy', %s, %s, %s"
                    " WHERE NOT EXISTS (SELECT 1 FROM printer_wear_replacements"
                    " WHERE printer = %s AND source = %s) ON CONFLICT DO NOTHING",
                    (
                        uuid.uuid4(),
                        key,
                        task,
                        row.at,
                        hours,
                        None if hours is None else "bambuddy",
                        storable_notes(row.notes),
                        row.history_id,
                        source,
                        IMPORTED_BY,
                        key,
                        source,
                    ),
                ).rowcount
            conn.execute(
                "INSERT INTO printer_maintenance_reads (printer, read_at) VALUES (%s, now())"
                " ON CONFLICT (printer) DO UPDATE SET read_at = excluded.read_at",
                (key,),
            )
        return written

    def event_by_source(self, printer: str, source: str) -> MachineRateEvent | None:
        key = printer_uuid(printer)
        if key is None:
            return None
        with self._pool.connection() as conn:
            row = conn.execute(
                f"{EVENTS_SQL} WHERE printer = %s AND source = %s", (key, source)
            ).fetchone()
        return None if row is None else _event(row)

    def event(self, printer: str, event_id: str) -> MachineRateEvent | None:
        key, ident = printer_uuid(printer), _uuid(event_id)
        if key is None or ident is None:
            return None
        with self._pool.connection() as conn:
            row = conn.execute(
                f"{EVENTS_SQL} WHERE printer = %s AND id = %s", (key, ident)
            ).fetchone()
        return None if row is None else _event(row)

    def patch_event(
        self,
        printer: str,
        event_id: str,
        patch: MachineRateEventPatch,
        *,
        counter: HoursSource | None,
    ) -> MachineRateEvent | None:
        """The fields ``patch`` sets; None for no such event. A reading set on an event
        with none takes ``counter`` (the printer's source now) as its source; one
        cleared clears the source. :class:`EventFieldError` for a field the kind lacks;
        :class:`ImportedEventError` for a reading cleared on an event imported from
        Bambuddy, whose counter source stays as read."""
        key, ident = printer_uuid(printer), _uuid(event_id)
        if key is None or ident is None:
            return None
        with self._pool.connection() as conn, conn.transaction():
            current = _locked_event(conn, key, ident)
            if current is None:
                return None
            sent = patch.model_dump(exclude_unset=True)
            if current.origin == "bambuddy" and "hours_at" in sent and sent["hours_at"] is None:
                raise ImportedEventError(event_id)
            wrong = {"cost", "hours_used"} if current.kind == "task" else {"minutes", "consumables"}
            if wrong & sent.keys():
                raise EventFieldError(current.kind, sorted(wrong & sent.keys()))
            if "cost" in sent and sent["cost"] is None:
                raise EventFieldError(current.kind, ["cost"])
            if "hours_at" in sent:
                reading = sent["hours_at"]
                sent["hours_source"] = (
                    None if reading is None else current.hours_source or counter or "typed"
                )
            if not sent:
                return current
            columns = ", ".join(f"{name} = %s" for name in sent)
            conn.execute(
                f"UPDATE {_EVENT_TABLES[current.kind]} SET {columns}"
                " WHERE printer = %s AND id = %s",
                (*sent.values(), key, ident),
            )
            row = conn.execute(
                f"{EVENTS_SQL} WHERE printer = %s AND id = %s", (key, ident)
            ).fetchone()
        assert row is not None
        return _event(row)

    def delete_event(self, printer: str, event_id: str) -> bool:
        """A ScadBuddy event gone; False for none. :class:`ImportedEventError` for one
        imported from Bambuddy: the next import would bring it back."""
        key, ident = printer_uuid(printer), _uuid(event_id)
        if key is None or ident is None:
            return False
        with self._pool.connection() as conn, conn.transaction():
            current = _locked_event(conn, key, ident)
            if current is None:
                return False
            if current.origin == "bambuddy":
                raise ImportedEventError(event_id)
            conn.execute(
                f"DELETE FROM {_EVENT_TABLES[current.kind]} WHERE printer = %s AND id = %s",
                (key, ident),
            )
        return True

    def list_events(
        self,
        printer: str,
        *,
        kind: EventKind | None = None,
        name: str | None = None,
        cursor: str | None = None,
        limit: int = EVENTS_PAGE,
    ) -> MachineRateEventPage:
        """Every event of the printer, newest first (by time, then id), a page at a time.
        :class:`ValueError` for a cursor this did not give."""
        key = printer_uuid(printer)
        if key is None:
            return MachineRateEventPage(events=[])
        where = ["printer = %s"]
        args: list[object] = [key]
        if kind is not None:
            where.append("kind = %s")
            args.append(kind)
        if name is not None:
            where.append("name = %s")
            args.append(name)
        if cursor is not None:
            at, ident = _cursor(cursor)
            where.append("(at, id) < (%s, %s)")
            args += [at, ident]
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"{EVENTS_SQL} WHERE {' AND '.join(where)} ORDER BY at DESC, id DESC LIMIT %s",
                (*args, limit + 1),
            ).fetchall()
        events = [_event(r) for r in rows[:limit]]
        last = events[-1] if len(rows) > limit else None
        return MachineRateEventPage(
            events=events, next=None if last is None else f"{last.at.isoformat()}|{last.id}"
        )

    def counter(self, printer: str) -> LanCount | None:
        """ScadBuddy's own counter for the printer (``printer_run_hours``, #5774 §3.6), a
        plain read with the profile's offset (0 with no profile); None when the printer
        was never LAN-linked."""
        key = printer_uuid(printer)
        if key is None:
            return None
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT h.run_seconds, h.unseen_seconds, h.counted_to, h.started_at,"
                " coalesce(c.lan_hours_offset, %s::numeric) AS offset_hours"
                " FROM printer_run_hours AS h"
                " LEFT JOIN printer_cost_profiles AS c ON c.printer = h.printer"
                " WHERE h.printer = %s",
                (DEFAULT_LAN_OFFSET, key),
            ).fetchone()
        if row is None:
            return None
        run = run_hours.hours(row["run_seconds"])
        offset: Decimal = row["offset_hours"]
        return LanCount(
            hours=_text(run + offset),
            run_hours=_text(run),
            offset_hours=_text(offset),
            unseen_hours=_text(run_hours.hours(row["unseen_seconds"])),
            counted_to=row["counted_to"],
            started_at=row["started_at"],
        )

    def delete(self, printer: str) -> bool:
        """Every input of the printer gone; whether it had a profile."""
        key = printer_uuid(printer)
        if key is None:
            return False
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM printer_wear_parts WHERE printer = %s", (key,))
            conn.execute("DELETE FROM printer_maintenance_costs WHERE printer = %s", (key,))
            gone = conn.execute("DELETE FROM printer_cost_profiles WHERE printer = %s", (key,))
            return gone.rowcount > 0

    def remember_intervals(
        self, printer: str, bambuddy_printer_id: int, tasks: Sequence[MaintenanceTask]
    ) -> None:
        """Bambuddy's intervals kept on the printer's task rows of the same name, for
        when it does not answer. Only a counted task's: a calendar task's interval is
        days, not print-hours. Every other row's interval is cleared (a task disabled,
        made a calendar one or gone from Bambuddy since), so the stored rate counts what
        this read counted. ``bambuddy_printer_id`` is the Bambuddy printer the tasks were
        read from: once the printer no longer links it (unlinked, or linked to another
        since the read began), nothing is kept."""
        key = printer_uuid(printer)
        if key is None:
            return
        counted = {
            task.task: hours
            for task in tasks
            if task.enabled
            and task.interval_type == "hours"
            and (hours := _hours(task.interval_hours)) is not None
        }
        with self._pool.connection() as conn, conn.transaction():
            # Waits for a replace of the same printer, which rewrites these rows.
            conn.execute(
                "SELECT 1 FROM printer_cost_profiles WHERE printer = %s FOR UPDATE", (key,)
            )
            # Held while it writes, so an unlink waits for it, or it sees the unlink.
            linked = conn.execute(
                "SELECT bambuddy_printer_id FROM printers WHERE id = %s FOR SHARE", (key,)
            ).fetchone()
            if linked is None or linked["bambuddy_printer_id"] != bambuddy_printer_id:
                return
            for name, hours in counted.items():
                conn.execute(
                    "UPDATE printer_maintenance_costs SET interval_hours = %s,"
                    " interval_read_at = now() WHERE printer = %s AND task = %s",
                    (hours, key, name),
                )
            conn.execute(
                "UPDATE printer_maintenance_costs SET interval_hours = NULL,"
                " interval_read_at = NULL WHERE printer = %s AND NOT (task = ANY(%s))"
                " AND (interval_hours IS NOT NULL OR interval_read_at IS NOT NULL)",
                (key, list(counted)),
            )


class MachineRateView(BaseModel):
    """`GET /printers/{id}/machine-rate`: a printer's inputs (null with no profile) and
    the rate they give now, with each part."""

    printer: str
    printer_name: str
    #: The Bambuddy printer it is linked to, whose maintenance tasks it takes.
    bambuddy_printer_id: int | None = None
    inputs: MachineRateInputs | None = None
    breakdown: MachineRateBreakdown
    #: The names of the tasks Bambuddy tracks for it, from a live read: also for a
    #: printer with no profile yet, so its first tasks can be named as Bambuddy does.
    #: Empty when it is not linked or Bambuddy did not answer.
    bambuddy_tasks: list[str] = []
    #: #5774 (§5): each row's logged events in its window and its actual, and the events
    #: whose name no row has.
    actuals: ActualsView
    #: #5774 (§3.2, §5): the printer's print-hour counter.
    counter: CounterView


class RowActuals(BaseModel):
    """One wear part's or task's actual (§3.3, §3.4): its events in the window, newest
    first, the skipped ones between them, so the list and "k of N" agree with the rate."""

    kind: EventKind
    name: str
    events: list[MachineRateEvent]
    actual_per_hour: str | None = None
    #: A task's mean gap and cost of one occurrence; a part's mean hours used.
    actual_interval_hours: str | None = None
    actual_cost_per_occurrence: str | None = None
    actual_hours_used: str | None = None
    have: int
    need: int
    confidence: int
    partly_typed: bool = False
    skipped_gaps: list[SkippedGap] = Field(default_factory=list)


class Orphan(BaseModel):
    """Events kept under a name no row has now (a row deleted): never lost."""

    kind: EventKind
    name: str
    events: int


class ActualsView(BaseModel):
    window: int = DEFAULT_WINDOW
    rows: list[RowActuals] = Field(default_factory=list)
    orphaned: list[Orphan] = Field(default_factory=list)
    #: When the events were last read from Bambuddy's log in full; null until then, and
    #: kept as it was by a read that failed (§3.7).
    events_read_at: datetime | None = None


class CounterView(BaseModel):
    """The printer's print-hour counter (§3.2). ``hours`` is the reading an event gets
    now: null for ``typed`` (no counter) or when Bambuddy does not answer. ``lan_watch``
    is ScadBuddy's own count when the printer has one; on a printer linked to Bambuddy
    it is a check beside Bambuddy's and never prices."""

    source: HoursSource
    hours: str | None = None
    lan_watch: LanCount | None = None


def hours_source(row: PrinterRow) -> HoursSource:
    """The printer's one counter (§3.2): Bambuddy's on a linked printer, ScadBuddy's own
    on one only the LAN reaches, else none (``typed``)."""
    if row.bambuddy_printer_id is not None:
        return "bambuddy"
    if row.lan_host is not None:
        return "lan_watch"
    return "typed"


def bambuddy_hours(tasks: Sequence[MaintenanceTask] | None) -> Decimal | None:
    """Bambuddy's counter from a live task read: every task carries the same one."""
    for task in tasks or ():
        if task.current_hours is not None:
            return reading(task.current_hours)
    return None


#: A machine-rate refusal's problem type.
PROBLEM = "https://scadbuddy.dev/problems/machine-rate/"


def refusal(status: int, code: str, detail: str) -> ApiError:
    return ApiError(status, detail, type_=PROBLEM + code, code=code)


def actuals_view(breakdown: MachineRateBreakdown, stored: Stored) -> ActualsView:
    """Each priced row's events in its window (§5), and the names no row has."""
    logged = {e.id: e for e in stored.logged}
    events = stored.events
    window = stored.window
    labour = Decimal(breakdown.labour_rate_per_hour or "0")
    costs = {c.task: c for c in stored.costs}
    rows: list[RowActuals] = []

    def listed(actual: Actual) -> list[MachineRateEvent]:
        return [logged[i] for i in actual.window if i in logged]

    for task in breakdown.maintenance:
        actual = task_actual(events.tasks.get(task.task, ()), costs.get(task.task), labour, window)
        rows.append(
            RowActuals(
                kind="task",
                name=task.task,
                events=listed(actual),
                actual_per_hour=task.actual_per_hour,
                actual_interval_hours=task.actual_interval_hours,
                actual_cost_per_occurrence=task.actual_cost_per_occurrence,
                have=actual.have,
                need=window,
                confidence=actual.confidence,
                partly_typed=actual.partly_typed,
                skipped_gaps=actual.skipped,
            )
        )
    for part in breakdown.wear:
        actual = part_actual(events.parts.get(part.name, ()), window)
        rows.append(
            RowActuals(
                kind="part",
                name=part.name,
                events=listed(actual),
                actual_per_hour=part.actual_per_hour,
                actual_hours_used=part.actual_hours_used,
                have=actual.have,
                need=window,
                confidence=actual.confidence,
                skipped_gaps=actual.skipped,
            )
        )
    tasks = {m.task for m in breakdown.maintenance} | set(costs)
    parts = {w.name for w in breakdown.wear} | {w.name for w in stored.wear}
    orphaned = sorted(
        (kind, name, count)
        for (kind, name), count in stored.counts.items()
        if name not in (tasks if kind == "task" else parts)
    )
    return ActualsView(
        window=window,
        rows=rows,
        orphaned=[Orphan(kind=k, name=n, events=c) for k, n, c in orphaned],
        events_read_at=stored.events_read_at,
    )


#: What a Bambuddy read raises when Bambuddy does not answer usefully: a refusal, a reply
#: that does not validate, is not JSON or is not text. Any other ValueError is a bug.
UNANSWERED = (ApiError, NotLinkedError, ValidationError, json.JSONDecodeError, UnicodeDecodeError)


#: How a read of a printer's Bambuddy log ended: ``read`` and kept; ``unanswered`` (the
#: task list or the log could not be read, or the store refused the log); ``unlinked``
#: (the printer no longer links the Bambuddy printer read, so nothing was kept).
LogRead = Literal["read", "unanswered", "unlinked"]


@dataclass
class PricedRate:
    """A printer's rate now, the tasks read from Bambuddy for it (None: none read), and
    how its Bambuddy log read ended (None: not asked, not linked or no profile)."""

    breakdown: MachineRateBreakdown
    tasks: list[MaintenanceTask] | None = None
    log: LogRead | None = None


@dataclass
class MoneyRates:
    """:meth:`MachineRates.money_rates`: the machine-rate fields of ``costs.CostRates``,
    named as they are there."""

    #: The fallback for an attempt naming no registry printer.
    machine_rate_per_hour: str
    printer_rates: dict[str, str]
    serial_rates: dict[str, str]
    #: #5774 (§6): profiled printers only; one missing is priced at the book's rate.
    printer_actuals: dict[str, bool]
    serial_actuals: dict[str, bool]
    printer_confidence: dict[str, int | None]
    serial_confidence: dict[str, int | None]
    #: The default printer whose rate the fallback is; None: the fallback is the book's.
    default_printer: str | None = None


class MachineRates:
    """The rate of a registry printer, as a quote, Settings and the money view read it."""

    def __init__(
        self,
        store: MachineRateStore,
        printers: PrinterStore,
        integrations: OrderIntegrations,
        settings: SettingsStore,
    ) -> None:
        self.store = store
        self.printers = printers
        self.integrations = integrations
        self.settings = settings

    async def breakdown(self, row: PrinterRow, *, live: bool = True) -> MachineRateBreakdown:
        """The printer's rate now. ``live`` reads a linked printer's tasks from Bambuddy
        (and keeps their intervals); without it, or when Bambuddy does not answer, the
        stored intervals price it (``intervals`` ``stored``). A printer with no profile
        is the book's rate."""
        return (await self._priced(row, live=live)).breakdown

    async def _priced(self, row: PrinterRow, *, live: bool, read_log: bool = False) -> PricedRate:
        """:meth:`breakdown`, and the tasks it read from Bambuddy. With ``read_log`` (the
        Settings view, its button and the sweep, never a quote: §3.7), a linked printer
        with a profile also reads its Bambuddy log after the live task read, and the rate
        prices with what that read added."""
        runtime = (await asyncio.to_thread(self.settings.snapshot)).runtime
        stored = await asyncio.to_thread(self.store.stored, row.id)
        if stored.profile is None:
            book = MachineRateBreakdown(
                printer=row.id,
                printer_name=row.name,
                source="price_book",
                rate_per_hour=runtime.pricing_machine_rate_per_hour,
                currency=runtime.orders_currency,
            )
            return PricedRate(book)
        tasks: list[MaintenanceTask] | None = None
        intervals: IntervalSource = "typed"
        log: LogRead | None = None
        if row.bambuddy_printer_id is not None:
            intervals = "stored"
            if live:
                tasks = await self._read_tasks(row)
            if tasks is not None:
                intervals = "bambuddy"
                await asyncio.to_thread(
                    self.store.remember_intervals, row.id, row.bambuddy_printer_id, tasks
                )
            if read_log:
                log = "unanswered" if tasks is None else await self._read_log(row, tasks, stored)
        rate = compute(
            stored.profile,
            stored.wear,
            stored.costs,
            tasks,
            default_labour=runtime.pricing_labour_rate_per_hour,
            currency=runtime.orders_currency,
            intervals=intervals,
            printer=row.id,
            printer_name=row.name,
            intervals_read_at=stored.read_at if intervals == "stored" else None,
            read_tasks=stored.read_tasks if intervals == "stored" else None,
            events=stored.events,
        )
        return PricedRate(rate, tasks, log)

    async def _read_tasks(self, row: PrinterRow) -> list[MaintenanceTask] | None:
        """A linked printer's tasks read from Bambuddy now; None when it does not answer."""
        if row.bambuddy_printer_id is None or not self.integrations.linked:
            return None
        try:
            return await self.integrations.maintenance_tasks(row.bambuddy_printer_id)
        except UNANSWERED:
            logger.warning(
                "Bambuddy's maintenance tasks could not be read; the stored intervals price"
                " the machine rate",
                extra={"printer": row.id},
            )
            return None

    async def _read_log(
        self, row: PrinterRow, tasks: Sequence[MaintenanceTask], stored: Stored
    ) -> LogRead:
        """The log of each task Bambuddy names for the printer (at most `MAX_ROWS`), read
        whole, then kept (§3.7), and how that ended. A read that fails, or one the printer
        was unlinked during, keeps nothing and leaves ``events_read_at`` as it was.
        ``stored`` takes the events it added."""
        assert row.bambuddy_printer_id is not None
        items: dict[int, str] = {}
        for task in tasks:
            if task.item_id is not None and len(items) < MAX_ROWS and storable_task(task.task):
                items[task.item_id] = task.task
        try:
            logs = await self.integrations.maintenance_history(list(items))
        except UNANSWERED:
            logger.warning(
                "Bambuddy's maintenance log could not be read; the stored events price the"
                " machine rate",
                extra={"printer": row.id},
            )
            return "unanswered"
        done = [
            (items[item], entry)
            for item, entries in logs.items()
            if item in items
            for entry in entries
        ]
        try:
            written = await asyncio.to_thread(
                self.store.import_events, row.id, row.bambuddy_printer_id, done
            )
        except (psycopg.DataError, psycopg.IntegrityError):
            # A row the bounds checks in `import_events` miss: the stored events price the
            # rate, and the view, the button and the sweep never fail on it.
            logger.exception(
                "Bambuddy's maintenance log could not be kept; the stored events price the"
                " machine rate",
                extra={"printer": row.id},
            )
            return "unanswered"
        if written is None:
            return "unlinked"
        if written:
            fresh = await asyncio.to_thread(self.store.stored, row.id)
            if fresh.profile is not None:
                stored.logged, stored.counts = fresh.logged, fresh.counts
        return "read"

    async def read_log(self, row: PrinterRow) -> LogRead | None:
        """The sweep's read of one printer's Bambuddy log (:meth:`read_logs`, §3.7), and
        how it ended. None, reading nothing, for a printer not linked or with no profile
        (the rate then lists no task). The view and its button read the log in
        :meth:`view`, through the same :meth:`_priced`."""
        return (await self._priced(row, live=True, read_log=True)).log

    async def read_logs(self, *, clock: Callable[[], float] = time.monotonic) -> tuple[int, int]:
        """The housekeeping sweep (§3.7): the log of every printer linked to Bambuddy
        with a profile, one after another through :meth:`read_log`. Each fails alone: any
        error is logged and counted, and the next printer is read. How many were read and
        how many failed; a printer unlinked during its read is neither. Bounded by
        `READ_LOGS_BUDGET` on ``clock``: no printer starts past it, and a read still
        running then is cut (a write already sent still commits); those printers are
        neither read nor failed, logged, and left for the next tick."""
        if not self.integrations.linked:
            return 0, 0
        read = failed = 0
        rows = await asyncio.to_thread(self.printers.list)
        linked = [row for row in rows if row.bambuddy_printer_id is not None]
        deadline = clock() + READ_LOGS_BUDGET
        for index, row in enumerate(linked):
            left = deadline - clock()
            if left <= 0:
                self._past_deadline(len(linked) - index)
                break
            try:
                async with asyncio.timeout(left):
                    log = await self.read_log(row)
            except TimeoutError:
                self._past_deadline(len(linked) - index)
                break
            except Exception:
                logger.exception(
                    "a printer's maintenance log could not be imported",
                    extra={"printer": row.id},
                )
                failed += 1
                continue
            if log == "read":
                read += 1
            elif log == "unanswered":
                failed += 1
        return read, failed

    @staticmethod
    def _past_deadline(skipped: int) -> None:
        logger.warning(
            "the maintenance log sweep reached its deadline; the printers left are read on"
            " the next tick",
            extra={"skipped": skipped, "budget_s": READ_LOGS_BUDGET},
        )

    def stored_rates(self) -> dict[str, str]:
        """Every profiled printer's rate from its stored intervals, by registry id: no
        Bambuddy call, for the money view (job cost spec §4.1). Blocking."""
        return {printer: rate.rate_per_hour for printer, rate in self._stored_rates()[0].items()}

    def _stored_rates(
        self,
    ) -> tuple[dict[str, MachineRateBreakdown], dict[str, MachineRateBreakdown]]:
        """Each profiled printer's stored breakdown (:meth:`stored_rates`), by registry id
        and by serial."""
        runtime = self.settings.snapshot().runtime
        by_id: dict[str, MachineRateBreakdown] = {}
        by_serial: dict[str, MachineRateBreakdown] = {}
        for printer, stored in self.store.stored_all().items():
            if stored.profile is None:
                continue
            rate = self._stored_breakdown(stored, runtime.pricing_labour_rate_per_hour)
            by_id[printer] = rate
            if stored.serial is not None:
                by_serial[stored.serial] = rate
        return by_id, by_serial

    @classmethod
    def _stored_rate(cls, stored: Stored, default_labour: str) -> str:
        return cls._stored_breakdown(stored, default_labour).rate_per_hour

    @staticmethod
    def _stored_breakdown(stored: Stored, default_labour: str) -> MachineRateBreakdown:
        assert stored.profile is not None
        return compute(
            stored.profile,
            stored.wear,
            stored.costs,
            None,
            default_labour=default_labour,
            currency="",
            intervals="stored",
            read_tasks=stored.stored_reads,
            events=stored.events,
        )

    def check_rate(self, printer: str | None, printer_id: int | None) -> str | None:
        """The stored rate of the printer a quote names (:meth:`resolve`), with no
        Bambuddy call, for the quote's check; None for no printer or no profile.
        Blocking."""
        row = self.resolve(printer, printer_id)
        if row is None:
            return None
        stored = self.store.stored(row.id)
        if stored.profile is None:
            return None
        labour = self.settings.snapshot().runtime.pricing_labour_rate_per_hour
        return self._stored_rate(stored, labour)

    def money_rates(self) -> MoneyRates:
        """The money view's machine rates (job cost spec §4.1), for an attempt with no
        quote's rate: every registry printer's, by id and by serial (a printer merged
        away is found by the serial it kept), its stored rate (:meth:`stored_rates`) or,
        with no profile, the book's ``machine_rate_per_hour`` (§6.2); and the fallback for
        an attempt naming no registry printer: the configured default printer's rate,
        else the book's. #5774 (§6): each profiled printer's ``actuals`` and
        ``confidence`` beside its rate. Blocking."""
        stored, stored_by_serial = self._stored_rates()
        book = self.settings.snapshot().runtime.pricing_machine_rate_per_hour
        money = MoneyRates(
            machine_rate_per_hour=book,
            printer_rates={p: r.rate_per_hour for p, r in stored.items()},
            serial_rates={s: r.rate_per_hour for s, r in stored_by_serial.items()},
            printer_actuals={p: r.actuals for p, r in stored.items()},
            serial_actuals={s: r.actuals for s, r in stored_by_serial.items()},
            printer_confidence={p: r.confidence for p, r in stored.items()},
            serial_confidence={s: r.confidence for s, r in stored_by_serial.items()},
        )
        for row in self.printers.list():
            money.printer_rates.setdefault(row.id, book)
            # A serial already in the maps (`_stored_rates`, read before this list: after a
            # merge or a serial change between the two reads, another row's) keeps that
            # profiled row's rate, actuals and confidence; this row adds none of its own.
            # So a serial's basis and confidence are always those of the rate it reads.
            money.serial_rates.setdefault(row.serial, money.printer_rates[row.id])
            if row.id in stored and row.serial not in stored_by_serial:
                money.serial_actuals[row.serial] = stored[row.id].actuals
                money.serial_confidence[row.serial] = stored[row.id].confidence
        default = self.resolve(None, None)
        if default is not None and default.id in stored:
            money.machine_rate_per_hour = money.printer_rates[default.id]
            money.default_printer = default.id
        return money

    def resolve(self, printer: str | None, printer_id: int | None) -> PrinterRow | None:
        """The registry printer a quote is priced for (§6.2): the one it names, else the
        one linked to the Bambuddy printer it names, else the configured default
        printer. None when it names none and none is configured, or names a row gone
        with no Bambuddy printer beside it. Blocking."""
        if printer is not None:
            row = self.printers.get(printer)
            if row is not None:
                return row
            if printer_id is None:
                # A named row gone (the quote's check refuses one, `registry_printer`;
                # this is one deleted between the check and the run): no printer, so
                # the book's rate, never the default printer's in its place.
                return None
        if printer_id is not None:
            return self.printers.by_bambuddy_id(printer_id)
        stored = self.settings.load()
        # The default printer is a registry id only (#5167).
        if stored.default_printer is not None:
            return self.printers.get(stored.default_printer)
        return None

    async def for_quote(
        self, printer: str | None, printer_id: int | None
    ) -> MachineRateBreakdown | None:
        """The rate a slice quote prices machine time at, for the printer it sliced
        for; None when no printer is named or configured (the book's rate prices it)."""
        row = await asyncio.to_thread(self.resolve, printer, printer_id)
        return None if row is None else await self.breakdown(row)

    async def view(self, printer: str, *, require_log: bool = False) -> MachineRateView | None:
        """None for a printer the registry does not hold. A linked printer with a profile
        reads its Bambuddy log after the live task read (§3.7); one that cannot be read
        keeps the stored events, unless ``require_log`` (the "Read from Bambuddy" button):
        then a refusal says why (409 ``not_linked``, also for a printer unlinked during the
        read or with no Bambuddy configured, 409 ``no_machine_rate``, 503
        ``log_unavailable``, also for a log the store refused)."""
        row = await asyncio.to_thread(self.printers.get, printer)
        if row is None:
            return None
        if require_log and (row.bambuddy_printer_id is None or not self.integrations.linked):
            # A row that still names a Bambuddy printer while no Bambuddy is configured
            # has no log to read either: 409, not 503 ``log_unavailable``.
            raise refusal(
                409, "not_linked", "The printer is not linked to Bambuddy: it has no log to read."
            )
        # The breakdown first: its live read keeps Bambuddy's intervals, which the
        # inputs then show, and its log read adds the events the view lists.
        priced = await self._priced(row, live=True, read_log=True)
        breakdown, tasks = priced.breakdown, priced.tasks
        if require_log and priced.log is None:
            raise refusal(
                409,
                "no_machine_rate",
                "The printer has no machine rate inputs yet: set them, then read its log.",
            )
        if require_log and priced.log == "unlinked":
            raise refusal(
                409, "not_linked", "The printer is not linked to Bambuddy: it has no log to read."
            )
        if require_log and priced.log == "unanswered":
            raise refusal(
                503,
                "log_unavailable",
                "Bambuddy did not answer. The rate uses the events last read.",
            )
        stored = await asyncio.to_thread(self.store.stored, row.id)
        inputs = None
        names: list[str] = []
        if stored.profile is not None:
            inputs = MachineRateInputs(
                profile=stored.profile, wear_parts=stored.wear, maintenance=stored.costs
            )
        # One Bambuddy read: the breakdown's when it priced a profile (even one deleted
        # since), else one of its own for the book's rate.
        if breakdown.intervals == "bambuddy":
            names = [m.task for m in breakdown.maintenance if m.why != "not_in_bambuddy"]
        elif breakdown.source == "price_book" and row.bambuddy_printer_id is not None:
            tasks = await self._read_tasks(row)
            names = [task.task for task in tasks or ()]
        source = hours_source(row)
        lan = await asyncio.to_thread(self.store.counter, row.id)
        hours: str | None = None
        if source == "bambuddy":
            hours = _number(bambuddy_hours(tasks))
        elif source == "lan_watch" and lan is not None:
            hours = lan.hours
        return MachineRateView(
            printer=row.id,
            printer_name=row.name,
            bambuddy_printer_id=row.bambuddy_printer_id,
            inputs=inputs,
            breakdown=breakdown,
            bambuddy_tasks=names,
            actuals=actuals_view(breakdown, stored),
            counter=CounterView(source=source, hours=hours, lan_watch=lan),
        )

    async def counter_reading(self, row: PrinterRow) -> tuple[HoursSource, Decimal | None]:
        """The printer's counter source and its reading now (§3.2): Bambuddy's live
        ``current_hours``, ScadBuddy's own count with its offset, or none (``typed``).
        None for a counter that cannot be read now."""
        source = hours_source(row)
        if source == "bambuddy":
            return source, bambuddy_hours(await self._read_tasks(row))
        if source == "lan_watch":
            lan = await asyncio.to_thread(self.store.counter, row.id)
            return source, None if lan is None else Decimal(lan.hours)
        return source, None

    async def add_event(
        self, row: PrinterRow, body: MachineRateEventCreate, *, created_by: str
    ) -> tuple[MachineRateEvent, bool]:
        """A ScadBuddy event (§5), its ``hours_at`` filled from the printer's counter when
        left out, and whether this call wrote it. Refusals are :class:`ApiError`."""
        if body.source is not None:
            # A retry returns the first event without reading the counter again, and
            # before the name check: its row may have been renamed or deleted since.
            first = await asyncio.to_thread(self.store.event_by_source, row.id, body.source)
            if first is not None and first.kind == body.kind:
                return first, False
        source = hours_source(row)
        if body.kind == "task" and source == "bambuddy":
            raise refusal(
                409,
                "maintenance_from_bambuddy",
                "Mark tasks done in Bambuddy; ScadBuddy reads them.",
            )
        stored = await asyncio.to_thread(self.store.stored, row.id)
        names = (
            {c.task for c in stored.costs} if body.kind == "task" else {p.name for p in stored.wear}
        )
        if body.name not in names:
            what = "maintenance task" if body.kind == "task" else "wear part"
            raise refusal(
                422, f"unknown_{body.kind}", f"the printer has no {what} named {body.name!r}"
            )
        hours_at = body.hours_at
        recorded: HoursSource | None = source
        if hours_at is None:
            _, now = await self.counter_reading(row)
            if now is not None:
                hours_at = _text(now)
            elif source != "typed":
                raise refusal(
                    503,
                    "counter_unavailable",
                    "The printer's counter cannot be read now. Try again, or enter the reading.",
                )
            elif body.kind == "task" or body.hours_used is None:
                raise refusal(
                    422,
                    "counter_reading_required",
                    "The printer has no counter: enter the reading (or, for a part, the"
                    " hours it ran).",
                )
            else:
                recorded = None
        try:
            return await asyncio.to_thread(
                self.store.add_event,
                row.id,
                body,
                hours_at=hours_at,
                hours_source=recorded if hours_at is not None else None,
                created_by=created_by,
            )
        except EventSourceTakenError as error:
            raise refusal(
                409, "source_taken", "another kind of event on the printer has this source"
            ) from error
