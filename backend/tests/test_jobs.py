from __future__ import annotations

import asyncio
import json
import threading
import zipfile
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
import pytest_asyncio
import trimesh

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.render import jobs
from scadbuddy.render.bambu3mf import plates_of
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.jobs import (
    THUMBNAIL_FAILED_WARNING,
    THUMBNAIL_TIMEOUT_WARNING,
    UNCOLOURED_WARNING,
    Job,
    JobResult,
    JobStore,
    PartInfo,
    RenderQueue,
    extruder_order,
    plate_thumbnails,
    solid_parts,
    unreadable_colour_warnings,
)
from scadbuddy.render.runner import OpenSCADError
from scadbuddy.render.schema import CustomizerSchema, Parameter, ParamValue
from scadbuddy.render.solids import STAGED_ASSET_PREFIX, SolidRender
from scadbuddy.render.split import ColourPart
from tests.conftest import write_openscad_3mf

CONFIG = Config(data_dir=Path("/unused"), render_concurrency=2, job_ttl=3600.0)


def _result() -> JobResult:
    return JobResult(
        model_3mf="jobs/x.work/model.3mf",
        preview_glb="jobs/x.work/preview.glb",
        source_version="sha256:test",
        parts=[PartInfo(name="Color 1", colour="#FF6AC1", extruder=1, watertight=True)],
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )


@pytest.fixture
def paths(tmp_path: Path) -> DataPaths:
    data = DataPaths(tmp_path)
    data.ensure()
    return data


def _job(job_id: str, **kwargs: object) -> Job:
    return Job(id=job_id, slug="demo", created_at=datetime.now(UTC), **kwargs)  # type: ignore[arg-type]


def test_store_round_trip(paths: DataPaths) -> None:
    store = JobStore(paths)
    job = _job("a")
    store.write(job)
    assert store.read("a") == job
    assert [j.id for j in store.list_jobs()] == ["a"]


def test_fail_unfinished_marks_pending_and_running_jobs(paths: DataPaths) -> None:
    store = JobStore(paths)
    store.write(_job("pending"))
    store.write(_job("running", state="running"))
    store.write(_job("done", state="done"))

    assert sorted(j.id for j in store.fail_unfinished()) == ["pending", "running"]
    assert store.read("pending").state == "failed"
    assert store.read("pending").error == "interrupted by a restart"
    assert store.read("done").state == "done"


def test_prune_removes_expired_jobs_and_their_work_dirs(paths: DataPaths) -> None:
    store = JobStore(paths)
    old = _job("old", state="done", finished_at=datetime.now(UTC) - timedelta(hours=3))
    store.write(old)
    fresh = _job("fresh", state="done", finished_at=datetime.now(UTC))
    store.write(fresh)
    work = paths.job_work_dir("old")
    work.mkdir()
    (work / "model.3mf").write_bytes(b"x")

    assert store.prune(3600.0) == ["old"]
    assert not work.exists()
    assert [j.id for j in store.list_jobs()] == ["fresh"]


@pytest_asyncio.fixture
async def queue(paths: DataPaths) -> AsyncIterator[RenderQueue]:
    queue = RenderQueue(CONFIG, paths, render=lambda job: _fake_render(job))
    await queue.start()
    yield queue
    await queue.aclose()


async def _fake_render(job: Job) -> tuple[JobResult, list[str]]:
    if job.params.get("mode") == "boom":
        raise OpenSCADError("openscad exited with 1", ["ERROR: something"], 1)
    if job.params.get("mode") == "bug":
        raise KeyError("unexpected")
    return _result(), ["Total rendering time: 0:00:00.065"]


async def test_a_successful_job_records_its_result(queue: RenderQueue) -> None:
    job = await queue.submit("demo", {"name": "Reagan"})
    assert queue.store.read(job.id).state == "pending"
    await queue.join()

    done = queue.store.read(job.id)
    assert done.state == "done"
    assert done.result == _result()
    assert done.log_tail == ["Total rendering time: 0:00:00.065"]
    assert done.started_at is not None and done.finished_at is not None


