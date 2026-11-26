"""Which SB spool is in each AMS tray (#6262 Task 4, spec
2026-11-14-filament-management-design.md §5.3 "current tray").

A tray is resolved from what the printer's latest report shows in it (`SeenTray`): the
spool whose tag the tray reports, by an open binding of its ``tray_uuid``, then of its
``tag_uid``; else, for an untagged tray, the spool a person placed at that tray (its
``location_id`` is the tray's place, a location with ``printer_id`` set) whose
``tray_fingerprint`` still equals the tray's. A tray refilled with another material or
colour then holds no spool, and the dialog asks about it again (plan Review Focus 3).
The fingerprint keeps the alpha byte (`SeenTray.rgba`, from the report's
``tray_color``), so a translucent tray never holds an opaque spool (spec §5.1).

The shelf is every unarchived spool that no printer's latest report resolves as held
(plan F6): a spool placed at a tray whose report no longer matches is on the shelf.

An untagged tray's placed spool must also agree with what the tray reports (#6713,
`tray_agreement`). The scheduled pull reconciles it (`reconcile_trays`): where it does not
and Bambuddy assigns the tray a linked, unarchived spool that does, that spool moves in and
the placed one moves to no place. Where neither agrees, nothing moves, and the tray view
carries a ``mismatch`` (`view`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, Protocol
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.filaments import colour_distance
from scadbuddy.bambuddy.models import PrinterStatus, SpoolAssignment
from scadbuddy.bambuddy.trays import CLOSE_COLOUR, place_label, seen_trays, tray_label
from scadbuddy.filament import models as m
from scadbuddy.filament import tags
from scadbuddy.filament.catalog import catalog_key, resolve_or_add
from scadbuddy.filament.colour import normalise_rgba
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.models import (
    FilamentInventorySpool,
    SbSpools,
    TrayFoundBy,
    TrayHolder,
    TrayPlace,
    TrayView,
)
from scadbuddy.filament.presets import PoolSpoolPresets, effective_presets
from scadbuddy.filament.spools import FilamentDefaults, FilamentSpoolStore
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.reports import PrinterReportStore, report_status
from scadbuddy.printers.status import is_fresh
from scadbuddy.printers.store import PrinterStore
from scadbuddy.spools.models import SeenTray, clean_tag_uid, clean_tray_uuid

__all__ = [
    "RECONCILE_ACTOR",
    "FilamentInventory",
    "FilamentInventorySpool",
    "PoolSbSpoolReads",
    "SbSpoolReads",
    "TrayHolder",
    "TrayMove",
    "TrayNotFilledError",
    "TrayPlace",
    "TrayTagClashError",
    "TrayView",
    "material_key",
    "material_names",
    "read_sb_spools",
    "route_trays",
    "tray_agreement",
    "tray_fingerprint",
]

logger = logging.getLogger(__name__)

Conn = Connection[DictRow]
FoundBy = TrayFoundBy
#: Each tray place's spools with a fingerprint, newest placement first.
_Placed = Mapping[tuple[UUID, int, int], list[tuple[UUID, str]]]


#: Who a tray reconcile's moves name (#6713).
RECONCILE_ACTOR: Final = "system:tray-reconcile"


def material_key(name: str | None) -> str:
    """A material name as a tray's ``tray_type`` is compared with it (#6713): its catalog
    key (`catalog.catalog_key`, R-6706-23), so ``PETG-CF`` and ``petg cf`` are one key and
    ``PLA+`` is not ``PLA``. Bambu's ``tray_type`` values are ASCII, where the Python key
    is the SQL one (R-6706-14)."""
    return catalog_key(name or "")


def material_names(conn: Conn) -> dict[UUID, frozenset[str]]:
    """Every material's stored keys, its name's and its aliases' (#6706), by id."""
    keys: dict[UUID, set[str]] = {}
    for row in conn.execute("SELECT material_id, key FROM filament_material_names"):
        keys.setdefault(row["material_id"], set()).add(row["key"])
    return {material: frozenset(found) for material, found in keys.items()}


def tray_agreement(
    spool: m.Spool, tray: SeenTray, keys: Mapping[UUID, frozenset[str]]
) -> bool | None:
    """Whether ``spool`` agrees with what ``tray`` reports (#6713): the tray's
    `material_key` is a key of the spool's effective material, its name's or an
    alias's (``keys``, `material_names`, R-6706-23), and its colour is within
    `CLOSE_COLOUR` of the tray's (CIEDE2000, the cut-off an untagged tray's spools are
    matched by, `bambuddy/trays.py` `close_spools`), alpha ignored (`colour_distance`
    drops it). ``None`` when either side has no material (or the material's name has no
    key), or the materials match and either side has no colour: unknown, which is never
    a disagreement."""
    reported = material_key(tray.material)
    material = spool.effective.material_id
    own = keys.get(material, frozenset()) if material is not None else frozenset()
    if not reported or not own:
        return None
    if reported not in own:
        return False
    distance = colour_distance(spool.effective.rgba, tray.rgba or tray.colour)
    return None if distance is None else distance <= CLOSE_COLOUR


@dataclass(frozen=True)
class TrayMove:
    """One tray `FilamentInventory.reconcile_trays` set right: the spool it moved to no
    place, and the spool it put in the tray."""

    printer_id: UUID
    ams_id: int
    tray_id: int
    removed: UUID
    placed: UUID


def _untagged(tray: SeenTray) -> bool:
    return tray.tray_uuid is None and tray.tag_uid is None


