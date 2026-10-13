"""What crosses a flow run's history (plan 2026-10-09-durable-phase-6-flows.md Ruling 7).

A step never holds a host call's arguments or result, only what the Workflows page
shows. No ``from __future__ import annotations``: the harness reads these types.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from scadbuddy.flows.grants import Narrowing

#: A run grant at the gate (#2481): `workflows/flow_grants.py`'s activities.
FLOW_GRANT_CHECK = "flow_grant_check"
FLOW_GRANT_APPROVE = "flow_grant_approve"
FLOW_GRANT_WITHDRAW = "flow_grant_withdraw"
#: The harness refused the grant's Update for good (not "not parked yet").
GRANT_REFUSED = "GrantApprovalRefused"


class StepPolicy(BaseModel):
    """What a script said about the outward calls of a session an `agent` or
    `ask_session` step talks to (#2481, spec 2026-10-11 §4.3): its narrowing of the run
    grant (None: the run grant as it is) and what a call outside it does (Part B)."""

    model_config = ConfigDict(extra="forbid")

    grant: Narrowing | None = None
    on_outside: Literal["escalate", "deny"] = "escalate"


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
    #: The run grant that let this outward call through without asking (#2481).
    grant_id: str | None = None
    #: An `agent`/`ask_session` step's narrowing and `on_outside` (#2481), which
    #: `flow_grant_check` reads for its session's calls.
    policy: StepPolicy | None = None


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
    #: The commit of the version it runs (#2503); "" from a run started before.
    commit: str = ""


class GrantCheck(BaseModel):
    """One call to check against the run's grant (#2481). It carries the call's
    arguments, so it is sealed under the run's key like the rest of its history."""

    run_id: str
    #: `flow:<run>:<workflow run>:<call>` for a flow-own call; a child's
    #: `durable:<session>:<workflow run>:<tool use>`.
    request_id: str
    fn: str
    tool: str | None = None
    args: dict[str, Any] = {}
    #: A child session's call: the session, and the run's step that sent its turn. The
    #: step's narrowing is read from the run's record of it, never sent.
    session_id: str | None = None
    flow_call_id: str | None = None


class GrantVerdict(BaseModel):
    """`flows.grants.Inside` or `Outside`, flattened for the history."""

    verdict: Literal["inside", "outside"]
    grant_id: str | None = None
    rule_index: int | None = None
    input_hash: str | None = None
    #: The call's arguments with the defaults the check resolved pinned in: what runs.
    args: dict[str, Any] | None = None
    reason: str | None = None
    timeout_s: int = 0
    parent_hop: bool = False


class GrantApprove(BaseModel):
    """Let one parked flow-own call through on its grant's say. Only the call is named:
    the run is the calling workflow's, and the verdict is derived again from the store
    and the call as it is parked, never taken from here."""

    model_config = ConfigDict(extra="forbid")

    call_id: str


#: `flow_grant_approve`'s answer: let through; decided first by something else; or not
#: (the grant lapsed, or its use is not for this call as parked), so a person is asked.
GrantApproval = Literal["approved", "resolved", "ask"]
