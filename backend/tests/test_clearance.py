"""#956: the by-object clearance pre-check, a warning and never a refusal."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh

from scadbuddy.bambuddy.clearance import (
    PlacedObject,
    clearance_for,
    clearance_warnings,
    placed_objects,
)
from scadbuddy.render.bambu3mf import PlateParts, write_plates_3mf
from scadbuddy.render.split import ColourPart

H2C = clearance_for("H2C")


def _box(name: str, x: float, height: float = 10.0) -> PlacedObject:
    return PlacedObject(name, (x, 0.0, 0.0), (x + 20.0, 20.0, height))


def _at(x: float, height: float) -> np.ndarray:
    matrix = np.eye(4)
    matrix[:3, 3] = (x, 0, height / 2)
    return matrix


def two_objects_3mf(out: Path, *, apart: float, height: float = 10.0) -> bytes:
    """Two 20 mm boxes ``apart`` mm apart edge to edge, each its own object."""
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
        [PlateParts(parts, (1, 1), (1, 2))], ["#FF6AC1"], out, thumbnails=None, model_name="bins"
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
