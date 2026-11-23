"""The filament metrics read from the database (#6269, spec §5.7): a spool's history, the
forecast, the inventory and purchasing metrics and the gauges' snapshot. The arithmetic
is `filament/metrics.py`'s; this module reads the rows and groups them.

The spools come from `FilamentSpoolStore.list` with the archived ones (their effective
values, catalog names and ``hours_outside_dry``; an archived spool's plates still count
as consumption), and their plates from `ledger.plate_events` (plan Decision 20's index).
A burn of 0 or less is no forecast: no day count or empty day is divided out of it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import isfinite
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.core.settings import Settings
from scadbuddy.filament import ledger, metrics
from scadbuddy.filament import metrics_models as mm
from scadbuddy.filament import models as m
from scadbuddy.filament.invoice_money import price_per_kg
from scadbuddy.filament.spools import FilamentSpoolStore

Conn = Connection[DictRow]
Row = Mapping[str, Any]

_CENT = Decimal("0.01")
_EVENT_COLUMNS = ", ".join(f"e.{f}" for f in m.SpoolEvent.model_fields)
#: What a key with no value is called, per dimension.
_NONE_LABEL = {
    "material": "No material",
    "brand": "No brand",
    "type": "No type",
    "supplier": "No supplier",
    "location": "No location",
    "printer": "No printer",
    "material_number": "No material number",
}
_WASTE: tuple[mm.WasteDimension, ...] = ("material", "type", "printer")


def _default(name: str) -> Any:
    return Settings.model_fields[name].default


@dataclass(frozen=True)
class MetricsSettings:
    """The settings the metrics read, as one read of them: the burn window
    (``filament_burn_window_days``), a type's lead time and safety margin when it names
    none (``filament_lead_time_days``, ``filament_safety_margin_days``), the shop's
    ``orders_currency`` and the cost per kg of a spool and type that name none
    (``filament_default_cost_per_kg``, as the summary values it)."""

    window_days: int = _default("filament_burn_window_days")
    lead_time_days: int = _default("filament_lead_time_days")
    safety_margin_days: int = _default("filament_safety_margin_days")
    currency: str = _default("orders_currency")
    default_cost_per_kg: Decimal | None = None


@dataclass(frozen=True)
class _Read:
    """Every spool (archived too) with its plates, place kind and supplier, and the
    types' and printers' rows the groups name."""

    spools: list[m.Spool]
    plates: dict[UUID, list[mm.Plate]]
    #: A spool's place's kind name.
    kinds: dict[UUID, str | None]
    #: A spool's supplier: its invoice line's confirmed invoice's.
    suppliers: dict[UUID, tuple[UUID, str]]
    types: dict[UUID, Row]
    printers: dict[UUID, str]

    def stocked(self) -> list[m.Spool]:
        return [s for s in self.spools if s.archived_at is None]


def _days_of_stock(grams: float, burn: float | None) -> float | None:
    """``grams`` over ``burn``; ``None`` with no burn above 0 or a result past
    `metrics.MAX_FORECAST_DAYS`, as `metrics.empty_on`."""
    if burn is None or not isfinite(burn) or burn <= 0:
        return None
    days = grams / burn
    return days if isfinite(days) and days <= metrics.MAX_FORECAST_DAYS else None


def _union_burn(
    spools: Iterable[m.Spool], plates: Mapping[UUID, list[mm.Plate]], window: int, now: datetime
) -> float | None:
    """The burn of the union of ``spools``' plates, born at the oldest one (F16)."""
    group = list(spools)
    if not group:
        return None
    union = [p for s in group for p in plates.get(s.id, [])]
    born = min(s.created_at for s in group)
    return metrics.burn(union, window_days=window, born=born, now=now)


def _bucket(day: date, bucket: mm.Bucket) -> date:
    if bucket == "week":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(_CENT)


