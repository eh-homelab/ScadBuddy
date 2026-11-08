"""The deposit sync (#4961): read the linked account's new deposits from Plaid and suggest
the orders each may pay. A suggestion is a ``bank_deposit_matches`` row, never a payment:
the shop confirms it on the order. Run by the housekeeping sweep
``housekeeping_sync_bank_deposits`` (``workflows/housekeeping.py``), never by a loop in
the API. Plaid's facts this rests on (the sign of ``amount``, pending transactions, the
restart on ``TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION``) are ``plaid.py``'s docstring.

What is kept of a deposit: its Plaid id, amount, currency, date and the bank's text with
every run of five or more digits masked (`mask`), so an account number in a memo is never
stored or logged. Only posted deposits on the linked account are read: a pending one is
removed by Plaid when it posts, and the posted one comes with a new id. A suggestion is:

- ``order_number``: the text names an order's number (``ORD-<yyyy>-<n>``), on an order
  neither cancelled nor closed;
- ``amount``: the deposit equals the balance of an order past ``draft``, neither cancelled
  nor closed, in the deposit's currency, made on or before the deposit's date.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.store import Conn, OrderStore
from scadbuddy.orders.totals import totals_of
from scadbuddy.payments import plaid
from scadbuddy.payments.bank import BankLinks, LinkedAccess
from scadbuddy.payments.plaid import PlaidError

logger = logging.getLogger(__name__)

#: An order number in a deposit's text, any case, leading zeros allowed in the count.
ORDER_NUMBER: Final = re.compile(r"\bORD-(\d{4})-0*(\d{1,9})\b", re.IGNORECASE)
_LONG_DIGITS: Final = re.compile(r"\d{5,}")
#: The longest text kept of a deposit.
DESCRIPTION_CHARS: Final = 200
#: Pages one sync reads before it stores its cursor and stops (500 transactions each).
MAX_PAGES: Final = 10
#: How many times a sync starts its page loop again after a mutation during pagination.
MAX_RESTARTS: Final = 2
#: The order states no deposit is suggested for.
ENDED: Final = ("cancelled", "closed")


def mask(text: str) -> str:
    """``text`` with every run of five or more digits replaced: no account number stays."""
    return _LONG_DIGITS.sub("•••", text)


@dataclass(frozen=True)
class Deposit:
    transaction_id: str
    amount_minor: int
    currency: str
    posted_on: date
    #: The bank's text, unmasked: read for order numbers, never stored.
    text: str


def deposit_of(tx: dict[str, Any], account_id: str) -> Deposit | None:
    """The transaction as a deposit on ``account_id``; None for anything else."""
    amount, currency = tx.get("amount"), tx.get("iso_currency_code")
    if (
        tx.get("account_id") != account_id
        or tx.get("pending") is not False
        or not isinstance(tx.get("transaction_id"), str)
        or not isinstance(currency, str)
        or not isinstance(amount, int | float)
        or amount >= 0
    ):
        return None
    try:
        exponent = minor_exponent(currency.upper())
        minor = -Decimal(str(amount)).scaleb(exponent)
        posted = date.fromisoformat(str(tx.get("date")))
    except (ValueError, LookupError, InvalidOperation):
        return None
    if minor != minor.to_integral_value():
        return None
    text = tx.get("original_description") or tx.get("name") or ""
    return Deposit(
        transaction_id=tx["transaction_id"],
        amount_minor=int(minor),
        currency=currency.upper(),
        posted_on=posted,
        text=str(text),
    )


@dataclass
class _Read:
    """What one sync read: the transactions by id, the last change of each winning."""

    changed: dict[str, dict[str, Any]]
    removed: set[str]
    cursor: str


async def _read(linked: plaid.PlaidKeys, access_token: str, start: str | None) -> _Read:
    """Every page from ``start``, the loop started again on a mutation."""
    for _ in range(MAX_RESTARTS + 1):
        read = _Read(changed={}, removed=set(), cursor=start or "")
        cursor = start
        try:
            for _ in range(MAX_PAGES):
                found = await plaid.transactions_sync(linked, access_token, cursor)
                for tx in [*found.added, *found.modified]:
                    read.changed[str(tx.get("transaction_id"))] = tx
                    read.removed.discard(str(tx.get("transaction_id")))
                for tx_id in found.removed:
                    read.changed.pop(tx_id, None)
                    read.removed.add(tx_id)
                cursor = read.cursor = found.next_cursor
                if not found.has_more:
                    break
            return read
        except PlaidError as error:
            if error.code != plaid.SYNC_MUTATION:
                raise
    raise PlaidError("Plaid's transactions kept changing", plaid.SYNC_MUTATION)


async def sync_deposits(bank: BankLinks) -> int:
    """Read the linked account's deposits since the stored cursor and suggest their
    orders. The number of new suggestions. A Plaid failure keeps its code on the link
    and the cursor where it was. Every Postgres call runs in a thread: the sync also runs
    on the API's event loop."""
    linked = await asyncio.to_thread(bank.access)
    if linked is None:
        return 0
    start = await asyncio.to_thread(_cursor, bank.store)
    try:
        read = await _read(linked.keys, linked.access_token, start)
    except PlaidError as error:
        logger.warning("the deposit sync could not read Plaid: %s", error.code)
        await asyncio.to_thread(_failed, bank.store, linked.item_id, error.code)
        return 0
    return await asyncio.to_thread(_store, bank.store, linked, start, read)


