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

from scadbuddy.render.bambu3mf import (
    BAMBU_APPLICATION,
    CORE_NS,
    MODEL_SETTINGS_NAME,
    PLACEHOLDER_NOZZLE_DIAMETER,
    PLATE_PICK,
    PLATE_THUMBNAIL,
    PLATE_THUMBNAIL_SMALL,
    PLATE_TOP,
    PRODUCTION_NS,
    PlateParts,
    cover_names,
    laid_out_plates,
    plate_columns,
    plate_origin,
    plate_settings,
    plates_of,
    replate_3mf,
    write_bambu_3mf,
    write_plates_3mf,
)
from scadbuddy.render.geometry import parts_from_3mf
from scadbuddy.render.plate import DEFAULT_PLATE, PlateFitError, PlateGeometry, plate_for
from scadbuddy.render.split import ColourPart
from scadbuddy.render.thumbnail import (
    PLATE_PNG_SIZE,
    PLATE_SMALL_PNG_SIZE,
    render_plate_thumbnails,
)
from tests.conftest import GOLDEN, read_png

GOLDEN_DIR = GOLDEN / "two_boxes"
# The text half of the archive, compared byte for byte against the golden.
TEXT_ENTRIES = [
    "[Content_Types].xml",
    "_rels/.rels",
    "3D/3dmodel.model",
    "3D/_rels/3dmodel.model.rels",
    "3D/Objects/object_1.model",
    "3D/Objects/object_2.model",
    "Metadata/model_settings.config",
    "Metadata/project_settings.config",
]
# The cover images. Deliberately NOT golden bytes: they are a pure function of
# the mesh, but a recorded PNG would turn every lighting or framing tweak into a
# binary diff nobody can review. `tests/test_thumbnail.py` pins them as
# properties instead.
IMAGE_ENTRIES = [PLATE_THUMBNAIL, PLATE_THUMBNAIL_SMALL, PLATE_TOP, PLATE_PICK]
ENTRIES = TEXT_ENTRIES + IMAGE_ENTRIES


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


def _write(out: Path, *, covers: bool = True, plate: PlateGeometry = DEFAULT_PLATE) -> Path:
    parts = _parts()
    write_bambu_3mf(
        parts,
        out,
        thumbnails=render_plate_thumbnails(parts) if covers else None,
        model_name="two_boxes",
        plate=plate,
    )
    return out


@pytest.fixture
def written(tmp_path: Path) -> Path:
    return _write(tmp_path / "model.3mf")


def test_archive_entries_are_the_bambu_layout(written: Path) -> None:
    with zipfile.ZipFile(written) as archive:
        assert archive.namelist() == ENTRIES


def test_matches_the_golden_files(written: Path) -> None:
    with zipfile.ZipFile(written) as archive:
        for entry in TEXT_ENTRIES:
            produced = archive.read(entry).decode("utf-8")
            golden = GOLDEN_DIR / entry
            if os.environ.get("SCADBUDDY_UPDATE_GOLDEN"):
                golden.parent.mkdir(parents=True, exist_ok=True)
                golden.write_text(produced, encoding="utf-8")
            assert produced == golden.read_text(encoding="utf-8"), entry


def test_output_is_byte_for_byte_reproducible(tmp_path: Path, written: Path) -> None:
    again = _write(tmp_path / "again.3mf")
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
    write_bambu_3mf(_parts()[:1], out, thumbnails=None, model_name="one_box")
    with zipfile.ZipFile(out) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert "wipe_tower_x" not in settings


