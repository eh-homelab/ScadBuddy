"""NTAG tags through SpoolBuddy: scan, write, confirm and cancel (#6264).

A SpoolBuddy reader is a Bambuddy device. A scan listens on Bambuddy's WebSocket
(`BambuddyClient.next_tag_scan`); a write queues one on the device
(`queue_tag_write`), and the person holds a blank tag to the reader. Bambuddy then
records the tag on its spool with ``data_origin`` ``opentag3d``. ScadBuddy binds the tag
to its own spool only in `confirm`, after it has read that record: `write` changes no
binding."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import TagScan
from scadbuddy.filament import models as m
from scadbuddy.filament.component import Filament
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.spools.models import clean_tag_uid, clean_tray_uuid

#: Bambuddy's ``data_origin`` for a tag it wrote itself (``data_origin_values`` in
#: ``tests/bambuddy/recordings/openapi/rfid-routes.json``).
OPENTAG_ORIGIN = "opentag3d"


class NtagNoBambuddySpoolError(ValueError):
    """The spool has no Bambuddy record to write a tag for."""

    code = "no_bambuddy_spool"

    def __init__(self) -> None:
        super().__init__("the spool is not linked to a Bambuddy spool; import from Bambuddy first")


class NtagOverrideOpenError(ValueError):
    """The spool's open tag is an override: its values stand over the tag's, and a write
    would replace that tag."""

    code = "override_tag_open"

    def __init__(self) -> None:
        super().__init__(
            "the spool's tag is an override tag; write with replace to put a new tag on it"
        )


def scan_kind(scan: TagScan) -> m.TagKind:
    """A tray UUID names a Bambu tag; an NTAG chip type, an OpenTag3D tag."""
    if scan.tray_uuid:
        return "bambu_mifare"
    if scan.tag_type is not None and scan.tag_type.upper().startswith("NTAG"):
        return "ntag_opentag"
    return "other"


def _holder(
    store: Filament, uid: str | None, tray: str | None, bb_spool_id: int | None
) -> tuple[m.SpoolRef, m.TagKind | None] | None:
    """The SB spool whose open binding holds the keys, with that tag's kind, else the one
    linked to ``bb_spool_id`` (no kind)."""
    with store.spools.transaction() as conn:
        row = None
        if uid is not None or tray is not None:
            row = conn.execute(
                "SELECT s.id, s.number, t.kind FROM filament_tags t"
                " JOIN filament_tag_bindings b ON b.tag_id = t.id AND b.unbound_at IS NULL"
                " JOIN filament_spools s ON s.id = b.spool_id"
                " WHERE t.tag_uid = %s OR t.tray_uuid = %s",
                (uid, tray),
            ).fetchone()
        if row is not None:
            return m.SpoolRef(id=row["id"], number=row["number"]), row["kind"]
        if bb_spool_id is not None:
            row = conn.execute(
                "SELECT s.id, s.number FROM filament_bb_links l"
                " JOIN filament_spools s ON s.id = l.spool_id WHERE l.bb_spool_id = %s",
                (bb_spool_id,),
            ).fetchone()
    return None if row is None else (m.SpoolRef(id=row["id"], number=row["number"]), None)


async def scan(
    store: Filament, client: BambuddyClient, *, timeout: float, device_id: str | None
) -> m.ScanResult:
    """Wait up to ``timeout`` seconds for a tag at a reader and say what it is. A matched
    frame carries no chip type, so its kind is the bound tag's, else ``ntag_opentag`` when
    Bambuddy's record says OpenTag3D wrote it, else ``other``."""
    found = await client.next_tag_scan(timeout=timeout, device_id=device_id)
    if found is None:
        return m.ScanResult(status="timeout")
    held = await asyncio.to_thread(
        _holder,
        store,
        clean_tag_uid(found.tag_uid),
        clean_tray_uuid(found.tray_uuid),
        found.bb_spool_id,
    )
    kind = scan_kind(found)
    if found.bb_spool_id is not None and not found.tray_uuid and found.tag_type is None:
        # A matched frame names no chip type: the tag's own binding says what it is, else
        # the origin of Bambuddy's record does.
        if held is not None and held[1] is not None:
            kind = held[1]
        elif (await client.spool_row(found.bb_spool_id)).data_origin == OPENTAG_ORIGIN:
            kind = "ntag_opentag"
    return m.ScanResult(
        status="tag",
        tag_uid=found.tag_uid,
        tray_uuid=found.tray_uuid,
        tag_type=found.tag_type,
        kind=kind,
        bambuddy_spool_id=found.bb_spool_id,
        sb_spool=None if held is None else held[0],
    )


def _linked(store: Filament, spool_id: UUID) -> tuple[int | None, bool]:
    """The spool's Bambuddy id and whether its open binding is an override."""
    with store.spools.transaction() as conn:
        link = conn.execute(
            "SELECT bb_spool_id FROM filament_bb_links WHERE spool_id = %s", (spool_id,)
        ).fetchone()
        bound = conn.execute(
            "SELECT override FROM filament_tag_bindings WHERE spool_id = %s AND unbound_at IS NULL",
            (spool_id,),
        ).fetchone()
    return (
        None if link is None else link["bb_spool_id"],
        bound is not None and bound["override"],
    )


