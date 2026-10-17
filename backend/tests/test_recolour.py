from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
import pytest
import trimesh

from scadbuddy.render import bambu3mf, zipcopy
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


def test_recolouring_copies_what_it_does_not_change_and_writes_nothing_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3679: the meshes are copied as their compressed bytes, not deflated again, and
    the covers are drawn from the recoloured file in memory, not from a copy on disk."""
    payload = _written(tmp_path)
    copied: list[str] = []
    real = zipcopy.copy_raw

    def spy(source: BinaryIO, info: zipfile.ZipInfo, *args: Any, **kwargs: Any) -> None:
        copied.append(info.filename)
        real(source, info, *args, **kwargs)

    def no_disk(*_: object, **__: object) -> None:
        raise AssertionError("recolouring wrote a temporary file")

    monkeypatch.setattr(bambu3mf, "copy_raw", spy)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", no_disk)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", no_disk)
    recoloured = recolour_3mf(payload, ["#00C000", "#FF9425"])

    assert {"3D/Objects/object_1.model", "3D/Objects/object_2.model"} <= set(copied)
    assert PROJECT_SETTINGS_NAME not in copied
    assert _colours(recoloured) == ["#00C000", "#FF9425"]
    assert not np.array_equal(_cover(recoloured), _cover(payload))
