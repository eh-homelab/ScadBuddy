"""A session's row and what its tool calls touched behind CRUD routes (#5299 step 9, part a):
under ``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`).
Each route is one method of the agent's store and runs its SQL, a transaction as one
transaction (#5299 D2); the SQL is in ``agent_data/sessions.py``. The agent's stores
(``agent/src/sessions/rows.ts`` `CrudSessionRows`, ``agent/src/sessions/touched.ts``
`CrudResourceRows`) call these routes once ``…/areas`` lists the area, and their own SQL
until then (D9).

Area ``sessions``, the ``ai_sessions`` row as the session manager reads and writes it:

- ``GET …/sessions/{id}``: the row, with what a turn's claim reads (``session: null`` for none);
- ``POST …/sessions/list``: a principal's sessions, filtered, newest first;
- ``GET …/session-starts``: how many sessions an owner started in a window;
- ``POST …/sessions``: a new row, under the owner's start lock (``rate_limited``) and, for a
  fork, its parent's lock (``forgotten``);
- ``POST …/sessions/{id}/agent``: a Workflow agent's chat's model, effort and subagent;
- ``POST …/sessions/{id}/offer``, ``…/offer/cancel`` and ``…/owner``: a handoff, each
  conditional on what the agent read (``changed: false`` otherwise);
- ``POST …/sessions/{id}/budget``: a raise of the lineage's budget (``budget_usd: null`` when
  refused);
- ``POST …/sessions/{id}/discard``: a fork that failed before it logged anything.

Area ``session-resources``, ``ai_session_resources``: ``POST …/sessions/{id}/resources`` a
call's rows (``recorded: false`` for a session that is gone) and ``GET`` the session's.

The session id is the only id placed in a path, a UUID checked by its pattern; an owner's id
travels in the body or the query. Every body is bounded by the TypeScript limits it carries.
No route takes an ``Idempotency-Key``: a read, a conditional write, a budget raise the agent
never sends again after a lost answer (it reads the row instead), and an insert whose id the
agent mints, which a repeat refuses as a duplicate key.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Annotated, Any, Literal

import psycopg
from fastapi import Depends, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from scadbuddy.agent_data import sessions
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router

router = crud_router("agent")

#: A session's id: a UUID.
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
#: The longest owner id or label (the agent's principals: a token's id, an OIDC subject).
NAME_MAX = 512
#: The longest title kept. A person's or a tool's title is at most 200 characters
#: (``routes/sessions.ts``, ``tools/sessions.ts``) and a prompt's 80 (``TITLE_MAX``); a fork
#: adds `` (fork)`` to its parent's, so a chain of forks grows it, and this leaves room for
#: hundreds.
TITLE_MAX = 4096
#: Tags: a tool gives at most 10 of 50 characters (``tools/sessions.ts``), a Workflow agent's
#: chat one; this leaves room for rows from before those bounds, which a fork copies.
TAGS_MAX = 64
TAG_MAX = 200
#: A session's scope, as JSON: a tool's is three ids (``tools/sessions.ts``).
SCOPE_BYTES_MAX = 64 * 1024
#: The largest JSON body any route here takes: an insert's with its scope and tags.
BODY_BYTES_MAX = 160 * 1024
#: A list's filters (``ListFilter``): a search, the kinds a file input takes (a picker's
#: ``accept``, ``routes/workspacePicker.ts``), a resource's id.
SEARCH_MAX = 500
#: A picker's ``accept`` is at most 1000 characters (``ACCEPT_MAX`` there), so at most 334
#: entries of ``.a`` and a comma; an entry is at most 192 characters (its ``ACCEPT_ENTRY``).
ACCEPT_MAX = 500
ACCEPT_ENTRY_MAX = 200
#: The most rows one call records (``touched.ts`` ``MAX_TOUCHES_PER_CALL``), each id cut to
#: ``ID_MAX`` (300) UTF-16 units there, which is never more than 300 characters here.
RESOURCES_MAX = 100
RESOURCE_ID_MAX = 300
TOOL_MAX = 300
#: A budget at most (``manager.ts`` ``MAX_SESSION_BUDGET_USD``), and the most a raise adds.
BUDGET_MAX_USD = 100.0
#: The most turns a session gets (``ai_settings`` ``session_max_turns``, a positive integer).
MAX_TURNS_MAX = 1_000_000
#: The new-session window and count (``manager.ts`` ``MAX_NEW_SESSIONS``,
#: ``NEW_SESSION_WINDOW_MS``; a test passes others).
WINDOW_MS_MAX = 24 * 3600 * 1000
LIMIT_MAX = 10_000
#: A handoff offer's life (``HANDOFF_OFFER_TTL_MS``, an hour; a test passes less).
OFFER_TTL_MS_MAX = 7 * 24 * 3600 * 1000

SessionIdPath = Annotated[str, Path(pattern=UUID_PATTERN)]
OwnerKind = Literal["browser", "bearer", "oidc", "anonymous", "flow"]
Origin = Literal["chat", "mcp", "analyzer", "hook"]
Status = Literal["running", "waiting_input", "waiting_approval", "idle", "done", "failed"]
ResourceType = Literal[
    "model",
    "revision",
    "preset",
    "asset",
    "render_job",
    "output",
    "print_run",
    "print",
    "library",
    "font",
    "setting",
    "project",
    "bambuddy_file",
    "print_archive",
    "library_file",
    "unclassified",
]


async def _bounded(request: Request) -> None:
    """A JSON body names its length, at most ``BODY_BYTES_MAX``: refused before it is read."""
    if request.method != "POST":
        return
    length = request.headers.get("content-length")
    if length is None or not length.isdigit():
        raise ApiError(status.HTTP_411_LENGTH_REQUIRED, "a body names its Content-Length")
    if int(length) > BODY_BYTES_MAX:
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE, f"a body is at most {BODY_BYTES_MAX} bytes"
        )


Bounded = Depends(_bounded)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _in_conn[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    def run() -> T:
        with state.settings_store.pool.connection() as conn:
            return fn(conn)

    return await asyncio.to_thread(run)


class Who(_Body):
    """A principal as a condition: its kind and id."""

    kind: OwnerKind
    id: str = Field(min_length=1, max_length=NAME_MAX)


class Owner(Who):
    """A principal as the row records it (the agent's ``Owner``)."""

    label: str = Field(max_length=NAME_MAX)


class SessionRow(BaseModel):
    """The row as the session manager reads it (``rows.ts`` ``COLUMNS``); timestamps are
    ISO 8601 in UTC at milliseconds."""

    id: str
    origin: str
    owner_kind: str
    owner_id: str
    owner_label: str
    creator_kind: str
    creator_id: str
    offer_kind: str | None
    offer_id: str | None
    offer_label: str | None
    offer_until: str | None
    status: str
    title: str
    tags: list[str]
    scope: dict[str, Any]
    parent_id: str | None
    runtime: str | None
    max_turns: int
    budget_usd: float
    cost_usd: float
    pool_cost_usd: float | None
    turns: int
    turn_active: bool | None
    created_at: str
    updated_at: str
    archived_at: str | None


class FullRow(SessionRow):
    """With what a turn's claim reads (``ClaimRow``): ``claimed_at`` at microseconds."""

    unpriced_cost_usd: float
    agent_model: str | None
    agent_effort: str | None
    agent_type: str | None
    background_tools: bool
    turn_flow: bool
    turn_tiers: dict[str, Any] | None
    durable_turn_id: str | None
    claimed_at: str


class Count(BaseModel):
    count: int


class OneSession(BaseModel):
    session: FullRow | None


class SessionList(BaseModel):
    sessions: list[SessionRow]


@router.get("/session-starts", name="crud_agent_session_starts")
async def started_recently(
    state: StateDep,
    owner_kind: OwnerKind,
    owner_id: Annotated[str, Query(min_length=1, max_length=NAME_MAX)],
    window_ms: Annotated[int, Query(ge=1, le=WINDOW_MS_MAX)],
) -> Count:
    count = await _in_conn(
        state, lambda conn: sessions.started_recently(conn, (owner_kind, owner_id), window_ms)
    )
    return Count(count=count)


@router.get("/sessions/{session_id}", name="crud_agent_session")
async def get_session(session_id: SessionIdPath, state: StateDep) -> OneSession:
    row = await _in_conn(state, lambda conn: sessions.get(conn, session_id))
    return OneSession(session=None if row is None else FullRow.model_validate(row))


class ResourceRef(_Body):
    type: ResourceType
    id: str = Field(min_length=1, max_length=RESOURCE_ID_MAX)


class ListBody(_Body):
    #: The principal whose sessions: none for the browser user, who sees every one.
    principal: Who | None
    status: Status | None = None
    origin: Origin | None = None
    archived: Literal["exclude", "only", "include"] = "exclude"
    resource: ResourceRef | None = None
    search: str | None = Field(default=None, max_length=SEARCH_MAX)
    accept: list[Annotated[str, Field(max_length=ACCEPT_ENTRY_MAX)]] = Field(
        default_factory=list, max_length=ACCEPT_MAX
    )
    with_file: bool = False
    limit: int = Field(default=100, ge=1, le=500)


@router.post("/sessions/list", name="crud_agent_sessions_list", dependencies=[Bounded])
async def list_sessions(body: ListBody, state: StateDep) -> SessionList:
    principal = None if body.principal is None else (body.principal.kind, body.principal.id)
    rows = await _in_conn(
        state,
        lambda conn: sessions.list_sessions(
            conn,
            principal,
            status=body.status,
            origin=body.origin,
            archived=body.archived,
            resource=None if body.resource is None else body.resource.model_dump(),
            search=body.search,
            accept=body.accept,
            with_file=body.with_file,
            limit=body.limit,
        ),
    )
    return SessionList(sessions=[SessionRow.model_validate(r) for r in rows])


class ForkOf(_Body):
    parent_id: str = Field(pattern=UUID_PATTERN)
    cut: int = Field(ge=0, le=2**31 - 1)


class NewSessionLimit(_Body):
    max: int = Field(ge=1, le=LIMIT_MAX)
    window_ms: int = Field(ge=1, le=WINDOW_MS_MAX)


class NewSession(_Body):
    id: str = Field(pattern=UUID_PATTERN)
    origin: Origin
    owner: Owner
    title: str = Field(max_length=TITLE_MAX)
    tags: list[Annotated[str, Field(max_length=TAG_MAX)]] = Field(max_length=TAGS_MAX)
    scope: dict[str, Any]
    parent_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    runtime: Literal["durable", "unified"] | None = None
    max_turns: int = Field(ge=1, le=MAX_TURNS_MAX)
    budget_usd: float = Field(gt=0, le=BUDGET_MAX_USD)
    #: The session whose lineage's budget this one spends from (a fork, a Workflow agent's chat).
    budget_of: str | None = Field(default=None, pattern=UUID_PATTERN)
    fork_of: ForkOf | None = None
    #: False: none; left out, the parent's, for a session with one, else true (#5870).
    background_tools: Literal[False] | None = None
    #: Counted under the owner's start lock: refused ``rate_limited`` at ``max`` in the window.
    limit: NewSessionLimit | None = None

    @field_validator("scope")
    @classmethod
    def _small_scope(cls, scope: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(scope).encode()) > SCOPE_BYTES_MAX:
            raise ValueError(f"a scope is at most {SCOPE_BYTES_MAX} bytes as JSON")
        return scope


@router.post("/sessions", name="crud_agent_session_create", dependencies=[Bounded])
async def create_session(body: NewSession, state: StateDep) -> OneSession:
    def run(conn: psycopg.Connection[Any]) -> tuple[str | None, dict[str, Any] | None]:
        refused = sessions.insert(
            conn,
            session_id=body.id,
            origin=body.origin,
            owner=body.owner.model_dump(),
            title=body.title,
            tags=body.tags,
            scope=body.scope,
            parent_id=body.parent_id,
            runtime=body.runtime,
            max_turns=body.max_turns,
            budget_usd=body.budget_usd,
            budget_of=body.budget_of,
            fork_of=None if body.fork_of is None else (body.fork_of.parent_id, body.fork_of.cut),
            background_tools=body.background_tools,
            limit=None if body.limit is None else (body.limit.max, body.limit.window_ms),
        )
        return refused, None if refused else sessions.get(conn, body.id)

    try:
        refused, row = await _in_conn(state, run)
    except psycopg.errors.UniqueViolation:
        raise ApiError(
            status.HTTP_409_CONFLICT, f"session {body.id} exists", reason="exists"
        ) from None
    except psycopg.errors.ForeignKeyViolation:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, "the parent session is gone", reason="parent_gone"
        ) from None
    if refused == "rate_limited":
        raise ApiError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "the owner has started too many sessions in the window",
            reason="rate_limited",
        )
    if refused == "forgotten":
        raise ApiError(
            status.HTTP_404_NOT_FOUND, "the parent session was forgotten", reason="forgotten"
        )
    return OneSession(session=None if row is None else FullRow.model_validate(row))


