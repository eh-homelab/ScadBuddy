"""A slice quote's steps (#4250, spec 2026-10-18 §5.2; plan Rulings 1-3, 6-8): the run of
the ``order_item_quote`` operation (``orders/operations.py``), each step a function the
tests call on its own.

The check (:func:`quote_check`) refuses from the rows alone and writes nothing: the
order open for items, the item not settled, every colour slot covered by a filament of
the book that is not archived once the body's are merged over the item's, and a book that
prices something. The run then writes, in this order, each after the operation's record
exists: the body's filaments onto the item (:func:`set_filaments`, Ruling 6), the output
the item renders to when it has none that matches (:func:`ensure_output`, Ruling 7), then
every plate's slice (:func:`slice_item`, the print dialog's own background slice, Ruling
2), the sliced files kept as blobs the quote holds before its row exists
(:func:`keep_sliced`, Ruling 3), each plate read (:func:`read_plate`), priced
(:func:`price_quote`) and recorded through the lifecycle.

Slots: the sliced file numbers its filament slots from 1, as the 3MF does
(``bambuddy/filaments.py``), and an item numbers its colour slots from 0
(``orders/reference.py`` ``colours_of``), so the sliced file's slot ``n`` is the item's
colour slot ``n - 1``. Everything an order records (``filaments``, a quote's ``plates``)
is in the item's numbering.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.preview import PreviewStarted
from scadbuddy.bambuddy.resolver import PrintChoices
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputMeta, OutputNotFoundError
from scadbuddy.orders.lifecycle import (
    OPEN_FOR_EDITS,
    QUOTE_HOLDER,
    QUOTE_TIMEOUT,
    SETTLED,
    Lifecycle,
    LifecycleError,
)
from scadbuddy.orders.models import (
    Actor,
    Filament,
    Order,
    OrderItem,
    PricingPolicy,
    Quote,
    Reference,
)
from scadbuddy.orders.pricing import Plate, PlateFilament, Priced, PricingError, price
from scadbuddy.orders.problems import PROBLEMS, refused
from scadbuddy.orders.reference import colours_of, is_commit_id
from scadbuddy.orders.store import OrderStore
from scadbuddy.render.inputs import inputs_key
from scadbuddy.render.job_models import Job
from scadbuddy.render.runner import with_defaults
from scadbuddy.render.schema import CustomizerSchema
from scadbuddy.slicing.slice_info import read_sliced
from scadbuddy.store import BlobStore
from scadbuddy.store.content_models import BlobScope
from scadbuddy.store.refs import BlobRefs

__all__ = ["QUOTE_TIMEOUT"]

logger = logging.getLogger(__name__)

#: How often a run reads a render or a slice it waits on (Ruling 7, Task 4).
FOLLOW_EVERY = 2.0
#: The file a quote's blob keeps.
SLICED_NAME = "sliced.3mf"
#: A sliced file past this is not kept (as the dialog's reads bound it).
MAX_SLICED_BYTES = 128 * 1024 * 1024


class SliceQuoteRequest(BaseModel):
    """`POST /orders/{id}/items/{item}/quote` (Ruling 2): the print dialog's own choices,
    which slice the item exactly as the dialog would print it, and the price-book
    filament of each of the item's colour slots (``{slot: filament_id}``, from 0 as
    ``colours[].slot``), which prices it. The spool slices; the filament prices; nothing
    checks that one is the other until phase 5 (§8.3)."""

    model_config = ConfigDict(extra="forbid")

    #: The printer to slice for, as the dialog's; omitted, the configured one.
    printer_id: int | None = None
    #: One spool per slot (``slot_id`` from 1, the dialog's).
    filament_plan: FilamentPlan
    choices: PrintChoices
    filaments: dict[int, str] = Field(default_factory=dict)


class QuoteError(ValueError):
    """A sliced plate the quote cannot price honestly (§5.2 step 3): a file that cannot
    be read, a plate it lacks, a slot at 0 g. Names the plate, and the slot."""

    def __init__(self, message: str, *, plate: int | None, slot: int | None = None) -> None:
        super().__init__(message)
        self.plate = plate
        self.slot = slot


def _unprocessable(field: str, message: str) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        message,
        type_=PROBLEMS + field,
        errors=[{"loc": ["body", field], "msg": message}],
    )


# -- the check (Ruling 8): the rows alone, nothing written -----------------------------------


def prices_nothing(policy: PricingPolicy, filaments: Sequence[Filament]) -> bool:
    """Every line would be 0: no machine rate, no setup fee, no labour, and every
    filament the item's slots name free. A markup of nothing is nothing."""
    rates = (policy.machine_rate_per_hour, policy.setup_fee, policy.labour_per_item)
    return all(Decimal(rate) == 0 for rate in rates) and all(
        Decimal(filament.cost_per_kg) == 0 for filament in filaments
    )


