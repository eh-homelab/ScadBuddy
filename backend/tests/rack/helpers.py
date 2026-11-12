"""Invented rack fixtures (#836). Every serial here is made up (spec 2026-10-01 §7)."""

from __future__ import annotations

from typing import Any

import psycopg

from scadbuddy.bambuddy.models import (
    FilamentGroup,
    FilamentRequirement,
    NozzleRackSlot,
    PrinterStatus,
)
from scadbuddy.rack.rank import RackGroup
from scadbuddy.render import pg_store
from tests.bambuddy.conftest import recording

#: Two registry printers a hotend is seen on (#5167): the rack tables name the registry
#: printer, a foreign key. :func:`registry_printers` makes them.
P1 = "00000000-0000-4000-8000-00000000c001"
P2 = "00000000-0000-4000-8000-00000000c002"


def registry_printers(conninfo: str) -> None:
    """Migrate the schema and add ``P1`` and ``P2``."""
    with psycopg.connect(conninfo) as conn:
        pg_store.migrate(conn)
        conn.execute(
            "INSERT INTO printers (id, serial, name) VALUES"
            " (%s, 'TESTRACK00001', 'Rack 1'), (%s, 'TESTRACK00002', 'Rack 2')",
            (P1, P2),
        )


def serial(rack_id: int) -> str:
    """An invented serial for the hotend at status id ``rack_id``."""
    return f"TEST-HOTEND-{rack_id:02d}"


#: Every invented serial :func:`invented_status` hands out.
INVENTED_SERIALS = [serial(rack_id) for rack_id in (0, 1, 17, 18, 19, 20, 21)]


def slot(
    position: int,
    nozzle_type: str = "HS01",
    diameter: str = "0.4",
    color: str = "000000FF",
    *,
    serial_number: str | None = None,
) -> NozzleRackSlot:
    rack_id = position + 15
    return NozzleRackSlot(
        id=rack_id,
        nozzle_type=nozzle_type,
        nozzle_diameter=diameter,
        filament_color=color,
        serial_number=serial(rack_id) if serial_number is None else serial_number,
    )


def mounted(
    nozzle_type: str = "HS00", diameter: str = "0.2", color: str = "27272CFF"
) -> NozzleRackSlot:
    """The rack-side hotend on the carriage: status id 0 (``extruders.RACK_SIDE``)."""
    return NozzleRackSlot(
        id=0,
        nozzle_type=nozzle_type,
        nozzle_diameter=diameter,
        filament_color=color,
        serial_number=serial(0),
    )


def group(
    group_id: int = 0,
    *,
    diameter: str = "0.40",
    volume: str = "Standard",
    color: str | None = None,
    materials: tuple[str, ...] = ("PLA",),
    abrasive: bool = False,
) -> RackGroup:
    return RackGroup(
        group_id=group_id,
        nozzle_diameter=diameter,
        volume_type=volume,
        color=color,
        materials=materials,
        abrasive=abrasive,
    )


def requirement(
    slot_id: int = 1,
    *,
    filament_type: str = "PLA",
    group_id: int | None = 0,
    diameter: str = "0.40",
    volume: str = "Standard",
    color: str = "#FF6A13",
    on_rack: bool = True,
    used: bool = True,
) -> FilamentRequirement:
    return FilamentRequirement(
        slot_id=slot_id,
        type=filament_type,
        color=color,
        used_in_plate=used,
        group_id=group_id,
        group=FilamentGroup(
            on_rack=on_rack, nozzle_diameter=diameter, volume_type=volume, filament_color=color
        ),
    )


def status(*rack: NozzleRackSlot) -> PrinterStatus:
    return PrinterStatus(id=1, name="H2C", connected=True, nozzle_rack=list(rack))


def invented_status() -> dict[str, Any]:
    """``printer-status-rack.json`` with each hotend's serial replaced by an invented one."""
    body: dict[str, Any] = recording("printer-status-rack.json")
    for entry in body["nozzle_rack"]:
        entry["serial_number"] = serial(entry["id"])
    return body