def _record_baseline(store: Filament, spool_id: UUID, device_id: str, baseline: str | None) -> None:
    with store.spools.transaction() as conn:
        conn.execute(
            "INSERT INTO filament_ntag_writes (spool_id, device_id, baseline_uid)"
            " VALUES (%s, %s, %s)"
            " ON CONFLICT (spool_id) DO UPDATE SET baseline_uid = EXCLUDED.baseline_uid,"
            " device_id = EXCLUDED.device_id, queued_at = now()",
            (spool_id, device_id, baseline),
        )


def _baseline(store: Filament, spool_id: UUID) -> tuple[bool, str | None]:
    """Whether a write is queued for the spool, and the UID its record held then."""
    with store.spools.transaction() as conn:
        row = conn.execute(
            "SELECT baseline_uid FROM filament_ntag_writes WHERE spool_id = %s", (spool_id,)
        ).fetchone()
    return (row is not None, None if row is None else row["baseline_uid"])


def _written(store: Filament, spool_id: UUID, encoded_at: datetime | None, actor: str) -> None:
    """#7149 R-7149-1, R-7149-3: the spool's tag was written by OpenTag3D, when Bambuddy's
    record says (its ``encode_time``), else now."""
    with store.spools.transaction() as conn:
        FilamentSpoolStore.set_origin(
            conn,
            spool_id,
            actor,
            data_origin=OPENTAG_ORIGIN,
            tag_encoded_at=encoded_at or datetime.now(UTC),
        )


def _clear_baseline(store: Filament, spool_id: UUID) -> None:
    with store.spools.transaction() as conn:
        conn.execute("DELETE FROM filament_ntag_writes WHERE spool_id = %s", (spool_id,))


def _clear_device(store: Filament, spool_id: UUID, device_id: str) -> None:
    with store.spools.transaction() as conn:
        conn.execute(
            "DELETE FROM filament_ntag_writes WHERE device_id = %s"
            " OR (spool_id = %s AND (device_id = %s OR device_id IS NULL))",
            (device_id, spool_id, device_id),
        )


async def write(
    store: Filament, client: BambuddyClient, spool_id: UUID, device_id: str, *, replace: bool
) -> m.NtagQueued:
    """Queue one tag write for the spool's Bambuddy record on the device. Changes no
    binding; it records the tag UID the Bambuddy record holds now, which `confirm` ignores.
    `NtagNoBambuddySpoolError` for a spool with no link, `NtagOverrideOpenError`
    for one with an override tag unless ``replace``."""
    bb_spool_id, override = await asyncio.to_thread(_linked, store, spool_id)
    if bb_spool_id is None:
        raise NtagNoBambuddySpoolError()
    if override and not replace:
        raise NtagOverrideOpenError()
    baseline = clean_tag_uid((await client.spool_row(bb_spool_id)).tag_uid)
    warnings = await client.queue_tag_write(device_id, bb_spool_id)
    await asyncio.to_thread(_record_baseline, store, spool_id, device_id, baseline)
    return m.NtagQueued(status="queued", warnings=warnings)


async def confirm(
    store: Filament, client: BambuddyClient, spool_id: UUID, *, actor: str
) -> m.NtagConfirm:
    """Bind the tag Bambuddy wrote on the spool's record: a ``tag_uid`` set, written by
    OpenTag3D (``data_origin``) and not the one the record held when `write` queued. The
    spool then keeps that origin and the record's ``encode_time`` (`_written`, #7149). With
    no queued write, or otherwise, change nothing."""
    bb_spool_id, _ = await asyncio.to_thread(_linked, store, spool_id)
    if bb_spool_id is None:
        raise NtagNoBambuddySpoolError()
    queued, baseline = await asyncio.to_thread(_baseline, store, spool_id)
    if not queued:
        return m.NtagConfirm(bound=False)
    record = await client.spool_row(bb_spool_id)
    uid = clean_tag_uid(record.tag_uid)
    if uid is None or record.data_origin != OPENTAG_ORIGIN or uid == baseline:
        return m.NtagConfirm(bound=False)
    bound = await asyncio.to_thread(
        lambda: store.spools.bind_tag(spool_id, tag_uid=uid, kind="ntag_opentag", actor=actor)
    )
    if bound is not None:
        await asyncio.to_thread(_written, store, spool_id, record.encode_time, actor)
        await asyncio.to_thread(_clear_baseline, store, spool_id)
    return m.NtagConfirm(bound=bound is not None, tag_uid=uid if bound is not None else None)


async def cancel(store: Filament, client: BambuddyClient, spool_id: UUID, device_id: str) -> None:
    """Drop the write the device has queued, the record of every write queued on that
    device, and the spool's own record if it names no device, so a later `confirm` binds
    nothing for them. The spool's write queued on another device is kept."""
    await client.cancel_tag_write(device_id)
    await asyncio.to_thread(_clear_device, store, spool_id, device_id)
