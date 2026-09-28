"""Fixtures for the analyzer unit tests: real meshes measured by the real geometry
analysis, and contexts built the way ``gather_context`` builds them."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import numpy as np
import trimesh

from scadbuddy.analyzers.context import AnalysisContext, AnalysisRequest, FilamentSlot
from scadbuddy.bambuddy.resolver import NozzleChoice, PrintChoices
from scadbuddy.library.outputs import OutputMeta
from scadbuddy.render.geometry import GeometryAnalysis, analyze_geometry
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.plate import plate_for
from scadbuddy.render.split import ColourPart

OUTPUT_ID = "0123456789abcdef0123456789abcdef"


def part(mesh: trimesh.Trimesh, colour: str = "#FF0000", index: int = 1) -> ColourPart:
    return ColourPart(material_index=index, name=f"Color {index}", colour=colour, mesh=mesh)


def cube(size: float = 10.0, at: tuple[float, float, float] = (0, 0, 0)) -> trimesh.Trimesh:
    box: trimesh.Trimesh = trimesh.creation.box(extents=(size, size, size))
    box.apply_translation(np.array(at) + size / 2)
    return box


def tee() -> trimesh.Trimesh:
    """A T: a 10 mm stem with a 30 mm bar on top, whose underside is a flat ceiling."""
    stem = trimesh.creation.box(extents=(10, 10, 20))
    stem.apply_translation((15, 5, 10))
    bar = trimesh.creation.box(extents=(30, 10, 5))
    bar.apply_translation((15, 5, 22.5))
    return cast(trimesh.Trimesh, trimesh.util.concatenate([stem, bar]))


def open_box() -> trimesh.Trimesh:
    """A cube with its top face (two triangles) removed: four open edges."""
    box = cube()
    top = np.where(box.face_normals[:, 2] > 0.99)[0]
    keep = np.setdiff1d(np.arange(len(box.faces)), top)
    return trimesh.Trimesh(vertices=box.vertices, faces=box.faces[keep], process=False)


def touching_cubes() -> trimesh.Trimesh:
    """Two cubes sharing one edge in a single part: that edge is used by four faces."""
    return cast(trimesh.Trimesh, trimesh.util.concatenate([cube(), cube(at=(10, 10, 0))]))


def geometry_of(*meshes: trimesh.Trimesh) -> GeometryAnalysis:
    return analyze_geometry([part(mesh, index=index) for index, mesh in enumerate(meshes, 1)])


def output(
    *,
    size: tuple[float, float, float] = (10, 10, 10),
    colours: list[str] | None = None,
    library_file_id: int | None = None,
    model_version: str | None = "a" * 40,
) -> OutputMeta:
    return OutputMeta(
        id=OUTPUT_ID,
        slug="demo",
        model_version=model_version,
        job_id="f" * 32,
        created_at=datetime(2026, 9, 28, tzinfo=UTC),
        bbox_mm=BoundingBox(min=(0, 0, 0), max=size, size=size),
        colors=colours or ["#FF0000"],
        library_file_id=library_file_id,
    )


def choices(size: str = "0.4", **extra: Any) -> PrintChoices:
    """The spool-first dialog's choices: one nozzle size on both sides (spec §2)."""
    nozzle = NozzleChoice.model_validate({"size": size})
    return PrintChoices(nozzles=[nozzle, nozzle.model_copy()], **extra)


def silk_slot(slot_id: int = 1) -> FilamentSlot:
    """Spool 5 of ``inventory-spools.json``: a tri-colour silk the inventory records as
    subtype ``Tri Color`` with the preset ``Bambu PLA Silk``."""
    return FilamentSlot(
        slot_id=slot_id,
        spool_id=5,
        material="PLA",
        subtype="Tri Color",
        brand="Bambu",
        preset_name="Bambu PLA Silk",
    )


def basic_slot(slot_id: int = 1) -> FilamentSlot:
    return FilamentSlot(
        slot_id=slot_id,
        spool_id=2,
        material="PLA",
        subtype="Basic",
        brand="Bambu",
        preset_name="Bambu PLA Basic",
    )


def context(**fields: Any) -> AnalysisContext:
    fields.setdefault("slug", "demo")
    fields.setdefault("params", {"width": 12})
    fields.setdefault("request", AnalysisRequest())
    fields.setdefault("plate", plate_for("H2C"))
    return AnalysisContext(**fields)
