"""A visitor's inputs, checked against the listing (#5282, spec 2026-11-02 §5.4), and the
line item's reference built from the listing's snapshot (plan point 2).

`listing_values` never trusts the edge: it checks each asked value against the
listing's bound, not only against the schema. A parameter the visitor does not ask for
takes its schema default, checked the same way. Every parameter the listing does not
offer takes the value publish captured in the snapshot (``ListingSnapshot.fixed``: the
shop's locks and the commit's defaults), and asking for it is refused, even with that
same value.

`listing_reference` reads no git: publish stored the schema, the template's name and its
library pins on the listing. It builds the same `Reference` as `orders/reference.py`
`resolve_reference` (``tests/api/test_storefront_public.py`` pins that).
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from typing import Literal

from scadbuddy.orders.models import ColourSlot, Filament, Reference
from scadbuddy.orders.reference import ResolvedReference, render_key_of
from scadbuddy.orders.storefront.wire import (
    ChoiceBound,
    Listing,
    ListingSnapshot,
    NumberBound,
    ParamBound,
    TextBound,
    is_support,
)
from scadbuddy.render.runner import format_scad_value, with_defaults
from scadbuddy.render.schema import Parameter, ParamValue

InputReason = Literal["unknown", "locked", "range", "step", "choice", "length", "colour"]

#: How far a value may sit from a step, to a float's rounding.
_STEP_TOLERANCE = 1e-9
#: The largest number a visitor may send, either sign: far past any print's size, and
#: well inside a float, so no check below can overflow.
MAX_MAGNITUDE = 1e15
#: The reason when a value inside the bound fails the schema's own checks.
_SCHEMA_REFUSAL: dict[str, InputReason] = {"number": "range", "choice": "choice", "text": "length"}


class InputOutsideListingError(ValueError):
    """A value the listing does not allow: ``name`` is the parameter (or
    ``colours.<slot>`` for a colour slot), ``reason`` says why."""

    def __init__(self, name: str, reason: InputReason) -> None:
        super().__init__(f"{name!r} is outside the listing ({reason})")
        self.name = name
        self.reason = reason


def _snapshot(listing: Listing) -> ListingSnapshot:
    if listing.snapshot is None:
        raise ValueError(f"listing {listing.id!r} is not published")
    return listing.snapshot


class MixedMaterialError(ValueError):
    """A ``one_material`` listing's colours that share no one material (#6152)."""


def customer_filaments(listing: Listing, filaments: Mapping[str, Filament]) -> list[Filament]:
    """The listing's filaments that the price book still has and a customer may pick:
    never a support material (#6152)."""
    return [
        filaments[f]
        for f in listing.colours
        if f in filaments and not is_support(filaments[f].material)
    ]


def _offered_colours(listing: Listing, filaments: Mapping[str, Filament]) -> set[str]:
    """The colours of the listing's filaments that a customer may pick, casefolded."""
    return {f.colour.casefold() for f in customer_filaments(listing, filaments)}


def _same(choice: ParamValue, value: ParamValue) -> bool:
    """Equal in type and value: ``True`` is not ``1``."""
    return isinstance(choice, bool) == isinstance(value, bool) and choice == value


def _on_step(value: float, start: float, step: float) -> bool:
    times = (value - start) / step
    return abs(times - round(times)) < _STEP_TOLERANCE


def _number_reason(
    parameter: Parameter, bound: NumberBound, value: ParamValue
) -> InputReason | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "range"
    # Before any float conversion: a JSON integer of 400 digits overflows a float.
    if abs(value) > MAX_MAGNITUDE or (isinstance(value, float) and not math.isfinite(value)):
        return "range"
    if not bound.min <= value <= bound.max:
        return "range"
    if parameter.type == "integer" and not float(value).is_integer():
        return "step"
    step = bound.step if bound.step is not None else parameter.step
    if step is not None and step > 0 and not _on_step(value, bound.min, step):
        return "step"
    return None


def _reason(
    parameter: Parameter, bound: ParamBound, value: ParamValue, colours: Collection[str]
) -> InputReason | None:
    """Why ``value`` is outside ``bound``, or None when the listing allows it."""
    if parameter.type == "color" and (
        not isinstance(value, str) or value.casefold() not in colours
    ):
        return "colour"
    if isinstance(bound, NumberBound):
        try:
            reason = _number_reason(parameter, bound, value)
        except OverflowError:
            reason = "range"
    elif isinstance(bound, ChoiceBound):
        if parameter.type == "color":
            assert isinstance(value, str)
            allowed = any(
                isinstance(c, str) and c.casefold() == value.casefold() for c in bound.choices
            )
        else:
            allowed = any(_same(choice, value) for choice in bound.choices)
        reason = None if allowed else "choice"
    else:
        assert isinstance(bound, TextBound)
        reason = None if isinstance(value, str) and len(value) <= bound.max_length else "length"
    if reason is not None:
        return reason
    # Last, the schema's own checks, as a render makes them.
    try:
        format_scad_value(parameter, value)
    except (ValueError, OverflowError):
        return _SCHEMA_REFUSAL[bound.kind]
    return None


