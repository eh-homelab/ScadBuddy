"""The agent's notification stores behind its CRUD routes (#5299 step 5, area ``notify``):
under ``/api/v1/internal/crud/agent/``, for the ``agent`` credential only (`crud_router`).

The agent's Web Push records (``agent/src/notify/pushStore.ts``) and its notification
channels (``notify/channels.ts``) hold secrets sealed under the KEK. Here the API seals and
opens them (#5299 D5), so the agent needs no KEK for them: each route is one of the store's
methods, a transaction as one transaction (D2), and the plaintext reaches the agent only
from a route that is for it (the VAPID private key, a subscription's keys, a channel's
token, for a send). Every seal uses the agent's AAD, so a row written here opens in an
agent that still reads the table over SQL with the KEK, and the other way round (#5299 D9).

- ``GET …/push/vapid`` the VAPID key pair, or none; ``POST …/push/vapid`` stores the pair
  the agent made, the first one stored winning (``ON CONFLICT DO NOTHING``), and answers
  the stored one;
- ``POST …/push/subscriptions`` stores a browser's subscription for its owner, under the
  endpoint row's ``FOR UPDATE`` (another owner's is 409 ``subscription-taken``), keeping at
  most ``SUBSCRIPTIONS_PER_OWNER``; ``POST …/push/subscriptions/remove`` forgets one;
  ``GET …/push/subscriptions`` lists them with their keys opened, skipping one the key does
  not open;
- ``POST …/push/sent`` claims telling a target of a request, once across replicas;
- ``GET …/push/waiting`` the flow grants waiting for a person, by id (#6399), which the
  agent pushes beside its own pending input; ``POST …/push/poll-lease`` lets one agent
  replica a window run that timed poll;
- ``…/notify-channels``: the list (never a token), a create and an update (the token sealed
  with the channel's id, kind and URL; an update with ``keep_token`` keeps the sealed one
  only while the kind and URL are unchanged), a delete, a channel opened for a send, the
  enabled ones opened, and a send's outcome.

The agent still checks a channel's URL (its egress rules) and a subscription's endpoint
before it calls these, and sends the notifications itself.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import status
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.agent_data.notify import VAPID_AAD
from scadbuddy.agent_data.notify import channel_aad as _channel_aad
from scadbuddy.agent_data.notify import subscription_aad as _subscription_aad
from scadbuddy.api.deps import StateDep
from scadbuddy.api.flow_triggers import GRANT_REQUESTS_MAX
from scadbuddy.core.problems import ApiError
from scadbuddy.core.secrets import (
    Envelope,
    Kek,
    SealError,
    SecretKeyError,
    open_secret,
    seal_secret,
)
from scadbuddy.crud.guard import crud_router
from scadbuddy.flow_triggers.component import TriggersDep
from scadbuddy.library.settings_store import settings_kek

router = crud_router("agent")

#: The route's reasons: no KEK on the API; an endpoint another person subscribed; a
#: channel's kind or URL moved under an update that kept its token.
KEK_MISSING = "kek-missing"
SUBSCRIPTION_TAKEN = "subscription-taken"
CHANNEL_MOVED = "channel-moved"
#: ``notify/pushStore.ts`` ``SUBSCRIPTIONS_PER_OWNER``, ``SENT_KEEP_DAYS``.
SUBSCRIPTIONS_PER_OWNER = 5
SENT_KEEP_DAYS = 2
#: A P-256 public point (65 octets) as hex, a base64url text (a private key, a
#: subscription's key), an endpoint, a channel's token.
POINT_HEX = r"^04[0-9a-f]{128}$"
B64URL = r"^[A-Za-z0-9_-]+={0,2}$"
ENDPOINT_MAX = 2048
TOKEN_MAX = 4096
ERROR_MAX = 500


def _last4(secret: str) -> str:
    """``secrets.ts`` ``last4``."""
    return "" if len(secret) < 12 else secret[-4:]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _kek(state: Any) -> Kek:
    try:
        kek = settings_kek(state.settings.secret_key_file)
    except SecretKeyError:
        kek = None
    if kek is None:
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The API has no key-encryption key (SCADBUDDY_SECRET_KEY_FILE).",
            reason=KEK_MISSING,
        )
    return kek


async def _in_tx[T](state: Any, fn: Callable[[psycopg.Connection[Any]], T]) -> T:
    def run() -> T:
        with state.settings_store.pool.connection() as conn, conn.transaction():
            return fn(conn)

    return await asyncio.to_thread(run)


def _envelope(row: Any) -> Envelope:
    return Envelope(
        secret_sealed=bytes(row["secret_sealed"]),
        dek_sealed=bytes(row["dek_sealed"]),
        kek_id=row["kek_id"],
    )


class Done(BaseModel):
    done: bool


# -- Web Push -----------------------------------------------------------------------------


class Vapid(BaseModel):
    #: The uncompressed point, hex.
    public_key: str
    #: The private key as the agent sealed it: base64url text.
    private_key: str


class VapidAnswer(BaseModel):
    vapid: Vapid | None


def _read_vapid(conn: psycopg.Connection[Any], kek: Kek) -> Vapid | None:
    row = conn.execute(
        "SELECT public_key, secret_sealed, dek_sealed, kek_id FROM ai_push_vapid WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    try:
        private = open_secret(kek, _envelope(row), VAPID_AAD)
    except SealError as e:
        raise ApiError(status.HTTP_409_CONFLICT, f"the VAPID key does not open: {e}") from None
    return Vapid(public_key=bytes(row["public_key"]).hex(), private_key=private)


@router.get("/push/vapid", name="crud_agent_push_vapid")
async def push_vapid(state: StateDep) -> VapidAnswer:
    kek = _kek(state)
    return VapidAnswer(vapid=await _in_tx(state, lambda conn: _read_vapid(conn, kek)))


class VapidMade(_Body):
    public_key: str = Field(pattern=POINT_HEX)
    private_key: str = Field(min_length=40, max_length=64, pattern=B64URL)


@router.post("/push/vapid", name="crud_agent_push_vapid_store")
async def push_vapid_store(body: VapidMade, state: StateDep) -> VapidAnswer:
    """Stores the pair the agent made unless one is stored; answers the stored one (two
    replicas making one at once: the first insert wins and both read it back)."""
    kek = _kek(state)
    sealed = seal_secret(kek, body.private_key, VAPID_AAD)

    def run(conn: psycopg.Connection[Any]) -> Vapid | None:
        conn.execute(
            "INSERT INTO ai_push_vapid (id, public_key, secret_sealed, dek_sealed, kek_id)"
            " VALUES (1, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (
                bytes.fromhex(body.public_key),
                sealed.secret_sealed,
                sealed.dek_sealed,
                sealed.kek_id,
            ),
        )
        return _read_vapid(conn, kek)

    return VapidAnswer(vapid=await _in_tx(state, run))


class Owner(_Body):
    kind: Literal["browser", "bearer", "oidc", "anonymous", "flow"]
    id: str = Field(min_length=1, max_length=500)
    label: str = Field(max_length=500)


class Subscribe(_Body):
    owner: Owner
    endpoint: str = Field(min_length=8, max_length=ENDPOINT_MAX)
    #: As the browser gave them, base64url: 65 and 16 octets.
    p256dh: str = Field(min_length=86, max_length=88, pattern=B64URL)
    auth: str = Field(min_length=22, max_length=24, pattern=B64URL)


class Subscribed(BaseModel):
    id: str


@router.post("/push/subscriptions", name="crud_agent_push_subscribe")
async def push_subscribe(body: Subscribe, state: StateDep) -> Subscribed:
    kek = _kek(state)
    # The plaintext the agent seals (pushStore.ts `subscribe`), so either side opens it.
    secret = json.dumps({"p256dh": body.p256dh, "auth": body.auth}, separators=(",", ":"))
    owner = body.owner

    def run(conn: psycopg.Connection[Any]) -> str:
        held = conn.execute(
            "SELECT id, owner_kind, owner_id FROM ai_push_subscriptions WHERE endpoint = %s"
            " FOR UPDATE",
            (body.endpoint,),
        ).fetchone()
        if held is not None and (held["owner_kind"], held["owner_id"]) != (owner.kind, owner.id):
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "this push endpoint is subscribed for someone else",
                reason=SUBSCRIPTION_TAKEN,
            )
        if held is not None:
            # The same browser again: the same id, so what was pushed to it is not pushed
            # again; its keys sealed again, and it counts as the newest.
            sub_id = str(held["id"])
            sealed = seal_secret(kek, secret, _subscription_aad(sub_id, body.endpoint))
            conn.execute(
                "UPDATE ai_push_subscriptions SET owner_label = %s, secret_sealed = %s,"
                " dek_sealed = %s, kek_id = %s, created_at = now() WHERE id = %s",
                (owner.label, sealed.secret_sealed, sealed.dek_sealed, sealed.kek_id, held["id"]),
            )
            return sub_id
        new_id = uuid4()
        sealed = seal_secret(kek, secret, _subscription_aad(str(new_id), body.endpoint))
        conn.execute(
            "INSERT INTO ai_push_subscriptions (id, owner_kind, owner_id, owner_label, endpoint,"
            " secret_sealed, dek_sealed, kek_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                new_id,
                owner.kind,
                owner.id,
                owner.label,
                body.endpoint,
                sealed.secret_sealed,
                sealed.dek_sealed,
                sealed.kek_id,
            ),
        )
        conn.execute(
            "DELETE FROM ai_push_subscriptions WHERE id IN ("
            " SELECT id FROM ai_push_subscriptions WHERE owner_kind = %s AND owner_id = %s"
            " ORDER BY created_at DESC, id OFFSET %s)",
            (owner.kind, owner.id, SUBSCRIPTIONS_PER_OWNER),
        )
        return str(new_id)

    return Subscribed(id=await _in_tx(state, run))


class Unsubscribe(_Body):
    endpoint: str = Field(min_length=1, max_length=ENDPOINT_MAX)
    #: Only while this owner stored it; left out, whoever did (gone at the push service).
    owner: Owner | None = None


@router.post("/push/subscriptions/remove", name="crud_agent_push_unsubscribe")
async def push_unsubscribe(body: Unsubscribe, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> bool:
        if body.owner is None:
            cur = conn.execute(
                "DELETE FROM ai_push_subscriptions WHERE endpoint = %s", (body.endpoint,)
            )
        else:
            cur = conn.execute(
                "DELETE FROM ai_push_subscriptions WHERE endpoint = %s AND owner_kind = %s"
                " AND owner_id = %s",
                (body.endpoint, body.owner.kind, body.owner.id),
            )
        return cur.rowcount > 0

    return Done(done=await _in_tx(state, run))


class OpenSubscription(BaseModel):
    id: str
    owner: dict[str, str]
    endpoint: str
    p256dh: str
    auth: str


class OpenSubscriptions(BaseModel):
    subscriptions: list[OpenSubscription]


@router.get("/push/subscriptions", name="crud_agent_push_subscriptions")
async def push_subscriptions(state: StateDep) -> OpenSubscriptions:
    """Every subscription whose keys the KEK opens; one it cannot is skipped."""
    kek = _kek(state)

    def run(conn: psycopg.Connection[Any]) -> list[Any]:
        return conn.execute(
            "SELECT id::text AS id, owner_kind, owner_id, owner_label, endpoint,"
            " secret_sealed, dek_sealed, kek_id FROM ai_push_subscriptions ORDER BY created_at"
        ).fetchall()

    out: list[OpenSubscription] = []
    for row in await _in_tx(state, run):
        try:
            keys = json.loads(
                open_secret(kek, _envelope(row), _subscription_aad(row["id"], row["endpoint"]))
            )
        except (SealError, ValueError):
            continue
        out.append(
            OpenSubscription(
                id=row["id"],
                owner={
                    "kind": row["owner_kind"],
                    "id": row["owner_id"],
                    "label": row["owner_label"],
                },
                endpoint=row["endpoint"],
                p256dh=str(keys["p256dh"]),
                auth=str(keys["auth"]),
            )
        )
    return OpenSubscriptions(subscriptions=out)


class SentClaim(_Body):
    request_id: str = Field(min_length=1, max_length=1024)
    target: str = Field(min_length=1, max_length=300)


class Claimed(BaseModel):
    claimed: bool


@router.post("/push/sent", name="crud_agent_push_sent")
async def push_sent(body: SentClaim, state: StateDep) -> Claimed:
    """Claims telling ``target`` of ``request_id``: true for the one caller that sends it.
    Rows older than ``SENT_KEEP_DAYS`` go at the same time."""

    def run(conn: psycopg.Connection[Any]) -> bool:
        conn.execute(
            "DELETE FROM ai_notifications_sent WHERE sent_at < now() - make_interval(days => %s)",
            (SENT_KEEP_DAYS,),
        )
        row = conn.execute(
            "INSERT INTO ai_notifications_sent (request_id, target) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING RETURNING 1",
            (body.request_id, body.target),
        ).fetchone()
        return row is not None

    return Claimed(claimed=await _in_tx(state, run))


#: The agent's ``notify/dispatcher.ts`` ``WAITING_PUSH_WINDOW_MS``: a request older than
#: this is not pushed, so it is not listed.
PUSH_WINDOW = timedelta(hours=24)


def _epoch_s(at: datetime) -> int:
    """Whole seconds since the epoch, floored: the page's ``Math.floor(Date.parse(at) / 1000)``."""
    return (at - _EPOCH) // timedelta(seconds=1)


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class PollLease(_Body):
    #: The poll's name: one lease row each (``lease:<name>`` in ``ai_notifications_sent``).
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9-]*$")
    #: How long a holder keeps it: no other caller takes it again sooner.
    seconds: int = Field(ge=1, le=3600)


