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

#7983: the pool is shared with the fleet's login rotator. Kind ``claude_login`` holds a whole
Claude Code ``claudeAiOauth`` object, with its account in clear columns; ``scope`` says who may
use a row (``agent``, ``fleet`` or ``both``); a ``claude_login`` row may hold a machine lease.
Core's ``internal/secrets/credentials.go`` serves these routes too, with the same rules.
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

Kind = Literal["anthropic_api_key", "claude_login", "claude_oauth_token", "gateway"]
Status = Literal["active", "cooling_down", "disabled"]
Scope = Literal["agent", "fleet", "both"]

#: The kind whose secret is a whole Claude Code ``claudeAiOauth`` object: the fleet's only.
CLAUDE_LOGIN = "claude_login"
ACCOUNT_FIELD_MAX = 200
EMAIL_MAX = 320
EMAIL_MESSAGE = (
    "email must be one address, at most 320 characters, with no space or control character"
)
WIDENING_MESSAGE = "to share a fleet credential with the agent, enter its secret again"
#: The agent's caller changes the agent's rows only (#7983): a fleet machine installs a
#: claude_login, so the agent's caller never creates, saves, deletes, renames, resets or
#: records an event on a claude_login or a row whose scope is fleet, never names one in a
#: reorder (which leaves their priorities alone), and never saves a row into scope fleet.
FLEET_ROW_MESSAGE = (
    "the fleet's credentials are not changed here: the assistant may not save, delete,"
    " rename, reset, reorder or record an event on a claude_login or a credential whose"
    " scope is fleet"
)
#: The agent's caller uses a row whose scope is both, but the fleet's login rotator reads that row
#: too (#8587). So the agent's caller never gives a row a scope other than agent, and never saves
#: over, deletes or disables a row whose scope is both. It may still use, rename, reset and
#: reorder such a row, and record its other health events.
SHARED_ROW_MESSAGE = (
    "a credential whose scope is both is shared with the fleet: the assistant may use it, but"
    " may not give a credential a scope other than agent, or save over, delete or disable a"
    " credential whose scope is both"
)
LOGIN_MESSAGE = "a claude_login secret must be a claudeAiOauth JSON object"
LOGIN_NEEDS = (
    "a claude_login secret needs accessToken and refreshToken as text and expiresAt as a number"
)
#: The account fields of a ``claudeAiOauth`` object, by their column.
ACCOUNT_FIELDS = {
    "account_uuid": "accountUuid",
    "organization_uuid": "organizationUuid",
    "subscription_type": "subscriptionType",
    "rate_limit_tier": "rateLimitTier",
}

#: The most credentials stored at once (credentials.ts ``MAX_CREDENTIALS``).
MAX_CREDENTIALS = 100
#: The most characters a name has (credentials.ts ``CREDENTIAL_NAME_MAX``).
NAME_MAX = 60
TOO_MANY_MESSAGE = f"at most {MAX_CREDENTIALS} Claude credentials can be stored; delete one first"
STALE_ORDER_MESSAGE = (
    "the order must name every credential this caller may order exactly once (the"
    " assistant's caller: every credential the assistant uses, no fleet row); the list"
    " changed since it was read, so reload it"
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

    def __init__(self, message: str, status: Literal[400, 404, 409, 422, 503]) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Update:
    """A save (credentials.ts ``CredentialUpdate``): ``secret`` None keeps the stored one."""

    kind: Kind
    base_url: str | None = None
    secret: str | None = None
    name: str | None = None
    #: None keeps the stored scope (a new row: agent, or fleet for a claude_login).
    scope: Scope | None = None
    #: A claude_login's email: None keeps the stored one, "" clears it.
    email: str | None = None


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
    " last_error, last_error_at, last_used_at, epoch, scope, account_uuid, organization_uuid,"
    " subscription_type, rate_limit_tier, email, leased_to, leased_at, lease_epoch"
)


def _dict(row: Any, names: list[str]) -> dict[str, Any]:
    return dict(row) if isinstance(row, dict) else dict(zip(names, row, strict=True))


