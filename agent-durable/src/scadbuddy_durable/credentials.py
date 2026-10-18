"""The Claude credential, read as the agent reads it (spec 2026-10-01 §6.2).

Ports agent/src/credentials.ts `credentialAad` and `openCredential`, the pool's
"usable, in priority order" (`CredentialPool`, agent/src/harness/fallback.ts),
agent/src/harness/run.ts `credentialEnv`, and the health write a turn reports
(`CredentialRepo.record`, #2320): a segment stamps the key it ran on as used, or
records its rate limit or error, so classic and durable agree on a key's state. Only
the agent writes a secret; this writes the health columns alone, and never disables a
key (that waits on the agent's probe, which a durable segment does not run).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast, get_args

import psycopg

from scadbuddy_durable.secrets import SEAL_V1, Envelope, Kek, SealError, open_secret, sealed_version

log = logging.getLogger(__name__)

CredentialKind = Literal["anthropic_api_key", "claude_oauth_token", "gateway"]

LEGACY_FORMAT_MESSAGE = (
    "the stored credential was saved in an older format that did not bind its kind and"
    " base URL; save it again"
)


@dataclass(frozen=True)
class Credential:
    id: str
    priority: int
    kind: CredentialKind
    base_url: str | None
    secret: str = field(repr=False)
    #: The row's epoch when it was read: a record against it is dropped once the key
    #: was replaced (credentials.ts `record`, `AND epoch = …`).
    epoch: int = 0

    @property
    def label(self) -> str:
        """For logs: which credential, never its secret (credentials.ts `credentialLabel`)."""
        what = {"gateway": f"gateway {self.base_url or ''}", "claude_oauth_token": "OAuth token"}
        return f"credential {self.priority + 1} ({what.get(self.kind, 'API key')})"


def credential_aad(id: str, kind: str, base_url: str | None) -> str:
    # JSON.stringify: keys in this order, no spaces, non-ASCII left as is.
    bound = json.dumps(
        {"kind": kind, "base_url": base_url}, separators=(",", ":"), ensure_ascii=False
    )
    return f"ai_credentials:{id}:{bound}"


def open_credential(
    kek: Kek,
    *,
    id: str,
    priority: int,
    kind: CredentialKind,
    base_url: str | None,
    envelope: Envelope,
    epoch: int = 0,
) -> Credential:
    if kind not in get_args(CredentialKind):
        # Never guess where a secret goes: a kind a later agent migration adds is
        # unusable here until this port knows it.
        raise SealError(f"unknown credential kind {kind!r}")
    if sealed_version(envelope.secret_sealed) == SEAL_V1:
        raise SealError(LEGACY_FORMAT_MESSAGE)
    secret = open_secret(kek, envelope, credential_aad(id, kind, base_url))
    if kind == "gateway" and base_url is None:
        raise SealError("gateway credential has no base_url")
    return Credential(
        id=id, priority=priority, kind=kind, base_url=base_url, secret=secret, epoch=epoch
    )


def credential_env(c: Credential) -> dict[str, str]:
    if c.kind == "anthropic_api_key":
        return {"ANTHROPIC_API_KEY": c.secret}
    if c.kind == "claude_oauth_token":
        return {"CLAUDE_CODE_OAUTH_TOKEN": c.secret}
    if c.kind == "gateway" and c.base_url is not None:
        return {"ANTHROPIC_BASE_URL": c.base_url, "ANTHROPIC_AUTH_TOKEN": c.secret}
    raise ValueError(f"no environment for credential kind {c.kind!r}")


async def usable_credentials(conn: psycopg.AsyncConnection[Any], kek: Kek) -> list[Credential]:
    """Every credential a query may use now, in priority order.

    Usable is the agent's rule: active, or cooling down with its cooldown passed;
    sealed under the mounted key; in the current format; and authenticating against
    its row. A row that fails any of those is skipped and logged by label.
    """
    cur = await conn.execute(
        "SELECT id, priority, kind, base_url, secret_sealed, dek_sealed, kek_id, epoch"
        " FROM ai_credentials"
        " WHERE status = 'active' OR (status = 'cooling_down' AND cooldown_until <= now())"
        " ORDER BY priority"
    )
    usable: list[Credential] = []
    rows = await cur.fetchall()
    for id, priority, kind, base_url, secret_sealed, dek_sealed, kek_id, epoch in rows:
        envelope = Envelope(
            secret_sealed=bytes(secret_sealed), dek_sealed=bytes(dek_sealed), kek_id=kek_id
        )
        if kind not in get_args(CredentialKind):
            log.warning("credential %d has unknown kind %r; skipped", priority + 1, kind)
            continue
        if kek_id != kek.id:
            log.info("credential %d is sealed under another key; skipped", priority + 1)
            continue
        try:
            usable.append(
                open_credential(
                    kek,
                    id=id,
                    priority=priority,
                    kind=cast(CredentialKind, kind),
                    base_url=base_url,
                    envelope=envelope,
                    epoch=epoch,
                )
            )
        except SealError as err:
            log.warning("credential %d cannot be opened (%s); skipped", priority + 1, err)
    return usable


#: credentialErrors.ts DEFAULT_COOLDOWN_MS and MAX_COOLDOWN_MS.
DEFAULT_COOLDOWN = timedelta(seconds=60)
MAX_COOLDOWN = timedelta(hours=24)
#: What `last_error` keeps of a failure's text.
ERROR_MAX = 500

Health = Literal["used", "cooling_down", "error"]


@dataclass(frozen=True)
class Transition:
    """The row's status before and after a record (credentials.ts `CredentialTransition`)."""

    before: str
    after: str
    recovered: bool


