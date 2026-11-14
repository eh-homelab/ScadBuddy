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

from scadbuddy.orders.models import ColourSlot, Filament
from scadbuddy.orders.storefront.drafts import _captions
from scadbuddy.orders.storefront.inputs import (
    InputOutsideListingError,
    listing_reference,
    listing_values,
)
from scadbuddy.orders.storefront.listings import public_listing
from scadbuddy.orders.storefront.wire import (
    Listing,
    ListingSnapshot,
    PublicChoice,
    PublicListing,
    PublicText,
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
        listing_values(listing, asked, FILAMENTS)
    assert refused.value.reason == reason
    assert refused.value.name == next(iter(asked))


def test_the_values_are_the_fixed_and_the_asked(listing: Listing) -> None:
    values = listing_values(listing, {"width": 40, "colour": "#0000ff", "label": "Ada"}, FILAMENTS)
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
        listing_values(listing, {"colour": "#0000FF"}, without_blue)
    assert refused.value.reason == "colour"


def test_an_unpublished_listing_has_no_values(listing: Listing) -> None:
    with pytest.raises(ValueError, match="not published"):
        listing_values(listing.model_copy(update={"snapshot": None}), {}, FILAMENTS)


def test_the_reference_comes_from_the_snapshot(listing: Listing) -> None:
    values = listing_values(listing, {"width": 40}, FILAMENTS)
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
    values = listing_values(listing, {}, FILAMENTS)
    with pytest.raises(InputOutsideListingError) as refused:
        listing_reference(listing, values, chosen, filaments=FILAMENTS)
    assert (refused.value.name, refused.value.reason) == ("colours.0", "colour")


def _public(listing: Listing, filaments: dict[str, Filament]) -> PublicListing:
    # No media, so the content store is never read.
    return asyncio.run(public_listing(cast(ContentStore, None), listing, filaments))


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
    assert listing_values(unlocked, {}, FILAMENTS)["height"] == 7
    with pytest.raises(InputOutsideListingError) as refused:
        listing_values(unlocked, {"height": 7}, FILAMENTS)
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
    assert listing_values(at_b, {}, FILAMENTS)["height"] == 9


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