def test_writing_for_a_printer_centres_on_its_reachable_area(tmp_path: Path) -> None:
    out = tmp_path / "h2c.3mf"
    write_bambu_3mf(_parts(), out, thumbnails=None, model_name="two_boxes", plate=plate_for("H2C"))
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
        # The same package, covers and all — replating rewrites the placement and
        # touches nothing else, so the bytes have to match what a direct write gives.
        direct = _write(tmp_path / "direct.3mf", plate=plate_for("H2C"))
        assert replate_3mf(written.read_bytes(), plate_for("H2C")) == direct.read_bytes()

    def test_replating_keeps_every_other_entry(self, written: Path) -> None:
        moved = replate_3mf(written.read_bytes(), plate_for("H2C"))
        with zipfile.ZipFile(io.BytesIO(moved)) as archive:
            assert archive.namelist() == ENTRIES

    def test_replating_is_idempotent(self, written: Path) -> None:
        once = replate_3mf(written.read_bytes(), plate_for("H2C"))
        assert replate_3mf(once, plate_for("H2C")) == once

    def test_replating_states_the_target_nozzle_diameter(self, written: Path) -> None:
        """#126: the send path knows the pipeline's nozzle, so the file says so."""
        moved = replate_3mf(written.read_bytes(), plate_for("H2C"), nozzle_diameter="0.2")
        with zipfile.ZipFile(io.BytesIO(moved)) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        # Same one-entry arity as the placeholder it replaces.
        assert settings["nozzle_diameter"] == ["0.2"]

    def test_replating_without_a_nozzle_keeps_the_placeholder(self, written: Path) -> None:
        moved = replate_3mf(written.read_bytes(), plate_for("H2C"))
        with zipfile.ZipFile(io.BytesIO(moved)) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert settings["nozzle_diameter"] == PLACEHOLDER_NOZZLE_DIAMETER

    def test_a_model_too_big_for_the_printer_is_refused(self, tmp_path: Path) -> None:
        big = tmp_path / "big.3mf"
        write_bambu_3mf(
            [ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box(extents=(200, 200, 4)))],
            big,
            thumbnails=None,
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
        write_bambu_3mf([], tmp_path / "empty.3mf", thumbnails=None)


def test_the_cover_images_are_where_bambuddy_looks(written: Path) -> None:
    """`Metadata/plate_1.png` is the first entry Bambuddy's `ThreeMFParser`
    (`services/archive.py::_extract_thumbnail`) tries on an unsliced upload, and
    what becomes the library file's `thumbnail_path`. This is that contract."""
    with zipfile.ZipFile(written) as archive:
        assert PLATE_THUMBNAIL == "Metadata/plate_1.png"
        assert read_png(archive.read(PLATE_THUMBNAIL)).shape == (
            PLATE_PNG_SIZE,
            PLATE_PNG_SIZE,
            4,
        )
        assert read_png(archive.read(PLATE_THUMBNAIL_SMALL)).shape == (
            PLATE_SMALL_PNG_SIZE,
            PLATE_SMALL_PNG_SIZE,
            4,
        )


def test_the_cover_image_carries_both_part_colours(written: Path) -> None:
    with zipfile.ZipFile(written) as archive:
        image = read_png(archive.read(PLATE_THUMBNAIL))
    opaque = image[..., 3] == 255
    assert (opaque & (image[..., 0] > image[..., 2])).any()
    assert (opaque & (image[..., 2] > image[..., 0])).any()


def test_png_entries_declare_their_content_type(written: Path) -> None:
    with zipfile.ZipFile(written) as archive:
        types = ET.fromstring(archive.read("[Content_Types].xml"))
    defaults = {
        default.get("Extension"): default.get("ContentType")
        for default in types.findall("{*}Default")
    }
    assert defaults["png"] == "image/png"


def test_the_package_relationships_point_at_the_covers(written: Path) -> None:
    with zipfile.ZipFile(written) as archive:
        rels = ET.fromstring(archive.read("_rels/.rels"))
        names = archive.namelist()
    targets = {
        relationship.get("Type"): relationship.get("Target")
        for relationship in rels.findall("{*}Relationship")
    }
    assert (
        targets["http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"]
        == f"/{PLATE_THUMBNAIL}"
    )
    assert (
        targets["http://schemas.bambulab.com/package/2021/cover-thumbnail-middle"]
        == f"/{PLATE_THUMBNAIL}"
    )
    assert (
        targets["http://schemas.bambulab.com/package/2021/cover-thumbnail-small"]
        == f"/{PLATE_THUMBNAIL_SMALL}"
    )
    # Every relationship target resolves to a real entry; a dangling one is how
    # a reader ends up with no cover at all.
    for target in targets.values():
        assert (target or "").lstrip("/") in names


