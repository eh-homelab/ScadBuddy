"""The one tag read function (#7273 Task 1)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from scadbuddy.filament import tags
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.models import (
    Material,
    NdefRecord,
    NfcJobClaimIn,
    NfcJobIn,
    NfcJobResultIn,
    SpoolCreate,
    TagBackup,
    TagBackupIn,
    TagRecordsIn,
    TagWrittenIn,
    records_sha256,
)
from scadbuddy.filament.spools import FilamentDefaults, FilamentSpoolStore, TagKeysClashError
from scadbuddy.filament.tags import TagRead
from scadbuddy.spools.models import SeenTray
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

DEFAULTS = FilamentDefaults(20, None)
_TRAY = "0123456789ABCDEF0123456789ABCDEF"


def _spool(store: FilamentSpoolStore, pool: PgPool) -> UUID:
    pla = next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )
    return store.create(SpoolCreate(material_id=pla), actor="ann").id


def _count(pool: PgPool, table: str) -> int:
    with pool.connection() as conn:
        row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
    assert row is not None
    return int(row["n"])


def _tag(pool: PgPool, tag_id: UUID | None) -> dict[str, Any]:
    with pool.connection() as conn:
        row = conn.execute("SELECT * FROM filament_tags WHERE id = %s", (tag_id,)).fetchone()
    assert row is not None
    return dict(row)


def test_a_phone_read_of_a_new_uid_makes_the_tag_and_marks_it_seen(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    result = tags.read(
        store, TagRead(source="phone", uid="04:a2:b3:c4:d5:e6:f7"), actor="ann", defaults=DEFAULTS
    )
    assert result.tag is not None and result.tag.tag_uid == "04A2B3C4D5E6F7"
    assert result.tag.last_seen_source == "phone" and result.tag.last_seen_at is not None
    assert result.spool is None and result.next == "bind_or_create"


def test_a_read_of_a_bound_uid_names_its_spool(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    store.bind_tag(spool, tag_uid="04A2B3C4D5E6F7", kind="other", actor="ann")
    result = tags.read(
        store, TagRead(source="phone", uid="04a2b3c4d5e6f7"), actor="ann", defaults=DEFAULTS
    )
    assert result.spool is not None and result.spool.id == spool and result.next == "open_spool"


def test_a_read_with_no_uid_makes_no_tag(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    result = tags.read(store, TagRead(source="phone", uid=""), actor="ann", defaults=DEFAULTS)
    assert (result.tag, result.spool, result.next) == (None, None, "none")
    assert _count(pg_pool, "filament_tags") == 0


def test_a_read_with_no_records_claims_no_format(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    result = tags.read(
        store, TagRead(source="phone", uid="A1B2C3D4"), actor="ann", defaults=DEFAULTS
    )
    assert result.tag is not None
    assert result.decoded is None and result.tag.kind == "other"


def test_a_read_with_bind_spool_id_binds_and_books_tag_bound(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    result = tags.read(
        store,
        TagRead(source="phone", uid="04AA"),
        actor="ann",
        bind_spool_id=spool,
        defaults=DEFAULTS,
    )
    assert result.spool is not None and result.spool.id == spool and result.next == "open_spool"
    kinds = [e.kind for e in store.events(spool)]
    assert kinds[-2:] == ["tag_bound", "edited"]
    assert kinds.count("tag_bound") == 1


def test_a_bind_with_no_uid_is_refused(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    with pytest.raises(tags.BindWithoutUidError):
        tags.read(
            store,
            TagRead(source="phone", uid=None),
            actor="ann",
            bind_spool_id=spool,
            defaults=DEFAULTS,
        )


def test_an_ams_read_never_makes_a_tag(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    with store.transaction() as conn:
        assert tags.seen_on(conn, TagRead(source="ams", uid="AABB", tray_uuid="0123")) == (
            None,
            None,
        )
    assert _count(pg_pool, "filament_tags") == 0


def test_an_ams_read_of_a_bound_tray_marks_it_seen(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    store.bind_tag(spool, tray_uuid=_TRAY, kind="bambu_mifare", actor="ann")
    with store.transaction() as conn:
        tag_id, holder = tags.seen_on(conn, TagRead(source="ams", uid=None, tray_uuid=_TRAY))
    assert holder == spool and _tag(pg_pool, tag_id)["last_seen_source"] == "ams"


def test_an_ams_read_keeps_what_a_phone_read_and_skips_an_unchanged_mark(
    pg_pool: PgPool,
) -> None:
    # #7419 review: an AMS sighting keeps `last_seen_records`, and a second one within
    # `AMS_SEEN_THROTTLE_SECONDS` writes nothing.
    store = FilamentSpoolStore(pg_pool)
    record = NdefRecord(record_type="mime", data_hex="7b7d")
    spool = _spool(store, pg_pool)
    store.bind_tag(spool, tag_uid="04AA", kind="other", actor="ann")
    read = tags.read(
        store,
        TagRead(source="phone", uid="04AA", records=(record,)),
        actor="ann",
        defaults=DEFAULTS,
    )
    assert read.tag is not None
    with store.transaction() as conn:
        tags.seen_on(conn, TagRead(source="ams", uid="04AA"))
    first = _tag(pg_pool, read.tag.id)
    assert first["last_seen_source"] == "ams"
    assert first["last_seen_records"][0]["data_hex"] == "7b7d"
    with pg_pool.connection() as conn:
        xmin = conn.execute(
            "SELECT xmin::text AS x FROM filament_tags WHERE id = %s", (read.tag.id,)
        ).fetchone()
    with store.transaction() as conn:
        tags.seen_on(conn, TagRead(source="ams", uid="04AA"))
    with pg_pool.connection() as conn:
        again = conn.execute(
            "SELECT xmin::text AS x FROM filament_tags WHERE id = %s", (read.tag.id,)
        ).fetchone()
    assert xmin == again
    assert _tag(pg_pool, read.tag.id)["last_seen_at"] == first["last_seen_at"]
    # Past the throttle the mark is written again.
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_tags SET last_seen_at = now() - make_interval(secs => %s)"
            " WHERE id = %s",
            (tags.AMS_SEEN_THROTTLE_SECONDS + 1, read.tag.id),
        )
    with store.transaction() as conn:
        tags.seen_on(conn, TagRead(source="ams", uid="04AA"))
    assert _tag(pg_pool, read.tag.id)["last_seen_at"] > first["last_seen_at"]


def test_the_inventory_finds_a_tray_holder_through_the_same_lookup(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    store.bind_tag(spool, tray_uuid=_TRAY, kind="bambu_mifare", actor="ann")
    seen = SeenTray(ams_id=0, tray_id=0, tray_uuid=_TRAY, tag_uid=None)
    with store.transaction() as conn:
        by_keys = tags.holder_by_keys(conn, _TRAY, None)
        found = FilamentInventory.holder_on(conn, UUID(int=1), seen)
    assert by_keys == spool and found == (spool, "tag")


def test_a_read_naming_two_tags_is_a_key_clash(pg_pool: PgPool) -> None:
    # The phone route sends a UID only, so only an agent or AMS read can name two tags.
    store = FilamentSpoolStore(pg_pool)
    store.bind_tag(_spool(store, pg_pool), tray_uuid=_TRAY, kind="bambu_mifare", actor="ann")
    store.bind_tag(_spool(store, pg_pool), tag_uid="04AA", kind="other", actor="ann")
    with pytest.raises(TagKeysClashError):
        tags.read(
            store,
            TagRead(source="agent", uid="04aa", tray_uuid=_TRAY),
            actor="ann",
            defaults=DEFAULTS,
        )


def test_an_ams_read_skips_the_mark_on_a_locked_tag_without_waiting(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    store.bind_tag(_spool(store, pg_pool), tray_uuid=_TRAY, kind="bambu_mifare", actor="ann")
    with pg_pool.connection() as holder, holder.transaction():
        holder.execute("SELECT 1 FROM filament_tags WHERE tray_uuid = %s FOR UPDATE", (_TRAY,))
        with store.transaction() as conn:
            conn.execute("SET LOCAL lock_timeout = '2s'")
            tag_id, spool = tags.seen_on(conn, TagRead(source="ams", uid=None, tray_uuid=_TRAY))
        assert spool is not None
    assert _tag(pg_pool, tag_id)["last_seen_at"] is None


def test_an_ams_read_prefers_the_tray_tag_when_the_keys_name_two(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    store.bind_tag(_spool(store, pg_pool), tray_uuid=_TRAY, kind="bambu_mifare", actor="ann")
    store.bind_tag(_spool(store, pg_pool), tag_uid="04AA", kind="other", actor="ann")
    with store.transaction() as conn:
        tag_id, _ = tags.seen_on(conn, TagRead(source="ams", uid="04AA", tray_uuid=_TRAY))
    assert _tag(pg_pool, tag_id)["tray_uuid"] == _TRAY


def test_the_records_are_stored_on_the_tag(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    record = NdefRecord(record_type="mime", media_type="application/json", data_hex="7B7D")
    result = tags.read(
        store,
        TagRead(source="phone", uid="04AA", records=(record,)),
        actor="ann",
        defaults=DEFAULTS,
    )
    assert result.tag is not None
    stored = _tag(pg_pool, result.tag.id)["last_seen_records"]
    assert stored[0]["record_type"] == "mime" and stored[0]["data_hex"] == "7b7d"


def test_an_ams_read_cannot_bind(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    with pytest.raises(ValueError, match="never binds"):
        tags.read(
            store,
            TagRead(source="ams", uid="04AA"),
            actor="ann",
            bind_spool_id=uuid4(),
            defaults=DEFAULTS,
        )


def test_a_bind_with_no_uid_is_refused_before_the_spool_is_looked_up(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    with pytest.raises(tags.BindWithoutUidError):
        tags.read(
            store,
            TagRead(source="phone", uid=None),
            actor="ann",
            bind_spool_id=uuid4(),
            defaults=DEFAULTS,
        )


# The OpenSpool README's sample: https://github.com/spuder/OpenSpool/blob/c060a06c/README.md
OPENSPOOL_SAMPLE = {
    "protocol": "openspool",
    "version": "1.0",
    "type": "PLA",
    "color_hex": "FFAABB",
    "brand": "Generic",
    "min_temp": "220",
    "max_temp": "240",
}


def _json_record(obj: object) -> NdefRecord:
    return NdefRecord(
        record_type="mime", media_type="application/json", data_hex=json.dumps(obj).encode().hex()
    )


def _pla(pool: PgPool) -> UUID:
    return next(
        r.id
        for r in CatalogStore(pool).list("materials")
        if isinstance(r, Material) and r.name == "PLA"
    )


def _openspool_read(uid: str | None = "04AA", sample: dict[str, str] = OPENSPOOL_SAMPLE) -> TagRead:
    return TagRead(source="phone", uid=uid, records=(_json_record(sample),))


def test_a_read_decodes_openspool_and_sets_the_tags_kind(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    result = tags.read(store, _openspool_read(), actor="ann", defaults=DEFAULTS)
    assert result.decoded is not None and result.decoded.format == "openspool"
    assert result.tag is not None and result.tag.kind == "ntag_openspool"


def test_a_read_changes_a_known_tags_kind_to_its_records_format(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    store.bind_tag(_spool(store, pg_pool), tag_uid="04AA", kind="other", actor="ann")
    # tag_version 2004, then material "PLA"; every other byte reads as 0.
    payload = (2004).to_bytes(2, "big") + b"PLA"
    ot3d = NdefRecord(
        record_type="mime", media_type="application/opentag3d", data_hex=payload.hex()
    )
    result = tags.read(
        store, TagRead(source="phone", uid="04AA", records=(ot3d,)), actor="ann", defaults=DEFAULTS
    )
    assert result.tag is not None and result.tag.kind == "ntag_opentag"
    result = tags.read(store, _openspool_read(), actor="ann", defaults=DEFAULTS)
    assert result.tag is not None and result.tag.kind == "ntag_openspool"


def test_a_new_tag_with_a_known_material_answers_a_draft(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    result = tags.read(store, _openspool_read(), actor="ann", defaults=DEFAULTS)
    assert result.draft is not None and result.draft.material_id == _pla(pg_pool)
    assert (result.draft.nozzle_temp_min, result.draft.nozzle_temp_max) == (220, 240)
    assert result.draft.rgba == "#FFAABBFF"


def test_a_new_tag_with_an_unknown_material_answers_no_draft(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    sample = OPENSPOOL_SAMPLE | {"type": "UNOBTAINIUM"}
    result = tags.read(store, _openspool_read(sample=sample), actor="ann", defaults=DEFAULTS)
    assert result.decoded is not None and result.draft is None


def test_a_draft_leaves_out_a_value_a_spool_cannot_hold(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    sample = OPENSPOOL_SAMPLE | {"max_temp": "5000"}
    result = tags.read(store, _openspool_read(sample=sample), actor="ann", defaults=DEFAULTS)
    assert result.draft is not None and result.draft.material_id == _pla(pg_pool)
    assert (result.draft.nozzle_temp_min, result.draft.nozzle_temp_max) == (220, None)


def test_a_held_tag_answers_no_draft(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    store.bind_tag(_spool(store, pg_pool), tag_uid="04AA", kind="other", actor="ann")
    result = tags.read(store, _openspool_read(), actor="ann", defaults=DEFAULTS)
    assert result.next == "open_spool" and result.decoded is not None and result.draft is None


def test_a_read_with_no_uid_still_answers_what_the_records_say(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    result = tags.read(store, _openspool_read(uid=None), actor="ann", defaults=DEFAULTS)
    assert (result.tag, result.spool, result.next) == (None, None, "none")
    assert result.decoded is not None and result.decoded.material == "PLA"


def test_a_bambu_tag_keeps_its_kind_whatever_its_records_say(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    store.bind_tag(spool, tag_uid="04AA", tray_uuid=_TRAY, kind="bambu_mifare", actor="ann")
    result = tags.read(store, _openspool_read(), actor="ann", defaults=DEFAULTS)
    assert result.tag is not None and result.tag.kind == "bambu_mifare"


# -- backups (#7273 Task 3) ---------------------------------------------------------------

REC = NdefRecord(record_type="mime", media_type="application/json", data_hex="7b7d")


def _backup(
    store: FilamentSpoolStore, uid: str = "04AA", reason: Any = "spool_write", records: Any = ()
) -> TagBackup:
    return tags.back_up(
        store, TagBackupIn(uid=uid, records=list(records), reason=reason), actor="ann"
    )


def test_a_backup_makes_the_tag_and_keeps_the_records(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    made = _backup(store, "04:aa", records=[REC])
    assert made.uid == "04AA" and made.records == [REC] and made.actor == "ann"
    assert made.job_id is None
    found = tags.backups(store, made.tag_id)
    assert found is not None and [b.id for b in found] == [made.id]


def test_a_backup_with_no_uid_is_refused(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    with pytest.raises(tags.NoUidError):
        _backup(store, "::")
    with pytest.raises(tags.NoUidError):
        _backup(store, "")
    assert _count(pg_pool, "filament_tag_backups") == 0


def test_backups_are_never_changed_or_deleted(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    made = _backup(store)
    with pg_pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM filament_tag_backups WHERE id = %s", (made.id,))
    with pg_pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE filament_tag_backups SET actor = 'x' WHERE id = %s", (made.id,))


def test_every_write_keeps_its_own_backup_newest_first(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    a = _backup(store)
    b = _backup(store, reason="restore", records=[REC])
    found = tags.backups(store, a.tag_id)
    assert found is not None and [x.id for x in found] == [b.id, a.id]


def test_backups_of_an_unknown_tag_are_none(pg_pool: PgPool) -> None:
    assert tags.backups(FilamentSpoolStore(pg_pool), uuid4()) is None


def test_unbinding_keeps_the_backups(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    made = _backup(store)
    tags.read(
        store,
        TagRead(source="phone", uid="04AA"),
        actor="ann",
        bind_spool_id=spool,
        defaults=DEFAULTS,
    )
    store.unbind_tag(made.tag_id, actor="ann")
    found = tags.backups(store, made.tag_id)
    assert found is not None and [b.id for b in found] == [made.id]


def test_valid_backup_refuses_another_uid_an_old_backup_and_another_reason(
    pg_pool: PgPool,
) -> None:
    store = FilamentSpoolStore(pg_pool)
    made = _backup(store)
    with store.transaction() as conn:
        assert tags.valid_backup(conn, made.id, "04:aa", "spool_write").id == made.id
        for uid, reason in (("04BB", "spool_write"), ("04AA", "restore")):
            with pytest.raises(tags.BackupMismatchError):
                tags.valid_backup(conn, made.id, uid, reason)  # type: ignore[arg-type]
        with pytest.raises(tags.BackupMismatchError):
            tags.valid_backup(conn, uuid4(), "04AA", "spool_write")
    with pg_pool.connection() as conn:
        old = conn.execute(
            "INSERT INTO filament_tag_backups (tag_id, uid, records, reason, actor, at)"
            " VALUES (%s, '04AA', '[]', 'spool_write', 'ann', now() - interval '11 minutes')"
            " RETURNING id",
            (made.tag_id,),
        ).fetchone()
    assert old is not None
    with store.transaction() as conn, pytest.raises(tags.BackupMismatchError):
        tags.valid_backup(conn, old["id"], "04AA", "spool_write")


def _armed_job(store: FilamentSpoolStore, kind: str = "write") -> UUID:
    records = [NdefRecord(record_type="mime", data_hex="7B7D")] if kind == "write" else None
    job = tags.create_job(
        store, NfcJobIn.model_validate({"kind": kind, "records": records}), actor="agent"
    )
    claim = None if records is None else NfcJobClaimIn(records_sha256=records_sha256(records))
    return tags.claim_job(store, job.id, claim, actor="ann").id


def test_an_agent_backup_must_match_its_job(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    job = _armed_job(store)
    made = tags.back_up(
        store,
        TagBackupIn(uid="04AA", records=[], reason="agent_write", job_id=job),
        actor="agent",
    )
    with store.transaction() as conn:
        assert tags.valid_backup(conn, made.id, "04AA", "agent_write", job_id=job).id == made.id
        with pytest.raises(tags.BackupMismatchError):
            tags.valid_backup(conn, made.id, "04AA", "agent_write", job_id=uuid4())


def test_an_agent_backup_needs_an_armed_write_job(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    pending = tags.create_job(
        store,
        NfcJobIn(kind="write", records=[NdefRecord(record_type="mime", data_hex="00")]),
        actor="agent",
    ).id
    for job in (pending, _armed_job(store, "read"), uuid4()):
        with pytest.raises(tags.JobNotArmedError):
            tags.back_up(
                store,
                TagBackupIn(uid="04AA", records=[], reason="agent_write", job_id=job),
                actor="agent",
            )
    assert _count(pg_pool, "filament_tag_backups") == 0
    assert _count(pg_pool, "filament_tags") == 0


def test_a_job_goes_pending_armed_done(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    job = tags.create_job(store, NfcJobIn(kind="read"), actor="agent")
    assert job.state == "pending" and job.created_by == "agent" and job.armed_by is None
    assert [j.id for j in tags.pending_jobs(store)] == [job.id]
    armed = tags.claim_job(store, job.id, None, actor="ann")
    assert armed.state == "armed" and armed.armed_by == "ann"
    with pytest.raises(tags.JobNotPendingError):
        tags.claim_job(store, job.id, None, actor="bob")
    done = tags.finish_job(
        store, job.id, NfcJobResultIn(uid="04:aa", records=[]), actor="ann", defaults=DEFAULTS
    )
    assert done.state == "done" and done.result is not None
    # #7273 M2: the result names the UID as it is stored, not as the page sent it.
    assert done.result["next"] == "bind_or_create" and done.result["uid"] == "04AA"
    assert tags.get_job(store, job.id) == done
    with pytest.raises(tags.JobNotArmedError):
        tags.finish_job(store, job.id, NfcJobResultIn(error="x"), actor="ann", defaults=DEFAULTS)
    assert tags.get_job(store, uuid4()) is None


def test_expire_jobs_ends_open_jobs_past_their_time(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    pending = tags.create_job(store, NfcJobIn(kind="read"), actor="agent").id
    armed = _armed_job(store)
    later = tags.create_job(store, NfcJobIn(kind="read"), actor="agent").id
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE filament_nfc_jobs SET expires_at = now() - interval '1 second'"
            " WHERE id = ANY(%s)",
            ([pending, armed],),
        )
    with store.transaction() as conn:
        tags.expire_jobs(conn)
    with pg_pool.connection() as conn:
        rows = conn.execute("SELECT id, state FROM filament_nfc_jobs").fetchall()
    assert {r["id"]: r["state"] for r in rows} == {
        pending: "expired",
        armed: "expired",
        later: "pending",
    }


def _age_job(pool: PgPool, job: UUID) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE filament_nfc_jobs SET expires_at = now() - interval '1 second' WHERE id = %s",
            (job,),
        )


def test_a_write_whose_time_ends_after_its_backup_still_takes_its_result(
    pg_pool: PgPool,
) -> None:
    # #7419 review: the page backed the tag up and wrote it; the result comes after the
    # job's time. The write has begun, so the job is not expired and takes the result.
    store = FilamentSpoolStore(pg_pool)
    job = _armed_job(store)
    backup = tags.back_up(
        store, TagBackupIn(uid="04AA", records=[], reason="agent_write", job_id=job), actor="ann"
    )
    _age_job(pg_pool, job)
    with store.transaction() as conn:
        tags.expire_jobs(conn)
    found = tags.get_job(store, job)
    assert found is not None and found.state == "armed"
    done = tags.finish_job(
        store,
        job,
        NfcJobResultIn(
            uid="04AA",
            records=[NdefRecord(record_type="mime", data_hex="7b7d")],
            backup_id=backup.id,
        ),
        actor="ann",
        defaults=DEFAULTS,
    )
    assert done.state == "done" and done.result is not None
    assert done.result["backup_id"] == str(backup.id)


def test_an_armed_job_with_no_backup_past_its_time_expires(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    job = _armed_job(store)
    # Another job's backup does not keep this one open.
    other = _armed_job(store)
    tags.back_up(
        store, TagBackupIn(uid="04AA", records=[], reason="agent_write", job_id=other), actor="ann"
    )
    _age_job(pg_pool, job)
    found = tags.get_job(store, job)
    assert found is not None and found.state == "expired"
    with pytest.raises(tags.JobNotArmedError):
        tags.finish_job(store, job, NfcJobResultIn(error="x"), actor="ann", defaults=DEFAULTS)


def test_a_job_read_says_whether_its_write_began(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    job = _armed_job(store)
    found = tags.get_job(store, job)
    assert found is not None and found.write_begun is False
    tags.back_up(
        store, TagBackupIn(uid="04AA", records=[], reason="agent_write", job_id=job), actor="ann"
    )
    found = tags.get_job(store, job)
    assert found is not None and found.write_begun is True


def test_a_begun_write_cannot_be_cancelled(pg_pool: PgPool) -> None:
    # #7419 round 2 (I1): its tag may hold the new records; the job ends only by its result.
    store = FilamentSpoolStore(pg_pool)
    job = _armed_job(store)
    tags.back_up(
        store, TagBackupIn(uid="04AA", records=[], reason="agent_write", job_id=job), actor="ann"
    )
    with pytest.raises(tags.WriteBegunError) as refused:
        tags.cancel_job(store, job)
    assert refused.value.code == "write_begun"
    found = tags.get_job(store, job)
    assert found is not None and found.state == "armed"


def test_a_begun_write_with_no_result_past_the_backup_ttl_fails_no_read_back(
    pg_pool: PgPool,
) -> None:
    store = FilamentSpoolStore(pg_pool)
    job = _armed_job(store)
    tag = _backup(store)
    with pg_pool.connection() as conn:
        old = conn.execute(
            "INSERT INTO filament_tag_backups (tag_id, uid, records, reason, job_id, actor, at)"
            " VALUES (%s, '04AA', '[]', 'agent_write', %s, 'ann', now() - interval '11 minutes')"
            " RETURNING id",
            (tag.tag_id, job),
        ).fetchone()
    assert old is not None
    _age_job(pg_pool, job)
    found = tags.get_job(store, job)
    assert found is not None and found.state == "failed" and found.result is not None
    assert found.result["code"] == "no_read_back"
    assert found.result["backup_id"] == str(old["id"])


def _while_a_backup_commits(pg_pool: PgPool, job: UUID, run: Callable[[], object]) -> object:
    """Runs ``run`` on another connection while this one holds the job ``FOR SHARE`` and has
    inserted its ``agent_write`` backup, as `back_up` does; commits once ``run`` waits on the
    lock, and answers what ``run`` returned or raised (#7419 round 3)."""
    tag = _backup(FilamentSpoolStore(pg_pool), "04CC")
    answer: list[object] = []

    def other() -> None:
        try:
            answer.append(run())
        except Exception as exc:  # the caller asserts on it
            answer.append(exc)

    with pg_pool.connection() as conn, conn.transaction():
        conn.execute("SELECT 1 FROM filament_nfc_jobs WHERE id = %s FOR SHARE", (job,))
        conn.execute(
            "INSERT INTO filament_tag_backups (tag_id, uid, records, reason, job_id, actor)"
            " VALUES (%s, '04CC', '[]', 'agent_write', %s, 'ann')",
            (tag.tag_id, job),
        )
        thread = threading.Thread(target=other)
        thread.start()
        with pg_pool.connection() as watch:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                waiting = watch.execute(
                    "SELECT count(*) AS n FROM pg_stat_activity"
                    " WHERE wait_event_type = 'Lock' AND datname = current_database()"
                ).fetchone()
                if waiting is not None and waiting["n"] > 0:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("the other connection never waited on the job's lock")
    thread.join(10)
    assert not thread.is_alive() and len(answer) == 1
    return answer[0]


def test_expire_jobs_waiting_on_a_backup_leaves_the_begun_job_armed(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    job = _armed_job(store)
    _age_job(pg_pool, job)

    def expire() -> None:
        with store.transaction() as conn:
            tags.expire_jobs(conn)

    assert _while_a_backup_commits(pg_pool, job, expire) is None
    with pg_pool.connection() as conn:
        row = conn.execute("SELECT state FROM filament_nfc_jobs WHERE id = %s", (job,)).fetchone()
    assert row is not None and row["state"] == "armed"


def test_a_cancel_waiting_on_a_backup_is_refused_write_begun(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    job = _armed_job(store)
    answer = _while_a_backup_commits(pg_pool, job, lambda: tags.cancel_job(store, job))
    assert isinstance(answer, tags.WriteBegunError)
    found = tags.get_job(store, job)
    assert found is not None and found.state == "armed" and found.write_begun


def test_arming_gives_the_run_its_own_time(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    job = tags.create_job(store, NfcJobIn(kind="read", ttl_s=5), actor="agent")
    armed = tags.claim_job(store, job.id, None, actor="ann")
    with pg_pool.connection() as conn:
        row = conn.execute("SELECT now() AS now").fetchone()
    assert row is not None
    left = (armed.expires_at - row["now"]).total_seconds()
    assert tags.ARM_SECONDS - 5 < left <= tags.ARM_SECONDS
    found = tags.get_job(store, job.id)
    assert found is not None and found.expires_at == armed.expires_at


def test_a_write_job_checks_the_read_back(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    job = _armed_job(store)
    backup = tags.back_up(
        store, TagBackupIn(uid="04AA", records=[], reason="agent_write", job_id=job), actor="ann"
    )
    read_back = [NdefRecord(record_type="mime", data_hex="7b7d")]
    done = tags.finish_job(
        store,
        job,
        NfcJobResultIn(uid="04AA", records=read_back, backup_id=backup.id),
        actor="ann",
        defaults=DEFAULTS,
    )
    assert done.state == "done" and done.result is not None
    assert done.result["backup_id"] == str(backup.id)
    assert _tag(pg_pool, backup.tag_id)["last_seen_records"][0]["data_hex"] == "7b7d"


def test_a_mismatched_read_back_still_marks_the_tag_seen(pg_pool: PgPool) -> None:
    # #7273 M3: the tag is recorded with what it holds, and the job fails.
    store = FilamentSpoolStore(pg_pool)
    job = _armed_job(store)
    backup = tags.back_up(
        store, TagBackupIn(uid="04AA", records=[], reason="agent_write", job_id=job), actor="ann"
    )
    read_back = [NdefRecord(record_type="mime", data_hex="5b5d")]
    failed = tags.finish_job(
        store,
        job,
        NfcJobResultIn(uid="04:aa", records=read_back, backup_id=backup.id),
        actor="ann",
        defaults=DEFAULTS,
    )
    assert failed.state == "failed" and failed.result is not None
    assert failed.result["uid"] == "04AA"
    seen = _tag(pg_pool, backup.tag_id)
    assert seen["last_seen_source"] == "agent"
    assert seen["last_seen_records"][0]["data_hex"] == "5b5d"


def test_records_sha256_hashes_the_canonical_json_of_the_records() -> None:
    # #7273 R22: the phone page computes the same bytes with crypto.subtle.
    records = [
        NdefRecord(record_type="mime", media_type="application/json", data_hex="7B7D"),
        NdefRecord(record_type="text", lang="en", encoding="utf-8", data_hex="c3a9"),
    ]
    canonical = (
        '[{"data_hex":"7b7d","encoding":null,"id":null,"lang":null,'
        '"media_type":"application/json","record_type":"mime"},'
        '{"data_hex":"c3a9","encoding":"utf-8","id":null,"lang":"en",'
        '"media_type":null,"record_type":"text"}]'
    )
    assert records_sha256(records) == hashlib.sha256(canonical.encode()).hexdigest()
    # Non-ASCII text is written as UTF-8, not escaped.
    raw = (
        '[{"data_hex":"","encoding":null,"id":"é","lang":null,"media_type":null,'
        '"record_type":"mime"}]'
    )
    assert records_sha256([NdefRecord(record_type="mime", id="é", data_hex="")]) == (
        hashlib.sha256(raw.encode()).hexdigest()
    )


def test_a_write_job_is_armed_only_for_the_records_shown(pg_pool: PgPool) -> None:
    # #7273 R22: the claim names the hash of the records the page showed.
    store = FilamentSpoolStore(pg_pool)
    records = [NdefRecord(record_type="mime", data_hex="7b7d")]
    job = tags.create_job(store, NfcJobIn(kind="write", records=records), actor="agent").id
    other = records_sha256([NdefRecord(record_type="mime", data_hex="5b5d")])
    for claim in (None, NfcJobClaimIn(), NfcJobClaimIn(records_sha256=other)):
        with pytest.raises(tags.RecordsMismatchError):
            tags.claim_job(store, job, claim, actor="ann")
        found = tags.get_job(store, job)
        assert found is not None and found.state == "pending" and found.armed_by is None
    right = NfcJobClaimIn(records_sha256=records_sha256(records).upper())
    assert tags.claim_job(store, job, right, actor="ann").state == "armed"


def test_a_read_job_claim_takes_no_records_hash(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    job = tags.create_job(store, NfcJobIn(kind="read"), actor="agent").id
    with pytest.raises(tags.RecordsHashNotTakenError):
        tags.claim_job(store, job, NfcJobClaimIn(records_sha256="0" * 64), actor="ann")
    found = tags.get_job(store, job)
    assert found is not None and found.state == "pending"
    assert tags.claim_job(store, job, NfcJobClaimIn(), actor="ann").state == "armed"


def test_cancel_ends_an_open_job_and_leaves_an_ended_one(pg_pool: PgPool) -> None:
    # #7273 M1: an agent that stops waiting cancels its job, so no page arms it later.
    store = FilamentSpoolStore(pg_pool)
    pending = tags.create_job(store, NfcJobIn(kind="read"), actor="agent").id
    armed = _armed_job(store)
    ended = _armed_job(store, "read")
    failed = tags.finish_job(
        store, ended, NfcJobResultIn(error="declined"), actor="ann", defaults=DEFAULTS
    )
    for job_id in (pending, armed):
        cancelled = tags.cancel_job(store, job_id)
        assert cancelled.state == "cancelled" and cancelled.result is None
    assert tags.pending_jobs(store) == []
    with pytest.raises(tags.JobNotPendingError):
        tags.claim_job(store, pending, None, actor="ann")
    with pytest.raises(tags.JobNotArmedError):
        tags.back_up(
            store,
            TagBackupIn(uid="04AA", records=[], reason="agent_write", job_id=armed),
            actor="ann",
        )
    with pytest.raises(tags.JobNotArmedError):
        tags.finish_job(store, armed, NfcJobResultIn(error="x"), actor="ann", defaults=DEFAULTS)
    assert tags.cancel_job(store, ended) == failed
    with pytest.raises(tags.NfcJobNotFoundError):
        tags.cancel_job(store, uuid4())


def test_the_job_id_goes_with_agent_write_only() -> None:
    with pytest.raises(ValueError):
        TagBackupIn(uid="04AA", records=[], reason="agent_write")
    with pytest.raises(ValueError):
        TagBackupIn(uid="04AA", records=[], reason="restore", job_id=uuid4())


def test_backups_come_in_pages(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    made = [_backup(store) for _ in range(5)]
    newest_first = [b.id for b in reversed(made)]
    first = tags.backups(store, made[0].tag_id, limit=2)
    assert first is not None and [b.id for b in first] == newest_first[:2]
    second = tags.backups(store, made[0].tag_id, limit=2, before=first[-1].id)
    assert second is not None and [b.id for b in second] == newest_first[2:4]
    rest = tags.backups(store, made[0].tag_id, before=second[-1].id)
    assert rest is not None and [b.id for b in rest] == newest_first[4:]
    assert tags.backups(store, made[0].tag_id, before=uuid4()) == []


def test_a_page_before_another_tags_backup_is_empty(pg_pool: PgPool) -> None:
    # `before` names a backup of this tag only; one of another tag pages nothing.
    store = FilamentSpoolStore(pg_pool)
    mine = _backup(store, "04AA")
    other = _backup(store, "04BB")
    assert tags.backups(store, mine.tag_id, before=other.id) == []
    found = tags.backups(store, mine.tag_id)
    assert found is not None and [b.id for b in found] == [mine.id]


def test_backups_made_at_one_time_page_by_id(pg_pool: PgPool) -> None:
    # Backups of one transaction share `at` (now()); the id breaks the tie, highest first,
    # and `before` pages past each of them exactly once.
    store = FilamentSpoolStore(pg_pool)
    tag_id = _backup(store).tag_id
    with pg_pool.connection() as conn, conn.transaction():
        for _ in range(3):
            conn.execute(
                "INSERT INTO filament_tag_backups (tag_id, uid, records, reason, actor)"
                " VALUES (%s, '04AA', '[]', 'spool_write', 'ann')",
                (tag_id,),
            )
    found = tags.backups(store, tag_id)
    assert found is not None and len(found) == 4
    tied = found[:3]
    assert len({b.at for b in tied}) == 1
    assert [b.id for b in tied] == sorted((b.id for b in tied), reverse=True)
    paged: list[UUID] = []
    before: UUID | None = None
    while page := tags.backups(store, tag_id, limit=1, before=before):
        paged.append(page[0].id)
        before = page[0].id
    assert paged == [b.id for b in found]


def test_backups_cannot_be_truncated(pg_pool: PgPool) -> None:
    _backup(FilamentSpoolStore(pg_pool))
    with pg_pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute("TRUNCATE filament_tag_backups")


def test_deleting_a_spool_keeps_the_backups(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    made = _backup(store)
    tags.read(
        store,
        TagRead(source="phone", uid="04AA"),
        actor="ann",
        bind_spool_id=spool,
        defaults=DEFAULTS,
    )
    assert store.delete(spool) is True
    found = tags.backups(store, made.tag_id)
    assert found is not None and [b.id for b in found] == [made.id]


@pytest.mark.parametrize("field", ["media_type", "id", "encoding", "lang"])
def test_a_record_with_a_lone_surrogate_is_refused(field: str) -> None:
    # The HTTP body's JSON parse refuses one already (measured: 422); a record built in
    # Python must be refused too, before it is hashed or stored.
    with pytest.raises(ValueError):
        NdefRecord.model_validate({"record_type": "text", "data_hex": "", field: "a\ud800"})
    with pytest.raises(ValueError):
        NfcJobResultIn(error="a\ud800")


def test_a_backup_over_one_tags_bytes_is_refused() -> None:
    full = NdefRecord(record_type="mime", data_hex="00" * 888)
    TagBackupIn(uid="04AA", records=[full], reason="spool_write")
    with pytest.raises(ValueError):
        TagBackupIn(uid="04AA", records=[full, REC], reason="spool_write")
    half = NdefRecord(record_type="mime", data_hex="00" * 500)
    with pytest.raises(ValueError):
        TagBackupIn(uid="04AA", records=[half, half], reason="spool_write")


def test_same_records_compares_type_media_and_data_in_order() -> None:
    other = NdefRecord(record_type="mime", media_type="x/y", data_hex="00")
    assert tags.same_records([REC, other], [REC, other])
    assert not tags.same_records([REC, other], [other, REC])
    assert not tags.same_records([REC], [REC, other])
    assert tags.same_records([REC], [REC.model_copy(update={"id": "x", "lang": "en"})])


def _poster(data_hex: str) -> dict[str, str]:
    return {"record_type": "smart-poster", "data_hex": data_hex}


#: ``(a, b, equal)``: the same vectors as ``SAME_RECORD_VECTORS`` in
#: ``frontend/src/lib/webNfc.test.ts``, so the page's ``sameRecords`` and ``same_records``
#: give one verdict. The first posters hold one "U" record for https://example.org: prefix 0
#: and the whole URL, prefix 4 and the rest, the long (non-SR) form, and one with an id (IL).
SAME_RECORD_VECTORS: list[tuple[dict[str, str], dict[str, str], bool]] = [
    (
        _poster("d10114550068747470733a2f2f6578616d706c652e6f7267"),
        _poster("d1010c55046578616d706c652e6f7267"),
        True,
    ),
    (
        _poster("d1010c55046578616d706c652e6f7267"),
        _poster("c1010000000c55046578616d706c652e6f7267"),
        True,
    ),
    (
        _poster("d1010c55046578616d706c652e6f7267"),
        _poster("d9010c015561046578616d706c652e6f7267"),
        True,
    ),
    (
        _poster("D1010C55046578616D706C652E6F7267"),
        _poster("d1010c55046578616d706c652e6f7267"),
        True,
    ),
    (
        _poster("d1010c55046578616d706c652e6f7267"),
        _poster("d1010c55046578616d706c652e636f6d"),
        False,
    ),
    (
        _poster("91010c55046578616d706c652e6f72675101055402656e4869"),
        _poster("910114550068747470733a2f2f6578616d706c652e6f72675101055402656e4869"),
        True,
    ),
    (
        _poster("91010c55046578616d706c652e6f72675101055402656e4869"),
        _poster("d1010c55046578616d706c652e6f7267"),
        False,
    ),
    (_poster("91"), _poster("91"), True),
    (_poster("91"), _poster("d1"), False),
    (_poster("91"), _poster("d1010c55046578616d706c652e6f7267"), False),
    # Every other type is compared by its bytes: a "url" record's prefix is not expanded.
    (
        {"record_type": "url", "data_hex": "68747470733a2f2f6578616d706c652e6f7267"},
        {"record_type": "url", "data_hex": "046578616d706c652e6f7267"},
        False,
    ),
    (
        {"record_type": "mime", "media_type": "application/json", "data_hex": "AB"},
        {"record_type": "mime", "media_type": "application/json", "data_hex": "ab"},
        True,
    ),
    (
        {"record_type": "mime", "media_type": "application/json", "data_hex": "ab"},
        {"record_type": "mime", "media_type": "text/plain", "data_hex": "ab"},
        False,
    ),
]


@pytest.mark.parametrize(("a", "b", "equal"), SAME_RECORD_VECTORS)
def test_same_records_reads_a_smart_poster_by_its_records(
    a: dict[str, str], b: dict[str, str], equal: bool
) -> None:
    # Web NFC writes a poster's nested message itself, with its own URI prefix codes and
    # SR/IL flags, so a correct write can read back as other bytes.
    one, other = NdefRecord.model_validate(a), NdefRecord.model_validate(b)
    assert tags.same_records([one], [other]) is equal
    assert tags.same_records([other], [one]) is equal


def test_a_checked_write_books_one_edited_event_with_the_formats_origin(pg_pool: PgPool) -> None:
    store = FilamentSpoolStore(pg_pool)
    spool = _spool(store, pg_pool)
    made = _backup(store)

    def weight(_: UUID) -> int | None:
        return None

    sent = tags.records_for_spool(
        store,
        spool,
        TagRecordsIn(uid="04AA", records=[], backup_id=made.id),
        default="openspool",
        empty_weight_g=weight,
        defaults=DEFAULTS,
    )
    assert sent is not None
    result = tags.written(
        store,
        spool,
        TagWrittenIn(uid="04AA", backup_id=made.id, sent=sent.records, read_back=sent.records),
        actor="ann",
        defaults=DEFAULTS,
        empty_weight_g=weight,
        default="openspool",
    )
    assert result is not None and result.spool is not None
    assert result.spool.data_origin == "openspool" and result.spool.tag_encoded_at is not None
    with pg_pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM filament_spool_events"
            " WHERE spool_id = %s AND kind = 'edited'",
            (spool,),
        ).fetchone()
    assert row is not None and row["n"] == 1
