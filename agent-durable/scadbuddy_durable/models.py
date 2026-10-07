"""Types that cross the workflow history and the activities. Plain dataclasses, safe in
the workflow sandbox."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from temporalio.claude_agent_sdk import AgentState, ToolOutcome

WORKFLOW_NAME = "DurableSession"
TASK_QUEUE = "agent"
SEND_UPDATE = "send_message"
REVIEW_UPDATE = "review"
PENDING_QUERY = "pending_approvals"
DECISIONS_QUERY = "decisions"
EXPIRED_BY = "system:expired"
# The send validator's refusal while a Stop closes the run (the agent maps it to `busy`).
STOPPING = "the session is stopping; send again"
# A nudge's refusals: another message's turn is running, the message will never run (a
# Stop or a refusal abandoned it), or no committed message has its id.
BUSY = "the session is busy"
ABANDONED = "this message was abandoned and will not run"
UNKNOWN_INPUT = "no message with this id was committed for this session"
# The activities that read and take the session's committed messages (inputs.py).
LOAD_INPUTS = "durable_load_inputs"
START_INPUT = "durable_start_input"
# How many ids of messages that ran a run carries into the next (Continue-As-New).
RECENT_INPUTS = 64

RESTORE_REASON = "the previous run of this session stopped unexpectedly"
_RECENT_CALLS = 256  # the plugin's run-once guard keeps this many ids (_workflow.py)


@dataclass
class InFlight:
    id: str
    name: str
    status: str


@dataclass
class Restored:
    in_flight: list[InFlight] = field(default_factory=list)


@dataclass
class SessionInput:
    session_id: str
    max_turns: int
    approval_expiry_seconds: int
    model: str | None = None
    restored: Restored | None = None
    #: Ids of the latest messages that ran, carried over Continue-As-New (the durable
    #: record is `ai_durable_inputs.status`; this only answers a late nudge for one).
    ran: list[str] = field(default_factory=list)


@dataclass
class Nudge:
    """The `send_message` Update: the id of a message committed to `ai_durable_inputs`."""

    id: str


@dataclass
class Message:
    """A committed message (`ai_durable_inputs`), as a run takes it."""

    id: str
    text: str
    context: str | None = None
    note: str | None = None


@dataclass
class LoadInputs:
    session_id: str
    #: A message whose status the loader also reports (`Loaded.status`).
    asked: str | None = None


@dataclass
class Loaded:
    #: The session's pending messages, oldest first.
    pending: list[Message] = field(default_factory=list)
    #: `LoadInputs.asked`'s status (pending, run, abandoned), or None with no such message.
    status: str | None = None


@dataclass
class StartInput:
    session_id: str
    id: str


@dataclass
class SnapshotInput:
    session_id: str
    state: AgentState
    in_flight: list[InFlight] = field(default_factory=list)
    version: int = 0


def render_prompt(m: Message) -> str:
    """The prompt the model sees: the page context and the restore note are model-only
    (`user.turn` shows `text`)."""
    return "\n\n".join(part for part in (m.text, m.context, m.note) if part is not None)


def _outcome(status: str) -> ToolOutcome:
    """What Claude learns about an unanswered call in this status (`_outcome_after_stop`'s
    texts, and the plugin's rejection text, for a run that stopped unexpectedly)."""
    if status in ("started", "cancelled"):
        text = (
            f"This tool call was interrupted ({RESTORE_REASON}); whether it took "
            "effect is unknown. Check before running it again."
        )
    elif status in ("done", "failed"):
        text = (
            f"This tool call ran, but its result was lost when {RESTORE_REASON}. "
            "Check its effect before running it again."
        )
    elif status == "rejected":
        text = "A human reviewer rejected this action. Do not retry it."
    else:  # waiting for approval, unknown tool
        text = f"This tool call did not run: {RESTORE_REASON}."
    return ToolOutcome(content=text, is_error=True)


def restore_state(state: AgentState, in_flight: list[InFlight]) -> AgentState:
    """`_end_task` applied by hand to a snapshot (plan ruling 15); the input is not changed."""
    pending = dict(state.pending)
    recent = list(state.recent_call_ids)
    for call in in_flight:
        pending[call.id] = _outcome(call.status)
        recent.append(call.id)
    return dataclasses.replace(
        state,
        pending=pending,
        recent_call_ids=recent[-_RECENT_CALLS:],
        conversation=list(state.conversation),
        task_prompt=None,
        task_segments=0,
        fork_next=True,
        session_id=state.session_id if state.checkpoint is not None else None,
    )
