"""The agent's remote plugins and their sign-ins behind its CRUD routes (#5299 step 5, area
``plugins``): under ``/api/v1/internal/crud/agent/``, for the ``agent`` credential only
(`crud_router`).

The plugin registry (``agent/src/plugins/registry.ts`` ``PluginStore``) and the OAuth
sign-ins (``plugins/oauthStore.ts`` ``PluginOAuth``) hold secrets sealed under the KEK:
a plugin's header value (``ai_plugins``), a sign-in's tokens (``ai_plugin_oauth``) and a
sign-in under way (``ai_plugin_oauth_flows``). Here the API seals and opens them with its
KEK under the agent's own AADs (`agent_data/plugins.py`), so the agent needs no KEK for
them, and a row either side writes opens on the other (#5299 D5, D9). Each route is one
of the store's methods, a transaction as one transaction (D2), with the warm process
cache's eviction (``scadbuddy_warm_evict``) in it where the agent's SQL sent one.

The agent still checks every value before it calls (the name, the URL's form, the header
name, the secret, the tool tiers) and every egress rule; it still talks to the plugin and
to the authorization server itself. The plaintext reaches the agent only from a route
that is for it: a plugin opened for a test or a turn (``…/reveal``, ``…/open-enabled``), a
sign-in's tokens for a request (``…/grants/{plugin}/open``), a flow's state and verifier
for its redirect and its callback, and every stored token for the redaction list
(``…/stored-secrets``).

A token refresh (``…/grants/{plugin}/refreshed``) is stored only while the caller holds
the lease ``plugin-oauth:<plugin>`` (`agent_data/leases.py`, ``…/agent/leases/{key}``),
which replaces the row lock the agent held across the token endpoint's answer (#5299 D3).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import Path, status
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.agent_data import access as acc
from scadbuddy.agent_data import leases
from scadbuddy.agent_data.plugins import envelope_of, flow_aad, grant_aad, js_json, plugin_aad
from scadbuddy.agent_data.rotation import kek_at
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.core.secrets import Kek, SealError, open_secret, seal_secret
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

#: The routes' reasons: no KEK on the API; a refusal the registry makes (the agent's
#: ``PluginError``, its status kept); the plugin gone under a sign-in; a refresh whose
#: lease another holder took.
KEK_MISSING = "kek-missing"
PLUGIN_REFUSED = "plugin-refused"
PLUGIN_GONE = "plugin-gone"
LEASE_LOST = "lease-lost"
#: ``registry.ts`` ``PLUGIN_NAME_RE``, ``MAX_TOOL_ENTRIES``, ``DEFAULT_AUTH_HEADER``.
NAME_PATTERN = r"^[a-z][a-z0-9-]{0,30}[a-z0-9]$"
MAX_TOOL_ENTRIES = 256
DEFAULT_AUTH_HEADER = "Authorization"
NO_KEK_TO_OPEN = "no key-encryption key is configured to open the plugin secret"
URL_MAX = 2048
TEXT_MAX = 2048
TOKEN_MAX = 16384
HASH = r"^[0-9a-f]{64}$"

Name = Annotated[str, Field(pattern=NAME_PATTERN)]
PathName = Annotated[str, Path(pattern=NAME_PATTERN)]
Url = Annotated[str, Field(min_length=1, max_length=URL_MAX)]
Text = Annotated[str, Field(max_length=TEXT_MAX)]
ToolName = Annotated[str, Field(min_length=1, max_length=128)]
ToolAccess = Literal["off", "read", "write", "outward"]
Tier = Literal["read", "write", "outward"]
By = Annotated[str, Field(min_length=1, max_length=600)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _kek(state: Any) -> Kek | None:
    return kek_at(state.settings.secret_key_file)


def _need_kek(state: Any) -> Kek:
    kek = _kek(state)
    if kek is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The API has no key-encryption key (SCADBUDDY_SECRET_KEY_FILE).",
            reason=KEK_MISSING,
        )
    return kek


def _refused(code: int, detail: str) -> ApiError:
    return ApiError(code, detail, reason=PLUGIN_REFUSED)


async def _in_tx[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    def run() -> T:
        with state.settings_store.pool.connection() as conn, conn.transaction():
            return fn(conn)

    return await asyncio.to_thread(run)


def _last4(secret: str) -> str:
    """``secrets.ts`` ``last4``."""
    return "" if len(secret) < 12 else secret[-4:]


class Done(BaseModel):
    done: bool


# -- the registry -------------------------------------------------------------------------


class AccessRow(BaseModel):
    source: str
    item: str
    access: str
    updated_by: str


class OAuthRow(BaseModel):
    """A sign-in's public columns (``registry.ts`` ``OAUTH_ROW``): never a sealed one."""

    status: Literal["connected", "error"]
    last_error: str | None
    issuer: str
    scope: str | None
    registration: Literal["dynamic", "configured"]
    has_refresh: bool
    access_expires_at: datetime | None
    connected_at: datetime
    grant_id: str
    plugin_url: str