def test_the_plate_names_its_cover_images(written: Path) -> None:
    with zipfile.ZipFile(written) as archive:
        plate = ET.fromstring(archive.read("Metadata/model_settings.config")).find("./plate")
        names = archive.namelist()
    assert plate is not None
    metadata = {entry.get("key"): entry.get("value") for entry in plate.findall("metadata")}
    assert metadata["thumbnail_file"] == PLATE_THUMBNAIL
    assert metadata["top_file"] == PLATE_TOP
    assert metadata["pick_file"] == PLATE_PICK
    for key in ("thumbnail_file", "top_file", "pick_file"):
        assert metadata[key] in names


def test_without_covers_nothing_is_left_pointing_at_them(tmp_path: Path) -> None:
    """`render.jobs` writes the 3MF without cover images when the rasteriser
    blows its budget. The package has to stay self-consistent when it does: a
    relationship or a `thumbnail_file` naming an entry that is not in the zip is
    exactly the silent breakage the references exist to prevent."""
    with zipfile.ZipFile(_write(tmp_path / "bare.3mf", covers=False)) as archive:
        names = archive.namelist()
        types = ET.fromstring(archive.read("[Content_Types].xml"))
        rels = ET.fromstring(archive.read("_rels/.rels"))
        plate = ET.fromstring(archive.read("Metadata/model_settings.config")).find("./plate")

    assert names == TEXT_ENTRIES
    assert "png" not in {d.get("Extension") for d in types.findall("{*}Default")}
    for relationship in rels.findall("{*}Relationship"):
        assert (relationship.get("Target") or "").lstrip("/") in names
    assert plate is not None
    keys = {entry.get("key") for entry in plate.findall("metadata")}
    assert keys.isdisjoint({"thumbnail_file", "top_file", "pick_file"})


def test_the_part_and_colour_metadata_survive_without_covers(tmp_path: Path) -> None:
    """The cover images are a nicety; the extruder assignment is the print. A
    3MF written without covers still has to slice."""
    with zipfile.ZipFile(_write(tmp_path / "bare.3mf", covers=False)) as archive:
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    extruders = [
        metadata.get("value")
        for part in config.findall("./object/part")
        for metadata in part.findall("metadata")
        if metadata.get("key") == "extruder"
    ]
    assert extruders == ["1", "2"]
    # Two colours, so the placement reserved a prime tower and recorded it (#105);
    # the rest is what the BambuStudio-project claim commits the file to (#110).
    assert settings == {
        "filament_colour": ["#FF6AC1", "#1F6FEB"],
        "printer_settings_id": "ScadBuddy",
        "print_settings_id": "ScadBuddy",
        "filament_settings_id": ["ScadBuddy", "ScadBuddy"],
        "nozzle_diameter": ["0.4"],
        "printable_height": "250",
        "wipe_tower_x": ["98"],
        "wipe_tower_y": ["5"],
    }


