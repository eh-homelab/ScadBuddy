"""The Bambuddy printer sync (#4439, #4747; plan 2026-10-26-bambuddy-printer-sync
Rulings 2-4): the registry follows Bambuddy, idempotently, and never overwrites."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from scadbuddy.bambuddy.models import Printer, VirtualPrinter, VirtualPrinterList
from scadbuddy.core.problems import ApiError
from scadbuddy.printers.bambuddy_sync import (
    LAN_PROBE_FAILED,
    LAN_WITHHELD,
    PrinterSyncResult,
    may_adopt,
    plan_printer,
    sync_printers,
)
from scadbuddy.printers.component import Printers
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import LanLink, PrinterCreate
from scadbuddy.printers.store import PrinterStore
from tests.conftest import PgPool
from tests.printers.conftest import KEK

pytestmark = pytest.mark.requires_postgres

SERIAL = "01P00A000000001"
OTHER_SERIAL = "01P00A000000002"
VP_SERIAL = "09400A391800001"
CODE = "secretc0de"
CERT = "ab" * 32


class FakeClient:
    """`BambuddyClient`'s reads the sync makes, from fixed answers."""

    def __init__(
        self,
        printers: list[dict[str, Any]],
        vps: list[dict[str, Any]] | None = None,
        codes: dict[int, str | None] | None = None,
    ) -> None:
        self._printers = printers
        self._vps = vps or []
        self.codes = codes or {}
        self.code_reads: list[int] = []
        self.vp_error: ApiError | None = None

    async def printers(self) -> list[Printer]:
        return [Printer.model_validate(p) for p in self._printers]

    async def virtual_printers(self) -> VirtualPrinterList:
        if self.vp_error is not None:
            raise self.vp_error
        return VirtualPrinterList.model_validate({"printers": self._vps, "models": {}})

    async def printer_access_code(self, printer_id: int) -> str | None:
        self.code_reads.append(printer_id)
        return self.codes.get(printer_id)


class RecordedWatches:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def restart(self, printer_id: str) -> None:
        self.calls.append(("restart", printer_id))


class FakePrinters:
    """The parts of the `Printers` component the sync uses, over a real store."""

    def __init__(self, store: PrinterStore) -> None:
        self.store = store
        self.kek = store.kek
        self.watches = RecordedWatches()
        self.changed_ids: list[str] = []
        self.probes: list[tuple[str, str, str]] = []
        self.answer = True

        async def probe(host: str, serial: str, code: str) -> tuple[LanReport, str]:
            self.probes.append((host, serial, code))
            if not self.answer:
                raise ConnectionRefusedError("nope")
            return LanReport(serial=serial, received_at=datetime.now(UTC), print={}), CERT

        self.probe = probe

    def changed(self, printer_id: str) -> None:
        self.changed_ids.append(printer_id)


def bambuddy(
    printer_id: int, serial: str | None, ip: str | None = "192.168.1.10", **more: Any
) -> dict[str, Any]:
    return {
        "id": printer_id,
        "name": f"P{printer_id}",
        "model": "H2C",
        "serial_number": serial,
        "ip_address": ip,
        "is_active": True,
        "nozzle_count": 2,
        **more,
    }


def vp(
    vp_id: int, serial: str, target: int | None, mode: str = "queue", **more: Any
) -> dict[str, Any]:
    return {
        "id": vp_id,
        "name": f"VP{vp_id}",
        "enabled": True,
        "mode": mode,
        "model": "O1C",
        "model_name": "H2C",
        "serial": serial,
        "target_printer_id": target,
        "bind_ip": "0.0.0.0",
        "remote_interface_ip": "",
        "access_code_set": True,
        "status": {"running": True, "pending_files": 0},
        **more,
    }


@pytest.fixture
def store(pg_pool: PgPool) -> PrinterStore:
    return PrinterStore(pg_pool, KEK)


@pytest.fixture
def printers(store: PrinterStore) -> FakePrinters:
    return FakePrinters(store)


async def run(
    printers: FakePrinters, client: FakeClient, *, dry_run: bool = False
) -> PrinterSyncResult:
    return await sync_printers(cast(Printers, printers), cast(Any, client), dry_run=dry_run)


async def test_an_empty_registry_fills_with_linked_printers_and_their_lan_links(
    printers: FakePrinters, store: PrinterStore
) -> None:
    client = FakeClient(
        [bambuddy(1, SERIAL), bambuddy(2, OTHER_SERIAL, ip="192.168.1.11")],
        codes={1: CODE, 2: CODE},
    )
    result = await run(printers, client)
    assert (result.created, result.lan_linked) == (2, 2)
    rows = {row.serial: row for row in store.list()}
    one = rows[SERIAL]
    assert one.bambuddy_printer_id == 1 and one.preferred_path == "auto"
    assert one.lan_host == "192.168.1.10" and one.has_access_code
    assert one.bambuddy_view is not None and one.bambuddy_view.nozzle_count == 2
    assert store.open_access_code(one.id) == CODE
    assert ("192.168.1.10", SERIAL, CODE) in printers.probes
    assert ("restart", one.id) in printers.watches.calls


