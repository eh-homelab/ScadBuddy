"""ScadBuddy's own hour counter, pure (#5774, spec 2026-11-17-machine-rate-actuals §3.6,
§10): `run_hours.accrue` on one row, one write or touch at a time."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from scadbuddy.printers.run_hours import COUNTED_STATE, GAP_LIMIT, RunHours, accrue, hours
from scadbuddy.printers.status import STALE_AFTER

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
JOB = "run-1 plate 1"


def _row(state: str | None = "RUNNING", subtask: str | None = JOB) -> RunHours:
    return RunHours(
        run_seconds=Decimal(100),
        unseen_seconds=Decimal(0),
        counted_to=T0,
        state=state,
        subtask=subtask,
    )


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _report(state: str | None = "RUNNING", subtask: str | None = JOB) -> dict[str, object]:
    report: dict[str, object] = {"mc_percent": 40}
    if state is not None:
        report["gcode_state"] = state
    if subtask is not None:
        report["subtask_name"] = subtask
    return report


def test_the_counter_counts_only_running_and_its_limit_is_the_stale_limit() -> None:
    assert COUNTED_STATE == "RUNNING"
    assert GAP_LIMIT == STALE_AFTER == timedelta(seconds=90)


@pytest.mark.parametrize("gap", [1, 10, 90])
def test_a_short_gap_in_running_counts(gap: int) -> None:
    after = accrue(_row(), _at(gap), _report())
    assert after.run_seconds == Decimal(100 + gap)
    assert after.unseen_seconds == 0
    assert after.counted_to == _at(gap)


def test_a_short_gap_counts_on_a_touch_too() -> None:
    after = accrue(_row(), _at(10), None)
    assert after.run_seconds == Decimal(110) and after.counted_to == _at(10)
    assert (after.state, after.subtask) == ("RUNNING", JOB)


def test_a_fraction_of_a_second_counts() -> None:
    after = accrue(_row(), _at(1.25), _report())
    assert after.run_seconds == Decimal("101.25")


@pytest.mark.parametrize("state", ["PAUSE", "PREPARE", "SLICING", "IDLE", "FINISH", "FAILED", None])
def test_any_other_state_counts_nothing(state: str | None) -> None:
    after = accrue(_row(state=state), _at(10), _report(state="RUNNING"))
    assert after.run_seconds == 100 and after.unseen_seconds == 0
    assert after.counted_to == _at(10)
    assert after.state == "RUNNING"


@pytest.mark.parametrize("state", ["PAUSE", "IDLE"])
def test_a_long_gap_from_another_state_counts_nothing_and_is_not_unseen(state: str) -> None:
    after = accrue(_row(state=state), _at(600), _report(state="FINISH"))
    assert after.run_seconds == 100 and after.unseen_seconds == 0
    assert after.counted_to == _at(600)


def test_a_long_gap_with_the_same_print_still_running_counts() -> None:
    """A watch restart: the printer printed on while ScadBuddy's link was down."""
    after = accrue(_row(), _at(600), _report())
    assert after.run_seconds == 700 and after.unseen_seconds == 0
    assert after.counted_to == _at(600)


@pytest.mark.parametrize(
    "report",
    [
        _report(state="FINISH"),
        _report(state="PAUSE"),
        _report(subtask="run-2 plate 1"),
        _report(state=None),
        _report(subtask=None),
        _report(state=None, subtask=None),
    ],
    ids=["finished", "paused", "another-print", "no-state", "no-subtask", "delta"],
)
def test_a_long_gap_that_ends_otherwise_is_unseen(report: dict[str, object]) -> None:
    after = accrue(_row(), _at(600), report)
    assert after.run_seconds == 100
    assert after.unseen_seconds == 600
    assert after.counted_to == _at(600)


def test_a_long_gap_after_a_row_with_no_subtask_is_unseen() -> None:
    after = accrue(_row(subtask=None), _at(600), _report())
    assert after.run_seconds == 100 and after.unseen_seconds == 600


def test_a_touch_after_a_long_gap_changes_nothing_and_the_next_report_decides() -> None:
    row = _row()
    touched = accrue(row, _at(600), None)
    assert touched == row
    assert accrue(touched, _at(610), _report()).run_seconds == 710
    assert accrue(touched, _at(610), _report(state="FINISH")).unseen_seconds == 610


def test_a_resend_at_the_same_seen_at_adds_nothing() -> None:
    once = accrue(_row(), _at(10), _report())
    assert accrue(once, _at(10), _report()) == once


def test_a_touch_stamped_before_counted_to_adds_nothing() -> None:
    row = _row()
    assert accrue(row, _at(-20), None) == row


