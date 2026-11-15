"""A visitor's renders (#5282, spec 2026-11-02 §5.4, plan Task 7).

`start_render` checks the visitor's values against the listing, never trusting the edge,
and renders through `RenderService` with ``reuse_finished``, so visitors who choose the
same values share one render. A render that looks shared (an open job of the same key, or
a finished one still held) is `RenderLimits.free`; any other is `RenderLimits.admit`,
counted against the visitor's and the client's limits. Both run inside `idempotent_write`
keyed by the edge's ``Idempotency-Key``, so a request the edge sends again after a lost
answer is counted once (plan point 21). That look takes no lock, so the submit's answer
decides at the end: a joined or reused job is not counted, and a run of its own is counted
under the same limits as `admit` (`RenderLimits.count`); over them, the run is withdrawn
and the visitor gets 429 ``render_limited``. A start
that fails before any job can exist (`never_started`), or that a full queue refuses,
drops the row and its recorded reply (`RenderLimits.undo`): a render that never started
costs nothing. A start that may have made a job keeps both, so the same key sent again
joins that job.

A visitor reads a render only when one of its own rows names it (`RenderLimits.owns`):
any other job id, a guessed one among them, is 404 ``render_not_found``. `store_render`
shows a render with a fixed text in place of its error, never the log.

`render_estimate` is the firm price of a done render (#6153): the price of a slice of its
variant through the back office's quote path (`slice_price.py`), each slot at the listing
filament of its colour in the material the visitor's colours resolved to (``material``,
kept on the visitor's row, `inputs.py` `resolve_choice`, #6258), else the one it chose
for its slot, else the first listing filament of that colour (a locked support colour).
A render with no firm price has none: the visitor never reads a volume estimate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from fastapi import status
from temporalio.client import WorkflowUpdateFailedError

from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.crud.idempotency import Replay, idempotent_write
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.estimate import StoreEstimate
from scadbuddy.orders.models import Filament, Reference
from scadbuddy.orders.reference import ResolvedReference, render_key_of
from scadbuddy.orders.storefront.inputs import (
    ColoursClashError,
    InputOutsideListingError,
    LockedColoursClashError,
    ResolvedChoice,
    check_one_material,
    customer_filaments,
    effective_materials,
    listing_reference,
    listing_values,
    resolve_choice,
    slot_filaments,
)
from scadbuddy.orders.storefront.limits import OwnedRender, RenderLimited, RenderLimits
from scadbuddy.orders.storefront.listings import problem, published_listing
from scadbuddy.orders.storefront.slice_price import (
    NO_PRICE,
    model_digest,
    pending,
    slice_estimate,
)
from scadbuddy.orders.storefront.visitors import VisitorStore
from scadbuddy.orders.storefront.wire import Listing, RenderBody, StoreRender
from scadbuddy.render.job_models import Job, JobNotFoundError, QueueFullError
from scadbuddy.render.jobs import SnapshotPendingError, SnapshotUnavailableError
from scadbuddy.render.schema import ParamValue
from scadbuddy.store.cache import materialize_result
from scadbuddy.store.content import StoreFullError
from scadbuddy.storefront.settings import STOREFRONT_PREFIX
from scadbuddy.storefront.wire import JOB_ID
from scadbuddy.workflows.commands import TemporalUnavailableError, TemporalUnreachableError

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

logger = logging.getLogger(__name__)

#: The ledger's name of the route (`idempotent_write`).
RENDER_ROUTE: Final = "POST /storefront/renders"
#: What a visitor reads of a render that failed or was cancelled: never its log.
RENDER_FAILED: Final = "This design could not be made with these choices. Try other values."
RENDER_CANCELLED: Final = "This render was stopped. Try again."
#: What a visitor whose render could not start waits before it asks again.
UNAVAILABLE_RETRY: Final = 5
#: The longest a status or estimate request waits on work behind it (#6120): a done
#: render's files being fetched. Past it the request answers "not ready yet" and the page
#: asks again, so no request is held near Cloudflare's 100 s. The fetch state below
#: (`_fetches`, `_ready`, `_failed`) is this process's: it assumes one API process, as
#: today; with more, a preview read may still fetch on its own.
READY_WAIT: Final = 3.0


def not_found() -> ApiError:
    """404 ``render_not_found``: an unknown job and another visitor's job alike."""
    return problem(status.HTTP_404_NOT_FOUND, "render_not_found", "There is no such render.")


