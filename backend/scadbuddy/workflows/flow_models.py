"""What crosses a flow run's history (plan 2026-10-09-durable-phase-6-flows.md Ruling 7).

A step never holds a host call's arguments or result, only what the Workflows page
shows. No ``from __future__ import annotations``: the harness reads these types.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class FlowStep(BaseModel):
    """One host call of a run."""

    seq: int
    fn: str
    call_id: str
    status: Literal["running", "succeeded", "failed"]
    outward: bool
    started_at: datetime
    ended_at: datetime | None = None
    #: The history length when the call started: a Reset drops the steps past it.
    history_length: int
    #: The history length when the call ended: a Reset to before it reopens the step.
    ended_history_length: int | None = None
    #: The execution whose write recorded it, set by the store (never by the script): a
    #: Reset leaves the new execution's own records alone.
    workflow_run_id: str | None = None
    #: The exception's type name, never its message.
    error: str | None = None
    #: The agent session an `agent` or `ask_session` call talks to, for the page's link.
    session_id: str | None = None


class FlowWaiting(BaseModel):
    """A host call parked for a person: an approval, or an answer to `prompt`."""

    call_id: str
    kind: Literal["approval", "answer"]
    fn: str
    prompt: str | None = None
    since: datetime
    #: The history length when it parked, and (once resolved) when it was resolved: a
    #: Reset to between the two parks it again.
    history_length: int | None = None
    resolved_at: int | None = None
    #: The execution whose write parked it, set by the store, as on `FlowStep`.
    workflow_run_id: str | None = None


class ProjectionWrite(BaseModel):
    """One change to a run's row, written by the run's own workflow."""

    run_id: str
    workflow_run_id: str
    status: Literal["running", "waiting", "succeeded", "failed"] | None = None
    step: FlowStep | None = None
    waiting_add: FlowWaiting | None = None
    waiting_remove: str | None = None
    result: str | None = None
    #: A Reset's write (6e), the one that may reopen a finished run.
    reset: bool = False
    #: The history length when written (`project` sets it).
    history_length: int | None = None


class FlowRecord(BaseModel):
    """The run's row as its first activity inserts it."""

    run_id: str
    definition_id: str
    version: int
    name: str
    workflow_id: str
    workflow_run_id: str
    started_by: dict[str, Any]
    #: Seconds an outward call waits for a decision; 0 is never.
    approval_timeout_s: int = 0