def merged_slots(item: OrderItem, filaments: Mapping[int, str]) -> dict[int, str | None]:
    """The item's slots with the body's filaments over them (Ruling 6), in memory."""
    slots: dict[int, str | None] = {slot.slot: slot.filament_id for slot in item.colours}
    if item.colours:
        slots.update({slot: filament_id for slot, filament_id in filaments.items()})
    else:
        # An item made by hand has no slots until its first render (`ensure_output`).
        slots = dict(filaments)
    return slots


def quote_check(
    store: OrderStore,
    policy: PricingPolicy,
    order_id: str,
    item_id: str,
    request: SliceQuoteRequest,
) -> dict[int, str | None]:
    """Ruling 8's refusals, from the rows: 404 for an unknown order or item, 409
    ``order_not_open`` unless the order is `draft` or `quoted`, 409 ``item_not_open``
    for a settled item, 422 ``filaments`` for a slot the item lacks, a slot with no
    filament once the body's are applied, or one unknown or archived, and 422
    ``price_book`` when nothing would price the item. Blocking; writes nothing. The
    merged slots, which the run writes first."""
    order = store.order(order_id)
    if order is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    item = store.item(item_id)
    if item is None or item.order_id != order.id:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no item {item_id!r} of order {order.number}")
    if order.status not in OPEN_FOR_EDITS:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"order {order.number} is {order.status}: an item is quoted while it is draft"
            " or quoted",
            type_=PROBLEMS + "order_not_open",
        )
    if item.status in SETTLED:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"item {item_id} is {item.status}",
            type_=PROBLEMS + "item_not_open",
        )
    known = {slot.slot for slot in item.colours}
    unknown = sorted(set(request.filaments) - known) if item.colours else []
    if unknown:
        raise _unprocessable(
            "filaments", f"the item has no colour slot {', '.join(map(str, unknown))}"
        )
    slots = merged_slots(item, request.filaments)
    empty = sorted(slot for slot, filament_id in slots.items() if filament_id is None)
    if empty:
        raise _unprocessable(
            "filaments", f"colour slot {empty[0]} has no filament to price it with"
        )
    named: list[Filament] = []
    for slot, filament_id in sorted(slots.items()):
        assert filament_id is not None
        record = store.filament(filament_id)
        if record is None:
            raise _unprocessable(
                "filaments", f"colour slot {slot}: no filament {filament_id} in the price book"
            )
        if record.archived_at is not None:
            raise _unprocessable(
                "filaments", f"colour slot {slot}: filament {filament_id} is archived"
            )
        named.append(record)
    if prices_nothing(policy, named):
        raise _unprocessable(
            "price_book",
            "the price book prices nothing: every rate is 0 and every filament this item"
            " names costs 0",
        )
    return slots


# -- the run's steps ---------------------------------------------------------------------------


