"""What an activity row is (spec 2026-10-09 §3) and what a kind declares (§4)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

ActivityStatus = Literal["queued", "running", "waiting", "succeeded", "failed", "cancelled"]
STATUSES: tuple[ActivityStatus, ...] = (
    "queued",
    "running",
    "waiting",
    "succeeded",
    "failed",
    "cancelled",
)
FINISHED: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})
FacetType = Literal["keyword", "number", "bool", "time"]


class ActivityRow(BaseModel):
    id: str
    kind: str
    principal: str | None = None
    session: str | None = None
    via_tool: str | None = None
    subject: str = ""
    status: ActivityStatus
    title: str
    parent_id: str | None = None
    facets: dict[str, Any] = {}
    detail: dict[str, Any] = {}
    source: str
    source_id: str
    workflow_id: str | None = None
    run_id: str | None = None
    traceparent: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True)
class Facet:
    name: str
    type: FacetType
    label: str
    filterable: bool = True


@dataclass(frozen=True)
class ActivityLink:
    rel: str
    label: str
    href: str


@dataclass(frozen=True)
class ActivityKind:
    key: str
    label: str
    facets: tuple[Facet, ...] = ()
    #: Stages, request, result, error, read from the source; `row.detail` when None.
    detail: Callable[[ActivityRow], Mapping[str, Any]] | None = None
    links: Callable[[ActivityRow], list[ActivityLink]] = field(default=lambda row: [])