def _verdict(
    tray: SeenTray,
    holder: m.Spool | None,
    found_by: FoundBy | None,
    assigned: m.Spool | None,
    keys: Mapping[UUID, frozenset[str]],
) -> Literal["move", "mismatch"] | None:
    """What an untagged tray's placed spool (``found_by`` ``"link"``) needs (#6713):
    ``"move"`` when it disagrees with the tray and ``assigned`` (Bambuddy's assignment's
    linked, unarchived SB spool, else ``None``) agrees; ``"mismatch"`` when it disagrees
    and ``assigned`` does not agree; else ``None``. A tagged tray, and one resolved by
    its tag, need nothing."""
    if holder is None or found_by != "link" or not _untagged(tray):
        return None
    if tray_agreement(holder, tray, keys) is not False:
        return None
    if assigned is not None and assigned.id != holder.id and tray_agreement(assigned, tray, keys):
        return "move"
    return "mismatch"


def _assigned_trays(
    assignments: Sequence[SpoolAssignment], printers: Mapping[str, PrinterRow]
) -> dict[tuple[UUID, int, int], int]:
    """The Bambuddy spool each registry tray is assigned, by ``(printer_id, ams_id,
    tray_id)``: each Bambuddy spool's last row (Bambuddy keeps history rows, as `rows`
    reads them). A tray two spools' last rows name is left out: Bambuddy does not say
    which is there."""
    by_bambuddy = {
        row.bambuddy_printer_id: UUID(row.id)
        for row in printers.values()
        if row.bambuddy_printer_id is not None
    }
    trays: dict[tuple[UUID, int, int], set[int]] = {}
    for assignment in {a.spool_id: a for a in assignments}.values():
        printer_id = by_bambuddy.get(assignment.printer_id)
        if printer_id is not None:
            key = (printer_id, assignment.ams_id, assignment.tray_id)
            trays.setdefault(key, set()).add(assignment.spool_id)
    return {key: next(iter(ids)) for key, ids in trays.items() if len(ids) == 1}


def _reason(tray: SeenTray, holder: m.Spool, assigned: m.Spool) -> str:
    """A reconcile move's ``data.reason``: what the tray reports, what the placed spool
    is, and what Bambuddy assigns."""
    return (
        f"{tray_label(tray.ams_id, tray.tray_id)} reports {tray.material}"
        f" {tray.rgba or tray.colour or 'with no colour'}; #{holder.number} is"
        f" {holder.names.material or 'no material'}"
        f" {holder.effective.rgba or 'with no colour'}, and Bambuddy assigns"
        f" #{assigned.number} there"
    )


#: Where a material a tray names first is sorted in its catalog: after the seeded ones, as
#: the Bambuddy pull and the CSV import add theirs (`bambuddy_pull._ADDED_SORT`).
_ADDED_SORT: Final = 1000


class TrayNotFilledError(ValueError):
    """The printer's latest report does not show the tray filled."""

    def __init__(self) -> None:
        super().__init__("the printer's last report does not show that tray filled")


class TrayTagClashError(ValueError):
    """The tray reports a tag that is bound to another spool."""

    def __init__(self) -> None:
        super().__init__("the tray's tag is on another spool; that spool is the tray's")


class TrayColourError(ValueError):
    """Create from slot (#7150): the tray's stored colour is not ``#RRGGBBAA``."""

    def __init__(self, colour: str) -> None:
        super().__init__(f"the tray's colour {colour[:16]!r} is not #RRGGBBAA")


def tray_fingerprint(material: str | None, colour: str | None) -> str:
    """``MATERIAL|#RRGGBBAA``: what a person saw in the tray when they placed a spool
    there. The material upper-cased and stripped; the colour as `normalise_rgba` writes
    it, alpha included, or nothing after the ``|`` for no colour (plan F6). Never the
    tray's ``state``, which changes as the AMS feeds."""
    part = normalise_rgba(colour) if colour else ""
    return f"{(material or '').strip().upper()}|{part}"


class _Keys:
    """The open tag bindings and the tray placements, read once."""

    def __init__(self, conn: Conn) -> None:
        self.by_tray_uuid: dict[str, UUID] = {}
        self.by_tag_uid: dict[str, UUID] = {}
        for row in conn.execute(
            "SELECT t.tray_uuid, t.tag_uid, b.spool_id FROM filament_tags t"
            " JOIN filament_tag_bindings b ON b.tag_id = t.id AND b.unbound_at IS NULL"
            " JOIN filament_spools s ON s.id = b.spool_id AND s.archived_at IS NULL"
        ).fetchall():
            if row["tray_uuid"] is not None:
                self.by_tray_uuid[row["tray_uuid"]] = row["spool_id"]
            if row["tag_uid"] is not None:
                self.by_tag_uid[row["tag_uid"]] = row["spool_id"]
        placed: dict[tuple[UUID, int, int], list[tuple[UUID, str]]] = {}
        for row in conn.execute(
            "SELECT s.id, s.tray_fingerprint, l.printer_id, l.ams_id, l.tray_id"
            " FROM filament_spools s JOIN filament_locations l ON l.id = s.location_id"
            " WHERE l.printer_id IS NOT NULL AND l.archived_at IS NULL"
            " AND s.archived_at IS NULL AND s.tray_fingerprint IS NOT NULL"
            " ORDER BY s.updated_at DESC, s.id"
        ).fetchall():
            key = (row["printer_id"], row["ams_id"], row["tray_id"])
            placed.setdefault(key, []).append((row["id"], row["tray_fingerprint"]))
        self.placed: _Placed = placed

    def hold(self, printer_id: UUID, tray: SeenTray) -> tuple[UUID | None, FoundBy | None]:
        """The spool in ``tray`` of ``printer_id`` and how it was found."""
        if tray.tray_uuid is not None and tray.tray_uuid in self.by_tray_uuid:
            return self.by_tray_uuid[tray.tray_uuid], "tag"
        if tray.tag_uid is not None and tray.tag_uid in self.by_tag_uid:
            return self.by_tag_uid[tray.tag_uid], "tag"
        fingerprint = tray_fingerprint(tray.material, tray.rgba or tray.colour)
        for spool_id, stored in self.placed.get((printer_id, tray.ams_id, tray.tray_id), []):
            if stored == fingerprint:
                return spool_id, "link"
        return None, None


