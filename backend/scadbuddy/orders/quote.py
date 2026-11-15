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

A library file item (#5544) names a file of the library, not a template: the run checks
the file is still there (:class:`LibraryFiles`, 409 ``library_file_gone`` when it is not),
slices it as the dialog slices a library file (``Slicer.start_library``), gives the item
the slots its plates use when it has none (:func:`library_colours`), and records a quote
that names no revision, since such an item has none.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
import zipfile
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from fastapi import status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.part_settings import PartSettingsMap, dumped
from scadbuddy.bambuddy.preview import PreviewStarted
from scadbuddy.bambuddy.prime_tower import PrimeTowerChoice
from scadbuddy.bambuddy.resolver import PrintChoices
from scadbuddy.bambuddy.supports import SupportsChoice
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import PrintSequence
from scadbuddy.library.outputs import OutputMeta, OutputNotFoundError
from scadbuddy.operations.kinds import to_thread_to_end
from scadbuddy.orders.lifecycle import (
    QUOTE_HOLDER,
    QUOTE_TIMEOUT,
    SETTLED,
    Lifecycle,
    LifecycleError,
    check_submitted,
    requote_open,
)
from scadbuddy.orders.models import (
    Actor,
    ColourSlot,
    Filament,
    LibraryFileRef,
    MachineRateBreakdown,
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
    #: The registry printer (#4551), as the print dialog's run names it: sent with
    #: ``printer_id`` it must link that Bambuddy printer, and alone it names a printer
    #: only the LAN reaches. A printer a run reaches over the LAN is sliced for with no
    #: Bambuddy call (plan 2026-10-18 D2, D3).
    printer: str | None = Field(default=None, max_length=64)
    #: One spool per slot (``slot_id`` from 1, the dialog's).
    filament_plan: FilamentPlan
    choices: PrintChoices
    filaments: dict[int, str] = Field(default_factory=dict)
    #: The path the item's print will take (#5352, `PrintRunRequest.path`), so the quote
    #: slices on the run's route and the print can queue that slice. Omitted is the
    #: run's own choice.
    path: Literal["bambuddy", "native"] | None = None
    #: Slicer settings for each slot's parts (#5571, `PrintRunRequest.part_settings`):
    #: recorded with the quote, and the item's print prints with them when it sends none.
    part_settings: PartSettingsMap | None = None
    #: The price-book filament of each sliced slot that is no colour slot of the item
    #: (``{slot: filament_id}``, from 0 as ``filaments``): the support or support
    #: interface filament (#4625, order operations spec §6.3), priced as its own line
    #: from the slice's grams. Never written onto the item.
    support_filaments: dict[int, str] = Field(default_factory=dict)
    #: The prime tower, supports and print sequence (#4673, order operations spec §5.1),
    #: in `PrintRunRequest`'s own shapes: the quote slices with them and records them,
    #: so the print slices what was priced. Omitted is the template's, as the print's.
    prime_tower: PrimeTowerChoice | None = None
    supports: SupportsChoice | None = None
    print_sequence: PrintSequence | None = None


class ItemSliceQuoteRequest(SliceQuoteRequest):
    """The quote route's body (#4673 PR 3, order operations spec §5.1): a slice quote's,
    and the design to quote. With ``inputs`` or ``commit`` (each left out is the item's
    current one) that differ from the item's, the route first makes a revision of them
    through the revise path, then quotes under it. Not part of the operation's request:
    the revision is, as ``revision_id``."""

    inputs: dict[str, Any] | None = None
    #: A commit id, as a revision's reference takes it (7 to 40 hex characters).
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")


#: The fields of a slice quote's request that are its print setup (#4673): what the print
#: sends to slice what was priced. `filaments` prices and `filament_plan` has a column
#: of its own; `part_settings` has its own since #5571.
SETUP_FIELDS = frozenset(
    {"printer_id", "printer", "path", "choices", "prime_tower", "supports", "print_sequence"}
)


def print_setup(request: SliceQuoteRequest) -> dict[str, Any]:
    """The quote's ``choices`` column (#4673): the request's print setup as JSON, every
    field present (an omitted one as null), in ``PrintRunRequest``'s shapes."""
    return request.model_dump(mode="json", include=set(SETUP_FIELDS))


class ArchiveQuoteRequest(BaseModel):
    """`POST /orders/{id}/items/{item}/quotes/archive` (#4777, order operations spec
    §3.2): the past print to price the item from (``archive_id``: one the item was taken
    from, or one its output or library file printed; omitted, the first archive it was
    taken from) and the price-book filament of each colour slot, as a slice quote's
    ``filaments``. ``filament_id`` prices every slot the print used that ``filaments``
    does not name, for an item with no slots yet (a library file item)."""

    model_config = ConfigDict(extra="forbid")

    archive_id: int | None = Field(default=None, ge=1)
    filaments: dict[int, str] = Field(default_factory=dict)
    filament_id: str | None = None


class QuoteError(ValueError):
    """A sliced plate the quote cannot price honestly (§5.2 step 3): a file that cannot
    be read, a plate it lacks, a slot at 0 g. Names the plate, and the slot."""

    def __init__(self, message: str, *, plate: int | None, slot: int | None = None) -> None:
        super().__init__(message)
        self.plate = plate
        self.slot = slot


def reference_of(item: OrderItem) -> Reference:
    """What the item names to make. A custom item (#4954) names nothing, so there is
    nothing to render or slice: 409 ``item_custom``, and its price is typed instead. A
    request has none until its first revision: 409 ``item_has_no_design``."""
    if item.reference is None:
        if item.kind == "request":
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"item {item.id} is a request with no design yet: add a revision first",
                type_=PROBLEMS + "item_has_no_design",
                items=[item.id],
            )
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"item {item.id} is a custom item: it has no model to render or slice, so its"
            " price is typed (PATCH the item's unit_price_minor)",
            type_=PROBLEMS + "item_custom",
            items=[item.id],
        )
    return item.reference


