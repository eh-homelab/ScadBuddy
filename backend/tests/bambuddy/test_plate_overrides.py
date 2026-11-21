"""#6820 part 3: a print's ``process_overrides`` win over the values a plate of the file
states in ``Metadata/model_settings.config``, which Bambu Studio's CLI applies over the
process file (``--load-settings``)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.send import Target, _laid_out_for
from scadbuddy.render.bambu3mf import (
    MODEL_SETTINGS_NAME,
    plate_overrides,
    set_plate_settings,
    with_plate_settings,
)
from scadbuddy.render.plate import plate_for
from tests.bambuddy.test_part_settings import author_two_colours

TWO_PLATES = """<?xml version="1.0" encoding="UTF-8"?>
<config>
  <object id="2">
    <metadata key="name" value="Spool"/>
    <metadata key="print_sequence" value="by object"/>
    <part id="1" subtype="normal_part">
      <metadata key="extruder" value="1"/>
    </part>
  </object>
  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="print_sequence" value="by object"/>
    <metadata key="filament_map_mode" value="Auto For Flush"/>
    <metadata key="filament_maps" value="1 1"/>
    <model_instance>
      <metadata key="object_id" value="2"/>
    </model_instance>
  </plate>
  <plate>
    <metadata key="plater_id" value="2"/>
    <metadata key="print_sequence" value="by object"/>
    <metadata key="bed_type" value="Cool Plate"/>
  </plate>
</config>
"""


def _plates(model_settings: str) -> list[dict[str, str]]:
    return [
        {m.get("key", ""): m.get("value", "") for m in plate.findall("metadata")}
        for plate in ET.fromstring(model_settings).iter("plate")
    ]


def test_a_key_every_plate_states_is_rewritten_on_every_plate() -> None:
    written = set_plate_settings(TWO_PLATES, (("print_sequence", "by layer"),))
    assert [plate["print_sequence"] for plate in _plates(written)] == ["by layer", "by layer"]
    # Only the plates: the object's own key, and everything else, stay byte for byte.
    assert written == TWO_PLATES.replace(
        '    <metadata key="print_sequence" value="by object"/>\n    <metadata key="fil',
        '    <metadata key="print_sequence" value="by layer"/>\n    <metadata key="fil',
    ).replace(
        '"2"/>\n    <metadata key="print_sequence" value="by object"/>',
        '"2"/>\n    <metadata key="print_sequence" value="by layer"/>',
    )


def test_a_key_no_plate_states_leaves_the_file_unchanged() -> None:
    assert set_plate_settings(TWO_PLATES, (("spiral_mode", "true"),)) == TWO_PLATES


def test_a_key_one_plate_states_is_rewritten_there_only() -> None:
    plates = _plates(set_plate_settings(TWO_PLATES, (("bed_type", "Textured PEI Plate"),)))
    assert "bed_type" not in plates[0]
    assert plates[1]["bed_type"] == "Textured PEI Plate"


def test_overrides_take_the_plates_own_keys_and_formats() -> None:
    """The plate's reader (``bbs_3mf.cpp`` v02.08.02.61, ``_handle_start_config_metadata``)
    stores ``bed_type`` as ``curr_bed_type`` and ``filament_maps`` as ``filament_map``,
    reads an int list split on blanks and ``spiral_mode`` with ``std::boolalpha``."""
    assert plate_overrides(
        {
            "curr_bed_type": "Textured PEI Plate",
            "filament_map": ["2", "2"],
            "filament_volume_map": "0,1",
            "spiral_mode": "1",
            "first_layer_print_sequence": "1, 2",
            "print_sequence": "by layer",
            "sparse_infill_density": "15%",
        }
    ) == (
        ("bed_type", "Textured PEI Plate"),
        ("filament_maps", "2 2"),
        ("filament_volume_maps", "0 1"),
        ("first_layer_print_sequence", "1 2"),
        ("print_sequence", "by layer"),
        ("spiral_mode", "true"),
    )
    assert plate_overrides({"sparse_infill_density": "15%"}) is None
    assert plate_overrides({}) is None


def test_a_host_side_key_is_never_a_plate_key() -> None:
    assert plate_overrides({"post_process": "rm -rf /", "plater_id": "9"}) is None


def test_a_request_override_beats_the_plate_and_the_pinned_map(tmp_path: Path) -> None:
    """The explicit override wins over ScadBuddy's own pinned plate map (#3955) too."""
    payload = author_two_colours(tmp_path, TWO_PLATES)
    request = PrintRunRequest.model_validate(
        {
            "printer_id": 1,
            "filament_plan": {"slots": [{"slot_id": 1, "spool_id": 9}]},
            "choices": {"nozzles": [{"size": "0.4"}], "tier": "standard"},
            "process_overrides": {"filament_map": "2,2", "print_sequence": "by layer"},
        }
    )
    target = Target(plate_for("H2C"), "0.4", plate_settings=plate_overrides(request.overrides()))
    laid = _laid_out_for(payload, target)
    with zipfile.ZipFile(io.BytesIO(laid)) as archive:
        plates = _plates(archive.read(MODEL_SETTINGS_NAME).decode())
    assert [plate["print_sequence"] for plate in plates] == ["by layer", "by layer"]
    assert plates[0]["filament_maps"] == "2 2"


def test_no_plate_settings_leave_the_payload_as_it_is(tmp_path: Path) -> None:
    payload = author_two_colours(tmp_path, TWO_PLATES)
    assert with_plate_settings(payload, None) == payload
    assert with_plate_settings(payload, ()) == payload


def test_the_copy_key_differs_with_the_overrides() -> None:
    plate = plate_for("H2C")
    plain = Target(plate, "0.4", colours=("#FFFFFF",))
    by_layer = Target(
        plate, "0.4", colours=("#FFFFFF",), plate_settings=(("print_sequence", "by layer"),)
    )
    by_object = Target(
        plate, "0.4", colours=("#FFFFFF",), plate_settings=(("print_sequence", "by object"),)
    )
    keys = {plain.key, by_layer.key, by_object.key}
    assert len(keys) == 3
    assert plain.key.startswith(plain.uncoloured_key)
    assert by_layer.uncoloured_key != plain.uncoloured_key
    assert by_layer.key.startswith(by_layer.uncoloured_key)
