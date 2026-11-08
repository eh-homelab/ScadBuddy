"""A visitor's renders (#5282, spec 2026-11-02 §5.4, plan Task 7).

`start_render` checks the visitor's values against the listing, never trusting the edge,
and renders through `RenderService` with ``reuse_finished``, so visitors who choose the
same values share one render. A render that joins an open job of the same key, or reuses
a finished one, is `RenderLimits.free`; any other is `RenderLimits.admit`, counted against
the visitor's and the client's limits. Both run inside `idempotent_write` keyed by the
edge's ``Idempotency-Key``, so a request the edge sends again after a lost answer is
counted once (plan point 21).

A visitor reads a render only when one of its own rows names it (`RenderLimits.owns`):
any other job id, a guessed one among them, is 404 ``render_not_found``. `store_render`
shows a render with a fixed text in place of its error, never the log.

`render_estimate` is the rough price of a done render (plan point 4): its part volumes,
analysed once per job into ``storefront_job_parts`` (plan point 5), each at the listing
filament the visitor chose for its colour.
"""

from __future__ import annotations

import asyncio
import logging
import zipfile
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from fastapi import status
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from scadbuddy.api.jobs import submit_problems
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import Replay, idempotent_write
from scadbuddy.orders.component import ORDERS, OrdersComponent
from scadbuddy.orders.estimate import EstimatedPart, StoreEstimate, part_volumes, rough_estimate
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
from scadbuddy.store.cache import materialize_result
from scadbuddy.storefront.settings import STOREFRONT_PREFIX
from scadbuddy.storefront.wire import JOB_ID

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

logger = logging.getLogger(__name__)

#: The ledger's name of the route (`idempotent_write`).
RENDER_ROUTE: Final = "POST /storefront/renders"
#: What a visitor reads of a render that failed or was cancelled: never its log.
RENDER_FAILED: Final = "This design could not be made with these choices. Try other values."
RENDER_CANCELLED: Final = "This render was stopped. Try again."
_PARTS: Final = TypeAdapter(list[EstimatedPart])


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
    ``name`` and ``reason``), 429 ``render_limited`` (with ``limit``) and 429
    ``render_queue_full``, each with ``Retry-After``."""
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
    runtime = await asyncio.to_thread(lambda: state.settings_store.snapshot().runtime)
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
        raise problem(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "render_limited",
            "You have made a lot of renders. Wait a moment, then try again.",
            headers={"Retry-After": str(error.retry_after)},
            limit=error.limit,
            retry_after=error.retry_after,
        ) from None
    row: int = recorded.body["row"]
    params = reference.inputs.get("params", {})
    # A refusal other than a full queue is the operator's or Temporal's (a 5xx the edge
    # sends again with the same key, or a missing snapshot), not the visitor's.
    with submit_problems():
        try:
            job = await state.render.submit(
                reference.slug,
                params if isinstance(params, dict) else {},
                model_version=reference.commit,
                inputs=reference.inputs,
                whole_inputs=listing.snapshot.pipeline,
                request_id=key,
                reuse_finished=True,
            )
        except QueueFullError as error:
            # The render never ran: it counts against nothing.
            await asyncio.to_thread(limits.uncount, row)
            raise problem(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "render_queue_full",
                "The shop is busy making other designs. Try again in a moment.",
                headers={"Retry-After": str(error.retry_after)},
                retry_after=error.retry_after,
            ) from None
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


def _cached_parts(state: AppState, job_id: str) -> list[EstimatedPart] | None:
    with state.projection.pool.connection() as conn:
        row = conn.execute(
            "SELECT parts FROM storefront_job_parts WHERE job_id = %s", (job_id,)
        ).fetchone()
    return None if row is None else _PARTS.validate_python(row["parts"])


def _store_parts(state: AppState, job_id: str, parts: list[EstimatedPart]) -> None:
    with state.projection.pool.connection() as conn:
        conn.execute(
            "INSERT INTO storefront_job_parts (job_id, parts) VALUES (%s, %s)"
            " ON CONFLICT (job_id) DO NOTHING",
            (job_id, Jsonb(_PARTS.dump_python(parts, mode="json"))),
        )


async def _parts(state: AppState, job: Job) -> list[EstimatedPart]:
    """The job's part volumes: stored, else analysed once under ``render_timeout``."""
    cached = await asyncio.to_thread(_cached_parts, state, job.id)
    if cached is not None:
        return cached
    result = job.result
    assert result is not None
    try:
        await materialize_result(state.store.blobs, result)
        parts = await asyncio.wait_for(
            asyncio.to_thread(
                part_volumes,
                state.paths.root / result.model_3mf,
                max(len(result.plates), 1),
                result.warnings,
            ),
            timeout=state.settings.render_timeout,
        )
    except (ValueError, KeyError, OSError, zipfile.BadZipFile, TimeoutError):
        logger.warning("a render's 3MF could not be analysed", exc_info=True)
        raise _unavailable(
            "We cannot price this design here. Add it to your order and we will price it."
        ) from None
    await asyncio.to_thread(_store_parts, state, job.id, parts)
    return parts


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
        if slot < len(render_colours) and filament_id in listing.colours:
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