def listing_values(
    listing: Listing, asked: Mapping[str, ParamValue], filaments: Mapping[str, Filament]
) -> dict[str, ParamValue]:
    """The published snapshot's ``fixed`` values and the asked values of the listing's
    ``params``, each checked against its bound. A ``// color`` value must be the colour
    of one of the listing's filaments that ``filaments`` (the price book, by id) still
    has. Raises
    `InputOutsideListingError` for the first value outside the listing; ``ValueError``
    for a listing that is not published."""
    snapshot = _snapshot(listing)
    schema = snapshot.schema_
    # The values publish captured, never the row's ``fixed``: that holds only the shop's
    # locks, and is the next publish's input.
    fixed = snapshot.fixed
    for name in asked:
        if name in fixed:
            raise InputOutsideListingError(name, "locked")
        if name not in listing.params:
            raise InputOutsideListingError(name, "unknown")
    by_name = {parameter.name: parameter for parameter in schema.parameters}
    colours = _offered_colours(listing, filaments)
    values = dict(fixed)
    for name, bound in listing.params.items():
        parameter = by_name.get(name)
        if parameter is None:
            # Publish checked every name against this schema.
            raise InputOutsideListingError(name, "unknown")
        if name in asked:
            value = asked[name]
        elif parameter.initial is not None:
            value = parameter.initial
        else:
            continue
        reason = _reason(parameter, bound, value, colours)
        if reason is not None:
            raise InputOutsideListingError(name, reason)
        values[name] = value
    return values


def listing_reference(
    listing: Listing,
    values: Mapping[str, ParamValue],
    chosen: Mapping[int, str],
    *,
    filaments: Mapping[str, Filament],
) -> ResolvedReference:
    """The line item's reference, from the listing's snapshot (no git), and a colour
    slot for each of ``chosen`` (slot to filament id). Each chosen filament must be one
    of the listing's that ``filaments`` still has, else `InputOutsideListingError`
    (``colours.<slot>``, ``colour``). ``values`` are `listing_values`' answer."""
    snapshot = _snapshot(listing)
    slots: list[ColourSlot] = []
    for slot, filament_id in sorted(chosen.items()):
        if (
            filament_id not in listing.colours
            or filament_id not in filaments
            or is_support(filaments[filament_id].material)
        ):
            raise InputOutsideListingError(f"colours.{slot}", "colour")
        slots.append(
            ColourSlot(slot=slot, colour=filaments[filament_id].colour, filament_id=filament_id)
        )
    inputs = {"params": with_defaults(snapshot.schema_, values), "v": 0}
    reference = Reference(
        slug=listing.slug,
        commit=listing.commit,
        inputs=inputs,
        libraries=list(snapshot.libraries),
        template_name=snapshot.template_name,
        render_key=render_key_of(listing.slug, inputs, listing.commit, pipeline=snapshot.pipeline),
    )
    return ResolvedReference(reference, slots)


def checked_picks(
    listing: Listing,
    values: Mapping[str, ParamValue],
    picks: Mapping[str, str],
    filaments: Mapping[str, Filament],
) -> dict[str, Filament]:
    """Each of ``picks`` (parameter name to filament id) as its filament (#6152). The
    parameter must be an offered ``// color`` parameter, and the filament one of the
    listing's that the price book still has, not a support material, whose colour is the
    parameter's value in ``values`` (`listing_values`' answer), casefolded. Else
    `InputOutsideListingError` (``picks.<name>``, ``colour``)."""
    by_name = {p.name: p for p in _snapshot(listing).schema_.parameters}
    offered = {f.id: f for f in customer_filaments(listing, filaments)}
    picked: dict[str, Filament] = {}
    for name, filament_id in picks.items():
        parameter = by_name.get(name)
        filament = offered.get(filament_id)
        value = values.get(name)
        if (
            name not in listing.params
            or parameter is None
            or parameter.type != "color"
            or filament is None
            or not isinstance(value, str)
            or filament.colour.casefold() != value.casefold()
        ):
            raise InputOutsideListingError(f"picks.{name}", "colour")
        picked[name] = filament
    return picked


def check_one_material(
    listing: Listing,
    values: Mapping[str, ParamValue],
    picked: Mapping[str, Filament],
    filaments: Mapping[str, Filament],
) -> None:
    """For a ``one_material`` listing, `MixedMaterialError` unless one material (casefolded)
    holds every pick and, for each offered colour parameter with no pick, a listing filament
    of its value's colour (#6152). Any other listing passes."""
    if not listing.one_material:
        return
    by_name = {p.name: p for p in _snapshot(listing).schema_.parameters}
    offered = customer_filaments(listing, filaments)
    materials: set[str] | None = None
    for name in listing.params:
        parameter = by_name.get(name)
        if parameter is None or parameter.type != "color":
            continue
        if name in picked:
            these = {picked[name].material.casefold()}
        else:
            value = values.get(name)
            if not isinstance(value, str):
                continue
            these = {
                f.material.casefold() for f in offered if f.colour.casefold() == value.casefold()
            }
        materials = these if materials is None else materials & these
        if not materials:
            raise MixedMaterialError(f"{listing.id!r}: {name!r} takes another material")


def slots_from_picks(
    chosen: Mapping[int, str],
    picked: Mapping[str, Filament],
    render_colours: Sequence[str],
) -> dict[int, str]:
    """``chosen`` (slot to filament id), with each slot it lacks filled from ``picked``:
    the picked filament whose colour is the slot's colour in the render
    (``job.result.colors``), casefolded (#6152). A slot no pick matches stays out, and
    the shop picks it."""
    by_colour: dict[str, str] = {}
    for filament in picked.values():
        by_colour.setdefault(filament.colour.casefold(), filament.id)
    filled = dict(chosen)
    for slot, colour in enumerate(render_colours):
        if slot not in filled and colour.casefold() in by_colour:
            filled[slot] = by_colour[colour.casefold()]
    return filled
