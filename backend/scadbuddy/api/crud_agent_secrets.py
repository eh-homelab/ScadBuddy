"""The agent's settings, Claude credentials and payload data keys behind CRUD routes
(#5299 step 4, D5; #5342): under ``/api/v1/internal/crud/agent/``, for the ``agent``
credential only (`crud_router`). Each route is one transaction (D2); the SQL is in
``agent_data/``, and the agent's stores (``agent/src/credentials.ts``,
``agent/src/temporal/payloadCodec.ts``) call these routes once ``…/areas`` lists the
area, and their own SQL until then (D9).

Area ``settings``, ``ai_settings``:

- ``GET …/settings?key=…`` (repeated): the stored values of the keys, an absent key left
  out;
- ``POST …/settings``: a write of one or more keys, with ``expect`` a compare-and-set
  under the table's lock, and the NOTIFYs the agent's caches follow sent in the same
  transaction (``agent_data/settings.py``).

Area ``credentials``, ``ai_credentials``, sealed and opened here with the KEK:

- ``GET …/credentials/key``: whether this API holds a KEK, and its id (the agent tells a
  credential sealed under another key by it);
- ``GET …/credentials`` and ``GET …/credentials/one``: every credential, or one (the
  first by priority without ``id``), with no secret;
- ``POST …/credentials`` (create), ``…/save``, ``…/delete``, ``…/order``, ``…/reset``,
  ``…/name`` and ``…/events`` (what a query learned): the agent's store's writes;
- ``POST …/credentials/reveal``: the plaintext of one credential, or of every one the KEK
  opens, for a turn (``purpose`` ``turn``), the redaction of tool output (``redact``) or a
  connection test (``test``). Each reveal writes an ``ai_audit`` row in its transaction.

Area ``payload-keys``: ``POST …/payload-keys/{subject}``, a subject's data key, made first
with ``create`` (``agent_data/payload_keys.py``: the subject's lock and the tombstone check
inside this transaction), with an ``ai_audit`` row. Never the KEK.

A secret travels only in these routes' bodies: every CRUD answer is ``no-store``, and no
log line here names one. A credential id is a body field, never a path segment.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from fastapi import Path, Query, status
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from scadbuddy.agent_data import audit, payload_keys
from scadbuddy.agent_data import credentials as store
from scadbuddy.agent_data import settings as agent_settings
from scadbuddy.agent_data.rotation import kek_at
from scadbuddy.api.deps import StateDep
from scadbuddy.core.problems import ApiError
from scadbuddy.core.secrets import Kek, SealError
from scadbuddy.crud.guard import crud_router
from scadbuddy.flows.agent_reads import Pool

router = crud_router("agent")

T = TypeVar("T")

#: A refusal of the credential store (its message is the agent's, its status 400 to 503).
CREDENTIAL_REFUSED = "credential-refused"
#: A credential the KEK does not open (wrong key, altered, or the v1 format).
CREDENTIAL_SEALED = "credential-sealed"
#: This API holds no KEK, so nothing is sealed or opened here.
KEK_MISSING = "kek-missing"
#: A payload key refused: the subject has none (forgotten, or never made).
SUBJECT_FORGOTTEN = "subject-forgotten"
#: A payload key sealed under a KEK this API does not hold.
KEY_NOT_HELD = "key-not-held"

#: A settings key: printable ASCII, no space.
SETTING_KEY = r"^[\x21-\x7e]{1,200}$"
_SETTING_KEY = re.compile(SETTING_KEY)
#: The most keys one read or write names.
SETTINGS_MAX = 50
#: The longest value written, as JSON text.
SETTING_VALUE_MAX = 256 * 1024
#: How long one ``reveal.redact`` audit row stands for every later reveal for redaction.
REDACT_AUDIT_WINDOW = "10 minutes"
#: A credential's id: the agent's (a UUID, or `default` for the row #255 made).
CREDENTIAL_ID = Annotated[str, Field(min_length=1, max_length=64)]


def _pool(state: Any) -> Pool:
    pool: Pool = state.settings_store.pool
    return pool


def _keks(state: Any) -> tuple[Kek | None, Kek | None]:
    """The current and previous KEK. A key once read is kept, so no request reads it again;
    a file that could not be read is tried again on a later call, at most every few seconds
    (``kek_at``)."""
    return kek_at(state.settings.secret_key_file), kek_at(state.settings.secret_key_previous_file)


async def _in_conn[T](state: Any, fn: Callable[[Any], T]) -> T:
    def run() -> T:
        with _pool(state).connection() as conn:
            return fn(conn)

    try:
        return await asyncio.to_thread(run)
    except store.CredentialRefusedError as refused:
        raise ApiError(refused.status, str(refused), reason=CREDENTIAL_REFUSED) from None


def _no_kek() -> ApiError:
    return ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "The API has no key-encryption key (SCADBUDDY_SECRET_KEY_FILE).",
        reason=KEK_MISSING,
    )


# --- settings ---------------------------------------------------------------------------


class SettingValues(BaseModel):
    #: Each key that has a row, and its value.
    values: dict[str, Any]


def _bounded(values: dict[str, Any]) -> dict[str, Any]:
    for key, value in values.items():
        if len(json.dumps(value)) > SETTING_VALUE_MAX:
            raise ValueError(f"the value of {key} is over {SETTING_VALUE_MAX} bytes as JSON")
    return values


class ExpectedSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=SETTING_KEY)
    #: False when the agent read no row for the key.
    present: bool
    value: Any = None

    @field_validator("value")
    @classmethod
    def _value_bounded(cls, value: Any, info: ValidationInfo) -> Any:
        _bounded({info.data.get("key", "expect"): value})
        return value


class SettingsNotify(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Tell every replica's warm process cache to evict (a key a warm process was spawned by).
    warm_evict: bool = False
    #: Tell every replica to re-read the session-runtime settings.
    runtime: bool = False


class SettingsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: dict[Annotated[str, Field(pattern=SETTING_KEY)], Any] = Field(
        min_length=1, max_length=SETTINGS_MAX
    )
    #: The keys as the agent read them: write only while each still holds that.
    expect: list[ExpectedSetting] | None = Field(default=None, max_length=SETTINGS_MAX)
    notify: SettingsNotify = SettingsNotify()

    _values_bounded = field_validator("values")(_bounded)


class SettingsWritten(BaseModel):
    #: False when ``expect`` no longer held: nothing was written.
    written: bool


@router.get("/settings", name="crud_agent_settings_read")
async def read_settings(
    state: StateDep,
    key: Annotated[list[str], Query(min_length=1, max_length=SETTINGS_MAX)],
) -> SettingValues:
    if not all(_SETTING_KEY.match(k) for k in key):
        raise ApiError(422, "a settings key is printable ASCII with no space, 1 to 200 characters")
    return SettingValues(values=await _in_conn(state, lambda conn: agent_settings.read(conn, key)))


@router.post("/settings", name="crud_agent_settings_write")
async def write_settings(body: SettingsWrite, state: StateDep) -> SettingsWritten:
    expect = (
        None
        if body.expect is None
        else [agent_settings.Expected(e.key, e.present, e.value) for e in body.expect]
    )
    written = await _in_conn(
        state,
        lambda conn: agent_settings.write(
            conn,
            body.values,
            expect=expect,
            warm_evict=body.notify.warm_evict,
            runtime=body.notify.runtime,
        ),
    )
    return SettingsWritten(written=written)


# --- credentials ------------------------------------------------------------------------

CredentialKind = Literal["anthropic_api_key", "claude_oauth_token", "gateway"]


class KeyStatus(BaseModel):
    #: Whether this API holds a KEK: without one nothing is sealed or opened.
    ok: bool
    kek_id: str | None = None
    #: Why not, safe to show (names the variable, never the file).
    reason: str | None = None


class StoredCredential(BaseModel):
    """The agent's ``StoredCredential``: never a secret."""

    id: str
    priority: int
    kind: CredentialKind
    name: str | None
    base_url: str | None
    last4: str
    updated_at: str
    status: Literal["active", "cooling_down", "disabled"]
    cooldown_until: str | None
    last_error: str | None
    last_error_at: str | None
    last_used_at: str | None
    epoch: int
    kek_id: str
    legacy_format: bool


