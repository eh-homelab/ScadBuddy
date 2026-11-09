"""A suggested deposit confirmed on its order (#4961): "Record this deposit as payment"
records it once through ``Lifecycle.add_payment``, method ``bank_transfer`` and the Plaid
transaction id as ``reference``; "Not this order" dismisses the suggestion."""

from __future__ import annotations

import asyncio
import contextlib
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


async def test_a_dismissed_suggestion_stays_dismissed_after_a_new_link(bank: Bank) -> None:
    """Unlink drops the Item's deposits; "Not this order" is kept apart from them."""
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    dismiss_deposit(bank.rows, owing.id, "t1")
    with respx.mock as router:
        router.post(f"{SANDBOX}/item/remove").mock(
            return_value=httpx.Response(200, json=payload("item.remove.json"))
        )
        await bank.bank.unlink()
        plaid_answers(router)
        await bank.bank.link("public-sandbox-2", [CHECKING])
    with respx.mock as router:
        router.post(SYNC).mock(return_value=page([transaction("t1", -10, "ACH")]))
        # The sync makes no suggestion of a dismissed pair.
        assert await sync_deposits(bank.bank) == 0
    assert suggestions(bank.rows, owing.id) == []


async def test_recording_holds_one_connection(bank: Bank, monkeypatch: pytest.MonkeyPatch) -> None:
    """The deposit's lock is taken in the payment's own transaction, not around it."""
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    depth = threading.local()
    deepest: list[int] = []
    real = OrderStore.transaction

    @contextlib.contextmanager
    def counted(self: OrderStore, *args: Any, **kwargs: Any) -> Any:
        depth.n = getattr(depth, "n", 0) + 1
        deepest.append(depth.n)
        try:
            with real(self, *args, **kwargs) as conn:
                yield conn
        finally:
            depth.n -= 1

    monkeypatch.setattr(OrderStore, "transaction", counted)
    record(bank, owing, "t1")
    assert max(deepest) == 1


async def test_a_recorded_deposit_plaid_changes_is_shown_on_its_order(bank: Bank) -> None:
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    record(bank, owing, "t1")
    assert suggestions(bank.rows, owing.id) == []
    with respx.mock as router:
        router.post(SYNC).mock(
            return_value=page(modified=[transaction("t1", -12, "ACH")], cursor="c2")
        )
        await sync_deposits(bank.bank)
    [changed] = suggestions(bank.rows, owing.id)
    assert (changed.reason, changed.amount_minor, changed.recorded_minor) == ("changed", 1200, 1000)


async def test_a_sync_tells_each_suggested_order_s_page(bank: Bank) -> None:
    owing = bank.owing()
    heard: list[Any] = []

    class Bus:
        def publish(self, event: Any) -> None:
            heard.append(event)

    with respx.mock as router:
        router.post(SYNC).mock(return_value=page([transaction("t1", -10, "ACH")]))
        await sync_deposits(bank.bank, Bus())  # type: ignore[arg-type]
    assert [(e.kind, e.order_id) for e in heard] == [("order.bank_deposits", owing.id)]


def adjust(bank: Bank, order: Order, amount_minor: int, tx_id: str) -> None:
    """The shop's correction: payments are never edited, so it records the difference."""
    bank.shop.lifecycle.add_payment(
        order.id, BY, amount_minor=amount_minor, method="bank_transfer", reference=tx_id
    )


async def test_the_changed_warning_clears_once_the_payment_is_corrected(bank: Bank) -> None:
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    record(bank, owing, "t1")
    with respx.mock as router:
        router.post(SYNC).mock(
            return_value=page(modified=[transaction("t1", -12, "ACH")], cursor="c2")
        )
        await sync_deposits(bank.bank)
    adjust(bank, owing, 200, "t1")
    assert suggestions(bank.rows, owing.id) == []


