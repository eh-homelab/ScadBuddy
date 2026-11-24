"""The one tag read function (#7273 Task 1)."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament import tags
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.models import Material, NdefRecord, SpoolCreate
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
