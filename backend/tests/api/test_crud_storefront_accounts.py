"""The accounts area's CRUD routes (#5283, spec 2026-11-02 §5.2): a sign-in whose session
token comes back only in a header, sign-out, the account, and what it sees. Only the
``storefront`` credential reaches them."""

from __future__ import annotations

import hashlib
import re

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from scadbuddy.core.settings import Settings
from scadbuddy.orders.storefront.accounts import FAILURES_PER_CLIENT_ADDRESS, request_code
from scadbuddy.storefront.wire import CLIENT_HEADER, SESSION_HEADER, VISITOR_HEADER
from tests.api.test_crud_storefront_pay import token

BASE = "/api/v1/internal/crud/storefront/account"
VISITOR = "v" * 43
HEADERS = {VISITOR_HEADER: VISITOR, CLIENT_HEADER: "203.0.113.9"}
PROBLEMS = "https://scadbuddy.dev/problems/storefront/"


def code_for(settings: Settings, email: str) -> str:
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        code = request_code(conn, email, "192.0.2.1")
    assert code is not None
    return code


def headers(
    client: TestClient, session: str | None = None, visitor: str = VISITOR
) -> dict[str, str]:
    sent = {**token(client), **HEADERS, VISITOR_HEADER: visitor}
    if session is not None:
        sent[SESSION_HEADER] = session
    return sent


def sign_in(
    client: TestClient, settings: Settings, email: str = "ada@example.com", visitor: str = VISITOR
) -> str:
    code = code_for(settings, email)
    answer = client.post(
        f"{BASE}/verify",
        json={"email": email, "code": code},
        headers=headers(client, visitor=visitor),
    )
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"email": email}
    session: str = answer.headers[SESSION_HEADER]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", session)
    assert session not in answer.text
    return session


def storefront_order(settings: Settings, visitor: str, number: str) -> str:
    """A submitted storefront order of ``visitor``, for its account's customer."""
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT v.id, a.customer_id FROM storefront_visitors v"
            " JOIN storefront_accounts a ON a.id = v.account_id WHERE v.token_sha256 = %s",
            (hashlib.sha256(visitor.encode()).hexdigest(),),
        ).fetchone()
        assert row is not None
        order = conn.execute(
            "INSERT INTO orders (source, storefront_visitor_id, customer_id, currency,"
            " created_by, number, submitted_at) VALUES ('storefront', %s, %s, 'USD',"
            """ '{"kind": "visitor", "principal": "t"}', %s, now()) RETURNING id""",
            (row["id"], row["customer_id"], number),
        ).fetchone()
        assert order is not None
        return str(order["id"])


def test_a_sign_in_answers_the_account_and_puts_the_session_in_a_header(
    client: TestClient, settings: Settings
) -> None:
    session = sign_in(client, settings)
    me = client.get(BASE, headers=headers(client, session))
    assert me.status_code == 200 and me.json() == {"email": "ada@example.com"}


def test_a_wrong_code_is_one_refusal(client: TestClient, settings: Settings) -> None:
    code_for(settings, "ada@example.com")
    answer = client.post(
        f"{BASE}/verify",
        json={"email": "ada@example.com", "code": "ZZZZZZZZ"},
        headers=headers(client),
    )
    assert answer.status_code == 400
    assert answer.json()["type"] == f"{PROBLEMS}sign_in_failed"
    assert SESSION_HEADER not in answer.headers
    nobody = client.post(
        f"{BASE}/verify",
        json={"email": "nobody@example.com", "code": "ZZZZZZZZ"},
        headers=headers(client),
    )
    assert nobody.status_code == 400 and nobody.json()["detail"] == answer.json()["detail"]


@pytest.mark.parametrize("body", [{}, {"email": "a@example.com"}, {"email": 1, "code": "x"}])
def test_a_body_that_is_not_a_sign_in_is_refused(
    client: TestClient, body: dict[str, object]
) -> None:
    answer = client.post(f"{BASE}/verify", json=body, headers=headers(client))
    assert answer.status_code == 422
    assert answer.json()["type"] == f"{PROBLEMS}account_body_invalid"