def set_filaments(
    lifecycle: Lifecycle,
    order_id: str,
    item_id: str,
    filaments: Mapping[int, str],
    by: Actor,
) -> OrderItem:
    """The body's filaments onto the item (Ruling 6), the run's first write: a change
    drops the accepted quote as a reference change does; the item's own filaments sent
    again write nothing. Only the slots the item has: an item made by hand gets its
    slots, and these filaments, with its first render (:func:`ensure_output`)."""
    item = lifecycle.store.item(item_id)
    assert item is not None
    known = {slot.slot for slot in item.colours}
    mine = {slot: filament_id for slot, filament_id in filaments.items() if slot in known}
    if not mine:
        return item
    return lifecycle.update_item(order_id, item_id, by, filaments=mine)


class OutputsRead(Protocol):
    """What :func:`ensure_output` reads of an output (``OutputStore``'s)."""

    def get(self, output_id: str) -> OutputMeta: ...
    def inputs(self, output_id: str) -> dict[str, Any]: ...
    def arranged_from(self, output_id: str) -> list[str]: ...


class PolicySource(Protocol):
    """The book's policy as each quote reads it (``PriceBookReader.policy``)."""

    def policy(self) -> tuple[str, PricingPolicy, str]: ...


class Renders(Protocol):
    """What :func:`ensure_output` renders with: ``RenderService``'s submit and read."""

    async def submit(
        self,
        slug: str,
        params: Mapping[str, Any],
        *,
        model_version: str | None = ...,
        inputs: Mapping[str, Any] | None = ...,
        whole_inputs: bool = ...,
    ) -> Job: ...

    def read(self, job_id: str) -> Job: ...


#: A settled render's states other than `done` (`render/job_models.py` `JobState`).
_SETTLED_JOB = frozenset({"failed", "cancelled"})


def output_matches(
    outputs: OutputsRead,
    output_id: str | None,
    item: OrderItem,
    schema: CustomizerSchema | None = None,
) -> bool:
    """The output is there and its ``meta.json`` and inputs are the reference's (Ruling
    7): its ``slug``, ``model_version`` and normalised inputs. An arranged output records
    a content hash where a render records its commit, and the item pins the template's
    commit instead (#4565): no render of the reference remakes the arrangement, so its
    hash stands for the pinned commit.

    With ``schema`` (the commit's), both inputs are compared at its defaults where they
    omit one (#4774): an output saved before renders filled them records only the params
    it was sent, and is still the render of a reference that names its defaults."""
    if output_id is None:
        return False
    try:
        meta = outputs.get(output_id)
        inputs = outputs.inputs(output_id)
        arranged = not is_commit_id(meta.model_version) and bool(outputs.arranged_from(output_id))
    except (OutputNotFoundError, OSError, ValueError):
        return False
    reference = item.reference
    return (
        meta.slug == reference.slug
        and (meta.model_version == reference.commit or arranged)
        and _at_defaults(inputs, schema) == _at_defaults(reference.inputs, schema)
    )


def _at_defaults(inputs: Mapping[str, Any], schema: CustomizerSchema | None) -> dict[str, Any]:
    if schema is None:
        return dict(inputs)
    params = inputs.get("params", {})
    return {**inputs, "params": with_defaults(schema, params if isinstance(params, dict) else {})}


