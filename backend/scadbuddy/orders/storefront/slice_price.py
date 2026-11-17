"""A storefront variant's firm price (#6153, spec 2026-11-13 §5.4, §5.6), from a slice
through the back office's quote path.

One pricing path (owner, 2026-11-14): the price is ``orders/quote.py``
:func:`~scadbuddy.orders.quote.slice_and_price`, which a back-office quote runs too. The
variant is rendered to an output (``orders/rendering.py`` ``output_for_reference``),
sliced by the print dialog's background slice (``orders/operations.py``
``PreviewSlicer``, the native slicer or Bambuddy's, as a quote routes it), and priced at
the price book's policy and the machine rate of the profile's printer. The template gives
the support and process settings, as for any output. The shop slice profile
(``slice_profile.py``) gives only the printer and a spool per material, and the print
dialog's remembered choices of the model give the rest (:func:`storefront_choices`).

One price per variant (:func:`price_key`): the render's 3MF content, the price-book
filament per slot, the profile, the choices, and the price book and the machine rate as
they stand. A change to any of them is another key, so an old price is never read again.
A row is kept in ``storefront_slice_prices`` (:class:`SlicePrices`) by its key and holds no
job id and no bytes, so it outlives the render prune.

The slice runs as the operation ``storefront_slice_price`` on the ``library`` queue, as a
back-office quote does (:func:`price_variant`). A visitor's estimate read starts it, only
for the render on screen. At most ``storefront_slices_concurrent`` run at once
(:meth:`SlicePrices.claim`); the rest wait, and the page reads again. A slice that fails,
or a profile spool that the printer no longer offers (a loaded tray's negative id moves
when a spool is swapped), gives no price: the shop prices that line by hand.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.bambuddy.extruders import default_nozzles, one_flow
from scadbuddy.bambuddy.resolver import DEFAULT_BED, PrintChoices
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import STOREFRONT, storefront_output_id
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.estimate import StoreEstimate
from scadbuddy.orders.lifecycle import QUOTE_TIMEOUT
from scadbuddy.orders.models import (
    Filament,
    MachineRateBreakdown,
    PricingPolicy,
    QuoteLine,
    Reference,
)
from scadbuddy.orders.pricing import Plate
from scadbuddy.orders.quote import (
    FOLLOW_EVERY,
    PolicySource,
    PriceSlots,
    QuoteError,
    SlicedPrice,
    SliceQuoteRequest,
    Slicer,
    slice_and_price,
    sliced_plate_problem,
    sliced_slots,
)
from scadbuddy.orders.rendering import (
    OutputsRead,
    RenderedOutput,
    Renders,
    output_for_reference,
)
from scadbuddy.orders.storefront.slice_profile import (
    ProfileMissingMaterialError,
    StorefrontSliceProfile,
    profile_digest,
    slice_request,
)

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState
    from scadbuddy.orders.component import OrdersComponent

logger = logging.getLogger(__name__)

#: The operation kind a storefront slice runs as (``orders/operations.py``).
SLICE_PRICE_KIND: Final = "storefront_slice_price"
#: The attempts of one slice's operation, as a back-office quote's.
RUN_ATTEMPTS: Final = 3
#: A ``running`` row older than this is lost (its operation ended and wrote nothing):
#: it no longer counts against the capacity, and a read starts its slice again.
LOST_AFTER: Final = QUOTE_TIMEOUT * RUN_ATTEMPTS + timedelta(minutes=5)
#: A failed price is tried again by a read this long after it failed: a slicer or a
#: printer away for a while must not hand-price a variant for good.
RETRY_FAILED_AFTER: Final = timedelta(hours=1)
#: What the page waits before it reads a price that is being worked out.
PENDING_RETRY: Final = 2
#: Why a variant has no price, as its row records it.
PROFILE_STALE: Final = "the shop slice profile names a spool the printer no longer offers"
FILAMENT_GONE: Final = "a slot's filament is no longer in the price book"
GEOMETRY_DIFFERS: Final = (
    "the variant rendered to another 3MF than the one its key names, so its price would"
    " not be for the design the visitor sees"
)
#: Every digest of a price key changes when this does.
KEY_VERSION: Final = 1
#: How long the live machine rate a key holds is read again only after: a page
#: reads its price every `PENDING_RETRY` seconds while the slice runs, and each
#: live read may ask Bambuddy for the printer's tasks.
RATE_TTL: Final = 60.0
#: The advisory lock every claim takes, so the capacity is counted once at a time.
_CLAIM_LOCK: Final = "storefront_slice_prices"


Status = Literal["running", "done", "failed"]


class SliceUnavailableError(Exception):
    """A passing failure of a variant's slice (a 5xx, 408 or 429): the operation
    tries again, and the row stays ``running`` (#6370 review)."""


def _passing(error: ApiError) -> bool:
    """Whether ``error`` may pass: a 5xx, a 408 or a 429. Any other 4xx (the 0 g
    ``sliced_plate``, a refused slice) comes back every time, so it is recorded."""
    return error.status >= 500 or error.status in (408, 429)


class PriceRow(BaseModel):
    """One ``storefront_slice_prices`` row."""

    price_key: str
    model_sha256: str
    status: Status
    attempt: int
    #: This claim's own key, which keys its operation: never reused (#6370 review).
    claim_key: str | None = None
    operation_key: str | None = None
    currency: str | None = None
    unit_price_minor: int | None = None
    total_minor: int | None = None
    unit_cost_minor: int | None = None
    lines: list[QuoteLine] | None = None
    print_seconds: int | None = None
    #: Grams per slot (from 0), as decimal strings.
    grams: dict[str, str] | None = None
    #: Each plate the slice gave (#6153 PR B); None on a row written before.
    plates: list[Plate] | None = None
    printer: str | None = None
    failure: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


@dataclass(frozen=True)
class Claim:
    """What :meth:`SlicePrices.claim` found: the row, and whether this claim made it
    ``running`` (the caller then starts its slice). With no row and not claimed, the
    capacity is full. ``previous`` is the row as it was before this claim, which a
    start that fails puts back (:meth:`SlicePrices.release`)."""

    row: PriceRow | None
    claimed: bool
    previous: PriceRow | None = None


_COLUMNS: Final = (
    "price_key, model_sha256, status, attempt, claim_key, operation_key, currency,"
    " unit_price_minor,"
    " total_minor, unit_cost_minor, lines, print_seconds, grams, plates, printer, failure,"
    " started_at, finished_at"
)


def _row(found: dict[str, Any] | None) -> PriceRow | None:
    return None if found is None else PriceRow.model_validate(found)


class SlicePrices:
    """The ``storefront_slice_prices`` rows (#6153). The CRUD process only (#5295)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def get(self, price_key: str) -> PriceRow | None:
        with self._pool.connection() as conn:
            found = conn.execute(
                f"SELECT {_COLUMNS} FROM storefront_slice_prices WHERE price_key = %s",
                (price_key,),
            ).fetchone()
        return _row(found)

    def running(self) -> list[PriceRow]:
        with self._pool.connection() as conn:
            found = conn.execute(
                f"SELECT {_COLUMNS} FROM storefront_slice_prices WHERE status = 'running'"
            ).fetchall()
        return [PriceRow.model_validate(row) for row in found]

    def claim(self, price_key: str, model_sha256: str, *, limit: int) -> Claim:
        """The key's row, made ``running`` by this claim when there is none, it was lost
        (`LOST_AFTER`), or it failed `RETRY_FAILED_AFTER` ago, and fewer than ``limit``
        other rows run. One transaction under one advisory lock, so two reads of one
        variant start one slice and the count is never passed."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (_CLAIM_LOCK,))
            now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            row = _row(
                conn.execute(
                    f"SELECT {_COLUMNS} FROM storefront_slice_prices WHERE price_key = %s",
                    (price_key,),
                ).fetchone()
            )
            if row is not None and _stands(row, now):
                return Claim(row, claimed=False)
            running = conn.execute(
                "SELECT count(*) AS n FROM storefront_slice_prices WHERE status = 'running'"
                " AND started_at > %s AND price_key <> %s",
                (now - LOST_AFTER, price_key),
            ).fetchone()["n"]
            if running >= limit:
                # No room: the row (if any) does not stand, so the page waits.
                return Claim(None, claimed=False)
            made = conn.execute(
                "INSERT INTO storefront_slice_prices (price_key, model_sha256, status,"
                " claim_key) VALUES (%s, %s, 'running', %s)"
                " ON CONFLICT (price_key) DO UPDATE SET status = 'running',"
                " attempt = storefront_slice_prices.attempt + 1,"
                " claim_key = EXCLUDED.claim_key, operation_key = NULL,"
                " failure = NULL, started_at = clock_timestamp(), finished_at = NULL"
                f" RETURNING {_COLUMNS}",
                (price_key, model_sha256, uuid.uuid4().hex),
            ).fetchone()
        claimed = _row(made)
        assert claimed is not None
        return Claim(claimed, claimed=True, previous=row)

    def set_operation(self, price_key: str, claim_key: str, operation_key: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE storefront_slice_prices SET operation_key = %s"
                " WHERE price_key = %s AND claim_key = %s AND status = 'running'",
                (operation_key, price_key, claim_key),
            )

    def release(self, price_key: str, claim_key: str, previous: PriceRow | None) -> None:
        """A claim whose slice never started: the row as it was before the claim, or
        no row when the claim made it, so the next read claims it again. The next
        claim makes a new ``claim_key``, so it never meets this claim's operation
        (#6370 review)."""
        with self._pool.connection() as conn:
            if previous is None:
                conn.execute(
                    "DELETE FROM storefront_slice_prices"
                    " WHERE price_key = %s AND claim_key = %s AND status = 'running'",
                    (price_key, claim_key),
                )
                return
            conn.execute(
                "UPDATE storefront_slice_prices SET status = %s, claim_key = %s,"
                " operation_key = %s, failure = %s, started_at = %s, finished_at = %s"
                " WHERE price_key = %s AND claim_key = %s AND status = 'running'",
                (
                    previous.status,
                    previous.claim_key,
                    previous.operation_key,
                    previous.failure,
                    previous.started_at,
                    previous.finished_at,
                    price_key,
                    claim_key,
                ),
            )

    def lost(self, price_key: str, claim_key: str, failure: str) -> None:
        """A ``running`` row whose operation ended and wrote nothing: failed, unless a
        later claim or the run itself has written it since."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE storefront_slice_prices SET status = 'failed', failure = %s,"
                " finished_at = clock_timestamp()"
                " WHERE price_key = %s AND claim_key = %s AND status = 'running'",
                (failure[:1000], price_key, claim_key),
            )

    def finish(
        self, price_key: str, model_sha256: str, currency: str, sliced: SlicedPrice
    ) -> PriceRow:
        """The price written once the slice is done."""
        grams: dict[int, Decimal] = {}
        for plate in sliced.plates:
            for used in plate.filaments:
                grams[used.slot] = grams.get(used.slot, Decimal(0)) + used.used_g
        priced = sliced.priced
        with self._pool.connection() as conn:
            found = conn.execute(
                "INSERT INTO storefront_slice_prices (price_key, model_sha256, status,"
                " currency, unit_price_minor, total_minor, unit_cost_minor, lines,"
                " print_seconds, grams, plates, printer, finished_at)"
                " VALUES (%s, %s, 'done', %s, %s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())"
                " ON CONFLICT (price_key) DO UPDATE SET status = 'done',"
                " currency = EXCLUDED.currency, unit_price_minor = EXCLUDED.unit_price_minor,"
                " total_minor = EXCLUDED.total_minor,"
                " unit_cost_minor = EXCLUDED.unit_cost_minor, lines = EXCLUDED.lines,"
                " print_seconds = EXCLUDED.print_seconds, grams = EXCLUDED.grams,"
                " plates = EXCLUDED.plates,"
                " printer = EXCLUDED.printer, failure = NULL,"
                " finished_at = EXCLUDED.finished_at"
                f" RETURNING {_COLUMNS}",
                (
                    price_key,
                    model_sha256,
                    currency,
                    priced.unit_price_minor,
                    priced.total_minor,
                    priced.unit_cost_minor,
                    Jsonb([line.model_dump(mode="json") for line in priced.lines]),
                    sum(plate.print_seconds for plate in sliced.plates),
                    Jsonb({str(slot): format(used, "f") for slot, used in sorted(grams.items())}),
                    Jsonb([plate.model_dump(mode="json") for plate in sliced.plates]),
                    None if sliced.rate is None else sliced.rate.printer,
                ),
            ).fetchone()
        row = _row(found)
        assert row is not None
        return row

    def fail(self, price_key: str, model_sha256: str, failure: str) -> PriceRow:
        """No price for the variant, and why."""
        with self._pool.connection() as conn:
            found = conn.execute(
                "INSERT INTO storefront_slice_prices (price_key, model_sha256, status,"
                " failure, finished_at) VALUES (%s, %s, 'failed', %s, clock_timestamp())"
                " ON CONFLICT (price_key) DO UPDATE SET status = 'failed',"
                " failure = EXCLUDED.failure, finished_at = EXCLUDED.finished_at"
                f" RETURNING {_COLUMNS}",
                (price_key, model_sha256, failure[:1000]),
            ).fetchone()
        row = _row(found)
        assert row is not None
        return row


def _stands(row: PriceRow, now: datetime) -> bool:
    """Whether ``row`` is the answer as it is: a price, a slice still within
    `LOST_AFTER`, or a failure newer than `RETRY_FAILED_AFTER`."""
    if row.status == "done":
        return True
    if row.status == "running":
        return row.started_at > now - LOST_AFTER
    return row.finished_at is not None and row.finished_at > now - RETRY_FAILED_AFTER


# -- the key -----------------------------------------------------------------------------------


def storefront_choices(settings: StoredSettings, slug: str, printer: str) -> PrintChoices:
    """The print dialog's choices for ``slug`` on registry printer ``printer``, as the
    back office's dialog opens on them: the model's remembered nozzles, tier and process
    (``model_print_choices``), else the dialog's defaults (Standard 0.4 mm on both sides,
    ``extruders.default_nozzles``), and the plate remembered on the printer, else the
    default plate."""
    remembered = settings.model_print_choices.get(slug)
    nozzles = one_flow(remembered.nozzles) if remembered and remembered.nozzles else None
    return PrintChoices(
        nozzles=nozzles or default_nozzles(None),
        tier=(remembered.tier if remembered and remembered.tier else "standard"),
        process_name=remembered.process_name if remembered else None,
        bed_type=settings.bed_type_of(printer=printer) or DEFAULT_BED,
    )


def price_key(
    *,
    model_sha256: str,
    filaments: Sequence[Filament],
    profile: StorefrontSliceProfile,
    choices: PrintChoices,
    currency: str,
    policy: PricingPolicy,
    machine_rate: str | None,
) -> str:
    """The sha256 of everything a variant's price rests on (owner, 2026-11-14): the 3MF's
    content, each slot's price-book filament as it is priced, the profile, the dialog's
    choices, and the price book's policy and the machine rate that prices it (the live
    rate of the profile's printer, `MachineRates.for_quote`, #6153)."""
    body = {
        "v": KEY_VERSION,
        "model": model_sha256,
        "filaments": [
            {"id": f.id, "material": f.material, "cost_per_kg": f.cost_per_kg} for f in filaments
        ],
        "profile": profile_digest(profile),
        "choices": choices.model_dump(mode="json"),
        "currency": currency,
        "policy": policy.model_dump(mode="json"),
        "machine_rate": machine_rate,
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


#: The sha256 of each done job's 3MF, by job id: one read of the file per job and
#: process. Bounded; a job that falls out is only hashed again.
_digests: OrderedDict[str, str] = OrderedDict()
_digests_lock = threading.Lock()
DIGESTS_KEPT: Final = 4096


def model_digest(job_id: str, path: Path) -> str:
    """The sha256 of the render's 3MF at ``path``. Blocking: call it in a thread."""
    with _digests_lock:
        found = _digests.get(job_id)
    if found is not None:
        return found
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    found = digest.hexdigest()
    with _digests_lock:
        _digests[job_id] = found
        _digests.move_to_end(job_id)
        while len(_digests) > DIGESTS_KEPT:
            _digests.popitem(last=False)
    return found


# -- the operation's run -----------------------------------------------------------------------


class SliceVariant(BaseModel):
    """The ``storefront_slice_price`` operation's request: one variant to slice and
    price."""

    model_config = ConfigDict(extra="forbid")

    price_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference: Reference
    slice: SliceQuoteRequest
    #: The price-book filament of each slot of the render, from 0.
    slot_filaments: list[str]
    currency: str
    #: The profile's spool of each slot, which the run checks the printer still offers.
    spools: list[int]
    #: The machine rate the estimate read put in the key, which prices the slice: the
    #: run never reads the rate again, so the key and the price use one rate (#6153).
    #: None prices machine time at the book's rate.
    machine_rate: MachineRateBreakdown | None = None


def subject_of(key: str) -> str:
    """The subject of a variant's slice and its operation."""
    return f"storefront_price:{key}"


@dataclass
class SliceRun:
    """What a storefront slice's run reads (``orders/operations.py`` builds it from the
    state, as a quote's ``QuoteRun``)."""

    prices: SlicePrices
    #: The price-book filament of an id, or None.
    filament: Callable[[str], Filament | None]
    book: PolicySource
    outputs: OutputsRead
    renders: Renders
    create_output: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    slicer: Slicer
    #: The spool ids the print dialog offers on a registry printer now, or None for a
    #: printer the registry does not hold (``api/storefront_slice.py`` ``offered_spools``).
    offered: Callable[[str], Awaitable[Collection[int] | None]]
    #: The sha256 of the 3MF of the render job a rendered output was saved from, or
    #: None when that cannot be read (#6370 review).
    rendered_sha256: Callable[[RenderedOutput], Awaitable[str | None]]
    follow_every: float = FOLLOW_EVERY


async def price_variant(run: SliceRun, variant: SliceVariant) -> PriceRow:
    """The variant sliced and priced through :func:`slice_and_price`, and the price
    written. A row that is no longer ``running`` (an earlier attempt finished it) is the
    answer as it is. No price, with the reason, when a profile spool is not offered on
    the printer now, a slot's filament is gone or archived, the render is of another 3MF
    than the key's (`GEOMETRY_DIFFERS`), the slice fails, or a sliced
    plate cannot be priced. A passing failure (:func:`_passing`) is raised as
    :class:`SliceUnavailableError`, and any other failure as it is, so the operation
    tries again and the row stays ``running``."""
    key = variant.price_key
    stood = await asyncio.to_thread(run.prices.get, key)
    if stood is not None and stood.status != "running":
        return stood
    printer = variant.slice.printer
    assert printer is not None
    try:
        offered = await run.offered(printer)
        if offered is None or any(spool not in offered for spool in variant.spools):
            return await _failed(run, variant, PROFILE_STALE)
        records = [await asyncio.to_thread(run.filament, f) for f in variant.slot_filaments]
        if any(record is None or record.archived_at is not None for record in records):
            return await _failed(run, variant, FILAMENT_GONE)
        filaments = {slot: record for slot, record in enumerate(records) if record is not None}
        rendered = await output_for_reference(
            variant.reference,
            outputs=run.outputs,
            renders=run.renders,
            create_output=run.create_output,
            name=f"Storefront: {variant.reference.template_name}",
            saved_as=storefront_output_id(variant.reference.render_key),
            # #6371: kept out of the back office's Recent renders.
            source=STOREFRONT,
            follow_every=run.follow_every,
        )
        # Only the visitor's own geometry is priced (#6370 review): a render of another
        # 3MF than the key's (the design moved under it) gets no firm price.
        if await run.rendered_sha256(rendered) != variant.model_sha256:
            return await _failed(run, variant, GEOMETRY_DIFFERS)
        subject = subject_of(key)

        async def machine_rate(
            _printer: str | None, _printer_id: int | None
        ) -> MachineRateBreakdown | None:
            return variant.machine_rate

        async def priced_for(plates: list[Any]) -> PriceSlots:
            sliced_slots(plates, range(len(filaments)))
            return PriceSlots(filaments=filaments, quantity=1)

        sliced = await slice_and_price(
            run.slicer,
            lambda: run.slicer.start(rendered.output_id, variant.slice, subject),
            subject,
            variant.slice,
            book=run.book,
            machine_rate=machine_rate,
            currency=variant.currency,
            priced_for=priced_for,
            follow_every=run.follow_every,
        )
    except QuoteError as error:
        return await _failed(run, variant, sliced_plate_problem(error).detail)
    except ApiError as error:
        if _passing(error):
            raise SliceUnavailableError(error.detail) from error
        return await _failed(run, variant, error.detail)
    return await asyncio.to_thread(
        run.prices.finish, key, variant.model_sha256, variant.currency, sliced
    )


async def _failed(run: SliceRun, variant: SliceVariant, failure: str) -> PriceRow:
    logger.info("a storefront variant has no slice price: %s", failure)
    return await asyncio.to_thread(
        run.prices.fail, variant.price_key, variant.model_sha256, failure
    )


# -- the estimate read -------------------------------------------------------------------------


@dataclass(frozen=True)
class Planned:
    """A variant ready to price: its key and the operation's request."""

    key: str
    variant: SliceVariant


def plan_variant(
    *,
    settings: StoredSettings,
    reference: Reference,
    model_sha256: str,
    filaments: Sequence[Filament],
    currency: str,
    policy: PricingPolicy,
    machine_rate: MachineRateBreakdown | None,
) -> Planned | None:
    """The variant of ``reference``'s render with ``filaments`` on its slots (from 0),
    or None when it has no slice price: no shop slice profile, or a slot's material
    with no spool in it."""
    profile = settings.storefront_slice_profile
    if profile is None or not filaments:
        return None
    choices = storefront_choices(settings, reference.slug, profile.printer)
    try:
        request = slice_request(profile, [f.material for f in filaments], choices)
    except ProfileMissingMaterialError:
        return None
    key = price_key(
        model_sha256=model_sha256,
        filaments=filaments,
        profile=profile,
        choices=choices,
        currency=currency,
        policy=policy,
        machine_rate=None if machine_rate is None else machine_rate.rate_per_hour,
    )
    return Planned(
        key=key,
        variant=SliceVariant(
            price_key=key,
            model_sha256=model_sha256,
            reference=reference,
            slice=request,
            slot_filaments=[f.id for f in filaments],
            currency=currency,
            spools=[slot.spool_id for slot in request.filament_plan.slots],
            machine_rate=machine_rate,
        ),
    )


#: The answer when there is no firm price: the shop prices the line by hand.
NO_PRICE: Final = StoreEstimate(source="none")


def pending(key: str | None = None) -> StoreEstimate:
    """The answer while the price is worked out: read it again after `PENDING_RETRY`."""
    return StoreEstimate(source="pending", retry_after=PENDING_RETRY, price_key=key)


def answer_of(row: PriceRow) -> StoreEstimate:
    """A row as the estimate answers it: the price once done, pending while it runs, and
    no price once it failed."""
    if row.status == "running":
        return pending(row.price_key)
    if row.status == "failed" or row.currency is None or row.total_minor is None:
        return NO_PRICE.model_copy(update={"price_key": row.price_key})
    return StoreEstimate(
        source="slice",
        price_minor=row.total_minor,
        currency=row.currency,
        minor_exponent=minor_exponent(row.currency),
        price_key=row.price_key,
        grams=row.grams,
        print_seconds=row.print_seconds,
    )


def _orders(state: AppState) -> OrdersComponent:
    # Here, not at the top: the orders component imports this module.
    from scadbuddy.orders.component import ORDERS

    orders: OrdersComponent = state.components.get(ORDERS)
    return orders


async def _planned(
    state: AppState,
    *,
    reference: Reference,
    model_sha256: str,
    filaments: Sequence[Filament] | None,
) -> Planned | None:
    """The variant's key and its operation's request, as the shop's settings, the price
    book and the live machine rate stand now; None when it has no slice price (a slot
    with no filament, no shop slice profile, or no spool for a slot's material)."""
    if filaments is None:
        return None
    orders = _orders(state)
    settings = await asyncio.to_thread(state.settings_store.load)
    profile = settings.storefront_slice_profile
    if profile is None:
        return None
    currency, policy, _ = await asyncio.to_thread(orders.price_book.policy)
    # The live rate, as a back-office quote prices it: the key holds it and the run
    # prices with it, so a rate change is a new key (#6153).
    rate = await live_rate(orders.machine_rates, profile.printer)
    return plan_variant(
        settings=settings,
        reference=reference,
        model_sha256=model_sha256,
        filaments=filaments,
        currency=currency,
        policy=policy,
        machine_rate=rate,
    )


async def firm_price(
    state: AppState,
    *,
    reference: Reference,
    model_sha256: str,
    filaments: Sequence[Filament] | None,
) -> PriceRow | None:
    """The variant's firm price as it stands now, read only (#6153): the ``done`` row of
    the key made from the variant and the shop's settings, the price book and the live
    machine rate now, the same key :func:`slice_estimate` makes. None when the key has
    no ``done`` row (a slice still running or failed, or a key that moved because the
    book, the rate or the profile changed). It starts no slice. A cart line's add and the
    checkout read it (``line_prices.py``), so the server never takes a price from the
    browser."""
    planned = await _planned(
        state, reference=reference, model_sha256=model_sha256, filaments=filaments
    )
    if planned is None:
        return None
    row = await asyncio.to_thread(_orders(state).slice_prices.get, planned.key)
    if row is None or row.status != "done" or row.currency is None:
        return None
    return row


async def slice_estimate(
    state: AppState,
    *,
    reference: Reference,
    model_sha256: str,
    filaments: Sequence[Filament] | None,
) -> StoreEstimate:
    """The firm price of the render on screen (#6153): the cached price of its variant,
    else its slice started (the read starts it, and a re-read joins it) while fewer than
    ``storefront_slices_concurrent`` run, else pending. ``filaments`` are the price-book
    filament of each slot of the render, or None when a slot has none: then, and with no
    shop slice profile or no spool for a slot's material, there is no price."""
    planned = await _planned(
        state, reference=reference, model_sha256=model_sha256, filaments=filaments
    )
    if planned is None:
        return NO_PRICE
    prices = _orders(state).slice_prices
    row = await asyncio.to_thread(prices.get, planned.key)
    if row is not None and row.status == "done":
        return answer_of(row)
    await _settle_ended(state, prices)
    limit = (await asyncio.to_thread(state.settings_store.snapshot)).runtime
    claim = await asyncio.to_thread(
        prices.claim, planned.key, model_sha256, limit=limit.storefront_slices_concurrent
    )
    if not claim.claimed:
        # Another read's slice of this variant, or no room yet: the page reads again.
        return pending(planned.key) if claim.row is None else answer_of(claim.row)
    assert claim.row is not None and claim.row.claim_key is not None
    await _start(state, prices, planned, claim.row.claim_key, claim.previous)
    return pending(planned.key)


async def _start(
    state: AppState,
    prices: SlicePrices,
    planned: Planned,
    claim_key: str,
    previous: PriceRow | None,
) -> None:
    """The variant's slice started as its operation, which this claim's own key keys.
    A start that fails gives the claim back (:meth:`SlicePrices.release`), so the next
    read claims it again; one whose answer says the operation may still run keeps the
    row ``running``, so it still counts against the capacity, and `_settle_ended`
    settles it once the operation ends."""
    # Here, not at the top: the API's operations import the components.
    from fastapi import Response

    from scadbuddy.api.operations import may_still_run, run_operation
    from scadbuddy.operations.component import OPERATIONS
    from scadbuddy.operations.kinds import operation_key

    ops = state.components.get(OPERATIONS)
    kind = ops.kinds[SLICE_PRICE_KIND]
    body = planned.variant.model_dump(mode="json")
    subject = subject_of(planned.key)
    idempotency_key = claim_key
    keyed = operation_key(kind.name, subject, body, idempotency_key, kind.unkeyed)
    await asyncio.to_thread(prices.set_operation, planned.key, claim_key, keyed)
    try:
        await run_operation(
            ops,
            Response(),
            kind=kind,
            subject=subject,
            request=body,
            idempotency_key=idempotency_key,
        )
    except ApiError as error:
        if may_still_run(error):
            logger.warning("a storefront slice may have started: %s", error.detail)
            return
        logger.warning("a storefront slice was not started: %s", error.detail)
        await asyncio.to_thread(prices.release, planned.key, claim_key, previous)
    except Exception:
        logger.warning("a storefront slice was not started", exc_info=True)
        await asyncio.to_thread(prices.release, planned.key, claim_key, previous)


async def _settle_ended(state: AppState, prices: SlicePrices) -> None:
    """Every ``running`` row whose operation has ended without writing it (its last
    attempt failed) is failed, so it holds no place in the capacity. One operation
    read per running row: at most `storefront_slices_concurrent` rows run, and that
    cap is small (1 by default), so the reads stay few."""
    from scadbuddy.operations.component import OPERATIONS

    ops = state.components.get(OPERATIONS)
    for row in await asyncio.to_thread(prices.running):
        if row.operation_key is None or row.claim_key is None:
            continue
        operation = await ops.store.find(row.operation_key)
        if operation is None or operation.status == "running":
            continue
        failure = operation.error.detail if operation.error is not None else None
        await asyncio.to_thread(
            prices.lost, row.price_key, row.claim_key, failure or "the slice ended with no price"
        )


#: The live rate each printer was last read at, per `MachineRates` (one per app).
_rates: weakref.WeakKeyDictionary[Any, dict[str, tuple[float, MachineRateBreakdown | None]]] = (
    weakref.WeakKeyDictionary()
)


async def live_rate(rates: Any, printer: str) -> MachineRateBreakdown | None:
    """``rates.for_quote(printer, None)``, read again only `RATE_TTL` after the last read
    in this process (#6370 review): a page's re-reads while its slice runs ask Bambuddy
    nothing. The key holds the value read, and the run prices with it."""
    now = time.monotonic()
    kept = _rates.setdefault(rates, {})
    found = kept.get(printer)
    if found is not None and now - found[0] < RATE_TTL:
        return found[1]
    rate: MachineRateBreakdown | None = await rates.for_quote(printer, None)
    kept[printer] = (now, rate)
    return rate