#: The agent's rows only (#7983): never a fleet row. The reveal is the agent's, so every read
#: of sealed bytes takes it, in the SQL: a fleet row reads as no row.
AGENTS_OWN = "scope IN ('agent', 'both') AND kind <> 'claude_login'"
#: What the agent's caller's delete and disable add to ``AGENTS_OWN`` (#8587): never a row whose
#: scope is both, which the fleet's login rotator reads too.
NOT_SHARED = "scope <> 'both'"


def _rows(
    conn: Connection[Any], row_id: str | None = None, *, sealed: bool = False, own: bool = False
) -> list[dict[str, Any]]:
    """Rows as of the database's clock: an expired cooldown reads as ``active``. With
    ``sealed``, the sealed bytes too, of the agent's own rows only; with ``own``, the agent's
    own rows only."""
    columns = _COLUMNS + (", secret_sealed, dek_sealed" if sealed else "")
    clauses = ([AGENTS_OWN] if sealed or own else []) + ([] if row_id is None else ["id = %s"])
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    cur = conn.execute(
        f"SELECT {columns} FROM ai_credentials{where} ORDER BY priority",
        () if row_id is None else (row_id,),
    )
    names = [d.name for d in cur.description or []]
    return [_dict(r, names) for r in cur.fetchall()]


def _first_id(conn: Connection[Any], *, agent: bool) -> str | None:
    """The first row by priority; with ``agent``, the first row the agent uses."""
    rows = _rows(conn, own=agent)
    return rows[0]["id"] if rows else None


def _fleets(row: dict[str, Any]) -> bool:
    """Whether the row is the fleet's (#7983): a claude_login, or scope fleet. Every other row
    is one that ``AGENTS_OWN`` matches."""
    return bool(row["kind"] == CLAUDE_LOGIN or row["scope"] == "fleet")


def _refuse_if_fleets(conn: Connection[Any], row_id: str, *, shared: bool = False) -> None:
    """After a write of the agent's caller that matched no row of ``AGENTS_OWN``: a 409 when
    the row is the fleet's, so the caller tells a fleet row from a row that is gone. With
    ``shared``, the write also left out rows whose scope is both (``NOT_SHARED``), so such a
    row is a 409 too (#8587)."""
    found = _rows(conn, row_id)
    if found and _fleets(found[0]):
        raise CredentialRefusedError(FLEET_ROW_MESSAGE, 409)
    if shared and found and found[0]["scope"] == "both":
        raise CredentialRefusedError(SHARED_ROW_MESSAGE, 409)


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
        "scope": row["scope"],
        "account_uuid": row["account_uuid"],
        "organization_uuid": row["organization_uuid"],
        "subscription_type": row["subscription_type"],
        "rate_limit_tier": row["rate_limit_tier"],
        "email": row["email"],
        "leased_to": row["leased_to"],
        "leased_at": iso(row["leased_at"]),
        "lease_epoch": row["lease_epoch"],
    }


def label(row: dict[str, Any]) -> str:
    """credentials.ts ``credentialLabel``: which credential, never its secret or name."""
    kind = row["kind"]
    what = (
        f"gateway {row['base_url'] or ''}"
        if kind == "gateway"
        else "OAuth token"
        if kind == "claude_oauth_token"
        else "Claude login"
        if kind == CLAUDE_LOGIN
        else "API key"
    )
    tail = f" …{row['last4']}" if row["last4"] else ""
    return f"credential {row['priority'] + 1} ({what}{tail})"


def _lock(conn: Connection[Any]) -> None:
    conn.execute("LOCK TABLE ai_credentials IN SHARE ROW EXCLUSIVE MODE")


def _evict(conn: Connection[Any]) -> None:
    conn.execute("SELECT pg_notify(%s, '*')", (WARM_EVICT_CHANNEL,))


def _scope(update: Update, current: dict[str, Any] | None) -> str:
    """The row's scope after ``update``: the one asked, else the stored one, else agent. A
    claude_login is the fleet's only: its scope is fleet, and any other scope asked is a
    422."""
    if update.kind == CLAUDE_LOGIN:
        if update.scope not in (None, "fleet"):
            raise CredentialRefusedError(
                'kind "claude_login" is the fleet\'s only: its scope must be "fleet"', 422
            )
        return "fleet"
    scope: str = "agent" if current is None else current["scope"]
    if update.scope is not None:
        scope = update.scope
    return scope