def check_design(item: OrderItem) -> None:
    """:func:`reference_of`'s refusals, except for a library file item (#5544), which
    names its file instead of a reference."""
    if item.library_file is None:
        reference_of(item)


def library_file_gone(item_id: str, file: LibraryFileRef) -> ApiError:
    """409 ``library_file_gone``: the library file a library file item names was deleted,
    or is not in the library that serves now (#5544)."""
    where = "Bambuddy's library" if file.library == "bambuddy" else "ScadBuddy's library"
    return ApiError(
        status.HTTP_409_CONFLICT,
        f"item {item_id}'s library file {file.filename} (id {file.id}) is no longer in"
        f" {where}: it was deleted, or the library that serves changed. Add the file again"
        " as a new item",
        type_=PROBLEMS + "library_file_gone",
        items=[item_id],
        library_file_id=file.id,
    )


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


def check_filaments(store: OrderStore, slots: Mapping[int, str]) -> list[Filament]:
    """422 ``filaments`` when a slot names a filament the price book lacks, or one that is
    archived. Blocking. The filaments, in slot order."""
    named: list[Filament] = []
    for slot, filament_id in sorted(slots.items()):
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
    return named


def quote_check(
    store: OrderStore,
    policy: PricingPolicy,
    order_id: str,
    item_id: str,
    request: SliceQuoteRequest | ArchiveQuoteRequest,
    *,
    expected_revision_id: str | None = None,
    body_only: bool = False,
) -> dict[int, str | None]:
    """Ruling 8's refusals, from the rows (with ``expected_revision_id``, the revision a
    slice's new inputs made, 409 ``revision_superseded`` once another is current, #4673):
    With ``body_only`` (a slice whose new design the route revises first, #5761
    review) the item's state is the revise path's to refuse, and so are the new design's
    slots, known only once it renders (the run's): this checks the cart, that the item
    has a design (``check_design``), the filaments the body names, each in the book and
    not archived, and the price book. The item's pinned filaments go with the revision,
    so the run judges those it still uses.
    404 for an unknown order or item, 409
    ``order_not_open`` unless the order is `draft` or `quoted` (or the item has no
    accepted quote on an order that takes items after acceptance, #5349, or was revised after
    acceptance, #5350), 409 ``item_not_open``
    for a settled item, 422 ``filaments`` for a slot the item lacks, a slot with no
    filament once the body's are applied, or one unknown or archived, and 422
    ``price_book`` when nothing would price the item, and 409 ``order_not_submitted`` for
    a storefront cart (#5282). Blocking; writes nothing. The merged slots, which the run
    writes first."""
    order = store.order(order_id)
    if order is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    try:
        check_submitted(order)
    except LifecycleError as error:
        raise refused(error) from None
    item = store.item(item_id)
    if item is None or item.order_id != order.id:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no item {item_id!r} of order {order.label}")
    if not body_only and not requote_open(order, item, store.quotes(item.id)):
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"order {order.label} is {order.status}: an item is quoted while the order is"
            " draft or quoted, or, past that, while the item has no accepted quote or was"
            " revised after acceptance",
            type_=PROBLEMS + "order_not_open",
        )
    if not body_only and item.status in SETTLED:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"item {item_id} is {item.status}",
            type_=PROBLEMS + "item_not_open",
        )
    check_design(item)
    if expected_revision_id is not None and item.current_revision_id != expected_revision_id:
        raise refused(_superseded(item_id))
    known = {slot.slot for slot in item.colours}
    # Before a new design's revision its slots are not known yet: only the filaments
    # named are judged here, the slots by the run once it renders (#5761 review).
    unknown = sorted(set(request.filaments) - known) if item.colours and not body_only else []
    if unknown:
        raise _unprocessable(
            "filaments", f"the item has no colour slot {', '.join(map(str, unknown))}"
        )
    slots = merged_slots(item, request.filaments)
    empty = sorted(slot for slot, filament_id in slots.items() if filament_id is None)
    if empty and not body_only:
        raise _unprocessable(
            "filaments", f"colour slot {empty[0]} has no filament to price it with"
        )
    judged = dict(request.filaments) if body_only else slots
    named = check_filaments(store, {slot: f for slot, f in judged.items() if f is not None})
    if body_only:
        # The pins the revision carries price it too, when they still stand.
        pinned = (store.filament(f) for slot, f in slots.items() if f and slot not in judged)
        named = [
            *named,
            *(record for record in pinned if record is not None and record.archived_at is None),
        ]
    if prices_nothing(policy, named):
        raise _unprocessable(
            "price_book",
            "the price book prices nothing: every rate is 0 and every filament this item"
            " names costs 0",
        )
    return slots


