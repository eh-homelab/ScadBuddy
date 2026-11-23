"""Location labels (#6955), and the labels a PDF draws (#7157)."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow

from scadbuddy.bambuddy.trays import EXTERNAL, place_label
from scadbuddy.codes.parse import format_code
from scadbuddy.filament import models as m
from scadbuddy.filament import prints
from scadbuddy.filament.catalog import CatalogRefError
from scadbuddy.filament.label_pdf import Label, PlaceLabel, SpoolLabel
from scadbuddy.filament.locations import LocationStore, NoAmsKindError
from scadbuddy.filament.spools import FilamentSpoolStore
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.status import status_from_report
from scadbuddy.printers.store import PrinterStore

logger = logging.getLogger(__name__)


def label_base_url(read: Mapping[str, Any]) -> str | None:
    """The https origin a location label's QR opens: ``filament_label_base_url`` (already
    validated), else ``public_url`` reduced to its origin (``https`` with a host; any path
    dropped), else ``None``. Empty strings count as unset. ``read`` is what
    ``settings_store.read`` gives for both names."""
    return read.get("filament_label_base_url") or _https_origin(read.get("public_url"))


def _https_origin(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or not host:
        return None
    shown = f"[{host}]" if ":" in host else host
    return f"https://{shown}" + (f":{port}" if port else "")


def printer_labels(
    store: LocationStore, printer_ids: Sequence[UUID] | None
) -> tuple[list[m.PrinterLabel], list[str]]:
    """The labels of each printer (by name; every printer when ``printer_ids`` is
    ``None``): its own place (depth 0), each AMS unit by ``ams_id`` (1) with its slots by
    ``tray_id`` (2), the external holder (1) last, and the names of the printers with no
    report that lists an AMS unit or an external holder. A printer's place is made if it
    has none (`LocationStore.ensure_printer_on`), and so is the place of each AMS unit,
    slot and external holder its latest report lists (`_ensure_reported`), so a slot that
    never held a spool gets a label too; a printer with no such report gets labels only for
    the places it has. A place a person made beside the printer is left out. Each printer's
    places are made under its place lock (`LocationStore.lock_printer_on`, taken first by
    `ensure_printer_on`), as capture makes them, so the two never deadlock (#7081).
    `CatalogRefError` for an unknown printer, `NoPrinterKindError` for no printer kind."""
    labels: list[m.PrinterLabel] = []
    unreported: list[str] = []
    with store.pool.connection() as conn:
        if printer_ids is None:
            printers = conn.execute("SELECT id FROM printers ORDER BY upper(name), id").fetchall()
            ids = [row["id"] for row in printers]
        else:
            found = {
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM printers WHERE id = ANY(%s)", (list(printer_ids),)
                ).fetchall()
            }
            if not found.issuperset(printer_ids):
                raise CatalogRefError()
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM printers WHERE id = ANY(%s) ORDER BY upper(name), id",
                    (list(printer_ids),),
                ).fetchall()
            ]
        for printer_id in ids:
            with conn.transaction():
                root = LocationStore.ensure_printer_on(conn, printer_id)
                reported = _ensure_reported(conn, printer_id)
            rows = conn.execute(
                "SELECT id, name, number, ams_id, tray_id FROM filament_locations"
                " WHERE printer_id = %s AND archived_at IS NULL AND NOT printer_root"
                " AND ams_id IS NOT NULL ORDER BY ams_id = %s, ams_id, tray_id NULLS FIRST",
                (printer_id, EXTERNAL),
            ).fetchall()
            head = conn.execute(
                "SELECT name, number FROM filament_locations WHERE id = %s", (root,)
            ).fetchone()
            assert head is not None
            if not reported:
                unreported.append(str(head["name"]))
            labels.append(_label(root, head, 0))
            for row in rows:
                depth = 2 if row["tray_id"] is not None and row["ams_id"] != EXTERNAL else 1
                labels.append(_label(row["id"], row, depth))
    return labels, unreported


def _ensure_reported(conn: Connection[DictRow], printer_id: UUID) -> bool:
    """Make the place of each AMS unit, slot and external holder the printer's latest
    report lists (`printer_reports`, read as capture reads it, `status_from_report`), named
    as capture names them (`place_label`). False when there is no report, or it lists no
    unit and no external holder: then nothing is made. With no ``ams`` kind nothing is made
    either, and the places the printer has are listed."""
    row = conn.execute(
        "SELECT report, seen_at FROM printer_reports WHERE printer_id = %s", (printer_id,)
    ).fetchone()
    printer = PrinterStore.read_on(conn, str(printer_id))
    if row is None or printer is None:
        return False
    at = row["seen_at"]
    try:
        status = status_from_report(
            printer,
            LanReport(serial=printer.serial, received_at=at, print=dict(row["report"])),
            now=at,
        )
    except Exception as error:
        # A stored report that cannot be read is one printer's problem, as in `reconcile_all`.
        logger.warning(
            prints.FILAMENT_CAPTURE_FAILED,
            extra={"printer_id": str(printer_id), "what": "labels", "error": type(error).__name__},
        )
        return False
    slots = [(unit.id, tray.id) for unit in status.ams for tray in unit.tray]
    slots += [(EXTERNAL, tray.id - 254) for tray in status.vt_tray]
    if not status.ams and not slots:
        return False
    try:
        for unit in status.ams:
            LocationStore.ensure_ams_unit_on(conn, printer_id, unit.id)
        for ams_id, tray_id in slots:
            LocationStore.ensure_ams_on(
                conn, printer_id, ams_id, tray_id, label=place_label(printer.name, ams_id, tray_id)
            )
    except NoAmsKindError:
        pass
    return True


def _label(location_id: UUID, row: Mapping[str, Any], depth: int) -> m.PrinterLabel:
    return m.PrinterLabel(
        location_id=location_id,
        code=format_code("L", row["number"]),
        name=row["name"],
        depth=depth,
    )


#: A place label's QR payload at most (`labelSheet.ts` `MAX_PAYLOAD_BYTES`): QR version 5 at
#: level M, a module of at least 0.5 mm in the 19.8 mm box.
MAX_PAYLOAD_BYTES = 84


class LabelNotFoundError(LookupError):
    """A spool or place a labels PDF names does not exist."""

    def __init__(self, what: str, row_id: UUID) -> None:
        super().__init__(f"no {what} {row_id}")


class NoLabelBaseError(Exception):
    """A place label needs an address, and neither the label address nor the public URL
    gives one (#7157, R-7157-5)."""


class LabelPayloadTooLongError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(
            f"The address for {code} is too long for a QR code on a label."
            " Shorten the label address in Settings."
        )


def pdf_labels(
    spools: FilamentSpoolStore,
    locations: LocationStore,
    spool_ids: Sequence[UUID],
    location_ids: Sequence[UUID],
    base: str | None,
) -> list[Label]:
    """The labels of ``spool_ids``, then ``location_ids``, from SB's own rows, with what the
    browser label carries (`frontend/src/components/filament/Label.tsx`): a spool's number,
    material and subtype, colour name and brand, its QR `scadbuddy:spool:<id>`; a place's
    code and name, its QR `<base>/x/L-<n>`. An archived row still gets its label."""
    labels: list[Label] = []
    for spool_id in spool_ids:
        spool = spools.get(spool_id)
        if spool is None:
            raise LabelNotFoundError("spool", spool_id)
        material = " ".join(n for n in (spool.names.material, spool.names.subtype) if n)
        labels.append(
            SpoolLabel(
                number=spool.number,
                material=material or None,
                colour=spool.effective.colour_name or spool.colour_name,
                brand=spool.names.brand,
                qr=f"scadbuddy:spool:{spool_id}",
            )
        )
    for location_id in location_ids:
        place = locations.get(location_id)
        if place is None:
            raise LabelNotFoundError("location", location_id)
        if base is None:
            raise NoLabelBaseError()
        code = format_code("L", place.number)
        payload = f"{base.rstrip('/')}/x/{code}"
        if len(payload.encode()) > MAX_PAYLOAD_BYTES:
            raise LabelPayloadTooLongError(code)
        labels.append(PlaceLabel(code=code, name=place.name, qr=payload))
    return labels
