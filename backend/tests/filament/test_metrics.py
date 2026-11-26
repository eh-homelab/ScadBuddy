"""Plates, spool metrics, the AMS reading error and the grams series (#6269, no database)."""

from __future__ import annotations

import itertools
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from scadbuddy.filament import metrics
from scadbuddy.filament.metrics_models import AmsError, Plate
from scadbuddy.filament.models import SpoolEvent

NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)
SPOOL = UUID(int=1)
PRINTER = UUID(int=7)

_SEQ = itertools.count(1)


def event(at: datetime, kind: str, source: str | None = None, **over: Any) -> SpoolEvent:
    values: dict[str, Any] = {
        "id": uuid4(),
        "seq": next(_SEQ),
        "spool_id": SPOOL,
        "at": at,
        "kind": kind,
        "source": source,
        "printer_id": None,
        "ams_id": None,
        "tray_id": None,
        "grams_before": None,
        "grams_after": None,
        "delta_g": None,
        "ref_kind": None,
        "ref_id": None,
        "actor": None,
        "data": {},
    }
    return SpoolEvent.model_validate(values | over)


def days(n: float) -> datetime:
    return NOW - timedelta(days=n)


def sb_start(at: datetime, grams: float, job: str) -> SpoolEvent:
    return event(
        at,
        "print_started",
        "print_estimate",
        delta_g=-grams,
        ref_kind="print_run",
        ref_id=f"run/{job}/1",
        data={"job": job},
    )


def sb_end(
    at: datetime, grams: float, job: str, basis: str = "sliced", length_m: float | None = None
) -> SpoolEvent:
    data: dict[str, Any] = {"job": job, "basis": basis}
    if length_m is not None:
        data["length_m"] = length_m
    return event(
        at,
        "print_ended",
        "print_actual",
        delta_g=-grams,
        ref_kind="print_run",
        ref_id=f"run/{job}/1",
        data=data,
    )


def bb_end(at: datetime, grams: float, job: str, status: str = "completed") -> SpoolEvent:
    return event(
        at,
        "print_ended",
        "print_actual",
        delta_g=-grams,
        ref_kind="bb_usage",
        ref_id=str(next(_SEQ)),
        printer_id=PRINTER,
        data={"job": job, "basis": "bb_usage", "bb_status": status},
    )


def ams(at: datetime, grams: float, *, printing: bool = False) -> SpoolEvent:
    return event(at, "ams_reading", "ams_remain", grams_after=grams, data={"printing": printing})


def scale(at: datetime, grams: float) -> SpoolEvent:
    return event(at, "weighed", "scale", grams_after=grams)


def plate(at: datetime, grams: float) -> Plate:
    return Plate(
        job="j", at=at, grams=grams, outcome="completed", printer_id=None, event_id=uuid4()
    )


def test_a_paired_plate_counts_once() -> None:
    bb = bb_end(days(1), 28, "a")
    events = [sb_start(days(1.1), 30, "a"), sb_end(days(1), 30, "a"), bb]
    assert metrics.plates(events) == [
        Plate(
            job="a", at=bb.at, grams=28.0, outcome="completed", printer_id=PRINTER, event_id=bb.id
        )
    ]


def test_bb_status_sets_the_outcome() -> None:
    events = [
        bb_end(days(3), 1, "a", "failed"),
        bb_end(days(2), 1, "b", "cancelled"),
        bb_end(days(1), 1, "c", "aborted"),
        bb_end(days(0.5), 1, "d", "Canceled"),
        bb_end(days(0.4), 1, "e", "complete"),
        bb_end(days(0.3), 1, "f", "printing"),
    ]
    assert [p.outcome for p in metrics.plates(events)] == [
        "failed",
        "cancelled",
        "cancelled",
        "cancelled",
        "completed",
        "unknown",
    ]


def test_not_applied_is_a_failed_plate_of_zero() -> None:
    events = [sb_start(days(2), 30, "a"), sb_end(days(1), 0, "a", "not_applied")]
    [found] = metrics.plates(events)
    assert (found.grams, found.outcome) == (0.0, "failed")


def test_covered_plate_is_skipped() -> None:
    events = [sb_start(days(2), 30, "a"), sb_end(days(1), 0, "a", "covered")]
    assert metrics.plates(events) == []


def test_open_estimate_is_not_used() -> None:
    assert metrics.plates([sb_start(days(1), 30, "a")]) == []


def test_burn_over_the_window() -> None:
    plates = [plate(days(10), 60), plate(days(20), 30), plate(days(40), 100)]
    assert metrics.burn(plates, window_days=30, born=days(90), now=NOW) == 3.0