def _superseded(item_id: str) -> LifecycleError:
    """409 ``revision_superseded`` (#4673): the slice was made for a revision (its new
    inputs') that is no longer the item's current one."""
    return LifecycleError(
        "revision_superseded",
        f"item {item_id} has a newer revision than the one this slice was made for: slice it again",
        items=[item_id],
    )


#: After a slice's new design rendered (#5761 review): the revision stands, so the way
#: out is the same slice without that support slot.
RENDERED_SUPPORT_HINT = (
    ": the new design's render made it a colour slot, and its revision stays;"
    " send the slice again without that support slot"
)


def support_check(
    store: OrderStore,
    item_id: str,
    request: SliceQuoteRequest,
    *,
    colours: Collection[int] | None = None,
    hint: str = "",
) -> None:
    """422 ``support_filaments`` for a support slot that is a colour slot of the item (its
    filament is the slot's own), or below 0, or a support filament unknown or archived
    (§6.3). Blocking; writes nothing. ``colours`` stands in for the item's slots: before a
    slice's new design renders, the slots its body names (#5761 review). ``hint`` follows
    a colour slot's refusal."""
    item = store.item(item_id)
    if item is None:
        return
    if colours is None:
        colours = {slot.slot for slot in item.colours}
    for slot, filament_id in sorted(request.support_filaments.items()):
        if slot < 0 or slot in colours:
            raise _unprocessable(
                "support_filaments",
                f"colour slot {slot} is priced with its own filament, not as support{hint}"
                if slot in colours
                else f"slot {slot} is no slot",
            )
        record = store.filament(filament_id)
        if record is None:
            raise _unprocessable(
                "support_filaments", f"slot {slot}: no filament {filament_id} in the price book"
            )
        if record.archived_at is not None:
            raise _unprocessable(
                "support_filaments", f"slot {slot}: filament {filament_id} is archived"
            )


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
    return output_id is not None and _matching(outputs, output_id, item, schema) is not None


def _matching(
    outputs: OutputsRead, output_id: str, item: OrderItem, schema: CustomizerSchema | None
) -> OutputMeta | None:
    """The output's ``meta.json`` when it matches (:func:`output_matches`), from one
    read of it."""
    try:
        meta = outputs.get(output_id)
        inputs = outputs.inputs(output_id)
        arranged = not is_commit_id(meta.model_version) and bool(outputs.arranged_from(output_id))
    except (OutputNotFoundError, OSError, ValueError):
        return None
    reference = item.reference
    if reference is None:
        return None
    if (
        meta.slug == reference.slug
        and (meta.model_version == reference.commit or arranged)
        and _at_defaults(inputs, schema) == _at_defaults(reference.inputs, schema)
    ):
        return meta
    return None


