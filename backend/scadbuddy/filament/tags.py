"""One read function for every tag reading (#7273): a phone, an agent job or an AMS report
each hand it a `TagRead`; it finds or makes the tag, marks it seen, may bind it, and names
the spool that holds it."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import find_by_key
from scadbuddy.filament.spools import Conn, FilamentDefaults, FilamentSpoolStore, NoSpoolError
from scadbuddy.filament.tag_formats import (
    KIND_OF,
    ORIGIN_OF,
    TagFormat,
    decode,
    encode,
    facts_of,
    format_of,
    pick_format,
)
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
    """Keep what a tag holds, before a write. The tag is found or made by its UID. An
    ``agent_write`` backup needs its job to be an armed ``write`` (`JobNotArmedError`)."""
    uid = clean_tag_uid(body.uid)
    if uid is None:
        raise NoUidError()
    # `seen_on` finds or makes the tag row the backup points at, and marks it seen with the
    # pre-write records: they are what the reader saw just now, from the phone, or from the
    # agent's job for an `agent_write`.
    source: m.TagSource = "agent" if body.reason == "agent_write" else "phone"
    with spools.transaction() as conn:
        if body.reason == "agent_write":
            # Before `seen_on`: a job's lock comes before its tag's, as in `finish_job`.
            expire_jobs(conn)
            job = conn.execute(
                "SELECT kind, state FROM filament_nfc_jobs WHERE id = %s FOR SHARE",
                (body.job_id,),
            ).fetchone()
            if job is None or job["kind"] != "write" or job["state"] != "armed":
                raise JobNotArmedError()
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


def backups(
    spools: FilamentSpoolStore, tag_id: UUID, *, limit: int = 50, before: UUID | None = None
) -> list[m.TagBackup] | None:
    """Up to ``limit`` of a tag's backups, newest first, those older than backup ``before``
    when it is given (R18); ``None`` for no such tag."""
    with spools.transaction() as conn:
        if conn.execute("SELECT 1 FROM filament_tags WHERE id = %s", (tag_id,)).fetchone() is None:
            return None
        rows = conn.execute(
            f"SELECT {_BACKUP_COLUMNS} FROM filament_tag_backups WHERE tag_id = %s"
            " AND (%s::uuid IS NULL OR (at, id) < (SELECT at, id FROM filament_tag_backups AS b"
            "  WHERE b.id = %s AND b.tag_id = %s))"
            " ORDER BY at DESC, id DESC LIMIT %s",
            (tag_id, before, before, tag_id, limit),
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


class ReadBackMismatchError(ValueError):
    """The tag does not hold what the phone sent."""

    code = "read_back_mismatch"

    def __init__(self) -> None:
        super().__init__("the tag does not hold what was sent")


class SentMismatchError(ValueError):
    """What the phone sent is not what ScadBuddy would write for this spool now."""

    code = "sent_mismatch"

    def __init__(self) -> None:
        super().__init__("the records sent are not what ScadBuddy writes for this spool now")


class UnknownFormatError(ValueError):
    """The records sent are not one OpenSpool or OpenTag3D record."""

    code = "unknown_format"

    def __init__(self) -> None:
        super().__init__("the records sent are not one OpenSpool or OpenTag3D record")


class TagBackupNotFoundError(LookupError):
    """No such tag, or no such backup of it."""


def same_records(a: Sequence[m.NdefRecord], b: Sequence[m.NdefRecord]) -> bool:
    """Two record lists are equal when their ``(record_type, media_type, data_hex)``
    triples are, in order (``data_hex`` is lower case already)."""
    return [(r.record_type, r.media_type, r.data_hex) for r in a] == [
        (r.record_type, r.media_type, r.data_hex) for r in b
    ]


def _encoded(
    spool: m.Spool, fmt: TagFormat, empty_weight_g: Callable[[UUID], int | None]
) -> m.TagRecords:
    insert = spool.effective.insert_type_id
    weight = None if insert is None else empty_weight_g(insert)
    encoded = encode(fmt, facts_of(spool, weight))
    return m.TagRecords(format=encoded.format, records=encoded.records, warnings=encoded.warnings)


def records_for_spool(
    spools: FilamentSpoolStore,
    spool_id: UUID,
    body: m.TagRecordsIn,
    *,
    default: TagFormat,
    empty_weight_g: Callable[[UUID], int | None],
    defaults: FilamentDefaults,
) -> m.TagRecords | None:
    """The records that put the spool on the tag, in the tag's own format, else
    ``default``; ``None`` for no such spool. The backup must cover this write: it holds
    the records the tag holds now (R19)."""
    with spools.transaction() as conn:
        backup = valid_backup(conn, body.backup_id, body.uid, "spool_write")
        if not same_records(body.records, backup.records):
            raise BackupMismatchError()
        spool = FilamentSpoolStore.read(conn, spool_id, defaults=defaults)
    if spool is None:
        return None
    return _encoded(spool, pick_format(body.records, default), empty_weight_g)


def written(
    spools: FilamentSpoolStore,
    spool_id: UUID,
    body: m.TagWrittenIn,
    *,
    actor: str,
    default: TagFormat,
    defaults: FilamentDefaults,
    empty_weight_g: Callable[[UUID], int | None],
) -> m.TagReadResult:
    """Check a write and bind the tag, in one transaction: the backup covers it, the tag
    holds what was sent, and what was sent is what `records_for_spool` gives now, in the
    format it would choose (R20). The check runs after the bind, which holds the spool's
    lock, so no edit slips in between; a refusal rolls the bind back. The spool then
    holds the tag, with the format's origin and the write's time (R11).
    `SpoolNotFoundError` for no such spool."""
    with spools.transaction() as conn:
        backup = valid_backup(conn, body.backup_id, body.uid, "spool_write")
        if not same_records(body.sent, body.read_back):
            raise ReadBackMismatchError()
        fmt = format_of(body.sent) if len(body.sent) == 1 else None
        if fmt is None:
            raise UnknownFormatError()
        result = read_on(
            conn,
            TagRead(source="phone", uid=body.uid, records=tuple(body.read_back)),
            actor=actor,
            bind_spool_id=spool_id,
            marks_origin=False,
            defaults=defaults,
        )
        assert result.spool is not None
        want = _encoded(result.spool, pick_format(backup.records, default), empty_weight_g)
        if not same_records(body.sent, want.records):
            raise SentMismatchError()
        FilamentSpoolStore.set_origin(
            conn,
            spool_id,
            actor,
            data_origin=ORIGIN_OF[fmt],
            tag_encoded_at=datetime.now(UTC),
        )
        # The origin changed the spool row after `read_on` read it.
        return result.model_copy(
            update={"spool": FilamentSpoolStore.read(conn, spool_id, defaults=defaults)}
        )


def restore_records(
    spools: FilamentSpoolStore, tag_id: UUID, backup_id: UUID, body: m.TagRecordsIn
) -> m.TagRecords:
    """The records of an old backup, to write back. ``body.backup_id`` is the fresh
    backup (reason ``restore``) of the tag as it is now, and is of this tag.
    `TagBackupNotFoundError` for no such tag or backup."""
    with spools.transaction() as conn:
        old = conn.execute(
            "SELECT records FROM filament_tag_backups WHERE id = %s AND tag_id = %s",
            (backup_id, tag_id),
        ).fetchone()
        if old is None:
            raise TagBackupNotFoundError()
        fresh = valid_backup(conn, body.backup_id, body.uid, "restore")
        if fresh.tag_id != tag_id or not same_records(body.records, fresh.records):
            raise BackupMismatchError()
    records = [m.NdefRecord.model_validate(r) for r in old["records"]]
    return m.TagRecords(format=format_of(records), records=records)


class NfcJobNotFoundError(LookupError):
    """No such NFC job."""


class JobNotPendingError(ValueError):
    """Only a pending NFC job can be armed."""

    code = "job_not_pending"

    def __init__(self) -> None:
        super().__init__("the NFC job is not pending: another page armed it, or it ended")


class JobNotArmedError(ValueError):
    """Only an armed NFC job takes a result, and only an armed write job a backup."""

    code = "job_not_armed"

    def __init__(self) -> None:
        super().__init__("no armed NFC job of this kind: it is pending, ended or unknown")


class RecordsMismatchError(ValueError):
    """A write job's claim names no hash, or not the hash of the job's records (#7273 R22)."""

    code = "records_mismatch"

    def __init__(self) -> None:
        super().__init__(
            "`records_sha256` is not the hash of this write job's records: show the job again"
        )


class RecordsHashNotTakenError(ValueError):
    """A read job's claim names a records hash; a read job has no records."""

    code = "records_sha256_not_taken"

    def __init__(self) -> None:
        super().__init__("a read job has no records, so its claim takes no `records_sha256`")


_JOB_COLUMNS: Final = (
    "id, kind, records, created_by, state, result, created_at, expires_at, armed_by"
)


def _job_of(row: dict[str, Any]) -> m.NfcJob:
    return m.NfcJob.model_validate(row)


def expire_jobs(conn: Conn) -> None:
    """End every open job whose time is up. Every job function runs it first."""
    conn.execute(
        "UPDATE filament_nfc_jobs SET state = 'expired'"
        " WHERE state IN ('pending', 'armed') AND expires_at <= now()"
    )


def create_job(spools: FilamentSpoolStore, body: m.NfcJobIn, *, actor: str) -> m.NfcJob:
    """Queue a job for the phone page; it waits ``ttl_s`` seconds for a person to arm it
    and finish it."""
    records = None if body.records is None else Jsonb([r.model_dump() for r in body.records])
    with spools.transaction() as conn:
        expire_jobs(conn)
        row = conn.execute(
            "INSERT INTO filament_nfc_jobs (kind, records, created_by, expires_at)"
            " VALUES (%s, %s, %s, now() + make_interval(secs => %s))"
            f" RETURNING {_JOB_COLUMNS}",
            (body.kind, records, actor, body.ttl_s),
        ).fetchone()
    assert row is not None
    return _job_of(row)


def pending_jobs(spools: FilamentSpoolStore) -> list[m.NfcJob]:
    """The jobs no page armed yet, oldest first."""
    with spools.transaction() as conn:
        expire_jobs(conn)
        rows = conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM filament_nfc_jobs WHERE state = 'pending'"
            " ORDER BY created_at, id"
        ).fetchall()
    return [_job_of(r) for r in rows]


