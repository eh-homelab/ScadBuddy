from __future__ import annotations

import io
import json
import math
import os
import re
import tracemalloc
import zipfile
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any, BinaryIO
from xml.etree import ElementTree as ET

import numpy as np
import pytest
import trimesh

from scadbuddy.render import bambu3mf, zipcopy
from scadbuddy.render.bambu3mf import (
    BAMBU_APPLICATION,
    CORE_NS,
    MAX_SETTINGS_BYTES,
    MAX_UNCOMPRESSED_BYTES,
    MODEL_SETTINGS_NAME,
    PLACEHOLDER_NOZZLE_DIAMETER,
    PLATE_PICK,
    PLATE_THUMBNAIL,
    PLATE_THUMBNAIL_SMALL,
    PLATE_TOP,
    PRESET_PLACEHOLDER,
    PRODUCTION_NS,
    SLOT_HINT_NAME,
    ArchiveTooLargeError,
    PlateParts,
    cover_names,
    hint_painted_slots,
    laid_out_plates,
    layout_of,
    nozzles_statable,
    parse_model,
    plate_columns,
    plate_origin,
    plate_settings,
    plates_of,
    replate_3mf,
    single_plate,
    state_nozzles,
    stl_3mf,
    write_bambu_3mf,
    write_plates_3mf,
)
from scadbuddy.render.geometry import parts_from_3mf
from scadbuddy.render.jobs import MAX_PLATES
from scadbuddy.render.objects3mf import read_objects
from scadbuddy.render.paint import single_code
from scadbuddy.render.plate import DEFAULT_PLATE, PlateFitError, PlateGeometry, plate_for
from scadbuddy.render.split import PAINT_ATTRIBUTE, ColourPart, Paint
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


def test_project_settings_size_every_filament_for_a_slice_with_no_profiles(
    tmp_path: Path,
) -> None:
    """Bambu Studio's CLI with no profiles takes this file as its whole config, and
    segfaults without ``filament_diameter``; a multi-colour one then needs
    ``filament_is_support`` the length of ``filament_colour`` (#4089)."""
    parts = [
        ColourPart(index, f"Color {index}", colour, trimesh.creation.box(extents=(4, 4, 4)))
        for index, colour in enumerate(["#FF0000", "#00FF00", "#0000FF"], start=1)
    ]
    out = tmp_path / "three.3mf"
    write_plates_3mf([single_plate(parts)], [p.colour for p in parts], out, thumbnails=None)
    settings = _settings_of(out)
    assert settings["filament_diameter"] == ["1.75", "1.75", "1.75"]
    assert settings["filament_is_support"] == ["0", "0", "0"]
    # Beside the five keys spec §3 requires.
    for key in (
        "printer_settings_id",
        "print_settings_id",
        "filament_settings_id",
        "nozzle_diameter",
        "printable_height",
    ):
        assert key in settings


def _restated(payload: bytes, **changes: list[str] | None) -> bytes:
    """``payload`` with ``project_settings.config`` keys set, or removed for ``None``."""
    settings = _settings_of_bytes(payload)
    for key, value in changes.items():
        if value is None:
            settings.pop(key)
        else:
            settings[key] = value
    restated = (json.dumps(settings, indent=4) + "\n").encode()
    return _with_entries(payload, {"Metadata/project_settings.config": restated})


def test_re_placing_a_file_written_before_4089_adds_the_filament_vectors(
    written: Path,
) -> None:
    old = _restated(written.read_bytes(), filament_diameter=None, filament_is_support=None)
    assert "filament_diameter" not in _settings_of_bytes(old)
    settings = _settings_of_bytes(replate_3mf(old, plate_for("H2C")))
    assert settings["filament_diameter"] == ["1.75", "1.75"]
    assert settings["filament_is_support"] == ["0", "0"]


def test_re_placing_keeps_the_filament_vectors_a_file_states(written: Path) -> None:
    thick = _restated(written.read_bytes(), filament_diameter=["2.85", "2.85"])
    settings = _settings_of_bytes(replate_3mf(thick, plate_for("A1")))
    assert settings["filament_diameter"] == ["2.85", "2.85"]
    assert settings["filament_is_support"] == ["0", "0"]


def _settings_of(out: Path) -> dict[str, object]:
    with zipfile.ZipFile(out) as archive:
        settings: dict[str, object] = json.loads(archive.read("Metadata/project_settings.config"))
    return settings


def test_a_render_states_no_material(written: Path) -> None:
    """ScadBuddy does not know what a render prints in: it names no material rather
    than invent one."""
    assert "filament_type" not in _settings_of(written)


def test_materials_known_for_every_slot_are_stated_in_slot_order(tmp_path: Path) -> None:
    """Bambuddy's slice dialog picks each slot's filament preset by ``filament_type``;
    without one it picks Bambu ABS for an arranged PLA keychain."""
    parts = _parts()
    out = tmp_path / "arranged.3mf"
    write_plates_3mf(
        [single_plate(parts)],
        [p.colour for p in parts],
        out,
        thumbnails=None,
        materials={"#1F6FEB": "PETG", "#FF6AC1": "PLA", "#000000": "ABS"},
    )
    assert _settings_of(out)["filament_type"] == ["PLA", "PETG"]


