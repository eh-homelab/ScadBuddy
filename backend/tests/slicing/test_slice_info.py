import io
import zipfile
from pathlib import Path

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


def test_a_plate_of_several_is_read_by_its_number() -> None:
    two = sliced_3mf(plates={1: (60, [1.0]), 2: (120, [2.0, 0.5])}, gcode={1: b"", 2: b""})
    read = read_sliced(two, 2)
    assert (read.print_time_seconds, read.grams_by_slot, read.filament_used_g) == (
        120,
        {1: 2.0, 2: 0.5},
        2.5,
    )


def test_unloads_are_not_changes() -> None:
    assert filament_changes(b"M620 S255A\nM620 S0A\nM620 S1A\n") == 1


def test_an_unreadable_file_reads_as_unknown() -> None:
    read = read_sliced(b"not a zip", 1)
    assert read.print_time_seconds is None
    assert read.grams_by_slot == {}
    assert read.filament_changes is None


def test_plate_gcode_is_none_for_a_bad_zip() -> None:
    assert plate_gcode(b"not a zip", 1) is None


#: Two plates sliced by Bambu Studio's CLI v02.08.02.61 (the `slicer-dist` stage's,
#: `bambu-studio --load-settings … --load-filaments f1;f2;f3 --slice N --export-3mf`,
#: as `workers/rust` runs it) from a 3-plate file `write_plates_3mf` wrote with three
#: Bambu PLA Basic @BBL H2C filaments, on 2026-10-25 (#4711): plate 3 prints filament 3
#: alone; plate 2 prints filaments 2 and 3, one object each.
NATIVE = Path(__file__).parents[1] / "fixtures" / "native_slices"


def test_a_one_filament_plates_grams_are_on_the_filament_it_prints() -> None:
    """#4711: the CLI names a plate's one filament ``id="1"`` in ``slice_info.config``
    (with filament 1's colour) whatever filament it prints; the G-code's header
    (``; filament: 3``) and ``filament_sequence.json`` (``"sequence": [3]``) say which."""
    read = read_sliced((NATIVE / "plate3-one-filament.gcode.3mf").read_bytes(), 3)
    assert read.grams_by_slot == {3: 0.5}
    assert read.filament_used_g == 0.5


def test_what_a_plate_prints_before_its_first_change_is_its_first_filaments() -> None:
    """#4711: before a plate's first change the CLI counts its extrusion as filament
    1's, which the plate never loads: it is the first filament of the plate's sequence
    (``filament_sequence.json``), here filament 2."""
    read = read_sliced((NATIVE / "plate2-two-filaments.gcode.3mf").read_bytes(), 2)
    assert read.grams_by_slot == {2: 1.3, 3: 1.1}
    assert read.filament_used_g == 2.4


@pytest.mark.parametrize(
    "header",
    [b"", b"; HEADER_BLOCK_START\n; filament: 2,3\n; HEADER_BLOCK_END\n"],
    ids=["no-header", "several-and-no-sequence"],
)
def test_grams_stay_as_written_when_nothing_says_which_filament_is_first(header: bytes) -> None:
    """With no ``filament_sequence.json``, and a header naming no filament or several, the
    plate's first filament is unknown: its grams are left on the ids the slicer wrote."""
    file = sliced_3mf(plates={1: (60, [0.2, 1.0, 2.0])}, gcode={1: header + b"M620 S1A\n"})
    assert read_sliced(file, 1).grams_by_slot == {1: 0.2, 2: 1.0, 3: 2.0}


def test_a_sequence_too_deep_to_parse_reads_as_none() -> None:
    """A ``filament_sequence.json`` nested past the parser's recursion limit is no
    sequence: the plate still reads, its grams on the ids as written."""
    file = io.BytesIO(sliced_3mf(plates={1: (60, [0.2, 1.0])}, gcode={1: b"M620 S1A\n"}))
    with zipfile.ZipFile(file, "a") as archive:
        archive.writestr("Metadata/filament_sequence.json", "[" * 100_000 + "]" * 100_000)
    read = read_sliced(file.getvalue(), 1)
    assert (read.grams_by_slot, read.filament_changes) == ({1: 0.2, 2: 1.0}, 0)
