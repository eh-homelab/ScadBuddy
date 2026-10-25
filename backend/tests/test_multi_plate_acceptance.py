"""#385: a ScadBuddy multi-plate 3MF through the real OpenSCAD and Bambu Studio.

`models/maze-puzzle` in `ball_lid` mode at 15 x 15 cells of 16 mm is too big for the
tray and its lid to share a bed, so the template echoes `plates = 2` and draws the lid
on plate 2, on extruder 4 (spec §6.4, #289). The 3MF writer places each plate by
Bambu Studio's own rule (`plate_origin`); these tests check that against the binaries
rather than against our reading of Studio's source.

The render half needs a real `openscad` (the `test` image). The slice half also needs
Bambuddy's slicer sidecar, `ghcr.io/maziggy/bambu-studio-api`, run locally and named by
`SCADBUDDY_TEST_SLICER_URL`:

    docker run --rm -p 3000:3000 ghcr.io/maziggy/bambu-studio-api:bambuddy-1.2.5.6
    SCADBUDDY_TEST_SLICER_URL=http://127.0.0.1:3000 pytest tests/test_multi_plate_acceptance.py

It posts the 3MF straight to that sidecar's `/slice`, as Bambuddy does, and reads the
sliced 3MF back. Nothing here talks to Bambuddy: nothing is queued or printed.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.render.bambu3mf import MODEL_SETTINGS_NAME, plate_settings, replate_3mf
from scadbuddy.render.jobs import JobResult
from scadbuddy.render.plate import plate_for
from scadbuddy.render.schema import ParamValue
from tests.conftest import openscad_binary, render_once
from tests.test_bambu3mf import _placed_boxes, _studio_bed, _studio_plate

MODELS = Path(__file__).resolve().parents[2] / "models"
SLUG = "maze-puzzle"
#: The issue's case: neither side by side nor front to back fits the H2C's bed.
PARAMS: dict[str, ParamValue] = {"mode": "ball_lid", "cells_x": 15, "cells_y": 15, "cell_size": 16}
LID = "#FFFFFF"

SLICER = os.environ.get("SCADBUDDY_TEST_SLICER_URL")
#: Bambu Studio's own system presets, which the sidecar resolves an upload's
#: `inherits` against (`resources/profiles/BBL`); the names are its files' names.
PRINTER = os.environ.get("SCADBUDDY_TEST_SLICER_PRINTER", "Bambu Lab H2C 0.4 nozzle")
PROCESS = os.environ.get("SCADBUDDY_TEST_SLICER_PROCESS", "0.20mm Standard @BBL H2C")
FILAMENT = os.environ.get("SCADBUDDY_TEST_SLICER_FILAMENT", "Bambu PLA Basic @BBL H2C")
#: A slice of a model this size takes Studio tens of seconds a plate.
SLICE_TIMEOUT = 600.0

requires_slicer = pytest.mark.skipif(
    not SLICER, reason="SCADBUDDY_TEST_SLICER_URL names no bambu-studio-api sidecar"
)
pytestmark = pytest.mark.requires_openscad


@dataclass(frozen=True)
class Maze:
    result: JobResult
    #: The 3MF as the send path hands it to Bambuddy for an H2C: re-placed for its bed.
    payload: bytes


@pytest.fixture(scope="module")
def maze(tmp_path_factory: pytest.TempPathFactory) -> Maze:
    # A module fixture is built before the function-scoped skip runs.
    if openscad_binary() is None:
        pytest.skip("openscad is not on PATH")
    paths = DataPaths(tmp_path_factory.mktemp("maze") / "data")
    paths.ensure()
    paths.model_dir(SLUG).mkdir(parents=True)
    shutil.copy(MODELS / SLUG / "model.scad", paths.model_source(SLUG))
    _, result = asyncio.run(render_once(paths, SLUG, PARAMS))
    stored = (paths.root / result.model_3mf).read_bytes()
    return Maze(result, replate_3mf(stored, plate_for("H2C"), nozzle_diameter="0.4"))


def test_the_lid_is_plate_2_on_its_own_extruder(maze: Maze) -> None:
    result = maze.result
    assert [plate.index for plate in result.plates] == [1, 2]
    assert LID not in result.plates[0].colors
    assert result.plates[1].colors == [LID]
    assert [part.extruder for part in result.parts if part.colour == LID] == [4]
    assert any("plate 2" in note for note in result.notes), result.notes


def test_bambu_studio_puts_each_object_on_the_plate_the_3mf_lists_it_on(maze: Maze) -> None:
    """Studio ignores the `<plate>` list and puts an object on the first plate whose
    bed it overlaps (`PartPlateList::reload_all_objects`); on the H2C's bed the two
    must agree, or the lid would slice with the tray."""
    with zipfile.ZipFile(io.BytesIO(maze.payload)) as archive:
        listed = plate_settings(ET.fromstring(archive.read(MODEL_SETTINGS_NAME)))
    boxes = _placed_boxes(maze.payload)
    assert _studio_bed(maze.payload) == plate_for("H2C").size
    placed = {
        object_id: _studio_plate(*boxes[object_id], len(listed), _studio_bed(maze.payload))
        for plate in listed
        for object_id in plate.object_ids
    }
    assert placed == {object_id: plate.index for plate in listed for object_id in plate.object_ids}
    assert sorted(set(placed.values())) == [1, 2]


# ── through Bambu Studio ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class SlicedPlate:
    index: int
    filaments: list[str]
    warnings: list[str]
    gcode: str


def _slice(payload: bytes, plate: int) -> list[SlicedPlate]:
    """``payload`` sliced by the sidecar: plate ``plate``, or every plate for 0."""
    assert SLICER is not None

    def profile(name: str, kind: str) -> bytes:
        return json.dumps({"name": f"ScadBuddy #385 {kind}", "inherits": name}).encode()

    files = [
        # bambuddy-1.2.5.6's upload filter takes only `model/3mf` or `model/stl`
        # (`dist/middleware/upload.js`); the package type answers 400 (#4061).
        ("file", ("maze.3mf", payload, "model/3mf")),
        ("printerProfile", ("printer.json", profile(PRINTER, "printer"), "application/json")),
        ("presetProfile", ("process.json", profile(PROCESS, "process"), "application/json")),
        *(
            (
                "filamentProfile",
                (f"filament_{n}.json", profile(FILAMENT, "filament"), "application/json"),
            )
            for n in range(1, 5)
        ),
    ]
    data = {"plate": str(plate), "exportType": "3mf", "bedType": "Textured PEI Plate"}
    response = httpx.post(f"{SLICER}/slice", files=files, data=data, timeout=SLICE_TIMEOUT)
    assert response.status_code == 200, response.text[:2000]
    with zipfile.ZipFile(io.BytesIO(response.content)) as sliced:
        info = ET.fromstring(sliced.read("Metadata/slice_info.config"))
        plates = []
        for element in info.iter("plate"):
            meta = {m.get("key"): m.get("value") for m in element.findall("metadata")}
            index = int(meta["index"] or 0)
            plates.append(
                SlicedPlate(
                    index=index,
                    filaments=[f.get("id") or "" for f in element.findall("filament")],
                    warnings=[w.get("msg") or "" for w in element.findall("warning")],
                    gcode=sliced.read(f"Metadata/plate_{index}.gcode").decode("utf-8", "replace"),
                )
            )
    return plates


#: What #385 says a good slice is free of.
TROUBLE = re.compile(r"unprintable|outside|out of|extruder|filament|colou?r|nozzle", re.IGNORECASE)
PRIME_TOWER = "; FEATURE: Prime tower"


@requires_slicer
@pytest.mark.parametrize("plate", [1, 2])
def test_each_plate_slices_cleanly_on_its_own(maze: Maze, plate: int) -> None:
    [sliced] = _slice(maze.payload, plate)
    assert sliced.index == plate
    assert not [w for w in sliced.warnings if TROUBLE.search(w)], sliced.warnings
    if plate == 2:
        # The lid alone: one filament, extruder 4, and so no prime tower.
        assert sliced.filaments == ["4"]
        assert PRIME_TOWER not in sliced.gcode
    else:
        assert "4" not in sliced.filaments


@requires_slicer
def test_all_plates_slice_together(maze: Maze) -> None:
    sliced = {plate.index: plate for plate in _slice(maze.payload, 0)}
    assert sorted(sliced) == [1, 2]
    for plate in sliced.values():
        assert not [w for w in plate.warnings if TROUBLE.search(w)], plate.warnings
    assert sliced[2].filaments == ["4"]
    assert PRIME_TOWER not in sliced[2].gcode
