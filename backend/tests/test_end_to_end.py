from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest
import trimesh

from scadbuddy.core.config import Config, load_config
from scadbuddy.core.paths import DataPaths
from scadbuddy.render import jobs
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.jobs import Job, JobResult, RenderQueue
from tests.conftest import FIXTURES, installed_font_families

SLUG = "name_keychain"
KEYCHAIN_MODEL = Path(__file__).resolve().parents[2] / "models" / "name-keychain" / "model.scad"
pytestmark = pytest.mark.requires_openscad


async def _render(paths: DataPaths, slug: str, params: dict[str, object]) -> tuple[Job, JobResult]:
    queue = RenderQueue(Config(openscad=load_config().openscad, data_dir=paths.root), paths)
    await queue.start()
    try:
        job = await queue.submit(slug, params)  # type: ignore[arg-type]
        await queue.join()
    finally:
        await queue.aclose()
    done = queue.store.read(job.id)
    assert done.state == "done", done.error
    assert done.result is not None
    return done, done.result


@pytest.fixture
def data(tmp_path: Path) -> DataPaths:
    paths = DataPaths(tmp_path)
    paths.ensure()
    paths.model_dir(SLUG).mkdir(parents=True)
    shutil.copy(FIXTURES / f"{SLUG}.scad", paths.model_source(SLUG))
    return paths


async def test_render_pipeline_produces_a_two_colour_bambu_3mf(data: DataPaths) -> None:
    _, result = await _render(
        data, SLUG, {"name": "Reagan", "base_colour": "#ff6ac1", "text_colour": "#1f6feb"}
    )

    assert [part.extruder for part in result.parts] == [1, 2]
    assert result.colors == ["#FF6AC1", "#1F6FEB"]
    assert [part.name for part in result.parts] == ["Color 1", "Color 2"]
    assert result.warnings == []

    # Each part comes from its own solid render, so each one closes on its own.
    assert [part.watertight for part in result.parts] == [True, True]

    assert result.bbox_mm.size[0] == pytest.approx(60.0)
    assert result.bbox_mm.size[2] == pytest.approx(6.0)

    schema = json.loads(data.model_meta(SLUG).read_text(encoding="utf-8"))["schema"]
    assert [p["name"] for p in schema["parameters"]][:3] == ["name", "text_colour", "text_font"]

    scene = trimesh.load(data.root / result.model_3mf, file_type="3mf")
    assert isinstance(scene, trimesh.Scene)
    assert len(scene.geometry) == 2
    assert all(mesh.is_watertight for mesh in scene.geometry.values())

    preview = trimesh.load(data.root / result.preview_glb, file_type="glb")
    assert isinstance(preview, trimesh.Scene)
    assert sorted(preview.geometry) == ["Color 1", "Color 2"]


async def test_the_model_directory_is_left_as_it_was(data: DataPaths) -> None:
    await _render(data, SLUG, {"name": "Reagan"})
    assert sorted(p.name for p in data.model_dir(SLUG).iterdir()) == ["model.json", "model.scad"]


@pytest.mark.skipif(not KEYCHAIN_MODEL.is_file(), reason="models/name-keychain is not present")
async def test_the_shipped_keychain_measures_as_the_spec_says(tmp_path: Path) -> None:
    if "lobster two" not in installed_font_families():
        pytest.skip("Lobster Two is not installed; the model would silently fall back to DejaVu")

    paths = DataPaths(tmp_path)
    paths.ensure()
    paths.model_dir("name-keychain").mkdir(parents=True)
    shutil.copy(KEYCHAIN_MODEL, paths.model_source("name-keychain"))

    _, result = await _render(paths, "name-keychain", {"name": "Reagan"})

    assert result.colors == ["#0047BB", "#FF1493"]
    assert [part.extruder for part in result.parts] == [1, 2]
    assert [part.watertight for part in result.parts] == [True, True]
    assert result.warnings == []
    assert result.bbox_mm.size[0] == pytest.approx(95.576, abs=0.1)
    assert result.bbox_mm.size[1] == pytest.approx(34.776, abs=0.1)
    assert result.bbox_mm.size[2] == pytest.approx(6.8, abs=0.1)


async def test_the_3mf_is_written_off_the_event_loop(
    data: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing the 3MF now rasterises four cover images, which is seconds of
    numpy rather than the milliseconds the XML and zip used to cost. On the
    event loop that would stall every other job's poll and `/healthz` — one loop
    serves the whole process, and §5.3's debounced preview means a slider drag
    submits these back to back."""
    wrote_on: list[str] = []

    def record(*args: object, **kwargs: object) -> None:
        wrote_on.append(threading.current_thread().name)
        write_bambu_3mf(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(jobs, "write_bambu_3mf", record)
    await _render(data, SLUG, {"name": "Ada"})

    assert wrote_on and threading.main_thread().name not in wrote_on