async def test_an_openscad_failure_keeps_the_log_tail(queue: RenderQueue) -> None:
    job = await queue.submit("demo", {"mode": "boom"})
    await queue.join()

    failed = queue.store.read(job.id)
    assert failed.state == "failed"
    assert failed.error == "openscad exited with 1"
    assert failed.log_tail == ["ERROR: something"]
    assert failed.result is None


async def test_an_unexpected_error_fails_the_job_and_the_worker_lives_on(
    queue: RenderQueue,
) -> None:
    broken = await queue.submit("demo", {"mode": "bug"})
    good = await queue.submit("demo", {})
    await queue.join()

    assert queue.store.read(broken.id).state == "failed"
    assert queue.store.read(broken.id).error == "KeyError: 'unexpected'"
    assert queue.store.read(good.id).state == "done"


async def test_concurrency_is_capped_by_the_config(paths: DataPaths) -> None:
    in_flight = 0
    peak = 0
    release = asyncio.Event()

    async def slow(job: Job) -> tuple[JobResult, list[str]]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await release.wait()
        in_flight -= 1
        return _result(), []

    queue = RenderQueue(replace(CONFIG, render_concurrency=2), paths, render=slow)
    await queue.start()
    try:
        # Distinct parameters: identical waiting renders would coalesce into one job.
        for n in range(4):
            await queue.submit("demo", {"n": n})
        await asyncio.sleep(0.05)
        assert peak == 2
        release.set()
        await queue.join()
    finally:
        await queue.aclose()
    assert peak == 2


async def test_start_fails_jobs_left_behind_by_a_restart(paths: DataPaths) -> None:
    JobStore(paths).write(_job("stale", state="running"))
    queue = RenderQueue(CONFIG, paths, render=_fake_render)
    await queue.start()
    try:
        assert queue.store.read("stale").state == "failed"
    finally:
        await queue.aclose()


async def test_uncoloured_geometry_falls_back_to_the_split_parts() -> None:
    preview = [
        ColourPart(0, "Default", "#F9D72C", trimesh.creation.box()),
        ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box()),
    ]
    parts, warnings = await solid_parts(
        Path("/nonexistent/model.scad"),
        CustomizerSchema(),
        {},
        preview,
        Path("/nonexistent"),
        config=Config(openscad="/nonexistent/openscad"),
    )
    assert parts == preview
    assert warnings == [UNCOLOURED_WARNING]


async def test_the_plate_thumbnail_is_rendered_off_the_event_loop() -> None:
    """Seconds of numpy on the one loop that also serves every job poll and
    `/healthz` — and §5.3's debounce submits these back to back."""
    parts = [ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box())]
    ran_on: list[str] = []

    def record(_: object) -> object:
        ran_on.append(threading.current_thread().name)
        return object()

    with mock.patch.object(jobs, "render_plate_thumbnails", record):
        await plate_thumbnails(parts, config=replace(CONFIG, render_timeout=30.0))

    assert ran_on and threading.main_thread().name not in ran_on


async def test_the_model_hash_is_taken_off_the_event_loop(paths: DataPaths) -> None:
    """It reads every file under the model directory, and §5.3's debounce fires one
    render per keystroke — on the loop that is the whole server, not one job."""
    paths.model_dir("demo").mkdir(parents=True, exist_ok=True)
    paths.model_source("demo").write_text("cube(10);\n", encoding="utf-8")
    ran_on: list[str] = []

    def record(directory: Path) -> str:
        ran_on.append(threading.current_thread().name)
        raise OpenSCADError("far enough", [])

    with mock.patch.object(jobs, "source_version", record), pytest.raises(OpenSCADError):
        await jobs.render_job(_job("h"), config=CONFIG, paths=paths)

    assert ran_on and threading.main_thread().name not in ran_on


