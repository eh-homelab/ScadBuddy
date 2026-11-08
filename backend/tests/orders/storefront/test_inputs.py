"""A visitor's inputs, checked against the listing (#5282, spec 2026-11-02 §5.4), and the
reference built from the listing's snapshot with no git (plan point 2). The parity test
against `orders/reference.py` `resolve_reference` needs git and the API: it is in
``tests/api/test_storefront_public.py``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from scadbuddy.orders.models import ColourSlot, Filament
from scadbuddy.orders.storefront.inputs import (
    InputOutsideListingError,
    listing_reference,
    listing_values,
)
from scadbuddy.orders.storefront.wire import Listing, ListingSnapshot
from scadbuddy.render.job_models import render_key
from scadbuddy.render.schema import CustomizerSchema, Option, Parameter

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
