"""What crosses DurableSession's history: its input, its Update arguments, its activities'.

Plain dataclasses only: the workflow sandbox imports this module, and the agent
service sends the same shapes as JSON (agent/src/sessions/durable.ts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal

from temporalio.claude_agent_sdk import AgentState, ToolOutcome

WORKFLOW = "DurableSession"
TASK_QUEUE = "agent"
TOOLS_QUEUE = "agent-tools"
# Where the backend serves `flow_grant_check` (#2481): SCADBUDDY_TEMPORAL_TASK_QUEUE_PROJECTS's
# default, as TOOLS_QUEUE is the agent tools queue's.
PROJECTS_QUEUE = "projects"
SEND_MESSAGE_UPDATE = "send_message"
# What the segment runner reads from its workflow: the turn's images (Ruling 5) and the
# system prompt's append (SegmentContext).
SEGMENT_CONTEXT_QUERY = "segment_context"
# The turn the workflow holds (taken, running, or finishing), or None between turns:
# what the agent's sweep reads to give back a claim no turn will end (plan 5c Ruling 18).
TURN_QUERY = "turn"
# The committed conversation a fork of the session starts from (#2688), a page at a time.
FORK_POINT_QUERY = "fork_point"
# What a segment that starts with nothing left to spend ends with; the workflow reads it
# from the plugin's error to end the turn as `budget_exhausted` (plan 5c Ruling 3).
BUDGET_EXHAUSTED = "budget_exhausted: the chat used its budget"
# A task fails after this many segments (the plugin's max_segments, Ruling 3).
MAX_SEGMENTS = 50
# A segment that keeps failing ends its turn after this many attempts, about 25 minutes
# of the server's backoff (1 s doubling to 100 s), instead of retrying without end (#2243).
SEGMENT_ATTEMPTS = 20
# One segment attempt's cap (the plugin's segment_timeout, 10 minutes by default). A
# subagent runs inside its parent's segment, so this bounds the whole subagent: at 10
# minutes an authoring subagent never finished and each retry started it over (#4026).
# A dead worker is still caught by the segment's 30 s heartbeat timeout.
SEGMENT_TIMEOUT = timedelta(hours=1)
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
class FlowTurn:
    """A turn a flow run sent (#2481, spec 2026-10-11 §3.4): set only by the agent
    service, from the `sessions_start`/`sessions_send` activity's own context (a
    `flow-<run>` workflow, activity `tool-<call id>`), never from a script's arguments.
    The backend checks it against the run's own record of its steps."""

    run_id: str
    call_id: str


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
    # What the model is told in place of the plugin's fixed rejection, by tool_use id: a
    # call the agent refused as it described it (#4442), which no person rejected.
    replaced: dict[str, str] = field(default_factory=dict)
    # workflow.now() as the turn started (ISO 8601), the plugin's events' `at` clock:
    # a subscriber of a turn carried over by continue-as-new ignores an end before it.
    started_at: str | None = None
    # The flow run and step that sent this turn (#2481); None for any other turn.
    flow: FlowTurn | None = None


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
class ForkPointArgs:
    """``fork_point``'s argument: the first entry, and the most characters of entries."""

    start: int = 0
    limit: int = 512 * 1024


@dataclass
class ForkPoint:
    """A page of the conversation the session's agent holds, for a fork (#2688).

    ``entries`` are transcript entries as JSON text, as AgentState keeps them; the
    agent service reads every page and seeds the child's SessionStart.agent with them
    (agent/src/sessions/durable.ts ``forkSource``). The rest of the agent's state at
    the checkpoint is on the first page only."""

    # The Claude session the entries belong to, and where it was last committed.
    session_id: str | None
    checkpoint: str | None
    # The agent's task runs: what it writes past the checkpoint is not committed yet.
    busy: bool
    total: int
    entries: list[str] = field(default_factory=list)
    # What the agent still owes at the checkpoint (#4173), for a child that continues
    # there: the results its next segment delivers (a stopped task's paused calls), and
    # whether that segment continues in a copy of the session. Without them the child
    # resumes at a paused call with nothing to deliver, and its first segment fails.
    pending: dict[str, ToolOutcome] = field(default_factory=dict)
    fork_next: bool = False
    # The calls that already ran, so none runs again in the child.
    recent_call_ids: list[str] = field(default_factory=list)


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
    # Set by the agent service for a turn a flow run sent (#2481).
    flow: FlowTurn | None = None


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
    # The tools Settings → Tools & plugins disabled: the segment runner leaves them undeclared.
    disabled_tools: list[str] = field(default_factory=list)
    # The turn's plugin packages (#2320), which the segment runner loads once each hashes
    # to its pin.
    packages: list[TurnPackage] = field(default_factory=list)
    # The turn's refused calls, by tool_use id: what the model is told besides the
    # plugin's fixed rejection, in a classic turn's words (#2392).
    refusals: dict[str, str] = field(default_factory=dict)
    # The turn's calls the agent refused before they parked (#4442), by tool_use id: what
    # the model is told in place of the plugin's fixed rejection.
    replaced: dict[str, str] = field(default_factory=dict)


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
    # Its tier when that does not depend on the input, for the transcript's label;
    # None: it does (or an older agent listed it), and the label says outward.
    tier: str | None = None


@dataclass
class TurnTools:
    tools: list[TurnTool] = field(default_factory=list)
    # Defaulted: a listing an older agent made named no packages.
    packages: list[TurnPackage] = field(default_factory=list)
    # Said to the model in the turn's system prompt: the built-ins it gets as turn tools
    # (#2393, agent/src/temporal/turnServers.ts builtinNote). None from an older agent.
    system_note: str | None = None


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
    the registry tools' access in ai_access (#2088, #2171), resolved against the manifest
    (tools.sanitise_overrides). Defaulted: a turn an older worker started read none."""

    approval_expiry_s: int
    question_expiry_s: int
    # Every tier that differs from the manifest's, by tool name, raised or lowered.
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
    # This turn's turn tools' tiers, as turn_tools listed them, for the calls' labels:
    # the manifest knows only ScadBuddy's own tools.
    tiers: dict[str, str] = field(default_factory=dict)


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
