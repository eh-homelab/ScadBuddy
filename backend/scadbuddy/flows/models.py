"""A flow and its runs, as the routes answer them (spec 2026-10-01 §7.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from scadbuddy.workflows.flow_models import FlowStep, FlowWaiting, HopRecord

RunStatus = Literal["starting", "running", "waiting", "succeeded", "failed", "terminated"]
TERMINAL: frozenset[str] = frozenset({"succeeded", "failed", "terminated"})
#: The longest result a run's row keeps; a longer one is cut and flagged.
RESULT_MAX = 4096
#: The host function behind a harness callback or gated tool, by the tool's name.
HOST_FN = {
    "human_answer": "wait_for_human",
    "approved_print": "queue_print",
    "approved_arrange": "arrange",
    "approved_tool": "tool",
    "approved_turn_tool": "tool",
}


class DefinitionSummary(BaseModel):
    """A flow version, without its script. Each version is one commit in `data/flows`
    (#2503), and `version` is that commit's position in the flow's history."""

    id: str
    name: str
    version: int
    #: The version's commit in `data/flows`; None for one not yet backfilled there.
    commit: str | None = None
    #: Seconds an outward call of its runs waits for a decision: None takes the global
    #: setting, 0 is never. A run may override it.
    approval_timeout_s: int | None = None
    created_by: dict[str, Any]
    created_at: datetime
    #: When the flow (every version) was archived and by whom (#2485): hidden from the
    #: list by default and never started, until unarchived. None when it is not.
    archived_at: datetime | None = None
    archived_by: dict[str, Any] | None = None
    #: When the flow was deleted and by whom (#2485): its folder removed from
    #: `data/flows` by a commit, its versions kept in the history. None when it is not.
    deleted_at: datetime | None = None
    deleted_by: dict[str, Any] | None = None


class FlowState(BaseModel):
    """Whether a flow is archived or deleted, and who did it (#2485)."""

    name: str
    archived_at: datetime | None = None
    archived_by: dict[str, Any] | None = None
    deleted_at: datetime | None = None
    deleted_by: dict[str, Any] | None = None
    #: The commit that removed `<name>/` from `data/flows`.
    deleted_commit: str | None = None


class Definition(DefinitionSummary):
    """A flow version: immutable."""

    script: str


class StoredDefinition(DefinitionSummary):
    """A version's row: ``script`` only on a row written before flows were in git."""

    script: str | None = None


class FlowVersion(BaseModel):
    """One commit of a flow's history (#2503)."""

    #: The version's id (its `ScadBuddy-Flow-Version` trailer): what `GET /workflows/{id}`
    #: and a run's start take.
    id: str
    commit: str
    short: str
    version: int
    message: str
    #: Who made it, from the commit's author: `{"kind": "agent", "principal",
    #: "session"}` or `{"kind": "browser"}`.
    created_by: dict[str, Any]
    created_at: datetime
    #: The flow's newest version.
    current: bool = False


class FlowDiff(BaseModel):
    """A flow's script between two commits, as a unified patch naming `flow.py`."""

    name: str
    base: str
    head: str
    patch: str


class BackfillRow(BaseModel):
    """A version written before flows were in git, still to be committed (#2503)."""

    id: str
    name: str
    version: int
    script: str
    created_by: dict[str, Any]
    created_at: datetime


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
    #: The commit of the version it ran (#2503); None for a run whose version was not
    #: imported into `data/flows` yet.
    commit: str | None = None
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
    #: An approval's binding to the parked call's tool and input (#2318), which the
    #: agent checks before it runs the call; None for anything else.
    input_hash: str | None = None
    #: The entry's way up before it was decided (#2482), and the decision's own step.
    hops: list[HopRecord] = []
    created_at: datetime | None = None