def test_a_slot_of_unknown_material_states_none_for_any_slot(tmp_path: Path) -> None:
    parts = _parts()
    out = tmp_path / "arranged.3mf"
    write_plates_3mf(
        [single_plate(parts)],
        [p.colour for p in parts],
        out,
        thumbnails=None,
        materials={"#FF6AC1": "PLA"},
    )
    assert "filament_type" not in _settings_of(out)


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

    def test_replating_copies_an_untouched_entry_as_it_stands(
        self, written: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2942: a mesh object is copied as its compressed bytes, not inflated and
        deflated again, and the file is the same as when every entry is rewritten."""
        copied: list[str] = []
        real = zipcopy.copy_raw

        def spy(source: BinaryIO, info: zipfile.ZipInfo, *args: Any, **kwargs: Any) -> None:
            copied.append(info.filename)
            real(source, info, *args, **kwargs)

        monkeypatch.setattr(bambu3mf, "copy_raw", spy)
        moved = replate_3mf(written.read_bytes(), plate_for("H2C"))
        objects = [name for name in ENTRIES if name.startswith("3D/Objects/")]
        assert objects and set(objects) <= set(copied)
        assert not {bambu3mf.ROOT_MODEL_NAME, bambu3mf.PROJECT_SETTINGS_NAME} & set(copied)

        monkeypatch.setattr(bambu3mf, "raw_copy_checked", lambda: False)
        copied.clear()
        assert replate_3mf(written.read_bytes(), plate_for("H2C")) == moved
        assert copied == []

    def test_replating_rewrites_an_entry_compressed_against_the_policy(self, written: Path) -> None:
        """An object stored uncompressed by someone else is not copied as it stands: the
        replated file is deflated as the writer deflates it."""
        stored = io.BytesIO()
        with (
            zipfile.ZipFile(written) as source,
            zipfile.ZipFile(stored, "w", zipfile.ZIP_STORED) as out,
        ):
            for info in source.infolist():
                out.writestr(info.filename, source.read(info))
        expected = replate_3mf(written.read_bytes(), plate_for("H2C"))
        assert replate_3mf(stored.getvalue(), plate_for("H2C")) == expected

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

    def test_replating_for_a_nozzle_maps_every_filament_to_one_extruder(
        self, tmp_path: Path
    ) -> None:
        """#768: the shape Bambu Studio saved for the maintainer's two-colour keychain
        that printed on one 0.2 mm nozzle."""
        two = tmp_path / "two.3mf"
        box = trimesh.creation.box(extents=(10, 10, 4))
        write_bambu_3mf(
            [ColourPart(1, "Color 1", "#C48CF3", box), ColourPart(2, "Color 2", "#EC008C", box)],
            two,
            thumbnails=None,
            model_name="two",
        )
        moved = replate_3mf(two.read_bytes(), plate_for("H2C"), nozzle_diameter="0.2")
        with zipfile.ZipFile(io.BytesIO(moved)) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert settings["filament_map"] == ["1", "1"]
        assert settings["filament_map_mode"] == "Auto For Flush"

    def test_replating_states_which_extruders_have_the_nozzle(self, written: Path) -> None:
        """#834: the key the slicer groups filaments by, and its newer twin, which it
        reads first; Bambu Studio writes both."""
        stats = ["Standard#0", "Standard#1"]
        moved = replate_3mf(
            written.read_bytes(), plate_for("H2C"), nozzle_diameter="0.2", nozzle_stats=stats
        )
        with zipfile.ZipFile(io.BytesIO(moved)) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert settings["extruder_nozzle_stats"] == stats
        assert settings["extruder_nozzle_stats_new"] == stats

    def test_replating_states_each_extruders_flow(self, written: Path) -> None:
        """#484: the one key Bambu Studio 02.08.02.61 sets per extruder for High Flow,
        in the slicer's order; the CLI keeps it because the H2C printer preset has
        none. ``default_nozzle_volume_type`` is left alone (Studio keeps it Standard),
        and ``slice_info.config`` stays the slicer's output."""
        flows = ["High Flow", "Standard"]
        moved = replate_3mf(
            written.read_bytes(), plate_for("H2C"), nozzle_diameter="0.4", nozzle_volume_type=flows
        )
        with zipfile.ZipFile(io.BytesIO(moved)) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
            names = archive.namelist()
        assert settings["nozzle_volume_type"] == flows
        assert "default_nozzle_volume_type" not in settings
        assert "Metadata/slice_info.config" not in names
        # The five keys the slicer dereferences are all still there.
        for key in (
            "printer_settings_id",
            "print_settings_id",
            "filament_settings_id",
            "nozzle_diameter",
            "printable_height",
        ):
            assert key in settings

    def test_replating_without_a_flow_states_none(self, written: Path) -> None:
        moved = replate_3mf(written.read_bytes(), plate_for("H2C"), nozzle_diameter="0.4")
        with zipfile.ZipFile(io.BytesIO(moved)) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert "nozzle_volume_type" not in settings

    def test_replating_without_nozzle_stats_states_none(self, written: Path) -> None:
        moved = replate_3mf(written.read_bytes(), plate_for("H2C"), nozzle_diameter="0.2")
        with zipfile.ZipFile(io.BytesIO(moved)) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert "extruder_nozzle_stats" not in settings
        assert "extruder_nozzle_stats_new" not in settings

    def test_replating_without_a_nozzle_keeps_the_placeholder(self, written: Path) -> None:
        moved = replate_3mf(written.read_bytes(), plate_for("H2C"))
        with zipfile.ZipFile(io.BytesIO(moved)) as archive:
            settings = json.loads(archive.read("Metadata/project_settings.config"))
        assert settings["nozzle_diameter"] == PLACEHOLDER_NOZZLE_DIAMETER
        assert "filament_map" not in settings

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


def _with_entries(payload: bytes, extra: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as source, zipfile.ZipFile(buffer, "w") as out:
        for info in source.infolist():
            if info.filename not in extra:
                out.writestr(info.filename, source.read(info.filename))
        for name, data in extra.items():
            out.writestr(name, data)
    return buffer.getvalue()


def test_stating_the_nozzles_of_a_library_file_changes_nothing_else(written: Path) -> None:
    """#484: a library file's copy states the flow and nozzle stats as a replated output
    does, and is otherwise the file: same entries, same placement, same settings."""
    payload = written.read_bytes()
    flows, stats = ["High Flow", "High Flow"], ["High Flow#1", "High Flow#0"]

    stated = state_nozzles(payload, nozzle_stats=stats, nozzle_volume_type=flows)

    assert nozzles_statable(payload)
    with (
        zipfile.ZipFile(io.BytesIO(payload)) as before,
        zipfile.ZipFile(io.BytesIO(stated)) as after,
    ):
        assert after.namelist() == before.namelist()
        for name in before.namelist():
            if name != "Metadata/project_settings.config":
                assert after.read(name) == before.read(name), name
        old = json.loads(before.read("Metadata/project_settings.config"))
        new = json.loads(after.read("Metadata/project_settings.config"))
    assert new == {
        **old,
        "nozzle_volume_type": flows,
        "extruder_nozzle_stats": stats,
        "extruder_nozzle_stats_new": stats,
    }
    # The same file stated the same way is the same bytes, so its copy is found again.
    assert state_nozzles(payload, nozzle_stats=stats, nozzle_volume_type=flows) == stated


def test_only_an_unsliced_3mf_with_bambus_settings_can_state_its_nozzles(written: Path) -> None:
    payload = written.read_bytes()
    assert not nozzles_statable(_with_entries(payload, {"Metadata/plate_1.gcode": b"; G28\n"}))
    assert not nozzles_statable(
        _with_entries(payload, {"Metadata/project_settings.config": b"<config/>"})
    )
    assert not nozzles_statable(b"solid stl\nendsolid\n")


def _settings_bomb(size: int) -> bytes:
    """A 3MF whose ``project_settings.config`` is valid JSON of ``size`` bytes, almost
    all whitespace, which deflates to a few KB."""
    body = b'{"nozzle_diameter": ["0.4"]' + b" " * (size - 29) + b"}\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        out.writestr("3D/3dmodel.model", "<model/>")
        out.writestr("Metadata/project_settings.config", body)
    return buffer.getvalue()


def _declaring(payload: bytes, name: str, size: int) -> bytes:
    """``payload`` with ``name``'s uncompressed size declared as ``size`` in both its
    local header and the central directory: a header that lies."""
    data = bytearray(payload)
    encoded = name.encode()
    # (signature, offset of the uncompressed size, offset of the file name)
    for signature, size_at, name_at in ((b"PK\x03\x04", 22, 30), (b"PK\x01\x02", 24, 46)):
        start = data.find(signature)
        while start != -1:
            if data[start + name_at : start + name_at + len(encoded)] == encoded:
                data[start + size_at : start + size_at + 4] = size.to_bytes(4, "little")
            start = data.find(signature, start + 4)
    return bytes(data)


class _Inflated:
    """Counts the bytes every zip entry read hands back."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.total = 0
        read = zipfile.ZipExtFile.read

        def counted(entry: zipfile.ZipExtFile, n: int = -1) -> bytes:
            data = read(entry, n)
            self.total += len(data)
            return data

        monkeypatch.setattr(zipfile.ZipExtFile, "read", counted)


def test_settings_that_expand_past_the_cap_are_not_statable_and_not_inflated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A library file is untrusted (MakerWorld downloads land there too): a few KB that
    inflate to gigabytes must not exhaust the worker. Over the cap is not statable, so
    the file prints as it is, and nothing past the cap is ever decompressed."""
    bomb = _settings_bomb(MAX_SETTINGS_BYTES * 64)
    assert len(bomb) < MAX_SETTINGS_BYTES
    inflated = _Inflated(monkeypatch)

    assert not nozzles_statable(bomb)
    assert inflated.total <= 64 * 1024  # the model's few bytes at most, never the settings


def test_a_header_that_understates_its_size_does_not_get_past_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared size is checked first, but not trusted: reads are counted, and an
    entry that inflates past what it declares is refused."""
    lying = _declaring(
        _settings_bomb(MAX_SETTINGS_BYTES * 64), "Metadata/project_settings.config", 1024
    )
    inflated = _Inflated(monkeypatch)

    assert not nozzles_statable(lying)
    assert inflated.total <= MAX_SETTINGS_BYTES + 64 * 1024


def test_the_archive_cap_bounds_one_prints_memory_and_fits_real_projects() -> None:
    """``state_nozzles`` holds the download and its copy at once, so the cap is that
    print's memory, give or take. The largest real 3MF measured for #484 (a sliced
    multi-plate archive, 18 MB compressed) inflates to 73 MB."""
    assert 73_193_348 * 2 < MAX_UNCOMPRESSED_BYTES <= 256 * 1024 * 1024


def test_an_archive_over_the_total_cap_is_not_statable(written: Path) -> None:
    payload = _declaring(written.read_bytes(), "3D/3dmodel.model", MAX_UNCOMPRESSED_BYTES + 1)
    assert not nozzles_statable(payload)


# --- #1752: whoever laid a 3MF out, a print lays it out the one way ---------------------


def _saved_by_an_author(payload: bytes) -> bytes:
    """``payload`` as Bambu Studio saves a project: its settings name a real printer
    and process preset rather than ScadBuddy's placeholder."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    settings["printer_settings_id"] = "Bambu Lab H2C 0.4 nozzle"
    settings["print_settings_id"] = "0.20mm Standard @BBL H2C"
    return _with_entries(
        payload, {"Metadata/project_settings.config": json.dumps(settings).encode()}
    )


def test_a_3mf_laid_out_for_a_printer_names_it_as_bambu_studio_does(tmp_path: Path) -> None:
    """Bambuddy's slice dialog pre-picks the printer by this name; with ScadBuddy's
    placeholder it took the first one it lists."""
    out = _write(tmp_path / "a1.3mf", covers=False, plate=plate_for("A1"))
    settings = _settings_of(out)
    assert settings["printer_settings_id"] == "Bambu Lab A1 0.4 nozzle"
    # Still ScadBuddy's own layout, to re-place for another printer.
    assert layout_of(out.read_bytes()) == "scadbuddy"


def test_re_placing_names_the_new_printer_and_nozzle(written: Path) -> None:
    payload = written.read_bytes()
    named = replate_3mf(payload, plate_for("H2C"), nozzle_diameter="0.2")
    assert _settings_of_bytes(named)["printer_settings_id"] == "Bambu Lab H2C 0.2 nozzle"
    # Moved to another printer: named for it, at the nozzle the file states.
    moved = replate_3mf(named, plate_for("A1"))
    assert _settings_of_bytes(moved)["printer_settings_id"] == "Bambu Lab A1 0.2 nozzle"
    # The fallback plate is no printer.
    assert _settings_of_bytes(replate_3mf(named, DEFAULT_PLATE))["printer_settings_id"] == (
        PRESET_PLACEHOLDER
    )


def _settings_of_bytes(payload: bytes) -> dict[str, object]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        settings: dict[str, object] = json.loads(archive.read("Metadata/project_settings.config"))
    return settings


def test_a_3mf_is_laid_out_by_scadbuddy_by_its_author_or_as_it_is(written: Path) -> None:
    payload = written.read_bytes()

    assert layout_of(payload) == "scadbuddy"
    assert layout_of(_saved_by_an_author(payload)) == "author"
    # ScadBuddy's placeholder on a file whose plates do not read back is not ScadBuddy's
    # to re-place, but its settings can still be stated.
    assert layout_of(_with_entries(payload, {"3D/3dmodel.model": b"<model/>"})) == "author"
    assert layout_of(_with_entries(payload, {"Metadata/plate_1.gcode": b"; G28\n"})) == "as_is"
    assert layout_of(b"solid stl\nendsolid\n") == "as_is"


def test_stating_an_authors_file_keeps_every_plate_and_states_the_colours(
    tmp_path: Path,
) -> None:
    """The riskiest part of #1752: an author's multi-plate layout is theirs. Only the
    settings change, and in them only the nozzles and the filament colours."""
    payload = _saved_by_an_author(_write_plates(tmp_path / "maze.3mf").read_bytes())
    flows, stats = ["High Flow", "Standard"], ["High Flow#1", "Standard#0"]

    stated = state_nozzles(
        payload,
        nozzle_stats=stats,
        nozzle_volume_type=flows,
        filament_colour=["#00FF00", "#0000FF", "#FFFFFF"],
    )

    with (
        zipfile.ZipFile(io.BytesIO(payload)) as before,
        zipfile.ZipFile(io.BytesIO(stated)) as after,
    ):
        assert after.namelist() == before.namelist()
        for name in before.namelist():
            if name != "Metadata/project_settings.config":
                assert after.read(name) == before.read(name), name
        old = json.loads(before.read("Metadata/project_settings.config"))
        new = json.loads(after.read("Metadata/project_settings.config"))
    assert [plate.index for plate in plates_of(io.BytesIO(stated))] == [1, 2]
    assert new == {
        **old,
        "nozzle_volume_type": flows,
        "extruder_nozzle_stats": stats,
        "extruder_nozzle_stats_new": stats,
        "filament_colour": ["#00FF00", "#0000FF", "#FFFFFF"],
    }


def test_colours_of_another_count_are_not_stated(written: Path) -> None:
    payload = written.read_bytes()

    stated = state_nozzles(payload, filament_colour=["#00FF00"])

    with zipfile.ZipFile(io.BytesIO(stated)) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert settings["filament_colour"] == ["#FF6AC1", "#1F6FEB"]


def test_an_stl_is_wrapped_as_scadbuddys_one_plate_of_one_filament() -> None:
    stl = trimesh.creation.box(extents=(20, 10, 5)).export(file_type="stl")

    wrapped = stl_3mf(stl, model_name="bracket")

    assert layout_of(wrapped) == "scadbuddy"
    assert [plate.index for plate in plates_of(io.BytesIO(wrapped))] == [1]
    with zipfile.ZipFile(io.BytesIO(wrapped)) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert len(settings["filament_colour"]) == 1
    # Laid out for a printer like any of ScadBuddy's own.
    replate_3mf(wrapped, plate_for("H2C"), nozzle_diameter="0.4")


def test_an_stl_with_no_triangles_is_not_wrapped() -> None:
    with pytest.raises(ValueError):
        stl_3mf(b"solid empty\nendsolid empty\n")


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
        "filament_diameter": ["1.75", "1.75"],
        "filament_is_support": ["0", "0"],
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
        # A slice with no profiles segfaults without it (#4089).
        assert settings["filament_diameter"] == ["1.75"]
        assert settings["filament_is_support"] == ["0"]

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

    def test_a_plate_is_named_by_its_plater_name(self, tmp_path: Path) -> None:
        """#929: the name Bambu Studio gives a plate is what the UI labels it by."""
        path = _edit_settings(
            _write(tmp_path / "named.3mf", covers=False),
            lambda config: config.replace(
                '<metadata key="plater_name" value=""/>',
                '<metadata key="plater_name" value="Body"/>',
            ),
        )
        assert [plate.name for plate in plates_of(path)] == ["Body"]

    def test_an_unnamed_plate_falls_back_to_its_object_names(self, tmp_path: Path) -> None:
        """#929: no ``plater_name``, so the objects on the plate name it, joined."""
        path = _edit_settings(
            _write(tmp_path / "objects.3mf", covers=False),
            lambda config: config.replace(
                "</config>",
                ' <object id="90">\n  <metadata key="name" value="Lid"/>\n </object>\n'
                " <plate>\n"
                '  <metadata key="plater_id" value="2"/>\n'
                '  <metadata key="plater_name" value=""/>\n'
                "  <model_instance>\n"
                '   <metadata key="object_id" value="3"/>\n'
                "  </model_instance>\n"
                "  <model_instance>\n"
                '   <metadata key="object_id" value="90"/>\n'
                "  </model_instance>\n"
                " </plate>\n</config>",
            ),
        )
        assert [plate.name for plate in plates_of(path)] == ["two_boxes", "two_boxes + Lid"]

    def test_an_instance_without_an_object_id_names_no_object(self, tmp_path: Path) -> None:
        """#929 review: a missing ``object_id`` and an ``<object>`` with no ``id`` must not
        meet on ``""`` and lend the plate an unrelated object's name."""
        path = _edit_settings(
            _write(tmp_path / "foreign.3mf", covers=False),
            lambda config: config.replace(
                "</config>",
                ' <object>\n  <metadata key="name" value="Stray"/>\n </object>\n'
                " <plate>\n"
                '  <metadata key="plater_id" value="2"/>\n'
                '  <model_instance>\n   <metadata key="instance_id" value="0"/>\n'
                "  </model_instance>\n"
                " </plate>\n</config>",
            ),
        )
        assert [plate.name for plate in plates_of(path)] == ["two_boxes", None]

    def test_a_plate_with_no_name_and_no_objects_has_none(self, tmp_path: Path) -> None:
        path = add_plate(_write(tmp_path / "bare.3mf", covers=False), 2)
        assert [plate.name for plate in plates_of(path)] == ["two_boxes", None]

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


def _edit_settings(path: Path, edit: Callable[[str], str]) -> Path:
    """Rewrite a written 3MF's ``model_settings.config`` through ``edit``."""
    with zipfile.ZipFile(path) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    config = entries["Metadata/model_settings.config"].decode("utf-8")
    entries["Metadata/model_settings.config"] = edit(config).encode("utf-8")
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


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

    def test_the_column_count_is_the_ceiling_of_the_square_root(self) -> None:
        """Spec §6.4 states the rule as ``ceil(sqrt(n))``: rounding up to the nearest
        whole number, or down and adding one, lands on the ceiling either way, and on
        the root itself for a square. Checked well past `MAX_PLATES`."""
        for count in range(1, 10 * MAX_PLATES + 1):
            assert plate_columns(count) == math.ceil(math.sqrt(count)), count

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


def test_a_write_that_fails_part_way_leaves_the_previous_3mf_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#867: the file is replaced whole, so a retried `finish_piece` racing a timed-out
    attempt's thread never leaves (or serves) a half-written archive."""
    out = _write_plates(tmp_path / "model.3mf", covers=False)
    before = out.read_bytes()
    writes = 0
    original = zipfile.ZipFile.writestr

    def failing(self: zipfile.ZipFile, *args: object, **kwargs: object) -> None:
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("disk full")
        original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(zipfile.ZipFile, "writestr", failing)
    with pytest.raises(OSError, match="disk full"):
        _write_plates(out, covers=False)
    assert out.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["model.3mf"]


def _model_zip(body: str, name: str = "3D/3dmodel.model") -> zipfile.ZipFile:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, f'<model xmlns="{CORE_NS}"><resources>{body}</resources></model>')
    return zipfile.ZipFile(io.BytesIO(out.getvalue()))


