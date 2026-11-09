"""The storefront's mail (#5283, spec 2026-11-02 §5.7), sent only by the CRUD service.

A change that mails the customer writes a `storefront_mail` row in its own transaction
(`queue_mail`), so a rollback leaves none. After the commit the lifecycle kicks the
sender (`StorefrontMail.kick`), which runs `send_queued` in a thread; the housekeeping
sweep `housekeeping_send_storefront_mail` sends what is left. Each row is claimed with
``FOR UPDATE SKIP LOCKED``, so the kick and the sweep never send one twice, and is sent
over ``smtplib`` (STARTTLS when ``storefront_mail_starttls``) with a 20 s timeout. A
failure is kept as ``last_error`` and tried again, at most `MAX_ATTEMPTS` times. A token
or a code in a row's context is dropped once the row is sent or given up.

The SMTP settings are the env-seeded ``storefront_mail_*`` settings, read on each send;
the password is sealed under the KEK in `storefront_mail_secret` (as
``payments/credentials.py`` seals a provider's secret) and never read back. With no host
or no sender nothing is sent: the `order_link` and `sign_in_code` rows stay queued, and
the four order events (`ORDER_EVENTS`) queue nothing (`storefront_mail_covers`).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import smtplib
import ssl
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from scadbuddy.core.secrets import Envelope, Kek, SealError, open_secret, seal_secret
from scadbuddy.orders.store import Conn, OrderStore
from scadbuddy.orders.storefront.mail_templates import (
    ORDER_EVENTS,
    MailEvent,
    render,
)
from scadbuddy.payments.credentials import CredentialUnreadableError, KekMissingError

__all__ = [
    "MAIL_SETTINGS",
    "MAX_ATTEMPTS",
    "ORDER_EVENTS",
    "SMTP_TIMEOUT",
    "MailConfig",
    "MailEvent",
    "StorefrontMail",
    "mail_recipient",
    "queue_mail",
    "storefront_mail_covers",
]

logger = logging.getLogger(__name__)

#: A row is tried at most this many times, then left with its last error.
MAX_ATTEMPTS = 8
#: Each SMTP connection's timeout, in seconds.
SMTP_TIMEOUT = 20.0
#: The longest ``last_error`` kept.
_ERROR_MAX = 500
#: What a sent or given-up row no longer needs: a secret only the send read.
_SECRETS = ("token", "code")
#: Failures of one row's own (its recipient, its sender, its message): the rest of the
#: batch is still sent. Any other failure (the server, the network, the login) stops it.
_ROW_FAILURES: tuple[type[Exception], ...] = (
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPDataError,
    ValueError,
)

#: The env-seeded settings the sender reads (`core/settings.py`).
MAIL_SETTINGS = (
    "storefront_mail_host",
    "storefront_mail_port",
    "storefront_mail_user",
    "storefront_mail_sender",
    "storefront_mail_starttls",
)

#: Set by `queue_mail`; the lifecycle clears it as a transition takes its order's lock and
#: reads it in its after-commit tell: whether the transition queued mail, so the commit
#: kicks the sender. A context variable, so each request's thread or task has its own.
QUEUED: contextvars.ContextVar[bool] = contextvars.ContextVar("storefront_mail_queued")


def secret_aad(row_id: str) -> str:
    return f"storefront_mail_secret:{row_id}"


@dataclass(frozen=True)
class MailConfig:
    host: str | None
    port: int
    user: str | None
    sender: str | None
    starttls: bool

    @property
    def ready(self) -> bool:
        """Whether a mail can be sent: a host and a sender."""
        return bool(self.host) and bool(self.sender)


def queue_mail(
    conn: Conn, event: MailEvent, to: str, context: dict[str, Any], order_id: str | None
) -> str:
    """A `storefront_mail` row in the caller's transaction; its id. Sent after the
    commit; gone with a rollback."""
    row = conn.execute(
        "INSERT INTO storefront_mail (order_id, to_address, template, context)"
        " VALUES (%s, %s, %s, %s::jsonb) RETURNING id",
        (order_id, to, event, json.dumps(context)),
    ).fetchone()
    assert row is not None
    QUEUED.set(True)
    return str(row["id"])


def mail_recipient(conn: Conn, order_id: str) -> str | None:
    """Where the order's mail goes: the address its newest live link was sent to."""
    row = conn.execute(
        "SELECT sent_to FROM storefront_order_links WHERE order_id = %s AND revoked_at IS NULL"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (order_id,),
    ).fetchone()
    return None if row is None else str(row["sent_to"])


def storefront_mail_covers(
    conn: Conn, order_id: str, event: MailEvent, *, host: str | None
) -> bool:
    """Whether ScadBuddy mails ``event`` for the order itself: a mail host is set
    (``host``, the running ``storefront_mail_host``) and the order has a live order link.
    When false the event leaves no row, and stays #5660's plugin's job."""
    del event  # Every event is covered alike today; the plugin asks per event.
    return bool(host) and mail_recipient(conn, order_id) is not None


