"""The machine rate's arithmetic (#4625, order operations spec 2026-11-07 §6.2): pure, on
``Decimal``, each part rounded half up to 0.0001 and the rate their sum. No Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from scadbuddy.orders.integrations import MaintenanceTask
from scadbuddy.orders.machine_rate import (
    CostProfile,
    Events,
    MachineRateInputs,
    MaintenanceCost,
    Replacement,
    TaskEvent,
    WearPart,
    compute,
    part_actual,
    task_actual,
)
from scadbuddy.orders.models import HoursSource, MachineRateBreakdown, PricedBy

PROFILE = CostProfile(
    purchase_price="1440",
    life_hours="3000",
    watts="120",
    kwh_price="0.25",
    labour_rate_per_hour="30",
)
WEAR = [
    WearPart(name="Nozzle", cost="14", life_hours="200"),
    WearPart(name="Plate", cost="35", life_hours="500"),
]
TYPED = [
    MaintenanceCost(task="Lubricate rods", minutes="20", consumables="2", interval_hours="100"),
    MaintenanceCost(task="Clean carbon rods", minutes="10", consumables="1", interval_hours="50"),
]


def test_the_rate_adds_maintenance_wear_power_and_depreciation() -> None:
    rate = compute(
        PROFILE, WEAR, TYPED, None, default_labour="0", currency="USD", intervals="typed"
    )
    # (20 min x 30 / 60 + 2) / 100 + (10 x 30 / 60 + 1) / 50
    assert rate.maintenance_per_hour == "0.24"
    assert [part.per_hour for part in rate.maintenance] == ["0.12", "0.12"]
    assert rate.wear_per_hour == "0.14"
    assert rate.power_per_hour == "0.03"
    assert rate.depreciation_per_hour == "0.48"
    assert rate.rate_per_hour == "0.89"
    assert rate.source == "profile"
    assert rate.labour_rate_per_hour == "30"
    assert rate.intervals == "typed"


def test_the_labour_rate_defaults_to_the_shops() -> None:
    profile = PROFILE.model_copy(update={"labour_rate_per_hour": None})
    rate = compute(profile, [], TYPED, None, default_labour="60", currency="USD", intervals="typed")
    assert rate.labour_rate_per_hour == "60"
    # (20 x 60 / 60 + 2) / 100 + (10 x 60 / 60 + 1) / 50
    assert rate.maintenance_per_hour == "0.44"


def test_no_life_is_no_depreciation() -> None:
    profile = PROFILE.model_copy(update={"life_hours": None})
    rate = compute(profile, [], [], None, default_labour="0", currency="USD", intervals="typed")
    assert rate.depreciation_per_hour == "0"
    assert rate.rate_per_hour == "0.03"


def test_each_part_is_rounded_and_the_rate_is_their_sum() -> None:
    profile = CostProfile(purchase_price="1", life_hours="3")
    wear = [
        WearPart(name="A", cost="1", life_hours="3"),
        WearPart(name="B", cost="1", life_hours="3"),
    ]
    rate = compute(profile, wear, [], None, default_labour="0", currency="USD", intervals="typed")
    assert [part.per_hour for part in rate.wear] == ["0.3333", "0.3333"]
    assert rate.wear_per_hour == "0.6666"
    assert rate.depreciation_per_hour == "0.3333"
    assert rate.rate_per_hour == "0.9999"
    assert Decimal(rate.rate_per_hour) == sum(
        Decimal(part)
        for part in (
            rate.maintenance_per_hour,
            rate.wear_per_hour,
            rate.power_per_hour,
            rate.depreciation_per_hour,
        )
    )


def test_bambuddys_tasks_give_the_intervals() -> None:
    """A linked printer's tasks are Bambuddy's: each one's interval is Bambuddy's (the
    stored one is not used), a task with no costs typed counts 0, a calendar or disabled
    task is listed and not counted, and a cost row Bambuddy no longer lists is not
    counted."""
    costs = [
        MaintenanceCost(task="Lubricate rods", minutes="20", consumables="2", interval_hours="999"),
        MaintenanceCost(task="Gone task", minutes="60", consumables="10", interval_hours="10"),
        MaintenanceCost(task="Clean nozzle", minutes="5", consumables="0"),
        MaintenanceCost(task="Check belts", minutes="30", consumables="0"),
    ]
    tasks = [
        MaintenanceTask(task="Lubricate rods", interval_hours=100, interval_type="hours"),
        MaintenanceTask(task="Clean build plate", interval_hours=25, interval_type="hours"),
        MaintenanceTask(task="Clean nozzle", interval_hours=30, interval_type="days"),
        MaintenanceTask(
            task="Check belts", interval_hours=200, interval_type="hours", enabled=False
        ),
    ]
    rate = compute(
        PROFILE, [], costs, tasks, default_labour="0", currency="USD", intervals="bambuddy"
    )
    shown = {
        part.task: (part.interval_hours, part.per_hour, part.counted, part.why)
        for part in rate.maintenance
    }
    assert shown == {
        "Lubricate rods": ("100", "0.12", True, None),
        "Clean build plate": ("25", "0", True, None),
        "Clean nozzle": ("30", "0", False, "calendar"),
        "Check belts": ("200", "0", False, "disabled"),
        "Gone task": ("10", "0", False, "not_in_bambuddy"),
    }
    assert rate.maintenance_per_hour == "0.12"


def test_a_typed_task_with_no_interval_adds_nothing() -> None:
    costs = [MaintenanceCost(task="Lubricate rods", minutes="20", consumables="2")]
    rate = compute(PROFILE, [], costs, None, default_labour="0", currency="USD", intervals="typed")
    assert rate.maintenance[0].counted is False
    assert rate.maintenance[0].why == "no_interval"
    assert rate.maintenance_per_hour == "0"


def test_inputs_refuse_a_repeated_name() -> None:
    with pytest.raises(ValidationError, match="Nozzle"):
        MachineRateInputs(profile=PROFILE, wear_parts=[WEAR[0], WEAR[0]])
    with pytest.raises(ValidationError, match="Lubricate rods"):
        MachineRateInputs(profile=PROFILE, maintenance=[TYPED[0], TYPED[0]])


def test_a_life_must_be_above_zero() -> None:
    with pytest.raises(ValidationError):
        WearPart(name="Nozzle", cost="14", life_hours="0")
    with pytest.raises(ValidationError):
        CostProfile(life_hours="0")


# -- actuals and confidence (#5774, spec 2026-11-17-machine-rate-actuals §3.3, §3.4) --------

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def task_event(
    hours: str | None,
    *,
    n: int = 0,
    source: HoursSource = "bambuddy",
    minutes: str | None = None,
    consumables: str | None = None,
) -> TaskEvent:
    return TaskEvent(
        id=f"t{n}",
        at=T0 + timedelta(days=n),
        hours_at=None if hours is None else Decimal(hours),
        hours_source=None if hours is None else source,
        minutes=None if minutes is None else Decimal(minutes),
        consumables=None if consumables is None else Decimal(consumables),
    )


def replacement(
    hours: str | None,
    cost: str,
    *,
    n: int = 0,
    source: HoursSource = "lan_watch",
    used: str | None = None,
) -> Replacement:
    return Replacement(
        id=f"r{n}",
        at=T0 + timedelta(days=n),
        hours_at=None if hours is None else Decimal(hours),
        hours_source=None if hours is None else source,
        cost=Decimal(cost),
        hours_used=None if used is None else Decimal(used),
    )


#: Prod's Clean Build Plate log (spec §2.1): five rows, the last three gaps 35.0, 37.9
#: and 123.7 h, against an interval of 25 h.
PLATE_LOG = [
    task_event(h, n=i, minutes="10", consumables="0")
    for i, h in enumerate(["66.5", "269.0", "304.0", "341.9", "465.6"])
]
PLATE_COST = MaintenanceCost(
    task="Clean Build Plate", minutes="10", consumables="0", priced_by="actual"
)
PLATE_TASK = MaintenanceTask(task="Clean Build Plate", interval_hours=25)
LABOUR_30 = CostProfile(labour_rate_per_hour="30")


def plate_rate(
    events: list[TaskEvent], *, window: int = 3, priced_by: PricedBy = "actual"
) -> MachineRateBreakdown:
    return compute(
        LABOUR_30.model_copy(update={"actuals_window": window}),
        [],
        [PLATE_COST.model_copy(update={"priced_by": priced_by})],
        [PLATE_TASK],
        default_labour="0",
        currency="USD",
        intervals="bambuddy",
        events=Events(tasks={"Clean Build Plate": events}),
    )


def test_prods_build_plate_prices_from_its_actual_at_full_confidence() -> None:
    rate = plate_rate(PLATE_LOG)
    part = rate.maintenance[0]
    # 15.00 / (35.0 + 37.9 + 123.7) = 15.00 / 196.6
    assert (part.estimate_per_hour, part.actual_per_hour) == ("0.2", "0.0763")
    assert (part.basis, part.per_hour, part.have, part.need, part.confidence) == (
        "actual",
        "0.0763",
        3,
        3,
        100,
    )
    assert (part.actual_interval_hours, part.actual_cost_per_occurrence) == ("65.5", "5")
    assert part.actual_why is None
    assert part.partly_typed is False
    assert rate.maintenance_per_hour == "0.0763"
    assert (rate.actuals, rate.actuals_window, rate.confidence) == (True, 3, 100)


def test_the_actual_is_a_ratio_of_sums_not_a_mean_of_rates() -> None:
    # Gaps of 10 h (cost 5) and 90 h (cost 5): 10 / 100, not the mean of 0.5 and 0.0556.
    events = [task_event(h, n=i, minutes="10") for i, h in enumerate(["0", "10", "100"])]
    part = plate_rate(events, window=2).maintenance[0]
    assert part.actual_per_hour == "0.1"


@pytest.mark.parametrize(
    ("window", "have", "confidence", "basis"),
    [(1, 1, 100, "actual"), (2, 2, 100, "actual"), (3, 3, 100, "actual"), (5, 4, 80, "estimate")],
)
def test_the_window_is_n_usable_gaps(window: int, have: int, confidence: int, basis: str) -> None:
    part = plate_rate(PLATE_LOG, window=window).maintenance[0]
    assert (part.have, part.need, part.confidence, part.basis) == (have, window, confidence, basis)


def test_fewer_than_n_gaps_price_by_the_estimate_with_a_confidence() -> None:
    part = plate_rate(PLATE_LOG[-2:]).maintenance[0]
    assert (part.basis, part.per_hour, part.actual_why, part.have, part.confidence) == (
        "estimate",
        "0.2",
        "too_few",
        1,
        33,
    )
    # The partial actual still shows: 5 / 123.7.
    assert part.actual_per_hour == "0.0404"


@pytest.mark.parametrize(
    ("count", "confidence", "why"),
    [
        (0, 0, "no_events"),
        (1, 0, "no_events"),
        (2, 33, "too_few"),
        (3, 66, "too_few"),
        (4, 100, None),
        (5, 100, None),
    ],
)
def test_confidence_counts_usable_gaps(count: int, confidence: int, why: str | None) -> None:
    part = plate_rate(PLATE_LOG[len(PLATE_LOG) - count :]).maintenance[0]
    assert (part.confidence, part.actual_why) == (confidence, why)
    assert (part.actual_per_hour is None) == (count < 2)


def test_a_zero_gap_is_skipped_and_the_window_reaches_further_back() -> None:
    events = [
        task_event("0", n=0, minutes="10"),
        task_event("10", n=1, minutes="10"),
        task_event("20", n=2, minutes="10"),
        task_event("20", n=3, minutes="10"),
    ]
    part = plate_rate(events, window=2).maintenance[0]
    assert part.have == 2
    assert [(gap.event, gap.previous, gap.reason) for gap in part.skipped_gaps] == [
        ("t3", "t2", "not_positive")
    ]
    # Gaps 10 and 10 (the 0 h one skipped): 10 / 20.
    assert part.actual_per_hour == "0.5"
    assert part.confidence == 100


def test_a_gap_across_two_counters_is_skipped() -> None:
    events = [
        task_event("100", n=0, minutes="10", source="typed"),
        task_event("150", n=1, minutes="10", source="bambuddy"),
        task_event("200", n=2, minutes="10", source="bambuddy"),
    ]
    part = plate_rate(events).maintenance[0]
    assert part.have == 1
    assert [(gap.event, gap.reason) for gap in part.skipped_gaps] == [("t1", "counter_changed")]
    assert part.confidence == 33


def test_an_event_with_no_reading_opens_no_gap() -> None:
    events = [
        task_event("100", n=0, minutes="10"),
        task_event(None, n=1, minutes="10"),
        task_event("150", n=2, minutes="10"),
    ]
    part = plate_rate(events, window=1).maintenance[0]
    assert part.have == 1
    assert part.actual_per_hour == "0.1"


def test_an_event_with_no_figures_uses_the_typed_ones() -> None:
    events = [
        task_event("0", n=0),
        task_event("10", n=1),
        task_event("20", n=2, minutes="20", consumables="1"),
    ]
    part = plate_rate(events, window=2).maintenance[0]
    # (10 x 30 / 60) + (20 x 30 / 60 + 1) = 5 + 11 over 20 h.
    assert part.actual_per_hour == "0.8"
    assert part.partly_typed is True


def test_a_task_not_counted_with_too_few_events_carries_both_reasons() -> None:
    rate = compute(
        LABOUR_30,
        [],
        [PLATE_COST],
        [PLATE_TASK.model_copy(update={"enabled": False})],
        default_labour="0",
        currency="USD",
        intervals="bambuddy",
        events=Events(tasks={"Clean Build Plate": PLATE_LOG[-2:]}),
    )
    part = rate.maintenance[0]
    assert (part.why, part.actual_why, part.basis, part.per_hour) == (
        "disabled",
        "too_few",
        "estimate",
        "0",
    )
    # A row that cannot price does not lower the rate's confidence.
    assert rate.confidence is None


def test_a_task_not_counted_never_prices_by_its_actual() -> None:
    rate = compute(
        LABOUR_30,
        [],
        [PLATE_COST],
        [PLATE_TASK.model_copy(update={"interval_type": "days"})],
        default_labour="0",
        currency="USD",
        intervals="bambuddy",
        events=Events(tasks={"Clean Build Plate": PLATE_LOG}),
    )
    part = rate.maintenance[0]
    assert (part.why, part.basis, part.per_hour, part.confidence) == (
        "calendar",
        "estimate",
        "0",
        100,
    )
    assert rate.actuals is False


def test_a_row_set_to_estimate_prices_by_it_and_does_not_lower_the_confidence() -> None:
    rate = plate_rate(PLATE_LOG, priced_by="estimate")
    part = rate.maintenance[0]
    assert (part.basis, part.per_hour, part.confidence, part.actual_why) == (
        "estimate",
        "0.2",
        100,
        None,
    )
    assert (rate.actuals, rate.confidence) == (False, None)


NOZZLE = WearPart(name="Nozzle", cost="35", life_hours="1000", priced_by="actual")


def nozzle_rate(replacements: list[Replacement], *, window: int = 3) -> MachineRateBreakdown:
    return compute(
        CostProfile(actuals_window=window),
        [NOZZLE],
        [],
        None,
        default_labour="0",
        currency="USD",
        intervals="typed",
        events=Events(parts={"Nozzle": replacements}),
    )


def test_a_wear_parts_actual_is_cost_over_the_hours_each_part_ran() -> None:
    replacements = [
        replacement("0", "35", n=0),
        replacement("600", "35", n=1),
        replacement("1200", "40", n=2),
        replacement("1800", "30", n=3),
    ]
    rate = nozzle_rate(replacements)
    part = rate.wear[0]
    # (35 + 40 + 30) / 1800; the first marks only where the next part's life starts.
    assert (part.estimate_per_hour, part.actual_per_hour, part.actual_hours_used) == (
        "0.035",
        "0.0583",
        "600",
    )
    assert (part.basis, part.per_hour, part.have, part.confidence) == ("actual", "0.0583", 3, 100)
    assert part.partly_typed is False
    assert rate.wear_per_hour == "0.0583"
    assert rate.rate_per_hour == "0.0583"


def test_a_typed_hours_used_is_always_usable() -> None:
    part = nozzle_rate([replacement(None, "35", n=0, used="640")], window=1).wear[0]
    assert (part.have, part.actual_per_hour, part.basis) == (1, "0.0547", "actual")


def test_a_parts_first_replacement_counts_nothing() -> None:
    part = nozzle_rate([replacement("10", "35")]).wear[0]
    assert (part.have, part.actual_why, part.confidence, part.actual_per_hour) == (
        0,
        "no_events",
        0,
        None,
    )
    assert (part.basis, part.per_hour) == ("estimate", "0.035")


def test_a_derived_hours_used_of_zero_or_across_counters_is_skipped() -> None:
    replacements = [
        replacement("100", "35", n=0),
        replacement("100", "35", n=1),
        replacement("700", "35", n=2, source="bambuddy"),
        replacement("900", "35", n=3, source="bambuddy"),
    ]
    part = nozzle_rate(replacements).wear[0]
    assert [(gap.event, gap.reason) for gap in part.skipped_gaps] == [
        ("r2", "counter_changed"),
        ("r1", "not_positive"),
    ]
    assert (part.have, part.confidence, part.actual_why) == (1, 33, "too_few")


def test_a_tasks_and_a_parts_window_list_the_same_events() -> None:
    """Both windows list each counted gap's two events, so the list and "k of N" read the
    same for a task and a part: the newest event and the one its gap starts from."""
    readings = ["0", "100", "200"]
    task = task_actual([task_event(h, n=i) for i, h in enumerate(readings)], None, Decimal(0), 1)
    part = part_actual([replacement(h, "35", n=i) for i, h in enumerate(readings)], 1)
    assert (task.have, task.window) == (1, ["t2", "t1"])
    assert (part.have, part.window) == (1, ["r2", "r1"])
    # A typed hours_used reads no event before it.
    typed = part_actual([replacement("0", "35", n=0), replacement(None, "35", n=1, used="9")], 1)
    assert (typed.have, typed.window) == (1, ["r1"])


def test_the_rates_confidence_is_the_lowest_of_the_rows_set_to_actual() -> None:
    rate = compute(
        LABOUR_30,
        [NOZZLE],
        [PLATE_COST],
        [PLATE_TASK],
        default_labour="0",
        currency="USD",
        intervals="bambuddy",
        events=Events(
            tasks={"Clean Build Plate": PLATE_LOG},
            parts={"Nozzle": [replacement("0", "35", n=0), replacement("600", "35", n=1)]},
        ),
    )
    assert [rate.maintenance[0].confidence, rate.wear[0].confidence] == [100, 33]
    assert (rate.actuals, rate.confidence) == (True, 33)
    total = Decimal(rate.maintenance_per_hour) + Decimal(rate.wear_per_hour)
    assert Decimal(rate.rate_per_hour) == total


def test_a_quote_recorded_before_reads_estimate_and_no_confidence() -> None:
    old = MachineRateBreakdown.model_validate(
        {
            "source": "profile",
            "rate_per_hour": "0.5",
            "currency": "USD",
            "maintenance": [
                {
                    "task": "Lubricate rods",
                    "interval_hours": "100",
                    "minutes": "20",
                    "consumables": "2",
                    "per_hour": "0.12",
                    "counted": True,
                }
            ],
            "wear": [{"name": "Nozzle", "cost": "14", "life_hours": "200", "per_hour": "0.07"}],
        }
    )
    assert (old.maintenance[0].basis, old.maintenance[0].confidence) == ("estimate", None)
    assert (old.wear[0].basis, old.wear[0].confidence) == ("estimate", None)
    assert (old.actuals, old.confidence) == (False, None)


def test_the_window_is_between_one_and_twenty() -> None:
    with pytest.raises(ValidationError):
        CostProfile(actuals_window=0)
    with pytest.raises(ValidationError):
        CostProfile(actuals_window=21)
    with pytest.raises(ValidationError):
        CostProfile(lan_hours_offset="-1")


def test_a_rename_may_not_take_another_rows_name() -> None:
    with pytest.raises(ValidationError, match="renamed_from"):
        MachineRateInputs(
            profile=PROFILE,
            wear_parts=[
                WearPart(name="Nozzle", cost="14", life_hours="200"),
                WearPart(name="Hotend", cost="50", life_hours="900", renamed_from="Nozzle"),
            ],
        )
    with pytest.raises(ValidationError, match="renamed_from"):
        MachineRateInputs(
            profile=PROFILE,
            maintenance=[
                MaintenanceCost(task="A", renamed_from="X"),
                MaintenanceCost(task="B", renamed_from="X"),
            ],
        )
