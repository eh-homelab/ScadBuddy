"""Reading activity (spec 2026-10-09 §6, §7): the list, its facets and one row.

A request with the agent-actor marker (the agent's headless browser) reads only its
session owner's rows, in all three (``activity/visibility.py``); every other request
reads everything. Unknown query parameters answer 400: FastAPI would ignore them, and a
misspelt filter would then list everything.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
import contextlib
import math
from collections.abc import Mapping
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
BAD_VALUE = "https://scadbuddy.dev/problems/bad-parameter-value"
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
        "advanced",
    }
)
#: The row's fields an answer carries only with ``?advanced=1``.
ADVANCED_FIELDS = frozenset(
    {"workflow_id", "run_id", "traceparent", "source", "source_id", "facets"}
)
#: The raw keys of a row's ``detail`` an answer leaves out without ``?advanced=1``.
ADVANCED_DETAIL = frozenset({"request", "result", "error"})


class ActivityItem(BaseModel):
    """An activity row as answered: the fields in ``ADVANCED_FIELDS`` are left out, and
    ``detail`` has no raw ``request``/``result``/``error``, unless ``?advanced=1``."""

    id: str
    kind: str
    principal: str | None
    session: str | None
    via_tool: str | None
    subject: str
    status: ActivityStatus
    title: str
    parent_id: str | None
    detail: dict[str, Any]
    created_at: datetime | None
    updated_at: datetime | None
    finished_at: datetime | None
    facets: dict[str, Any] | None = None
    source: str | None = None
    source_id: str | None = None
    workflow_id: str | None = None
    run_id: str | None = None
    traceparent: str | None = None


def _item(row: ActivityRow, advanced: bool) -> ActivityItem:
    fields = row.model_dump()
    if not advanced:
        fields = {k: v for k, v in fields.items() if k not in ADVANCED_FIELDS}
        fields["detail"] = _plain(row.detail)
    return ActivityItem(**fields)


def _plain(detail: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in detail.items() if k not in ADVANCED_DETAIL}


class ActivityPage(BaseModel):
    items: list[ActivityItem]
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
    row: ActivityItem
    children: list[ActivityItem]
    links: list[LinkOut]
    detail: dict[str, Any]


def _no_nul(request: Request, *values: str) -> None:
    """A NUL byte in any query parameter or path value is 400, before it reaches
    Postgres (whose text cannot hold one)."""
    texts = [*values, *(t for item in request.query_params.multi_items() for t in item)]
    if any("\x00" in text for text in texts):
        raise ApiError(400, "a query parameter or path holds a NUL byte")


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
        with contextlib.suppress(ValueError):
            return int(value)
        try:
            number = float(value)
        except ValueError:
            number = math.nan
        if not math.isfinite(number):
            raise ApiError(400, f"facet {facet.name!r} is a number", type_=BAD_VALUE)
        return number
    if facet.type == "bool":
        if value not in ("true", "false"):
            raise ApiError(400, f"facet {facet.name!r} is true or false", type_=BAD_VALUE)
        return value == "true"
    return value


@router.get("", response_model=ActivityPage, response_model_exclude_unset=True)
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
    advanced: bool = False,
) -> ActivityPage:
    """Newest first, a page at a time: pass ``next_cursor`` back as ``cursor``. Filter
    by any field, and by ``facet.<name>=<value>`` for a filterable facet of a selected
    ``kind``."""
    _no_nul(request)
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
    return ActivityPage(items=[_item(r, advanced) for r in rows], next_cursor=next_cursor)


def _counts(counts: list[Count]) -> list[CountOut]:
    return [CountOut(value=c.value, count=c.count) for c in counts]


@router.get("/facets", response_model=ActivityFacets)
async def activity_facets(
    request: Request, activity: ActivityDep, since: datetime | None = None
) -> ActivityFacets:
    """What the filter bar offers: every kind with its facets, the statuses, and the
    principals, tools and subjects present since ``since`` (default the last 7 days)."""
    _no_nul(request)
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


@router.get("/{activity_id}", response_model=ActivityDetail, response_model_exclude_unset=True)
async def get_activity(
    request: Request, activity_id: str, activity: ActivityDep, advanced: bool = False
) -> ActivityDetail:
    """One row with its children, its links and its kind's detail. A row the request
    may not read answers 404, as a missing one does."""
    _no_nul(request, activity_id)
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
    return ActivityDetail(
        row=_item(row, advanced),
        children=[_item(c, advanced) for c in children],
        links=[LinkOut(rel=link.rel, label=link.label, href=link.href) for link in links],
        detail=detail if advanced else _plain(detail),
    )