class MetricsStore:
    """The metrics reads over a pool whose connections autocommit and return dict rows
    (the settings store's). Synchronous: callers run it in a thread. ``settings`` may take
    a connection of its own, so each read calls it before it takes one of the pool's."""

    def __init__(
        self,
        pool: ConnectionPool[Conn],
        spools: FilamentSpoolStore,
        settings: Callable[[], MetricsSettings],
    ) -> None:
        self._pool = pool
        self._spools = spools
        self._settings = settings

    # -- one spool ----------------------------------------------------------------------

    def history(self, spool_id: UUID) -> mm.SpoolHistory | None:
        """The spool's timeline (newest first, with its order and BB print links), its
        grams series, its metrics and its type's AMS error; ``None`` for no such spool."""
        settings = self._settings()
        spool = self._spools.get(spool_id)
        if spool is None:
            return None
        now = datetime.now(UTC)
        with self._pool.connection() as conn:
            events = ledger.events(conn, spool_id)
            orders = _orders(conn, events)
            type_error = None if spool.type_id is None else _type_ams_error(conn, spool.type_id)
        cost = spool.effective.cost_per_kg
        if cost is None:
            cost = settings.default_cost_per_kg
        label = spool.effective.label_weight_g
        return mm.SpoolHistory(
            events=[
                mm.TimelineRow(event=e, order_id=_order_of(e, orders), archive_id=_archive_of(e))
                for e in sorted(events, key=lambda e: (e.at, e.seq), reverse=True)
            ],
            series=metrics.series(
                events, spool.grams_left, weight_locked=spool.weight_locked, label_weight_g=label
            ),
            metrics=metrics.spool_metrics(
                events,
                grams_left=spool.grams_left,
                cost_per_kg=cost,
                created_at=spool.created_at,
                last_used_at=spool.last_used_at,
                hours_outside_dry=spool.hours_outside_dry,
                label_weight_g=label,
                window_days=settings.window_days,
                now=now,
            ),
            type_ams_error=type_error,
            currency=settings.currency,
            window_days=settings.window_days,
        )

    # -- the inventory ------------------------------------------------------------------

    def forecast(self, now: datetime | None = None) -> mm.Forecast:
        """Each unarchived spool's burn and empty day, and each type's stock and reorder
        day. A type is listed when it has a spool, archived or not, unless the type is
        archived and none of its spools is in stock."""
        settings = self._settings()
        read = self._read()
        now = datetime.now(UTC) if now is None else now
        window = settings.window_days
        spools = []
        for spool in read.stocked():
            burn = metrics.burn(
                read.plates.get(spool.id, []), window_days=window, born=spool.created_at, now=now
            )
            spools.append(
                mm.SpoolForecast(
                    spool_id=spool.id,
                    number=spool.number,
                    type_id=spool.type_id,
                    grams_left=spool.grams_left,
                    burn_g_per_day=burn,
                    empty_on=metrics.empty_on(spool.grams_left, burn, now),
                )
            )
        return mm.Forecast(
            window_days=window, spools=spools, types=_type_forecasts(read, settings, now)
        )

    def inventory(
        self, dimension: mm.Dimension, bucket: mm.Bucket, since: date, until: date
    ) -> mm.InventoryMetrics:
        """Stock now by ``dimension``; the consumption of the plates that ended from
        ``since`` to ``until`` (UTC days, both included) by ``bucket`` and ``dimension``;
        their waste by material, type and printer."""
        settings = self._settings()
        read = self._read()
        stock: dict[str | None, list[Any]] = {}
        for spool in read.stocked():
            key, label = _key(read, spool, dimension)
            row = stock.setdefault(key, [label, 0, 0.0, None])
            row[1] += 1
            row[2] += spool.grams_left or 0.0
            value = _value(spool, settings.default_cost_per_kg)
            if value is not None:
                row[3] = (row[3] or Decimal(0)) + value
        used: dict[tuple[date, str | None], list[Any]] = {}
        waste: dict[tuple[mm.WasteDimension, str | None], list[Any]] = {}
        for spool in read.spools:
            for plate in read.plates.get(spool.id, []):
                day = plate.at.astimezone(UTC).date()
                if not since <= day <= until:
                    continue
                lost = plate.grams if plate.outcome in ("failed", "cancelled") else 0.0
                key, label = _key(read, spool, dimension)
                row = used.setdefault((_bucket(day, bucket), key), [label, 0.0, 0.0])
                row[1] += plate.grams
                row[2] += lost
                for name in _WASTE:
                    if name == "printer":
                        key, label = _printer(read, plate.printer_id)
                    else:
                        key, label = _key(read, spool, name)
                    row = waste.setdefault((name, key), [label, 0.0, 0.0])
                    row[1] += plate.grams
                    row[2] += lost
        return mm.InventoryMetrics(
            currency=settings.currency,
            dimension=dimension,
            bucket=bucket,
            since=since,
            until=until,
            stock=sorted(
                (
                    mm.StockRow(key=k, label=r[0], spools=r[1], grams_left=r[2], value=_money(r[3]))
                    for k, r in stock.items()
                ),
                key=lambda r: (-r.grams_left, r.label.upper()),
            ),
            consumption=sorted(
                (
                    mm.ConsumptionRow(bucket=b, key=k, label=r[0], used_g=r[1], wasted_g=r[2])
                    for (b, k), r in used.items()
                ),
                key=lambda r: (r.bucket, r.label.upper(), r.key or ""),
            ),
            waste=sorted(
                (
                    mm.WasteRow(
                        dimension=d,
                        key=k,
                        label=r[0],
                        used_g=r[1],
                        wasted_g=r[2],
                        rate=r[2] / r[1] if r[1] > 0 else None,
                    )
                    for (d, k), r in waste.items()
                ),
                key=lambda r: (_WASTE.index(r.dimension), -r.wasted_g, r.label.upper()),
            ),
        )

    def material_numbers(self, since: date, until: date) -> mm.MaterialNumberStats:
        """Stock now and the plates that ended from ``since`` to ``until`` (UTC days, both
        included) by material number: the spool's type's code, case folded, archived types
        of the code in it (#7152, R-7152-5 to R-7152-8). A key is listed when it has a spool
        in stock or a plate in the range."""
        settings = self._settings()
        read = self._read()
        by_key: dict[str | None, list[Any]] = {}
        for spool in read.spools:
            row = None if spool.type_id is None else read.types.get(spool.type_id)
            # Postgres's upper(), the unique index's fold, not str.upper (R-7152-10).
            key = None if row is None else row["code_key"]
            found = by_key.setdefault(key, [0, 0.0, 0.0, 0.0, None, False])
            if spool.archived_at is None:
                found[0] += 1
                found[1] += spool.grams_left or 0.0
                found[5] = True
                value = _value(spool, settings.default_cost_per_kg)
                if value is not None:
                    found[4] = (found[4] or Decimal(0)) + value
            for plate in read.plates.get(spool.id, []):
                if since <= plate.at.astimezone(UTC).date() <= until:
                    found[2] += plate.grams
                    found[3] += plate.grams if plate.outcome in ("failed", "cancelled") else 0.0
                    found[5] = True
        codes: dict[str, list[Row]] = defaultdict(list)
        for row in read.types.values():
            codes[row["code_key"]].append(row)
        rows = []
        for key, (spools, grams, used, wasted, value, listed) in by_key.items():
            if not listed:
                continue
            types = codes.get(key, []) if key is not None else []
            rows.append(
                mm.MaterialNumberRow(
                    key=key,
                    label=_NONE_LABEL["material_number"] if key is None else _code_label(types),
                    types=[
                        mm.MaterialNumberType(
                            id=t["id"], code=t["code"], archived=t["archived_at"] is not None
                        )
                        for t in sorted(types, key=lambda t: (t["code"], str(t["id"])))
                    ],
                    spools=spools,
                    grams_left=grams,
                    used_g=used,
                    wasted_g=wasted,
                    value=_money(value),
                )
            )
        return mm.MaterialNumberStats(
            currency=settings.currency,
            since=since,
            until=until,
            rows=sorted(rows, key=lambda r: (r.key is None, -r.grams_left, r.label.upper())),
        )

    def purchasing(self, since: date, until: date) -> mm.PurchasingMetrics:
        """The price per kg of each type at each supplier, one series per currency, and
        the confirmed invoices' totals per supplier, month and currency, from ``since`` to
        ``until`` (both included). Archived suppliers and products are kept: their
        prices were paid."""
        with self._pool.connection() as conn:
            prices = conn.execute(
                "SELECT pp.price, pp.currency, pp.on_date, p.pack_size, p.type_id,"
                " t.code AS type_label, t.label_weight_g, p.supplier_id, s.name AS supplier_name"
                " FROM filament_price_points pp"
                " JOIN filament_supplier_products p ON p.id = pp.product_id"
                " JOIN filament_spool_types t ON t.id = p.type_id"
                " JOIN filament_suppliers s ON s.id = p.supplier_id"
                " WHERE pp.on_date BETWEEN %s AND %s"
                " ORDER BY pp.on_date, pp.created_at, pp.id",
                (since, until),
            ).fetchall()
            spend = conn.execute(
                "SELECT i.supplier_id, s.name AS supplier_name,"
                " date_trunc('month', i.invoice_date)::date AS month, i.currency,"
                " sum(i.total) AS total, count(*)::int AS invoices"
                " FROM filament_invoices i JOIN filament_suppliers s ON s.id = i.supplier_id"
                " WHERE i.status = 'confirmed' AND i.invoice_date BETWEEN %s AND %s"
                " GROUP BY i.supplier_id, s.name, month, i.currency"
                " ORDER BY month, upper(s.name), i.supplier_id, i.currency",
                (since, until),
            ).fetchall()
        series: dict[tuple[UUID, UUID, str], mm.PriceSeries] = {}
        for row in prices:
            found = series.setdefault(
                (row["type_id"], row["supplier_id"], row["currency"]),
                mm.PriceSeries(
                    type_id=row["type_id"],
                    type_label=row["type_label"],
                    supplier_id=row["supplier_id"],
                    supplier_name=row["supplier_name"],
                    currency=row["currency"],
                    points=[],
                ),
            )
            found.points.append(
                mm.PricePerKg(
                    on_date=row["on_date"],
                    price_per_kg=price_per_kg(
                        row["price"], row["pack_size"], row["label_weight_g"]
                    ),
                )
            )
        return mm.PurchasingMetrics(
            prices=sorted(
                series.values(),
                key=lambda s: (s.type_label.upper(), s.supplier_name.upper(), s.currency),
            ),
            spend=[mm.SpendRow.model_validate(row) for row in spend],
        )

    def snapshot(self) -> mm.Snapshot:
        """What the gauges read now, by raw catalog name (`Snapshot`)."""
        settings = self._settings()
        read = self._read()
        now = datetime.now(UTC)
        stocked = read.stocked()
        stock: dict[tuple[str | None, str | None, str | None], list[Any]] = {}
        for spool in stocked:
            labels = (spool.names.material, spool.names.brand, read.kinds.get(spool.id))
            row = stock.setdefault(labels, [0, 0.0, None])
            row[0] += 1
            row[1] += spool.grams_left or 0.0
            value = _value(spool, settings.default_cost_per_kg)
            if value is not None:
                row[2] = (row[2] or Decimal(0)) + value
        used: dict[tuple[str | None, mm.Outcome], float] = defaultdict(float)
        by_material: dict[str | None, list[m.Spool]] = defaultdict(list)
        for spool in read.spools:
            by_material[spool.names.material].append(spool)
            for plate in read.plates.get(spool.id, []):
                used[(spool.names.material, plate.outcome)] += plate.grams
        in_stock = {s.names.material for s in stocked}
        burn: dict[str | None, float] = {}
        days: dict[str | None, float] = {}
        for material, group in by_material.items():
            rate = _union_burn(group, read.plates, settings.window_days, now) or 0.0
            if material not in in_stock and rate <= 0:
                continue
            burn[material] = rate
            grams = sum(s.grams_left or 0.0 for s in group if s.archived_at is None)
            found = _days_of_stock(grams, rate)
            if found is not None:
                days[material] = found
        low = dict.fromkeys(in_stock, 0)
        for spool in stocked:
            low[spool.names.material] += spool.low_stock
        reorder = dict.fromkeys(in_stock, 0)
        # A type counts under its stocked spools' (else all spools') material with most grams,
        # ties by name, so the pick never depends on list order.
        type_material: dict[UUID, str | None] = {}
        for pool in (stocked, read.spools):
            by_grams: dict[UUID, dict[str | None, float]] = defaultdict(lambda: defaultdict(float))
            for spool in pool:
                if spool.type_id is not None:
                    by_grams[spool.type_id][spool.names.material] += spool.grams_left or 0.0
            for type_id, by_name in by_grams.items():
                type_material.setdefault(
                    type_id, min(by_name, key=lambda n: (-by_name[n], n or ""))
                )
        for kind in _type_forecasts(read, settings, now):
            if kind.reorder_now:
                name = type_material.get(kind.type_id, kind.material)
                reorder[name] = reorder.get(name, 0) + 1
        return mm.Snapshot(
            stock=[(*k, r[0], r[1], _money(r[2])) for k, r in stock.items()],
            used=dict(used),
            burn=burn,
            days_of_stock=days,
            low_stock=low,
            reorder=reorder,
            currency=settings.currency,
        )

    def _read(self) -> _Read:
        """Every spool and its plates, place kind and supplier. The spool list takes a
        connection of its own, so it is read before this one is taken."""
        spools = self._spools.list(include_archived=True)
        with self._pool.connection() as conn:
            extra = conn.execute(
                "SELECT s.id, k.name AS kind, i.supplier_id, sup.name AS supplier_name"
                " FROM filament_spools s"
                " LEFT JOIN filament_locations l ON l.id = s.location_id"
                " LEFT JOIN filament_location_kinds k ON k.id = l.kind_id"
                " LEFT JOIN filament_invoice_lines il ON il.id = s.invoice_line_id"
                " LEFT JOIN filament_invoices i ON i.id = il.invoice_id AND i.status = 'confirmed'"
                " LEFT JOIN filament_suppliers sup ON sup.id = i.supplier_id"
            ).fetchall()
            events = ledger.plate_events(conn)
            types = conn.execute(
                "SELECT t.id, t.code, upper(t.code) AS code_key, t.lead_time_days,"
                " t.safety_margin_days, t.archived_at, mat.name AS material"
                " FROM filament_spool_types t"
                " LEFT JOIN filament_materials mat ON mat.id = t.material_id"
            ).fetchall()
            printers = conn.execute("SELECT id, name FROM printers").fetchall()
        return _Read(
            spools=spools,
            plates={spool_id: metrics.plates(found) for spool_id, found in events.items()},
            kinds={r["id"]: r["kind"] for r in extra},
            suppliers={
                r["id"]: (r["supplier_id"], r["supplier_name"])
                for r in extra
                if r["supplier_id"] is not None
            },
            types={r["id"]: r for r in types},
            printers={r["id"]: r["name"] for r in printers},
        )


