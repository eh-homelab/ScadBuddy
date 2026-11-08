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
_VIEW = (
    f"{_PUBLIC}, provider_ref, link_error, payment_id, created_by, close_pending_at,"
    " close_abandoned_at"
)

#: #5700: the first wait before the sweep closes a marked page. It gives the close after
#: the commit time to finish; each failed try doubles the wait, up to a day.
CLOSE_RETRY_FIRST_MINUTES = 5
#: The most marked pages one sweep tries.
CLOSE_RETRY_BATCH = 8
#: A page is given up (`close_abandoned_at`) on the first try, at or after this many
#: failed tries (the close after the commit counts), that finds its provider not
#: connected. With the backoff the tenth try comes about 66.5 hours (2.8 days) after the cancel
#: (10 + 20 + ... + 1280 minutes, then a day): long enough for a shop to reconnect, and
#: a reconnect starts the tries over (`resume_closes`).
CLOSE_GIVE_UP_ATTEMPTS = 10


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
    """#5700: the one place that marks a provider's page to close (``close_pending_at``).
    A request with a page that is cancelled, or paid by a payment that is not its own
    provider's (by hand, or a payment on another request that cleared the balance), is
    marked. Its own provider's payment clears the mark: the page was paid. A new mark, or
    a cleared one, starts the close over: no tries and no give-up (#5728 review)."""
    row = conn.execute(
        "UPDATE payment_requests r SET status = %(status)s,"
        " paid_at = CASE WHEN %(status)s = 'paid' THEN clock_timestamp() END,"
        " payment_id = COALESCE(%(payment)s, r.payment_id),"
        " close_pending_at = CASE WHEN m.mark THEN clock_timestamp()"
        "  WHEN %(status)s = 'paid' THEN NULL ELSE r.close_pending_at END,"
        " close_abandoned_at = CASE WHEN m.mark OR %(status)s = 'paid' THEN NULL"
        "  ELSE r.close_abandoned_at END,"
        " close_attempts = CASE WHEN m.mark OR %(status)s = 'paid' THEN 0"
        "  ELSE r.close_attempts END,"
        " close_tried_at = CASE WHEN m.mark OR %(status)s = 'paid' THEN NULL"
        "  ELSE r.close_tried_at END"
        " FROM (SELECT q.provider_ref IS NOT NULL AND (%(status)s = 'cancelled' OR"
        "   (%(status)s = 'paid' AND EXISTS (SELECT 1 FROM order_payments p"
        "    WHERE p.id = %(payment)s"
        "    AND (p.provider IS NULL OR p.payment_request_id IS DISTINCT FROM q.id))))"
        "  AS mark FROM payment_requests q WHERE q.id = %(id)s) m"
        f" WHERE r.id = %(id)s RETURNING {_VIEW}",
        {"status": status, "payment": payment_id, "id": request_id},
    ).fetchone()
    assert row is not None
    return PaymentRequestView.model_validate(row)


def closes_due(conn: Conn, limit: int = CLOSE_RETRY_BATCH) -> list[PaymentRequestView]:
    """Claim the marked pages whose wait is over: `CLOSE_RETRY_FIRST_MINUTES` after the
    mark or the last try, doubled for each failed try, at most a day; never a page given
    up. Oldest mark first. The sweep runs from both Schedules, so the rows are taken with
    ``FOR UPDATE SKIP LOCKED`` and their ``close_tried_at`` is stamped as they are taken:
    an overlapping run finds them not due, and a page is never tried twice at once.
    A sweep that dies after the claim and before `close_failed` leaves the stamp but no
    counted try, so the page waits one full backoff at its current count. That is
    intended: the claim is what keeps a second sweep off the page."""
    rows = conn.execute(
        "UPDATE payment_requests SET close_tried_at = clock_timestamp()"
        " WHERE id IN (SELECT id FROM payment_requests WHERE close_pending_at IS NOT NULL"
        "  AND close_abandoned_at IS NULL"
        "  AND COALESCE(close_tried_at, close_pending_at) <= clock_timestamp() - LEAST("
        "   %s * interval '1 minute' * power(2, LEAST(close_attempts, 16)),"
        "   interval '1 day')"
        "  ORDER BY close_pending_at, id LIMIT %s FOR UPDATE SKIP LOCKED)"
        f" RETURNING {_VIEW}",
        (CLOSE_RETRY_FIRST_MINUTES, limit),
    ).fetchall()
    found = [PaymentRequestView.model_validate(r) for r in rows]
    return sorted(found, key=lambda r: (r.close_pending_at or r.created_at, r.id))


