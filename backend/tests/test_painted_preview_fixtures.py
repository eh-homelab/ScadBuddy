"""#3956: a real Bambu Studio painting reaches ScadBuddy's own previews."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from scadbuddy.render.glb import read_glb, write_glb
from scadbuddy.render.objects3mf import read_plate_parts
from tests.support.fixtures import makerworld_path

pytestmark = pytest.mark.requires_fixtures
CARROT = "688-carrot-garden.3mf"
GREEN = "#0ACC38"  # the carrots' painted leaves (manifest: filament_colours)
ORANGE = "#F98C36"  # their bodies


def test_a_library_files_preview_draws_its_painting(tmp_path: Path) -> None:
    read = read_plate_parts(makerworld_path(CARROT).read_bytes(), 1)
    painted = [part for part in read.parts if part.paint is not None]
    assert len(painted) == 2  # the two carrots, kept whole for the mesh checks
    assert all(part.mesh.is_watertight for part in painted)
    write_glb(read.parts, tmp_path / "preview.glb")
    drawn = read_glb(tmp_path / "preview.glb")
    green = [part for part in drawn if part.colour == GREEN]
    orange = [part for part in drawn if part.colour == ORANGE]
    assert len(green) == 2
    # The leaves grow out of the top of each body: no green below its carrot's middle.
    for leaves in green:
        body = min(
            orange, key=lambda p: np.linalg.norm(p.mesh.centroid[:2] - leaves.mesh.centroid[:2])
        )
        low, high = body.mesh.bounds[:, 2]
        assert leaves.mesh.bounds[0, 2] > (low + high) / 2
