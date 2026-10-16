"""dollhouse-kit's whole-house pipeline (spec 2026-09-27 §5.4; epic #427 "Done when")."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.support.pipelines import FakeWorld, a_job, run_job
from tests.support.temporal import temporal_client

TEMPLATE = Path(__file__).resolve().parents[2] / "models" / "dollhouse-kit"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "dollhouse_pipeline", TEMPLATE / "pipeline" / "pipeline.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_labels_match_the_designer() -> None:
    labels = {
        e["id"]: e["label"]
        for e in _module().house_pieces({"cols": 1, "rows": 1, "storeys": 2, "windows": 1})
    }
    assert labels["wall:lower"] == "Wall, lower course"
    assert labels["wall_door_lower"] == "Door wall, lower course"
    assert labels["corner_post:upper"] == "Corner post, upper course"
    assert labels["connectors"] == "Connectors (keys, pegs, hinge pins)"


def test_a_two_by_one_house_counts_like_the_designer() -> None:
    counts = {
        e["id"]: e["count"]
        for e in _module().house_pieces({"cols": 2, "rows": 1, "storeys": 1, "windows": 2})
    }
    # ui/pieces.js: walls and corner posts come in a lower and an upper course.
    assert counts == {
        "wall_door_lower": 1,
        "wall:lower": 5,
        "wall_door_upper": 1,
        "wall_window": 2,
        "wall:upper": 3,
        "corner_post:lower": 4,
        "corner_post:upper": 4,
        "floor_tile": 2,
        "roof_panel": 2,
        "door_leaf_lower": 1,
        "door_leaf_upper": 1,
        "connectors": 1,
    }


@pytest.mark.parametrize(
    "case",
    json.loads((TEMPLATE / "pipeline" / "piece-counts.json").read_text(encoding="utf-8"))["cases"],
    ids=lambda case: "x".join(str(v) for v in case["house"].values()),
)
def test_the_pipeline_counts_every_piece_as_the_designer_does(case: dict[str, Any]) -> None:
    """#905: the designer's counts (ui/pieces.js, checked against the same file by
    frontend/src/template-ui/dollhouse.test.ts) are the pieces the server renders."""
    counts = {e["id"]: e["count"] for e in _module().house_pieces(case["house"])}
    # The designer leaves connectors to the user; the pipeline renders one sheet of them.
    assert case["counts"].pop("connectors") is None
    assert counts.pop("connectors") == 1
    assert counts == case["counts"]
    assert sum(counts.values()) == case["total"]


def test_each_coursed_piece_renders_its_own_course() -> None:
    m = _module()
    entries = {e["id"]: e for e in m.house_pieces({"cols": 1, "rows": 1, "storeys": 1})}
    assert m.piece_params(entries["corner_post:lower"], {})["course"] == "lower"
    assert m.piece_params(entries["wall_window"], {})["course"] == "upper"
    assert "course" not in m.piece_params(entries["floor_tile"], {})


#: Every style parameter set away from its default (model.scad), and the hidden preview.
EVERY_STYLE = {
    "trim": False,
    "door_style": "french",
    "door_width": 120,
    "door_height": 350,
    "connector_type": "hinge_pins",
    "preview": "room",
}
DOOR_READERS = {"door_style", "door_width", "door_height"}
#: Which of trim and the door's shape each piece's geometry reads (model.scad: `LIN`/`FW`
#: in `dr_w` and the opening's architrave; `DOOR_PIECE`, whose hinge pins double for
#: French doors and are sized from the door).
READS = {
    "wall": set(),
    "corner_post": set(),
    "wall_window": {"trim"},
    "wall_door_lower": {"trim", *DOOR_READERS},
    "wall_door_upper": {"trim", *DOOR_READERS},
    "door_leaf_lower": {"trim", *DOOR_READERS},
    "door_leaf_upper": {"trim", *DOOR_READERS},
    "connectors": {"trim", *DOOR_READERS},
    "floor_tile": set(),
    "roof_panel": set(),
    "stairs_lower": set(),
    "stairs_upper": set(),
    "railing": set(),
}


def test_each_piece_carries_the_trim_and_door_style_it_reads() -> None:
    module = _module()
    entries = module.house_pieces({"cols": 2, "rows": 2, "storeys": 2, "windows": 2})
    assert {e["piece"] for e in entries} == set(READS)
    for entry in entries:
        params = module.piece_params(entry, EVERY_STYLE)
        carried = {"trim", *DOOR_READERS} & set(params)
        assert carried == READS[entry["piece"]], entry["id"]
        assert "preview" not in params, entry["id"]


@pytest.mark.parametrize(("value", "expected"), [(None, 1), ("", 1), ("3", 3), ("x", 2)])
def test_a_null_or_empty_value_counts_as_zero_like_the_designer(
    value: object, expected: int
) -> None:
    # clampHouse: Number(null) and Number("") are 0 (clamped to 1); Number("x") is NaN
    # (the default).
    assert _module().clamp_house({"cols": value})["cols"] == expected


def test_the_house_is_clamped_to_the_designer_limits() -> None:
    assert _module().clamp_house({"cols": 9, "rows": 0, "storeys": 2, "windows": -1}) == {
        "cols": 4,
        "rows": 1,
        "storeys": 2,
        "windows": 0,
    }


def test_v0_inputs_migrate_to_a_default_house() -> None:
    m = _module()
    assert m.migrate({"params": {"wallpaper": "stars"}, "v": 0}, 0) == {
        "params": {"wallpaper": "stars"},
        "v": 0,
        "house": m.DEFAULT_HOUSE,
    }


@pytest.mark.requires_temporal
async def test_a_house_is_one_output_with_a_bom_and_a_guide() -> None:
    world = FakeWorld(
        (TEMPLATE / "pipeline" / "pipeline.py").read_text(),
        activities_py=TEMPLATE / "pipeline" / "activities.py",
    )
    job = a_job(
        params={"wallpaper": "stripes"},
        house={"cols": 2, "rows": 1, "storeys": 1, "windows": 2},
        v=1,
    )
    async with temporal_client() as client:
        await run_job(world, job, client=client)
    assert world.final().state == "done", world.final().failure
    assert len({p.piece_key for p in world.pieces}) == 12
    [out] = world.outputs
    assert sum(b.count for b in out.bom) == 27
    assert "assembly.svg" in out.files
    assert sum(len(p.items) for p in out.layout.plates) == 27


@pytest.mark.requires_temporal
async def test_changing_wallpaper_rerenders_walls_but_not_floors() -> None:
    source = (TEMPLATE / "pipeline" / "pipeline.py").read_text()
    keys: dict[str, dict[str, str]] = {}
    for wallpaper in ("stripes", "stars"):
        world = FakeWorld(source, activities_py=TEMPLATE / "pipeline" / "activities.py")
        job = a_job(
            params={"wallpaper": wallpaper},
            house={"cols": 1, "rows": 1, "storeys": 1, "windows": 1},
            v=1,
        )
        # One revision for both: a job without one renders pieces of its own (#642).
        job.model_version = "abc1234"
        async with temporal_client() as client:
            await run_job(world, job, client=client)
        keys[wallpaper] = {
            f"{p.params['piece']}"
            + (f":{p.params['course']}" if "course" in p.params else ""): p.piece_key
            for p in world.pieces
        }
    before, after = keys["stripes"], keys["stars"]
    assert before["wall:lower"] != after["wall:lower"]
    assert before["floor_tile"] == after["floor_tile"]
    assert before["roof_panel"] == after["roof_panel"]