def _email(update: Update, current: dict[str, Any] | None) -> str | None:
    """A claude_login's email after ``update``: the one asked ("" clears it), else the
    stored one."""
    if update.kind != CLAUDE_LOGIN:
        if update.email:
            raise CredentialRefusedError('email applies to kind "claude_login" only', 400)
        return None
    if update.email is None:
        if current is not None and current["kind"] == CLAUDE_LOGIN:
            email: str | None = current["email"]
            return email
        return None
    if update.email == "":
        return None
    email = update.email
    if (
        len(email) > EMAIL_MAX
        or "@" not in email
        or any(c.isspace() or unicodedata.category(c) == "Cc" for c in email)
    ):
        raise CredentialRefusedError(EMAIL_MESSAGE, 400)
    return email


def _refuse_constant(name: str) -> Any:
    raise ValueError(name)


def login_account(secret: str) -> tuple[dict[str, str | None], str]:
    """A claude_login's secret read: a ``claudeAiOauth`` object with accessToken and
    refreshToken as text and expiresAt as a number. Answers the account in it, by column,
    and the last four characters of its access token."""
    try:
        found = json.loads(secret, parse_constant=_refuse_constant)
    except ValueError:
        found = None
    if not isinstance(found, dict):
        raise CredentialRefusedError(LOGIN_MESSAGE, 400)
    access, refresh, expires = (
        found.get("accessToken"),
        found.get("refreshToken"),
        found.get("expiresAt"),
    )
    if (
        not isinstance(access, str)
        or not access
        or not isinstance(refresh, str)
        or not refresh
        or isinstance(expires, bool)
        or not isinstance(expires, int | float)
    ):
        raise CredentialRefusedError(LOGIN_NEEDS, 400)
    account: dict[str, str | None] = {}
    for column, field in ACCOUNT_FIELDS.items():
        value = found.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > ACCOUNT_FIELD_MAX):
            raise CredentialRefusedError(
                f"{field} must be text of at most {ACCOUNT_FIELD_MAX} characters", 400
            )
        account[column] = value
    return account, last4(access)


@dataclass(frozen=True)
class _Plan:
    """A save planned: ``envelope`` None keeps the stored secret."""

    base_url: str | None
    envelope: Envelope | None
    last4: str
    scope: str
    email: str | None
    account: dict[str, str | None]

    def changes(self, current: dict[str, Any]) -> bool:
        """Whether a plan that keeps the secret still writes the scope or the email."""
        return bool(self.scope != current["scope"] or self.email != current["email"])


_NO_ACCOUNT: dict[str, str | None] = dict.fromkeys(ACCOUNT_FIELDS)


def _refuse_fleet_rows(update: Update, current: dict[str, Any] | None) -> None:
    """A create or save of the agent's caller (#7983): never a claude_login, never a row whose
    scope is or becomes fleet. A 409, which the agent's store passes on. Its delete, rename,
    reset and events refuse a fleet row with the same 409 (``_refuse_if_fleets``), and so
    does a reorder that names one. #8587: the fleet reads a row whose scope is both too, so
    the agent's caller asks no scope but agent, and never saves over a row whose scope is
    both (its secret, kind, base URL or scope), with the same 409 shape."""
    if update.kind == CLAUDE_LOGIN or update.scope == "fleet":
        raise CredentialRefusedError(FLEET_ROW_MESSAGE, 409)
    if current is not None and (current["kind"] == CLAUDE_LOGIN or current["scope"] == "fleet"):
        raise CredentialRefusedError(FLEET_ROW_MESSAGE, 409)
    if update.scope not in (None, "agent"):
        raise CredentialRefusedError(SHARED_ROW_MESSAGE, 409)
    if current is not None and current["scope"] == "both":
        raise CredentialRefusedError(SHARED_ROW_MESSAGE, 409)


