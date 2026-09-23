from __future__ import annotations

from pathlib import Path

import pytest
import trimesh

from scadbuddy.render.glb import bounding_box, write_glb
from scadbuddy.render.split import ColourPart


def _parts() -> list[ColourPart]:
    return [
        ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box(extents=(10, 20, 4))),
        ColourPart(
            2,
            "Color 2",
            "#1F6FEB",
            trimesh.creation.box(
                extents=(2, 2, 2),
                transform=trimesh.transformations.translation_matrix([0, 0, 3]),
            ),
        ),
    ]


def test_bounding_box_is_the_assembly_in_millimetres() -> None:
    box = bounding_box(_parts())
    assert box.min == (-5.0, -10.0, -2.0)
    assert box.max == (5.0, 10.0, 4.0)
    assert box.size == (10.0, 20.0, 6.0)


def test_write_glb_emits_one_mesh_per_colour(tmp_path: Path) -> None:
    out = tmp_path / "preview.glb"
    write_glb(_parts(), out)
    assert out.read_bytes()[:4] == b"glTF"

    scene = trimesh.load(out, file_type="glb")
    assert isinstance(scene, trimesh.Scene)
    assert sorted(scene.geometry) == ["Color 1", "Color 2"]


def test_glb_materials_carry_the_part_colour(tmp_path: Path) -> None:
    out = tmp_path / "preview.glb"
    write_glb(_parts(), out)
    scene = trimesh.load(out, file_type="glb")
    assert isinstance(scene, trimesh.Scene)
    colours = {
        name: tuple(int(c) for c in mesh.visual.material.baseColorFactor[:3])
        for name, mesh in scene.geometry.items()
    }
    assert colours == {"Color 1": (255, 106, 193), "Color 2": (31, 111, 235)}


def test_glb_is_y_up(tmp_path: Path) -> None:
    out = tmp_path / "preview.glb"
    write_glb(_parts(), out)
    scene = trimesh.load(out, file_type="glb")
    assert isinstance(scene, trimesh.Scene)
    # 10 x 20 x 6 in OpenSCAD's Z-up becomes 10 x 6 x 20 in glTF's Y-up.
    assert [round(float(v), 3) for v in scene.extents] == [10.0, 6.0, 20.0]


def test_write_glb_returns_the_z_up_bounding_box(tmp_path: Path) -> None:
    assert write_glb(_parts(), tmp_path / "preview.glb") == bounding_box(_parts())


def test_empty_part_list_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one colour part"):
        write_glb([], tmp_path / "preview.glb")
