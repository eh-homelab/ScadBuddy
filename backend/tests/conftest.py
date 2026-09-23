from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest

from scadbuddy.core.config import load_config

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parent / "golden"


def load_fixture_param(stem: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{stem}.param").read_text(encoding="utf-8"))
    return data


def load_fixture_source(stem: str) -> str:
    return (FIXTURES / f"{stem}.scad").read_text(encoding="utf-8")


def openscad_binary() -> str | None:
    return shutil.which(load_config().openscad)


@pytest.fixture(autouse=True)
def _skip_without_openscad(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("requires_openscad") and openscad_binary() is None:
        pytest.skip("openscad is not on PATH")


def write_openscad_3mf(
    path: Path,
    parts: list[tuple[str, str, Any]],
    *,
    default_colour: str = "#F9D72C00",
) -> Path:
    """Write a 3MF shaped like OpenSCAD's Manifold export: one object, basematerials,
    per-triangle material index, index 0 reserved for an unused Default."""
    bases = [f'<base name="Default" displaycolor="{default_colour}" />']
    vertices: list[str] = []
    triangles: list[str] = []
    offset = 0
    for index, (name, colour, mesh) in enumerate(parts, start=1):
        bases.append(f'<base name="{name}" displaycolor="{colour}" />')
        for vertex in mesh.vertices:
            vertices.append(f'<vertex x="{vertex[0]:f}" y="{vertex[1]:f}" z="{vertex[2]:f}" />')
        for face in mesh.faces:
            triangles.append(
                f'<triangle v1="{face[0] + offset}" v2="{face[1] + offset}" '
                f'v3="{face[2] + offset}" pid="1" p1="{index}" />'
            )
        offset += len(mesh.vertices)
    model = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'unit="millimeter" xml:lang="en-US">\n'
        "<resources>\n"
        '<basematerials id="1">' + "".join(bases) + "</basematerials>\n"
        '<object id="2" name="OpenSCAD Model" type="model" pid="1" pindex="0"><mesh>'
        "<vertices>" + "".join(vertices) + "</vertices>"
        "<triangles>" + "".join(triangles) + "</triangles>"
        "</mesh></object>\n"
        "</resources>\n"
        '<build><item objectid="2" /></build>\n'
        "</model>\n"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("3D/3dmodel.model", model)
    return path