class PluginRow(BaseModel):
    """A plugin as ``registry.ts`` reads it, with no secret: its ``remote:<name>`` access
    rows and its sign-in's public columns. ``opens``: no secret, or one the API's key
    opens (the Settings row's ``usable``)."""

    name: str
    kind: str
    url: str
    auth_header: str | None
    kek_id: str | None
    last4: str | None
    created_at: datetime
    updated_at: datetime
    access_rows: list[AccessRow]
    oauth: OAuthRow | None
    opens: bool


class Header(BaseModel):
    name: str
    value: str


class OpenedPlugin(PluginRow):
    """A plugin with its header value opened; ``seal_error`` when it cannot be (no key,
    another key, an altered row), as the agent's ``SealError``."""

    header: Header | None
    seal_error: str | None


class PluginAnswer(BaseModel):
    plugin: PluginRow | None


class PluginList(BaseModel):
    plugins: list[PluginRow]


class OpenedAnswer(BaseModel):
    plugin: OpenedPlugin | None


class OpenedList(BaseModel):
    plugins: list[OpenedPlugin]


_COLUMNS = (
    "name, kind, url, auth_header, secret_sealed, dek_sealed, kek_id, last4, created_at, updated_at"
)


def _select(conn: psycopg.Connection[Any], name: str | None) -> list[dict[str, Any]]:
    if name is None:
        rows = conn.execute(f"SELECT {_COLUMNS} FROM ai_plugins ORDER BY name").fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM ai_plugins WHERE name = %s", (name,)
        ).fetchall()
    out = [dict(r) for r in rows]
    if not out:
        return out
    names = [r["name"] for r in out]
    access = acc.access_rows(conn, [acc.remote_source(n) for n in names])
    oauth = {
        r["plugin"]: r
        for r in conn.execute(
            "SELECT plugin, status, last_error, issuer, scope, registration, has_refresh,"
            " access_expires_at, connected_at, grant_id::text AS grant_id, plugin_url"
            " FROM ai_plugin_oauth WHERE plugin = ANY(%s)",
            (names,),
        ).fetchall()
    }
    for row in out:
        source = acc.remote_source(row["name"])
        row["access_rows"] = [a for a in access if a["source"] == source]
        row["oauth"] = oauth.get(row["name"])
    return out


