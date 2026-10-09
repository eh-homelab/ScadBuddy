"""What crosses DurableSession's history: its input, its Update arguments, its activities'.

Plain dataclasses only: the workflow sandbox imports this module, and the agent
service sends the same shapes as JSON (agent/src/sessions/durable.ts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from temporalio.claude_agent_sdk import AgentState

WORKFLOW = "DurableSession"
TASK_QUEUE = "agent"
TOOLS_QUEUE = "agent-tools"
SEND_MESSAGE_UPDATE = "send_message"
# What the segment runner reads from its workflow: the turn's images (Ruling 5) and the
# system prompt's append (SegmentContext).
SEGMENT_CONTEXT_QUERY = "segment_context"
# What a segment that starts with nothing left to spend ends with; the workflow reads it
# from the plugin's error to end the turn as `budget_exhausted` (plan 5c Ruling 3).
BUDGET_EXHAUSTED = "budget_exhausted: the chat used its budget"
# A task fails after this many segments (the plugin's max_segments, Ruling 3).
MAX_SEGMENTS = 50


@dataclass
class Owner:
    kind: str
    id: str
    label: str = ""


@dataclass
class ImageRef:
    """An image the user sent, held in ai_session_blobs by name (plan 5c Ruling 5)."""

    name: str
    mediaType: str  # noqa: N815 - the protocol's own field name


@dataclass
class TurnStart:
    """The turn that runs: carried across a continue-as-new in the middle of it."""

    turn_id: str
    author: Owner
    images: list[ImageRef] = field(default_factory=list)


@dataclass
class SessionStart:
    """DurableSession's input: the session, and the agent's state after a continue-as-new."""

    session_id: str
    creator: Owner
    owner: Owner
    max_turns: int = 50
    # What the agent service appends to Claude Code's own system prompt for the
    # session, as a classic turn's (manager.ts systemPromptAppend).
    system_append: str | None = None
    agent: AgentState | None = None
    turn: TurnStart | None = None
    # A Stop recorded in the activation that continued as new, carried to the turn.
    interrupted: str | None = None


@dataclass
class Message:
    """``send_message``: one user turn, accepted while no turn runs."""

    turn_id: str
    text: str
    author: Owner
    images: list[ImageRef] = field(default_factory=list)


@dataclass
class SegmentContext:
    images: list[ImageRef] = field(default_factory=list)
    system_append: str | None = None


@dataclass
class SendAnswer:
    accepted: bool
    turn_id: str


@dataclass
class GateSettings:
    """The timers of the session's entries (ai_settings), read when a turn starts."""

    approval_expiry_s: int
    question_expiry_s: int


@dataclass
class FollowArgs:
    session_id: str
    workflow_id: str
    # The turn sent a prompt: it ends only on an end that follows its `prompt` event.
    prompted: bool = False


@dataclass
class FollowResult:
    ended: str
    events: int


TurnOutcome = Literal["done", "failed", "interrupted", "budget_exhausted"]


@dataclass
class FinishTurn:
    session_id: str
    turn_id: str
    outcome: TurnOutcome
    message: str | None = None
