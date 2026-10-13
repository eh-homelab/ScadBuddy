"""The farm's context, read from Bambuddy (#1912, #251's farm-context tier).

What the agent's analyzers need beyond a single print: the queue, every archive with its
outcome, the aggregate stats and the spool inventory with each loaded slot's remaining
grams. Every read here is ``can_read_status`` and none of them writes to Bambuddy.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from pydantic import BaseModel, Field

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import ArchiveDetail, Printer, SlotMaterial


class ArchiveOutcome(BaseModel):
    """One Bambuddy archive (a past or current print, or an upload that never printed)
    and how it went. Any archive, not only ScadBuddy's own (``GET /prints`` is those)."""

    id: int
    print_name: str | None = None
    status: str | None = None
    printer_id: int | None = None
    project_id: int | None = None
    plate_id: int | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    print_time_seconds: int | None = None
    actual_time_seconds: int | None = None
    filament_type: str | None = None
    filament_color: str | None = None
    filament_used_grams: float | None = None
    cost: float | None = None
    failure_reason: str | None = None
    quantity: int = 1
    run_count: int = 0
    successful_run_count: int = 0
    failed_run_count: int = 0
    last_run_at: datetime | None = None

    @classmethod
    def of(cls, archive: ArchiveDetail) -> ArchiveOutcome:
        return cls.model_validate(archive.model_dump(include=set(cls.model_fields)))


class SpoolPlace(BaseModel):
    """Where a spool is loaded."""

    printer_id: int
    printer_name: str | None = None
    ams_id: int
    tray_id: int


class InventorySpool(BaseModel):
    id: int
    material: str
    subtype: str | None = None
    brand: str | None = None
    color_name: str | None = None
    #: ``RRGGBBAA``, no ``#``.
    rgba: str | None = None
    label_weight: int = 0
    weight_used: float = 0.0
    #: The label weight less what Bambuddy counted as used; a loaded slot's
    #: ``remaining_g`` is the reconciled figure.
    remaining_g: float
    storage_location: str | None = None
    archived: bool = False
    loaded: SpoolPlace | None = None


class LoadedSlot(BaseModel):
    """One loaded slot of an active printer, as ``inventory-remain`` reports it."""

    printer_id: int
    printer_name: str
    ams_id: int
    tray_id: int
    #: The number ``ams_mapping`` carries.
    global_tray_id: int
    extruder: int | None = None
    remaining_g: float | None = None
    #: The inventory spool assigned to the slot, when one is.
    spool_id: int | None = None
    brand: str | None = None
    material: str | None = None
    subtype: str | None = None
    color_name: str | None = None
    rgba: str | None = None


class InventoryView(BaseModel):
    spools: list[InventorySpool] = Field(default_factory=list)
    slots: list[LoadedSlot] = Field(default_factory=list)


def _slot(printer: Printer, slot: SlotMaterial, spool_id: int | None) -> LoadedSlot:
    spool = slot.spool or {}
    return LoadedSlot(
        printer_id=printer.id,
        printer_name=printer.name,
        ams_id=slot.ams_id,
        tray_id=slot.tray_id,
        global_tray_id=slot.global_tray_id,
        extruder=slot.extruder,
        remaining_g=slot.remaining_g,
        spool_id=spool_id,
        brand=spool.get("brand"),
        material=spool.get("material"),
        subtype=spool.get("subtype"),
        color_name=spool.get("color_name"),
        rgba=spool.get("rgba"),
    )


async def inventory_view(
    client: BambuddyClient, *, include_archived: bool = False, printer_id: int | None = None
) -> InventoryView:
    """Every spool, with where it is loaded, and every loaded slot of each active printer
    (or of ``printer_id`` alone) with its remaining grams. ``printer_id`` narrows only the
    slots: every spool keeps its placement, so one loaded elsewhere never reads as free."""
    spools = await client.spools(include_archived=include_archived)
    assignments = await client.spool_assignments()
    printers = (
        [await client.printer(printer_id)] if printer_id is not None else await client.printers()
    )
    active = [printer for printer in printers if printer.is_active]
    remains = await asyncio.gather(*(client.inventory_remain(p.id) for p in active))

    places = {
        assignment.spool_id: SpoolPlace(
            printer_id=assignment.printer_id,
            printer_name=assignment.printer_name,
            ams_id=assignment.ams_id,
            tray_id=assignment.tray_id,
        )
        for assignment in assignments
    }
    in_slot = {(a.printer_id, a.ams_id, a.tray_id): a.spool_id for a in assignments}
    return InventoryView(
        spools=[
            InventorySpool(
                **spool.model_dump(
                    include={
                        "id",
                        "material",
                        "subtype",
                        "brand",
                        "color_name",
                        "rgba",
                        "label_weight",
                        "weight_used",
                        "storage_location",
                    }
                ),
                remaining_g=spool.remaining_g,
                archived=spool.archived_at is not None,
                loaded=places.get(spool.id),
            )
            for spool in spools
        ],
        slots=[
            _slot(printer, slot, in_slot.get((printer.id, slot.ams_id, slot.tray_id)))
            for printer, remain in zip(active, remains, strict=True)
            for slot in sorted(remain.slot_materials, key=lambda s: s.global_tray_id)
        ],
    )