async def test_a_recorded_deposit_plaid_removes_is_shown_until_it_is_reversed(
    bank: Bank,
) -> None:
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    record(bank, owing, "t1")
    with respx.mock as router:
        router.post(SYNC).mock(return_value=page(removed=["t1"], cursor="c2"))
        await sync_deposits(bank.bank)
    [removed] = suggestions(bank.rows, owing.id)
    assert (removed.reason, removed.recorded_minor) == ("removed", 1000)
    adjust(bank, owing, -1000, "t1")
    assert suggestions(bank.rows, owing.id) == []


async def test_a_sync_tells_the_page_of_a_recorded_deposit_it_changed(bank: Bank) -> None:
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    record(bank, owing, "t1")
    heard: list[Any] = []

    class Bus:
        def publish(self, event: Any) -> None:
            heard.append(event)

    with respx.mock as router:
        router.post(SYNC).mock(
            return_value=page(modified=[transaction("t1", -12, "ACH")], cursor="c2")
        )
        assert await sync_deposits(bank.bank, Bus()) == 0  # type: ignore[arg-type]
    assert [e.order_id for e in heard] == [owing.id]


async def removed_after_recording(bank: Bank, order: Order) -> None:
    await synced(bank, transaction("t1", -10, "ACH"))
    record(bank, order, "t1")
    with respx.mock as router:
        router.post(SYNC).mock(return_value=page(removed=["t1"], cursor="c2"))
        await sync_deposits(bank.bank)


async def test_a_cancelled_order_shows_no_bank_warning(bank: Bank) -> None:
    """A cancelled order takes no payment, so a warning asking for one is never shown."""
    owing = bank.owing()
    await removed_after_recording(bank, owing)
    bank.shop.lifecycle.cancel_order(owing.id, BY)
    assert suggestions(bank.rows, owing.id) == []


async def test_a_refund_with_the_deposit_s_reference_is_no_bank_change(bank: Bank) -> None:
    """The warning is about the bank's change: an unchanged deposit never shows it."""
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    record(bank, owing, "t1")
    adjust(bank, owing, -1000, "t1")
    assert suggestions(bank.rows, owing.id) == []


async def test_a_removed_deposit_whose_payment_is_gone_is_pruned(bank: Bank) -> None:
    owing = bank.owing()
    await removed_after_recording(bank, owing)
    with bank.pool.connection() as conn:
        conn.execute("DELETE FROM order_payments WHERE reference = 't1'")
    with respx.mock as router:
        router.post(SYNC).mock(return_value=page(cursor="c3"))
        await sync_deposits(bank.bank)
    assert bank.deposits() == []


async def test_a_new_link_keeps_the_payment_the_deposit_was_recorded_as(bank: Bank) -> None:
    """After a new link the deposit is tied to its own payment, not to a refund with the
    same reference, so an unchanged deposit shows no warning."""
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    record(bank, owing, "t1")
    adjust(bank, owing, -1000, "t1")
    with bank.pool.connection() as conn:
        # The recorded payment's row rewritten: an unordered read now finds the refund first.
        conn.execute("UPDATE order_payments SET notes = notes || '' WHERE amount_minor > 0")
    with respx.mock as router:
        router.post(f"{SANDBOX}/item/remove").mock(
            return_value=httpx.Response(200, json=payload("item.remove.json"))
        )
        await bank.bank.unlink()
        plaid_answers(router)
        await bank.bank.link("public-sandbox-2", [CHECKING])
    await synced(bank, transaction("t1", -10, "ACH"))
    with bank.pool.connection() as conn:
        row = conn.execute(
            "SELECT p.amount_minor FROM bank_deposits d JOIN order_payments p"
            " ON p.id = d.payment_id WHERE d.transaction_id = 't1'"
        ).fetchone()
    assert row is not None and row["amount_minor"] == 1000
    assert suggestions(bank.rows, owing.id) == []


