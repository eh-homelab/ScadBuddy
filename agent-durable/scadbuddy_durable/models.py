"""Types that cross the workflow history and the activities (extended by later tasks)."""

from __future__ import annotations

from dataclasses import dataclass, field

from temporalio.claude_agent_sdk import AgentState


@dataclass
class InFlight:
    id: str
    name: str
    status: str


@dataclass
class SnapshotInput:
    session_id: str
    state: AgentState
    in_flight: list[InFlight] = field(default_factory=list)
    version: int = 0
