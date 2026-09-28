from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh

from scadbuddy.render.bambu3mf import (
    PLATE_THUMBNAIL,
    PROJECT_SETTINGS_NAME,
    write_bambu_3mf,
)
from scadbuddy.render.recolour import recolour_3mf
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