async def ensure_output(
    *,
    lifecycle: Lifecycle,
    outputs: OutputsRead,
    renders: Renders,
    create_output: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    order: Order,
    item: OrderItem,
    filaments: Mapping[int, str],
    by: Actor,
    follow_every: float = FOLLOW_EVERY,
    schema: CustomizerSchema | None = None,
) -> str:
    """The output the item slices (Ruling 7): its own while it matches the reference,
    else the reference rendered at its commit, waited on through the projection, saved
    as ``POST /outputs`` saves one (``create_output``, ``output_create``'s run) and set
    as the item's ``output_id``. An item with no colour slots takes the render's, with
    ``filaments`` on them. ``schema`` is the reference commit's, for `output_matches`."""
    if await asyncio.to_thread(output_matches, outputs, item.output_id, item, schema):
        assert item.output_id is not None
        return item.output_id
    reference = item.reference
    params = reference.inputs.get("params", {})
    pipeline = reference.render_key == inputs_key(
        reference.slug, reference.inputs, reference.commit
    )
    job = await renders.submit(
        reference.slug,
        params if isinstance(params, dict) else {},
        model_version=reference.commit,
        inputs=reference.inputs,
        whole_inputs=pipeline,
    )
    while job.state not in ("done", *_SETTLED_JOB):
        await asyncio.sleep(follow_every)
        job = await asyncio.to_thread(renders.read, job.id)
    if job.state != "done":
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"the item's render, job {job.id}, is {job.state}"
            + (f": {job.error}" if job.error else ""),
            job_id=job.id,
        )
    saved = await create_output(
        {
            "slug": reference.slug,
            "job_id": job.id,
            "index": 0,
            "inputs": reference.inputs,
            "name": f"{order.number} {item.title}",
        }
    )
    output_id = str(saved["id"])
    colours = saved.get("colors") or []
    try:
        await asyncio.to_thread(
            lambda: lifecycle.update_item(
                order.id,
                item.id,
                by,
                output_id=output_id,
                colours=colours_of(colours) if not item.colours else None,
                filaments=None if item.colours else dict(filaments) or None,
            )
        )
    except LifecycleError as error:
        raise refused(error) from None
    return output_id


@dataclass(frozen=True)
class SlicedPlate:
    """One plate's finished slice: Bambuddy's sliced library file, or a native slice's
    blob."""

    plate_id: int
    slicer: Literal["bambuddy", "native"]
    file_id: int | None = None
    blob_key: str | None = None


class Slicer(Protocol):
    """The print dialog's background slice, for a quote (Ruling 2)."""

    async def start(
        self, output_id: str, request: SliceQuoteRequest, subject: str
    ) -> list[PreviewStarted]: ...

    async def poll(self, started: PreviewStarted, subject: str) -> SlicedPlate | None:
        """The finished plate, ``None`` while it slices; a failure raises."""
        ...

    async def fetch(self, sliced: SlicedPlate) -> bytes: ...


async def slice_item(
    slicer: Slicer,
    output_id: str,
    request: SliceQuoteRequest,
    subject: str,
    *,
    follow_every: float = FOLLOW_EVERY,
) -> list[SlicedPlate]:
    """Every plate of the output sliced (``start_preview_plates``) and followed to its
    end, in plate order; a plate that fails fails the quote with the slicer's text."""
    started = await slicer.start(output_id, request, subject)
    sliced: list[SlicedPlate] = []
    for one in started:
        while (done := await slicer.poll(one, subject)) is None:
            await asyncio.sleep(follow_every)
        sliced.append(done)
    return sliced


def read_plate(
    payload: bytes, plate_id: int, *, blob_key: str | None = None, sha256: str | None = None
) -> Plate:
    """§5.2 step 3: ``plate_id``'s seconds and grams per slot from the sliced file, in
    the item's slot numbering. A file that cannot be read, a plate it lacks, a plate of
    no time or no filament, or a slot at 0 g (not sliced) is a :class:`QuoteError`
    naming the plate, so nothing is priced at nothing."""
    try:
        read = read_sliced(payload, plate_id)
    except ApiError:
        raise QuoteError(
            f"plate {plate_id}: the sliced file has no such plate", plate=plate_id
        ) from None
    if read.print_time_seconds is None and not read.grams_by_slot:
        raise QuoteError(f"plate {plate_id}: the sliced file could not be read", plate=plate_id)
    if not read.print_time_seconds:
        raise QuoteError(f"plate {plate_id}: the sliced file has no print time", plate=plate_id)
    if not read.grams_by_slot:
        raise QuoteError(f"plate {plate_id}: the sliced file uses no filament", plate=plate_id)
    for slot_id, grams in sorted(read.grams_by_slot.items()):
        if not grams:
            raise QuoteError(
                f"plate {plate_id}: colour slot {slot_id - 1} (slot {slot_id}) reads 0 g,"
                " so it was not sliced",
                plate=plate_id,
                slot=slot_id - 1,
            )
    return Plate(
        index=plate_id,
        print_seconds=read.print_time_seconds,
        filaments=[
            # The slicer's float grams enter Decimal through `str`, once (§3.6).
            PlateFilament(slot=slot_id - 1, used_g=Decimal(str(grams)))
            for slot_id, grams in sorted(read.grams_by_slot.items())
        ],
        blob_key=blob_key,
        sha256=sha256,
    )