class TestBambuProjectIdentity:
    """#110 — the file has to identify as a BambuStudio project, and then satisfy
    what that claim commits it to.

    ``bbs_3mf.cpp`` reads ``Metadata/project_settings.config`` only when the root
    model's ``Application`` starts with ``BambuStudio-``; otherwise it sets
    ``dont_load_config`` and drops the file, so the prime-tower position we
    compute never reaches the slicer. Making that claim is half the fix: once the
    BBL path is taken, ``BambuStudio.cpp`` dereferences five options without a
    null check and **segfaults** on a file that omits them — not a validation
    error, a crash before slicing starts.

    The slicer itself cannot run in CI, so these pin the file contents that the
    measured-good archive had. The end-to-end proof is in the PR.
    """

    REQUIRED = (
        "printer_settings_id",
        "print_settings_id",
        "filament_settings_id",
        "nozzle_diameter",
        "printable_height",
    )

    def test_the_root_model_claims_to_be_a_bambustudio_project(self, written: Path) -> None:
        with zipfile.ZipFile(written) as archive:
            root = archive.read("3D/3dmodel.model").decode()
        assert f'<metadata name="Application">{BAMBU_APPLICATION}</metadata>' in root
        assert BAMBU_APPLICATION.startswith("BambuStudio-"), "the loader matches this prefix"
        assert '<metadata name="BambuStudio:3mfVersion">1</metadata>' in root
        # Claiming BambuStudio for the loader's benefit must not erase who wrote it.
        assert '<metadata name="Origin">ScadBuddy</metadata>' in root

    def test_the_claimed_version_triggers_no_compatibility_path(self) -> None:
        # BambuStudio.cpp translates old configs below 1.5.9, regenerates
        # thumbnails below 1.5.9, keeps old params below 2.0.0, disables wrapping
        # detection below 2.2.0 and resets skirt_per_object below 2.7.0.
        major, minor = (int(part) for part in BAMBU_APPLICATION.split("-")[1].split(".")[:2])
        assert (major, minor) >= (2, 7), "an older claim silently changes slicer behaviour"

    @pytest.mark.parametrize("key", REQUIRED)
    def test_every_option_the_loader_dereferences_is_present(self, written: Path, key: str) -> None:
        with zipfile.ZipFile(written) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert key in settings, f"omitting {key} segfaults the loader, it does not warn"

    def test_a_single_colour_file_carries_them_too(self, tmp_path: Path) -> None:
        # No tower, but the identity claim is unconditional, so the keys are too.
        out = tmp_path / "one.3mf"
        write_bambu_3mf(_parts()[:1], out, thumbnails=None, model_name="one_box")
        with zipfile.ZipFile(out) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert all(key in settings for key in self.REQUIRED)
        assert "wipe_tower_x" not in settings

    def test_filament_settings_id_has_one_entry_per_colour(self, written: Path) -> None:
        with zipfile.ZipFile(written) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert len(settings["filament_settings_id"]) == len(settings["filament_colour"]) == 2

    def test_printable_height_follows_the_plate(self, tmp_path: Path) -> None:
        out = tmp_path / "h2c.3mf"
        write_bambu_3mf(
            _parts(), out, thumbnails=None, model_name="two_boxes", plate=plate_for("H2C")
        )
        with zipfile.ZipFile(out) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert settings["printable_height"] == "325"

    def test_replating_restates_the_height_for_the_new_plate(self, written: Path) -> None:
        moved = replate_3mf(written.read_bytes(), plate_for("H2C"))
        with zipfile.ZipFile(io.BytesIO(moved)) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert settings["printable_height"] == "325"
        assert all(key in settings for key in self.REQUIRED)