class Held(BaseModel):
    held: bool


@router.post("/push/poll-lease", name="crud_agent_push_poll_lease")
async def push_poll_lease(body: PollLease, state: StateDep) -> Held:
    """#6399: one agent replica a window runs a timed poll (the Web Push pass over the
    waiting grants and pairings). One row per poll name, upserted: taken when it is new or
    its last take is at least ``seconds`` old by the database's clock, so the replicas'
    clocks never decide it, and it is refreshed on each take, so the 2-day sweep of
    ``ai_notifications_sent`` never reaches it while a replica polls."""

    def run(conn: psycopg.Connection[Any]) -> bool:
        row = conn.execute(
            "INSERT INTO ai_notifications_sent (request_id, target) VALUES (%s, 'lease')"
            " ON CONFLICT (request_id, target) DO UPDATE SET sent_at = now()"
            " WHERE ai_notifications_sent.sent_at <= now() - make_interval(secs => %s)"
            " RETURNING 1",
            (f"lease:{body.name}", body.seconds),
        ).fetchone()
        return row is not None

    return Held(held=await _in_tx(state, run))


class WaitingRequest(BaseModel):
    """A flow grant waiting for a person's decision, as a push names it: the id the page's
    own notification uses as its tag (frontend `lib/needsYou.ts`), never what it asks."""

    id: str
    reason: Literal["grant"]
    #: When it started to wait: the agent pushes it only while it is young, so a swept claim
    #: (``SENT_KEEP_DAYS``) is never claimed again.
    since: datetime