def _unavailable(detail: str) -> ApiError:
    return problem(status.HTTP_422_UNPROCESSABLE_CONTENT, "estimate_unavailable", detail)


def _orders(state: AppState) -> OrdersComponent:
    orders: OrdersComponent = state.components.get(ORDERS)
    return orders


def _limits(state: AppState) -> RenderLimits:
    return RenderLimits(state.projection.pool)


def store_render(job: Job, *, ready: bool = True) -> StoreRender:
    """The visitor's view of ``job``. A done job's ``preview_url`` is there only once
    its files are ``ready`` in this process (`shown_render`)."""
    result = job.result
    error = {"failed": RENDER_FAILED, "cancelled": RENDER_CANCELLED}.get(job.state)
    stage = next((step.name for step in job.steps if step.state == "running"), None)
    return StoreRender(
        id=job.id,
        status=job.state,
        stage=stage if job.state == "running" else None,
        plates=list(result.plates) if result else [],
        bbox_mm=result.bbox_mm if result else None,
        colors=list(result.colors) if result else None,
        preview_url=(
            f"{STOREFRONT_PREFIX}/renders/{job.id}/preview.glb"
            if result is not None and job.state == "done" and ready
            else None
        ),
        error=error,
    )


def _filaments(orders: OrdersComponent) -> dict[str, Filament]:
    return {f.id: f for f in orders.store.filaments()}


#: Each done job's files being fetched into this process's cache, by job id: one fetch
#: per job, which every request for it shares and none cancels.
_fetches: dict[str, asyncio.Task[None]] = {}
#: The done jobs whose files this process fetched, newest last: their status reads skip
#: the fetch. Bounded; a job that falls out is only fetched (from the cache) again.
_ready: OrderedDict[str, None] = OrderedDict()
READY_KEPT: Final = 4096
#: Each job's failed fetches in a row, and when the first failed. Past `FETCH_TRIES`
#: its files are taken as lost for the status only: it names the preview, whose read then
#: says so at once (404), rather than the page polling a render that can never show. The
#: estimate never takes lost files as there (`files_ready`). A count older than
#: `FAILED_FOR` starts over, so an outage that ends is tried again; bounded as `_ready`.
_failed: OrderedDict[str, tuple[int, float]] = OrderedDict()
FETCH_TRIES: Final = 3
FAILED_FOR: Final = 600.0


def _fetched(job_id: str, task: asyncio.Task[None]) -> None:
    if _fetches.get(job_id) is task:
        del _fetches[job_id]
    if task.cancelled():
        return
    if task.exception() is not None:
        count, first = _failures(job_id)
        _failed[job_id] = (count + 1, first if count else time.monotonic())
        _failed.move_to_end(job_id)
        while len(_failed) > READY_KEPT:
            _failed.popitem(last=False)
        logger.warning("a render's files could not be fetched", exc_info=task.exception())
        return
    _failed.pop(job_id, None)
    _ready[job_id] = None
    _ready.move_to_end(job_id)
    while len(_ready) > READY_KEPT:
        _ready.popitem(last=False)


def _failures(job_id: str) -> tuple[int, float]:
    """The job's failed fetches in a row and when the first failed; none once they are
    older than `FAILED_FOR`."""
    count, first = _failed.get(job_id, (0, 0.0))
    if count and time.monotonic() - first > FAILED_FOR:
        _failed.pop(job_id, None)
        return 0, 0.0
    return count, first


def files_lost(job_id: str) -> bool:
    """Whether the job's fetch failed `FETCH_TRIES` times in a row, lately."""
    return _failures(job_id)[0] >= FETCH_TRIES


async def _fetch(state: AppState, job: Job) -> bool:
    """Starts the fetch of the done ``job``'s files (`materialize_result`) when none
    runs, shared by every request, and waits on it at most `READY_WAIT` seconds: whether
    it succeeded. It runs on when the wait ends, so the next request finds it done."""
    task = _fetches.get(job.id)
    if task is None:
        task = asyncio.create_task(materialize_result(state.store.blobs, job.result))
        _fetches[job.id] = task
        task.add_done_callback(lambda done: _fetched(job.id, done))
    done, _ = await asyncio.wait({task}, timeout=READY_WAIT)
    return task in done and not task.cancelled() and task.exception() is None


