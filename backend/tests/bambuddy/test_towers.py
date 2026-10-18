"""#4014: an author's prime tower that the printer cannot reach is moved where it can."""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh

from scadbuddy.bambuddy import send
from scadbuddy.bambuddy.send import towers_within_reach
from scadbuddy.render.bambu3mf import (
    MODEL_SETTINGS_NAME,
    PROJECT_SETTINGS_NAME,
    ROOT_MODEL_NAME,
    PlateParts,
    plate_origin,
    rezipped,
    write_plates_3mf,
)
from scadbuddy.render.objects3mf import UnreadableObjectsError, plate_boxes, read_objects
from scadbuddy.render.plate import (
    DEFAULT_PLATE,
    PRIME_TOWER_BRIM,
    PRIME_TOWER_SIDE,
    TOWER_CLEARANCE,
    Rect,
    plate_for,
    tower_reaches,
)
from scadbuddy.render.split import ColourPart

A1_MINI = plate_for("A1 mini")
H2C = plate_for("H2C")


def _laid_out_on_an_a1_mini(tmp_path: Path, towers: tuple[str, str] = ("15", "145")) -> bytes:
    """Library file 2440's shape: two plates of two colours each, laid out on an A1
    mini by its author, the tower at the A1 mini's x 15 on both."""
    body = trimesh.creation.box(extents=(30, 30, 20))
    eye = trimesh.creation.box(extents=(4, 4, 4))
    plates = [
        PlateParts(
            (ColourPart(1, "Body", "#D3CFB5", body), ColourPart(2, "Eye", "#27272C", eye)),
            (1, 2),
        )
        for _ in range(2)
    ]
    out = tmp_path / "cats.3mf"
    write_plates_3mf(plates, ["#D3CFB5", "#27272C"], out, thumbnails=None, plate=A1_MINI)
    payload = out.read_bytes()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    settings.update(
        print_settings_id="0.20mm Standard @BBL A1M",
        printable_area=["0x0", "180x0", "180x180", "0x180"],
        wipe_tower_x=[towers[0], towers[0]],
        wipe_tower_y=[towers[1], towers[1]],
        prime_tower_width="22",
    )
    return rezipped(payload, {PROJECT_SETTINGS_NAME: json.dumps(settings).encode()})


