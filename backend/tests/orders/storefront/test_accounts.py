"""Storefront accounts (#5283, spec 2026-11-02 §5.2): a passwordless sign-in by a
sixteen-character code, server-side sessions, the customer link rule, and what an account
sees. No address can be locked: nothing one client does stops another from signing in."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg import Connection
from psycopg.rows import DictRow, dict_row

from scadbuddy.orders.storefront.accounts import (
    CODE_ALPHABET,
    CODES_PER_ADDRESS,
    CODES_PER_ADDRESS_CLIENT,
    CODES_PER_CLIENT,
    FAILURES_PER_CLIENT,
    EmailInvalidError,
    SignedIn,
    SignInLimitedError,
    account_for_session,
    account_owns_order,
    attach_order,
    end_session,
    normalized_code,
    normalized_email,
    orders_of_account,
    request_code,
    verify_code,
)
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

type Conn = Connection[DictRow]

EMAIL = "Ada@Example.com"
CLIENT = "203.0.113.9"
#: The owner's second device.
LAPTOP = "203.0.113.10"
VISITOR = "v" * 43
CODE_SHAPE = re.compile("-".join([f"[{CODE_ALPHABET}]{{4}}"] * 4))


@pytest.fixture
def conn(pg_pool: PgPool) -> Iterator[Conn]:
    with pg_pool.connection() as conn:
        yield conn


def customer(conn: Conn, name: str, email: str | None, *, archived: bool = False) -> str:
    row = conn.execute(
        "INSERT INTO customers (display_name, archived_at) VALUES (%s, %s) RETURNING id",
        (name, "2026-01-01" if archived else None),
    ).fetchone()
    assert row is not None
    if email is not None:
        conn.execute(
            "INSERT INTO customer_contacts (customer_id, kind, value, position)"
            " VALUES (%s, 'email', %s, 0)",
            (row["id"], email),
        )
    return str(row["id"])


def verify(conn: Conn, code: str, *, email: str = EMAIL, client: str = CLIENT) -> SignedIn | None:
    return verify_code(conn, email, code, client, visitor_id=None, previous_session=None)


def code_for(conn: Conn, email: str = EMAIL, client: str = CLIENT) -> str:
    code = request_code(conn, email, client)
    assert code is not None
    return code


def sign_in(conn: Conn, email: str = EMAIL) -> SignedIn:
    code = code_for(conn, email)
    signed = verify(conn, code, email=email)
    assert signed is not None
    return signed


def wrong(code: str) -> str:
    """Another code of the same shape."""
    first = CODE_ALPHABET[(CODE_ALPHABET.index(code[0]) + 1) % len(CODE_ALPHABET)]
    return first + code[1:]


def test_an_address_is_one_plain_lowercased_address() -> None:
    assert normalized_email("  Ada@Example.COM ") == "ada@example.com"
    for bad in (
        "",
        "ada",
        "a@b",
        "a b@example.com",
        "a@example.com, b@example.com",
        "a@example.com\r\nBcc: x@y.z",
        "<a@example.com>",
        "a" * 250 + "@x.io",
    ):
        with pytest.raises(EmailInvalidError):
            normalized_email(bad)


def test_the_alphabet_has_no_ambiguous_characters() -> None:
    assert len(CODE_ALPHABET) == len(set(CODE_ALPHABET)) == 30
    assert not set("01ILOU") & set(CODE_ALPHABET)


def test_a_code_is_sixteen_characters_shown_grouped_and_only_its_digest_is_kept(
    conn: Conn,
) -> None:
    code = code_for(conn)
    assert CODE_SHAPE.fullmatch(code)
    rows = conn.execute("SELECT * FROM storefront_sign_in_codes").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["email"] == "ada@example.com" and row["client"] == CLIENT
    bare = normalized_code(code)
    assert code not in str(dict(row)) and bare not in str(dict(row))
    assert row["code_sha256"] == hashlib.sha256(f"{row['id']}:{bare}".encode()).hexdigest()


def test_the_right_code_signs_in_once_and_makes_a_session(conn: Conn) -> None:
    code = code_for(conn)
    signed = verify(conn, code)
    assert signed is not None
    assert signed.account.email == "ada@example.com"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", signed.session_token)
    session = conn.execute("SELECT * FROM storefront_sessions").fetchone()
    assert session is not None and signed.session_token not in str(dict(session))
    assert account_for_session(conn, signed.session_token) == signed.account
    # Single use: the same code again reads nothing.
    assert verify(conn, code) is None


@pytest.mark.parametrize("typed", ["{bare}", "{lower}", " {a} {b} ", "{a}--{b}"])
def test_a_code_reads_whatever_its_case_spaces_or_hyphens(conn: Conn, typed: str) -> None:
    code = code_for(conn)
    bare = normalized_code(code)
    text = typed.format(bare=bare, lower=code.lower(), a=bare[:8], b=bare[8:])
    assert verify(conn, text) is not None


def test_a_code_asked_for_on_one_client_signs_in_on_another(conn: Conn) -> None:
    code = code_for(conn)
    assert verify(conn, code, client=LAPTOP) is not None


def test_wrong_guesses_never_spend_a_correct_code(conn: Conn) -> None:
    code = code_for(conn)
    for n in range(100):
        assert verify(conn, wrong(code), client=f"198.51.100.{n % 10}") is None
    assert verify(conn, code, client=LAPTOP) is not None


def test_an_expired_code_reads_nothing(conn: Conn) -> None:
    code = code_for(conn)
    conn.execute("UPDATE storefront_sign_in_codes SET expires_at = now() - interval '1 second'")
    assert verify(conn, code) is None


def test_requests_are_limited_per_address_and_client_and_per_client(conn: Conn) -> None:
    for _ in range(CODES_PER_ADDRESS_CLIENT):
        code_for(conn)
    with pytest.raises(SignInLimitedError) as limited:
        code_for(conn)
    assert 0 < limited.value.retry_after <= 3600
    # Another client still gets a code for the address.
    assert CODE_SHAPE.fullmatch(code_for(conn, client=LAPTOP))
    for n in range(CODES_PER_CLIENT - CODES_PER_ADDRESS_CLIENT):
        request_code(conn, f"other{n}@example.com", CLIENT)
    with pytest.raises(SignInLimitedError):
        request_code(conn, "fresh@example.com", CLIENT)


def test_others_requests_and_guesses_never_lock_the_owner_out(conn: Conn) -> None:
    count = math.ceil(CODES_PER_ADDRESS / CODES_PER_ADDRESS_CLIENT) + 1
    attackers = [f"198.51.100.{n}" for n in range(count)]
    inbox: list[str] = []
    for attacker in attackers:
        for _ in range(CODES_PER_ADDRESS_CLIENT):
            code = request_code(conn, EMAIL, attacker)
            if code is not None:
                inbox.append(code)
    # The cap is full: every code made went to the owner's inbox, and no more are made.
    assert len(inbox) == CODES_PER_ADDRESS
    assert request_code(conn, EMAIL, CLIENT) is None
    for n in range(100):
        guess = wrong(inbox[n % len(inbox)])
        assert verify(conn, guess, client=attackers[n % len(attackers)]) is None
    # The owner signs in from a fresh client with any code in the inbox.
    assert verify(conn, inbox[0], client="192.0.2.77") is not None


def test_a_full_caps_codes_are_live_until_the_cap_frees(conn: Conn) -> None:
    inbox = fill_cap(conn)
    # 59 minutes on: the cap is still full, so the owner gets no new code ...
    conn.execute(
        "UPDATE storefront_sign_in_codes SET created_at = created_at - interval '59 minutes',"
        " expires_at = expires_at - interval '59 minutes'"
    )
    assert request_code(conn, EMAIL, CLIENT) is None
    # ... but every code the cap counts is still live, so an inbox code signs in.
    assert verify(conn, inbox[0], client=CLIENT) is not None


def fill_cap(conn: Conn) -> list[str]:
    """Every code the cap allows for `EMAIL`, from as few clients as their limit lets."""
    count = math.ceil(CODES_PER_ADDRESS / CODES_PER_ADDRESS_CLIENT)
    inbox = [
        code
        for n in range(count)
        for _ in range(CODES_PER_ADDRESS_CLIENT)
        if (code := request_code(conn, EMAIL, f"198.51.100.{n}")) is not None
    ]
    assert len(inbox) == CODES_PER_ADDRESS
    return inbox


def test_a_sign_in_frees_the_cap(conn: Conn) -> None:
    inbox = fill_cap(conn)
    assert request_code(conn, EMAIL, CLIENT) is None
    assert verify(conn, inbox[3]) is not None
    fresh = request_code(conn, EMAIL, LAPTOP)
    assert fresh is not None
    assert verify(conn, fresh, client=LAPTOP) is not None


@pytest.mark.parametrize("live", [0, 1, CODES_PER_ADDRESS])
def test_a_verify_compares_as_many_digests_whatever_codes_are_live(
    conn: Conn, monkeypatch: pytest.MonkeyPatch, live: int
) -> None:
    codes = fill_cap(conn) if live == CODES_PER_ADDRESS else [code_for(conn)] * live
    calls: list[int] = []
    real = hmac.compare_digest

    def counted(a: str, b: str) -> bool:
        calls.append(1)
        return bool(real(a, b))

    # `accounts` calls `hmac.compare_digest` through the module, so patch it there.
    monkeypatch.setattr(hmac, "compare_digest", counted)
    assert verify(conn, wrong(codes[0]) if codes else "ABCD-EFGH-JKMN-PQRS") is None
    assert len(calls) == CODES_PER_ADDRESS
    if codes:
        calls.clear()
        assert verify(conn, codes[-1], client=LAPTOP) is not None
        assert len(calls) == CODES_PER_ADDRESS


def test_the_mail_cap_makes_no_code_and_raises_nothing(conn: Conn) -> None:
    others = [code_for(conn, client=f"198.51.100.{n}") for n in range(CODES_PER_ADDRESS)]
    assert len({*others}) == CODES_PER_ADDRESS
    # Past the cap: no code, no row, no refusal; the caller sends nothing.
    assert request_code(conn, EMAIL, CLIENT) is None
    count = conn.execute("SELECT count(*) AS n FROM storefront_sign_in_codes").fetchone()
    assert count is not None and count["n"] == CODES_PER_ADDRESS
    # Not a lockout: a code another client's request put in the inbox signs in here.
    assert verify(conn, others[-1]) is not None
    # Other addresses are not capped by it.
    assert code_for(conn, "bob@example.com")


@pytest.mark.parametrize(
    ("first", "second", "shared"),
    [
        ("2001:db8:1:2::1", "2001:db8:1:2::ffff", True),  # one /64
        ("2001:db8:1:2::1", "2001:db8:1:3::1", False),
        ("203.0.113.9", "203.0.113.10", False),  # IPv4 whole
    ],
)
def test_ipv6_clients_are_counted_by_their_64(
    conn: Conn, first: str, second: str, shared: bool
) -> None:
    for _ in range(CODES_PER_ADDRESS_CLIENT):
        code_for(conn, client=first)
    if shared:
        with pytest.raises(SignInLimitedError):
            request_code(conn, EMAIL, second)
    else:
        assert code_for(conn, client=second)


def test_a_code_typed_at_once_signs_in_once(conn: Conn, pg_conninfo: str) -> None:
    code = code_for(conn)
    tries = 10
    start = threading.Barrier(tries)

    def use(n: int) -> SignedIn | None:
        with psycopg.connect(pg_conninfo, row_factory=dict_row) as own, own.transaction():
            start.wait()
            return verify_code(
                own, EMAIL, code, f"198.51.100.{n}", visitor_id=None, previous_session=None
            )

    with ThreadPoolExecutor(tries) as pool:
        signed = [s for s in pool.map(use, range(tries)) if s is not None]
    assert len(signed) == 1


def test_requests_at_once_never_pass_a_cap(conn: Conn, pg_conninfo: str) -> None:
    requests = 10
    start = threading.Barrier(requests)

    def ask(n: int) -> bool:
        try:
            with psycopg.connect(pg_conninfo, row_factory=dict_row) as own, own.transaction():
                start.wait()
                # Half for one address, half for others: both caps are under test.
                request_code(own, EMAIL if n % 2 else f"x{n}@example.com", CLIENT)
        except SignInLimitedError:
            return False
        return True

    with ThreadPoolExecutor(requests) as pool:
        issued = sum(pool.map(ask, range(requests)))
    rows = conn.execute(
        "SELECT count(*) FILTER (WHERE email = 'ada@example.com') AS ada, count(*) AS n"
        " FROM storefront_sign_in_codes"
    ).fetchone()
    assert rows is not None
    assert rows["ada"] == CODES_PER_ADDRESS_CLIENT and rows["n"] == issued == 8


def test_wrong_guesses_are_limited_per_client(conn: Conn) -> None:
    for n in range(FAILURES_PER_CLIENT):
        assert verify(conn, "ABCD-EFGH-JKMN-PQRS", email=f"x{n}@example.com") is None
    code = code_for(conn, client="198.51.100.1")
    with pytest.raises(SignInLimitedError):
        verify(conn, code)
    # Another client is not held back by this one's guesses.
    assert verify(conn, code, client="198.51.100.1") is not None


def test_the_account_links_the_one_customer_with_that_email(conn: Conn) -> None:
    known = customer(conn, "Ada", "ada@EXAMPLE.com")
    customer(conn, "Bob", "bob@example.com")
    signed = sign_in(conn)
    assert signed.account.customer_id == known


@pytest.mark.parametrize("others", [0, 2])
def test_with_none_or_several_customers_a_new_one_is_made(conn: Conn, others: int) -> None:
    existing = {customer(conn, f"Ada {n}", EMAIL) for n in range(others)}
    signed = sign_in(conn)
    made = signed.account.customer_id
    assert made not in existing
    contact = conn.execute(
        'SELECT kind, value, "primary" FROM customer_contacts WHERE customer_id = %s', (made,)
    ).fetchone()
    assert contact == {"kind": "email", "value": "ada@example.com", "primary": True}


def test_a_second_sign_in_keeps_the_account_and_rotates_the_session(conn: Conn) -> None:
    first = sign_in(conn)
    code = code_for(conn)
    second = verify_code(
        conn,
        EMAIL,
        code,
        CLIENT,
        visitor_id=None,
        previous_session=first.session_token,
    )
    assert second is not None
    assert second.account == first.account
    assert second.session_token != first.session_token
    # Rotation deletes the old session's row, not only its use.
    assert account_for_session(conn, first.session_token) is None
    left = conn.execute("SELECT count(*) AS n FROM storefront_sessions").fetchone()
    assert left is not None and left["n"] == 1
    assert account_for_session(conn, second.session_token) == second.account


def test_sign_out_ends_the_session_and_an_expired_one_reads_nothing(conn: Conn) -> None:
    signed = sign_in(conn)
    token = signed.session_token
    end_session(conn, token)
    assert account_for_session(conn, token) is None
    again = sign_in(conn)
    conn.execute("UPDATE storefront_sessions SET expires_at = now() - interval '1 second'")
    assert account_for_session(conn, again.session_token) is None
    for bad in ("", "x", "v" * 44, "../" + "v" * 40):
        assert account_for_session(conn, bad) is None


def order(conn: Conn, visitor_row: str | None) -> str:
    row = conn.execute(
        "INSERT INTO orders (source, storefront_visitor_id, currency, created_by)"
        """ VALUES ('storefront', %s, 'USD', '{"kind": "visitor", "principal": "t"}')"""
        " RETURNING id",
        (visitor_row,),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def visitor_row(conn: Conn, visitor_id: str) -> str:
    row = conn.execute(
        "SELECT id FROM storefront_visitors WHERE token_sha256 = %s",
        (hashlib.sha256(visitor_id.encode()).hexdigest(),),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def test_a_visitor_with_no_row_still_joins(conn: Conn) -> None:
    unknown = "u" * 43
    code = code_for(conn)
    signed = verify_code(conn, EMAIL, code, CLIENT, visitor_id=unknown, previous_session=None)
    assert signed is not None
    row = conn.execute(
        "SELECT account_id, last_seen_at > now() - interval '1 minute' AS fresh"
        " FROM storefront_visitors WHERE token_sha256 = %s",
        (hashlib.sha256(unknown.encode()).hexdigest(),),
    ).fetchone()
    assert row is not None and row["account_id"] == signed.account.id and row["fresh"]


def test_a_sign_in_marks_only_live_codes_used(conn: Conn) -> None:
    old = code_for(conn, client=LAPTOP)
    conn.execute("UPDATE storefront_sign_in_codes SET expires_at = now() - interval '1 second'")
    sign_in(conn)
    row = conn.execute(
        "SELECT used_at FROM storefront_sign_in_codes WHERE client = %s", (LAPTOP,)
    ).fetchone()
    assert row is not None and row["used_at"] is None
    assert verify(conn, old, client=LAPTOP) is None


def test_the_visitor_joins_the_account_and_its_orders_are_the_accounts(conn: Conn) -> None:
    code = code_for(conn)
    signed = verify_code(conn, EMAIL, code, CLIENT, visitor_id=VISITOR, previous_session=None)
    assert signed is not None
    mine = order(conn, visitor_row(conn, VISITOR))
    stranger = order(conn, None)
    assert orders_of_account(conn, signed.account.id) == [mine]
    assert account_owns_order(conn, signed.account.id, mine)
    assert not account_owns_order(conn, signed.account.id, stranger)
    # An order whose link was opened while signed in joins it, once.
    attach_order(conn, signed.account.id, stranger)
    attach_order(conn, signed.account.id, stranger)
    assert sorted(orders_of_account(conn, signed.account.id)) == sorted([mine, stranger])


def test_a_visitor_of_another_account_is_not_moved(conn: Conn) -> None:
    code = code_for(conn)
    ada = verify_code(conn, EMAIL, code, CLIENT, visitor_id=VISITOR, previous_session=None)
    assert ada is not None
    adas = order(conn, visitor_row(conn, VISITOR))
    code = code_for(conn, "bob@example.com")
    bob = verify_code(
        conn, "bob@example.com", code, CLIENT, visitor_id=VISITOR, previous_session=None
    )
    assert bob is not None
    assert orders_of_account(conn, bob.account.id) == []
    assert orders_of_account(conn, ada.account.id) == [adas]


def test_the_same_answer_whether_or_not_an_account_exists(conn: Conn) -> None:
    sign_in(conn)
    # Both addresses get a code the same way; nothing says which has an account.
    known = code_for(conn, EMAIL, "198.51.100.2")
    unknown = code_for(conn, "nobody@example.com", "198.51.100.2")
    assert CODE_SHAPE.fullmatch(known) and CODE_SHAPE.fullmatch(unknown)
