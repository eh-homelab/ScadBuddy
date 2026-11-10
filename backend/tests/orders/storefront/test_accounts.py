"""Storefront accounts (#5283, spec 2026-11-02 §5.2): a passwordless sign-in by a six-digit
code, server-side sessions, the customer link rule, and what an account sees."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator

import pytest
from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.orders.storefront.accounts import (
    CODE_TRIES,
    CODES_PER_ADDRESS,
    CODES_PER_CLIENT,
    FAILURES_PER_CLIENT,
    EmailInvalidError,
    SignedIn,
    SignInLimitedError,
    account_for_session,
    account_owns_order,
    attach_order,
    end_session,
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
VISITOR = "v" * 43


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


def sign_in(conn: Conn, email: str = EMAIL) -> SignedIn:
    code = request_code(conn, email, CLIENT)
    signed = verify_code(conn, email, code, CLIENT, visitor_id=None, previous_session=None)
    assert signed is not None
    return signed


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


def test_a_code_is_six_digits_and_only_its_digest_is_kept(conn: Conn) -> None:
    code = request_code(conn, EMAIL, CLIENT)
    assert re.fullmatch(r"\d{6}", code)
    rows = conn.execute("SELECT * FROM storefront_sign_in_codes").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["email"] == "ada@example.com" and row["client"] == CLIENT
    assert code not in str(dict(row))
    assert row["code_sha256"] == hashlib.sha256(f"{row['id']}:{code}".encode()).hexdigest()


def test_the_right_code_signs_in_once_and_makes_a_session(conn: Conn) -> None:
    code = request_code(conn, EMAIL, CLIENT)
    signed = verify_code(conn, EMAIL, code, CLIENT, visitor_id=None, previous_session=None)
    assert signed is not None
    assert signed.account.email == "ada@example.com"
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", signed.session_token)
    session = conn.execute("SELECT * FROM storefront_sessions").fetchone()
    assert session is not None and signed.session_token not in str(dict(session))
    assert account_for_session(conn, signed.session_token) == signed.account
    # Single use: the same code again reads nothing.
    assert verify_code(conn, EMAIL, code, CLIENT, visitor_id=None, previous_session=None) is None


def test_a_wrong_code_counts_and_the_fifth_wrong_try_spends_the_code(conn: Conn) -> None:
    code = request_code(conn, EMAIL, CLIENT)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for _ in range(CODE_TRIES):
        assert (
            verify_code(conn, EMAIL, wrong, CLIENT, visitor_id=None, previous_session=None) is None
        )
    assert verify_code(conn, EMAIL, code, CLIENT, visitor_id=None, previous_session=None) is None


def test_an_expired_code_or_a_superseded_one_reads_nothing(conn: Conn) -> None:
    old = request_code(conn, EMAIL, CLIENT)
    new = request_code(conn, EMAIL, CLIENT)
    assert verify_code(conn, EMAIL, old, CLIENT, visitor_id=None, previous_session=None) is None
    conn.execute("UPDATE storefront_sign_in_codes SET expires_at = now() - interval '1 second'")
    assert verify_code(conn, EMAIL, new, CLIENT, visitor_id=None, previous_session=None) is None


def test_codes_are_limited_per_address_and_per_client(conn: Conn) -> None:
    for _ in range(CODES_PER_ADDRESS):
        request_code(conn, EMAIL, CLIENT)
    with pytest.raises(SignInLimitedError) as limited:
        request_code(conn, EMAIL, "198.51.100.1")
    assert 0 < limited.value.retry_after <= 3600
    for n in range(CODES_PER_CLIENT - CODES_PER_ADDRESS):
        request_code(conn, f"other{n}@example.com", CLIENT)
    with pytest.raises(SignInLimitedError):
        request_code(conn, "fresh@example.com", CLIENT)


def test_wrong_guesses_are_limited_per_client(conn: Conn) -> None:
    for n in range(FAILURES_PER_CLIENT):
        assert (
            verify_code(
                conn, f"x{n}@example.com", "000000", CLIENT, visitor_id=None, previous_session=None
            )
            is None
        )
    code = request_code(conn, EMAIL, "198.51.100.1")
    with pytest.raises(SignInLimitedError):
        verify_code(conn, EMAIL, code, CLIENT, visitor_id=None, previous_session=None)
    # Another client is not held back by this one's guesses.
    assert verify_code(conn, EMAIL, code, "198.51.100.1", visitor_id=None, previous_session=None)


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
    code = request_code(conn, EMAIL, CLIENT)
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


def test_the_visitor_joins_the_account_and_its_orders_are_the_accounts(conn: Conn) -> None:
    code = request_code(conn, EMAIL, CLIENT)
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
    code = request_code(conn, EMAIL, CLIENT)
    ada = verify_code(conn, EMAIL, code, CLIENT, visitor_id=VISITOR, previous_session=None)
    assert ada is not None
    adas = order(conn, visitor_row(conn, VISITOR))
    code = request_code(conn, "bob@example.com", CLIENT)
    bob = verify_code(
        conn, "bob@example.com", code, CLIENT, visitor_id=VISITOR, previous_session=None
    )
    assert bob is not None
    assert orders_of_account(conn, bob.account.id) == []
    assert orders_of_account(conn, ada.account.id) == [adas]


def test_the_same_answer_whether_or_not_an_account_exists(conn: Conn) -> None:
    sign_in(conn)
    # Both addresses get a code the same way; nothing says which has an account.
    known = request_code(conn, EMAIL, "198.51.100.2")
    unknown = request_code(conn, "nobody@example.com", "198.51.100.2")
    assert re.fullmatch(r"\d{6}", known) and re.fullmatch(r"\d{6}", unknown)