async def files_ready(state: AppState, job: Job) -> bool:
    """Whether the status may name the done ``job``'s preview: its files were fetched
    into this process's cache (a job fetched before answers at once), else `_fetch`.
    Only the preview leans on this: its read fetches again if the cache evicted them.
    The estimate always fetches (`render_estimate`): it hashes the 3MF."""
    return job.id in _ready or await _fetch(state, job)


async def shown_render(state: AppState, job: Job) -> StoreRender:
    """`store_render` of ``job``, its preview named only once its files are fetched
    (#6120): the preview request then streams at once, and the page polls the status
    until it is. A fetch that failed is tried again on the next poll; once it failed
    `FETCH_TRIES` times lately, the preview is named all the same, so its read says the
    files are gone rather than the page polling to its limit."""
    ready = (
        job.state != "done"
        or job.result is None
        or await files_ready(state, job)
        or files_lost(job.id)
    )
    return store_render(job, ready=ready)


def _limited(error: RenderLimited) -> ApiError:
    """429 ``render_limited``, with the limit and the seconds to wait."""
    return problem(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "render_limited",
        "You have made a lot of renders. Wait a moment, then try again.",
        headers={"Retry-After": str(error.retry_after)},
        limit=error.limit,
        retry_after=error.retry_after,
    )


async def _withdraw_limited(
    state: AppState, limits: RenderLimits, job: Job, row: int, key: str
) -> None:
    """A run of the visitor's own over its limits: its claim withdrawn (the last claim
    cancels the job), then its row and reply dropped. A withdrawal that fails keeps the
    row, counted and naming the job, so the run still counts against the limits."""
    try:
        await state.render.withdraw(job.id, request_id=f"{key}:limited")
    except Exception:
        logger.warning("a run over a visitor's limit could not be withdrawn", exc_info=True)
        await asyncio.to_thread(limits.set_counted, row, True)
        await asyncio.to_thread(limits.attach, row, job.id)
        return
    await asyncio.to_thread(limits.undo, row, key)


def _expired() -> ApiError:
    return problem(
        status.HTTP_410_GONE, "render_expired", "This preview has expired. Start it again."
    )


def _unavailable_render() -> ApiError:
    return problem(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "render_unavailable",
        "The shop could not start this render. Try again in a moment.",
        headers={"Retry-After": str(UNAVAILABLE_RETRY)},
    )


def _runtime(state: AppState) -> Settings:
    """The stored settings as they are now, which the limits and the estimate's timeout
    both read. Blocking: call it in a thread."""
    return state.settings_store.snapshot().runtime


def never_started(error: Exception) -> bool:
    """Whether a submit that raised ``error`` cannot have started a job: a snapshot not
    there or still storing, a full store, a run that answered it could not start
    (`api/jobs.py` ``may_have_started=False``), or Temporal's first connect failing
    (`api/jobs.py` `_temporal_problem`). Anything else may have started one."""
    if isinstance(
        error,
        SnapshotUnavailableError
        | SnapshotPendingError
        | StoreFullError
        | WorkflowUpdateFailedError
        | TemporalUnreachableError,
    ):
        return True
    return isinstance(error, TemporalUnavailableError) and isinstance(error.__cause__, RuntimeError)


def _shared(state: AppState, conn: Any, render_key: str) -> bool:
    """Whether a render of ``render_key`` is open now, or done with its result still
    held: the visitor then shares it, at no cost."""
    row = conn.execute(
        "SELECT 1 FROM render_jobs WHERE render_key = %s AND kind = 'render'"
        " AND state IN ('pending', 'running') LIMIT 1",
        (render_key,),
    ).fetchone()
    return row is not None or state.projection.reuse_done(render_key) is not None


#: The slot colours (``job.result.colors``) of the visitor's render of a render key, or
#: None when there is none to read.
RenderColours = Callable[[str], Awaitable[Sequence[str] | None]]


