"""#769: the presets a download names, written over the placeholders."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import trimesh

from scadbuddy.bambuddy.download import ProjectPresets, with_presets
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.split import ColourPart

PRESETS = ProjectPresets(
    printer_settings_id="Bambu Lab H2C 0.2 nozzle",
    print_settings_id="0.08mm High Quality @BBL H2C 0.2 nozzle",
    filament_settings_id=[
        "Bambu PLA Basic @BBL H2C 0.2 nozzle",
        "Bambu PLA Silk @BBL H2C 0.2 nozzle",
    ],
    nozzle_diameter=["0.2", "0.2"],
    printer_model="Bambu Lab H2C",
)


def _written(tmp_path: Path) -> bytes:
    parts = [
        ColourPart(1, "Pink", "#FF6AC1", trimesh.creation.box(extents=(10, 10, 5))),
        ColourPart(2, "Blue", "#1F6FEB", trimesh.creation.box(extents=(4, 4, 8))),
    ]
    out = tmp_path / "model.3mf"
    write_bambu_3mf(parts, out, thumbnails=None, model_name="demo")
    return out.read_bytes()


def _settings(payload: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        loaded: dict[str, Any] = json.loads(archive.read("Metadata/project_settings.config"))
        return loaded


def test_the_real_presets_replace_the_placeholders(tmp_path: Path) -> None:
    before = _written(tmp_path)

    settings = _settings(with_presets(before, PRESETS))

    assert settings["printer_settings_id"] == "Bambu Lab H2C 0.2 nozzle"
    assert settings["print_settings_id"] == "0.08mm High Quality @BBL H2C 0.2 nozzle"
    assert settings["filament_settings_id"] == PRESETS.filament_settings_id
    assert settings["nozzle_diameter"] == ["0.2", "0.2"]
    assert settings["printer_model"] == "Bambu Lab H2C"
    # Every other key the file had, the five the slicer dereferences among them.
    assert settings.keys() >= _settings(before).keys()
    assert settings["printable_height"] == _settings(before)["printable_height"]
    assert settings["filament_colour"] == ["#FF6AC1", "#1F6FEB"]


def test_print_settings_are_written_as_edits_to_the_system_process(tmp_path: Path) -> None:
    """#770, in the shape the maintainer's own Bambu Studio save has:
    ``["enable_prime_tower;wipe_tower_no_sparse_layers", "", "", ""]`` for two filaments."""
    before = _written(tmp_path)

    settings = _settings(
        with_presets(
            before,
            PRESETS,
            {"enable_prime_tower": "1", "wipe_tower_no_sparse_layers": "1"},
        )
    )

    assert settings["print_settings_id"] == PRESETS.print_settings_id
    assert settings["enable_prime_tower"] == "1"
    assert settings["wipe_tower_no_sparse_layers"] == "1"
    assert settings["different_settings_to_system"] == [
        "enable_prime_tower;wipe_tower_no_sparse_layers",
        "",
        "",
        "",
    ]


def test_print_settings_alone_keep_the_placeholders(tmp_path: Path) -> None:
    before = _written(tmp_path)

    settings = _settings(with_presets(before, None, {"enable_support": "0"}))

    added = {"enable_support", "different_settings_to_system"}
    assert {key: value for key, value in settings.items() if key not in added} == {
        key: value for key, value in _settings(before).items() if key not in added
    }
    assert settings["enable_support"] == "0"
    assert settings["different_settings_to_system"] == ["enable_support", "", "", ""]


def test_no_print_settings_add_no_edit_list(tmp_path: Path) -> None:
    assert "different_settings_to_system" not in _settings(
        with_presets(_written(tmp_path), PRESETS)
    )


def test_every_other_entry_is_kept_byte_for_byte(tmp_path: Path) -> None:
    before = _written(tmp_path)
    after = with_presets(before, PRESETS)
    with zipfile.ZipFile(io.BytesIO(before)) as old, zipfile.ZipFile(io.BytesIO(after)) as new:
        assert new.namelist() == old.namelist()
        for name in old.namelist():
            if name != "Metadata/project_settings.config":
                assert new.read(name) == old.read(name)
