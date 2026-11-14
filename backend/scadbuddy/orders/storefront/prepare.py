"""Every colour combination of a listing, rendered before a visitor asks (#6237).

A combination is one value for each offered colour parameter of a published listing:
the colours of its bound that the public listing offers (`public_listing`: the listing
filaments the price book still has, never a support filament, #6152), each colour once
whatever its case. Every other offered parameter stays at its effective default
(`effective_default`). A visitor's page sends only the values the visitor set, so a
colour at its effective default is left out of what a combination asks, exactly as the
page leaves it out on its first load.

`combinations` builds each one through the visitor path's own `listing_values` and
`listing_reference`, so its render key is the key a visitor's render of the same choice
has, and that render reuses the prepared job (`reuse_finished`).

`prepare` queues each combination through `RenderService` at `PREPARE_PRIORITY`, below
every other render. `progress` reads the newest job of each combination's render key
(no table of its own: the key is deterministic). Neither route enumerates a listing
whose count is over the cap: the count is a product, computed first.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from fastapi import status
from pydantic import BaseModel

from scadbuddy.core.problems import ApiError
from scadbuddy.orders.models import Filament, Reference
from scadbuddy.orders.storefront.inputs import (
    effective_default,
    listing_reference,
    listing_values,
)
from scadbuddy.orders.storefront.listings import listing_not_found, problem
from scadbuddy.orders.storefront.wire import ChoiceBound, Listing, is_support
from scadbuddy.render.job_models import QueueFullError
from scadbuddy.render.schema import ParamValue
from scadbuddy.render.submit import PREPARE_PRIORITY

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState
    from scadbuddy.orders.component import OrdersComponent

logger = logging.getLogger(__name__)

#: How many submits one prepare has in flight at once.
PREPARE_CONCURRENCY: Final = 8
#: How long one prepare starts submits (seconds). A combination it did not reach in that
#: time is not queued (``queued`` is less than ``count``); the next prepare queues it.
#: This keeps the request well below the proxy's 15 s.
PREPARE_BUDGET: Final = 8.0


class PrepareProgress(BaseModel):
    """``GET …/listings/{id}/prepare``: the listing's colour combinations (``count``), the
    cap, and of the combinations, how many have a done render (``ready``), a render in the
    queue or running (``running``) and a failed render (``failed``). Over the cap the three
    are 0: the combinations are not enumerated."""

    count: int
    cap: int
    ready: int
    running: int
    failed: int


class PrepareStarted(BaseModel):
    """``POST …/listings/{id}/prepare``: the listing's colour combinations, and how many
    now have a render queued, running or done."""

    count: int
    queued: int


@dataclass(frozen=True)
class Combination:
    """One combination: the values a visitor's page sends for it, and its reference."""

    asked: dict[str, ParamValue]
    reference: Reference


def colour_choices(listing: Listing, filaments: Mapping[str, Filament]) -> dict[str, list[str]]:
    """Each offered colour parameter of the published ``listing``, by name: the colours of
    its bound that the public listing offers, in the bound's order, each once (casefolded)."""
    assert listing.snapshot is not None
    by_name = {p.name: p for p in listing.snapshot.schema_.parameters}
    offered = {
        filaments[f].colour.casefold()
        for f in listing.colours
        if f in filaments and not is_support(filaments[f].material)
    }
    choices: dict[str, list[str]] = {}
    for name, bound in listing.params.items():
        parameter = by_name.get(name)
        if parameter is None or parameter.type != "color" or not isinstance(bound, ChoiceBound):
            continue
        seen: set[str] = set()
        kept: list[str] = []
        for choice in bound.choices:
            if not isinstance(choice, str):
                continue
            folded = choice.casefold()
            if folded in offered and folded not in seen:
                seen.add(folded)
                kept.append(choice)
        choices[name] = kept
    return choices


def combination_count(choices: Mapping[str, Collection[str]]) -> int:
    """How many combinations ``choices`` make: 0 with no colour parameter."""
    return math.prod(len(c) for c in choices.values()) if choices else 0


def combinations(
    listing: Listing, filaments: Mapping[str, Filament], choices: Mapping[str, list[str]]
) -> Iterator[Combination]:
    """Each combination of ``choices`` (`colour_choices`), as a visitor's page asks for
    it: a colour equal (casefolded) to its effective default is left out."""
    assert listing.snapshot is not None
    by_name = {p.name: p for p in listing.snapshot.schema_.parameters}
    names = list(choices)
    defaults: dict[str, str | None] = {}
    for name in names:
        start = effective_default(by_name[name], listing.params[name])
        defaults[name] = start.casefold() if isinstance(start, str) else None
    for picked in itertools.product(*(choices[name] for name in names)):
        asked: dict[str, ParamValue] = {
            name: value
            for name, value in zip(names, picked, strict=True)
            if value.casefold() != defaults[name]
        }
        values = listing_values(listing, asked, filaments)
        resolved = listing_reference(listing, values, {}, filaments=filaments)
        yield Combination(asked=asked, reference=resolved.reference)


def _runtime_cap(state: AppState) -> int:
    cap: int = state.settings_store.snapshot().runtime.storefront_prepare_cap
    return cap


def _published(orders: OrdersComponent, listing_id: str) -> Listing:
    listing = orders.listings.get(listing_id)
    if listing is None:
        raise listing_not_found(listing_id)
    if not listing.published or listing.snapshot is None:
        raise problem(
            status.HTTP_409_CONFLICT,
            "listing_not_published",
            "Publish this listing before you prepare its colours.",
        )
    return listing


