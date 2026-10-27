"""The Bambuddy printer sync (#4439, #4747; plan 2026-10-26-bambuddy-printer-sync
Rulings 2-4): the registry follows Bambuddy, idempotently, and never overwrites."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from scadbuddy.bambuddy.models import Printer, VirtualPrinter, VirtualPrinterList
from scadbuddy.core.problems import ApiError
from scadbuddy.printers.bambuddy_sync import (
    LAN_PROBE_FAILED,
    LAN_WITHHELD,
    VP_TARGET_MISSING,
    PrinterSyncResult,
    may_adopt,
    plan_printer,
    printer_view,
    sync_printers,
    sync_virtual_printers,
)
from scadbuddy.printers.component import Printers
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.models import (
    BambuddyView,
    LanLink,
    PrinterCreate,
    PrinterPatch,
    PrinterRow,
)
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
        #: Run inside a code read: a write by something else in the middle of a sync.
        self.during_code_read: Callable[[], None] | None = None

    async def printers(self) -> list[Printer]:
        return [Printer.model_validate(p) for p in self._printers]

    async def virtual_printers(self) -> VirtualPrinterList:
        if self.vp_error is not None:
            raise self.vp_error
        return VirtualPrinterList.model_validate({"printers": self._vps, "models": {}})

    async def printer_access_code(self, printer_id: int) -> str | None:
        self.code_reads.append(printer_id)
        if self.during_code_read is not None:
            self.during_code_read()
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
        #: Run inside a probe: a change that lands while the sync dials the printer.
        self.during_probe: Callable[[], None] | None = None

        async def probe(host: str, serial: str, code: str) -> tuple[LanReport, str]:
            self.probes.append((host, serial, code))
            if self.during_probe is not None:
                self.during_probe()
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


async def test_each_entry_says_whether_the_sync_changed_its_row(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """#4798: the agent records the rows a sync wrote, `already_linked` ones included."""
    client = FakeClient([bambuddy(1, SERIAL)], [vp(5, VP_SERIAL, 1)], codes={1: CODE})
    first = await run(printers, client)
    assert [e.changed for e in first.entries] == [True, True]
    again = await run(printers, client)
    assert [e.changed for e in again.entries] == [False, False]
    renamed = FakeClient(
        [bambuddy(1, SERIAL, name="Renamed in Bambuddy")], [vp(5, VP_SERIAL, None)], codes={1: CODE}
    )
    third = await run(printers, renamed)
    assert [(e.action, e.changed) for e in third.entries] == [
        ("already_linked", True),
        ("already_linked", True),
    ]
    dry = await run(printers, renamed, dry_run=True)
    assert not any(e.changed for e in dry.entries)


