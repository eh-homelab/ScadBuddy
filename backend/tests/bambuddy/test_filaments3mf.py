"""#2550 — a library 3MF's filament slots, read from the file as Bambuddy reads them."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest

from scadbuddy.bambuddy.filaments3mf import (
    UnreadableFilamentsError,
    model_key,
    read_filaments,
    same_family,
)

SETTINGS = {
    "filament_colour": ["#FF0000", "#0000FF", "#FFFFFF"],
    "filament_type": ["PLA", "PETG", "PLA"],
}

MODEL_SETTINGS = """<config>
  <object id="1">
    <metadata key="extruder" value="1"/>
    <part id="1" subtype="normal_part"/>
    <part id="2" subtype="support_blocker"><metadata key="extruder" value="3"/></part>
  </object>
  <object id="2">
    <metadata key="extruder" value="2"/>
    <part id="3" subtype="normal_part"/>
  </object>
  <plate>
    <metadata key="plater_id" value="1"/>
    <model_instance><metadata key="object_id" value="1"/></model_instance>
  </plate>
  <plate>
    <metadata key="plater_id" value="2"/>
    <model_instance><metadata key="object_id" value="2"/></model_instance>
  </plate>
</config>"""

SLICE_INFO = """<config>
  <plate>
    <metadata key="index" value="1"/>
    <filament id="1" type="PLA" color="#FF0000" used_g="12.34"/>
    <filament id="3" type="PLA" color="#FFFFFF" used_g="0.00"/>
  </plate>
  <plate>
    <metadata key="index" value="2"/>
    <filament id="2" type="PETG" color="#0000FF" used_g="5"/>
  </plate>
</config>"""


def archive(entries: dict[str, Any]) -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for name, content in entries.items():
            zipped.writestr(name, content if isinstance(content, str) else json.dumps(content))
    buffer.seek(0)
    return buffer


def used(entries: dict[str, Any], plate: int | None) -> list[int]:
    answer = read_filaments(archive(entries)).for_plate(plate)
    return [f.slot_id for f in answer.filaments if f.used_in_plate]


def project(**extra: Any) -> dict[str, Any]:
    return {
        "3D/3dmodel.model": "<model/>",
        "Metadata/project_settings.config": SETTINGS,
        "Metadata/model_settings.config": MODEL_SETTINGS,
        **extra,
    }


def test_an_unsliced_project_answers_every_project_slot_in_order() -> None:
    answer = read_filaments(archive(project())).for_plate(None)

    assert [(f.slot_id, f.type, f.color, f.used_grams) for f in answer.filaments] == [
        (1, "PLA", "#FF0000", 0.0),
        (2, "PETG", "#0000FF", 0.0),
        (3, "PLA", "#FFFFFF", 0.0),
    ]
    assert all(f.used_in_plate for f in answer.filaments)


def test_a_plate_uses_its_parts_extruders_and_never_a_support_blocker_s() -> None:
    assert used(project(), 1) == [1]
    assert used(project(), 2) == [2]


def test_a_support_filament_counts_on_every_plate() -> None:
    settings = {**SETTINGS, "support_filament": "3", "support_interface_filament": "0"}

    entries = project(**{"Metadata/project_settings.config": settings})

    assert used(entries, 1) == [1, 3]
    assert used(entries, 2) == [2, 3]


def test_painting_makes_every_slot_count_as_used() -> None:
    """Which colours a painted part prints is the slicer's to work out: every slot is
    offered, as Bambuddy offers them when its preview slice says nothing."""
    painted = '<model><triangle v1="0" v2="1" v3="2" paint_color="8"/></model>'

    assert used(project(**{"3D/3dmodel.model": painted}), 2) == [1, 2, 3]


def test_a_sliced_project_answers_slice_info_s_used_filaments() -> None:
    entries = project(**{"Metadata/slice_info.config": SLICE_INFO})
    local = read_filaments(archive(entries))

    plate_1 = local.for_plate(1).filaments
    every = local.for_plate(None).filaments

    assert [(f.slot_id, f.used_grams) for f in plate_1] == [(1, 12.3)]
    assert [f.slot_id for f in every] == [1, 2]


@pytest.mark.parametrize(
    "payload",
    [
        io.BytesIO(b"not a zip archive"),
        archive({"Metadata/plate_1.gcode": "; sliced\n"}),
        archive({"Metadata/project_settings.config": "{not json"}),
    ],
    ids=["not-a-zip", "sliced", "bad-settings"],
)
def test_a_file_this_cannot_read_is_refused_for_bambuddy_to_answer(payload: io.BytesIO) -> None:
    with pytest.raises(UnreadableFilamentsError):
        read_filaments(payload)


SLICED_SETTINGS = {
    **SETTINGS,
    "printer_model": "Bambu Lab H2C",
    "printer_settings_id": "Bambu Lab H2C 0.4 nozzle",
    "print_settings_id": "0.20mm Standard @BBL H2C",
    "curr_bed_type": "Textured PEI Plate",
    "nozzle_diameter": ["0.40", "0.40"],
}

SLICED_INFO = """<config>
  <plate>
    <metadata key="index" value="2"/>
    <metadata key="nozzle_diameters" value="0.6 0.4"/>
    <metadata key="prediction" value="3600"/>
    <metadata key="weight" value="21.456"/>
    <filament id="2" type="PETG" color="#0000FF" used_g="21.456"/>
  </plate>
  <plate>
    <metadata key="index" value="3"/>
    <metadata key="prediction" value="60"/>
  </plate>
