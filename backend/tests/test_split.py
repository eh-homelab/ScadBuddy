from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh

from scadbuddy.render.split import MaterialSplitError, normalise_colour, split_by_material
from tests.conftest import write_openscad_3mf


def _translate(x: float, y: float, z: float) -> np.ndarray:
    matrix = np.eye(4)
    matrix[:3, 3] = (x, y, z)
    return matrix


def _two_boxes(path: Path) -> Path:
    return write_openscad_3mf(
        path,
        [
            ("Color 1", "#FF6AC100", trimesh.creation.box(extents=(10, 10, 4))),
            (
                "Color 2",
                "#1F6FEB00",
                trimesh.creation.box(
                    extents=(2, 2, 2),
                    transform=_translate(20, 0, 0),
                ),
            ),
        ],
    )


def test_normalise_colour_drops_the_bogus_alpha_byte() -> None:
    assert normalise_colour("#1f6feb00") == "#1F6FEB"
    assert normalise_colour("#FF6AC1") == "#FF6AC1"
    assert normalise_colour(None) == "#FFFFFF"


def test_split_returns_one_part_per_material_in_index_order(tmp_path: Path) -> None:
    parts = split_by_material(_two_boxes(tmp_path / "in.3mf"))
    assert [(p.material_index, p.name, p.colour) for p in parts] == [
        (1, "Color 1", "#FF6AC1"),
        (2, "Color 2", "#1F6FEB"),
    ]


def test_split_drops_the_unused_default_material(tmp_path: Path) -> None:
    parts = split_by_material(_two_boxes(tmp_path / "in.3mf"))
    assert all(part.name != "Default" for part in parts)


def test_split_parts_are_closed_and_keep_their_volume(tmp_path: Path) -> None:
    parts = split_by_material(_two_boxes(tmp_path / "in.3mf"))
    assert [part.watertight for part in parts] == [True, True]
    assert [len(part.mesh.vertices) for part in parts] == [8, 8]
    assert parts[0].mesh.volume == pytest.approx(400.0)
    assert parts[1].mesh.volume == pytest.approx(8.0)


def test_split_rejects_a_triangle_with_no_material(tmp_path: Path) -> None:
    path = _two_boxes(tmp_path / "in.3mf")
    with zipfile.ZipFile(path) as archive:
        model = archive.read("3D/3dmodel.model").decode()
    broken = tmp_path / "broken.3mf"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("3D/3dmodel.model", model.replace('p1="2"', 'p1="9"'))
    with pytest.raises(MaterialSplitError, match="unknown material"):
        split_by_material(broken)
