"""#7204 Phase B: copy and edit a ScadBuddy filament preset, as Temporal activities on
the ``library`` queue (plan `docs/superpowers/plans/2026-11-26-filament-presets-view.md`,
rulings R2 to R7, R11 and R12).

The Go routes ``POST /api/v1/filament/presets/{preset_id}/copy`` and ``PATCH
/api/v1/filament/presets/{preset_id}`` check the body and start these activities (not
built yet: they wait for ``internal/temporalx``, ruling N7). The API process serves them,
because it is the only process with a database secret (#5295).

Each activity reads the current document (`FilamentPresetStore.read`), applies the
change and writes the row's name into the document's ``name`` key (R6), and keeps the
document as a blob (`keep_document`). Then, in one transaction, `idempotent_write` runs
the write once per actor, key and preset (R12): it takes `lock_presets_on`, reads the
row ``FOR UPDATE``, and makes a new ``manual`` preset (copy, R4) or a new version (edit,
R3, R5). A blob kept for a refused write stays unheld, and the blob sweep reclaims it.
After the commit the preset goes to Bambuddy (`mirror_pending`), bounded by
`WRITE_MIRROR_TIMEOUT` (R11). A mirror failure never fails the write (Decision 11).

Every failure is a non-retryable `ApplicationError` whose ``type`` is one of the problem
codes below.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Annotated, Any, Final, Literal, Self

from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
from pydantic_core import PydanticCustomError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.core.credentials import is_credential
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.idempotency import Replay, idempotent_write
from scadbuddy.filament.preset_mirror import mirror_pending, wants_mirror
from scadbuddy.filament.sb_presets import (
    Conn,
    FilamentPreset,
    FilamentPresetStore,
    create_on,
    document_bytes,
    keep_document,
    lock_presets_on,
    version_on,
)
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.store import BlobStore

COPY_ACTIVITY: Final = "filament_preset_copy"
EDIT_ACTIVITY: Final = "filament_preset_edit"
#: The routes the idempotency ledger keys a reply under (the Go routes that start these).
COPY_ROUTE: Final = "POST /filament/presets/{preset_id}/copy"
EDIT_ROUTE: Final = "PATCH /filament/presets/{preset_id}"

#: The bound on the mirror after a write (R11); past it the outcome is ``pending``.
WRITE_MIRROR_TIMEOUT: Final = 30.0
#: The most keys one edit sets plus unsets (R7).
MAX_EDIT_KEYS: Final = 500
#: The shape of a key an edit may set or unset (R7).
KEY: Final = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

PRESET_NOT_FOUND: Final = "preset_not_found"
NOT_MANUAL: Final = "not_manual"
VERSION_MOVED: Final = "version_moved"
DOCUMENT_MISSING: Final = "document_missing"
KEY_REFUSED: Final = "key_refused"
#: An idempotency key sent again with another body (`crud/idempotency.py` `KEY_PROBLEM`).
KEY_REUSED: Final = "idempotency_key_reused"

PresetName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Mirror = Literal["sent", "failed", "pending", "off"]


class PresetCopyInput(BaseModel):
    preset_id: int
    name: PresetName
    actor: str
    idempotency_key: str


class PresetEditInput(BaseModel):
    """A patch of a ``manual`` preset (R5). The validators apply R7."""

    preset_id: int
    expected_version: int = Field(gt=0)
    name: PresetName | None = None
    set: dict[str, str | list[str]] = {}
    unset: list[str] = []
    actor: str
    idempotency_key: str

    @field_validator("set", "unset")
    @classmethod
    def _keys_allowed(cls, value: Any) -> Any:
        for key in value:
            if not KEY.match(key) or key == "name" or is_credential(key):
                raise PydanticCustomError(
                    KEY_REFUSED, "the key {key} cannot be set or unset", {"key": key}
                )
        return value

    @model_validator(mode="after")
    def _edit_shape(self) -> Self:
        if len(self.set) + len(self.unset) > MAX_EDIT_KEYS:
            raise ValueError(f"an edit sets and unsets at most {MAX_EDIT_KEYS} keys")
        both = sorted(self.set.keys() & set(self.unset))
        if both:
            raise PydanticCustomError(
                KEY_REFUSED, "the key {key} is both set and unset", {"key": both[0]}
            )
        if self.name is None and not self.set and not self.unset:
            raise ValueError("an edit needs a name, a key to set or a key to unset")
        return self


class PresetWritten(BaseModel):
    preset_id: int
    version: int
    changed: bool
    mirror: Mirror


def _refused(type_: str, message: str, *details: Any) -> ApplicationError:
    return ApplicationError(message, *details, type=type_, non_retryable=True)


async def _current(
    store: FilamentPresetStore, preset_id: int
) -> tuple[FilamentPreset, dict[str, Any]]:
    """The preset and its current document, or the refusal."""
    row = await asyncio.to_thread(store.get, preset_id)
    if row is None:
        raise _refused(PRESET_NOT_FOUND, f"no filament preset {preset_id}")
    doc = await store.read(row.blob_key)
    if doc is None:
        raise _refused(DOCUMENT_MISSING, f"the document of filament preset {preset_id} is lost")
    return row, doc


def _locked_row(conn: Conn, preset_id: int) -> dict[str, Any]:
    lock_presets_on(conn)
    row = conn.execute(
        "SELECT source, version, type_id, printer_model, nozzle_diameter"
        " FROM filament_presets WHERE id = %s FOR UPDATE",
        (preset_id,),
    ).fetchone()
    if row is None:
        raise _refused(PRESET_NOT_FOUND, f"no filament preset {preset_id}")
    return row


def _write(
    pool: ConnectionPool[Conn],
    route: str,
    body: PresetCopyInput | PresetEditInput,
    run: Callable[[Conn], Replay],
) -> dict[str, Any]:
    """``run`` once per actor, key and preset (R12): its reply's body, or the stored one."""
    key = f"{body.actor}\x1f{body.idempotency_key}\x1f{body.preset_id}"
    asked = body.model_dump(mode="json", exclude={"actor", "idempotency_key"})
    with pool.connection() as conn:
        try:
            reply = idempotent_write(conn, "filament", key, route, asked, lambda: run(conn))
        except ApiError as error:
            raise _refused(KEY_REUSED, error.detail) from None
    written: dict[str, Any] = reply.body
    return written