def _value(spool: m.Spool, default_cost: Decimal | None) -> Decimal | None:
    """The spool's grams left at its effective cost per kg, else the default's."""
    cost = spool.effective.cost_per_kg
    if cost is None:
        cost = default_cost
    if cost is None or spool.grams_left is None:
        return None
    return Decimal(str(spool.grams_left)) / 1000 * cost


def _key(read: _Read, spool: m.Spool, dimension: str) -> tuple[str | None, str]:
    """The spool's id and label in ``dimension``."""
    found: tuple[UUID | None, str | None]
    if dimension == "material":
        found = (spool.effective.material_id, spool.names.material)
    elif dimension == "brand":
        found = (spool.effective.brand_id, spool.names.brand)
    elif dimension == "type":
        row = None if spool.type_id is None else read.types.get(spool.type_id)
        found = (spool.type_id, None if row is None else row["code"])
    elif dimension == "supplier":
        found = read.suppliers.get(spool.id, (None, None))
    else:
        found = (spool.location_id, spool.names.location)
    key, label = found
    if key is None:
        return None, _NONE_LABEL[dimension]
    return str(key), label or str(key)


def _code_label(types: list[Row]) -> str:
    """A material number's label: its live type's code, else the newest archived one's."""
    live = [t for t in types if t["archived_at"] is None]
    if live:
        return str(live[0]["code"])
    return str(max(types, key=lambda t: (t["archived_at"], str(t["id"])))["code"])


