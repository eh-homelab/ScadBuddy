"""Paint transfer on the real Carrot Garden (#2146, spec 2026-10-11 §7, §10): Bambuddy
library file 688 rendered through the production pipeline (`render_job` into the image's
openscad, `tests/conftest.py` `render_once`) carries its painting into the 3MF. Re-measures
meshmodel/paint.py's docstring on the pinned image; tens of seconds each."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np
import pytest
from pydantic import TypeAdapter

from scadbuddy.core.paths import DataPaths
from scadbuddy.meshmodel.compile import compile_scene
from scadbuddy.meshmodel.edits import Edit, apply_edits
from scadbuddy.meshmodel.meshes import part_loader
from scadbuddy.meshmodel.paint import paint_file
from scadbuddy.meshmodel.scene import SCENE_NAME, Scene, dump_scene, text_sha256
from scadbuddy.render.objects3mf import read_objects
from scadbuddy.render.read_budget import ReadBudget
from tests.conftest import render_once
from tests.test_mesh_models_fixtures import CARROT, GREEN, carrot_neck, neck_anchor, opened

pytestmark = [pytest.mark.requires_openscad, pytest.mark.requires_fixtures]
EDITS = TypeAdapter(list[Edit])
SLUG = "carrot-garden"


def compiled_into(directory: Path, scene: Scene) -> None:
    text = compile_scene(scene)
    (directory / "model.scad").write_text(text)
    scene = scene.model_copy(update={"generated_sha256": text_sha256(text)})
    (directory / SCENE_NAME).write_text(dump_scene(scene))


def source_codes(directory: Path, scene: Scene) -> tuple[Counter[str], tuple[str, ...]]:
    """Every painted source face's code, and the palette they number (one for every mesh)."""
    codes: Counter[str] = Counter()
    palettes: set[tuple[str, ...]] = set()
    seen: set[str] = set()
    for obj in scene.objects:
        for part in obj.parts:
            if part.painted and part.mesh not in seen:
                seen.add(part.mesh)
                raw = json.loads((directory / paint_file(part)).read_text())
                codes.update(code for code in raw["codes"] if code)
                palettes.add(tuple(raw["colours"]))
    assert len(palettes) == 1, palettes
    return codes, palettes.pop()


def printed_codes(model_3mf: Path, palette: tuple[str, ...]) -> Counter[str]:
    """Every painted face of the 3MF, its code numbered against the source's palette (the
    writer numbers codes against the file's own filament list)."""
    found: Counter[str] = Counter()
    for obj in read_objects(model_3mf.read_bytes(), ReadBudget()):
        for part in obj.parts:
            if part.paint is not None:
                found.update(code for code in part.paint.renumbered(palette).codes if code)
    return found


async def test_the_carrot_gardens_painting_arrives_whole(tmp_path: Path) -> None:
    """Op-free (spec §10): every painted face's code is in the 3MF, verbatim."""
    paths = DataPaths(tmp_path)
    paths.ensure()
    directory = paths.model_dir(SLUG)
    directory.mkdir(parents=True)
    _, scene = opened(CARROT, directory)
    compiled_into(directory, scene)

    _, result = await render_once(paths, SLUG, {})

    expected, palette = source_codes(directory, scene)
    assert sum(expected.values()) == 13_873  # manifest.json painted_triangles
    assert printed_codes(paths.root / result.model_3mf, palette) == expected
    assert GREEN in result.colors


async def test_the_anchored_carrots_painted_leaves_print_green(tmp_path: Path) -> None:
    """The user's case (#2146): the small carrot anchored with a green cylinder through
    its neck (phase 2's coloured add) renders with its leaves' green paint on every face
    the anchor left as it was, and the anchor and the paint share one green filament."""
    paths = DataPaths(tmp_path)
    paths.ensure()
    directory = paths.model_dir(SLUG)
    directory.mkdir(parents=True)
    _, scene = opened(CARROT, directory)
    small, mesh, z, neck = carrot_neck(directory, scene)
    edit = neck_anchor(small, z, neck)
    anchored = apply_edits(scene, EDITS.validate_python([edit]), part_loader(directory))
    only = [o for o in anchored.objects if o.id == small.id]
    compiled_into(directory, anchored.model_copy(update={"objects": only}))

    _, result = await render_once(paths, SLUG, {})

    codes = json.loads((directory / paint_file(small.parts[0])).read_text())["codes"]
    radius = edit["size"][0] / 2
    corners = mesh.triangles
    off_axis = np.hypot(corners[:, :, 0] - neck.centroid[0], corners[:, :, 1] - neck.centroid[1])
    clear = (
        (off_axis > radius + 1e-3)
        | (corners[:, :, 2] < z - 3 - 1e-3)
        | (corners[:, :, 2] > z + 3 + 1e-3)
    ).all(axis=1)
    expected = Counter(code for code, ok in zip(codes, clear, strict=True) if ok and code)
    _, palette = source_codes(directory, scene)
    printed = printed_codes(paths.root / result.model_3mf, palette)
    # Measured 2026-10-13: exactly these 5,639 of the 6,319 painted faces.
    assert not expected - printed
    assert sum(printed.values()) <= sum(1 for code in codes if code)
    assert printed["8"]  # whole-green leaf faces (extruder 2: green)
    assert result.colors.count(GREEN) == 1
