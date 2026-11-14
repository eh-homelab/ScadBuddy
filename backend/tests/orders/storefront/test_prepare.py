"""A listing's colour combinations (#6237), with no database: which colours each offered
colour parameter takes, how many combinations they make, and that each one is asked as a
visitor's page asks for it. The routes, and a visitor's reuse of a prepared render, are
``tests/api/test_storefront_prepare.py``."""

from __future__ import annotations

from typing import Any

import pytest

from scadbuddy.orders.storefront.inputs import listing_reference, listing_values
from scadbuddy.orders.storefront.prepare import (
    colour_choices,
    combination_count,
    combinations,
)
from scadbuddy.orders.storefront.wire import Listing, ListingSnapshot
from scadbuddy.render.schema import CustomizerSchema, Parameter
from tests.orders.storefront.test_inputs import (
    COMMIT,
    FILAMENTS,
    NOW,
    PETG_GREEN,
    PLA_BLUE,
    PLA_RED,
)

SCHEMA = CustomizerSchema(
    parameters=[
        Parameter(name="width", type="number", initial=50, min=10, max=100, step=1),
        Parameter(name="body", type="color", initial="#FF0000"),
        Parameter(name="trim", type="color", initial="#0000FF"),
        Parameter(name="height", type="number", initial=5),
    ]
)


def a_listing(params: dict[str, Any], colours: list[str] | None = None) -> Listing:
    return Listing.model_validate(
        {
            "id": "listing-1",
            "slug": "demo",
            "commit": COMMIT,
            "title": "Demo",
            "params": params,
            "fixed": {},
            "colours": colours if colours is not None else [PLA_RED, PLA_BLUE],
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


BOTH = {"kind": "choice", "choices": ["#ff0000", "#0000FF"]}


def test_each_colour_is_taken_once_whatever_its_case() -> None:
    listing = a_listing(
        {"body": {"kind": "choice", "choices": ["#ff0000", "#FF0000", "#0000ff", "#0000FF"]}}
    )
    assert colour_choices(listing, FILAMENTS) == {"body": ["#ff0000", "#0000ff"]}


def test_a_colour_the_public_listing_does_not_offer_is_left_out() -> None:
    # Green is in the price book but not the listing's; blue's filament was archived.
    book = {k: v for k, v in FILAMENTS.items() if k != PLA_BLUE}
    listing = a_listing(
        {"body": {"kind": "choice", "choices": ["#ff0000", "#0000FF", "#00FF00"]}},
        colours=[PLA_RED, PLA_BLUE],
    )
    assert PETG_GREEN in book
    assert colour_choices(listing, book) == {"body": ["#ff0000"]}


def test_a_support_filament_is_never_a_combination() -> None:
    """#6152: support is the shop's choice, so its colour is never prepared."""
    pva = FILAMENTS[PETG_GREEN].model_copy(update={"material": "PVA"})
    book = {**FILAMENTS, PETG_GREEN: pva}
    listing = a_listing(
        {"body": {"kind": "choice", "choices": ["#ff0000", "#00FF00"]}},
        colours=[PLA_RED, PETG_GREEN],
    )
    assert colour_choices(listing, book) == {"body": ["#ff0000"]}


def test_the_count_is_the_product_of_the_colour_parameters() -> None:
    listing = a_listing(
        {"body": BOTH, "trim": BOTH, "width": {"kind": "number", "min": 20, "max": 80}}
    )
    choices = colour_choices(listing, FILAMENTS)
    assert set(choices) == {"body", "trim"}
    assert combination_count(choices) == 4
    assert len(list(combinations(listing, FILAMENTS, choices))) == 4


def test_no_colour_parameter_is_no_combination() -> None:
    listing = a_listing({"width": {"kind": "number", "min": 20, "max": 80}})
    assert combination_count(colour_choices(listing, FILAMENTS)) == 0


@pytest.mark.parametrize("default", [None, "#FF0000"])
def test_a_colour_at_its_default_is_not_asked(default: str | None) -> None:
    """The page sends only what the visitor set, so the combination of the defaults asks
    for nothing: its render key is the page's first render's."""
    body = {**BOTH, "default": default}
    listing = a_listing({"body": body, "width": {"kind": "number", "min": 20, "max": 80}})
    every = list(combinations(listing, FILAMENTS, colour_choices(listing, FILAMENTS)))
    assert [c.asked for c in every] == [{}, {"body": "#0000FF"}]
    first_load = listing_reference(
        listing, listing_values(listing, {}, FILAMENTS), {}, filaments=FILAMENTS
    )
    assert every[0].reference.render_key == first_load.reference.render_key


def test_a_picked_colour_has_the_render_key_of_a_visitors_pick() -> None:
    listing = a_listing({"body": BOTH, "trim": BOTH})
    every = list(combinations(listing, FILAMENTS, colour_choices(listing, FILAMENTS)))
    for combination in every:
        visitor = listing_reference(
            listing, listing_values(listing, combination.asked, FILAMENTS), {}, filaments=FILAMENTS
        )
        assert combination.reference.render_key == visitor.reference.render_key
    assert len({c.reference.render_key for c in every}) == 4
    # Every other parameter stays at its default.
    assert all(c.reference.inputs["params"]["width"] == 50 for c in every)