async def checked_choice(
    orders: OrdersComponent,
    body: RenderBody,
    shop_materials: Sequence[str],
    *,
    render_colours: RenderColours | None = None,
) -> tuple[Listing, ResolvedReference, ResolvedChoice]:
    """The published listing ``body`` names, the reference of ``body``'s values and
    colours on it, and what its colours resolve to (`inputs.py` `resolve_choice`, #6258):
    the one material, among the listing's materials (else ``shop_materials``, the
    ``storefront_materials`` setting), that the render, the estimate and the order line
    all print in. Each colour the visitor leaves alone is settled in that material, so
    the reference renders those values. With ``render_colours``, each slot
    ``body.colours`` lacks takes that material's filament of the slot's colour in that
    render (`slot_filaments`). Refusals: 404 ``listing_not_found``, 409
    ``listing_changed`` (the listing moved since the page read it), 422
    ``input_outside_listing`` (with ``name`` and ``reason``), 422
    ``listing_colours_clash`` (colours no one allowed material offers together, with
    ``parts``, `ColoursClashError`; only the visitor's colours and slots, never a locked
    one), 409 ``listing_unavailable`` (no allowed material is printable,
    `printable_materials`, `LockedColoursClashError`: a listing published before publish
    refused that, or broken since by the materials or the price book), 409
    ``render_pending`` (with ``render_colours``, colours given but no done render of the
    visitor's to take the slots from)."""
    listing = await asyncio.to_thread(published_listing, orders.listings, body.listing_id)
    if listing.updated_at != body.listing_updated_at:
        raise problem(
            status.HTTP_409_CONFLICT,
            "listing_changed",
            "This listing has changed. Load it again.",
        )
    filaments = await asyncio.to_thread(_filaments, orders)
    materials = effective_materials(listing, shop_materials)
    try:
        values = listing_values(listing, body.values, filaments, materials)
        choice = resolve_choice(
            listing, values, body.values.keys(), filaments, materials, slots=body.colours
        )
        resolved = listing_reference(listing, choice.values, body.colours, filaments=filaments)
        if render_colours is not None:
            colours = await render_colours(resolved.reference.render_key)
            given = [name for name in body.values if name in values and _is_colour(listing, name)]
            if not colours and given:
                # The slots are known only from a done render; without them the order
                # line would lose the visitor's colours (#6152, #6258).
                raise problem(
                    status.HTTP_409_CONFLICT,
                    "render_pending",
                    "Your preview is still being made. Add it once it shows.",
                )
            if colours:
                chosen = slot_filaments(body.colours, choice, colours)
                resolved = listing_reference(listing, choice.values, chosen, filaments=filaments)
        # A print never mixes materials (#6152, always on since #6258).
        check_one_material(resolved.colours, choice, filaments)
    except LockedColoursClashError:
        # No material can print it (published before publish checked, or broken since):
        # no visitor's colour can fix it, so the refusal names none (#6302 review).
        raise problem(
            status.HTTP_409_CONFLICT,
            "listing_unavailable",
            "This design can't be made right now. Please try again later.",
        ) from None
    except ColoursClashError as error:
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "listing_colours_clash",
            "Some of these colours can't go together: each print is made from one kind of"
            " plastic. Change one of them.",
            parts=error.parts,
        ) from None
    except InputOutsideListingError as error:
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "input_outside_listing",
            "This choice is not offered for this listing.",
            name=error.name,
            reason=error.reason,
        ) from None
    return listing, resolved, choice


def _is_colour(listing: Listing, name: str) -> bool:
    """Whether ``name`` is a ``// color`` parameter of the published ``listing``."""
    assert listing.snapshot is not None
    return any(p.name == name and p.type == "color" for p in listing.snapshot.schema_.parameters)


def _colour_values(listing: Listing, choice: ResolvedChoice) -> dict[str, ParamValue]:
    """The values the render takes of the colour parameters the listing offers, as
    `resolve_choice` settled them: what the page shows (#6258, #6302 review)."""
    return {
        name: value
        for name, value in choice.values.items()
        if name in listing.params and _is_colour(listing, name)
    }


