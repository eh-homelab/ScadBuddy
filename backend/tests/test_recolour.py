from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh

from scadbuddy.render.bambu3mf import (
    PLATE_THUMBNAIL,
    PROJECT_SETTINGS_NAME,
    write_bambu_3mf,
)
from scadbuddy.bambuddy.extruders import LEFT, RIGHT
from scadbuddy.render.recolour import pin_extruders_3mf, recolour_3mf
from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import render_plate_thumbnails
from tests.conftest import read_png


def _parts() -> list[ColourPart]:
    top = np.eye(4)
    top[:3, 3] = (0, 0, 3)
    return [
        ColourPart(1, "Color 1", "#1F6FEB", trimesh.creation.box(extents=(10, 10, 4))),
        ColourPart(2, "Color 2", "#FF6AC1", trimesh.creation.box(extents=(2, 2, 2), transform=top)),
    ]


def _written(tmp_path: Path, *, covers: bool = True) -> bytes:
    parts = _parts()
    out = tmp_path / "model.3mf"
    write_bambu_3mf(
        parts,
        out,
        thumbnails=render_plate_thumbnails(parts) if covers else None,
        model_name="two_boxes",
    )
    return out.read_bytes()


def _colours(payload: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        colours: list[str] = json.loads(archive.read(PROJECT_SETTINGS_NAME))["filament_colour"]
    return colours


def _cover(payload: bytes) -> np.ndarray:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return read_png(archive.read(PLATE_THUMBNAIL))


def test_the_chosen_colours_replace_the_models_own(tmp_path: Path) -> None:
    recoloured = recolour_3mf(_written(tmp_path), ["#00c000", "FF9425"])
    assert _colours(recoloured) == ["#00C000", "#FF9425"]


def test_the_cover_image_is_redrawn_in_the_chosen_colours(tmp_path: Path) -> None:
    """The slicer keeps the cover it is handed, so a stale one shows the model's colours
    on Bambuddy's queue while the print uses the spools' (#476)."""
    image = _cover(recolour_3mf(_written(tmp_path), ["#00C000", "#FF9425"]))
    opaque = image[..., 3] == 255
    red, green, blue = (image[..., channel].astype(int) for channel in range(3))
    assert (opaque & (green > red + 40) & (green > blue + 40)).any()  # the green base
    assert (opaque & (red > green + 40) & (green > blue + 40)).any()  # the orange top
    assert not (opaque & (blue > red + 40) & (blue > green + 40)).any()  # no blue left


def test_the_models_own_colours_give_back_the_same_file(tmp_path: Path) -> None:
    payload = _written(tmp_path)
    assert recolour_3mf(payload, ["#1F6FEB", "#FF6AC1"]) == payload


def test_a_file_without_covers_gets_none(tmp_path: Path) -> None:
    recoloured = recolour_3mf(_written(tmp_path, covers=False), ["#00C000", "#FF9425"])
    with zipfile.ZipFile(io.BytesIO(recoloured)) as archive:
        assert not any(name.endswith(".png") for name in archive.namelist())
    assert _colours(recoloured) == ["#00C000", "#FF9425"]


def test_a_colour_list_of_the_wrong_length_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="2 filament"):
        recolour_3mf(_written(tmp_path), ["#00C000"])


# --- #469: pinning each filament to the extruder its spool feeds ------------------------


def _settings(payload: bytes) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        settings: dict[str, Any] = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    return settings


def _with_settings(payload: bytes, **changes: Any) -> bytes:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    settings = json.loads(entries[PROJECT_SETTINGS_NAME])
    settings.update(changes)
    entries[PROJECT_SETTINGS_NAME] = json.dumps(settings).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        for name, data in entries.items():
            out.writestr(name, data)
    return buffer.getvalue()


def test_each_filament_is_pinned_to_its_spools_extruder(tmp_path: Path) -> None:
    """The H2C's ``physical_extruder_map`` is ``["1","0"]``: logical extruder 1 is the
    left (physical 1) and logical 2 the right (physical 0), so a filament on the right
    is ``"2"`` in ``filament_map``."""
    pinned = pin_extruders_3mf(_written(tmp_path), [RIGHT, LEFT])
    settings = _settings(pinned)
    assert settings["filament_map_mode"] == "Manual"
    assert settings["filament_map"] == ["2", "1"]
    assert settings["filament_colour"] == ["#1F6FEB", "#FF6AC1"]


def test_both_filaments_on_one_side_share_its_extruder(tmp_path: Path) -> None:
    assert _settings(pin_extruders_3mf(_written(tmp_path), [RIGHT, RIGHT]))["filament_map"] == [
        "2",
        "2",
    ]


def test_the_files_own_physical_extruder_map_wins(tmp_path: Path) -> None:
    payload = _with_settings(_written(tmp_path), physical_extruder_map=["0", "1"])
    assert _settings(pin_extruders_3mf(payload, [RIGHT, LEFT]))["filament_map"] == ["1", "2"]


def test_pinning_leaves_the_covers_alone(tmp_path: Path) -> None:
    payload = _written(tmp_path)
    assert _cover(pin_extruders_3mf(payload, [RIGHT, LEFT])).tobytes() == _cover(payload).tobytes()


def test_an_extruder_list_of_the_wrong_length_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="2 filament"):
        pin_extruders_3mf(_written(tmp_path), [RIGHT])