</config>"""


def test_a_sliced_file_says_what_it_was_sliced_for_and_which_plates_it_holds() -> None:
    """#2671: only plates with G-code count; a plate's own nozzle diameters win over the
    project's, and each is written as Bambuddy writes one."""
    read = read_filaments(
        archive(
            {
                "Metadata/project_settings.config": SLICED_SETTINGS,
                "Metadata/slice_info.config": SLICED_INFO,
                "Metadata/plate_2.gcode": "; sliced",
            }
        ),
        sliced=True,
    )

    profile = read.profile
    assert profile is not None
    assert (profile.printer_model, profile.process, profile.bed_type) == (
        "Bambu Lab H2C",
        "0.20mm Standard @BBL H2C",
        "Textured PEI Plate",
    )
    assert [(p.index, p.print_seconds, p.weight_g) for p in profile.plates] == [(2, 3600, 21.46)]
    assert profile.nozzle_diameters == ["0.6", "0.4"]
    assert [f.slot_id for f in read.for_plate(2).filaments] == [2]


def test_a_sliced_files_plate_with_no_slice_info_keeps_the_projects_nozzle() -> None:
    read = read_filaments(
        archive(
            {
                "Metadata/project_settings.config": SLICED_SETTINGS,
                "Metadata/plate_1.gcode": "; sliced",
            }
        ),
        sliced=True,
    )

    assert read.profile is not None
    assert [p.index for p in read.profile.plates] == [1]
    assert read.profile.diameters_for([1]) == ["0.4"]


@pytest.mark.parametrize(
    ("entries", "sliced", "reason"),
    [
        pytest.param({"Metadata/plate_1.gcode": ";"}, False, "sliced already", id="unasked"),
        pytest.param({}, True, "no G-code", id="not-sliced"),
    ],
)
def test_a_file_read_as_the_wrong_kind_is_unreadable(
    entries: dict[str, Any], sliced: bool, reason: str
) -> None:
    with pytest.raises(UnreadableFilamentsError, match=reason):
        read_filaments(
            archive({"Metadata/project_settings.config": SETTINGS, **entries}), sliced=sliced
        )


@pytest.mark.parametrize(
    ("studio", "bambuddy", "same"),
    [
        ("Bambu Lab H2C", "H2C", True),
        ("Bambu Lab X1 Carbon", "X1C", True),
        ("Bambu Lab A1 mini", "A1 Mini", True),
        ("Bambu Lab X1 Carbon", "H2C", False),
        ("Bambu Lab P1S", "P1P", False),
    ],
)
def test_printer_models_compare_across_bambu_studio_and_bambuddy(
    studio: str, bambuddy: str, same: bool
) -> None:
    assert (model_key(studio) == model_key(bambuddy)) is same


