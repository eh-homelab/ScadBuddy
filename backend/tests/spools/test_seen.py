"""The spool refresh's ``seen`` half over the printer registry's report store (#4191):
every LAN printer's latest report, its filled trays as `SeenTray`s."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from temporalio.worker import Worker

from scadbuddy.core.components import Components, Core
from scadbuddy.printers.component import PRINTERS
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.status import STALE_AFTER
from scadbuddy.printers.store import PrinterStore
from scadbuddy.spools import component as spools_component
from scadbuddy.spools.activities import record_seen, spool_activities
from scadbuddy.spools.component import Spools
from scadbuddy.spools.models import SeenTray
from scadbuddy.spools.seen import ReportTrays
from scadbuddy.spools.store import SpoolStore
from scadbuddy.workflows.spool_refresh import SpoolRefresh
from tests.conftest import PgPool
from tests.support.temporal import temporal_client

pytestmark = pytest.mark.requires_postgres

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
UUID_A = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
TAG_A = "1A2B3C4D5E6F7081"


class Registry:
    def __init__(self, pool: PgPool) -> None:
        self.store = PrinterStore(pool, None)
        self.reports = PrinterReportStore(pool)


def lan_printer(pool: PgPool, name: str) -> str:
    printer_id = uuid.uuid4()
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO printers (id, serial, name, lan_host) VALUES (%s, %s, %s, '10.0.0.9')",
            (printer_id, f"T{printer_id.hex[:15].upper()}", name),
        )
    return str(printer_id)


def report(*trays: dict[str, Any], vt_tray: dict[str, Any] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {"ams": {"ams": [{"id": "0", "tray": list(trays)}]}}
    if vt_tray is not None:
        data["vt_tray"] = vt_tray
    return data


def ams_tray(tray_id: int, **fields: Any) -> dict[str, Any]:
    return {
        "id": str(tray_id),
        "tray_type": "PLA",
        "tray_color": "FF0000FF",
        "tray_info_idx": "GFA00",
        "state": 11,
        "tray_uuid": "0" * 32,
        "tag_uid": "0" * 16,
        **fields,
    }


async def test_each_printers_latest_report_gives_its_filled_trays(pg_pool: PgPool) -> None:
    registry = Registry(pg_pool)
    fresh = lan_printer(pg_pool, "fresh")
    gone_quiet = lan_printer(pg_pool, "gone quiet")
    registry.reports.put(
        fresh,
        report(
            ams_tray(0, tray_uuid=UUID_A.lower(), tag_uid=TAG_A),
            # The state changes as the AMS feeds; nothing here reads it but `_filled`.
            ams_tray(1, tray_type="PETG", tray_color="00FF00FF", state=27),
            # An empty slot: no material.
            ams_tray(2, tray_type="", tray_color="00000000", state=0),
            vt_tray={"id": "254", "tray_type": "TPU", "tray_color": "0000FFFF", "state": 11},
        ),
        NOW,
    )
    registry.reports.put(
        gone_quiet,
        report(ams_tray(0, tray_uuid="B" * 32)),
        NOW - STALE_AFTER - timedelta(seconds=1),
    )

    seen = await ReportTrays(registry, now=lambda: NOW).latest()

    assert seen == [
        SeenTray(
            ams_id=0,
            tray_id=0,
            tray_uuid=UUID_A,
            tag_uid=TAG_A,
            material="PLA",
            colour="#FF0000",
            tray_info_idx="GFA00",
        ),
        SeenTray(ams_id=0, tray_id=1, material="PETG", colour="#00FF00", tray_info_idx="GFA00"),
        SeenTray(ams_id=255, tray_id=0, material="TPU", colour="#0000FF"),
    ]


@pytest.mark.requires_temporal
async def test_a_refresh_records_a_tagged_trays_spool(pg_pool: PgPool) -> None:
    """The whole refresh on Temporal, with the real activities: the tagged tray's spool is
    made, with its ``tray_info_idx`` and ``last_seen_at``."""
    registry = Registry(pg_pool)
    printer = lan_printer(pg_pool, "P")
    registry.reports.put(printer, report(ams_tray(0, tray_uuid=UUID_A, tag_uid=TAG_A)), NOW)
    spools = Spools(
        store=SpoolStore(pg_pool), providers=[], trays=ReportTrays(registry, now=lambda: NOW)
    )
    queue = f"library-{uuid.uuid4().hex[:8]}"
    async with (
        temporal_client() as client,
        Worker(
            client, task_queue=queue, workflows=[SpoolRefresh], activities=spool_activities(spools)
        ),
    ):
        await client.execute_workflow(
            SpoolRefresh.run, id=f"spools-{uuid.uuid4().hex}", task_queue=queue
        )

    with pg_pool.connection() as conn:
        rows = conn.execute(
            "SELECT tray_uuid, tag_uid, material, colour, tray_info_idx, last_seen_at"
            " FROM physical_spools"
        ).fetchall()
    assert len(rows) == 1
    (row,) = rows
    assert (row["tray_uuid"], row["tag_uid"], row["material"], row["colour"]) == (
        UUID_A,
        TAG_A,
        "PLA",
        "#FF0000",
    )
    assert row["tray_info_idx"] == "GFA00"
    assert row["last_seen_at"] is not None


async def test_an_untagged_tray_makes_no_spool(pg_pool: PgPool) -> None:
    registry = Registry(pg_pool)
    printer = lan_printer(pg_pool, "P")
    registry.reports.put(printer, report(ams_tray(0)), NOW)
    store = SpoolStore(pg_pool)
    await record_seen(
        Spools(store=store, providers=[], trays=ReportTrays(registry, now=lambda: NOW))
    )
    with pg_pool.connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM physical_spools").fetchone() == {"n": 0}


def test_the_component_reads_the_printer_registry() -> None:
    """`SPOOLS` is built with a tray source over `PRINTERS`, and reads nothing yet."""
    printers = object()
    core = SimpleNamespace(
        projection=SimpleNamespace(pool=object()),
        settings_store=SimpleNamespace(load=None, pool=None),
        print_follower=SimpleNamespace(on_read=[]),
    )
    components = SimpleNamespace(get=lambda key: printers if key is PRINTERS else None)
    built = spools_component._build(cast(Core, core), cast(Components, components))
    assert isinstance(built.trays, ReportTrays)
    assert built.trays.registry is printers
    # What a native print used comes off its spools from the follow's reads (#4643).
    assert len(core.print_follower.on_read) == 1
