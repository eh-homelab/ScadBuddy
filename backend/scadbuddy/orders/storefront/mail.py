"""The storefront's mail (#5283, spec 2026-11-02 §5.7), sent only by the CRUD service.

A change that mails the customer writes a `storefront_mail` row in its own transaction
(`queue_mail`), so a rollback leaves none. After the commit the lifecycle kicks the
sender (`StorefrontMail.kick`), which runs `send_queued` in a thread; the housekeeping
sweep `housekeeping_send_storefront_mail` sends what is left.

A pass claims one row at a time with a short transaction that leases it (`LEASE`,
``FOR UPDATE SKIP LOCKED``), commits, sends with no connection held, then stamps the
result in another short transaction: another sender skips a leased row, and no
transaction is open over SMTP. One pass runs at a time in a process. A pass asked for
while one runs is run by the holder after it, and a `send_queued` call, with those passes,
stops claiming at `PASS_BUDGET` from its start.

Failures (review I2):
- of the server or its setup (anything the SMTP session raises that is not the
  recipient's: connect, TLS and its certificate, login and a password smtplib cannot
  encode, sender refused, a timeout; and a password saved for another connection, no
  password or no key): not charged to a row. The row is let go, the process waits
  (`SERVER_BACKOFF_FIRST`, doubling to `SERVER_BACKOFF_MAX`) and the error is kept in
  `storefront_mail_server` for the shop. Only a server failure the server answered to
  the row's own mail (an ``SMTPResponseException`` from the send, not the sender
  refused) counts on the row (``server_failures``): at `SERVER_FAILURES_MAX` in a row
  the row is charged an attempt, so a crafted address or message cannot hold the head
  of the queue for good;
- of the recipient or this message (``SMTPRecipientsRefused``, or an ``SMTPDataError``
  `_recipients_fault` reads as about them): charged to the row, which waits 1, 2, 4 ...
  minutes (at most an hour) and is given up after `MAX_ATTEMPTS`;
- fixed (a message that cannot be built: a template, a header, a revoked link): the row
  fails at once.

One setting, ``storefront_mail_tls``, picks the connection: ``starttls`` (the server
must offer STARTTLS; without it that is a server failure, never a fall back to plain),
``implicit`` (``SMTP_SSL``), each with a verifying context, or ``none``, a plain
connection, allowed only with no user (a LAN relay with no login). A login is never sent
in plain text. The password is sealed under the KEK in `storefront_mail_secret` and bound
to the host, port, user and TLS mode it was saved for (stored beside it and in the seal's
AAD); the sender logs in only while the live settings name the same four.

A sign-in code is good for `CODE_TTL`: a `sign_in_code` row not sent within it of
``created_at`` is given up as ``expired`` and its code and sign-in link cleared, before
every pass sends (`send_queued`, so the sweep too), and the claim never takes a late code.

Every order mail carries the order link: its row names the link (``link_id``), and the
sender opens the link's sealed token to build ``{storefront_public_url}/o#{token}``. With
no host or no sender nothing is sent: the `order_link` and `sign_in_code` rows stay
queued, and the four order events (`ORDER_EVENTS`) queue nothing
(`storefront_mail_covers`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import smtplib
import ssl
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Literal

from scadbuddy.core.secrets import Envelope, Kek, SealError, open_secret, seal_secret
from scadbuddy.orders.store import MAIL_QUEUED, Conn, OrderStore
from scadbuddy.orders.storefront.addresses import EmailInvalidError, check_address
from scadbuddy.orders.storefront.links import live_link, open_token, sealed_token
from scadbuddy.orders.storefront.mail_templates import ORDER_EVENTS, MailEvent, render
from scadbuddy.payments.credentials import KekMissingError

__all__ = [
    "MAIL_SETTINGS",
    "MAX_ATTEMPTS",
    "ORDER_EVENTS",
    "SMTP_TIMEOUT",
    "EmailInvalidError",
    "MailConfig",
    "MailConnectionUnsetError",
    "MailEvent",
    "MailStatus",
    "StorefrontMail",
    "check_address",
    "queue_mail",
    "storefront_mail_covers",
]

logger = logging.getLogger(__name__)

#: A row is charged at most this many recipient refusals, then given up.
MAX_ATTEMPTS = 8
#: Each SMTP socket operation's timeout, in seconds.
SMTP_TIMEOUT = 20.0
#: How long a claimed row is left to its sender.
LEASE = "2 minutes"
# How long a sign-in code may wait in the outbox before it is given up as expired.
CODE_TTL = "10 minutes"
#: The process's wait after a server failure: a minute, doubling to an hour.
SERVER_BACKOFF_FIRST = 60.0
SERVER_BACKOFF_MAX = 3600.0
#: Server failures in a row answered to one row's mail before that row is charged.
SERVER_FAILURES_MAX = 3
#: How long one pass may run, below the sweep's `STOREFRONT_MAIL_TIMEOUT` (10 min).
PASS_BUDGET = 480.0
#: The longest ``last_error`` kept.
_ERROR_MAX = 500
#: What a sent or given-up row no longer needs: a secret only the send read.
_SECRETS = ["code", "link"]

#: The env-seeded settings the sender reads (`core/settings.py`).
MAIL_SETTINGS = (
    "storefront_mail_host",
    "storefront_mail_port",
    "storefront_mail_user",
    "storefront_mail_sender",
    "storefront_mail_tls",
)

MailTls = Literal["starttls", "implicit", "none"]


@dataclass(frozen=True)
class MailConfig:
    host: str | None
    port: int
    user: str | None
    sender: str | None
    tls: MailTls = "starttls"

    @property
    def ready(self) -> bool:
        """Whether a mail can be sent: a host and a sender."""
        return bool(self.host) and bool(self.sender)


@dataclass(frozen=True)
class MailStatus:
    """What the shop sees: whether a password is set (and for which connection), and the
    last failure of the server or its setup. Never the password."""

    password_set: bool
    password_for: str | None
    server_error: str | None
    server_error_at: datetime | None


class MailConnectionUnsetError(Exception):
    """A password is bound to a host and a user: set them first."""

    def __init__(self) -> None:
        super().__init__("set the SMTP host and user before the password")


class _ServerFailureError(Exception):
    """A failure of the server or the setup: never charged to a row."""


class _FixedFailureError(Exception):
    """A failure no retry mends: the row fails at once."""


def secret_aad(row_id: str, host: str, port: int, user: str, tls: MailTls) -> str:
    return f"storefront_mail_secret:{row_id}:{host}:{port}:{user}:{tls}"


def queue_mail(
    conn: Conn, event: MailEvent, to: str, context: dict[str, Any], order_id: str | None
) -> str:
    """A `storefront_mail` row in the caller's transaction; its id. ``to`` must be one
    address (`EmailInvalidError` otherwise). An order mail's ``context`` names its link
    (``link_id``), never the token. Sent after the commit; gone with a rollback."""
    to = check_address(to)
    row = conn.execute(
        "INSERT INTO storefront_mail (order_id, to_address, template, context)"
        " VALUES (%s, %s, %s, %s::jsonb) RETURNING id",
        (order_id, to, event, json.dumps(context)),
    ).fetchone()
    assert row is not None
    MAIL_QUEUED.set(True)
    return str(row["id"])


def storefront_mail_covers(
    conn: Conn, order_id: str, event: MailEvent, *, host: str | None
) -> bool:
    """Whether ScadBuddy mails ``event`` for the order itself: a mail host is set
    (``host``, the running ``storefront_mail_host``) and the order has a live order link.
    When false the event leaves no row, and stays #5660's plugin's job."""
    del event  # Every event is covered alike today; the plugin asks per event.
    return bool(host) and live_link(conn, order_id) is not None


