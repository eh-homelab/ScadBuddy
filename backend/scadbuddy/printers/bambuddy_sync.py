"""The Bambuddy printer sync (#4439, #4747; plan 2026-10-26-bambuddy-printer-sync):
one idempotent pass that brings the registry up to what Bambuddy knows.

- Each active Bambuddy printer with a serial gets a row, linked (Rulings 2): created,
  linked to the row holding its serial, or left as it is when already linked. A printer
  with no serial is skipped and a conflict is reported; nothing is unlinked, deleted or
  overwritten.
- A linked row with no LAN link gets one from Bambuddy's ``ip_address`` and
  ``access_code`` through the probe ``PUT /printers/{id}/lan`` runs (Ruling 3). An
  existing link is kept, except that a link that followed Bambuddy's address moves when
  Bambuddy's address does. Bambuddy gives the code to an API key only while its auth is
  off (`BambuddyClient.printer_access_code`); otherwise the row says the code is
  withheld. The code goes from that read to the probe and the sealed column, and nowhere
  else: no log, span, response, activity row or Temporal payload.
- Each Bambuddy virtual printer gets a ``bambuddy_virtual`` row (Ruling 4), naming the
  printer it forwards to, or none ("any <model>").

The pass runs in the API, from ``POST /printers/sync-bambuddy`` and from the housekeeping
sweep ``housekeeping_sync_bambuddy_printers`` on the ``library`` worker (the API's own
process), so the code never crosses a process. ``dry_run`` plans the same steps and
writes nothing."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ValidationError, computed_field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import Printer, VirtualPrinter
from scadbuddy.core.problems import ApiError
from scadbuddy.printers.address import UnsafeAddressError
from scadbuddy.printers.component import Printers
from scadbuddy.printers.lan_link import LanProbeFailedError, probe_and_link
from scadbuddy.printers.models import (
    BambuddyView,
    LanLink,
    PrinterKind,
    PrinterRow,
    is_synthetic_serial,
)
from scadbuddy.printers.store import (
    BambuddyLinkTakenError,
    KekMissingError,
    LanLinkChangedError,
    NoAccessCodeError,
    PrinterNotFoundError,
    PrinterStore,
    SerialTakenError,
    SerialUnknownError,
    VirtualPrinterRowError,
    adopts_for_virtual,
)

logger = logging.getLogger(__name__)

SyncAction = Literal["created", "linked", "already_linked", "skipped", "conflict", "missing"]
#: What happened to the row's LAN link: ``linked`` (filled from Bambuddy), ``relinked``
#: (moved to Bambuddy's new address), ``kept`` (an existing link, untouched), ``failed``
#: (a probe failed; the next sync tries again), ``withheld`` (Bambuddy gave no code),
#: ``none`` (nothing to fill from: no address, or a serial ScadBuddy does not know),
#: ``would_try`` (a dry run that would probe).
LanAction = Literal["linked", "relinked", "kept", "failed", "withheld", "none", "would_try"]

NO_SERIAL = "Bambuddy did not report a serial: link it from a printer you created by serial"
UNLINKED_BY_USER = (
    "this printer was unlinked from Bambuddy printer {id} by hand; link it again in"
    " Settings > Printers"
)
SERIAL_LINKED_ELSEWHERE = (
    "the row with this serial is linked to Bambuddy printer {other}; nothing was changed"
)
SERIAL_IS_VIRTUAL = "the row with this serial is a Bambuddy virtual printer; nothing was changed"
RACE = "another change to this printer landed meanwhile; the next sync tries again"
SERIAL_KEPT = (
    "another printer holds this virtual printer's serial and keeps it; the virtual"
    " printer has a row of its own, bambuddy-vp-{vp_id}"
)
NOT_LISTED = "Bambuddy no longer lists this virtual printer; the row is kept"

LAN_WITHHELD = (
    "Bambuddy withholds the access code from API keys while its auth is on: link the"
    " LAN in Settings > Printers"
)
LAN_UNREADABLE = "could not read the access code from Bambuddy; the next sync tries again"
LAN_PROBE_FAILED = (
    "the printer did not answer at Bambuddy's address, or did not take Bambuddy's"
    " access code; the next sync tries again"
)
LAN_MOVE_FAILED = (
    "Bambuddy reports a new address and the printer did not answer there; the LAN link"
    " keeps the old one, and the next sync tries again"
)
LAN_UNSAFE = "Bambuddy's address for this printer is not a private LAN address"
LAN_KEK_MISSING = "LAN printing needs SCADBUDDY_SECRET_KEY_FILE"


class SyncEntry(BaseModel):
    kind: PrinterKind
    name: str
    bambuddy_printer_id: int | None = None
    bambuddy_vp_id: int | None = None
    #: The registry row, when there is (or, in a dry run, already was) one.
    printer_id: str | None = None
    action: SyncAction
    reason: str | None = None
    lan: LanAction | None = None
    lan_reason: str | None = None
    #: A virtual printer's target row; ``None`` with a ``target_model`` is "any <model>".
    forwards_to: str | None = None
    target_model: str | None = None


class PrinterSyncResult(BaseModel):
    dry_run: bool
    entries: list[SyncEntry]
    #: Why virtual printers could not be read (an older Bambuddy); printers still synced.
    virtual_printers_error: str | None = None

    def _count(self, action: SyncAction) -> int:
        return sum(1 for entry in self.entries if entry.action == action)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def created(self) -> int:
        return self._count("created")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def linked(self) -> int:
        return self._count("linked")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def already_linked(self) -> int:
        return self._count("already_linked")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def skipped(self) -> int:
        return self._count("skipped")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def conflicts(self) -> int:
        return self._count("conflict")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def lan_linked(self) -> int:
        return sum(1 for entry in self.entries if entry.lan in ("linked", "relinked"))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def lan_failed(self) -> int:
        return sum(1 for entry in self.entries if entry.lan in ("failed", "withheld"))


# ── planning (pure) ──────────────────────────────────────────────────────────────


def plan_printer(
    printer: Printer, rows: list[PrinterRow]
) -> tuple[SyncAction, PrinterRow | None, str | None]:
    """What the sync does for one active Bambuddy printer, from the rows alone."""
    linked = next((r for r in rows if r.bambuddy_printer_id == printer.id), None)
    if linked is not None:
        return "already_linked", linked, None
    serial = printer.serial_number
    if serial is None:
        return "skipped", None, NO_SERIAL
    holder = next((r for r in rows if r.serial == serial), None)
    if holder is None:
        return "created", None, None
    if holder.kind != "physical":
        return "conflict", holder, SERIAL_IS_VIRTUAL
    if holder.bambuddy_printer_id is not None:
        return "conflict", holder, SERIAL_LINKED_ELSEWHERE.format(other=holder.bambuddy_printer_id)
    if holder.bambuddy_unlinked_id == printer.id:
        return "skipped", holder, UNLINKED_BY_USER.format(id=printer.id)
    return "linked", holder, None


def may_adopt(vp: VirtualPrinter, physical_serials: set[str]) -> bool:
    """Whether the VP may take over a row by its serial at all: never in ``proxy`` mode
    (its serial is its target's, `VirtualPrinter`), nor when a Bambuddy printer reports
    that serial (security review of the sync)."""
    return vp.mode != "proxy" and vp.serial not in physical_serials


def plan_virtual(
    vp: VirtualPrinter, rows: list[PrinterRow], physical_serials: set[str]
) -> tuple[SyncAction, PrinterRow | None, str | None]:
    """What the sync does for one virtual printer (Ruling 4), from the rows alone: the
    same rules as `PrinterStore.upsert_virtual`."""
    own = next((r for r in rows if r.bambuddy_vp_id == vp.id), None)
    if own is not None:
        return "already_linked", own, None
    holder = None if vp.serial is None else next((r for r in rows if r.serial == vp.serial), None)
    if holder is None:
        return "created", None, None
    if adopts_for_virtual(holder, may_adopt=may_adopt(vp, physical_serials)):
        return "linked", holder, None
    return "conflict", holder, SERIAL_KEPT.format(vp_id=vp.id)


def printer_view(printer: Printer, now: datetime) -> BambuddyView:
    return BambuddyView(
        seen_at=now,
        name=printer.name,
        model=printer.model,
        ip_address=printer.ip_address,
        is_active=printer.is_active,
        nozzle_count=printer.nozzle_count,
    )


def virtual_view(vp: VirtualPrinter, now: datetime) -> BambuddyView:
    return BambuddyView(
        seen_at=now,
        name=vp.name,
        model=vp.model_name or vp.model,
        model_code=vp.model,
        enabled=vp.enabled,
        mode=vp.mode,
        target_printer_id=vp.target_printer_id,
        bind_ip=vp.bind_ip,
        remote_interface_ip=vp.remote_interface_ip,
        access_code_set=vp.access_code_set,
        running=vp.status.running,
        pending_files=vp.status.pending_files,
    )


# ── the pass ─────────────────────────────────────────────────────────────────────


async def sync_printers(
    printers: Printers, client: BambuddyClient, *, dry_run: bool = False
) -> PrinterSyncResult:
    """One pass (module docstring). Bambuddy's printer list failing fails the pass
    (an `ApiError`); its virtual-printer list failing is reported and the printers
    still sync."""
    now = datetime.now(UTC)
    active = [printer for printer in await client.printers() if printer.is_active]
    vp_error: str | None = None
    vps: list[VirtualPrinter] = []
    try:
        vps = (await client.virtual_printers()).printers
    except ApiError as error:
        vp_error = error.detail
    store = printers.store
    rows = await asyncio.to_thread(store.list)
    entries: list[SyncEntry] = []
    for printer in active:
        entries.append(await _sync_printer(printers, client, printer, rows, now, dry_run=dry_run))
    rows = await asyncio.to_thread(store.list)
    physical_serials = {p.serial_number for p in active if p.serial_number is not None}
    for vp in vps:
        entries.append(
            await _sync_virtual(printers, vp, rows, physical_serials, now, dry_run=dry_run)
        )
    if vp_error is None:
        listed = {vp.id for vp in vps}
        entries.extend(
            SyncEntry(
                kind=row.kind,
                name=row.name,
                bambuddy_vp_id=row.bambuddy_vp_id,
                printer_id=row.id,
                action="missing",
                reason=NOT_LISTED,
                forwards_to=row.forwards_to,
            )
            for row in rows
            if row.bambuddy_vp_id is not None and row.bambuddy_vp_id not in listed
        )
    return PrinterSyncResult(dry_run=dry_run, entries=entries, virtual_printers_error=vp_error)


async def _sync_printer(
    printers: Printers,
    client: BambuddyClient,
    printer: Printer,
    rows: list[PrinterRow],
    now: datetime,
    *,
    dry_run: bool,
) -> SyncEntry:
    store = printers.store
    action, row, reason = plan_printer(printer, rows)
    entry = SyncEntry(
        kind="physical",
        name=printer.name,
        bambuddy_printer_id=printer.id,
        printer_id=None if row is None else row.id,
        action=action,
        reason=reason,
    )
    if action in ("skipped", "conflict"):
        return entry
    if dry_run:
        has_lan = row is not None and row.lan_host is not None
        serial_known = printer.serial_number is not None or (
            row is not None and not is_synthetic_serial(row.serial)
        )
        if has_lan:
            entry.lan = "kept"
        elif printer.ip_address is not None and serial_known:
            entry.lan = "would_try"
        else:
            entry.lan = "none"
        return entry
    try:
        if action == "created":
            assert printer.serial_number is not None
            row = await asyncio.to_thread(
                store.create_linked,
                serial=printer.serial_number,
                name=printer.name,
                model=printer.model,
                bambuddy_printer_id=printer.id,
            )
        elif action == "linked":
            assert row is not None
            row = await asyncio.to_thread(store.link_bambuddy, row.id, printer.id)
        else:
            assert row is not None
            if printer.serial_number is not None and is_synthetic_serial(row.serial):
                # The serial is known now: the strip's own adoption (or merge).
                row = await asyncio.to_thread(
                    store.ensure_bambuddy,
                    printer.id,
                    serial=printer.serial_number,
                    name=printer.name,
                    model=printer.model,
                )
    except (SerialTakenError, BambuddyLinkTakenError, VirtualPrinterRowError):
        entry.action, entry.reason = "conflict", RACE
        return entry
    except PrinterNotFoundError:
        entry.action, entry.reason = "conflict", RACE
        return entry
    assert row is not None
    entry.printer_id = row.id
    previous_ip = None if row.bambuddy_view is None else row.bambuddy_view.ip_address
    before = row
    row = await asyncio.to_thread(
        store.record_bambuddy_view, row.id, printer_view(printer, now), model=printer.model
    )
    entry.lan, entry.lan_reason = await _sync_lan(printers, client, row, printer, previous_ip)
    if action != "already_linked" or row != before:
        printers.changed(row.id)
    return entry


async def fill_lan_after_link(
    printers: Printers, client: BambuddyClient, row: PrinterRow, printer: Printer
) -> PrinterRow:
    """#4747 item 2 for a manual link (``PUT /printers/{id}/bambuddy``): record what
    Bambuddy reports and, when the row has no LAN link, fill it from Bambuddy as the sync
    would. Best effort: a failure is the row's `lan_sync_error`, never the link's."""
    store = printers.store
    previous_ip = None if row.bambuddy_view is None else row.bambuddy_view.ip_address
    row = await asyncio.to_thread(
        store.record_bambuddy_view, row.id, printer_view(printer, datetime.now(UTC)), model=None
    )
    await _sync_lan(printers, client, row, printer, previous_ip)
    return await asyncio.to_thread(store.require, row.id)


async def _sync_lan(
    printers: Printers,
    client: BambuddyClient,
    row: PrinterRow,
    printer: Printer,
    previous_ip: str | None,
) -> tuple[LanAction, str | None]:
    """Fill an empty LAN link from Bambuddy, or move one that followed Bambuddy's
    address (Ruling 3). Records the outcome's reason on the row (`lan_sync_error`), and
    clears a reason an earlier sync left when this one has none."""
    action, reason = await _lan_outcome(printers, client, row, printer, previous_ip)
    if reason is None and action in ("kept", "none") and row.lan_sync_error is not None:
        with contextlib.suppress(PrinterNotFoundError):
            await asyncio.to_thread(printers.store.set_lan_sync_error, row.id, None)
        printers.changed(row.id)
    return action, reason


async def _lan_outcome(
    printers: Printers,
    client: BambuddyClient,
    row: PrinterRow,
    printer: Printer,
    previous_ip: str | None,
) -> tuple[LanAction, str | None]:
    ip = printer.ip_address
    if is_synthetic_serial(row.serial) or ip is None:
        return ("kept", None) if row.lan_host is not None else ("none", None)
    if row.lan_host is not None:
        # A move is due when the link followed Bambuddy's last address, or when an
        # earlier move failed: that sync already recorded the new address in the view,
        # so only the recorded failure still says the link was following it.
        followed = previous_ip is not None and row.lan_host == previous_ip
        retry = row.lan_sync_error == LAN_MOVE_FAILED
        if not (followed or retry) or ip == row.lan_host:
            return "kept", None
        return await _move_lan(printers, client, row, printer, ip)
    try:
        code = await client.printer_access_code(printer.id)
    except ApiError:
        return await _lan_failed(printers, row, "failed", LAN_UNREADABLE)
    if code is None:
        return await _lan_failed(printers, row, "withheld", LAN_WITHHELD)
    return await _link(printers, row, ip, code, expect_host=None, failed=LAN_PROBE_FAILED)


async def _move_lan(
    printers: Printers, client: BambuddyClient, row: PrinterRow, printer: Printer, ip: str
) -> tuple[LanAction, str | None]:
    """Bambuddy's address moved: probe the new one with the stored code, then with
    Bambuddy's when it differs; the first that passes is the link (#4747 item 2, "the
    stored code stays unless Bambuddy's differs and the probe passes with it")."""
    old_host = row.lan_host
    try:
        stored = await asyncio.to_thread(printers.store.open_access_code, row.id)
    except (NoAccessCodeError, KekMissingError, PrinterNotFoundError):
        stored = None
    except Exception:  # a code another key sealed: Bambuddy's may still do
        logger.warning("could not open a printer's access code", extra={"printer_id": row.id})
        stored = None
    if stored is not None:
        outcome = await _link(printers, row, ip, stored, expect_host=old_host, failed=None)
        if outcome[0] == "linked":
            return "relinked", None
    try:
        code = await client.printer_access_code(printer.id)
    except ApiError:
        code = None
    if code is not None and code != stored:
        outcome = await _link(printers, row, ip, code, expect_host=old_host, failed=None)
        if outcome[0] == "linked":
            return "relinked", None
    return await _lan_failed(printers, row, "failed", LAN_MOVE_FAILED)


async def _link(
    printers: Printers,
    row: PrinterRow,
    host: str,
    code: str,
    *,
    expect_host: str | None,
    failed: str | None,
) -> tuple[LanAction, str | None]:
    """One probe and link; ``failed`` is recorded on the row when it does not pass
    (``None``: the caller records its own)."""
    try:
        body = LanLink(host=host, access_code=code)
    except ValidationError:
        reason = LAN_UNSAFE if failed is not None else None
        return await _lan_failed(printers, row, "failed", reason)
    try:
        await probe_and_link(printers, row, body, expect_host=expect_host)
    except LanLinkChangedError:
        return "kept", None
    except UnsafeAddressError:
        return await _lan_failed(printers, row, "failed", LAN_UNSAFE if failed else None)
    except KekMissingError:
        return await _lan_failed(printers, row, "failed", LAN_KEK_MISSING if failed else None)
    except (LanProbeFailedError, SerialUnknownError, PrinterNotFoundError):
        return await _lan_failed(printers, row, "failed", failed)
    return "linked", None


async def _lan_failed(
    printers: Printers, row: PrinterRow, action: LanAction, reason: str | None
) -> tuple[LanAction, str | None]:
    if reason is not None and reason != row.lan_sync_error:
        with contextlib.suppress(PrinterNotFoundError):
            await asyncio.to_thread(printers.store.set_lan_sync_error, row.id, reason)
        printers.changed(row.id)
    return action, reason


class VirtualRegistry(Protocol):
    """What the virtual-printer part of the pass needs: the store, and a way to
    announce a row's change. `Printers` is one; the print worker's is `StoreRegistry`."""

    @property
    def store(self) -> PrinterStore: ...

    def changed(self, printer_id: str) -> None: ...


class StoreRegistry:
    """A `VirtualRegistry` over a bare store, for a process with no `Printers`
    component (the print worker, #1060): a change is announced by nothing there, and the
    page that made the write reads the registry again."""

    def __init__(self, store: PrinterStore) -> None:
        self._store = store

    @property
    def store(self) -> PrinterStore:
        return self._store

    def changed(self, printer_id: str) -> None:
        return None


async def sync_virtual_printers(registry: VirtualRegistry, client: BambuddyClient) -> None:
    """The pass's virtual-printer part alone (#4747 item 4): the registry follows a VP
    write on whichever worker ran it, so a write that outlives the route's deadline is
    followed too. No LAN link is made here (a VP gets none), so no probe runs."""
    now = datetime.now(UTC)
    active = [printer for printer in await client.printers() if printer.is_active]
    vps = (await client.virtual_printers()).printers
    rows = await asyncio.to_thread(registry.store.list)
    physical_serials = {p.serial_number for p in active if p.serial_number is not None}
    for vp in vps:
        await _sync_virtual(registry, vp, rows, physical_serials, now, dry_run=False)


async def _sync_virtual(
    printers: VirtualRegistry,
    vp: VirtualPrinter,
    rows: list[PrinterRow],
    physical_serials: set[str],
    now: datetime,
    *,
    dry_run: bool,
) -> SyncEntry:
    action, row, reason = plan_virtual(vp, rows, physical_serials)
    target = (
        None
        if vp.target_printer_id is None
        else next((r for r in rows if r.bambuddy_printer_id == vp.target_printer_id), None)
    )
    entry = SyncEntry(
        kind="bambuddy_virtual",
        name=vp.name,
        bambuddy_vp_id=vp.id,
        printer_id=None if row is None or action == "conflict" else row.id,
        action=action,
        reason=reason,
        forwards_to=None if target is None else target.id,
        target_model=(vp.model_name or vp.model) if vp.target_printer_id is None else None,
    )
    if dry_run:
        return entry
    try:
        synced, done, changed = await asyncio.to_thread(
            printers.store.upsert_virtual,
            vp.id,
            serial=vp.serial,
            name=vp.name,
            model=vp.model_name or vp.model,
            mode=vp.mode,
            forwards_to=entry.forwards_to,
            view=virtual_view(vp, now),
            may_adopt=may_adopt(vp, physical_serials),
        )
    except SerialTakenError:
        entry.action, entry.reason = "conflict", RACE
        return entry
    entry.printer_id, entry.action = synced.id, done
    if done == "conflict":
        entry.reason = SERIAL_KEPT.format(vp_id=vp.id)
    if changed:
        printers.changed(synced.id)
    return entry
