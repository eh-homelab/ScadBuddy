"""The agent's Claude credentials, ``ai_credentials`` (#255, #1093; #5299 D5): sealed and
opened here, under the KEK, and nowhere else once the agent uses these routes.

This is ``agent/src/credentials.ts`` `CredentialStore` in Python, statement for
statement, so a row either side writes reads the same on the other during a rolling
deploy (#5299 D9). The AAD binds a sealed secret to its row and to the columns that say
where it is sent (``credentialAad``: the row id, ``kind`` and ``base_url``). Every
structural write (create, save, delete, reorder) takes the table's own lock (``SHARE ROW
EXCLUSIVE``: readers are not blocked), and a health write locks the row (``FOR UPDATE``)
in the conditional ``UPDATE`` that records it, against the epoch the turn read. Each write
a warm Claude Code depends on tells every agent replica's cache on commit
(``scadbuddy_warm_evict``), as the agent's store did.

The agent normalises a gateway's base URL and a credential's name before it sends them
(``normaliseBaseUrl``, ``normaliseCredentialName``: the URL parser is the agent's, and the
AAD binds the URL as the agent spells it); this module checks the result again.
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from psycopg import Connection

from scadbuddy.agent_data.settings import WARM_EVICT_CHANNEL
from scadbuddy.core.secrets import (
    SEAL_V1,
    Envelope,
    Kek,
    SealError,
    open_secret,
    seal_secret,
)
from scadbuddy.core.secrets import rewrap as rewrap_envelope

Kind = Literal["anthropic_api_key", "claude_oauth_token", "gateway"]
Status = Literal["active", "cooling_down", "disabled"]

#: The most credentials stored at once (credentials.ts ``MAX_CREDENTIALS``).
MAX_CREDENTIALS = 100
#: The most characters a name has (credentials.ts ``CREDENTIAL_NAME_MAX``).
NAME_MAX = 60
TOO_MANY_MESSAGE = f"at most {MAX_CREDENTIALS} Claude credentials can be stored; delete one first"
STALE_ORDER_MESSAGE = (
    "the order must name every stored credential exactly once; the list changed since it"
    " was read, so reload it"
)
LEGACY_FORMAT_MESSAGE = (
    "the stored credential was saved in an older format that did not bind its kind and"
    " base URL; save it again"
)
NO_KEK_MESSAGE = (
    "credentials cannot be saved: no key-encryption key is configured on the API"
    " (SCADBUDDY_SECRET_KEY_FILE, spec §9)"
)


class CredentialRefusedError(Exception):
    """A request the store refuses; ``status`` is what the route answers with."""

    def __init__(self, message: str, status: Literal[400, 404, 409, 503]) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Update:
    """A save (credentials.ts ``CredentialUpdate``): ``secret`` None keeps the stored one."""

    kind: Kind
    base_url: str | None = None
    secret: str | None = None
    name: str | None = None


def credential_aad(row_id: str, kind: str, base_url: str | None) -> str:
    """credentials.ts ``credentialAad``: JSON as ``JSON.stringify`` writes it."""
    bound = json.dumps(
        {"kind": kind, "base_url": base_url}, separators=(",", ":"), ensure_ascii=False
    )
    return f"ai_credentials:{row_id}:{bound}"


def last4(secret: str) -> str:
    """secrets.ts ``last4``: empty for a secret under 12 characters."""
    return "" if len(secret) < 12 else secret[-4:]


def check_name(raw: str | None) -> str | None:
    """The name as stored, already normalised by the agent (`normaliseCredentialName`):
    refused unless it is one trimmed line of plain text, 1 to 60 characters."""
    if raw is None:
        return None
    name = raw.strip()
    if not name:
        return None
    if name != raw:
        raise CredentialRefusedError("name must be trimmed: no leading or trailing space", 400)
    if len(name) > NAME_MAX:
        raise CredentialRefusedError(f"name must be at most {NAME_MAX} characters", 400)
    if any(unicodedata.category(c) in {"Cc", "Cf", "Zl", "Zp"} for c in name):
        raise CredentialRefusedError(
            "name must be one line of plain text, with no control or formatting characters",
            400,
        )
    return name


def check_base_url(raw: str) -> str:
    """A base URL as the agent normalised it: http(s), no credentials, query, fragment or
    trailing slash. Refused otherwise, never rewritten: the AAD binds it as given."""
    try:
        url = urlsplit(raw)
    except ValueError:
        raise CredentialRefusedError("base_url is not a valid URL", 400) from None
    if url.scheme not in ("http", "https") or not url.netloc:
        raise CredentialRefusedError("base_url must be an http(s) URL", 400)
    if "@" in url.netloc:
        raise CredentialRefusedError(
            "base_url must not carry credentials; put the token in secret", 400
        )
    if url.query or url.fragment or "?" in raw or "#" in raw:
        raise CredentialRefusedError("base_url must not have a query or fragment", 400)
    if raw.endswith("/") or raw != raw.strip():
        raise CredentialRefusedError("base_url must be normalised: no trailing slash", 400)
    return raw


_COLUMNS = (
    "id, priority, kind, name, base_url, last4, updated_at, kek_id,"
    " get_byte(secret_sealed, 0) AS seal_version,"
    " CASE WHEN status = 'cooling_down' AND cooldown_until <= now() THEN 'active'"
    " ELSE status END AS status,"
    " CASE WHEN status = 'cooling_down' AND cooldown_until > now() THEN cooldown_until"
    " END AS cooldown_until,"
    " last_error, last_error_at, last_used_at, epoch"
)


def _dict(row: Any, names: list[str]) -> dict[str, Any]:
    return dict(row) if isinstance(row, dict) else dict(zip(names, row, strict=True))


def _rows(
    conn: Connection[Any], row_id: str | None = None, *, sealed: bool = False
) -> list[dict[str, Any]]:
    """Rows as of the database's clock: an expired cooldown reads as ``active``."""
    columns = _COLUMNS + (", secret_sealed, dek_sealed" if sealed else "")
    where = "" if row_id is None else " WHERE id = %s"
    cur = conn.execute(
        f"SELECT {columns} FROM ai_credentials{where} ORDER BY priority",
        () if row_id is None else (row_id,),
    )
    names = [d.name for d in cur.description or []]
    return [_dict(r, names) for r in cur.fetchall()]