class StorefrontMail:
    """The outbox's sender and the sealed SMTP password."""

    def __init__(
        self,
        store: OrderStore,
        kek: Kek | None,
        config: Callable[[], MailConfig],
        public_url: Callable[[], str | None],
        *,
        smtp: Callable[..., Any] = smtplib.SMTP,
    ) -> None:
        self._store = store
        self._kek = kek
        self.config = config
        self._public_url = public_url
        self._smtp = smtp
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running: asyncio.Task[None] | None = None
        self._again = False

    # -- the password -------------------------------------------------------------------

    @property
    def can_seal(self) -> bool:
        return self._kek is not None

    def has_password(self) -> bool:
        with self._store.transaction() as conn:
            row = conn.execute("SELECT 1 FROM storefront_mail_secret").fetchone()
        return row is not None

    def set_password(self, password: str) -> None:
        if self._kek is None:
            raise KekMissingError()
        with self._store.transaction() as conn:
            conn.execute("LOCK TABLE storefront_mail_secret IN EXCLUSIVE MODE")
            found = conn.execute("SELECT id FROM storefront_mail_secret").fetchone()
            row_id = str(found["id"]) if found else str(uuid.uuid4())
            sealed = seal_secret(self._kek, password, secret_aad(row_id))
            conn.execute(
                "INSERT INTO storefront_mail_secret (id, secret_sealed, dek_sealed, kek_id)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET secret_sealed ="
                " excluded.secret_sealed, dek_sealed = excluded.dek_sealed, kek_id ="
                " excluded.kek_id, saved_at = clock_timestamp()",
                (row_id, sealed.secret_sealed, sealed.dek_sealed, sealed.kek_id),
            )

    def clear_password(self) -> bool:
        """False when none was set."""
        with self._store.transaction() as conn:
            gone = conn.execute("DELETE FROM storefront_mail_secret RETURNING id").fetchone()
        return gone is not None

    def _password(self) -> str | None:
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT id, secret_sealed, dek_sealed, kek_id FROM storefront_mail_secret"
            ).fetchone()
        if row is None:
            return None
        if self._kek is None:
            raise KekMissingError()
        envelope = Envelope(
            secret_sealed=bytes(row["secret_sealed"]),
            dek_sealed=bytes(row["dek_sealed"]),
            kek_id=row["kek_id"],
        )
        try:
            return open_secret(self._kek, envelope, secret_aad(str(row["id"])))
        except SealError:
            logger.warning("the stored storefront mail password cannot be opened")
            raise CredentialUnreadableError("storefront mail") from None

    # -- the send -----------------------------------------------------------------------

    def send_queued(self, limit: int = 50) -> int:
        """Send up to ``limit`` queued rows, oldest first; how many were sent. With no
        host or sender nothing is sent; an order link waits for the public URL."""
        config = self.config()
        if not config.ready:
            return 0
        public_url = self._public_url()
        password: str | None = None
        if config.user:
            try:
                password = self._password()
            except (KekMissingError, CredentialUnreadableError):
                logger.warning("storefront mail not sent: the SMTP password cannot be read")
                return 0
        sent = 0
        tried: list[str] = []
        for _ in range(limit):
            with self._store.transaction() as conn:
                row = conn.execute(
                    "SELECT id, to_address, template, context FROM storefront_mail"
                    " WHERE sent_at IS NULL AND attempts < %s AND NOT (id = ANY(%s))"
                    " AND (template <> 'order_link' OR %s)"
                    " ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED",
                    (MAX_ATTEMPTS, tried, public_url is not None),
                ).fetchone()
                if row is None:
                    break
                tried.append(str(row["id"]))
                try:
                    self._deliver(config, password, self._message(config, row, public_url))
                except Exception as error:
                    self._failed(conn, str(row["id"]), error)
                    if isinstance(error, _ROW_FAILURES):
                        continue
                    break
                conn.execute(
                    "UPDATE storefront_mail SET sent_at = clock_timestamp(), attempts ="
                    " attempts + 1, last_error = NULL, context = context - %s::text[]"
                    " WHERE id = %s",
                    (list(_SECRETS), row["id"]),
                )
                sent += 1
        return sent

    def _message(self, config: MailConfig, row: Any, public_url: str | None) -> EmailMessage:
        rendered = render(row["template"], row["context"], public_url)
        message = EmailMessage()
        message["Subject"] = rendered.subject
        message["From"] = config.sender
        message["To"] = row["to_address"]
        message.set_content(rendered.body)
        return message

    def _deliver(self, config: MailConfig, password: str | None, message: EmailMessage) -> None:
        with self._smtp(config.host, config.port, timeout=SMTP_TIMEOUT) as smtp:
            if config.starttls:
                smtp.starttls(context=ssl.create_default_context())
            if config.user:
                smtp.login(config.user, password or "")
            smtp.send_message(message)

    def _failed(self, conn: Conn, row_id: str, error: Exception) -> None:
        # The error's class and text: never the password, which no smtplib error carries.
        text = f"{type(error).__name__}: {error}"[:_ERROR_MAX]
        logger.warning("storefront mail not sent", extra={"mail_id": row_id, "error": text})
        conn.execute(
            "UPDATE storefront_mail SET attempts = attempts + 1, last_error = %s,"
            " context = CASE WHEN attempts + 1 >= %s THEN context - %s::text[] ELSE context END"
            " WHERE id = %s",
            (text, MAX_ATTEMPTS, list(_SECRETS), row_id),
        )

    # -- the after-commit kick ----------------------------------------------------------

    def bind(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """The loop a kick runs the sender on (the API's), or None to stop kicking."""
        self._loop = loop

    def kick(self) -> None:
        """Send what is queued, soon, off the caller's thread. Safe from any thread; a
        kick while a send runs runs one more after it. Without a loop it does nothing:
        the housekeeping sweep sends."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._start)
        except RuntimeError:
            # The loop closed between the look and the call: the sweep sends.
            return

    def _start(self) -> None:
        if self._running is not None and not self._running.done():
            self._again = True
            return
        self._running = asyncio.ensure_future(self._drain())

    async def _drain(self) -> None:
        while True:
            self._again = False
            try:
                await asyncio.to_thread(self.send_queued)
            except Exception:
                logger.exception("could not send the queued storefront mail")
            if not self._again:
                return

    async def stop(self) -> None:
        self._loop = None
        running, self._running = self._running, None
        if running is not None and not running.done():
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await running