def test_too_many_wrong_codes_from_a_client_at_an_address_are_429(client: TestClient) -> None:
    for _ in range(FAILURES_PER_CLIENT_ADDRESS):
        client.post(
            f"{BASE}/verify",
            json={"email": "x@example.com", "code": "ZZZZZZZZ"},
            headers=headers(client),
        )
    answer = client.post(
        f"{BASE}/verify",
        json={"email": "x@example.com", "code": "ZZZZZZZZ"},
        headers=headers(client),
    )
    assert answer.status_code == 429
    assert answer.json()["type"] == f"{PROBLEMS}sign_in_limited"
    assert int(answer.headers["Retry-After"]) > 0


def test_sign_out_ends_the_session(client: TestClient, settings: Settings) -> None:
    session = sign_in(client, settings)
    out = client.post(f"{BASE}/sign-out", headers=headers(client, session))
    assert out.status_code == 204
    assert client.get(BASE, headers=headers(client, session)).json() is None
    orders = client.get(f"{BASE}/orders", headers=headers(client, session))
    assert orders.status_code == 401
    assert orders.json()["type"] == f"{PROBLEMS}sign_in_required"


def test_a_sign_in_ends_the_session_the_browser_held(
    client: TestClient, settings: Settings
) -> None:
    first = sign_in(client, settings)
    code = code_for(settings, "ada@example.com")
    again = client.post(
        f"{BASE}/verify",
        json={"email": "ada@example.com", "code": code},
        headers=headers(client, first),
    )
    assert again.status_code == 200
    assert client.get(BASE, headers=headers(client, first)).json() is None
    assert client.get(BASE, headers=headers(client, again.headers[SESSION_HEADER])).json()


def test_a_session_whose_customer_is_gone_reads_as_none(
    client: TestClient, settings: Settings
) -> None:
    session = sign_in(client, settings)
    with psycopg.connect(settings.database_url) as conn:
        # The foreign key refuses the delete; drop it as a damaged database would.
        conn.execute(
            "ALTER TABLE storefront_accounts DROP CONSTRAINT storefront_accounts_customer_id_fkey"
        )
        conn.execute("DELETE FROM customer_contacts")
        conn.execute("DELETE FROM customers")
    assert client.get(BASE, headers=headers(client, session)).json() is None
    assert client.get(f"{BASE}/orders", headers=headers(client, session)).status_code == 401


def test_each_account_lists_only_its_own_orders(client: TestClient, settings: Settings) -> None:
    ada_visitor, bob_visitor = "a" * 43, "b" * 43
    ada = sign_in(client, settings, "ada@example.com", ada_visitor)
    bob = sign_in(client, settings, "bob@example.com", bob_visitor)
    adas = storefront_order(settings, ada_visitor, "ORD-2026-1")
    bobs = storefront_order(settings, bob_visitor, "ORD-2026-2")
    listed = client.get(f"{BASE}/orders", headers=headers(client, ada, ada_visitor)).json()
    assert [o["id"] for o in listed["orders"]] == [adas]
    (row,) = listed["orders"]
    # Only what the list shows: no cost, note, customer or event.
    assert set(row) == {"id", "number", "status", "created_at", "submitted_at", "items"}
    assert row["number"] == "ORD-2026-1" and row["items"] == 0
    listed = client.get(f"{BASE}/orders", headers=headers(client, bob, bob_visitor)).json()
    assert [o["id"] for o in listed["orders"]] == [bobs]


def test_a_cart_not_yet_submitted_is_not_listed(client: TestClient, settings: Settings) -> None:
    session = sign_in(client, settings)
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        visitor = conn.execute("SELECT id FROM storefront_visitors").fetchone()
        assert visitor is not None
        conn.execute(
            "INSERT INTO orders (source, storefront_visitor_id, currency, created_by)"
            """ VALUES ('storefront', %s, 'USD', '{"kind": "visitor", "principal": "t"}')""",
            (visitor["id"],),
        )
    listed = client.get(f"{BASE}/orders", headers=headers(client, session)).json()
    assert listed == {"orders": []}


def test_only_the_storefront_credential_reaches_the_area(client: TestClient) -> None:
    for caller in ("agent", "print"):
        sent = {**token(client, caller), **HEADERS}
        assert client.get(BASE, headers=sent).status_code == 403
