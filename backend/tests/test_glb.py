from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
import trimesh

from scadbuddy.render.glb import bounding_box, read_glb, write_glb
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


def _srgb_to_linear(byte: int) -> float:
    channel = byte / 255
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _material_factors(glb: Path) -> dict[str, list[float]]:
    """Each mesh's `baseColorFactor` as the file holds it, read off the JSON chunk."""
    data = glb.read_bytes()
    length = struct.unpack("<I", data[12:16])[0]
    tree = json.loads(data[20 : 20 + length])
    return {
        mesh["name"]: tree["materials"][mesh["primitives"][0]["material"]]["pbrMetallicRoughness"][
            "baseColorFactor"
        ]
        for mesh in tree["meshes"]
    }


def test_glb_materials_carry_the_part_colour_as_linear(tmp_path: Path) -> None:
    """#1319: glTF's `baseColorFactor` is linear; the hex a template picks is sRGB."""
    out = tmp_path / "preview.glb"
    write_glb(_parts(), out)

    factors = _material_factors(out)

    expected = {"Color 1": (255, 106, 193), "Color 2": (31, 111, 235)}
    for name, rgb in expected.items():
        assert factors[name][:3] == pytest.approx(
            [_srgb_to_linear(byte) for byte in rgb], abs=1 / 255
        )
        assert factors[name][3] == 1.0


def test_a_dark_colour_reads_back_exactly(tmp_path: Path) -> None:
    """Linear light crushes the darks into a few 8-bit steps; the hex must survive."""
    out = tmp_path / "preview.glb"
    parts = [ColourPart(1, "Color 1", "#0A0B0C", trimesh.creation.box(extents=(1, 1, 1)))]
    write_glb(parts, out)

    assert [part.colour for part in read_glb(out)] == ["#0A0B0C"]


def test_a_preview_written_before_1319_reads_back_its_srgb_colour(tmp_path: Path) -> None:
    """A saved output's GLB from before the fix holds the sRGB bytes as the factor."""
    out = tmp_path / "preview.glb"
    scene = trimesh.Scene()
    mesh = trimesh.creation.box(extents=(1, 1, 1))
    mesh.visual = trimesh.visual.TextureVisuals(
        material=trimesh.visual.material.PBRMaterial(baseColorFactor=[0, 71, 187, 255])
    )
    scene.add_geometry(mesh, geom_name="Color 1")
    out.write_bytes(scene.export(file_type="glb"))

    assert [part.colour for part in read_glb(out)] == ["#0047BB"]


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


def test_a_preview_reads_back_as_the_parts_it_was_written_from(tmp_path: Path) -> None:
    """Colours, names and Z-up millimetres: what a named view is drawn from (#252)."""
    out = tmp_path / "preview.glb"
    write_glb(_parts(), out)

    parts = read_glb(out)

    assert [(part.name, part.colour) for part in sorted(parts, key=lambda p: p.name)] == [
        ("Color 1", "#FF6AC1"),
        ("Color 2", "#1F6FEB"),
    ]
    assert bounding_box(parts) == bounding_box(_parts())
