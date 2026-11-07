"""A flow trigger and what it records (#2484, spec 2026-10-11 §12).

Every request body here forbids extra fields: who made a trigger, who a run acts as and
a grant's status are the server's record, never something a request says (§12.1).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any, Literal, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scadbuddy.core.settings import check_approval_timeout
from scadbuddy.flows.grants import GrantSpec, GrantStatus, RuleUse
from scadbuddy.orders.models import ItemStatus, OrderStatus

EventKind = Literal[
    "render.finished",
    "model.changed",
    "print.finished",
    "print.failed",
    "order.changed",
    "order_item.changed",
]
SubjectKind = Literal["template", "order", "order_item"]
Overlap = Literal["skip", "allow_all"]
TriggerKind = Literal["schedule", "event"]
#: `admitted`: past the overlap and the rate cap, its run not yet started.
FireOutcome = Literal["pending", "admitted", "started", "skipped", "failed"]
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
#: A template slug, or an order's or a line item's id (text uuids).
_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


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


class Subject(BaseModel):
    """What an event trigger is narrowed to (orders spec §7.1): a template by its slug, an
    order or a line item by its id. Without an id, any of that kind."""

    model_config = ConfigDict(extra="forbid")

    kind: SubjectKind
    id: str | None = Field(default=None, pattern=_ID)

    @model_validator(mode="after")
    def _slug(self) -> Subject:
        if self.kind == "template" and self.id is not None and not re.match(_SLUG, self.id):
            raise ValueError(f"not a template slug: {self.id!r}")
        return self


#: The subjects each event kind can be narrowed to: an item's event to its order or itself.
SUBJECTS: dict[str, frozenset[str]] = {
    "render.finished": frozenset({"template"}),
    "model.changed": frozenset({"template"}),
    "print.finished": frozenset({"template"}),
    "print.failed": frozenset({"template"}),
    "order.changed": frozenset({"order"}),
    "order_item.changed": frozenset({"order", "order_item"}),
}
#: The event fields a `match` may name, and the values each may take (Ruling 13).
MATCHABLE: dict[str, dict[str, frozenset[str]]] = {
    "order.changed": {"status": frozenset(get_args(OrderStatus))},
    "order_item.changed": {"status": frozenset(get_args(ItemStatus))},
}
MAX_MATCH_VALUES = 32


class EventTiming(BaseModel):
    """When an event trigger fires: on each event of `kind`, for `subject` only if set,
    and only when the event's fields hold one of the values `match` lists for each.

    A `slug` (the form before #4252, still in stored rows) reads as a template subject."""

    model_config = ConfigDict(extra="forbid")

    kind: EventKind
    subject: Subject | None = None
    match: dict[str, list[str]] | None = None

    @model_validator(mode="before")
    @classmethod
    def _from_slug(cls, value: Any) -> Any:
        if isinstance(value, dict) and "slug" in value:
            value = dict(value)
            slug = value.pop("slug")
            if "subject" in value:
                raise ValueError("give subject or slug, not both")
            if slug is not None:
                if not isinstance(slug, str) or not re.match(_SLUG, slug):
                    raise ValueError(f"not a template slug: {slug!r}")
                value["subject"] = {"kind": "template", "id": slug}
        return value

    @model_validator(mode="after")
    def _fits(self) -> EventTiming:
        if self.subject is not None and self.subject.kind not in SUBJECTS[self.kind]:
            raise ValueError(f"a {self.kind} trigger has no {self.subject.kind} subject")
        if self.match is not None:
            fields = MATCHABLE.get(self.kind, {})
            if not self.match:
                raise ValueError(
                    "match names at least one field; leave it out to match every event"
                )
            for field, values in self.match.items():
                if field not in fields:
                    raise ValueError(f"a {self.kind} event has no field {field!r} to match")
                if not 1 <= len(values) <= MAX_MATCH_VALUES:
                    raise ValueError(f"match.{field} lists 1 to {MAX_MATCH_VALUES} values")
                if unknown := sorted(set(values) - fields[field]):
                    raise ValueError(f"match.{field}: {', '.join(unknown)} is not one it takes")
        return self


def matches(timing: EventTiming, facts: dict[str, Any]) -> bool:
    """Whether an event with these facts passes the trigger's `match`."""
    return all(facts.get(field) in values for field, values in (timing.match or {}).items())


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
    """A change to a trigger: only what is sent changes. Any change returns its standing
    grant to `requested` (Ruling 20)."""

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
    #: Set afresh on each approval; the run grants it seeds name it.
    approval_id: str | None = None
    #: Per approval id, per rule index: what it let through across every run it seeded
    #: under that approval. Its limits are spent cumulatively, never once per run.
    used: dict[str, dict[str, RuleUse]] = Field(default_factory=dict)

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


def state_hash(trigger: Trigger, grant: StandingGrant | None) -> str:
    """What a person sees of a trigger and its live standing grant, as one hash. An
    approval (or a browser's attach, which is one) names the hash it saw, and is refused
    when the trigger or the grant has changed since (§12.4)."""
    seen = {
        "trigger": trigger.id,
        "name": trigger.name,
        "definition_id": trigger.definition_id,
        "version": trigger.version,
        "kind": trigger.kind,
        "schedule": trigger.schedule.model_dump(mode="json") if trigger.schedule else None,
        "event": trigger.event.model_dump(mode="json") if trigger.event else None,
        "overlap": trigger.overlap,
        "inputs": trigger.inputs,
        "approval_timeout_s": trigger.approval_timeout_s,
        "created_by": trigger.created_by,
        "grant": None
        if grant is None
        else {
            "id": grant.id,
            "status": grant.status,
            "spec": grant.spec.model_dump(mode="json", by_alias=True),
            "requested_by": grant.requested_by,
        },
    }
    canonical = json.dumps(seen, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