def _first_id(conn: Connection[Any]) -> str | None:
    rows = _rows(conn)
    return rows[0]["id"] if rows else None


def stored(row: dict[str, Any]) -> dict[str, Any]:
    """A row as the agent's ``StoredCredential``, on the wire: no sealed bytes."""

    def iso(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat()

    return {
        "id": row["id"],
        "priority": row["priority"],
        "kind": row["kind"],
        "name": row["name"],
        "base_url": row["base_url"],
        "last4": row["last4"],
        "updated_at": iso(row["updated_at"]),
        "status": row["status"],
        "cooldown_until": iso(row["cooldown_until"]),
        "last_error": row["last_error"],
        "last_error_at": iso(row["last_error_at"]),
        "last_used_at": iso(row["last_used_at"]),
        "epoch": row["epoch"],
        "kek_id": row["kek_id"],
        "legacy_format": row["seal_version"] == SEAL_V1,
    }


def label(row: dict[str, Any]) -> str:
    """credentials.ts ``credentialLabel``: which credential, never its secret or name."""
    kind = row["kind"]
    what = (
        f"gateway {row['base_url'] or ''}"
        if kind == "gateway"
        else "OAuth token"
        if kind == "claude_oauth_token"
        else "API key"
    )
    tail = f" …{row['last4']}" if row["last4"] else ""
    return f"credential {row['priority'] + 1} ({what}{tail})"


def _lock(conn: Connection[Any]) -> None:
    conn.execute("LOCK TABLE ai_credentials IN SHARE ROW EXCLUSIVE MODE")


def _evict(conn: Connection[Any]) -> None:
    conn.execute("SELECT pg_notify(%s, '*')", (WARM_EVICT_CHANNEL,))


def _plan(
    update: Update, current: dict[str, Any] | None, kek: Kek | None, row_id: str
) -> tuple[str | None, Envelope, str] | None:
    """credentials.ts ``planPut``: the base URL, the new envelope and last four, or None to
    keep the stored secret (only when neither ``kind`` nor ``base_url`` changes)."""
    base_url: str | None = None
    if update.kind == "gateway":
        if not update.base_url:
            raise CredentialRefusedError('kind "gateway" needs base_url', 400)
        base_url = check_base_url(update.base_url)
    elif update.base_url:
        raise CredentialRefusedError('base_url applies to kind "gateway" only', 400)

    if update.secret is None:
        if current is None:
            raise CredentialRefusedError("no credential is stored yet; secret is required", 400)
        if current["kind"] != update.kind or current["base_url"] != base_url:
            raise CredentialRefusedError(
                "changing kind or base_url needs the secret again: the stored one is not"
                " sent to a new destination",
                409,
            )
        if current["seal_version"] == SEAL_V1:
            raise CredentialRefusedError(LEGACY_FORMAT_MESSAGE, 409)
        return None

    secret = update.secret.strip()
    if not secret:
        raise CredentialRefusedError("secret is empty", 400)
    if any(c.isspace() for c in secret):
        raise CredentialRefusedError("secret must not contain whitespace", 400)
    if kek is None:
        raise CredentialRefusedError(NO_KEK_MESSAGE, 503)
    envelope = seal_secret(kek, secret, credential_aad(row_id, update.kind, base_url))
    return base_url, envelope, last4(secret)


def _one(conn: Connection[Any], row_id: str) -> dict[str, Any]:
    rows = _rows(conn, row_id)
    if not rows:
        raise RuntimeError(f"credential {row_id} vanished inside its own transaction")
    return rows[0]


def list_all(conn: Connection[Any]) -> list[dict[str, Any]]:
    return _rows(conn)


def get(conn: Connection[Any], row_id: str | None) -> dict[str, Any] | None:
    """One credential; the first by priority when ``row_id`` is None."""
    rows = _rows(conn, row_id)
    return rows[0] if rows else None


def _insert(conn: Connection[Any], update: Update, kek: Kek | None) -> dict[str, Any]:
    # Under the table lock, so racing creates cannot pass the cap together.
    count = conn.execute("SELECT count(*) AS n FROM ai_credentials").fetchone()
    n = _dict(count, ["n"])["n"] if count is not None else 0
    if n >= MAX_CREDENTIALS:
        raise CredentialRefusedError(TOO_MANY_MESSAGE, 409)
    row_id = str(uuid.uuid4())
    name = check_name(update.name)
    plan = _plan(update, None, kek, row_id)
    if plan is None:
        raise RuntimeError("a save kept a credential that does not exist")
    base_url, envelope, tail = plan
    conn.execute(
        "INSERT INTO ai_credentials (id, priority, kind, name, base_url, secret_sealed,"
        " dek_sealed, kek_id, last4) VALUES (%s, (SELECT COALESCE(MAX(priority) + 1, 0)"
        " FROM ai_credentials), %s, %s, %s, %s, %s, %s, %s)",
        (
            row_id,
            update.kind,
            name,
            base_url,
            envelope.secret_sealed,
            envelope.dek_sealed,
            envelope.kek_id,
            tail,
        ),
    )
    _evict(conn)
    return _one(conn, row_id)


def create(conn: Connection[Any], update: Update, kek: Kek | None) -> dict[str, Any]:
    if update.secret is None:
        raise CredentialRefusedError("secret is required for a new credential", 400)
    with conn.transaction():
        _lock(conn)
        return _insert(conn, update, kek)


def put(
    conn: Connection[Any], update: Update, kek: Kek | None, row_id: str | None
) -> dict[str, Any]:
    """A save over a credential, the first by priority when ``row_id`` is None (one is
    created when there is none). A new secret makes it ``active`` again."""
    with conn.transaction():
        _lock(conn)
        current = get(conn, row_id)
        if current is None:
            if row_id is not None:
                raise CredentialRefusedError(f"no credential {row_id}", 404)
            return _insert(conn, update, kek)
        plan = _plan(update, current, kek, current["id"])
        if plan is None:
            return current
        base_url, envelope, tail = plan
        conn.execute(
            "UPDATE ai_credentials SET kind = %s, base_url = %s, secret_sealed = %s,"
            " dek_sealed = %s, kek_id = %s, last4 = %s, updated_at = now(),"
            " status = 'active', cooldown_until = NULL, last_error = NULL,"
            " last_error_at = NULL, epoch = epoch + 1 WHERE id = %s",
            (
                update.kind,
                base_url,
                envelope.secret_sealed,
                envelope.dek_sealed,
                envelope.kek_id,
                tail,
                current["id"],
            ),
        )
        _evict(conn)
        return _one(conn, current["id"])


def delete(conn: Connection[Any], row_id: str | None) -> bool:
    """Deletes a credential (the first by priority when ``row_id`` is None); the rest
    close up."""
    with conn.transaction():
        _lock(conn)
        target = row_id if row_id is not None else _first_id(conn)
        if target is None:
            return False
        deleted = conn.execute("DELETE FROM ai_credentials WHERE id = %s", (target,))
        if deleted.rowcount == 0:
            return False
        conn.execute(
            "UPDATE ai_credentials c SET priority = n.rn - 1 FROM (SELECT id,"
            " row_number() OVER (ORDER BY priority) AS rn FROM ai_credentials) n"
            " WHERE c.id = n.id AND c.priority <> n.rn - 1"
        )
        _evict(conn)
        return True


def reorder(conn: Connection[Any], ids: list[str]) -> list[dict[str, Any]]:
    """Puts the credentials in this order; ``ids`` must name every one exactly once."""
    with conn.transaction():
        _lock(conn)
        current = {r["id"] for r in _rows(conn)}
        wanted = set(ids)
        if len(wanted) != len(ids) or wanted != current:
            raise CredentialRefusedError(STALE_ORDER_MESSAGE, 409)
        conn.execute(
            "UPDATE ai_credentials c SET priority = v.ord - 1 FROM unnest(%s::text[])"
            " WITH ORDINALITY AS v(id, ord) WHERE c.id = v.id",
            (ids,),
        )
        _evict(conn)
        return _rows(conn)


def reset(conn: Connection[Any], row_id: str) -> dict[str, Any] | None:
    """Makes a credential ``active`` again, ending a cooldown or a disable."""
    with conn.transaction():
        updated = conn.execute(
            "UPDATE ai_credentials SET status = 'active', cooldown_until = NULL,"
            " last_error = NULL, last_error_at = NULL, epoch = epoch + 1 WHERE id = %s",
            (row_id,),
        )
        if updated.rowcount == 0:
            return None
        _evict(conn)
        return get(conn, row_id)


def rename(conn: Connection[Any], row_id: str, name: str | None) -> dict[str, Any] | None:
    """Sets or clears the name, and nothing else. No warm eviction: a Claude Code process
    never sees the name."""
    with conn.transaction():
        updated = conn.execute(
            "UPDATE ai_credentials SET name = %s WHERE id = %s", (check_name(name), row_id)
        )
        if updated.rowcount == 0:
            return None
        return get(conn, row_id)


@dataclass(frozen=True)
class Event:
    """What a query learned (credentials.ts ``CredentialEvent``)."""

    kind: Literal["used", "disabled", "cooling_down", "transient"]
    reason: str | None = None
    until: datetime | None = None
    replacing: datetime | None = None


_BEFORE = (
    "SELECT id, status AS raw, CASE WHEN status = 'cooling_down' AND cooldown_until <= now()"
    " THEN 'active' ELSE status END AS status FROM ai_credentials"
    " WHERE id = %(id)s AND epoch = %(epoch)s FOR UPDATE"
)
_RECORD = {
    "used": (
        "UPDATE ai_credentials c SET last_used_at = now(),"
        " status = CASE WHEN b.status = 'active' THEN 'active' ELSE c.status END,"
        " cooldown_until = CASE WHEN b.status = 'active' THEN NULL ELSE c.cooldown_until END"
    ),
    "disabled": (
        "UPDATE ai_credentials c SET status = 'disabled', cooldown_until = NULL,"
        " last_error = %(reason)s, last_error_at = now()"
    ),
    "cooling_down": (
        "UPDATE ai_credentials c SET"
        " status = CASE WHEN c.status = 'disabled' THEN 'disabled' ELSE 'cooling_down' END,"
        " cooldown_until = CASE WHEN c.status = 'disabled' THEN NULL"
        " WHEN %(replacing)s::timestamptz IS NOT NULL"
        " AND c.cooldown_until = %(replacing)s::timestamptz THEN %(until)s::timestamptz"
        " ELSE GREATEST(%(until)s::timestamptz,"
        " CASE WHEN b.status = 'cooling_down' THEN c.cooldown_until END) END,"
        " last_error = %(reason)s, last_error_at = now()"
    ),
    "transient": "UPDATE ai_credentials c SET last_error = %(reason)s, last_error_at = now()",
}


def record_event(
    conn: Connection[Any], row_id: str, epoch: int, event: Event
) -> dict[str, Any] | None:
    """Records what a query learned; None when the row is gone or its epoch moved on.
    ``before`` locks the row, so the transition answered is the one written even when
    several pods report at once."""
    sql = (
        f"WITH b AS ({_BEFORE}) {_RECORD[event.kind]} FROM b WHERE c.id = b.id"
        " RETURNING b.status AS before, b.raw AS raw, c.status AS after"
    )
    params = {
        "id": row_id,
        "epoch": epoch,
        "reason": event.reason,
        "until": event.until,
        "replacing": event.replacing,
    }
    with conn.transaction():
        cur = conn.execute(sql, params)
        names = [d.name for d in cur.description or []]
        row = cur.fetchone()
    if row is None:
        return None
    found = _dict(row, names)
    return {
        "before": found["before"],
        "after": found["after"],
        "recovered": found["raw"] == "cooling_down" and found["after"] == "active",
    }


def open_row(kek: Kek, row: dict[str, Any]) -> dict[str, Any]:
    """credentials.ts ``openCredential``: the agent's ``Credential``. SealError on a wrong
    KEK, an altered row, or a v1 row."""
    if row["seal_version"] == SEAL_V1:
        raise SealError(LEGACY_FORMAT_MESSAGE)
    envelope = Envelope(
        secret_sealed=bytes(row["secret_sealed"]),
        dek_sealed=bytes(row["dek_sealed"]),
        kek_id=row["kek_id"],
    )
    secret = open_secret(kek, envelope, credential_aad(row["id"], row["kind"], row["base_url"]))
    if row["kind"] == "gateway":
        if row["base_url"] is None:
            raise SealError("gateway credential has no base_url")
        return {"kind": "gateway", "base_url": row["base_url"], "secret": secret}
    return {"kind": row["kind"], "secret": secret}


def sealed_rows(conn: Connection[Any], row_id: str | None) -> list[dict[str, Any]]:
    """Every row with its sealed bytes (one for ``row_id``), in priority order."""
    return _rows(conn, row_id, sealed=True)


def rewrap(conn: Connection[Any], previous: Kek, current: Kek) -> tuple[int, int]:
    """Key rotation (spec §9, "Rotating it re-wraps the data keys only"): each row whose
    data key is sealed under ``previous`` gets it sealed under ``current``; the sealed
    secret and ``updated_at`` stay. A row ``previous`` cannot open (altered, or v1) is
    left for the operator and counted as failed; each update is conditional on
    ``kek_id``, so two pods rotating at once do not overwrite each other. Answers
    (re-wrapped, failed)."""
    if previous.id == current.id:
        return 0, 0
    cur = conn.execute(
        "SELECT id, kind, base_url, secret_sealed, dek_sealed, kek_id FROM ai_credentials"
        " WHERE kek_id = %s",
        (previous.id,),
    )
    names = [d.name for d in cur.description or []]
    rewrapped = failed = 0
    for found in cur.fetchall():
        row = _dict(found, names)
        envelope = Envelope(
            secret_sealed=bytes(row["secret_sealed"]),
            dek_sealed=bytes(row["dek_sealed"]),
            kek_id=row["kek_id"],
        )
        try:
            if envelope.secret_sealed[:1] == bytes([SEAL_V1]):
                raise SealError(LEGACY_FORMAT_MESSAGE)
            aad = credential_aad(row["id"], row["kind"], row["base_url"])
            resealed = rewrap_envelope(previous, current, envelope, aad)
        except SealError:
            failed += 1
            continue
        updated = conn.execute(
            "UPDATE ai_credentials SET dek_sealed = %s, kek_id = %s WHERE id = %s AND kek_id = %s",
            (resealed.dek_sealed, resealed.kek_id, row["id"], previous.id),
        )
        rewrapped += updated.rowcount
    return rewrapped, failed