def test_a_model_file_s_meshes_are_read_into_arrays_and_left_out_of_the_tree() -> None:
    archive = _model_zip(
        '<object id="1"><mesh><vertices>'
        '<vertex x="1" y="2" z="3"/><vertex x="4" y="5" z="6"><x/></vertex><vertex x="7"/>'
        "</vertices><triangles>"
        '<triangle v1="0" v2="1" v3="2"/>'
        '<triangle v1="2" v2="1" v3="0" pid="5" p1="1"/>'
        '<triangle v1="0" v2="2" v3="1" pid="7"/>'
        "</triangles></mesh></object>"
        '<object id="2"><components><component objectid="1"/></components></object>'
    )

    model = parse_model(archive, "3D/3dmodel.model")

    (mesh_element, mesh), *rest = model.meshes.items()
    assert not rest
    assert model.root.find(f".//{{{CORE_NS}}}mesh") is mesh_element
    left = mesh_element.find(f"{{{CORE_NS}}}vertices")
    assert left is not None and not len(left)
    assert model.root.find(f".//{{{CORE_NS}}}component") is not None
    assert mesh.vertices.tolist() == [[1, 2, 3], [4, 5, 6], [7, 0, 0]]
    assert mesh.faces.tolist() == [[0, 1, 2], [2, 1, 0], [0, 2, 1]]
    assert [mesh.pids[i] if i >= 0 else None for i in mesh.pid] == [None, "5", "7"]
    assert mesh.p1.tolist() == [-1, 1, -1]
    assert mesh.paint is None and not mesh.prusa_painted


