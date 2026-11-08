"""A machine rate per registry printer (#4625, order operations spec 2026-11-07 §6).

``rate = sum tasks (minutes x labour rate / 60 + consumables) / interval_hours
       + sum wear (cost / life_hours) + watts / 1000 x kWh price + purchase / life_hours``

Every amount is in the shop's ``orders_currency``, as the price book's rates are. Each
task's, part's and term's figure per hour is rounded half up to 0.0001 once
(:data:`RATE_PLACES`), and every sum adds the rounded figures, so a breakdown always adds
up to its rate. A task counts only with an interval in print-hours: Bambuddy's calendar
tasks (``interval_type`` ``days``) and its disabled ones are listed, never counted.

The inputs are three tables of the CRUD service's own (``printer_cost_profiles``,
``printer_wear_parts``, ``printer_maintenance_costs``), keyed by the registry printer. A
printer linked to Bambuddy takes its tasks and their intervals from Bambuddy's
maintenance tracker, read through the orders seam (``integrations.py``), and keeps the
last intervals read; when Bambuddy does not answer, the rate uses those and says so
(``intervals`` ``stored``). A printer only the LAN reaches has its intervals typed.
A printer with no profile is priced at the book's ``machine_rate_per_hour``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Self

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.orders.integrations import MaintenanceTask, NotLinkedError, OrderIntegrations
from scadbuddy.orders.models import (
    IntervalSource,
    MachineRateBreakdown,
    MaintenancePart,
    NotCounted,
    WearPartRate,
    _places,
)
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.store import PrinterStore, printer_uuid

logger = logging.getLogger(__name__)

#: Each figure per hour is rounded to this, once.
RATE_PLACES = Decimal("0.0001")
MINUTES_PER_HOUR = Decimal(60)
WATTS_PER_KW = Decimal(1000)
#: How many wear parts or tasks one printer takes.
MAX_ROWS = 50

#: ``numeric(14, 4)``: a price or a cost.
Amount = Annotated[str, _places(4, 10**10)]
#: ``numeric(10, 1)`` above 0: a life or an interval in print-hours.
Hours = Annotated[str, _places(1, 10**9, positive=True)]
#: ``numeric(8, 1)``: watts or minutes.
Tenths = Annotated[str, _places(1, 10**7)]
#: ``numeric(10, 4)``: the price of a kWh.
KwhPrice = Annotated[str, _places(4, 10**6)]
#: ``numeric(12, 4)``: a labour rate.
LabourRate = Annotated[str, _places(4, 10**8)]


class CostProfile(BaseModel):
    """What a printer cost and costs to run (§6.1). A null ``life_hours`` is no
    depreciation; a null ``labour_rate_per_hour`` is the shop's
    ``pricing_labour_rate_per_hour``."""

    model_config = ConfigDict(extra="forbid")

    purchase_price: Amount = "0"
    life_hours: Hours | None = None
    watts: Tenths = "0"
    kwh_price: KwhPrice = "0"
    labour_rate_per_hour: LabourRate | None = None


class WearPart(BaseModel):
    """A part that wears out with print-hours: a nozzle, a plate, PTFE tube, a filter."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    cost: Amount
    life_hours: Hours


class MaintenanceCost(BaseModel):
    """One occurrence of a maintenance task: the minutes it takes and its consumables.
    ``interval_hours`` is typed for a printer Bambuddy does not track; for a linked one
    it is the last interval read from Bambuddy, and what is sent is ignored."""

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=100)
    minutes: Tenths = "0"
    consumables: Amount = "0"
    interval_hours: Hours | None = None


def _unique(names: Sequence[str], what: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"two {what} are named {name!r}")
        seen.add(name)


