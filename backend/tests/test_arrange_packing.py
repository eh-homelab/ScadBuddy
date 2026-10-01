"""The Arrange packer (spec 2026-09-27 §7): goals, rotation, real plates."""

from __future__ import annotations

import itertools

import pytest

from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.plate import fit_problem, plate_for
from scadbuddy.template import Part
from scadbuddy.workflows.arrange import GOALS, arrange, geometry_of, order_plates, signature_of
from scadbuddy.workflows.models import Layout, PackItem, PlateSize, SlotPlan
from scadbuddy.workflows.packing import GAP_MM, PackError, explicit_plate

DEFAULT = PlateSize(key="default", width=256.0, depth=256.0)


def plate(model: str) -> PlateSize:
    g = plate_for(model)
    return PlateSize(key=g.key, width=g.usable.width, depth=g.usable.depth)


def part(key: str, w: float, d: float, *colours: str, h: float = 5.0, plates: int = 1) -> Part:
    return Part(
        piece_key=key,
        file="model.scad",
        colours=list(colours or ("#FF0000",)),
        plates=plates,
        bbox=BoundingBox(min=(0, 0, 0), max=(w, d, h), size=(w, d, h)),
    )


def footprints(
    layout: Layout, parts: dict[str, Part]
) -> list[list[tuple[float, float, float, float]]]:
    out = []
    for p in layout.plates:
        rects = []
        for placed in p.items:
            w, d = parts[placed.piece_key].bbox.size[:2]
            if placed.rot % 180:
                w, d = d, w
            rects.append((placed.x, placed.y, placed.x + w, placed.y + d))
        out.append(rects)
    return out


def assert_no_overlap(rects: list[tuple[float, float, float, float]]) -> None:
    for a, b in itertools.combinations(rects, 2):
        assert (
            a[2] + GAP_MM <= b[0] + 1e-6
            or b[2] + GAP_MM <= a[0] + 1e-6
            or a[3] + GAP_MM <= b[1] + 1e-6
            or b[3] + GAP_MM <= a[1] + 1e-6
        ), (a, b)


def test_one_part_alone_keeps_its_own_plates() -> None:
    assert arrange([PackItem(part=part("a", 10, 10, plates=3))], DEFAULT) == Layout(own="a")


def test_arrange_packs_even_one_object() -> None:
    # Arrange passes allow_own=False: the piece as rendered would skip the writer, and
    # with it the pinned filament order (Review Focus 4).
    layout = arrange([PackItem(part=part("a", 10, 10))], DEFAULT, allow_own=False)
    assert layout.own is None
    assert [[p.piece_key for p in plate.items] for plate in layout.plates] == [["a"]]


def test_copies_never_overlap_and_fill_a_plate_before_starting_another() -> None:
    a = part("a", 60, 40)
    layout = arrange([PackItem(part=a, count=12)], DEFAULT)
    assert len(layout.plates) == 1
    assert_no_overlap(footprints(layout, {"a": a})[0])


def test_a_long_part_is_turned_to_share_a_plate() -> None:
    # 240 x 60 beside 150 x 200 fits one 256 mm plate only turned (60 x 240 in the strip
    # right of the square); unturned it needs a second plate.
    long, square = part("long", 240, 60), part("sq", 150, 200)
    layout = arrange([PackItem(part=long), PackItem(part=square)], DEFAULT)
    assert len(layout.plates) == 1
    assert {p.piece_key: p.rot for p in layout.plates[0].items}["long"] == 90.0
    assert_no_overlap(footprints(layout, {"long": long, "sq": square})[0])


def test_a_part_no_turn_fits_is_refused_with_the_plate_saying_why() -> None:
    with pytest.raises(
        PackError, match=r"^model\.scad \(300 x 10 mm\) does not fit the plate \(256 x 256 mm\): "
    ):
        arrange([PackItem(part=part("a", 300, 10)), PackItem(part=part("b", 1, 1))], DEFAULT)


def test_a_multi_plate_part_cannot_share() -> None:
    with pytest.raises(PackError, match="its own 2 plates"):
        arrange(
            [PackItem(part=part("a", 10, 10, plates=2)), PackItem(part=part("b", 1, 1))], DEFAULT
        )


def test_an_unknown_goal_names_the_goals() -> None:
    with pytest.raises(PackError, match="fewest_swaps"):
        arrange([PackItem(part=part("a", 1, 1), count=2)], DEFAULT, goal="prettiest")


