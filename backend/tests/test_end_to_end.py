from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import trimesh

from scadbuddy.core.config import Config, load_config
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.jobs import RenderQueue
from scadbuddy.render.split import split_by_material
from tests.conftest import FIXTURES

SLUG = "name_keychain"
pytestmark = pytest.mark.requires_openscad


@pytest.fixture
def data(tmp_path: Path) -> DataPaths:
    paths = DataPaths(tmp_path)
    paths.ensure()
    paths.model_dir(SLUG).mkdir(parents=True)
    shutil.copy(FIXTURES / f"{SLUG}.scad", paths.model_source(SLUG))
    return paths


async def test_render_pipeline_produces_a_two_colour_bambu_3mf(data: DataPaths) -> None:
    config = Config(openscad=load_config().openscad, data_dir=data.root)
    queue = RenderQueue(config, data)
    await queue.start()
    try:
        job = await queue.submit(
            SLUG, {"name": "Reagan", "base_colour": "#ff6ac1", "text_colour": "#1f6feb"}
        )
        await queue.join()
    finally:
        await queue.aclose()

    done = queue.store.read(job.id)
    assert done.state == "done", done.error
    result = done.result
    assert result is not None

    assert [part.extruder for part in result.parts] == [1, 2]
    assert [part.colour for part in result.parts] == ["#FF6AC1", "#1F6FEB"]
    assert [part.name for part in result.parts] == ["Color 1", "Color 2"]

    # OpenSCAD unions the two colour solids and drops the faces where they touch, so
    # neither part closes on its own; the assembly does.
    assert [part.watertight for part in result.parts] == [False, False]
    raw = split_by_material(data.job_work_dir(job.id) / "render.3mf")
    assembly = trimesh.Trimesh()
    for part in raw:
        assembly += part.mesh
    assembly.merge_vertices()
    assert assembly.is_watertight

    assert result.bounding_box.size[0] == pytest.approx(60.0)
    assert result.bounding_box.size[2] == pytest.approx(6.0)

    schema = json.loads(data.model_meta(SLUG).read_text(encoding="utf-8"))["schema"]
    assert [p["name"] for p in schema["parameters"]][:3] == ["name", "text_colour", "text_font"]

    scene = trimesh.load(data.root / result.model_3mf, file_type="3mf")
    assert isinstance(scene, trimesh.Scene)
    assert len(scene.geometry) == 2
    assert sorted(round(float(m.volume), 1) for m in scene.geometry.values()) == sorted(
        round(float(part.mesh.volume), 1) for part in raw
    )

    preview = trimesh.load(data.root / result.preview_glb, file_type="glb")
    assert isinstance(preview, trimesh.Scene)
    assert sorted(preview.geometry) == ["Color 1", "Color 2"]
