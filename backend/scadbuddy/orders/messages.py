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
outcome or a person's "Mark sent" is written.

The storefront overlap (spec §8): a template the storefront mails itself for this order
(`STOREFRONT_EVENT`, asked of ``storefront_mail_covers`` with the running mail host) is
not sent through the plugin too. Its claim writes a ``skipped`` row, reason
``storefront_mail``, unless the body says ``also_when_storefront_mails``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from psycopg.errors import UniqueViolation
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
from scadbuddy.orders.storefront.mail import storefront_mail_covers
from scadbuddy.orders.storefront.mail_templates import MailEvent

#: The storefront mail event that tells the customer what each template tells (#5283's
#: events: `revision_ready`, `price_ready`, `payment_requested`, `shipped`). A template
#: with none (`accepted`, `ready`) is never skipped.
STOREFRONT_EVENT: Final[dict[NotifyTemplate, MailEvent]] = {
    "quoted": "price_ready",
    "shipped": "shipped",
}
#: A skipped row's reason.
STOREFRONT_MAIL: Final = "storefront_mail"

#: Statuses that mean the customer was, or may have been, told.
_TOLD = ("sent", "sending", "unknown")
_STALE = f"created_at < clock_timestamp() - interval '{STALE_SENDING_MINUTES} minutes'"
_COLUMNS = (
    "id, order_id, template, source, tool, recipients, subject, text,"
    f" CASE WHEN status = 'sending' AND {_STALE} THEN 'unknown' ELSE status END AS status,"
    " reason, provider_message_id, episode, resend_of, by, created_at, finished_at"
)


class OrderMessages:
    """The record over the orders store's pool. ``mail_host`` is the running
    ``storefront_mail_host`` (the lifecycle's), read at each skip check."""

    def __init__(
        self, store: OrderStore, mail_host: Callable[[], str | None] = lambda: None
    ) -> None:
        self.store = store
        self.mail_host = mail_host

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

    def check(
        self, order_id: str, template: NotifyTemplate, key: str | None = None
    ) -> tuple[OrderMessage | None, bool, OrderMessage | None]:
        """What a claim of ``template`` under ``key`` would meet now, read in one
        transaction: the message that already told the customer since the order's last
        status change (``told``), whether the storefront mails this news to the customer
        itself (so a claim without ``also_when_storefront_mails`` would be skipped), and the
        row ``key`` already claimed. A claimed key answers its row and no refusal, as its
        claim would (a retry after a lost answer is served from the first claim). Writes
        nothing."""
        with self.store.transaction() as conn:
            order = self._order(conn, order_id, lock=False)
            if key is not None:
                existing = conn.execute(
                    f"SELECT {_COLUMNS} FROM order_messages WHERE idempotency_key = %s", (key,)
                ).fetchone()
                if existing is not None:
                    claimed = self._same_order(OrderMessage.model_validate(existing), order)
                    return None, False, claimed
            check_submitted(order)
            told = self._told(conn, order_id, template, self._episode(conn, order_id))
            return told, self._skipped_for_storefront(conn, order.id, template), None

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
                return self._same_order(OrderMessage.model_validate(existing), order), False
            check_submitted(order)
            episode = self._episode(conn, order.id)
            if body.resend_of is not None:
                self._resendable(conn, order, body.resend_of, body.template)
            else:
                told = self._told(conn, order.id, body.template, episode)
                if told is not None:
                    raise LifecycleError(
                        "already_notified",
                        f"{order.label}'s customer was already sent '{body.template}' "
                        f"(message {told.id}, {told.status}) since its last status change; "
                        "name it in resend_of to send it again",
                    )
            skip = not body.also_when_storefront_mails and self._skipped_for_storefront(
                conn, order.id, body.template
            )
            try:
                # A savepoint: another order's claim may commit the same key between the
                # read above and this insert (this order's lock does not cover it).
                with conn.transaction():
                    row = conn.execute(
                        "INSERT INTO order_messages (order_id, template, source, tool,"
                        " recipients, subject, text, status, reason, finished_at,"
                        " idempotency_key, episode, resend_of, by)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,"
                        " CASE WHEN %s THEN clock_timestamp() END, %s, %s, %s, %s)"
                        f" RETURNING {_COLUMNS}",
                        (
                            order.id,
                            body.template,
                            body.source,
                            body.tool,
                            Jsonb([r.model_dump() for r in body.recipients]),
                            body.subject,
                            body.text,
                            "skipped" if skip else "sending",
                            STOREFRONT_MAIL if skip else None,
                            skip,
                            key,
                            episode,
                            body.resend_of,
                            Jsonb(by.model_dump(mode="json")),
                        ),
                    ).fetchone()
            except UniqueViolation:
                taken = conn.execute(
                    f"SELECT {_COLUMNS} FROM order_messages WHERE idempotency_key = %s", (key,)
                ).fetchone()
                assert taken is not None
                return self._same_order(OrderMessage.model_validate(taken), order), False
            assert row is not None
            message = OrderMessage.model_validate(row)
            kind = "message.skipped" if skip else "message.sending"
            self._event(
                conn,
                order,
                by,
                kind,
                message,
                resend_of=body.resend_of,
                reason=STOREFRONT_MAIL if skip else None,
            )
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

    def _skipped_for_storefront(self, conn: Conn, order_id: str, template: NotifyTemplate) -> bool:
        """§8: the storefront mails this template's news for the order itself (a mail host
        is set and the order has a live order link). A template with no storefront event
        never asks."""
        event = STOREFRONT_EVENT.get(template)
        return event is not None and storefront_mail_covers(
            conn, order_id, event, host=self.mail_host()
        )

    @staticmethod
    def _told(conn: Conn, order_id: str, template: str, episode: int | None) -> OrderMessage | None:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM order_messages WHERE order_id = %s AND template = %s"
            " AND episode IS NOT DISTINCT FROM %s AND status = ANY(%s)"
            " ORDER BY created_at DESC LIMIT 1",
            (order_id, template, episode, list(_TOLD)),
        ).fetchone()
        return None if row is None else OrderMessage.model_validate(row)

    @staticmethod
    def _same_order(found: OrderMessage, order: Order) -> OrderMessage:
        if found.order_id != order.id:
            raise LifecycleError(
                "idempotency_key_reused",
                "this Idempotency-Key already claimed a message of another order",
            )
        return found

    def _resendable(
        self, conn: Conn, order: Order, resend_of: str, template: NotifyTemplate
    ) -> None:
        """A message sent again is one of the order's, of the same template, and not in
        flight (a stale ``sending`` row reads ``unknown`` and may be sent again by name)."""
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM order_messages WHERE id = %s AND order_id = %s",
            (resend_of, order.id),
        ).fetchone()
        if row is None:
            raise LifecycleError(
                "resend_of", f"no message {resend_of} of {order.label}", unprocessable=True
            )
        if row["template"] != template:
            raise LifecycleError(
                "resend_of",
                f"message {resend_of} is '{row['template']}', not '{template}'",
                unprocessable=True,
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