class AgentSettings(_Body):
    """A Workflow agent's chat's own (the columns' CHECKs)."""

    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._:\[\]-]{1,100}$")
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    type: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")


class Done(BaseModel):
    ok: Literal[True] = True


@router.post(
    "/sessions/{session_id}/agent", name="crud_agent_session_agent", dependencies=[Bounded]
)
async def set_agent(session_id: SessionIdPath, body: AgentSettings, state: StateDep) -> Done:
    await _in_conn(
        state, lambda conn: sessions.set_agent(conn, session_id, body.model, body.effort, body.type)
    )
    return Done()


class Changed(BaseModel):
    #: False when the row was not as the condition read it.
    changed: bool


class Offer(_Body):
    #: The owner the agent read: the offer applies only while it still owns the session.
    owner: Who
    to: Owner
    ttl_ms: int = Field(ge=1, le=OFFER_TTL_MS_MAX)


@router.post(
    "/sessions/{session_id}/offer", name="crud_agent_session_offer", dependencies=[Bounded]
)
async def offer(session_id: SessionIdPath, body: Offer, state: StateDep) -> Changed:
    changed = await _in_conn(
        state,
        lambda conn: sessions.offer(
            conn, session_id, (body.owner.kind, body.owner.id), body.to.model_dump(), body.ttl_ms
        ),
    )
    return Changed(changed=changed)


