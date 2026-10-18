"""`SpoolStore` against Postgres (#3818 tier A, plan 2026-10-18 D1): a tray holds one
physical spool, and each spool provider keeps its own record of it (1:N)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from scadbuddy.spools.models import SeenTray, SpoolSnapshot
from scadbuddy.spools.store import SPOOL_OVERLAY_STALE, SpoolStore
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
PRINTER = "printer-1"
UUID_A = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
TAG_A = "1A2B3C4D5E6F7081"
ZERO_UUID = "0" * 32
ZERO_TAG = "0" * 16


@pytest.fixture
def store(pg_pool: PgPool) -> SpoolStore:
    return SpoolStore(pg_pool)


def tray(
    ams_id: int = 0,
    tray_id: int = 0,
    *,
    tray_uuid: str | None = None,
    tag_uid: str | None = None,
    material: str = "PLA",
    colour: str = "#FF0000",
) -> SeenTray:
    return SeenTray(
        ams_id=ams_id,
        tray_id=tray_id,
        tray_uuid=tray_uuid,
        tag_uid=tag_uid,
        material=material,
        colour=colour,
        tray_info_idx="GFA00",
        fingerprint=f"{material}|{colour}|11",
    )


def record(record_id: str, **fields: object) -> SpoolSnapshot:
    return SpoolSnapshot.model_validate({"record_id": record_id, "material": "PLA", **fields})


def rows(pool: PgPool, sql: str, *args: object) -> list[dict[str, object]]:
    with pool.connection() as conn:
        return list(conn.execute(sql.encode(), args).fetchall())


async def test_an_rfid_record_links_to_the_spool_its_tray_uuid_names(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    await store.seen([tray(tray_uuid=UUID_A, tag_uid=TAG_A)])
    await store.replace_records("bambuddy", [record("9", tray_uuid=UUID_A)], NOW)
    overlay = await store.overlay(
        PRINTER, [tray(tray_uuid=UUID_A, tag_uid=TAG_A)], now=NOW, providers=["bambuddy"]
    )
    (only,) = overlay.trays
    assert only.linked_by == "rfid"
    assert only.records["bambuddy"].record_id == "9"
    assert len(rows(pg_pool, "SELECT id FROM physical_spools")) == 1


async def test_a_record_with_a_tag_never_seen_makes_its_physical_spool(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    await store.replace_records("bambuddy", [record("9", tag_uid=TAG_A)], NOW)
    (spool,) = rows(pg_pool, "SELECT tray_uuid, tag_uid, material FROM physical_spools")
    assert spool == {"tray_uuid": None, "tag_uid": TAG_A, "material": "PLA"}
    overlay = await store.overlay(PRINTER, [tray(tag_uid=TAG_A)], now=NOW, providers=["bambuddy"])
    assert overlay.trays[0].records["bambuddy"].record_id == "9"


async def test_two_providers_records_of_one_spool_both_appear(store: SpoolStore) -> None:
    """D1: physical spool to provider records is 1:N."""
    await store.replace_records("bambuddy", [record("9", tray_uuid=UUID_A)], NOW)
    await store.replace_records("native", [record("n-1", tray_uuid=UUID_A, brand="Elegoo")], NOW)
    overlay = await store.overlay(
        PRINTER, [tray(tray_uuid=UUID_A)], now=NOW, providers=["bambuddy", "native"]
    )
    assert set(overlay.trays[0].records) == {"bambuddy", "native"}
    assert overlay.trays[0].records["native"].brand == "Elegoo"
    assert overlay.stale == {}


async def test_all_zeros_is_never_a_key(store: SpoolStore, pg_pool: PgPool) -> None:
    await store.seen([tray(tray_uuid=ZERO_UUID, tag_uid=ZERO_TAG)])
    await store.replace_records(
        "bambuddy", [record("9", tray_uuid=ZERO_UUID, tag_uid=ZERO_TAG)], NOW
    )
    assert rows(pg_pool, "SELECT id FROM physical_spools") == []
    (row,) = rows(pg_pool, "SELECT physical_spool_id, linked_by FROM spool_provider_records")
    assert row == {"physical_spool_id": None, "linked_by": None}


async def test_a_record_the_provider_no_longer_lists_is_dropped(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    await store.replace_records("bambuddy", [record("9"), record("10")], NOW)
    await store.replace_records("bambuddy", [record("10")], NOW + timedelta(minutes=5))
    assert [
        r["record_id"] for r in rows(pg_pool, "SELECT record_id FROM spool_provider_records")
    ] == ["10"]


async def test_a_refresh_leaves_another_providers_records(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    await store.replace_records("native", [record("n-1")], NOW)
    await store.replace_records("bambuddy", [], NOW)
    assert len(rows(pg_pool, "SELECT 1 FROM spool_provider_records")) == 1


async def test_a_manual_link_overlays_an_untagged_tray_and_survives_a_refresh(
    store: SpoolStore,
) -> None:
    await store.replace_records("bambuddy", [record("13", colour="#7389BC")], NOW)
    linked = await store.link(PRINTER, tray(1, 2), provider="bambuddy", record_id="13")
    assert linked.linked_by == "manual"
    assert linked.records["bambuddy"].record_id == "13"
    await store.replace_records(
        "bambuddy", [record("13", colour="#7389BC", remaining_g=420.0)], NOW + timedelta(minutes=5)
    )
    overlay = await store.overlay(
        PRINTER, [tray(1, 2)], now=NOW + timedelta(minutes=5), providers=["bambuddy"]
    )
    (only,) = overlay.trays
    assert only.physical_spool_id == linked.physical_spool_id
    assert only.linked_by == "manual"
    assert only.records["bambuddy"].remaining_g == 420.0


async def test_a_manual_link_is_dropped_when_the_trays_fingerprint_changes(
    store: SpoolStore,
) -> None:
    await store.replace_records("bambuddy", [record("13")], NOW)
    await store.link(PRINTER, tray(1, 2), provider="bambuddy", record_id="13")
    changed = tray(1, 2, colour="#00FF00")
    overlay = await store.overlay(PRINTER, [changed], now=NOW, providers=["bambuddy"])
    (only,) = overlay.trays
    assert only.physical_spool_id is None
    assert only.linked_by is None
    assert only.records == {}


async def test_a_manual_link_moves_a_record_and_never_duplicates_one(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    """Review Focus 7: one record per provider per physical spool, and one link per
    record."""
    await store.replace_records("bambuddy", [record("13"), record("14")], NOW)
    first = await store.link(PRINTER, tray(0, 0), provider="bambuddy", record_id="13")
    # The same record into another tray: the record moves with its spool.
    second = await store.link(PRINTER, tray(0, 1), provider="bambuddy", record_id="13")
    assert second.physical_spool_id == first.physical_spool_id
    links = rows(pg_pool, "SELECT ams_id, tray_id FROM tray_spool_links")
    assert links == [{"ams_id": 0, "tray_id": 1}]
    # Another record of the same provider onto that spool unlinks the first.
    third = await store.link(PRINTER, tray(0, 1), physical_spool_id=first.physical_spool_id)
    assert third.records["bambuddy"].record_id == "13"
    moved = await store.link(PRINTER, tray(0, 1), provider="bambuddy", record_id="14")
    assert moved.records["bambuddy"].record_id == "14"
    linked = rows(
        pg_pool,
        "SELECT record_id FROM spool_provider_records WHERE physical_spool_id IS NOT NULL",
    )
    assert linked == [{"record_id": "14"}]


async def test_the_schema_refuses_two_records_of_one_provider_on_one_spool(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    await store.replace_records("bambuddy", [record("9", tray_uuid=UUID_A), record("10")], NOW)
    (spool,) = rows(pg_pool, "SELECT id FROM physical_spools")
    with pytest.raises(psycopg.errors.UniqueViolation):
        rows(
            pg_pool,
            "UPDATE spool_provider_records SET physical_spool_id = %s, linked_by = 'manual'"
            " WHERE record_id = '10'",
            spool["id"],
        )


async def test_two_records_naming_one_tag_link_neither(store: SpoolStore, pg_pool: PgPool) -> None:
    """A tag two of one provider's records carry says nothing about which is in the
    tray: Bambuddy's own recording carries one placeholder tag on seven spools."""
    await store.replace_records(
        "bambuddy", [record("1", tag_uid=TAG_A), record("2", tag_uid=TAG_A)], NOW
    )
    assert rows(pg_pool, "SELECT id FROM physical_spools") == []
    assert all(
        r["physical_spool_id"] is None
        for r in rows(pg_pool, "SELECT physical_spool_id FROM spool_provider_records")
    )