async def test_a_rename_landing_before_the_view_is_recorded_is_not_the_syncs_change(
    printers: FakePrinters, store: PrinterStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4858 review: the user renames the printer after the pass read its rows and
    before `record_bambuddy_view` locks the row. The sync keeps the user's name and
    writes nothing, so its entry is not `changed`."""
    client = FakeClient([bambuddy(1, SERIAL)], codes={1: None})
    await run(printers, client)
    (row,) = store.list()
    real = store.record_bambuddy_view

    def renamed_first(printer_id: str, view: BambuddyView, *, model: str | None = None) -> Any:
        store.patch(printer_id, PrinterPatch(name="Mine"))
        return real(printer_id, view, model=model)

    monkeypatch.setattr(store, "record_bambuddy_view", renamed_first)
    again = await run(printers, client)
    assert store.require(row.id).name == "Mine"
    assert again.entries[0].changed is False


async def test_an_adoption_the_strip_made_meanwhile_is_not_the_syncs_change(
    printers: FakePrinters, store: PrinterStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4861 review: the pass read the row with its synthetic serial, then the strip's own
    `ensure_bambuddy` adopted the real one before the sync's. The sync wrote nothing, so
    its entry is not `changed`, though the serial it read did change."""
    synthetic = store.ensure_bambuddy(1, serial=None, name="P1", model="H2C")
    printer = Printer.model_validate(bambuddy(1, SERIAL, ip=None))
    store.record_bambuddy_view(synthetic.id, printer_view(printer, datetime.now(UTC)))
    real_list = store.list

    def listed_then_adopted() -> list[PrinterRow]:
        rows = real_list()
        monkeypatch.setattr(store, "list", real_list)
        store.ensure_bambuddy(1, serial=SERIAL, name="P1", model="H2C")  # the strip
        return rows

    monkeypatch.setattr(store, "list", listed_then_adopted)
    result = await run(printers, FakeClient([bambuddy(1, SERIAL, ip=None)]))
    (entry,) = result.entries
    assert entry.action == "already_linked" and store.require(synthetic.id).serial == SERIAL
    assert entry.changed is False


async def test_the_syncs_own_adoption_is_its_change(
    printers: FakePrinters, store: PrinterStore
) -> None:
    synthetic = store.ensure_bambuddy(1, serial=None, name="P1", model="H2C")
    printer = Printer.model_validate(bambuddy(1, SERIAL, ip=None))
    store.record_bambuddy_view(synthetic.id, printer_view(printer, datetime.now(UTC)))
    # #4864 review: the view the sync records is the one already there, so the adoption
    # is the only write that can make the entry `changed`.
    _, recorded = store.record_bambuddy_view(
        synthetic.id, printer_view(printer, datetime.now(UTC)), model="H2C"
    )
    assert recorded is False
    result = await run(printers, FakeClient([bambuddy(1, SERIAL, ip=None)]))
    (entry,) = result.entries
    assert store.require(synthetic.id).serial == SERIAL and entry.changed is True


async def test_the_syncs_merge_into_the_serials_row_is_its_change(
    printers: FakePrinters, store: PrinterStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4864 review: `_ensure_locked`'s merge path. The Bambuddy printer's synthetic row
    learns its serial, which an unlinked physical row already holds: the synthetic row is
    merged into that one, which takes the link, and both are announced."""
    synthetic = store.ensure_bambuddy(1, serial=None, name="P1", model="H2C")
    printer = Printer.model_validate(bambuddy(1, SERIAL, ip=None))
    store.record_bambuddy_view(synthetic.id, printer_view(printer, datetime.now(UTC)))
    holder = store.create(PrinterCreate(serial=SERIAL, name="Mine", model="H2C"))
    store.record_bambuddy_view(holder.id, printer_view(printer, datetime.now(UTC)))
    # As above: the view is already the holder's, so only the merge can be the write.
    _, recorded = store.record_bambuddy_view(
        holder.id, printer_view(printer, datetime.now(UTC)), model="H2C"
    )
    assert recorded is False
    announced: list[str] = []
    monkeypatch.setattr(store, "_announce", lambda *ids: announced.extend(ids))

    result = await run(printers, FakeClient([bambuddy(1, SERIAL, ip=None)]))

    (entry,) = result.entries
    assert (entry.action, entry.printer_id, entry.changed) == ("already_linked", holder.id, True)
    assert announced == [synthetic.id, holder.id]
    assert store.get(synthetic.id) is None
    merged = store.require(holder.id)
    assert merged.bambuddy_printer_id == 1 and merged.name == "Mine"
    assert holder.id in printers.changed_ids


async def test_a_write_by_something_else_during_a_sync_is_not_its_change(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """#4850 review: `changed` is what this sync wrote, never a bumped `updated_at`."""
    client = FakeClient([bambuddy(1, SERIAL)], codes={1: None})
    await run(printers, client)  # linked; the code withheld, and recorded so
    (row,) = store.list()

    def bump() -> None:
        with store.pool.connection() as conn:
            conn.execute("UPDATE printers SET updated_at = now() WHERE id = %s", (row.id,))

    client.during_code_read = bump
    again = await run(printers, client)
    assert again.entries[0].lan == "withheld"
    assert store.require(row.id).updated_at > row.updated_at
    assert again.entries[0].changed is False


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


async def test_a_link_the_user_makes_during_a_move_is_kept(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """#4796 finding 1: the move's probe passes, but the user linked the printer
    meanwhile. That link wins now, and is not marked as a failed move that the next
    sync then moves to Bambuddy's address."""
    await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: CODE}))
    (row,) = store.list()
    moved = FakeClient([bambuddy(1, SERIAL, ip="192.168.1.77")], codes={1: CODE})

    def users_link() -> None:
        printers.during_probe = None
        store.link_lan(
            row.id, LanLink(host="192.168.1.50", access_code="mine1234"), cert_sha256=CERT
        )

    printers.during_probe = users_link
    result = await run(printers, moved)
    assert result.entries[0].lan == "kept"
    after = store.require(row.id)
    assert after.lan_host == "192.168.1.50"
    assert after.lan_sync_error is None and not after.lan_move_pending

    await run(printers, moved)
    assert store.require(row.id).lan_host == "192.168.1.50"
    assert store.open_access_code(row.id) == "mine1234"


