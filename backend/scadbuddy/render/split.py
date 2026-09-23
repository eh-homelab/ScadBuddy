from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np
import trimesh

CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
MODEL_ENTRY = "3D/3dmodel.model"
DEFAULT_MATERIAL_NAME = "Default"

_CORE = f"{{{CORE_NS}}}"


class MaterialSplitError(ValueError):
    pass


@dataclass(frozen=True)
class ColourPart:
    material_index: int
    name: str
    colour: str
    mesh: trimesh.Trimesh

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
    for obj in root.iter(f"{_CORE}object"):
        mesh_node = obj.find(f"{_CORE}mesh")
        if mesh_node is None:
            continue
        vertices = _vertices(mesh_node)
        triangles = mesh_node.find(f"{_CORE}triangles")
        object_pid = obj.get("pid", "")
        object_index = int(obj.get("pindex", 0))
        buckets: dict[tuple[str, int], list[tuple[int, int, int]]] = {}
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
        for key, faces in buckets.items():
            part = trimesh.Trimesh(vertices=vertices.copy(), faces=np.array(faces), process=False)
            part.remove_unreferenced_vertices()
            grouped.setdefault(key, []).append(part)

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
        parts.append(ColourPart(material_index=key[1], name=name, colour=colour, mesh=mesh))
    return parts