async def test_a_stale_provider_is_left_out_and_named(store: SpoolStore) -> None:
    await store.replace_records("bambuddy", [record("9", tray_uuid=UUID_A)], NOW)
    late = NOW + SPOOL_OVERLAY_STALE + timedelta(minutes=1)
    overlay = await store.overlay(
        PRINTER, [tray(tray_uuid=UUID_A)], now=late, providers=["bambuddy", "native"]
    )
    assert overlay.trays[0].physical_spool_id is not None
    assert overlay.trays[0].records == {}
    assert overlay.stale == {"bambuddy": NOW, "native": None}


async def test_a_failed_refresh_keeps_the_rows_and_says_so(store: SpoolStore) -> None:
    await store.replace_records("bambuddy", [record("9", tray_uuid=UUID_A)], NOW)
    await store.failed("bambuddy", NOW + timedelta(minutes=5), "ApiError")
    overlay = await store.overlay(
        PRINTER, [tray(tray_uuid=UUID_A)], now=NOW + timedelta(minutes=5), providers=["bambuddy"]
    )
    assert overlay.trays[0].records["bambuddy"].record_id == "9"
    (refresh,) = await store.refreshes()
    assert refresh.provider == "bambuddy"
    assert refresh.refreshed_at == NOW
    assert refresh.failure == "ApiError"