def _plan(
    update: Update, current: dict[str, Any] | None, kek: Kek | None, row_id: str, *, agent: bool
) -> _Plan:
    """credentials.ts ``planPut``: the base URL, the new envelope (None to keep the stored
    secret, only when neither ``kind`` nor ``base_url`` changes), the scope, the email and a
    claude_login's account. ``agent``: the agent's caller saves, so a fleet row is refused."""
    if agent:
        _refuse_fleet_rows(update, current)
    base_url: str | None = None
    if update.kind == "gateway":
        if not update.base_url:
            raise CredentialRefusedError('kind "gateway" needs base_url', 400)
        base_url = check_base_url(update.base_url)
    elif update.base_url:
        raise CredentialRefusedError('base_url applies to kind "gateway" only', 400)
    scope = _scope(update, current)
    email = _email(update, current)

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
        # The agent's reveal opens agent and both rows only. A caller that widens a fleet row
        # (never the agent's: `_refuse_fleet_rows`) must carry its secret, so the agent's reveal
        # never opens a secret the widening caller did not know.
        if current["scope"] == "fleet" and scope != "fleet":
            raise CredentialRefusedError(WIDENING_MESSAGE, 422)
        return _Plan(base_url, None, "", scope, email, _NO_ACCOUNT)

    secret = update.secret.strip()
    if not secret:
        raise CredentialRefusedError("secret is empty", 400)
    if update.kind == CLAUDE_LOGIN:
        # A claudeAiOauth object as Claude Code writes it: its whitespace is JSON's.
        account, tail = login_account(secret)
    else:
        if any(c.isspace() for c in secret):
            raise CredentialRefusedError("secret must not contain whitespace", 400)
        account, tail = _NO_ACCOUNT, last4(secret)
    if kek is None:
        raise CredentialRefusedError(NO_KEK_MESSAGE, 503)
    envelope = seal_secret(kek, secret, credential_aad(row_id, update.kind, base_url))
    return _Plan(base_url, envelope, tail, scope, email, account)


def _one(conn: Connection[Any], row_id: str) -> dict[str, Any]:
    rows = _rows(conn, row_id)
    if not rows:
        raise RuntimeError(f"credential {row_id} vanished inside its own transaction")
    return rows[0]


def list_all(conn: Connection[Any]) -> list[dict[str, Any]]:
    return _rows(conn)


def get(conn: Connection[Any], row_id: str | None, *, agent: bool = True) -> dict[str, Any] | None:
    """One credential; the first by priority when ``row_id`` is None, and with ``agent`` (the
    agent's caller, the default) the first one the agent uses: never a fleet row (#7983)."""
    rows = _rows(conn, row_id, own=agent and row_id is None)
    return rows[0] if rows else None


def _insert(
    conn: Connection[Any], update: Update, kek: Kek | None, *, agent: bool
) -> dict[str, Any]:
    # Under the table lock, so racing creates cannot pass the cap together.
    count = conn.execute("SELECT count(*) AS n FROM ai_credentials").fetchone()
    n = _dict(count, ["n"])["n"] if count is not None else 0
    if n >= MAX_CREDENTIALS:
        raise CredentialRefusedError(TOO_MANY_MESSAGE, 409)
    row_id = str(uuid.uuid4())
    name = check_name(update.name)
    plan = _plan(update, None, kek, row_id, agent=agent)
    envelope = plan.envelope
    if envelope is None:
        raise RuntimeError("a save kept a credential that does not exist")
    conn.execute(
        "INSERT INTO ai_credentials (id, priority, kind, name, base_url, secret_sealed,"
        " dek_sealed, kek_id, last4, scope, account_uuid, organization_uuid,"
        " subscription_type, rate_limit_tier, email) VALUES (%s, (SELECT"
        " COALESCE(MAX(priority) + 1, 0) FROM ai_credentials), %s, %s, %s, %s, %s, %s, %s,"
        " %s, %s, %s, %s, %s, %s)",
        (
            row_id,
            update.kind,
            name,
            plan.base_url,
            envelope.secret_sealed,
            envelope.dek_sealed,
            envelope.kek_id,
            plan.last4,
            plan.scope,
            *plan.account.values(),
            plan.email,
        ),
    )
    _evict(conn)
    return _one(conn, row_id)


