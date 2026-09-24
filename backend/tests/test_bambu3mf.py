from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import pytest
import trimesh

from scadbuddy.render.bambu3mf import replate_3mf, write_bambu_3mf
from scadbuddy.render.plate import PlateFitError, plate_for
from scadbuddy.render.split import ColourPart
from tests.conftest import GOLDEN

GOLDEN_DIR = GOLDEN / "two_boxes"
ENTRIES = [
    "[Content_Types].xml",
    "_rels/.rels",
    "3D/3dmodel.model",
    "3D/_rels/3dmodel.model.rels",
    "3D/Objects/object_1.model",
    "3D/Objects/object_2.model",
    "Metadata/model_settings.config",
    "Metadata/project_settings.config",
]


def _translate(x: float, y: float, z: float) -> np.ndarray:
    matrix = np.eye(4)
    matrix[:3, 3] = (x, y, z)
    return matrix


def _parts() -> list[ColourPart]:
    return [
        ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box(extents=(10, 10, 4))),
        ColourPart(
            2,
            "Color 2",
            "#1F6FEB",
            trimesh.creation.box(extents=(2, 2, 2), transform=_translate(0, 0, 3)),
        ),
    ]


@pytest.fixture
def written(tmp_path: Path) -> Path:
    out = tmp_path / "model.3mf"
    write_bambu_3mf(_parts(), out, model_name="two_boxes")
    return out


def test_archive_entries_are_the_bambu_layout(written: Path) -> None:
    with zipfile.ZipFile(written) as archive:
        assert archive.namelist() == ENTRIES


def test_matches_the_golden_files(written: Path) -> None:
    with zipfile.ZipFile(written) as archive:
        for entry in ENTRIES:
            produced = archive.read(entry).decode("utf-8")
            golden = GOLDEN_DIR / entry
            if os.environ.get("SCADBUDDY_UPDATE_GOLDEN"):
                golden.parent.mkdir(parents=True, exist_ok=True)
                golden.write_text(produced, encoding="utf-8")
            assert produced == golden.read_text(encoding="utf-8"), entry


def test_output_is_byte_for_byte_reproducible(tmp_path: Path, written: Path) -> None:
    again = tmp_path / "again.3mf"
    write_bambu_3mf(_parts(), again, model_name="two_boxes")
    assert again.read_bytes() == written.read_bytes()


def test_every_part_gets_its_own_extruder(written: Path) -> None:
    with zipfile.ZipFile(written) as archive:
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
    parts = config.findall("./object/part")
    assert [part.get("id") for part in parts] == ["1", "2"]
    extruders = [
        metadata.get("value")
        for part in parts
        for metadata in part.findall("metadata")
        if metadata.get("key") == "extruder"
    ]
    assert extruders == ["1", "2"]


def test_filament_colours_follow_part_order(written: Path) -> None:
    with zipfile.ZipFile(written) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert settings["filament_colour"] == ["#FF6AC1", "#1F6FEB"]


def test_project_settings_carry_the_prime_tower_corner(written: Path) -> None:
    """Bambu Studio writes every vector option as an array of strings."""
    with zipfile.ZipFile(written) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert settings["wipe_tower_x"] == ["98"]
    assert settings["wipe_tower_y"] == ["5"]


def test_a_single_colour_model_gets_no_prime_tower(tmp_path: Path) -> None:
    out = tmp_path / "one.3mf"
    write_bambu_3mf(_parts()[:1], out, model_name="one_box")
    with zipfile.ZipFile(out) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert "wipe_tower_x" not in settings


def test_writing_for_a_printer_centres_on_its_reachable_area(tmp_path: Path) -> None:
    out = tmp_path / "h2c.3mf"
    write_bambu_3mf(_parts(), out, model_name="two_boxes", plate=plate_for("H2C"))
    with zipfile.ZipFile(out) as archive:
        root = ET.fromstring(archive.read("3D/3dmodel.model"))
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    item = root.find(".//{*}item")
    assert item is not None
    transform = [float(v) for v in (item.get("transform") or "").split()]
    assert transform[9:11] == [175.0, 160.0]
    # x=15 is the PrintConfig default that extruder 2 cannot reach (#105).
    assert float(settings["wipe_tower_x"][0]) >= 25.0


class TestReplate:
    def test_replating_moves_the_object_and_the_tower(self, written: Path) -> None:
        moved = replate_3mf(written.read_bytes(), plate_for("H2C"))
        with zipfile.ZipFile(io.BytesIO(moved)) as archive:
            root = ET.fromstring(archive.read("3D/3dmodel.model"))
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        item = root.find(".//{*}item")
        assert item is not None
        transform = [float(v) for v in (item.get("transform") or "").split()]
        assert transform[9:11] == [175.0, 160.0]
        assert settings["wipe_tower_x"] == ["145"]
        assert settings["filament_colour"] == ["#FF6AC1", "#1F6FEB"]

    def test_replating_is_what_writing_for_that_plate_would_have_produced(
        self, tmp_path: Path, written: Path
    ) -> None:
        direct = tmp_path / "direct.3mf"
        write_bambu_3mf(_parts(), direct, model_name="two_boxes", plate=plate_for("H2C"))
        assert replate_3mf(written.read_bytes(), plate_for("H2C")) == direct.read_bytes()

    def test_replating_keeps_every_other_entry(self, written: Path) -> None:
        moved = replate_3mf(written.read_bytes(), plate_for("H2C"))
        with zipfile.ZipFile(io.BytesIO(moved)) as archive:
            assert archive.namelist() == ENTRIES

    def test_replating_is_idempotent(self, written: Path) -> None:
        once = replate_3mf(written.read_bytes(), plate_for("H2C"))
        assert replate_3mf(once, plate_for("H2C")) == once

    def test_a_model_too_big_for_the_printer_is_refused(self, tmp_path: Path) -> None:
        big = tmp_path / "big.3mf"
        write_bambu_3mf(
            [ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box(extents=(200, 200, 4)))],
            big,
            model_name="big",
        )
        with pytest.raises(PlateFitError, match="A1 mini"):
            replate_3mf(big.read_bytes(), plate_for("A1 mini"))


def test_build_item_centres_the_assembly_on_the_plate_at_z0(written: Path) -> None:
    with zipfile.ZipFile(written) as archive:
        root = ET.fromstring(archive.read("3D/3dmodel.model"))
    item = root.find(".//{*}item")
    assert item is not None
    assert item.get("objectid") == "3"
    transform = [float(v) for v in (item.get("transform") or "").split()]
    assert transform[:9] == [1, 0, 0, 0, 1, 0, 0, 0, 1]
    assert transform[9:] == [128.0, 128.0, 2.0]


def test_trimesh_reads_the_written_file_back(written: Path) -> None:
    scene = trimesh.load(written, file_type="3mf")
    assert isinstance(scene, trimesh.Scene)
    assert len(scene.geometry) == 2
    volumes = sorted(round(float(mesh.volume), 3) for mesh in scene.geometry.values())
    assert volumes == [8.0, 400.0]


def test_empty_part_list_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one colour part"):
        write_bambu_3mf([], tmp_path / "empty.3mf")