class CredentialList(BaseModel):
    credentials: list[StoredCredential]


class OneCredential(BaseModel):
    credential: StoredCredential | None


class CredentialFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: CredentialKind
    #: As the agent normalised it (`normaliseBaseUrl`).
    base_url: str | None = Field(default=None, max_length=2048)
    #: Omitted: keep the stored one (a save only).
    secret: str | None = Field(default=None, min_length=1, max_length=4096)
    #: As the agent normalised it (`normaliseCredentialName`); read by a create only.
    name: str | None = Field(default=None, max_length=1024)

    def update(self) -> store.Update:
        return store.Update(
            kind=self.kind, base_url=self.base_url, secret=self.secret, name=self.name
        )


class CredentialSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The credential to save over; the first by priority when omitted.
    id: CREDENTIAL_ID | None = None
    update: CredentialFields


class CredentialTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: CREDENTIAL_ID | None = None


class CredentialId(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: CREDENTIAL_ID


class CredentialRename(CredentialId):
    name: str | None = Field(max_length=1024)


class CredentialOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: list[CREDENTIAL_ID] = Field(max_length=store.MAX_CREDENTIALS)


class Deleted(BaseModel):
    deleted: bool


class CredentialEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["used", "disabled", "cooling_down", "transient"]
    #: Redacted of the secret by the agent.
    reason: str | None = Field(default=None, max_length=2000)
    until: datetime | None = None
    replacing: datetime | None = None


class CredentialRecord(CredentialId):
    epoch: int = Field(ge=0, le=2**31 - 1)
    event: CredentialEvent


class Transition(BaseModel):
    before: str
    after: str
    recovered: bool


class Recorded(BaseModel):
    #: None when the credential is gone or its epoch moved on.
    transition: Transition | None


class RevealAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: One credential (the first by priority when omitted), unless ``all``.
    id: CREDENTIAL_ID | None = None
    #: Every credential, each opened where the KEK opens it.
    all: bool = False
    #: What the plaintext is for, on the audit row.
    purpose: Literal["turn", "redact", "test"]
    #: The turn the plaintext is for, on the audit row.
    session_id: UUID | None = None
    turn_id: UUID | None = None

    def ids(self) -> dict[str, str | None]:
        return {
            "session_id": None if self.session_id is None else str(self.session_id),
            "turn_id": None if self.turn_id is None else str(self.turn_id),
        }


class Opened(BaseModel):
    """The agent's ``Credential``: the secret in plaintext."""

    kind: CredentialKind
    base_url: str | None = None
    secret: str


class RevealedRow(BaseModel):
    stored: StoredCredential
    #: None where the KEK does not open it.
    credential: Opened | None


class Revealed(BaseModel):
    #: ``all``: every credential.
    credentials: list[RevealedRow] | None = None
    #: One: None for no such credential.
    credential: Opened | None = None


def _stored(row: dict[str, Any]) -> StoredCredential:
    return StoredCredential.model_validate(store.stored(row))


@router.get("/credentials/key", name="crud_agent_credentials_key")
async def credentials_key(state: StateDep) -> KeyStatus:
    kek, _ = _keks(state)
    if kek is None:
        return KeyStatus(
            ok=False, reason="the API has no key-encryption key (SCADBUDDY_SECRET_KEY_FILE)"
        )
    return KeyStatus(ok=True, kek_id=kek.id)


@router.get("/credentials", name="crud_agent_credentials_list")
async def list_credentials(state: StateDep) -> CredentialList:
    rows = await _in_conn(state, store.list_all)
    return CredentialList(credentials=[_stored(r) for r in rows])


@router.get("/credentials/one", name="crud_agent_credentials_get")
async def get_credential(
    state: StateDep, id: Annotated[str | None, Query(min_length=1, max_length=64)] = None
) -> OneCredential:
    row = await _in_conn(state, lambda conn: store.get(conn, id))
    return OneCredential(credential=None if row is None else _stored(row))


@router.post(
    "/credentials", status_code=status.HTTP_201_CREATED, name="crud_agent_credentials_create"
)
async def create_credential(body: CredentialFields, state: StateDep) -> OneCredential:
    kek, _ = _keks(state)
    row = await _in_conn(state, lambda conn: store.create(conn, body.update(), kek))
    return OneCredential(credential=_stored(row))


@router.post("/credentials/save", name="crud_agent_credentials_save")
async def save_credential(body: CredentialSave, state: StateDep) -> OneCredential:
    kek, _ = _keks(state)
    row = await _in_conn(state, lambda conn: store.put(conn, body.update.update(), kek, body.id))
    return OneCredential(credential=_stored(row))


@router.post("/credentials/delete", name="crud_agent_credentials_delete")
async def delete_credential(body: CredentialTarget, state: StateDep) -> Deleted:
    return Deleted(deleted=await _in_conn(state, lambda conn: store.delete(conn, body.id)))


@router.post("/credentials/order", name="crud_agent_credentials_order")
async def order_credentials(body: CredentialOrder, state: StateDep) -> CredentialList:
    rows = await _in_conn(state, lambda conn: store.reorder(conn, body.ids))
    return CredentialList(credentials=[_stored(r) for r in rows])


@router.post("/credentials/reset", name="crud_agent_credentials_reset")
async def reset_credential(body: CredentialId, state: StateDep) -> OneCredential:
    row = await _in_conn(state, lambda conn: store.reset(conn, body.id))
    return OneCredential(credential=None if row is None else _stored(row))


@router.post("/credentials/name", name="crud_agent_credentials_name")
async def rename_credential(body: CredentialRename, state: StateDep) -> OneCredential:
    row = await _in_conn(state, lambda conn: store.rename(conn, body.id, body.name))
    return OneCredential(credential=None if row is None else _stored(row))


@router.post("/credentials/events", name="crud_agent_credentials_event")
async def record_credential_event(body: CredentialRecord, state: StateDep) -> Recorded:
    e = body.event
    if e.kind != "used" and e.reason is None:
        raise ApiError(422, f"a {e.kind} event needs a reason", reason=CREDENTIAL_REFUSED)
    if e.kind == "cooling_down" and e.until is None:
        raise ApiError(422, "a cooling_down event needs until", reason=CREDENTIAL_REFUSED)
    event = store.Event(kind=e.kind, reason=e.reason, until=e.until, replacing=e.replacing)
    found = await _in_conn(state, lambda conn: store.record_event(conn, body.id, body.epoch, event))
    return Recorded(transition=None if found is None else Transition.model_validate(found))


def _opened(value: dict[str, Any]) -> Opened:
    return Opened.model_validate(value)


def _reveal(conn: Any, body: RevealAsk, kek: Kek) -> Revealed | SealError:
    """One transaction: the rows, opened, and the audit row. A credential that does not
    open answers its SealError, once its audit row is written."""
    action = f"reveal.{body.purpose}"
    with conn.transaction():
        if body.all:
            rows = store.sealed_rows(conn, None)
            out: list[RevealedRow] = []
            for row in rows:
                opened: Opened | None = None
                if row["kek_id"] == kek.id and not store.stored(row)["legacy_format"]:
                    try:
                        opened = _opened(store.open_row(kek, row))
                    except SealError:
                        opened = None
                out.append(RevealedRow(stored=_stored(row), credential=opened))
            count = sum(1 for r in out if r.credential is not None)
            # Redaction reads every credential at tool-call rate: one row a window says it did.
            if body.purpose != "redact" or not audit.recorded_lately(
                conn, "credential", (action,), REDACT_AUDIT_WINDOW
            ):
                audit.record(
                    conn,
                    "credential",
                    action,
                    "ok",
                    f"opened {count} of {len(out)} credential(s)",
                    **body.ids(),
                )
            return Revealed(credentials=out)
        found = store.sealed_rows(conn, body.id)
        if not found:
            audit.record(conn, "credential", action, "refused", "no such credential", **body.ids())
            return Revealed(credential=None)
        row = found[0]
        try:
            opened_one = _opened(store.open_row(kek, row))
        except SealError as err:
            audit.record(
                conn,
                "credential",
                action,
                "error",
                f"{store.label(row)} did not open",
                **body.ids(),
            )
            return err
        audit.record(conn, "credential", action, "ok", store.label(row), **body.ids())
        return Revealed(credential=opened_one)


@router.post("/credentials/reveal", name="crud_agent_credentials_reveal")
async def reveal_credentials(body: RevealAsk, state: StateDep) -> Revealed:
    kek, _ = _keks(state)
    if kek is None:
        raise _no_kek()
    answer = await _in_conn(state, lambda conn: _reveal(conn, body, kek))
    if isinstance(answer, SealError):
        raise ApiError(status.HTTP_409_CONFLICT, str(answer), reason=CREDENTIAL_SEALED)
    return answer


# --- payload keys -----------------------------------------------------------------------


class KeyAsk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Make the key first when the subject has none (and was not forgotten).
    create: bool = False
    #: Make it only while the session the subject names has its ``ai_sessions`` row.
    only_for_standing_session: bool = False


class DataKey(BaseModel):
    #: The subject's data key, base64: 32 bytes.
    data_key: str


@router.post("/payload-keys/{subject}", name="crud_agent_payload_key")
async def release_payload_key(
    subject: Annotated[str, Path(pattern=payload_keys.SUBJECT_PATTERN)],
    body: KeyAsk,
    state: StateDep,
) -> DataKey:
    if body.only_for_standing_session and not subject.startswith("session-"):
        raise ApiError(422, "only a session's key is made for a standing session")
    kek, previous = _keks(state)
    if kek is None:
        raise _no_kek()

    def run(conn: Any) -> bytes | None:
        return payload_keys.release(
            conn,
            subject,
            create=body.create,
            standing=body.only_for_standing_session,
            kek=kek,
            previous=previous,
        )

    try:
        key = await _in_conn(state, run)
    except payload_keys.KeyNotHeldError as err:
        raise ApiError(status.HTTP_409_CONFLICT, str(err), reason=KEY_NOT_HELD) from None
    if key is None:
        raise ApiError(
            status.HTTP_410_GONE,
            f"{subject} has no payload key: it was forgotten, or never made.",
            reason=SUBJECT_FORGOTTEN,
        )
    return DataKey(data_key=base64.b64encode(key).decode())
