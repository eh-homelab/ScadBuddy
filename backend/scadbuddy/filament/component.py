"""Filament as a component (`core/components.py`): ScadBuddy's own filament inventory
(#6261, epic #6260). Every store is built over the settings store's pool."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.filament.assignments_cache import AssignmentsCache
from scadbuddy.filament.bb_sync_passes import SyncPassStore
from scadbuddy.filament.catalog import CatalogStore
from scadbuddy.filament.insert_weights import InsertWeightStore
from scadbuddy.filament.inventory import FilamentInventory, PoolSbSpoolReads
from scadbuddy.filament.invoices import InvoiceStore
from scadbuddy.filament.label_sheets import LabelSheetStore
from scadbuddy.filament.locations import LocationStore
from scadbuddy.filament.metrics_store import MetricsSettings, MetricsStore
from scadbuddy.filament.presets import PoolSpoolPresets
from scadbuddy.filament.print_uses import PoolPrintUses, print_use_hook
from scadbuddy.filament.profile_fill import TypeFiller
from scadbuddy.filament.rfid_mode import RfidModeStore
from scadbuddy.filament.sb_presets import FilamentPresetStore
from scadbuddy.filament.sensors import SensorStore
from scadbuddy.filament.shopping import ShoppingStore
from scadbuddy.filament.spools import FilamentDefaults, FilamentSpoolStore
from scadbuddy.filament.suppliers import SuppliersStore
from scadbuddy.filament.types import SpoolTypeStore
from scadbuddy.orders.density import DensityTable, density_table


@dataclass
class Filament:
    catalog: CatalogStore
    types: SpoolTypeStore
    spools: FilamentSpoolStore
    locations: LocationStore
    inventory: FilamentInventory
    sensors: SensorStore
    #: What a print's plan reads of SB spools' presets (#6262 F1, `resolve_plates`).
    presets: PoolSpoolPresets
    #: The RFID override mode's row (#6264).
    rfid: RfidModeStore
    #: Suppliers, their products and price points (#6266).
    suppliers: SuppliersStore
    #: Invoice drafts and their confirmation into spools and costs (#6266).
    invoices: InvoiceStore
    #: The spools a person means to buy (#6266).
    shopping: ShoppingStore
    #: A spool's history, the forecast and the inventory metrics (#6269).
    metrics: MetricsStore
    #: Bambuddy's tray assignments, as this app last read them (#6580).
    assignments: AssignmentsCache = field(default_factory=AssignmentsCache)
    #: The image's slicer profiles a type is filled from (#6714); ``None`` without them.
    profile_table: Callable[[], DensityTable | None] = lambda: None
    #: ScadBuddy's own filament presets (#7176); ``None`` in a test that builds no store.
    own_presets: FilamentPresetStore | None = None

    @property
    def filler(self) -> TypeFiller:
        """Fills spool types from their slicer profiles (#6714)."""
        return TypeFiller(self.types, self.profile_table, self.own_presets)

    @property
    def reads(self) -> PoolSbSpoolReads:
        """Every SB spool read a print's plan makes, as the print worker's CRUD routes
        make them (`SbSpoolReads`): Bambuddy's path's picker rows among them (#6262
        Task 7)."""
        return PoolSbSpoolReads(self.inventory, self.presets)

    @property
    def insert_weights(self) -> InsertWeightStore:
        """An insert's empty weight from new spools' weighings (#7280)."""
        return InsertWeightStore(self.spools)

    @property
    def label_sheets(self) -> LabelSheetStore:
        """The saved label sheet grids (#7216)."""
        return LabelSheetStore(self.spools.pool)

    @property
    def sync_passes(self) -> SyncPassStore:
        """The Bambuddy sync's pass history (#6905)."""
        return SyncPassStore(self.spools)


FILAMENT: Key[Filament] = Key("filament")


def _build(core: Core, components: Components) -> Filament:
    pool = core.settings_store.pool

    def defaults() -> FilamentDefaults:
        # Two rows in one query, read on each use: a change applies to the next read. It
        # takes a connection of its own, so the stores call it before they take theirs.
        read = core.settings_store.read(
            {
                "filament_low_stock_pct",
                "filament_default_cost_per_kg",
                "filament_drying_temp_c",
                "filament_drying_hours",
                "filament_weighing_drift_notice_g",
            }
        )
        return FilamentDefaults(
            low_stock_pct=int(read["filament_low_stock_pct"]),
            cost_per_kg=read["filament_default_cost_per_kg"],
            drying_temp_c=read["filament_drying_temp_c"],
            drying_hours=read["filament_drying_hours"],
            drift_notice_g=int(read["filament_weighing_drift_notice_g"]),
        )

    def shop_currency() -> str:
        # A settings read on a connection of its own, on each use (`defaults`' rule).
        return core.settings_store.snapshot().runtime.orders_currency

    def metrics_settings() -> MetricsSettings:
        # One query, on a connection of its own, on each use (`defaults`' rule).
        read = core.settings_store.read(
            {
                "filament_burn_window_days",
                "filament_lead_time_days",
                "filament_safety_margin_days",
                "filament_default_cost_per_kg",
                "orders_currency",
            }
        )
        return MetricsSettings(
            window_days=int(read["filament_burn_window_days"]),
            lead_time_days=int(read["filament_lead_time_days"]),
            safety_margin_days=int(read["filament_safety_margin_days"]),
            currency=read["orders_currency"],
            default_cost_per_kg=read["filament_default_cost_per_kg"],
        )

    # What a native print used is settled on its SB spools as the follow reads it settled
    # (#4643, #6262 Task 6); the follow is a core service and imports no feature.
    core.print_follower.on_read.append(print_use_hook(PoolPrintUses(pool)))
    types = SpoolTypeStore(pool)
    spools = FilamentSpoolStore(pool, defaults)
    return Filament(
        catalog=CatalogStore(pool),
        types=types,
        spools=spools,
        locations=LocationStore(pool),
        inventory=FilamentInventory(pool, defaults),
        sensors=SensorStore(pool),
        presets=PoolSpoolPresets(pool),
        rfid=RfidModeStore(pool),
        suppliers=SuppliersStore(pool, shop_currency),
        invoices=InvoiceStore(pool, shop_currency, spools, types),
        shopping=ShoppingStore(pool),
        metrics=MetricsStore(pool, spools, metrics_settings),
        profile_table=lambda: density_table(core.settings.slicer_profiles_dir),
        own_presets=FilamentPresetStore(pool, lambda: core.store.blobs),
    )


COMPONENT = Component(FILAMENT, build=_build)

FilamentDep = Annotated[Filament, component_dep(FILAMENT)]