def _at_defaults(inputs: Mapping[str, Any], schema: CustomizerSchema | None) -> dict[str, Any]:
    if schema is None:
        return dict(inputs)
    params = inputs.get("params", {})
    if not isinstance(params, dict):
        # A malformed row is compared exactly, never read as an all-defaults render.
        return dict(inputs)
    return {**inputs, "params": with_defaults(schema, params)}


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
    revision_id: str | None = None,
    saved_as: str | None = None,
    carried: Mapping[int, str] | None = None,
) -> str:
    """The output the item slices (Ruling 7): its own while it matches the reference,
    else the reference rendered at its commit, waited on through the projection, saved
    as ``POST /outputs`` saves one (``create_output``, ``output_create``'s run) and set
    as the item's ``output_id``. An item with no colour slots takes the render's, with
    ``filaments`` on them. ``schema`` is the reference commit's, for `output_matches`.
    With ``revision_id`` (the quote's run) the render is of that revision: once the
    item has another, nothing is written and the run is refused `revision_not_newest`.
    ``saved_as`` is the id a new output is saved under (:func:`operation_output_id`,
    #5390): an output of that id that matches is one an earlier attempt of the same
    operation saved before it stopped, and is set on the item without a second render
    or save. The save claims the id first (``OutputStore.claim``): a directory already
    there (another attempt's save still writing, or an output that does not match) is
    left alone, and the new output gets an id of its own. ``carried`` (#4673, a slice's
    new design) are the filaments the item had pinned before that revision: the fill
    puts each on the rendered slot of its number, under ``filaments``, and drops the
    rest."""
    if await asyncio.to_thread(output_matches, outputs, item.output_id, item, schema):
        assert item.output_id is not None
        return item.output_id
    if saved_as is not None:
        earlier = await asyncio.to_thread(_matching, outputs, saved_as, item, schema)
        if earlier is not None:
            return await _set_output(
                lifecycle,
                order,
                item,
                saved_as,
                list(earlier.colors),
                filaments,
                by,
                revision_id,
                carried,
            )
    reference = reference_of(item)
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
            "name": f"{order.label} {item.title}",
            **({"output_id": saved_as} if saved_as is not None else {}),
        }
    )
    return await _set_output(
        lifecycle,
        order,
        item,
        str(saved["id"]),
        saved.get("colors") or [],
        filaments,
        by,
        revision_id,
        carried,
    )