@pytest.mark.parametrize("model", ["H2C", "X1C", "A1 mini"])
@pytest.mark.parametrize("goal", GOALS)
def test_every_packed_plate_places_on_the_real_printer(model: str, goal: str) -> None:
    size = plate(model)
    geometry = geometry_of(size)
    parts = {
        "red": part("red", 70, 50, "#FF0000", h=20),
        "two": part("two", 90, 35, "#FF0000", "#FFFFFF", h=10),
        "white": part("white", 30, 110, "#FFFFFF", h=15),
    }
    items = [
        PackItem(part=parts["red"], count=5, group="a"),
        PackItem(part=parts["two"], count=4, group="b"),
        PackItem(part=parts["white"], count=6, group="a" if goal != "keep_together" else "c"),
    ]
    layout = arrange(items, size, goal=goal)
    placed = sum(len(p.items) for p in layout.plates)
    assert placed == 15
    for rects, lp in zip(footprints(layout, parts), layout.plates, strict=True):
        assert_no_overlap(rects)
        colours = {c for i in lp.items for c in parts[i.piece_key].colours}
        block = (
            max(r[2] for r in rects) - min(r[0] for r in rects),
            max(r[3] for r in rects) - min(r[1] for r in rects),
            max(parts[i.piece_key].bbox.size[2] for i in lp.items),
        )
        assert fit_problem(block, geometry, tower=len(colours) > 1) is None


def test_by_colour_gives_single_colour_plates_that_need_no_tower() -> None:
    red, white = part("red", 50, 50, "#FF0000"), part("white", 50, 50, "#FFFFFF")
    layout = arrange(
        [PackItem(part=red, count=3), PackItem(part=white, count=3)], DEFAULT, goal="by_colour"
    )
    per_plate = [{i.piece_key for i in p.items} for p in layout.plates]
    assert per_plate == [{"red"}, {"white"}]
    mixed = arrange([PackItem(part=red, count=3), PackItem(part=white, count=3)], DEFAULT)
    assert len(mixed.plates) == 1


def test_fewest_swaps_lets_a_subset_ride_on_a_plate_that_already_has_its_filament() -> None:
    both = part("both", 60, 60, "#FF0000", "#FFFFFF")
    red = part("red", 20, 20, "#FF0000")
    blue = part("blue", 20, 20, "#0000FF")
    layout = arrange(
        [PackItem(part=both, count=2), PackItem(part=red, count=2), PackItem(part=blue)],
        DEFAULT,
        goal="fewest_swaps",
    )
    per_plate = [sorted({i.piece_key for i in p.items}) for p in layout.plates]
    assert per_plate == [["both", "red"], ["blue"]]


def test_a_plan_makes_two_colours_on_one_spool_one_filament() -> None:
    a, b = part("a", 10, 10, "#FF0000"), part("b", 10, 10, "#FE0000")
    colours = ["#FF0000", "#FE0000"]
    assert signature_of(a, None, colours) != signature_of(b, None, colours)
    plan = SlotPlan(slots={1: 7, 2: 7})
    assert (
        signature_of(a, plan, colours) == signature_of(b, plan, colours) == frozenset({"spool:7"})
    )
    layout = arrange(
        [PackItem(part=a), PackItem(part=b)], DEFAULT, goal="by_colour", plan=plan, colours=colours
    )
    assert len(layout.plates) == 1


def test_keep_together_keeps_a_group_on_one_plate_or_says_it_cannot() -> None:
    a, b = part("a", 100, 100), part("b", 100, 100)
    layout = arrange(
        [PackItem(part=a, group="left"), PackItem(part=b, group="right")],
        DEFAULT,
        goal="keep_together",
    )
    assert [[i.piece_key for i in p.items] for p in layout.plates] == [["a"], ["b"]]
    with pytest.raises(PackError, match="group 'big' does not fit on one plate"):
        arrange(
            [PackItem(part=part("c", 200, 200), count=2, group="big")],
            DEFAULT,
            goal="keep_together",
        )


def test_plates_are_ordered_so_each_needs_the_fewest_new_filaments() -> None:
    r, w, b, rw = (frozenset(s) for s in (["r"], ["w"], ["b"], ["r", "w"]))
    # rw first (most filaments); r and w each add none after it, the earlier wins; then b, w
    assert order_plates([r, b, rw, w]) == [2, 0, 1, 3]


def test_the_same_request_packs_the_same_way() -> None:
    items = [
        PackItem(part=part(k, 10 + i * 7, 30, "#FF0000" if i % 2 else "#00FF00"), count=3)
        for i, k in enumerate("abcdef")
    ]
    assert arrange(items, DEFAULT, goal="fewest_swaps") == arrange(
        items, DEFAULT, goal="fewest_swaps"
    )


def test_an_explicit_plate_takes_quarter_turns() -> None:
    placed = explicit_plate([part("a", 10, 20)], [(5.0, 6.0, 90.0)], plate=DEFAULT)
    assert placed.items[0].rot == 90.0
    with pytest.raises(PackError, match="quarter turns"):
        explicit_plate([part("a", 10, 20)], [(0.0, 0.0, 45.0)], plate=DEFAULT)
