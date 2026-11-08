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

`render_estimate` is the rough price of a done render (plan point 4): its part volumes,
analysed once per job into ``storefront_job_parts`` (plan point 5), each at the listing
filament the visitor chose for its colour. The analyses run on `ANALYSIS_WORKERS` threads
shared by the process, at most `ANALYSIS_QUEUE` held at once, one per job (`_flights`):
every request for a job awaits the same work, which records its own outcome, so a request
that leaves starts nothing again. A 3MF that cannot be analysed, or that runs past
``render_timeout`` from its start, is recorded as such and never analysed again; one that
waits in the queue past it is withdrawn ("not ready yet") and analysed on a later request.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Mapping
from concurrent.futures import Future, InvalidStateError, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from fastapi import status
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter
from temporalio.client import WorkflowUpdateFailedError

from scadbuddy.core.problems import ApiError
from scadbuddy.core.settings import Settings
from scadbuddy.crud.idempotency import Replay, idempotent_write
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.estimate import EstimatedPart, StoreEstimate, rough_estimate
from scadbuddy.orders.estimate import part_volumes as part_volumes  # tests count its calls
from scadbuddy.orders.models import Filament
from scadbuddy.orders.storefront.inputs import (
    InputOutsideListingError,
    listing_reference,
    listing_values,
)
from scadbuddy.orders.storefront.limits import OwnedRender, RenderLimited, RenderLimits
from scadbuddy.orders.storefront.listings import problem, published_listing
from scadbuddy.orders.storefront.visitors import VisitorStore
from scadbuddy.orders.storefront.wire import Listing, RenderBody, StoreRender
from scadbuddy.render.job_models import Job, JobNotFoundError, QueueFullError
from scadbuddy.render.jobs import SnapshotPendingError, SnapshotUnavailableError
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
_PARTS: Final = TypeAdapter(list[EstimatedPart])
#: What a visitor whose render could not start waits before it asks again.
UNAVAILABLE_RETRY: Final = 5


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


def store_render(job: Job) -> StoreRender:
    """The visitor's view of ``job``."""
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
            if result is not None and job.state == "done"
            else None
        ),
        error=error,
    )


def _filaments(orders: OrdersComponent) -> dict[str, Filament]:
    return {f.id: f for f in orders.store.filaments()}


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