async def _set_output(
    lifecycle: Lifecycle,
    order: Order,
    item: OrderItem,
    output_id: str,
    colours: Sequence[str],
    filaments: Mapping[int, str],
    by: Actor,
    revision_id: str | None,
    carried: Mapping[int, str] | None = None,
) -> str:
    """The item's ``output_id`` set to the saved output, with its colour slots when the
    item has none: ``filaments`` on them, over the ``carried`` ones the render has."""
    slots = colours_of(colours)
    rendered = {slot.slot for slot in slots}
    fill = {slot: f for slot, f in (carried or {}).items() if slot in rendered}
    fill.update(filaments)
    try:
        await asyncio.to_thread(
            lambda: lifecycle.update_item(
                order.id,
                item.id,
                by,
                output_id=output_id,
                colours=slots if not item.colours else None,
                filaments=None if item.colours else fill or None,
                expected_revision_id=revision_id,
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

    async def start_library(
        self, file_id: int, request: SliceQuoteRequest, subject: str
    ) -> list[PreviewStarted]:
        """Every plate of a library file (#5544), as :meth:`start` slices an output's."""
        ...


class LibraryFiles(Protocol):
    """What a library file item's quote reads of the library that serves (#5544)."""

    async def exists(self, file_id: int) -> bool:
        """Whether the file is in the library that serves; false for a deleted file and
        for one of the other library. Any other failure (Bambuddy away) raises."""
        ...

    async def colours(self, file_id: int) -> dict[int, str]:
        """The file's slot colours as ``#RRGGBB``, by its slot id (from 1)."""
        ...


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
    return await _follow(slicer, started, subject, follow_every)


async def _follow(
    slicer: Slicer, started: list[PreviewStarted], subject: str, follow_every: float
) -> list[SlicedPlate]:
    """Each started plate followed to its end, in plate order."""
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


def library_colours(
    plates: Sequence[Plate], colours: Mapping[int, str], filaments: Mapping[int, str]
) -> list[ColourSlot]:
    """A library file item's slots from its first slice quote (#5544): every slot a
    plate uses, from 0, coloured as the file names it (``colours`` by slot id from 1; grey
    when it names none), priced with the body's filament."""
    used = sorted({filament.slot for plate in plates for filament in plate.filaments})
    return [
        ColourSlot(
            slot=slot,
            colour=file_colour(colours.get(slot + 1)) or "#808080",
            filament_id=filaments.get(slot),
        )
        for slot in used
    ]


def file_colour(colour: str | None) -> str | None:
    """``#RRGGBB`` of a colour the file names (``#RRGGBBAA`` drops its alpha)."""
    if colour is None:
        return None
    text = colour if colour.startswith("#") else f"#{colour}"
    if len(text) in (7, 9) and all(c in "0123456789abcdefABCDEF" for c in text[1:]):
        return text[:7].upper()
    return None


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


async def plate_image(blobs: BlobStore, quote: Quote, plate: int) -> bytes | None:
    """The image a slice quote's sliced file holds for ``plate`` (``Metadata/plate_<n>.png``,
    #4673 PR 4): the quote's "Print setups" preview. None for a plate the quote lacks, a
    file gone or with no such image."""
    found = next((row for row in quote.plates if row.get("index") == plate), None)
    key = found.get("blob_key") if found is not None else None
    if not isinstance(key, str) or not await blobs.fetch(key):
        return None

    def read() -> bytes | None:
        try:
            with zipfile.ZipFile(blobs.dir_for(key) / SLICED_NAME) as archive:
                return archive.read(f"Metadata/plate_{plate}.png")
        except (FileNotFoundError, KeyError, zipfile.BadZipFile):
            return None

    return await asyncio.to_thread(read)


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
    await asyncio.to_thread(refs.hold, key, QUOTE_HOLDER, quote_id)
    return key, sha256


def price_quote(
    plates: Sequence[Plate],
    item: OrderItem,
    store: OrderStore,
    policy: PricingPolicy,
    *,
    currency: str,
    support: Mapping[int, str] | None = None,
) -> Priced:
    """Task 2's arithmetic over the item's filaments as they are now, the book's policy
    copied into the lines. ``support`` prices each slot that is no colour slot of the
    item with its own line (§6.3). A slot with no filament, or a support slot no plate
    uses, is a 422 naming it."""
    filaments: dict[int, Filament] = {}
    for slot in item.colours:
        if slot.filament_id is not None:
            record = store.filament(slot.filament_id)
            if record is not None:
                filaments[slot.slot] = record
    used = {filament.slot for plate in plates for filament in plate.filaments}
    for number in sorted(support or {}):
        if number not in used and number not in filaments:
            raise _unprocessable(
                "support_filaments",
                f"slot {number} (slot {number + 1}) is used on no sliced plate; a support"
                " filament is given only for a slot the slice uses",
            )
    supports: set[int] = set()
    for number, filament_id in (support or {}).items():
        record = store.filament(filament_id)
        # One archived since the check is refused as an unpriced slot (``price``).
        if number not in filaments and record is not None and record.archived_at is None:
            filaments[number] = record
            supports.add(number)
    try:
        return price(
            plates,
            filaments,
            policy,
            currency=currency,
            quantity=item.quantity,
            support=supports,
        )
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
    #: The library that serves, for a library file item's quote (#5544).
    library: LibraryFiles | None = None
    #: The machine rate of the printer the request slices for (``printer``,
    #: ``printer_id``; ``MachineRates.for_quote``, #4625); None prices at the book's rate.
    machine_rate: (
        Callable[[str | None, int | None], Awaitable[MachineRateBreakdown | None]] | None
    ) = None


def operation_quote_id(operation: str | None) -> str:
    """The id a quote's run records its quote under (#5353): the operation's (its
    workflow id, the same on every attempt), so an attempt that runs again after its
    worker stopped (a rollout) finds the quote an earlier attempt recorded. A run
    outside an operation gets one of its own."""
    if operation is None:
        return uuid.uuid4().hex
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scadbuddy:order_item_quote:{operation}").hex


def operation_output_id(quote_id: str) -> str:
    """The id a quote's run saves a new output under (#5390): one per quote id, so per
    operation (:func:`operation_quote_id`). An attempt that runs again after its worker
    stopped between the save and the item's ``output_id`` write finds the output the
    earlier attempt saved (:func:`ensure_output`) instead of saving a second."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scadbuddy:order_item_quote_output:{quote_id}").hex


def subject_of(item_id: str) -> str:
    """The quote's subject (§5.4's, Ruling 1): an item's quote and its print are the
    same subject's work."""
    return f"order_item:{item_id}"


async def quote_run(
    run: QuoteRun,
    order_id: str,
    item_id: str,
    request: SliceQuoteRequest,
    by: Actor,
    *,
    quote_id: str | None = None,
    expected_revision_id: str | None = None,
    carried_filaments: Mapping[int, str] | None = None,
) -> Quote:
    """The whole run, in the order the module docstring gives. ``quote_id`` is the id
    every attempt of one operation records under (:func:`operation_quote_id`): an
    attempt that finds it recorded answers with it and does nothing (#5353). With
    ``expected_revision_id`` (#4673) it quotes that revision or, once another is
    current, nothing (``revision_superseded``): checked when it reads the item, and again
    by the record under the order's lock (its ``revision_not_newest``, named so here).
    ``carried_filaments`` are the item's filaments before that revision, which the
    render's slots take where the body names none (:func:`ensure_output`)."""
    lifecycle = run.lifecycle
    store = lifecycle.store
    quote_id = quote_id or uuid.uuid4().hex
    recorded = await asyncio.to_thread(store.quote, quote_id)
    if recorded is not None and recorded.order_item_id == item_id:
        return recorded
    try:
        await asyncio.to_thread(set_filaments, lifecycle, order_id, item_id, request.filaments, by)
    except LifecycleError as error:
        raise refused(error) from None
    order = await asyncio.to_thread(store.order, order_id)
    item = await asyncio.to_thread(store.item, item_id)
    if order is None or item is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no item {item_id!r} of order {order_id!r}")
    if item.library_file is not None:
        return await _library_quote(run, order, item, item.library_file, request, by, quote_id)
    reference = reference_of(item)
    try:
        # The revision the run quotes, read before the render: a revision made while
        # it renders or slices refuses the quote (`ensure_output`, `record_slice_quote`).
        item = await asyncio.to_thread(lifecycle.ensure_revision, order_id, item_id, by)
    except LifecycleError as error:
        raise refused(error) from None
    revision_id = item.current_revision_id
    if revision_id is None:
        raise refused(LifecycleError("item_has_no_design", f"item {item_id} has no design yet"))
    if expected_revision_id is not None and revision_id != expected_revision_id:
        raise refused(_superseded(item_id))
    schema = await run.schema_of(reference) if run.schema_of is not None else None
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
        revision_id=revision_id,
        saved_as=operation_output_id(quote_id),
        carried=carried_filaments,
    )
    if expected_revision_id is not None:
        # A slice's new design has its colour slots only now: its support slots are
        # judged against them here (#5761 review).
        await asyncio.to_thread(
            lambda: support_check(store, item_id, request, hint=RENDERED_SUPPORT_HINT)
        )
    subject = subject_of(item_id)
    sliced = await slice_item(
        run.slicer, output_id, request, subject, follow_every=run.follow_every
    )
    scope = BlobScope(slug=reference.slug, title=reference.template_name)
    try:
        return await _priced(
            run,
            order,
            item_id,
            sliced,
            scope,
            by,
            quote_id,
            revision_id,
            part_settings=dumped(request.part_settings),
            request=request,
        )
    except ApiError as error:
        if expected_revision_id is not None and error.type == PROBLEMS + "revision_not_newest":
            raise refused(_superseded(item_id)) from None
        raise


