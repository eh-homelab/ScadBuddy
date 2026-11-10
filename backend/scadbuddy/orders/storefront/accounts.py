"""Storefront accounts (#5283, spec 2026-11-02 §5.2): a passwordless sign-in. A visitor
asks for a code by email (`request_code`), types it back (`verify_code`), and gets a
server-side session. Every function runs on the caller's connection, in its transaction,
so the route that asks for a code can queue its mail in the same one (the code route, a
later PR).

The client: every limit below is kept per client, the browser's address as the edge
trusts it (`core/proxies.py` `client_address`: a trusted proxy's ``X-Forwarded-For``, else
the peer; never a raw header), sent as ``X-ScadBuddy-Store-Client`` and grouped by
`core/proxies.py` `client_key`: an IPv4 address whole, an IPv6 one as its /64. The
visitor cookie is not used: a visitor can drop it at will.

Codes:

- Sixteen characters from `CODE_ALPHABET` (Crockford's base32 less ``0`` and ``1``; it
  has no ``I``, ``L``, ``O`` or ``U`` already), drawn with `secrets`: 30^16, about 4.3 *
  10^23 or 2^78. A code is shown grouped, ``XXXX-XXXX-XXXX-XXXX`` (`request_code` returns
  it so), and read with case, spaces and hyphens ignored; the mail also carries a sign-in
  link, so nobody types it. It is single use and dies only when it is used or when
  `CODE_TTL` (an hour) ends: no wrong guess, by anyone, ever spends it. At 78 bits a
  per-code try count adds nothing to the bound below, and it let an attacker kill the
  owner's codes.
- Only a digest is kept: sha256 over ``<row id>:<code>``. The row id salts it, so one
  table of digests does not read every row. It is not a keyed hash, but a 78-bit code
  that lives an hour cannot be found from its digest, and a key would need the KEK,
  which a deployment may lack.
- The digests are compared in constant time (`hmac.compare_digest`), against every live
  code of the address, with no early exit.

No address can be locked: nothing one client does stops another from signing in.

- A correct live code signs in from any client: a code asked for on a phone can be typed
  on a laptop, and every code in the inbox works, whoever asked for it.
- Requests are limited per address and client (`CODES_PER_ADDRESS_CLIENT`, 3 an hour)
  and per client (`CODES_PER_CLIENT`, 10 an hour); each refusal is `SignInLimitedError`.
- Mail to one address is capped (`CODES_PER_ADDRESS`, 20): the cap counts the address's
  live codes (unused and unexpired), from all clients. Past the cap `request_code` answers
  as usual, raising nothing, but makes no code and returns None, so no mail is sent. That
  is not a lockout: whenever the cap is full, 20 live codes are in the owner's inbox,
  whatever anyone guesses, and each signs in from any client. A sign-in marks every unused
  code used, which frees the cap at once. Mail flooding stays bounded: a code lives an
  hour (`CODE_TTL`), and only someone who read the mail can use one to free the cap, so
  others can send at most 20 an hour.
- There is no address-wide guess budget: it would be a lock.

The guess bound. Wrong guesses are limited per client: `FAILURES_PER_CLIENT` (20) an hour
here, by the client key above, and on the edge the bot check's failure limits (#5857: per
IPv4 address or IPv6 /64, and per /24 or /48). A guess is compared with every live code
of the address, and the cap bounds those too: at most 20 are live. Each guess has a
chance of at most 20 in 30^16 (4.3 * 10^23), about 4.6 * 10^-23. At G = 2 * 10^5 guesses
an hour (10^4 clients each at its limit, solving the bot check every time), that is about
10^-17 an hour, or 8 * 10^-14 a year, per address.

The bot check: whenever a site key is set, the route that asks for a code takes
Turnstile with action ``sign_up``, and the route that verifies one with ``sign_in`` (the
account routes, a later PR). This extends spec §5.8.

Concurrency: a request and a verify each hold transaction locks on the client, then on
the address (`_lock`), before they count, read or spend anything, so requests at once
never pass a cap and a code signs in at most once. Each caller must run them inside a
transaction.

Enumeration: `request_code` does the same thing for every valid address, whether or not
an account exists. Under the cap it makes and returns a code, and the route queues the
mail; past the cap it returns None, and the route sends nothing but answers the same way.
The cap counts codes, not accounts, so neither case says whether an account exists.
Sign-up and sign-in are one flow: the account is made at the first verify.

Sessions: the token is `secrets.token_urlsafe(32)` (256 bits). Only its sha256 is
kept. It lives `SESSION_TTL` (30 days). A sign-in makes a new token and deletes the
session the browser held before (rotation), and sign-out deletes the row.

The customer link (§5.2): at the first verify, an account links the one customer, not
archived, with that address as an email contact. With none or several, it makes a new
customer with the address as its primary email contact, and the shop merges by hand.

What an account sees (§5.2): the orders its visitors made (`storefront_visitors.account_id`,
set at verify for a visitor of no account; a visitor of another account is never moved)
and the orders attached to it (`attach_order`: a link opened while signed in). Each
request deletes codes past `KEEP_ROWS`, each wrong guess failures past it, and each
sign-in every expired session, so the tables need no sweep.
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

from scadbuddy.core.proxies import client_key
from scadbuddy.orders.store import Conn
from scadbuddy.orders.storefront.visitors import token_sha256 as visitor_sha256

#: Crockford's base32 (https://www.crockford.com/base32.html) less ``0`` and ``1``.
CODE_ALPHABET: Final = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH: Final = 16
#: The code as shown: groups of this many characters, joined by hyphens.
CODE_GROUP: Final = 4
CODES_PER_ADDRESS_CLIENT: Final = 3
CODES_PER_CLIENT: Final = 10
#: Live codes one address may have, from all clients: the mail cap, and the most a
#: verify compares.
CODES_PER_ADDRESS: Final = 20
FAILURES_PER_CLIENT: Final = 20
LIMIT_WINDOW: Final = timedelta(hours=1)
#: How long a code lives; with the cap, others can mail an address at most 20 an hour.
CODE_TTL: Final = timedelta(hours=1)
#: How long a code or failure row is kept: past the limits' window.
KEEP_ROWS: Final = timedelta(days=1)
SESSION_TTL: Final = timedelta(days=30)
#: An address's longest form (RFC 5321 §4.5.3.1.3's path, less its brackets).
MAX_EMAIL: Final = 254
_CODE: Final = re.compile(f"[{CODE_ALPHABET}]{{{CODE_LENGTH}}}")
#: What a person may type around a code: spaces and hyphens, in any case.
_CODE_SPACING: Final = re.compile(r"[\s-]")
#: What a missing live code is compared with: no code's digest.
_DUMMY: Final = "0" * 64
_TOKEN: Final = re.compile(r"[A-Za-z0-9_-]{43}")
#: One plain address: no spaces, commas, brackets or control characters, one ``@`` and a
#: dotted domain. The code route's mail step (a later PR) will check it again.
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


def normalized_code(raw: str) -> str:
    """``raw`` with spaces and hyphens taken out, in upper case."""
    return _CODE_SPACING.sub("", raw).upper()


def _code_digest(row_id: str, code: str) -> str:
    return hashlib.sha256(f"{row_id}:{code}".encode()).hexdigest()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _lock(conn: Conn, client: str, address: str | None) -> None:
    """The client's sign-in lock, then the address's, each held to the end of the
    caller's transaction. Always in that order, so two callers never wait on each other."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"storefront_signin_client:{client}",),
    )
    if address is not None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"storefront_signin:{address}",),
        )