class MachineRateInputs(BaseModel):
    """`PUT /printers/{id}/machine-rate`: a printer's inputs, replaced whole."""

    model_config = ConfigDict(extra="forbid")

    profile: CostProfile
    wear_parts: list[WearPart] = Field(default_factory=list, max_length=MAX_ROWS)
    maintenance: list[MaintenanceCost] = Field(default_factory=list, max_length=MAX_ROWS)

    @model_validator(mode="after")
    def _names(self) -> Self:
        _unique([part.name for part in self.wear_parts], "wear parts")
        _unique([cost.task for cost in self.maintenance], "maintenance tasks")
        return self


def _text(number: Decimal) -> str:
    """The shortest decimal string of ``number``: ``0.1200`` reads ``0.12``."""
    return format(number.normalize(), "f") if number else "0"


def _rounded(number: Decimal) -> Decimal:
    return number.quantize(RATE_PLACES, rounding=ROUND_HALF_UP)


def _hours(value: float) -> str:
    """An interval Bambuddy gave as a float, as the shortest decimal string (``str``
    once: never the float's binary expansion)."""
    return _text(Decimal(str(value)))


def _task_part(
    task: str,
    interval: str | None,
    interval_type: Literal["hours", "days"],
    cost: MaintenanceCost | None,
    labour: Decimal,
    why: NotCounted | None,
) -> MaintenancePart:
    minutes = Decimal(cost.minutes) if cost else Decimal(0)
    consumables = Decimal(cost.consumables) if cost else Decimal(0)
    if why is None and interval is None:
        why = "no_interval"
    per_hour = Decimal(0)
    if why is None:
        assert interval is not None
        occurrence = minutes * labour / MINUTES_PER_HOUR + consumables
        per_hour = _rounded(occurrence / Decimal(interval))
    return MaintenancePart(
        task=task,
        interval_hours=interval,
        interval_type=interval_type,
        minutes=_text(minutes),
        consumables=_text(consumables),
        per_hour=_text(per_hour),
        counted=why is None,
        why=why,
    )


def compute(
    profile: CostProfile,
    wear: Sequence[WearPart],
    costs: Sequence[MaintenanceCost],
    tasks: Sequence[MaintenanceTask] | None,
    *,
    default_labour: str,
    currency: str,
    intervals: IntervalSource,
    printer: str | None = None,
    printer_name: str | None = None,
    intervals_read_at: datetime | None = None,
) -> MachineRateBreakdown:
    """The rate and its parts. ``tasks`` are Bambuddy's (a linked printer read now):
    the tasks are its, each with its interval, priced with the cost row of the same
    name. ``None`` prices every cost row with its own ``interval_hours`` (typed, or the
    last read from Bambuddy)."""
    labour = Decimal(profile.labour_rate_per_hour or default_labour or "0")
    parts: list[MaintenancePart] = []
    if tasks is None:
        for cost in costs:
            parts.append(_task_part(cost.task, cost.interval_hours, "hours", cost, labour, None))
    else:
        by_task = {cost.task: cost for cost in costs}
        for task in tasks:
            why: NotCounted | None = None
            if not task.enabled:
                why = "disabled"
            elif task.interval_type == "days":
                why = "calendar"
            elif task.interval_hours <= 0:
                why = "no_interval"
            interval = _hours(task.interval_hours) if task.interval_hours > 0 else None
            parts.append(
                _task_part(
                    task.task,
                    interval,
                    task.interval_type,
                    by_task.pop(task.task, None),
                    labour,
                    why,
                )
            )
        for cost in by_task.values():
            parts.append(
                _task_part(cost.task, cost.interval_hours, "hours", cost, labour, "not_in_bambuddy")
            )
    maintenance = sum((Decimal(part.per_hour) for part in parts), Decimal(0))

    wear_rates = [
        WearPartRate(
            name=part.name,
            cost=_text(Decimal(part.cost)),
            life_hours=_text(Decimal(part.life_hours)),
            per_hour=_text(_rounded(Decimal(part.cost) / Decimal(part.life_hours))),
        )
        for part in wear
    ]
    wear_total = sum((Decimal(part.per_hour) for part in wear_rates), Decimal(0))
    power = _rounded(Decimal(profile.watts) / WATTS_PER_KW * Decimal(profile.kwh_price))
    depreciation = (
        _rounded(Decimal(profile.purchase_price) / Decimal(profile.life_hours))
        if profile.life_hours is not None
        else Decimal(0)
    )
    return MachineRateBreakdown(
        printer=printer,
        printer_name=printer_name,
        source="profile",
        rate_per_hour=_text(maintenance + wear_total + power + depreciation),
        currency=currency,
        labour_rate_per_hour=_text(labour),
        maintenance=parts,
        maintenance_per_hour=_text(maintenance),
        wear=wear_rates,
        wear_per_hour=_text(wear_total),
        watts=_text(Decimal(profile.watts)),
        kwh_price=_text(Decimal(profile.kwh_price)),
        power_per_hour=_text(power),
        purchase_price=_text(Decimal(profile.purchase_price)),
        life_hours=None if profile.life_hours is None else _text(Decimal(profile.life_hours)),
        depreciation_per_hour=_text(depreciation),
        intervals=intervals,
        intervals_read_at=intervals_read_at,
    )


