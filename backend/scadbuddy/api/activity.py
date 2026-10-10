"""Reading activity (spec 2026-10-09 §6, §7): the list, its facets and one row.

A request with the agent-actor marker (the agent's headless browser) reads only its
session owner's rows, in all three (``activity/visibility.py``); every other request
reads everything. Unknown query parameters answer 400: FastAPI would ignore them, and a
misspelt filter would then list everything.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from scadbuddy.activity.component import ACTIVITY, ActivityComponent
from scadbuddy.activity.index import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    ActivityFilter,
    Count,
    decode_cursor,
)
from scadbuddy.activity.kinds import KindRegistry
from scadbuddy.activity.models import STATUSES, ActivityRow, ActivityStatus, Facet, FacetType
from scadbuddy.activity.visibility import pinned_principal, readable
from scadbuddy.api.components import component_dep
from scadbuddy.core.problems import ApiError

router = APIRouter(prefix="/activity", tags=["activity"])

ActivityDep = Annotated[ActivityComponent, component_dep(ACTIVITY)]

UNKNOWN_PARAMETER = "https://scadbuddy.dev/problems/unknown-parameter"
BAD_CURSOR = "https://scadbuddy.dev/problems/bad-cursor"
FACET_PREFIX = "facet."
LIST_PARAMS = frozenset(
    {
        "kind",
        "status",
        "principal",
        "session",
        "via_tool",
        "subject",
        "since",
        "until",
        "parent",
        "q",
        "cursor",
        "limit",
    }
)
#: The row's fields a detail answers only with ``?advanced=1``.
ADVANCED_FIELDS = frozenset(
    {"workflow_id", "run_id", "traceparent", "source", "source_id", "facets"}
)
#: The raw keys of a row's ``detail`` an answer leaves out without ``?advanced=1``.
ADVANCED_DETAIL = frozenset({"request", "result", "error"})


class ActivityPage(BaseModel):
    items: list[ActivityRow]
    next_cursor: str | None


class FacetOut(BaseModel):
    name: str
    type: FacetType
    label: str
    filterable: bool


class KindOut(BaseModel):
    key: str
    label: str
    facets: list[FacetOut]


class CountOut(BaseModel):
    value: str
    count: int


class ActivityFacets(BaseModel):
    kinds: list[KindOut]
    statuses: list[ActivityStatus]
    principals: list[CountOut]
    tools: list[CountOut]
    subjects: list[CountOut]


class LinkOut(BaseModel):
    rel: str
    label: str
    href: str


class ActivityDetail(BaseModel):
    #: The row; its advanced fields only with ``?advanced=1``.
    row: dict[str, Any]
    children: list[ActivityRow]
    links: list[LinkOut]
    detail: dict[str, Any]


def _unknown(request: Request, allowed: frozenset[str]) -> None:
    for name in request.query_params:
        if name not in allowed:
            raise ApiError(400, f"unknown query parameter {name!r}", type_=UNKNOWN_PARAMETER)


def _facet_filters(request: Request, kinds: KindRegistry, selected: list[str]) -> dict[str, Any]:
    """The ``facet.<name>`` parameters, each a filterable facet of a selected kind, as
    the JSON value the row's facets hold; anything else not in ``LIST_PARAMS`` is 400."""
    facets: dict[str, Any] = {}
    for name, value in request.query_params.multi_items():
        if name in LIST_PARAMS:
            continue
        if not name.startswith(FACET_PREFIX):
            raise ApiError(400, f"unknown query parameter {name!r}", type_=UNKNOWN_PARAMETER)
        facet_name = name.removeprefix(FACET_PREFIX)
        declared = [kinds.facet(k, facet_name) for k in selected]
        facet = next((f for f in declared if f is not None and f.filterable), None)
        if facet is None:
            raise ApiError(
                400,
                f"no selected kind declares a filterable facet {facet_name!r}",
                type_=UNKNOWN_PARAMETER,
            )
        facets[facet_name] = _facet_value(facet, value)
    return facets


