"""Location labels (#6955)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from scadbuddy.bambuddy.trays import EXTERNAL
from scadbuddy.codes.parse import format_code
from scadbuddy.filament import models as m
from scadbuddy.filament.catalog import CatalogRefError
from scadbuddy.filament.locations import LocationStore


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
) -> list[m.PrinterLabel]:
    """The labels of each printer (by name; every printer when ``printer_ids`` is
    ``None``): its own place (depth 0), each AMS unit by ``ams_id`` (1) with its slots by
    ``tray_id`` (2), the external holder (1) last. A printer's place is made if it has none
    (`LocationStore.ensure_printer_on`); a place a person made beside the printer is left
    out. `CatalogRefError` for an unknown printer, `NoPrinterKindError` for no printer
    kind."""
    labels: list[m.PrinterLabel] = []
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
            root = LocationStore.ensure_printer_on(conn, printer_id)
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
            labels.append(_label(root, head, 0))
            for row in rows:
                depth = 2 if row["tray_id"] is not None and row["ams_id"] != EXTERNAL else 1
                labels.append(_label(row["id"], row, depth))
    return labels


def _label(location_id: UUID, row: Mapping[str, Any], depth: int) -> m.PrinterLabel:
    return m.PrinterLabel(
        location_id=location_id,
        code=format_code("L", row["number"]),
        name=row["name"],
        depth=depth,
    )
