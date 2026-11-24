"""One read function for every tag reading (#7273): a phone, an agent job or an AMS report
each hand it a `TagRead`; it finds or makes the tag, marks it seen, may bind it, and names
the spool that holds it."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import find_by_key
from scadbuddy.filament.spools import Conn, FilamentDefaults, FilamentSpoolStore, NoSpoolError
from scadbuddy.filament.tag_formats import KIND_OF, decode, format_of
from scadbuddy.spools.models import clean_tag_uid, clean_tray_uuid


class SpoolNotFoundError(LookupError):
    """The ``bind_spool_id`` names no spool."""

    def __init__(self, spool_id: UUID) -> None:
        super().__init__(f"no spool {spool_id}")
        self.spool_id = spool_id


class BindWithoutUidError(ValueError):
    """A ``bind_spool_id`` with no UID to bind."""

    code = "no_uid"

    def __init__(self) -> None:
        super().__init__("a tag with no UID cannot be bound to a spool")


#: How old a backup may be and still cover a write (#7273 R5).
BACKUP_TTL: Final = timedelta(minutes=10)


class NoUidError(ValueError):
    """A backup of a tag with no UID."""

    code = "no_uid"

    def __init__(self) -> None:
        super().__init__("a tag with no UID cannot be backed up")


class BackupMismatchError(ValueError):
    """The backup does not cover this write: unknown, another UID, too old, another reason
    or another job."""

    code = "backup_mismatch"

    def __init__(self) -> None:
        super().__init__("no current backup of this tag covers this write")


@dataclass(frozen=True)
class TagRead:
    """One reading of one tag, whoever made it."""

    source: m.TagSource
    uid: str | None
    tray_uuid: str | None = None
    tag_type: str | None = None
    records: tuple[m.NdefRecord, ...] = ()
    printer_id: UUID | None = None
    ams_id: int | None = None
    tray_id: int | None = None


def holder_by_keys(conn: Conn, tray_uuid: str | None, tag_uid: str | None) -> UUID | None:
    """The unarchived spool with an open binding of the tray UUID, else of the UID, on
    ``conn``. The keys are cleaned already."""
    for column, key in (("tray_uuid", tray_uuid), ("tag_uid", tag_uid)):
        if key is None:
            continue
        row = conn.execute(
            "SELECT b.spool_id FROM filament_tags t"
            " JOIN filament_tag_bindings b ON b.tag_id = t.id AND b.unbound_at IS NULL"
            " JOIN filament_spools s ON s.id = b.spool_id AND s.archived_at IS NULL"
            f" WHERE t.{column} = %s",
            (key,),
        ).fetchone()
        if row is not None:
            holder: UUID = row["spool_id"]
            return holder
    return None


def kind_of(read: TagRead) -> m.TagKind:
    """The kind a reading claims: a Bambu tray UUID is a MIFARE tag; else the kind of the
    format its records hold (`tag_formats.format_of`), else ``other``."""
    if clean_tray_uuid(read.tray_uuid) is not None:
        return "bambu_mifare"
    fmt = format_of(read.records)
    return "other" if fmt is None else KIND_OF[fmt]


def _set_kind(conn: Conn, tag_id: UUID, read: TagRead) -> None:
    """Give the tag the kind its records' format names (R13). A Bambu tag keeps its kind."""
    fmt = format_of(read.records)
    if fmt is not None:
        conn.execute(
            "UPDATE filament_tags SET kind = %s"
            " WHERE id = %s AND kind <> 'bambu_mifare' AND kind <> %s",
            (KIND_OF[fmt], tag_id, KIND_OF[fmt]),
        )


#: The decoded values a draft copies as they are.
_DRAFT_FIELDS: Final = (
    "colour_name",
    "rgba",
    "extra_colours",
    "diameter_mm",
    "density_g_cm3",
    "nozzle_temp_min",
    "nozzle_temp_max",
    "bed_temp_min",
    "bed_temp_max",
    "drying_temp_c",
    "drying_hours",
    "label_weight_g",
)