async def test_a_thumbnail_that_blows_its_budget_costs_the_cover_not_the_job() -> None:
    """§6.1 promises a bounded job, and `SCADBUDDY_RENDER_TIMEOUT` used to deliver
    that by killing an `openscad` child. The rasteriser has no child to kill, so
    it gets the same budget — and on blowing it the 3MF is written WITHOUT cover
    images rather than not written at all."""
    parts = [ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box())]
    # `wait_for` abandons the worker thread rather than cancelling it, and the
    # loop joins the executor on shutdown — so the test has to release it, or it
    # pays the stall it is asserting does not reach the caller.
    released = threading.Event()

    def blocked(_: object) -> object:
        assert released.wait(timeout=30), "the test never released the thread"
        return object()

    with mock.patch.object(jobs, "render_plate_thumbnails", blocked):
        try:
            rendered, warnings = await plate_thumbnails(
                parts, config=replace(CONFIG, render_timeout=0.05)
            )
        finally:
            released.set()

    assert rendered is None
    assert warnings == [THUMBNAIL_TIMEOUT_WARNING]


async def test_a_thumbnail_that_raises_costs_the_cover_not_the_job() -> None:
    """A rasteriser bug is as non-critical as a slow rasteriser (#116)."""
    parts = [ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box())]

    def broken(_: object) -> object:
        raise ValueError("degenerate face")

    with mock.patch.object(jobs, "render_plate_thumbnails", broken):
        rendered, warnings = await plate_thumbnails(parts, config=CONFIG)

    assert rendered is None
    assert warnings == [THUMBNAIL_FAILED_WARNING]


async def test_the_queue_gives_the_rasteriser_its_own_threads(paths: DataPaths) -> None:
    """An abandoned cover thread must not hold a slot the 3MF writer needs (#116)."""
    parts = [ColourPart(1, "Color 1", "#FF6AC1", trimesh.creation.box())]
    ran_on: list[str] = []

    def record(_: object) -> None:
        ran_on.append(threading.current_thread().name)

    queue = RenderQueue(CONFIG, paths)
    try:
        with mock.patch.object(jobs, "render_plate_thumbnails", record):
            await plate_thumbnails(parts, config=CONFIG, executor=queue._thumbnails)
    finally:
        await queue.aclose()

    assert ran_on and ran_on[0].startswith("thumbnail")
    with pytest.raises(RuntimeError):
        queue._thumbnails.submit(lambda: None)


def test_a_job_written_before_the_source_hash_existed_still_loads(paths: DataPaths) -> None:
    """Job files outlive a deploy on the PVC, and the queue reads every one at startup —
    a field the old writer never wrote must not turn an upgrade into a crash loop."""
    store = JobStore(paths)
    job = _job("old", state="done", result=_result())
    store.write(job)
    raw = json.loads(paths.job_file("old").read_text(encoding="utf-8"))
    del raw["result"]["source_version"]
    paths.job_file("old").write_text(json.dumps(raw), encoding="utf-8")

    loaded = store.read("old")
    assert loaded.result is not None
    assert loaded.result.source_version == ""


def _colour_schema(*colours: tuple[str, str]) -> CustomizerSchema:
    return CustomizerSchema(
        parameters=[
            Parameter(name="name", type="string", initial="Reagan"),
            *(Parameter(name=name, type="color", initial=initial) for name, initial in colours),
        ]
    )


def _part(index: int, colour: str) -> ColourPart:
    name = "Default" if index == 0 else f"Color {index}"
    return ColourPart(index, name, colour, trimesh.creation.box())


def _order(parts: list[ColourPart]) -> list[tuple[int, str]]:
    return [(part.material_index, part.colour) for part in parts]


def test_extruders_follow_colour_parameter_order_not_first_use() -> None:
    """OpenSCAD numbers materials in the order the geometry first uses them (measured
    on 2026.09.23), so a model that colours its letters first makes them material 1."""
    schema = _colour_schema(("base_color", "#0047BB"), ("text_color", "#FF1493"))
    split = [_part(1, "#FF1493"), _part(2, "#0047BB")]

    assert _order(extruder_order(split, schema, {})) == [(2, "#0047BB"), (1, "#FF1493")]


