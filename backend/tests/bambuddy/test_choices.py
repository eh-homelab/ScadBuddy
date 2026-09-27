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
    assert last_bed_type(rows) == "Textured PEI Plate"


def test_no_archives_names_no_plate() -> None:
    """Review focus 5."""
    assert last_bed_type([]) is None


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