def draft_of(conn: Conn, decoded: m.DecodedSpool) -> m.SpoolCreate | None:
    """A new spool as ``decoded`` describes it, its material, brand and subtype found in
    the catalog by key (R12); ``None`` when no material is found. A value a spool cannot
    hold (a temperature over 1000) is left out."""
    material_id = None if not decoded.material else find_by_key(conn, "materials", decoded.material)
    if material_id is None:
        return None
    values: dict[str, object] = {
        "material_id": material_id,
        "brand_id": None if not decoded.brand else find_by_key(conn, "brands", decoded.brand),
        "subtype_id": None
        if not decoded.subtype
        else find_by_key(conn, "subtypes", decoded.subtype, material=material_id),
    }
    values |= {f: getattr(decoded, f) for f in _DRAFT_FIELDS}
    try:
        return m.SpoolCreate.model_validate(values)
    except ValidationError as exc:
        for error in exc.errors():
            values.pop(str(error["loc"][0]), None)
    return m.SpoolCreate.model_validate(values)


def seen_on(conn: Conn, read: TagRead) -> tuple[UUID | None, UUID | None]:
    """``(tag_id, holder_spool_id)`` of a reading, on ``conn``'s transaction, with the
    tag marked seen. A phone or an agent with a UID makes the tag; an AMS report only
    finds it. An AMS report skips the mark when the tag row is locked: its caller holds
    spools, and binding locks the tag before them, so waiting could deadlock."""
    uid = clean_tag_uid(read.uid)
    tray = clean_tray_uuid(read.tray_uuid)
    if read.source == "ams":
        tag_id = _find(conn, uid, tray)
        if tag_id is not None:
            locked = conn.execute(
                "SELECT id FROM filament_tags WHERE id = %s FOR UPDATE SKIP LOCKED", (tag_id,)
            ).fetchone()
            if locked is not None:
                _mark(conn, tag_id, read)
    elif uid is None:
        return None, None
    else:
        tag_id = FilamentSpoolStore.tag_on(conn, uid, tray, kind_of(read))
        _set_kind(conn, tag_id, read)
        _mark(conn, tag_id, read)
    if tag_id is None:
        return None, None
    return tag_id, holder_by_keys(conn, tray, uid)


def _find(conn: Conn, uid: str | None, tray: str | None) -> UUID | None:
    """The tag the tray UUID names, else the one the UID names (as `holder_by_keys`)."""
    for column, key in (("tray_uuid", tray), ("tag_uid", uid)):
        if key is None:
            continue
        row = conn.execute(f"SELECT id FROM filament_tags WHERE {column} = %s", (key,)).fetchone()
        if row is not None:
            found: UUID = row["id"]
            return found
    return None


def _mark(conn: Conn, tag_id: UUID, read: TagRead) -> None:
    conn.execute(
        "UPDATE filament_tags SET last_seen_at = now(), last_seen_source = %s,"
        " last_seen_records = %s WHERE id = %s",
        (read.source, Jsonb([r.model_dump() for r in read.records]), tag_id),
    )


