"""What `filament/metrics.py` gives (#6269): plates, a spool's metrics, the AMS reading
error and the grams series; and what `filament/metrics_store.py` reads into them: a
spool's history, the forecast, the inventory and purchasing metrics and the gauges'
snapshot."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from scadbuddy.filament.models import EventKind, SpoolEvent

#: How a plate ended: BB's status, or SB's (applied is ``completed``, else ``failed``).
Outcome = Literal["completed", "failed", "cancelled", "unknown"]


class Plate(BaseModel):
    """One plate on one spool, counted once (plan Decision 6)."""

    job: str | None
    at: datetime
    #: The grams the plate used, never negative.
    grams: float
    outcome: Outcome
    printer_id: UUID | None
    #: The event the plate's grams come from.
    event_id: UUID
    #: The metres the plate extruded (#7685, the end's ``data.length_m``); ``None`` for a
    #: plate booked without one (a BB usage row, or a print from before the length).
    length_m: float | None = None


class AmsError(BaseModel):
    """AMS grams less scale grams over the paired readings (plan Decision 14)."""

    pairs: int
    mean_abs_g: float
    bias_g: float


class GramsPoint(BaseModel):
    """The grams left after one event (plan Decision 13)."""

    at: datetime
    grams: float
    #: The event is the reading the grams left anchored on at its own time.
    anchor: bool
    kind: EventKind


class SpoolMetrics(BaseModel):
    """One spool's use, cost and forecast."""

    used_g: float
    #: The grams of the plates after the newest ``consumed_reset`` event, all of them with
    #: none (#7149 R-7149-4): the counter a person resets, as Bambuddy's "Total consumed".
    consumed_g: float = 0.0
    #: When that reset was; ``None`` with no reset.
    consumed_since: datetime | None = None
    prints: int
    grams_per_print: float | None
    #: Grams of ``failed`` and ``cancelled`` plates (plan Decision 7).
    wasted_g: float
    cost_used: Decimal | None
    value_left: Decimal | None
    burn_g_per_day: float | None
    empty_on: date | None
    opened_at: datetime | None
    days_since_opened: float | None
    days_since_last_use: float | None
    hours_outside_dry: float
    ams_error: AmsError | None
    #: #7685 (spec §5.6): the metres of the plates that carry a length; ``None`` when
    #: none does.
    used_m: float | None = None
    #: The density those plates' grams and metres imply over the spool's diameter;
    #: ``None`` without metres.
    implied_density_g_cm3: float | None = None
    #: The spool's effective type density, for the comparison.
    density_g_cm3: float | None = None
    #: Both known and apart by more than `metrics.DENSITY_TOLERANCE`.
    density_mismatch: bool = False


class TimelineRow(BaseModel):
    """One event of a spool's timeline, with what it links to (plan Decision 17)."""

    event: SpoolEvent
    #: The order whose attempt printed the plate: ``order_item_fulfilments.print_run_id``
    #: names the run in the event's ``ref_id`` (``run/subtask/slot``).
    order_id: str | None
    #: The Bambuddy print archive the plate matched (``data.bb_archive_id``).
    archive_id: int | None


class SpoolHistory(BaseModel):
    """A spool's timeline, its grams series, its metrics and its type's AMS error."""

    #: Newest first by ``(at, seq)``, as `ledger.page` orders them (F28).
    events: list[TimelineRow]
    series: list[GramsPoint]
    metrics: SpoolMetrics
    #: The AMS error over every spool of the spool's type; ``None`` with no type or no pair.
    type_ams_error: AmsError | None
    #: The shop's ``orders_currency``: the currency of the metrics' money.
    currency: str
    #: The burn rate's window, ``filament_burn_window_days`` (F24).
    window_days: int


#: What the inventory metrics group by. ``location`` is the spool's place (by name).
Dimension = Literal["material", "brand", "type", "supplier", "location"]
Bucket = Literal["week", "month"]
#: What the waste is grouped by: the spool's material or type, or the plate's printer.
WasteDimension = Literal["material", "type", "printer"]


class StockRow(BaseModel):
    """The unarchived spools of one value of the dimension. ``key`` is the id (``None``:
    none set); ``value`` is the grams left at cost, ``None`` when no spool has a cost."""

    key: str | None
    label: str
    spools: int
    grams_left: float
    value: Decimal | None