class FilamentInventory:
    """The SB tray resolution over a pool whose connections autocommit and return dict
    rows (the settings store's). Synchronous: callers run it in a thread."""

    def __init__(
        self, pool: ConnectionPool[Conn], defaults: Callable[[], FilamentDefaults] | None = None
    ) -> None:
        self._pool = pool
        self._spools = (
            FilamentSpoolStore(pool) if defaults is None else FilamentSpoolStore(pool, defaults)
        )
        self._locations = LocationStore(pool)

    @staticmethod
    def holder_on(
        conn: Conn, printer_id: UUID, seen: SeenTray
    ) -> tuple[UUID | None, FoundBy | None]:
        """The unarchived spool in ``seen`` (a filled tray of ``printer_id``) and how it was
        found, on ``conn`` (`trays` and `filament/capture.py` both call it). An open binding
        of the tray's ``tray_uuid``, then of its ``tag_uid``: ``"tag"``. Else the spool
        placed at the tray whose ``tray_fingerprint`` equals the tray's: ``"link"``, for a
        tray with no keys and for one whose keys no binding holds (#6263 preflight C5, the
        AMS rescan). Else none."""
        keyed = tags.holder_by_keys(conn, seen.tray_uuid, seen.tag_uid)
        if keyed is not None:
            return keyed, "tag"
        linked = FilamentInventory.holder_by_link(conn, printer_id, seen)
        return (None, None) if linked is None else (linked, "link")

    @staticmethod
    def holder_by_link(conn: Conn, printer_id: UUID, seen: SeenTray) -> UUID | None:
        """`holder_on`'s second look only: the spool placed at the tray whose
        ``tray_fingerprint`` equals the tray's. For a caller that looked the keys up already
        (`filament/capture.py`, through `tags.seen_on`)."""
        placed = conn.execute(
            "SELECT s.id FROM filament_spools s JOIN filament_locations l ON l.id = s.location_id"
            " WHERE l.printer_id = %s AND l.ams_id = %s AND l.tray_id = %s"
            " AND l.archived_at IS NULL AND s.archived_at IS NULL AND s.tray_fingerprint = %s"
            " ORDER BY s.updated_at DESC, s.id LIMIT 1",
            (
                printer_id,
                seen.ams_id,
                seen.tray_id,
                tray_fingerprint(seen.material, seen.rgba or seen.colour),
            ),
        ).fetchone()
        if placed is None:
            return None
        found: UUID = placed["id"]
        return found

    @staticmethod
    def place_on(
        conn: Conn,
        spool_id: UUID,
        location_id: UUID,
        seen: SeenTray,
        *,
        actor: str,
        at: datetime | None = None,
        reason: str | None = None,
    ) -> m.SpoolEvent | None:
        """Put ``spool_id`` in the tray place ``location_id`` and remember what ``seen``
        shows there (`LocationStore.move_on`, then ``tray_fingerprint``); `link`,
        `reconcile_trays` and `filament/capture.py` call it."""
        moved = LocationStore.move_on(
            conn, spool_id, location_id, actor=actor, at=at, reason=reason
        )
        conn.execute(
            "UPDATE filament_spools SET tray_fingerprint = %s WHERE id = %s",
            (tray_fingerprint(seen.material, seen.rgba or seen.colour), spool_id),
        )
        return moved

    def trays(self, printer_id: UUID, seen: Sequence[SeenTray]) -> TrayView:
        """``seen`` (``printer_id``'s reported trays) with the SB spool each holds, and
        the shelf. Every other registry printer's latest report is resolved too, so a
        spool another printer holds is not on the shelf."""
        with self._pool.connection() as conn:
            keys = _Keys(conn)
            own = [(tray, *self.holder_on(conn, printer_id, tray)) for tray in seen]
        held = {
            spool_id
            for other, trays in self._others(printer_id)
            for spool_id, _ in (keys.hold(other, tray) for tray in trays)
            if spool_id is not None
        }
        held |= {spool_id for _, spool_id, _ in own if spool_id is not None}
        spools = {spool.id: spool for spool in self._spools.list()}
        return TrayView(
            trays=[
                TrayHolder(
                    ams_id=tray.ams_id,
                    tray_id=tray.tray_id,
                    spool=None if spool_id is None else spools.get(spool_id),
                    found_by=found_by if spool_id in spools else None,
                )
                for tray, spool_id, found_by in own
            ],
            shelf=[spool for spool in spools.values() if spool.id not in held],
        )

    def rows(
        self,
        *,
        include_archived: bool = False,
        bb_assignments: Sequence[SpoolAssignment] = (),
    ) -> list[FilamentInventorySpool]:
        """Every SB spool (by number; the archived ones too with ``include_archived``),
        the tray it is in, and its Bambuddy spool (#6262 Task 7). ``loaded`` is the tray
        a registry printer's latest report holds it in (`_Keys.hold`, as :meth:`trays`
        reads it), else the tray Bambuddy assigns its linked Bambuddy spool to
        (``bb_assignments``, ``GET /inventory/assignments``; the last row for a spool
        wins, as Bambuddy keeps history rows), else ``None`` (plan Decision 3). A registry
        printer with no stored report has no trays to read, so a spool placed in one of
        its trays (`_Keys.placed`) is there, as last linked (#6262 Task 7 review I4).
        Each row carries the spool's effective presets."""
        spools = self._spools.list(include_archived=include_archived)
        with self._pool.connection() as conn:
            keys = _Keys(conn)
            links = {
                row["spool_id"]: row["bb_spool_id"]
                for row in conn.execute("SELECT spool_id, bb_spool_id FROM filament_bb_links")
            }
            presets = {spool.id: effective_presets(conn, spool.id) for spool in spools}
        printers = {row.id: row for row in PrinterStore(self._pool, None).list()}
        places: dict[UUID, TrayPlace] = {}
        reported = self._reported(printers)
        for printer_id, trays in reported:
            row = printers[str(printer_id)]
            for tray in trays:
                spool_id, _ = keys.hold(printer_id, tray)
                if spool_id is not None and spool_id not in places:
                    places[spool_id] = TrayPlace(
                        printer_id=row.id,
                        bambuddy_printer_id=row.bambuddy_printer_id,
                        printer_name=row.name,
                        ams_id=tray.ams_id,
                        tray_id=tray.tray_id,
                    )
        # A report, read above, wins over a placement on a printer that has none.
        unreported = {UUID(printer_id) for printer_id in printers} - {
            printer_id for printer_id, _ in reported
        }
        for (printer_id, ams_id, tray_id), held in keys.placed.items():
            if printer_id not in unreported:
                continue
            row = printers[str(printer_id)]
            for spool_id, _ in held:
                places.setdefault(
                    spool_id,
                    TrayPlace(
                        printer_id=row.id,
                        bambuddy_printer_id=row.bambuddy_printer_id,
                        printer_name=row.name,
                        ams_id=ams_id,
                        tray_id=tray_id,
                    ),
                )
        by_bambuddy = {
            row.bambuddy_printer_id: row
            for row in printers.values()
            if row.bambuddy_printer_id is not None
        }
        assigned = {assignment.spool_id: assignment for assignment in bb_assignments}
        found: list[FilamentInventorySpool] = []
        for spool in spools:
            bb_id = links.get(spool.id)
            loaded = places.get(spool.id)
            assignment = assigned.get(bb_id) if bb_id is not None else None
            if loaded is None and assignment is not None:
                registry = by_bambuddy.get(assignment.printer_id)
                loaded = TrayPlace(
                    printer_id=registry.id if registry is not None else None,
                    bambuddy_printer_id=assignment.printer_id,
                    printer_name=assignment.printer_name
                    or (registry.name if registry is not None else None),
                    ams_id=assignment.ams_id,
                    tray_id=assignment.tray_id,
                )
            found.append(
                FilamentInventorySpool(
                    spool=spool, loaded=loaded, bb_spool_id=bb_id, presets=presets[spool.id]
                )
            )
        return found

    def reconcile_trays(self, bb_assignments: Sequence[SpoolAssignment]) -> list[TrayMove]:
        """Set right each untagged tray, of a registry printer's fresh report
        (`is_fresh`), whose placed spool disagrees with what the tray reports, where
        Bambuddy's assignment (``bb_assignments``, `_assigned_trays`) names a linked,
        unarchived SB spool that agrees (`_verdict`, #6713): the placed spool moves to no
        place and the assigned one into the tray, each with a ``moved`` event by
        `RECONCILE_ACTOR` whose ``data.reason`` says why. An assigned spool that another
        reported tray holds stays where it is. Nothing else moves. The moves made."""
        printers = {row.id: row for row in PrinterStore(self._pool, None).list()}
        assigned = _assigned_trays(bb_assignments, printers)
        if not assigned:
            return []
        spools = {spool.id: spool for spool in self._spools.list()}
        with self._pool.connection() as conn:
            keys = _Keys(conn)
            materials = material_names(conn)
            links: dict[int, UUID] = {
                row["bb_spool_id"]: row["spool_id"]
                for row in conn.execute("SELECT spool_id, bb_spool_id FROM filament_bb_links")
            }
        reported = self._reported(printers, fresh_at=datetime.now(UTC))
        resolved = [
            (printer_id, tray, *keys.hold(printer_id, tray))
            for printer_id, trays in reported
            for tray in trays
        ]
        held = {spool_id for _, _, spool_id, _ in resolved if spool_id is not None}
        moved: list[TrayMove] = []
        for printer_id, tray, holder_id, found_by in resolved:
            bb_id = assigned.get((printer_id, tray.ams_id, tray.tray_id))
            sb_id = None if bb_id is None else links.get(bb_id)
            candidate = None if sb_id is None or sb_id in held else spools.get(sb_id)
            holder = None if holder_id is None else spools.get(holder_id)
            if _verdict(tray, holder, found_by, candidate, materials) != "move":
                continue
            assert holder is not None and candidate is not None
            row = printers[str(printer_id)]
            if self._swap(row.name, printer_id, tray, holder, candidate):
                moved.append(
                    TrayMove(printer_id, tray.ams_id, tray.tray_id, holder.id, candidate.id)
                )
                held.add(candidate.id)
        return moved

    def tray_presets(self) -> dict[UUID, str]:
        """Each SB spool a registry printer's latest report holds (`_Keys.hold`), by the
        preset id (``tray_info_idx``) that tray reports: the last step of a price-book
        record's density (`orders/density.py`, #6262 Task 9 review)."""
        with self._pool.connection() as conn:
            keys = _Keys(conn)
        printers = {row.id: row for row in PrinterStore(self._pool, None).list()}
        found: dict[UUID, str] = {}
        for printer_id, seen in self._reported(printers):
            for tray in seen:
                spool_id, _ = keys.hold(printer_id, tray)
                preset = (tray.tray_info_idx or "").strip()
                if spool_id is not None and preset and spool_id not in found:
                    found[spool_id] = preset
        return found

    def view(
        self,
        *,
        include_archived: bool = False,
        bb_assignments: Sequence[SpoolAssignment] = (),
    ) -> m.FilamentInventoryView:
        """Every SB spool with its tray (:meth:`rows`, placed by ``bb_assignments`` where no
        report holds it), and each registry printer's reported trays with the SB spool
        each holds, as its latest stored report shows them (`_Keys.hold`), by printer name
        (#6262 Task 9). An untagged tray whose placed spool disagrees with it, where
        ``bb_assignments`` names no linked, unarchived spool that agrees, carries a
        ``mismatch`` (#6713, `_verdict`)."""
        rows = self.rows(include_archived=include_archived, bb_assignments=bb_assignments)
        numbers = {row.spool.id: row.spool.number for row in rows}
        own = {row.spool.id: row.spool for row in rows}
        by_bb = {
            row.bb_spool_id: row.spool
            for row in rows
            if row.bb_spool_id is not None and row.spool.archived_at is None
        }
        with self._pool.connection() as conn:
            keys = _Keys(conn)
            materials = material_names(conn)
        printers = {row.id: row for row in PrinterStore(self._pool, None).list()}
        assigned = _assigned_trays(bb_assignments, printers)
        reported = self._reported(printers)
        # An assigned spool another reported tray holds is not the assigned tray's
        # (`reconcile_trays` leaves it there too).
        held = {
            spool_id
            for printer_id, seen in reported
            for spool_id, _ in (keys.hold(printer_id, tray) for tray in seen)
            if spool_id is not None
        }
        found: list[m.PrinterTrays] = []
        for printer_id, seen in reported:
            row = printers[str(printer_id)]
            trays: list[m.InventoryTray] = []
            for tray in seen:
                spool_id, found_by = keys.hold(printer_id, tray)
                if spool_id is not None and spool_id not in numbers:
                    # Archived since the read above: `_Keys` reads unarchived ones only.
                    spool_id, found_by = None, None
                bb_id = assigned.get((printer_id, tray.ams_id, tray.tray_id))
                candidate = None if bb_id is None else by_bb.get(bb_id)
                verdict = _verdict(
                    tray,
                    None if spool_id is None else own[spool_id],
                    found_by,
                    None if candidate is None or candidate.id in held else candidate,
                    materials,
                )
                trays.append(
                    m.InventoryTray(
                        ams_id=tray.ams_id,
                        tray_id=tray.tray_id,
                        label=tray_label(tray.ams_id, tray.tray_id),
                        material=tray.material or None,
                        rgba=tray.rgba,
                        spool_id=spool_id,
                        spool_number=None if spool_id is None else numbers[spool_id],
                        found_by=found_by,
                        mismatch=m.TrayMismatch(
                            reported_type=tray.material, reported_rgba=tray.rgba
                        )
                        if verdict == "mismatch" and tray.material
                        else None,
                    )
                )
            found.append(m.PrinterTrays(printer_id=row.id, printer_name=row.name, trays=trays))
        found.sort(key=lambda p: (p.printer_name.casefold(), p.printer_id))
        return m.FilamentInventoryView(spools=rows, trays=found)

    def summary(
        self,
        spool_id: UUID,
        grams: float | None = None,
        bb_assignments: Sequence[SpoolAssignment] = (),
    ) -> m.FilamentSpoolSummary | None:
        """One SB spool's stock, tray and price, and its filament's stock: the
        unarchived spools of its effective material, subtype, brand and ``#RRGGBBAA``
        (its colour's name for one with none), placed as :meth:`rows` places them. An
        archived spool is still summarised. ``None`` for no such spool."""
        rows = self.rows(include_archived=True, bb_assignments=bb_assignments)
        row = next((r for r in rows if r.spool.id == spool_id), None)
        if row is None:
            return None
        key = _filament_key(row.spool)
        same = [r for r in rows if r.spool.archived_at is None and _filament_key(r.spool) == key]
        same.sort(key=lambda r: r.spool.number)
        cost = row.spool.effective.cost_per_kg
        per_g = None if cost is None else round(float(cost) / 1000, 6)
        left = row.spool.grams_left
        effective, names = row.spool.effective, row.spool.names
        return m.FilamentSpoolSummary(
            spool=row,
            cost_per_kg=cost,
            price_per_g=per_g,
            value_left=None if per_g is None or left is None else round(left * per_g, 4),
            grams=grams,
            grams_cost=None if per_g is None or grams is None else round(grams * per_g, 4),
            filament=m.SameFilamentStock(
                material=names.material,
                subtype=names.subtype,
                brand=names.brand,
                colour_name=effective.colour_name,
                rgba=effective.rgba,
                spool_count=len(same),
                spool_numbers=[r.spool.number for r in same],
                grams_left=round(sum(r.spool.grams_left or 0.0 for r in same), 2),
                loaded=[
                    m.SameFilamentPlace(
                        spool_id=r.spool.id, spool_number=r.spool.number, loaded=r.loaded
                    )
                    for r in same
                    if r.loaded is not None
                ],
            ),
        )

    def by_number(self, number: int) -> tuple[UUID, int | None] | None:
        """Unarchived SB spool #``number``'s id and the Bambuddy spool it is linked to
        (``None`` for none), or ``None`` for no such spool."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT s.id, l.bb_spool_id FROM filament_spools s"
                " LEFT JOIN filament_bb_links l ON l.spool_id = s.id"
                " WHERE s.number = %s AND s.archived_at IS NULL",
                (number,),
            ).fetchone()
        return None if row is None else (row["id"], row["bb_spool_id"])

    def bb_to_sb(self, ids: Iterable[int]) -> dict[int, int]:
        """The SB spool number each Bambuddy spool id among ``ids`` is linked to
        (`filament_bb_links`); an unlinked id is left out (#6262 F14)."""
        wanted = sorted(set(ids))
        if not wanted:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT l.bb_spool_id, s.number FROM filament_bb_links l"
                " JOIN filament_spools s ON s.id = l.spool_id WHERE l.bb_spool_id = ANY(%s)",
                (wanted,),
            ).fetchall()
        return {row["bb_spool_id"]: row["number"] for row in rows}

    def link(
        self,
        printer_id: UUID,
        ams_id: int,
        tray_id: int,
        spool_id: UUID,
        seen: SeenTray,
        *,
        actor: str,
    ) -> m.Spool:
        """Place ``spool_id`` in the tray, remembering what ``seen`` shows there. The
        tray's previous holder moves to no place first, then the spool moves to the
        tray's place (`LocationStore.ensure_ams`), each with one ``moved`` event.

        A tagged tray's tag is bound to the spool in the same transaction
        (`FilamentSpoolStore.bind_on`), so the tray then resolves by its tag: a tag on no
        spool, one not seen before, or one whose open binding is on an archived spool
        (taken for re-use, as the BB pull's `_bind` takes it; that binding closes with
        ``reason='rebound'``). A spool that holds another tag is rebound to the tray's:
        the tray's reading is the physical truth, and a spool has one tag. The old
        binding closes with ``reason='rebound'`` and a ``tag_unbound`` event.

        `TrayNotFilledError` when ``seen`` is not that tray filled; `TrayTagClashError`
        when either of the tray's tags is bound to another unarchived spool;
        `TagKeysClashError` when its two keys name two different tags; `LookupError` for
        no such printer, or no such unarchived spool.

        Locks, in this order: the tray's advisory lock (`_tray_lock`), so two links or
        unlinks of one tray take turns; the tray's tag, made first when it is new
        (`FilamentSpoolStore.tag_on`; #6261: every writer of bindings takes the tag
        first); the spools by id; then the place, ``FOR SHARE`` in
        `LocationStore.move_on`, after its spool, as `LocationStore.move` does. The
        place's row is never locked ``FOR UPDATE`` here, which would invert that order."""
        place = self.tray_place(printer_id, ams_id, tray_id, seen)
        defaults = self._spools.defaults()
        with self._pool.connection() as conn, conn.transaction():
            self.link_on(conn, place, printer_id, ams_id, tray_id, spool_id, seen, actor=actor)
            linked = FilamentSpoolStore.read(conn, spool_id, defaults=defaults)
        assert linked is not None
        return linked

    def tray_place(self, printer_id: UUID, ams_id: int, tray_id: int, seen: SeenTray) -> UUID:
        """The tray's place, made when it has none (`LocationStore.ensure_ams`), for `link`
        and `create_in_tray_on`. `TrayNotFilledError` when ``seen`` is not that tray
        filled; `LookupError` for no such printer."""
        material = (seen.material or "").strip()
        if (seen.ams_id, seen.tray_id) != (ams_id, tray_id) or not material:
            raise TrayNotFilledError()
        with self._pool.connection() as conn:
            printer = conn.execute(
                "SELECT name FROM printers WHERE id = %s", (printer_id,)
            ).fetchone()
        if printer is None:
            raise LookupError(f"no printer {printer_id}")
        return self._locations.ensure_ams(
            printer_id, ams_id, tray_id, label=place_label(printer["name"], ams_id, tray_id)
        ).id

    def create_in_tray_on(
        self,
        conn: Conn,
        place_id: UUID,
        printer_id: UUID,
        ams_id: int,
        tray_id: int,
        seen: SeenTray,
        *,
        actor: str,
    ) -> m.Spool:
        """A new spool from what the tray reports, placed in it, in the caller's
        transaction (#7150, R-7150-4, R-7150-5): the tray's material (by key against every
        material name and alias, added when no row has it), its ``#RRGGBBAA``, and its RFID
        keys as the spool's tag; then `link_on`'s steps. No type, label weight or grams.
        `TrayTagClashError` when the tray's tag is on an unarchived spool (that spool is
        the tray's), and `TrayColourError` when its colour is not ``#RRGGBBAA``, both
        checked before anything is written."""
        if seen.rgba is not None:
            try:
                normalise_rgba(seen.rgba)
            except ValueError:
                raise TrayColourError(seen.rgba) from None
        _tray_lock(conn, printer_id, ams_id, tray_id)
        self._no_tag_clash(conn, seen, None)
        material = resolve_or_add(
            conn, "materials", (seen.material or "").strip(), sort_order=_ADDED_SORT
        )
        body = m.SpoolCreate(material_id=material, rgba=seen.rgba)
        spool_id = self._spools.create_on(conn, body, actor=actor)
        self.link_on(conn, place_id, printer_id, ams_id, tray_id, spool_id, seen, actor=actor)
        made = FilamentSpoolStore.read(conn, spool_id, defaults=self._spools.defaults())
        assert made is not None
        return made

    def link_on(
        self,
        conn: Conn,
        place_id: UUID,
        printer_id: UUID,
        ams_id: int,
        tray_id: int,
        spool_id: UUID,
        seen: SeenTray,
        *,
        actor: str,
    ) -> None:
        """`link` on ``conn``, in the caller's transaction, at the tray's place
        ``place_id`` (`tray_place`)."""
        _tray_lock(conn, printer_id, ams_id, tray_id)
        self._no_tag_clash(conn, seen, spool_id)
        uid, tray = clean_tag_uid(seen.tag_uid), clean_tray_uuid(seen.tray_uuid)
        kind: m.TagKind = "bambu_mifare" if tray is not None else "other"
        if uid is not None or tray is not None:
            FilamentSpoolStore.tag_on(conn, uid, tray, kind)
        rows = conn.execute(
            "SELECT id, location_id, archived_at FROM filament_spools"
            " WHERE id = %s OR location_id = %s ORDER BY id FOR UPDATE",
            (spool_id, place_id),
        ).fetchall()
        spool = next((row for row in rows if row["id"] == spool_id), None)
        if spool is None or spool["archived_at"] is not None:
            raise LookupError(f"no spool {spool_id}")
        for row in rows:
            if row["id"] != spool_id:
                LocationStore.move_on(conn, row["id"], None, actor=actor)
        self.place_on(conn, spool_id, place_id, seen, actor=actor)
        if uid is not None or tray is not None:
            FilamentSpoolStore.bind_on(conn, spool_id, uid, tray, kind, None, actor)

    def unlink(self, printer_id: UUID, ams_id: int, tray_id: int, *, actor: str) -> None:
        """Move every spool in the tray's place to no place, each with a ``moved``
        event. Nothing is written for a tray with no place or no spool. Takes the tray's
        advisory lock first, then the spools, as `link` does."""
        with self._pool.connection() as conn, conn.transaction():
            _tray_lock(conn, printer_id, ams_id, tray_id)
            rows = conn.execute(
                "SELECT s.id FROM filament_spools s JOIN filament_locations l"
                " ON l.id = s.location_id WHERE l.printer_id = %s AND l.ams_id = %s"
                " AND l.tray_id = %s AND l.archived_at IS NULL ORDER BY s.id FOR UPDATE OF s",
                (printer_id, ams_id, tray_id),
            ).fetchall()
            for row in rows:
                LocationStore.move_on(conn, row["id"], None, actor=actor)

    # -- internals --------------------------------------------------------------------

    def _swap(
        self, name: str, printer_id: UUID, tray: SeenTray, holder: m.Spool, assigned: m.Spool
    ) -> bool:
        """`reconcile_trays`'s two moves for one tray, in one transaction: ``holder`` to no
        place, ``assigned`` into the tray. Locks as `link` does: the tray's advisory lock,
        then both spools by id. False, with nothing written, when the tray no longer holds
        ``holder`` by its placement or either spool is archived since the read: the next
        pass looks again."""
        reason = _reason(tray, holder, assigned)
        place = self._locations.ensure_ams(
            printer_id,
            tray.ams_id,
            tray.tray_id,
            label=place_label(name, tray.ams_id, tray.tray_id),
        )
        with self._pool.connection() as conn, conn.transaction():
            _tray_lock(conn, printer_id, tray.ams_id, tray.tray_id)
            locked = conn.execute(
                "SELECT id FROM filament_spools WHERE id = ANY(%s) AND archived_at IS NULL"
                " ORDER BY id FOR UPDATE",
                ([holder.id, assigned.id],),
            ).fetchall()
            if len(locked) != 2 or self.holder_on(conn, printer_id, tray) != (holder.id, "link"):
                return False
            LocationStore.move_on(conn, holder.id, None, actor=RECONCILE_ACTOR, reason=reason)
            self.place_on(conn, assigned.id, place.id, tray, actor=RECONCILE_ACTOR, reason=reason)
        logger.info(
            "moved a tray's placed spool out for the one Bambuddy assigns",
            extra={
                "printer_id": str(printer_id),
                "ams_id": tray.ams_id,
                "tray_id": tray.tray_id,
                "removed": holder.number,
                "placed": assigned.number,
            },
        )
        return True

    def _others(self, printer_id: UUID) -> list[tuple[UUID, list[SeenTray]]]:
        """Every other registry printer's trays, as its latest stored report shows them."""
        rows = {row.id: row for row in PrinterStore(self._pool, None).list()}
        return [(other, trays) for other, trays in self._reported(rows) if other != printer_id]

    def _reported(
        self, rows: Mapping[str, PrinterRow], *, fresh_at: datetime | None = None
    ) -> list[tuple[UUID, list[SeenTray]]]:
        """The trays of each printer among ``rows`` that has a stored report, as its latest
        report shows them; with ``fresh_at``, only a report fresh then (`is_fresh`)."""
        now = datetime.now(UTC)
        found: list[tuple[UUID, list[SeenTray]]] = []
        for printer_id, report in PrinterReportStore(self._pool).all().items():
            row = rows.get(printer_id)
            if fresh_at is not None and not is_fresh(report[1], fresh_at):
                continue
            if row is not None:
                found.append((UUID(printer_id), seen_trays(report_status(row, report, now=now))))
        return found

    @staticmethod
    def _no_tag_clash(conn: Conn, seen: SeenTray, spool_id: UUID | None) -> None:
        """`TrayTagClashError` when a tag the tray reports (its ``tray_uuid``'s or its
        ``tag_uid``'s, both checked, as `_Keys.hold` reads both) is bound to an unarchived
        spool other than ``spool_id``. An archived holder does not count: `_Keys` ignores
        it, and the link takes its tag. Each tag found is locked."""
        for column, key in (("tray_uuid", seen.tray_uuid), ("tag_uid", seen.tag_uid)):
            if key is None:
                continue
            tag = conn.execute(
                f"SELECT id FROM filament_tags WHERE {column} = %s FOR UPDATE", (key,)
            ).fetchone()
            if tag is None:
                continue
            holder = conn.execute(
                "SELECT b.spool_id FROM filament_tag_bindings b JOIN filament_spools s"
                " ON s.id = b.spool_id WHERE b.tag_id = %s AND b.unbound_at IS NULL"
                " AND s.archived_at IS NULL",
                (tag["id"],),
            ).fetchone()
            if holder is not None and holder["spool_id"] != spool_id:
                raise TrayTagClashError()


