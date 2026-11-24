"""One read function for every tag reading (#7273): a phone, an agent job or an AMS report
each hand it a `TagRead`; it finds or makes the tag, marks it seen, may bind it, and names
the spool that holds it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from psycopg.types.json import Jsonb

from scadbuddy.filament import models as m
from scadbuddy.filament.spools import Conn, FilamentDefaults, FilamentSpoolStore, NoSpoolError
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
    """The kind a reading claims: a Bambu tray UUID is a MIFARE tag; no other format is
    known yet."""
    if clean_tray_uuid(read.tray_uuid) is not None:
        return "bambu_mifare"
    return "other"


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
    return m.TagReadResult(tag=tag, spool=spool, decoded=None, draft=None, next=next_step)


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