class WaitingRequests(BaseModel):
    requests: list[WaitingRequest]


@router.get("/push/waiting", name="crud_agent_push_waiting")
async def push_waiting(triggers: TriggersDep) -> WaitingRequests:
    """#6399: the flow grants waiting for a person (a run's and a trigger's standing grant),
    which the agent pushes to a closed tab beside its own pending input: those of
    ``GET /api/v1/flow-grant-requests`` requested within ``PUSH_WINDOW``, by id only. A
    standing grant's id carries its ``requested_at`` (epoch seconds), as the page's does, so
    a grant an edit returned to ``requested`` is a new request with a claim of its own."""
    # Only those the agent still pushes, so older ones never crowd a new one out of the page.
    newer_than = datetime.now(UTC) - PUSH_WINDOW
    runs, _ = await triggers.flows.store.requested_grants(GRANT_REQUESTS_MAX, newer_than)
    standing = await triggers.store.requested_grant_ids(GRANT_REQUESTS_MAX, newer_than)
    return WaitingRequests(
        requests=[
            WaitingRequest(id=f"run-grant:{grant.id}", reason="grant", since=grant.created_at)
            for grant, _ in runs
        ]
        + [
            WaitingRequest(id=f"trigger-grant:{gid}:{_epoch_s(asked)}", reason="grant", since=asked)
            for gid, asked in standing
        ]
    )


