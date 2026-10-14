"""#956: the by-object clearance pre-check, a warning and never a refusal."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh

from scadbuddy.bambuddy import clearance
from scadbuddy.bambuddy.clearance import (
    PlacedObject,
    clearance_for,
    clearance_warnings,
    placed_objects,
    placed_objects_by_plate,
)
from scadbuddy.render import bambu3mf
from scadbuddy.render.bambu3mf import PlateParts, write_plates_3mf
from scadbuddy.render.read_budget import ReadBudget
from scadbuddy.render.split import ColourPart

H2C = clearance_for("H2C")


def _box(name: str, x: float, height: float = 10.0) -> PlacedObject:
    return PlacedObject(name, (x, 0.0, 0.0), (x + 20.0, 20.0, height))


def _at(x: float, height: float) -> np.ndarray:
    matrix = np.eye(4)
    matrix[:3, 3] = (x, 0, height / 2)
    return matrix


def two_objects_3mf(out: Path, *, apart: float, height: float = 10.0, plates: int = 1) -> bytes:
    """Two 20 mm boxes ``apart`` mm apart edge to edge, each its own object, on each of
    ``plates`` plates."""
    parts = tuple(
        ColourPart(
            index,
            f"Box {index}",
            "#FF6AC1",
            trimesh.creation.box(extents=(20, 20, height), transform=_at(x, height)),
        )
        for index, x in ((1, 0.0), (2, 20.0 + apart))
    )
    write_plates_3mf(
        [PlateParts(parts, (1, 1), (1, 2))] * plates,
        ["#FF6AC1"],
        out,
        thumbnails=None,
        model_name="bins",
    )
    return out.read_bytes()


def test_the_h2c_clearances_come_from_its_machine_profile() -> None:
    assert H2C is not None
    assert (H2C.radius, H2C.height_to_rod, H2C.height_to_lid) == (49.0, 47.4, 201.0)
    assert clearance_for("Bambu Lab H2C 0.4 nozzle") == H2C
    assert clearance_for("No Such Printer") is None


def test_objects_closer_than_the_toolhead_warn() -> None:
    assert H2C is not None
    [warning] = clearance_warnings([_box("A", 0), _box("B", 30)], H2C)
    assert warning.kind == "sequence-clearance"
    assert "A and B are 10 mm apart" in warning.message
    assert "49 mm" in warning.message


def test_objects_far_enough_apart_and_short_say_nothing() -> None:
    assert H2C is not None
    assert clearance_warnings([_box("A", 0), _box("B", 80)], H2C) == []


def test_one_object_clears_everything() -> None:
    assert H2C is not None
    assert clearance_warnings([_box("A", 0, height=300)], H2C) == []


def test_only_one_object_may_pass_under_the_gantry() -> None:
    assert H2C is not None
    one_tall = [_box("A", 0, 60), _box("B", 100)]
    two_tall = [_box("A", 0, 60), _box("B", 100, 50)]
    assert clearance_warnings(one_tall, H2C) == []
    [warning] = clearance_warnings(two_tall, H2C)
    assert "only one object may be taller than 47.4 mm" in warning.message
    assert "A, B" in warning.message


def test_an_object_past_the_lid_warns() -> None:
    assert H2C is not None
    [*_, warning] = clearance_warnings([_box("A", 0, 210), _box("B", 100)], H2C)
    assert "A is 210 mm tall" in warning.message


def test_the_writers_objects_are_read_where_they_sit(tmp_path: Path) -> None:
    payload = two_objects_3mf(tmp_path / "bins.3mf", apart=15)
    objects = placed_objects(payload, 1)
    assert [obj.name for obj in objects] == ["bins (object 1)", "bins (object 2)"]
    gap = objects[1].low[0] - objects[0].high[0]
    assert abs(gap - 15) < 1e-6
    assert all(abs(obj.height - 10) < 1e-6 for obj in objects)
    assert placed_objects(payload, 2) == []


def _fan_out_3mf(levels: int, fan: int) -> bytes:
    """One build item whose object names the next level ``fan`` times, ``levels`` deep,
    ending in one triangle: read naively, ``fan ** levels`` meshes."""
    core = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    objects = [
        '<object id="1" type="model"><mesh><vertices>'
        '<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="5"/>'
        '</vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
    ]
    for level in range(2, levels + 2):
        components = "".join(
            f'<component objectid="{level - 1}" transform="1 0 0 0 1 0 0 0 1 {i} 0 0"/>'
            for i in range(fan)
        )
        objects.append(
            f'<object id="{level}" type="model"><components>{components}</components></object>'
        )
    model = (
        f'<model xmlns="{core}" unit="millimeter"><resources>{"".join(objects)}</resources>'
        f'<build><item objectid="{levels + 1}"/></build></model>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("3D/3dmodel.model", model)
    return buffer.getvalue()


def test_an_object_named_many_times_is_read_once() -> None:
    """A security review's fan-out: 40 components a level, 6 levels, is 4e9 meshes read
    naively; each object's box is read once, so it is 7."""
    [obj] = placed_objects(_fan_out_3mf(levels=6, fan=40), 1)
    assert obj.low == (0.0, 0.0, 0.0)
    assert obj.high == (10.0 + 6 * 39, 10.0, 5.0)