class CancelOffer(_Body):
    #: The principal the live offer is to.
    to: Who


@router.post(
    "/sessions/{session_id}/offer/cancel",
    name="crud_agent_session_offer_cancel",
    dependencies=[Bounded],
)
async def cancel_offer(session_id: SessionIdPath, body: CancelOffer, state: StateDep) -> Changed:
    changed = await _in_conn(
        state, lambda conn: sessions.cancel_offer(conn, session_id, (body.to.kind, body.to.id))
    )
    return Changed(changed=changed)


class Transfer(_Body):
    owner: Who
    to: Owner
    #: Only while the live offer is to ``to``.
    accepting: bool = False


@router.post(
    "/sessions/{session_id}/owner", name="crud_agent_session_owner", dependencies=[Bounded]
)
async def transfer(session_id: SessionIdPath, body: Transfer, state: StateDep) -> Changed:
    changed = await _in_conn(
        state,
        lambda conn: sessions.transfer(
            conn,
            session_id,
            (body.owner.kind, body.owner.id),
            body.to.model_dump(),
            body.accepting,
        ),
    )
    return Changed(changed=changed)


class Raise(_Body):
    #: Who raises it: the session's owner, or the raise is refused.
    by: Who
    add_usd: float = Field(gt=0, le=BUDGET_MAX_USD)
    max_usd: float = Field(gt=0, le=BUDGET_MAX_USD)


