"""`SpoolStore` against Postgres (#3818 tier A, plan 2026-10-18 D1): a tray holds one
physical spool, and each spool provider keeps its own record of it (1:N)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from scadbuddy.spools.models import SeenTray, SpoolLocation, SpoolSnapshot, tray_fingerprint
from scadbuddy.spools.store import _LOCK, SPOOL_OVERLAY_STALE, SpoolStore, repoint_tray_links
from tests.conftest import PgPool

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
PRINTER = uuid.UUID("5d0c8f1e-0000-4000-8000-000000000001")
UUID_A = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
TAG_A = "1A2B3C4D5E6F7081"
ZERO_UUID = "0" * 32
ZERO_TAG = "0" * 16


def registered(pool: PgPool, *printer_ids: uuid.UUID) -> None:
    """Registry rows for ``printer_ids``: a tray link names a printer (#4190's key)."""
    with pool.connection() as conn:
        for printer_id in printer_ids:
            conn.execute(
                "INSERT INTO printers (id, serial, name) VALUES (%s, %s, 'P')",
                (printer_id, f"T{printer_id.hex[:15].upper()}"),
            )


@pytest.fixture
def store(pg_pool: PgPool) -> SpoolStore:
    registered(pg_pool, PRINTER)
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
    assert len(rows(pg_pool, "SELECT 1 FROM spool_provider_records")) == 1
    assert rows(pg_pool, "SELECT 1 FROM spool_record_links") == []


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
    assert changed.fingerprint != tray(1, 2).fingerprint
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
    linked = rows(pg_pool, "SELECT record_id FROM spool_record_links")
    assert linked == [{"record_id": "14"}]


async def test_the_schema_refuses_two_records_of_one_provider_on_one_spool(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    await store.replace_records("bambuddy", [record("9", tray_uuid=UUID_A), record("10")], NOW)
    (spool,) = rows(pg_pool, "SELECT id FROM physical_spools")
    with pytest.raises(psycopg.errors.UniqueViolation):
        rows(
            pg_pool,
            "INSERT INTO spool_record_links (provider, record_id, physical_spool_id, linked_by)"
            " VALUES ('bambuddy', '10', %s, 'manual')",
            spool["id"],
        )


async def test_two_records_naming_one_tag_link_neither(store: SpoolStore, pg_pool: PgPool) -> None:
    """A tag two of one provider's records carry says nothing about which is in the
    tray: Bambuddy's own recording carries one placeholder tag on seven spools."""
    await store.replace_records(
        "bambuddy", [record("1", tag_uid=TAG_A), record("2", tag_uid=TAG_A)], NOW
    )
    assert rows(pg_pool, "SELECT id FROM physical_spools") == []
    assert rows(pg_pool, "SELECT 1 FROM spool_record_links") == []


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


# -- review of #4180 -------------------------------------------------------------------


async def test_a_manual_link_survives_a_refresh_that_leaves_the_record_out(
    store: SpoolStore,
) -> None:
    """C1: a person's link is not the provider's cache: an empty answer, or a record
    missing from one refresh, leaves it."""
    await store.replace_records("bambuddy", [record("1")], NOW)
    linked = await store.link(PRINTER, tray(1, 2), provider="bambuddy", record_id="1")
    await store.replace_records("bambuddy", [], NOW + timedelta(minutes=5))
    missing = await store.overlay(
        PRINTER, [tray(1, 2)], now=NOW + timedelta(minutes=5), providers=["bambuddy"]
    )
    assert missing.trays[0].physical_spool_id == linked.physical_spool_id
    assert missing.trays[0].records == {}
    await store.replace_records("bambuddy", [record("1")], NOW + timedelta(minutes=10))
    back = await store.overlay(
        PRINTER, [tray(1, 2)], now=NOW + timedelta(minutes=10), providers=["bambuddy"]
    )
    assert back.trays[0].records["bambuddy"].record_id == "1"


async def test_a_manual_link_survives_an_archive_and_restore(store: SpoolStore) -> None:
    """C1: an archived spool is not listed (`BambuddyClient.spools` excludes them), and
    comes back when restored."""
    await store.replace_records("bambuddy", [record("1"), record("2")], NOW)
    await store.link(PRINTER, tray(0, 3), provider="bambuddy", record_id="1")
    await store.replace_records("bambuddy", [record("2")], NOW + timedelta(minutes=5))
    await store.replace_records("bambuddy", [record("1"), record("2")], NOW + timedelta(minutes=10))
    overlay = await store.overlay(
        PRINTER, [tray(0, 3)], now=NOW + timedelta(minutes=10), providers=["bambuddy"]
    )
    assert overlay.trays[0].linked_by == "manual"
    assert overlay.trays[0].records["bambuddy"].record_id == "1"


async def test_a_manually_linked_record_that_learns_a_new_tag_tags_its_spool(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    """I1: no spool has the key yet, so it is the linked spool's, and RFID takes over."""
    await store.replace_records("bambuddy", [record("1")], NOW)
    linked = await store.link(PRINTER, tray(255, 0), provider="bambuddy", record_id="1")
    await store.replace_records("bambuddy", [record("1", tray_uuid=UUID_A)], NOW)
    (spool,) = rows(pg_pool, "SELECT id, tray_uuid FROM physical_spools")
    assert spool == {"id": linked.physical_spool_id, "tray_uuid": UUID_A}
    (link,) = rows(pg_pool, "SELECT linked_by FROM spool_record_links")
    assert link == {"linked_by": "rfid"}
    overlay = await store.overlay(
        PRINTER, [tray(0, 1, tray_uuid=UUID_A)], now=NOW, providers=["bambuddy"]
    )
    assert overlay.trays[0].physical_spool_id == linked.physical_spool_id
    assert overlay.trays[0].records["bambuddy"].record_id == "1"


async def test_a_manually_linked_record_whose_tag_names_another_spool_moves_to_it(
    store: SpoolStore,
) -> None:
    """I1: the tag is the evidence (Bambuddy's own `get_spool_by_tag` prefers it); the
    hand-linked spool keeps its tray link and loses the record."""
    await store.seen([tray(0, 1, tray_uuid=UUID_A)])
    await store.replace_records("bambuddy", [record("1")], NOW)
    linked = await store.link(PRINTER, tray(255, 0), provider="bambuddy", record_id="1")
    await store.replace_records("bambuddy", [record("1", tray_uuid=UUID_A)], NOW)
    overlay = await store.overlay(
        PRINTER,
        [tray(0, 1, tray_uuid=UUID_A), tray(255, 0)],
        now=NOW,
        providers=["bambuddy"],
    )
    tagged, untagged = overlay.trays
    assert tagged.linked_by == "rfid"
    assert tagged.records["bambuddy"].record_id == "1"
    assert untagged.physical_spool_id == linked.physical_spool_id
    assert untagged.records == {}


def test_the_fingerprint_is_material_and_colour_only() -> None:
    """I2: never the tray's `state`, a bitfield that changes as the AMS feeds and scans
    (`SeenTray` carries none)."""
    assert tray_fingerprint("PLA", "#FF0000") == tray_fingerprint(" PLA ", "ff0000ff")
    assert tray(1, 2).fingerprint == "PLA|FF0000"


async def test_a_link_made_while_the_tray_was_not_fed_holds_once_it_feeds(
    store: SpoolStore,
) -> None:
    """I2: the same tray, read at state 10 when linked and 11 or 15 later, is the same
    fingerprint, since the state is not in it."""
    await store.replace_records("bambuddy", [record("1")], NOW)
    linked = await store.link(PRINTER, tray(1, 2), provider="bambuddy", record_id="1")
    overlay = await store.overlay(PRINTER, [tray(1, 2)], now=NOW, providers=["bambuddy"])
    assert overlay.trays[0].physical_spool_id == linked.physical_spool_id


def located(record_id: str, ams_id: int, tray_id: int, **fields: object) -> SpoolSnapshot:
    return record(
        record_id,
        colour="#FF0000",
        location=SpoolLocation(
            printer="bambuddy:1",
            ams_id=ams_id,
            tray_id=tray_id,
            fingerprint=tray_fingerprint("PLA", "#FF0000"),
        ),
        **fields,
    )


async def test_a_providers_own_assignment_seeds_an_untagged_tray(store: SpoolStore) -> None:
    """I5: a mapping a person already made in the provider is shown, not asked again."""
    await store.replace_records("bambuddy", [located("13", 1, 2)], NOW)
    overlay = await store.overlay(
        PRINTER, [tray(1, 2)], now=NOW, providers=["bambuddy"], located={"bambuddy": "bambuddy:1"}
    )
    (only,) = overlay.trays
    assert only.linked_by == "provider"
    assert only.records["bambuddy"].record_id == "13"
    # Another printer's assignment, or one whose tray has changed, is not this tray's.
    other = await store.overlay(
        PRINTER, [tray(1, 2)], now=NOW, providers=["bambuddy"], located={"bambuddy": "bambuddy:2"}
    )
    assert other.trays[0].records == {}
    changed = await store.overlay(
        PRINTER,
        [tray(1, 2, colour="#00FF00")],
        now=NOW,
        providers=["bambuddy"],
        located={"bambuddy": "bambuddy:1"},
    )
    assert changed.trays[0].records == {}


async def test_a_persons_link_wins_over_the_providers_assignment(store: SpoolStore) -> None:
    await store.replace_records("bambuddy", [located("13", 1, 2), record("14")], NOW)
    await store.link(PRINTER, tray(1, 2), provider="bambuddy", record_id="14")
    overlay = await store.overlay(
        PRINTER, [tray(1, 2)], now=NOW, providers=["bambuddy"], located={"bambuddy": "bambuddy:1"}
    )
    assert overlay.trays[0].linked_by == "manual"
    assert overlay.trays[0].records["bambuddy"].record_id == "14"


async def test_a_success_clears_the_last_failure(store: SpoolStore) -> None:
    """Minor 3."""
    await store.failed("bambuddy", NOW, "ApiError")
    await store.replace_records("bambuddy", [], NOW + timedelta(minutes=5))
    (refresh,) = await store.refreshes()
    assert (refresh.failed_at, refresh.failure) == (None, None)


async def test_seen_moves_a_tag_to_the_spool_it_was_read_on(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    """Minor 8: a tag read beside another tray_uuid is that spool's now; a tag read
    beside its spool's first unknown tray_uuid gives the spool that uuid."""
    other = "B" * 32
    await store.seen([tray(0, 0, tag_uid=TAG_A)])
    await store.seen([tray(0, 0, tray_uuid=UUID_A, tag_uid=TAG_A)])
    assert rows(pg_pool, "SELECT tray_uuid, tag_uid FROM physical_spools") == [
        {"tray_uuid": UUID_A, "tag_uid": TAG_A}
    ]
    await store.seen([tray(0, 1, tray_uuid=other, tag_uid=TAG_A)])
    spools = rows(pg_pool, "SELECT tray_uuid, tag_uid FROM physical_spools ORDER BY tray_uuid")
    assert spools == [
        {"tray_uuid": UUID_A, "tag_uid": None},
        {"tray_uuid": other, "tag_uid": TAG_A},
    ]


async def test_a_key_is_matched_as_bambuddy_normalises_it(store: SpoolStore) -> None:
    """Minor 1: hex only; a tag_uid's last 16 digits, a tray_uuid's first 32."""
    await store.seen([tray(tray_uuid=f"{UUID_A}FF", tag_uid=f"00:{TAG_A}")])
    await store.replace_records(
        "bambuddy", [record("9", tray_uuid=UUID_A.lower(), tag_uid=TAG_A)], NOW
    )
    overlay = await store.overlay(
        PRINTER, [tray(tray_uuid=UUID_A)], now=NOW, providers=["bambuddy"]
    )
    assert overlay.trays[0].records["bambuddy"].record_id == "9"


async def test_a_merged_printers_tray_links_move_to_the_printer_it_merged_into(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    """The registry's merge of a synthetic-serial Bambuddy row into the real one
    repoints the tray links in its own transaction; a tray both link keeps the real
    printer's."""
    synthetic, real = uuid.uuid4(), uuid.uuid4()
    registered(pg_pool, synthetic, real)
    await store.replace_records("bambuddy", [record("1"), record("2"), record("3")], NOW)
    await store.link(synthetic, tray(0, 0), provider="bambuddy", record_id="1")
    await store.link(synthetic, tray(0, 1), provider="bambuddy", record_id="2")
    await store.link(real, tray(0, 1), provider="bambuddy", record_id="3")
    with pg_pool.connection() as conn, conn.transaction():
        repoint_tray_links(conn, synthetic, real)
    overlay = await store.overlay(real, [tray(0, 0), tray(0, 1)], now=NOW, providers=["bambuddy"])
    assert [t.records["bambuddy"].record_id for t in overlay.trays] == ["1", "3"]
    assert rows(pg_pool, "SELECT 1 FROM tray_spool_links WHERE printer_id = %s", synthetic) == []


async def test_a_providers_assignment_never_puts_a_person_linked_spool_on_a_second_tray(
    store: SpoolStore,
) -> None:
    """Re-review N1 (probe P1): Bambuddy still says (0,0); a person put it in (0,1)."""
    await store.replace_records("bambuddy", [located("1", 0, 0)], NOW)
    linked = await store.link(PRINTER, tray(0, 1), provider="bambuddy", record_id="1")
    overlay = await store.overlay(
        PRINTER,
        [tray(0, 0), tray(0, 1)],
        now=NOW,
        providers=["bambuddy"],
        located={"bambuddy": "bambuddy:1"},
    )
    stale, placed = overlay.trays
    assert (stale.physical_spool_id, stale.linked_by, stale.records) == (None, None, {})
    assert placed.physical_spool_id == linked.physical_spool_id
    assert placed.linked_by == "manual"


async def test_a_providers_assignment_never_puts_a_tagged_spool_on_an_untagged_tray(
    store: SpoolStore,
) -> None:
    """Re-review N1 (probe P2): a tagged spool's tray is the one reporting its keys."""
    await store.replace_records("bambuddy", [located("1", 0, 0, tray_uuid=UUID_A)], NOW)
    overlay = await store.overlay(
        PRINTER,
        [tray(0, 0), tray(0, 2, tray_uuid=UUID_A)],
        now=NOW,
        providers=["bambuddy"],
        located={"bambuddy": "bambuddy:1"},
    )
    untagged, tagged = overlay.trays
    assert (untagged.physical_spool_id, untagged.linked_by, untagged.records) == (None, None, {})
    assert tagged.linked_by == "rfid"
    assert tagged.records["bambuddy"].record_id == "1"


async def test_a_write_that_cannot_finish_in_time_commits_nothing(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    """Re-review N2: a refresh past its provider's timeout is recorded as failed; its
    write must not commit after that."""
    with pg_pool.connection() as holder, holder.transaction():
        holder.execute(_LOCK)
        # Its lock wait is cut short, by lock_timeout or statement_timeout.
        with pytest.raises(psycopg.OperationalError):
            await store.replace_records("bambuddy", [record("1")], NOW, timeout=0.2)
    assert rows(pg_pool, "SELECT 1 FROM spool_provider_records") == []
    assert await store.refreshes() == []


async def test_a_write_past_its_deadline_rolls_back(pg_pool: PgPool) -> None:
    """Every statement finished in time, but the deadline passed before the commit."""
    ticks = iter([0.0, 0.0])
    store = SpoolStore(pg_pool, clock=lambda: next(ticks, 100.0))
    with pytest.raises(TimeoutError):
        await store.replace_records("bambuddy", [record("1")], NOW, timeout=30.0)
    assert rows(pg_pool, "SELECT 1 FROM spool_provider_records") == []


def test_repointing_waits_for_the_stores_writers(pg_pool: PgPool) -> None:
    """Re-review N3: it serialises with `link()` and the refresh on the store's lock."""
    with pg_pool.connection() as holder, holder.transaction():
        holder.execute(_LOCK)
        with pg_pool.connection() as conn, conn.transaction():
            conn.execute("SET LOCAL lock_timeout = '200ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                repoint_tray_links(conn, uuid.uuid4(), uuid.uuid4())


# -- the shelf (#4504) ------------------------------------------------------------------


async def test_the_shelf_is_every_fresh_record_no_tray_holds(store: SpoolStore) -> None:
    """#4504: what an untagged tray's question offers. A record a tray holds (a person's
    link, the provider's own assignment), one the provider places in any tray, one of a
    tagged spool and a stale provider's are not on it."""
    await store.replace_records(
        "bambuddy",
        [
            record("free", colour="#FFFFFF", remaining_g=500.0),
            record("linked", colour="#FFFFFF"),
            located("placed", 1, 2),
            record("tagged", tray_uuid=UUID_A),
        ],
        NOW,
    )
    await store.replace_records("old", [record("o1")], NOW - SPOOL_OVERLAY_STALE * 2)
    await store.link(PRINTER, tray(0, 1, colour="#FFFFFF"), provider="bambuddy", record_id="linked")
    overlay = await store.overlay(
        PRINTER,
        [tray(0, 1, colour="#FFFFFF"), tray(1, 2)],
        now=NOW,
        providers=["bambuddy", "old"],
        located={"bambuddy": "bambuddy:1"},
    )
    (only,) = overlay.shelf
    assert only.physical_spool_id is None
    assert list(only.records) == ["bambuddy"]
    assert only.records["bambuddy"].record_id == "free"
    assert only.records["bambuddy"].remaining_g == 500.0


async def test_one_spools_records_are_one_shelf_entry(store: SpoolStore) -> None:
    """Two providers' records of one physical spool no tray holds: one entry, its records
    in the providers' order, under the spool a link names."""
    await store.replace_records("bambuddy", [record("b1")], NOW)
    await store.replace_records("native", [record("n1")], NOW)
    first = await store.link(PRINTER, tray(0, 1), provider="bambuddy", record_id="b1")
    await store.link(PRINTER, tray(0, 1), provider="native", record_id="n1")
    await store.unlink(PRINTER, 0, 1)
    overlay = await store.overlay(PRINTER, [tray(0, 1)], now=NOW, providers=["native", "bambuddy"])
    (only,) = overlay.shelf
    assert only.physical_spool_id == first.physical_spool_id
    assert list(only.records) == ["native", "bambuddy"]


async def test_a_spool_whose_tray_changed_is_back_on_the_shelf(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    """A person's link this printer's tray no longer matches holds nothing: the spool
    came out, so it may be linked again. A link on another printer still holds it."""
    other = uuid.UUID("6e1d9f2a-0000-4000-8000-000000000002")
    registered(pg_pool, other)
    await store.replace_records("bambuddy", [record("13"), record("14")], NOW)
    await store.link(PRINTER, tray(0, 1), provider="bambuddy", record_id="13")
    await store.link(other, tray(0, 1), provider="bambuddy", record_id="14")
    overlay = await store.overlay(
        PRINTER, [tray(0, 1, colour="#00FF00")], now=NOW, providers=["bambuddy"]
    )
    assert [entry.records["bambuddy"].record_id for entry in overlay.shelf] == ["13"]


async def test_a_tagged_record_with_no_spool_is_not_on_the_shelf(store: SpoolStore) -> None:
    """Review N1: two records naming one tag link neither, so neither has a physical
    spool; their tag still says they are a tagged spool, which is wherever the tag is
    read, never in an untagged tray."""
    await store.replace_records(
        "bambuddy",
        [record("a", tag_uid=TAG_A), record("b", tag_uid=TAG_A), record("free")],
        NOW,
    )
    overlay = await store.overlay(PRINTER, [tray(0, 1)], now=NOW, providers=["bambuddy"])
    assert [entry.records["bambuddy"].record_id for entry in overlay.shelf] == ["free"]


async def test_a_spool_linked_on_another_printer_is_not_on_the_shelf(
    store: SpoolStore, pg_pool: PgPool
) -> None:
    """Review N4: a person's link on another printer holds its spool, whatever this
    printer's trays show."""
    other = uuid.UUID("7f2e0a3b-0000-4000-8000-000000000003")
    registered(pg_pool, other)
    await store.replace_records("bambuddy", [record("13"), record("free")], NOW)
    await store.link(other, tray(0, 1), provider="bambuddy", record_id="13")
    overlay = await store.overlay(PRINTER, [tray(0, 1)], now=NOW, providers=["bambuddy"])
    assert [entry.records["bambuddy"].record_id for entry in overlay.shelf] == ["free"]


async def test_no_untagged_tray_reads_no_shelf(store: SpoolStore) -> None:
    """Review N2: only an untagged tray is asked about, so with none the records are not
    scanned."""
    await store.replace_records("bambuddy", [record("free")], NOW)
    overlay = await store.overlay(
        PRINTER, [tray(0, 0, tray_uuid=UUID_A)], now=NOW, providers=["bambuddy"]
    )
    assert overlay.shelf == []


async def test_a_spool_its_provider_places_in_a_tray_is_not_on_the_shelf(
    store: SpoolStore,
) -> None:
    """Review 2 N1: one spool, Bambuddy's record placing it in tray (1,2) and another
    provider's with no location. The spool is that tray's, so neither record is offered
    for another tray."""
    await store.replace_records("bambuddy", [record("13", colour="#FF0000")], NOW)
    await store.replace_records("native", [record("n1", colour="#FF0000")], NOW)
    await store.link(PRINTER, tray(0, 1), provider="bambuddy", record_id="13")
    await store.link(PRINTER, tray(0, 1), provider="native", record_id="n1")
    await store.unlink(PRINTER, 0, 1)
    await store.replace_records("bambuddy", [located("13", 1, 2)], NOW)
    overlay = await store.overlay(
        PRINTER,
        [tray(1, 2), tray(0, 3)],
        now=NOW,
        providers=["native", "bambuddy"],
        located={"bambuddy": "bambuddy:1"},
    )
    assert overlay.trays[0].linked_by == "provider"
    assert overlay.shelf == []


async def test_a_link_to_a_tray_now_tagged_holds_nothing(store: SpoolStore) -> None:
    """Review 2 N2: a person's link to a tray that now reads a tag is not that tray's
    spool (`_tray_spool`), even with the same material and colour: it is on the shelf."""
    await store.replace_records("bambuddy", [record("13", colour="#FF0000")], NOW)
    await store.link(PRINTER, tray(0, 1), provider="bambuddy", record_id="13")
    overlay = await store.overlay(
        PRINTER, [tray(0, 1, tray_uuid=UUID_A), tray(0, 2)], now=NOW, providers=["bambuddy"]
    )
    assert overlay.trays[0].records == {}
    assert [entry.records["bambuddy"].record_id for entry in overlay.shelf] == ["13"]


async def test_the_shelf_read_is_traced_by_its_counts_only(
    store: SpoolStore, spans: InMemorySpanExporter
) -> None:
    """Review 3 N1: a large inventory's cost shows in a trace as counts, never a record's
    values."""
    await store.replace_records(
        "bambuddy", [record("free", colour="#FFFFFF"), record("other")], NOW
    )
    spans.clear()
    await store.overlay(PRINTER, [tray(0, 1)], now=NOW, providers=["bambuddy"])
    (shelf,) = (s for s in spans.get_finished_spans() if s.name == "spools.shelf")
    assert dict(shelf.attributes or {}) == {
        "scadbuddy.spools.shelf_records": 2,
        "scadbuddy.spools.shelf_spools": 2,
    }


async def test_a_stale_providers_placement_does_not_hold_a_spool_off_the_shelf(
    store: SpoolStore,
) -> None:
    """Review 4 N1: a stale provider's records are left out everywhere, its placement
    of a spool too."""
    await store.replace_records("old", [record("o1", colour="#FF0000")], NOW)
    await store.replace_records("native", [record("n1", colour="#FF0000")], NOW)
    await store.link(PRINTER, tray(0, 1), provider="old", record_id="o1")
    await store.link(PRINTER, tray(0, 1), provider="native", record_id="n1")
    await store.unlink(PRINTER, 0, 1)
    await store.replace_records("old", [located("o1", 1, 2)], NOW - SPOOL_OVERLAY_STALE * 2)
    overlay = await store.overlay(
        PRINTER,
        [tray(1, 2), tray(0, 3)],
        now=NOW,
        providers=["native", "old"],
        located={"old": "bambuddy:1"},
    )
    assert "old" in overlay.stale
    assert [list(entry.records) for entry in overlay.shelf] == [["native"]]


async def test_a_stale_placement_leaves_its_spool_on_the_shelf(store: SpoolStore) -> None:
    """#4511: a provider's placement this printer's tray no longer matches holds nothing,
    as a person's link does not: the tray does not show the spool, so the shelf does. A
    placement on another printer still holds it."""
    await store.replace_records(
        "bambuddy",
        [
            located("13", 1, 2),
            record(
                "14",
                location=SpoolLocation(printer="bambuddy:2", ams_id=0, tray_id=0),
            ),
        ],
        NOW,
    )
    overlay = await store.overlay(
        PRINTER,
        [tray(1, 2, colour="#00FF00")],
        now=NOW,
        providers=["bambuddy"],
        located={"bambuddy": "bambuddy:1"},
    )
    assert overlay.trays[0].records == {}
    assert [entry.records["bambuddy"].record_id for entry in overlay.shelf] == ["13"]


async def test_a_placement_by_a_provider_with_no_name_here_holds_its_spool(
    store: SpoolStore,
) -> None:
    """#4513 review: a provider ``located`` does not name has no way to place a spool on
    this printer, so its placement is somewhere else, and holds the spool there."""
    await store.replace_records("bambuddy", [record("13", colour="#FF0000")], NOW)
    await store.replace_records("native", [record("n1", colour="#FF0000")], NOW)
    await store.link(PRINTER, tray(0, 1), provider="bambuddy", record_id="13")
    await store.link(PRINTER, tray(0, 1), provider="native", record_id="n1")
    await store.unlink(PRINTER, 0, 1)
    await store.replace_records("bambuddy", [located("13", 1, 2), record("free")], NOW)
    overlay = await store.overlay(PRINTER, [tray(1, 2)], now=NOW, providers=["native", "bambuddy"])
    assert overlay.trays[0].records == {}
    assert [entry.records["bambuddy"].record_id for entry in overlay.shelf] == ["free"]


async def test_a_spool_placed_here_is_off_the_shelf_with_all_its_records(
    store: SpoolStore,
) -> None:
    """#4513 review: one spool, Bambuddy's record placing it in this printer's matching
    tray and another provider's unplaced: the tray shows it, so neither record is on the
    shelf."""
    await store.replace_records("bambuddy", [record("13", colour="#FF0000")], NOW)
    await store.replace_records("native", [record("n1", colour="#FF0000")], NOW)
    await store.link(PRINTER, tray(0, 1), provider="bambuddy", record_id="13")
    await store.link(PRINTER, tray(0, 1), provider="native", record_id="n1")
    await store.unlink(PRINTER, 0, 1)
    await store.replace_records("bambuddy", [located("13", 1, 2)], NOW)
    overlay = await store.overlay(
        PRINTER,
        [tray(1, 2), tray(0, 3)],
        now=NOW,
        providers=["bambuddy", "native"],
        located={"bambuddy": "bambuddy:1"},
    )
    assert overlay.trays[0].linked_by == "provider"
    assert list(overlay.trays[0].records) == ["bambuddy", "native"]
    assert overlay.shelf == []


async def test_a_second_placement_in_one_tray_is_on_the_shelf(store: SpoolStore) -> None:
    """#4513 review: two records both place themselves in one matching tray. The tray
    shows the first (`_located`, by record id); the other is not in the tray, so it is on
    the shelf for a person to link."""
    await store.replace_records("bambuddy", [located("13", 1, 2), located("14", 1, 2)], NOW)
    overlay = await store.overlay(
        PRINTER, [tray(1, 2)], now=NOW, providers=["bambuddy"], located={"bambuddy": "bambuddy:1"}
    )
    assert overlay.trays[0].records["bambuddy"].record_id == "13"
    assert [entry.records["bambuddy"].record_id for entry in overlay.shelf] == ["14"]