def test_a_painted_mesh_keeps_each_triangles_paint_code() -> None:
    # The first triangle is unpainted: a mesh's codes start once one is painted (#1965).
    archive = _model_zip(
        '<object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/></vertices><triangles>'
        '<triangle v1="0" v2="0" v3="0"/><triangle v1="0" v2="0" v3="0" paint_color="8"/>'
        '<triangle v1="0" v2="0" v3="0"/></triangles></mesh></object>'
    )

    (mesh,) = parse_model(archive, "3D/3dmodel.model").meshes.values()

    assert mesh.paint == ("", "8", "") and not mesh.prusa_painted


def test_a_paint_code_past_its_cap_is_refused_as_it_is_parsed() -> None:
    # The archive cap bounds a code's bytes, not what decoding one costs: refused while
    # parsing, before any decode sees it (#1965).
    from scadbuddy.render.paint import MAX_CODE_DIGITS

    archive = _model_zip(
        '<object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/></vertices><triangles>'
        f'<triangle v1="0" v2="0" v3="0" paint_color="{"4" * (MAX_CODE_DIGITS + 1)}"/>'
        "</triangles></mesh></object>"
    )

    with pytest.raises(ValueError, match="paint_color"):
        parse_model(archive, "3D/3dmodel.model")


def test_prusaslicer_painting_marks_its_mesh() -> None:
    archive = _model_zip(
        '<object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/></vertices>'
        '<triangles><triangle v1="0" v2="0" v3="0"'
        ' xmlns:s="http://schemas.slic3r.org/3mf/2017/06" s:mmu_segmentation="4"/>'
        "</triangles></mesh></object>"
    )

    (mesh,) = parse_model(archive, "3D/3dmodel.model").meshes.values()

    assert mesh.prusa_painted and mesh.paint is None


def test_a_model_file_past_its_cap_is_refused_whatever_its_header_says() -> None:
    archive = _model_zip("<object/>" * 1000)

    with pytest.raises(ArchiveTooLargeError, match="inflates past 100 bytes"):
        parse_model(archive, "3D/3dmodel.model", cap=100)


def test_a_model_file_costs_about_its_own_size_to_read() -> None:
    """An element per vertex costs some 500 bytes against the 27 of XML it is read from,
    so an untrusted model entry near the archive's cap would take gigabytes (#1949)."""
    vertices = 300_000
    archive = _model_zip(
        '<object id="1"><mesh><vertices>'
        + '<vertex x="1" y="2" z="3"/>' * vertices
        + "</vertices><triangles/></mesh></object>"
    )
    size = archive.getinfo("3D/3dmodel.model").file_size

    tracemalloc.start()
    try:
        model = parse_model(archive, "3D/3dmodel.model")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    (mesh,) = model.meshes.values()
    assert mesh.vertices.shape == (vertices, 3)
    # ET.fromstring measured 13x here; arrays and expat's buffers stay well under 4x.
    assert peak < 4 * size, f"{peak} bytes to read {size}"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            '<triangle v1="0" v2="0" v3="99999999999999999999"/>', id="index-past-64-bits"
        ),
        pytest.param(
            '<triangle v1="0" v2="0" v3="0" p1="99999999999999999999"/>', id="p1-past-64-bits"
        ),
        pytest.param('<triangle v1="x" v2="0" v3="0"/>', id="not-a-number"),
    ],
)
def test_a_triangle_that_is_not_an_index_is_a_value_error(body: str) -> None:
    archive = _model_zip(
        '<object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/></vertices>'
        f"<triangles>{body}</triangles></mesh></object>"
    )

    with pytest.raises(ValueError):
        parse_model(archive, "3D/3dmodel.model")


@pytest.mark.parametrize(
    ("body", "block"),
    [
        pytest.param(
            '<vertices><vertex x="0" y="0" z="0"/></vertices>'
            '<vertices><vertex x="1" y="0" z="0"/></vertices><triangles/>',
            "vertices",
            id="vertices",
        ),
        pytest.param(
            '<vertices><vertex x="0" y="0" z="0"/></vertices>'
            '<triangles><triangle v1="0" v2="0" v3="0"/></triangles>'
            '<triangles><triangle v1="0" v2="0" v3="0"/></triangles>',
            "triangles",
            id="triangles",
        ),
    ],
)
def test_a_mesh_with_a_repeated_block_is_refused(body: str, block: str) -> None:
    """#2024: a second block would be appended to the first, so its indices would not
    mean what the writer meant. The old parse read only the first."""
    archive = _model_zip(f'<object id="1"><mesh>{body}</mesh></object>')

    with pytest.raises(ValueError, match=f"more than one <{block}>"):
        parse_model(archive, "3D/3dmodel.model")


def test_each_mesh_has_its_own_blocks() -> None:
    mesh = (
        '<mesh><vertices><vertex x="0" y="0" z="0"/></vertices>'
        '<triangles><triangle v1="0" v2="0" v3="0"/></triangles></mesh>'
    )
    archive = _model_zip(f'<object id="1">{mesh}</object><object id="2">{mesh}</object>')

    assert len(parse_model(archive, "3D/3dmodel.model").meshes) == 2