def get_job(spools: FilamentSpoolStore, job_id: UUID) -> m.NfcJob | None:
    with spools.transaction() as conn:
        expire_jobs(conn)
        row = conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM filament_nfc_jobs WHERE id = %s", (job_id,)
        ).fetchone()
    return None if row is None else _job_of(row)


def _locked_job(conn: Conn, job_id: UUID) -> m.NfcJob:
    row = conn.execute(
        f"SELECT {_JOB_COLUMNS} FROM filament_nfc_jobs WHERE id = %s FOR UPDATE", (job_id,)
    ).fetchone()
    if row is None:
        raise NfcJobNotFoundError()
    return _job_of(row)


def _set_job(conn: Conn, job_id: UUID, state: m.NfcJobState, result: dict[str, Any]) -> m.NfcJob:
    row = conn.execute(
        f"UPDATE filament_nfc_jobs SET state = %s, result = %s WHERE id = %s"
        f" RETURNING {_JOB_COLUMNS}",
        (state, Jsonb(result), job_id),
    ).fetchone()
    assert row is not None
    return _job_of(row)


def claim_job(
    spools: FilamentSpoolStore, job_id: UUID, body: m.NfcJobClaimIn | None, *, actor: str
) -> m.NfcJob:
    """Arm a pending job for the page whose person tapped Arm. `JobNotPendingError` for a
    job in any other state, so one page runs it; `NfcJobNotFoundError` for no such job. A
    write job is armed only with the hash of its records (#7273 R22): a missing or other
    one is `RecordsMismatchError`, and the job stays pending. A read job takes no hash
    (`RecordsHashNotTakenError`)."""
    sent = None if body is None else body.records_sha256
    with spools.transaction() as conn:
        expire_jobs(conn)
        job = _locked_job(conn, job_id)
        if job.state != "pending":
            raise JobNotPendingError()
        if job.records is None:
            if sent is not None:
                raise RecordsHashNotTakenError()
        elif sent != m.records_sha256(job.records):
            raise RecordsMismatchError()
        row = conn.execute(
            "UPDATE filament_nfc_jobs SET state = 'armed', armed_by = %s WHERE id = %s"
            f" RETURNING {_JOB_COLUMNS}",
            (actor, job_id),
        ).fetchone()
    assert row is not None
    return _job_of(row)