def _printer(read: _Read, printer_id: UUID | None) -> tuple[str | None, str]:
    if printer_id is None:
        return None, _NONE_LABEL["printer"]
    return str(printer_id), read.printers.get(printer_id, str(printer_id))


def _type_forecasts(read: _Read, settings: MetricsSettings, now: datetime) -> list[mm.TypeForecast]:
    by_type: dict[UUID, list[m.Spool]] = defaultdict(list)
    for spool in read.spools:
        if spool.type_id is not None:
            by_type[spool.type_id].append(spool)
    today = now.date()
    found = []
    for type_id, group in by_type.items():
        row = read.types.get(type_id)
        stocked = [s for s in group if s.archived_at is None]
        if row is None or (row["archived_at"] is not None and not stocked):
            continue
        burn = _union_burn(group, read.plates, settings.window_days, now)
        grams = sum(s.grams_left or 0.0 for s in stocked)
        empty = metrics.empty_on(grams, burn, now)
        lead = row["lead_time_days"]
        lead = settings.lead_time_days if lead is None else lead
        margin = row["safety_margin_days"]
        margin = settings.safety_margin_days if margin is None else margin
        reorder = metrics.reorder_on(empty, lead, margin)
        found.append(
            mm.TypeForecast(
                type_id=type_id,
                label=row["code"],
                material=row["material"],
                spools=len(stocked),
                grams_left=grams,
                burn_g_per_day=burn,
                days_of_stock=_days_of_stock(grams, burn),
                empty_on=empty,
                lead_time_days=lead,
                safety_margin_days=margin,
                reorder_on=reorder,
                reorder_now=reorder is not None and reorder <= today,
                low_stock=bool(stocked) and all(s.low_stock for s in stocked),
            )
        )
    return sorted(found, key=lambda t: (t.label.upper(), str(t.type_id)))