def test_young_spool_burns_over_its_age() -> None:
    assert metrics.burn([plate(days(1), 50)], window_days=30, born=days(5), now=NOW) == 10.0
    young = NOW - timedelta(hours=6)
    assert metrics.burn([plate(NOW, 50)], window_days=30, born=young, now=NOW) == 50.0


def test_no_plate_in_the_window_is_no_burn() -> None:
    assert metrics.burn([plate(days(40), 50)], window_days=30, born=days(90), now=NOW) is None


def test_empty_on() -> None:
    assert metrics.empty_on(300, 10, NOW) == date(2026, 12, 18)
    assert metrics.empty_on(300, None, NOW) is None
    assert metrics.empty_on(300, 0, NOW) is None
    assert metrics.empty_on(None, 10, NOW) is None


def test_empty_on_is_none_past_the_forecast_bound() -> None:
    assert metrics.empty_on(1000, 1000 / metrics.MAX_FORECAST_DAYS, NOW) is not None
    assert metrics.empty_on(1000, 1000 / (metrics.MAX_FORECAST_DAYS + 1), NOW) is None
    tiny = metrics.burn([plate(days(1), 0.1)], window_days=365, born=days(400), now=NOW)
    assert tiny is not None
    assert metrics.empty_on(1000, tiny, NOW) is None
    assert metrics.empty_on(1000, 5e-324, NOW) is None


def test_empty_on_is_none_for_a_value_that_is_not_finite() -> None:
    nan, inf = float("nan"), float("inf")
    assert metrics.empty_on(nan, 10, NOW) is None
    assert metrics.empty_on(inf, 10, NOW) is None
    assert metrics.empty_on(300, nan, NOW) is None
    assert metrics.empty_on(300, inf, NOW) is None


def test_a_window_of_zero_gram_plates_has_burn_zero_and_no_forecast() -> None:
    zero = [plate(days(1), 0.0), plate(days(2), 0.0)]
    rate = metrics.burn(zero, window_days=30, born=days(90), now=NOW)
    assert rate == 0.0
    assert metrics.empty_on(500, rate, NOW) is None
    ended = sb_end(days(1), 0, "a", basis="not_applied")
    found = metrics.plates([ended])
    assert [p.grams for p in found] == [0.0]
    assert metrics.burn(found, window_days=30, born=days(90), now=NOW) == 0.0


def test_reorder_date_in_the_past_is_now() -> None:
    empty = NOW.date() + timedelta(days=5)
    assert metrics.reorder_on(empty, 7, 3) == NOW.date() - timedelta(days=5)
    assert metrics.reorder_on(None, 7, 3) is None


def test_ams_error_pairs_the_newest_reading_before_a_scale() -> None:
    t0, t1, t2 = days(1), days(0.9), days(0.5)
    assert metrics.ams_pairs([ams(t0, 520), ams(t1, 510), scale(t2, 500)], 1000) == [10.0]
    between = sb_end(days(0.7), 5, "a")
    assert metrics.ams_pairs([ams(t0, 520), ams(t1, 510), between, scale(t2, 500)], 1000) == []
    printing = [ams(t0, 520), ams(t1, 510, printing=True), scale(t2, 500)]
    assert metrics.ams_pairs(printing, 1000) == [20.0]
    assert metrics.ams_pairs([ams(t1, 510, printing=True), scale(t2, 500)], 1000) == []
    late = [ams(t2 - timedelta(hours=25), 510), scale(t2, 500)]
    assert metrics.ams_pairs(late, 1000) == []


def test_only_a_plate_that_consumed_grams_breaks_an_ams_pair() -> None:
    t0, t1, t2 = days(1), days(0.7), days(0.5)
    zero = sb_end(t1, 0, "a", basis="not_applied")
    assert metrics.ams_pairs([ams(t0, 520), zero, scale(t2, 500)], 1000) == [20.0]
    covered = sb_end(t1, 0, "b", basis="covered")
    assert metrics.ams_pairs([ams(t0, 520), covered, scale(t2, 500)], 1000) == [20.0]
    heavy = sb_end(t1, 0.1, "c")
    assert metrics.ams_pairs([ams(t0, 520), heavy, scale(t2, 500)], 1000) == []
    started = sb_start(t1, 12, "d")
    assert metrics.ams_pairs([ams(t0, 520), started, scale(t2, 500)], 1000) == []