async def start_render(
    state: AppState, visitor_id: str, client: str, key: str, body: RenderBody
) -> StoreRender:
    """The visitor's render of the listing at ``body``'s values. Refusals: 404
    ``listing_not_found``, 409 ``listing_changed``, 422 ``input_outside_listing`` (with
    ``name`` and ``reason``), 429 ``render_limited`` (with ``limit``), 429
    ``render_queue_full`` and 503 ``render_unavailable`` (any other failed start; one
    that `never_started` costs nothing), each with ``Retry-After``. The edge's client
    sends a 503 again with the same key for its budget, so a visitor sees the edge's own
    503 when the start keeps failing."""
    orders = _orders(state)
    listing = await asyncio.to_thread(published_listing, orders.listings, body.listing_id)
    if listing.updated_at != body.listing_updated_at:
        raise problem(
            status.HTTP_409_CONFLICT,
            "listing_changed",
            "This listing has changed. Load it again.",
        )
    filaments = await asyncio.to_thread(_filaments, orders)
    try:
        values = listing_values(listing, body.values, filaments)
        resolved = listing_reference(listing, values, body.colours, filaments=filaments)
    except InputOutsideListingError as error:
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "input_outside_listing",
            "This choice is not offered for this listing.",
            name=error.name,
            reason=error.reason,
        ) from None
    reference = resolved.reference
    assert listing.snapshot is not None
    visitor = await asyncio.to_thread(VisitorStore(state.projection.pool).ensure, visitor_id)
    runtime = await asyncio.to_thread(_runtime, state)
    limits = _limits(state)
    asked = {
        "visitor": visitor.id,
        "client": client,
        **body.model_dump(mode="json"),
    }

    def write(conn: Any) -> Replay:
        if _shared(state, conn, reference.render_key):
            row = limits.free(
                conn,
                visitor.id,
                client,
                listing_id=listing.id,
                render_key=reference.render_key,
                colours=body.colours,
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
        if never_started(error):
            # No job can exist: the visitor is not charged, and the same key sent
            # again (the edge's client retries a 5xx) admits a new row.
            await asyncio.to_thread(limits.undo, row, key)
        # Else the job may exist: the row and its reply stay, so the same key sent again
        # replays the row and joins the job, counted once.
        raise problem(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "render_unavailable",
            "The shop could not start this render. Try again in a moment.",
            headers={"Retry-After": str(UNAVAILABLE_RETRY)},
        ) from None
    # `_shared` read the jobs before the submit, with no lock. The submit decides.
    if counted and coalesced:
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
    return store_render(job)


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


@dataclass(frozen=True)
class _Analysis:
    """A job's analysis: its parts, or why there are none. ``error`` is ``unreadable``
    or ``timeout`` (both recorded, never tried again), or ``busy`` (not recorded: the
    queue was full, or the analysis did not start within the render timeout)."""

    parts: list[EstimatedPart] | None = None
    error: str | None = None


BUSY: Final = _Analysis(error="busy")
#: The most analyses one process runs at once: every estimate shares these threads.
ANALYSIS_WORKERS: Final = 2
#: The most analyses one process holds, queued or running; past it, "not ready yet".
ANALYSIS_QUEUE = 8
_analyses: ThreadPoolExecutor | None = None
#: Each job's analysis while its work is queued or running: its outcome, which every
#: request for the job awaits. The work owns it, not a request (`_begin`).
_flights: dict[str, Future[_Analysis]] = {}
_held = 0
_lock = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    global _analyses
    with _lock:
        if _analyses is None:
            _analyses = ThreadPoolExecutor(ANALYSIS_WORKERS, thread_name_prefix="estimate")
        return _analyses


def _recorded(state: AppState, job_id: str) -> _Analysis | None:
    with state.projection.pool.connection() as conn:
        row = conn.execute(
            "SELECT parts, error FROM storefront_job_parts WHERE job_id = %s", (job_id,)
        ).fetchone()
    if row is None:
        return None
    if row["error"] is not None:
        return _Analysis(error=row["error"])
    return _Analysis(parts=_PARTS.validate_python(row["parts"]))


def _record(state: AppState, job_id: str, analysis: _Analysis) -> _Analysis:
    """``analysis`` written once for the job; the record that stands."""
    parts = None
    if analysis.parts is not None:
        parts = Jsonb(_PARTS.dump_python(analysis.parts, mode="json"))
    with state.projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO storefront_job_parts (job_id, parts, error) VALUES (%s, %s, %s)"
            " ON CONFLICT (job_id) DO NOTHING",
            (job_id, parts, analysis.error),
        )
    stood = _recorded(state, job_id)
    assert stood is not None
    return stood


def _settle(outcome: Future[_Analysis], analysis: _Analysis) -> None:
    """The flight's outcome, once: a later one (the work after its timeout) is dropped."""
    with suppress(InvalidStateError):
        outcome.set_result(analysis)


def _begin(state: AppState, job: Job, timeout: float) -> Future[_Analysis]:
    """The job's analysis in flight: the one already there, else its record, else a new
    one on the shared threads, else `BUSY` when `ANALYSIS_QUEUE` analyses are held.
    Blocking: call it in a thread. The work records its
    own outcome and ends its flight, whatever the requests do. One not started within
    ``timeout`` is withdrawn (`BUSY`, not recorded); one that runs past ``timeout`` from
    its start is recorded ``timeout``."""
    global _held
    result = job.result
    assert result is not None
    with _lock:
        flying = _flights.get(job.id)
        if flying is not None:
            return flying
        # A flight that ended between the caller's look and this one recorded first.
        stood = _recorded(state, job.id)
        if stood is not None or _held >= ANALYSIS_QUEUE:
            answered: Future[_Analysis] = Future()
            answered.set_result(stood or BUSY)
            return answered
        outcome: Future[_Analysis] = Future()
        _flights[job.id] = outcome
        _held += 1
    model = state.paths.root / result.model_3mf
    plates = max(len(result.plates), 1)

    def timed_out() -> None:
        try:
            _settle(outcome, _record(state, job.id, _Analysis(error="timeout")))
        except Exception:
            logger.warning("a render's analysis timeout could not be recorded", exc_info=True)
            _settle(outcome, BUSY)
        logger.warning("a render's 3MF took past the render timeout to analyse")

    def work() -> list[EstimatedPart]:
        withdraw.cancel()
        watchdog = threading.Timer(timeout, timed_out)
        watchdog.daemon = True
        watchdog.start()
        try:
            return part_volumes(model, plates, result.warnings)
        finally:
            watchdog.cancel()

    def withdrawn() -> None:
        running.cancel()

    def settled(done: Future[list[EstimatedPart]]) -> None:
        global _held
        analysis = BUSY
        try:
            if not done.cancelled():
                error = done.exception()
                if error is None:
                    analysis = _record(state, job.id, _Analysis(parts=done.result()))
                else:
                    logger.warning("a render's 3MF could not be analysed", exc_info=error)
                    analysis = _record(state, job.id, _Analysis(error="unreadable"))
        except Exception:
            logger.warning("a render's analysis could not be recorded", exc_info=True)
        finally:
            with _lock:
                if _flights.get(job.id) is outcome:
                    del _flights[job.id]
                _held -= 1
            _settle(outcome, analysis)

    withdraw = threading.Timer(timeout, withdrawn)
    withdraw.daemon = True
    try:
        running = _executor().submit(work)
    except BaseException:
        # No work, so no `settled`: take the flight and its place back.
        with _lock:
            if _flights.get(job.id) is outcome:
                del _flights[job.id]
            _held -= 1
        _settle(outcome, BUSY)
        raise
    running.add_done_callback(settled)
    try:
        withdraw.start()
    except BaseException:
        # `settled` ends the flight: at once when the work is cancelled, else when it ends.
        running.cancel()
        raise
    return outcome