# -- notification channels ----------------------------------------------------------------

ChannelKind = Literal["ntfy", "home_assistant"]
Token = Annotated[str, Field(min_length=1, max_length=TOKEN_MAX, pattern=r"^[^\r\n]+$")]


class ChannelFields(_Body):
    """A channel as the agent checked it (``channels.ts`` ``#check``)."""

    kind: ChannelKind
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=8, max_length=2048, pattern=r"^https?://")
    target: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,100}$")
    enabled: bool = True


class ChannelCreate(ChannelFields):
    token: Token | None = None


class ChannelUpdate(ChannelFields):
    #: A new token, or none (``null``) when ``keep_token`` is false.
    token: Token | None = None
    #: Keep the sealed token: only while the kind and URL are the row's.
    keep_token: bool = False


class ChannelView(BaseModel):
    id: str
    kind: ChannelKind
    name: str
    url: str
    target: str | None
    enabled: bool
    has_token: bool
    token_last4: str
    last_error: str | None
    last_sent_at: datetime | None


class ChannelList(BaseModel):
    channels: list[ChannelView]


_VIEW = (
    "id::text AS id, kind, name, url, target, enabled, secret_sealed IS NOT NULL AS has_token,"
    " token_last4, last_error, last_sent_at"
)


def _check_target(fields: ChannelFields, has_token: bool) -> None:
    if (fields.kind == "home_assistant") != (fields.target is not None):
        raise ApiError(422, "a Home Assistant channel names its notify service; ntfy names none")
    if fields.kind == "home_assistant" and not has_token:
        raise ApiError(422, "Home Assistant needs a long-lived access token")


