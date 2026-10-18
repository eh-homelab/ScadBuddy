import pytest

from scadbuddy.core.problems import ApiError
from scadbuddy.slicing.slice_info import filament_changes, plate_gcode, read_sliced
from tests.support.sliced3mf import sliced_3mf

ONE = sliced_3mf(plates={1: (3600, [12.5, 3.0])}, gcode={1: b"M620 S0A\nM620 S1A\nM620 S0A\n"})


def test_read_sliced_reads_time_grams_and_changes() -> None:
    read = read_sliced(ONE, 1)
    assert (
        read.print_time_seconds,
        read.grams_by_slot,
        read.filament_used_g,
        read.filament_changes,
    ) == (3600, {1: 12.5, 2: 3.0}, 15.5, 2)


def test_one_plate_is_read_whatever_its_number() -> None:
    """A whole-file slice (plan.whole_file) names plate 0."""
    assert read_sliced(ONE, 2).print_time_seconds == 3600


def test_a_plate_a_file_of_several_lacks_is_404() -> None:
    two = sliced_3mf(plates={1: (60, [1.0]), 2: (60, [1.0])}, gcode={1: b"", 2: b""})
    with pytest.raises(ApiError) as missing:
        read_sliced(two, 3)
    assert missing.value.status == 404


def test_unloads_are_not_changes() -> None:
    assert filament_changes(b"M620 S255A\nM620 S0A\nM620 S1A\n") == 1


def test_an_unreadable_file_reads_as_unknown() -> None:
    read = read_sliced(b"not a zip", 1)
    assert read.print_time_seconds is None
    assert read.grams_by_slot == {}
    assert read.filament_changes is None


def test_plate_gcode_is_none_for_a_bad_zip() -> None:
    assert plate_gcode(b"not a zip", 1) is None