def _view(row: dict[str, Any], kek: Kek | None) -> PluginRow:
    return PluginRow(
        name=row["name"],
        kind=row["kind"],
        url=row["url"],
        auth_header=row["auth_header"],
        kek_id=row["kek_id"],
        last4=row["last4"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        access_rows=[AccessRow(**a) for a in row["access_rows"]],
        oauth=None if row["oauth"] is None else OAuthRow(**_public(row["oauth"])),
        opens=row["kek_id"] is None or (kek is not None and kek.id == row["kek_id"]),
    )


def _public(oauth: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in oauth.items() if k != "plugin"}


def _opened(row: dict[str, Any], kek: Kek | None) -> OpenedPlugin:
    """``registry.ts`` ``openPlugin``'s header, opened here."""
    header: Header | None = None
    seal_error: str | None = None
    if row["secret_sealed"] is not None and row["auth_header"] is not None:
        if kek is None:
            seal_error = NO_KEK_TO_OPEN
        else:
            try:
                value = open_secret(
                    kek, envelope_of(row), plugin_aad(row["name"], row["url"], row["auth_header"])
                )
                header = Header(name=row["auth_header"], value=value)
            except SealError as e:
                seal_error = str(e)
    return OpenedPlugin(**_view(row, kek).model_dump(), header=header, seal_error=seal_error)


def _one(conn: psycopg.Connection[Any], name: str) -> dict[str, Any]:
    rows = _select(conn, name)
    if not rows:
        raise RuntimeError(f"plugin {name} is gone inside its own transaction")
    return rows[0]


def _stored(row: dict[str, Any]) -> tuple[dict[str, str], list[str], bool]:
    """(tiers, disabled tools, enabled): the plugin's access as ``ai_access`` gives it."""
    source = acc.remote_source(row["name"])
    tiers, disabled = acc.plugin_overrides(source, row["access_rows"])
    return tiers, disabled, acc.source_enabled(source, row["access_rows"])


def _tool_rows(tiers: dict[str, str], disabled: list[str]) -> dict[str, str]:
    """``registry.ts`` ``toolRows``: a disabled tool is ``off``, whatever its tier."""
    return {**tiers, **dict.fromkeys(disabled, "off")}


def _write_access(
    conn: psycopg.Connection[Any],
    name: str,
    *,
    tiers: dict[str, str],
    disabled: list[str],
    enabled: bool,
    by: str,
    write_enabled: bool,
    write_tools: bool,
) -> None:
    """``PluginStore.writeAccess``: the access rows the write names, then the eviction."""
    conn.execute("UPDATE ai_plugins SET updated_at = now() WHERE name = %s", (name,))
    source = acc.remote_source(name)
    if write_enabled:
        acc.write_enabled(conn, source, enabled, by)
    if write_tools:
        acc.write_tools(conn, source, _tool_rows(tiers, disabled), by)
    acc.notify_warm_evict(conn)


@router.get("/plugins", name="crud_agent_plugins")
async def plugins(state: StateDep) -> PluginList:
    kek = _kek(state)
    rows = await _in_tx(state, lambda conn: _select(conn, None))
    return PluginList(plugins=[_view(r, kek) for r in rows])


@router.get("/plugins/{name}", name="crud_agent_plugin")
async def plugin(name: PathName, state: StateDep) -> PluginAnswer:
    kek = _kek(state)
    rows = await _in_tx(state, lambda conn: _select(conn, name))
    return PluginAnswer(plugin=_view(rows[0], kek) if rows else None)


class PluginCreate(_Body):
    """A plugin the agent checked (``PluginStore.create``): its URL normalised, its header
    and secret validated, its tool rows built from the tiers and disabled tools."""

    name: Name
    url: Url
    auth_header: str | None = Field(default=None, min_length=1, max_length=64)
    secret: str | None = Field(default=None, min_length=1, max_length=4096)
    tools: dict[ToolName, ToolAccess] = Field(default_factory=dict, max_length=2 * MAX_TOOL_ENTRIES)
    by: By


@router.post("/plugins", status_code=status.HTTP_201_CREATED, name="crud_agent_plugin_create")
async def plugin_create(body: PluginCreate, state: StateDep) -> PluginRow:
    if (body.secret is None) != (body.auth_header is None):
        raise _refused(status.HTTP_400_BAD_REQUEST, "auth_header needs a secret")
    kek = _kek(state)
    sealed = None
    if body.secret is not None and body.auth_header is not None:
        sealed = seal_secret(
            _need_kek(state), body.secret, plugin_aad(body.name, body.url, body.auth_header)
        )

    def run(conn: psycopg.Connection[Any]) -> dict[str, Any]:
        made = conn.execute(
            "INSERT INTO ai_plugins (name, kind, url, auth_header, secret_sealed, dek_sealed,"
            " kek_id, last4) VALUES (%s, 'remote_mcp', %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (name) DO NOTHING RETURNING name",
            (
                body.name,
                body.url,
                body.auth_header if sealed else None,
                sealed.secret_sealed if sealed else None,
                sealed.dek_sealed if sealed else None,
                sealed.kek_id if sealed else None,
                _last4(body.secret) if sealed and body.secret else None,
            ),
        ).fetchone()
        if made is None:
            raise _refused(status.HTTP_409_CONFLICT, f'a plugin named "{body.name}" already exists')
        source = acc.remote_source(body.name)
        # A row an earlier plugin of this name left behind is not this one's.
        acc.forget_source(conn, source)
        acc.write_enabled(conn, source, False, "system")
        acc.write_tools(conn, source, body.tools, "system")
        acc.notify_warm_evict(conn)
        return _one(conn, body.name)

    return _view(await _in_tx(state, run), kek)


class PluginUpdate(_Body):
    """``PluginStore.update``'s patch as the agent checked it. A field left out is kept;
    ``secret`` null removes the header and its value; ``auth_header`` null names none."""

    url: Url | None = None
    auth_header: str | None = Field(default=None, min_length=1, max_length=64)
    secret: str | None = Field(default=None, min_length=1, max_length=4096)
    enabled: bool | None = None
    tool_tiers: dict[ToolName, Tier] | None = Field(default=None, max_length=MAX_TOOL_ENTRIES)
    disabled_tools: list[ToolName] | None = Field(default=None, max_length=MAX_TOOL_ENTRIES)
    by: By = "system"


@router.post("/plugins/{name}/update", name="crud_agent_plugin_update")
async def plugin_update(name: PathName, body: PluginUpdate, state: StateDep) -> PluginRow:
    sent = body.model_fields_set
    kek = _kek(state)

    def run(conn: psycopg.Connection[Any]) -> dict[str, Any]:
        locked = conn.execute(
            "SELECT name FROM ai_plugins WHERE name = %s FOR UPDATE", (name,)
        ).fetchone()
        if locked is None:
            raise _refused(status.HTTP_404_NOT_FOUND, f'no plugin named "{name}"')
        current = _one(conn, name)
        tiers, disabled, enabled = _stored(current)
        url = body.url if body.url is not None else current["url"]
        if body.tool_tiers is not None:
            tiers = dict(body.tool_tiers)
        if body.disabled_tools is not None:
            disabled = acc.js_sort(set(body.disabled_tools))
        if body.enabled is not None:
            enabled = body.enabled
        # The secret: a new URL or header name re-binds it, which needs the value again.
        columns: tuple[Any, ...]
        if "secret" in sent and body.secret is None:
            if body.auth_header:
                raise _refused(status.HTTP_400_BAD_REQUEST, "auth_header needs a secret")
            columns = (None, None, None, None, None)
        elif body.secret is not None:
            header = body.auth_header or current["auth_header"] or DEFAULT_AUTH_HEADER
            sealed = seal_secret(_need_kek(state), body.secret, plugin_aad(name, url, header))
            columns = (
                header,
                sealed.secret_sealed,
                sealed.dek_sealed,
                sealed.kek_id,
                _last4(body.secret),
            )
        else:
            header = body.auth_header if "auth_header" in sent else current["auth_header"]
            if current["auth_header"] is None:
                if header:
                    raise _refused(status.HTTP_400_BAD_REQUEST, "auth_header needs a secret")
            elif url != current["url"] or header != current["auth_header"]:
                raise _refused(
                    status.HTTP_409_CONFLICT,
                    "changing url or auth_header needs the secret again (or secret: null to"
                    " remove it): the stored one is not sent to a new destination",
                )
            columns = (
                current["auth_header"],
                current["secret_sealed"],
                current["dek_sealed"],
                current["kek_id"],
                current["last4"],
            )
        conn.execute(
            "UPDATE ai_plugins SET url = %s, auth_header = %s, secret_sealed = %s,"
            " dek_sealed = %s, kek_id = %s, last4 = %s, updated_at = now() WHERE name = %s",
            (url, *columns, name),
        )
        source = acc.remote_source(name)
        # The last listing was the old server's (#4994).
        if url != current["url"]:
            conn.execute("DELETE FROM ai_tool_listings WHERE source = %s", (source,))
        # A sign-in was for the old resource; a header replaces it (#780).
        if url != current["url"] or body.secret is not None:
            conn.execute("DELETE FROM ai_plugin_oauth_flows WHERE plugin = %s", (name,))
            conn.execute("DELETE FROM ai_plugin_oauth WHERE plugin = %s", (name,))
        _write_access(
            conn,
            name,
            tiers=tiers,
            disabled=disabled,
            enabled=enabled,
            by=body.by,
            write_enabled=body.enabled is not None,
            write_tools=body.tool_tiers is not None or body.disabled_tools is not None,
        )
        return _one(conn, name)

    return _view(await _in_tx(state, run), kek)


class ToolAccessSet(_Body):
    """One tool's access (``PluginStore.setToolAccess``): ``off`` disables it, a tier sets
    it, null drops both. The agent checked the tool's name for the access it takes."""

    tool: ToolName
    access: ToolAccess | None
    by: By = "system"


@router.post("/plugins/{name}/tool-access", name="crud_agent_plugin_tool_access")
async def plugin_tool_access(name: PathName, body: ToolAccessSet, state: StateDep) -> PluginRow:
    kek = _kek(state)

    def run(conn: psycopg.Connection[Any]) -> dict[str, Any]:
        locked = conn.execute(
            "SELECT name FROM ai_plugins WHERE name = %s FOR UPDATE", (name,)
        ).fetchone()
        if locked is None:
            raise _refused(status.HTTP_404_NOT_FOUND, f'no plugin named "{name}"')
        tiers, disabled, enabled = _stored(_one(conn, name))
        source = acc.remote_source(name)
        # Each tool row as it stands, so only this tool's is attributed to this write.
        before = conn.execute(
            "SELECT item, access, updated_by, updated_at::text AS updated_at FROM ai_access"
            " WHERE source = %s AND item <> %s",
            (source, acc.SOURCE_ITEM),
        ).fetchall()
        tiers.pop(body.tool, None)
        disabled = [t for t in disabled if t != body.tool]
        if body.access == "off":
            disabled.append(body.tool)
        elif body.access is not None:
            tiers[body.tool] = body.access
        if len(disabled) > MAX_TOOL_ENTRIES:
            raise _refused(
                status.HTTP_400_BAD_REQUEST,
                f'At most {MAX_TOOL_ENTRIES} tools of plugin "{name}" can be off: reset one'
                " of them first",
            )
        if len(tiers) > MAX_TOOL_ENTRIES:
            raise _refused(
                status.HTTP_400_BAD_REQUEST,
                f'At most {MAX_TOOL_ENTRIES} tools of plugin "{name}" can have an access other'
                " than off or the default: reset one of them first",
            )
        _write_access(
            conn,
            name,
            tiers=tiers,
            disabled=acc.js_sort(set(disabled)),
            enabled=enabled,
            by=body.by,
            write_enabled=False,
            write_tools=True,
        )
        # Every tool row was written as new: the rows this write left get their author back.
        kept = [dict(r) for r in before if r["item"] != body.tool]
        if kept:
            conn.execute(
                "UPDATE ai_access a SET updated_by = k.updated_by, updated_at = k.updated_at"
                " FROM jsonb_to_recordset(%s) AS k(item text, access text, updated_by text,"
                " updated_at timestamptz)"
                " WHERE a.source = %s AND a.item = k.item AND a.access = k.access",
                (Jsonb(kept), source),
            )
        return _one(conn, name)

    return _view(await _in_tx(state, run), kek)


@router.post("/plugins/{name}/delete", name="crud_agent_plugin_delete")
async def plugin_delete(name: PathName, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> bool:
        gone = conn.execute("DELETE FROM ai_plugins WHERE name = %s", (name,)).rowcount
        if gone == 0:
            return False
        acc.forget_source(conn, acc.remote_source(name))
        acc.notify_warm_evict(conn)
        return True

    return Done(done=await _in_tx(state, run))


@router.post("/plugins/{name}/reveal", name="crud_agent_plugin_reveal")
async def plugin_reveal(name: PathName, state: StateDep) -> OpenedAnswer:
    """One plugin with its header value opened, for a connection test."""
    kek = _kek(state)
    rows = await _in_tx(state, lambda conn: _select(conn, name))
    return OpenedAnswer(plugin=_opened(rows[0], kek) if rows else None)


@router.post("/plugins/open-enabled", name="crud_agent_plugins_open_enabled")
async def plugins_open_enabled(state: StateDep) -> OpenedList:
    """Every plugin ``ai_access`` turns on, opened, for a turn (``PluginStore.enabled``).
    One that cannot be opened is listed with its ``seal_error``."""
    kek = _kek(state)
    rows = await _in_tx(state, lambda conn: _select(conn, None))
    on = [r for r in rows if _stored(r)[2]]
    return OpenedList(plugins=[_opened(r, kek) for r in on])


# -- OAuth sign-ins -----------------------------------------------------------------------


class FlowMeta(_Body):
    """``oauthStore.ts`` ``FlowMeta``."""

    issuer: Url
    authorization_endpoint: Url
    token_endpoint: Url
    iss_supported: bool
    resource: Url
    scope: Text | None
    client_id: Annotated[str, Field(min_length=1, max_length=TEXT_MAX)]
    auth_method: Literal["none", "client_secret_post", "client_secret_basic"]
    registration: Literal["dynamic", "configured"]
    redirect_uri: Url


class FlowSecret(_Body):
    state: Annotated[str, Field(min_length=1, max_length=512)]
    verifier: Annotated[str, Field(min_length=1, max_length=512)]
    client_secret: Annotated[str, Field(min_length=1, max_length=TOKEN_MAX)] | None = None


def _flow_text(secret: FlowSecret) -> str:
    """The plaintext the agent seals (``start``), in its key order."""
    value: dict[str, str] = {"state": secret.state, "verifier": secret.verifier}
    if secret.client_secret:
        value["client_secret"] = secret.client_secret
    return js_json(value)


class FlowStart(_Body):
    state_hash: Annotated[str, Field(pattern=HASH)]
    plugin: Name
    plugin_url: Url
    ticket_hash: Annotated[str, Field(pattern=HASH)]
    meta: FlowMeta
    secret: FlowSecret
    created_by: By
    ttl_ms: int = Field(gt=0, le=3_600_000)


@router.post(
    "/plugin-oauth/flows",
    status_code=status.HTTP_201_CREATED,
    name="crud_agent_plugin_oauth_flow_start",
)
async def oauth_flow_start(body: FlowStart, state: StateDep) -> Done:
    """A sign-in under way (``PluginOAuth.start``): its state and verifier sealed, the
    expired flows dropped first."""
    kek = _need_kek(state)
    meta = body.meta.model_dump()
    sealed = seal_secret(
        kek, _flow_text(body.secret), flow_aad(body.state_hash, body.plugin, body.plugin_url, meta)
    )

    def run(conn: psycopg.Connection[Any]) -> None:
        conn.execute("DELETE FROM ai_plugin_oauth_flows WHERE expires_at < now()")
        try:
            with conn.transaction():
                conn.execute(
                    "INSERT INTO ai_plugin_oauth_flows (state_hash, plugin, plugin_url,"
                    " ticket_hash, meta, secret_sealed, dek_sealed, kek_id, created_by,"
                    " expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,"
                    " now() + make_interval(secs => %s))",
                    (
                        body.state_hash,
                        body.plugin,
                        body.plugin_url,
                        body.ticket_hash,
                        Jsonb(meta),
                        sealed.secret_sealed,
                        sealed.dek_sealed,
                        sealed.kek_id,
                        body.created_by,
                        body.ttl_ms / 1000,
                    ),
                )
        except psycopg.errors.ForeignKeyViolation:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"plugin {body.plugin} was removed during the sign-in",
                reason=PLUGIN_GONE,
            ) from None

    await _in_tx(state, run)
    return Done(done=True)


class FlowOpened(BaseModel):
    plugin: str
    plugin_url: str
    meta: dict[str, Any]
    secret: FlowSecret | None
    #: Why ``secret`` is null: no key, another key, an altered row.
    seal_error: str | None


def _open_flow(row: dict[str, Any], kek: Kek | None) -> FlowOpened:
    secret: FlowSecret | None = None
    seal_error: str | None = None
    if kek is None:
        seal_error = "no key-encryption key is configured to open the sign-in"
    else:
        try:
            text = open_secret(
                kek,
                envelope_of(row),
                flow_aad(row["state_hash"], row["plugin"], row["plugin_url"], row["meta"]),
            )
            secret = FlowSecret.model_validate_json(text)
        except SealError as e:
            seal_error = str(e)
    return FlowOpened(
        plugin=row["plugin"],
        plugin_url=row["plugin_url"],
        meta=row["meta"],
        secret=secret,
        seal_error=seal_error,
    )


class FlowAuthorize(_Body):
    ticket_hash: Annotated[str, Field(pattern=HASH)]
    binding_hash: Annotated[str, Field(pattern=HASH)]


class FlowAnswer(BaseModel):
    flow: FlowOpened | None


@router.post("/plugin-oauth/flows/authorize", name="crud_agent_plugin_oauth_flow_authorize")
async def oauth_flow_authorize(body: FlowAuthorize, state: StateDep) -> FlowAnswer:
    """Spends a ticket once (``PluginOAuth.authorize``): binds its flow to the browser's
    cookie and answers the flow opened, for the redirect. None for an unknown, spent or
    expired ticket. Without a KEK nothing is spent."""
    kek = _need_kek(state)

    def run(conn: psycopg.Connection[Any]) -> dict[str, Any] | None:
        row = conn.execute(
            "UPDATE ai_plugin_oauth_flows SET ticket_hash = NULL, binding_hash = %s"
            " WHERE ticket_hash = %s AND binding_hash IS NULL AND expires_at > now()"
            " RETURNING state_hash, plugin, plugin_url, meta, secret_sealed, dek_sealed, kek_id",
            (body.binding_hash, body.ticket_hash),
        ).fetchone()
        return None if row is None else dict(row)

    row = await _in_tx(state, run)
    return FlowAnswer(flow=None if row is None else _open_flow(row, kek))


class FlowTake(_Body):
    state_hash: Annotated[str, Field(pattern=HASH)]
    #: The cookie's hash; empty when the callback carried none.
    binding_hash: Annotated[str, Field(pattern=r"^([0-9a-f]{64})?$")]


class FlowTaken(FlowOpened):
    created_by: str
    #: Whether it was past its life when taken (by the database's clock).
    expired: bool


class TakeAnswer(BaseModel):
    flow: FlowTaken | None
    #: With no flow: whether one with this state waits for another browser.
    held: bool


@router.post("/plugin-oauth/flows/take", name="crud_agent_plugin_oauth_flow_take")
async def oauth_flow_take(body: FlowTake, state: StateDep) -> TakeAnswer:
    """The callback's flow (``PluginOAuth.complete``), deleted before anything else: a
    state is good once, and only with the browser's binding."""
    kek = _kek(state)

    def run(conn: psycopg.Connection[Any]) -> tuple[dict[str, Any] | None, bool]:
        row = conn.execute(
            "DELETE FROM ai_plugin_oauth_flows WHERE state_hash = %s AND binding_hash = %s"
            " RETURNING state_hash, plugin, plugin_url, meta, secret_sealed, dek_sealed, kek_id,"
            " created_by, expires_at <= now() AS expired",
            (body.state_hash, body.binding_hash),
        ).fetchone()
        if row is not None:
            return dict(row), False
        held = conn.execute(
            "SELECT 1 FROM ai_plugin_oauth_flows WHERE state_hash = %s AND expires_at > now()",
            (body.state_hash,),
        ).fetchone()
        return None, held is not None

    row, held = await _in_tx(state, run)
    if row is None:
        return TakeAnswer(flow=None, held=held)
    opened = _open_flow(row, kek)
    return TakeAnswer(
        flow=FlowTaken(**opened.model_dump(), created_by=row["created_by"], expired=row["expired"]),
        held=False,
    )


class GrantSecret(_Body):
    """``oauthStore.ts`` ``GrantSecret``."""

    access_token: Annotated[str, Field(min_length=1, max_length=TOKEN_MAX)]
    refresh_token: Annotated[str, Field(min_length=1, max_length=TOKEN_MAX)] | None = None
    client_secret: Annotated[str, Field(min_length=1, max_length=TOKEN_MAX)] | None = None


def _grant_text(secret: GrantSecret) -> str:
    """The plaintext the agent seals (``sealGrant``), in its key order."""
    value: dict[str, str] = {"access_token": secret.access_token}
    if secret.refresh_token:
        value["refresh_token"] = secret.refresh_token
    if secret.client_secret:
        value["client_secret"] = secret.client_secret
    return js_json(value)


class GrantStore(_Body):
    plugin: Name
    plugin_url: Url
    resource: Url
    issuer: Url
    token_endpoint: Url
    client_id: Annotated[str, Field(min_length=1, max_length=TEXT_MAX)]
    auth_method: Literal["none", "client_secret_post", "client_secret_basic"]
    registration: Literal["dynamic", "configured"]
    scope: Text | None
    secret: GrantSecret
    has_refresh: bool
    access_expires_at: datetime | None
    connected_by: By


class Stored(BaseModel):
    stored: bool


@router.post("/plugin-oauth/grants", name="crud_agent_plugin_oauth_grant_store")
async def oauth_grant_store(body: GrantStore, state: StateDep) -> Stored:
    """A completed sign-in (``PluginOAuth.complete``), sealed, kept only while the plugin
    still stands at the URL it was signed in for. False: nothing was kept."""
    kek = _need_kek(state)
    base = body.model_dump(
        include={"plugin", "plugin_url", "resource", "issuer", "token_endpoint", "client_id"}
    )
    sealed = seal_secret(kek, _grant_text(body.secret), grant_aad(base))

    def run(conn: psycopg.Connection[Any]) -> bool:
        plugin = conn.execute(
            "SELECT name FROM ai_plugins WHERE name = %s AND url = %s FOR UPDATE",
            (body.plugin, body.plugin_url),
        ).fetchone()
        if plugin is None:
            return False
        conn.execute(
            "INSERT INTO ai_plugin_oauth (plugin, plugin_url, resource, issuer, token_endpoint,"
            " client_id, auth_method, registration, scope, secret_sealed, dek_sealed, kek_id,"
            " has_refresh, access_expires_at, status, last_error, grant_id, connected_by,"
            " connected_at, refreshed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
            " %s, %s, %s, 'connected', NULL, %s, %s, now(), NULL)"
            " ON CONFLICT (plugin) DO UPDATE SET plugin_url = EXCLUDED.plugin_url,"
            " resource = EXCLUDED.resource, issuer = EXCLUDED.issuer,"
            " token_endpoint = EXCLUDED.token_endpoint, client_id = EXCLUDED.client_id,"
            " auth_method = EXCLUDED.auth_method, registration = EXCLUDED.registration,"
            " scope = EXCLUDED.scope, secret_sealed = EXCLUDED.secret_sealed,"
            " dek_sealed = EXCLUDED.dek_sealed, kek_id = EXCLUDED.kek_id,"
            " has_refresh = EXCLUDED.has_refresh, access_expires_at = EXCLUDED.access_expires_at,"
            " status = 'connected', last_error = NULL, grant_id = EXCLUDED.grant_id,"
            " connected_by = EXCLUDED.connected_by, connected_at = EXCLUDED.connected_at,"
            " refreshed_at = NULL",
            (
                body.plugin,
                body.plugin_url,
                body.resource,
                body.issuer,
                body.token_endpoint,
                body.client_id,
                body.auth_method,
                body.registration,
                body.scope,
                sealed.secret_sealed,
                sealed.dek_sealed,
                sealed.kek_id,
                body.has_refresh,
                body.access_expires_at,
                uuid4(),
                body.connected_by,
            ),
        )
        conn.execute("UPDATE ai_plugins SET updated_at = now() WHERE name = %s", (body.plugin,))
        acc.notify_warm_evict(conn)
        return True

    return Stored(stored=await _in_tx(state, run))


class GrantOpen(_Body):
    plugin_url: Url
    grant: UUID


class OpenedGrant(BaseModel):
    """A sign-in's row for a token request, its secret opened."""

    plugin: str
    plugin_url: str
    resource: str
    issuer: str
    token_endpoint: str
    client_id: str
    auth_method: str
    grant_id: str
    access_expires_at: datetime | None
    secret: GrantSecret


class GrantOpenAnswer(BaseModel):
    grant: OpenedGrant | None
    #: Why there is none (``PluginOAuth``'s ``GrantError`` text): sign in again.
    fail: str | None


def _grant_row(conn: psycopg.Connection[Any], plugin: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT plugin, plugin_url, resource, issuer, token_endpoint, client_id, auth_method,"
        " grant_id::text AS grant_id, access_expires_at, status, last_error, secret_sealed,"
        " dek_sealed, kek_id FROM ai_plugin_oauth WHERE plugin = %s",
        (plugin,),
    ).fetchone()
    return None if row is None else dict(row)


def _open_grant(row: dict[str, Any] | None, kek: Kek | None, body: GrantOpen) -> GrantOpenAnswer:
    """``PluginOAuth.fetchToken``'s checks, then the row opened."""

    def fail(reason: str) -> GrantOpenAnswer:
        return GrantOpenAnswer(grant=None, fail=reason)

    if kek is None:
        return fail("no key-encryption key is configured to open the sign-in")
    if row is None:
        return fail("it is not signed in")
    if row["status"] == "error":
        return fail(row["last_error"] or "its sign-in failed")
    if row["plugin_url"] != body.plugin_url:
        return fail("it was signed in at another URL")
    if row["grant_id"] != str(body.grant):
        return fail("it was signed in again or out since this run loaded it")
    try:
        secret = GrantSecret.model_validate_json(open_secret(kek, envelope_of(row), grant_aad(row)))
    except SealError as e:
        return fail(f"its sign-in cannot be opened ({e})")
    return GrantOpenAnswer(
        grant=OpenedGrant(
            **{k: row[k] for k in OpenedGrant.model_fields if k != "secret"}, secret=secret
        ),
        fail=None,
    )


@router.post("/plugin-oauth/grants/{plugin}/open", name="crud_agent_plugin_oauth_grant_open")
async def oauth_grant_open(plugin: PathName, body: GrantOpen, state: StateDep) -> GrantOpenAnswer:
    kek = _kek(state)
    row = await _in_tx(state, lambda conn: _grant_row(conn, plugin))
    return _open_grant(row, kek, body)


class GrantRefreshed(_Body):
    #: The holder's token of the lease ``plugin-oauth:<plugin>``.
    lease_token: UUID
    grant: UUID
    secret: GrantSecret
    has_refresh: bool
    access_expires_at: datetime | None
    scope: Text | None


@router.post(
    "/plugin-oauth/grants/{plugin}/refreshed", name="crud_agent_plugin_oauth_grant_refreshed"
)
async def oauth_grant_refreshed(plugin: PathName, body: GrantRefreshed, state: StateDep) -> Stored:
    """A refreshed sign-in, kept only while the caller holds the refresh lease (else 409
    ``lease-lost``: another replica may spend the refresh token) and the grant is still
    the one it refreshed and connected, as the SQL path's refresh requires. False: the grant
    was replaced, removed or marked for a new sign-in since."""
    kek = _need_kek(state)

    def run(conn: psycopg.Connection[Any]) -> bool:
        if not leases.held_in(conn, f"plugin-oauth:{plugin}", str(body.lease_token)):
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "The refresh lease is not held by this token.",
                reason=LEASE_LOST,
            )
        row = conn.execute(
            "SELECT plugin, plugin_url, resource, issuer, token_endpoint, client_id"
            " FROM ai_plugin_oauth WHERE plugin = %s AND grant_id = %s AND status = 'connected'"
            " FOR UPDATE",
            (plugin, body.grant),
        ).fetchone()
        if row is None:
            return False
        sealed = seal_secret(kek, _grant_text(body.secret), grant_aad(row))
        conn.execute(
            "UPDATE ai_plugin_oauth SET secret_sealed = %s, dek_sealed = %s, kek_id = %s,"
            " has_refresh = %s, access_expires_at = %s, refreshed_at = now(),"
            " scope = coalesce(%s, scope) WHERE plugin = %s",
            (
                sealed.secret_sealed,
                sealed.dek_sealed,
                sealed.kek_id,
                body.has_refresh,
                body.access_expires_at,
                body.scope,
                plugin,
            ),
        )
        return True

    return Stored(stored=await _in_tx(state, run))