def unsliced_slots(plates: Sequence[Plate], item: OrderItem) -> None:
    """Every colour slot of the item is used on some plate: one no plate reads grams for
    was not sliced (a 0 g slot is left out of ``slice_info.config``'s read,
    ``bambuddy/filaments3mf.py``), and is never priced at nothing."""
    used = {filament.slot for plate in plates for filament in plate.filaments}
    for slot in item.colours:
        if slot.slot not in used:
            raise QuoteError(
                f"colour slot {slot.slot} (slot {slot.slot + 1}) reads 0 g on every plate,"
                " so it was not sliced",
                plate=None,
                slot=slot.slot,
            )


def sliced_key(sha256: str) -> str:
    """The blob key a quote keeps a sliced file under (Ruling 3)."""
    return f"quote-{sha256}"


def _write_sliced(blobs: BlobStore, key: str, payload: bytes) -> Path:
    """Written under a name of its own and moved into place (``os.replace``, as
    ``store/local.py`` writes), so two quotes of byte-identical slices writing the same
    key at once never leave a half-written ``sliced.3mf`` for the other to publish."""
    directory = blobs.dir_for(key)
    path = directory / SLICED_NAME
    staging = directory / f".{SLICED_NAME}.{uuid.uuid4().hex}"
    staging.write_bytes(payload)
    os.replace(staging, path)
    return path


async def keep_sliced(
    blobs: BlobStore,
    refs: BlobRefs,
    payload: bytes,
    quote_id: str,
    scope: BlobScope,
) -> tuple[str, str]:
    """The sliced file kept as ``quote-<sha256>/sliced.3mf`` (published as its template's
    blob), then held by ``(order_item_quote, quote_id)`` before the quote's row exists
    (Ruling 3): a crash after the publish leaves a blob the sweep frees, one after the
    add a ref the reaper drops, never a row whose bytes can go. Its key and digest."""
    sha256 = hashlib.sha256(payload).hexdigest()
    key = sliced_key(sha256)
    await asyncio.to_thread(_write_sliced, blobs, key, payload)
    await blobs.publish(key, scope=scope)
    await asyncio.to_thread(refs.add, key, QUOTE_HOLDER, quote_id)
    return key, sha256


def price_quote(
    plates: Sequence[Plate],
    item: OrderItem,
    store: OrderStore,
    policy: PricingPolicy,
    *,
    currency: str,
) -> Priced:
    """Task 2's arithmetic over the item's filaments as they are now, the book's policy
    copied into the lines. A slot with no filament is a 422 naming it."""
    filaments: dict[int, Filament] = {}
    for slot in item.colours:
        if slot.filament_id is not None:
            record = store.filament(slot.filament_id)
            if record is not None:
                filaments[slot.slot] = record
    try:
        return price(plates, filaments, policy, currency=currency, quantity=item.quantity)
    except PricingError as error:
        raise _unprocessable("filaments", str(error)) from None


