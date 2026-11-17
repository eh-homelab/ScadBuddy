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
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.bambuddy.models import PrinterStatus, SpoolAssignment
from scadbuddy.bambuddy.trays import seen_trays, tray_label
from scadbuddy.filament import models as m
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
from scadbuddy.printers.store import PrinterStore
from scadbuddy.spools.models import SeenTray, clean_tag_uid, clean_tray_uuid

__all__ = [
    "FilamentInventory",
    "FilamentInventorySpool",
    "PoolSbSpoolReads",
    "SbSpoolReads",
    "TrayHolder",
    "TrayNotFilledError",
    "TrayPlace",
    "TrayTagClashError",
    "TrayView",
    "read_sb_spools",
    "route_trays",
    "tray_fingerprint",
]

logger = logging.getLogger(__name__)

Conn = Connection[DictRow]
FoundBy = TrayFoundBy
#: Each tray place's spools with a fingerprint, newest placement first.
_Placed = Mapping[tuple[UUID, int, int], list[tuple[UUID, str]]]


class TrayNotFilledError(ValueError):
    """The printer's latest report does not show the tray filled."""

    def __init__(self) -> None:
        super().__init__("the printer's last report does not show that tray filled")


class TrayTagClashError(ValueError):
    """The tray reports a tag that is bound to another spool."""

    def __init__(self) -> None:
        super().__init__("the tray's tag is on another spool; that spool is the tray's")


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
        if tray.tray_uuid is not None or tray.tag_uid is not None:
            return None, None
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

    def trays(self, printer_id: UUID, seen: Sequence[SeenTray]) -> TrayView:
        """``seen`` (``printer_id``'s reported trays) with the SB spool each holds, and
        the shelf. Every other registry printer's latest report is resolved too, so a
        spool another printer holds is not on the shelf."""
        with self._pool.connection() as conn:
            keys = _Keys(conn)
        held = {
            spool_id
            for other, trays in self._others(printer_id)
            for spool_id, _ in (keys.hold(other, tray) for tray in trays)
            if spool_id is not None
        }
        own = [(tray, *keys.hold(printer_id, tray)) for tray in seen]
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
        (#6262 Task 9)."""
        rows = self.rows(include_archived=include_archived, bb_assignments=bb_assignments)
        numbers = {row.spool.id: row.spool.number for row in rows}
        with self._pool.connection() as conn:
            keys = _Keys(conn)
        printers = {row.id: row for row in PrinterStore(self._pool, None).list()}
        found: list[m.PrinterTrays] = []
        for printer_id, seen in self._reported(printers):
            row = printers[str(printer_id)]
            trays: list[m.InventoryTray] = []
            for tray in seen:
                spool_id, found_by = keys.hold(printer_id, tray)
                if spool_id is not None and spool_id not in numbers:
                    # Archived since the read above: `_Keys` reads unarchived ones only.
                    spool_id, found_by = None, None
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
        material = (seen.material or "").strip()
        if (seen.ams_id, seen.tray_id) != (ams_id, tray_id) or not material:
            raise TrayNotFilledError()
        defaults = self._spools.defaults()
        with self._pool.connection() as conn:
            printer = conn.execute(
                "SELECT name FROM printers WHERE id = %s", (printer_id,)
            ).fetchone()
        if printer is None:
            raise LookupError(f"no printer {printer_id}")
        place = self._locations.ensure_ams(
            printer_id, ams_id, tray_id, label=f"{printer['name']} {tray_label(ams_id, tray_id)}"
        )
        with self._pool.connection() as conn, conn.transaction():
            _tray_lock(conn, printer_id, ams_id, tray_id)
            self._no_tag_clash(conn, seen, spool_id)
            uid, tray = clean_tag_uid(seen.tag_uid), clean_tray_uuid(seen.tray_uuid)
            kind: m.TagKind = "bambu_mifare" if tray is not None else "other"
            if uid is not None or tray is not None:
                FilamentSpoolStore.tag_on(conn, uid, tray, kind)
            rows = conn.execute(
                "SELECT id, location_id, archived_at FROM filament_spools"
                " WHERE id = %s OR location_id = %s ORDER BY id FOR UPDATE",
                (spool_id, place.id),
            ).fetchall()
            spool = next((row for row in rows if row["id"] == spool_id), None)
            if spool is None or spool["archived_at"] is not None:
                raise LookupError(f"no spool {spool_id}")
            for row in rows:
                if row["id"] != spool_id:
                    LocationStore.move_on(conn, row["id"], None, actor=actor)
            LocationStore.move_on(conn, spool_id, place.id, actor=actor)
            if uid is not None or tray is not None:
                FilamentSpoolStore.bind_on(conn, spool_id, uid, tray, kind, False, actor)
            conn.execute(
                "UPDATE filament_spools SET tray_fingerprint = %s WHERE id = %s",
                (tray_fingerprint(material, seen.rgba or seen.colour), spool_id),
            )
            linked = FilamentSpoolStore.read(conn, spool_id, defaults=defaults)
        assert linked is not None
        return linked

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

    def _others(self, printer_id: UUID) -> list[tuple[UUID, list[SeenTray]]]:
        """Every other registry printer's trays, as its latest stored report shows them."""
        rows = {row.id: row for row in PrinterStore(self._pool, None).list()}
        return [(other, trays) for other, trays in self._reported(rows) if other != printer_id]

    def _reported(self, rows: Mapping[str, PrinterRow]) -> list[tuple[UUID, list[SeenTray]]]:
        """The trays of each printer among ``rows`` that has a stored report, as its latest
        report shows them."""
        now = datetime.now(UTC)
        found: list[tuple[UUID, list[SeenTray]]] = []
        for printer_id, report in PrinterReportStore(self._pool).all().items():
            row = rows.get(printer_id)
            if row is not None:
                found.append((UUID(printer_id), seen_trays(report_status(row, report, now=now))))
        return found

    @staticmethod
    def _no_tag_clash(conn: Conn, seen: SeenTray, spool_id: UUID) -> None:
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
