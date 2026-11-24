"""The one tag read function (#7273 Task 1)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from scadbuddy.filament import tags
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.inventory import FilamentInventory
from scadbuddy.filament.models import Material, SpoolCreate
from scadbuddy.filament.spools import FilamentDefaults, FilamentSpoolStore
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
    assert FilamentInventory is not None