def create(
    conn: Connection[Any], update: Update, kek: Kek | None, *, agent: bool = True
) -> dict[str, Any]:
    """A new credential. ``agent`` (the default): the agent's caller creates it, so a
    claude_login or a fleet row is refused (#7983); only the fleet's own caller passes False."""
    if update.secret is None:
        raise CredentialRefusedError("secret is required for a new credential", 400)
    with conn.transaction():
        _lock(conn)
        return _insert(conn, update, kek, agent=agent)


def put(
    conn: Connection[Any],
    update: Update,
    kek: Kek | None,
    row_id: str | None,
    *,
    agent: bool = True,
) -> dict[str, Any]:
    """A save over a credential, the first by priority when ``row_id`` is None (one is
    created when there is none). A new secret makes it ``active`` again. ``agent`` as for
    ``create``: the agent's caller never saves over a claude_login or a fleet row, and its
    save with no ``row_id`` goes to the first row the agent uses."""
    with conn.transaction():
        _lock(conn)
        current = get(conn, row_id, agent=agent)
        if current is None:
            if row_id is not None:
                raise CredentialRefusedError(f"no credential {row_id}", 404)
            return _insert(conn, update, kek, agent=agent)
        plan = _plan(update, current, kek, current["id"], agent=agent)
        envelope = plan.envelope
        if envelope is None:
            if not plan.changes(current):
                return current
            # The scope or the email only: the secret and its epoch stay.
            conn.execute(
                "UPDATE ai_credentials SET scope = %s, email = %s, updated_at = now()"
                " WHERE id = %s",
                (plan.scope, plan.email, current["id"]),
            )
        else:
            # A kind other than claude_login holds no lease: a lease it held ends, and each
            # lease change bumps lease_epoch.
            conn.execute(
                "UPDATE ai_credentials SET kind = %(kind)s, base_url = %(base_url)s,"
                " secret_sealed = %(secret_sealed)s, dek_sealed = %(dek_sealed)s,"
                " kek_id = %(kek_id)s, last4 = %(last4)s, updated_at = now(),"
                " status = 'active', cooldown_until = NULL, last_error = NULL,"
                " last_error_at = NULL, epoch = epoch + 1, scope = %(scope)s,"
                " account_uuid = %(account_uuid)s, organization_uuid = %(organization_uuid)s,"
                " subscription_type = %(subscription_type)s,"
                " rate_limit_tier = %(rate_limit_tier)s, email = %(email)s,"
                " leased_to = CASE WHEN %(kind)s = 'claude_login' THEN leased_to END,"
                " leased_at = CASE WHEN %(kind)s = 'claude_login' THEN leased_at END,"
                " lease_epoch = CASE WHEN %(kind)s <> 'claude_login' AND leased_to IS NOT NULL"
                " THEN lease_epoch + 1 ELSE lease_epoch END"
                " WHERE id = %(id)s",
                {
                    "kind": update.kind,
                    "base_url": plan.base_url,
                    "secret_sealed": envelope.secret_sealed,
                    "dek_sealed": envelope.dek_sealed,
                    "kek_id": envelope.kek_id,
                    "last4": plan.last4,
                    "scope": plan.scope,
                    **plan.account,
                    "email": plan.email,
                    "id": current["id"],
                },
            )
        _evict(conn)
        return _one(conn, current["id"])


def delete(conn: Connection[Any], row_id: str | None, *, agent: bool = True) -> bool:
    """Deletes a credential (the first by priority when ``row_id`` is None); the rest
    close up. ``agent`` (the default): the agent's caller deletes the agent's rows only, a
    fleet row or a row whose scope is both is a 409 (#8587), and with no ``row_id`` it deletes
    the first row the agent uses."""
    with conn.transaction():
        _lock(conn)
        target = row_id if row_id is not None else _first_id(conn, agent=agent)
        if target is None:
            return False
        own = f" AND {AGENTS_OWN} AND {NOT_SHARED}" if agent else ""
        deleted = conn.execute(f"DELETE FROM ai_credentials WHERE id = %s{own}", (target,))
        if deleted.rowcount == 0:
            if agent:
                _refuse_if_fleets(conn, target, shared=True)
            return False
        conn.execute(
            "UPDATE ai_credentials c SET priority = n.rn - 1 FROM (SELECT id,"
            " row_number() OVER (ORDER BY priority) AS rn FROM ai_credentials) n"
            " WHERE c.id = n.id AND c.priority <> n.rn - 1"
        )
        _evict(conn)
        return True