def test_a_mesh_naming_too_many_property_groups_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2024: each distinct ``pid`` costs a dict entry, so their number is capped."""
    monkeypatch.setattr(bambu3mf, "MAX_MESH_PIDS", 3)
    triangles = "".join(f'<triangle v1="0" v2="0" v3="0" pid="{n}"/>' for n in range(4))
    archive = _model_zip(
        '<object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/></vertices>'
        f"<triangles>{triangles}</triangles></mesh></object>"
    )

    with pytest.raises(ValueError, match="more than 3 property groups"):
        parse_model(archive, "3D/3dmodel.model")
    # As many as the cap still read, and the same pid again costs nothing.
    triangles = "".join(f'<triangle v1="0" v2="0" v3="0" pid="{n % 3}"/>' for n in range(9))
    archive = _model_zip(
        '<object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/></vertices>'
        f"<triangles>{triangles}</triangles></mesh></object>"
    )
    [arrays] = parse_model(archive, "3D/3dmodel.model").meshes.values()
    assert arrays.pids == ("0", "1", "2")


def test_a_mesh_inside_a_mesh_is_refused() -> None:
    archive = _model_zip(
        '<object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/>'
        "<mesh/></vertices><triangles/></mesh></object>"
    )

    with pytest.raises(ValueError, match="inside another"):
        parse_model(archive, "3D/3dmodel.model")


@pytest.mark.parametrize(
    ("painted_in", "tower"),
    [
        pytest.param(LID, False, id="its-own-filament"),
        pytest.param("#FF6AC1", True, id="another-filament"),
    ],
)
def test_a_painting_needs_a_tower_only_in_a_filament_the_plate_does_not_use(
    tmp_path: Path, painted_in: str, tower: bool
) -> None:
    """#2108: one part, painted all over (state 1 is whole-triangle ``4``) in its own
    filament, uses one filament and needs no tower; in another, it uses two."""
    mesh = trimesh.creation.box(extents=(20, 20, 4))
    paint = Paint(("4",) * len(mesh.faces), (painted_in,))
    part = ColourPart(1, "Lid", LID, mesh, paint=paint)
    colours = [LID] if painted_in == LID else [LID, painted_in]
    out = tmp_path / "painted.3mf"
    write_plates_3mf([PlateParts((part,), (1,))], colours, out, thumbnails=None)
    with zipfile.ZipFile(out) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert ("wipe_tower_x" in settings) is tower


# ── #956: one object per declared object ──────────────────────────────────────


def _two_objects(colours: tuple[str, str] = ("#FF6AC1", "#FF6AC1")) -> PlateParts:
    """Two 10 mm boxes 60 mm apart, each its own object; in one colour by default."""
    left = ColourPart(1, "Left", colours[0], trimesh.creation.box(extents=(10, 10, 4)))
    right = ColourPart(
        2,
        "Right",
        colours[1],
        trimesh.creation.box(extents=(10, 10, 8), transform=_translate(60, 0, 2)),
    )
    extruders = (1, 1) if colours[0] == colours[1] else (1, 2)
    return PlateParts((left, right), extruders, (1, 2))


def _items(payload: bytes) -> list[ET.Element]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        root = parse_model(archive, "3D/3dmodel.model").root
    return list(root.iter(f"{{{bambu3mf.CORE_NS}}}item"))


def test_each_declared_object_is_its_own_build_item_on_its_plate(tmp_path: Path) -> None:
    out = tmp_path / "objects.3mf"
    plate = _two_objects()
    write_plates_3mf([plate], ["#FF6AC1"], out, thumbnails=None, model_name="bins")

    payload = out.read_bytes()
    items = _items(payload)
    # Two objects, each placed by the plate's one offset, so they keep their spacing.
    assert len(items) == 2
    assert items[0].get("transform") == items[1].get("transform")
    with zipfile.ZipFile(out) as archive:
        [settings] = plate_settings(ET.fromstring(archive.read("Metadata/model_settings.config")))
        [laid_out] = laid_out_plates(archive)
    assert len(settings.object_ids) == 2
    assert laid_out.assembly_ids == tuple(item.get("objectid") for item in items)
    assert laid_out.extruders == frozenset({1})
    assert [entry.name for entry in plates_of(out)] == ["bins"]  # named as a plate (#2874 review)
    # A reader of any 3MF (Arrange's) sees the two objects too.
    assert [obj.name for obj in read_objects(payload)] == ["bins (object 1)", "bins (object 2)"]


def test_one_object_writes_the_file_it_always_did(tmp_path: Path) -> None:
    parts = _parts()
    implicit, explicit = tmp_path / "implicit.3mf", tmp_path / "explicit.3mf"
    write_plates_3mf([single_plate(parts)], ["#FF6AC1", "#1F6FEB"], implicit, thumbnails=None)
    write_plates_3mf(
        [PlateParts(tuple(parts), (1, 2), (7, 7))],
        ["#FF6AC1", "#1F6FEB"],
        explicit,
        thumbnails=None,
    )
    assert implicit.read_bytes() == explicit.read_bytes()


def test_a_plate_of_several_objects_is_named_as_a_plate(tmp_path: Path) -> None:
    """#2874 review: a packed plate of many copies is named after the model, not after
    every object's name joined; one object leaves the name unset, as it always did."""
    many, one = tmp_path / "many.3mf", tmp_path / "one.3mf"
    parts = tuple(
        ColourPart(n, f"Bin {n}", "#FF6AC1", trimesh.creation.box(extents=(4, 4, 4)))
        for n in range(1, 21)
    )
    write_plates_3mf(
        [PlateParts(parts, (1,) * 20, tuple(range(1, 21)))],
        ["#FF6AC1"],
        many,
        thumbnails=None,
        model_name="Bins",
    )
    write_plates_3mf([single_plate(_parts())], ["#FF6AC1", "#1F6FEB"], one, thumbnails=None)

    assert [plate.name for plate in plates_of(many)] == ["Bins"]
    # With several plates, each is numbered.
    two = tmp_path / "two.3mf"
    write_plates_3mf(
        [PlateParts(parts[:2], (1, 1), (1, 2)), PlateParts(parts[2:4], (1, 1), (1, 2))],
        ["#FF6AC1"],
        two,
        thumbnails=None,
        model_name="Bins",
    )
    assert [plate.name for plate in plates_of(two)] == ["Bins (plate 1)", "Bins (plate 2)"]
    with zipfile.ZipFile(one) as archive:
        [plate] = plate_settings(ET.fromstring(archive.read("Metadata/model_settings.config")))
    assert plate.metadata["plater_name"] == ""  # a one-object plate is left unnamed


def test_re_placing_moves_every_object_of_a_plate_together(tmp_path: Path) -> None:
    out = tmp_path / "objects.3mf"
    write_plates_3mf([_two_objects()], ["#FF6AC1"], out, thumbnails=None)

    moved = replate_3mf(out.read_bytes(), plate_for("Bambu Lab H2C"))

    transforms = {item.get("transform") for item in _items(moved)}
    assert len(transforms) == 1
    assert transforms != {item.get("transform") for item in _items(out.read_bytes())}
    # One filament across both objects: no prime tower, though the plate has two parts.
    assert "wipe_tower_x" not in _settings_of_bytes(moved)


def test_objects_in_two_colours_still_get_a_prime_tower(tmp_path: Path) -> None:
    out = tmp_path / "objects.3mf"
    write_plates_3mf(
        [_two_objects(("#FF6AC1", "#1F6FEB"))], ["#FF6AC1", "#1F6FEB"], out, thumbnails=None
    )
    moved = replate_3mf(out.read_bytes(), plate_for("Bambu Lab H2C"))
    assert "wipe_tower_x" in _settings_of_bytes(moved)