def cooldown_until(now: datetime, until: datetime | None = None) -> datetime:
    """credentialErrors.ts `cooldownUntil`: ``until``, or the default, held between one
    second and MAX_COOLDOWN from now."""
    at = until or now + DEFAULT_COOLDOWN
    return min(max(at, now + timedelta(seconds=1)), now + MAX_COOLDOWN)


#: Claude Code's subscription-limit result names its reset, "resets 10:30pm (UTC)" or
#: "resets Oct 21, 4am (UTC)" (#4088, measured in prod `ai_credentials.last_error`).
_RESET = re.compile(
    r"\bresets\s+(?:(?P<month>[a-z]{3})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<half>am|pm)\s*\(UTC\)",
    re.IGNORECASE,
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def rate_limit_reset(text: str, now: datetime) -> datetime | None:
    """The reset a rate-limited result's text names, as the next such time after
    ``now``: a time alone is today's or tomorrow's, a date this year's or next year's.
    None when the text names none, or one that is not a time; the caller then keeps the
    default cooldown. A segment never sees the 429's ``anthropic-ratelimit-*-reset``
    headers a classic probe reads (credentialErrors.ts ``rateLimitResetFromHeaders``)."""
    m = _RESET.search(text)
    if m is None:
        return None
    hour, minute = int(m["hour"]), int(m["minute"] or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if m["half"].lower() == "pm" else 0)
    now = now.astimezone(UTC)
    try:
        if m["month"] is None:
            at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return at if at > now else at + timedelta(days=1)
        month = _MONTHS.index(m["month"].lower()) + 1
        at = datetime(now.year, month, int(m["day"]), hour, minute, tzinfo=UTC)
        return at if at > now else at.replace(year=now.year + 1)
    except ValueError:
        # Not a month, or not a day of it (Feb 30; Feb 29 of next year).
        return None


def redacted(text: str, secret: str) -> str:
    """A failure's text for `last_error` and the audit: never the key, and bounded."""
    if secret:
        text = text.replace(secret, "[redacted]")
    return text[:ERROR_MAX]


_BEFORE = (
    "SELECT id, status AS raw, CASE WHEN status = 'cooling_down'"
    " AND cooldown_until <= now() THEN 'active' ELSE status END AS status"
    " FROM ai_credentials WHERE id = %(id)s AND epoch = %(epoch)s FOR UPDATE"
)
_UPDATES: dict[Health, str] = {
    "used": (
        "UPDATE ai_credentials c SET last_used_at = now(),"
        " status = CASE WHEN b.status = 'active' THEN 'active' ELSE c.status END,"
        " cooldown_until = CASE WHEN b.status = 'active' THEN NULL ELSE c.cooldown_until END"
    ),
    "cooling_down": (
        "UPDATE ai_credentials c SET"
        " status = CASE WHEN c.status = 'disabled' THEN 'disabled' ELSE 'cooling_down' END,"
        " cooldown_until = CASE WHEN c.status = 'disabled' THEN NULL"
        " ELSE GREATEST(%(until)s::timestamptz,"
        " CASE WHEN b.status = 'cooling_down' THEN c.cooldown_until END) END,"
        " last_error = %(reason)s, last_error_at = now()"
    ),
    "error": "UPDATE ai_credentials c SET last_error = %(reason)s, last_error_at = now()",
}


async def record_health(
    conn: psycopg.AsyncConnection[Any],
    c: Credential,
    health: Health,
    *,
    reason: str = "",
    until: datetime | None = None,
) -> Transition | None:
    """credentials.ts `record` for what a segment reports: ``used`` (the endpoint
    answered), ``cooling_down`` (a rate limit, until ``until``) or ``error`` (any other
    failure the key met; ``transient`` there). None when the row is gone or its key was
    replaced since it was read. A disabled key stays disabled."""
    params = {
        "id": c.id,
        "epoch": c.epoch,
        "reason": redacted(reason, c.secret),
        "until": until or cooldown_until(datetime.now(UTC)),
    }
    cur = await conn.execute(
        f"WITH b AS ({_BEFORE}) {_UPDATES[health]} FROM b WHERE c.id = b.id"
        " RETURNING b.status, b.raw, c.status",
        params,
    )
    row = await cur.fetchone()
    if row is None:
        return None
    was, raw, after = row
    return Transition(
        before=was, after=after, recovered=raw == "cooling_down" and after == "active"
    )


async def audit_health(
    conn: psycopg.AsyncConnection[Any],
    c: Credential,
    change: Transition | None,
    *,
    reason: str = "",
    until: datetime | None = None,
    session_id: str | None = None,
) -> None:
    """The ``credential`` audit row CredentialPool.report writes when a key changes
    state: ``recover`` or ``cooldown`` (``disable`` follows the agent's probe, which a
    durable segment never runs)."""
    if change is None:
        return
    if change.recovered:
        action, outcome = "recover", "ok"
        detail = f"{c.label} is usable again: its rate limit has passed"
    elif change.after == "cooling_down" and until is not None:
        action, outcome = "cooldown", "error"
        detail = (
            f"{c.label} is rate limited until {until.isoformat()}: {redacted(reason, c.secret)}"
        )
    else:
        return
    log.warning("credentials: %s: %s", action, detail)
    await conn.execute(
        "INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id,"
        " principal_label, session_id, outcome, detail)"
        " VALUES ('credential', %s, 'harness', 'system', 'scadbuddy', 'ScadBuddy', %s, %s, %s)",
        (action, session_id, outcome, detail[:500]),
    )
