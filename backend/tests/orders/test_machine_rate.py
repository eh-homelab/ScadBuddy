"""The machine rate's arithmetic (#4625, order operations spec 2026-11-07 §6.2): pure, on
``Decimal``, each part rounded half up to 0.0001 and the rate their sum. No Postgres."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from scadbuddy.orders.integrations import MaintenanceTask
from scadbuddy.orders.machine_rate import (
    CostProfile,
    MachineRateInputs,
    MaintenanceCost,
    WearPart,
    compute,
)

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
