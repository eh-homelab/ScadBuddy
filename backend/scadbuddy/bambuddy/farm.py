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
from scadbuddy.bambuddy.models import ArchiveDetail, Printer, SlotMaterial, Spool
from scadbuddy.core.problems import ApiError


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
    #: Price per kilogram, ``None`` when Bambuddy has none (#785).
    cost_per_kg: float | None = None
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
                        "cost_per_kg",
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


class LoadedSpool(SpoolPlace):
    spool_id: int


class FilamentStock(BaseModel):
    """Every unarchived spool of one filament: the same material, subtype, brand and
    colour (``rgba``, else the colour's name)."""

    material: str
    subtype: str | None = None
    brand: str | None = None
    color_name: str | None = None
    rgba: str | None = None
    spool_count: int
    spool_ids: list[int] = Field(default_factory=list)
    #: Sum of each spool's label weight less what Bambuddy counted as used.
    remaining_g: float
    #: Where the filament's spools are loaded, one row per loaded spool.
    loaded: list[LoadedSpool] = Field(default_factory=list)


class SpoolSummary(BaseModel):
    """What a filament hover card shows for one spool (#785): its stock and price, and
    the stock of every spool of the same filament. Prices are Bambuddy's ``cost_per_kg``
    as entered there; every price and cost is ``None`` when it has none, never a guess."""

    spool: InventorySpool
    #: ``cost_per_kg`` / 1000.
    price_per_g: float | None = None
    #: What the filament left on this spool is worth.
    remaining_value: float | None = None
    #: ``grams`` x ``price_per_g``, when the caller asked about ``grams`` (a plate's use).
    grams: float | None = None
    grams_cost: float | None = None
    filament: FilamentStock


def _filament_key(spool: Spool) -> tuple[str, str, str, str]:
    colour = (spool.rgba or spool.color_name or "").strip().upper()
    return (
        spool.material.strip().upper(),
        (spool.subtype or "").strip().upper(),
        (spool.brand or "").strip().upper(),
        colour,
    )


def _money(value: float) -> float:
    return round(value, 4)


async def spool_summary(
    client: BambuddyClient, spool_id: int, *, grams: float | None = None
) -> SpoolSummary:
    """One spool's stock and price, and its filament's stock across the inventory. An
    archived spool is still summarised; its filament's stock counts unarchived spools."""
    spools, assignments = await asyncio.gather(
        client.spools(include_archived=True), client.spool_assignments()
    )
    spool = next((row for row in spools if row.id == spool_id), None)
    if spool is None:
        raise ApiError(404, f"spool {spool_id} is not in Bambuddy's inventory")
    places = {
        a.spool_id: SpoolPlace(
            printer_id=a.printer_id, printer_name=a.printer_name, ams_id=a.ams_id, tray_id=a.tray_id
        )
        for a in assignments
    }
    key = _filament_key(spool)
    same = [row for row in spools if row.archived_at is None and _filament_key(row) == key]
    price_per_g = spool.cost_per_kg / 1000 if spool.cost_per_kg is not None else None
    return SpoolSummary(
        spool=InventorySpool(
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
                    "cost_per_kg",
                }
            ),
            remaining_g=spool.remaining_g,
            archived=spool.archived_at is not None,
            loaded=places.get(spool.id),
        ),
        price_per_g=price_per_g,
        remaining_value=_money(spool.remaining_g * price_per_g)
        if price_per_g is not None
        else None,
        grams=grams,
        grams_cost=(
            _money(grams * price_per_g) if grams is not None and price_per_g is not None else None
        ),
        filament=FilamentStock(
            material=spool.material,
            subtype=spool.subtype,
            brand=spool.brand,
            color_name=spool.color_name,
            rgba=spool.rgba,
            spool_count=len(same),
            spool_ids=[row.id for row in same],
            remaining_g=round(sum(row.remaining_g for row in same), 2),
            loaded=[
                LoadedSpool(spool_id=row.id, **place.model_dump())
                for row in same
                if (place := places.get(row.id)) is not None
            ],
        ),
    )
