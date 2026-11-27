"""#7143 (G5): resolve an unknown tray, as a Temporal activity on the ``library`` queue.

The Go route ``POST /api/v1/filament/unknown-trays/{id}/resolve`` will check the body and
start this activity (not built yet: it waits for ``internal/temporalx``). The activity links
a spool to the tray, books the spool's first weight and deletes the tray's row, in one
transaction. The API process serves it: it is the only process with a database secret
(#5295). It publishes no event (G8).

Locks, in this order (G9): the tray's advisory lock, the row ``FOR UPDATE``, then
`FilamentInventory.link_on`'s own (the tag, the spools by id, the place ``FOR SHARE``).
Every failure is a non-retryable `ApplicationError` whose ``type`` is one of the
constants below; the transaction rolls back, so nothing is half-written.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.bambuddy.trays import place_label
from scadbuddy.codes.parse import format_code, parse_code
from scadbuddy.filament import ledger
from scadbuddy.filament import models as m
from scadbuddy.filament.component import Filament
from scadbuddy.filament.inventory import (
    SpoolNotFoundError,
    TrayTagClashError,
    _tray_lock,
)
from scadbuddy.filament.locations import LocationStore, NoAmsKindError
from scadbuddy.filament.spools import FilamentSpoolStore, TagKeysClashError
from scadbuddy.filament.unknown_tray_rows import TRAY_CHANGED
from scadbuddy.spools.models import SeenTray

Conn = Connection[DictRow]

RESOLVE_UNKNOWN_TRAY_ACTIVITY: Final = "filament_resolve_unknown_tray"

CODE_UNKNOWN: Final = "code_unknown"
SPOOL_ARCHIVED: Final = "spool_archived"
NO_AMS_KIND: Final = "no_ams_kind"
SPOOL_MISSING: Final = "spool_missing"
TAG_CLASH: Final = "tag_clash"

#: The events a spool can have and still be new (D4): no reading, use or load yet.
_NEW_KINDS: Final = ("created", "edited", "moved", "tag_bound", "tag_unbound")


class ResolveInput(BaseModel):
    tray_id: UUID
    spool_id: UUID | None = None
    code: str | None = None
    net_weight_g: float
    seen_at: datetime
    actor: str


class ResolveOutput(BaseModel):
    """What the Go route answers (G6)."""

    id: UUID
    number: int
    code: str
    location_id: UUID | None


def _refused(type_: str, message: str) -> ApplicationError:
    return ApplicationError(message, type=type_, non_retryable=True)


def _spool_id(conn: Conn, body: ResolveInput) -> UUID:
    """The spool the body names, by id or by code. An archived one is refused."""
    if body.spool_id is not None:
        row = conn.execute(
            "SELECT id, archived_at FROM filament_spools WHERE id = %s", (body.spool_id,)
        ).fetchone()
        if row is None:
            raise _refused(SPOOL_MISSING, f"no spool {body.spool_id}")
    else:
        code = parse_code(body.code or "")
        row = None
        if code is not None and code.prefix == "S":
            row = conn.execute(
                "SELECT id, archived_at FROM filament_spools WHERE number = %s", (code.number,)
            ).fetchone()
        if row is None:
            raise _refused(CODE_UNKNOWN, f"no spool has the code {body.code!r}")
    if row["archived_at"] is not None:
        raise _refused(SPOOL_ARCHIVED, "that spool is archived")
    spool_id: UUID = row["id"]
    return spool_id


def _first_weight(
    conn: Conn, spools: FilamentSpoolStore, spool: m.Spool, body: ResolveInput
) -> None:
    """D4: the net weight becomes the spool's own label weight only for a spool with no
    reading, use or load yet and only when it differs from the label weight the spool
    has. The ``weighed`` reading is booked either way."""
    label = round(body.net_weight_g)
    used = conn.execute(
        "SELECT 1 FROM filament_spool_events WHERE spool_id = %s AND kind <> ALL(%s) LIMIT 1",
        (spool.id, list(_NEW_KINDS)),
    ).fetchone()
    if used is None and label > 0 and label != spool.effective.label_weight_g:
        spools.update_on(conn, spool.id, m.SpoolPatch(label_weight_g=label), actor=body.actor)
    ledger.record(
        conn,
        spool.id,
        "weighed",
        source="person",
        absolute_g=body.net_weight_g,
        ref_kind="unknown_tray",
        ref_id=str(body.tray_id),
        actor=body.actor,
        data={},
    )


def resolve_unknown_tray(
    filament: Filament, pool: ConnectionPool[Conn], body: ResolveInput
) -> ResolveOutput:
    """Link the spool to the tray, book its first weight and delete the row, in one
    transaction. Synchronous: the activity runs it in a thread."""
    with pool.connection() as conn, conn.transaction():
        cell = conn.execute(
            "SELECT printer_id, ams_id, tray_id FROM filament_unknown_trays WHERE id = %s",
            (body.tray_id,),
        ).fetchone()
        if cell is None:
            raise _refused(TRAY_CHANGED, "the tray changed or was resolved")
        printer_id, ams_id, tray_id = cell["printer_id"], cell["ams_id"], cell["tray_id"]
        _tray_lock(conn, printer_id, ams_id, tray_id)
        row = conn.execute(
            "SELECT u.tray_uuid, u.tag_uid, u.material, u.colour_rgba, u.remain_pct, u.seen_at,"
            " p.name AS printer_name FROM filament_unknown_trays u"
            " JOIN printers p ON p.id = u.printer_id WHERE u.id = %s FOR UPDATE OF u",
            (body.tray_id,),
        ).fetchone()
        if row is None or row["seen_at"] != body.seen_at:
            raise _refused(TRAY_CHANGED, "the tray changed or was resolved")
        spool_id = _spool_id(conn, body)
        seen = SeenTray(
            ams_id=ams_id,
            tray_id=tray_id,
            tray_uuid=row["tray_uuid"],
            tag_uid=row["tag_uid"],
            material=row["material"],
            colour=row["colour_rgba"],
            rgba=row["colour_rgba"],
            remain=row["remain_pct"],
        )
        try:
            place = LocationStore.ensure_ams_on(
                conn,
                printer_id,
                ams_id,
                tray_id,
                label=place_label(row["printer_name"], ams_id, tray_id),
            )
            filament.inventory.link_on(
                conn, place.id, printer_id, ams_id, tray_id, spool_id, seen, actor=body.actor
            )
        except NoAmsKindError as error:
            raise _refused(NO_AMS_KIND, str(error)) from None
        except (TrayTagClashError, TagKeysClashError) as error:
            raise _refused(TAG_CLASH, str(error)) from None
        except SpoolNotFoundError as error:
            raise _refused(SPOOL_MISSING, str(error)) from None
        spool = FilamentSpoolStore.read(conn, spool_id, defaults=filament.spools.defaults())
        assert spool is not None
        _first_weight(conn, filament.spools, spool, body)
        spool = FilamentSpoolStore.read(conn, spool_id, defaults=filament.spools.defaults())
        assert spool is not None
        return ResolveOutput(
            id=spool.id,
            number=spool.number,
            code=format_code("S", spool.number),
            location_id=spool.location_id,
        )


def resolve_unknown_tray_activity(
    filament: Callable[[], Filament], pool: Callable[[], ConnectionPool[Conn]]
) -> Callable[..., Any]:
    """``filament_resolve_unknown_tray`` for the ``library`` worker (`main._run_library_worker`)."""

    @activity.defn(name=RESOLVE_UNKNOWN_TRAY_ACTIVITY)
    async def filament_resolve_unknown_tray(body: ResolveInput) -> ResolveOutput:
        return await asyncio.to_thread(resolve_unknown_tray, filament(), pool(), body)

    return filament_resolve_unknown_tray
