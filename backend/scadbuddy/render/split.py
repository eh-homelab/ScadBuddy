from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np
import trimesh

from scadbuddy.render.paint import remap, states

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
MODEL_ENTRY = "3D/3dmodel.model"
DEFAULT_MATERIAL_NAME = "Default"
#: Bambu Studio's per-triangle painting attribute (`scadbuddy.render.paint`).
PAINT_ATTRIBUTE = "paint_color"

_CORE = f"{{{CORE_NS}}}"


class MaterialSplitError(ValueError):
    pass


@dataclass(frozen=True)
class Paint:
    """Bambu Studio's per-triangle painting of a part (#1965): each face's
    ``paint_color`` (`scadbuddy.render.paint`), ``""`` for a face left the part's own
    colour, and the colour each extruder number in them names (extruder ``n`` is
    ``colours[n - 1]``). The faces are the part's mesh's, in its order."""

    codes: tuple[str, ...]
    colours: tuple[str, ...]

    def used(self) -> list[str]:
        """The colours the painting prints with, in extruder order."""
        found = sorted({state for code in self.codes if code for state in states(code)})
        if found and found[-1] > len(self.colours):
            raise ValueError(f"the painting names extruder {found[-1]}, which has no colour")
        return list(dict.fromkeys(self.colours[state - 1] for state in found))

    def renumbered(self, colours: Sequence[str]) -> Paint:
        """The same painting with its extruder numbers indexing ``colours`` instead,
        each of the painting's colours found in it (`ValueError` if one is not)."""
        wanted = [colour.upper() for colour in colours]
        mapping: dict[int, int] = {}
        for state, colour in enumerate(self.colours, start=1):
            if colour.upper() in wanted:
                mapping[state] = wanted.index(colour.upper()) + 1
        missing = [c for c in self.used() if c.upper() not in wanted]
        if missing:
            raise ValueError(f"the painting's colour {missing[0]} is not one of the filaments")
        return Paint(
            tuple(remap(code, mapping) if code else "" for code in self.codes),
            tuple(wanted),
        )


@dataclass(frozen=True)
class ColourPart:
    material_index: int
    name: str
    colour: str
    mesh: trimesh.Trimesh
    #: Its faces' painting in other colours, when it is painted (#1965).
    paint: Paint | None = None

    @property
    def watertight(self) -> bool:
        return bool(self.mesh.is_watertight)


def normalise_colour(displaycolor: str | None) -> str:
    if not displaycolor:
        return "#FFFFFF"
    return "#" + displaycolor.lstrip("#")[:6].upper()


def _materials(root: ET.Element) -> dict[tuple[str, int], tuple[str, str]]:
    materials: dict[tuple[str, int], tuple[str, str]] = {}
    for group in root.iter(f"{_CORE}basematerials"):
        group_id = group.get("id", "")
        for index, base in enumerate(group.findall(f"{_CORE}base")):
            name = base.get("name") or f"Material {index}"
            materials[(group_id, index)] = (name, normalise_colour(base.get("displaycolor")))
    return materials


def _vertices(mesh: ET.Element) -> np.ndarray:
    node = mesh.find(f"{_CORE}vertices")
    if node is None:
        raise MaterialSplitError("mesh has no vertices")
    return np.array(
        [[float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))] for v in node],
        dtype=np.float64,
    )


def split_by_material(path: Path) -> list[ColourPart]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read(MODEL_ENTRY))

    materials = _materials(root)
    grouped: dict[tuple[str, int], list[trimesh.Trimesh]] = {}
    # Each group's faces' paint codes, in the order its meshes are joined (#1965).
    codes: dict[tuple[str, int], list[str]] = {}
    for obj in root.iter(f"{_CORE}object"):
        mesh_node = obj.find(f"{_CORE}mesh")
        if mesh_node is None:
            continue
        vertices = _vertices(mesh_node)
        triangles = mesh_node.find(f"{_CORE}triangles")
        object_pid = obj.get("pid", "")
        object_index = int(obj.get("pindex", 0))
        buckets: dict[tuple[str, int], list[tuple[int, int, int]]] = {}
        painted: dict[tuple[str, int], list[str]] = {}
        for triangle in triangles if triangles is not None else []:
            key = (
                triangle.get("pid", object_pid),
                int(triangle.get("p1", object_index)),
            )
            buckets.setdefault(key, []).append(
                (
                    int(triangle.get("v1", 0)),
                    int(triangle.get("v2", 0)),
                    int(triangle.get("v3", 0)),
                )
            )
            painted.setdefault(key, []).append(triangle.get(PAINT_ATTRIBUTE) or "")
        for key, faces in buckets.items():
            part = trimesh.Trimesh(vertices=vertices.copy(), faces=np.array(faces), process=False)
            part.remove_unreferenced_vertices()
            grouped.setdefault(key, []).append(part)
            codes.setdefault(key, []).extend(painted[key])

    parts: list[ColourPart] = []
    for key in sorted(grouped):
        if key not in materials:
            raise MaterialSplitError(f"triangle references unknown material {key}")
        name, colour = materials[key]
        meshes = grouped[key]
        mesh = (
            meshes[0]
            if len(meshes) == 1
            else cast(trimesh.Trimesh, trimesh.util.concatenate(meshes))
        )
        paint = None
        if any(codes[key]):
            palette = sorted(
                (index, entry[1]) for (group, index), entry in materials.items() if group == key[0]
            )
            paint = Paint(tuple(codes[key]), tuple(colour for _, colour in palette))
        parts.append(
            ColourPart(material_index=key[1], name=name, colour=colour, mesh=mesh, paint=paint)
        )
    return parts