@pytest.mark.parametrize(("painted_in", "tower"), [("#1F6FEB", True), ("#FF6AC1", False)])
def test_re_placing_two_objects_keeps_the_tower_their_painting_needs(
    tmp_path: Path, painted_in: str, tower: bool
) -> None:
    """#2874 review: two packed copies of a piece painted in a second filament. Their
    parts state one extruder, and replate counted only those, so it dropped the tower
    the painting needs. A painting in the filament they already use still needs none."""
    left, right = _two_objects().parts
    paint = Paint(("4",) * len(right.mesh.faces), (painted_in,))
    painted = ColourPart(right.material_index, right.name, right.colour, right.mesh, paint=paint)
    colours = ["#FF6AC1"] if painted_in == "#FF6AC1" else ["#FF6AC1", painted_in]
    out = tmp_path / "painted-objects.3mf"
    write_plates_3mf([PlateParts((left, painted), (1, 1), (1, 2))], colours, out, thumbnails=None)
    assert ("wipe_tower_x" in _settings_of_bytes(out.read_bytes())) is tower

    moved = replate_3mf(out.read_bytes(), plate_for("Bambu Lab H2C"))

    assert ("wipe_tower_x" in _settings_of_bytes(moved)) is tower


def _rewritten_objects(payload: bytes, change: Callable[[bytes], bytes]) -> bytes:
    """``payload`` with every object file passed through ``change``."""
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(payload)) as archive,
        zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out,
    ):
        for info in archive.infolist():
            data = archive.read(info.filename)
            if info.filename.startswith("3D/Objects/"):
                data = change(data)
            out.writestr(info, data)
    return buffer.getvalue()


def _painted_two_objects(tmp_path: Path) -> bytes:
    left, right = _two_objects().parts
    paint = Paint(("4",) * len(right.mesh.faces), ("#1F6FEB",))
    painted = ColourPart(right.material_index, right.name, right.colour, right.mesh, paint=paint)
    out = tmp_path / "painted-objects.3mf"
    write_plates_3mf(
        [PlateParts((left, painted), (1, 1), (1, 2))], ["#FF6AC1", "#1F6FEB"], out, thumbnails=None
    )
    return out.read_bytes()


