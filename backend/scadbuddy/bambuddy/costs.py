"""Bambuddy's cost of an archive, believed only as far as its filament readings are
(#950): the print page shows it (``api/print_history.py``), and an order's real cost shows
it beside its own sums (orders spec 2026-10-18 §6.4).

A run whose grams are far over the archive's read a spool's weight, not what the print
used, and Bambuddy priced that reading. Such a run's cost is its own price per gram at
the archive's grams, and the archive's cost has the overstatement taken back out when it
evidently carries it.
"""

from __future__ import annotations

from collections.abc import Sequence

from scadbuddy.bambuddy.models import ArchiveRun

#: How many times the archive's grams a run may read before it is not believed.
#: A reprint uses about what the archive says; a spool's weight is 50x and more.
SUSPECT_RUN_GRAMS_RATIO = 3.0


def run_cost(run: ArchiveRun, archive_grams: float | None) -> tuple[float | None, bool]:
    """The run's cost and whether its filament reading is suspect."""
    grams = run.filament_used_grams
    if not archive_grams or not grams or grams <= archive_grams * SUSPECT_RUN_GRAMS_RATIO:
        return run.cost, False
    return (None if run.cost is None else round(run.cost * archive_grams / grams, 2)), True


def archive_cost(
    cost: float | None, archive_grams: float | None, runs: Sequence[ArchiveRun]
) -> float | None:
    """The archive's ``cost`` without what its suspect runs overstated."""
    overstated = 0.0
    for run in runs:
        corrected, suspect = run_cost(run, archive_grams)
        if suspect:
            overstated += (run.cost or 0.0) - (corrected or 0.0)
    if cost is not None and 0 < overstated <= cost:
        return round(cost - overstated, 2)
    return cost
