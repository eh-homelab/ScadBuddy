"""Bambuddy's cost of an archive, corrected for a spool-weight reading (#950)."""

from __future__ import annotations

from scadbuddy.bambuddy.costs import archive_cost, run_cost
from scadbuddy.bambuddy.models import ArchiveRun


def run(grams: float | None, cost: float | None) -> ArchiveRun:
    return ArchiveRun(id=1, status="completed", filament_used_grams=grams, cost=cost)


def test_a_believable_run_keeps_its_cost() -> None:
    assert run_cost(run(30, 0.6), 25) == (0.6, False)
    assert run_cost(run(30, 0.6), None) == (0.6, False)


def test_a_spool_weight_reading_is_priced_at_the_archives_grams() -> None:
    assert run_cost(run(1000, 20.0), 25) == (0.5, True)


def test_the_archive_cost_drops_what_a_suspect_run_overstated() -> None:
    runs = [run(25, 0.5), run(1000, 20.0)]
    assert archive_cost(20.5, 25, runs) == 1.0


def test_a_cost_below_the_overstatement_is_left_as_it_is() -> None:
    assert archive_cost(5.0, 25, [run(1000, 20.0)]) == 5.0