def _filament_key(spool: m.Spool) -> tuple[str, str, str, str]:
    """Which spools are one filament: the same effective material, subtype and brand, and
    the same ``#RRGGBBAA``, else the same colour name for spools with none (#2918: a
    spool known by name is never assumed to be one known by colour)."""
    names = spool.names
    colour = spool.effective.rgba or "name:" + (spool.effective.colour_name or "").upper()
    return (
        (names.material or "").strip().upper(),
        (names.subtype or "").strip().upper(),
        (names.brand or "").strip().upper(),
        colour,
    )


def _tray_lock(conn: Conn, printer_id: UUID, ams_id: int, tray_id: int) -> None:
    """The transaction's advisory lock on one printer's tray, taken before any row lock."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"filament_tray:{printer_id}:{ams_id}:{tray_id}",),
    )


def try_tray_lock(conn: Conn, printer_id: UUID, ams_id: int, tray_id: int) -> bool:
    """`_tray_lock`'s lock, taken only when it is free. False when a link or an unlink
    holds it (#7143): the caller leaves the tray to the next report."""
    row = conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS got",
        (f"filament_tray:{printer_id}:{ams_id}:{tray_id}",),
    ).fetchone()
    assert row is not None
    return bool(row["got"])


async def route_trays(
    inventory: FilamentInventory | None, row: PrinterRow | None, status: PrinterStatus | None
) -> TrayView | None:
    """The SB spool in each tray ``status`` (registry printer ``row``'s, as its LAN report
    reads) shows, and the shelf: a Postgres read, in a thread. ``None`` without all three,
    and the dialog then shows the trays alone."""
    if inventory is None or row is None or status is None:
        return None
    return await asyncio.to_thread(inventory.trays, UUID(row.id), seen_trays(status))


