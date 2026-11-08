"""SQL of the payments seam (#4957). Every function runs in the caller's transaction, so
a lifecycle step writes its payment, its request and its audit line together."""

from __future__ import annotations

from collections.abc import Collection

from psycopg.types.json import Jsonb

from scadbuddy.orders.models import Actor, Money
from scadbuddy.orders.store import Conn
from scadbuddy.payments.models import (
    EventOutcome,
    PaymentRequest,
    PaymentRequestKind,
    PaymentRequestStatus,
    PaymentRequestView,
    ProviderEvent,
)

_PUBLIC = (
    "id, order_id, kind, provider, amount_minor, currency, hosted_url, status, created_at, paid_at"
)
_VIEW = f"{_PUBLIC}, provider_ref, link_error, payment_id, created_by"


def insert_request(
    conn: Conn,
    *,
    order_id: str,
    kind: PaymentRequestKind,
    provider: str | None,
    amount_minor: Money,
    currency: str,
    created_by: Actor,
) -> PaymentRequestView:
    row = conn.execute(
        "INSERT INTO payment_requests"
        " (order_id, kind, provider, amount_minor, currency, created_by)"
        f" VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_VIEW}",
        (
            order_id,
            kind,
            provider,
            amount_minor,
            currency,
            Jsonb(created_by.model_dump(mode="json")),
        ),
    ).fetchone()
    assert row is not None
    return PaymentRequestView.model_validate(row)


def request(conn: Conn, request_id: str) -> PaymentRequestView | None:
    row = conn.execute(
        f"SELECT {_VIEW} FROM payment_requests WHERE id = %s", (request_id,)
    ).fetchone()
    return None if row is None else PaymentRequestView.model_validate(row)


def requests_for_order(conn: Conn, order_id: str) -> list[PaymentRequestView]:
    rows = conn.execute(
        f"SELECT {_VIEW} FROM payment_requests WHERE order_id = %s ORDER BY created_at, id",
        (order_id,),
    ).fetchall()
    return [PaymentRequestView.model_validate(r) for r in rows]


def open_requests_for_order(conn: Conn, order_id: str) -> list[PaymentRequest]:
    """Built from the public columns only, so ``provider_ref`` never leaves."""
    rows = conn.execute(
        f"SELECT {_PUBLIC} FROM payment_requests WHERE order_id = %s AND status = 'open'"
        " ORDER BY created_at, id",
        (order_id,),
    ).fetchall()
    return [PaymentRequest.model_validate(r) for r in rows]


def request_by_ref(conn: Conn, provider: str, provider_ref: str) -> PaymentRequestView | None:
    row = conn.execute(
        f"SELECT {_VIEW} FROM payment_requests WHERE provider = %s AND provider_ref = %s",
        (provider, provider_ref),
    ).fetchone()
    return None if row is None else PaymentRequestView.model_validate(row)


def set_request_status(
    conn: Conn,
    request_id: str,
    status: PaymentRequestStatus,
    *,
    payment_id: str | None = None,
) -> PaymentRequestView:
    row = conn.execute(
        "UPDATE payment_requests SET status = %s,"
        " paid_at = CASE WHEN %s = 'paid' THEN clock_timestamp() END,"
        f" payment_id = COALESCE(%s, payment_id) WHERE id = %s RETURNING {_VIEW}",
        (status, status, payment_id, request_id),
    ).fetchone()
    assert row is not None
    return PaymentRequestView.model_validate(row)


def set_request_link(
    conn: Conn,
    request_id: str,
    *,
    provider_ref: str | None,
    hosted_url: str | None,
    link_error: str | None,
) -> PaymentRequestView:
    row = conn.execute(
        "UPDATE payment_requests SET provider_ref = %s, hosted_url = %s, link_error = %s"
        f" WHERE id = %s RETURNING {_VIEW}",
        (provider_ref, hosted_url, link_error, request_id),
    ).fetchone()
    assert row is not None
    return PaymentRequestView.model_validate(row)


def insert_event(conn: Conn, provider: str, event: ProviderEvent) -> int | None:
    """The new row's id, or None when this provider already sent this event."""
    row = conn.execute(
        "INSERT INTO payment_events (provider, event_id, kind) VALUES (%s, %s, %s)"
        " ON CONFLICT (provider, event_id) DO NOTHING RETURNING id",
        (provider, event.event_id, event.kind),
    ).fetchone()
    return None if row is None else int(row["id"])


def finish_event(
    conn: Conn,
    event_row: int,
    *,
    outcome: EventOutcome,
    order_id: str | None = None,
    payment_request_id: str | None = None,
    payment_id: str | None = None,
) -> None:
    conn.execute(
        "UPDATE payment_events SET outcome = %s, order_id = %s, payment_request_id = %s,"
        " payment_id = %s WHERE id = %s",
        (outcome, order_id, payment_request_id, payment_id, event_row),
    )


def default_provider(conn: Conn, offered: Collection[str]) -> str | None:
    """The default provider when it is still ``offered``, its row held ``FOR SHARE`` until
    the caller commits (see ``hold_provider``)."""
    row = conn.execute(
        "SELECT provider FROM payment_credentials WHERE is_default AND provider = ANY(%s)"
        " FOR SHARE",
        (list(offered),),
    ).fetchone()
    return None if row is None else str(row["provider"])


def hold_provider(conn: Conn, provider: str) -> bool:
    """Whether ``provider`` is connected. Its row stays ``FOR SHARE`` until the caller
    commits: a disconnect (``EXCLUSIVE`` on the table) waits, then sees the request the
    caller wrote and refuses (plan gap 11)."""
    row = conn.execute(
        "SELECT 1 FROM payment_credentials WHERE provider = %s FOR SHARE", (provider,)
    ).fetchone()
    return row is not None


def has_open_requests(conn: Conn, provider: str) -> bool:
    """An open request, or a pending one: its settling event still needs the credential."""
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM payment_requests WHERE provider = %s"
        " AND status IN ('open', 'pending'))"
        " AS found",
        (provider,),
    ).fetchone()
    return bool(row and row["found"])