async def start_render(
    state: AppState, visitor_id: str, client: str, key: str, body: RenderBody
) -> StoreRender:
    """The visitor's render of the listing at ``body``'s values. Refusals: 404
    ``listing_not_found``, 409 ``listing_changed``, 422 ``input_outside_listing`` (with
    ``name`` and ``reason``), 429 ``render_limited`` (with ``limit``), 429
    ``render_queue_full`` and 503 ``render_unavailable`` (any other failed start; one
    that `never_started` costs nothing), each with ``Retry-After``. The edge's client
    sends a 503 again with the same key for its budget, so a visitor sees the edge's own
    503 when the start keeps failing. A key seen before replays its row: the job the
    row names (410 ``render_expired`` when that job is gone; it is never submitted
    again), else the submit sent again, counted as a fresh one is when it runs on its own
    and the row is free. The answer carries the colour values the render takes
    (`StoreRender.values`, #6302 review)."""
    orders = _orders(state)
    runtime = await asyncio.to_thread(_runtime, state)
    listing, resolved, choice = await checked_choice(orders, body, runtime.storefront_materials)
    reference = resolved.reference
    assert listing.snapshot is not None
    visitor = await asyncio.to_thread(VisitorStore(state.projection.pool).ensure, visitor_id)
    limits = _limits(state)
    asked = {
        "visitor": visitor.id,
        "client": client,
        **body.model_dump(mode="json"),
    }

    fresh = False

    def write(conn: Any) -> Replay:
        nonlocal fresh
        fresh = True
        if _shared(state, conn, reference.render_key):
            row = limits.free(
                conn,
                visitor.id,
                client,
                listing_id=listing.id,
                render_key=reference.render_key,
                colours=body.colours,
                material=choice.material,
            )
            return Replay(200, {"row": row, "counted": False})
        row = limits.admit(
            conn,
            visitor.id,
            client,
            listing_id=listing.id,
            render_key=reference.render_key,
            settings=runtime,
            colours=body.colours,
            material=choice.material,
        )
        return Replay(200, {"row": row, "counted": True})

    def record() -> Replay:
        with state.projection.pool.connection() as conn:
            return idempotent_write(
                conn, "storefront", key, RENDER_ROUTE, asked, lambda: write(conn)
            )

    try:
        recorded = await asyncio.to_thread(record)
    except RenderLimited as error:
        raise _limited(error) from None
    row: int = recorded.body["row"]
    counted: bool = recorded.body["counted"]
    if not fresh:
        # A replayed key: a second submit cannot tell how the first went (a finished job
        # answers "reused", Temporal repeats a run's first answer), so a job the row names
        # is the answer. One that is gone is never submitted again: that would start a run
        # the row, maybe free, never counted. A new key starts a fresh, counted render.
        stood = await asyncio.to_thread(limits.state_of, row)
        if stood is None:
            raise _expired()
        named, counted = stood
        if named is not None:
            try:
                named_job = await asyncio.to_thread(state.render.store.read, named)
                shown = await shown_render(state, named_job)
                return shown.model_copy(update={"values": _colour_values(listing, choice)})
            except JobNotFoundError:
                raise _expired() from None
    params = reference.inputs.get("params", {})
    try:
        job, coalesced = await state.render.submit_answer(
            reference.slug,
            params if isinstance(params, dict) else {},
            model_version=reference.commit,
            inputs=reference.inputs,
            whole_inputs=listing.snapshot.pipeline,
            request_id=key,
            reuse_finished=True,
        )
    except QueueFullError as error:
        if not fresh:
            raise _unavailable_render() from None
        # The render never ran: its row and its reply go, so it counts against nothing.
        await asyncio.to_thread(limits.undo, row, key)
        raise problem(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "render_queue_full",
            "The shop is busy making other designs. Try again in a moment.",
            headers={"Retry-After": str(error.retry_after)},
            retry_after=error.retry_after,
        ) from None
    except Exception as error:
        logger.warning("a visitor's render was not started", exc_info=True)
        if fresh and never_started(error):
            # No job can exist: the visitor is not charged, and the same key sent
            # again (the edge's client retries a 5xx) admits a new row.
            await asyncio.to_thread(limits.undo, row, key)
        # Else the job may exist: the row and its reply stay, so the same key sent again
        # replays the row and joins the job, counted once.
        raise _unavailable_render() from None
    # `_shared` read the jobs before the submit, with no lock. The submit decides. A
    # replay (its first answer lost before `attach`) never uncounts its row, but a free
    # row whose submit runs on its own counts, as a fresh one does.
    if fresh and counted and coalesced:
        # A job opened in between: joined or reused, it costs nothing.
        await asyncio.to_thread(limits.set_counted, row, False)
    elif not counted and not coalesced:
        # The open job settled in between: the run is the visitor's own, and passes the
        # same limits as any other.
        try:
            await asyncio.to_thread(limits.count, row, visitor.id, client, settings=runtime)
        except RenderLimited as error:
            await _withdraw_limited(state, limits, job, row, key)
            raise _limited(error) from None
    await asyncio.to_thread(limits.attach, row, job.id)
    shown = await shown_render(state, job)
    return shown.model_copy(update={"values": _colour_values(listing, choice)})