async def test_a_second_sync_changes_nothing(printers: FakePrinters, store: PrinterStore) -> None:
    client = FakeClient([bambuddy(1, SERIAL)], [vp(5, VP_SERIAL, 1)], codes={1: CODE})
    await run(printers, client)
    before = {row.id: row for row in store.list()}
    printers.changed_ids.clear()
    printers.probes.clear()
    again = await run(printers, client)
    assert {entry.action for entry in again.entries} == {"already_linked"}
    assert {row.id: row for row in store.list()} == before  # not even updated_at
    assert printers.probes == [] and printers.changed_ids == []


async def test_dry_run_plans_and_writes_nothing(
    printers: FakePrinters, store: PrinterStore
) -> None:
    client = FakeClient([bambuddy(1, SERIAL)], [vp(5, VP_SERIAL, None)], codes={1: CODE})
    result = await run(printers, client, dry_run=True)
    assert [(e.action, e.lan) for e in result.entries] == [
        ("created", "would_try"),
        ("created", None),
    ]
    assert result.entries[1].target_model == "H2C"
    assert store.list() == [] and printers.probes == [] and client.code_reads == []


async def test_no_serial_is_skipped_with_the_reason(printers: FakePrinters) -> None:
    result = await run(printers, FakeClient([bambuddy(1, None)]))
    (entry,) = result.entries
    assert entry.action == "skipped" and entry.reason is not None and "serial" in entry.reason


async def test_the_row_holding_the_serial_is_linked(
    printers: FakePrinters, store: PrinterStore
) -> None:
    mine = store.create(PrinterCreate(serial=SERIAL, name="Mine"))
    result = await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: CODE}))
    assert result.entries[0].action == "linked"
    row = store.require(mine.id)
    assert row.bambuddy_printer_id == 1 and row.name == "Mine" and row.lan_host is not None


async def test_conflicts_are_reported_never_overwritten(
    printers: FakePrinters, store: PrinterStore
) -> None:
    held = store.create(PrinterCreate(serial=SERIAL, name="Held"))
    store.link_bambuddy(held.id, 7)
    unlinked = store.create(PrinterCreate(serial=OTHER_SERIAL, name="Unlinked"))
    store.link_bambuddy(unlinked.id, 2)
    store.unlink_bambuddy(unlinked.id)
    result = await run(printers, FakeClient([bambuddy(1, SERIAL), bambuddy(2, OTHER_SERIAL)]))
    assert [e.action for e in result.entries] == ["conflict", "skipped"]
    assert store.require(held.id).bambuddy_printer_id == 7
    assert store.require(unlinked.id).bambuddy_printer_id is None


def test_plan_matches_the_store_rules(store: PrinterStore) -> None:
    printer = Printer.model_validate(bambuddy(3, SERIAL))
    assert plan_printer(printer, [])[0] == "created"


async def test_a_withheld_code_is_reported_and_nothing_is_probed(
    printers: FakePrinters, store: PrinterStore
) -> None:
    result = await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: None}))
    entry = result.entries[0]
    assert (entry.lan, entry.lan_reason) == ("withheld", LAN_WITHHELD)
    assert printers.probes == []
    (row,) = store.list()
    assert row.lan_host is None and row.lan_sync_error == LAN_WITHHELD


async def test_a_failed_probe_is_recorded_and_tried_again(
    printers: FakePrinters, store: PrinterStore
) -> None:
    printers.answer = False
    client = FakeClient([bambuddy(1, SERIAL)], codes={1: CODE})
    first = await run(printers, client)
    assert first.entries[0].lan == "failed"
    (row,) = store.list()
    assert row.lan_sync_error == LAN_PROBE_FAILED and row.lan_host is None
    printers.answer = True
    second = await run(printers, client)
    assert second.entries[0].lan == "linked"
    (row,) = store.list()
    assert row.lan_sync_error is None and row.lan_host == "192.168.1.10"


async def test_an_existing_lan_link_is_never_overwritten(
    printers: FakePrinters, store: PrinterStore
) -> None:
    mine = store.create(PrinterCreate(serial=SERIAL, name="Mine"))
    store.link_lan(mine.id, LanLink(host="192.168.1.50", access_code="mine1234"), cert_sha256=CERT)
    await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: CODE}))
    row = store.require(mine.id)
    assert row.lan_host == "192.168.1.50" and store.open_access_code(mine.id) == "mine1234"
    assert printers.probes == []


