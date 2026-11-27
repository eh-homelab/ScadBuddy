"""A spool's metrics from its events (#6269, spec §5.7), with no database: its plates
(plan Decision 6), the burn rate and the forecast (Decisions 8, 9), the AMS reading error
(Decision 14) and the grams-left series (Decision 13). A type's or a material's burn is
`burn` over the union of its spools' plates, born at the oldest spool's ``created_at``.

A plate is counted once, by the ledger's rule 3 (`ledger.pairs`): when BB's usage row and
SB's events of one job are on the spool, BB's row is the plate. Otherwise SB's
``print_ended`` is the plate: ``covered`` is skipped (BB booked its grams on another
spool), ``not_applied`` is a failed plate of 0 g. A ``print_started`` with no end (a print
still running) is not a plate yet.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import ceil, isfinite, pi

from scadbuddy.bambuddy.stages import stage_of
from scadbuddy.filament import ledger
from scadbuddy.filament.metrics_models import AmsError, GramsPoint, Outcome, Plate, SpoolMetrics
from scadbuddy.filament.models import SpoolEvent

#: A scale reading pairs with an AMS reading at most this much older (Decision 14).
AMS_PAIR_WINDOW = timedelta(hours=24)
#: A forecast further out than this many days is no forecast (``empty_on`` is ``None``).
MAX_FORECAST_DAYS = 3650
#: BB's ``bb_status`` through ``stages.stage_of`` (its words differ per object: ``canceled``,
#: ``aborted``, ``complete``); a status that is neither done, failed nor cancelled is ``unknown``.
_STAGE_OUTCOMES: dict[str, Outcome] = {
    "done": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}
#: #7685 (spec §5.6): the prints' implied density and the type's are a mismatch when
#: apart by more than this fraction of the type's. A sentence on the page, never a booking.
DENSITY_TOLERANCE = 0.05
#: The diameter a spool with none known is read at (the catalog's own default).
DEFAULT_DIAMETER_MM = 1.75
_PLATE_KINDS = frozenset({ledger.PRINT_STARTED, ledger.PRINT_ENDED})
_CENT = Decimal("0.01")
_DAY = 86400.0


def _ordered(events: Sequence[SpoolEvent]) -> list[SpoolEvent]:
    return sorted(events, key=lambda e: (e.at, e.seq))


def _grams(event: SpoolEvent) -> float:
    return -event.delta_g if event.delta_g else 0.0


def _length(event: SpoolEvent) -> float | None:
    """The plate's metres from its end's ``data.length_m`` (#7685); ``None`` without one."""
    metres = event.data.get("length_m")
    if isinstance(metres, (int, float)) and not isinstance(metres, bool):
        return float(metres)
    return None


def plates(events: Sequence[SpoolEvent]) -> list[Plate]:
    """The spool's plates, oldest first, each counted once (module docstring)."""
    paired = ledger.pairs(events)
    # #7685: Bambuddy's usage rows carry no length, so a paired plate takes the length of
    # ScadBuddy's own ends of the job, summed: one spool can feed several slots of a job,
    # each booked as an end of its own (`record_uses_in`, one event per slot).
    sb_lengths: dict[str, float] = {}
    for event in events:
        if event.kind != ledger.PRINT_ENDED or event.ref_kind != ledger.PRINT_RUN:
            continue
        sb_job = ledger.job_of(event)
        sb_length = _length(event)
        if sb_job is not None and sb_length is not None:
            sb_lengths[sb_job] = sb_lengths.get(sb_job, 0.0) + sb_length
    found: list[Plate] = []
    for event in _ordered(events):
        if event.kind != ledger.PRINT_ENDED:
            continue
        job = ledger.job_of(event)
        grams = _grams(event)
        length: float | None = None
        if event.ref_kind == ledger.BB_USAGE:
            status = event.data.get("bb_status")
            outcome = _STAGE_OUTCOMES.get(
                stage_of(status if isinstance(status, str) else None), "unknown"
            )
            length = sb_lengths.get(job) if job is not None else None
        elif event.ref_kind == ledger.PRINT_RUN and job not in paired:
            basis = event.data.get("basis")
            if basis == "covered":
                continue
            outcome = "failed" if basis == "not_applied" else "completed"
            grams = 0.0 if basis == "not_applied" else grams
            length = _length(event)
        else:
            continue
        found.append(
            Plate(
                job=job,
                at=event.at,
                grams=grams,
                outcome=outcome,
                printer_id=event.printer_id,
                event_id=event.id,
                length_m=length,
            )
        )
    return found


def implied_density(grams: float, metres: float, diameter_mm: float) -> float | None:
    """The density ``grams`` over ``metres`` of filament of ``diameter_mm`` implies, in
    g/cm³ (spec §5.6: ``grams / (metres * 100 * pi * (diameter_mm / 20) ** 2)``; 1 m of
    1.75 mm at 1.24 g/cm³ is 2.98 g). ``None`` when either is 0 or less."""
    if grams <= 0 or metres <= 0 or diameter_mm <= 0:
        return None
    volume_cm3 = metres * 100 * pi * (diameter_mm / 20) ** 2
    return grams / volume_cm3


def burn(
    plates: Sequence[Plate], *, window_days: int, born: datetime, now: datetime
) -> float | None:
    """Grams per day: the grams of the plates that ended in the last ``window_days``,
    over the window, or over the age since ``born`` when that is shorter (never less than
    one day). ``None`` when no plate ended in the window."""
    start = now - timedelta(days=window_days)
    inside = [p.grams for p in plates if start < p.at <= now]
    if not inside:
        return None
    age = (now - born).total_seconds() / _DAY
    return sum(inside) / max(1.0, min(float(window_days), age))


def empty_on(grams_left: float | None, burn_g_per_day: float | None, now: datetime) -> date | None:
    """The day the spool runs out at ``burn_g_per_day``; ``None`` with no grams left
    known, no burn (0 or less), a value that is not finite, or a day more than
    `MAX_FORECAST_DAYS` after ``now``. The bound is checked before any date arithmetic,
    which overflows near year 9999."""
    if grams_left is None or burn_g_per_day is None:
        return None
    if not (isfinite(grams_left) and isfinite(burn_g_per_day)) or burn_g_per_day <= 0:
        return None
    left_days = max(grams_left, 0.0) / burn_g_per_day
    if left_days > MAX_FORECAST_DAYS:
        return None
    return (now + timedelta(days=left_days)).date()


def reorder_on(empty: date | None, lead_time_days: int, safety_margin_days: int) -> date | None:
    """The day to reorder: the empty day less the lead time and the safety margin. It
    may be in the past; the caller reads that as "reorder now"."""
    if empty is None:
        return None
    return empty - timedelta(days=lead_time_days + safety_margin_days)


def ams_pairs(events: Sequence[SpoolEvent], label_weight_g: int | None) -> list[float]:
    """AMS grams less scale grams, one per scale reading that has an AMS reading to pair:
    the newest ``ams_remain`` reading before it that is not ``printing``, at most
    `AMS_PAIR_WINDOW` older, with no plate event that consumed grams (more than 0 g)
    between them."""
    errors: list[float] = []
    ams: tuple[datetime, float] | None = None
    for event in _ordered(events):
        if event.kind in _PLATE_KINDS:
            if event.delta_g:
                ams = None
        elif event.source == "ams_remain" and event.data.get("printing") is not True:
            value = ledger.reading(event, label_weight_g)
            if value is not None:
                ams = (event.at, value)
        elif (
            event.source == "scale"
            and event.grams_after is not None
            and ams is not None
            and event.at - ams[0] <= AMS_PAIR_WINDOW
        ):
            errors.append(ams[1] - event.grams_after)
    return errors


def ams_error(errors: Sequence[float]) -> AmsError | None:
    """The pair count, mean absolute error and mean signed bias; ``None`` with no pair."""
    if not errors:
        return None
    return AmsError(
        pairs=len(errors),
        mean_abs_g=sum(abs(e) for e in errors) / len(errors),
        bias_g=sum(errors) / len(errors),
    )


def series(
    events: Sequence[SpoolEvent],
    grams_left: float | None,
    *,
    weight_locked: bool,
    label_weight_g: int | None,
    limit: int = 500,
) -> list[GramsPoint]:
    """The grams left after each event, by time: the next event's ``grams_before`` (by
    ``seq``), and ``grams_left`` after the last. A point with no value is skipped. A
    point is an anchor when `ledger.is_anchor` holds for its event. Past ``limit`` points
    (at least 1), the manual and BB anchors, the plates and the last point stay, and
    every k-th of the rest (AMS anchors too), so the result never passes ``limit``."""
    by_seq = sorted(events, key=lambda e: e.seq)
    if not by_seq:
        return []
    after = [e.grams_before for e in by_seq[1:]] + [grams_left]
    found: list[tuple[SpoolEvent, GramsPoint, bool]] = []
    for event, value in zip(by_seq, after, strict=True):
        if value is None:
            continue
        anchor = ledger.is_anchor(event, weight_locked=weight_locked, label_weight_g=label_weight_g)
        keep = (anchor and event.source != "ams_remain") or event.kind in _PLATE_KINDS
        point = GramsPoint(at=event.at, grams=value, anchor=anchor, kind=event.kind)
        found.append((event, point, keep))
    found.sort(key=lambda f: (f[0].at, f[0].seq))
    if len(found) <= limit:
        return [point for _, point, _ in found]
    last = len(found) - 1
    must = [i for i, (_, _, keep) in enumerate(found) if keep and i != last]
    rest = [i for i, (_, _, keep) in enumerate(found) if not keep and i != last]
    budget = limit - 1
    if len(must) > budget:
        must, rest = _every(must, budget), []
    else:
        rest = _every(rest, budget - len(must))
    return [found[i][1] for i in sorted([*must, *rest, last])]


def _every(items: list[int], budget: int) -> list[int]:
    """Every k-th of ``items``, k as small as keeps it within ``budget``."""
    if budget <= 0 or not items:
        return []
    return items[:: ceil(len(items) / budget)]


def _money(grams: float, cost_per_kg: Decimal) -> Decimal:
    return (Decimal(str(grams)) / 1000 * cost_per_kg).quantize(_CENT)


def _days(since: datetime | None, now: datetime) -> float | None:
    return None if since is None else (now - since).total_seconds() / _DAY


def spool_metrics(
    events: Sequence[SpoolEvent],
    *,
    grams_left: float | None,
    cost_per_kg: Decimal | None,
    created_at: datetime,
    last_used_at: datetime | None,
    hours_outside_dry: float,
    label_weight_g: int | None,
    window_days: int,
    now: datetime,
    diameter_mm: float = DEFAULT_DIAMETER_MM,
    density_g_cm3: float | None = None,
) -> SpoolMetrics:
    """One spool's metrics. "Opened" is its first ``loaded`` event, else its first plate
    (Decision 15). Money is quantized to the cent. The consumed counter counts the plates
    after the newest ``consumed_reset`` (#7149 R-7149-4); the other figures are lifetime.
    The metres and the implied density (#7685) are over the plates that carry a length
    over 0 m (a failed plate's 0 m counts for nothing), at ``diameter_mm``, against
    ``density_g_cm3``, the spool's effective type density."""
    found = plates(events)
    used = sum(p.grams for p in found)
    lengthed = [p for p in found if p.length_m is not None and p.length_m > 0]
    metres = sum(p.length_m or 0.0 for p in lengthed) if lengthed else None
    implied = (
        None
        if metres is None
        else implied_density(sum(p.grams for p in lengthed), metres, diameter_mm)
    )
    mismatch = (
        implied is not None
        and density_g_cm3 is not None
        and density_g_cm3 > 0
        and abs(implied / density_g_cm3 - 1) > DENSITY_TOLERANCE
    )
    resets = [e for e in _ordered(events) if e.kind == ledger.CONSUMED_RESET]
    since = resets[-1].at if resets else None
    consumed = sum(p.grams for p in found if since is None or p.at > since)
    rate = burn(found, window_days=window_days, born=created_at, now=now)
    loaded = next((e.at for e in _ordered(events) if e.kind == "loaded"), None)
    opened = loaded if loaded is not None else (found[0].at if found else None)
    return SpoolMetrics(
        used_g=used,
        consumed_g=consumed,
        consumed_since=since,
        prints=len(found),
        grams_per_print=used / len(found) if found else None,
        wasted_g=sum(p.grams for p in found if p.outcome in ("failed", "cancelled")),
        cost_used=None if cost_per_kg is None else _money(used, cost_per_kg),
        value_left=(
            None if cost_per_kg is None or grams_left is None else _money(grams_left, cost_per_kg)
        ),
        burn_g_per_day=rate,
        empty_on=empty_on(grams_left, rate, now),
        opened_at=opened,
        days_since_opened=_days(opened, now),
        days_since_last_use=_days(last_used_at, now),
        hours_outside_dry=hours_outside_dry,
        ams_error=ams_error(ams_pairs(events, label_weight_g)),
        used_m=metres,
        implied_density_g_cm3=implied,
        density_g_cm3=density_g_cm3,
        density_mismatch=mismatch,
    )
