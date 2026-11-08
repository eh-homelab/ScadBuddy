"""The deposit sync (#4961): the linked account's deposits read from Plaid's
``/transactions/sync``, and the orders each one may pay suggested, never recorded. Plaid is
answered by respx; each transaction is the recorded sandbox one in
``plaid_payloads/transactions.sync.json`` with its amount, text or ids changed."""

from __future__ import annotations

import copy
import json
import logging
import threading
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from scadbuddy.orders.models import Order
from scadbuddy.orders.store import OrderStore
from scadbuddy.payments.bank import BankLinks
from scadbuddy.payments.deposits import mask, sync_deposits
from scadbuddy.store.refs import BlobRefs
from tests.conftest import PgPool
from tests.orders.test_lifecycle import BY, Shop
from tests.payments.test_bank import (
    CHECKING,
    KEK,
    SANDBOX,
    linked,
    payload,
)

pytestmark = pytest.mark.requires_postgres

SAVINGS = "dVzbVMLjrxTnLjX4G66XUp5GLklm4oiZy88yK"
SYNC = f"{SANDBOX}/transactions/sync"
TODAY = datetime.now(UTC).date().isoformat()


def transaction(tx_id: str, amount: float, text: str, **fields: Any) -> dict[str, Any]:
    """The recorded deposit with ``amount`` (negative is money in) and ``text``."""
    found: dict[str, Any] = copy.deepcopy(payload("transactions.sync.json")["added"][0])
    found.update(
        {
            "transaction_id": tx_id,
            "amount": amount,
            "name": text,
            "original_description": text,
            "date": TODAY,
        }
    )
    found.update(fields)
    return found


def page(
    added: list[dict[str, Any]] = (),  # type: ignore[assignment]
    *,
    modified: list[dict[str, Any]] = (),  # type: ignore[assignment]
    removed: list[str] = (),  # type: ignore[assignment]
    cursor: str = "c1",
    more: bool = False,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "added": list(added),
            "modified": list(modified),
            "removed": [{"account_id": CHECKING, "transaction_id": t} for t in removed],
            "next_cursor": cursor,
            "has_more": more,
            "request_id": "r",
        },
    )


class Bank:
    def __init__(self, rows: OrderStore, pool: PgPool) -> None:
        self.rows = rows
        self.pool = pool
        self.bank = BankLinks(rows, KEK)
        self.shop = Shop(rows, BlobRefs(pool))

    def deposits(self) -> list[dict[str, Any]]:
        with self.pool.connection() as conn:
            return conn.execute(
                "SELECT transaction_id, amount_minor, currency, description FROM bank_deposits"
                " ORDER BY transaction_id"
            ).fetchall()

    def matches(self) -> set[tuple[str, str, str]]:
        with self.pool.connection() as conn:
            found = conn.execute(
                "SELECT transaction_id, order_id, reason FROM bank_deposit_matches"
            ).fetchall()
        return {(m["transaction_id"], m["order_id"], m["reason"]) for m in found}

    def cursor(self) -> str | None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT sync_cursor FROM bank_links").fetchone()
        assert row is not None
        cursor: str | None = row["sync_cursor"]
        return cursor

    def owing(self, paid: int = 0) -> Order:
        """An accepted order of 1000 (USD) with ``paid`` already paid."""
        order, _ = self.shop.accepted()
        if paid:
            self.shop.lifecycle.add_payment(order.id, BY, amount_minor=paid)
        return order


@pytest.fixture
async def bank(rows: OrderStore, pool: PgPool) -> Bank:
    made = Bank(rows, pool)
    with respx.mock as router:
        await linked(made.bank, router)
    return made


