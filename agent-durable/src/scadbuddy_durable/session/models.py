"""What crosses DurableSession's history: its input, its Update arguments, its activities'.

Plain dataclasses only: the workflow sandbox imports this module, and the agent
service sends the same shapes as JSON (agent/src/sessions/durable.ts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from temporalio.claude_agent_sdk import AgentState

WORKFLOW = "DurableSession"
TASK_QUEUE = "agent"
TOOLS_QUEUE = "agent-tools"
SEND_MESSAGE_UPDATE = "send_message"
# What the segment runner reads from its workflow: the turn's images (Ruling 5) and the
# system prompt's append (SegmentContext).
SEGMENT_CONTEXT_QUERY = "segment_context"
# The turn the workflow holds (taken, running, or finishing), or None between turns:
# what the agent's sweep reads to give back a claim no turn will end (plan 5c Ruling 18).
TURN_QUERY = "turn"
# What a segment that starts with nothing left to spend ends with; the workflow reads it
# from the plugin's error to end the turn as `budget_exhausted` (plan 5c Ruling 3).
BUDGET_EXHAUSTED = "budget_exhausted: the chat used its budget"
# A task fails after this many segments (the plugin's max_segments, Ruling 3).
MAX_SEGMENTS = 50
# A segment that keeps failing ends its turn after this many attempts, about 25 minutes
# of the server's backoff (1 s doubling to 100 s), instead of retrying without end (#2243).
SEGMENT_ATTEMPTS = 20
# The most characters of a failed turn's message that reach FinishTurn and its event.
MESSAGE_MAX = 500


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
    # The append this turn was sent with (#2525); None for one sent before it was.
    system_append: str | None = None
    # Why each of its calls refused at the gate was not run, by tool_use id (#2392): the
    # segment runner adds it to the plugin's fixed rejection the model is given.
    refusals: dict[str, str] = field(default_factory=dict)
    # workflow.now() as the turn started (ISO 8601), the plugin's events' `at` clock:
    # a subscriber of a turn carried over by continue-as-new ignores an end before it.
    started_at: str | None = None


@dataclass
class SessionStart:
    """DurableSession's input: the session, and the agent's state after a continue-as-new."""

    session_id: str
    creator: Owner
    owner: Owner
    max_turns: int = 50
    # What the agent service appends to Claude Code's own system prompt for the
    # session, as a classic turn's (manager.ts systemPromptAppend): the first turn's.
    # Each turn sends its own (Message.system_append, #2525), which wins.
    system_append: str | None = None
    agent: AgentState | None = None
    turn: TurnStart | None = None
    # A Stop recorded in the activation that continued as new, carried to the turn.
    interrupted: str | None = None
    # The replica the session's plugin calls are pinned to (#2581): it holds the session's
    # MCP clients, so the next turn is listed there too; carried over a continue-as-new.
    host_queue: str | None = None


@dataclass
class Message:
    """``send_message``: one user turn, accepted while no turn runs."""

    turn_id: str
    text: str
    author: Owner
    images: list[ImageRef] = field(default_factory=list)
    # The append as Settings stand when this turn is sent (#2525): a session started
    # while the headless browser was off is told about it once it is on, and back.
    system_append: str | None = None


@dataclass
class TurnPackage:
    """A plugin package a durable turn's Claude Code loads (agent/src/temporal/turnTools.ts
    ``TurnPackage``, #2320): copied by the agent to ``<payload dir>/plugins/<dir>``."""

    name: str
    dir: str
    content_hash: str
    # Approved with allow_refused: loaded as it is, with the built-ins it names.
    allowed: bool = False
    builtin_tools: list[str] = field(default_factory=list)


@dataclass
class SegmentContext:
    images: list[ImageRef] = field(default_factory=list)
    system_append: str | None = None
    # The tools Settings → Plugins disabled: the segment runner leaves them undeclared.
    disabled_tools: list[str] = field(default_factory=list)
    # The turn's plugin packages (#2320), which the segment runner loads once each hashes
    # to its pin.
    packages: list[TurnPackage] = field(default_factory=list)
    # The turn's refused calls, by tool_use id: what the model is told besides the
    # plugin's fixed rejection, in a classic turn's words (#2392).
    refusals: dict[str, str] = field(default_factory=dict)


@dataclass
class SendAnswer:
    accepted: bool
    turn_id: str


@dataclass
class TurnTool:
    """One of a turn's other tools (agent/src/temporal/turnTools.ts ``TurnToolEntry``):
    a remote plugin's or ``http_request`` (#2320). Its calls run
    as the activity ``turn:<name>`` on agent-tools."""

    name: str
    description: str
    input_schema: dict[str, Any]
    # Whether a call may be outward: it then asks turn_tool_tier before it parks.
    may_park: bool
    # The replica's own queue when its server's state lives there (http_request's saved
    # bodies); None: agent-tools.
    task_queue: str | None = None


@dataclass
class TurnTools:
    tools: list[TurnTool] = field(default_factory=list)
    # Defaulted: a listing an older agent made named no packages.
    packages: list[TurnPackage] = field(default_factory=list)


@dataclass
class MemoryTurn:
    """memory_recall's and memory_retain's argument (agent temporal/memoryActivities.ts)."""

    turn_id: str


@dataclass
class MemoryRecalled:
    """The ``<hindsight_memories>`` block a classic turn's recall hook adds; None: none."""

    context: str | None = None


@dataclass
class GateSettings:
    """The timers of the session's entries (ai_settings), read when a turn starts, and
    the tools' Settings → Plugins overrides (#2088), sanitised against the manifest
    (tools.sanitise_overrides). Defaulted: a turn an older worker started read none."""

    approval_expiry_s: int
    question_expiry_s: int
    # Raised tiers by tool name: only raises, never lowers.
    tool_tiers: dict[str, str] = field(default_factory=dict)
    disabled_tools: list[str] = field(default_factory=list)


@dataclass
class FollowArgs:
    session_id: str
    workflow_id: str
    # The turn sent a prompt: it ends only on an end that follows its `prompt` event.
    prompted: bool = False
    # TurnStart.started_at: an end published before it is an earlier turn's (#2123).
    since: str | None = None


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