def test_equal_times_are_ordered_by_seq() -> None:
    t = days(1)
    first, second = ams(t, 520), scale(t, 500)
    assert first.seq < second.seq
    assert metrics.ams_pairs([second, first], 1000) == [20.0]
    swapped = ams(t, 520).model_copy(update={"seq": second.seq + 1})
    assert metrics.ams_pairs([swapped, second], 1000) == []
    early = sb_end(t, 5, "a").model_copy(update={"seq": 10})
    late = sb_end(t, 6, "b").model_copy(update={"seq": 11})
    assert [p.job for p in metrics.plates([late, early])] == ["a", "b"]
    created = event(t, "created").model_copy(update={"seq": 20})
    after = scale(t, 400).model_copy(update={"seq": 21, "grams_before": 900})
    points = _series([after, created], 400)
    assert [p.kind for p in points] == ["created", "weighed"]
    assert [p.grams for p in points] == [900, 400]


def test_ams_error_reads_the_remain_percent() -> None:
    pct = event(days(1), "ams_reading", "ams_remain", data={"remain_pct": 52})
    assert metrics.ams_pairs([pct, scale(days(0.5), 500)], 1000) == [20.0]


def test_ams_error_summary() -> None:
    assert metrics.ams_error([10, -4]) == AmsError(pairs=2, mean_abs_g=7.0, bias_g=3.0)
    assert metrics.ams_error([]) is None


def _series(events: list[SpoolEvent], grams_left: float | None, limit: int = 500) -> Any:
    return metrics.series(events, grams_left, weight_locked=False, label_weight_g=1000, limit=limit)


def test_series_uses_grams_before_of_the_next_event() -> None:
    events = [
        event(days(3), "created"),
        scale(days(2), 1000).model_copy(update={"grams_before": 1000}),
        sb_end(days(1), 30, "a").model_copy(update={"grams_before": 970}),
    ]
    points = _series(events, 950)
    assert [p.grams for p in points] == [1000, 970, 950]
    assert [p.anchor for p in points] == [False, True, False]
    assert [p.kind for p in points] == ["created", "weighed", "print_ended"]


def test_series_skips_an_unknown_value() -> None:
    assert _series([event(days(1), "created")], None) == []


def test_series_marks_an_ams_anchor_at_its_time() -> None:
    first = ams(days(2), 900)
    second = scale(days(1), 800).model_copy(update={"grams_before": 900})
    points = _series([first, second], 800)
    assert [(p.grams, p.anchor) for p in points] == [(900, True), (800, True)]


def test_series_thins_to_the_limit_and_keeps_anchors() -> None:
    start = days(100)
    readings = [ams(start + timedelta(minutes=i), 1000 - i / 10) for i in range(1200)]
    scales = [scale(start + timedelta(minutes=300 * k + 7), 900) for k in (1, 2, 3)]
    events = sorted(readings + scales, key=lambda e: e.at)
    events = [e.model_copy(update={"seq": i, "grams_before": 990.0}) for i, e in enumerate(events)]
    points = _series(events, 880, limit=500)
    assert len(points) <= 500
    assert sum(p.kind == "weighed" for p in points) == 3
    assert points[-1].at == events[-1].at
    assert points[-1].grams == 880
    assert sum(p.kind == "ams_reading" for p in points) < 1200


def test_type_burn_is_the_union_of_its_spools() -> None:
    x = [plate(days(5), 60)]
    y = [plate(days(3), 30)]
    assert metrics.burn(x + y, window_days=30, born=days(90), now=NOW) == 3.0


def _metrics(events: list[SpoolEvent], **over: Any) -> Any:
    values: dict[str, Any] = {
        "grams_left": None,
        "cost_per_kg": None,
        "created_at": days(30),
        "last_used_at": None,
        "hours_outside_dry": 0.0,
        "label_weight_g": 1000,
        "window_days": 30,
        "now": NOW,
        "diameter_mm": 1.75,
        "density_g_cm3": 1.24,
    }
    return metrics.spool_metrics(events, **(values | over))


def test_a_bare_spool_has_no_metrics() -> None:
    found = _metrics([])
    assert found.used_g == 0
    assert found.prints == 0
    assert found.grams_per_print is None
    assert found.burn_g_per_day is None
    assert found.empty_on is None
    assert found.ams_error is None
    assert found.opened_at is None
    assert found.days_since_opened is None
    assert found.days_since_last_use is None
    assert metrics.series([], None, weight_locked=False, label_weight_g=1000) == []


def test_cost_used_and_value_left() -> None:
    events = [bb_end(days(2), 200, "a"), bb_end(days(1), 50, "b", "failed")]
    found = _metrics(events, grams_left=750.0, cost_per_kg=Decimal("24.0000"))
    assert found.cost_used == Decimal("6.00")
    assert found.value_left == Decimal("18.00")
    assert (found.used_g, found.prints, found.grams_per_print) == (250.0, 2, 125.0)
    assert found.wasted_g == 50.0