class SbSpoolReads(Protocol):
    """What a plan reads of SB spools (#6262 Task 6): the SB spool in each tray a printer
    reports and spools' presets by number; on the Bambuddy path (Task 7) every unarchived
    SB spool with its tray (`FilamentInventory.rows`) and the SB number of each Bambuddy
    spool id an older plan names (`FilamentInventory.bb_to_sb`). The API's from Postgres
    (:class:`PoolSbSpoolReads`); the print worker's over its CRUD routes (`crud/print.py`
    `RemoteFilamentTrays`, #5295), so both read them alike."""

    async def trays(self, printer_id: UUID, seen: Sequence[SeenTray]) -> TrayView: ...

    async def presets(self, numbers: Sequence[int]) -> dict[int, list[m.EffectivePreset]]: ...

    async def rows(
        self, bb_assignments: Sequence[SpoolAssignment]
    ) -> list[FilamentInventorySpool]: ...

    async def bb_to_sb(self, ids: Sequence[int]) -> dict[int, int]: ...


class PoolSbSpoolReads:
    """:class:`SbSpoolReads` over a pool whose connections autocommit and return dict
    rows (the settings store's): each read in a thread."""

    def __init__(self, inventory: FilamentInventory, presets: PoolSpoolPresets) -> None:
        self._inventory = inventory
        self._presets = presets

    @classmethod
    def over(cls, pool: ConnectionPool[Conn]) -> PoolSbSpoolReads:
        return cls(FilamentInventory(pool), PoolSpoolPresets(pool))

    async def trays(self, printer_id: UUID, seen: Sequence[SeenTray]) -> TrayView:
        return await asyncio.to_thread(self._inventory.trays, printer_id, seen)

    async def presets(self, numbers: Sequence[int]) -> dict[int, list[m.EffectivePreset]]:
        return await self._presets.presets(numbers)

    async def rows(self, bb_assignments: Sequence[SpoolAssignment]) -> list[FilamentInventorySpool]:
        return await asyncio.to_thread(self._inventory.rows, bb_assignments=bb_assignments)

    async def bb_to_sb(self, ids: Sequence[int]) -> dict[int, int]:
        return await asyncio.to_thread(self._inventory.bb_to_sb, ids)