def _text(error: BaseException) -> str:
    # The error's class and text: never the password, which no smtplib error carries.
    return f"{type(error).__name__}: {error}"[:_ERROR_MAX]


#: An RFC 3463 enhanced status code in a reply: ``5.<subject>.<detail>``.
_ENHANCED = re.compile(r"\b5\.(\d{1,3})\.(\d{1,3})\b")
#: Words in a reply that point at the sender or the setup.
_SETUP_WORDS = re.compile(r"sender|\bfrom\b|verif|authori[sz]|authenticat|relay")
#: What may stand on each side of an address named in a reply.
_BEFORE = r"(?<![^\s<>:,;'\"()\[\]])"
_AFTER = r"(?=$|[\s<>:,;'\"()\[\]]|\.(?:$|\s))"


def _address(address: str) -> re.Pattern[str]:
    """``address`` as a whole address in a lower-cased reply, never inside another."""
    return re.compile(_BEFORE + re.escape(address.lower()) + _AFTER)


def _recipients_fault(error: Exception, sender: str | None, to: str) -> bool:
    """Whether the server refused this mail's recipient or content for good, so the row
    is charged; anything else is a server failure.

    ``SMTPRecipientsRefused`` (the answer to RCPT TO) is the recipient's. An
    ``SMTPDataError`` (the answer to DATA, which smtplib reaches only after RCPT TO was
    accepted) is the row's only when it is a 5xx about the recipient or the message. The
    reply is read case-insensitively, in this order:
    - a reply that names the recipient ``to`` as a whole address is the row's. The
      recipient is the customer's to choose, so nothing in it may make a server failure;
    - a reply that names the sender as a whole address (bounded by the reply's ends,
      whitespace, ``<>:,;'"()[]``, or a closing full stop) is the setup's: an unverified
      sender. An address that only contains the sender is not it;
    - with an RFC 3463 code ``5.S.D`` (S not 0): ``5.1.D`` (addressing) is the row's
      except 5.1.7 and 5.1.8 (the sender's address); ``5.2.D`` (the mailbox), 5.3.4 (too
      big) and ``5.6.D`` (the content) are the row's; ``5.7.D`` (security and policy) is
      the row's unless the reply has a setup word (sender, from, verif, authoris,
      authenticat, relay); the rest (5.3 the mail system, 5.4 routing, 5.5 the protocol)
      are the setup's;
    - with no such code: a reply with a setup word is the setup's; 550 to 554 (RFC 5321's
      mailbox and transaction refusals) are the row's, any other 5xx the setup's.
    The setup words are looked for only in the reply with the recipient's address taken
    out. ``SMTPSenderRefused`` (the answer to MAIL FROM) is always the setup's."""
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return True
    if not isinstance(error, smtplib.SMTPDataError) or error.smtp_code < 500:
        return False
    reply = error.smtp_error
    if isinstance(reply, bytes):
        reply = reply.decode(errors="replace")
    reply = reply.lower()
    if _address(to).search(reply):
        return True
    if sender and _address(sender).search(reply):
        return False
    setup = _SETUP_WORDS.search(reply.replace(to.lower(), " ")) is not None
    code = _ENHANCED.search(reply)
    if code is not None and int(code[1]) != 0:
        subject, detail = int(code[1]), int(code[2])
        if subject == 1:
            return detail not in (7, 8)
        if subject == 7:
            return not setup
        return subject in (2, 6) or (subject, detail) == (3, 4)
    if setup:
        return False
    return 550 <= error.smtp_code <= 554


