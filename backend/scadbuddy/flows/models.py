"""A flow and its runs, as the routes answer them (spec 2026-10-01 §7.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from scadbuddy.workflows.flow_models import FlowStep, FlowWaiting

RunStatus = Literal["starting", "running", "waiting", "succeeded", "failed", "terminated"]
TERMINAL: frozenset[str] = frozenset({"succeeded", "failed", "terminated"})
#: The longest result a run's row keeps; a longer one is cut and flagged.
RESULT_MAX = 4096


class DefinitionSummary(BaseModel):
    """A flow version, without its script."""

    id: str
    name: str
    version: int
    #: Seconds an outward call of its runs waits for a decision: None takes the global
    #: setting, 0 is never. A run may override it.
    approval_timeout_s: int | None = None
    created_by: dict[str, Any]
    created_at: datetime


class Definition(DefinitionSummary):
    """A flow version: immutable."""

    script: str


class Run(BaseModel):
    """A run of a flow version, as its workflow last recorded it."""

    id: str
    definition_id: str
    version: int
    name: str
    status: RunStatus
    #: The host calls parked for a person.
    waiting_on: list[FlowWaiting] = []
    #: One entry per host call, in order.
    steps: list[FlowStep] = []
    #: The script's outcome: ``result: <repr>`` or ``Script error (…)``.
    result: str | None = None
    result_truncated: bool = False
    #: Seconds an outward call of this run waits for a decision; 0 is never.
    approval_timeout_s: int = 0
    workflow_id: str
    workflow_run_id: str
    started_by: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class Decision(BaseModel):
    """A person's answer to one parked call of a run (`workflow_run_decisions`)."""

    request_id: str
    run_id: str
    workflow_run_id: str
    call_id: str
    kind: Literal["approval", "answer"]
    outcome: Literal["approved", "denied", "answered"]
    response: dict[str, Any] = {}
    responder: str
    created_at: datetime | None = None