def _cursor(store: OrderStore) -> str | None:
    with store.transaction() as conn:
        row = conn.execute("SELECT sync_cursor FROM bank_links").fetchone()
    return row["sync_cursor"] if row else None


def _failed(store: OrderStore, item_id: str, code: str | None) -> None:
    with store.transaction() as conn:
        conn.execute(
            "UPDATE bank_links SET sync_error = %s WHERE item_id = %s",
            (code or "unavailable", item_id),
        )


def _store(store: OrderStore, linked: LinkedAccess, start: str | None, read: _Read) -> int:
    with store.transaction() as conn:
        now = conn.execute("SELECT item_id, sync_cursor FROM bank_links FOR UPDATE").fetchone()
        if now is None or now["item_id"] != linked.item_id or now["sync_cursor"] != start:
            # Unlinked, or another sync stored its cursor first: that one's reads stand.
            return 0
        suggested = _apply(store, conn, linked.item_id, linked.account_id, read)
        conn.execute(
            "UPDATE bank_links SET sync_cursor = %s, synced_at = clock_timestamp(),"
            " sync_error = NULL WHERE item_id = %s",
            (read.cursor or None, linked.item_id),
        )
    return suggested


def _apply(store: OrderStore, conn: Conn, item_id: str, account_id: str, read: _Read) -> int:
    gone = set(read.removed)
    suggested = 0
    for tx_id, tx in read.changed.items():
        deposit = deposit_of(tx, account_id)
        if deposit is None:
            gone.add(tx_id)
            continue
        conn.execute(
            "INSERT INTO bank_deposits (transaction_id, item_id, amount_minor, currency,"
            " posted_on, description) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT"
            " (transaction_id) DO UPDATE SET amount_minor = EXCLUDED.amount_minor, currency ="
            " EXCLUDED.currency, posted_on = EXCLUDED.posted_on, description ="
            " EXCLUDED.description",
            (
                deposit.transaction_id,
                item_id,
                deposit.amount_minor,
                deposit.currency,
                deposit.posted_on,
                mask(deposit.text)[:DESCRIPTION_CHARS],
            ),
        )
        # A changed deposit is matched again from nothing.
        conn.execute(
            "DELETE FROM bank_deposit_matches WHERE transaction_id = %s",
            (deposit.transaction_id,),
        )
        for order_id, reason in _matches(store, conn, deposit):
            conn.execute(
                "INSERT INTO bank_deposit_matches (transaction_id, order_id, reason)"
                " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (deposit.transaction_id, order_id, reason),
            )
            suggested += 1
    if gone:
        conn.execute("DELETE FROM bank_deposits WHERE transaction_id = ANY(%s)", (sorted(gone),))
    return suggested


def _matches(store: OrderStore, conn: Conn, deposit: Deposit) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    numbers = sorted(
        {f"ORD-{m.group(1)}-{int(m.group(2))}" for m in ORDER_NUMBER.finditer(deposit.text)}
    )
    if numbers:
        for row in conn.execute(
            "SELECT id FROM orders WHERE number = ANY(%s) AND status <> ALL(%s)",
            (numbers, list(ENDED)),
        ).fetchall():
            found[str(row["id"])] = "order_number"
    for row in conn.execute(
        "SELECT id FROM orders WHERE number IS NOT NULL AND status <> ALL(%s)"
        " AND currency = %s AND created_at::date <= %s ORDER BY created_at",
        ([*ENDED, "draft"], deposit.currency, deposit.posted_on),
    ).fetchall():
        order_id = str(row["id"])
        if order_id in found:
            continue
        if totals_of(store, conn, order_id).balance_minor == deposit.amount_minor:
            found[order_id] = "amount"
    return list(found.items())