class Raised(BaseModel):
    #: The lineage's budget after the raise, or null when refused (not the owner, or past
    #: ``max_usd``).
    budget_usd: float | None


@router.post(
    "/sessions/{session_id}/budget", name="crud_agent_session_budget", dependencies=[Bounded]
)
async def raise_budget(session_id: SessionIdPath, body: Raise, state: StateDep) -> Raised:
    after = await _in_conn(
        state,
        lambda conn: sessions.raise_budget(
            conn, session_id, (body.by.kind, body.by.id), body.add_usd, body.max_usd
        ),
    )
    return Raised(budget_usd=after)


class Discarded(BaseModel):
    #: False when there was no such session, or it has logged an event.
    discarded: bool


@router.post("/sessions/{session_id}/discard", name="crud_agent_session_discard")
async def discard(session_id: SessionIdPath, state: StateDep) -> Discarded:
    return Discarded(
        discarded=await _in_conn(state, lambda conn: sessions.discard(conn, session_id))
    )


# --- area session-resources ---------------------------------------------------------------


class Touch(_Body):
    tool: str = Field(min_length=1, max_length=TOOL_MAX)
    resource_type: ResourceType
    resource_id: str | None = Field(max_length=RESOURCE_ID_MAX)
    action: Literal["created", "modified", "deleted"]
    model_slug: str | None = Field(max_length=RESOURCE_ID_MAX)
    before_id: str | None = Field(max_length=RESOURCE_ID_MAX)
    after_id: str | None = Field(max_length=RESOURCE_ID_MAX)


class Touches(_Body):
    rows: list[Touch] = Field(min_length=1, max_length=RESOURCES_MAX)


class Recorded(BaseModel):
    #: False when the session is gone.
    recorded: bool


class Touched(BaseModel):
    resource_type: str
    resource_id: str | None
    action: str
    model_slug: str | None
    before_id: str | None
    after_id: str | None
    tool: str
    at: str


class TouchedList(BaseModel):
    resources: list[Touched]


@router.post(
    "/sessions/{session_id}/resources",
    name="crud_agent_session_resources_record",
    dependencies=[Bounded],
)
async def record_resources(session_id: SessionIdPath, body: Touches, state: StateDep) -> Recorded:
    rows = [t.model_dump() for t in body.rows]
    for row in rows:
        if (row["resource_type"] == "unclassified") != (row["resource_id"] is None):
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "an unclassified row has no id, and every other row has one",
            )
    recorded = await _in_conn(state, lambda conn: sessions.record_resources(conn, session_id, rows))
    return Recorded(recorded=recorded)


@router.get("/sessions/{session_id}/resources", name="crud_agent_session_resources")
async def list_resources(session_id: SessionIdPath, state: StateDep) -> TouchedList:
    rows = await _in_conn(state, lambda conn: sessions.list_resources(conn, session_id))
    return TouchedList(resources=[Touched.model_validate(r) for r in rows])
