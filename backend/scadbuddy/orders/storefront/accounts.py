"""Storefront accounts (#5283, spec 2026-11-02 §5.2): a passwordless sign-in. A visitor
asks for a code by email (`request_code`), types it back (`verify_code`), and gets a
server-side session. Every function runs on the caller's connection, in its transaction:
the code's mail is queued in the same one (`orders/storefront/mail.py` `queue_mail`).

Codes:

- Eight characters from `CODE_ALPHABET` (Crockford's base32 less ``0`` and ``1``; it has
  no ``I``, ``L``, ``O`` or ``U`` already), drawn with `secrets`: 30^8, about 2^39. A code
  is good for `CODE_TTL` (10 minutes) and `CODE_TRIES` (5) guesses, and is single use.
- A new code does not void the live ones: up to `LIVE_CODES` (3) live codes of an address
  are each good, and only a fourth voids the oldest. So someone who asks for codes for
  another's address cannot cancel the code that person is about to type.
- Only a digest is kept: sha256 over ``<row id>:<code>``. The row id salts it, so one
  table of digests does not read every row. It is not a keyed hash. Someone who can read
  the table can try one row's 2^39 codes, but that reader already reads every order and
  customer, and the code dies in 10 minutes. A key would need the KEK, which a deployment
  may lack, so the salt is the choice.
- The digests are compared in constant time (`hmac.compare_digest`), against every live
  code, with no early exit. A correct live code always signs in.
- A verify holds a transaction lock per address (`pg_advisory_xact_lock`) before it reads
  the codes, so guesses at once cannot spend more than a code's `CODE_TRIES`.
- Limits:
  - `CODES_PER_ADDRESS` codes an hour per address, and `CODES_PER_CLIENT` per client
    address. Over either is `SignInLimitedError` with the seconds to wait.
  - `FAILURES_PER_CLIENT` wrong guesses an hour per client (`SignInLimitedError`).
- No address is ever locked: a lock would let anyone who asks for a code and guesses
  wrong lock its owner out. The bound is the code itself. An address gets at most
  `CODES_PER_ADDRESS` codes an hour with `CODE_TRIES` tries each, so at most 25 guesses an
  hour from any number of clients, each with a chance of at most `LIVE_CODES` in 30^8
  (about 2^39). A success then takes about 30^8 / (3 * 25), about 8.7 * 10^9 hours.
  Failures are kept by address too, for the logs only.
- The route that asks for a code takes the bot check (Turnstile, action ``sign_up``)
  whenever a site key is set (`storefront/turnstile.py` `require_turnstile`).

Enumeration: `request_code` does the same thing for every valid address, whether or not
an account exists. It always makes and returns a code, so the route can always queue the
mail and answer the same way. Sign-up and sign-in are one flow: the account is made at
the first verify.

Sessions: the token is `secrets.token_urlsafe(32)` (256 bits). Only its sha256 is
kept. It lives `SESSION_TTL` (30 days). A sign-in makes a new token and deletes the
session the browser held before (rotation), and sign-out deletes the row.

The customer link (§5.2): at the first verify, an account links the one customer, not
archived, with that address as an email contact. With none or several, it makes a new
customer with the address as its primary email contact, and the shop merges by hand.

What an account sees (§5.2): the orders its visitors made (`storefront_visitors.account_id`,
set at verify for a visitor of no account; a visitor of another account is never moved)
and the orders attached to it (`attach_order`: a link opened while signed in). Old codes,
failures and expired sessions are deleted as new rows are written.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from scadbuddy.orders.store import Conn
from scadbuddy.orders.storefront.visitors import token_sha256 as visitor_sha256

#: Crockford's base32 (https://www.crockford.com/base32.html) less ``0`` and ``1``.
CODE_ALPHABET: Final = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH: Final = 8
CODE_TTL: Final = timedelta(minutes=10)
CODE_TRIES: Final = 5
LIVE_CODES: Final = 3
CODES_PER_ADDRESS: Final = 5
CODES_PER_CLIENT: Final = 10
FAILURES_PER_CLIENT: Final = 20
LIMIT_WINDOW: Final = timedelta(hours=1)
#: How long a code or failure row is kept: past the limits' window, for the logs.
KEEP_ROWS: Final = timedelta(days=1)
SESSION_TTL: Final = timedelta(days=30)
#: An address's longest form (RFC 5321 §4.5.3.1.3's path, less its brackets).
MAX_EMAIL: Final = 254
_CODE: Final = re.compile(f"[{CODE_ALPHABET}]{{{CODE_LENGTH}}}")
#: What a person may type around a code: spaces and hyphens, in any case.
_CODE_SPACING: Final = re.compile(r"[\s-]")
_TOKEN: Final = re.compile(r"[A-Za-z0-9_-]{43}")
#: One plain address: no spaces, commas, brackets or control characters, one ``@`` and a
#: dotted domain. The mail step checks it again (`queue_mail`'s `email_invalid`).
_EMAIL: Final = re.compile(r"[^\s@,;<>()\[\]\\\"]+@[^\s@,;<>()\[\]\\\"]+\.[^\s@,;<>()\[\]\\\".]+")


class EmailInvalidError(ValueError):
    """Not one plain address."""


class SignInLimitedError(Exception):
    """Over a sign-in limit; ``retry_after`` is the seconds to wait."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"try again in {retry_after} seconds")
        self.retry_after = retry_after


