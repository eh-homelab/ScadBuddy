"""The agent's small stores behind its CRUD routes (#5299 step 5): under
``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`). Each
route is one of the agent store's methods, and runs what that method ran over SQL, a
transaction as one transaction (#5299 D2), so the agent's TypeScript stores keep their
interfaces and only their implementation moves (``agent/src/…``, each store's ``Crud…``
class). The agent uses an area's routes once ``GET …/agent/areas`` lists it, and its SQL
until then (#5299 D9; `api/crud_areas.py`).

- area ``activity-kinds`` (``agent/src/activity/kinds.ts``): ``POST …/activity-kinds``, the
  agent's activity kinds upserted at its start and every other one retired, in one
  transaction;
- area ``browser-origins`` (``browser/store.ts``): a session's approved origins
  (``GET …/browser-origins``), an approval (``POST …/browser-origins/approve``, ``once``
  never narrowing ``session``), a withdrawal (``…/revoke``) and Settings' list
  (``GET …/browser-origins/approved``);
- area ``bridge-tabs``: which tab a session pairs with (``bridge/sessionTabs.ts``,
  ``…/session-tabs``, a write only while the row still names the tab the caller saw), which
  replica's queue holds a tab (``bridge/tabOwners.ts``, ``…/tab-owners``), and a tab's
  images between replicas (``bridge/tabImages.ts``, ``…/tab-images/{name}``: the bytes are
  the request and the answer's body, never JSON, #5299 D8 and #2479; a name is the bytes'
  sha256, checked here again);
- area ``pairings`` (``bridge/pairings.ts``): a pairing request, under the transaction's
  advisory lock that serialises its cap check and insert; an accept, under the row's
  ``FOR UPDATE``, comparing the code's hash in constant time; deny, end and the reads. The
  code never reaches the API: the agent sends its SHA-256 only;
- area ``mcp-tokens`` (``auth/tokens.ts``): a token's hash looked up and stamped in one
  ``UPDATE … RETURNING`` per authenticated MCP request (the plaintext never reaches the API:
  the agent sends its SHA-256 only, and the API compares hashes only), a mint with the
  hash the agent made, a revoke, the list and the two live-token reads. Every expiry is
  judged by the database's clock.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import Path, Query, Request, Response, status
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

#: A session id, a tab id (the frontend's random base64url), a replica's queue.
TAB_ID_MAX = 200
QUEUE_MAX = 300
#: An http(s) origin, as `ai_browser_session_origins.origin` checks it.
ORIGIN_MAX = 2048
ORIGIN_PATTERN = r"^https?://[^/?#@\s]+$"
#: The most tabs one refresh or ``paired-with`` names.
TABS_MAX = 500
#: The most approved origins Settings lists (``browser/store.ts`` ``APPROVED_LISTED``).
APPROVED_LISTED = 200
#: A tab image's name and its largest bytes (``sessions/images.ts`` ``IMAGE_BYTES_MAX``).
TAB_IMAGE_NAME = r"^[0-9a-f]{64}\.(png|jpg|gif|webp)$"
TAB_IMAGE_BYTES_MAX = 5 * 1024 * 1024 // 4 * 3
#: How long a tab's image is held (``bridge/tabImages.ts`` ``TAB_IMAGE_TTL_MS``).
TAB_IMAGE_TTL_MS = 10 * 60_000
TAB_IMAGE_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
#: The pairing policy (``bridge/pairings.ts``, spec §8.5).
PAIRING_REQUEST_TTL_MS = 5 * 60_000
PAIRING_PAIRED_TTL_MS = 8 * 60 * 60_000
PAIRING_MAX_ATTEMPTS = 5
PAIRING_MAX_PENDING_PER_PRINCIPAL = 3
PAIRING_MAX_PENDING = 20
PAIRING_REQUEST_LOCK = "scadbuddy.ai_browser_pairings.request"
#: The route's reason for a pairing request it refuses (a cap reached).
PAIRING_REFUSED = "pairing-refused"
#: A hex SHA-256 (a code's, a token's).
SHA256_HEX = r"^[0-9a-f]{64}$"
#: An activity kind's key, label and facets (``activity/kinds.ts``).
ACTIVITY_KINDS_MAX = 500
LABEL_MAX = 300
#: A fallback label's longest: its prefix and a `principal_id` of up to 500.
FALLBACK_LABEL_MAX = 600

Hash = Annotated[str, Field(pattern=SHA256_HEX)]
TabId = Annotated[str, Field(min_length=1, max_length=TAB_ID_MAX)]
Queue = Annotated[str, Field(min_length=1, max_length=QUEUE_MAX)]
Origin = Annotated[str, Field(min_length=8, max_length=ORIGIN_MAX, pattern=ORIGIN_PATTERN)]
Scope = Literal["once", "session"]
PrincipalKind = Literal["bearer", "oidc", "anonymous", "flow"]
Tier = Literal["read", "write", "outward"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _pool(state: Any) -> Any:
    return state.settings_store.pool


async def _in_tx[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    """``fn`` in one transaction on a pooled connection, off the event loop."""

    def run() -> T:
        with _pool(state).connection() as conn, conn.transaction():
            return fn(conn)

    return await asyncio.to_thread(run)


def _ms(ms: int) -> str:
    return f"{ms} milliseconds"


class Ok(BaseModel):
    """A write that answers nothing more than that it ran."""

    ok: bool = True


# -- activity kinds ------------------------------------------------------------------------


class Facet(_Body):
    name: str = Field(min_length=1, max_length=100)
    type: Literal["keyword", "number", "bool", "time"]
    label: str = Field(max_length=LABEL_MAX)


class ActivityKind(_Body):
    key: str = Field(min_length=7, max_length=200, pattern=r"^agent\.[A-Za-z0-9_.-]+$")
    label: str = Field(max_length=LABEL_MAX)
    facets: list[Facet] = Field(default_factory=list, max_length=50)


class ActivityKinds(_Body):
    kinds: list[ActivityKind] = Field(max_length=ACTIVITY_KINDS_MAX)


@router.post("/activity-kinds", name="crud_agent_activity_kinds")
async def activity_kinds(body: ActivityKinds, state: StateDep) -> Ok:
    def run(conn: psycopg.Connection[Any]) -> None:
        for k in body.kinds:
            conn.execute(
                "INSERT INTO ai_activity_kinds (key, label, facets) VALUES (%s, %s, %s)"
                " ON CONFLICT (key) DO UPDATE SET label = EXCLUDED.label,"
                " facets = EXCLUDED.facets, retired_at = NULL, updated_at = now()",
                (k.key, k.label, Jsonb([f.model_dump() for f in k.facets])),
            )
        conn.execute(
            "UPDATE ai_activity_kinds SET retired_at = now()"
            " WHERE retired_at IS NULL AND key <> ALL(%s)",
            ([k.key for k in body.kinds],),
        )

    await _in_tx(state, run)
    return Ok()


# -- browser origins ----------------------------------------------------------------------


class Approved(BaseModel):
    origin: str
    scope: Scope


class ApprovedOrigins(BaseModel):
    origins: list[Approved]


@router.get("/browser-origins", name="crud_agent_browser_origins")
async def browser_origins(session_id: UUID, state: StateDep) -> ApprovedOrigins:
    def run(conn: psycopg.Connection[Any]) -> list[Any]:
        return conn.execute(
            "SELECT origin, scope FROM ai_browser_session_origins WHERE session_id = %s",
            (session_id,),
        ).fetchall()

    rows = await _in_tx(state, run)
    return ApprovedOrigins(origins=[Approved(origin=r["origin"], scope=r["scope"]) for r in rows])


class OriginApproval(_Body):
    session_id: UUID
    origin: Origin
    scope: Scope


class OriginRevoke(_Body):
    session_id: UUID
    origin: Origin


class Done(BaseModel):
    #: Whether a row was written or removed.
    done: bool


@router.post("/browser-origins/approve", name="crud_agent_browser_origin_approve")
async def browser_origin_approve(body: OriginApproval, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> bool:
        row = conn.execute(
            "INSERT INTO ai_browser_session_origins (session_id, origin, scope)"
            " SELECT id, %s, %s FROM ai_sessions WHERE id = %s"
            " ON CONFLICT (session_id, origin) DO UPDATE"
            " SET scope = CASE WHEN ai_browser_session_origins.scope = 'session'"
            " THEN 'session' ELSE EXCLUDED.scope END, approved_at = now()"
            " RETURNING origin",
            (body.origin, body.scope, body.session_id),
        ).fetchone()
        return row is not None

    return Done(done=await _in_tx(state, run))


@router.post("/browser-origins/revoke", name="crud_agent_browser_origin_revoke")
async def browser_origin_revoke(body: OriginRevoke, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> bool:
        row = conn.execute(
            "DELETE FROM ai_browser_session_origins WHERE session_id = %s AND origin = %s"
            " RETURNING origin",
            (body.session_id, body.origin),
        ).fetchone()
        return row is not None

    return Done(done=await _in_tx(state, run))


class ApprovedRow(BaseModel):
    session_id: str
    title: str
    origin: str
    scope: Scope
    approved_at: datetime


class ApprovedList(BaseModel):
    approved: list[ApprovedRow]


@router.get("/browser-origins/approved", name="crud_agent_browser_origins_approved")
async def browser_origins_approved(state: StateDep, session_id: UUID | None = None) -> ApprovedList:
    def run(conn: psycopg.Connection[Any]) -> list[Any]:
        return conn.execute(
            "SELECT o.session_id::text AS session_id, s.title, o.origin, o.scope, o.approved_at"
            " FROM ai_browser_session_origins o JOIN ai_sessions s ON s.id = o.session_id"
            " WHERE (%s::uuid IS NULL OR o.session_id = %s::uuid)"
            " ORDER BY o.approved_at DESC, o.origin LIMIT %s",
            (session_id, session_id, APPROVED_LISTED),
        ).fetchall()

    rows = await _in_tx(state, run)
    return ApprovedList(approved=[ApprovedRow.model_validate(r) for r in rows])


# -- session tabs, tab owners, tab images (area ``bridge-tabs``) --------------------------


class SessionTabWrite(_Body):
    session_id: UUID
    tab_id: TabId
    #: Write only while the row names this tab, or there is no row (``setIf``); left out,
    #: the write always lands (``set``). ``seen`` null with ``conditional``: insert only.
    conditional: bool = False
    seen: TabId | None = None


class Written(BaseModel):
    written: bool


@router.post("/session-tabs", name="crud_agent_session_tab_write")
async def session_tab_write(body: SessionTabWrite, state: StateDep) -> Written:
    def run(conn: psycopg.Connection[Any]) -> bool:
        if not body.conditional:
            sql = (
                "INSERT INTO ai_session_tabs (session_id, tab_id) VALUES (%(s)s, %(t)s)"
                " ON CONFLICT (session_id) DO UPDATE SET tab_id = EXCLUDED.tab_id,"
                " paired_at = now() RETURNING 1"
            )
        elif body.seen is None:
            sql = (
                "INSERT INTO ai_session_tabs (session_id, tab_id) VALUES (%(s)s, %(t)s)"
                " ON CONFLICT (session_id) DO NOTHING RETURNING 1"
            )
        else:
            sql = (
                "INSERT INTO ai_session_tabs (session_id, tab_id) VALUES (%(s)s, %(t)s)"
                " ON CONFLICT (session_id) DO UPDATE SET tab_id = EXCLUDED.tab_id,"
                " paired_at = now() WHERE ai_session_tabs.tab_id = %(seen)s RETURNING 1"
            )
        try:
            row = conn.execute(
                sql, {"s": body.session_id, "t": body.tab_id, "seen": body.seen}
            ).fetchone()
        except psycopg.errors.ForeignKeyViolation:
            raise ApiError(status.HTTP_404_NOT_FOUND, "no such session") from None
        return row is not None

    return Written(written=await _in_tx(state, run))


class SessionTab(BaseModel):
    tab_id: str | None


@router.get("/session-tabs", name="crud_agent_session_tab")
async def session_tab(session_id: UUID, state: StateDep) -> SessionTab:
    def run(conn: psycopg.Connection[Any]) -> str | None:
        row = conn.execute(
            "SELECT tab_id FROM ai_session_tabs WHERE session_id = %s", (session_id,)
        ).fetchone()
        return None if row is None else str(row["tab_id"])

    return SessionTab(tab_id=await _in_tx(state, run))


class TabClaim(_Body):
    tab_id: TabId
    queue: Queue


class TabRefresh(_Body):
    tab_ids: list[TabId] = Field(max_length=TABS_MAX)
    queue: Queue


class TabRelease(_Body):
    tab_id: TabId
    queue: Queue
    #: Only a row not claimed or refreshed after this moment, as Postgres's own text
    #: (``claimed_at`` as `tab_owner` answered it, to the microsecond).
    claimed_before: str | None = Field(default=None, max_length=64)


class QueueOnly(_Body):
    queue: Queue


@router.post("/tab-owners/claim", name="crud_agent_tab_claim")
async def tab_claim(body: TabClaim, state: StateDep) -> Ok:
    await _in_tx(
        state,
        lambda conn: conn.execute(
            "INSERT INTO ai_tab_owners (tab_id, queue) VALUES (%s, %s)"
            " ON CONFLICT (tab_id) DO UPDATE SET queue = EXCLUDED.queue, claimed_at = now()",
            (body.tab_id, body.queue),
        ),
    )
    return Ok()


@router.post("/tab-owners/refresh", name="crud_agent_tab_refresh")
async def tab_refresh(body: TabRefresh, state: StateDep) -> Ok:
    if not body.tab_ids:
        return Ok()
    await _in_tx(
        state,
        lambda conn: conn.execute(
            "INSERT INTO ai_tab_owners (tab_id, queue)"
            " SELECT tab_id, %s FROM unnest(%s::text[]) AS t (tab_id)"
            " ON CONFLICT (tab_id) DO UPDATE SET claimed_at = now()"
            " WHERE ai_tab_owners.queue = EXCLUDED.queue",
            (body.queue, body.tab_ids),
        ),
    )
    return Ok()


@router.post("/tab-owners/release", name="crud_agent_tab_release")
async def tab_release(body: TabRelease, state: StateDep) -> Ok:
    def run(conn: psycopg.Connection[Any]) -> None:
        if body.claimed_before is None:
            conn.execute(
                "DELETE FROM ai_tab_owners WHERE tab_id = %s AND queue = %s",
                (body.tab_id, body.queue),
            )
        else:
            conn.execute(
                "DELETE FROM ai_tab_owners WHERE tab_id = %s AND queue = %s"
                " AND claimed_at <= %s::text::timestamptz",
                (body.tab_id, body.queue, body.claimed_before),
            )

    try:
        await _in_tx(state, run)
    except psycopg.errors.DataError:
        raise ApiError(422, "claimed_before is not a timestamp") from None
    return Ok()


@router.post("/tab-owners/release-all", name="crud_agent_tab_release_all")
async def tab_release_all(body: QueueOnly, state: StateDep) -> Ok:
    await _in_tx(
        state,
        lambda conn: conn.execute("DELETE FROM ai_tab_owners WHERE queue = %s", (body.queue,)),
    )
    return Ok()


class TabOwner(BaseModel):
    queue: str
    #: Postgres's own text, to the microsecond.
    claimed_at: str


class TabOwnerAnswer(BaseModel):
    owner: TabOwner | None


@router.get("/tab-owners", name="crud_agent_tab_owner")
async def tab_owner(
    tab_id: Annotated[str, Query(min_length=1, max_length=TAB_ID_MAX)], state: StateDep
) -> TabOwnerAnswer:
    def run(conn: psycopg.Connection[Any]) -> Any:
        return conn.execute(
            "SELECT queue, claimed_at::text AS claimed_at FROM ai_tab_owners WHERE tab_id = %s",
            (tab_id,),
        ).fetchone()

    row = await _in_tx(state, run)
    return TabOwnerAnswer(
        owner=None if row is None else TabOwner(queue=row["queue"], claimed_at=row["claimed_at"])
    )


ImageName = Annotated[str, Path(pattern=TAB_IMAGE_NAME)]


@router.put(
    "/tab-images/{name}", status_code=status.HTTP_204_NO_CONTENT, name="crud_agent_tab_image_put"
)
async def tab_image_put(name: ImageName, request: Request, state: StateDep) -> None:
    media_type = TAB_IMAGE_TYPES[name.rsplit(".", 1)[1]]
    if request.headers.get("content-type", "").split(";")[0].strip() != media_type:
        raise ApiError(422, f"a .{name.rsplit('.', 1)[1]} image is sent as {media_type}")
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > TAB_IMAGE_BYTES_MAX:
        raise ApiError(413, "the image is larger than a tab image may be")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > TAB_IMAGE_BYTES_MAX:
            raise ApiError(413, "the image is larger than a tab image may be")
    if not data or hashlib.sha256(data).hexdigest() != name.split(".", 1)[0]:
        raise ApiError(422, "an image's name is its bytes' sha256")
    ttl = _ms(TAB_IMAGE_TTL_MS)

    def run(conn: psycopg.Connection[Any]) -> None:
        conn.execute("DELETE FROM ai_tab_images WHERE stored_at < now() - %s::interval", (ttl,))
        conn.execute(
            "INSERT INTO ai_tab_images (name, media_type, bytes) VALUES (%s, %s, %s)"
            " ON CONFLICT (name) DO UPDATE SET stored_at = now()",
            (name, media_type, bytes(data)),
        )

    await _in_tx(state, run)


@router.post(
    "/tab-images/{name}/take",
    name="crud_agent_tab_image_take",
    response_class=Response,
    responses={200: {"content": {"application/octet-stream": {}}}, 204: {}},
)
async def tab_image_take(name: ImageName, state: StateDep) -> Response:
    """The image's bytes, deleted as they are read (its media type is the answer's); 204 for
    none, or one past its time. The expired rows go with it."""
    ttl = _ms(TAB_IMAGE_TTL_MS)

    def run(conn: psycopg.Connection[Any]) -> list[Any]:
        return conn.execute(
            "DELETE FROM ai_tab_images WHERE name = %s OR stored_at < now() - %s::interval"
            " RETURNING media_type, bytes,"
            " (name = %s AND stored_at >= now() - %s::interval) AS fresh",
            (name, ttl, name, ttl),
        ).fetchall()

    row = next((r for r in await _in_tx(state, run) if r["fresh"]), None)
    if row is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return Response(content=bytes(row["bytes"]), media_type=row["media_type"])


# -- pairings -----------------------------------------------------------------------------


class Pairing(BaseModel):
    id: str
    label: str
    expires_at: datetime


class PairedTab(Pairing):
    tab_id: str


class PairingAsk(_Body):
    principal_kind: PrincipalKind
    principal_id: str = Field(min_length=1, max_length=500)
    #: What the tab shows when the principal is not a named token (the agent's own words:
    #: a short prefix and the principal's id, or a client address).
    fallback_label: str = Field(min_length=1, max_length=FALLBACK_LABEL_MAX)
    code_hash: Hash


def _pairing(row: Any) -> Pairing:
    return Pairing(id=str(row["id"]), label=row["principal_label"], expires_at=row["expires_at"])


def _token_label(conn: psycopg.Connection[Any], principal: PairingAsk) -> str:
    """A bearer principal is named after its token (``token:<uuid>``)."""
    if principal.principal_kind == "bearer" and principal.principal_id.startswith("token:"):
        try:
            token_id = UUID(principal.principal_id.removeprefix("token:"))
        except ValueError:
            return principal.fallback_label
        row = conn.execute("SELECT name FROM ai_mcp_tokens WHERE id = %s", (token_id,)).fetchone()
        if row is not None:
            return f"MCP token “{row['name']}”"
    return principal.fallback_label


@router.post("/pairings", status_code=status.HTTP_201_CREATED, name="crud_agent_pairing_request")
async def pairing_request(body: PairingAsk, state: StateDep) -> Pairing:
    def sweep(conn: psycopg.Connection[Any]) -> None:
        # Rows past their use a day ago go, so the table stays small without a sweeper.
        conn.execute(
            "DELETE FROM ai_browser_pairings WHERE expires_at < now() - interval '1 day'"
            " OR (status IN ('denied', 'ended')"
            " AND COALESCE(ended_at, created_at) < now() - interval '1 day')"
        )

    def run(conn: psycopg.Connection[Any]) -> Pairing:
        label = _token_label(conn, body)
        # Count and insert under one transaction-scoped advisory lock, so two concurrent
        # requests cannot both read the counts below a cap and both insert (#731 review).
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (PAIRING_REQUEST_LOCK,)
        )
        counts = conn.execute(
            "SELECT count(*) FILTER (WHERE principal_kind = %s AND principal_id = %s)::int"
            " AS mine, count(*)::int AS all FROM ai_browser_pairings"
            " WHERE status = 'pending' AND expires_at > now()",
            (body.principal_kind, body.principal_id),
        ).fetchone()
        mine, every = (counts["mine"], counts["all"]) if counts else (0, 0)
        if mine >= PAIRING_MAX_PENDING_PER_PRINCIPAL:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"this caller already has {PAIRING_MAX_PENDING_PER_PRINCIPAL} pairing requests"
                " waiting; ask the user to answer one, or wait for them to expire",
                reason=PAIRING_REFUSED,
            )
        if every >= PAIRING_MAX_PENDING:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "too many pairing requests are waiting for the user; try again in a few minutes",
                reason=PAIRING_REFUSED,
            )
        row = conn.execute(
            "INSERT INTO ai_browser_pairings"
            " (id, principal_kind, principal_id, principal_label, code_hash, status, expires_at)"
            " VALUES (%s, %s, %s, %s, %s, 'pending', now() + %s::interval)"
            " RETURNING id, principal_label, expires_at",
            (
                uuid4(),
                body.principal_kind,
                body.principal_id,
                label,
                body.code_hash,
                _ms(PAIRING_REQUEST_TTL_MS),
            ),
        ).fetchone()
        return _pairing(row)

    await _in_tx(state, sweep)
    return await _in_tx(state, run)


class PairingAccept(_Body):
    code_hash: Hash
    tab_id: TabId


class Accepted(BaseModel):
    result: Literal["ok", "wrong_code", "gone"]
    pairing: Pairing | None = None
    principal_kind: str | None = None
    principal_id: str | None = None
    attempts_left: int | None = None


@router.post("/pairings/{pairing_id}/accept", name="crud_agent_pairing_accept")
async def pairing_accept(pairing_id: UUID, body: PairingAccept, state: StateDep) -> Accepted:
    def run(conn: psycopg.Connection[Any]) -> Accepted:
        row = conn.execute(
            "SELECT id, principal_kind, principal_id, principal_label, code_hash, attempts,"
            " expires_at FROM ai_browser_pairings"
            " WHERE id = %s AND status = 'pending' AND expires_at > now() FOR UPDATE",
            (pairing_id,),
        ).fetchone()
        if row is None:
            return Accepted(result="gone")
        if not hmac.compare_digest(body.code_hash.encode(), str(row["code_hash"]).encode()):
            attempts = int(row["attempts"]) + 1
            denied = attempts >= PAIRING_MAX_ATTEMPTS
            conn.execute(
                "UPDATE ai_browser_pairings SET attempts = %s,"
                " status = CASE WHEN %s THEN 'denied' ELSE status END,"
                " ended_at = CASE WHEN %s THEN now() ELSE ended_at END WHERE id = %s",
                (attempts, denied, denied, pairing_id),
            )
            if denied:
                return Accepted(result="gone")
            return Accepted(result="wrong_code", attempts_left=PAIRING_MAX_ATTEMPTS - attempts)
        # A newer pairing replaces the principal's old one (one tab per principal).
        conn.execute(
            "UPDATE ai_browser_pairings SET status = 'ended', ended_at = now()"
            " WHERE principal_kind = %s AND principal_id = %s AND status = 'paired'",
            (row["principal_kind"], row["principal_id"]),
        )
        paired = conn.execute(
            "UPDATE ai_browser_pairings SET status = 'paired', tab_id = %s, paired_at = now(),"
            " expires_at = now() + %s::interval WHERE id = %s"
            " RETURNING id, principal_label, expires_at",
            (body.tab_id, _ms(PAIRING_PAIRED_TTL_MS), pairing_id),
        ).fetchone()
        return Accepted(
            result="ok",
            pairing=_pairing(paired),
            principal_kind=row["principal_kind"],
            principal_id=row["principal_id"],
        )

    return await _in_tx(state, run)


@router.post("/pairings/{pairing_id}/deny", name="crud_agent_pairing_deny")
async def pairing_deny(pairing_id: UUID, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> bool:
        return (
            conn.execute(
                "UPDATE ai_browser_pairings SET status = 'denied', ended_at = now()"
                " WHERE id = %s AND status = 'pending' RETURNING id",
                (pairing_id,),
            ).fetchone()
            is not None
        )

    return Done(done=await _in_tx(state, run))


class PairingEnd(_Body):
    tab_id: TabId


@router.post("/pairings/{pairing_id}/end", name="crud_agent_pairing_end")
async def pairing_end(pairing_id: UUID, body: PairingEnd, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> bool:
        return (
            conn.execute(
                "UPDATE ai_browser_pairings SET status = 'ended', ended_at = now()"
                " WHERE id = %s AND status = 'paired' AND tab_id = %s RETURNING id",
                (pairing_id, body.tab_id),
            ).fetchone()
            is not None
        )

    return Done(done=await _in_tx(state, run))


class PairedTabAnswer(BaseModel):
    pairing: PairedTab | None


@router.get("/pairings/paired-tab", name="crud_agent_pairing_paired_tab")
async def pairing_paired_tab(
    principal_kind: PrincipalKind,
    principal_id: Annotated[str, Query(min_length=1, max_length=500)],
    state: StateDep,
) -> PairedTabAnswer:
    def run(conn: psycopg.Connection[Any]) -> Any:
        return conn.execute(
            "SELECT id, principal_label, expires_at, tab_id FROM ai_browser_pairings"
            " WHERE principal_kind = %s AND principal_id = %s"
            " AND status = 'paired' AND expires_at > now()",
            (principal_kind, principal_id),
        ).fetchone()

    row = await _in_tx(state, run)
    if row is None:
        return PairedTabAnswer(pairing=None)
    return PairedTabAnswer(
        pairing=PairedTab(
            id=str(row["id"]),
            label=row["principal_label"],
            expires_at=row["expires_at"],
            tab_id=row["tab_id"],
        )
    )


class Pairings(BaseModel):
    pairings: list[Pairing]


@router.get("/pairings/pending", name="crud_agent_pairings_pending")
async def pairings_pending(state: StateDep) -> Pairings:
    def run(conn: psycopg.Connection[Any]) -> list[Any]:
        return conn.execute(
            "SELECT id, principal_label, expires_at FROM ai_browser_pairings"
            " WHERE status = 'pending' AND expires_at > now() ORDER BY created_at, id"
        ).fetchall()

    return Pairings(pairings=[_pairing(r) for r in await _in_tx(state, run)])


class TabIds(_Body):
    tab_ids: list[TabId] = Field(max_length=TABS_MAX)


class PairedWith(BaseModel):
    #: Each live pairing with its tab, oldest pairing first.
    pairings: list[PairedTab]


@router.post("/pairings/paired-with", name="crud_agent_pairings_paired_with")
async def pairings_paired_with(body: TabIds, state: StateDep) -> PairedWith:
    if not body.tab_ids:
        return PairedWith(pairings=[])

    def run(conn: psycopg.Connection[Any]) -> list[Any]:
        return conn.execute(
            "SELECT id, principal_label, expires_at, tab_id FROM ai_browser_pairings"
            " WHERE status = 'paired' AND expires_at > now() AND tab_id = ANY(%s)"
            " ORDER BY paired_at, id",
            (body.tab_ids,),
        ).fetchall()

    rows = await _in_tx(state, run)
    return PairedWith(
        pairings=[
            PairedTab(
                id=str(r["id"]),
                label=r["principal_label"],
                expires_at=r["expires_at"],
                tab_id=r["tab_id"],
            )
            for r in rows
        ]
    )


# -- MCP tokens ---------------------------------------------------------------------------


class TokenVerify(_Body):
    token_hash: Hash


class VerifiedToken(BaseModel):
    id: str
    tier: Tier


class Verified(BaseModel):
    token: VerifiedToken | None


@router.post("/mcp-tokens/verify", name="crud_agent_mcp_token_verify")
async def mcp_token_verify(body: TokenVerify, state: StateDep) -> Verified:
    """Finds, checks and stamps the token in one statement, by its hash's unique index: a
    token revoked or expired by the time the row lock is taken does not verify, and
    ``last_used_at`` only moves forward."""

    def run(conn: psycopg.Connection[Any]) -> Any:
        return conn.execute(
            "UPDATE ai_mcp_tokens SET last_used_at = GREATEST(last_used_at, now())"
            " WHERE token_hash = %s AND revoked_at IS NULL"
            " AND (expires_at IS NULL OR expires_at > now()) RETURNING id, tier",
            (body.token_hash,),
        ).fetchone()

    row = await _in_tx(state, run)
    return Verified(
        token=None if row is None else VerifiedToken(id=str(row["id"]), tier=row["tier"])
    )


class TokenMint(_Body):
    name: str = Field(min_length=1, max_length=200)
    tier: Tier
    token_hash: Hash
    expires_at: datetime | None = None
    approval_grant: bool = False


class TokenRecord(BaseModel):
    id: str
    name: str
    tier: Tier
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    approval_grant: bool


class TokenAnswer(BaseModel):
    token: TokenRecord


_TOKEN_COLUMNS = (
    "id::text AS id, name, tier, created_at, expires_at, revoked_at, last_used_at, approval_grant"
)


@router.post("/mcp-tokens", status_code=status.HTTP_201_CREATED, name="crud_agent_mcp_token_mint")
async def mcp_token_mint(body: TokenMint, state: StateDep) -> TokenAnswer:
    if body.approval_grant and body.tier != "outward":
        raise ApiError(
            422,
            "an approval grant needs an outward token: deciding an outward action is at least"
            " as much as taking one",
        )

    def run(conn: psycopg.Connection[Any]) -> Any:
        return conn.execute(
            "INSERT INTO ai_mcp_tokens (id, name, tier, token_hash, expires_at, approval_grant)"
            f" VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_TOKEN_COLUMNS}",
            (uuid4(), body.name, body.tier, body.token_hash, body.expires_at, body.approval_grant),
        ).fetchone()

    try:
        row = await _in_tx(state, run)
    except psycopg.errors.UniqueViolation:
        raise ApiError(status.HTTP_409_CONFLICT, "a token with this hash exists") from None
    return TokenAnswer(token=TokenRecord.model_validate(row))


@router.post("/mcp-tokens/{token_id}/revoke", name="crud_agent_mcp_token_revoke")
async def mcp_token_revoke(token_id: UUID, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> bool:
        return (
            conn.execute(
                "UPDATE ai_mcp_tokens SET revoked_at = now()"
                " WHERE id = %s AND revoked_at IS NULL RETURNING id",
                (token_id,),
            ).fetchone()
            is not None
        )

    return Done(done=await _in_tx(state, run))


class TokenList(BaseModel):
    tokens: list[TokenRecord]


@router.get("/mcp-tokens", name="crud_agent_mcp_tokens")
async def mcp_tokens(state: StateDep) -> TokenList:
    def run(conn: psycopg.Connection[Any]) -> list[Any]:
        return conn.execute(
            f"SELECT {_TOKEN_COLUMNS} FROM ai_mcp_tokens ORDER BY created_at, id"
        ).fetchall()

    return TokenList(tokens=[TokenRecord.model_validate(r) for r in await _in_tx(state, run)])


class LiveToken(BaseModel):
    #: The live (unrevoked, unexpired) token's tier; None for none.
    tier: Tier | None
    #: Whether it holds the approval grant (an outward token's only).
    approval_grant: bool


@router.get("/mcp-tokens/{token_id}/live", name="crud_agent_mcp_token_live")
async def mcp_token_live(token_id: UUID, state: StateDep) -> LiveToken:
    def run(conn: psycopg.Connection[Any]) -> Any:
        return conn.execute(
            "SELECT tier, approval_grant AND tier = 'outward' AS grant FROM ai_mcp_tokens"
            " WHERE id = %s AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())",
            (token_id,),
        ).fetchone()

    row = await _in_tx(state, run)
    if row is None:
        return LiveToken(tier=None, approval_grant=False)
    return LiveToken(tier=row["tier"], approval_grant=bool(row["grant"]))
