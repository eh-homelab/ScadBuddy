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
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from scadbuddy.orders.models import ColourSlot, Filament, Reference
from scadbuddy.orders.reference import ResolvedReference, render_key_of
from scadbuddy.orders.storefront.closest_colour import Swatch, closest_colour
from scadbuddy.orders.storefront.wire import (
    SUPPORT_PARAM,
    ChoiceBound,
    Listing,
    ListingSnapshot,
    NumberBound,
    ParamBound,
    TextBound,
    is_support,
    shown_label,
)
from scadbuddy.render.runner import format_scad_value, with_defaults
from scadbuddy.render.schema import CustomizerSchema, Parameter, ParamValue

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


class LockedColoursClashError(ValueError):
    """No allowed material is printable (`printable_materials`, #6302 review): none
    offers every colour the shop locked and a choice of each offered colour parameter, so
    the listing cannot be printed in one material, whatever the visitor gives. Publish
    refuses such a listing (``listing_locked_colours_unprintable``); one published before
    that check, or broken since by the materials or the price book, is unavailable.
    ``names`` are the locked colour parameters (none when no colour is locked)."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(f"{', '.join(names)} share no allowed material")
        self.names = names


def sets_support(parameter: Parameter, label: str) -> bool:
    """Whether ``parameter`` sets support: its name, caption, group or shown ``label`` says
    so, as the page's and the agent's ``isSupport`` read it (#6152)."""
    texts = (parameter.name, parameter.caption, parameter.group, label)
    return any(t is not None and SUPPORT_PARAM.search(t) is not None for t in texts)


def locked_colours(
    schema: CustomizerSchema, fixed: Mapping[str, ParamValue], offered: Collection[str]
) -> dict[str, str]:
    """The colours the shop locked that a print takes from its one material (#6302
    review): each ``// color`` parameter of ``schema`` in ``fixed`` that is not offered
    (``offered``, the listing's ``params``) and does not set support (`sets_support`), by
    name, in the schema's order."""
    return {
        p.name: value
        for p in schema.parameters
        if p.type == "color"
        and p.name not in offered
        and isinstance(value := fixed.get(p.name), str)
        and not sets_support(p, shown_label(p.name, None, p.caption))
    }


def printable_of(
    schema: CustomizerSchema,
    fixed: Mapping[str, ParamValue],
    params: Mapping[str, ParamBound],
    offered: Iterable[tuple[str, str]],
    materials: Sequence[str],
) -> list[str]:
    """`printable_materials` from its parts: ``offered`` is each customer filament's
    material and colour (support excluded). Publish asks it before a snapshot exists."""
    colours: dict[str, set[str]] = {}
    for material, colour in offered:
        colours.setdefault(material_key(material), set()).add(colour.casefold())
    wanted = {v.casefold() for v in locked_colours(schema, fixed, params).values()}
    by_name = {p.name: p for p in schema.parameters}
    parts = [
        _choice_colours(bound)
        for name, bound in params.items()
        if (p := by_name.get(name)) is not None and p.type == "color"
    ]
    printable: list[str] = []
    seen: set[str] = set()
    for material in materials:
        key = material_key(material)
        if key in seen or key not in colours:
            continue
        seen.add(key)
        if wanted <= colours[key] and all(c is None or c & colours[key] for c in parts):
            printable.append(material)
    return printable


def printable_materials(
    listing: Listing, filaments: Mapping[str, Filament], materials: Sequence[str]
) -> list[str]:
    """The one rule of which materials a print of ``listing`` can be made of (#6258,
    #6302 review): of ``materials`` (the effective ones, in order, `effective_materials`),
    each that offers, among the listing's customer filaments (`customer_filaments`), every
    colour the shop locked that is not support (`locked_colours`) and at least one choice
    of each offered colour parameter. The render (`resolve_choice`), publish
    (``check_locked_colours``), the public listing and its card, and the prepare run all
    use it. Empty with a colour to print: the listing cannot be made
    (`LockedColoursClashError`)."""
    snapshot = _snapshot(listing)
    return printable_of(
        snapshot.schema_,
        snapshot.fixed,
        listing.params,
        ((f.material, f.colour) for f in customer_filaments(listing, filaments, materials)),
        materials,
    )


def needs_material(listing: Listing) -> bool:
    """Whether a print of ``listing`` takes a colour of a material: it offers a colour
    parameter or locks a colour that is not support. One that does not prints with no
    material of its own (the shop picks the slots)."""
    snapshot = _snapshot(listing)
    by_name = {p.name: p for p in snapshot.schema_.parameters}
    offered = any((p := by_name.get(n)) is not None and p.type == "color" for n in listing.params)
    return offered or bool(locked_colours(snapshot.schema_, snapshot.fixed, listing.params))


def effective_materials(listing: Listing, shop: Sequence[str]) -> list[str]:
    """The materials a print of ``listing`` may be made of, in order (#6258): its own,
    else the shop's default (``storefront_materials``)."""
    return list(listing.materials) or list(shop)


def material_key(material: str) -> str:
    """A material as it is compared: trimmed and casefolded, as `unique_materials` and
    the materials backfill keep it (#6302 review), so stray whitespace in the price book
    never drops a filament."""
    return material.strip().casefold()


def customer_filaments(
    listing: Listing, filaments: Mapping[str, Filament], materials: Collection[str]
) -> list[Filament]:
    """The listing's filaments that the price book still has and a customer may pick:
    never a support material (#6152), and only of one of ``materials`` (`material_key`,
    `effective_materials`, #6258). In the listing's order."""
    allowed = {material_key(m) for m in materials}
    return [
        filaments[f]
        for f in listing.colours
        if f in filaments
        and not is_support(filaments[f].material)
        and material_key(filaments[f].material) in allowed
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
            offered = colours
        elif (effective := effective_default(parameter, bound)) is not None:
            value = effective
            # A colour default the visitor did not send is never refused for its
            # material: `resolve_choice` moves it to the closest colour that the chosen
            # material offers (#6258, #6302 review).
            offered = {value.casefold()} if isinstance(value, str) else colours
        else:
            continue
        reason = value_reason(parameter, bound, value, offered)
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
    - A print never mixes materials, so the material is one of `printable_materials`:
      it offers each colour the shop locked that is not support and a choice of each
      offered colour parameter (#6302 review). None is printable:
      `LockedColoursClashError`, whatever the visitor gives.
    - Of the materials that fit, the first that also offers the value (the listing's
      default) of each colour parameter the visitor does not give wins. Only when none
      does is it the first that fits (#6258 "otherwise use default", #6302 review). So a
      listing the visitor does not touch renders as the shop set it.
    - Such a parameter keeps its value (the listing's default) when the material offers
      it, else takes the closest colour the material offers among its choices
      (`closest_colour.py`). It is never stored as a pick.

    No material fits: `ColoursClashError`. Its ``parts`` are a smallest set of those
    parts that still clashes: each part in turn is left out while the rest still clash,
    the shop's parts first, then the slots, then the visitor's, so the refusal names the
    visitor's own colours whenever they alone clash. A locked colour is never named: only
    the printable materials are weighed. A slot filament that is not a
    customer filament of ``materials``: `InputOutsideListingError` (``colours.<slot>``)."""
    snapshot = _snapshot(listing)
    by_name = {p.name: p for p in snapshot.schema_.parameters}
    offered = customer_filaments(listing, filaments, materials)
    of: dict[str, list[Filament]] = {}
    for filament in offered:
        of.setdefault(material_key(filament.material), []).append(filament)
    colours = {m: {f.colour.casefold() for f in each} for m, each in of.items()}
    # Only a printable material is ever chosen (#6302 review): one that offers each
    # colour the shop locked, so the locked slots take its filaments, and a choice of
    # each offered colour parameter. A visitor's clash is judged among those alone.
    order = [material_key(m) for m in printable_materials(listing, filaments, materials)]
    if not order and needs_material(listing):
        raise LockedColoursClashError(
            list(locked_colours(snapshot.schema_, snapshot.fixed, listing.params))
        )
    given_parts: list[tuple[str, set[str]]] = []
    shop_parts: list[tuple[str, set[str]]] = []
    #: The materials that offer each shop part's value as it is.
    keeps: list[set[str]] = []
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
            keeps.append(
                {m for m in order if isinstance(value, str) and value.casefold() in colours[m]}
            )
    slot_parts: list[tuple[str, set[str]]] = []
    by_id = {f.id: f for f in offered}
    for slot, filament_id in sorted((slots or {}).items()):
        slot_filament = by_id.get(filament_id)
        if slot_filament is None:
            raise InputOutsideListingError(f"colours.{slot}", "colour")
        slot_parts.append((f"colours.{slot}", {material_key(slot_filament.material)}))
    parts = dict(given_parts + slot_parts + shop_parts)

    def fits(names: Collection[str]) -> str | None:
        return next((m for m in order if all(m in parts[n] for n in names)), None)

    chosen = fits(parts)
    if chosen is not None:
        chosen = next(
            (m for m in order if all(m in parts[n] for n in parts) and all(m in k for k in keeps)),
            chosen,
        )
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
    spelling = next(m for m in materials if material_key(m) == chosen)
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
        # Any material's: the default may be of a material no longer allowed.
        own = next(
            (
                filaments[f]
                for f in listing.colours
                if f in filaments
                and not is_support(filaments[f].material)
                and filaments[f].colour.casefold() == value.casefold()
            ),
            None,
        )
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
    (``job.result.colors``), casefolded (#6258). A locked colour that is not support
    always has one: `resolve_choice` chooses only a material that offers each locked
    colour (#6302 review). A slot that material has no filament of (a locked support
    colour) stays out, and the shop picks it."""
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
            material_key(filament.material) != material_key(resolved.material)
        ):
            raise ColoursClashError([f"colours.{slot.slot}"])