async def _library_quote(
    run: QuoteRun,
    order: Order,
    item: OrderItem,
    file: LibraryFileRef,
    request: SliceQuoteRequest,
    by: Actor,
    quote_id: str,
) -> Quote:
    """A library file item's quote (#5544): the file still there, sliced, its slots
    given to an item that has none, then priced and recorded as any quote, naming no
    revision."""
    library = run.library
    if library is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "this run cannot read the library")
    if not await library.exists(file.id):
        raise library_file_gone(item.id, file)
    subject = subject_of(item.id)
    started = await run.slicer.start_library(file.id, request, subject)
    sliced = await _follow(run.slicer, started, subject, run.follow_every)
    scope = BlobScope(slug=None, title=file.filename)

    async def recolour(plates: Sequence[Plate]) -> None:
        current = await asyncio.to_thread(run.lifecycle.store.item, item.id)
        if current is None or current.colours:
            return
        colours = library_colours(plates, await library.colours(file.id), request.filaments)
        try:
            await asyncio.to_thread(
                lambda: run.lifecycle.update_item(order.id, item.id, by, colours=colours)
            )
        except LifecycleError as error:
            raise refused(error) from None

    return await _priced(
        run,
        order,
        item.id,
        sliced,
        scope,
        by,
        quote_id,
        None,
        recolour,
        part_settings=dumped(request.part_settings),
        request=request,
    )