def test_extruder_order_matches_the_rendered_values() -> None:
    """The job's value wins over the default, and a value matches the way OpenSCAD
    resolves it: any case, `#RGB` shorthand, an alpha channel, a CSS name."""
    schema = _colour_schema(("a", "#000000"), ("b", "#111111"), ("c", "#222222"), ("d", "red"))
    split = [
        _part(1, "#FF0000"),
        _part(2, "#AABBCC"),
        _part(3, "#1F6FEB"),
        _part(4, "#0000FF"),
    ]
    params: dict[str, ParamValue] = {"a": "#1f6feb80", "b": "Blue", "c": "#abc"}

    assert _order(extruder_order(split, schema, params)) == [
        (3, "#1F6FEB"),
        (4, "#0000FF"),
        (2, "#AABBCC"),
        (1, "#FF0000"),
    ]


def test_a_malformed_colour_matches_no_part() -> None:
    """#187: a 5-digit hex is not truncated or padded into some other colour's match."""
    schema = _colour_schema(("a", "#FF000"), ("b", "#0047BB"))
    split = [_part(1, "#FF0000"), _part(2, "#0047BB")]

    assert _order(extruder_order(split, schema, {})) == [(2, "#0047BB"), (1, "#FF0000")]


def test_an_unreadable_colour_is_a_job_warning() -> None:
    """#187: a value no part can match says so instead of silently getting no extruder."""
    schema = _colour_schema(("a", "#FF000"), ("b", "red"), ("c", "#0047BB"))

    assert unreadable_colour_warnings(schema, {"c": "#GGGGGG"}) == [
        "colour parameter 'a' is '#FF000', not a colour; it gets no extruder",
        "colour parameter 'c' is '#GGGGGG', not a colour; it gets no extruder",
    ]


def test_parameters_sharing_a_colour_share_the_first_ones_extruder() -> None:
    """One colour is one part, so it takes the first parameter's place and the numbers
    stay dense: an extruder is a filament slot, and a gap would ask for a filament no
    part uses."""
    schema = _colour_schema(("a", "#FF1493"), ("b", "#FF1493"), ("c", "#0047BB"))
    split = [_part(1, "#0047BB"), _part(2, "#FF1493")]

    assert _order(extruder_order(split, schema, {})) == [(2, "#FF1493"), (1, "#0047BB")]


def test_colours_no_parameter_names_come_last_in_material_order() -> None:
    """Hard-coded colours, colours computed from a parameter, and the uncoloured
    Default -- which never claims a parameter, even one set to its yellow. A parameter
    the geometry never uses gets no extruder."""
    schema = _colour_schema(("unused", "#123456"), ("base", "#0047BB"), ("x", "#F9D72C"))
    split = [
        _part(0, "#F9D72C"),
        _part(1, "#FFFFFF"),
        _part(2, "#0047BB"),
        _part(3, "#000000"),
    ]

    assert _order(extruder_order(split, schema, {})) == [
        (2, "#0047BB"),
        (0, "#F9D72C"),
        (1, "#FFFFFF"),
        (3, "#000000"),
    ]


async def test_the_3mf_the_preview_and_the_result_number_extruders_alike(
    paths: DataPaths,
) -> None:
    schema = _colour_schema(("base_color", "#0047BB"), ("text_color", "#FF1493"))
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")

    async def text_first(*args: object, **kwargs: object) -> object:
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(
            out,
            [
                ("Color 1", "#FF149300", trimesh.creation.box(extents=(2, 2, 2))),
                ("Color 2", "#0047BB00", trimesh.creation.box(extents=(10, 10, 1))),
            ],
        )
        return mock.Mock(log_tail=[], missing_files=(), notes=(), plates=None)

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return schema

    async def render_solids(*args: object, **kwargs: object) -> SolidRender:
        return SolidRender()

    with (
        mock.patch.object(jobs, "render_3mf", text_first),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        mock.patch.object(jobs, "render_solids", render_solids),
    ):
        result, _ = await jobs.render_job(_job("j"), config=CONFIG, paths=paths)

    assert [(p.extruder, p.colour) for p in result.parts] == [(1, "#0047BB"), (2, "#FF1493")]
    assert result.colors == ["#0047BB", "#FF1493"]

    with zipfile.ZipFile(paths.root / result.model_3mf) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        parts = archive.read("Metadata/model_settings.config").decode()
    assert settings["filament_colour"] == ["#0047BB", "#FF1493"]
    assert parts.index('"Color 2"') < parts.index('"Color 1"')

    preview = trimesh.load(paths.root / result.preview_glb, file_type="glb")
    assert isinstance(preview, trimesh.Scene)
    assert list(preview.geometry) == ["Color 2", "Color 1"]