def _wait(oldest: object, now: object, window: timedelta) -> int:
    return max(1, math.ceil((oldest + window - now).total_seconds()))  # type: ignore[operator]


def _limit(conn: Conn, sql: str, args: tuple[object, ...], most: int) -> None:
    """`SignInLimitedError` when ``sql`` (``count(*) AS n, min(<at>) AS oldest``) counts
    ``most`` rows in `LIMIT_WINDOW`."""
    row = conn.execute(sql, (*args, LIMIT_WINDOW)).fetchone()
    assert row is not None
    if row["n"] >= most:
        raise SignInLimitedError(_wait(row["oldest"], row["now"], LIMIT_WINDOW))


def request_code(conn: Conn, email: str, client: str) -> str | None:
    """A new code for ``email``, asked from ``client``, to mail to it, as
    ``XXXX-XXXX-XXXX-XXXX``; or None past the address's mail cap (`CODES_PER_ADDRESS`),
    when the caller sends nothing and answers exactly as it would with a code.

    Enumeration-safe: it does the same for every valid address, whether or not an account
    exists, so its caller answers the same way. `EmailInvalidError` for an address that is
    not one, and `SignInLimitedError` over a client's limit; neither depends on an
    account. No code already made is voided."""
    address = normalized_email(email)
    client = client_key(client)
    _lock(conn, client, address)
    conn.execute("DELETE FROM storefront_sign_in_codes WHERE created_at < now() - %s", (KEEP_ROWS,))
    _limit(
        conn,
        "SELECT count(*) AS n, min(created_at) AS oldest, now() AS now"
        " FROM storefront_sign_in_codes"
        " WHERE email = %s AND client = %s AND created_at > now() - %s",
        (address, client),
        CODES_PER_ADDRESS_CLIENT,
    )
    _limit(
        conn,
        "SELECT count(*) AS n, min(created_at) AS oldest, now() AS now"
        " FROM storefront_sign_in_codes WHERE client = %s AND created_at > now() - %s",
        (client,),
        CODES_PER_CLIENT,
    )
    capped = conn.execute(
        "SELECT count(*) >= %s AS full FROM storefront_sign_in_codes"
        " WHERE email = %s AND used_at IS NULL AND expires_at > now()",
        (CODES_PER_ADDRESS, address),
    ).fetchone()
    if capped is not None and capped["full"]:
        return None
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    row_id = secrets.token_hex(16)
    conn.execute(
        "INSERT INTO storefront_sign_in_codes (id, email, client, code_sha256, expires_at)"
        " VALUES (%s, %s, %s, %s, now() + %s)",
        (row_id, address, client, _code_digest(row_id, code), CODE_TTL),
    )
    return "-".join(code[i : i + CODE_GROUP] for i in range(0, CODE_LENGTH, CODE_GROUP))


