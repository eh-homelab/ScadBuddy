"""The deposit sync (#4961): read the linked account's new deposits from Plaid and suggest
the orders each may pay. A suggestion is a ``bank_deposit_matches`` row, never a payment:
the shop confirms it on the order. Run by the housekeeping sweep
``housekeeping_sync_bank_deposits`` (``workflows/housekeeping.py``), never by a loop in
the API. Plaid's facts this rests on (the sign of ``amount``, pending transactions, the
restart on ``TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION``) are ``plaid.py``'s docstring.

What is kept of a deposit: its Plaid id, amount, currency, date and the bank's text with
every run of five or more digits masked (`mask`), a run going on across single spaces or
hyphens, so a number printed in groups (an IBAN) is masked too; an order number is kept.
The mask cannot know every format a bank prints, so the text is kept short
(``DESCRIPTION_CHARS``) and never logged. Only posted deposits on the linked account are
read: a pending one is removed by Plaid when it posts, and the posted one comes with a new
id. A suggestion is:

- ``order_number``: the text names an order's number (``ORD-<yyyy>-<n>``), on an order
  neither cancelled nor closed;
- ``amount``: the deposit equals the balance of an order past ``draft``, neither cancelled
  nor closed, in the deposit's currency, made on or before the deposit's date.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from pydantic import BaseModel

from scadbuddy.orders.currency import minor_exponent
from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.models import Money, Payment
from scadbuddy.orders.store import Conn, OrderStore
from scadbuddy.orders.totals import totals_of
from scadbuddy.payments import plaid
from scadbuddy.payments.bank import AccessTokenMissingError, BankLinks, LinkedAccess
from scadbuddy.payments.credentials import CredentialUnreadableError, KekMissingError
from scadbuddy.payments.plaid import PlaidError

logger = logging.getLogger(__name__)

#: An order number in a deposit's text, any case, leading zeros allowed in the count.
ORDER_NUMBER: Final = re.compile(r"\bORD-(\d{4})-0*(\d{1,9})\b", re.IGNORECASE)
#: Digits, and digits joined by single spaces or hyphens: one run for `mask`.
_DIGIT_RUN: Final = re.compile(r"\d(?:[ -]?\d)*")
#: The fewest digits a run holds for `mask` to hide it.
MASKED_DIGITS: Final = 5
#: The longest text kept of a deposit.
DESCRIPTION_CHARS: Final = 200
#: Pages one sync reads before it stores its cursor and stops (500 transactions each).
MAX_PAGES: Final = 10
#: How many times a sync starts its page loop again after a mutation during pagination.
MAX_RESTARTS: Final = 2
#: The order states no deposit is suggested for.
ENDED: Final = ("cancelled", "closed")


def _mask_runs(text: str) -> str:
    return _DIGIT_RUN.sub(
        lambda m: "•••" if sum(c.isdigit() for c in m.group()) >= MASKED_DIGITS else m.group(),
        text,
    )


def mask(text: str) -> str:
    """``text`` with every run of ``MASKED_DIGITS`` or more digits replaced, a run going on
    across single spaces or hyphens. Order numbers (``ORDER_NUMBER``) are kept as they are."""
    kept, last = [], 0
    for m in ORDER_NUMBER.finditer(text):
        kept += [_mask_runs(text[last : m.start()]), m.group()]
        last = m.end()
    kept.append(_mask_runs(text[last:]))
    return "".join(kept)


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
    #: Plaid still had more pages when ``MAX_PAGES`` stopped the loop.
    more: bool = False


async def _read(
    linked: plaid.PlaidKeys, access_token: str, start: str | None, first: str | None
) -> _Read:
    """Every page from ``start``, at most ``MAX_PAGES``. On a mutation the loop starts
    again from ``first``, the cursor of the update's first page: Plaid says to restart
    from there, and an update that ``MAX_PAGES`` cut short began on an earlier run. A
    transaction read twice is written once (by its id)."""
    for attempt in range(MAX_RESTARTS + 1):
        begin = start if attempt == 0 else first
        read = _Read(changed={}, removed=set(), cursor=begin or "")
        cursor = begin
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
                read.more = found.has_more
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
    try:
        linked = await asyncio.to_thread(bank.access)
    except (CredentialUnreadableError, KekMissingError, AccessTokenMissingError) as error:
        # Every run would fail the same way: the card shows why instead.
        code = _UNREADABLE[type(error)]
        logger.warning("the deposit sync could not open the link: %s", code)
        await asyncio.to_thread(_failed, bank.store, None, code)
        return 0
    if linked is None:
        return 0
    start, first = await asyncio.to_thread(_cursor, bank.store)
    try:
        read = await _read(linked.keys, linked.access_token, start, first)
    except PlaidError as error:
        logger.warning("the deposit sync could not read Plaid: %s", error.code)
        await asyncio.to_thread(_failed, bank.store, linked.item_id, error.code)
        return 0
    return await asyncio.to_thread(_store, bank.store, linked, start, first, read)


_UNREADABLE: Final[dict[type[Exception], str]] = {
    CredentialUnreadableError: "credential_unreadable",
    KekMissingError: "kek_missing",
    AccessTokenMissingError: "access_token_missing",
}


def _cursor(store: OrderStore) -> tuple[str | None, str | None]:
    """The stored cursor, and the first cursor of an update a run left unfinished (else
    the stored cursor itself)."""
    with store.transaction() as conn:
        row = conn.execute("SELECT sync_cursor, sync_loop_cursor FROM bank_links").fetchone()
    if row is None:
        return None, None
    loop = row["sync_loop_cursor"]
    # '' is "from the very start": the first update of a new link.
    first = (loop or None) if loop is not None else row["sync_cursor"]
    return row["sync_cursor"], first


def _failed(store: OrderStore, item_id: str | None, code: str | None) -> None:
    """Keep the failure's code on the link: on ``item_id``'s, or on any linked row."""
    with store.transaction() as conn:
        conn.execute(
            "UPDATE bank_links SET sync_error = %s WHERE item_id IS NOT NULL"
            + (" AND item_id = %s" if item_id is not None else ""),
            (code or "unavailable", *([item_id] if item_id is not None else [])),
        )


def _store(
    store: OrderStore, linked: LinkedAccess, start: str | None, first: str | None, read: _Read
) -> int:
    with store.transaction() as conn:
        now = conn.execute("SELECT item_id, sync_cursor FROM bank_links FOR UPDATE").fetchone()
        if now is None or now["item_id"] != linked.item_id or now["sync_cursor"] != start:
            # Unlinked, or another sync stored its cursor first: that one's reads stand.
            return 0
        suggested = _apply(store, conn, linked.item_id, linked.account_id, read)
        # An update cut short keeps its first cursor for a later run's restart.
        loop = (first or "") if read.more else None
        conn.execute(
            "UPDATE bank_links SET sync_cursor = %s, sync_loop_cursor = %s,"
            " synced_at = clock_timestamp(), sync_error = NULL WHERE item_id = %s",
            (read.cursor or None, loop, linked.item_id),
        )
    return suggested


def _apply(store: OrderStore, conn: Conn, item_id: str, account_id: str, read: _Read) -> int:
    gone = set(read.removed)
    suggested = 0
    balances = _Balances(store, conn)
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
        # A changed deposit is matched again; a dismissed suggestion stays dismissed.
        conn.execute(
            "DELETE FROM bank_deposit_matches WHERE transaction_id = %s AND dismissed_at IS NULL",
            (deposit.transaction_id,),
        )
        recorded = _recorded(conn, deposit.transaction_id)
        if recorded is not None:
            # Recorded already (before an unlink and a new link): never suggested again.
            conn.execute(
                "UPDATE bank_deposits SET payment_id = %s WHERE transaction_id = %s",
                (recorded, deposit.transaction_id),
            )
            continue
        for order_id, reason in _matches(conn, balances, deposit):
            added = conn.execute(
                "INSERT INTO bank_deposit_matches (transaction_id, order_id, reason)"
                " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (deposit.transaction_id, order_id, reason),
            )
            suggested += added.rowcount
    if gone:
        conn.execute("DELETE FROM bank_deposits WHERE transaction_id = ANY(%s)", (sorted(gone),))
    return suggested


class _Balances:
    """Each open order's balance, read once per sync: nothing the sync does changes one."""

    def __init__(self, store: OrderStore, conn: Conn) -> None:
        self._store = store
        self._conn = conn
        self._orders: list[tuple[str, str, date]] | None = None
        self._balances: dict[str, int] = {}

    def open_orders(self, currency: str, on: date) -> list[str]:
        """Orders past ``draft``, not ended, in ``currency``, made on or before ``on``."""
        if self._orders is None:
            self._orders = [
                (str(row["id"]), row["currency"], row["created_on"])
                for row in self._conn.execute(
                    "SELECT id, currency, created_at::date AS created_on FROM orders WHERE"
                    " number IS NOT NULL AND status <> ALL(%s) ORDER BY created_at",
                    ([*ENDED, "draft"],),
                ).fetchall()
            ]
        return [o for o, cur, made in self._orders if cur == currency and made <= on]

    def balance(self, order_id: str) -> int:
        if order_id not in self._balances:
            totals = totals_of(self._store, self._conn, order_id)
            self._balances[order_id] = totals.balance_minor
        return self._balances[order_id]