class GrantMark(_Body):
    reason: Annotated[str, Field(min_length=1, max_length=TEXT_MAX)]
    #: Only while this grant is the plugin's; left out, whichever is.
    grant: UUID | None = None


@router.post("/plugin-oauth/grants/{plugin}/mark", name="crud_agent_plugin_oauth_grant_mark")
async def oauth_grant_mark(plugin: PathName, body: GrantMark, state: StateDep) -> Done:
    """Marks the sign-in failed (``PluginOAuth.mark``): "Sign in again" in Settings."""

    def run(conn: psycopg.Connection[Any]) -> bool:
        if body.grant is None:
            updated = conn.execute(
                "UPDATE ai_plugin_oauth SET status = 'error', last_error = %s"
                " WHERE plugin = %s AND status = 'connected'",
                (body.reason, plugin),
            ).rowcount
        else:
            updated = conn.execute(
                "UPDATE ai_plugin_oauth SET status = 'error', last_error = %s"
                " WHERE plugin = %s AND status = 'connected' AND grant_id = %s",
                (body.reason, plugin, body.grant),
            ).rowcount
        if updated > 0:
            acc.notify_warm_evict(conn)
        return updated > 0

    return Done(done=await _in_tx(state, run))


@router.post("/plugin-oauth/grants/{plugin}/forget", name="crud_agent_plugin_oauth_grant_forget")
async def oauth_grant_forget(plugin: PathName, state: StateDep) -> Done:
    """Signs the plugin out (``PluginOAuth.forget``): the grant and any flow under way."""

    def run(conn: psycopg.Connection[Any]) -> bool:
        conn.execute("DELETE FROM ai_plugin_oauth_flows WHERE plugin = %s", (plugin,))
        gone = conn.execute("DELETE FROM ai_plugin_oauth WHERE plugin = %s", (plugin,)).rowcount
        if gone > 0:
            acc.notify_warm_evict(conn)
        return gone > 0

    return Done(done=await _in_tx(state, run))


class StoredSecrets(BaseModel):
    secrets: list[GrantSecret]


@router.post("/plugin-oauth/stored-secrets", name="crud_agent_plugin_oauth_stored_secrets")
async def oauth_stored_secrets(state: StateDep) -> StoredSecrets:
    """Every stored sign-in's secrets, opened, for the redaction list
    (``PluginOAuth.storedSecrets``). A row the key does not open gives nothing; none
    without a KEK."""
    kek = _kek(state)
    if kek is None:
        return StoredSecrets(secrets=[])

    def run(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT plugin, plugin_url, resource, issuer, token_endpoint, client_id,"
                " secret_sealed, dek_sealed, kek_id FROM ai_plugin_oauth"
            ).fetchall()
        ]

    out: list[GrantSecret] = []
    for row in await _in_tx(state, run):
        try:
            out.append(
                GrantSecret.model_validate_json(open_secret(kek, envelope_of(row), grant_aad(row)))
            )
        except SealError:
            continue
    return StoredSecrets(secrets=out)