def test_opened_is_the_first_load_else_the_first_plate() -> None:
    found = _metrics([bb_end(days(4), 20, "a")], last_used_at=days(2))
    assert found.opened_at == days(4)
    assert found.days_since_opened == 4.0
    assert found.days_since_last_use == 2.0
    loaded = _metrics([event(days(6), "loaded"), bb_end(days(4), 20, "a")])
    assert loaded.opened_at == days(6)


def test_consumed_counts_every_plate_with_no_reset() -> None:
    found = _metrics([bb_end(days(3), 40, "a"), bb_end(days(1), 10, "b")])
    assert (found.consumed_g, found.consumed_since) == (50.0, None)


def test_a_consumed_reset_counts_only_the_plates_after_the_newest() -> None:
    """#7149 R-7149-4: a reset starts the consumed counter again; the lifetime figures
    stay."""
    newest = event(days(2), "consumed_reset", "person")
    events = [
        bb_end(days(5), 40, "a"),
        event(days(4), "consumed_reset", "bb"),
        bb_end(days(3), 25, "b"),
        newest,
        bb_end(days(1), 10, "c"),
    ]
    found = _metrics(events)
    assert (found.consumed_g, found.consumed_since) == (10.0, newest.at)
    assert found.used_g == 75.0


# -- length and density (#7685, spec §5.6) ---------------------------------------------


def test_plates_carry_their_length() -> None:
    found = metrics.plates([sb_end(days(1), 2.98, "a", length_m=1.0), sb_end(days(2), 5, "b")])
    assert [p.length_m for p in found] == [None, 1.0]
    assert metrics.plates([bb_end(days(1), 3, "c")])[0].length_m is None


def test_implied_density_of_a_metre_of_pla() -> None:
    """1 m of 1.75 mm filament at 1.24 g/cm³ is 2.98 g (spec §5.6)."""
    assert metrics.implied_density(2.98, 1.0, 1.75) == pytest.approx(1.239, abs=0.001)


def test_the_density_uses_the_spools_diameter() -> None:
    assert metrics.implied_density(7.9, 1.0, 2.85) == pytest.approx(1.239, abs=0.001)


def test_zero_metres_give_no_density() -> None:
    assert metrics.implied_density(5.0, 0.0, 1.75) is None
    assert metrics.implied_density(0.0, 1.0, 1.75) is None
    assert metrics.implied_density(5.0, -1.0, 1.75) is None


def test_metrics_sum_the_metres_of_the_plates_that_have_one() -> None:
    """The density is over the grams and metres of the plates with a length only; a
    plate from before the slice counts in ``used_g`` and nowhere else."""
    events = [
        sb_end(days(3), 2.98, "a", length_m=1.0),
        sb_end(days(2), 5.96, "b", length_m=2.0),
        sb_end(days(1), 10, "c"),
    ]
    found = _metrics(events)
    assert found.used_g == pytest.approx(18.94)
    assert found.used_m == 3.0
    assert found.implied_density_g_cm3 == pytest.approx(1.239, abs=0.001)
    assert found.density_g_cm3 == 1.24
    assert found.density_mismatch is False


def test_plates_with_no_length_give_no_metres() -> None:
    found = _metrics([sb_end(days(1), 10, "a"), bb_end(days(2), 5, "b")])
    assert found.used_m is None
    assert found.implied_density_g_cm3 is None
    assert found.density_g_cm3 == 1.24
    assert found.density_mismatch is False
    bare = _metrics([], density_g_cm3=None)
    assert (bare.used_m, bare.implied_density_g_cm3, bare.density_g_cm3) == (None, None, None)


def test_a_failed_plate_adds_no_metres() -> None:
    events = [
        sb_end(days(2), 2.98, "a", length_m=1.0),
        sb_end(days(1), 0, "b", basis="not_applied", length_m=0.0),
    ]
    found = _metrics(events)
    assert (found.used_m, found.prints) == (1.0, 2)
    assert found.implied_density_g_cm3 == pytest.approx(1.239, abs=0.001)


def test_a_density_off_by_more_than_five_percent_is_a_mismatch() -> None:
    off = _metrics([sb_end(days(1), 3.3, "a", length_m=1.0)])
    assert off.implied_density_g_cm3 == pytest.approx(1.372, abs=0.001)
    assert off.density_mismatch is True
    close = _metrics([sb_end(days(1), 3.0, "a", length_m=1.0)])
    assert close.density_mismatch is False
    unknown = _metrics([sb_end(days(1), 3.3, "a", length_m=1.0)], density_g_cm3=None)
    assert unknown.density_mismatch is False
