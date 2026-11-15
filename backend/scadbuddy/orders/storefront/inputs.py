"""A visitor's inputs, checked against the listing (#5282, spec 2026-11-02 §5.4), and the
line item's reference built from the listing's snapshot (plan point 2).

`listing_values` never trusts the edge: it checks each asked value against the
listing's bound, not only against the schema. A parameter the visitor does not ask for
takes the listing's ``default``, else its schema default (`effective_default`, #6144),
checked the same way. Every parameter the listing does not
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
from dataclasses import dataclass
from typing import Literal

from scadbuddy.orders.models import ColourSlot, Filament, Reference
from scadbuddy.orders.reference import ResolvedReference, render_key_of
from scadbuddy.orders.storefront.closest_colour import Swatch, closest_colour
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


class ColoursClashError(ValueError):
    """Colours that no one allowed material offers together (#6258). ``parts`` names
    them: the colour parameters (and ``colours.<slot>`` slots) of a smallest set that
    still clashes, the visitor's own colours kept before the shop's (`resolve_choice`)."""

    def __init__(self, parts: list[str]) -> None:
        super().__init__(f"{', '.join(parts)} share no allowed material")
        self.parts = parts


def effective_materials(listing: Listing, shop: Sequence[str]) -> list[str]:
    """The materials a print of ``listing`` may be made of, in order (#6258): its own,
    else the shop's default (``storefront_materials``)."""
    return list(listing.materials) or list(shop)


def customer_filaments(
    listing: Listing, filaments: Mapping[str, Filament], materials: Collection[str]
) -> list[Filament]:
    """The listing's filaments that the price book still has and a customer may pick:
    never a support material (#6152), and only of one of ``materials`` (casefolded,
    `effective_materials`, #6258). In the listing's order."""
    allowed = {m.casefold() for m in materials}
    return [
        filaments[f]
        for f in listing.colours
        if f in filaments
        and not is_support(filaments[f].material)
        and filaments[f].material.casefold() in allowed
    ]


def _offered_colours(
    listing: Listing, filaments: Mapping[str, Filament], materials: Collection[str]
) -> set[str]:
    """The colours of the listing's filaments that a customer may pick, casefolded."""
    return {f.colour.casefold() for f in customer_filaments(listing, filaments, materials)}


def same_value(choice: ParamValue, value: ParamValue) -> bool:
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


def effective_default(parameter: Parameter, bound: ParamBound) -> ParamValue | None:
    """What a render of an offered parameter takes while the customer picks none: the
    listing's ``default``, else the template's (#6144)."""
    return bound.default if bound.default is not None else parameter.initial


def value_reason(
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
            allowed = any(same_value(choice, value) for choice in bound.choices)
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
    listing: Listing,
    asked: Mapping[str, ParamValue],
    filaments: Mapping[str, Filament],
    materials: Collection[str],
) -> dict[str, ParamValue]:
    """The published snapshot's ``fixed`` values and the asked values of the listing's
    ``params``, each checked against its bound. A ``// color`` value must be the colour
    of one of the listing's filaments that ``filaments`` (the price book, by id) still
    has and a customer may pick (never a support material, and only of ``materials``,
    `customer_filaments`, #6152, #6258). Raises
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
    colours = _offered_colours(listing, filaments, materials)
    values = dict(fixed)
    for name, bound in listing.params.items():
        parameter = by_name.get(name)
        if parameter is None:
            # Publish checked every name against this schema.
            raise InputOutsideListingError(name, "unknown")
        if name in asked:
            value = asked[name]
        elif (effective := effective_default(parameter, bound)) is not None:
            value = effective
        else:
            continue
        reason = value_reason(parameter, bound, value, colours)
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
    of the listing's that ``filaments`` still has and a customer may pick (never a support
    material, `customer_filaments`, #6152), else `InputOutsideListingError`
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


@dataclass(frozen=True)
class ResolvedChoice:
    """What a visitor's colours resolve to (#6258): the one material the print is made
    of (the effective list's spelling; None when the listing offers no colour and no
    filament), the values with each colour the visitor did not give settled in that
    material, and that material's customer filaments, in the listing's order."""

    material: str | None
    values: dict[str, ParamValue]
    filaments: list[Filament]

    def filament_of(self, colour: str) -> Filament | None:
        """The material's first filament of ``colour``, casefolded, or None."""
        return next((f for f in self.filaments if f.colour.casefold() == colour.casefold()), None)


def _choice_colours(bound: ParamBound) -> set[str] | None:
    """A colour parameter's choices, casefolded; None when it has no choice bound."""
    if not isinstance(bound, ChoiceBound):
        return None
    return {c.casefold() for c in bound.choices if isinstance(c, str)}


def resolve_choice(
    listing: Listing,
    values: Mapping[str, ParamValue],
    given: Collection[str],
    filaments: Mapping[str, Filament],
    materials: Sequence[str],
    *,
    slots: Mapping[int, str] | None = None,
) -> ResolvedChoice:
    """The one material a print of ``values`` (`listing_values`' answer) is made of, and
    the values in it (#6258). The render, the estimate and the add to the order all use
    it, and the page's copy follows the same rule.

    - The colour parameters the visitor gives (``given``, the names it sent) fix the
      material: the first of ``materials`` (in order) that offers each given colour on
      this listing. A slot of ``slots`` (slot to filament id) fixes it to its filament's.
    - A print never mixes materials, so the material must also offer one of the choices
      of each colour parameter the visitor does not give.
    - Such a parameter keeps its value (the listing's default) when the material offers
      it, else takes the closest colour the material offers among its choices
      (`closest_colour.py`). It is never stored as a pick.

    No material fits: `ColoursClashError`. Its ``parts`` are a smallest set of those
    parts that still clashes: each part in turn is left out while the rest still clash,
    the shop's parts first, then the slots, then the visitor's, so the refusal names the
    visitor's own colours whenever they alone clash. A slot filament that is not a
    customer filament of ``materials``: `InputOutsideListingError` (``colours.<slot>``)."""
    by_name = {p.name: p for p in _snapshot(listing).schema_.parameters}
    offered = customer_filaments(listing, filaments, materials)
    of: dict[str, list[Filament]] = {}
    for filament in offered:
        of.setdefault(filament.material.casefold(), []).append(filament)
    order = [m for m in dict.fromkeys(m.casefold() for m in materials) if m in of]
    colours = {m: {f.colour.casefold() for f in each} for m, each in of.items()}
    given_parts: list[tuple[str, set[str]]] = []
    shop_parts: list[tuple[str, set[str]]] = []
    for name, bound in listing.params.items():
        parameter = by_name.get(name)
        if parameter is None or parameter.type != "color":
            continue
        value = values.get(name)
        if name in given and isinstance(value, str):
            given_parts.append((name, {m for m in order if value.casefold() in colours[m]}))
        else:
            choices = _choice_colours(bound)
            shop_parts.append((name, {m for m in order if choices is None or choices & colours[m]}))
    slot_parts: list[tuple[str, set[str]]] = []
    by_id = {f.id: f for f in offered}
    for slot, filament_id in sorted((slots or {}).items()):
        slot_filament = by_id.get(filament_id)
        if slot_filament is None:
            raise InputOutsideListingError(f"colours.{slot}", "colour")
        slot_parts.append((f"colours.{slot}", {slot_filament.material.casefold()}))
    parts = dict(given_parts + slot_parts + shop_parts)

    def fits(names: Collection[str]) -> str | None:
        return next((m for m in order if all(m in parts[n] for n in names)), None)

    chosen = fits(parts)
    if chosen is None and parts:
        kept = list(parts)
        for name in [n for n, _ in shop_parts + slot_parts + given_parts]:
            rest = [n for n in kept if n != name]
            if rest and fits(rest) is None:
                kept = rest
        # Named in the template's parameter order, then the slots: `listing.params`
        # comes from jsonb, which keeps no key order (#6302 CI).
        place = {name: i for i, name in enumerate(by_name)}
        raise ColoursClashError(sorted(kept, key=lambda n: (place.get(n, len(place)), n)))
    if chosen is None:
        # No colour to choose and no filament: the shop picks the slots.
        return ResolvedChoice(material=None, values=dict(values), filaments=[])
    spelling = next(m for m in materials if m.casefold() == chosen)
    settled = dict(values)
    for name, _ in shop_parts:
        value = values.get(name)
        if not isinstance(value, str) or value.casefold() in colours[chosen]:
            continue
        choices = _choice_colours(listing.params[name])
        candidates: list[Filament] = []
        seen: set[str] = set()
        for filament in of[chosen]:
            key = filament.colour.casefold()
            if key not in seen and (choices is None or key in choices):
                seen.add(key)
                candidates.append(filament)
        # The target is a filament's colour when one has it: that filament's name and
        # alpha (#6258).
        own = next((f for f in offered if f.colour.casefold() == value.casefold()), None)
        index = closest_colour(
            Swatch(
                hex=value,
                name=own.colour_name if own else None,
                alpha=own.alpha if own else None,
            ),
            [Swatch(hex=f.colour, name=f.colour_name, alpha=f.alpha) for f in candidates],
        )
        assert index is not None  # `fits` keeps only a material with a choice of it.
        settled[name] = candidates[index].colour
    return ResolvedChoice(material=spelling, values=settled, filaments=of[chosen])


def slot_filaments(
    chosen: Mapping[int, str], resolved: ResolvedChoice, render_colours: Sequence[str]
) -> dict[int, str]:
    """``chosen`` (slot to filament id), with each slot it lacks filled with the
    resolved material's filament of the slot's colour in the render
    (``job.result.colors``), casefolded (#6258). A slot that material has no filament of
    (a locked support colour, say) stays out, and the shop picks it."""
    filled = dict(chosen)
    for slot, colour in enumerate(render_colours):
        filament = resolved.filament_of(colour)
        if slot not in filled and filament is not None:
            filled[slot] = filament.id
    return filled


def check_one_material(
    slots: Collection[ColourSlot], resolved: ResolvedChoice, filaments: Mapping[str, Filament]
) -> None:
    """`ColoursClashError` unless every slot filament (support aside) is of the
    resolved material: a print never mixes materials (#6152; always on since #6258)."""
    for slot in slots:
        filament = filaments.get(slot.filament_id or "")
        if filament is None or is_support(filament.material):
            continue
        if resolved.material is None or (
            filament.material.casefold() != resolved.material.casefold()
        ):
            raise ColoursClashError([f"colours.{slot.slot}"])