async def _priced(
    run: QuoteRun,
    order: Order,
    item_id: str,
    sliced: Sequence[SlicedPlate],
    scope: BlobScope,
    by: Actor,
    quote_id: str,
    revision_id: str | None,
    recolour: Callable[[Sequence[Plate]], Awaitable[None]] | None = None,
    *,
    part_settings: dict[str, dict[str, Any]] | None = None,
    request: SliceQuoteRequest,
) -> Quote:
    """Each sliced plate fetched, read, kept and priced, then the quote recorded (or
    the run's holds dropped as it fails). ``recolour`` gives an item with no slots the
    ones its plates use, before they are checked (a library file item, #5544).
    ``part_settings`` are the settings per slot it sliced with (#5571), and
    ``request``'s print setup and spools (#4673) are recorded with it too. Machine time
    is priced at the rate of the printer ``request`` sliced for, recorded with each part
    of it (#4625), and its support slots with their own filaments."""
    lifecycle = run.lifecycle
    store = lifecycle.store
    order_id = order.id
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
        if recolour is not None:
            await recolour(plates)
        item = await asyncio.to_thread(store.item, item_id)
        assert item is not None
        unsliced_slots(plates, item)
        _, policy, _ = await asyncio.to_thread(run.book.policy)
        rate = None
        if run.machine_rate is not None:
            rate = await run.machine_rate(request.printer, request.printer_id)
        if rate is not None:
            policy = policy.model_copy(update={"machine_rate_per_hour": rate.rate_per_hour})
        priced = await asyncio.to_thread(
            lambda: price_quote(
                plates,
                item,
                store,
                policy,
                currency=order.currency,
                support=request.support_filaments,
            )
        )
        first = plates[0]
        assert first.sha256 is not None and first.blob_key is not None
        try:
            # To its end: a cancel (the worker stopping) waits for the commit, so the
            # drop below sees the quote it recorded (#5353).
            return await to_thread_to_end(
                lambda: lifecycle.record_slice_quote(
                    order_id,
                    item_id,
                    priced,
                    plates,
                    sliced_sha256=first.sha256 or "",
                    sliced_blob_key=first.blob_key or "",
                    by=by,
                    revision_id=revision_id,
                    quote_id=quote_id,
                    part_settings=part_settings,
                    printer=None if rate is None else rate.printer,
                    machine_rate_breakdown=rate,
                    choices=print_setup(request),
                    filament_plan=request.filament_plan.model_dump(mode="json"),
                ),
            )
        except LifecycleError as error:
            raise refused(error) from None
    except QuoteError as error:
        await _drop(lifecycle, order_id, quote_id, held)
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
        await _drop(lifecycle, order_id, quote_id, held)
        raise


async def _drop(lifecycle: Lifecycle, order_id: str, quote_id: str, held: bool) -> None:
    """A run that fails after its first add drops its own holder before it raises
    (Ruling 3), unless its quote was recorded: every attempt of one operation holds
    under one id, and another may have recorded it (#5353). Then it drops only the
    holds on keys that quote does not name (#5415). One the drop misses is the
    reaper's. Off the loop, as every other store call of the run: a slow database must
    not stall the worker."""
    if not held:
        return
    try:
        await asyncio.to_thread(lifecycle.drop_unrecorded_quote_holds, order_id, quote_id)
    except Exception:
        logger.exception("could not drop a failed quote's hold", extra={"id": quote_id})