def _number(value: Decimal | None) -> str | None:
    return None if value is None else _text(value)


@dataclass
class Stored:
    """A printer's rows: its profile (None for none), parts and task costs, and when its
    intervals were last read from Bambuddy (the latest of its rows')."""

    profile: CostProfile | None
    wear: list[WearPart]
    costs: list[MaintenanceCost]
    read_at: datetime | None


class MachineRateStore:
    """The three tables, plain CRUD (the CRUD service's own, #5295)."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    def stored(self, printer: str) -> Stored:
        key = printer_uuid(printer)
        if key is None:
            return Stored(None, [], [], None)
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT purchase_price, life_hours, watts, kwh_price, labour_rate_per_hour"
                " FROM printer_cost_profiles WHERE printer = %s",
                (key,),
            ).fetchone()
            parts = conn.execute(
                "SELECT name, cost, life_hours FROM printer_wear_parts WHERE printer = %s"
                " ORDER BY position",
                (key,),
            ).fetchall()
            rows = conn.execute(
                "SELECT task, minutes, consumables, interval_hours, interval_read_at"
                " FROM printer_maintenance_costs WHERE printer = %s ORDER BY task",
                (key,),
            ).fetchall()
        profile = None
        if row is not None:
            profile = CostProfile.model_validate({k: _number(v) for k, v in row.items()})
        wear = [
            WearPart(name=r["name"], cost=_text(r["cost"]), life_hours=_text(r["life_hours"]))
            for r in parts
        ]
        costs = [
            MaintenanceCost(
                task=r["task"],
                minutes=_text(r["minutes"]),
                consumables=_text(r["consumables"]),
                interval_hours=_number(r["interval_hours"]),
            )
            for r in rows
        ]
        read = [r["interval_read_at"] for r in rows if r["interval_read_at"] is not None]
        return Stored(profile, wear, costs, max(read) if read else None)

    def profiled(self) -> list[str]:
        """Every printer with a profile."""
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT printer FROM printer_cost_profiles").fetchall()
        return [str(row["printer"]) for row in rows]

    def replace(self, printer: str, body: MachineRateInputs) -> None:
        """The printer's inputs, replaced whole in one transaction. A task whose interval
        was read from Bambuddy keeps it (and when it was read): what is sent for it is
        ignored."""
        key = printer_uuid(printer)
        assert key is not None
        profile = body.profile
        with self._pool.connection() as conn, conn.transaction():
            read = {
                r["task"]: (r["interval_hours"], r["interval_read_at"])
                for r in conn.execute(
                    "SELECT task, interval_hours, interval_read_at FROM printer_maintenance_costs"
                    " WHERE printer = %s AND interval_read_at IS NOT NULL",
                    (key,),
                ).fetchall()
            }
            conn.execute(
                "INSERT INTO printer_cost_profiles (printer, purchase_price, life_hours, watts,"
                " kwh_price, labour_rate_per_hour) VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (printer) DO UPDATE SET purchase_price = EXCLUDED.purchase_price,"
                " life_hours = EXCLUDED.life_hours, watts = EXCLUDED.watts,"
                " kwh_price = EXCLUDED.kwh_price,"
                " labour_rate_per_hour = EXCLUDED.labour_rate_per_hour, updated_at = now()",
                (
                    key,
                    profile.purchase_price,
                    profile.life_hours,
                    profile.watts,
                    profile.kwh_price,
                    profile.labour_rate_per_hour,
                ),
            )
            conn.execute("DELETE FROM printer_wear_parts WHERE printer = %s", (key,))
            for position, part in enumerate(body.wear_parts):
                conn.execute(
                    "INSERT INTO printer_wear_parts (printer, position, name, cost, life_hours)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    (key, position, part.name, part.cost, part.life_hours),
                )
            conn.execute("DELETE FROM printer_maintenance_costs WHERE printer = %s", (key,))
            for cost in body.maintenance:
                interval, read_at = read.get(cost.task, (cost.interval_hours, None))
                conn.execute(
                    "INSERT INTO printer_maintenance_costs (printer, task, minutes, consumables,"
                    " interval_hours, interval_read_at) VALUES (%s, %s, %s, %s, %s, %s)",
                    (key, cost.task, cost.minutes, cost.consumables, interval, read_at),
                )

    def delete(self, printer: str) -> bool:
        """Every input of the printer gone; whether it had a profile."""
        key = printer_uuid(printer)
        if key is None:
            return False
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM printer_wear_parts WHERE printer = %s", (key,))
            conn.execute("DELETE FROM printer_maintenance_costs WHERE printer = %s", (key,))
            gone = conn.execute("DELETE FROM printer_cost_profiles WHERE printer = %s", (key,))
            return gone.rowcount > 0

    def remember_intervals(self, printer: str, tasks: Sequence[MaintenanceTask]) -> None:
        """Bambuddy's intervals kept on the printer's task rows of the same name, for
        when it does not answer. Only a counted task's: a calendar task's interval is
        days, not print-hours."""
        key = printer_uuid(printer)
        if key is None:
            return
        with self._pool.connection() as conn, conn.transaction():
            for task in tasks:
                if task.enabled and task.interval_type == "hours" and task.interval_hours > 0:
                    conn.execute(
                        "UPDATE printer_maintenance_costs SET interval_hours = %s,"
                        " interval_read_at = now() WHERE printer = %s AND task = %s",
                        (_hours(task.interval_hours), key, task.task),
                    )


class MachineRateView(BaseModel):
    """`GET /printers/{id}/machine-rate`: a printer's inputs (null with no profile) and
    the rate they give now, with each part."""

    printer: str
    printer_name: str
    #: The Bambuddy printer it is linked to, whose maintenance tasks it takes.
    bambuddy_printer_id: int | None = None
    inputs: MachineRateInputs | None = None
    breakdown: MachineRateBreakdown


class MachineRates:
    """The rate of a registry printer, as a quote, Settings and the money view read it."""

    def __init__(
        self,
        store: MachineRateStore,
        printers: PrinterStore,
        integrations: OrderIntegrations,
        settings: SettingsStore,
    ) -> None:
        self.store = store
        self.printers = printers
        self.integrations = integrations
        self.settings = settings

    async def breakdown(self, row: PrinterRow, *, live: bool = True) -> MachineRateBreakdown:
        """The printer's rate now. ``live`` reads a linked printer's tasks from Bambuddy
        (and keeps their intervals); without it, or when Bambuddy does not answer, the
        stored intervals price it (``intervals`` ``stored``). A printer with no profile
        is the book's rate."""
        runtime = (await asyncio.to_thread(self.settings.snapshot)).runtime
        stored = await asyncio.to_thread(self.store.stored, row.id)
        if stored.profile is None:
            return MachineRateBreakdown(
                printer=row.id,
                printer_name=row.name,
                source="price_book",
                rate_per_hour=runtime.pricing_machine_rate_per_hour,
                currency=runtime.orders_currency,
            )
        tasks: list[MaintenanceTask] | None = None
        intervals: IntervalSource = "typed"
        if row.bambuddy_printer_id is not None:
            intervals = "stored"
            if live and self.integrations.linked:
                try:
                    tasks = await self.integrations.maintenance_tasks(row.bambuddy_printer_id)
                except (ApiError, NotLinkedError):
                    logger.warning(
                        "Bambuddy's maintenance tasks could not be read; the stored"
                        " intervals price the machine rate",
                        extra={"printer": row.id},
                    )
                else:
                    intervals = "bambuddy"
                    await asyncio.to_thread(self.store.remember_intervals, row.id, tasks)
        return compute(
            stored.profile,
            stored.wear,
            stored.costs,
            tasks,
            default_labour=runtime.pricing_labour_rate_per_hour,
            currency=runtime.orders_currency,
            intervals=intervals,
            printer=row.id,
            printer_name=row.name,
            intervals_read_at=stored.read_at if intervals == "stored" else None,
        )

    def stored_rates(self) -> dict[str, str]:
        """Every profiled printer's rate from its stored intervals, by registry id: no
        Bambuddy call, for the money view (job cost spec §4.1). Blocking."""
        runtime = self.settings.snapshot().runtime
        rates: dict[str, str] = {}
        for printer in self.store.profiled():
            stored = self.store.stored(printer)
            if stored.profile is None:
                continue
            rates[printer] = compute(
                stored.profile,
                stored.wear,
                stored.costs,
                None,
                default_labour=runtime.pricing_labour_rate_per_hour,
                currency=runtime.orders_currency,
                intervals="stored",
            ).rate_per_hour
        return rates

    def money_rates(self) -> tuple[str, dict[str, str]]:
        """The money view's machine rates (job cost spec §4.1): for an attempt with no
        quote's rate, its printer's rate (:meth:`stored_rates`), else the configured
        default printer's, else the book's ``machine_rate_per_hour``. Blocking."""
        rates = self.stored_rates()
        fallback = self.settings.snapshot().runtime.pricing_machine_rate_per_hour
        default = self.resolve(None, None)
        if default is not None and default.id in rates:
            fallback = rates[default.id]
        return fallback, rates

    def resolve(self, printer: str | None, printer_id: int | None) -> PrinterRow | None:
        """The registry printer a quote is priced for (§6.2): the one it names, else the
        one linked to the Bambuddy printer it names, else the configured default
        printer. None when it names none and none is configured. Blocking."""
        if printer is not None:
            row = self.printers.get(printer)
            if row is not None:
                return row
        if printer_id is not None:
            return self.printers.by_bambuddy_id(printer_id)
        stored = self.settings.load()
        if stored.printer_id is not None:
            return self.printers.by_bambuddy_id(stored.printer_id)
        if stored.default_printer is not None:
            return self.printers.get(stored.default_printer)
        return None

    async def for_quote(
        self, printer: str | None, printer_id: int | None
    ) -> MachineRateBreakdown | None:
        """The rate a slice quote prices machine time at, for the printer it sliced
        for; None when no printer is named or configured (the book's rate prices it)."""
        row = await asyncio.to_thread(self.resolve, printer, printer_id)
        return None if row is None else await self.breakdown(row)

    async def view(self, printer: str) -> MachineRateView | None:
        """None for a printer the registry does not hold."""
        row = await asyncio.to_thread(self.printers.get, printer)
        if row is None:
            return None
        stored = await asyncio.to_thread(self.store.stored, row.id)
        inputs = None
        if stored.profile is not None:
            inputs = MachineRateInputs(
                profile=stored.profile, wear_parts=stored.wear, maintenance=stored.costs
            )
        return MachineRateView(
            printer=row.id,
            printer_name=row.name,
            bambuddy_printer_id=row.bambuddy_printer_id,
            inputs=inputs,
            breakdown=await self.breakdown(row),
        )