async def test_a_failed_move_is_retried_whatever_its_message_says(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """#4796 finding 2: the retry is its own state, not the wording of the message a
    row was stored with."""
    await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: CODE}))
    moved = FakeClient([bambuddy(1, SERIAL, ip="192.168.1.77")], codes={1: CODE})
    printers.answer = False
    await run(printers, moved)
    (row,) = store.list()
    assert row.lan_move_pending
    store.set_lan_sync_error(row.id, "an older wording of the failed-move message")

    printers.answer = True
    again = await run(printers, moved)
    assert again.entries[0].lan == "relinked"
    assert store.require(row.id).lan_host == "192.168.1.77"


async def test_a_failed_move_survives_a_sync_with_no_address(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """#4796 finding 3: Bambuddy reporting no address for one sync does not forget the
    move that is due."""
    await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: CODE}))
    moved = FakeClient([bambuddy(1, SERIAL, ip="192.168.1.77")], codes={1: CODE})
    printers.answer = False
    await run(printers, moved)
    printers.answer = True
    blank = await run(printers, FakeClient([bambuddy(1, SERIAL, ip=None)], codes={1: CODE}))
    assert blank.entries[0].lan == "kept"

    again = await run(printers, moved)
    assert again.entries[0].lan == "relinked"
    row = store.list()[0]
    assert row.lan_host == "192.168.1.77" and not row.lan_move_pending


async def test_a_pending_move_ends_when_bambuddy_returns_to_the_links_address(
    printers: FakePrinters, store: PrinterStore
) -> None:
    await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: CODE}))
    printers.answer = False
    await run(printers, FakeClient([bambuddy(1, SERIAL, ip="192.168.1.77")], codes={1: CODE}))
    back = await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: CODE}))
    assert back.entries[0].lan == "kept"
    (row,) = store.list()
    assert row.lan_host == "192.168.1.10"
    assert row.lan_sync_error is None and not row.lan_move_pending
    assert back.entries[0].changed