async def test_a_built_ins_3mf_is_titled_by_its_bare_slug(paths: DataPaths) -> None:
    """The `builtin:` of the model id is not a name to show in the slicer."""
    paths.model_dir("builtin:demo").mkdir(parents=True)
    paths.model_source("builtin:demo").write_text("// stand-in\n", encoding="utf-8")

    async def one_box(*args: object, **kwargs: object) -> object:
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(out, [("Color 1", "#0047BB00", trimesh.creation.box())])
        return mock.Mock(log_tail=[], missing_files=(), notes=(), plates=None)

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return _colour_schema(("base_color", "#0047BB"))

    async def render_solids(*args: object, **kwargs: object) -> SolidRender:
        return SolidRender()

    job = _job("b").model_copy(update={"slug": "builtin:demo"})
    with (
        mock.patch.object(jobs, "render_3mf", one_box),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        mock.patch.object(jobs, "render_solids", render_solids),
    ):
        result, _ = await jobs.render_job(job, config=CONFIG, paths=paths)

    with zipfile.ZipFile(paths.root / result.model_3mf) as archive:
        root = archive.read("3D/3dmodel.model").decode()
        settings = archive.read("Metadata/model_settings.config").decode()
    assert '<metadata name="Title">demo</metadata>' in root
    assert 'name="demo"' in root
    assert '<metadata key="name" value="demo"/>' in settings
    assert "builtin:" not in root and "builtin:" not in settings


# ── #204: uploaded files staged for the render and every wrapper render ───────

OVERLAY_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><path d="M0 0H4V4Z"/></svg>'
)


def _file_schema() -> CustomizerSchema:
    return CustomizerSchema(
        parameters=[
            Parameter(name="overlay", type="file", initial="", accept=["svg", "png"]),
            Parameter(name="base_color", type="color", initial="#0047BB"),
        ]
    )


async def test_an_uploaded_file_is_staged_beside_the_model_for_every_render(
    paths: DataPaths,
) -> None:
    model_dir = paths.model_dir("demo")
    model_dir.mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")
    asset = AssetStore(paths.assets).put(OVERLAY_SVG, "heart.svg")
    seen: dict[str, tuple[str, bytes]] = {}

    def staged(label: str, params: object) -> None:
        assert isinstance(params, dict)
        name = params["overlay"]
        assert isinstance(name, str)
        # A bare generated name, in the model's own directory, with the upload's bytes.
        assert "/" not in name and name.startswith(STAGED_ASSET_PREFIX) and name.endswith(".svg")
        seen[label] = (name, (model_dir / name).read_bytes())

    async def render(*args: object, **kwargs: object) -> object:
        staged("main", args[2])
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(out, [("Color 1", "#0047BB00", trimesh.creation.box())])
        return mock.Mock(log_tail=[], missing_files=(), notes=(), plates=None)

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return _file_schema()

    async def render_solids(*args: object, **kwargs: object) -> SolidRender:
        # §6.3's wrapper renders run in the same directory, with the same values.
        scad = args[0]
        assert isinstance(scad, Path) and scad.parent == model_dir
        staged("solids", args[2])
        return SolidRender()

    job = _job("s", params={"overlay": asset.id, "base_color": "#0047BB"})
    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        mock.patch.object(jobs, "render_solids", render_solids),
    ):
        result, _ = await jobs.render_job(job, config=CONFIG, paths=paths)

    stored = AssetStore(paths.assets).blob_path(asset).read_bytes()
    assert seen["main"] == seen["solids"]
    assert seen["main"][1] == stored
    # Gone once the render is: the model directory is the versioned one.
    assert not any(p.name.startswith(STAGED_ASSET_PREFIX) for p in model_dir.iterdir())
    # The job keeps the asset id, which is what params.json and provenance record.
    assert job.params["overlay"] == asset.id
    assert result.warnings == []