async def test_a_removal_waits_for_a_confirmation_in_flight(bank: Bank) -> None:
    """A sync that removes a deposit while a confirmation holds its row sees the payment
    once the confirmation commits, and marks the row instead of skipping it."""
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    payment = bank.shop.lifecycle.add_payment(
        owing.id, BY, amount_minor=1000, method="bank_transfer", reference="t1"
    )
    with bank.pool.connection() as conn:
        # The confirmation's transaction (the pool's connections autocommit).
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE bank_deposits SET payment_id = %s WHERE transaction_id = 't1'",
            (payment.id,),
        )
        with respx.mock as router:
            router.post(SYNC).mock(return_value=page(removed=["t1"], cursor="c2"))
            syncing = asyncio.create_task(sync_deposits(bank.bank))
            await asyncio.sleep(1.5)
            conn.execute("COMMIT")
            await syncing
    with bank.pool.connection() as conn:
        row = conn.execute(
            "SELECT removed_at FROM bank_deposits WHERE transaction_id = 't1'"
        ).fetchone()
    assert row is not None and row["removed_at"] is not None


async def resynced(bank: Bank, amount: float, cursor: str, bus: Any = None) -> None:
    with respx.mock as router:
        router.post(SYNC).mock(
            return_value=page(modified=[transaction("t1", amount, "ACH")], cursor=cursor)
        )
        await sync_deposits(bank.bank, bus)


async def test_a_deposit_changed_and_changed_back_warns_again(bank: Bank) -> None:
    """Once the bank moved a recorded deposit, the payments are checked against it for
    good: a change back after the shop's correction warns again."""
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    record(bank, owing, "t1")
    await resynced(bank, -12, "c2")
    adjust(bank, owing, 200, "t1")
    assert suggestions(bank.rows, owing.id) == []
    await resynced(bank, -10, "c3")
    [back] = suggestions(bank.rows, owing.id)
    assert (back.reason, back.amount_minor, back.recorded_minor) == ("changed", 1000, 1200)


async def test_an_unlink_keeps_a_removed_deposit_s_warning(bank: Bank) -> None:
    """Unlink drops the deposits nothing recorded; a recorded one keeps its warning."""
    owing = bank.owing()
    await removed_after_recording(bank, owing)
    with respx.mock as router:
        router.post(f"{SANDBOX}/item/remove").mock(
            return_value=httpx.Response(200, json=payload("item.remove.json"))
        )
        await bank.bank.unlink()
        plaid_answers(router)
        await bank.bank.link("public-sandbox-2", [CHECKING])
    [removed] = suggestions(bank.rows, owing.id)
    assert removed.reason == "removed"


async def test_a_sync_that_reads_a_recorded_deposit_unchanged_tells_no_page(bank: Bank) -> None:
    owing = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    record(bank, owing, "t1")
    heard: list[Any] = []

    class Bus:
        def publish(self, event: Any) -> None:
            heard.append(event)

    await resynced(bank, -10, "c2", Bus())
    assert heard == []


async def test_a_removed_deposit_read_again_tells_its_page(bank: Bank) -> None:
    """The bank brings back a deposit it removed: the page drops its "removed" warning."""
    owing = bank.owing()
    await removed_after_recording(bank, owing)
    heard: list[Any] = []

    class Bus:
        def publish(self, event: Any) -> None:
            heard.append(event)

    await resynced(bank, -10, "c3", Bus())
    assert [e.order_id for e in heard] == [owing.id]
    assert suggestions(bank.rows, owing.id) == []


async def test_an_unlink_drops_a_recorded_deposit_s_suggestions(bank: Bank) -> None:
    """The recorded row stays for its warning; the suggestions it had go with the Item."""
    owing = bank.owing()
    other = bank.owing()
    await synced(bank, transaction("t1", -10, "ACH"))
    record(bank, owing, "t1")
    with respx.mock as router:
        router.post(f"{SANDBOX}/item/remove").mock(
            return_value=httpx.Response(200, json=payload("item.remove.json"))
        )
        await bank.bank.unlink()
    with bank.pool.connection() as conn:
        kept = conn.execute("SELECT count(*) AS n FROM bank_deposits").fetchone()
        matched = conn.execute("SELECT count(*) AS n FROM bank_deposit_matches").fetchone()
    assert kept is not None and kept["n"] == 1
    assert matched is not None and matched["n"] == 0
    assert other.id
