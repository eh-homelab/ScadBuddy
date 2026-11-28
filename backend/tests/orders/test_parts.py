"""Parts (#5543, job cost spec §11): the rule. The part rows are inventory items since
#7067; the rule and order routes are tested in `tests/api/test_parts.py`."""

from __future__ import annotations

from scadbuddy.orders.models import ColourSlot
from scadbuddy.orders.parts import (
    PartRuleLine,
    PartRuleWhen,
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