async def test_staging_is_undone_when_the_render_fails(paths: DataPaths) -> None:
    model_dir = paths.model_dir("demo")
    model_dir.mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")
    asset = AssetStore(paths.assets).put(OVERLAY_SVG, "heart.svg")

    async def render(*args: object, **kwargs: object) -> object:
        raise OpenSCADError("openscad exited with 1", [])

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return _file_schema()

    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        pytest.raises(OpenSCADError),
    ):
        await jobs.render_job(_job("f", params={"overlay": asset.id}), config=CONFIG, paths=paths)

    assert [p.name for p in model_dir.iterdir()] == ["model.scad"]


async def test_a_file_openscad_could_not_open_is_a_job_warning(paths: DataPaths) -> None:
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")

    async def render(*args: object, **kwargs: object) -> object:
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(out, [("Color 1", "#0047BB00", trimesh.creation.box())])
        return mock.Mock(log_tail=[], missing_files=("pic.svg",), notes=(), plates=None)

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return _file_schema()

    async def render_solids(*args: object, **kwargs: object) -> SolidRender:
        return SolidRender()

    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        mock.patch.object(jobs, "render_solids", render_solids),
    ):
        result, _ = await jobs.render_job(_job("m"), config=CONFIG, paths=paths)

    assert result.warnings == ["OpenSCAD could not open pic.svg; the model rendered without it"]


async def test_the_notes_a_template_echoed_are_on_the_result(paths: DataPaths) -> None:
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")

    async def render(*args: object, **kwargs: object) -> object:
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(out, [("Color 1", "#0047BB00", trimesh.creation.box())])
        return mock.Mock(log_tail=[], missing_files=(), notes=("letter_size reduced",), plates=None)

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return _file_schema()

    async def render_solids(*args: object, **kwargs: object) -> SolidRender:
        return SolidRender()

    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        mock.patch.object(jobs, "render_solids", render_solids),
    ):
        result, _ = await jobs.render_job(_job("m"), config=CONFIG, paths=paths)

    assert result.notes == ["letter_size reduced"]


# ── #289: a template that asks for more than one plate ────────────────────────

TRAY = ("Color 1", "#0047BB00", trimesh.creation.box(extents=(10, 10, 2)))
WALL = ("Color 2", "#FF149300", trimesh.creation.box(extents=(2, 2, 6)))
LID = ("Color 3", "#FFFFFF00", trimesh.creation.box(extents=(12, 12, 1)))


def _plate_of(extra_defines: object) -> int:
    """The `$plate` a render was asked for, 0 when none."""
    defines = list(extra_defines) if isinstance(extra_defines, list | tuple) else []
    for define in defines:
        if isinstance(define, str) and define.startswith("$plate="):
            return int(define.removeprefix("$plate="))
    return 0


def _plated_render(
    drawn: dict[int, list[tuple[str, str, trimesh.Trimesh]]], plates: int | None
) -> tuple[object, list[int]]:
    """A stand-in `render_3mf` drawing ``drawn[$plate]``, and the plates it was asked for."""
    asked: list[int] = []

    async def render(*args: object, **kwargs: object) -> object:
        out = args[3]
        assert isinstance(out, Path)
        plate = _plate_of(kwargs.get("extra_defines"))
        asked.append(plate)
        write_openscad_3mf(out, drawn[plate])
        return mock.Mock(log_tail=[], missing_files=(), notes=(), plates=plates)

    return render, asked


async def _render_plated(
    paths: DataPaths,
    drawn: dict[int, list[tuple[str, str, trimesh.Trimesh]]],
    plates: int | None,
    solids: object | None = None,
) -> tuple[JobResult, list[int]]:
    paths.model_dir("demo").mkdir(parents=True, exist_ok=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")
    render, asked = _plated_render(drawn, plates)
    schema = _colour_schema(
        ("floor_color", "#0047BB"), ("wall_color", "#FF1493"), ("lid_color", "#FFFFFF")
    )

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return schema

    async def no_solids(*args: object, **kwargs: object) -> SolidRender:
        return SolidRender()

    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        mock.patch.object(jobs, "render_solids", solids or no_solids),
    ):
        result, _ = await jobs.render_job(_job("p"), config=CONFIG, paths=paths)
    return result, asked