async def _parts(state: AppState, job: Job) -> list[EstimatedPart]:
    """The job's part volumes: recorded, else analysed once per job (`_begin`)."""
    analysis = await asyncio.to_thread(_recorded, state, job.id)
    if analysis is None:
        with _lock:
            outcome = _flights.get(job.id)
        if outcome is None:
            assert job.result is not None
            await materialize_result(state.store.blobs, job.result)
            runtime = await asyncio.to_thread(_runtime, state)
            outcome = await asyncio.to_thread(_begin, state, job, runtime.render_timeout)
        # Shielded: a request that leaves never cancels the flight others await.
        analysis = await asyncio.shield(asyncio.wrap_future(outcome))
    if analysis.parts is not None:
        return analysis.parts
    if analysis.error == "busy":
        raise _unavailable("The price is not ready yet. Try again in a moment.")
    raise _unavailable(
        "We cannot price this design here. Add it to your order and we will price it."
    )


def _filament_for(
    listing: Listing,
    filaments: Mapping[str, Filament],
    colours: Mapping[int, str],
    render_colours: list[str],
) -> dict[str, Filament]:
    """Each colour of the render, casefolded, to its filament: the listing filament the
    visitor chose for its slot, else the first listing filament of that colour."""
    offered = [filaments[f] for f in listing.colours if f in filaments]
    chosen: dict[str, Filament] = {}
    for slot, filament_id in sorted(colours.items()):
        if 0 <= slot < len(render_colours) and filament_id in listing.colours:
            record = filaments.get(filament_id)
            if record is not None:
                chosen.setdefault(render_colours[slot].casefold(), record)
    for record in offered:
        chosen.setdefault(record.colour.casefold(), record)
    return chosen


async def render_estimate(state: AppState, visitor_id: str, job_id: str) -> StoreEstimate:
    """The rough price of the visitor's done render. 404 ``render_not_found`` as
    `owned_job` says; 422 ``estimate_unavailable`` for a job not done, a 3MF that cannot
    be analysed (or not within ``render_timeout``), a colour no listing filament has,
    or a filament with no density."""
    owned, job = await owned_job(state, visitor_id, job_id)
    if job.state != "done" or job.result is None:
        raise _unavailable("The price is ready once the render is done.")
    parts = await _parts(state, job)
    orders = _orders(state)
    listing = await asyncio.to_thread(orders.listings.get, owned.listing_id)
    if listing is None:
        raise _unavailable("This listing is no longer offered.")
    filaments = await asyncio.to_thread(_filaments, orders)
    currency, policy, factor = await asyncio.to_thread(orders.price_book.policy)
    by_colour = _filament_for(listing, filaments, owned.colours, list(job.result.colors))
    try:
        return rough_estimate(
            parts,
            lambda colour: by_colour[colour.casefold()],
            policy,
            currency=currency,
            factor=factor,
        )
    except (KeyError, ValueError):
        logger.warning("a render could not be priced", exc_info=True)
        raise _unavailable(
            "We cannot price this design here. Add it to your order and we will price it."
        ) from None