def _matches(conn: Conn, balances: _Balances, deposit: Deposit) -> list[tuple[str, str]]:
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
    for order_id in balances.open_orders(deposit.currency, deposit.posted_on):
        if order_id in found:
            continue
        if balances.balance(order_id) == deposit.amount_minor:
            found[order_id] = "amount"
    return list(found.items())


def _recorded(conn: Conn, transaction_id: str) -> str | None:
    """The payment already recorded from this deposit, on any order."""
    row = conn.execute(
        "SELECT id FROM order_payments WHERE method = %s AND reference = %s LIMIT 1",
        (METHOD, transaction_id),
    ).fetchone()
    return str(row["id"]) if row else None


# -- the order page ----------------------------------------------------------------------

#: The method a confirmed deposit is recorded with; its reference is the transaction id.
METHOD: Final = "bank_transfer"
MatchReason = Literal["amount", "order_number"]


class DepositSuggestion(BaseModel):
    """A deposit the order may have been paid by, as the order page shows it."""

    transaction_id: str
    amount_minor: Money
    currency: str
    posted_on: date
    #: The bank's text, every run of five or more digits masked.
    description: str
    reason: MatchReason


def suggestions(store: OrderStore, order_id: str) -> list[DepositSuggestion]:
    """The order's suggested deposits not dismissed and not recorded, newest first."""
    with store.transaction() as conn:
        found = conn.execute(
            "SELECT d.transaction_id, d.amount_minor, d.currency, d.posted_on,"
            " d.description, m.reason FROM bank_deposit_matches m JOIN bank_deposits d"
            " USING (transaction_id) WHERE m.order_id = %s AND m.dismissed_at IS NULL"
            " AND d.payment_id IS NULL AND NOT EXISTS (SELECT 1 FROM order_payments p"
            " WHERE p.method = %s AND p.reference = d.transaction_id)"
            " ORDER BY d.posted_on DESC, d.transaction_id",
            (order_id, METHOD),
        ).fetchall()
    return [DepositSuggestion.model_validate(row) for row in found]