async def test_each_plate_is_its_own_render_and_the_3mf_holds_them_all(
    paths: DataPaths,
) -> None:
    solid_defines: list[object] = []

    async def solids(*args: object, **kwargs: object) -> SolidRender:
        solid_defines.append(kwargs.get("extra_defines"))
        return SolidRender()

    result, asked = await _render_plated(
        paths, {0: [TRAY, WALL, LID], 1: [TRAY, WALL], 2: [LID]}, plates=2, solids=solids
    )

    # The everything render, then one per plate; the wrapper renders get `$plate` too.
    assert asked == [0, 1, 2]
    assert [_plate_of(defines) for defines in solid_defines] == [1, 2]
    assert result.colors == ["#0047BB", "#FF1493", "#FFFFFF"]
    assert [(p.extruder, p.colour) for p in result.parts] == [
        (1, "#0047BB"),
        (2, "#FF1493"),
        (3, "#FFFFFF"),
    ]
    assert [(p.index, p.colors) for p in result.plates] == [
        (1, ["#0047BB", "#FF1493"]),
        (2, ["#FFFFFF"]),
    ]
    assert result.plates[1].bbox_mm.size == pytest.approx((12.0, 12.0, 1.0))
    assert result.warnings == []

    model = paths.root / result.model_3mf
    assert [plate.index for plate in plates_of(model)] == [1, 2]
    with zipfile.ZipFile(model) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        config = archive.read("Metadata/model_settings.config").decode()
    assert settings["filament_colour"] == ["#0047BB", "#FF1493", "#FFFFFF"]
    # The lid is extruder 3 on plate 2, not extruder 1 of a plate of its own.
    assert 'value="Color 3"/>\n   <metadata key="extruder" value="3"/>' in config


async def test_one_plate_or_no_count_is_the_ordinary_render(paths: DataPaths) -> None:
    for plates in (None, 1, 0):
        result, asked = await _render_plated(paths, {0: [TRAY, WALL]}, plates=plates)
        assert asked == [0]
        assert result.plates == []
        assert [plate.index for plate in plates_of(paths.root / result.model_3mf)] == [1]


async def test_a_colour_only_a_plate_draws_is_appended_with_a_warning(paths: DataPaths) -> None:
    result, _ = await _render_plated(paths, {0: [TRAY, WALL], 1: [TRAY, WALL], 2: [LID]}, plates=2)
    assert result.colors == ["#0047BB", "#FF1493", "#FFFFFF"]
    assert result.warnings == [
        "plate 2: #FFFFFF is not in the all-plates render; it gets extruder 3"
    ]


async def test_a_colour_no_plate_draws_gives_up_its_extruder(paths: DataPaths) -> None:
    result, _ = await _render_plated(paths, {0: [TRAY, WALL, LID], 1: [TRAY], 2: [LID]}, plates=2)
    assert result.colors == ["#0047BB", "#FFFFFF"]
    assert [(p.index, p.colors) for p in result.plates] == [(1, ["#0047BB"]), (2, ["#FFFFFF"])]
    assert result.warnings == ["#FF1493 is drawn only with every plate at once; it is on no plate"]


async def test_too_many_plates_fails_the_job(paths: DataPaths) -> None:
    with pytest.raises(OpenSCADError, match="asks for 17 plates"):
        await _render_plated(paths, {0: [TRAY]}, plates=jobs.MAX_PLATES + 1)


async def test_an_empty_plate_fails_the_job_naming_it(paths: DataPaths) -> None:
    with pytest.raises(OpenSCADError, match="plate 2 of 2 rendered no geometry"):
        await _render_plated(paths, {0: [TRAY, LID], 1: [TRAY], 2: []}, plates=2)
