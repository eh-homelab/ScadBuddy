"""A storefront variant's firm price (#6153, spec 2026-11-13 §5.4, §5.6), from a slice
through the back office's quote path.

One pricing path (owner, 2026-11-14): the price is ``orders/quote.py``
:func:`~scadbuddy.orders.quote.slice_and_price`, which a back-office quote runs too. A
variant names its subject (#6710 §7.1): a template's render, or a file listing's library
file. A render's variant is rendered to an output (``orders/rendering.py``
``output_for_reference``); a file's is the listing's held copy of the file
(``held_file.py``), which nothing renders. Either is sliced by the print dialog's
background slice (``orders/operations.py`` ``PreviewSlicer``, the native slicer or
Bambuddy's, as a quote routes it; a file through ``start_library``, as a back-office
quote of a library file item slices it) and priced at the price book's policy and the
machine rate of the profile's printer. The template, or the file itself, gives the
support and process settings, as for any output or library file. The shop slice profile
(``slice_profile.py``) gives only the printer and a filament type per material. The
rest is what the print dialog opens on for the subject on that printer
(``bambuddy/choices.py`` :func:`~scadbuddy.bambuddy.choices.opening_choices`, #6574): the
remembered choices of the subject's scope (the model's slug, or ``library:<file id>``),
else the defaults from the printer's live status, read as the dialog reads it
(:func:`~scadbuddy.bambuddy.choices.printer_opening`), and the print sequence, process
overrides and settings per part the dialog seeds from those remembered choices
(``slice_profile.py`` ``slice_request``, #6710 §7.2). A printer whose status cannot be
read gives no price (`STATUS_UNREAD`): a default would guess the nozzle flow. Each slot
has no spool: it is sliced with its material's filament type's preset for the H2C and
the chosen nozzle, as a spool of that type would be, in the visitor's colour
(``slice_profile.py`` ``slot_presets``, ``bambuddy/resolver.py`` ``resolve``: a render's
3MF has the colours already, a file's slot takes its filament's colour as
``slot_colours``). A type with no preset for that nozzle gives no price
(`NO_TYPE_PRESET`).

One price per variant (:func:`price_key`): the sha256 of the file the slice reads (the
render's 3MF, or the file listing's pinned copy), the subject's kind, each slot's
price-book filament, colour and preset, the profile, the choices and the remembered
setup, and the price book and the machine rate as they stand. A change to any of them is
another key, so an old price is never read again.
A row is kept in ``storefront_slice_prices`` (:class:`SlicePrices`) by its key and holds no
job id and no bytes, so it outlives the render prune.

The slice runs as the operation ``storefront_slice_price`` on the ``library`` queue, as a
back-office quote does (:func:`price_variant`). A visitor's estimate read starts it, only
for the render on screen. At most ``storefront_slices_concurrent`` run at once
(:meth:`SlicePrices.claim`); the rest wait, and the page reads again. A slice that fails
(a preset that does not fit the nozzle among its causes) gives no price: the shop prices
that line by hand.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import threading
import time
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scadbuddy.bambuddy.choices import PrinterOpening, opening_choices, printer_opening
from scadbuddy.bambuddy.options import library_options_scope
from scadbuddy.bambuddy.resolver import PRINTER_MODEL, PrintChoices
from scadbuddy.core.problems import ApiError
from scadbuddy.filament.models import EffectivePreset
from scadbuddy.filament.presets import preset_for
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
from scadbuddy.orders.storefront.held_file import FILE_GONE as FILE_GONE
from scadbuddy.orders.storefront.held_file import HeldFile, PricedFile
from scadbuddy.orders.storefront.slice_profile import (
    ProfileMissingMaterialError,
    StorefrontSliceProfile,
    profile_digest,
    slice_request,
    slot_presets,
    slot_types,
)

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState
    from scadbuddy.orders.component import OrdersComponent
    from scadbuddy.store.content import ContentStore

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
#: #6574: the owner's rule is no price when unsure, so a status not read is no price,
#: never a guess at the nozzle flow.
STATUS_UNREAD: Final = "the status of the shop slice profile's printer cannot be read"
NO_TYPE_PRESET: Final = "a slot's filament type has no preset for the H2C and the nozzle"
FILAMENT_GONE: Final = "a slot's filament is no longer in the price book"
GEOMETRY_DIFFERS: Final = (
    "the variant rendered to another 3MF than the one its key names, so its price would"
    " not be for the design the visitor sees"
)
#: Every digest of a price key changes when this does. 4 (#6710 §7.4): ``model`` is the
#: sha256 of the file the slice reads (a render's 3MF or a file listing's held copy), and
#: the key holds the subject's kind and the remembered setup.
KEY_VERSION: Final = 4
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
    #: The machine rate's parts that priced the slice (#5774), which the checkout's
    #: `storefront` quote records; None when the book's rate priced it, or on a row
    #: written before it was kept.
    machine_rate_breakdown: MachineRateBreakdown | None = None
    failure: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    #: The variant key of the render a ``done`` row priced (#6921, `renders.py`
    #: `variant_key`), and the price-book filament of each of its slots (from 0), so the
    #: instant read can build its price key again. None on a row written before.
    variant_key: str | None = None
    #: The listing the variant key was made for: its preview is served only while that
    #: listing is published (#6921 review).
    listing_id: str | None = None
    #: A file listing's row (#6710) holds one entry per slot the file uses, in the order
    #: of its ``PricedFile.used``, not one per slot from 0: map an index through ``used``
    #: to get the slot. The same holds for ``slot_colours``.
    slot_filaments: list[str] | None = None
    #: Each slot's colour, and the output the slice saved ("Storefront: ...", which
    #: outlives the render prune): the instant read shows its preview (#6921). A file
    #: listing's row has no output (``output_id`` None) and its colours by ``used``.
    slot_colours: list[str] | None = None
    output_id: str | None = None
    #: A file listing's row only (#7523): :func:`setup_key` of the slice. None on a
    #: template's row and on a row written before.
    setup_key: str | None = None


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
    " total_minor, unit_cost_minor, lines, print_seconds, grams, plates, printer,"
    " machine_rate_breakdown, failure, started_at, finished_at, variant_key, listing_id,"
    " slot_filaments, slot_colours, output_id, setup_key"
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
        self,
        price_key: str,
        model_sha256: str,
        currency: str,
        sliced: SlicedPrice,
        *,
        variant_key: str | None = None,
        listing_id: str | None = None,
        slot_filaments: Sequence[str] | None = None,
        slot_colours: Sequence[str] | None = None,
        output_id: str | None = None,
        setup_key: str | None = None,
    ) -> PriceRow:
        """The price written once the slice is done, with the variant key of the render it
        priced and its listing, the filament and colour of each of its slots and the output
        it saved (#6921), and a file's setup (:func:`setup_key`, #7523). A NULL of these
        keeps the row's value: a slice whose operation started before #6921 never clears a key
        the row recorded or adopted."""
        grams: dict[int, Decimal] = {}
        for plate in sliced.plates:
            for used in plate.filaments:
                grams[used.slot] = grams.get(used.slot, Decimal(0)) + used.used_g
        priced = sliced.priced
        with self._pool.connection() as conn:
            found = conn.execute(
                "INSERT INTO storefront_slice_prices (price_key, model_sha256, status,"
                " currency, unit_price_minor, total_minor, unit_cost_minor, lines,"
                " print_seconds, grams, plates, printer, machine_rate_breakdown, variant_key,"
                " listing_id, slot_filaments, slot_colours, output_id, setup_key, finished_at)"
                " VALUES (%s, %s, 'done', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " %s, %s, %s, clock_timestamp())"
                " ON CONFLICT (price_key) DO UPDATE SET status = 'done',"
                " currency = EXCLUDED.currency, unit_price_minor = EXCLUDED.unit_price_minor,"
                " total_minor = EXCLUDED.total_minor,"
                " unit_cost_minor = EXCLUDED.unit_cost_minor, lines = EXCLUDED.lines,"
                " print_seconds = EXCLUDED.print_seconds, grams = EXCLUDED.grams,"
                " plates = EXCLUDED.plates,"
                " printer = EXCLUDED.printer,"
                " machine_rate_breakdown = EXCLUDED.machine_rate_breakdown, failure = NULL,"
                " variant_key"
                " = coalesce(EXCLUDED.variant_key, storefront_slice_prices.variant_key),"
                " listing_id = coalesce(EXCLUDED.listing_id, storefront_slice_prices.listing_id),"
                " slot_filaments"
                " = coalesce(EXCLUDED.slot_filaments, storefront_slice_prices.slot_filaments),"
                " slot_colours"
                " = coalesce(EXCLUDED.slot_colours, storefront_slice_prices.slot_colours),"
                " output_id = coalesce(EXCLUDED.output_id, storefront_slice_prices.output_id),"
                " setup_key = coalesce(EXCLUDED.setup_key, storefront_slice_prices.setup_key),"
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
                    None if sliced.rate is None else Jsonb(sliced.rate.model_dump(mode="json")),
                    variant_key,
                    listing_id,
                    None if slot_filaments is None else Jsonb(list(slot_filaments)),
                    None if slot_colours is None else Jsonb(list(slot_colours)),
                    output_id,
                    setup_key,
                ),
            ).fetchone()
        row = _row(found)
        assert row is not None
        return row

    def adopt(
        self, price_key: str, variant_key: str, listing_id: str, slot_filaments: Sequence[str]
    ) -> None:
        """A ``done`` row with no variant key (written before #6921) takes ``variant_key``
        and its listing, the key of a render whose estimate read it, so the instant read
        finds it. A row that has a key keeps it: one variant's price never moves to
        another's."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE storefront_slice_prices SET variant_key = %s, listing_id = %s,"
                " slot_filaments = %s"
                " WHERE price_key = %s AND status = 'done' AND variant_key IS NULL",
                (variant_key, listing_id, Jsonb(list(slot_filaments)), price_key),
            )

    def variant_row(self, variant_key: str) -> PriceRow | None:
        """The one ``done`` row of ``variant_key`` that the instant read and the cart add
        both take (#6921): the newest that names the output its slice saved, else the
        newest. So the price shown, the preview and the 3MF a line is charged from are of
        one row. None when the variant has no ``done`` row."""
        with self._pool.connection() as conn:
            found = conn.execute(
                f"SELECT {_COLUMNS} FROM storefront_slice_prices"
                " WHERE variant_key = %s AND status = 'done'"
                " ORDER BY (output_id IS NOT NULL) DESC, finished_at DESC LIMIT 1",
                (variant_key,),
            ).fetchone()
        return _row(found)

    def listing_last(
        self, listing_id: str, model_sha256: str, *, filaments: Collection[str], setup_key: str
    ) -> PriceRow | None:
        """A file listing's last firm price (#6710 §7.5) while its visitor's colours are
        priced: a ``done`` row of a file listing's slice (never a template's render of the
        same bytes, #7523) of the file ``model_sha256``. ``filaments`` are the filaments a
        print of the chosen material may take (`FileChoice.alike`).

        The listing's own rows first, of any colours and setup: the newest with every slot's
        filament in ``filaments``, else the newest. With none, the newest of another
        published listing (two listings of one file share a price, `key_subject`, and its
        row names only the one that made it), only with every filament in ``filaments`` and
        of the same setup (``setup_key``): a price of another material or setup is another
        print's. An unpublished listing's is never read, so a visitor sees no price of a
        listing they cannot open. None when there is no such row."""
        alike = Jsonb(sorted(filaments))
        read = (
            f"SELECT {_COLUMNS} FROM storefront_slice_prices"
            " WHERE model_sha256 = %s AND status = 'done'"
            " AND currency IS NOT NULL AND total_minor IS NOT NULL"
        )
        with self._pool.connection() as conn:
            found = conn.execute(
                # A file's row from before `setup_key` has no output and no variant key.
                f"{read} AND listing_id = %s"
                " AND (setup_key IS NOT NULL OR (variant_key IS NULL AND output_id IS NULL))"
                " ORDER BY coalesce(slot_filaments <@ %s, false) DESC, finished_at DESC LIMIT 1",
                (model_sha256, listing_id, alike),
            ).fetchone()
            if found is None:
                found = conn.execute(
                    f"{read} AND listing_id IN (SELECT id FROM storefront_listings WHERE published)"
                    " AND setup_key = %s AND slot_filaments <@ %s"
                    " ORDER BY finished_at DESC LIMIT 1",
                    (model_sha256, setup_key, alike),
                ).fetchone()
        return _row(found)

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


#: The remembered setup a slice request carries (#6710 §7.2), which the key holds.
SETUP_FIELDS: Final = frozenset({"print_sequence", "process_overrides", "part_settings"})


def key_subject(library_file: PricedFile | None) -> dict[str, str]:
    """The key's ``subject`` (#6710 §7.4): a template's render, or a library file of its
    library. Never a file id or a job id: two listings of one file share a price."""
    if library_file is None:
        return {"kind": "template"}
    return {"kind": "library_file", "library": library_file.library}


def key_setup(request: SliceQuoteRequest) -> dict[str, Any]:
    """The key's ``setup`` (#6710 §7.4): the remembered print sequence, process overrides
    and settings per part the request slices with."""
    return request.model_dump(mode="json", include=set(SETUP_FIELDS))


def price_key(
    *,
    model_sha256: str,
    filaments: Mapping[int, Filament],
    profile: StorefrontSliceProfile,
    choices: PrintChoices,
    presets: Mapping[int, Sequence[EffectivePreset]],
    currency: str,
    policy: PricingPolicy,
    machine_rate: str | None,
    subject: Mapping[str, str],
    setup: Mapping[str, Any],
) -> str:
    """The sha256 of everything a variant's price rests on (owner, 2026-11-14; #6574: what
    the slicer gets): the sha256 of the file the slice reads (``model_sha256``: a render's
    3MF, or a file listing's pinned copy, #6710), the subject's kind (:func:`key_subject`),
    each slot's price-book filament as it is priced (``filaments``, by slot from 0), with
    its colour and its filament type's preset for the H2C and the chosen nozzle
    (``presets``, by slot id; a colour pair changes the purge), the profile, the dialog's
    choices and the remembered setup (:func:`key_setup`), and the price book's policy and
    the machine rate that prices it (the live rate of the profile's printer,
    `MachineRates.for_quote`, #6153)."""
    size = choices.nozzles[0].size

    def preset_of(slot_id: int) -> dict[str, Any] | None:
        found = preset_for(presets.get(slot_id, ()), PRINTER_MODEL, size)
        if found is None:
            return None
        return {"filament": found.slicer_filament, "name": found.slicer_filament_name}

    body = {
        "v": KEY_VERSION,
        "model": model_sha256,
        "subject": dict(subject),
        "filaments": [
            {
                "slot": slot,
                "id": f.id,
                "material": f.material,
                "cost_per_kg": f.cost_per_kg,
                "colour": f.colour.upper(),
                "preset": preset_of(slot + 1),
            }
            for slot, f in sorted(filaments.items())
        ],
        "profile": profile_digest(profile),
        "choices": choices.model_dump(mode="json"),
        "setup": dict(setup),
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
    #: What it slices, one of two (#6710 §7.1): a template variant's render, or a file
    #: listing's file, whose held copy the slice reads. An operation started before #6710
    #: has a ``reference`` and no ``library_file``.
    reference: Reference | None = None
    library_file: PricedFile | None = None
    slice: SliceQuoteRequest
    #: The price-book filament of each slot, from 0: every slot of a render, or each used
    #: slot of a file (``library_file.used``), in that order.
    slot_filaments: list[str]
    currency: str
    #: An operation started before #6574 names the profile's spools; they are not read.
    spools: list[int] = Field(default_factory=list)
    #: Each slot's filament type's presets, by slot id (#6574): the slot has no spool and
    #: is sliced with its type's preset (``resolver.resolve``'s ``slot_presets``).
    slot_presets: dict[int, list[EffectivePreset]] = Field(default_factory=dict)
    #: The machine rate the estimate read put in the key, which prices the slice: the
    #: run never reads the rate again, so the key and the price use one rate (#6153).
    #: None prices machine time at the book's rate.
    machine_rate: MachineRateBreakdown | None = None
    #: The variant key of the render whose estimate started the slice (#6921), which the
    #: ``done`` row records. None on an operation started before.
    variant_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    #: The listing the variant key was made for (#6921 review), which the row records.
    listing_id: str | None = None
    #: Each spool-less slot's colour, by slot id (#6710): a file's slot takes its
    #: filament's colour (``resolve``'s ``slot_colours``). Empty for a render, whose 3MF
    #: holds the visitor's colours already.
    slot_colours: dict[int, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_subject(self) -> SliceVariant:
        if (self.reference is None) == (self.library_file is None):
            raise ValueError("a variant names a reference or a library file, exactly one")
        return self

    @property
    def slots(self) -> list[int]:
        """The slot (from 0) each of :attr:`slot_filaments` is the filament of."""
        if self.library_file is not None:
            return list(self.library_file.used)
        return list(range(len(self.slot_filaments)))


def setup_key(variant: SliceVariant) -> str:
    """The sha256 of how ``variant`` is sliced, whatever its colours (#7523): the subject's
    kind and the whole slice request (the printer, the choices and the remembered print
    sequence, overrides and settings per part). The filaments, the price book and the rates
    are not in it. A new field of the request moves it, so an old row then matches no other
    listing's setup."""
    body = {
        "subject": key_subject(variant.library_file),
        "slice": variant.slice.model_dump(mode="json"),
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


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
    #: The sha256 of the 3MF of the render job a rendered output was saved from, or
    #: None when that cannot be read (#6370 review).
    rendered_sha256: Callable[[RenderedOutput], Awaitable[str | None]]
    follow_every: float = FOLLOW_EVERY
    #: Where a file listing's held copy is (#6710 §7.3.1); None where there is none, so a
    #: file variant has no copy to slice (`FILE_GONE`).
    content: ContentStore | None = None


async def price_variant(run: SliceRun, variant: SliceVariant) -> PriceRow:
    """The variant sliced and priced through :func:`slice_and_price`, and the price
    written. A row that is no longer ``running`` (an earlier attempt finished it) is the
    answer as it is. A render's variant renders the reference and slices the saved output;
    a file's slices the listing's held copy (#6710 §7.3), through ``start_library`` with
    the copy in place of the library's reads, so the live library file is never read and
    its state never changes the price. No price, with the reason, when a slot's filament
    is gone or archived, the render is of another 3MF than the key's (`GEOMETRY_DIFFERS`),
    the held copy is missing (`FILE_GONE`), the slice fails (a preset that does not fit the
    nozzle among its causes), or a sliced plate cannot be priced (a slot it must use at
    0 g). A passing failure (:func:`_passing`) is raised as :class:`SliceUnavailableError`,
    and any other failure as it is, so the operation tries again and the row stays
    ``running``."""
    key = variant.price_key
    stood = await asyncio.to_thread(run.prices.get, key)
    if stood is not None and stood.status != "running":
        return stood
    subject = subject_of(key)
    slots = variant.slots
    try:
        records = [await asyncio.to_thread(run.filament, f) for f in variant.slot_filaments]
        if any(record is None or record.archived_at is not None for record in records):
            return await _failed(run, variant, FILAMENT_GONE)
        filaments = {
            slot: record for slot, record in zip(slots, records, strict=True) if record is not None
        }
        start: Callable[[], Awaitable[list[Any]]]
        if variant.library_file is not None:
            file = variant.library_file
            content = run.content
            # The held copy, not the library's file (§7.3 step 2): a hold keeps it, so it
            # is gone only after a fault. Its key is its sha256, so its bytes are the key's.
            if content is None or await content.stat(file.copy_key) is None:
                return await _failed(run, variant, FILE_GONE)
            held = HeldFile(file, content)

            def start() -> Awaitable[list[Any]]:
                return run.slicer.start_library(
                    file.id,
                    variant.slice,
                    subject,
                    slot_presets=variant.slot_presets,
                    slot_colours=variant.slot_colours,
                    held=held,
                )

            slot_colours = [record.colour for record in records if record is not None]
            output_id = None
        else:
            reference = variant.reference
            assert reference is not None
            rendered = await output_for_reference(
                reference,
                outputs=run.outputs,
                renders=run.renders,
                create_output=run.create_output,
                name=f"Storefront: {reference.template_name}",
                saved_as=storefront_output_id(reference.render_key),
                # #6371: kept out of the back office's Recent renders.
                source=STOREFRONT,
                follow_every=run.follow_every,
            )
            # Only the visitor's own geometry is priced (#6370 review): a render of another
            # 3MF than the key's (the design moved under it) gets no firm price.
            if await run.rendered_sha256(rendered) != variant.model_sha256:
                return await _failed(run, variant, GEOMETRY_DIFFERS)

            def start() -> Awaitable[list[Any]]:
                return run.slicer.start(
                    rendered.output_id, variant.slice, subject, slot_presets=variant.slot_presets
                )

            slot_colours = rendered.colours
            output_id = rendered.output_id

        async def machine_rate(
            _printer: str | None, _printer_id: int | None
        ) -> MachineRateBreakdown | None:
            return variant.machine_rate

        async def priced_for(plates: list[Any]) -> PriceSlots:
            sliced_slots(plates, slots)
            return PriceSlots(filaments=filaments, quantity=1)

        sliced = await slice_and_price(
            run.slicer,
            start,
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
        functools.partial(
            run.prices.finish,
            key,
            variant.model_sha256,
            variant.currency,
            sliced,
            variant_key=variant.variant_key,
            listing_id=variant.listing_id,
            slot_filaments=variant.slot_filaments,
            slot_colours=slot_colours,
            output_id=output_id,
            setup_key=None if variant.library_file is None else setup_key(variant),
        )
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
    filaments: Sequence[Filament],
    currency: str,
    policy: PricingPolicy,
    machine_rate: MachineRateBreakdown | None,
    printer: PrinterOpening,
    type_presets: Mapping[UUID, Sequence[EffectivePreset]],
    reference: Reference | None = None,
    model_sha256: str | None = None,
    library_file: PricedFile | None = None,
    variant_key: str | None = None,
    listing_id: str | None = None,
) -> Planned | None:
    """The variant of its subject with ``filaments`` on its slots, sliced with the choices
    the print dialog opens on for the subject on the profile's printer (``printer``, as
    :func:`~scadbuddy.bambuddy.choices.printer_opening` read it; #6574) and the setup the
    dialog seeds from the subject's remembered choices (``slice_request``, #6710 §7.2),
    each slot with its filament type's presets (``type_presets``, by type).

    The subject is one of two (#6710 §7.1): ``reference``'s render, whose 3MF is
    ``model_sha256``, ``filaments`` on its slots from 0, its choices remembered under the
    model's slug; or ``library_file``, a file listing's file, ``filaments`` on its used
    slots in order, keyed by its pinned sha256, its choices remembered under
    ``library:<file id>`` while that scope holds an entry, else the copy publish took
    (``library_file.remembered``). A file's slots take their filament's colour
    (``slot_colours``, by slot id). None when it has no slice price: no shop slice
    profile, a slot's material with no type in it, a printer status that could not be read
    (`STATUS_UNREAD`), or a type with no preset for the H2C and the chosen nozzle
    (`NO_TYPE_PRESET`)."""
    if (reference is None) == (library_file is None):
        raise ValueError("a variant names a reference or a library file, exactly one")
    if library_file is not None:
        remembered = settings.model_print_choices.get(
            library_options_scope(library_file.id), library_file.remembered
        )
        slots = list(library_file.used)
        model = library_file.sha256
    else:
        assert reference is not None
        if model_sha256 is None:
            raise ValueError("a render's variant names the sha256 of its 3MF")
        remembered = settings.model_print_choices.get(reference.slug)
        slots = list(range(len(filaments)))
        model = model_sha256
    if len(slots) != len(filaments):
        raise ValueError("a variant names one filament for each slot it uses")
    profile = settings.storefront_slice_profile
    if profile is None or not filaments:
        return None
    if printer.status is None:
        logger.info("a storefront variant has no slice price: %s", STATUS_UNREAD)
        return None
    choices = opening_choices(remembered, printer.status, printer.bed_type(settings))
    try:
        types = slot_types(profile, [f.material for f in filaments])
    except ProfileMissingMaterialError:
        return None
    # By slot id (from 1): slot 0's filament is the slicer's slot 1 (§7.3.1).
    by_position = slot_presets(types, type_presets)
    presets = {slot + 1: by_position[index + 1] for index, slot in enumerate(slots)}
    size = choices.nozzles[0].size
    if any(preset_for(rows, PRINTER_MODEL, size) is None for rows in presets.values()):
        logger.info("a storefront variant has no slice price: %s", NO_TYPE_PRESET)
        return None
    request = slice_request(profile, choices, remembered)
    colours = (
        {}
        if library_file is None
        else {slot + 1: f.colour for slot, f in zip(slots, filaments, strict=True)}
    )
    key = price_key(
        model_sha256=model,
        filaments=dict(zip(slots, filaments, strict=True)),
        profile=profile,
        choices=request.choices,
        presets=presets,
        currency=currency,
        policy=policy,
        machine_rate=None if machine_rate is None else machine_rate.rate_per_hour,
        subject=key_subject(library_file),
        setup=key_setup(request),
    )
    return Planned(
        key=key,
        variant=SliceVariant(
            price_key=key,
            model_sha256=model,
            reference=reference,
            library_file=library_file,
            slice=request,
            slot_filaments=[f.id for f in filaments],
            currency=currency,
            slot_presets=presets,
            slot_colours=colours,
            machine_rate=machine_rate,
            variant_key=variant_key,
            listing_id=listing_id,
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


async def planned_variant(
    state: AppState,
    *,
    filaments: Sequence[Filament] | None,
    reference: Reference | None = None,
    model_sha256: str | None = None,
    library_file: PricedFile | None = None,
    variant_key: str | None = None,
    listing_id: str | None = None,
) -> Planned | None:
    """The variant's key and its operation's request, as the shop's settings, the price
    book, the live machine rate, the printer's status and the filament types' presets
    stand now; None when it has no slice price (a slot with no filament, no shop slice
    profile, a printer the registry lacks or whose status cannot be read, or no type or
    type preset for a slot's material). Its subject is a render (``reference`` and the
    sha256 of its 3MF) or a file listing's file (``library_file``, #6710), as
    :func:`plan_variant` takes them: every read of a price builds its key here."""
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
    opening = await live_opening(state, settings, profile.printer)
    if opening is None:
        logger.info("a storefront variant has no slice price: no printer %s", profile.printer)
        return None
    wanted = {profile.materials.get(f.material.casefold()) for f in filaments}
    type_presets = await read_type_presets(state, [t for t in wanted if t is not None])
    return plan_variant(
        settings=settings,
        reference=reference,
        model_sha256=model_sha256,
        library_file=library_file,
        filaments=filaments,
        currency=currency,
        policy=policy,
        machine_rate=rate,
        printer=opening,
        type_presets=type_presets,
        variant_key=variant_key,
        listing_id=listing_id,
    )


async def read_type_presets(
    state: AppState, type_ids: Sequence[UUID]
) -> dict[UUID, list[EffectivePreset]]:
    """Each filament type's presets (`filament_spool_type_presets`, #6262), as a spool of
    the type with none of its own reads them (`filament/presets.py`
    `effective_presets`), by type (#6574)."""
    # Here, not at the top: the filament component imports the orders' modules.
    from scadbuddy.filament.component import FILAMENT
    from scadbuddy.filament.sb_presets import bambuddy_refs_on

    types = state.components.get(FILAMENT).types

    def read() -> dict[UUID, list[EffectivePreset]]:
        rows = {type_id: types.presets(type_id) for type_id in type_ids}
        # A ScadBuddy preset's Bambuddy id, for a slice on Bambuddy's path (#7176).
        with state.settings_store.pool.connection() as conn:
            bambuddy = bambuddy_refs_on(
                conn, {row.slicer_filament for group in rows.values() for row in group}
            )
        return {
            type_id: [
                EffectivePreset(
                    printer_model=row.printer_model,
                    nozzle_diameter=row.nozzle_diameter,
                    slicer_filament=row.slicer_filament,
                    slicer_filament_name=row.slicer_filament_name,
                    overridden=False,
                    bambuddy_id=bambuddy.get(row.slicer_filament),
                )
                for row in group
            ]
            for type_id, group in rows.items()
        }

    return await asyncio.to_thread(read)


async def firm_price(
    state: AppState,
    *,
    filaments: Sequence[Filament] | None,
    reference: Reference | None = None,
    model_sha256: str | None = None,
    library_file: PricedFile | None = None,
) -> PriceRow | None:
    """The variant's firm price as it stands now, read only (#6153): the ``done`` row of
    the key made from the variant and the shop's settings, the price book and the live
    machine rate now, the same key :func:`slice_estimate` makes. None when the key has
    no ``done`` row (a slice still running or failed, or a key that moved because the
    book, the rate or the profile changed). It starts no slice. A cart line's add and the
    checkout read it (``line_prices.py``), so the server never takes a price from the
    browser. Its subject is a render's or a file listing's file, as `planned_variant` takes it."""
    planned = await planned_variant(
        state,
        reference=reference,
        model_sha256=model_sha256,
        library_file=library_file,
        filaments=filaments,
    )
    if planned is None:
        return None
    row = await asyncio.to_thread(_orders(state).slice_prices.get, planned.key)
    if row is None or row.status != "done" or row.currency is None:
        return None
    return row


#: What a read that would claim a new slice of a key pays first (#6710 §7.5): it takes
#: one render of the visitor's limits (raising when over them) and answers how to give it
#: back, which the read calls when it claims no slice after all.
Charge = Callable[[str], Awaitable[Callable[[], Awaitable[None]]]]


async def slice_estimate(
    state: AppState,
    *,
    filaments: Sequence[Filament] | None,
    reference: Reference | None = None,
    model_sha256: str | None = None,
    library_file: PricedFile | None = None,
    variant_key: str | None = None,
    listing_id: str | None = None,
) -> StoreEstimate:
    """The firm price of the render on screen (#6153): the cached price of its variant,
    else its slice started (the read starts it, and a re-read joins it) while fewer than
    ``storefront_slices_concurrent`` run, else pending. ``filaments`` are the price-book
    filament of each slot of the render, or None when a slot has none: then, and with no
    shop slice profile, no preset for a slot's material or no printer status, there is no
    price. ``variant_key`` is the render's and ``listing_id`` its listing's (#6921): the
    slice's ``done`` row records them, and a ``done`` row from before #6921 takes them
    (:meth:`SlicePrices.adopt`). Its subject is a render's or a file listing's file, as
    `planned_variant` takes it."""
    planned = await planned_variant(
        state,
        reference=reference,
        model_sha256=model_sha256,
        library_file=library_file,
        filaments=filaments,
        variant_key=variant_key,
        listing_id=listing_id,
    )
    return NO_PRICE if planned is None else await planned_estimate(state, planned)


async def planned_estimate(
    state: AppState, planned: Planned, *, charge: Charge | None = None
) -> StoreEstimate:
    """:func:`slice_estimate` of a variant already ``planned`` (`planned_variant`). With
    ``charge``, a read that would claim a slice pays it first, and is refunded when it
    claims none; a read that finds a price, or a slice that stands, pays nothing."""
    variant_key, listing_id = planned.variant.variant_key, planned.variant.listing_id
    prices = _orders(state).slice_prices
    row = await asyncio.to_thread(prices.get, planned.key)
    if row is not None and row.status == "done":
        if variant_key is not None and listing_id is not None and row.variant_key is None:
            await asyncio.to_thread(
                prices.adopt, planned.key, variant_key, listing_id, planned.variant.slot_filaments
            )
        return answer_of(row)
    await _settle_ended(state, prices)
    limit = (await asyncio.to_thread(state.settings_store.snapshot)).runtime
    refund = None
    if charge is not None and (row is None or not _stands(row, datetime.now(UTC))):
        refund = await charge(planned.key)
    claim = await asyncio.to_thread(
        prices.claim,
        planned.key,
        planned.variant.model_sha256,
        limit=limit.storefront_slices_concurrent,
    )
    if not claim.claimed and refund is not None:
        await refund()
    if not claim.claimed:
        # Another read's slice of this variant, or no room yet: the page reads again.
        return pending(planned.key) if claim.row is None else answer_of(claim.row)
    assert claim.row is not None and claim.row.claim_key is not None
    started = await _start(state, prices, planned, claim.row.claim_key, claim.previous)
    if not started and refund is not None:
        # The claim went back: no slice ran, so the read costs nothing (#7075 review).
        await refund()
    return pending(planned.key)


def last_answer(row: PriceRow) -> StoreEstimate:
    """A ``done`` row as the instant read answers it when its key is not today's
    (#6921): the variant's last price, which the page shows marked as updating. A file
    listing's price read answers its listing's last row the same way while the colours'
    own slice runs (#6710, `file_prices.py` `listing_price`). Never a cart line's price
    (`line_prices.py` reads `firm_price` only)."""
    assert row.currency is not None
    return StoreEstimate(
        source="last",
        price_minor=row.total_minor,
        currency=row.currency,
        minor_exponent=minor_exponent(row.currency),
        price_key=row.price_key,
    )


async def instant_price(
    state: AppState, *, reference: Reference, newest: PriceRow | None
) -> StoreEstimate:
    """The price of a visitor's choices before any render (#6921), read only: it starts no
    render and no slice. ``newest`` is the variant's row (:meth:`SlicePrices.variant_row`,
    the row the preview and the cart add take too): it gives the 3MF's sha256 and the
    filament of each slot; with the shop's settings now (the same `planned_variant` inputs as the
    estimate: the book, the live rate, the profile, the printer's opening and the type
    presets) they give today's price key. When that key has a ``done`` row, it is the firm
    price (``slice``). Else the row's price is the last price (``last``). With no row,
    ``none``.

    The cost per read with a row (#6921 review): `planned_variant` loads the settings, the price
    book and the type presets from the database, then one more row read by key. The live
    rate and the printer's opening, its slow inputs, are already kept `RATE_TTL` in this
    process (`live_rate`, `live_opening`), so a read never asks the printer or Bambuddy
    again within that time. No further cache: the price book and the presets have no
    version to key it on, and a stale key would show an old price as firm."""
    orders = _orders(state)
    prices = orders.slice_prices
    if newest is None or newest.currency is None or newest.total_minor is None:
        return NO_PRICE
    filaments = (
        None
        if newest.slot_filaments is None
        else await asyncio.to_thread(book_filaments, orders, newest.slot_filaments)
    )
    planned = await planned_variant(
        state, reference=reference, model_sha256=newest.model_sha256, filaments=filaments
    )
    if planned is not None:
        if planned.key == newest.price_key:
            return answer_of(newest)
        row = await asyncio.to_thread(prices.get, planned.key)
        if row is not None and row.status == "done" and row.currency is not None:
            return answer_of(row)
    return last_answer(newest)


def book_filaments(orders: OrdersComponent, ids: Sequence[str]) -> list[Filament] | None:
    """The price-book filament of each id, or None when one is gone or archived. Blocking:
    call it in a thread."""
    found = [orders.store.filament(filament_id) for filament_id in ids]
    if any(record is None or record.archived_at is not None for record in found):
        return None
    return [record for record in found if record is not None]


async def _start(
    state: AppState,
    prices: SlicePrices,
    planned: Planned,
    claim_key: str,
    previous: PriceRow | None,
) -> bool:
    """The variant's slice started as its operation, which this claim's own key keys.
    A start that fails gives the claim back (:meth:`SlicePrices.release`), so the next
    read claims it again; one whose answer says the operation may still run keeps the
    row ``running``, so it still counts against the capacity, and `_settle_ended`
    settles it once the operation ends. False when the claim went back."""
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
            return True
        logger.warning("a storefront slice was not started: %s", error.detail)
        await asyncio.to_thread(prices.release, planned.key, claim_key, previous)
        return False
    except Exception:
        logger.warning("a storefront slice was not started", exc_info=True)
        await asyncio.to_thread(prices.release, planned.key, claim_key, previous)
        return False
    return True


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


#: The printer each registry printer was last read as, per `Printers` (one per app).
_openings: weakref.WeakKeyDictionary[Any, dict[str, tuple[float, PrinterOpening | None]]] = (
    weakref.WeakKeyDictionary()
)


async def live_opening(
    state: AppState, settings: StoredSettings, printer: str
) -> PrinterOpening | None:
    """``printer_opening`` of registry printer ``printer`` (#6574), read again only
    `RATE_TTL` after the last read in this process, as :func:`live_rate`: a page's
    re-reads while its slice runs ask the printer and Bambuddy nothing. A status that
    could not be read is not kept, so the next read tries again."""
    # Here, not at the top: the printers component imports the orders' modules.
    from scadbuddy.printers.component import PRINTERS

    printers = state.components.get(PRINTERS)
    now = time.monotonic()
    kept = _openings.setdefault(printers, {})
    found = kept.get(printer)
    if found is not None and now - found[0] < RATE_TTL:
        return found[1]
    opening = await printer_opening(settings, printers, printer)
    if opening is not None and opening.status is not None:
        kept[printer] = (now, opening)
    return opening