def _not_suggested(order_id: str, transaction_id: str) -> LifecycleError:
    return LifecycleError(
        "deposit_not_suggested",
        f"deposit {transaction_id} is not suggested for order {order_id}",
        not_found=True,
    )


def record_deposit(
    store: OrderStore,
    order_id: str,
    transaction_id: str,
    add: Callable[..., Payment],
) -> Payment:
    """Record a suggested deposit as the order's payment, once. ``add`` is the order's
    ``Lifecycle.add_payment`` (its own transaction, under the order's lock); this one
    holds the deposit's row lock around it, so two confirmations record one payment."""
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT d.amount_minor, d.currency, d.posted_on, d.description, d.payment_id,"
            " o.currency AS order_currency FROM bank_deposits d JOIN bank_deposit_matches m"
            " USING (transaction_id) JOIN orders o ON o.id = m.order_id"
            " WHERE d.transaction_id = %s AND m.order_id = %s AND m.dismissed_at IS NULL"
            " FOR UPDATE OF d",
            (transaction_id, order_id),
        ).fetchone()
        if row is None:
            raise _not_suggested(order_id, transaction_id)
        if row["payment_id"] is not None or _recorded(conn, transaction_id) is not None:
            raise LifecycleError(
                "deposit_recorded", f"deposit {transaction_id} is already recorded"
            )
        if row["currency"] != row["order_currency"]:
            raise LifecycleError(
                "deposit_currency",
                f"the deposit is in {row['currency']}, the order in {row['order_currency']}",
            )
        payment = add(
            amount_minor=row["amount_minor"],
            method=METHOD,
            reference=transaction_id,
            # Plaid gives the date only; noon UTC keeps that date in most time zones.
            received_at=datetime.combine(row["posted_on"], time(12), UTC),
            notes=row["description"],
        )
        conn.execute(
            "UPDATE bank_deposits SET payment_id = %s WHERE transaction_id = %s",
            (payment.id, transaction_id),
        )
    return payment


def dismiss_deposit(store: OrderStore, order_id: str, transaction_id: str) -> None:
    """ "Not this order": the suggestion is not shown again, nor made again by a sync."""
    with store.transaction() as conn:
        done = conn.execute(
            "UPDATE bank_deposit_matches SET dismissed_at = clock_timestamp()"
            " WHERE transaction_id = %s AND order_id = %s AND dismissed_at IS NULL",
            (transaction_id, order_id),
        )
        if done.rowcount == 0:
            raise _not_suggested(order_id, transaction_id)
