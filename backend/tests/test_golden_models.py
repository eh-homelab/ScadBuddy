"""Golden fixtures for the example models in `models/`.

Each case renders a model through the production pipeline — `RenderQueue` into a
real `openscad`, the colour split, then the 3MF and GLB writers — and compares
the result against the recording in `tests/golden/<case>/`.

The goldens tolerate OpenSCAD nightly drift on purpose. What they do and do not
pin, and why, is in `tests/golden/README.md`. Regenerate them with
`tests/golden/regenerate.sh`.
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import numpy as np
import pytest
import trimesh
import trimesh.graph

from scadbuddy.core.config import Config, load_config
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.jobs import JobResult, RenderQueue
from scadbuddy.render.schema import ParamValue
from tests.conftest import GOLDEN, installed_font_families

MODELS = Path(__file__).resolve().parents[2] / "models"

# The two files issue #42 asks for a diff of. The mesh objects
# (`3D/Objects/object_*.model`) are hundreds of kilobytes each and retessellate
# with every OpenSCAD nightly, so they are pinned as measurements in
# `signature.json` instead of as bytes.
GOLDEN_ENTRIES = ("3D/3dmodel.model", "Metadata/model_settings.config")
SIGNATURE = "signature.json"

LENGTH_TOLERANCE_MM = 0.1
MAGNITUDE_TOLERANCE = 0.01


@dataclass(frozen=True)
class Case:
    key: str
    slug: str
    params: dict[str, ParamValue] = field(default_factory=dict)
    # The family the model asks for. Missing it does not fail the render — OpenSCAD
    # substitutes DejaVu silently — it just makes every measurement below wrong.
    font: str = "lobster two"


CASES = (
    Case("name-keychain-reagan", "name-keychain", {"name": "Reagan"}),
    Case("name-keychain-no-hole", "name-keychain", {"name": "Ada", "hole": False}),
)


async def render_case(case: Case, root: Path) -> tuple[JobResult, DataPaths]:
    paths = DataPaths(root)
    paths.ensure()
    paths.model_dir(case.slug).mkdir(parents=True)
    shutil.copy(MODELS / case.slug / "model.scad", paths.model_source(case.slug))

    queue = RenderQueue(Config(openscad=load_config().openscad, data_dir=root), paths)
    await queue.start()
    try:
        job = await queue.submit(case.slug, case.params)
        await queue.join()
    finally:
        await queue.aclose()

    done = queue.store.read(job.id)
    assert done.state == "done", done.error
    assert done.result is not None
    return done.result, paths


def _bodies(mesh: trimesh.Trimesh) -> int:
    """Connected components. `Trimesh.body_count` needs scipy, which the image does
    not carry; networkx is already a runtime dependency."""
    components = trimesh.graph.connected_components(
        mesh.face_adjacency, nodes=np.arange(len(mesh.faces)), engine="networkx"
    )
    return len(components)


def _lengths(target: dict[str, float], prefix: str, values: Any) -> None:
    for axis, value in zip("xyz", values, strict=True):
        target[f"{prefix}.{axis}"] = round(float(value), 4)


def signature(case: Case, result: JobResult, paths: DataPaths) -> dict[str, Any]:
    model_path = paths.root / result.model_3mf
    model = trimesh.load(model_path, file_type="3mf")
    preview = trimesh.load(paths.root / result.preview_glb, file_type="glb")
    assert isinstance(model, trimesh.Scene)
    assert isinstance(preview, trimesh.Scene)
    with zipfile.ZipFile(model_path) as archive:
        entries = list(archive.namelist())

    lengths: dict[str, float] = {}
    magnitudes: dict[str, float] = {}

    parts: list[dict[str, Any]] = []
    for part, (name, mesh) in zip(result.parts, sorted(model.geometry.items()), strict=True):
        parts.append(
            {
                "name": part.name,
                "colour": part.colour,
                "extruder": part.extruder,
                "watertight": part.watertight,
                "geometry": str(name),
                "mesh_watertight": bool(mesh.is_watertight),
                "bodies": _bodies(mesh),
                "euler_number": int(mesh.euler_number),
            }
        )
        _lengths(lengths, f"part{part.extruder}.min", mesh.bounds[0])
        _lengths(lengths, f"part{part.extruder}.max", mesh.bounds[1])
        magnitudes[f"part{part.extruder}.volume_mm3"] = round(float(mesh.volume), 3)
        magnitudes[f"part{part.extruder}.area_mm2"] = round(float(mesh.area), 3)

    previews: list[dict[str, Any]] = []
    for name, mesh in sorted(preview.geometry.items()):
        previews.append(
            {
                "name": str(name),
                "base_color_rgba": [int(v) for v in mesh.visual.material.baseColorFactor],
                "watertight": bool(mesh.is_watertight),
            }
        )
        _lengths(lengths, f"preview.{name}.min", mesh.bounds[0])
        _lengths(lengths, f"preview.{name}.max", mesh.bounds[1])

    _lengths(lengths, "bbox.min", result.bbox_mm.min)
    _lengths(lengths, "bbox.max", result.bbox_mm.max)
    _lengths(lengths, "bbox.size", result.bbox_mm.size)

    return {
        "exact": {
            "slug": case.slug,
            "params": case.params,
            "colors": result.colors,
            "warnings": result.warnings,
            "archive_entries": entries,
            "parts": parts,
            "preview": previews,
        },
        "lengths_mm": lengths,
        "magnitudes": magnitudes,
    }


def _metadata_key(element: ET.Element) -> str | None:
    if element.tag.rsplit("}", 1)[-1] != "metadata":
        return None
    return element.get("key") or element.get("name")


def _metadata_keys(text: str) -> set[str]:
    return {key for e in ET.fromstring(text).iter() if (key := _metadata_key(e)) is not None}


def _canonicalise(element: ET.Element, keep_metadata: set[str]) -> None:
    for child in list(element):
        key = _metadata_key(child)
        if key is not None and key not in keep_metadata:
            element.remove(child)
        else:
            _canonicalise(child, keep_metadata)
    transform = element.get("transform")
    if transform is not None:
        element.set("transform", " ".join(str(round(float(v))) for v in transform.split()))


def canonical_xml(text: str, keep_metadata: set[str]) -> str:
    """The comparable form of a golden entry.

    Two deliberate blind spots. Metadata the golden does not carry is dropped, so a
    later change that ADDS a `<metadata>` entry does not have to land with a golden
    regeneration. And the build item's transform is rounded to whole millimetres:
    the plate offset is a function of the mesh bounds, so its floats move whenever
    the tessellation does — the geometry itself is pinned by `lengths_mm`.
    """
    root = ET.fromstring(text)
    _canonicalise(root, keep_metadata)
    return ET.canonicalize(ET.tostring(root, encoding="unicode"))


def write_golden(golden_dir: Path, archive_path: Path, produced: dict[str, Any]) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for entry in GOLDEN_ENTRIES:
            target = golden_dir / entry
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(archive.read(entry).decode("utf-8"), encoding="utf-8")
    (golden_dir / SIGNATURE).write_text(
        json.dumps(produced, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def assert_matches_golden(golden_dir: Path, archive_path: Path, produced: dict[str, Any]) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for entry in GOLDEN_ENTRIES:
            golden = (golden_dir / entry).read_text(encoding="utf-8")
            keep = _metadata_keys(golden)
            actual = archive.read(entry).decode("utf-8")
            assert canonical_xml(actual, keep) == canonical_xml(golden, keep), entry

    golden_signature = json.loads((golden_dir / SIGNATURE).read_text(encoding="utf-8"))
    assert produced["exact"] == golden_signature["exact"]
    assert produced["lengths_mm"] == pytest.approx(
        golden_signature["lengths_mm"], abs=LENGTH_TOLERANCE_MM
    )
    assert produced["magnitudes"] == pytest.approx(
        golden_signature["magnitudes"], rel=MAGNITUDE_TOLERANCE
    )


@pytest.mark.requires_openscad
@pytest.mark.parametrize("case", CASES, ids=[case.key for case in CASES])
async def test_the_model_still_renders_to_its_golden(case: Case, tmp_path: Path) -> None:
    if case.font not in installed_font_families():
        pytest.skip(f"{case.font} is not installed; the model would fall back to DejaVu")

    result, paths = await render_case(case, tmp_path)
    produced = signature(case, result, paths)
    golden_dir = GOLDEN / case.key
    if os.environ.get("SCADBUDDY_UPDATE_GOLDEN"):
        write_golden(golden_dir, paths.root / result.model_3mf, produced)
    assert_matches_golden(golden_dir, paths.root / result.model_3mf, produced)


def test_the_recorded_keychain_is_the_reference_print() -> None:
    """The acceptance numbers from epic #40, read off the golden rather than a render,
    so a regeneration that quietly moved them fails here even without openscad."""
    recorded = json.loads((GOLDEN / "name-keychain-reagan" / SIGNATURE).read_text("utf-8"))

    exact = recorded["exact"]
    assert exact["colors"] == ["#0047BB", "#FF1493"]
    assert [part["extruder"] for part in exact["parts"]] == [1, 2]
    assert [part["watertight"] for part in exact["parts"]] == [True, True]
    assert [part["mesh_watertight"] for part in exact["parts"]] == [True, True]
    assert [part["bodies"] for part in exact["parts"]] == [1, 1]
    assert exact["warnings"] == []

    lengths = recorded["lengths_mm"]
    assert lengths["bbox.size.x"] == pytest.approx(95.576, abs=0.1)
    assert lengths["bbox.size.y"] == pytest.approx(34.776, abs=0.1)
    assert lengths["bbox.size.z"] == pytest.approx(6.8, abs=0.1)