def close_confirmed(conn: Conn, request_id: str, provider_ref: str) -> None:
    """The provider closed the page ``provider_ref``, or says it was closed already. A
    request that no longer holds that reference keeps its mark."""
    conn.execute(
        "UPDATE payment_requests SET close_pending_at = NULL, close_abandoned_at = NULL"
        " WHERE id = %s AND provider_ref = %s",
        (request_id, provider_ref),
    )


def close_failed(
    conn: Conn, request_id: str, provider_ref: str, *, provider_gone: bool = False
) -> None:
    """One more failed try of the marked page ``provider_ref``; a request with no mark,
    or with another reference, is left as it is. ``provider_gone``: the provider is not
    connected; the first such try at or after the `CLOSE_GIVE_UP_ATTEMPTS`th (this try
    counted) gives the page up (`close_abandoned_at`)."""
    conn.execute(
        "UPDATE payment_requests SET close_attempts = close_attempts + 1,"
        " close_tried_at = clock_timestamp(),"
        " close_abandoned_at = CASE WHEN %s AND close_attempts + 1 >= %s"
        "  THEN clock_timestamp() ELSE close_abandoned_at END"
        " WHERE id = %s AND provider_ref = %s AND close_pending_at IS NOT NULL",
        (provider_gone, CLOSE_GIVE_UP_ATTEMPTS, request_id, provider_ref),
    )


def resume_closes(conn: Conn, provider: str) -> None:
    """#5728 review: the shop connected ``provider`` again. Its marked pages, given up or
    not, start their tries over, so the sweep closes them from the first wait."""
    conn.execute(
        "UPDATE payment_requests SET close_abandoned_at = NULL, close_attempts = 0,"
        " close_tried_at = NULL WHERE provider = %s AND close_pending_at IS NOT NULL",
        (provider,),
    )


def set_request_link(
    conn: Conn,
    request_id: str,
    *,
    provider_ref: str | None,
    hosted_url: str | None,
    link_error: str | None,
    close_pending: bool = False,
) -> PaymentRequestView:
    """``close_pending`` (#5700): the page was made after the request closed, so it is
    kept only as a page to close, marked as `set_request_status` marks one."""
    row = conn.execute(
        "UPDATE payment_requests SET provider_ref = %s, hosted_url = %s, link_error = %s,"
        " close_pending_at = CASE WHEN %s THEN clock_timestamp() ELSE close_pending_at END"
        f" WHERE id = %s RETURNING {_VIEW}",
        (provider_ref, hosted_url, link_error, close_pending, request_id),
    ).fetchone()
    assert row is not None
    return PaymentRequestView.model_validate(row)


def insert_event(conn: Conn, provider: str, event: ProviderEvent) -> int | None:
    """The new row's id, or None when this provider already sent this event."""
    row = conn.execute(
        "INSERT INTO payment_events (provider, event_id, kind, reverses)"
        " VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (provider, event_id) DO NOTHING RETURNING id",
        (provider, event.event_id, event.kind, event.reverses),
    ).fetchone()
    return None if row is None else int(row["id"])


def reversed_by_failure(conn: Conn, provider: str, event_id: str) -> bool:
    """Whether this provider already told the failure of the refund event ``event_id``."""
    row = conn.execute(
        "SELECT 1 FROM payment_events WHERE provider = %s AND reverses = %s LIMIT 1",
        (provider, event_id),
    ).fetchone()
    return row is not None


def told_event(conn: Conn, provider: str, event_id: str) -> tuple[bool, str | None]:
    """Whether this provider's event was told, and the payment it recorded, if any."""
    row = conn.execute(
        "SELECT outcome, payment_id FROM payment_events WHERE provider = %s AND event_id = %s",
        (provider, event_id),
    ).fetchone()
    if row is None:
        return False, None
    return True, row["payment_id"] if row["outcome"] == "recorded" else None


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