def _facet_value(facet: Facet, value: str) -> Any:
    if facet.type == "number":
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                raise ApiError(400, f"facet {facet.name!r} is a number") from None
    if facet.type == "bool":
        if value not in ("true", "false"):
            raise ApiError(400, f"facet {facet.name!r} is true or false")
        return value == "true"
    return value


@router.get("", response_model=ActivityPage)
async def list_activity(
    request: Request,
    activity: ActivityDep,
    kind: Annotated[list[str] | None, Query()] = None,
    status: Annotated[list[ActivityStatus] | None, Query()] = None,
    principal: str | None = None,
    session: str | None = None,
    via_tool: str | None = None,
    subject: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    parent: str | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> ActivityPage:
    """Newest first, a page at a time: pass ``next_cursor`` back as ``cursor``. Filter
    by any field, and by ``facet.<name>=<value>`` for a filterable facet of a selected
    ``kind``."""
    kinds = await asyncio.to_thread(activity.kinds)
    facets = _facet_filters(request, kinds, kind or [])
    position = None
    if cursor is not None:
        try:
            position = decode_cursor(cursor)
        except ValueError:
            raise ApiError(400, "cursor is not a next_cursor", type_=BAD_CURSOR) from None
    pinned = await pinned_principal(request, activity.index.pool)
    rows, next_cursor = await activity.index.list(
        ActivityFilter(
            kinds=tuple(kind or ()),
            statuses=tuple(status or ()),
            principal=principal,
            session=session,
            via_tool=via_tool,
            subject=subject,
            parent=parent,
            q=q,
            since=since,
            until=until,
            facets=facets,
        ),
        pinned=pinned,
        cursor=position,
        limit=limit,
    )
    return ActivityPage(items=rows, next_cursor=next_cursor)


def _counts(counts: list[Count]) -> list[CountOut]:
    return [CountOut(value=c.value, count=c.count) for c in counts]


@router.get("/facets", response_model=ActivityFacets)
async def activity_facets(
    request: Request, activity: ActivityDep, since: datetime | None = None
) -> ActivityFacets:
    """What the filter bar offers: every kind with its facets, the statuses, and the
    principals, tools and subjects present since ``since`` (default the last 7 days)."""
    _unknown(request, frozenset({"since"}))
    kinds = await asyncio.to_thread(activity.kinds)
    pinned = await pinned_principal(request, activity.index.pool)
    counts = await activity.index.counts(since=since, pinned=pinned)
    return ActivityFacets(
        kinds=[
            KindOut(
                key=k.key,
                label=k.label,
                facets=[
                    FacetOut(name=f.name, type=f.type, label=f.label, filterable=f.filterable)
                    for f in k.facets
                ],
            )
            for k in kinds.all()
        ],
        statuses=list(STATUSES),
        principals=_counts(counts.principals),
        tools=_counts(counts.tools),
        subjects=_counts(counts.subjects),
    )


@router.get("/{activity_id}", response_model=ActivityDetail)
async def get_activity(
    request: Request, activity_id: str, activity: ActivityDep, advanced: bool = False
) -> ActivityDetail:
    """One row with its children, its links and its kind's detail. A row the request
    may not read answers 404, as a missing one does."""
    _unknown(request, frozenset({"advanced"}))
    pinned = await pinned_principal(request, activity.index.pool)
    row = await activity.index.get(activity_id)
    if row is None or not readable(pinned, row.principal):
        raise ApiError(404, f"no activity {activity_id!r}")
    children = await activity.index.children(activity_id, pinned=pinned)
    kinds = await asyncio.to_thread(activity.kinds)
    kind = kinds.get(row.kind)
    links = [] if kind is None else kind.links(row)
    detail = dict(row.detail if kind is None or kind.detail is None else kind.detail(row))
    shown = row.model_dump(mode="json")
    if not advanced:
        shown = {k: v for k, v in shown.items() if k not in ADVANCED_FIELDS}
        shown["detail"] = {k: v for k, v in row.detail.items() if k not in ADVANCED_DETAIL}
        detail = {k: v for k, v in detail.items() if k not in ADVANCED_DETAIL}
    return ActivityDetail(
        row=shown,
        children=children,
        links=[LinkOut(rel=link.rel, label=link.label, href=link.href) for link in links],
        detail=detail,
    )
