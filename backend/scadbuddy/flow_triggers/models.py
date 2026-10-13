"""A flow trigger and what it records (#2484, spec 2026-10-11 §12).

Every request body here forbids extra fields: who made a trigger, who a run acts as and
a grant's status are the server's record, never something a request says (§12.1).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scadbuddy.core.settings import check_approval_timeout
from scadbuddy.flows.grants import GrantSpec, GrantStatus

EventKind = Literal["render.finished", "model.changed"]
Overlap = Literal["skip", "allow_all"]
TriggerKind = Literal["schedule", "event"]
FireOutcome = Literal["pending", "started", "skipped", "failed"]
ApprovalTimeout = int | Literal["never"]

#: A standing grant's longest life from its approval (Ruling 19).
MAX_STANDING_EXPIRY = timedelta(days=30)
#: Runs an event trigger may start in an hour, so a flow that causes its own event
#: cannot loop (§12.2).
MAX_EVENT_FIRES_PER_HOUR = 60
#: Temporal's shortest catch-up window is 10 s.
MIN_CATCHUP_S = 10
MAX_CATCHUP_S = 7 * 86400
MIN_INTERVAL_S = 60
MAX_CRON = 4
#: A cron field: digits, names and Temporal's cron punctuation. Five to seven fields
#: (`ScheduleSpec.cron_expressions`); Temporal checks the rest when it takes the Schedule.
_CRON_FIELD = re.compile(r"^[0-9A-Za-z*?/,#\-]+$")
_SLUG = r"^[a-z0-9][a-z0-9-]{0,62}$"


def timeout_seconds(value: ApprovalTimeout | None) -> int | None:
    """An approval timeout as stored: None inherits, 0 is never."""
    if value is None:
        return None
    if value == "never":
        return 0
    return check_approval_timeout(value, "approval_timeout")


class ScheduleTiming(BaseModel):
    """When a schedule trigger fires: a Temporal `ScheduleSpec` and its catch-up window."""

    model_config = ConfigDict(extra="forbid")

    #: Cron expressions, each five to seven fields (`ScheduleSpec.cron_expressions`).
    cron: list[str] | None = Field(default=None, min_length=1, max_length=MAX_CRON)
    #: Or every this many seconds (at least 60), shifted by `offset_s`.
    interval_s: int | None = Field(default=None, ge=MIN_INTERVAL_S, le=366 * 86400)
    offset_s: int = Field(default=0, ge=0)
    #: An IANA time zone (`ScheduleSpec.time_zone_name`).
    time_zone: str = "UTC"
    #: How late a missed fire may still run (`SchedulePolicy.catchup_window`).
    catchup_window_s: int = Field(default=300, ge=MIN_CATCHUP_S, le=MAX_CATCHUP_S)

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: list[str] | None) -> list[str] | None:
        for expression in value or []:
            fields = expression.split()
            if not 5 <= len(fields) <= 7 or not all(_CRON_FIELD.match(f) for f in fields):
                raise ValueError(f"not a cron expression: {expression!r}")
        return value

    @field_validator("time_zone")
    @classmethod
    def _zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"not a time zone: {value!r}") from None
        return value

    @model_validator(mode="after")
    def _one(self) -> ScheduleTiming:
        if (self.cron is None) == (self.interval_s is None):
            raise ValueError("give either cron or interval_s")
        if self.interval_s is not None and self.offset_s >= self.interval_s:
            raise ValueError("offset_s must be less than interval_s")
        return self


class EventTiming(BaseModel):
    """When an event trigger fires: on each event of `kind`, for `slug` only if set."""

    model_config = ConfigDict(extra="forbid")

    kind: EventKind
    slug: str | None = Field(default=None, pattern=_SLUG)


class _Timing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("approval_timeout", check_fields=False)
    @classmethod
    def _timeout(cls, value: ApprovalTimeout | None) -> ApprovalTimeout | None:
        timeout_seconds(value)
        return value


class TriggerCreate(_Timing):
    """A trigger to make: a pinned flow version, its inputs, and when it fires."""

    #: The flow version it runs, always this one (Ruling 18).
    definition_id: str = Field(min_length=1, max_length=64)
    schedule: ScheduleTiming | None = None
    event: EventTiming | None = None
    #: Whether a fire starts a run while the previous one is still open.
    overlap: Overlap = "skip"
    #: The runs' inputs, checked against the version's inputs schema.
    inputs: dict[str, Any] | None = None
    #: The runs' approval timeout: seconds, or "never". Omitted, the flow's.
    approval_timeout: ApprovalTimeout | None = None

    @model_validator(mode="after")
    def _one(self) -> TriggerCreate:
        if (self.schedule is None) == (self.event is None):
            raise ValueError("give either schedule or event")
        return self


class TriggerUpdate(_Timing):
    """A change to a trigger: only what is sent changes. Any change revokes its standing
    grant (Ruling 20)."""

    definition_id: str | None = Field(default=None, min_length=1, max_length=64)
    schedule: ScheduleTiming | None = None
    event: EventTiming | None = None
    overlap: Overlap | None = None
    inputs: dict[str, Any] | None = None
    approval_timeout: ApprovalTimeout | None = None

    @model_validator(mode="after")
    def _one(self) -> TriggerUpdate:
        if self.schedule is not None and self.event is not None:
            raise ValueError("give schedule or event, not both")
        return self


class Trigger(BaseModel):
    """A trigger as `flow_triggers` holds it."""

    id: str
    name: str
    definition_id: str
    #: The pinned version's number.
    version: int | None = None
    kind: TriggerKind
    schedule: ScheduleTiming | None = None
    event: EventTiming | None = None
    overlap: Overlap
    inputs: dict[str, Any] | None = None
    approval_timeout_s: int | None = None
    paused: bool
    paused_at: datetime | None = None
    paused_by: dict[str, Any] | None = None
    #: `flow archived`, `flow deleted`, or None for a pause by a person or the creator.
    paused_reason: str | None = None
    created_by: dict[str, Any]
    created_at: datetime
    updated_by: dict[str, Any] | None = None
    updated_at: datetime | None = None


class StandingGrant(BaseModel):
    """A trigger's standing grant (`flow_trigger_grants`)."""

    id: str
    trigger_id: str
    status: GrantStatus
    spec: GrantSpec
    requested_by: dict[str, Any]
    approved_by: str | None = None
    approved_at: datetime | None = None
    revoked_by: str | None = None
    revoked_at: datetime | None = None
    expires_at: datetime | None = None
    created_at: datetime

    def in_force(self, now: datetime) -> bool:
        return (
            self.status == "approved"
            and self.revoked_at is None
            and self.expires_at is not None
            and now < self.expires_at
        )


class Fire(BaseModel):
    """One fire of a trigger, and its run's status once the run has a row."""

    trigger_id: str
    fire_key: str
    run_id: str
    outcome: FireOutcome
    reason: str | None = None
    event: dict[str, Any] | None = None
    fired_at: datetime
    run_status: str | None = None


class RunTrigger(BaseModel):
    """The trigger and fire that started a run."""

    trigger_id: str
    kind: TriggerKind
    fire_key: str
    fired_at: datetime
    #: The event's facts, for an event trigger's fire.
    event: dict[str, Any] | None = None
