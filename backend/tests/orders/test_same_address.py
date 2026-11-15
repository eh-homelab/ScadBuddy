"""`lifecycle._same_address` (#5283): a signed-in submit reuses a stored address only
when it is the same place for the same recipient."""

from __future__ import annotations

from scadbuddy.orders.lifecycle import _same_address
from scadbuddy.orders.models import AddressInput

BASE = AddressInput(
    recipient="Ada Lovelace",
    lines=["1 Analytical Way", "Flat 2"],
    locality="London",
    postal_code="N1 9GU",
    country="GB",
    phone="+44 20 7946 0000",
)


def test_case_and_inner_spaces_do_not_matter() -> None:
    other = BASE.model_copy(
        update={
            "recipient": "  ada   LOVELACE ",
            "lines": ["1  analytical way", "FLAT 2"],
            "postal_code": "n1  9gu",
        }
    )
    assert _same_address(BASE, other)


def test_empty_lines_are_dropped() -> None:
    other = BASE.model_copy(update={"lines": ["", "1 Analytical Way", "  ", "Flat 2"]})
    assert _same_address(BASE, other)


def test_a_different_phone_is_another_address() -> None:
    assert not _same_address(BASE, BASE.model_copy(update={"phone": "+44 20 7946 0001"}))


def test_line_order_matters() -> None:
    other = BASE.model_copy(update={"lines": ["Flat 2", "1 Analytical Way"]})
    assert not _same_address(BASE, other)


def test_the_shops_label_and_defaults_are_not_compared() -> None:
    other = BASE.model_copy(update={"label": "Home", "default_shipping": True})
    assert _same_address(BASE, other)
