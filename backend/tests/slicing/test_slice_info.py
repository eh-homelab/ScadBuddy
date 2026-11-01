import io
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.core.problems import ApiError
from scadbuddy.slicing.slice_info import filament_changes, plate_gcode, read_sliced
from tests.support.sliced3mf import sliced_3mf, with_corrupt_member

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


def test_a_sequence_the_header_disagrees_with_leaves_the_grams_as_written() -> None:
    """#4716 review: plate 2's header names filaments 2 and 3; a sequence starting with
    filament 4 says nothing of which is first, so the phantom ``id="1"`` stays put."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(NATIVE / "plate2-two-filaments.gcode.3mf") as original,
        zipfile.ZipFile(out, "w") as archive,
    ):
        for name in original.namelist():
            data = original.read(name)
            if name == "Metadata/filament_sequence.json":
                sequences = json.loads(data)
                sequences["plate_2"]["sequence"] = [4, 2, 3]
                data = json.dumps(sequences).encode()
            archive.writestr(name, data)
    assert read_sliced(out.getvalue(), 2).grams_by_slot == {1: 0.1, 2: 1.2, 3: 1.1}


def test_a_gcode_that_cannot_be_inflated_reads_as_unknown_changes() -> None:
    """#4716 review: a corrupt G-code member costs only what is read from it (the
    changes); the grams still go on the filament the sequence says the plate prints."""
    payload = with_corrupt_member(
        (NATIVE / "plate3-one-filament.gcode.3mf").read_bytes(), "Metadata/plate_3.gcode"
    )
    read = read_sliced(payload, 3)
    assert (read.grams_by_slot, read.filament_changes) == ({3: 0.5}, None)


#: #4232: a plate sliced by the slicer image's CLI v02.08.02.61
#: (`ghcr.io/eh-homelab/scadbuddy-slicer:main`, `/opt/bambu-studio/AppRun --load-settings`
#: the flattened "Bambu Lab H2D 0.4 nozzle" and "0.20mm Standard @BBL H2D",
#: `--load-filaments` "Bambu PLA Basic @BBL H2D" twice, `--arrange 1 --slice 1
#: --export-3mf`) from two boxes `write_bambu_3mf` wrote in two colours, on 2026-11-01. The
#: CLI put filament 1 in group 1, whose nozzle is on slicer extruder 2, and filament 2 in
#: group 0 on extruder 1 (``filament_maps`` "2 1"); ``physical_extruder_map`` ["1", "0"]
#: makes slicer extruder 2 the physical 0, the right.
H2D = NATIVE / "h2d-two-filaments.gcode.3mf"


def test_each_slot_has_the_side_its_hotend_is_on() -> None:
    """#4232: a filament's ``group_id`` names its nozzle, the nozzle its slicer extruder,
    and ``physical_extruder_map`` that extruder's physical one: 0 is the right."""
    assert read_sliced(H2D.read_bytes(), 1).side_by_slot == {1: "R", 2: "L"}


def test_the_h2c_rack_is_the_right_side() -> None:
    """#4232: on the H2C the rack (slicer extruder 2) is the right side, as the Bambuddy
    path reads ``on_rack``. The phantom ``id="1"`` (#4711) has the side of the filament it
    is, which its own group gives."""
    read = read_sliced((NATIVE / "plate2-two-filaments.gcode.3mf").read_bytes(), 2)
    assert read.side_by_slot == {2: "R", 3: "L"}
    one = read_sliced((NATIVE / "plate3-one-filament.gcode.3mf").read_bytes(), 3)
    assert one.side_by_slot == {3: "R"}


def _edited(path: Path, member: str, edit: Callable[[bytes], bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(path) as original, zipfile.ZipFile(out, "w") as archive:
        for name in original.namelist():
            data = original.read(name)
            archive.writestr(name, edit(data) if name == member else data)
    return out.getvalue()


def test_a_slot_written_on_two_sides_has_no_side() -> None:
    """The phantom ``id="1"`` is slot 2; put on the other side than slot 2's own filament,
    nothing says which side slot 2 is on."""
    phantom = b'used_g="0.15" group_id="1"'
    payload = _edited(
        NATIVE / "plate2-two-filaments.gcode.3mf",
        "Metadata/slice_info.config",
        lambda data: data.replace(phantom, b'used_g="0.15" group_id="0"'),
    )
    assert read_sliced(payload, 2).side_by_slot == {3: "L"}


def _settings(edit: Callable[[dict[str, Any]], None]) -> Callable[[bytes], bytes]:
    def apply(data: bytes) -> bytes:
        settings = json.loads(data)
        edit(settings)
        return json.dumps(settings).encode()

    return apply


@pytest.mark.parametrize(
    "edit",
    [
        lambda s: s.pop("physical_extruder_map"),
        lambda s: s.update(physical_extruder_map=["0"]),
        lambda s: s.update(physical_extruder_map=["1", "7"]),
        lambda s: s.update(physical_extruder_map=["0", "0"]),
        lambda s: s.update(physical_extruder_map="1 0"),
    ],
    ids=["no-map", "one-extruder", "unknown-extruder", "one-side-twice", "not-a-list"],
)
def test_no_side_without_a_left_and_a_right(edit: Callable[[dict[str, Any]], None]) -> None:
    """A single-nozzle printer, or a map that does not name one left and one right, has
    no sides: never quietly the right-hand one."""
    payload = _edited(H2D, "Metadata/project_settings.config", _settings(edit))
    assert read_sliced(payload, 1).side_by_slot == {}


def test_a_group_with_no_nozzle_has_no_side() -> None:
    payload = _edited(
        H2D,
        "Metadata/slice_info.config",
        lambda data: data.replace(
            b'<nozzle id="1" extruder_id="2"', b'<nozzle id="9" extruder_id="2"'
        ),
    )
    assert read_sliced(payload, 1).side_by_slot == {2: "L"}


def test_a_file_with_no_nozzle_table_has_no_sides() -> None:
    assert read_sliced(ONE, 1).side_by_slot == {}