def test_an_earlier_report_changes_nothing() -> None:
    """A late write, or a report after the watch clock stepped back: ``counted_to``
    never moves back, so nothing is counted twice."""
    row = _row()
    assert accrue(row, _at(-5), _report()) == row
    assert accrue(row, _at(-5), _report(subtask="another")) == row


def test_a_late_write_on_a_clock_far_ahead_counts_nothing_twice() -> None:
    """The watch host is 45 s ahead and a write lands 35 s late (review of c83f2b0d7):
    it changes nothing, and the next report counts only its own gap."""
    row = _row()
    late = accrue(row, _at(-35), _report())
    assert late == row
    after = accrue(late, _at(10), _report())
    assert after.run_seconds == 110 and after.counted_to == _at(10)


def test_an_earlier_report_may_stop_the_counting() -> None:
    """A print that ended while the clock was behind ``counted_to`` stops the counting,
    so the next report counts nothing for its gap: low, never high."""
    stopped = accrue(_row(), _at(-5), _report(state="FINISH"))
    assert stopped == replace(_row(), state="FINISH")
    after = accrue(stopped, _at(10), _report(state="FINISH"))
    assert after.run_seconds == 100 and after.counted_to == _at(10)


def test_an_earlier_report_never_starts_the_counting() -> None:
    row = _row(state="PAUSE")
    assert accrue(row, _at(-5), _report()) == row


def test_after_a_clock_step_back_counting_waits_for_counted_to() -> None:
    """A clock an hour ahead that steps back loses its lead once, never more: reports
    before ``counted_to`` count nothing, and the first one past it counts its gap."""
    ahead = accrue(_row(), _at(3600), _report())  # the same print on, an hour "later"
    assert ahead.run_seconds == 3700
    stepped = accrue(accrue(ahead, _at(10), _report()), _at(20), _report())
    assert stepped == ahead
    after = accrue(stepped, _at(3610), _report())
    assert after.run_seconds == 3710 and after.counted_to == _at(3610)


def test_a_touch_after_a_clock_step_back_adds_nothing_and_keeps_counted_to() -> None:
    """A touch is stamped ``LINK_PROOF`` (20 s) before it is sent, so one stamped before
    ``counted_to`` is the usual case after a report: moving back on it would count that
    stretch twice."""
    row = _row()
    assert accrue(row, _at(-3600), None) == row


def test_a_delta_report_without_state_keeps_the_state() -> None:
    after = accrue(_row(), _at(10), _report(state=None, subtask=None))
    assert after.run_seconds == 110
    assert (after.state, after.subtask) == ("RUNNING", JOB)
    assert accrue(after, _at(20), {"mc_percent": 41}).run_seconds == 120


def test_a_report_sets_state_and_subtask_each_only_when_present() -> None:
    after = accrue(_row(), _at(10), _report(state="PAUSE", subtask=None))
    assert (after.state, after.subtask) == ("PAUSE", JOB)
    after = accrue(after, _at(20), _report(state=None, subtask="run-2 plate 1"))
    assert (after.state, after.subtask) == ("PAUSE", "run-2 plate 1")


def test_a_print_counts_running_only() -> None:
    """One print from the counter's first report on: 10 s ticks, a pause, a finish."""
    row = RunHours(
        run_seconds=Decimal(0),
        unseen_seconds=Decimal(0),
        counted_to=T0,
        state=None,
        subtask=None,
    )
    steps: list[tuple[float, dict[str, object] | None]] = [
        (0, _report(state="PREPARE")),
        (30, _report(state="RUNNING")),
        (40, None),
        (50, None),
        (55, _report(state="PAUSE")),
        (65, None),
        (75, _report(state="RUNNING")),
        (85, {"mc_percent": 90}),
        (95, _report(state="FINISH")),
        (105, None),
    ]
    for at, report in steps:
        row = accrue(row, _at(at), report)
    assert row.run_seconds == (55 - 30) + (95 - 75)
    assert row.unseen_seconds == 0
    assert (row.state, row.counted_to) == ("FINISH", _at(105))


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (Decimal(0), Decimal("0.0")),
        (Decimal(179), Decimal("0.0")),
        (Decimal(180), Decimal("0.1")),
        (Decimal(3600), Decimal("1.0")),
        (Decimal(3600 * 12 + 540), Decimal("12.2")),
        (Decimal("5579.9"), Decimal("1.5")),
    ],
)
def test_the_reading_rounds_half_up_to_a_tenth_of_an_hour(
    seconds: Decimal, expected: Decimal
) -> None:
    assert hours(seconds) == expected
