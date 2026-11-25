"""Parts (#5543, job cost spec §11): the rule and the part bodies. The part rows are
inventory items since #7067; the `/parts` aliases are tested in `tests/api/test_parts.py`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scadbuddy.orders.models import ColourSlot
from scadbuddy.orders.parts import (
    PartInput,
    PartPatch,
    PartRuleLine,
    PartRuleWhen,
    StockAdd,
    StockAdjust,
    rule_parts,
)


def _colours(*hexes: str) -> list[ColourSlot]:
    return [ColourSlot(slot=i, colour=c) for i, c in enumerate(hexes)]


def test_a_rule_picks_by_colour_else_its_default() -> None:
    lines = [
        PartRuleLine(
            per_unit=1, item_id="silver", when=[PartRuleWhen(colour="#FF69B4", item_id="pink")]
        ),
        PartRuleLine(per_unit=2, item_id="ring"),
        PartRuleLine(
            per_unit=1, item_id=None, when=[PartRuleWhen(colour="#000000", item_id="ring")]
        ),
    ]

    pink = rule_parts(lines, _colours("#ff69b4", "#ffffff"))
    plain = rule_parts(lines, _colours("#ffffff"))
    black = rule_parts(lines, _colours("#000000"))

    assert [(p.item_id, p.per_unit) for p in pink] == [("pink", 1), ("ring", 2)]
    assert [(p.item_id, p.per_unit) for p in plain] == [("silver", 1), ("ring", 2)]
    # A part two lines name is summed; a line with no default and no match gives nothing.
    assert [(p.item_id, p.per_unit) for p in black] == [("silver", 1), ("ring", 3)]


def test_a_link_must_be_http() -> None:
    with pytest.raises(ValidationError):
        PartInput(name="Hook", pack_size=1, pack_price_minor=1, link="javascript:alert(1)")


@pytest.mark.parametrize(
    "build",
    [
        lambda: PartInput(name="Ho\x00ok", pack_size=1, pack_price_minor=1),
        lambda: PartInput(name="Hook", supplier="A\x00", pack_size=1, pack_price_minor=1),
        lambda: PartInput(name="Hook", link="https://a\x00", pack_size=1, pack_price_minor=1),
        lambda: PartPatch(name="\x00"),
        lambda: StockAdd(note="\x00"),
        lambda: StockAdjust(on_hand=1, note="\x00"),
        lambda: StockAdjust(on_hand=-1, note="counted"),
    ],
)
def test_a_body_with_a_nul_or_a_negative_count_is_refused(build: object) -> None:
    """R-P2-16: a NUL byte, which Postgres `text` refuses, never reaches SQL."""
    with pytest.raises(ValidationError):
        build()  # type: ignore[operator]