def _run_of(event: m.SpoolEvent) -> str | None:
    """The print run a `ledger.PRINT_RUN` event names first in its ``ref_id``."""
    if event.ref_kind != ledger.PRINT_RUN or not event.ref_id:
        return None
    return event.ref_id.split("/", 1)[0]


def _orders(conn: Conn, events: Iterable[m.SpoolEvent]) -> dict[str, str]:
    """The order of each run the events name, through its fulfilment attempt."""
    runs = sorted({run for e in events if (run := _run_of(e)) is not None})
    if not runs:
        return {}
    rows = conn.execute(
        "SELECT f.print_run_id, i.order_id FROM order_item_fulfilments f"
        " JOIN order_items i ON i.id = f.order_item_id WHERE f.print_run_id = ANY(%s)",
        (runs,),
    ).fetchall()
    return {r["print_run_id"]: r["order_id"] for r in rows}


def _order_of(event: m.SpoolEvent, orders: Mapping[str, str]) -> str | None:
    run = _run_of(event)
    return None if run is None else orders.get(run)


def _archive_of(event: m.SpoolEvent) -> int | None:
    found = event.data.get("bb_archive_id")
    return found if isinstance(found, int) and not isinstance(found, bool) else None


def _type_ams_error(conn: Conn, type_id: UUID) -> mm.AmsError | None:
    """The AMS error over every spool of the type, archived too, each at its own label
    weight: the readings and the plates that can break a pair."""
    rows = conn.execute(
        f"SELECT {_EVENT_COLUMNS}, coalesce(s.label_weight_g, t.label_weight_g) AS label"
        " FROM filament_spool_events e JOIN filament_spools s ON s.id = e.spool_id"
        " LEFT JOIN filament_spool_types t ON t.id = s.type_id WHERE s.type_id = %s"
        " AND (e.kind IN ('print_started', 'print_ended') OR e.source IN ('ams_remain', 'scale'))"
        " ORDER BY e.spool_id, e.at, e.seq",
        (type_id,),
    ).fetchall()
    by_spool: dict[UUID, tuple[int | None, list[m.SpoolEvent]]] = {}
    for row in rows:
        _, found = by_spool.setdefault(row["spool_id"], (row["label"], []))
        found.append(m.SpoolEvent.model_validate(row))
    errors = [e for label, found in by_spool.values() for e in metrics.ams_pairs(found, label)]
    return metrics.ams_error(errors)