def test_re_placing_reads_its_own_object_files_without_parsing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2877: a print's re-placement parsed every vertex and triangle of every object
    file, through Python, to learn one box per plate; an 86 MB library file held the
    print worker for 39 s. A file this writer wrote is read by pattern, and gives the
    very bytes the parse gave."""
    payload = _painted_two_objects(tmp_path)
    parsed = replate_3mf(payload, plate_for("Bambu Lab H2C"))
    real = bambu3mf.parse_model

    def no_object_parse(archive: zipfile.ZipFile, name: str, *args: object, **kw: object) -> object:
        assert not name.startswith("3D/Objects/"), f"{name} was parsed vertex by vertex"
        return real(archive, name, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(bambu3mf, "parse_model", no_object_parse)

    assert replate_3mf(payload, plate_for("Bambu Lab H2C")) == parsed
    assert "wipe_tower_x" in _settings_of_bytes(parsed)


@pytest.mark.parametrize(
    "change",
    [
        # Attributes in another order: not this writer's vertex.
        lambda data: data.replace(b'<vertex x="', b'<vertex  x="'),
        # A comment could hide a vertex the pattern would count.
        lambda data: data.replace(b"<vertices>", b"<vertices><!-- a note -->"),
        # The core namespace under a prefix the pattern does not name.
        lambda data: data.replace(b"<vertex ", f'<m:vertex xmlns:m="{CORE_NS}" '.encode()),
    ],
    ids=["reordered", "comment", "prefixed"],
)
def test_an_object_file_written_otherwise_is_parsed_and_placed_the_same(
    tmp_path: Path, change: Callable[[bytes], bytes]
) -> None:
    payload = _painted_two_objects(tmp_path)
    other = _rewritten_objects(payload, change)

    assert (
        bambu3mf._written_extents(
            zipfile.ZipFile(io.BytesIO(other)).read("3D/Objects/object_1.model")
        )
        is None
    )
    parsed, read = replate_3mf(other, plate_for("H2C")), replate_3mf(payload, plate_for("H2C"))
    assert [item.get("transform") for item in _items(parsed)] == [
        item.get("transform") for item in _items(read)
    ]
    # The painting's tower too, which the paint codes decide.
    assert _settings_of_bytes(parsed) == _settings_of_bytes(read)
    assert "wipe_tower_x" in _settings_of_bytes(parsed)


def test_a_written_paint_code_past_its_cap_is_parsed_and_refused(tmp_path: Path) -> None:
    from scadbuddy.render.paint import MAX_CODE_DIGITS

    long = b'paint_color="' + b"4" * (MAX_CODE_DIGITS + 1) + b'"'
    other = _rewritten_objects(
        _painted_two_objects(tmp_path), lambda data: data.replace(b'paint_color="8"', long)
    )
    with pytest.raises(ValueError, match="paint_color"):
        replate_3mf(other, plate_for("H2C"))


def _object_bytes(
    vertices: bytes,
    triangles: bytes = b'<triangle v1="0" v2="1" v3="2"/>',
    *,
    head: bytes = b"",
    tail: bytes = b"",
) -> bytes:
    """One object file in :func:`bambu3mf.object_model`'s shape, around ``vertices``
    and ``triangles``; ``head`` and ``tail`` go inside the object, around its mesh."""
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\n<model xmlns="'
        + CORE_NS.encode()
        + b'"><resources><object id="1" type="model">'
        + head
        + b"<mesh><vertices>"
        + vertices
        + b"</vertices><triangles>"
        + triangles
        + b"</triangles></mesh>"
        + tail
        + b"</object></resources><build/></model>"
    )


_THREE = b'<vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="2"/>'


@pytest.mark.parametrize(
    "data",
    [
        # #2024: a second <vertices> block, which the parse refuses.
        _object_bytes(_THREE + b'</vertices><vertices><vertex x="9" y="9" z="9"/>'),
        # A second <triangles> block.
        _object_bytes(_THREE, b'<triangle v1="0" v2="1" v3="2"/></triangles><triangles>'),
        # Two meshes in one object.
        _object_bytes(
            _THREE, tail=b'<mesh><vertices><vertex x="5" y="5" z="5"/></vertices></mesh>'
        ),
        # A second object.
        _object_bytes(_THREE, tail=b'</object><object id="2" type="model">'),
    ],
    ids=["two-vertices", "two-triangles", "two-meshes", "two-objects"],
)
def test_other_than_one_object_mesh_and_block_is_left_to_the_parse(data: bytes) -> None:
    assert bambu3mf._written_extents(_object_bytes(_THREE)) is not None
    assert bambu3mf._written_extents(data) is None


def test_a_second_vertices_block_is_refused_as_the_parse_refuses_it(tmp_path: Path) -> None:
    second = b'</vertices>\n    <vertices><vertex x="9" y="9" z="9"/>'
    other = _rewritten_objects(
        _painted_two_objects(tmp_path), lambda data: data.replace(b"</vertices>", second, 1)
    )
    with pytest.raises(ValueError, match="more than one"):
        replate_3mf(other, plate_for("H2C"))


def test_a_processing_instruction_is_left_to_the_parse() -> None:
    assert bambu3mf._written_extents(_object_bytes(_THREE)) is not None
    assert bambu3mf._written_extents(_object_bytes(_THREE, head=b"<?note hi?>")) is None


def test_a_paint_code_off_a_triangle_paints_nothing() -> None:
    """The parse reads ``paint_color`` only on a ``<triangle>``; the pattern agrees."""
    data = _object_bytes(_THREE, head=b"<metadata " + PAINT_ATTRIBUTE.encode() + b'="8"/>')
    # The two paint counts differ, so the file is left to the parse (#3254 review): not
    # read here as unpainted.
    assert bambu3mf._written_extents(data) is None


def test_a_long_coordinate_is_not_copied_into_every_row() -> None:
    """#2877 review: a numpy bytes array is as wide as its longest value, so one 2 MB
    coordinate among a hundred short ones would have cost 600 MB to read a box. A
    coordinate longer than the writer ever writes leaves the file to the parse."""
    vertex = b'\n     <vertex x="1" y="2" z="3"/>'
    long = b'\n     <vertex x="1.' + b"0" * 2_000_000 + b'" y="2" z="3"/>'
    data = _object_bytes(vertex * 100 + long)
    assert bambu3mf._written_extents(_object_bytes(vertex * 101)) is not None

    tracemalloc.start()
    try:
        assert bambu3mf._written_extents(data) is None
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 20 * 1024 * 1024


@pytest.mark.parametrize(
    "junk",
    [b"<triangle ", b'<triangle paint_color="'],
    ids=["unterminated tags", "unterminated codes"],
)
def test_reading_paint_codes_is_one_pass_over_a_crafted_file(junk: bytes) -> None:
    """Security review of #3254: an unbounded run inside the paint pattern rescanned to
    the end of the file from every ``<triangle``, so 200 KB of them took 14 s. Every run
    in it is bounded, so no match reads further than a tag's worth of bytes; checked on
    the pattern itself, not by a clock a loaded shard can skew (#3476 review)."""
    assert not re.search(rb"(?<!\\)[*+]", bambu3mf._PAINT_CODE.pattern)
    # A crafted file is read, whatever it then answers, without hanging on it.
    bambu3mf._written_extents(_object_bytes(_THREE, junk * 40_000))


def test_reading_a_box_makes_no_copy_of_the_file() -> None:
    """#3254 review: skipping the XML declaration by slicing copied the whole object
    file (86 MB on #2942's) just to look past it."""
    padding = b'<metadata name="note">' + b"x" * 4_000_000 + b"</metadata>"
    data = _object_bytes(_THREE, head=padding)
    tracemalloc.start()
    try:
        assert bambu3mf._written_extents(data) is not None
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < len(data) // 4


@pytest.mark.parametrize(
    "root",
    [b'<model xmlns="urn:not-3mf">', b'<model xmlns="%s"><x xmlns="urn:other">' % CORE_NS.encode()],
    ids=["another root namespace", "a default redeclared inside"],
)
def test_vertices_outside_the_core_namespace_are_left_to_the_parse(root: bytes) -> None:
    """#3254 review: a bare ``<vertex>`` is the 3MF's only under the core namespace,
    which the parse requires; the pattern now asks the same of the root."""
    data = _object_bytes(_THREE).replace(b'<model xmlns="' + CORE_NS.encode() + b'">', root, 1)
    assert data != _object_bytes(_THREE)
    assert bambu3mf._written_extents(data) is None


def test_checking_the_root_namespace_is_one_pass_over_a_crafted_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Security review of #3434's fix: a search for the root re-ran its bounded run
    from every ``<model`` in a file made of them. The pattern is tried once, where the
    root starts; checked by what it is asked, not by a clock (#3476 review)."""
    declaration = b'<?xml version="1.0"?>'
    data = declaration + b"\n" + b"<model " * 400_000
    asked: list[int] = []
    real = bambu3mf._CORE_ROOT

    class Once:
        def match(self, text: bytes, pos: int = 0) -> re.Match[bytes] | None:
            asked.append(pos)
            return real.match(text, pos)

        def search(self, *_: object) -> None:
            raise AssertionError("the root is matched where it starts, never searched for")

    monkeypatch.setattr(bambu3mf, "_CORE_ROOT", Once())
    assert bambu3mf._written_extents(data) is None
    assert asked == [len(declaration) + 1]


def test_a_core_model_inside_another_root_is_left_to_the_parse() -> None:
    """#3476 review: the root is the first element, so a ``<model>`` nested in another
    is not taken for the 3MF's."""
    core = _object_bytes(_THREE)
    head, body = core.split(b"?>", 1)
    assert bambu3mf._written_extents(head + b"?><foo>" + body + b"</foo>") is None
    assert bambu3mf._written_extents(core) is not None


def test_a_paint_code_longer_than_the_bounded_read_is_left_to_the_parse() -> None:
    from scadbuddy.render.paint import MAX_CODE_DIGITS

    painted = (
        b'\n     <triangle v1="0" v2="1" v3="2" paint_color="'
        + b"4" * (MAX_CODE_DIGITS + 10)
        + b'"/>'
    )
    assert (
        bambu3mf._written_extents(_object_bytes(b'<vertex x="1" y="2" z="3"/>' * 3, painted))
        is None
    )


def test_written_extents_are_read_a_window_at_a_time() -> None:
    """#3678: vertices are matched a window of the file at a time; a box over several
    windows is the box over every vertex, and none is counted twice or missed."""
    count = 3 * bambu3mf._WINDOW // len(b'<vertex x="0" y="0" z="1"/>')
    vertices = b"".join(b'<vertex x="%d" y="%d" z="1"/>' % (i, -i) for i in range(count))
    data = _object_bytes(vertices)
    assert len(list(bambu3mf._windows(data, 0))) > 2
    box, painted = bambu3mf._written_extents(data) or (None, None)
    assert box is not None
    assert box.tolist() == [[0, -(count - 1), 1], [count - 1, 0, 1]]
    assert painted == frozenset()


def test_a_run_of_paint_attributes_is_counted_without_being_held() -> None:
    """Security review of #3711: the bare attribute needs no ``<``, so a window can hold
    any number of them; collecting them cost four times the file, which at the archive's
    cap is gigabytes."""
    junk = b' paint_color="' * 400_000
    data = _object_bytes(_THREE, tail=b'<metadata name="' + junk + b'"/>')
    tracemalloc.start()
    try:
        bambu3mf._written_extents(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < len(data) // 4


def test_windows_cover_the_file_and_end_before_a_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """#3678: the windows run end to end from where they start, and each but the last
    ends just before a ``<``, so no tag starts in one window and ends in the next."""
    data = _object_bytes(b'<vertex x="1" y="2" z="3"/>' * 50)
    monkeypatch.setattr(bambu3mf, "_WINDOW", 100)
    windows = list(bambu3mf._windows(data, 7))
    assert len(windows) > 5
    assert windows[0][0] == 7 and windows[-1][1] == len(data)
    assert all(end == start for (_, end), (start, _) in pairwise(windows))
    assert all(data[end : end + 1] == b"<" for _, end in windows[:-1])


def _object_files(payload: bytes) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return [
            (name, archive.read(name))
            for name in archive.namelist()
            if name.startswith("3D/Objects/")
        ]


def _parsed(name: str, data: bytes, **limits: int) -> bambu3mf.ParsedModel:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, data)
    return parse_model(zipfile.ZipFile(io.BytesIO(out.getvalue())), name, **limits)


def _assert_read_alike(fast: bambu3mf.ParsedModel, slow: bambu3mf.ParsedModel) -> None:
    """The same arrays and the same tree, but for the space between the vertices and
    triangles the parse keeps as their parent's text (#3679)."""
    (got,), (want,) = fast.meshes.values(), slow.meshes.values()
    for field in ("vertices", "faces", "pid", "p1"):
        a, b = getattr(got, field), getattr(want, field)
        assert a.dtype == b.dtype and a.shape == b.shape and np.array_equal(a, b), field
    assert (got.pids, got.paint, got.prusa_painted) == (want.pids, want.paint, want.prusa_painted)
    blocks = {f"{{{CORE_NS}}}vertices", f"{{{CORE_NS}}}triangles"}
    for x, y in zip(fast.root.iter(), slow.root.iter(), strict=True):
        assert (x.tag, x.attrib, x.tail) == (y.tag, y.attrib, y.tail)
        assert x.text == y.text or (x.tag in blocks and not x.text and not (y.text or "").strip())


def _parsed_by_xml(monkeypatch: pytest.MonkeyPatch, name: str, data: bytes) -> bambu3mf.ParsedModel:
    with monkeypatch.context() as patch:
        patch.setattr(bambu3mf, "_written_mesh", lambda *_: None)
        return _parsed(name, data)


@pytest.mark.parametrize("window", [None, 64], ids=["one window", "many windows"])
def test_a_written_object_file_is_read_by_pattern_as_the_parse_reads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: int | None
) -> None:
    """#3679: an object file in the writer's own shape has its vertices and triangles
    read by pattern, painted ones included, and comes out as the parse reads it."""
    if window is not None:
        monkeypatch.setattr(bambu3mf, "_WINDOW", window)
    files = _object_files(_painted_two_objects(tmp_path))
    assert any(b"paint_color" in data for _, data in files)
    for name, data in files:
        assert bambu3mf._written_mesh(data, None, None) is not None
        _assert_read_alike(_parsed(name, data), _parsed_by_xml(monkeypatch, name, data))


def test_the_rest_of_a_written_object_file_is_still_parsed_as_xml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3679: only the two blocks are read by pattern; an entity outside them is the
    XML parser's to expand, and a file that is not XML is refused as it always was."""
    name, data = _object_files(_painted_two_objects(tmp_path))[0]
    named = data.replace(b'name="', b'name="&#60;', 1)
    fast = bambu3mf._written_mesh(named, None, None)
    assert fast is not None
    _assert_read_alike(fast, _parsed_by_xml(monkeypatch, name, named))
    with pytest.raises(ET.ParseError):
        _parsed(name, data.replace(b"</object>", b"</objekt>"))


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.replace(b"<mesh>", b"<!-- a note --><mesh>"),
        lambda data: data.replace(b"<mesh>", b"<?note hi?><mesh>"),
        lambda data: data.replace(b'<vertex x="', b'<vertex id="1" x="', 1),
        lambda data: re.sub(rb'x="[^"]*"', b'x="1e2"', data, count=1),
        lambda data: data.replace(b'"/>\n    </triangles>', b'" pid="1"/>\n    </triangles>'),
    ],
    ids=["comment", "instruction", "attribute", "exponent", "pid"],
)
def test_an_object_file_written_otherwise_is_left_to_the_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: Callable[[bytes], bytes]
) -> None:
    """#3679: anything the pattern does not read exactly as the parse would, or that
    could hide a block's tags, is parsed as it always was."""
    name, data = _object_files(_painted_two_objects(tmp_path))[0]
    other = change(data)
    assert other != data
    assert bambu3mf._written_mesh(other, None, None) is None
    _assert_read_alike(_parsed(name, other), _parsed_by_xml(monkeypatch, name, other))