@router.get("/notify-channels", name="crud_agent_notify_channels")
async def notify_channels(state: StateDep) -> ChannelList:
    def run(conn: psycopg.Connection[Any]) -> list[Any]:
        return conn.execute(
            f"SELECT {_VIEW} FROM ai_notify_channels ORDER BY created_at, id"
        ).fetchall()

    return ChannelList(channels=[ChannelView.model_validate(r) for r in await _in_tx(state, run)])


class ChannelAnswer(BaseModel):
    channel: ChannelView | None


@router.get("/notify-channels/{channel_id}", name="crud_agent_notify_channel")
async def notify_channel(channel_id: UUID, state: StateDep) -> ChannelAnswer:
    def run(conn: psycopg.Connection[Any]) -> Any:
        return conn.execute(
            f"SELECT {_VIEW} FROM ai_notify_channels WHERE id = %s", (channel_id,)
        ).fetchone()

    row = await _in_tx(state, run)
    return ChannelAnswer(channel=None if row is None else ChannelView.model_validate(row))


class Created(BaseModel):
    id: str


def _seal_token(kek: Kek | None, channel_id: str, fields: ChannelFields, token: str | None) -> Any:
    if token is None:
        return (None, None, None, "")
    assert kek is not None
    sealed = seal_secret(kek, token, _channel_aad(channel_id, fields.kind, fields.url))
    return (sealed.secret_sealed, sealed.dek_sealed, sealed.kek_id, _last4(token))


@router.post(
    "/notify-channels", status_code=status.HTTP_201_CREATED, name="crud_agent_notify_channel_create"
)
async def notify_channel_create(body: ChannelCreate, state: StateDep) -> Created:
    _check_target(body, body.token is not None)
    kek = _kek(state) if body.token is not None else None
    channel_id = str(uuid4())
    sealed = _seal_token(kek, channel_id, body, body.token)

    def run(conn: psycopg.Connection[Any]) -> None:
        conn.execute(
            "INSERT INTO ai_notify_channels (id, kind, name, url, target, enabled,"
            " secret_sealed, dek_sealed, kek_id, token_last4)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (channel_id, body.kind, body.name, body.url, body.target, body.enabled, *sealed),
        )

    await _in_tx(state, run)
    return Created(id=channel_id)


@router.post("/notify-channels/{channel_id}/update", name="crud_agent_notify_channel_update")
async def notify_channel_update(channel_id: UUID, body: ChannelUpdate, state: StateDep) -> Done:
    """Changes a channel under its row lock. A token is bound to the kind and URL it was
    entered for: ``keep_token`` keeps it only while both are the row's (409
    ``channel-moved`` otherwise, for the agent to read again), and resets ``last_error``."""
    if body.keep_token and body.token is not None:
        raise ApiError(422, "keep_token takes no token")
    kek = _kek(state) if body.token is not None else None

    def run(conn: psycopg.Connection[Any]) -> bool:
        row = conn.execute(
            "SELECT kind, url, secret_sealed IS NOT NULL AS has_token FROM ai_notify_channels"
            " WHERE id = %s FOR UPDATE",
            (channel_id,),
        ).fetchone()
        if row is None:
            return False
        if body.keep_token:
            if (row["kind"], row["url"]) != (body.kind, body.url) or not row["has_token"]:
                raise ApiError(
                    status.HTTP_409_CONFLICT,
                    "the channel changed since it was read; its token no longer applies",
                    reason=CHANNEL_MOVED,
                )
            _check_target(body, True)
            conn.execute(
                "UPDATE ai_notify_channels SET name = %s, target = %s, enabled = %s,"
                " last_error = NULL WHERE id = %s",
                (body.name, body.target, body.enabled, channel_id),
            )
            return True
        _check_target(body, body.token is not None)
        sealed = _seal_token(kek, str(channel_id), body, body.token)
        conn.execute(
            "UPDATE ai_notify_channels SET kind = %s, name = %s, url = %s, target = %s,"
            " enabled = %s, secret_sealed = %s, dek_sealed = %s, kek_id = %s,"
            " token_last4 = %s, last_error = NULL WHERE id = %s",
            (body.kind, body.name, body.url, body.target, body.enabled, *sealed, channel_id),
        )
        return True

    return Done(done=await _in_tx(state, run))


