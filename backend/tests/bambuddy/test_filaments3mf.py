"""#2550 — a library 3MF's filament slots, read from the file as Bambuddy reads them."""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest

from scadbuddy.bambuddy.filaments3mf import UnreadableFilamentsError, read_filaments

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