#: Every model Bambu Studio writes as ``printer_model`` (``machine_model_list`` in
#: bambulab/BambuStudio@47a13eeb ``resources/profiles/BBL.json``), beside the short name
#: Bambuddy stores as ``Printer.model`` for it (``PRINTER_MODEL_MAP`` in
#: maziggy/bambuddy@943f3587 ``backend/app/utils/printer_models.py``) (#2956).
STUDIO_TO_BAMBUDDY = {
    "Bambu Lab X1 Carbon": "X1C",
    "Bambu Lab X1": "X1",
    "Bambu Lab X1E": "X1E",
    "Bambu Lab P1P": "P1P",
    "Bambu Lab P1S": "P1S",
    "Bambu Lab A1 mini": "A1 Mini",
    "Bambu Lab A1": "A1",
    "Bambu Lab H2D": "H2D",
    "Bambu Lab H2D Pro": "H2D Pro",
    "Bambu Lab H2S": "H2S",
    "Bambu Lab P2S": "P2S",
    "Bambu Lab H2C": "H2C",
    "Bambu Lab X2D": "X2D",
    "Bambu Lab A2L": "A2L",
    # Bambuddy's note: newer cloud presets write the short form (its #1649).
    "Bambu Lab A1M": "A1 Mini",
}
#: The code a printer announces over SSDP (``DevModel``), which Bambuddy may keep as
#: ``Printer.model``, beside its short name (``PRINTER_MODEL_ID_MAP``, same file).
DEVICE_CODES = {
    "BL-P001": "X1C",
    "BL-P002": "X1",
    "C13": "X1E",
    "C11": "P1P",
    "C12": "P1S",
    "N7": "P2S",
    "N6": "X2D",
    "N9": "A2L",
    "A11": "A1",
    "A12": "A1 Mini",
    "N1": "A1 Mini",
    "N2S": "A1",
    "A04": "A1 Mini",
    "O1D": "H2D",
    "O1E": "H2D Pro",
    "O2D": "H2D Pro",
    "O1C": "H2C",
    "O1C2": "H2C",
    "O1S": "H2S",
}


@pytest.mark.parametrize(("studio", "bambuddy"), STUDIO_TO_BAMBUDDY.items())
def test_every_model_bambu_studio_names_matches_bambuddys_name_for_it(
    studio: str, bambuddy: str
) -> None:
    """#2956: a file sliced for a model fits a printer Bambuddy names that model, and
    no other model's name."""
    assert model_key(studio) == model_key(bambuddy)
    others = {name for name in STUDIO_TO_BAMBUDDY.values() if name != bambuddy}
    assert all(model_key(studio) != model_key(other) for other in others)


@pytest.mark.parametrize(("code", "bambuddy"), DEVICE_CODES.items())
def test_a_printer_bambuddy_keeps_by_its_device_code_matches_its_model(
    code: str, bambuddy: str
) -> None:
    assert model_key(code) == model_key(bambuddy)


def test_a_name_neither_table_lists_is_not_matched_to_another() -> None:
    """A difference no source accounts for (a "Combo" suffix, say) is not matched, so
    the file is held for a manual start: the safe side."""
    assert model_key("Bambu Lab P1S Combo") != model_key("P1S")


@pytest.mark.parametrize(
    ("a", "b", "family"),
    [
        ("Bambu Lab X1 Carbon", "P1S", True),
        ("Bambu Lab X1E", "X1C", True),
        ("C11", "Bambu Lab X1", True),
        ("Bambu Lab X1 Carbon", "X1C", False),  # one model, not a family match
        ("Bambu Lab X1 Carbon", "H2C", False),
        ("Bambu Lab A1 mini", "A1", False),  # Bambuddy lists no A1 family
        ("Bambu Lab P2S", "P1S", False),
    ],
)
def test_only_bambuddys_g_code_family_counts_as_another_model_that_fits(
    a: str, b: str, family: bool
) -> None:
    """#2956: ``GCODE_COMPAT_FAMILIES`` (Bambuddy, same file) is the X1 and P1 series."""
    assert same_family(model_key(a), model_key(b)) is family