async def _mirrored(
    pool: ConnectionPool[Conn],
    store: FilamentPresetStore,
    settings: StoredSettings,
    preset_id: int,
) -> Mirror:
    """The preset sent to Bambuddy after the commit, and what came of it (R9, R11)."""
    if not wants_mirror(settings):
        return "off"
    try:
        await asyncio.wait_for(
            mirror_pending(pool, store, settings, [preset_id]), WRITE_MIRROR_TIMEOUT
        )
    except TimeoutError:
        return "pending"

    def state() -> Mirror:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT sha256, mirrored_sha256, mirror_error FROM filament_presets WHERE id = %s",
                (preset_id,),
            ).fetchone()
        if row is None:
            return "pending"
        if row["mirrored_sha256"] == row["sha256"]:
            return "sent"
        return "failed" if row["mirror_error"] is not None else "pending"

    return await asyncio.to_thread(state)


async def copy_preset(
    pool: ConnectionPool[Conn],
    blobs: BlobStore,
    settings: StoredSettings,
    body: PresetCopyInput,
) -> PresetWritten:
    """A new ``manual`` preset from ``body.preset_id``'s current document, any source
    (R4). It keeps the type, printer model and nozzle, and no 3DFP, Bambuddy or mirror
    column."""
    store = FilamentPresetStore(pool, lambda: blobs)
    _, doc = await _current(store, body.preset_id)
    copied = {**doc, "name": body.name}
    key, sha256 = await keep_document(blobs, document_bytes(copied))

    def run(conn: Conn) -> Replay:
        row = _locked_row(conn, body.preset_id)
        made = create_on(
            conn,
            name=body.name,
            source="manual",
            key=key,
            sha256=sha256,
            doc=copied,
            made_by=body.actor,
            type_id=row["type_id"],
            printer_model=row["printer_model"],
            nozzle_diameter=row["nozzle_diameter"],
        )
        return Replay(201, {"preset_id": made, "version": 1, "changed": True})

    written = await asyncio.to_thread(_write, pool, COPY_ROUTE, body, run)
    mirror = await _mirrored(pool, store, settings, written["preset_id"])
    return PresetWritten(**written, mirror=mirror)


async def edit_preset(
    pool: ConnectionPool[Conn],
    blobs: BlobStore,
    settings: StoredSettings,
    body: PresetEditInput,
) -> PresetWritten:
    """``body``'s patch of a ``manual`` preset as its next version (R3, R5), refused when
    the preset is no longer at ``expected_version``. A patch that changes nothing writes
    no version (``changed`` false)."""
    store = FilamentPresetStore(pool, lambda: blobs)
    current, doc = await _current(store, body.preset_id)
    name = body.name if body.name is not None else current.name
    edited = {k: v for k, v in doc.items() if k not in body.unset} | body.set | {"name": name}
    key, sha256 = await keep_document(blobs, document_bytes(edited))

    def run(conn: Conn) -> Replay:
        row = _locked_row(conn, body.preset_id)
        if row["source"] != "manual":
            raise _refused(
                NOT_MANUAL, f"filament preset {body.preset_id} is {row['source']}: copy it first"
            )
        if row["version"] != body.expected_version:
            raise _refused(
                VERSION_MOVED,
                f"filament preset {body.preset_id} is at version {row['version']} now",
                row["version"],
            )
        version = version_on(
            conn,
            body.preset_id,
            name=name,
            key=key,
            sha256=sha256,
            doc=edited,
            made_by=body.actor,
        )
        return Replay(
            200,
            {
                "preset_id": body.preset_id,
                "version": row["version"] if version is None else version,
                "changed": version is not None,
            },
        )

    written = await asyncio.to_thread(_write, pool, EDIT_ROUTE, body, run)
    mirror = await _mirrored(pool, store, settings, body.preset_id)
    return PresetWritten(**written, mirror=mirror)


def preset_write_activities(
    pool: Callable[[], ConnectionPool[Conn]],
    blobs: Callable[[], BlobStore],
    settings: Callable[[], StoredSettings],
) -> list[Callable[..., Any]]:
    """``filament_preset_copy`` and ``filament_preset_edit`` for the ``library`` worker
    (`main._run_library_worker`)."""

    @activity.defn(name=COPY_ACTIVITY)
    async def filament_preset_copy(body: PresetCopyInput) -> PresetWritten:
        loaded = await asyncio.to_thread(settings)
        return await copy_preset(pool(), blobs(), loaded, body)

    @activity.defn(name=EDIT_ACTIVITY)
    async def filament_preset_edit(body: PresetEditInput) -> PresetWritten:
        loaded = await asyncio.to_thread(settings)
        return await edit_preset(pool(), blobs(), loaded, body)

    return [filament_preset_copy, filament_preset_edit]