def _towers(payload: bytes) -> list[tuple[float, float]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    return [
        (float(x), float(y))
        for x, y in zip(settings["wipe_tower_x"], settings["wipe_tower_y"], strict=True)
    ]


def test_a_tower_the_h2c_cannot_reach_is_moved_beside_each_plates_objects(
    tmp_path: Path,
) -> None:
    """Library 2440 failed every H2C slice, "Found G-code in unprintable area": its
    tower at x 15 is below the x 25 the second extruder reaches."""
    payload = _laid_out_on_an_a1_mini(tmp_path)
    assert not tower_reaches((15, 145), H2C)

    moved = towers_within_reach(payload, H2C)

    boxes = plate_boxes(moved).boxes
    reserved = PRIME_TOWER_SIDE + 2 * PRIME_TOWER_BRIM
    for index, (x, y) in enumerate(_towers(moved), start=1):
        assert tower_reaches((x, y), H2C)
        (low, high), (ox, oy) = boxes[index], plate_origin(index, 2, A1_MINI)
        x0, y0 = x - PRIME_TOWER_BRIM, y - PRIME_TOWER_BRIM
        footprint = Rect(x0, y0, x0 + reserved, y0 + reserved)
        clear = Rect(
            low[0] - ox - TOWER_CLEARANCE,
            low[1] - oy - TOWER_CLEARANCE,
            high[0] - ox + TOWER_CLEARANCE,
            high[1] - oy + TOWER_CLEARANCE,
        )
        assert not footprint.overlaps(clear)
    with (
        zipfile.ZipFile(io.BytesIO(payload)) as before,
        zipfile.ZipFile(io.BytesIO(moved)) as after,
    ):
        for name in before.namelist():
            if name != PROJECT_SETTINGS_NAME:
                assert after.read(name) == before.read(name), name


def test_a_tower_every_extruder_reaches_is_left_where_its_author_put_it(tmp_path: Path) -> None:
    """Judged at the file's own 22 mm width: (165, 250) is the tower that sliced plate 2
    by hand (job 151), and #105's 60 mm square there would cross the H2C's wrapping
    strip at y 310."""
    payload = _laid_out_on_an_a1_mini(tmp_path, ("165", "250"))
    assert towers_within_reach(payload, H2C) is payload
    # The A1 mini reaches its own tower.
    unmoved = _laid_out_on_an_a1_mini(tmp_path)
    assert towers_within_reach(unmoved, A1_MINI) is unmoved


def test_no_printer_chosen_moves_no_tower(tmp_path: Path) -> None:
    payload = _laid_out_on_an_a1_mini(tmp_path)
    assert towers_within_reach(payload, DEFAULT_PLATE) is payload


def _without_settings(payload: bytes, settings: bytes | None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as source, zipfile.ZipFile(buffer, "w") as out:
        for info in source.infolist():
            if info.filename == MODEL_SETTINGS_NAME:
                if settings is not None:
                    out.writestr(info, settings)
                continue
            out.writestr(info, source.read(info))
    return buffer.getvalue()


def test_a_file_whose_model_settings_do_not_parse_keeps_its_towers(tmp_path: Path) -> None:
    """The file sent before #4014 and still does, its tower where its author put it
    (claude-review on #4057)."""
    damaged = _without_settings(_laid_out_on_an_a1_mini(tmp_path), b"<config><plate>")
    assert towers_within_reach(damaged, H2C) is damaged


def test_a_file_with_no_model_settings_is_one_plate_and_still_sends(tmp_path: Path) -> None:
    """Read as every reader reads one: a single plate holding every build item."""
    bare = _without_settings(_laid_out_on_an_a1_mini(tmp_path), None)
    moved = towers_within_reach(bare, H2C)
    assert tower_reaches(_towers(moved)[0], H2C)


def test_objects_that_leave_no_room_keep_the_authors_tower(tmp_path: Path) -> None:
    """A plate filled edge to edge has nowhere every extruder reaches clear of it: the
    tower stays, and the slicer says why rather than ScadBuddy guessing."""
    payload = _laid_out_on_an_a1_mini(tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(send, "tower_beside", lambda rect, plate: None)
        assert towers_within_reach(payload, H2C) is payload


def test_a_painted_mirrored_object_is_boxed_though_it_cannot_be_read_whole(
    tmp_path: Path,
) -> None:
    """2440's objects are mirrored and painted, which Arrange refuses (#4015); the tower
    needs only their box, which the mirror moves to the other side of the item."""
    payload = _laid_out_on_an_a1_mini(tmp_path)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        root = archive.read(ROOT_MODEL_NAME).decode()
    before = plate_boxes(payload).boxes[1]
    mirrored = re.sub(r'transform="1 0 0 ', 'transform="-1 0 0 ', root, count=1)
    assert mirrored != root
    flipped = rezipped(payload, {ROOT_MODEL_NAME: mirrored.encode()})
    painted = _painted(flipped)

    box = plate_boxes(painted).boxes[1]

    assert np.allclose(box[1] - box[0], before[1] - before[0])
    with pytest.raises(UnreadableObjectsError, match="painted and mirrored"):
        read_objects(painted)


def _painted(payload: bytes) -> bytes:
    """Every triangle of every object file painted filament 2."""
    changed: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in archive.namelist():
            if name.startswith("3D/Objects/"):
                data = archive.read(name)
                changed[name] = re.sub(rb"(<triangle [^/]*)/>", rb'\1 paint_color="8"/>', data)
    return rezipped(payload, changed)