@dataclass(frozen=True)
class Account:
    id: str
    email: str
    customer_id: str


@dataclass(frozen=True)
class SignedIn:
    account: Account
    #: The new session's token, for the edge's cookie. Only its sha256 is stored.
    session_token: str


def normalized_email(raw: str) -> str:
    """``raw`` trimmed and lowercased; `EmailInvalidError` unless it is one plain address."""
    email = raw.strip().lower()
    if len(email) > MAX_EMAIL or _EMAIL.fullmatch(email) is None:
        raise EmailInvalidError("not one plain address")
    return email


def _code_digest(row_id: str, code: str) -> str:
    return hashlib.sha256(f"{row_id}:{code}".encode()).hexdigest()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _limit(conn: Conn, table: str, column: str, value: str, at: str, most: int) -> None:
    """`SignInLimitedError` when ``table`` holds ``most`` rows of ``value`` in the window."""
    row = conn.execute(
        f"SELECT count(*) AS n, min({at}) AS oldest, now() AS now FROM {table}"
        f" WHERE {column} = %s AND {at} > now() - %s",
        (value, LIMIT_WINDOW),
    ).fetchone()
    assert row is not None
    if row["n"] >= most:
        wait = (row["oldest"] + LIMIT_WINDOW - row["now"]).total_seconds()
        raise SignInLimitedError(max(1, math.ceil(wait)))


def request_code(conn: Conn, email: str, client: str) -> str:
    """A new code for ``email``, asked from ``client``, to mail to it.

    Enumeration-safe: it does the same for every valid address, whether or not an account
    exists, so its caller answers the same way. `EmailInvalidError` for an address that is
    not one, and `SignInLimitedError` over a limit; neither depends on an account. The
    address's live codes stay good, up to `LIVE_CODES`; only the oldest beyond that is
    voided."""
    address = normalized_email(email)
    conn.execute("DELETE FROM storefront_sign_in_codes WHERE created_at < now() - %s", (KEEP_ROWS,))
    _limit(conn, "storefront_sign_in_codes", "email", address, "created_at", CODES_PER_ADDRESS)
    _limit(conn, "storefront_sign_in_codes", "client", client, "created_at", CODES_PER_CLIENT)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    row_id = secrets.token_hex(16)
    conn.execute(
        "INSERT INTO storefront_sign_in_codes (id, email, client, code_sha256, expires_at)"
        " VALUES (%s, %s, %s, %s, now() + %s)",
        (row_id, address, client, _code_digest(row_id, code), CODE_TTL),
    )
    conn.execute(
        "UPDATE storefront_sign_in_codes SET used_at = now() WHERE id IN ("
        " SELECT id FROM storefront_sign_in_codes"
        " WHERE email = %s AND used_at IS NULL AND expires_at > now()"
        " ORDER BY created_at DESC, id OFFSET %s)",
        (address, LIVE_CODES),
    )
    return code


