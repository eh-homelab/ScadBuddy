"""Every colour combination of a listing, rendered before a visitor asks (#6237).

A print is made of one material (#6258), so the combinations are enumerated per
printable material: of the listing's effective materials (`effective_materials`), in
order, those `printable_materials` keeps (#6302 review). For one
material, a combination is one value for each offered colour parameter of the published
listing: the colours of its bound that the material's customer filaments have
(`customer_filaments`: the listing filaments the price book still has, never a support
filament, #6152), in each spelling the page can send (`colour_choices`). Every other
offered parameter stays at its effective default (`effective_default`). A visitor's page
sends only the values the visitor set, so a colour at its effective default is left out
of what a combination asks, exactly as the page leaves it out on its first load.

`combinations` builds each one through the visitor's render path: `listing_values`, then
`resolve_choice`, then `listing_reference` (#6302 review). So its render key is the key a
visitor's render of the same picks has, and that render reuses the prepared job
(`reuse_finished`). A combination that clashes (`ColoursClashError`) is left out. The
combinations are one per render key: of those with one key, the first in material order
is kept. ``count`` is the number of render keys, and the cap applies to it.

`start_run` starts the listing's ``PrepareListing`` run (`workflows/prepare_listing.py`) on
the ``library`` queue. The run submits the combinations a few at a time
(`PREPARE_IN_FLIGHT`), at the default priority (#6299 review): a visitor's render of a
combination joins the prepared execution, so a lower priority would hold the visitor up.
Its activities are `submit` and `still_open`. `progress` reads the newest job of each
combination's render key (no table of its own: the key is deterministic) and the run's
state. No route enumerates a listing with more than `ENUMERATION_LIMIT` picks: that
bound is a sum of products, computed first.

Warming (#6922): once the renders settle, the run asks for each combination's storefront
slice price (`warm`, activity `prepare_listing_warm`) as a visitor's estimate asks for it:
the done render's 3MF and slots, with the combination's variant key, through
`slice_price.py` `slice_estimate`, which claims and starts the slice under
`storefront_slices_concurrent`. So there is no second slicer path, a variant with a
``done`` price starts no slice, and a variant with no room waits. Publish starts a run of
the default combination only (`warm_default`, `default_combination`), so the default
variant has a price before a visitor asks. A file listing (#7523) has no render: its run
submits none, and prices the colours its page opens on (`file_prices.py` `default_choice`)
with no visitor charge. A listing that publish never priced (published before #6922, a
start that failed, a run that ended with no price) is started by the housekeeping sweep
(#7665, `warm_unpriced`). All are off with `storefront_warm_prices`.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import math
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from fastapi import status
from pydantic import BaseModel
from temporalio import activity
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError

from scadbuddy.core.problems import ApiError
from scadbuddy.orders.estimate import StoreEstimate
from scadbuddy.orders.models import Filament, Reference
from scadbuddy.orders.storefront.file_prices import FileChoice, default_choice, plan_file
from scadbuddy.orders.storefront.inputs import (
    ColoursClashError,
    LockedColoursClashError,
    customer_filaments,
    effective_default,
    effective_materials,
    listing_reference,
    listing_values,
    printable_materials,
    resolve_choice,
)
from scadbuddy.orders.storefront.listings import listing_not_found, problem
from scadbuddy.orders.storefront.renders import estimate_of, variant_key
from scadbuddy.orders.storefront.slice_price import planned_estimate
from scadbuddy.orders.storefront.wire import ChoiceBound, Listing
from scadbuddy.render.job_models import Job, JobNotFoundError, QueueFullError
from scadbuddy.render.schema import ParamValue
from scadbuddy.workflows.commands import execution_gone
from scadbuddy.workflows.prepare_listing import (
    FAILED_MEMO,
    PREPARE_LISTING_WORKFLOW,
    SETTLED_ACTIVITY,
    SUBMIT_ACTIVITY,
    WARM_ACTIVITY,
    OpenJob,
    PrepareListingStart,
    SubmitAsk,
    Submitted,
    Warmed,
    default_workflow_id,
    workflow_id,
)

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState
    from scadbuddy.orders.component import OrdersComponent

logger = logging.getLogger(__name__)

#: How long a publish waits for its default price run to start (#6922).
WARM_START_TIMEOUT: Final = 5.0

#: The most default price runs the sweep (#7665) leaves open at once, so visitors' renders
#: and slices keep their room. The rest wait for the next pass.
WARM_SWEEP_BATCH: Final = 2
#: How long the sweep waits for one Temporal read or start.
WARM_SWEEP_TIMEOUT: Final = 10.0
#: The listings the sweep reads per query.
WARM_SWEEP_PAGE: Final = 100

#: A job state that is not settled yet.
OPEN_STATES: Final = ("pending", "running")

#: The most picks (the sum, over the materials, of the product of each colour
#: parameter's choices) that are enumerated. Above it, ``count`` is that sum, an upper
#: bound of the render keys: at least 4 times the highest cap the setting allows
#: (``storefront_prepare_cap``, at most 4096), so it is over any cap.
ENUMERATION_LIMIT: Final = 16384


class PrepareProgress(BaseModel):
    """``GET …/listings/{id}/prepare``: the listing's colour combinations (``count``), the
    cap, and of the combinations, how many have a done render (``ready``), a render in the
    queue or running (``rendering``) and a failed render (``failed``). Over the cap the
    three are 0: the combinations are not enumerated. ``running`` is whether the listing's
    prepare run is open, and ``failed_to_start`` how many combinations the last run could
    not submit and that have no done render since (#6299 review)."""

    count: int
    cap: int
    ready: int
    rendering: int
    failed: int
    running: bool
    failed_to_start: int


class PrepareStarted(BaseModel):
    """``POST …/listings/{id}/prepare``: the listing's colour combinations, and whether this
    POST started the run (false: a run was open already, and the POST joined it)."""

    count: int
    started: bool


@dataclass(frozen=True)
class Combination:
    """One combination: the values a visitor's page sends for it, its reference, the
    material its colours resolve to, and its variant key (`renders.py` `variant_key`,
    #6922): the key a visitor's choices of it have, which its warmed price records."""

    asked: dict[str, ParamValue]
    reference: Reference
    material: str | None
    variant_key: str


@dataclass(frozen=True)
class RunState:
    """The listing's prepare run (its newest one): whether it is open, its run ids (None
    with no run), and the indices of its failed submits. ``legacy_failed`` is the count a
    run from before `FAILED_INDICES_PATCH` keeps instead."""

    running: bool
    run_id: str | None = None
    first_run_id: str | None = None
    failed_indices: tuple[int, ...] = ()
    legacy_failed: int = 0


def request_id(listing_id: str, run_id: str, render_key: str) -> str:
    """The claim of one prepared render (#6299 review): one per run of the listing's
    prepare workflow and key. A retry of one submit joins its claim; a new run, or one
    continued as new, makes a new one, so a failed render is rendered again. Each index is
    submitted once per run."""
    return f"prepare:{listing_id}:{run_id}:{render_key}"


def not_started(run: RunState, states: Sequence[str | None]) -> int:
    """How many of the run's failed submits (``run.failed_indices``, indices into
    ``states``, each combination's newest job state) have no done job yet."""
    if run.legacy_failed:
        return run.legacy_failed
    return sum(1 for i in set(run.failed_indices) if i < len(states) and states[i] != "done")


def colour_choices(
    listing: Listing, filaments: Mapping[str, Filament], materials: Sequence[str]
) -> dict[str, dict[str, list[str]]]:
    """For each printable one of ``materials`` (the effective materials, in order,
    `effective_materials`; `printable_materials`, #6302 review):
    each offered colour parameter of the published ``listing``, by name, with each spelling
    a visitor's page can send for it in that material, in the bound's order (casefolded,
    each colour once). The page sends a picked filament's own ``colour`` (`ParamField.tsx`),
    and a spelling is part of the render key, so a colour takes each of the material's
    filaments' spelling of it, once. The effective default comes first when no filament
    spells it so: it is what the page's first load renders (#6299 review)."""
    # Only a printable material (#6302 review): another can only clash, or never renders.
    return {
        material: _material_choices(listing, customer_filaments(listing, filaments, [material]))
        for material in printable_materials(listing, filaments, materials)
    }


def _material_choices(listing: Listing, offered: Sequence[Filament]) -> dict[str, list[str]]:
    """`colour_choices` of one material, whose customer filaments are ``offered``."""
    by_name = {p.name: p for p in listing.design[2].schema_.parameters}
    spellings: dict[str, list[str]] = {}
    for filament in offered:
        same = spellings.setdefault(filament.colour.casefold(), [])
        if filament.colour not in same:
            same.append(filament.colour)
    choices: dict[str, list[str]] = {}
    for name, bound in listing.params.items():
        parameter = by_name.get(name)
        if parameter is None or parameter.type != "color" or not isinstance(bound, ChoiceBound):
            continue
        start = effective_default(parameter, bound)
        seen: set[str] = set()
        kept: list[str] = []
        for choice in bound.choices:
            if not isinstance(choice, str):
                continue
            folded = choice.casefold()
            if folded not in spellings or folded in seen:
                continue
            seen.add(folded)
            if (
                isinstance(start, str)
                and start.casefold() == folded
                and start not in spellings[folded]
            ):
                kept.append(start)
            kept += spellings[folded]
        choices[name] = kept
    return choices


def picks(choices: Mapping[str, Mapping[str, Collection[str]]]) -> int:
    """The sum, over the materials of ``choices`` (`colour_choices`), of the product of
    each colour parameter's choices: an upper bound of the combinations. 0 with no colour
    parameter."""
    return sum(math.prod(len(c) for c in each.values()) if each else 0 for each in choices.values())


def _build(
    listing: Listing,
    filaments: Mapping[str, Filament],
    materials: Sequence[str],
    choices: Mapping[str, Mapping[str, list[str]]],
) -> list[Combination]:
    """`combinations`, built: each material's picks in `itertools.product` order (the
    last name varies fastest), the materials in order. Each is asked as a visitor's page
    asks for it: a colour that is its effective default, in the same spelling, is left
    out. Then it goes through the visitor's render path (`renders.py` `checked_choice`):
    `listing_values`, `resolve_choice` with the asked names as given, `listing_reference`.
    A clash is left out, and of the combinations with one render key the first is kept."""
    by_name = {p.name: p for p in listing.design[2].schema_.parameters}
    built: dict[str, Combination] = {}
    for each in choices.values():
        names = list(each)
        if not names:
            continue
        defaults = {n: effective_default(by_name[n], listing.params[n]) for n in names}
        for picked in itertools.product(*(each[n] for n in names)):
            asked: dict[str, ParamValue] = {
                n: value for n, value in zip(names, picked, strict=True) if value != defaults[n]
            }
            try:
                combination = _combination(listing, filaments, materials, asked)
            except ColoursClashError:
                continue
            except LockedColoursClashError:
                # No material can print it: no combination renders (#6302 review).
                return []
            built.setdefault(combination.reference.render_key, combination)
    return list(built.values())


def _combination(
    listing: Listing,
    filaments: Mapping[str, Filament],
    materials: Sequence[str],
    asked: dict[str, ParamValue],
) -> Combination:
    """The combination a visitor's page asks for with ``asked``, through the visitor's
    render path. Raises `ColoursClashError` and `LockedColoursClashError` as
    `resolve_choice` does."""
    values = listing_values(listing, asked, filaments, materials)
    choice = resolve_choice(listing, values, asked.keys(), filaments, materials)
    reference = listing_reference(listing, choice.values, {}, filaments=filaments).reference
    return Combination(
        asked=asked,
        reference=reference,
        material=choice.material,
        # The page's first load, and a colour pick, choose no filament per slot.
        variant_key=variant_key(listing, choice, {}),
    )


def default_combination(
    listing: Listing, filaments: Mapping[str, Filament], materials: Sequence[str]
) -> Combination | None:
    """The published ``listing``'s default combination (#6922): what a visitor's page
    renders on its first load, which sends no value. None when its colours clash."""
    try:
        return _combination(listing, filaments, materials, {})
    except (ColoursClashError, LockedColoursClashError):
        return None


#: The newest built combinations, by what they are built from (`_cache_key`). A run
#: submits one index per activity, so each submit would build every combination again.
_BUILT: OrderedDict[tuple[str, ...], list[Combination]] = OrderedDict()
_BUILT_LOCK = threading.Lock()
_BUILT_SIZE: Final = 16


def _cache_key(
    listing: Listing, filaments: Mapping[str, Filament], materials: Sequence[str]
) -> tuple[str, ...]:
    """Everything a combination is built from: the listing, the price book's filaments of
    the listing, and the materials."""
    return (
        listing.model_dump_json(),
        *(filaments[f].model_dump_json() for f in listing.colours if f in filaments),
        "\0".join(materials),
    )


def combinations(
    listing: Listing, filaments: Mapping[str, Filament], materials: Sequence[str]
) -> list[Combination]:
    """The combinations of the published ``listing`` with ``materials`` (its effective
    materials, `effective_materials`), one per render key, in a fixed order (`_build`):
    the same listing, price book and materials give the same list, so an index names one
    combination (`combination_at`). Empty with more than `ENUMERATION_LIMIT` picks."""
    key = _cache_key(listing, filaments, materials)
    with _BUILT_LOCK:
        if key in _BUILT:
            _BUILT.move_to_end(key)
            return _BUILT[key]
    choices = colour_choices(listing, filaments, materials)
    built = (
        _build(listing, filaments, materials, choices)
        if picks(choices) <= ENUMERATION_LIMIT
        else []
    )
    with _BUILT_LOCK:
        _BUILT[key] = built
        while len(_BUILT) > _BUILT_SIZE:
            _BUILT.popitem(last=False)
    return built


def combination_count(
    listing: Listing, filaments: Mapping[str, Filament], materials: Sequence[str]
) -> int:
    """How many combinations the listing has: the number of render keys of
    `combinations`. With more than `ENUMERATION_LIMIT` picks, the picks (`picks`). 0 with
    no colour parameter."""
    bound = picks(colour_choices(listing, filaments, materials))
    if bound > ENUMERATION_LIMIT:
        return bound
    return len(combinations(listing, filaments, materials))


def combination_at(
    listing: Listing, filaments: Mapping[str, Filament], materials: Sequence[str], index: int
) -> Combination:
    """The combination at ``index`` of `combinations`. ``index`` is below
    `combination_count`."""
    return combinations(listing, filaments, materials)[index]


def _published(orders: OrdersComponent, listing_id: str) -> Listing:
    listing = orders.listings.get(listing_id)
    if listing is None:
        raise listing_not_found(listing_id)
    if not listing.published or listing.template_snapshot is None:
        # A file listing has no colour combinations to render (#6710).
        raise problem(
            status.HTTP_409_CONFLICT,
            "listing_not_published",
            "Publish this listing before you prepare its colours.",
        )
    return listing


@dataclass(frozen=True)
class _Facts:
    listing: Listing
    filaments: dict[str, Filament]
    #: The listing's effective materials (`effective_materials`, #6258).
    materials: list[str]
    cap: int
    #: `combination_count`.
    count: int


def _facts(state: AppState, orders: OrdersComponent, listing_id: str) -> _Facts:
    """Blocking: the published listing, the price book, the listing's effective materials,
    the cap, and the count of its combinations."""
    listing = _published(orders, listing_id)
    filaments = {f.id: f for f in orders.store.filaments()}
    runtime = state.settings_store.snapshot().runtime
    materials = effective_materials(listing, runtime.storefront_materials)
    return _Facts(
        listing=listing,
        filaments=filaments,
        materials=materials,
        cap=runtime.storefront_prepare_cap,
        count=combination_count(listing, filaments, materials),
    )


def _target(
    state: AppState, orders: OrdersComponent, ask: SubmitAsk
) -> tuple[Listing, Combination] | None:
    """Blocking: the published listing and the combination ``ask`` names, or None when
    there is none at its index (or the listing is over the cap). A publish's run
    (``default_only``) has one, the default (`default_combination`, #6922). Raises
    `ApiError` when the listing is gone or not published."""
    if not ask.default_only:
        facts = _facts(state, orders, ask.listing_id)
        if ask.index >= facts.count or facts.count > facts.cap:
            return None
        # `_facts` built them: this reads the cache.
        found = combination_at(facts.listing, facts.filaments, facts.materials, ask.index)
        return facts.listing, found
    listing = _published(orders, ask.listing_id)
    if ask.index > 0:
        return None
    filaments = {f.id: f for f in orders.store.filaments()}
    materials = effective_materials(
        listing, state.settings_store.snapshot().runtime.storefront_materials
    )
    default = default_combination(listing, filaments, materials)
    return None if default is None else (listing, default)


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


def _unavailable() -> ApiError:
    return problem(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "render_unavailable",
        "The shop could not start these renders. Try again in a moment.",
    )


@dataclass(frozen=True)
class DefaultRun:
    """The newest run of a listing's default combination."""

    running: bool
    started_at: datetime


class TemporalPreparer:
    """Starts and reads a listing's ``PrepareListing`` run."""

    def __init__(self, client: Client, task_queue: str) -> None:
        self.client = client
        self.task_queue = task_queue

    async def run_state(self, listing_id: str) -> RunState:
        """Whether the listing's run is open, its run ids, and the failed submits of its
        last run (none with no run). Raises `RPCError` when Temporal does not answer."""
        try:
            described = await self.client.get_workflow_handle(workflow_id(listing_id)).describe()
        except RPCError as error:
            if execution_gone(error):
                return RunState(running=False)
            raise
        memo = await described.memo()
        failed = memo.get(FAILED_MEMO)
        info = described.raw_description.workflow_execution_info
        return RunState(
            running=described.status == WorkflowExecutionStatus.RUNNING,
            run_id=described.run_id,
            first_run_id=info.first_run_id or None,
            failed_indices=tuple(i for i in failed if isinstance(i, int))
            if isinstance(failed, list)
            else (),
            legacy_failed=failed if isinstance(failed, int) else 0,
        )

    async def start(self, listing_id: str) -> bool:
        """Start the listing's run, or join the open one; True when this call started it.
        A run that ended is started again: its submits reuse every done job. Whether it
        started is read from the start's own answer (#6299 review): the run it names is not
        the open run described before, so a run that ends between the two calls is never
        taken as joined."""
        before = await self.run_state(listing_id)
        handle = await self.client.start_workflow(
            PREPARE_LISTING_WORKFLOW,
            PrepareListingStart(listing_id=listing_id),
            id=workflow_id(listing_id),
            task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
        if not before.running:
            return True
        # Joined: the answer names the open run, or (continued as new since) its chain.
        same_run = handle.result_run_id is not None and handle.result_run_id == before.run_id
        same_chain = (
            before.first_run_id is not None and handle.first_execution_run_id == before.first_run_id
        )
        return not (same_run or same_chain)

    async def last_default(self, listing_id: str) -> DefaultRun | None:
        """The newest run of the listing's default combination that Temporal still holds,
        or None. Raises `RPCError` when Temporal does not answer."""
        handle = self.client.get_workflow_handle(default_workflow_id(listing_id))
        try:
            described = await handle.describe()
        except RPCError as error:
            if execution_gone(error):
                return None
            raise
        return DefaultRun(
            running=described.status == WorkflowExecutionStatus.RUNNING,
            started_at=described.start_time,
        )

    async def start_default(self, listing_id: str, *, restart: bool = True) -> None:
        """Start the run of the listing's default combination (#6922). With ``restart`` (a
        publish's) a run of it still open is ended and started again: the listing it read
        may have moved (a new commit is a new render key, and a new version a new variant
        key). Without it (the sweep's, #7665) an open run is joined."""
        await self.client.start_workflow(
            PREPARE_LISTING_WORKFLOW,
            PrepareListingStart(listing_id=listing_id, default_only=True),
            id=default_workflow_id(listing_id),
            task_queue=self.task_queue,
            id_conflict_policy=(
                WorkflowIDConflictPolicy.TERMINATE_EXISTING
                if restart
                else WorkflowIDConflictPolicy.USE_EXISTING
            ),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )


def preparer(state: AppState) -> TemporalPreparer:
    """The app's preparer: the operations' client, on the ``library`` queue."""
    from scadbuddy.operations.component import OPERATIONS

    ops = state.components.get(OPERATIONS)
    return TemporalPreparer(ops.client, state.settings.temporal_task_queue_library)


async def warm_default(state: AppState, listing: Listing) -> None:
    """After a publish (#6922): start the run that renders the published ``listing``'s
    default combination and starts its storefront slice price (a file listing's has no
    render, #7523), unless `storefront_warm_prices` is off. A run that does not start is
    logged, never the publish's failure, nor is one that takes past `WARM_START_TIMEOUT`: a
    visitor's read still starts the slice. A start given up on may still have reached
    Temporal, so its log says it was given up, not that it failed."""
    if not state.settings.storefront_warm_prices or not listing.published:
        return
    try:
        # A Temporal that does not answer never holds up the publish (review of #6970).
        await asyncio.wait_for(preparer(state).start_default(listing.id), WARM_START_TIMEOUT)
    except TimeoutError:
        # Also a TimeoutError from inside start_default (a client timeout): that start
        # too may have reached Temporal, so it is "given up", with its traceback. Keep
        # the catch this wide.
        logger.warning(
            "gave up waiting for the default price run of listing %s to start",
            listing.id,
            exc_info=True,
        )
    except Exception:
        logger.warning(
            "could not start the default price run of listing %s", listing.id, exc_info=True
        )


async def _default_priced(state: AppState, orders: OrdersComponent, listing: Listing) -> bool:
    """Whether the published ``listing``'s default choices have a price (or can have none):
    a ``done`` row of the variant a visitor's page opens on, which the instant read answers
    from (`renders.py` `instant_variant`), or of a file listing's opening colours."""

    def read() -> tuple[FileChoice | None, bool]:
        filaments = {f.id: f for f in orders.store.filaments()}
        shop = state.settings_store.snapshot().runtime.storefront_materials
        if listing.file_snapshot is not None:
            choice = default_choice(listing, filaments, shop)
            return choice, choice is None
        default = default_combination(listing, filaments, effective_materials(listing, shop))
        return None, (
            default is None or orders.slice_prices.variant_row(default.variant_key) is not None
        )

    choice, settled = await asyncio.to_thread(read)
    if choice is None:
        return settled
    planned = await plan_file(state, listing, choice)
    if planned is None:
        return True
    row = await asyncio.to_thread(orders.slice_prices.get, planned.key)
    return row is not None and row.status == "done"


async def warm_unpriced(state: AppState, orders: OrdersComponent) -> int:
    """The housekeeping sweep ``housekeeping_warm_listings`` (#7665): start the default
    price run (`warm_default`) of each published listing whose default choices have no
    price, so the shop shows one before a visitor asks. It repairs what publish did not
    price: a listing published before #6922, a start that failed, a run that ended with no
    price.

    Temporal's own record bounds the retries: a listing whose newest run started since its
    version (``updated_at``, which its variant key holds) is left alone, so one that has no
    price to find is tried once per version, and again only when Temporal's retention
    drops that run. At most `WARM_SWEEP_BATCH` runs are open; the rest wait for the next
    pass. An open run is never ended. A listing that fails is logged, and the next one is
    read. The number of runs started."""
    if not state.settings.storefront_warm_prices:
        return 0
    starter = preparer(state)
    started = open_runs = 0
    after: tuple[int, str] | None = None
    while True:
        page = await asyncio.to_thread(
            orders.listings.published, limit=WARM_SWEEP_PAGE, after=after
        )
        for listing in page:
            try:
                if await _default_priced(state, orders, listing):
                    continue
                run = await asyncio.wait_for(starter.last_default(listing.id), WARM_SWEEP_TIMEOUT)
                if run is not None and (run.running or run.started_at >= listing.updated_at):
                    open_runs += run.running
                else:
                    await asyncio.wait_for(
                        starter.start_default(listing.id, restart=False), WARM_SWEEP_TIMEOUT
                    )
                    started += 1
                    open_runs += 1
            except Exception:
                logger.warning(
                    "could not sweep the default price of listing %s", listing.id, exc_info=True
                )
            if open_runs >= WARM_SWEEP_BATCH:
                return started
        if len(page) < WARM_SWEEP_PAGE:
            return started
        after = (page[-1].position, page[-1].id)


async def progress(state: AppState, orders: OrdersComponent, listing_id: str) -> PrepareProgress:
    """The listing's combinations, how far their renders are, and its run's state. 404
    ``listing_not_found``, 409 ``listing_not_published``. A listing with no colour
    parameter answers a ``count`` of 0. The run's state is read only for a listing that
    can be prepared (``0 < count <= cap``); when Temporal does not answer, the run reads as
    not running, so one failed read never fails the whole panel (#6299 review)."""
    facts = await asyncio.to_thread(_facts, state, orders, listing_id)
    count, cap = facts.count, facts.cap
    if count == 0 or count > cap:
        return PrepareProgress(
            count=count,
            cap=cap,
            ready=0,
            rendering=0,
            failed=0,
            running=False,
            failed_to_start=0,
        )
    try:
        run = await preparer(state).run_state(listing_id)
    except RPCError:
        logger.warning("could not read the prepare run of listing %s", listing_id)
        run = RunState(running=False)

    def read_states() -> tuple[list[str], dict[str, str]]:
        # Up to the cap of references and keys: off the event loop (#6299 review).
        every = combinations(facts.listing, facts.filaments, facts.materials)
        keys = [c.reference.render_key for c in every]
        return keys, job_states(state, facts.listing.design[0], keys)

    keys, states = await asyncio.to_thread(read_states)
    seen = [states.get(key) for key in keys]
    return PrepareProgress(
        count=count,
        cap=cap,
        ready=seen.count("done"),
        rendering=seen.count("pending") + seen.count("running"),
        failed=seen.count("failed"),
        running=run.running,
        failed_to_start=not_started(run, seen),
    )


async def start_run(state: AppState, orders: OrdersComponent, listing_id: str) -> PrepareStarted:
    """Start the listing's prepare run, or join the open one. Refusals: 404
    ``listing_not_found``, 409 ``listing_not_published``, 422 ``listing_no_colours`` (no
    offered colour parameter, or one with no colour left), 422
    ``listing_combinations_over_cap`` (with ``count`` and ``cap``), 503
    ``render_unavailable`` when Temporal does not start the run."""
    facts = await asyncio.to_thread(_facts, state, orders, listing_id)
    count, cap = facts.count, facts.cap
    if count == 0:
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "listing_no_colours",
            "This listing offers no colour to choose.",
        )
    if count > cap:
        raise _over_cap(count, cap)
    try:
        started = await preparer(state).start(listing_id)
    except Exception:
        logger.warning("could not start the prepare run of listing %s", listing_id)
        raise _unavailable() from None
    return PrepareStarted(count=count, started=started)


async def submit(
    state: AppState, orders: OrdersComponent, ask: SubmitAsk, run_id: str
) -> Submitted:
    """One step of the run: submit the combination at ``ask.index`` (`_target`) with
    ``reuse_finished``. A combination with an open job is left as it is (``open``), a done
    one answers ``settled``, and any other is rendered. ``end`` once the index is past the
    last combination, or the listing is not published (or is over the cap) any more.
    ``run_id`` is the prepare run's: it scopes the render's claim (`request_id`)."""
    try:
        target = await asyncio.to_thread(_target, state, orders, ask)
    except ApiError:
        return Submitted(outcome="end")
    if target is None:
        return Submitted(outcome="end")
    listing, combination = target
    slug, _, snapshot = listing.design
    reference = combination.reference
    job = OpenJob(slug=slug, render_key=reference.render_key)
    found = (await asyncio.to_thread(job_states, state, job.slug, [job.render_key])).get(
        job.render_key
    )
    if found in OPEN_STATES:
        return Submitted(outcome="open", job=job)
    if found == "done":
        return Submitted(outcome="settled")
    params = reference.inputs.get("params", {})
    try:
        answered, _ = await state.render.submit_answer(
            reference.slug,
            params if isinstance(params, dict) else {},
            model_version=reference.commit,
            inputs=reference.inputs,
            whole_inputs=snapshot.pipeline,
            # One claim per run and key (#6299 review): a retry of this submit joins it,
            # and "Prepare again" makes a new one, so a failed render is rendered again.
            request_id=request_id(listing.id, run_id, reference.render_key),
            reuse_finished=True,
        )
    except QueueFullError:
        return Submitted(outcome="busy")
    except Exception:
        logger.warning(
            "a listing's colour combination was not queued",
            exc_info=True,
            extra={"listing_id": listing.id},
        )
        return Submitted(outcome="failed")
    if answered.state in OPEN_STATES:
        return Submitted(outcome="open", job=job)
    return Submitted(outcome="settled")


async def still_open(state: AppState, jobs: Sequence[OpenJob]) -> list[OpenJob]:
    """Of ``jobs``, those whose newest job is still in the queue or running. A job with no
    row (cancelled, or pruned) counts as settled."""
    open_jobs: list[OpenJob] = []
    for slug in dict.fromkeys(j.slug for j in jobs):
        keys = [j.render_key for j in jobs if j.slug == slug]
        states = await asyncio.to_thread(job_states, state, slug, keys)
        open_jobs += [j for j in jobs if j.slug == slug and states.get(j.render_key) in OPEN_STATES]
    return open_jobs


def _done_job(state: AppState, slug: str, render_key: str) -> Job | None:
    """The newest done render job of ``render_key`` (of template ``slug``), or None.
    Blocking."""
    with state.projection.pool.connection() as conn:
        row = conn.execute(
            "SELECT id FROM render_jobs WHERE slug = %s AND state = 'done'"
            " AND kind = 'render' AND render_key = %s"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (slug, render_key),
        ).fetchone()
    if row is None:
        return None
    try:
        return state.render.store.read(row["id"])
    except JobNotFoundError:
        return None


async def warm(state: AppState, orders: OrdersComponent, ask: SubmitAsk) -> Warmed:
    """One step of the price phase (#6922): the storefront slice price of the combination
    at ``ask.index``, asked for as a visitor's render of it asks (`renders.py`
    `estimate_of`, `render_estimate`'s): its done render's 3MF and slots, with its variant
    key, through `slice_estimate`, which claims and starts the slice under
    `storefront_slices_concurrent`. ``settled`` once it has a price or none can be had (no
    done render, its files lost, no shop slice profile, a failed slice); ``running`` while
    its slice runs; ``waiting`` while its files are fetched or the cap leaves no room;
    ``end`` past the last combination, for a listing not published, or with
    `storefront_warm_prices` off. A publish's run of a file listing is `_warm_file`."""
    if not state.settings.storefront_warm_prices:
        return Warmed(outcome="end")
    if ask.default_only:
        file = await asyncio.to_thread(orders.listings.get, ask.listing_id)
        if file is not None and file.published and file.file_snapshot is not None:
            return await _warm_file(state, orders, file, ask.index)
    try:
        target = await asyncio.to_thread(_target, state, orders, ask)
    except ApiError:
        return Warmed(outcome="end")
    if target is None:
        return Warmed(outcome="end")
    listing, combination = target
    job = await asyncio.to_thread(
        _done_job, state, listing.design[0], combination.reference.render_key
    )
    if job is None or job.result is None:
        return Warmed(outcome="settled")
    estimate = await estimate_of(
        state, listing, job, {}, combination.material, variant_key=combination.variant_key
    )
    return await _warmed(orders, estimate)


async def _warm_file(
    state: AppState, orders: OrdersComponent, listing: Listing, index: int
) -> Warmed:
    """`warm`, for the published file ``listing`` (#7523): the one price of the colours its
    page opens on, read with no ``charge``, so it counts against no visitor."""
    if index > 0:
        return Warmed(outcome="end")

    def read() -> FileChoice | None:
        runtime = state.settings_store.snapshot().runtime
        filaments = {f.id: f for f in orders.store.filaments()}
        return default_choice(listing, filaments, runtime.storefront_materials)

    choice = await asyncio.to_thread(read)
    planned = None if choice is None else await plan_file(state, listing, choice)
    if planned is None:
        return Warmed(outcome="settled")
    return await _warmed(orders, await planned_estimate(state, planned))


async def _warmed(orders: OrdersComponent, estimate: StoreEstimate) -> Warmed:
    """What the run does next, from the price read's answer."""
    if estimate.source != "pending":
        return Warmed(outcome="settled")
    # No key: its files are still being fetched. A key: its slice runs, or waits for room.
    key = estimate.price_key
    row = None if key is None else await asyncio.to_thread(orders.slice_prices.get, key)
    return Warmed(outcome="running" if row is not None and row.status == "running" else "waiting")


def prepare_activities(state: AppState, orders: Callable[[], OrdersComponent]) -> list[Any]:
    """The prepare run's activities, for the ``library`` worker."""

    @activity.defn(name=SUBMIT_ACTIVITY)
    async def prepare_listing_submit(ask: SubmitAsk) -> Submitted:
        return await submit(state, orders(), ask, activity.info().workflow_run_id or "")

    @activity.defn(name=SETTLED_ACTIVITY)
    async def prepare_listing_settled(jobs: list[OpenJob]) -> list[OpenJob]:
        return await still_open(state, jobs)

    @activity.defn(name=WARM_ACTIVITY)
    async def prepare_listing_warm(ask: SubmitAsk) -> Warmed:
        return await warm(state, orders(), ask)

    return [prepare_listing_submit, prepare_listing_settled, prepare_listing_warm]