async def test_ending_a_pending_move_writes_the_lan_state_once(
    printers: FakePrinters, store: PrinterStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4850 review: the pending flag and the stale reason clear in one write."""
    await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: CODE}))
    printers.answer = False
    await run(printers, FakeClient([bambuddy(1, SERIAL, ip="192.168.1.77")], codes={1: CODE}))
    writes: list[tuple[str | None, bool | None]] = []
    real = store.set_lan_sync_error

    def counted(
        printer_id: str, error: str | None, *, move_pending: bool | None = None
    ) -> PrinterRow:
        writes.append((error, move_pending))
        return real(printer_id, error, move_pending=move_pending)

    monkeypatch.setattr(store, "set_lan_sync_error", counted)
    await run(printers, FakeClient([bambuddy(1, SERIAL)], codes={1: CODE}))
    assert writes == [(None, False)]


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


async def test_a_vp_whose_target_has_no_row_says_so_never_any(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """#4797: Bambuddy forwards the VP to printer 1, which has no row here (no serial,
    so it was skipped). Its NULL `forwards_to` is not "any H2C": the entry says so,
    and the row keeps Bambuddy's target id in its view."""
    result = await run(printers, FakeClient([bambuddy(1, None)], [vp(5, VP_SERIAL, 1)]))
    entry = next(e for e in result.entries if e.bambuddy_vp_id == 5)
    # #4850 review: the VP's own row was created, and the entry still says so.
    assert (entry.action, entry.target_missing) == ("created", True)
    assert entry.reason == VP_TARGET_MISSING.format(target=1) and entry.changed
    assert result.target_missing == 1  # #4858 review: the housekeeping log reads it
    assert entry.forwards_to is None and entry.target_model is None
    row = next(r for r in store.list() if r.bambuddy_vp_id == 5)
    assert row.forwards_to is None
    assert row.bambuddy_view is not None and row.bambuddy_view.target_printer_id == 1
    dry = await run(printers, FakeClient([bambuddy(1, None)], [vp(5, VP_SERIAL, 1)]), dry_run=True)
    assert [(e.action, e.target_missing) for e in dry.entries if e.bambuddy_vp_id == 5] == [
        ("already_linked", True)
    ]


