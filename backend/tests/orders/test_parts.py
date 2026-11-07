"""Parts inventory (#5543, job cost spec §11): the arithmetic, the rule and the part rows."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from scadbuddy.orders.models import Actor, ColourSlot
from scadbuddy.orders.parts import (
    PartExistsError,
    PartInput,
    PartNotFoundError,
    PartPatch,
    PartRuleInput,
    PartRuleLine,
    PartRuleWhen,
    PartStore,
    StockAdd,
    StockAdjust,
    average,
    rule_parts,
    use_amount,
)
from scadbuddy.orders.store import OrderStore

BY = Actor(kind="browser")


def test_average_weighs_stock_on_hand_with_the_new_pack() -> None:
    assert average(0, Decimal(0), 100, 599) == Decimal("5.99")
    assert average(100, Decimal(5), 100, 700) == Decimal(6)
    # Nothing (or less than nothing) on hand: the new units' own cost.
    assert average(-3, Decimal(5), 10, 100) == Decimal(10)


def test_use_amount_rounds_once_half_up() -> None:
    assert use_amount(Decimal("5.99"), 3) == 18
    assert use_amount(Decimal("0.5"), 1) == 1
    assert use_amount(Decimal("1.234567"), 10) == 12


def _colours(*hexes: str) -> list[ColourSlot]:
    return [ColourSlot(slot=i, colour=c) for i, c in enumerate(hexes)]


def test_a_rule_picks_by_colour_else_its_default() -> None:
    lines = [
        PartRuleLine(
            per_unit=1, part_id="silver", when=[PartRuleWhen(colour="#FF69B4", part_id="pink")]
        ),
        PartRuleLine(per_unit=2, part_id="ring"),
        PartRuleLine(
            per_unit=1, part_id=None, when=[PartRuleWhen(colour="#000000", part_id="ring")]
        ),
    ]

    pink = rule_parts(lines, _colours("#ff69b4", "#ffffff"))
    plain = rule_parts(lines, _colours("#ffffff"))
    black = rule_parts(lines, _colours("#000000"))

    assert [(p.part_id, p.per_unit) for p in pink] == [("pink", 1), ("ring", 2)]
    assert [(p.part_id, p.per_unit) for p in plain] == [("silver", 1), ("ring", 2)]
    # A part two lines name is summed; a line with no default and no match gives nothing.
    assert [(p.part_id, p.per_unit) for p in black] == [("silver", 1), ("ring", 3)]


def test_a_link_must_be_http() -> None:
    with pytest.raises(ValidationError):
        PartInput(name="Hook", pack_size=1, pack_price_minor=1, link="javascript:alert(1)")


@pytest.fixture
def parts(rows: OrderStore) -> PartStore:
    return PartStore(rows)


def _hook(parts: PartStore, **fields: object) -> str:
    body = {"name": "Silver hook", "pack_size": 100, "pack_price_minor": 599, **fields}
    return parts.create(PartInput.model_validate(body), currency="USD", by=BY).id


def test_a_part_starts_at_its_pack_price_over_its_size(parts: PartStore) -> None:
    part_id = _hook(parts, on_hand=40)

    part = parts.part(part_id)
    assert part is not None
    assert (part.unit_cost_minor, part.on_hand, part.currency) == (Decimal("5.99"), 40, "USD")
    assert part.model_dump(mode="json")["unit_cost_minor"] == "5.99"
    [move] = parts.moves(part_id)
    assert (move.kind, move.delta, move.on_hand_after) == ("adjust", 40, 40)


def test_add_stock_averages_and_records_the_pack(parts: PartStore) -> None:
    part_id = _hook(parts, pack_price_minor=500, on_hand=100)

    part = parts.add_stock(part_id, StockAdd(packs=1, pack_price_minor=700, note="restock"), BY)

    assert (part.on_hand, part.unit_cost_minor, part.pack_price_minor) == (200, Decimal(6), 700)
    move = parts.moves(part_id)[0]
    assert (move.kind, move.delta, move.amount_minor, move.note) == ("add", 100, 700, "restock")


def test_adjust_sets_the_count_with_a_note(parts: PartStore) -> None:
    part_id = _hook(parts, on_hand=10)
    with pytest.raises(ValidationError):
        StockAdjust(on_hand=7, note="")

    part = parts.adjust(part_id, StockAdjust(on_hand=7, note="counted"), BY)

    assert part.on_hand == 7
    move = parts.moves(part_id)[0]
    assert (move.kind, move.delta, move.note) == ("adjust", -3, "counted")


def test_low_when_at_or_below_its_level(parts: PartStore) -> None:
    part_id = _hook(parts, on_hand=5, low_stock=5)
    off_id = _hook(parts, name="Ring", on_hand=0)

    assert parts.part(part_id).low  # type: ignore[union-attr]
    assert not parts.part(off_id).low  # type: ignore[union-attr]
    assert not parts.update(part_id, PartPatch(low_stock=4)).low


def test_a_name_is_one_parts_in_any_case(parts: PartStore) -> None:
    part_id = _hook(parts)
    with pytest.raises(PartExistsError) as exists:
        _hook(parts, name="SILVER HOOK")
    assert exists.value.existing_id == part_id


def test_a_pack_edit_moves_the_unit_cost_only_with_nothing_on_hand(parts: PartStore) -> None:
    empty = _hook(parts)
    stocked = _hook(parts, name="Ring", on_hand=10)

    assert parts.update(empty, PartPatch(pack_price_minor=1000)).unit_cost_minor == Decimal(10)
    assert parts.update(stocked, PartPatch(pack_price_minor=1000)).unit_cost_minor == Decimal(
        "5.99"
    )


def test_a_rule_names_only_parts_that_exist(parts: PartStore) -> None:
    part_id = _hook(parts)
    with pytest.raises(PartNotFoundError):
        parts.set_rule("keychain", PartRuleInput(lines=[PartRuleLine(part_id="nope")]), BY)

    saved = parts.set_rule("keychain", PartRuleInput(lines=[PartRuleLine(part_id=part_id)]), BY)
    assert parts.rule("keychain") == saved
    assert parts.set_rule("keychain", PartRuleInput(lines=[]), BY).lines == []
    assert parts.rule("keychain").updated_at is None
