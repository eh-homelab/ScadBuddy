"""An order's customer messages (#5660, spec 2026-11-08-order-notify-customer-design.md §7):
the record of each message the agent sends, or tries to send, through a messaging plugin.
The agent calls the plugin; this module only keeps the record, under the order's row lock,
each write with its ``order_events`` row.

A send claims a ``sending`` row under its ``Idempotency-Key`` before the plugin is called,
so a retry of one call never writes a second row. One template is told once per status
change of the order (``episode``, the order's newest ``order_status_changes`` row): a
second claim is ``already_notified`` unless it names the message it sends again
(``resend_of``). A ``sending`` row older than ``STALE_SENDING_MINUTES`` reads as
``unknown`` and is never sent again by itself; it stays ``sending`` in storage until its
outcome or a person's "Mark sent" is written."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from scadbuddy.orders.lifecycle import LifecycleError, check_submitted
from scadbuddy.orders.message_models import (
    STALE_SENDING_MINUTES,
    MessageClaim,
    MessageOutcome,
    NotifyTemplate,
    OrderMessage,
)
from scadbuddy.orders.models import Actor, Order
from scadbuddy.orders.store import Conn, OrderNotFoundError, OrderStore

#: Statuses that mean the customer was, or may have been, told.
_TOLD = ("sent", "sending", "unknown")
_STALE = f"created_at < clock_timestamp() - interval '{STALE_SENDING_MINUTES} minutes'"
_COLUMNS = (
    "id, order_id, template, source, tool, recipients, subject, text,"
    f" CASE WHEN status = 'sending' AND {_STALE} THEN 'unknown' ELSE status END AS status,"
    " reason, provider_message_id, episode, resend_of, by, created_at, finished_at"
)


class OrderMessages:
    """The record over the orders store's pool."""

    def __init__(self, store: OrderStore) -> None:
        self.store = store

    # -- reads -------------------------------------------------------------------------

    def list(self, order_id: str) -> list[OrderMessage]:
        """The order's messages, newest first."""
        with self.store.transaction() as conn:
            self._order(conn, order_id, lock=False)
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM order_messages WHERE order_id = %s"
                " ORDER BY created_at DESC, id DESC",
                (order_id,),
            ).fetchall()
        return [OrderMessage.model_validate(row) for row in rows]

    def told(self, order_id: str, template: NotifyTemplate) -> OrderMessage | None:
        """The message that already told the customer ``template`` since the order's last
        status change, if any: what a claim without ``resend_of`` is refused for. Writes
        nothing."""
        with self.store.transaction() as conn:
            order = self._order(conn, order_id, lock=False)
            check_submitted(order)
            return self._told(conn, order_id, template, self._episode(conn, order_id))

    # -- writes ------------------------------------------------------------------------

    def claim(
        self, order_id: str, key: str, body: MessageClaim, by: Actor
    ) -> tuple[OrderMessage, bool]:
        """The ``sending`` row for this send, and whether this call wrote it. A key seen
        before answers its row as it is, and writes nothing."""
        with self.store.transaction() as conn:
            order = self._order(conn, order_id, lock=True)
            existing = conn.execute(
                f"SELECT {_COLUMNS} FROM order_messages WHERE idempotency_key = %s", (key,)
            ).fetchone()
            if existing is not None:
                found = OrderMessage.model_validate(existing)
                if found.order_id != order.id:
                    raise LifecycleError(
                        "idempotency_key_reused",
                        "this Idempotency-Key already claimed a message of another order",
                    )
                return found, False
            check_submitted(order)
            episode = self._episode(conn, order.id)
            if body.resend_of is not None:
                self._resendable(conn, order, body.resend_of)
            else:
                told = self._told(conn, order.id, body.template, episode)
                if told is not None:
                    raise LifecycleError(
                        "already_notified",
                        f"{order.label}'s customer was already sent '{body.template}' "
                        f"(message {told.id}, {told.status}) since its last status change; "
                        "name it in resend_of to send it again",
                    )
            row = conn.execute(
                "INSERT INTO order_messages (order_id, template, source, tool, recipients,"
                " subject, text, status, idempotency_key, episode, resend_of, by)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, 'sending', %s, %s, %s, %s)"
                f" RETURNING {_COLUMNS}",
                (
                    order.id,
                    body.template,
                    body.source,
                    body.tool,
                    Jsonb([r.model_dump() for r in body.recipients]),
                    body.subject,
                    body.text,
                    key,
                    episode,
                    body.resend_of,
                    Jsonb(by.model_dump(mode="json")),
                ),
            ).fetchone()
            assert row is not None
            message = OrderMessage.model_validate(row)
            self._event(conn, order, by, "message.sending", message, resend_of=body.resend_of)
        return message, True

    def outcome(
        self, order_id: str, message_id: str, body: MessageOutcome, by: Actor
    ) -> OrderMessage:
        """How the plugin's call ended. Taken while the row is ``sending`` (stale or not)
        or ``unknown``, so a slow plugin's late answer is still recorded; a final row
        (``sent``, ``failed``, ``skipped``) refuses it."""
        with self.store.transaction() as conn:
            order = self._order(conn, order_id, lock=True)
            stored = self._stored_status(conn, order.id, message_id)
            if stored not in ("sending", "unknown"):
                raise LifecycleError("message_final", f"message {message_id} is already {stored}")
            message = self._finish(
                conn, message_id, body.status, body.reason, body.provider_message_id
            )
            self._event(
                conn,
                order,
                by,
                f"message.{body.status}",
                message,
                reason=body.reason,
            )
        return message

    def mark_sent(self, order_id: str, message_id: str, by: Actor) -> OrderMessage:
        """A person's word that an ``unknown`` message (or a stale ``sending`` one) did
        reach the customer."""
        with self.store.transaction() as conn:
            order = self._order(conn, order_id, lock=True)
            current = self._message(conn, order.id, message_id)
            if current.status != "unknown":
                raise LifecycleError(
                    "message_not_unknown",
                    f"message {message_id} is {current.status}; only an unknown one is marked sent",
                )
            message = self._finish(conn, message_id, "sent", None, current.provider_message_id)
            self._event(conn, order, by, "message.marked_sent", message)
        return message

    # -- helpers -----------------------------------------------------------------------

    def _order(self, conn: Conn, order_id: str, *, lock: bool) -> Order:
        if lock:
            try:
                return self.store.lock_order(conn, order_id)
            except OrderNotFoundError:
                pass
        else:
            found = self.store.order(order_id, conn=conn)
            if found is not None:
                return found
        raise LifecycleError("order_not_found", f"no order {order_id}", not_found=True)

    @staticmethod
    def _episode(conn: Conn, order_id: str) -> int | None:
        row = conn.execute(
            "SELECT max(id) AS id FROM order_status_changes"
            " WHERE order_id = %s AND item_id IS NULL",
            (order_id,),
        ).fetchone()
        return None if row is None else row["id"]

    @staticmethod
    def _told(conn: Conn, order_id: str, template: str, episode: int | None) -> OrderMessage | None:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM order_messages WHERE order_id = %s AND template = %s"
            " AND episode IS NOT DISTINCT FROM %s AND status = ANY(%s)"
            " ORDER BY created_at DESC LIMIT 1",
            (order_id, template, episode, list(_TOLD)),
        ).fetchone()
        return None if row is None else OrderMessage.model_validate(row)

    def _resendable(self, conn: Conn, order: Order, resend_of: str) -> None:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM order_messages WHERE id = %s AND order_id = %s",
            (resend_of, order.id),
        ).fetchone()
        if row is None:
            raise LifecycleError(
                "resend_of", f"no message {resend_of} of {order.label}", unprocessable=True
            )
        if row["status"] == "sending":
            raise LifecycleError(
                "message_in_flight",
                f"message {resend_of} is still being sent; wait for its outcome",
            )

    @staticmethod
    def _message(conn: Conn, order_id: str, message_id: str) -> OrderMessage:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM order_messages WHERE id = %s AND order_id = %s FOR UPDATE",
            (message_id, order_id),
        ).fetchone()
        if row is None:
            raise LifecycleError(
                "message_not_found", f"no message {message_id} of this order", not_found=True
            )
        return OrderMessage.model_validate(row)

    @staticmethod
    def _stored_status(conn: Conn, order_id: str, message_id: str) -> str:
        row = conn.execute(
            "SELECT status FROM order_messages WHERE id = %s AND order_id = %s FOR UPDATE",
            (message_id, order_id),
        ).fetchone()
        if row is None:
            raise LifecycleError(
                "message_not_found", f"no message {message_id} of this order", not_found=True
            )
        status: str = row["status"]
        return status

    @staticmethod
    def _finish(
        conn: Conn,
        message_id: str,
        status: str,
        reason: str | None,
        provider_message_id: str | None,
    ) -> OrderMessage:
        row = conn.execute(
            "UPDATE order_messages SET status = %s, reason = %s, provider_message_id = %s,"
            f" finished_at = clock_timestamp() WHERE id = %s RETURNING {_COLUMNS}",
            (status, reason, provider_message_id, message_id),
        ).fetchone()
        assert row is not None
        return OrderMessage.model_validate(row)

    def _event(
        self,
        conn: Conn,
        order: Order,
        by: Actor,
        kind: str,
        message: OrderMessage,
        **extra: Any,
    ) -> None:
        detail: dict[str, Any] = {
            "message_id": message.id,
            "template": message.template,
            "source": message.source,
            "status": message.status,
        }
        detail.update({k: v for k, v in extra.items() if v is not None})
        self.store.insert_event(conn, order.id, by, kind, detail)