def reorder(conn: Connection[Any], ids: list[str], *, agent: bool = True) -> list[dict[str, Any]]:
    """Puts the credentials in this order; ``ids`` must name every row this caller may order
    exactly once. With
    ``agent`` (the default), the agent's rows only: ``ids`` names every row the agent uses and
    no fleet row (a 409), those rows take the priorities they hold between them in that
    order, and every fleet row keeps its own."""
    with conn.transaction():
        _lock(conn)
        rows = _rows(conn)
        named = set(ids)
        if agent and any(_fleets(r) for r in rows if r["id"] in named):
            raise CredentialRefusedError(FLEET_ROW_MESSAGE, 409)
        moved = [r for r in rows if not (agent and _fleets(r))]
        if len(named) != len(ids) or named != {r["id"] for r in moved}:
            raise CredentialRefusedError(STALE_ORDER_MESSAGE, 409)
        slots = sorted(r["priority"] for r in moved)
        conn.execute(
            "UPDATE ai_credentials c SET priority = v.priority FROM unnest(%s::text[],"
            " %s::int[]) AS v(id, priority) WHERE c.id = v.id",
            (ids, slots),
        )
        _evict(conn)
        return _rows(conn)


def reset(conn: Connection[Any], row_id: str, *, agent: bool = True) -> dict[str, Any] | None:
    """Makes a credential ``active`` again, ending a cooldown or a disable. ``agent`` (the
    default): the agent's rows only, a fleet row is a 409."""
    own = f" AND {AGENTS_OWN}" if agent else ""
    with conn.transaction():
        updated = conn.execute(
            "UPDATE ai_credentials SET status = 'active', cooldown_until = NULL,"
            f" last_error = NULL, last_error_at = NULL, epoch = epoch + 1 WHERE id = %s{own}",
            (row_id,),
        )
        if updated.rowcount == 0:
            if agent:
                _refuse_if_fleets(conn, row_id)
            return None
        _evict(conn)
        return get(conn, row_id)


def rename(
    conn: Connection[Any], row_id: str, name: str | None, *, agent: bool = True
) -> dict[str, Any] | None:
    """Sets or clears the name, and nothing else. No warm eviction: a Claude Code process
    never sees the name. ``agent`` (the default): the agent's rows only, a fleet row is a
    409."""
    own = f" AND {AGENTS_OWN}" if agent else ""
    with conn.transaction():
        updated = conn.execute(
            f"UPDATE ai_credentials SET name = %s WHERE id = %s{own}", (check_name(name), row_id)
        )
        if updated.rowcount == 0:
            if agent:
                _refuse_if_fleets(conn, row_id)
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
    " WHERE id = %(id)s AND epoch = %(epoch)s{own} FOR UPDATE"
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
    conn: Connection[Any], row_id: str, epoch: int, event: Event, *, agent: bool = True
) -> dict[str, Any] | None:
    """Records what a query learned; None when the row is gone or its epoch moved on.
    ``before`` locks the row, so the transition answered is the one written even when
    several pods report at once. ``agent`` (the default): the agent's rows only, a fleet row
    is a 409, so the agent's caller never cools down or disables a fleet login. Its
    ``disabled`` on a row whose scope is both is a 409 too (#8587), so it cannot take that row
    from the fleet. Its other events on such a row are recorded as before."""
    shared = agent and event.kind == "disabled"
    own = ""
    if agent:
        own = f" AND {AGENTS_OWN} AND {NOT_SHARED}" if shared else f" AND {AGENTS_OWN}"
    before = _BEFORE.format(own=own)
    sql = (
        f"WITH b AS ({before}) {_RECORD[event.kind]} FROM b WHERE c.id = b.id"
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
        if row is None and agent:
            _refuse_if_fleets(conn, row_id, shared=shared)
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
    """Every row of the agent's with its sealed bytes (one for ``row_id``), in priority
    order: never a fleet row (#7983)."""
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