async def test_the_sync_calls_the_database_off_the_event_loop(
    bank: Bank, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sync also runs on the API's loop, after a webhook's answer (step 4): every
    Postgres call goes through ``asyncio.to_thread``, as ``Payments.link`` does."""
    loop_thread = threading.get_ident()
    on_loop: list[str] = []
    real = OrderStore.transaction

    def watched(self: OrderStore, *args: Any, **kwargs: Any) -> Any:
        if threading.get_ident() == loop_thread:
            on_loop.append("transaction")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(OrderStore, "transaction", watched)
    with respx.mock as router:
        router.post(SYNC).mock(
            return_value=httpx.Response(200, json=payload("transactions.sync.json"))
        )
        await sync_deposits(bank.bank)
        router.post(SYNC).mock(side_effect=httpx.ConnectError("down"))
        await sync_deposits(bank.bank)
    assert on_loop == []


async def test_nothing_linked_syncs_nothing(rows: OrderStore) -> None:
    with respx.mock:
        assert await sync_deposits(BankLinks(rows, KEK)) == 0


async def test_deposits_are_kept_masked_and_withdrawals_and_other_accounts_are_not(
    bank: Bank, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG), respx.mock as router:
        router.post(SYNC).mock(
            return_value=httpx.Response(200, json=payload("transactions.sync.json"))
        )
        await sync_deposits(bank.bank)
    [deposit] = bank.deposits()
    assert deposit["transaction_id"] == "lPNjeW1nR6CDn5okmGQ6hEpMo4lLNoSrzqDje"
    assert (deposit["amount_minor"], deposit["currency"]) == (2500, "USD")
    # The bank's text, with no run of five or more digits: no account number is kept.
    assert deposit["description"] == "ZELLE PAYMENT FROM JANE DOE ACCT ••• ORD-2026-7"
    assert "123456789" not in repr(bank.deposits()) and "123456789" not in caplog.text
    assert bank.cursor() is not None and bank.cursor().startswith("tVUUL15l")  # type: ignore[union-attr]


def test_mask_hides_long_digit_runs_only() -> None:
    assert mask("REF 1234 ACCT 000123456789 ORD-2026-12") == "REF 1234 ACCT ••• ORD-2026-12"


async def test_pending_and_other_account_deposits_are_skipped(bank: Bank) -> None:
    with respx.mock as router:
        router.post(SYNC).mock(
            return_value=page(
                [
                    transaction("pending", -10, "PENDING DEPOSIT", pending=True),
                    transaction("savings", -10, "SAVINGS DEPOSIT", account_id=SAVINGS),
                    transaction("no-currency", -10, "X", iso_currency_code=None),
                ]
            )
        )
        await sync_deposits(bank.bank)
    assert bank.deposits() == []


async def test_a_deposit_equal_to_an_open_balance_is_suggested_and_not_recorded(
    bank: Bank,
) -> None:
    owing = bank.owing(paid=300)
    other = bank.owing()
    with respx.mock as router:
        router.post(SYNC).mock(return_value=page([transaction("t1", -7, "ACH CREDIT")]))
        assert await sync_deposits(bank.bank) == 1
    assert bank.matches() == {("t1", owing.id, "amount")}
    # Nothing recorded: the order still owes 700, the other 1000.
    assert [p.amount_minor for p in bank.rows.payments(owing.id)] == [300]
    assert bank.rows.payments(other.id) == []


async def test_a_deposit_naming_an_order_number_is_suggested(bank: Bank) -> None:
    owing = bank.owing()
    with respx.mock as router:
        router.post(SYNC).mock(
            return_value=page(
                [transaction("t1", -3.5, f"ZELLE {str(owing.number).lower()} THANKS")]
            )
        )
        await sync_deposits(bank.bank)
    assert bank.matches() == {("t1", owing.id, "order_number")}


async def test_a_cancelled_order_is_not_suggested(bank: Bank) -> None:
    order = bank.owing()
    bank.shop.lifecycle.cancel_order(order.id, BY)
    with respx.mock as router:
        router.post(SYNC).mock(return_value=page([transaction("t1", -10, f"{order.number}")]))
        await sync_deposits(bank.bank)
    assert bank.matches() == set()


async def test_pages_run_until_has_more_is_false(bank: Bank) -> None:
    with respx.mock as router:
        route = router.post(SYNC).mock(
            side_effect=[
                page([transaction("t1", -1, "A")], cursor="c1", more=True),
                page([transaction("t2", -2, "B")], cursor="c2"),
            ]
        )
        await sync_deposits(bank.bank)
        assert json.loads(route.calls[1].request.content)["cursor"] == "c1"
    assert [d["transaction_id"] for d in bank.deposits()] == ["t1", "t2"]
    assert bank.cursor() == "c2"


async def test_a_mutation_restarts_the_loop_from_its_first_cursor(bank: Bank) -> None:
    with respx.mock as router:
        route = router.post(SYNC).mock(
            side_effect=[
                page([transaction("t1", -1, "A")], cursor="c1", more=True),
                httpx.Response(400, json=payload("error.sync_mutation.json")),
                page([transaction("t1", -1, "A")], cursor="c1", more=True),
                page([transaction("t2", -2, "B")], cursor="c2"),
            ]
        )
        await sync_deposits(bank.bank)
        assert "cursor" not in json.loads(route.calls[2].request.content)
    assert [d["transaction_id"] for d in bank.deposits()] == ["t1", "t2"]
    assert bank.cursor() == "c2"


async def test_the_next_sync_starts_at_the_stored_cursor_and_removes_and_modifies(
    bank: Bank,
) -> None:
    owing = bank.owing()
    with respx.mock as router:
        router.post(SYNC).mock(
            return_value=page(
                [transaction("t1", -10, "ACH"), transaction("t2", -4, "B")], cursor="c1"
            )
        )
        await sync_deposits(bank.bank)
    assert bank.matches() == {("t1", owing.id, "amount")}
    with respx.mock as router:
        route = router.post(SYNC).mock(
            return_value=page(modified=[transaction("t1", -9, "ACH")], removed=["t2"], cursor="c2")
        )
        await sync_deposits(bank.bank)
        assert json.loads(route.calls.last.request.content)["cursor"] == "c1"
    assert [(d["transaction_id"], d["amount_minor"]) for d in bank.deposits()] == [("t1", 900)]
    # The amount no longer equals the balance: the suggestion goes.
    assert bank.matches() == set()


async def test_a_plaid_failure_keeps_the_cursor_and_its_code(bank: Bank) -> None:
    with respx.mock as router:
        router.post(SYNC).mock(
            return_value=httpx.Response(400, json=payload("error.invalid_api_keys.json"))
        )
        assert await sync_deposits(bank.bank) == 0
    assert bank.cursor() is None
    assert bank.bank.status().sync_error == "INVALID_API_KEYS"
    with respx.mock as router:
        router.post(SYNC).mock(return_value=page([], cursor="c1"))
        await sync_deposits(bank.bank)
    status = bank.bank.status()
    assert status.sync_error is None and status.synced_at is not None


async def test_unlink_drops_the_deposits_and_the_cursor(bank: Bank) -> None:
    bank.owing()
    with respx.mock as router:
        router.post(SYNC).mock(return_value=page([transaction("t1", -10, "ACH")]))
        await sync_deposits(bank.bank)
        router.post(f"{SANDBOX}/item/remove").mock(
            return_value=httpx.Response(200, json=payload("item.remove.json"))
        )
        await bank.bank.unlink()
    assert bank.deposits() == [] and bank.matches() == set()
    assert bank.cursor() is None


async def test_a_deposit_older_than_the_order_is_not_suggested(bank: Bank) -> None:
    bank.owing()
    with respx.mock as router:
        router.post(SYNC).mock(
            return_value=page([transaction("t1", -10, "ACH", date="2020-01-02")])
        )
        await sync_deposits(bank.bank)
    assert bank.matches() == set()
