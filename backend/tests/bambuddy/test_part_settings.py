"""#5571: slicer settings per filament slot, as a request takes them, as a run keys
them and as the laid-out 3MF carries them."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
import trimesh
from pydantic import ValidationError

from scadbuddy.bambuddy.part_settings import PartSettings, overrides_of, unknown_slots
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.runs import run_key
from scadbuddy.bambuddy.send import PART_SETTINGS_AS_IS, Target, _laid_out_for
from scadbuddy.core.problems import ApiError
from scadbuddy.render.bambu3mf import (
    MODEL_SETTINGS_NAME,
    PROJECT_SETTINGS_NAME,
    layout_of,
    printed_part_slots,
    set_part_settings,
    with_part_settings,
    write_bambu_3mf,
)
from scadbuddy.render.plate import plate_for
from scadbuddy.render.split import ColourPart


def body(**extra: Any) -> dict[str, Any]:
    return {
        "printer_id": 1,
        "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
        "choices": {"nozzles": [{"size": "0.4"}], "tier": "standard"},
        **extra,
    }


def test_each_setting_is_written_as_the_slicer_stores_it() -> None:
    settings = PartSettings(
        top_shell_layers=8,
        bottom_shell_layers=4,
        wall_loops=3,
        sparse_infill_density=25,
        sparse_infill_pattern="gyroid",
        ironing_type="top",
    )
    assert settings.metadata() == (
        ("bottom_shell_layers", "4"),
        ("ironing_type", "top"),
        ("sparse_infill_density", "25%"),
        ("sparse_infill_pattern", "gyroid"),
        ("top_shell_layers", "8"),
        ("wall_loops", "3"),
    )


@pytest.mark.parametrize(
    "settings",
    [
        {"top_shell_layers": -1},
        {"top_shell_layers": 1001},
        {"wall_loops": 1001},
        {"sparse_infill_density": 101},
        {"sparse_infill_pattern": "rectilinear"},
        {"ironing_type": "all"},
        {"infill_wall_overlap": "15%"},
    ],
)
def test_an_unknown_key_or_a_value_out_of_range_is_refused(settings: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PrintRunRequest.model_validate(body(part_settings={"2": settings}))


def test_slot_zero_is_refused() -> None:
    with pytest.raises(ValidationError):
        PrintRunRequest.model_validate(body(part_settings={"0": {"wall_loops": 3}}))


def test_part_settings_are_part_of_the_key_only_when_they_set_something() -> None:
    plain = PrintRunRequest.model_validate(body())
    empty = PrintRunRequest.model_validate(body(part_settings={}))
    unset = PrintRunRequest.model_validate(body(part_settings={"2": {}}))
    eight = PrintRunRequest.model_validate(body(part_settings={"2": {"top_shell_layers": 8}}))
    nine = PrintRunRequest.model_validate(body(part_settings={"2": {"top_shell_layers": 9}}))
    assert empty.part_settings is None and unset.part_settings is None
    key = run_key("a" * 32, plain)
    assert run_key("a" * 32, empty) == key
    assert run_key("a" * 32, unset) == key
    assert len({key, run_key("a" * 32, eight), run_key("a" * 32, nine)}) == 3


def test_overrides_are_in_slot_order_and_unknown_slots_are_named() -> None:
    request = PrintRunRequest.model_validate(
        body(part_settings={"3": {"wall_loops": 4}, "1": {"top_shell_layers": 6}})
    )
    assert overrides_of(request.part_settings) == (
        (1, (("top_shell_layers", "6"),)),
        (3, (("wall_loops", "4"),)),
    )
    assert unknown_slots(request.part_settings, {1, 2}) == [3]
    assert unknown_slots(request.part_settings, {1, 3}) == []


def test_a_copy_with_part_settings_keys_apart_from_one_without() -> None:
    plate = plate_for("H2C")
    plain = Target(plate, "0.4", colours=("#FFFFFF", "#800080"))
    eight = Target(
        plate, "0.4", colours=("#FFFFFF", "#800080"), part_settings=((2, (("wall_loops", "3"),)),)
    )
    # Without settings the key is the one copies were recorded under before #5571.
    assert "&parts:" not in plain.key
    assert plain.key != eight.key
    # The uncoloured key a project's Generate file is found by carries them too.
    assert plain.uncoloured_key != eight.uncoloured_key
    assert eight.key.startswith(eight.uncoloured_key)


SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<config>
 <object id="4">
  <metadata key="name" value="m"/>
  <metadata key="extruder" value="2"/>
  <part id="1" subtype="normal_part">
   <metadata key="name" value="Color 1"/>
   <metadata key="extruder" value="1"/>
   <metadata key="wall_loops" value="5"/>
  </part>
  <part id="2" subtype="normal_part">
   <metadata key="name" value="Color 2"/>
  </part>
  <part id="3" subtype="negative_part">
   <metadata key="name" value="Negative part"/>
   <metadata key="extruder" value="1"/>
  </part>
 </object>
 <plate>
  <metadata key="plater_id" value="1"/>
 </plate>
</config>
"""


