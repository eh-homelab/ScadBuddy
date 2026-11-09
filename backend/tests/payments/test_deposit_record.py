"""A suggested deposit confirmed on its order (#4961): "Record this deposit as payment"
records it once through ``Lifecycle.add_payment``, method ``bank_transfer`` and the Plaid
transaction id as ``reference``; "Not this order" dismisses the suggestion."""

from __future__ import annotations

import threading
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.models import Order, Payment
from scadbuddy.orders.store import OrderStore
from scadbuddy.payments.deposits import (
    dismiss_deposit,
    record_deposit,
    suggestions,
    sync_deposits,
)
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY
from tests.payments.test_bank import CHECKING, SANDBOX, linked, payload, plaid_answers
from tests.payments.test_deposits import SYNC, Bank, page, transaction

pytestmark = pytest.mark.requires_postgres


@pytest.fixture
async def bank(rows: OrderStore, pool: PgPool) -> Bank:
    made = Bank(rows, pool)
    with respx.mock as router:
        await linked(made.bank, router)
    return made


async def synced(bank: Bank, *transactions: dict[str, Any]) -> None:
    with respx.mock as router:
        router.post(SYNC).mock(return_value=page(list(transactions)))
        await sync_deposits(bank.bank)


def record(bank: Bank, order: Order, tx_id: str) -> Payment:
    def add(**fields: Any) -> Payment:
        return bank.shop.lifecycle.add_payment(order.id, BY, **fields)

    return record_deposit(bank.rows, order.id, tx_id, add)


async def test_an_order_lists_its_suggested_deposits(bank: Bank) -> None:
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH ACCT 123456789"))
    [found] = suggestions(bank.rows, owing.id)
    assert (found.transaction_id, found.amount_minor, found.currency) == ("t1", 1000, "USD")
    assert (found.reason, found.description) == ("amount", "ACH ACCT •••")
    assert suggestions(bank.rows, bank.owing().id) == []


async def test_recording_writes_one_bank_transfer_with_the_transaction_id(bank: Bank) -> None:
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    payment = record(bank, owing, "t1")
    [paid] = bank.rows.payments(owing.id)
    assert paid == payment
    assert (paid.amount_minor, paid.method, paid.reference) == (1000, "bank_transfer", "t1")
    assert suggestions(bank.rows, owing.id) == []
    with pytest.raises(LifecycleError) as again:
        record(bank, owing, "t1")
    assert again.value.slug == "deposit_recorded"
    assert len(bank.rows.payments(owing.id)) == 1


async def test_a_deposit_is_recorded_on_one_order_only(bank: Bank) -> None:
    first = bank.owing()
    second = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    assert {s.transaction_id for s in suggestions(bank.rows, second.id)} == {"t1"}
    record(bank, first, "t1")
    assert suggestions(bank.rows, second.id) == []
    with pytest.raises(LifecycleError) as refused:
        record(bank, second, "t1")
    assert refused.value.slug == "deposit_recorded"
    assert bank.rows.payments(second.id) == []


async def test_two_confirmations_at_once_record_one_payment(bank: Bank) -> None:
    first = bank.owing()
    second = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    outcomes: list[str] = []

    def confirm(order: Order) -> None:
        try:
            record(bank, order, "t1")
            outcomes.append("recorded")
        except LifecycleError as error:
            outcomes.append(error.slug)

    threads = [threading.Thread(target=confirm, args=(o,)) for o in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["deposit_recorded", "recorded"]
    assert len(bank.rows.payments(first.id)) + len(bank.rows.payments(second.id)) == 1


async def test_a_deposit_not_suggested_for_the_order_is_404(bank: Bank) -> None:
    owing = bank.owing()
    await synced(bank, transaction("t1", -3, "ACH"))
    with pytest.raises(LifecycleError) as missing:
        record(bank, owing, "t1")
    assert (missing.value.slug, missing.value.not_found) == ("deposit_not_suggested", True)


async def test_a_deposit_in_another_currency_is_refused(bank: Bank) -> None:
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, f"{owing.number}", iso_currency_code="CAD"))
    with pytest.raises(LifecycleError) as refused:
        record(bank, owing, "t1")
    assert refused.value.slug == "deposit_currency"


async def test_a_dismissed_suggestion_stays_dismissed_when_the_deposit_changes(
    bank: Bank,
) -> None:
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    dismiss_deposit(bank.rows, owing.id, "t1")
    assert suggestions(bank.rows, owing.id) == []
    with respx.mock as router:
        router.post(SYNC).mock(
            return_value=page(modified=[transaction("t1", -10, "ACH CHANGED")], cursor="c2")
        )
        await sync_deposits(bank.bank)
    assert suggestions(bank.rows, owing.id) == []
    with pytest.raises(LifecycleError) as missing:
        dismiss_deposit(bank.rows, owing.id, "t1")
    assert missing.value.slug == "deposit_not_suggested"


async def test_a_recorded_deposit_is_not_suggested_again_after_a_new_link(bank: Bank) -> None:
    owing = bank.owing(paid=0)
    await synced(bank, transaction("t1", -10, "ACH"))
    record(bank, owing, "t1")
    other = bank.owing()
    with respx.mock as router:
        router.post(f"{SANDBOX}/item/remove").mock(
            return_value=httpx.Response(200, json=payload("item.remove.json"))
        )
        await bank.bank.unlink()
        plaid_answers(router)
        await bank.bank.link("public-sandbox-2", [CHECKING])
    await synced(bank, transaction("t1", -10, "ACH"))
    assert suggestions(bank.rows, other.id) == []
