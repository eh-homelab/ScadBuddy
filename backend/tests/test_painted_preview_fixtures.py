"""#3956: a real Bambu Studio painting reaches ScadBuddy's own previews."""

from __future__ import annotations

from pathlib import Path

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
    # The two carrots are one orange entry, their painting kept and their mesh whole.
    (body,) = [part for part in read.parts if part.colour == ORANGE]
    assert body.paint is not None and body.mesh.is_watertight
    assert [part.colour for part in read.parts].count(ORANGE) == 1
    write_glb(read.parts, tmp_path / "preview.glb")
    drawn = read_glb(tmp_path / "preview.glb")
    (leaves,) = [part for part in drawn if part.colour == GREEN]
    (orange,) = [part for part in drawn if part.colour == ORANGE]
    # The leaves grow out of the bodies' tops: no green in their lower three quarters.
    assert leaves.mesh.bounds[0, 2] > 0.75 * orange.mesh.bounds[1, 2]