class _AnsweredError(Exception):
    """The server's refusal of this row's own mail, which counts on the row."""

    def __init__(self, error: smtplib.SMTPResponseException) -> None:
        super().__init__(str(error))
        self.error = error


@dataclass(frozen=True)
class _Claimed:
    id: str
    to: str
    template: MailEvent
    context: dict[str, Any]
    link_id: str | None
    envelope: Envelope | None


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
        smtp_ssl: Callable[..., Any] = smtplib.SMTP_SSL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._kek = kek
        self.config = config
        self._public_url = public_url
        self._smtp = smtp
        self._smtp_ssl = smtp_ssl
        self._clock = clock
        #: One pass at a time in this process; a pass asked for meanwhile runs after it.
        self._pass = threading.Lock()
        self._rerun = False
        #: The process's server backoff.
        self._retry_at = 0.0
        self._backoff = SERVER_BACKOFF_FIRST
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running: asyncio.Task[None] | None = None
        self._again = False

    # -- the password -------------------------------------------------------------------

    @property
    def can_seal(self) -> bool:
        return self._kek is not None

    def status(self) -> MailStatus:
        with self._store.transaction() as conn:
            secret = conn.execute(
                "SELECT host, port, username, tls FROM storefront_mail_secret"
            ).fetchone()
            server = conn.execute(
                "SELECT last_error, last_error_at FROM storefront_mail_server"
            ).fetchone()
        return MailStatus(
            password_set=secret is not None,
            password_for=None
            if secret is None
            else f"{secret['username']}@{secret['host']}:{secret['port']} ({secret['tls']})",
            server_error=None if server is None else server["last_error"],
            server_error_at=None if server is None else server["last_error_at"],
        )

    def set_password(self, password: str) -> None:
        """Seal ``password`` for the live host, port, user and TLS mode (R2): it is sent to
        no other connection."""
        if self._kek is None:
            raise KekMissingError()
        config = self.config()
        if not config.host or not config.user:
            raise MailConnectionUnsetError()
        with self._store.transaction() as conn:
            conn.execute("LOCK TABLE storefront_mail_secret IN EXCLUSIVE MODE")
            found = conn.execute("SELECT id FROM storefront_mail_secret").fetchone()
            row_id = str(found["id"]) if found else str(uuid.uuid4())
            aad = secret_aad(row_id, config.host, config.port, config.user, config.tls)
            sealed = seal_secret(self._kek, password, aad)
            conn.execute(
                "INSERT INTO storefront_mail_secret (id, host, port, username, tls,"
                " secret_sealed, dek_sealed, kek_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (id) DO UPDATE SET host = excluded.host, port = excluded.port,"
                " username = excluded.username, tls = excluded.tls,"
                " secret_sealed = excluded.secret_sealed,"
                " dek_sealed = excluded.dek_sealed, kek_id = excluded.kek_id,"
                " saved_at = clock_timestamp()",
                (
                    row_id,
                    config.host,
                    config.port,
                    config.user,
                    config.tls,
                    sealed.secret_sealed,
                    sealed.dek_sealed,
                    sealed.kek_id,
                ),
            )

    def clear_password(self) -> bool:
        """False when none was set."""
        with self._store.transaction() as conn:
            gone = conn.execute("DELETE FROM storefront_mail_secret RETURNING id").fetchone()
        return gone is not None

    def _login_password(self, config: MailConfig) -> str | None:
        """The password to log in with, None without a user. A `_ServerFailureError` when no
        login may be sent: no TLS, no password, one saved for another connection, or one
        that will not open."""
        if not config.user:
            return None
        if config.tls == "none":
            raise _ServerFailureError("a login needs TLS: use STARTTLS or implicit TLS")
        assert config.host is not None
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT id, host, port, username, tls, secret_sealed, dek_sealed, kek_id"
                " FROM storefront_mail_secret"
            ).fetchone()
        if row is None:
            raise _ServerFailureError("no SMTP password is set")
        if (row["host"], row["port"], row["username"], row["tls"]) != (
            config.host,
            config.port,
            config.user,
            config.tls,
        ):
            raise _ServerFailureError(
                "the SMTP password was saved for another host, port, user or TLS mode;"
                " save it again"
            )
        if self._kek is None:
            raise _ServerFailureError("the SMTP password needs SCADBUDDY_SECRET_KEY_FILE")
        envelope = Envelope(
            secret_sealed=bytes(row["secret_sealed"]),
            dek_sealed=bytes(row["dek_sealed"]),
            kek_id=row["kek_id"],
        )
        aad = secret_aad(str(row["id"]), config.host, config.port, config.user, config.tls)
        try:
            return open_secret(self._kek, envelope, aad)
        except SealError:
            raise _ServerFailureError("the stored SMTP password cannot be opened") from None

    # -- the send -----------------------------------------------------------------------

    def send_queued(self, limit: int = 50, *, budget: float = PASS_BUDGET) -> int:
        """Send up to ``limit`` due rows a pass, oldest first; how many were sent. A pass
        already running in this process takes this one on after it. The call, with every
        pass it takes on, stops claiming ``budget`` seconds after it started; a pass still
        asked for then is left to the next call. Late sign-in codes expire first,
        whatever the settings. With no host or sender, or in the server's backoff,
        nothing is sent."""
        deadline = self._clock() + budget
        sent = 0
        first = True
        while True:
            if not self._pass.acquire(blocking=False):
                self._rerun = True
                return sent
            try:
                if first:
                    first = False
                    self._expire_codes()
                    sent += self._send(limit, deadline)
                while self._rerun and self._clock() <= deadline:
                    self._rerun = False
                    sent += self._send(limit, deadline)
            finally:
                self._pass.release()
            # A pass asked for after the last look and before the release found the lock
            # held and left: take it on, while the budget lasts.
            if not self._rerun or self._clock() > deadline:
                return sent

    def _send(self, limit: int, deadline: float) -> int:
        config = self.config()
        if not config.ready:
            return 0
        if self._clock() < self._retry_at:
            return 0
        public_url = self._public_url()
        try:
            password = self._login_password(config)
        except _ServerFailureError as error:
            self._server_failed(str(error))
            return 0
        sent = 0
        for _ in range(limit):
            if self._clock() > deadline:
                break
            claimed = self._claim(links=public_url is not None)
            if claimed is None:
                break
            try:
                message = self._message(config, claimed, public_url)
            except _FixedFailureError as error:
                self._failed_now(claimed.id, str(error))
                continue
            except _ServerFailureError as error:
                self._release(claimed.id)
                self._server_failed(str(error))
                break
            except ValueError as error:
                # A message that cannot be built (a template, a header): no retry mends it.
                self._failed_now(claimed.id, _text(error))
                continue
            try:
                self._deliver(config, password, message)
            except Exception as caught:
                # Never a fixed failure: a certificate error and a password smtplib cannot
                # encode are ValueErrors too, and are the setup's.
                answered = isinstance(caught, _AnsweredError)
                refusal = caught.error if isinstance(caught, _AnsweredError) else caught
                if _recipients_fault(refusal, config.sender, claimed.to):
                    self._refused(claimed.id, _text(refusal))
                    continue
                if answered:
                    self._answered_failure(claimed.id, _text(refusal))
                else:
                    self._release(claimed.id)
                self._server_failed(_text(refusal))
                break
            self._sent(claimed.id)
            sent += 1
        if sent:
            self._server_ok()
        return sent

    def _claim(self, *, links: bool) -> _Claimed | None:
        """Lease the oldest due row and read its link's sealed token, in one short
        transaction. Order mails wait while ``links`` is false (no public URL)."""
        with self._store.transaction() as conn:
            row = conn.execute(
                "UPDATE storefront_mail SET lease_until = clock_timestamp() + %s::interval"
                " WHERE id = (SELECT id FROM storefront_mail WHERE sent_at IS NULL"
                " AND failed_at IS NULL AND next_attempt_at <= clock_timestamp()"
                " AND (lease_until IS NULL OR lease_until < clock_timestamp())"
                " AND (template = 'sign_in_code' OR %s)"
                " AND (template <> 'sign_in_code'"
                " OR created_at >= clock_timestamp() - %s::interval)"
                " ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED)"
                " RETURNING id, to_address, template, context",
                (LEASE, links, CODE_TTL),
            ).fetchone()
            if row is None:
                return None
            context = dict(row["context"])
            link_id = context.get("link_id")
            envelope = None if link_id is None else sealed_token(conn, str(link_id))
        return _Claimed(
            id=str(row["id"]),
            to=row["to_address"],
            template=row["template"],
            context=context,
            link_id=None if link_id is None else str(link_id),
            envelope=envelope,
        )

    def _link(self, claimed: _Claimed, public_url: str | None) -> str | None:
        if claimed.link_id is None:
            return None
        if claimed.envelope is None:
            raise _FixedFailureError("link_revoked")
        if self._kek is None:
            raise _ServerFailureError("the order link needs SCADBUDDY_SECRET_KEY_FILE")
        try:
            token = open_token(self._kek, claimed.link_id, claimed.envelope)
        except SealError:
            raise _ServerFailureError("an order link's token cannot be opened") from None
        assert public_url is not None
        return f"{public_url.rstrip('/')}/o#{token}"

    def _message(
        self, config: MailConfig, claimed: _Claimed, public_url: str | None
    ) -> EmailMessage:
        rendered = render(claimed.template, claimed.context, self._link(claimed, public_url))
        message = EmailMessage()
        message["Subject"] = rendered.subject
        message["From"] = config.sender
        message["To"] = claimed.to
        message.set_content(rendered.body)
        return message

    def _deliver(self, config: MailConfig, password: str | None, message: EmailMessage) -> None:
        if config.tls == "implicit":
            session = self._smtp_ssl(
                config.host,
                config.port,
                timeout=SMTP_TIMEOUT,
                context=ssl.create_default_context(),
            )
        else:
            session = self._smtp(config.host, config.port, timeout=SMTP_TIMEOUT)
        with session as smtp:
            if config.tls == "starttls":
                # Raises SMTPNotSupportedError when the server offers no STARTTLS: a
                # server failure, never a fall back to plain.
                smtp.starttls(context=ssl.create_default_context())
            if config.user:
                # `_login_password` refused a login without TLS before connecting.
                assert config.tls != "none" and password is not None
                smtp.login(config.user, password)
            try:
                smtp.send_message(message)
            except smtplib.SMTPSenderRefused:
                raise
            except smtplib.SMTPResponseException as error:
                raise _AnsweredError(error) from error

    # -- the stamps -----------------------------------------------------------------------

    def _expire_codes(self) -> None:
        """Give up every sign-in code not sent within `CODE_TTL` and clear its code and
        link. A row a live sender holds is left to it."""
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE storefront_mail SET failed_at = clock_timestamp(), lease_until = NULL,"
                " last_error = 'expired', context = context - %s::text[]"
                " WHERE template = 'sign_in_code' AND sent_at IS NULL AND failed_at IS NULL"
                " AND created_at < clock_timestamp() - %s::interval"
                " AND (lease_until IS NULL OR lease_until < clock_timestamp())",
                (_SECRETS, CODE_TTL),
            )

    def _sent(self, row_id: str) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE storefront_mail SET sent_at = clock_timestamp(), lease_until = NULL,"
                " last_error = NULL, server_failures = 0, context = context - %s::text[]"
                " WHERE id = %s",
                (_SECRETS, row_id),
            )

    def _release(self, row_id: str) -> None:
        with self._store.transaction() as conn:
            conn.execute("UPDATE storefront_mail SET lease_until = NULL WHERE id = %s", (row_id,))

    def _failed_now(self, row_id: str, text: str) -> None:
        logger.warning("storefront mail given up", extra={"mail_id": row_id, "error": text})
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE storefront_mail SET failed_at = clock_timestamp(), lease_until = NULL,"
                " last_error = %s, context = context - %s::text[] WHERE id = %s",
                (text, _SECRETS, row_id),
            )

    def _answered_failure(self, row_id: str, text: str) -> None:
        """Count a server failure answered to the row's mail; at `SERVER_FAILURES_MAX` in
        a row, charge the row an attempt instead of letting it go."""
        with self._store.transaction() as conn:
            row = conn.execute(
                "UPDATE storefront_mail SET server_failures = server_failures + 1,"
                " lease_until = NULL WHERE id = %s RETURNING server_failures",
                (row_id,),
            ).fetchone()
            if row is not None and row["server_failures"] >= SERVER_FAILURES_MAX:
                self._charge(conn, row_id, text)

    def _refused(self, row_id: str, text: str) -> None:
        with self._store.transaction() as conn:
            self._charge(conn, row_id, text)

    def _charge(self, conn: Conn, row_id: str, text: str) -> None:
        logger.warning("storefront mail refused", extra={"mail_id": row_id, "error": text})
        conn.execute(
            "UPDATE storefront_mail SET attempts = attempts + 1, last_error = %s,"
            " server_failures = 0,"
            " lease_until = NULL, next_attempt_at = clock_timestamp()"
            " + least(interval '1 minute' * power(2, attempts), interval '1 hour'),"
            " failed_at = CASE WHEN attempts + 1 >= %s THEN clock_timestamp() END,"
            " context = CASE WHEN attempts + 1 >= %s THEN context - %s::text[]"
            " ELSE context END WHERE id = %s",
            (text, MAX_ATTEMPTS, MAX_ATTEMPTS, _SECRETS, row_id),
        )

    def _server_failed(self, text: str) -> None:
        logger.warning("storefront mail server failed", extra={"error": text})
        self._retry_at = self._clock() + self._backoff
        self._backoff = min(self._backoff * 2, SERVER_BACKOFF_MAX)
        with self._store.transaction() as conn:
            conn.execute(
                "INSERT INTO storefront_mail_server (singleton, last_error, last_error_at)"
                " VALUES (true, %s, clock_timestamp()) ON CONFLICT (singleton) DO UPDATE"
                " SET last_error = excluded.last_error, last_error_at = excluded.last_error_at",
                (text[:_ERROR_MAX],),
            )

    def _server_ok(self) -> None:
        self._retry_at = 0.0
        self._backoff = SERVER_BACKOFF_FIRST
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE storefront_mail_server SET last_error = NULL, last_error_at = NULL"
                " WHERE last_error IS NOT NULL"
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