def _facts(
    state: AppState, orders: OrdersComponent, listing_id: str
) -> tuple[Listing, dict[str, Filament], dict[str, list[str]], int]:
    """Blocking: the published listing, the price book, its colour choices and the cap."""
    listing = _published(orders, listing_id)
    filaments = {f.id: f for f in orders.store.filaments()}
    return listing, filaments, colour_choices(listing, filaments), _runtime_cap(state)


def job_states(state: AppState, slug: str, keys: Collection[str]) -> dict[str, str]:
    """The state of the newest render job of each of ``keys`` (of template ``slug``) that
    has one. Each half reads a partial index on ``slug``: the open jobs, and the settled
    ones. A cancelled job is left out. Blocking."""
    if not keys:
        return {}
    with state.projection.pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (render_key) render_key, state FROM ("
            " SELECT render_key, state, created_at, id FROM render_jobs"
            "  WHERE slug = %(slug)s AND state IN ('pending', 'running')"
            "  AND kind = 'render' AND render_key = ANY(%(keys)s)"
            " UNION ALL"
            " SELECT render_key, state, created_at, id FROM render_jobs"
            "  WHERE slug = %(slug)s AND state IN ('done', 'failed')"
            "  AND kind = 'render' AND render_key = ANY(%(keys)s)"
            ") AS jobs ORDER BY render_key, created_at DESC, id DESC",
            {"slug": slug, "keys": list(keys)},
        ).fetchall()
    return {row["render_key"]: row["state"] for row in rows}


def _over_cap(count: int, cap: int) -> ApiError:
    return problem(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "listing_combinations_over_cap",
        f"This listing has {count} colour combinations. The shop prepares at most {cap} at a time.",
        count=count,
        cap=cap,
    )


async def progress(state: AppState, orders: OrdersComponent, listing_id: str) -> PrepareProgress:
    """The listing's combinations and how far their renders are. 404
    ``listing_not_found``, 409 ``listing_not_published``. A listing with no colour
    parameter answers a ``count`` of 0."""
    listing, filaments, choices, cap = await asyncio.to_thread(_facts, state, orders, listing_id)
    count = combination_count(choices)
    if count == 0 or count > cap:
        return PrepareProgress(count=count, cap=cap, ready=0, running=0, failed=0)
    keys = [c.reference.render_key for c in combinations(listing, filaments, choices)]
    states = await asyncio.to_thread(job_states, state, listing.slug, keys)
    seen = [states.get(key) for key in keys]
    return PrepareProgress(
        count=count,
        cap=cap,
        ready=seen.count("done"),
        running=seen.count("pending") + seen.count("running"),
        failed=seen.count("failed"),
    )


async def prepare(state: AppState, orders: OrdersComponent, listing_id: str) -> PrepareStarted:
    """Queue a render of each colour combination of the published listing at
    `PREPARE_PRIORITY`, with ``reuse_finished``: a combination with an open job is left
    as it is, a done one is reused, and any other is rendered. Refusals: 404
    ``listing_not_found``, 409 ``listing_not_published``, 422 ``listing_no_colours`` (no
    offered colour parameter, or one with no colour left), 422
    ``listing_combinations_over_cap`` (with ``count`` and ``cap``), 503
    ``render_unavailable`` when no combination could be queued. A full queue or the end
    of `PREPARE_BUDGET` stops the run: ``queued`` is then less than ``count``, and the next
    prepare queues the rest.

    A prepared render is kept as any other render is: the prune follows ``job_ttl`` (with
    ``job_ttl`` 0, #6244, it is kept), and a visitor's reuse keeps it a ``job_ttl`` more.
    There is no keep mark of its own."""
    listing, filaments, choices, cap = await asyncio.to_thread(_facts, state, orders, listing_id)
    count = combination_count(choices)
    if count == 0:
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "listing_no_colours",
            "This listing offers no colour to choose.",
        )
    if count > cap:
        raise _over_cap(count, cap)
    assert listing.snapshot is not None
    pipeline = listing.snapshot.pipeline
    every = list(combinations(listing, filaments, choices))
    states = await asyncio.to_thread(
        job_states, state, listing.slug, [c.reference.render_key for c in every]
    )
    queued = 0
    todo: list[Combination] = []
    for combination in every:
        if states.get(combination.reference.render_key) in ("pending", "running"):
            queued += 1
        else:
            todo.append(combination)
    loop = asyncio.get_running_loop()
    ends = loop.time() + PREPARE_BUDGET
    stopped = False
    failures = 0

    async def submit(combination: Combination) -> bool:
        reference = combination.reference
        params = reference.inputs.get("params", {})
        await state.render.submit_answer(
            reference.slug,
            params if isinstance(params, dict) else {},
            model_version=reference.commit,
            inputs=reference.inputs,
            whole_inputs=pipeline,
            # One claim per listing and key: a prepare sent again joins the same claim.
            request_id=f"prepare:{listing.id}:{reference.render_key}",
            reuse_finished=True,
            priority=PREPARE_PRIORITY,
        )
        return True

    for start in range(0, len(todo), PREPARE_CONCURRENCY):
        if stopped or loop.time() >= ends:
            break
        batch = todo[start : start + PREPARE_CONCURRENCY]
        answers = await asyncio.gather(*(submit(c) for c in batch), return_exceptions=True)
        for answer in answers:
            if answer is True:
                queued += 1
            elif isinstance(answer, QueueFullError):
                stopped = True
            elif isinstance(answer, BaseException):
                failures += 1
                stopped = True
                logger.warning(
                    "a listing's colour combination was not queued",
                    exc_info=answer,
                    extra={"listing_id": listing.id},
                )
    if queued == 0 and failures:
        raise problem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "render_unavailable",
            "The shop could not start these renders. Try again in a moment.",
        )
    return PrepareStarted(count=count, queued=queued)