async def owned_render(state: AppState, visitor_id: str, job_id: str) -> OwnedRender:
    """The visitor's row of ``job_id``; 404 ``render_not_found`` unless the visitor asked
    for that job (or for a job id that is not one)."""
    if JOB_ID.fullmatch(job_id) is None:
        raise not_found()

    def find() -> OwnedRender | None:
        visitor = VisitorStore(state.projection.pool).find(visitor_id)
        return None if visitor is None else _limits(state).owned(visitor.id, job_id)

    owned = await asyncio.to_thread(find)
    if owned is None:
        raise not_found()
    return owned


async def owned_job(state: AppState, visitor_id: str, job_id: str) -> tuple[OwnedRender, Job]:
    """The visitor's row of ``job_id`` and the job; 404 ``render_not_found`` as
    `owned_render` says, and for a job the prune took."""
    owned = await owned_render(state, visitor_id, job_id)
    try:
        job = await asyncio.to_thread(state.render.store.read, job_id)
    except JobNotFoundError:
        raise not_found() from None
    return owned, job


def _filament_for(
    listing: Listing,
    filaments: Mapping[str, Filament],
    colours: Mapping[int, str],
    render_colours: list[str],
    material: str | None = None,
) -> dict[str, Filament]:
    """Each colour of the render, casefolded, to its filament: the first customer
    filament of that colour in ``material``, the one the visitor's colours resolved to
    (#6258), else the one it chose for its slot, else the first listing filament of that
    colour. The last takes a support filament too: a locked support colour is a slot of
    the render, and the estimate prices it (#6152)."""
    listed = [filaments[f] for f in listing.colours if f in filaments]
    chosen: dict[str, Filament] = {}
    if material is not None:
        for picked in customer_filaments(listing, filaments, [material]):
            chosen.setdefault(picked.colour.casefold(), picked)
    for slot, filament_id in sorted(colours.items()):
        if 0 <= slot < len(render_colours) and filament_id in listing.colours:
            record = filaments.get(filament_id)
            if record is not None:
                chosen.setdefault(render_colours[slot].casefold(), record)
    for record in listed:
        chosen.setdefault(record.colour.casefold(), record)
    return chosen


async def render_estimate(state: AppState, visitor_id: str, job_id: str) -> StoreEstimate:
    """The firm price of the visitor's done render (#6153, `slice_price.py`
    `slice_estimate`). 404 ``render_not_found`` as `owned_job` says; 422
    ``estimate_unavailable`` for a job not done. ``pending`` while its files are fetched
    (#6120) or its slice runs; ``none`` when the listing is gone, its files are lost, a
    colour of the render has no listing filament, or the render is of another design
    than the listing's now (its slug or commit moved since, #6370 review): the price
    is never of a design the visitor does not see."""
    owned, job = await owned_job(state, visitor_id, job_id)
    if job.state != "done" or job.result is None:
        raise _unavailable("The price is ready once the render is done.")
    orders = _orders(state)
    listing = await asyncio.to_thread(orders.listings.get, owned.listing_id)
    if listing is None or listing.snapshot is None:
        return NO_PRICE
    if job.slug != listing.slug or job.model_version != listing.commit:
        # The shop moved the listing since this render: the page renders again.
        return NO_PRICE
    if not await _fetch(state, job):
        return NO_PRICE if files_lost(job.id) else pending()
    filaments = await asyncio.to_thread(_filaments, orders)
    by_colour = _filament_for(
        listing, filaments, owned.colours, list(job.result.colors), owned.material
    )
    slots = [by_colour.get(colour.casefold()) for colour in job.result.colors]
    digest = await asyncio.to_thread(model_digest, job.id, state.paths.root / job.result.model_3mf)
    snapshot = listing.snapshot
    reference = Reference(
        slug=listing.slug,
        commit=listing.commit,
        inputs=job.inputs,
        libraries=list(snapshot.libraries),
        template_name=snapshot.template_name,
        render_key=render_key_of(
            listing.slug, job.inputs, listing.commit, pipeline=snapshot.pipeline
        ),
    )
    return await slice_estimate(
        state,
        reference=reference,
        model_sha256=digest,
        filaments=None if None in slots else [slot for slot in slots if slot is not None],
    )
