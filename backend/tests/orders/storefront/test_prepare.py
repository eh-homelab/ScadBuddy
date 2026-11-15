"""A listing's colour combinations (#6237), with no database: which colours each offered
colour parameter takes, how many combinations they make, and that each one is asked as a
visitor's page asks for it. The routes, and a visitor's reuse of a prepared render, are
``tests/api/test_storefront_prepare.py``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.client import WorkflowExecutionStatus

from scadbuddy.orders.storefront.inputs import listing_reference, listing_values
from scadbuddy.orders.storefront.prepare import (
    RunState,
    TemporalPreparer,
    colour_choices,
    combination_at,
    combination_count,
    combinations,
    not_started,
    request_id,
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
    filament,
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


def test_each_colour_is_taken_once_in_its_filaments_spelling() -> None:
    """The page sends a picked filament's own ``colour``, not the bound's spelling."""
    listing = a_listing(
        {"body": {"kind": "choice", "choices": ["#ff0000", "#FF0000", "#0000ff", "#0000FF"]}}
    )
    assert colour_choices(listing, FILAMENTS) == {"body": ["#FF0000", "#0000FF"]}


def test_two_filaments_of_one_colour_in_two_cases_are_each_prepared() -> None:
    """#6299 review: a visitor's pick of either filament reuses a prepared job."""
    book = {**FILAMENTS, "pla-red-lower": filament("pla-red-lower", "#ff0000")}
    listing = a_listing(
        {"trim": {"kind": "choice", "choices": ["#FF0000", "#0000FF"]}},
        colours=[PLA_RED, "pla-red-lower", PLA_BLUE],
    )
    choices = colour_choices(listing, book)
    # The default (#0000FF) is the blue filament's spelling, so it is not added again.
    assert choices == {"trim": ["#FF0000", "#ff0000", "#0000FF"]}
    prepared = {c.reference.render_key for c in combinations(listing, book, choices)}
    for picked in ("pla-red", "pla-red-lower", "pla-blue"):
        visitor = listing_reference(
            listing,
            listing_values(listing, {"trim": book[picked].colour}, book),
            {},
            filaments=book,
        )
        assert visitor.reference.render_key in prepared


def test_a_default_no_filament_spells_so_is_prepared_too() -> None:
    """The page's first load renders the default in its own spelling."""
    listing = a_listing({"body": {**BOTH, "default": "#ff0000"}})
    choices = colour_choices(listing, FILAMENTS)
    assert choices == {"body": ["#ff0000", "#FF0000", "#0000FF"]}
    every = list(combinations(listing, FILAMENTS, choices))
    assert [c.asked for c in every] == [{}, {"body": "#FF0000"}, {"body": "#0000FF"}]
    first_load = listing_reference(
        listing, listing_values(listing, {}, FILAMENTS), {}, filaments=FILAMENTS
    )
    assert every[0].reference.render_key == first_load.reference.render_key


def test_a_colour_the_public_listing_does_not_offer_is_left_out() -> None:
    # Green is in the price book but not the listing's; blue's filament was archived.
    book = {k: v for k, v in FILAMENTS.items() if k != PLA_BLUE}
    listing = a_listing(
        {"body": {"kind": "choice", "choices": ["#ff0000", "#0000FF", "#00FF00"]}},
        colours=[PLA_RED, PLA_BLUE],
    )
    assert PETG_GREEN in book
    assert colour_choices(listing, book) == {"body": ["#FF0000"]}


def test_a_support_filament_is_never_a_combination() -> None:
    """#6152: support is the shop's choice, so its colour is never prepared."""
    pva = FILAMENTS[PETG_GREEN].model_copy(update={"material": "PVA"})
    book = {**FILAMENTS, PETG_GREEN: pva}
    listing = a_listing(
        {"body": {"kind": "choice", "choices": ["#ff0000", "#00FF00"]}},
        colours=[PLA_RED, PETG_GREEN],
    )
    assert colour_choices(listing, book) == {"body": ["#FF0000"]}


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


def test_the_combination_at_an_index_is_the_one_in_the_run_order() -> None:
    """The prepare run asks for each combination by its index (#6299 review)."""
    three = {"kind": "choice", "choices": ["#ff0000", "#0000FF", "#00FF00"]}
    listing = a_listing({"body": three, "trim": BOTH}, colours=[PLA_RED, PLA_BLUE, PETG_GREEN])
    choices = colour_choices(listing, FILAMENTS)
    every = list(combinations(listing, FILAMENTS, choices))
    assert len(every) == combination_count(choices) == 6
    assert [combination_at(listing, FILAMENTS, choices, i) for i in range(6)] == every


def test_each_run_makes_its_own_claim() -> None:
    """#6299 review: a retry in one run joins its claim; "Prepare again" is a new run, so a
    failed render is rendered again rather than answered with the first run's outcome."""
    one = request_id("listing-1", "run-a", "key")
    assert one == request_id("listing-1", "run-a", "key")
    assert one != request_id("listing-1", "run-b", "key")


def test_a_failed_submit_rendered_since_is_not_counted() -> None:
    run = RunState(running=False, failed_indices=(0, 2, 9))
    # Index 0 was rendered since; 2 has a failed job; 9 is past the combinations.
    assert not_started(run, ["done", "done", "failed"]) == 1
    assert not_started(RunState(running=False, legacy_failed=3), ["done"]) == 3


class _Client:
    """A Temporal client whose run is ``open`` (or not) before the start, and whose start
    answers ``answered`` (run id, first execution run id)."""

    def __init__(self, open_run: bool, answered: tuple[str, str]) -> None:
        self.open_run = open_run
        self.answered = answered

    def get_workflow_handle(self, _id: str) -> Any:
        async def memo() -> dict[str, Any]:
            return {}

        async def describe() -> Any:
            return SimpleNamespace(
                status=WorkflowExecutionStatus.RUNNING
                if self.open_run
                else WorkflowExecutionStatus.COMPLETED,
                run_id="run-a",
                memo=memo,
                raw_description=SimpleNamespace(
                    workflow_execution_info=SimpleNamespace(first_run_id="first-a")
                ),
            )

        return SimpleNamespace(describe=describe)

    async def start_workflow(self, *_args: Any, **_kwargs: Any) -> Any:
        run_id, first = self.answered
        return SimpleNamespace(result_run_id=run_id, first_execution_run_id=first)


@pytest.mark.parametrize(
    ("open_run", "answered", "started"),
    [
        (False, ("run-b", "run-b"), True),
        (True, ("run-a", "first-a"), False),
        # Continued as new since the read: still the open run's chain.
        (True, ("run-c", "first-a"), False),
        # The open run ended between the read and the start: this start made a new run.
        (True, ("run-d", "run-d"), True),
    ],
)
async def test_started_is_read_from_the_starts_answer(
    open_run: bool, answered: tuple[str, str], started: bool
) -> None:
    preparer = TemporalPreparer(_Client(open_run, answered), "library")  # type: ignore[arg-type]
    assert await preparer.start("listing-1") is started
