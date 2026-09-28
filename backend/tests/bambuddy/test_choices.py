from datetime import UTC, datetime

from scadbuddy.bambuddy.choices import (
    installed_nozzles,
    last_bed_type,
    nozzle_warning,
    plate_warning,
)
from scadbuddy.bambuddy.models import Archive, PrinterStatus
from tests.bambuddy.conftest import recording


def test_the_rack_is_counted_by_size_and_flow() -> None:
    status = PrinterStatus.model_validate(recording("printer-status-rack.json"))
    found = {(n.size, n.flow): n.count for n in installed_nozzles(status)}
    assert found[("0.2", "standard")] >= 1
    assert found[("0.4", "high_flow")] >= 1


def test_an_unreadable_status_installs_nothing() -> None:
    """Review focus 4."""
    assert installed_nozzles(None) == []


def test_the_newest_print_that_ran_names_the_plate_and_uploads_do_not_count() -> None:
    def at(hour: int) -> datetime:
        return datetime(2026, 9, 27, hour, tzinfo=UTC)

    rows = [
        Archive(id=1, printer_id=1, status="completed", bed_type="Cool Plate", started_at=at(1)),
        Archive(
            id=2, printer_id=1, status="cancelled", bed_type="Textured PEI Plate", started_at=at(5)
        ),
        Archive(
            id=3, printer_id=None, status="archived", bed_type="Engineering Plate", created_at=at(9)
        ),
    ]
    assert last_bed_type(rows, printer_id=1) == "Textured PEI Plate"


def test_another_printers_newer_print_does_not_name_this_printers_plate() -> None:
    """Final review 8: the archive read is filtered by printer, but the plate must not
    depend on that — a row from another printer is ignored explicitly."""
    rows = [
        Archive(
            id=1,
            printer_id=1,
            status="completed",
            bed_type="Cool Plate",
            started_at=datetime(2026, 9, 27, 1, tzinfo=UTC),
        ),
        Archive(
            id=2,
            printer_id=2,
            status="completed",
            bed_type="Engineering Plate",
            started_at=datetime(2026, 9, 27, 9, tzinfo=UTC),
        ),
    ]
    assert last_bed_type(rows, printer_id=1) == "Cool Plate"
    assert last_bed_type(rows, printer_id=3) is None


def test_no_archives_names_no_plate() -> None:
    """Review focus 5."""
    assert last_bed_type([], printer_id=1) is None


def test_a_different_plate_is_a_swap_reminder_and_the_same_one_is_not() -> None:
    warning = plate_warning("Cool Plate", "Textured PEI Plate", "3DP-31B-598")
    assert warning is not None and warning.kind == "plate-differs"
    assert "Swap to Cool Plate" in warning.message
    assert plate_warning("Textured PEI Plate", "Textured PEI Plate", "x") is None
    assert plate_warning("Cool Plate", None, "x") is None


def test_a_size_not_in_the_rack_warns() -> None:
    status = PrinterStatus.model_validate(recording("printer-status-rack.json"))
    installed = installed_nozzles(status)
    assert nozzle_warning("0.8", installed) is not None
    assert nozzle_warning("0.4", installed) is None
    assert nozzle_warning("0.8", []) is None  # nothing known, nothing claimed


def test_a_row_with_no_timestamp_is_the_oldest_and_never_crashes_the_ordering() -> None:
    """PR #335 review 3: ``datetime.min`` is naive and Bambuddy's rows are aware, so a
    timestampless row next to a stamped one raised ``TypeError`` from ``max``."""
    rows = [
        Archive(id=1, printer_id=1, status="completed", bed_type="Cool Plate"),
        Archive(
            id=2,
            printer_id=1,
            status="completed",
            bed_type="Engineering Plate",
            started_at=datetime(2026, 9, 27, 4, tzinfo=UTC),
        ),
    ]
    assert last_bed_type(rows, printer_id=1) == "Engineering Plate"
    assert last_bed_type(list(reversed(rows)), printer_id=1) == "Engineering Plate"


def test_naive_and_aware_timestamps_compare_with_the_naive_one_read_as_utc() -> None:
    """The recorded archives are naive; a row that carries an offset is still ordered."""
    rows = [
        Archive(
            id=1,
            printer_id=1,
            status="completed",
            bed_type="Engineering Plate",
            started_at=datetime(2026, 9, 27, 4, tzinfo=UTC),
        ),
        Archive(
            id=2,
            printer_id=1,
            status="completed",
            bed_type="Supertack Plate",
            started_at=datetime(2026, 9, 27, 9),
        ),
        Archive(id=3, printer_id=1, status="failed", bed_type="Cool Plate"),
    ]
    assert last_bed_type(rows, printer_id=1) == "Supertack Plate"


def test_only_timestampless_rows_still_name_a_plate() -> None:
    rows = [Archive(id=1, printer_id=1, status="completed", bed_type="Cool Plate")]
    assert last_bed_type(rows, printer_id=1) == "Cool Plate"
