"""What `filament/metrics.py` gives (#6269): plates, a spool's metrics, the AMS reading
error and the grams series."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from scadbuddy.filament.models import EventKind

#: How a plate ended: BB's status, or SB's (applied is ``completed``, else ``failed``).
Outcome = Literal["completed", "failed", "cancelled", "unknown"]


class Plate(BaseModel):
    """One plate on one spool, counted once (plan Decision 6)."""

    job: str | None
    at: datetime
    #: The grams the plate used, never negative.
    grams: float
    outcome: Outcome
    printer_id: UUID | None
    #: The event the plate's grams come from.
    event_id: UUID


class AmsError(BaseModel):
    """AMS grams less scale grams over the paired readings (plan Decision 14)."""

    pairs: int
    mean_abs_g: float
    bias_g: float


class GramsPoint(BaseModel):
    """The grams left after one event (plan Decision 13)."""

    at: datetime
    grams: float
    #: The event is the reading the grams left anchored on at its own time.
    anchor: bool
    kind: EventKind


class SpoolMetrics(BaseModel):
    """One spool's use, cost and forecast."""

    used_g: float
    prints: int
    grams_per_print: float | None
    #: Grams of ``failed`` and ``cancelled`` plates (plan Decision 7).
    wasted_g: float
    cost_used: Decimal | None
    value_left: Decimal | None
    burn_g_per_day: float | None
    empty_on: date | None
    opened_at: datetime | None
    days_since_opened: float | None
    days_since_last_use: float | None
    hours_outside_dry: float
    ams_error: AmsError | None
