"""A visitor's inputs, checked against the listing (#5282, spec 2026-11-02 §5.4), and the
reference built from the listing's snapshot with no git (plan point 2). The parity test
against `orders/reference.py` `resolve_reference` needs git and the API: it is in
``tests/api/test_storefront_public.py``."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError

from scadbuddy.core.problems import ApiError
from scadbuddy.orders.models import ColourSlot, Filament
from scadbuddy.orders.storefront.drafts import _captions
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
    printable_materials,
    resolve_choice,
    slot_filaments,
)
from scadbuddy.orders.storefront.listings import (
    check_locked_colours,
    check_materials,
    public_card,
    public_listing,
)
from scadbuddy.orders.storefront.renders import _filament_for
from scadbuddy.orders.storefront.wire import (
    Listing,
    ListingInput,
    ListingSnapshot,
    PublicChoice,
    PublicListing,
    PublicText,
    RenderBody,
    TextBound,
)
from scadbuddy.render.job_models import render_key
from scadbuddy.render.schema import CustomizerSchema, Option, Parameter
from scadbuddy.store.content import ContentStore

NOW = datetime(2026, 11, 7, tzinfo=UTC)
PLA_RED = "pla-red"
PLA_BLUE = "pla-blue"
PETG_GREEN = "petg-green"
COMMIT = "c" * 40
#: The shop's default materials (``storefront_materials``) these tests take.
SHOP = ["PLA", "PETG"]


def filament(filament_id: str, colour: str, material: str = "PLA") -> Filament:
    return Filament(
        id=filament_id,
        material=material,
        subtype=None,
        brand=None,
        colour_name=None,
        colour=colour,
        cost_per_kg="20",
        density_g_cm3=None,
        archived_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


#: The price book. Green is a filament of the book, not of the listing.
FILAMENTS = {
    PLA_RED: filament(PLA_RED, "#FF0000"),
    PLA_BLUE: filament(PLA_BLUE, "#0000FF"),
    PETG_GREEN: filament(PETG_GREEN, "#00FF00", "PETG"),
}

SCHEMA = CustomizerSchema(
    parameters=[
        Parameter(name="width", type="number", initial=50, min=10, max=100, step=1),
        Parameter(name="height", type="number", initial=5),
        Parameter(name="colour", type="color", initial="#FF0000"),
        Parameter(
            name="shape",
            type="select",
            initial="round",
            options=[Option(name="round", value="round"), Option(name="square", value="square")],
        ),
        Parameter(name="label", type="string", initial="Hi"),
        Parameter(name="count", type="integer", initial=2, min=1, max=10),
    ]
)


@pytest.fixture
def listing() -> Listing:
    return Listing.model_validate(
        {
            "id": "listing-1",
            "slug": "demo",
            "commit": COMMIT,
            "title": "Demo",
            "params": {
                "width": {"kind": "number", "min": 20, "max": 80, "step": 1},
                "colour": {"kind": "choice", "choices": ["#ff0000", "#0000FF"]},
                "shape": {"kind": "choice", "choices": ["round"]},
                "label": {"kind": "text", "max_length": 10},
                "count": {"kind": "number", "min": 1, "max": 5},
            },
            "fixed": {"height": 5},
            "colours": [PLA_RED, PLA_BLUE],
            "position": 0,
            "published": True,
            "snapshot": ListingSnapshot(
                schema=SCHEMA,
                pipeline=False,
                template_name="Demo",
                libraries=[],
                snapshot_key=f"src-demo-{COMMIT}",
                fixed={"height": 5},
            ),
            "media": [],
            "published_at": NOW,
            "created_at": NOW,
            "updated_at": NOW,
        }
    )


@pytest.mark.parametrize(
    ("asked", "reason"),
    [
        ({"nope": 1}, "unknown"),
        ({"height": 9}, "locked"),
        # A locked value is refused even when it is the listing's own.
        ({"height": 5}, "locked"),
        ({"width": 81}, "range"),
        ({"width": 19}, "range"),
        ({"width": "40"}, "range"),
        ({"width": True}, "range"),
        ({"width": float("nan")}, "range"),
        ({"width": 10**400}, "range"),
        ({"width": -(10**400)}, "range"),
        # A JSON number of 400 digits, as a body would parse it.
        ({"width": json.loads("1" + "0" * 399)}, "range"),
        ({"width": 10**16}, "range"),
        ({"width": float("inf")}, "range"),
        ({"shape": 10**400}, "choice"),
        ({"width": 20.5}, "step"),
        ({"count": 2.5}, "step"),
        ({"colour": "#00ff00"}, "colour"),
        ({"colour": 1}, "colour"),
        ({"shape": "square"}, "choice"),
        ({"label": "x" * 11}, "length"),
        ({"label": 5}, "length"),
    ],
)
def test_inputs_outside_the_listing_are_refused(
    listing: Listing, asked: dict[str, Any], reason: str
) -> None:
    with pytest.raises(InputOutsideListingError) as refused:
        listing_values(listing, asked, FILAMENTS, SHOP)
    assert refused.value.reason == reason
    assert refused.value.name == next(iter(asked))


def test_the_values_are_the_fixed_and_the_asked(listing: Listing) -> None:
    values = listing_values(
        listing, {"width": 40, "colour": "#0000ff", "label": "Ada"}, FILAMENTS, SHOP
    )
    # Each parameter not asked takes its schema default.
    assert values == {
        "height": 5,
        "width": 40,
        "colour": "#0000ff",
        "shape": "round",
        "label": "Ada",
        "count": 2,
    }


def test_a_colour_of_an_archived_filament_is_refused(listing: Listing) -> None:
    without_blue = {k: v for k, v in FILAMENTS.items() if k != PLA_BLUE}
    with pytest.raises(InputOutsideListingError) as refused:
        listing_values(listing, {"colour": "#0000FF"}, without_blue, SHOP)
    assert refused.value.reason == "colour"


def test_an_unpublished_listing_has_no_values(listing: Listing) -> None:
    with pytest.raises(ValueError, match="not published"):
        listing_values(listing.model_copy(update={"snapshot": None}), {}, FILAMENTS, SHOP)


def test_the_reference_comes_from_the_snapshot(listing: Listing) -> None:
    values = listing_values(listing, {"width": 40}, FILAMENTS, SHOP)
    resolved = listing_reference(listing, values, {0: PLA_RED, 1: PLA_BLUE}, filaments=FILAMENTS)
    reference = resolved.reference
    assert (reference.slug, reference.commit, reference.template_name) == ("demo", COMMIT, "Demo")
    assert reference.inputs == {"params": values, "v": 0}
    assert reference.render_key == render_key("demo", values, COMMIT)
    assert resolved.colours == [
        ColourSlot(slot=0, colour="#FF0000", filament_id=PLA_RED),
        ColourSlot(slot=1, colour="#0000FF", filament_id=PLA_BLUE),
    ]


@pytest.mark.parametrize("chosen", [{0: PETG_GREEN}, {0: "no-such-filament"}])
def test_a_slot_takes_only_a_listing_filament(listing: Listing, chosen: dict[int, str]) -> None:
    values = listing_values(listing, {}, FILAMENTS, SHOP)
    with pytest.raises(InputOutsideListingError) as refused:
        listing_reference(listing, values, chosen, filaments=FILAMENTS)
    assert (refused.value.name, refused.value.reason) == ("colours.0", "colour")


def _public(
    listing: Listing, filaments: dict[str, Filament], shop: list[str] | None = None
) -> PublicListing:
    # No media, so the content store is never read.
    return asyncio.run(public_listing(cast(ContentStore, None), listing, filaments, shop or SHOP))


def test_an_archived_filaments_colour_is_not_offered(listing: Listing) -> None:
    without_blue = {k: v for k, v in FILAMENTS.items() if k != PLA_BLUE}
    shown = _public(listing, without_blue)
    assert [f.id for f in shown.colours] == [PLA_RED]
    colour = shown.params["colour"]
    assert isinstance(colour, PublicChoice) and colour.choices == ["#ff0000"]
    # A choice of a parameter that is not a colour is left as it is.
    shape = shown.params["shape"]
    assert isinstance(shape, PublicChoice) and shape.choices == ["round"]


def test_every_colour_archived_offers_none(listing: Listing) -> None:
    colour = _public(listing, {PETG_GREEN: FILAMENTS[PETG_GREEN]}).params["colour"]
    assert isinstance(colour, PublicChoice) and colour.choices == []


def test_each_colour_choice_is_offered_once_whatever_its_case(listing: Listing) -> None:
    """#6299 review: two spellings of one colour are two render keys, and a prepare
    (#6237) renders only the first. So the page offers only the first spelling."""
    params = {
        **listing.params,
        "colour": {"kind": "choice", "choices": ["#ff0000", "#0000FF", "#FF0000", "#0000ff"]},
    }
    twice = Listing.model_validate({**listing.model_dump(by_alias=True), "params": params})
    colour = _public(twice, FILAMENTS).params["colour"]
    assert isinstance(colour, PublicChoice) and colour.choices == ["#ff0000", "#0000FF"]


def test_a_public_param_has_no_help(listing: Listing) -> None:
    """The schema has no help text (`Parameter` has none), so the view shows none."""
    assert "help" not in _public(listing, FILAMENTS).params["width"].model_dump()


def test_an_unlocked_default_is_the_published_snapshots(listing: Listing) -> None:
    """The row's ``fixed`` holds only the shop's locks; publish captured every other
    value a render uses in the snapshot. A visitor's render reads the snapshot."""
    assert listing.snapshot is not None
    unlocked = listing.model_copy(
        update={
            "fixed": {},
            "snapshot": listing.snapshot.model_copy(update={"fixed": {"height": 7}}),
        }
    )
    assert listing_values(unlocked, {}, FILAMENTS, SHOP)["height"] == 7
    with pytest.raises(InputOutsideListingError) as refused:
        listing_values(unlocked, {"height": 7}, FILAMENTS, SHOP)
    assert refused.value.reason == "locked"


def test_a_listing_republished_at_another_commit_renders_its_values(listing: Listing) -> None:
    """Published at A (height 5), republished at B (height 9): the snapshot of B
    decides, whatever the row still says."""
    assert listing.snapshot is not None
    at_b = listing.model_copy(
        update={
            "commit": "b" * 40,
            "fixed": {"height": 5},
            "snapshot": listing.snapshot.model_copy(update={"fixed": {"height": 9}}),
        }
    )
    assert listing_values(at_b, {}, FILAMENTS, SHOP)["height"] == 9


def _with_captions(listing: Listing, captions: dict[str, str]) -> Listing:
    assert listing.snapshot is not None
    parameters = [
        p.model_copy(update={"caption": captions.get(p.name, p.caption)})
        for p in listing.snapshot.schema_.parameters
    ]
    schema = listing.snapshot.schema_.model_copy(update={"parameters": parameters})
    return listing.model_copy(
        update={"snapshot": listing.snapshot.model_copy(update={"schema_": schema})}
    )


@pytest.mark.parametrize(
    ("caption", "shown"),
    [
        ("Letter colour (extruder 2)", "Letter colour"),
        ("Base and border colour (Extruder 1)", "Base and border colour"),
        ("Colour (extruder 1) of the rim", "Colour of the rim"),
        ("Width (mm)", "Width (mm)"),
        # A caption that is only the note keeps it, rather than show nothing.
        ("(extruder 1)", "(extruder 1)"),
        # Only "(extruder N)" is the note; other forms are the template's own words.
        ("Colour (ext. 1)", "Colour (ext. 1)"),
        ("Colour (AMS slot 2)", "Colour (AMS slot 2)"),
        # The same rule as the preview's `shownLabel` (params.test.ts has this table).
        ("Rim(extruder 2)colour", "Rim colour"),
        ("Colour (extruder \uff12)", "Colour (extruder \uff12)"),
    ],
)
def test_a_public_label_leaves_out_the_extruder_note(
    listing: Listing, caption: str, shown: str
) -> None:
    """#6137: a template's caption is written for the shop; the customer never reads
    which extruder prints a colour."""
    labelled = _with_captions(listing, {"colour": caption})
    assert _public(labelled, FILAMENTS).params["colour"].label == shown


def test_a_listings_own_label_wins_over_the_caption(listing: Listing) -> None:
    labelled = _with_captions(listing, {"colour": "Letter colour (extruder 2)"})
    bounds = dict(labelled.params)
    bounds["colour"] = bounds["colour"].model_copy(update={"label": "Colour of the letters"})
    shown = _public(labelled.model_copy(update={"params": bounds}), FILAMENTS)
    assert shown.params["colour"].label == "Colour of the letters"
    # A parameter with neither keeps its name.
    assert shown.params["width"].label == "width"


def test_a_text_parameter_shows_its_default_as_its_placeholder(listing: Listing) -> None:
    """#6137: the field's placeholder is what the design uses when it is left blank."""
    text = _public(listing, FILAMENTS).params["label"]
    assert isinstance(text, PublicText) and text.placeholder == "Hi"


def test_a_label_must_say_something() -> None:
    with pytest.raises(ValidationError):
        TextBound.model_validate({"kind": "text", "max_length": 10, "label": "   "})
    bound = TextBound.model_validate({"kind": "text", "max_length": 10, "label": " Your name "})
    assert bound.label == "Your name"


def test_the_cart_names_a_value_as_the_listing_does(listing: Listing) -> None:
    """The cart's labels follow the same rule as the listing page (#6137)."""
    labelled = _with_captions(
        listing, {"colour": "Letter colour (extruder 2)", "label": "Name (extruder 1)"}
    )
    bounds = dict(labelled.params)
    bounds["label"] = bounds["label"].model_copy(update={"label": "Your name"})
    found = _captions([labelled.model_copy(update={"params": bounds})])
    assert found[("demo", COMMIT)] == {"colour": "Letter colour", "label": "Your name"}


def test_the_cart_never_shows_another_listings_label(listing: Listing) -> None:
    """Two listings at one slug and commit label ``colour`` differently, and the cart
    item does not say which one it came from: it reads the caption's label, never the
    other listing's (#6142 review)."""
    labelled = _with_captions(listing, {"colour": "Letter colour (extruder 2)"})

    def with_label(listing_id: str, label: str | None) -> Listing:
        bounds = dict(labelled.params)
        bounds["colour"] = bounds["colour"].model_copy(update={"label": label})
        return labelled.model_copy(update={"id": listing_id, "params": bounds})

    found = _captions([with_label("a", "Letters"), with_label("b", "Rim colour")])
    assert found[("demo", COMMIT)]["colour"] == "Letter colour"
    found = _captions([with_label("a", "Letters"), with_label("b", None)])
    assert found[("demo", COMMIT)]["colour"] == "Letter colour"
    # Every listing at the pair agrees, so the cart shows the listings' own label.
    found = _captions([with_label("a", "Letters"), with_label("b", "Letters")])
    assert found[("demo", COMMIT)]["colour"] == "Letters"
    # A listing that does not offer the parameter has no say in its label.
    bounds = {k: v for k, v in labelled.params.items() if k != "colour"}
    locked = labelled.model_copy(update={"id": "c", "params": bounds})
    found = _captions([with_label("a", "Letters"), locked])
    assert found[("demo", COMMIT)]["colour"] == "Letters"


# -- one material, picked by colour, and support (#6152, #6258) -------------------------

PLA_RED_TOO = "pla-red-too"
PETG_RED = "petg-red"
PVA_RED = "pva-red"
WITH_RED_TOO = {
    **FILAMENTS,
    PLA_RED_TOO: filament(PLA_RED_TOO, "#FF0000"),
    PETG_RED: filament(PETG_RED, "#FF0000", "PETG"),
    PVA_RED: filament(PVA_RED, "#FF0000", "PVA"),
}
PETG_FIRST = ["PETG", "PLA"]


def _with_colours(listing: Listing, *ids: str, materials: list[str] | None = None) -> Listing:
    return listing.model_copy(update={"colours": list(ids), "materials": materials or []})


TWO_COLOURS = CustomizerSchema(
    parameters=[
        Parameter(name="colour", type="color", initial="#FF0000"),
        Parameter(name="trim", type="color", initial="#0000FF"),
    ]
)


def _two_colours(listing: Listing, *ids: str) -> Listing:
    assert listing.snapshot is not None
    snapshot = listing.snapshot.model_copy(update={"schema_": TWO_COLOURS, "fixed": {}})
    choices = {"kind": "choice", "choices": ["#FF0000", "#0000FF"]}
    return Listing.model_validate(
        {
            **listing.model_dump(),
            "params": {"colour": choices, "trim": choices},
            "fixed": {},
            "colours": list(ids),
            "snapshot": snapshot.model_dump(by_alias=True),
        }
    )


def test_the_listings_materials_win_over_the_shops(listing: Listing) -> None:
    assert effective_materials(listing, PETG_FIRST) == PETG_FIRST
    own = listing.model_copy(update={"materials": ["PLA"]})
    assert effective_materials(own, PETG_FIRST) == ["PLA"]


def test_a_customer_filament_is_of_an_allowed_material_and_never_support(
    listing: Listing,
) -> None:
    shown = _with_colours(listing, PVA_RED, PETG_RED, PLA_RED, PLA_BLUE)
    assert [f.id for f in customer_filaments(shown, WITH_RED_TOO, ["pla"])] == [
        PLA_RED,
        PLA_BLUE,
    ]
    assert [f.id for f in customer_filaments(shown, WITH_RED_TOO, PETG_FIRST)] == [
        PETG_RED,
        PLA_RED,
        PLA_BLUE,
    ]


def test_a_colour_of_a_material_not_allowed_is_refused(listing: Listing) -> None:
    shown = _with_colours(listing, PETG_RED, PLA_BLUE)
    with pytest.raises(InputOutsideListingError) as refused:
        listing_values(shown, {"colour": "#FF0000"}, WITH_RED_TOO, ["PLA"])
    assert (refused.value.name, refused.value.reason) == ("colour", "colour")


def test_the_public_listing_offers_only_the_allowed_materials(listing: Listing) -> None:
    shown = _public(
        _with_colours(listing, PETG_RED, PLA_BLUE, PVA_RED, materials=["PLA"]),
        WITH_RED_TOO,
        PETG_FIRST,
    )
    assert shown.materials == ["PLA"]
    assert [f.id for f in shown.colours] == [PLA_BLUE]
    colour = shown.params["colour"]
    assert isinstance(colour, PublicChoice) and colour.choices == ["#0000FF"]
    # With no materials of its own, the listing takes the shop's, in the shop's order.
    shop = _public(_with_colours(listing, PETG_RED, PLA_BLUE), WITH_RED_TOO, PETG_FIRST)
    assert shop.materials == PETG_FIRST
    assert [f.id for f in shop.colours] == [PETG_RED, PLA_BLUE]


def _resolve(
    listing: Listing,
    asked: dict[str, Any],
    materials: list[str],
    slots: dict[int, str] | None = None,
) -> ResolvedChoice:
    values = listing_values(listing, asked, WITH_RED_TOO, materials)
    return resolve_choice(listing, values, asked.keys(), WITH_RED_TOO, materials, slots=slots)


def test_the_given_colours_fix_the_first_material_that_offers_them(listing: Listing) -> None:
    two = _two_colours(listing, PLA_RED, PETG_RED, PLA_BLUE)
    both = _resolve(two, {"colour": "#FF0000", "trim": "#0000FF"}, PETG_FIRST)
    # PETG has no blue: PLA is the first material that has both.
    assert both.material == "PLA"
    assert (both.values["colour"], both.values["trim"]) == ("#FF0000", "#0000FF")
    red = _resolve(_two_colours(listing, PETG_RED, PLA_BLUE), {"colour": "#FF0000"}, PETG_FIRST)
    assert red.material == "PETG"


def test_a_colour_not_given_moves_to_the_closest_in_the_material(listing: Listing) -> None:
    """The trim was left alone: its default blue is only PLA's, and the visitor's red is
    only PETG's, so the trim takes PETG's closest colour among its choices."""
    two = _two_colours(listing, PETG_RED, PLA_BLUE)
    red = _resolve(two, {"colour": "#FF0000"}, PETG_FIRST)
    assert red.material == "PETG"
    assert red.values["trim"] == "#FF0000"


def test_the_material_that_keeps_the_defaults_wins(listing: Listing) -> None:
    """#6302 review: PETG and PLA both offer the visitor's red, and only PLA offers the
    trim's default blue, so PLA wins over the first material and the trim keeps blue."""
    two = _two_colours(listing, PLA_RED, PETG_RED, PLA_BLUE)
    red = _resolve(two, {"colour": "#FF0000"}, PETG_FIRST)
    assert red.material == "PLA"
    assert red.values["trim"] == "#0000FF"


def test_the_closest_colour_reads_each_filaments_alpha(listing: Listing) -> None:
    """#6258: the trim's default is a clear PLA blue (``alpha`` 0, its hex ``#RRGGBB``).
    PETG has no such blue, so the trim takes PETG's clear blue, not the opaque blue whose
    RGB is nearer."""
    clear = {
        "pla-clear-blue": filament("pla-clear-blue", "#0000FF").model_copy(update={"alpha": 0}),
        "petg-blue": filament("petg-blue", "#0A0AF0", "PETG"),
        "petg-clear-blue": filament("petg-clear-blue", "#3030C0", "PETG").model_copy(
            update={"alpha": 0}
        ),
    }
    book = {**WITH_RED_TOO, **clear}
    two = _two_colours(listing, PETG_RED, *clear)
    choices = {"kind": "choice", "choices": ["#FF0000", "#0000FF", "#0A0AF0", "#3030C0"]}
    two = Listing.model_validate(
        {**two.model_dump(by_alias=True), "params": {"colour": choices, "trim": choices}}
    )
    asked = {"colour": "#FF0000"}
    values = listing_values(two, asked, book, PETG_FIRST)
    resolved = resolve_choice(two, values, asked.keys(), book, PETG_FIRST)
    assert resolved.material == "PETG"
    assert resolved.values["trim"] == "#3030C0"


def test_with_nothing_given_the_listing_renders_as_the_shop_set_it(
    listing: Listing,
) -> None:
    """#6302 review: PETG is first but has no blue; PLA offers both defaults, so a bare
    listing is PLA and keeps them. Only when no material keeps them do they move."""
    two = _two_colours(listing, PLA_RED, PETG_RED, PLA_BLUE)
    shop = _resolve(two, {}, PETG_FIRST)
    assert shop.material == "PLA"
    assert (shop.values["colour"], shop.values["trim"]) == ("#FF0000", "#0000FF")
    split = _resolve(_two_colours(listing, PETG_RED, PLA_BLUE), {}, PETG_FIRST)
    assert split.material == "PETG"
    assert (split.values["colour"], split.values["trim"]) == ("#FF0000", "#FF0000")
    # A material the listing has no filament of is passed over.
    assert _resolve(_two_colours(listing, PLA_RED, PLA_BLUE), {}, PETG_FIRST).material == "PLA"


def test_a_default_of_a_material_no_longer_allowed_moves_not_refused(listing: Listing) -> None:
    """#6302 review: the trim's default blue is only PLA's, and the shop's materials now
    leave PLA out. A visitor who does not send the trim is not refused for it: the trim
    moves to the closest PETG colour."""
    two = _two_colours(listing, PETG_RED, PLA_BLUE)
    moved = _resolve(two, {}, ["PETG"])
    assert moved.material == "PETG"
    assert moved.values["trim"] == "#FF0000"
    # A colour the visitor sends is still refused for its material.
    with pytest.raises(InputOutsideListingError) as refused:
        _resolve(two, {"trim": "#0000FF"}, ["PETG"])
    assert (refused.value.name, refused.value.reason) == ("trim", "colour")


def test_colours_of_no_one_material_clash(listing: Listing) -> None:
    two = _two_colours(listing, PETG_RED, PLA_BLUE)
    with pytest.raises(ColoursClashError) as refused:
        _resolve(two, {"colour": "#FF0000", "trim": "#0000FF"}, PETG_FIRST)
    assert refused.value.parts == ["colour", "trim"]


def test_a_clash_names_the_visitors_colour_and_the_slot(listing: Listing) -> None:
    two = _two_colours(listing, PETG_RED, PLA_BLUE)
    with pytest.raises(ColoursClashError) as refused:
        _resolve(two, {"colour": "#FF0000"}, PETG_FIRST, slots={0: PLA_BLUE})
    # The trim (the shop's) fits PETG and PLA alike, so it is left out.
    assert refused.value.parts == ["colour", "colours.0"]


def test_a_slot_filament_must_be_an_allowed_customer_filament(listing: Listing) -> None:
    two = _two_colours(listing, PLA_RED, PETG_RED, PVA_RED, PLA_BLUE)
    for slot_filament in (PVA_RED, PETG_GREEN):
        with pytest.raises(InputOutsideListingError) as refused:
            _resolve(two, {}, ["PLA"], slots={0: slot_filament})
        assert refused.value.name == "colours.0"


def test_the_slots_take_the_resolved_materials_filament_of_their_colour(
    listing: Listing,
) -> None:
    two = _two_colours(listing, PVA_RED, PLA_RED, PETG_RED, PLA_BLUE)
    resolved = _resolve(two, {"colour": "#FF0000", "trim": "#0000FF"}, PETG_FIRST)
    # A slot already chosen stays; a colour the material lacks is left to the shop.
    assert slot_filaments({2: PLA_RED_TOO}, resolved, ["#ff0000", "#0000FF", "#00ff00"]) == {
        0: PLA_RED,
        1: PLA_BLUE,
        2: PLA_RED_TOO,
    }
    # Red alone is PETG's once PLA has no red.
    petg = _resolve(
        _two_colours(listing, PVA_RED, PETG_RED, PLA_BLUE), {"colour": "#FF0000"}, PETG_FIRST
    )
    assert slot_filaments({}, petg, ["#FF0000"]) == {0: PETG_RED}


def test_a_print_never_mixes_materials(listing: Listing) -> None:
    two = _two_colours(listing, PETG_RED, PLA_BLUE)
    resolved = _resolve(two, {"colour": "#FF0000"}, PETG_FIRST)
    with pytest.raises(ColoursClashError) as refused:
        check_one_material(
            [ColourSlot(slot=3, colour="#0000FF", filament_id=PLA_BLUE)], resolved, WITH_RED_TOO
        )
    assert refused.value.parts == ["colours.3"]
    # Support is the shop's, and is not counted.
    check_one_material(
        [
            ColourSlot(slot=0, colour="#FF0000", filament_id=PETG_RED),
            ColourSlot(slot=1, colour="#FF0000", filament_id=PVA_RED),
        ],
        resolved,
        WITH_RED_TOO,
    )


def test_a_slot_is_never_a_support_filament(listing: Listing) -> None:
    listing = _with_colours(listing, PLA_RED, PVA_RED)
    values = listing_values(listing, {}, WITH_RED_TOO, SHOP)
    with pytest.raises(InputOutsideListingError) as refused:
        listing_reference(listing, values, {0: PVA_RED}, filaments=WITH_RED_TOO)
    assert refused.value.name == "colours.0"


def test_the_public_listing_never_shows_a_support_filament(listing: Listing) -> None:
    support_blue = filament("pva-blue", "#0000FF", "PVA")
    shown = _public(
        _with_colours(listing, PLA_RED, "pva-blue", materials=["PLA", "PVA"]),
        {**FILAMENTS, "pva-blue": support_blue},
    )
    assert [f.id for f in shown.colours] == [PLA_RED]
    colour = shown.params["colour"]
    assert isinstance(colour, PublicChoice) and colour.choices == ["#ff0000"]


def test_the_estimate_prices_a_slot_at_the_resolved_material(listing: Listing) -> None:
    listing = _with_colours(listing, PLA_RED, PETG_RED)
    chosen = _filament_for(listing, WITH_RED_TOO, {}, ["#FF0000"], "PETG")
    assert chosen["#ff0000"].id == PETG_RED
    # A row with no material: the first listing filament of the colour.
    assert _filament_for(listing, WITH_RED_TOO, {}, ["#FF0000"])["#ff0000"].id == PLA_RED


def test_the_estimate_prices_a_locked_support_colour(listing: Listing) -> None:
    """#6152: a support filament is never a customer's pick, but a locked support colour
    is still a slot of the render, and the estimate prices it at that filament."""
    natural = filament("pva-natural", "#F4F1E8", "PVA")
    listing = _with_colours(listing, PLA_RED, natural.id)
    chosen = _filament_for(
        listing, {**FILAMENTS, natural.id: natural}, {}, ["#FF0000", "#F4F1E8"], "PLA"
    )
    assert chosen["#f4f1e8"].id == natural.id
    assert chosen["#ff0000"].id == PLA_RED


def test_a_render_accepts_and_ignores_old_picks() -> None:
    """#6258: a pick is the colour in ``values``. A page from before it still sends
    ``picks``: it is taken and never dumped (#6302 review), so it changes no request key."""
    body = {"listing_id": "l", "listing_updated_at": NOW.isoformat()}
    old = RenderBody.model_validate({**body, "picks": {"colour": "f"}})
    assert "picks" not in old.model_dump(mode="json")
    assert old.model_dump() == RenderBody.model_validate(body).model_dump()
    with pytest.raises(ValidationError):
        RenderBody.model_validate({**body, "one_material": True})


def test_a_listings_materials_are_kept_in_order_without_repeats() -> None:
    fields = {"slug": "demo", "commit": COMMIT, "title": "Demo"}
    kept = ListingInput.model_validate({**fields, "materials": [" PLA", "petg", "pla", ""]})
    assert kept.materials == ["PLA", "petg"]
    with pytest.raises(ValidationError):
        ListingInput.model_validate({**fields, "materials": [f"M{n}" for n in range(17)]})
    # One material is always on (#6258). A back office from before it still sends the old
    # switch: for one release it is taken and ignored, never dumped (#6302 review).
    old = ListingInput.model_validate({**fields, "one_material": True})
    assert "one_material" not in old.model_dump(mode="json")


def test_publish_refuses_a_filament_of_a_material_not_allowed(listing: Listing) -> None:
    body = _with_colours(listing, PLA_RED, PETG_RED, PVA_RED)
    materials = {f.id: f.material for f in WITH_RED_TOO.values()}
    with pytest.raises(ApiError) as refused:
        check_materials(body, materials, ["PLA"])
    assert refused.value.status == 422
    assert refused.value.type.endswith("listing_material_not_allowed")
    assert refused.value.extensions["name"] == PETG_RED
    # The support filament is the shop's and is not checked.
    check_materials(body, materials, PETG_FIRST)


def test_an_unpicked_value_is_the_listings_default(listing: Listing) -> None:
    """#6144: the listing's default stands in for the template's while the customer
    picks none; a picked value still wins."""
    bounds = dict(listing.params)
    bounds["colour"] = bounds["colour"].model_copy(update={"default": "#0000FF"})
    bounds["width"] = bounds["width"].model_copy(update={"default": 30})
    defaulted = listing.model_copy(update={"params": bounds})
    values = listing_values(defaulted, {}, FILAMENTS, SHOP)
    assert (values["colour"], values["width"]) == ("#0000FF", 30)
    assert listing_values(defaulted, {"width": 40}, FILAMENTS, SHOP)["width"] == 40


def test_the_page_preselects_what_an_unpicked_render_takes(listing: Listing) -> None:
    bounds = dict(listing.params)
    bounds["width"] = bounds["width"].model_copy(update={"default": 30})
    bounds["label"] = bounds["label"].model_copy(update={"default": "Ada"})
    shown = _public(listing.model_copy(update={"params": bounds}), FILAMENTS).params
    assert shown["width"].default == 30
    assert shown["shape"].default == "round"
    assert shown["colour"].default == "#FF0000"
    assert shown["label"].default == "Ada"
    assert getattr(shown["label"], "placeholder", None) == "Ada"


def test_a_colour_default_no_filament_is_left_for_is_not_shown(listing: Listing) -> None:
    """The template's red with red archived: the page preselects nothing."""
    left = {k: v for k, v in FILAMENTS.items() if k != PLA_RED}
    assert _public(listing, left).params["colour"].default is None


def test_a_choice_default_outside_its_choices_is_not_shown(listing: Listing) -> None:
    """A listing saved before #6144 may start a choice outside what it offers: the page
    preselects nothing rather than a value it cannot show."""
    bounds = dict(listing.params)
    bounds["shape"] = bounds["shape"].model_copy(update={"default": "square"})
    shown = _public(listing.model_copy(update={"params": bounds}), FILAMENTS).params
    assert shown["shape"].default is None


@pytest.mark.parametrize(
    "update",
    [{"min": 60, "max": 80}, {"default": 85}, {"default": 30.5}],
    ids=["template default below range", "listing default above range", "off step"],
)
def test_a_number_default_outside_its_bound_is_not_shown(
    listing: Listing, update: dict[str, float]
) -> None:
    """A listing saved before #6144 may start a number outside its range or off its step
    (the template's 50 on a 60 to 80 field): the page shows no default it would refuse."""
    bounds = dict(listing.params)
    bounds["width"] = bounds["width"].model_copy(update=update)
    shown = _public(listing.model_copy(update={"params": bounds}), FILAMENTS).params
    assert shown["width"].default is None


def test_a_choice_default_is_matched_as_publish_matches_it(listing: Listing) -> None:
    """``True`` is not ``1`` (`same_value`): the page shows no default publish would refuse."""
    bounds = dict(listing.params)
    bounds["shape"] = bounds["shape"].model_copy(update={"choices": [1], "default": True})
    shown = _public(listing.model_copy(update={"params": bounds}), FILAMENTS).params
    assert shown["shape"].default is None


# -- the shop's locked colours fix the material too (#6302 review) ----------------------

PLA_BLACK = "pla-black"
PLA_WHITE = "pla-white"
PETG_WHITE = "petg-white"
LOCKED = {
    **WITH_RED_TOO,
    PLA_BLACK: filament(PLA_BLACK, "#000000"),
    PLA_WHITE: filament(PLA_WHITE, "#FFFFFF"),
    PETG_WHITE: filament(PETG_WHITE, "#FFFFFF", "PETG"),
}
BASE_AND_TEXT = CustomizerSchema(
    parameters=[
        Parameter(name="base", type="color", initial="#000000"),
        Parameter(name="text", type="color", initial="#FFFFFF"),
    ]
)


def _locked(
    listing: Listing,
    ids: list[str],
    fixed: dict[str, Any],
    schema: CustomizerSchema = BASE_AND_TEXT,
    **params: Any,
) -> Listing:
    """A listing of ``schema`` whose ``fixed`` colours are locked, offering ``params``."""
    assert listing.snapshot is not None
    snapshot = listing.snapshot.model_copy(update={"schema_": schema, "fixed": fixed})
    return Listing.model_validate(
        {
            **listing.model_dump(),
            "params": params,
            "fixed": fixed,
            "colours": ids,
            "snapshot": snapshot.model_dump(by_alias=True),
        }
    )


def test_a_locked_colour_fixes_the_material(listing: Listing) -> None:
    """The review's case: the base is locked to black, which only PLA has; the text is
    offered white, in PETG and PLA. The untouched page prints in PLA, not PETG (the first
    material that keeps white), and every slot holds a PLA filament."""
    shown = _locked(
        listing,
        [PLA_BLACK, PETG_WHITE, PLA_WHITE],
        {"base": "#000000"},
        text={"kind": "choice", "choices": ["#FFFFFF"]},
    )
    values = listing_values(shown, {}, LOCKED, PETG_FIRST)
    resolved = resolve_choice(shown, values, (), LOCKED, PETG_FIRST)
    assert resolved.material == "PLA"
    slots = slot_filaments({}, resolved, ["#000000", "#FFFFFF"])
    assert slots == {0: PLA_BLACK, 1: PLA_WHITE}
    assert {LOCKED[f].material for f in slots.values()} == {"PLA"}


def test_a_clash_with_a_lock_names_only_the_visitors_colours(listing: Listing) -> None:
    """Green is only PETG's and the locked black only PLA's: the refusal names the
    visitor's text, or the visitor's slot, never the shop's base."""
    shown = _locked(
        listing,
        [PLA_BLACK, PETG_WHITE, PLA_WHITE, PETG_GREEN],
        {"base": "#000000"},
        text={"kind": "choice", "choices": ["#FFFFFF", "#00FF00"]},
    )
    values = listing_values(shown, {"text": "#00FF00"}, LOCKED, PETG_FIRST)
    with pytest.raises(ColoursClashError) as refused:
        resolve_choice(shown, values, ["text"], LOCKED, PETG_FIRST)
    assert refused.value.parts == ["text"]
    untouched = listing_values(shown, {}, LOCKED, PETG_FIRST)
    with pytest.raises(ColoursClashError) as slot:
        resolve_choice(shown, untouched, [], LOCKED, PETG_FIRST, slots={0: PETG_WHITE})
    assert slot.value.parts == ["colours.0"]


def test_locks_of_no_one_material_make_the_listing_unavailable(listing: Listing) -> None:
    """Published before publish checked its locks: black is only PLA's, green only
    PETG's. Whatever the visitor gives, `LockedColoursClashError` names the locks."""
    shown = _locked(listing, [PLA_BLACK, PETG_GREEN], {"base": "#000000", "text": "#00FF00"})
    values = listing_values(shown, {}, LOCKED, PETG_FIRST)
    with pytest.raises(LockedColoursClashError) as refused:
        resolve_choice(shown, values, (), LOCKED, PETG_FIRST)
    assert refused.value.names == ["base", "text"]


def test_a_locked_support_colour_does_not_fix_the_material(listing: Listing) -> None:
    """#6152: a support colour is the shop's choice, of its own material."""
    support = CustomizerSchema(
        parameters=[
            Parameter(name="support", type="color", initial="#FF0000"),
            Parameter(name="text", type="color", initial="#FFFFFF"),
        ]
    )
    shown = _locked(
        listing,
        [PVA_RED, PETG_WHITE],
        {"support": "#FF0000"},
        support,
        text={"kind": "choice", "choices": ["#FFFFFF"]},
    )
    values = listing_values(shown, {}, LOCKED, PETG_FIRST)
    assert resolve_choice(shown, values, (), LOCKED, PETG_FIRST).material == "PETG"


def test_publish_refuses_locks_of_no_one_material(listing: Listing) -> None:
    colours = {f.id: f.colour for f in LOCKED.values()}
    materials = {f.id: f.material for f in LOCKED.values()}
    # Black is only PLA's, green only PETG's.
    body = _locked(listing, [PLA_BLACK, PETG_GREEN, PETG_WHITE], {})
    fixed = {"base": "#000000", "text": "#00FF00"}
    with pytest.raises(ApiError) as refused:
        check_locked_colours(BASE_AND_TEXT, fixed, body, colours, materials, PETG_FIRST)
    assert refused.value.status == 422
    assert refused.value.type.endswith("listing_locked_colours_unprintable")
    assert refused.value.extensions["names"] == ["base", "text"]
    # PLA has black and white: one material offers both locks.
    both = _locked(listing, [PLA_BLACK, PLA_WHITE, PETG_WHITE], {})
    fixed = {"base": "#000000", "text": "#FFFFFF"}
    check_locked_colours(BASE_AND_TEXT, fixed, both, colours, materials, PETG_FIRST)


def test_two_slots_of_two_materials_clash_and_name_only_the_slots(listing: Listing) -> None:
    """The visitor gives no colour; its two slots are of two materials. The slots alone
    cause the clash, so the refusal names them, not the shop's colours (#6302 review)."""
    two = _two_colours(listing, PLA_RED, PETG_RED, PLA_BLUE)
    with pytest.raises(ColoursClashError) as refused:
        _resolve(two, {}, PETG_FIRST, slots={0: PLA_RED, 1: PETG_RED})
    assert refused.value.parts == ["colours.0", "colours.1"]


def test_a_material_with_stray_whitespace_is_still_its_material(listing: Listing) -> None:
    """#6302 review: materials compare trimmed, as `unique_materials` keeps them."""
    book = {**FILAMENTS, PETG_RED: filament(PETG_RED, "#FF0000", " PETG \t")}
    shown = _with_colours(listing, PETG_RED, PLA_BLUE)
    assert [f.id for f in customer_filaments(shown, book, ["petg"])] == [PETG_RED]
    values = listing_values(shown, {"colour": "#FF0000"}, book, PETG_FIRST)
    assert resolve_choice(shown, values, ["colour"], book, PETG_FIRST).material == "PETG"


# -- only printable materials count (#6302 review) --------------------------------------


def _card(listing: Listing, filaments: dict[str, Filament], shop: list[str]) -> bool:
    # No media, so the content store is never read.
    card = asyncio.run(public_card(cast(ContentStore, None), listing, filaments, shop))
    return card.available


def _review_shape(listing: Listing) -> Listing:
    """The review's case: the base is locked to black, which only PLA has; the text is
    offered only in PETG colours."""
    return _locked(
        listing,
        [PLA_BLACK, PETG_WHITE, PETG_GREEN],
        {"base": "#000000"},
        text={"kind": "choice", "choices": ["#FFFFFF", "#00FF00"]},
    )


def test_a_lock_and_an_offered_colour_of_two_materials_print_in_none(listing: Listing) -> None:
    shown = _review_shape(listing)
    assert printable_materials(shown, LOCKED, PETG_FIRST) == []
    # Publish refuses it; one published before the rule cannot render, names no
    # visitor's colour, and its card and page say it cannot be ordered.
    colours = {f.id: f.colour for f in LOCKED.values()}
    materials = {f.id: f.material for f in LOCKED.values()}
    assert shown.snapshot is not None
    with pytest.raises(ApiError) as refused:
        check_locked_colours(
            BASE_AND_TEXT, shown.snapshot.fixed, shown, colours, materials, PETG_FIRST
        )
    assert refused.value.type.endswith("listing_locked_colours_unprintable")
    assert refused.value.extensions["names"] == ["base"]
    values = listing_values(shown, {}, LOCKED, PETG_FIRST)
    with pytest.raises(LockedColoursClashError) as clash:
        resolve_choice(shown, values, (), LOCKED, PETG_FIRST)
    assert clash.value.names == ["base"]
    assert _card(shown, LOCKED, PETG_FIRST) is False
    page = _public(shown, LOCKED, PETG_FIRST)
    assert page.available is False
    assert page.materials == PETG_FIRST and page.colours == []


def test_only_the_printable_materials_colours_are_offered(listing: Listing) -> None:
    """PETG has no black for the locked base, so only PLA prints: the text offers PLA's
    white only, never PETG's green, and the page lists PLA's filaments only (its
    materials stay the effective list)."""
    shown = _locked(
        listing,
        [PLA_BLACK, PLA_WHITE, PETG_WHITE, PETG_GREEN],
        {"base": "#000000"},
        text={"kind": "choice", "choices": ["#FFFFFF", "#00FF00"]},
    )
    assert printable_materials(shown, LOCKED, PETG_FIRST) == ["PLA"]
    page = _public(shown, LOCKED, PETG_FIRST)
    assert page.available is True
    assert page.materials == PETG_FIRST
    assert [f.id for f in page.colours] == [PLA_BLACK, PLA_WHITE]
    text = page.params["text"]
    assert isinstance(text, PublicChoice) and text.choices == ["#FFFFFF"]
    assert _card(shown, LOCKED, PETG_FIRST) is True


NO_COLOURS = CustomizerSchema(
    parameters=[Parameter(name="size", type="number", initial=2, min=1, max=5)]
)


def test_a_listing_with_no_colour_to_print_stays_available(listing: Listing) -> None:
    """No colour offered and none locked: the shop picks the slots, so no material is
    needed and the listing can be ordered."""
    shown = _locked(listing, [], {}, NO_COLOURS, size={"kind": "number", "min": 1, "max": 5})
    assert printable_materials(shown, LOCKED, PETG_FIRST) == []
    assert _card(shown, LOCKED, PETG_FIRST) is True
    values = listing_values(shown, {}, LOCKED, PETG_FIRST)
    assert resolve_choice(shown, values, (), LOCKED, PETG_FIRST).material is None