def test_each_printed_part_of_a_slot_gets_its_settings() -> None:
    written = set_part_settings(
        SETTINGS,
        (
            (1, (("top_shell_layers", "8"), ("wall_loops", "3"))),
            (2, (("ironing_type", "top"),)),
        ),
    )
    assert written == SETTINGS.replace(
        '   <metadata key="wall_loops" value="5"/>\n  </part>',
        '   <metadata key="top_shell_layers" value="8"/>\n'
        '   <metadata key="wall_loops" value="3"/>\n  </part>',
    ).replace(
        '   <metadata key="name" value="Color 2"/>\n  </part>',
        # No extruder of its own: the object's, slot 2.
        '   <metadata key="name" value="Color 2"/>\n'
        '   <metadata key="ironing_type" value="top"/>\n  </part>',
    )


def test_a_slot_no_part_prints_with_changes_nothing() -> None:
    assert set_part_settings(SETTINGS, ((7, (("wall_loops", "3"),)),)) == SETTINGS


def test_only_the_slots_printed_parts_are_made_in_count() -> None:
    # Slot 1 and the object's slot 2; the negative part's slot 1 adds nothing.
    # A slot only painted on a part's faces (the file has 3 filaments, say) is no
    # part's, so it is not among them (#5571 review).
    assert printed_part_slots(SETTINGS) == {1, 2}


SELF_CLOSING = """<config>
 <object id="4">
  <metadata key="extruder" value="2"/>
  <part id="1" subtype="normal_part"/>
  <part id="2" subtype="normal_part">
   <metadata key="extruder" value="1"/>
  </part>
 </object>
</config>
"""


def test_a_self_closing_part_takes_its_settings_and_leaves_the_next_part_alone() -> None:
    assert printed_part_slots(SELF_CLOSING) == {1, 2}
    written = set_part_settings(SELF_CLOSING, ((2, (("wall_loops", "3"),)),))
    assert written == SELF_CLOSING.replace(
        '  <part id="1" subtype="normal_part"/>',
        '  <part id="1" subtype="normal_part">\n'
        '   <metadata key="wall_loops" value="3"/>\n'
        "  </part>",
    )
    # Slot 1's part keeps its own body; the self-closing one is not swallowed into it.
    written = set_part_settings(SELF_CLOSING, ((1, (("wall_loops", "4"),)),))
    assert written == SELF_CLOSING.replace(
        '   <metadata key="extruder" value="1"/>\n  </part>',
        '   <metadata key="extruder" value="1"/>\n'
        '   <metadata key="wall_loops" value="4"/>\n  </part>',
    )


def _two_colours(tmp_path: Path) -> bytes:
    base = trimesh.creation.box((40, 40, 4))
    block = trimesh.creation.box((20, 20, 6))
    block.apply_translation((0, 0, 5))
    out = tmp_path / "model.3mf"
    write_bambu_3mf(
        [ColourPart(1, "Color 1", "#FFFFFF", base), ColourPart(2, "Color 2", "#800080", block)],
        out,
        thumbnails=None,
    )
    return out.read_bytes()


def _model_settings(payload: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return archive.read(MODEL_SETTINGS_NAME).decode()


def test_the_laid_out_copy_carries_the_settings_and_nothing_else_changes(
    tmp_path: Path,
) -> None:
    payload = _two_colours(tmp_path)
    plate = plate_for("H2C")
    plain = _laid_out_for(payload, Target(plate, "0.4"))
    parts = _laid_out_for(
        payload, Target(plate, "0.4", part_settings=((2, (("top_shell_layers", "8"),)),))
    )
    settings = _model_settings(parts)
    assert settings == _model_settings(plain).replace(
        '   <metadata key="extruder" value="2"/>\n  </part>',
        '   <metadata key="extruder" value="2"/>\n'
        '   <metadata key="top_shell_layers" value="8"/>\n  </part>',
    )
    with zipfile.ZipFile(io.BytesIO(plain)) as a, zipfile.ZipFile(io.BytesIO(parts)) as b:
        for name in a.namelist():
            if name != MODEL_SETTINGS_NAME:
                assert a.read(name) == b.read(name), name
    assert with_part_settings(plain, None) == plain


def without_project_settings(payload: bytes) -> bytes:
    """``payload`` with no ``project_settings.config``: ``layout_of`` says ``as_is``."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as source, zipfile.ZipFile(out, "w") as copy:
        for info in source.infolist():
            if info.filename != PROJECT_SETTINGS_NAME:
                copy.writestr(info, source.read(info.filename))
    return out.getvalue()


def test_a_file_that_prints_as_it_is_refuses_part_settings(tmp_path: Path) -> None:
    """#5571 review: nothing is written into an ``as_is`` file, so a target with settings
    per part is refused rather than laid out under a key that names them."""
    payload = without_project_settings(_two_colours(tmp_path))
    assert layout_of(payload) == "as_is"
    plate = plate_for("H2C")
    assert _laid_out_for(payload, Target(plate, "0.4")) == payload
    with pytest.raises(ApiError) as refused:
        _laid_out_for(payload, Target(plate, "0.4", part_settings=((2, (("wall_loops", "3"),)),)))
    assert refused.value.status == 422
    assert refused.value.detail == PART_SETTINGS_AS_IS
