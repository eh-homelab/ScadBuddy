"""Reading activity (spec 2026-10-09 §6, §7): the list, its facets and one row.

Every request reads everything, as the browser does: a non-browser principal is held
to its own rows by the agent's activity tools. Unknown query parameters answer 400:
FastAPI would ignore them, and a misspelt filter would then list everything.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
import contextlib
import math
import re
import time
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
from scadbuddy.api.components import component_dep
from scadbuddy.api.deps import CatalogueDep, OutputsDep
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.outputs import OUTPUT_ID_PATTERN, OutputNotFoundError, OutputStore

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
    #: How a subject reads, where ScadBuddy knows it (#3856): the model's name for a
    #: slug, the output's for ``output:<id>``. The value stays the filter.
    label: str | None = None


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
        cursor=position,
        limit=limit,
    )
    return ActivityPage(items=[_item(r, advanced) for r in rows], next_cursor=next_cursor)


def _counts(counts: list[Count], labels: Mapping[str, str] | None = None) -> list[CountOut]:
    return [
        CountOut(value=c.value, count=c.count, label=(labels or {}).get(c.value)) for c in counts
    ]


#: How many subjects (the most frequent) the facets look up a name for (#3856).
LABELLED_SUBJECTS = 100
#: How long an output found gone is not looked for again: each look is a glob of the
#: outputs volume, and a page refetches its facets on every change.
GONE_OUTPUT_SECONDS = 60.0
_gone_outputs: dict[str, float] = {}


def _subject_labels(
    subjects: list[Count], catalogue: Catalogue, outputs: OutputStore
) -> dict[str, str]:
    """The names ScadBuddy holds for the subjects: a model's, and an output's (its own
    name, else its model's). A library file or project is Bambuddy's, so it has none."""
    # The most frequent first: past them a chip shows its reference, and the facets
    # stay one catalogue read and a bounded number of output reads.
    values = {c.value for c in subjects[:LABELLED_SUBJECTS]}
    metas: dict[str, tuple[str | None, str]] = {}
    for value in values:
        if not value.startswith("output:"):
            continue
        output_id = value.removeprefix("output:")
        if not re.fullmatch(OUTPUT_ID_PATTERN, output_id):
            continue  # not an id at all: never let it near the store's paths
        if _gone_outputs.get(output_id, 0.0) > time.monotonic():
            continue
        try:
            metas[value] = outputs.name_and_slug(output_id)
        except OutputNotFoundError:
            if len(_gone_outputs) >= 10 * LABELLED_SUBJECTS:
                _gone_outputs.clear()
            _gone_outputs[output_id] = time.monotonic() + GONE_OUTPUT_SECONDS
        except (OSError, ValueError):
            continue  # deleted mid-read, or its meta unreadable: the chip keeps its id
    wanted = values | {slug for _, slug in metas.values()}
    slugs = [slug for slug in catalogue.slugs() if slug in wanted]
    names = {record.slug: record.name for record in catalogue.list_models(slugs)}
    # An output's model is looked up only to name the output; it is no chip of its own.
    labels = {value: name for value, name in names.items() if value in values}
    for value, (own, slug) in metas.items():
        name = own or names.get(slug)
        if name:
            labels[value] = name
    return labels


@router.get("/facets", response_model=ActivityFacets)
async def activity_facets(
    request: Request,
    activity: ActivityDep,
    catalogue: CatalogueDep,
    outputs: OutputsDep,
    since: datetime | None = None,
) -> ActivityFacets:
    """What the filter bar offers: every kind with its facets, the statuses, and the
    principals, tools and subjects present since ``since`` (default the last 7 days)."""
    _no_nul(request)
    _unknown(request, frozenset({"since"}))
    kinds = await asyncio.to_thread(activity.kinds)
    counts = await activity.index.counts(since=since)
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
        subjects=_counts(
            counts.subjects,
            await asyncio.to_thread(_subject_labels, counts.subjects, catalogue, outputs),
        ),
    )


@router.get("/{activity_id}", response_model=ActivityDetail, response_model_exclude_unset=True)
async def get_activity(
    request: Request, activity_id: str, activity: ActivityDep, advanced: bool = False
) -> ActivityDetail:
    """One row with its children, its links and its kind's detail."""
    _no_nul(request, activity_id)
    _unknown(request, frozenset({"advanced"}))
    row = await activity.index.get(activity_id)
    if row is None:
        raise ApiError(404, f"no activity {activity_id!r}")
    children = await activity.index.children(activity_id)
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