def test_a_file_past_the_read_budget_is_not_checked() -> None:
    with pytest.raises(ValueError, match="read budget"):
        placed_objects(_fan_out_3mf(levels=6, fan=2), 1, ReadBudget(max_visits=3))


def test_a_model_file_is_held_to_the_one_file_cap_whatever_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clearance, "MAX_UNCOMPRESSED_BYTES", 100)
    with pytest.raises(ValueError, match="read budget"):
        placed_objects(_fan_out_3mf(levels=1, fan=1), 1, ReadBudget.widest())


def test_every_plate_is_read_in_one_pass_of_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2875 review: each plate re-opened and re-parsed every model file under a fresh
    budget, so an N-plate check did N times the work the budget meant to cap."""
    parts = [
        ColourPart(1, f"Box {n}", "#FF6AC1", trimesh.creation.box(extents=(20, 20, 10)))
        for n in (1, 2)
    ]
    out = tmp_path / "two-plates.3mf"
    write_plates_3mf(
        [PlateParts((parts[0],), (1,)), PlateParts((parts[1],), (1,))],
        ["#FF6AC1"],
        out,
        thumbnails=None,
    )
    parsed: list[str] = []
    real = bambu3mf.parse_model

    def counting(archive: zipfile.ZipFile, name: str, *args: Any, **kwargs: Any) -> Any:
        parsed.append(name)
        return real(archive, name, *args, **kwargs)

    monkeypatch.setattr(clearance, "parse_model", counting)

    by_plate = placed_objects_by_plate(out.read_bytes(), [1, 2])

    assert [len(by_plate[1]), len(by_plate[2])] == [1, 1]
    assert len(parsed) == len(set(parsed))


def test_a_component_naming_a_file_the_archive_lacks_is_a_value_error() -> None:
    core = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    production = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    model = (
        f'<model xmlns="{core}" xmlns:p="{production}" unit="millimeter"><resources>'
        '<object id="1" type="model"><components>'
        '<component p:path="/3D/Objects/missing.model" objectid="1"/>'
        '</components></object></resources><build><item objectid="1"/></build></model>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("3D/3dmodel.model", model)

    with pytest.raises(ValueError, match="does not hold"):
        placed_objects(buffer.getvalue(), 1)


def _repeated_items_3mf(items: int) -> bytes:
    """One small object placed ``items`` times by repeated build items (#3251)."""
    core = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    model = (
        f'<model xmlns="{core}" unit="millimeter"><resources>'
        '<object id="1" type="model"><mesh><vertices>'
        '<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="5"/>'
        '</vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
        f"</resources><build>{'<item objectid="1"/>' * items}</build></model>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("3D/3dmodel.model", model)
    return buffer.getvalue()


def test_each_build_item_costs_a_visit_though_its_box_is_read_once() -> None:
    """#3251: the box read is cached, so only the first of many items naming one object
    was charged, and the rest were free."""
    assert len(placed_objects(_repeated_items_3mf(5), 1, ReadBudget(max_visits=10))) == 5
    with pytest.raises(ValueError, match="read budget"):
        placed_objects(_repeated_items_3mf(50), 1, ReadBudget(max_visits=10))


def test_a_file_placing_more_objects_than_the_budget_is_not_checked() -> None:
    """#3251: every placed item is an object the pairwise check compares with every
    other, so a file is held to the read budget's object count too."""
    most = ReadBudget().max_objects
    assert len(placed_objects(_repeated_items_3mf(most), 1)) == most
    with pytest.raises(ValueError, match="read budget"):
        placed_objects(_repeated_items_3mf(most + 1), 1)