def _failed(conn: Conn, client: str, address: str | None) -> None:
    """A wrong guess by ``client``, at ``address`` when it was one (kept for the logs)."""
    conn.execute(
        "DELETE FROM storefront_sign_in_failures WHERE failed_at < now() - %s", (KEEP_ROWS,)
    )
    conn.execute(
        "INSERT INTO storefront_sign_in_failures (client, email) VALUES (%s, %s)",
        (client, address),
    )


def verify_code(
    conn: Conn,
    email: str,
    code: str,
    client: str,
    *,
    visitor_id: str | None,
    previous_session: str | None,
) -> SignedIn | None:
    """The sign-in ``code`` makes for ``email``, or None for any code that reads nothing
    (wrong, expired, used, voided, out of tries, or no code at all): one answer, so a
    caller says nothing more. `SignInLimitedError` once ``client`` guessed wrong
    `FAILURES_PER_CLIENT` times in the window."""
    _limit(
        conn,
        "storefront_sign_in_failures",
        "client",
        client,
        "failed_at",
        FAILURES_PER_CLIENT,
    )
    try:
        address = normalized_email(email)
    except EmailInvalidError:
        _failed(conn, client, None)
        return None
    # One verify of an address at a time, so guesses at once never overspend a code.
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"storefront_signin:{address}",)
    )
    rows = conn.execute(
        "SELECT id, code_sha256 FROM storefront_sign_in_codes"
        " WHERE email = %s AND used_at IS NULL AND expires_at > now() AND attempts < %s"
        " ORDER BY created_at DESC, id LIMIT %s FOR UPDATE",
        (address, CODE_TRIES, LIVE_CODES),
    ).fetchall()
    given = _CODE_SPACING.sub("", code).upper()
    shaped = _CODE.fullmatch(given) is not None
    matched = False
    for row in rows:
        # Every live code is compared, with no early exit.
        same = hmac.compare_digest(_code_digest(row["id"], given), row["code_sha256"])
        matched = matched or (shaped and same)
    if not matched:
        if rows:
            conn.execute(
                "UPDATE storefront_sign_in_codes SET attempts = attempts + 1 WHERE id = ANY(%s)",
                ([row["id"] for row in rows],),
            )
        _failed(conn, client, address)
        return None
    conn.execute(
        "UPDATE storefront_sign_in_codes SET used_at = now() WHERE email = %s AND used_at IS NULL",
        (address,),
    )
    account = _account(conn, address)
    if visitor_id is not None:
        _join(conn, visitor_id, account.id)
    if previous_session is not None and _TOKEN.fullmatch(previous_session):
        conn.execute(
            "DELETE FROM storefront_sessions WHERE token_sha256 = %s",
            (_token_digest(previous_session),),
        )
    conn.execute(
        "DELETE FROM storefront_sessions WHERE account_id = %s AND expires_at <= now()",
        (account.id,),
    )
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO storefront_sessions (account_id, token_sha256, expires_at)"
        " VALUES (%s, %s, now() + %s)",
        (account.id, _token_digest(token), SESSION_TTL),
    )
    return SignedIn(account=account, session_token=token)