async def read_sb_spools(
    reads: SbSpoolReads | None, row: PrinterRow | None, status: PrinterStatus | None
) -> SbSpools | None:
    """What a native plan reads of SB spools (#6262): the SB spool in each tray
    ``status`` shows and the held spools' own presets for the H2C
    (`resolver.PRINTER_MODEL`, all the plan reads). No shelf: a plan's choices are the
    trays. ``status`` must be read from the printer's stored report: a tray's RFID keys,
    which find its spool, never cross a workflow's history (`AmsTray` drops
    ``tray_uuid``). ``None`` without all three, and ``None`` when a read fails, which is
    logged by its type: the plan is then made on the trays alone, never failed."""
    from scadbuddy.bambuddy.resolver import PRINTER_MODEL

    if reads is None or row is None or status is None:
        return None
    try:
        held = await reads.trays(UUID(row.id), seen_trays(status))
        numbers = sorted({tray.spool.number for tray in held.trays if tray.spool is not None})
        presets = await reads.presets(numbers) if numbers else {}
    except Exception as error:
        # Its type only: on the print worker it is a CRUD call's error, whose text is
        # never logged (spec 2026-10-01 §7).
        logger.warning(
            "a native plan could not read the SB spools; planning on the trays alone",
            extra={"printer": row.id, "error": type(error).__name__},
        )
        return None
    return SbSpools.of(held, presets, PRINTER_MODEL)