def _failed(conn: Conn, client: str, address: str | None) -> None:
    """A wrong guess by ``client``, at ``address`` when it was one."""
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
    (wrong, expired, used, or no code at all): one answer, so a
    caller says nothing more. `SignInLimitedError` once ``client`` guessed wrong
    `FAILURES_PER_CLIENT` times in the window."""
    try:
        address: str | None = normalized_email(email)
    except EmailInvalidError:
        address = None
    client = client_key(client)
    _lock(conn, client, address)
    _limit(
        conn,
        "SELECT count(*) AS n, min(failed_at) AS oldest, now() AS now"
        " FROM storefront_sign_in_failures WHERE client = %s AND failed_at > now() - %s",
        (client,),
        FAILURES_PER_CLIENT,
    )
    if address is None:
        _failed(conn, client, None)
        return None
    rows = conn.execute(
        "SELECT id, code_sha256 FROM storefront_sign_in_codes"
        " WHERE email = %s AND used_at IS NULL AND expires_at > now()"
        " ORDER BY created_at DESC, id LIMIT %s FOR UPDATE",
        (address, CODES_PER_ADDRESS),
    ).fetchall()
    given = normalized_code(code)
    shaped = _CODE.fullmatch(given) is not None
    matched = False
    # Always `CODES_PER_ADDRESS` digests and compares, live codes padded with a dummy, with
    # no early exit: the time taken does not show how many live codes the address has.
    for n in range(CODES_PER_ADDRESS):
        row_id, stored = (rows[n]["id"], rows[n]["code_sha256"]) if n < len(rows) else ("", _DUMMY)
        same = hmac.compare_digest(_code_digest(row_id, given), stored)
        matched = matched or (shaped and same and n < len(rows))
    if not matched:
        _failed(conn, client, address)
        return None
    conn.execute(
        "UPDATE storefront_sign_in_codes SET used_at = now()"
        " WHERE email = %s AND used_at IS NULL AND expires_at > now()",
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
    conn.execute("DELETE FROM storefront_sessions WHERE expires_at <= now()")
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
    customer with it as the primary email contact.

    A customer with two email contacts may have an account for each, and both accounts
    see that customer's orders. That is intended: each address is one the customer
    gave the shop."""
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
    # An upsert: the edge normally registers the visitor first, but a visitor whose row
    # was pruned (or never written) still joins, with a fresh row seen now.
    conn.execute(
        "INSERT INTO storefront_visitors (token_sha256, account_id, last_seen_at)"
        " VALUES (%s, %s, now())"
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