async def test_a_vp_whose_target_this_sync_makes_is_not_missing(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """#4797 review: the target is created in the same pass. A dry run writes no row
    for it, so only the plan says it is there; neither run calls it missing."""
    client = FakeClient([bambuddy(1, SERIAL)], [vp(5, VP_SERIAL, 1)], codes={1: CODE})
    dry = await run(printers, client, dry_run=True)
    assert [(e.action, e.reason) for e in dry.entries] == [("created", None), ("created", None)]
    real = await run(printers, client)
    assert [(e.action, e.reason) for e in real.entries] == [("created", None), ("created", None)]


async def test_a_vp_whose_target_row_was_deleted_says_so(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """#4797: deleting the target row sets `forwards_to` NULL (ON DELETE SET NULL);
    the next sync reports it rather than reading it as "any <model>"."""
    await run(printers, FakeClient([bambuddy(1, SERIAL)], [vp(5, VP_SERIAL, 1)], codes={1: CODE}))
    target = store.by_serial(SERIAL)
    assert target is not None
    store.delete(target.id)
    gone = FakeClient([bambuddy(1, SERIAL, is_active=False)], [vp(5, VP_SERIAL, 1)])
    result = await run(printers, gone)
    (entry,) = result.entries
    assert (entry.action, entry.target_missing) == ("already_linked", True)
    assert entry.reason == VP_TARGET_MISSING.format(target=1)


async def test_a_deleted_target_bambuddy_still_lists_comes_back_and_is_not_missing(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """#4850 review: the deleted target is still active in Bambuddy, so this sync
    creates its row again and the VP forwards to it."""
    client = FakeClient([bambuddy(1, SERIAL)], [vp(5, VP_SERIAL, 1)], codes={1: CODE})
    await run(printers, client)
    target = store.by_serial(SERIAL)
    assert target is not None
    store.delete(target.id)
    result = await run(printers, client)
    again = store.by_serial(SERIAL)
    assert again is not None
    assert [(e.action, e.target_missing, e.reason) for e in result.entries] == [
        ("created", False, None),
        ("already_linked", False, None),
    ]
    assert result.entries[1].forwards_to == again.id
    virtual = next(r for r in store.list() if r.bambuddy_vp_id == 5)
    assert virtual.forwards_to == again.id


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
    assert store.list()[0].bambuddy_active is True
    printers.changed_ids.clear()
    result = await run(printers, FakeClient([], []))
    assert [e.action for e in result.entries] == ["missing"]
    (row,) = store.list()
    # #4889: marked on the row, not only in the result; listed again, it is back.
    assert row.bambuddy_active is False and printers.changed_ids == [row.id]
    dry = await run(printers, FakeClient([], [vp(1, VP_SERIAL, None)]), dry_run=True)
    assert dry.dry_run and store.require(row.id).bambuddy_active is False
    await run(printers, FakeClient([], [vp(1, VP_SERIAL, None)]))
    assert store.require(row.id).bambuddy_active is True


async def test_the_follow_after_a_vp_write_marks_one_bambuddy_no_longer_lists(
    printers: FakePrinters, store: PrinterStore
) -> None:
    """#4889: the follow a delete runs (`sync_virtual_printers`) marks the gone VP's
    row, as the full sync does, rather than leaving it listed until the next sweep."""
    await run(printers, FakeClient([], [vp(1, VP_SERIAL, None), vp(2, OTHER_SERIAL, None)]))
    await sync_virtual_printers(
        cast(Any, printers), cast(Any, FakeClient([], [vp(2, OTHER_SERIAL, None)]))
    )
    active = {row.bambuddy_vp_id: row.bambuddy_active for row in store.list()}
    assert active == {1: False, 2: True}


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


async def test_the_virtual_pass_alone_runs_over_a_bare_store(store: PrinterStore) -> None:
    """Review #4784: the print worker has no `Printers`; a VP write there still brings
    the registry along, and no probe runs (a VP gets no LAN link)."""
    from scadbuddy.printers.bambuddy_sync import StoreRegistry, sync_virtual_printers

    client = FakeClient([bambuddy(1, SERIAL)], [vp(5, VP_SERIAL, None)], codes={1: CODE})
    await sync_virtual_printers(StoreRegistry(store), cast(Any, client))
    (row,) = store.list()
    assert row.kind == "bambuddy_virtual" and row.bambuddy_vp_id == 5
    assert row.lan_host is None


async def test_the_housekeeping_sweep_logs_a_missing_target_quietly_unless_there_is_news(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#4858 review: a sync whose only news is a VP with a missing target still logs it.
    #4861 review: at DEBUG, as that VP repeats it on every sweep until its target has a
    row; at INFO beside real news (a printer created)."""
    from types import SimpleNamespace

    from scadbuddy import main
    from scadbuddy.printers.bambuddy_sync import SyncEntry

    missing = SyncEntry(
        kind="bambuddy_virtual",
        name="VP5",
        bambuddy_vp_id=5,
        action="already_linked",
        reason=VP_TARGET_MISSING.format(target=1),
        target_missing=True,
    )
    created = SyncEntry(kind="physical", name="P1", bambuddy_printer_id=1, action="created")
    result = PrinterSyncResult(dry_run=False, entries=[missing])

    async def synced(*_: object) -> PrinterSyncResult:
        return result

    async def with_bambuddy(_state: object, sweep: Callable[[Any], Any]) -> None:
        await sweep(object())

    monkeypatch.setattr(main, "sync_printers", synced)
    monkeypatch.setattr(main, "_with_bambuddy", with_bambuddy)
    caplog.set_level(logging.DEBUG, logger=main.logger.name)
    state = SimpleNamespace(components=SimpleNamespace(get=lambda _key: None))

    def logged() -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.getMessage() == "synced the printers from Bambuddy"]

    await main._sync_bambuddy_printers_logged(cast(Any, state))
    (record,) = logged()
    assert getattr(record, "target_missing", None) == 1
    assert record.levelno == logging.DEBUG

    caplog.clear()
    result = PrinterSyncResult(dry_run=False, entries=[created, missing])
    await main._sync_bambuddy_printers_logged(cast(Any, state))
    (record,) = logged()
    assert record.levelno == logging.INFO and getattr(record, "target_missing", None) == 1