async def test_a_link_that_followed_bambuddy_moves_with_its_address(
    printers: FakePrinters, store: PrinterStore
) -> None:
    await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: CODE}))
    printers.probes.clear()
    result = await run(
        printers, FakeClient([bambuddy(1, SERIAL, ip="192.168.1.77")], codes={1: CODE})
    )
    assert result.entries[0].lan == "relinked"
    (row,) = store.list()
    assert row.lan_host == "192.168.1.77"
    assert printers.probes == [("192.168.1.77", SERIAL, CODE)]  # the stored code first


async def test_a_failed_move_is_tried_again_on_the_next_sync(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """Review #4770: the failed sync already stored Bambuddy's new address in the view,
    so only the recorded failure says the link still follows it."""
    await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: CODE}))
    moved = FakeClient([bambuddy(1, SERIAL, ip="192.168.1.77")], codes={1: CODE})
    printers.answer = False
    failed = await run(printers, moved)
    assert failed.entries[0].lan == "failed"
    (row,) = store.list()
    assert row.lan_host == "192.168.1.10" and row.lan_sync_error is not None

    printers.answer = True
    printers.probes.clear()
    again = await run(printers, moved)

    assert again.entries[0].lan == "relinked"
    (row,) = store.list()
    assert row.lan_host == "192.168.1.77" and row.lan_sync_error is None
    assert printers.probes[0] == ("192.168.1.77", SERIAL, CODE)


async def test_a_reason_a_later_sync_no_longer_has_is_cleared(
    printers: FakePrinters, store: PrinterStore
) -> None:
    await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: None}))
    (row,) = store.list()
    assert row.lan_sync_error is not None  # the code withheld
    await run(printers, FakeClient([bambuddy(1, SERIAL, ip=None)], codes={1: None}))
    assert store.require(row.id).lan_sync_error is None


async def test_names_follow_bambuddy_only_until_renamed(
    printers: FakePrinters, store: PrinterStore
) -> None:
    await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: CODE}))
    await run(printers, FakeClient([bambuddy(1, SERIAL, name="Renamed in Bambuddy")]))
    (row,) = store.list()
    assert row.name == "Renamed in Bambuddy"
    from scadbuddy.printers.models import PrinterPatch

    store.patch(row.id, PrinterPatch(name="Mine"))
    await run(printers, FakeClient([bambuddy(1, SERIAL, name="Again")]))
    assert store.require(row.id).name == "Mine"


async def test_virtual_printers_import_with_their_target(
    printers: FakePrinters, store: PrinterStore
) -> None:
    client = FakeClient(
        [bambuddy(1, SERIAL)],
        [
            vp(5, VP_SERIAL, 1),
            vp(6, "09400A391800002", 1, mode="archive"),
            vp(7, "09400A391800003", None),
        ],
        codes={1: CODE},
    )
    result = await run(printers, client)
    rows = {row.bambuddy_vp_id: row for row in store.list() if row.bambuddy_vp_id}
    target = next(row for row in store.list() if row.bambuddy_printer_id == 1)
    assert rows[5].kind == "bambuddy_virtual" and rows[5].forwards_to == target.id
    assert rows[6].forwards_to == target.id  # many VPs may target one printer
    assert rows[7].forwards_to is None and rows[7].model == "H2C"
    assert rows[5].vp_mode == "queue" and rows[5].lan_host is None
    assert [e.target_model for e in result.entries if e.bambuddy_vp_id == 7] == ["H2C"]
    assert printers.probes == [("192.168.1.10", SERIAL, CODE)]  # never a VP


