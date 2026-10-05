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


@dataclass
class Message:
    text: str
    context: str | None = None


@dataclass
class SnapshotInput:
    session_id: str
    state: AgentState
    in_flight: list[InFlight] = field(default_factory=list)
    version: int = 0


def render_prompt(m: Message) -> str:
    """The prompt the model sees: the page context is model-only (`user.turn` shows `text`)."""
    return m.text if m.context is None else f"{m.text}\n\n{m.context}"


def _outcome(status: str) -> ToolOutcome:
    """`DurableClaudeAgent._outcome_after_stop`'s text for a call in this status."""
    if status in ("started", "cancelled"):
        text = (
            f"This tool call was interrupted ({RESTORE_REASON}); whether it took "
            "effect is unknown. Check before running it again."
        )
    else:
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
