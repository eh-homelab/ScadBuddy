from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scadbuddy.core.config import load_config
from scadbuddy.render.schema import CustomizerSchema
from scadbuddy.render.solids import CSS_COLOURS, render_solids, targets_for, wrapper_source
from tests.conftest import FIXTURES


def test_css_table_is_openscads_own_colour_list() -> None:
    assert len(CSS_COLOURS) == 147
    assert CSS_COLOURS["red"] == "#FF0000"
    assert CSS_COLOURS["gray"] == CSS_COLOURS["grey"] == "#808080"
    assert CSS_COLOURS["hotpink"] == "#FF69B4"
    assert all(value == value.upper() and len(value) == 7 for value in CSS_COLOURS.values())


def test_targets_cover_every_way_a_model_can_write_the_colour() -> None:
    assert targets_for("#ff0000") == ["#FF0000", "name:red"]
    assert targets_for("#808080") == ["#808080", "name:gray", "name:grey"]
    assert targets_for("#123456") == ["#123456"]


def test_wrapper_shadows_color_and_includes_the_model() -> None:
    source = wrapper_source("model.scad")
    assert "module color(c, alpha = 1)" in source
    assert source.endswith("include <model.scad>\n")


@pytest.mark.requires_openscad
async def test_named_and_vector_colours_each_render_as_a_closed_solid(tmp_path: Path) -> None:
    model = tmp_path / "named_colours.scad"
    shutil.copy(FIXTURES / "named_colours.scad", model)
    work = tmp_path / "work"
    work.mkdir()

    solids = await render_solids(
        model, CustomizerSchema(), {}, ["#FF0000", "#0080FF"], work, config=load_config()
    )

    assert solids.warnings == []
    assert sorted(solids.meshes) == ["#0080FF", "#FF0000"]
    assert all(mesh.is_watertight for mesh in solids.meshes.values())
    assert round(float(solids.meshes["#FF0000"].volume), 3) == 1000.0
    assert round(float(solids.meshes["#0080FF"].volume), 3) == 125.0


@pytest.mark.requires_openscad
async def test_a_colour_the_model_never_uses_is_reported_not_raised(tmp_path: Path) -> None:
    model = tmp_path / "named_colours.scad"
    shutil.copy(FIXTURES / "named_colours.scad", model)
    work = tmp_path / "work"
    work.mkdir()

    solids = await render_solids(
        model, CustomizerSchema(), {}, ["#ABCDEF"], work, config=load_config()
    )

    assert solids.meshes == {}
    assert len(solids.warnings) == 1
    assert solids.warnings[0].startswith("#ABCDEF: no closed solid")


@pytest.mark.requires_openscad
async def test_the_wrapper_is_removed_from_the_model_directory(tmp_path: Path) -> None:
    model = tmp_path / "named_colours.scad"
    shutil.copy(FIXTURES / "named_colours.scad", model)
    work = tmp_path / "work"
    work.mkdir()

    await render_solids(model, CustomizerSchema(), {}, ["#FF0000"], work, config=load_config())

    assert sorted(p.name for p in tmp_path.iterdir()) == ["named_colours.scad", "work"]