async def test_a_hand_made_row_with_the_vps_serial_is_adopted_with_its_lan_link(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """Prod's row for 09400A391800001: added by serial, LAN-linked by hand."""
    made = store.create(PrinterCreate(serial=VP_SERIAL, name="H2C virtual"))
    store.link_lan(made.id, LanLink(host="192.168.10.9", access_code="vpcode12"), cert_sha256=CERT)
    result = await run(printers, FakeClient([], [vp(1, VP_SERIAL, None)]))
    assert result.entries[0].action == "linked"
    row = store.require(made.id)
    assert row.kind == "bambuddy_virtual" and row.bambuddy_vp_id == 1
    assert row.lan_host == "192.168.10.9" and row.name == "H2C virtual"


async def test_a_proxy_vp_reporting_its_targets_serial_gets_a_row_of_its_own(
    printers: FakePrinters, store: PrinterStore
) -> None:
    client = FakeClient([bambuddy(1, SERIAL)], [vp(9, SERIAL, 1, mode="proxy")], codes={1: CODE})
    result = await run(printers, client)
    physical = store.by_serial(SERIAL)
    assert physical is not None and physical.kind == "physical"
    virtual = next(row for row in store.list() if row.bambuddy_vp_id == 9)
    assert virtual.serial == "bambuddy-vp-9" and virtual.forwards_to == physical.id
    assert [e.action for e in result.entries if e.bambuddy_vp_id == 9] == ["conflict"]


async def test_a_proxy_vp_never_adopts_a_lan_only_printer_with_its_serial(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """Security review: a proxy VP reports its target's real serial, so a LAN-only row
    with that serial is the physical printer, never the VP."""
    lan_only = store.create(PrinterCreate(serial=SERIAL, name="LAN only"))
    store.link_lan(
        lan_only.id, LanLink(host="192.168.1.50", access_code="mine1234"), cert_sha256=CERT
    )
    result = await run(printers, FakeClient([], [vp(9, SERIAL, None, mode="proxy")]))
    (entry,) = result.entries
    assert entry.action == "conflict" and entry.reason is not None
    row = store.require(lan_only.id)
    assert row.kind == "physical" and row.bambuddy_vp_id is None and row.lan_host == "192.168.1.50"
    virtual = next(r for r in store.list() if r.bambuddy_vp_id == 9)
    assert virtual.serial == "bambuddy-vp-9" and entry.printer_id == virtual.id


async def test_a_vp_never_adopts_a_row_the_user_unlinked_from_bambuddy(
    printers: FakePrinters, store: PrinterStore
) -> None:
    unlinked = store.create(PrinterCreate(serial=VP_SERIAL, name="Unlinked"))
    store.link_bambuddy(unlinked.id, 4)
    store.unlink_bambuddy(unlinked.id)
    result = await run(printers, FakeClient([], [vp(1, VP_SERIAL, None)]))
    assert [e.action for e in result.entries] == ["conflict"]
    row = store.require(unlinked.id)
    assert row.kind == "physical" and row.bambuddy_unlinked_id == 4


async def test_a_vp_never_adopts_a_serial_a_bambuddy_printer_reports(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """A Bambuddy printer reporting the serial is that printer: the VP keeps out, in the
    plan and in the store, even if the physical pass left the row unlinked."""
    queue_vp = VirtualPrinter.model_validate(vp(1, SERIAL, None))
    assert may_adopt(queue_vp, {SERIAL}) is False
    assert may_adopt(queue_vp, set()) is True
    assert (
        may_adopt(VirtualPrinter.model_validate(vp(1, SERIAL, None, mode="proxy")), set()) is False
    )
    held = store.create(PrinterCreate(serial=SERIAL, name="Held"))
    store.link_bambuddy(held.id, 1)
    store.unlink_bambuddy(held.id)  # the physical pass then skips it, unlinked
    result = await run(printers, FakeClient([bambuddy(1, SERIAL)], [vp(2, SERIAL, None)]))
    assert [e.action for e in result.entries] == ["skipped", "conflict"]
    assert store.require(held.id).kind == "physical"


async def test_dry_run_plans_the_same_vp_conflict(
    printers: FakePrinters, store: PrinterStore
) -> None:
    store.create(PrinterCreate(serial=SERIAL, name="LAN only"))
    result = await run(printers, FakeClient([], [vp(9, SERIAL, None, mode="proxy")]), dry_run=True)
    assert [e.action for e in result.entries] == ["conflict"]
    assert all(r.kind == "physical" for r in store.list())


async def test_a_vp_bambuddy_no_longer_lists_is_reported_and_kept(
    printers: FakePrinters, store: PrinterStore
) -> None:
    await run(printers, FakeClient([], [vp(1, VP_SERIAL, None)]))
    result = await run(printers, FakeClient([], []))
    assert [e.action for e in result.entries] == ["missing"]
    assert len(store.list()) == 1


async def test_unreadable_virtual_printers_still_sync_the_printers(
    printers: FakePrinters, store: PrinterStore
) -> None:
    client = FakeClient([bambuddy(1, SERIAL)], codes={1: CODE})
    client.vp_error = ApiError(502, "Bambuddy has no virtual printers route")
    result = await run(printers, client)
    assert result.virtual_printers_error is not None and result.created == 1


async def test_the_access_code_reaches_no_log_and_no_result(
    printers: FakePrinters, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    printers.answer = False
    client = FakeClient([bambuddy(1, SERIAL)], codes={1: CODE})
    result = await run(printers, client)
    printers.answer = True
    result2 = await run(printers, client)
    assert CODE not in caplog.text
    assert CODE not in result.model_dump_json() and CODE not in result2.model_dump_json()