def finish_job(
    spools: FilamentSpoolStore,
    job_id: UUID,
    body: m.NfcJobResultIn,
    *,
    actor: str,
    defaults: FilamentDefaults,
) -> m.NfcJob:
    """End an armed job with what the page got from the tag. `NfcJobNotFoundError` for no
    such job, `JobNotArmedError` for a job that is not armed. A read goes through `read_on`. A write
    needs its own ``agent_write`` backup (`BackupMismatchError`), and is done only when the
    read-back equals the records the job sent. Either way the tag is marked seen with the
    read-back, so a failed write still records what the tag holds."""
    with spools.transaction() as conn:
        expire_jobs(conn)
        job = _locked_job(conn, job_id)
        if job.state != "armed":
            raise JobNotArmedError()
        if body.error is not None:
            return _set_job(conn, job_id, "failed", {"error": body.error})
        records = [r.model_dump() for r in body.records]
        uid = clean_tag_uid(body.uid)
        reading = TagRead(source="agent", uid=body.uid, records=tuple(body.records))
        if job.kind == "read":
            found = read_on(conn, reading, actor=actor, defaults=defaults)
            result = found.model_dump(mode="json") | {"uid": uid, "records": records}
            return _set_job(conn, job_id, "done", result)
        if body.backup_id is None or body.uid is None:
            raise BackupMismatchError()
        valid_backup(conn, body.backup_id, body.uid, "agent_write", job_id=job_id)
        result = {"uid": uid, "records": records, "backup_id": str(body.backup_id)}
        seen_on(conn, reading)
        if not same_records(job.records or [], body.records):
            return _set_job(
                conn, job_id, "failed", {"error": str(ReadBackMismatchError())} | result
            )
        return _set_job(conn, job_id, "done", result)


def cancel_job(spools: FilamentSpoolStore, job_id: UUID) -> m.NfcJob:
    """End a pending or armed job as ``cancelled``: its agent stopped waiting, so no page
    may arm it or post its result (#7273 M1). A job that has ended already is answered as
    it is, unchanged. `NfcJobNotFoundError` for no such job."""
    with spools.transaction() as conn:
        expire_jobs(conn)
        job = _locked_job(conn, job_id)
        if job.state not in ("pending", "armed"):
            return job
        row = conn.execute(
            f"UPDATE filament_nfc_jobs SET state = 'cancelled' WHERE id = %s"
            f" RETURNING {_JOB_COLUMNS}",
            (job_id,),
        ).fetchone()
    assert row is not None
    return _job_of(row)