@dataclass
class QuoteRun:
    """What a quote's run reads, from the state as every kind does
    (``operations/kinds.py``)."""

    lifecycle: Lifecycle
    book: PolicySource
    outputs: OutputsRead
    renders: Renders
    create_output: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
    slicer: Slicer
    blobs: BlobStore
    refs: BlobRefs
    follow_every: float = FOLLOW_EVERY
    #: The schema of a reference's commit (None for a pipeline template, or none to
    #: read), so `output_matches` compares inputs at their defaults (#4774).
    schema_of: Callable[[Reference], Awaitable[CustomizerSchema | None]] | None = None


def subject_of(item_id: str) -> str:
    """The quote's subject (§5.4's, Ruling 1): an item's quote and its print are the
    same subject's work."""
    return f"order_item:{item_id}"


async def quote_run(
    run: QuoteRun, order_id: str, item_id: str, request: SliceQuoteRequest, by: Actor
) -> Quote:
    """The whole run, in the order the module docstring gives."""
    lifecycle = run.lifecycle
    store = lifecycle.store
    try:
        await asyncio.to_thread(set_filaments, lifecycle, order_id, item_id, request.filaments, by)
    except LifecycleError as error:
        raise refused(error) from None
    order = await asyncio.to_thread(store.order, order_id)
    item = await asyncio.to_thread(store.item, item_id)
    if order is None or item is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no item {item_id!r} of order {order_id!r}")
    schema = await run.schema_of(item.reference) if run.schema_of is not None else None
    output_id = await ensure_output(
        lifecycle=lifecycle,
        outputs=run.outputs,
        renders=run.renders,
        create_output=run.create_output,
        order=order,
        item=item,
        filaments=request.filaments,
        by=by,
        follow_every=run.follow_every,
        schema=schema,
    )
    subject = subject_of(item_id)
    sliced = await slice_item(
        run.slicer, output_id, request, subject, follow_every=run.follow_every
    )
    scope = BlobScope(slug=item.reference.slug, title=item.reference.template_name)
    quote_id = uuid.uuid4().hex
    held = False
    try:
        plates: list[Plate] = []
        for plate in sliced:
            payload = await run.slicer.fetch(plate)
            # Read before it is kept: a file that cannot be priced is never held.
            read_plate(payload, plate.plate_id)
            held = True
            key, sha256 = await keep_sliced(run.blobs, run.refs, payload, quote_id, scope)
            plates.append(read_plate(payload, plate.plate_id, blob_key=key, sha256=sha256))
        item = await asyncio.to_thread(store.item, item_id)
        assert item is not None
        unsliced_slots(plates, item)
        _, policy, _ = await asyncio.to_thread(run.book.policy)
        priced = await asyncio.to_thread(
            price_quote, plates, item, store, policy, currency=order.currency
        )
        first = plates[0]
        assert first.sha256 is not None and first.blob_key is not None
        try:
            return await asyncio.to_thread(
                lambda: lifecycle.record_slice_quote(
                    order_id,
                    item_id,
                    priced,
                    plates,
                    sliced_sha256=first.sha256 or "",
                    sliced_blob_key=first.blob_key or "",
                    by=by,
                    quote_id=quote_id,
                )
            )
        except LifecycleError as error:
            raise refused(error) from None
    except QuoteError as error:
        await _drop(run.refs, quote_id, held)
        extensions: dict[str, Any] = {}
        if error.plate is not None:
            extensions["plate"] = error.plate
        if error.slot is not None:
            extensions["slot"] = error.slot
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            str(error),
            type_=PROBLEMS + "sliced_plate",
            **extensions,
        ) from None
    except BaseException:
        await _drop(run.refs, quote_id, held)
        raise


async def _drop(refs: BlobRefs, quote_id: str, held: bool) -> None:
    """A run that fails after its first add drops its own holder before it raises
    (Ruling 3); one the drop misses is the reaper's. Off the loop, as every other store
    call of the run: a slow database must not stall the worker."""
    if not held:
        return
    try:
        await asyncio.to_thread(refs.drop_holder, QUOTE_HOLDER, quote_id)
    except Exception:
        logger.exception("could not drop a failed quote's hold", extra={"id": quote_id})