def read_on(
    conn: Conn,
    read: TagRead,
    *,
    actor: str,
    bind_spool_id: UUID | None = None,
    marks_origin: bool = True,
    defaults: FilamentDefaults,
) -> m.TagReadResult:
    """Record a reading on ``conn``'s transaction and say what to do next. With
    ``bind_spool_id`` the tag is bound to that spool, which then holds it."""
    if bind_spool_id is not None:
        if read.source == "ams":
            raise ValueError("an AMS report never binds a tag")
        if clean_tag_uid(read.uid) is None:
            raise BindWithoutUidError()
    tag_id, holder = seen_on(conn, read)
    if bind_spool_id is not None:
        try:
            FilamentSpoolStore.bind_on(
                conn,
                bind_spool_id,
                clean_tag_uid(read.uid),
                None,
                kind_of(read),
                None,
                actor,
                marks_origin=marks_origin,
            )
        except NoSpoolError:
            raise SpoolNotFoundError(bind_spool_id) from None
        holder = bind_spool_id
    spool = None if holder is None else FilamentSpoolStore.read(conn, holder, defaults=defaults)
    tag = None if tag_id is None else FilamentSpoolStore.tag_row_on(conn, tag_id)
    next_step: Literal["open_spool", "bind_or_create", "none"] = (
        "open_spool" if spool else "bind_or_create" if tag else "none"
    )
    decoded = decode(read.records)
    draft = None if decoded is None or holder is not None else draft_of(conn, decoded)
    return m.TagReadResult(tag=tag, spool=spool, decoded=decoded, draft=draft, next=next_step)


def read(
    spools: FilamentSpoolStore,
    read: TagRead,
    *,
    actor: str,
    bind_spool_id: UUID | None = None,
    defaults: FilamentDefaults,
) -> m.TagReadResult:
    """`read_on` in one transaction."""
    with spools.transaction() as conn:
        return read_on(conn, read, actor=actor, bind_spool_id=bind_spool_id, defaults=defaults)


_BACKUP_COLUMNS: Final = "id, tag_id, uid, records, reason, job_id, actor, at"


def _backup_of(row: dict[str, Any]) -> m.TagBackup:
    return m.TagBackup.model_validate(row)


def back_up(spools: FilamentSpoolStore, body: m.TagBackupIn, *, actor: str) -> m.TagBackup:
    """Keep what a tag holds, before a write. The tag is found or made by its UID."""
    uid = clean_tag_uid(body.uid)
    if uid is None:
        raise NoUidError()
    source: m.TagSource = "agent" if body.reason == "agent_write" else "phone"
    with spools.transaction() as conn:
        tag_id, _ = seen_on(conn, TagRead(source=source, uid=uid, records=tuple(body.records)))
        row = conn.execute(
            "INSERT INTO filament_tag_backups (tag_id, uid, records, reason, job_id, actor)"
            f" VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_BACKUP_COLUMNS}",
            (
                tag_id,
                uid,
                Jsonb([r.model_dump() for r in body.records]),
                body.reason,
                body.job_id,
                actor,
            ),
        ).fetchone()
    assert row is not None
    return _backup_of(row)


def backups(spools: FilamentSpoolStore, tag_id: UUID) -> list[m.TagBackup] | None:
    """A tag's backups, newest first; ``None`` for no such tag."""
    with spools.transaction() as conn:
        if conn.execute("SELECT 1 FROM filament_tags WHERE id = %s", (tag_id,)).fetchone() is None:
            return None
        rows = conn.execute(
            f"SELECT {_BACKUP_COLUMNS} FROM filament_tag_backups WHERE tag_id = %s"
            " ORDER BY at DESC, id DESC",
            (tag_id,),
        ).fetchall()
    return [_backup_of(r) for r in rows]


def valid_backup(
    conn: Conn,
    backup_id: UUID,
    uid: str,
    reason: m.BackupReason,
    *,
    job_id: UUID | None = None,
) -> m.TagBackup:
    """The backup, when it covers one write of this UID for this reason (#7273 R5)."""
    row = conn.execute(
        f"SELECT {_BACKUP_COLUMNS}, now() - at AS age FROM filament_tag_backups WHERE id = %s",
        (backup_id,),
    ).fetchone()
    if row is None:
        raise BackupMismatchError()
    age = row.pop("age")
    found = _backup_of(row)
    if (
        clean_tag_uid(uid) != found.uid
        or age > BACKUP_TTL
        or found.reason != reason
        or (reason == "agent_write" and found.job_id != job_id)
    ):
        raise BackupMismatchError()
    return found