def _account(conn: Conn, address: str) -> Account:
    """The address's account, made at its first sign-in with its customer (§5.2). Two
    first sign-ins of one address wait on one lock, so one customer is made."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"storefront_account:{address}",)
    )
    row = conn.execute(
        "UPDATE storefront_accounts SET last_sign_in_at = now() WHERE email = %s"
        " RETURNING id, email, customer_id",
        (address,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "INSERT INTO storefront_accounts (email, customer_id) VALUES (%s, %s)"
            " RETURNING id, email, customer_id",
            (address, _customer(conn, address)),
        ).fetchone()
    assert row is not None
    return Account(id=row["id"], email=row["email"], customer_id=row["customer_id"])


def _customer(conn: Conn, address: str) -> str:
    """The one customer, not archived, with ``address`` as an email contact; else a new
    customer with it as the primary email contact."""
    found = conn.execute(
        "SELECT DISTINCT c.id FROM customers c JOIN customer_contacts k ON k.customer_id = c.id"
        " WHERE c.archived_at IS NULL AND k.kind = 'email' AND lower(trim(k.value)) = %s",
        (address,),
    ).fetchall()
    if len(found) == 1:
        return str(found[0]["id"])
    made = conn.execute(
        "INSERT INTO customers (display_name) VALUES (%s) RETURNING id", (address,)
    ).fetchone()
    assert made is not None
    conn.execute(
        'INSERT INTO customer_contacts (customer_id, kind, value, "primary", position)'
        " VALUES (%s, 'email', %s, true, 0)",
        (made["id"], address),
    )
    return str(made["id"])


def _join(conn: Conn, visitor_id: str, account_id: str) -> None:
    """The visitor joins the account, unless it belongs to another one already: a shared
    browser's earlier account keeps its orders."""
    conn.execute(
        "INSERT INTO storefront_visitors (token_sha256, account_id) VALUES (%s, %s)"
        " ON CONFLICT (token_sha256) DO UPDATE SET account_id = EXCLUDED.account_id,"
        " last_seen_at = now() WHERE storefront_visitors.account_id IS NULL",
        (visitor_sha256(visitor_id), account_id),
    )


def account_for_session(conn: Conn, token: str) -> Account | None:
    """The account of a live session, or None for any token that names none."""
    if _TOKEN.fullmatch(token) is None:
        return None
    row = conn.execute(
        "SELECT a.id, a.email, a.customer_id, s.id AS session_id,"
        " s.last_seen_at < now() - interval '1 minute' AS stale"
        " FROM storefront_sessions s JOIN storefront_accounts a ON a.id = s.account_id"
        " WHERE s.token_sha256 = %s AND s.expires_at > now()",
        (_token_digest(token),),
    ).fetchone()
    if row is None:
        return None
    if row["stale"]:
        conn.execute(
            "UPDATE storefront_sessions SET last_seen_at = now() WHERE id = %s",
            (row["session_id"],),
        )
    return Account(id=row["id"], email=row["email"], customer_id=row["customer_id"])


def end_session(conn: Conn, token: str) -> None:
    """Sign-out: the session's row is deleted. An unknown token does nothing."""
    if _TOKEN.fullmatch(token) is not None:
        conn.execute(
            "DELETE FROM storefront_sessions WHERE token_sha256 = %s", (_token_digest(token),)
        )


_OWNED: Final = (
    "SELECT o.id, o.created_at FROM orders o"
    " JOIN storefront_visitors v ON v.id = o.storefront_visitor_id WHERE v.account_id = %(a)s"
    " UNION SELECT o.id, o.created_at FROM orders o"
    " JOIN storefront_account_orders ao ON ao.order_id = o.id WHERE ao.account_id = %(a)s"
)


def orders_of_account(conn: Conn, account_id: str) -> list[str]:
    """What the account sees (§5.2): its visitors' orders and the ones attached to it,
    newest first."""
    rows = conn.execute(
        f"SELECT id FROM ({_OWNED}) owned ORDER BY created_at DESC, id",
        {"a": account_id},
    ).fetchall()
    return [str(row["id"]) for row in rows]


def account_owns_order(conn: Conn, account_id: str, order_id: str) -> bool:
    """Whether the account sees ``order_id`` (`orders_of_account`)."""
    row = conn.execute(
        f"SELECT EXISTS (SELECT 1 FROM ({_OWNED}) owned WHERE id = %(o)s) AS owns",
        {"a": account_id, "o": order_id},
    ).fetchone()
    return bool(row and row["owns"])


def attach_order(conn: Conn, account_id: str, order_id: str) -> None:
    """The order joins the account: its link was opened while signed in (§5.2). The
    caller checked the link. A second attach does nothing."""
    conn.execute(
        "INSERT INTO storefront_account_orders (account_id, order_id) VALUES (%s, %s)"
        " ON CONFLICT DO NOTHING",
        (account_id, order_id),
    )