@router.post("/notify-channels/{channel_id}/delete", name="crud_agent_notify_channel_delete")
async def notify_channel_delete(channel_id: UUID, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> bool:
        cur = conn.execute("DELETE FROM ai_notify_channels WHERE id = %s", (channel_id,))
        return cur.rowcount > 0

    return Done(done=await _in_tx(state, run))


class OpenChannel(BaseModel):
    id: str
    kind: ChannelKind
    url: str
    target: str | None
    token: str | None


class OpenChannelAnswer(BaseModel):
    channel: OpenChannel | None


class OpenChannels(BaseModel):
    channels: list[OpenChannel]


def _open_channel(kek: Kek | None, row: Any) -> OpenChannel:
    token: str | None = None
    if row["secret_sealed"] is not None:
        if kek is None:
            raise SealError("no key-encryption key")
        token = open_secret(kek, _envelope(row), _channel_aad(row["id"], row["kind"], row["url"]))
    return OpenChannel(
        id=row["id"], kind=row["kind"], url=row["url"], target=row["target"], token=token
    )


_OPEN = "id::text AS id, kind, url, target, secret_sealed, dek_sealed, kek_id"


@router.post("/notify-channels/{channel_id}/open", name="crud_agent_notify_channel_open")
async def notify_channel_open(channel_id: UUID, state: StateDep) -> OpenChannelAnswer:
    """One channel with its token opened, for a send (a test from Settings). A token the
    KEK does not open is 409."""

    def run(conn: psycopg.Connection[Any]) -> Any:
        return conn.execute(
            f"SELECT {_OPEN} FROM ai_notify_channels WHERE id = %s", (channel_id,)
        ).fetchone()

    row = await _in_tx(state, run)
    if row is None:
        return OpenChannelAnswer(channel=None)
    kek = _kek(state) if row["secret_sealed"] is not None else None
    try:
        return OpenChannelAnswer(channel=_open_channel(kek, row))
    except SealError as e:
        raise ApiError(
            status.HTTP_409_CONFLICT, f"the channel's token does not open: {e}"
        ) from None


@router.post("/notify-channels/open-enabled", name="crud_agent_notify_channels_enabled")
async def notify_channels_enabled(state: StateDep) -> OpenChannels:
    """The enabled channels whose tokens the KEK opens, for a send; without a KEK, those
    with no token."""
    try:
        kek: Kek | None = _kek(state)
    except ApiError:
        kek = None

    def run(conn: psycopg.Connection[Any]) -> list[Any]:
        return conn.execute(
            f"SELECT {_OPEN} FROM ai_notify_channels WHERE enabled ORDER BY created_at, id"
        ).fetchall()

    out: list[OpenChannel] = []
    for row in await _in_tx(state, run):
        try:
            out.append(_open_channel(kek, row))
        except SealError:
            continue
    return OpenChannels(channels=out)


class SendOutcome(_Body):
    #: None when the send went through.
    error: str | None = Field(default=None, max_length=10_000)


@router.post("/notify-channels/{channel_id}/record", name="crud_agent_notify_channel_record")
async def notify_channel_record(channel_id: UUID, body: SendOutcome, state: StateDep) -> Done:
    def run(conn: psycopg.Connection[Any]) -> bool:
        if body.error is None:
            cur = conn.execute(
                "UPDATE ai_notify_channels SET last_error = NULL, last_sent_at = now()"
                " WHERE id = %s",
                (channel_id,),
            )
        else:
            cur = conn.execute(
                "UPDATE ai_notify_channels SET last_error = %s WHERE id = %s",
                (body.error[:ERROR_MAX], channel_id),
            )
        return cur.rowcount > 0

    return Done(done=await _in_tx(state, run))