def test_a_written_object_file_past_its_limits_is_refused_as_the_parse_refuses_it(
    tmp_path: Path,
) -> None:
    from scadbuddy.render.bambu3mf import TooManyTrianglesError

    name, data = _object_files(_painted_two_objects(tmp_path))[0]
    with pytest.raises(TooManyTrianglesError) as triangles:
        _parsed(name, data, max_triangles=1)
    with pytest.raises(TooManyTrianglesError) as vertices:
        _parsed(name, data, max_vertices=1)
    assert (triangles.value.kind, vertices.value.kind) == ("triangles", "vertices")


def test_an_entry_too_large_to_hold_is_parsed_as_it_inflates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name, data = _object_files(_painted_two_objects(tmp_path))[0]
    expected = _parsed_by_xml(monkeypatch, name, data)
    monkeypatch.setattr(bambu3mf, "_HELD_ENTRY", len(data) // 3)

    def unused(*_: object) -> None:
        raise AssertionError("an entry past what is held was held")

    monkeypatch.setattr(bambu3mf, "_written_mesh", unused)
    _assert_read_alike(_parsed(name, data), expected)


# --- #4013: a painted slot is named where Bambuddy reads it ---------------------------


def _cats(object_paint: dict[str, str]) -> bytes:
    """Two plates of one object each, the shape of library file 2440: a root of
    components, each object's meshes in its own file, and ``object_paint`` (object id to
    one triangle's paint code) painting a triangle of that object's file."""
    root = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter"'
        f' xmlns="{CORE_NS}" xmlns:p="{PRODUCTION_NS}"><resources>'
    )
    files: dict[str, bytes] = {}
    for object_id in ("6", "7"):
        path = f"3D/Objects/object_{object_id}.model"
        code = object_paint.get(object_id)
        paint = f' paint_color="{code}"' if code else ""
        files[path] = (
            f'<model unit="millimeter" xmlns="{CORE_NS}"><resources><object id="1"'
            ' type="model"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0"'
            ' z="0"/><vertex x="0" y="1" z="0"/></vertices><triangles>'
            f'<triangle v1="0" v2="1" v3="2"/><triangle v1="0" v2="2" v3="1"{paint}/>'
            "</triangles></mesh></object></resources></model>"
        ).encode()
        root += (
            f'<object id="{object_id}" type="model"><components>'
            f'<component p:path="/{path}" objectid="1"/></components></object>'
        )
    root += '</resources><build><item objectid="6"/><item objectid="7"/></build></model>'
    settings = "<config>\n"
    for object_id in ("6", "7"):
        settings += (
            f'  <object id="{object_id}">\n    <metadata key="extruder" value="1"/>\n'
            '    <part id="1" subtype="normal_part">\n'
            '      <metadata key="name" value="body"/>\n    </part>\n  </object>\n'
        )
    for plate, object_id in ((1, "6"), (2, "7")):
        settings += (
            f'  <plate>\n    <metadata key="plater_id" value="{plate}"/>\n'
            f'    <model_instance>\n      <metadata key="object_id" value="{object_id}"/>\n'
            "    </model_instance>\n  </plate>\n"
        )
    settings += "</config>\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        out.writestr("3D/3dmodel.model", root)
        for name, data in files.items():
            out.writestr(name, data)
        out.writestr(MODEL_SETTINGS_NAME, settings)
        out.writestr(
            "Metadata/project_settings.config",
            json.dumps({"filament_colour": ["#D3CFB5", "#27272C"]}),
        )
    return buffer.getvalue()


def _bambuddy_slots(payload: bytes, plate: int) -> set[int]:
    """The slots Bambuddy's ``extract_plate_extruder_set_from_3mf`` reads from the
    settings for ``plate``: each object's and each part's ``extruder``. (It also reads
    the painting, one slot per hex digit, which names no real slot here.)"""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        config = ET.fromstring(archive.read(MODEL_SETTINGS_NAME))
    objects: dict[str, set[int]] = {}
    for obj in config.findall(".//object"):
        found = {int(m.get("value", "0")) for m in obj.findall("metadata[@key='extruder']")}
        for part in obj.findall(".//part"):
            found |= {int(m.get("value", "0")) for m in part.findall("metadata[@key='extruder']")}
        objects[obj.get("id", "")] = found
    used: set[int] = set()
    for each in config.findall(".//plate"):
        if each.find("metadata[@key='plater_id']").get("value") != str(plate):  # type: ignore[union-attr]
            continue
        for instance in each.findall("model_instance"):
            used |= objects[instance.find("metadata").get("value", "")]  # type: ignore[union-attr]
    return used


def test_a_painted_slot_is_named_on_a_part_that_prints_nothing() -> None:
    """Library file 2440's eyes are painted filament 2 (``8``), which Bambuddy reads as
    slot 8: plate 2 sliced its black spool with the glow preset and colour. The hint
    names slot 2 on a negative part of the plate's object, with an id no part has."""
    payload = _cats({"7": "8"})
    assert _bambuddy_slots(payload, 2) == {1}

    hinted = hint_painted_slots(payload)

    assert _bambuddy_slots(hinted, 2) == {1, 2}
    assert _bambuddy_slots(hinted, 1) == {1}
    with zipfile.ZipFile(io.BytesIO(hinted)) as after:
        config = ET.fromstring(after.read(MODEL_SETTINGS_NAME))
        with zipfile.ZipFile(io.BytesIO(payload)) as before:
            for name in before.namelist():
                if name != MODEL_SETTINGS_NAME:
                    assert after.read(name) == before.read(name), name
    hints = [
        part
        for part in config.iter("part")
        if part.find("metadata[@key='name']").get("value") == SLOT_HINT_NAME  # type: ignore[union-attr]
    ]
    assert [(part.get("id"), part.get("subtype")) for part in hints] == [("8", "negative_part")]
    # Deterministic, so a copy uploaded earlier is found by its hash.
    assert hint_painted_slots(payload) == hinted


def test_each_object_is_named_the_slots_its_own_painting_uses() -> None:
    # Filament 3 is a ``11`` leaf and then four bits, two hex digits, unlike 1 and 2.
    hinted = hint_painted_slots(_cats({"6": single_code(3), "7": single_code(2)}))
    assert _bambuddy_slots(hinted, 1) == {1, 3}
    assert _bambuddy_slots(hinted, 2) == {1, 2}


def test_an_unpainted_file_is_left_alone() -> None:

    plain = _cats({})
    assert hint_painted_slots(plain) is plain


def test_a_paint_code_split_by_a_read_window_is_read_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bambu3mf, "_CHUNK", 7)
    assert _bambuddy_slots(hint_painted_slots(_cats({"7": "8"})), 2) == {1, 2}


def test_a_paint_code_that_does_not_decode_names_nothing() -> None:
    payload = _cats({"7": "F"})  # a split with no children
    assert hint_painted_slots(payload) is payload