class TestPlatesOf:
    """#83: which plates a 3MF carries, and which of them have a cover image."""

    def test_a_written_3mf_is_one_plate_with_its_cover(self, written: Path) -> None:
        [plate] = plates_of(written)
        assert (plate.index, plate.thumbnail) == (1, PLATE_THUMBNAIL)

    def test_a_3mf_written_without_covers_has_no_thumbnail(self, tmp_path: Path) -> None:
        [plate] = plates_of(_write(tmp_path / "bare.3mf", covers=False))
        assert (plate.index, plate.thumbnail) == (1, None)

    def test_every_plate_of_a_multi_plate_project_is_listed(self, tmp_path: Path) -> None:
        path = add_plate(_write(tmp_path / "two.3mf", covers=False), 2, thumbnail=b"png")
        assert [(plate.index, plate.thumbnail) for plate in plates_of(path)] == [
            (1, None),
            (2, "Metadata/plate_2.png"),
        ]

    def test_a_plate_without_a_plater_id_is_a_malformed_3mf(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "bad.3mf", covers=False)
        with zipfile.ZipFile(path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        config = entries["Metadata/model_settings.config"].decode("utf-8")
        entries["Metadata/model_settings.config"] = config.replace(
            "</config>", " <plate>\n </plate>\n</config>"
        ).encode("utf-8")
        with zipfile.ZipFile(path, "w") as archive:
            for name, payload in entries.items():
                archive.writestr(name, payload)
        with pytest.raises(ValueError, match="plater_id"):
            plates_of(path)


def add_plate(path: Path, index: int, *, thumbnail: bytes | None = None) -> Path:
    """Add a ``<plate>`` to a written 3MF, the way Bambu Studio lists a second plate."""
    with zipfile.ZipFile(path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    cover = f"Metadata/plate_{index}.png"
    extra = f'  <metadata key="thumbnail_file" value="{cover}"/>\n' if thumbnail else ""
    plate = f' <plate>\n  <metadata key="plater_id" value="{index}"/>\n{extra} </plate>\n</config>'
    config = entries["Metadata/model_settings.config"].decode("utf-8")
    entries["Metadata/model_settings.config"] = config.replace("</config>", plate).encode("utf-8")
    if thumbnail:
        entries[cover] = thumbnail
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


# ── #289: one 3MF, more than one plate ────────────────────────────────────────

LID = "#FFFFFF"


def _two_plates() -> tuple[list[PlateParts], list[str]]:
    """A tray (two colours) on plate 1 and a lid (a third) on plate 2, each drawn at
    the model origin the way a template draws a plate on its own."""
    tray = _parts()
    lid = ColourPart(3, "Lid", LID, trimesh.creation.box(extents=(12, 12, 2)))
    return (
        [PlateParts(tuple(tray), (1, 2)), PlateParts((lid,), (3,))],
        [part.colour for part in tray] + [LID],
    )


def _write_plates(out: Path, *, covers: bool = True, plate: PlateGeometry = DEFAULT_PLATE) -> Path:
    plates, colours = _two_plates()
    write_plates_3mf(
        plates,
        colours,
        out,
        thumbnails=[render_plate_thumbnails(each.parts) for each in plates] if covers else None,
        model_name="maze",
        plate=plate,
    )
    return out


def _placed_boxes(payload: bytes) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """Each build item's assembly, as world-space XY boxes: what Bambu Studio's
    ``reload_all_objects`` intersects with the plate areas to pick a plate."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        root = ET.fromstring(archive.read("3D/3dmodel.model"))
        paths = {
            obj.get("id"): [
                (component.get(f"{{{PRODUCTION_NS}}}path") or "").lstrip("/")
                for component in obj.iter(f"{{{CORE_NS}}}component")
            ]
            for obj in root.iter(f"{{{CORE_NS}}}object")
        }
        boxes: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for item in root.iter(f"{{{CORE_NS}}}item"):
            offset = np.array([float(v) for v in (item.get("transform") or "").split()[9:]])
            points = np.array(
                [
                    [float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0))]
                    for name in paths[item.get("objectid")]
                    for v in ET.fromstring(archive.read(name)).iter(f"{{{CORE_NS}}}vertex")
                ]
            )
            boxes[item.get("objectid") or ""] = (
                points.min(axis=0) + offset,
                points.max(axis=0) + offset,
            )
    return boxes


def _studio_plate(low: np.ndarray, high: np.ndarray, count: int, plate: PlateGeometry) -> int:
    """The first plate whose bed the box overlaps, as Bambu Studio assigns it."""
    for index in range(1, count + 1):
        x, y = plate_origin(index, count, plate)
        if (
            low[0] < x + int(plate.size[0])
            and high[0] > x
            and low[1] < y + int(plate.size[1])
            and high[1] > y
        ):
            return index
    return 0


class TestMultiplePlates:
    @pytest.mark.parametrize(
        ("count", "columns"), [(1, 1), (2, 2), (3, 2), (4, 2), (5, 3), (9, 3), (10, 4), (16, 4)]
    )
    def test_plates_are_laid_out_as_bambu_studio_counts_columns(
        self, count: int, columns: int
    ) -> None:
        # `compute_colum_count` in PartPlate.hpp: sqrt, rounded, +1 when it rounded down.
        assert plate_columns(count) == columns

    def test_plate_origins_step_a_bed_and_a_fifth(self) -> None:
        h2c = plate_for("H2C")  # a 330 x 320 bed
        assert plate_origin(1, 2, h2c) == (0.0, 0.0)
        assert plate_origin(2, 2, h2c) == pytest.approx((396.0, 0.0))
        assert plate_origin(3, 3, h2c) == pytest.approx((0.0, -384.0))
        assert plate_origin(2, 2, DEFAULT_PLATE) == pytest.approx((307.2, 0.0))

    def test_every_plate_is_listed_with_its_own_cover(self, tmp_path: Path) -> None:
        path = _write_plates(tmp_path / "maze.3mf")
        assert [(p.index, p.thumbnail) for p in plates_of(path)] == [
            (1, "Metadata/plate_1.png"),
            (2, "Metadata/plate_2.png"),
        ]
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        assert {*cover_names(1), *cover_names(2)} <= names

    def test_each_plate_lands_on_its_own_bed_as_bambu_studio_assigns_it(
        self, tmp_path: Path
    ) -> None:
        payload = _write_plates(tmp_path / "maze.3mf").read_bytes()
        boxes = _placed_boxes(payload)
        # Assemblies take the ids after the three part objects: 4 and 5.
        assert _studio_plate(*boxes["4"], 2, DEFAULT_PLATE) == 1
        assert _studio_plate(*boxes["5"], 2, DEFAULT_PLATE) == 2
        # Centred on its bed, sitting on z = 0.
        low, high = boxes["5"]
        assert (low[:2] + high[:2]) / 2 == pytest.approx((307.2 + 128.0, 128.0))
        assert low[2] == pytest.approx(0.0)

    def test_extruders_are_numbered_across_plates(self, tmp_path: Path) -> None:
        path = _write_plates(tmp_path / "maze.3mf", covers=False)
        with zipfile.ZipFile(path) as archive:
            config = ET.fromstring(archive.read("Metadata/model_settings.config"))
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        parts = {
            part.get("id"): {m.get("key"): m.get("value") for m in part.findall("metadata")}
            for part in config.iter("part")
        }
        assert {key: value["extruder"] for key, value in parts.items()} == {
            "1": "1",
            "2": "2",
            "3": "3",
        }
        assert parts["3"]["name"] == "Lid"
        instances = [
            (
                {m.get("key"): m.get("value") for m in plate.findall("metadata")}["plater_id"],
                {m.get("key"): m.get("value") for m in plate.iter("metadata")}["object_id"],
            )
            for plate in config.iter("plate")
        ]
        assert instances == [("1", "4"), ("2", "5")]
        assert settings["filament_colour"] == ["#FF6AC1", "#1F6FEB", LID]
        assert len(settings["filament_settings_id"]) == 3

    def test_only_a_plate_with_two_colours_gets_its_own_tower(self, tmp_path: Path) -> None:
        path = _write_plates(tmp_path / "h2c.3mf", covers=False, plate=plate_for("H2C"))
        with zipfile.ZipFile(path) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        # One entry per plate; the lid plate prints no tower and repeats plate 1's.
        assert len(settings["wipe_tower_x"]) == 2
        assert settings["wipe_tower_x"][1] == settings["wipe_tower_x"][0]

    def test_replating_moves_every_plate_onto_the_printers_stride(self, tmp_path: Path) -> None:
        written = _write_plates(tmp_path / "maze.3mf")
        h2c = plate_for("H2C")
        moved = replate_3mf(written.read_bytes(), h2c)
        boxes = _placed_boxes(moved)
        assert _studio_plate(*boxes["4"], 2, h2c) == 1
        assert _studio_plate(*boxes["5"], 2, h2c) == 2
        low, high = boxes["5"]
        # Centred on the H2C's reachable area (x 25..325), one stride over.
        assert (low[0] + high[0]) / 2 == pytest.approx(396.0 + 175.0)

    def test_replating_a_multi_plate_file_matches_writing_it_for_that_printer(
        self, tmp_path: Path
    ) -> None:
        written = _write_plates(tmp_path / "maze.3mf")
        direct = _write_plates(tmp_path / "direct.3mf", plate=plate_for("H2C"))
        moved = replate_3mf(written.read_bytes(), plate_for("H2C"))
        assert moved == direct.read_bytes()
        assert replate_3mf(moved, plate_for("H2C")) == moved

    def test_a_plate_too_big_for_the_printer_is_named(self, tmp_path: Path) -> None:
        tray = ColourPart(1, "Tray", "#FF6AC1", trimesh.creation.box(extents=(100, 100, 4)))
        lid = ColourPart(2, "Lid", LID, trimesh.creation.box(extents=(200, 200, 2)))
        path = tmp_path / "big-lid.3mf"
        write_plates_3mf(
            [PlateParts((tray,), (1,)), PlateParts((lid,), (2,))],
            ["#FF6AC1", LID],
            path,
            thumbnails=None,
        )
        with pytest.raises(PlateFitError, match="plate 2"):
            replate_3mf(path.read_bytes(), plate_for("A1 mini"))

    def test_the_geometry_reader_gives_each_part_its_extruders_colour(self, tmp_path: Path) -> None:
        path = _write_plates(tmp_path / "maze.3mf", covers=False)
        assert [(p.material_index, p.name, p.colour) for p in parts_from_3mf(path)] == [
            (1, "Color 1", "#FF6AC1"),
            (2, "Color 2", "#1F6FEB"),
        ]
        assert [(p.material_index, p.name, p.colour) for p in parts_from_3mf(path, 2)] == [
            (3, "Lid", LID),
        ]

    def test_both_plate_readers_share_one_parse_of_the_plate_list(self, tmp_path: Path) -> None:
        path = _write_plates(tmp_path / "maze.3mf")
        with zipfile.ZipFile(path) as archive:
            config = ET.fromstring(archive.read(MODEL_SETTINGS_NAME))
            laid_out = laid_out_plates(archive)
        settings = plate_settings(config)

        assert [(each.index, each.object_id) for each in settings] == [
            (each.index, each.assembly_id) for each in laid_out
        ]
        assert [each.index for each in plates_of(path)] == [each.index for each in settings]
        assert [each.metadata["thumbnail_file"] for each in settings] == [
            each.thumbnail for each in plates_of(path)
        ]

        plate = next(config.iter("plate"))
        for entry in plate.findall("metadata"):
            if entry.get("key") == "plater_id":
                plate.remove(entry)
        with pytest.raises(ValueError, match="no plater_id"):
            plate_settings(config)

    def test_cover_images_are_one_set_per_plate_or_none(self, tmp_path: Path) -> None:
        plates, colours = _two_plates()
        with pytest.raises(ValueError, match="one set per plate"):
            write_plates_3mf(
                plates,
                colours,
                tmp_path / "x.3mf",
                thumbnails=[render_plate_thumbnails(plates[0].parts)],
            )

    def test_an_extruder_outside_the_filament_list_is_refused(self, tmp_path: Path) -> None:
        plates, colours = _two_plates()
        with pytest.raises(ValueError, match="extruder"):
            write_plates_3mf(plates, colours[:2], tmp_path / "x.3mf", thumbnails=None)