async def test_seen_updates_what_the_tray_says_of_its_spool(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    await store.seen([tray(tray_uuid=UUID_A, colour="#FF0000")])
    await store.seen([tray(tray_uuid=UUID_A, tag_uid=TAG_A, colour="#00FF00")])
    (spool,) = rows(pg_pool, "SELECT tray_uuid, tag_uid, colour FROM physical_spools")
    assert spool == {"tray_uuid": UUID_A, "tag_uid": TAG_A, "colour": "#00FF00"}


async def test_linking_an_rfid_tray_writes_no_tray_link(store: SpoolStore, pg_pool: PgPool) -> None:
    """RFID trays resolve by join; only an untagged tray needs a link."""
    await store.replace_records("bambuddy", [record("15")], NOW)
    linked = await store.link(PRINTER, tray(tray_uuid=UUID_A), provider="bambuddy", record_id="15")
    assert linked.linked_by == "rfid"
    assert rows(pg_pool, "SELECT 1 FROM tray_spool_links") == []
    overlay = await store.overlay(
        PRINTER, [tray(tray_uuid=UUID_A)], now=NOW, providers=["bambuddy"]
    )
    assert overlay.trays[0].records["bambuddy"].record_id == "15"


async def test_unlink_forgets_the_tray_not_the_spool(store: SpoolStore, pg_pool: PgPool) -> None:
    await store.replace_records("bambuddy", [record("13")], NOW)
    await store.link(PRINTER, tray(1, 2), provider="bambuddy", record_id="13")
    await store.unlink(PRINTER, 1, 2)
    overlay = await store.overlay(PRINTER, [tray(1, 2)], now=NOW, providers=["bambuddy"])
    assert overlay.trays[0].physical_spool_id is None
    assert len(rows(pg_pool, "SELECT 1 FROM physical_spools")) == 1


async def test_link_refuses_what_it_cannot_find(store: SpoolStore) -> None:
    with pytest.raises(LookupError):
        await store.link(PRINTER, tray(), provider="bambuddy", record_id="missing")
    with pytest.raises(LookupError):
        await store.link(PRINTER, tray(), physical_spool_id=uuid.uuid4())
    with pytest.raises(ValueError):
        await store.link(PRINTER, tray())