class ConsumptionRow(BaseModel):
    """The plates of one bucket (its first day) and one value of the dimension."""

    bucket: date
    key: str | None
    label: str
    used_g: float
    wasted_g: float


class WasteRow(BaseModel):
    """The plates of one material, type or printer; ``rate`` is wasted over used, ``None``
    when nothing was used (plan Decision 7)."""

    dimension: WasteDimension
    key: str | None
    label: str
    used_g: float
    wasted_g: float
    rate: float | None


class InventoryMetrics(BaseModel):
    """Stock now, and the consumption and waste of the plates from ``since`` to ``until``
    (both days included). Money is in ``currency``, the shop's."""

    currency: str
    dimension: Dimension
    bucket: Bucket
    since: date
    until: date
    stock: list[StockRow]
    consumption: list[ConsumptionRow]
    waste: list[WasteRow]


class MaterialNumberType(BaseModel):
    """A type whose code is the material number (#7152, R-7152-5)."""

    id: UUID
    code: str
    archived: bool


class MaterialNumberRow(BaseModel):
    """One material number (a type code, case folded; ``None``: untyped spools). On hand
    is the unarchived spools (R-7152-7); ``used_g`` and ``wasted_g`` are the plates of the
    range, as `ConsumptionRow`'s (R-7152-6). ``value`` is ``None`` when no spool has a
    cost."""

    key: str | None
    label: str
    #: Every type of the code, archived ones too; empty for untyped spools.
    types: list[MaterialNumberType]
    spools: int
    grams_left: float
    used_g: float
    wasted_g: float
    value: Decimal | None


class MaterialNumberStats(BaseModel):
    """The stats by material number from ``since`` to ``until`` (both days included);
    money in ``currency``, the shop's."""

    currency: str
    since: date
    until: date
    rows: list[MaterialNumberRow]


class SpoolForecast(BaseModel):
    """One unarchived spool's burn rate and empty day."""

    spool_id: UUID
    number: int
    type_id: UUID | None
    grams_left: float | None
    burn_g_per_day: float | None
    empty_on: date | None


class TypeForecast(BaseModel):
    """One type's stock and when to reorder it (plan Decision 9)."""

    type_id: UUID
    label: str
    material: str | None
    #: Its unarchived spools.
    spools: int
    grams_left: float
    #: The burn of the union of all its spools' plates, archived ones too (F16).
    burn_g_per_day: float | None
    days_of_stock: float | None
    empty_on: date | None
    lead_time_days: int
    safety_margin_days: int
    reorder_on: date | None
    #: ``reorder_on`` is today or past.
    reorder_now: bool
    #: Every unarchived spool of the type is low on stock (false when none is in stock).
    low_stock: bool


class Forecast(BaseModel):
    window_days: int
    spools: list[SpoolForecast]
    types: list[TypeForecast]


class PricePerKg(BaseModel):
    on_date: date
    price_per_kg: Decimal


class PriceSeries(BaseModel):
    """One type's price per kg at one supplier, in one currency: no series mixes
    currencies (F14)."""

    type_id: UUID
    type_label: str
    supplier_id: UUID
    supplier_name: str
    currency: str
    points: list[PricePerKg]


class SpendRow(BaseModel):
    """The confirmed invoices of one supplier, month and currency, never converted."""

    supplier_id: UUID
    supplier_name: str
    month: date
    currency: str
    total: Decimal
    invoices: int


class PurchasingMetrics(BaseModel):
    prices: list[PriceSeries]
    spend: list[SpendRow]


class Snapshot(BaseModel):
    """What the gauges read (plan Decisions 3-5): raw catalog names, folded later.
    ``stock`` rows are (material, brand, location_kind, spools, grams, value) of the
    unarchived spools; ``used`` is every plate's grams by material and outcome; ``burn``
    is each material's union burn (0 with no use in the window); ``days_of_stock`` only
    the materials with a burn above 0; ``low_stock`` counts low spools, ``reorder`` the
    types to reorder now, per material."""

    stock: list[tuple[str | None, str | None, str | None, int, float, Decimal | None]]
    used: dict[tuple[str | None, Outcome], float]
    burn: dict[str | None, float]
    days_of_stock: dict[str | None, float]
    low_stock: dict[str | None, int]
    reorder: dict[str | None, int]
    currency: str
