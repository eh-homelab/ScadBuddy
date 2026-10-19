from __future__ import annotations

import asyncio
import json
import shutil
import threading
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest import mock

import psycopg
import pytest
import trimesh
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.fonts import GLYPH_CHECK_TIMEOUT
from scadbuddy.library.libraries import CheckoutGate, LibraryNotInstalledError
from scadbuddy.meshmodel.scene import SCENE_NAME
from scadbuddy.render import jobs
from scadbuddy.render import solids as solids_module
from scadbuddy.render import split as split_module
from scadbuddy.render.bambu3mf import plates_of, single_plate
from scadbuddy.render.diagnostics import Diagnostic
from scadbuddy.render.glb import BoundingBox, write_glb
from scadbuddy.render.jobs import (
    LAYOUT_NAME,
    MISSING_FILE_FAILED_WARNING,
    MODEL_NAME,
    PREVIEW_NAME,
    RAW_RENDER_NAME,
    THUMBNAIL_FAILED_WARNING,
    THUMBNAIL_TIMEOUT_WARNING,
    UNCOLOURED_WARNING,
    Job,
    JobResult,
    PartInfo,
    PartSource,
    PlateLayout,
    Prepared,
    extruder_order,
    finish_piece_stage,
    plate_thumbnails,
    prepare_source,
    render_main,
    render_solids_stage,
    solid_parts,
    unreadable_colour_warnings,
)
from scadbuddy.render.objects3mf import ReadObject, read_objects
from scadbuddy.render.read_budget import ReadBudget
from scadbuddy.render.runner import OpenSCADError, ProcessOutput
from scadbuddy.render.schema import CustomizerSchema, Parameter, ParamValue
from scadbuddy.render.solids import STAGED_ASSET_PREFIX, SolidRender, solid_file, solid_mesh
from scadbuddy.render.split import ColourPart, Paint, split_by_material
from tests.conftest import PgPool, write_openscad_3mf
from tests.meshmodel.test_paint import GREEN, ORANGE, painted_box, rendered, written

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
        await jobs.render_job(
            _job("h"), config=CONFIG, paths=paths, assets=AssetStore(paths.assets)
        )

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
        return mock.Mock(
            log_tail=[],
            missing_files=(),
            diagnostics=(),
            diagnostics_dropped=0,
            notes=(),
            plates=None,
            objects=None,
        )

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return schema

    async def render_solids(*args: object, **kwargs: object) -> SolidRender:
        return SolidRender()

    with (
        mock.patch.object(jobs, "render_3mf", text_first),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        mock.patch.object(jobs, "render_solids", render_solids),
    ):
        result, _ = await jobs.render_job(
            _job("j"), config=CONFIG, paths=paths, assets=AssetStore(paths.assets)
        )

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
        return mock.Mock(
            log_tail=[],
            missing_files=(),
            diagnostics=(),
            diagnostics_dropped=0,
            notes=(),
            plates=None,
            objects=None,
        )

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
        result, _ = await jobs.render_job(
            job, config=CONFIG, paths=paths, assets=AssetStore(paths.assets)
        )

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


async def test_staging_looks_the_upload_up_off_the_event_loop(
    paths: DataPaths, pg_pool: PgPool, pg_conninfo: str
) -> None:
    """Marking an upload used is a database round trip (#591) that waits on a row the
    sweep has locked; the render must wait in a thread, not stall the whole loop."""
    model_dir = paths.model_dir("demo")
    model_dir.mkdir(parents=True)
    store = AssetStore(paths.assets, pg_pool)
    asset = store.put(OVERLAY_SVG, "heart.svg")
    held = psycopg.connect(pg_conninfo)  # a transaction: the sweep's re-check, row locked
    held.execute("SELECT 1 FROM assets WHERE id = %s FOR UPDATE", (asset.id,))
    release = threading.Timer(1.0, held.commit)
    release.start()

    async def stage() -> dict[str, ParamValue]:
        async with jobs.staged_assets(
            _file_schema(), {"overlay": asset.id}, model_dir, store
        ) as params:
            return params

    try:
        staging = asyncio.create_task(stage())
        loop = asyncio.get_running_loop()
        gaps: list[float] = []
        last = loop.time()
        while not staging.done():
            await asyncio.sleep(0.05)
            gaps.append(loop.time() - last)
            last = loop.time()
        params = await staging
    finally:
        release.join()
        held.close()
    assert isinstance(params["overlay"], str)
    assert params["overlay"].startswith(STAGED_ASSET_PREFIX)
    # The row lock was held for a second; the loop never stopped for it.
    assert sum(gaps) >= 0.9
    assert max(gaps) < 0.5, f"the event loop stalled for {max(gaps):.2f}s"


async def test_an_uploaded_file_is_staged_beside_the_model_for_every_render(
    paths: DataPaths, pg_pool: PgPool
) -> None:
    model_dir = paths.model_dir("demo")
    model_dir.mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")
    store = AssetStore(paths.assets, pg_pool)
    asset = store.put(OVERLAY_SVG, "heart.svg")
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
        return mock.Mock(
            log_tail=[],
            missing_files=(),
            diagnostics=(),
            diagnostics_dropped=0,
            notes=(),
            plates=None,
            objects=None,
        )

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
        result, _ = await jobs.render_job(job, config=CONFIG, paths=paths, assets=store)

    stored = store.blob_path(asset).read_bytes()
    assert seen["main"] == seen["solids"]
    assert seen["main"][1] == stored
    # Gone once the render is: the model directory is the versioned one.
    assert not any(p.name.startswith(STAGED_ASSET_PREFIX) for p in model_dir.iterdir())
    # The job keeps the asset id, which is what params.json and provenance record.
    assert job.params["overlay"] == asset.id
    assert result.warnings == []


async def test_staging_is_undone_when_the_render_fails(paths: DataPaths, pg_pool: PgPool) -> None:
    model_dir = paths.model_dir("demo")
    model_dir.mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")
    store = AssetStore(paths.assets, pg_pool)
    asset = store.put(OVERLAY_SVG, "heart.svg")

    async def render(*args: object, **kwargs: object) -> object:
        raise OpenSCADError("openscad exited with 1", [])

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return _file_schema()

    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        pytest.raises(OpenSCADError),
    ):
        await jobs.render_job(
            _job("f", params={"overlay": asset.id}),
            config=CONFIG,
            paths=paths,
            assets=store,
        )

    assert [p.name for p in model_dir.iterdir()] == ["model.scad"]


async def test_a_file_openscad_could_not_open_is_a_job_warning(paths: DataPaths) -> None:
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")

    async def render(*args: object, **kwargs: object) -> object:
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(out, [("Color 1", "#0047BB00", trimesh.creation.box())])
        return mock.Mock(
            log_tail=[],
            missing_files=("pic.svg",),
            diagnostics=(),
            diagnostics_dropped=0,
            notes=(),
            plates=None,
            objects=None,
        )

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return _file_schema()

    async def render_solids(*args: object, **kwargs: object) -> SolidRender:
        return SolidRender()

    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        mock.patch.object(jobs, "render_solids", render_solids),
    ):
        result, _ = await jobs.render_job(
            _job("m"), config=CONFIG, paths=paths, assets=AssetStore(paths.assets)
        )

    assert result.warnings == ["OpenSCAD could not open pic.svg; the model rendered without it"]


# ── diagnostics (#252) ───────────────────────────────────────────────────────

WARNING = Diagnostic(severity="warning", message="unknown variable", file="model.scad", line=3)
ERROR = Diagnostic(severity="error", message="Parser error: syntax error", file="model.scad")


async def test_the_main_render_diagnostics_are_the_results(paths: DataPaths) -> None:
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")

    async def render(*args: object, **kwargs: object) -> object:
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(out, [("Color 1", "#0047BB00", trimesh.creation.box())])
        return mock.Mock(
            log_tail=[],
            missing_files=(),
            diagnostics=(WARNING,),
            diagnostics_dropped=0,
            notes=(),
            plates=None,
            objects=None,
        )

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return _file_schema()

    async def render_solids(*args: object, **kwargs: object) -> SolidRender:
        return SolidRender()

    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        mock.patch.object(jobs, "render_solids", render_solids),
    ):
        result, _ = await jobs.render_job(
            _job("d"), config=CONFIG, paths=paths, assets=AssetStore(paths.assets)
        )

    assert result.diagnostics == [WARNING]


# ── library leases (#253, review of #324) ────────────────────────────────────

LIBRARY_COMMIT = "c" * 40


def _model_pinning_a_library(paths: DataPaths) -> Path:
    """``demo`` pinned to a BOSL2 checkout that is on the volume; returns it."""
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")
    pin = {
        "name": "BOSL2",
        "url": "https://example.invalid/BOSL2.git",
        "ref": "v1",
        "commit": LIBRARY_COMMIT,
    }
    paths.model_meta("demo").write_text(json.dumps({"libraries": [pin]}), encoding="utf-8")
    checkout = paths.libraries / "BOSL2" / LIBRARY_COMMIT
    (checkout / "BOSL2").mkdir(parents=True)
    return checkout


async def test_a_render_holds_the_checkouts_it_resolved(paths: DataPaths) -> None:
    checkout = _model_pinning_a_library(paths)
    gate = CheckoutGate()
    seen: list[list[str]] = []

    async def render(*args: object, **kwargs: object) -> object:
        seen.append(gate.leased(checkout))
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(out, [("Color 1", "#0047BB00", trimesh.creation.box())])
        return mock.Mock(
            log_tail=[],
            missing_files=(),
            diagnostics=(),
            diagnostics_dropped=0,
            notes=(),
            plates=None,
            objects=None,
        )

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return _file_schema()

    async def render_solids(*args: object, **kwargs: object) -> SolidRender:
        seen.append(gate.leased(checkout.parent))
        return SolidRender()

    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        mock.patch.object(jobs, "render_solids", render_solids),
    ):
        await jobs.render_job(
            _job("l"), config=CONFIG, paths=paths, assets=AssetStore(paths.assets), checkouts=gate
        )

    assert seen == [["l"], ["l"]]
    assert gate.leased(checkout) == []


async def test_a_render_that_waited_out_a_removal_fails_cleanly(paths: DataPaths) -> None:
    """Resolved before the removal, leased after it: the checkout is gone, and the
    render says so rather than running OpenSCAD against a missing library."""
    checkout = _model_pinning_a_library(paths)
    gate = CheckoutGate()
    schema_reads: list[object] = []

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        schema_reads.append(args)
        return _file_schema()

    with mock.patch.object(jobs, "cached_schema", cached_schema):
        async with gate.removing():
            task = asyncio.create_task(
                jobs.render_job(
                    _job("w"),
                    config=CONFIG,
                    paths=paths,
                    assets=AssetStore(paths.assets),
                    checkouts=gate,
                )
            )
            await asyncio.sleep(0.2)
            shutil.rmtree(checkout)
        with pytest.raises(LibraryNotInstalledError, match="BOSL2"):
            await task

    assert schema_reads == []
    assert gate.leased(checkout) == []


async def test_a_cancelled_render_releases_its_lease(paths: DataPaths) -> None:
    """Shutdown cancels a worker mid-render; the checkout must not stay leased."""
    checkout = _model_pinning_a_library(paths)
    gate = CheckoutGate()
    started = asyncio.Event()

    async def render(*args: object, **kwargs: object) -> object:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return _file_schema()

    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "cached_schema", cached_schema),
    ):
        task = asyncio.create_task(
            jobs.render_job(
                _job("c"),
                config=CONFIG,
                paths=paths,
                assets=AssetStore(paths.assets),
                checkouts=gate,
            )
        )
        await asyncio.wait_for(started.wait(), 5)
        assert gate.leased(checkout) == ["c"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert gate.leased(checkout) == []


async def test_the_notes_a_template_echoed_are_on_the_result(paths: DataPaths) -> None:
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")

    async def render(*args: object, **kwargs: object) -> object:
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(out, [("Color 1", "#0047BB00", trimesh.creation.box())])
        return mock.Mock(
            log_tail=[],
            missing_files=(),
            notes=("letter_size reduced",),
            diagnostics=(),
            diagnostics_dropped=0,
            plates=None,
            objects=None,
        )

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return _file_schema()

    async def render_solids(*args: object, **kwargs: object) -> SolidRender:
        return SolidRender()

    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        mock.patch.object(jobs, "render_solids", render_solids),
    ):
        result, _ = await jobs.render_job(
            _job("m"), config=CONFIG, paths=paths, assets=AssetStore(paths.assets)
        )

    assert result.notes == ["letter_size reduced"]


# --- warnings on a failed render (#408) ---------------------------------------------


async def _failed_render(paths: DataPaths, render_3mf: object, **patches: object) -> OpenSCADError:
    paths.model_dir("demo").mkdir(parents=True, exist_ok=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return _colour_schema(("base_color", "#0047BB"), ("text_color", "#0047BB"))

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(jobs, "render_3mf", render_3mf))
        stack.enter_context(mock.patch.object(jobs, "cached_schema", cached_schema))
        for name, value in patches.items():
            stack.enter_context(mock.patch.object(jobs, name, value))
        raised = stack.enter_context(pytest.raises(OpenSCADError))
        await jobs.render_job(
            _job("j", params={"text_color": "not-a-colour"}),
            config=CONFIG,
            paths=paths,
            assets=AssetStore(paths.assets),
        )
    return raised.value


async def test_a_failed_openscad_run_keeps_its_warnings(paths: DataPaths) -> None:
    """The missing picture that likely made it fail, and the unreadable colour."""

    async def exits_1(*args: object, **kwargs: object) -> object:
        raise OpenSCADError("openscad exited with 1", [], 1, missing_files=("pic.svg",))

    error = await _failed_render(paths, exits_1)

    assert error.warnings == [
        MISSING_FILE_FAILED_WARNING.format(name="pic.svg"),
        *unreadable_colour_warnings(
            _colour_schema(("base_color", "#0047BB"), ("text_color", "#0047BB")),
            {"text_color": "not-a-colour"},
        ),
    ]


async def test_a_render_that_drew_nothing_says_which_file_it_could_not_open(
    paths: DataPaths,
) -> None:
    """A template that draws only the file parameter's picture renders no geometry
    when the picture is missing, and the missing-file warning is the reason why."""

    async def no_geometry(*args: object, **kwargs: object) -> object:
        return mock.Mock(
            log_tail=[], missing_files=("pic.svg",), diagnostics=(), diagnostics_dropped=0, notes=()
        )

    error = await _failed_render(paths, no_geometry, split_by_material=lambda path: [])

    assert str(error) == "the render produced no geometry"
    assert error.warnings[0] == MISSING_FILE_FAILED_WARNING.format(name="pic.svg")


def _plate_two(fails: bool) -> object:
    """A two-plate render whose plate 2 could not open pic.svg: OpenSCAD fails on it
    (``fails``), or it renders and draws nothing (#451)."""

    async def render(*args: object, **kwargs: object) -> object:
        out = args[3]
        assert isinstance(out, Path)
        plate = _plate_of(kwargs.get("extra_defines"))
        missing = ("pic.svg",) if plate == 2 else ()
        if plate == 2 and fails:
            raise OpenSCADError("openscad exited with 1", ["boom"], 1, missing_files=missing)
        write_openscad_3mf(out, [] if plate == 2 else [TRAY])
        return mock.Mock(
            log_tail=[],
            missing_files=missing,
            diagnostics=(),
            diagnostics_dropped=0,
            notes=(),
            plates=2,
            objects=None,
        )

    return render


@pytest.mark.parametrize("fails", [True, False], ids=["openscad-failed", "no-geometry"])
async def test_a_failed_plate_keeps_its_warnings_and_missing_files(
    paths: DataPaths, fails: bool
) -> None:
    async def no_solids(*args: object, **kwargs: object) -> SolidRender:
        return SolidRender()

    error = await _failed_render(paths, _plate_two(fails), render_solids=no_solids)

    assert str(error).startswith("plate 2 of 2")
    assert error.missing_files == ("pic.svg",)
    assert error.warnings == [
        MISSING_FILE_FAILED_WARNING.format(name="pic.svg"),
        *unreadable_colour_warnings(
            _colour_schema(("base_color", "#0047BB"), ("text_color", "#0047BB")),
            {"text_color": "not-a-colour"},
        ),
    ]


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
        return mock.Mock(
            log_tail=[],
            missing_files=(),
            diagnostics=(),
            diagnostics_dropped=0,
            notes=(),
            plates=plates,
            objects=None,
        )

    return render, asked


async def _render_plated(
    paths: DataPaths,
    drawn: dict[int, list[tuple[str, str, trimesh.Trimesh]]],
    plates: int | None,
    solids: object | None = None,
    render: object | None = None,
) -> tuple[JobResult, list[int]]:
    paths.model_dir("demo").mkdir(parents=True, exist_ok=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")
    plated, asked = _plated_render(drawn, plates)
    render = render or plated
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
        result, _ = await jobs.render_job(
            _job("p"), config=CONFIG, paths=paths, assets=AssetStore(paths.assets)
        )
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


async def test_a_plate_openscad_refuses_as_empty_fails_naming_it(paths: DataPaths) -> None:
    """#1328: OpenSCAD will not export an empty plate at all; it logs "Current top
    level object is empty." and exits 1 before there is a 3MF to split."""
    render, _ = _plated_render({0: [TRAY, LID], 1: [TRAY]}, plates=2)

    async def refuse_plate_two(*args: object, **kwargs: object) -> object:
        if _plate_of(kwargs.get("extra_defines")) == 2:
            raise OpenSCADError(
                "openscad exited with 1",
                ["ECHO: plates = 2", "Current top level object is empty."],
                1,
            )
        return await render(*args, **kwargs)  # type: ignore[operator]

    with pytest.raises(OpenSCADError) as raised:
        await _render_plated(paths, {}, plates=2, render=refuse_plate_two)

    message = str(raised.value)
    assert message.startswith("plate 2 of 2 rendered no geometry")
    assert "echo(plates = 2)" in message
    assert raised.value.log_tail == ["ECHO: plates = 2", "Current top level object is empty."]
    assert raised.value.returncode == 1


# ── #424: the stages the render activities run, each from what is on disk ─────


def _stage_openscad(
    drawn: dict[int, list[tuple[str, str, trimesh.Trimesh]]],
    plates: int | None,
    hollow: frozenset[str] = frozenset(),
) -> object:
    """A stand-in `render_3mf` for the main, plate and wrapper renders alike: a
    wrapper render draws only its target colour, or nothing for one in ``hollow``."""

    async def render(*args: object, **kwargs: object) -> ProcessOutput:
        out = args[3]
        assert isinstance(out, Path)
        extra = kwargs.get("extra_defines")
        defines = [str(define) for define in extra] if isinstance(extra, list | tuple) else []
        parts = drawn[_plate_of(defines)]
        targets = next((d for d in defines if d.startswith("_sb_targets=")), None)
        if targets is not None:
            parts = [
                part
                for part in parts
                if part[1][:7] not in hollow and f'"{part[1][:7]}"' in targets
            ]
        write_openscad_3mf(out, parts)
        return ProcessOutput(
            returncode=0,
            log_tail=["rendered"],
            duration_s=0.0,
            missing_files=("pic.svg",),
            diagnostics=(WARNING,),
            notes=("a note",),
            plates=plates,
        )

    return render


STAGE_CASES = [
    pytest.param({0: [TRAY, WALL]}, None, frozenset({"#FF1493"}), id="one-plate"),
    pytest.param({0: [TRAY, WALL, LID], 1: [TRAY, WALL], 2: [LID]}, 2, frozenset(), id="plates"),
]


def _stage_patches(
    render: object,
) -> ExitStack:
    schema = _colour_schema(
        ("floor_color", "#0047BB"), ("wall_color", "#FF1493"), ("lid_color", "#FFFFFF")
    )

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return schema

    stack = ExitStack()
    stack.enter_context(mock.patch.object(jobs, "render_3mf", render))
    stack.enter_context(mock.patch.object(solids_module, "render_3mf", render))
    stack.enter_context(mock.patch.object(jobs, "cached_schema", cached_schema))
    return stack


def _entries(model: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(model) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


@pytest.mark.parametrize(("drawn", "plates", "hollow"), STAGE_CASES)
async def test_the_four_stages_give_what_render_job_gives(
    paths: DataPaths,
    drawn: dict[int, list[tuple[str, str, trimesh.Trimesh]]],
    plates: int | None,
    hollow: frozenset[str],
) -> None:
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")
    params: dict[str, ParamValue] = {"lid_color": "not-a-colour"}
    assets = AssetStore(paths.assets)
    work = paths.root / "blob"

    with _stage_patches(_stage_openscad(drawn, plates, hollow)):
        expected, log_tail = await jobs.render_job(
            _job("j", params=params), config=CONFIG, paths=paths, assets=assets
        )
        prepared, config = await prepare_source(
            "demo", None, config=CONFIG, paths=paths, history=None, fetcher=None
        )
        output = await render_main(
            prepared, params, work, config=config, assets=assets, checkouts=None, holder="piece"
        )
        await render_solids_stage(
            prepared,
            params,
            work,
            output,
            config=config,
            assets=assets,
            checkouts=None,
            holder="piece",
        )
        result = await finish_piece_stage(
            prepared,
            params,
            work,
            output,
            config=config,
            paths=paths,
            slug="demo",
            thumbnail_executor=None,
        )

    assert {RAW_RENDER_NAME, PREVIEW_NAME, LAYOUT_NAME, MODEL_NAME} <= {
        path.name for path in work.iterdir()
    }
    assert result.model_3mf == "blob/model.3mf"
    assert result.preview_glb == "blob/preview.glb"
    fields = set(JobResult.model_fields) - {"model_3mf", "preview_glb"}
    assert result.model_dump(include=fields) == expected.model_dump(include=fields)
    assert expected.warnings  # the missing file and the unreadable colour, at least
    assert output.log_tail == log_tail
    assert _entries(work / MODEL_NAME) == _entries(paths.root / expected.model_3mf)


@pytest.mark.parametrize(("drawn", "plates", "hollow"), STAGE_CASES)
async def test_the_layout_round_trips_and_the_last_stage_works_from_it_alone(
    paths: DataPaths,
    drawn: dict[int, list[tuple[str, str, trimesh.Trimesh]]],
    plates: int | None,
    hollow: frozenset[str],
) -> None:
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")
    params: dict[str, ParamValue] = {"lid_color": "not-a-colour"}
    assets = AssetStore(paths.assets)
    work = paths.root / "blob"

    with _stage_patches(_stage_openscad(drawn, plates, hollow)):
        expected, _ = await jobs.render_job(
            _job("j", params=params), config=CONFIG, paths=paths, assets=assets
        )
        prepared, config = await prepare_source(
            "demo", None, config=CONFIG, paths=paths, history=None, fetcher=None
        )
        output = await render_main(
            prepared, params, work, config=config, assets=assets, checkouts=None, holder="piece"
        )
        layout = await render_solids_stage(
            prepared,
            params,
            work,
            output,
            config=config,
            assets=assets,
            checkouts=None,
            holder="piece",
        )

        loaded = PlateLayout.load(work / LAYOUT_NAME)
        assert loaded.colours == layout.colours
        assert loaded.warnings == layout.warnings
        assert loaded.bbox == layout.bbox
        assert loaded.sources == layout.sources
        assert [plate.extruders for plate in loaded.plates] == [
            plate.extruders for plate in layout.plates
        ]
        for got, want in zip(loaded.plates, layout.plates, strict=True):
            for part, original in zip(got.parts, want.parts, strict=True):
                assert (part.material_index, part.name, part.colour) == (
                    original.material_index,
                    original.name,
                    original.colour,
                )
                assert (part.mesh.vertices == original.mesh.vertices).all()
                assert (part.mesh.faces == original.mesh.faces).all()
        # Every file the layout names is under the piece's directory.
        named = [source.file for plate in layout.sources for source in plate]
        assert all((work / name).is_file() for name in named)
        if plates is None:
            assert {source.solid for source in layout.sources[0]} == {True, False}
        else:
            assert named[0].startswith("plate-1/")

        # As another process would: nothing carried over but plain values.
        fresh = Prepared(
            Path(str(prepared.scad)),
            str(prepared.version),
            tuple(Path(str(p)) for p in prepared.library_path),
            Path(str(prepared.schema_cache)),
        )
        plain = ProcessOutput(
            returncode=0,
            log_tail=list(output.log_tail),
            duration_s=0.0,
            missing_files=tuple(output.missing_files),
            diagnostics=tuple(
                Diagnostic.model_validate(d.model_dump()) for d in output.diagnostics
            ),
            diagnostics_dropped=output.diagnostics_dropped,
            notes=tuple(output.notes),
            plates=output.plates,
        )
        result = await finish_piece_stage(
            fresh,
            dict(params),
            Path(str(work)),
            plain,
            config=config,
            paths=paths,
            slug="demo",
            thumbnail_executor=None,
        )

    fields = set(JobResult.model_fields) - {"model_3mf", "preview_glb"}
    assert result.model_dump(include=fields) == expected.model_dump(include=fields)
    assert _entries(work / MODEL_NAME) == _entries(paths.root / expected.model_3mf)


async def test_the_last_stage_warns_of_a_glyph_the_font_lacks(
    paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2358: ``finish_piece_stage`` gives the missing-glyph warning, asking fontconfig
    with the short advisory timeout."""
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")
    schema = CustomizerSchema(
        parameters=[
            Parameter(name="word", type="string", initial="Hi 😀"),
            Parameter(name="font", type="font", initial="Lobster Two"),
        ]
    )
    asked: list[tuple[str, float]] = []

    def coverage(font: str, env: object, timeout: float) -> tuple[str, list[tuple[int, int]]]:
        asked.append((font, timeout))
        return "Lobster Two", [(0x20, 0x7E)]

    monkeypatch.setattr(jobs, "font_coverage", coverage)
    assets = AssetStore(paths.assets)
    work = paths.root / "blob"

    with _stage_patches(_stage_openscad({0: [TRAY]}, None)):
        prepared, config = await prepare_source(
            "demo", None, config=CONFIG, paths=paths, history=None, fetcher=None
        )
        output = await render_main(
            prepared, {}, work, config=config, assets=assets, checkouts=None, holder="piece"
        )
        await render_solids_stage(
            prepared, {}, work, output, config=config, assets=assets, checkouts=None, holder="piece"
        )
        result = await finish_piece_stage(
            prepared,
            {},
            work,
            output,
            config=config,
            paths=paths,
            slug="demo",
            thumbnail_executor=None,
            schema=schema,
        )

    assert "font 'Lobster Two' has no glyph for 😀; the text leaves them out" in result.warnings
    # What is left of the one shared wait, which starts at the short advisory timeout.
    [(font, timeout)] = asked
    assert font == "Lobster Two"
    assert 0 < timeout <= GLYPH_CHECK_TIMEOUT


async def test_render_job_reads_the_schema_once_under_its_lease(paths: DataPaths) -> None:
    """The colour warnings come from the schema the render used, derived while the
    checkouts were held (#253), not from a second read once the lease is gone."""
    checkout = _model_pinning_a_library(paths)
    gate = CheckoutGate()
    schema = _colour_schema(("base_color", "#0047BB"))
    leased: list[list[str]] = []

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        leased.append(gate.leased(checkout))
        return schema

    with _stage_patches(_stage_openscad({0: [TRAY]}, None)) as stack:
        stack.enter_context(mock.patch.object(jobs, "cached_schema", cached_schema))
        result, _ = await jobs.render_job(
            _job("s", params={"base_color": "not-a-colour"}),
            config=CONFIG,
            paths=paths,
            assets=AssetStore(paths.assets),
            checkouts=gate,
        )

    assert leased == [["s"]]
    assert unreadable_colour_warnings(schema, {"base_color": "not-a-colour"})[0] in result.warnings


# ── the pins a render read (#169) ─────────────────────────────────────────────


async def test_a_render_records_the_library_pins_it_was_built_from(paths: DataPaths) -> None:
    """The result names the exact library commits, not only the model revision."""
    _model_pinning_a_library(paths)
    with _stage_patches(_stage_openscad({0: [TRAY]}, None)):
        result, _ = await jobs.render_job(
            _job("p"), config=CONFIG, paths=paths, assets=AssetStore(paths.assets)
        )

    assert [(pin.name, pin.ref, pin.commit) for pin in result.libraries] == [
        ("BOSL2", "v1", LIBRARY_COMMIT)
    ]


async def test_a_render_of_a_model_with_no_libraries_records_none(paths: DataPaths) -> None:
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")
    with _stage_patches(_stage_openscad({0: [TRAY]}, None)):
        result, _ = await jobs.render_job(
            _job("n"), config=CONFIG, paths=paths, assets=AssetStore(paths.assets)
        )

    assert result.libraries == []


def test_a_result_stored_before_the_pins_were_recorded_still_loads() -> None:
    stored = _result().model_dump(mode="json")
    del stored["libraries"]
    assert JobResult.model_validate(stored).libraries == []


async def test_a_colour_fallback_leaves_no_span_in_error(
    paths: DataPaths, tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    """Spec 2026-10-01 §6: spans end ERROR exactly when the job fails. One colour's
    wrapper render failing is that colour's fallback (spec 09-22 §6.3), not a failure."""
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")
    solid = write_openscad_3mf(tmp_path / "solid.3mf", [TRAY])
    binary = tmp_path / "fake-openscad"
    binary.write_text(
        "#!/bin/sh\n"
        'case "$*" in *"#FF1493"*) exit 3;; esac\n'
        f'prev=; for a; do [ "$prev" = -o ] && cp "{solid}" "$a"; prev=$a; done\n'
        "exit 0\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    schema = _colour_schema(("floor_color", "#0047BB"), ("wall_color", "#FF1493"))

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return schema

    with (
        mock.patch.object(jobs, "render_3mf", _stage_openscad({0: [TRAY, WALL]}, None)),
        mock.patch.object(jobs, "cached_schema", cached_schema),
    ):
        result, _ = await jobs.render_job(
            _job("j"),
            config=replace(CONFIG, openscad=str(binary)),
            paths=paths,
            assets=AssetStore(paths.assets),
        )

    assert any(w.startswith("#FF1493: no closed solid") for w in result.warnings)
    finished = spans.get_finished_spans()
    assert [s.name for s in finished if s.status.status_code is StatusCode.ERROR] == []
    fallbacks = [
        s
        for s in finished
        if s.name == "render.solid" and (s.attributes or {}).get("scadbuddy.solid.fallback")
    ]
    assert len(fallbacks) == 1
    # The exit code is the export's, recorded once (review 5 of #1064).
    assert "scadbuddy.openscad.exit_code" not in (fallbacks[0].attributes or {})
    exports = [s for s in finished if s.name == "openscad.export"]
    assert sorted(
        cast(int, (s.attributes or {})["scadbuddy.openscad.exit_code"]) for s in exports
    ) == [0, 3]


# ── #956: a template declares its objects with `echo(objects = N)` ────────────


def _define_of(extra_defines: object, name: str) -> int | None:
    defines = list(extra_defines) if isinstance(extra_defines, list | tuple) else []
    for define in defines:
        if isinstance(define, str) and define.startswith(f"${name}="):
            return int(define.removeprefix(f"${name}="))
    return None


async def _object_layout(
    tmp_path: Path,
    drawn: dict[tuple[int | None, int | None], list[tuple[str, str, trimesh.Trimesh]]],
    *,
    plates: int = 1,
    objects: int,
    missing: tuple[str, ...] = (),
) -> tuple[PlateLayout, list[tuple[int | None, int | None]], list[object]]:
    """`plate_layout` with a stand-in OpenSCAD drawing ``drawn[($plate, $object)]``;
    a key missing from ``drawn`` draws nothing, as OpenSCAD refuses to export it,
    logging that it could not open the files ``missing``."""
    asked: list[tuple[int | None, int | None]] = []
    solid_defines: list[object] = []

    async def render(*args: object, **kwargs: object) -> object:
        out = args[3]
        assert isinstance(out, Path)
        key = (
            _define_of(kwargs.get("extra_defines"), "plate"),
            _define_of(kwargs.get("extra_defines"), "object"),
        )
        asked.append(key)
        if not drawn.get(key):
            raise OpenSCADError(
                "openscad exited with 1",
                [*(f"WARNING: Can't open file '{name}'" for name in missing), jobs.EMPTY_TOP_LEVEL],
                1,
                missing_files=missing,
            )
        write_openscad_3mf(out, drawn[key])
        return mock.Mock(log_tail=[], missing_files=(), diagnostics=(), diagnostics_dropped=0)

    async def solids(*args: object, **kwargs: object) -> SolidRender:
        solid_defines.append(kwargs.get("extra_defines"))
        return SolidRender()

    preview = tmp_path / "preview.3mf"
    write_openscad_3mf(preview, drawn[(None, None)])
    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "render_solids", solids),
    ):
        layout = await jobs.plate_layout(
            tmp_path / "model.scad",
            _colour_schema(("floor_color", "#0047BB"), ("lid_color", "#FFFFFF")),
            {},
            split_by_material(preview),
            plates,
            tmp_path,
            config=CONFIG,
            objects=objects,
        )
    return layout, asked, solid_defines


async def test_each_declared_object_is_its_own_render_and_its_own_parts(tmp_path: Path) -> None:
    layout, asked, solid_defines = await _object_layout(
        tmp_path, {(None, None): [TRAY, LID], (None, 1): [TRAY], (None, 2): [LID]}, objects=2
    )

    assert asked == [(None, 1), (None, 2)]
    # The per-colour solids are drawn per object too.
    assert [_define_of(defines, "object") for defines in solid_defines] == [1, 2]
    [plate] = layout.plates
    assert [part.colour for part in plate.parts] == ["#0047BB", "#FFFFFF"]
    assert plate.objects == (1, 2)
    assert plate.extruders == (1, 2)
    assert layout.warnings == []


async def test_an_object_on_some_plates_only_is_on_those(tmp_path: Path) -> None:
    layout, asked, _ = await _object_layout(
        tmp_path,
        {(None, None): [TRAY, LID], (1, 1): [TRAY], (2, 2): [LID]},
        plates=2,
        objects=2,
    )

    assert asked == [(1, 1), (1, 2), (2, 1), (2, 2)]
    # Each part keeps the object the template declared it with (#3219): plate 2's one
    # object is object 2, though the writer numbers it first on its plate.
    assert [plate.objects for plate in layout.plates] == [(1,), (2,)]
    assert [[part.colour for part in plate.parts] for plate in layout.plates] == [
        ["#0047BB"],
        ["#FFFFFF"],
    ]


async def test_an_object_drawn_on_no_plate_fails_naming_it(tmp_path: Path) -> None:
    with pytest.raises(OpenSCADError, match=r"object 2 of 2 rendered no geometry.*\$object = 2"):
        await _object_layout(tmp_path, {(None, None): [TRAY], (None, 1): [TRAY]}, objects=2)


async def test_a_plate_that_draws_no_object_still_fails_as_an_empty_plate(
    tmp_path: Path,
) -> None:
    """#956 review: with plates and objects both declared, a plate that draws none of
    its objects is the empty plate it always was, named as one."""
    with pytest.raises(OpenSCADError, match=r"plate 2 of 2 rendered no geometry.*\$plate = 2"):
        await _object_layout(
            tmp_path,
            {(None, None): [TRAY, LID], (1, 1): [TRAY], (1, 2): [LID]},
            plates=2,
            objects=2,
        )


async def test_a_colour_only_one_object_draws_is_named_by_plate_and_object(
    tmp_path: Path,
) -> None:
    """#956 review: the warning's short label came from str.replace, which garbled
    "object 3 of 12" on a one-plate layout; it is built from the numbers now."""
    drawn: dict[tuple[int | None, int | None], list[tuple[str, str, trimesh.Trimesh]]] = {
        (None, n): [TRAY] for n in range(1, 13)
    }
    drawn[(None, None)] = [TRAY]
    drawn[(None, 3)] = [TRAY, LID]
    layout, _, _ = await _object_layout(tmp_path, drawn, objects=12)

    assert layout.warnings == [
        "object 3: #FFFFFF is not in the everything-at-once render; it gets extruder 2"
    ]


async def test_no_object_drawing_anything_fails_with_the_log_of_why(tmp_path: Path) -> None:
    """#3218: with objects and no plates, every object may draw nothing because of a
    file it could not open; the error keeps that render's log and missing files, as an
    empty plate's does (#451)."""
    with pytest.raises(OpenSCADError, match="no geometry for any of its objects") as failed:
        await _object_layout(tmp_path, {(None, None): [TRAY]}, objects=2, missing=("logo.svg",))

    assert failed.value.missing_files == ("logo.svg",)
    assert failed.value.log_tail == ["WARNING: Can't open file 'logo.svg'", jobs.EMPTY_TOP_LEVEL]


async def test_objects_that_all_draw_the_same_are_warned_about(tmp_path: Path) -> None:
    """#3218: a template that declares objects but never reads $object draws the whole
    model for each, so the objects would sit on top of each other."""
    every = [TRAY, LID]
    layout, _, _ = await _object_layout(
        tmp_path, {(None, None): every, (None, 1): every, (None, 2): every}, objects=2
    )

    assert layout.warnings == [
        "every object draws the same geometry in the same place; the template declares"
        " echo(objects = 2), but does it read $object?"
    ]


async def test_too_many_objects_fails_the_job(tmp_path: Path) -> None:
    with pytest.raises(OpenSCADError, match="asks for 17 objects"):
        await _object_layout(tmp_path, {(None, None): [TRAY]}, objects=jobs.MAX_OBJECTS + 1)


async def test_plates_times_objects_past_the_render_cap_fails_before_rendering(
    tmp_path: Path,
) -> None:
    """#3250: each count was capped on its own, so 16 plates of 16 objects ran 256
    OpenSCAD renders in one job. The renders a job runs are capped as a whole."""
    plates, objects = 2, jobs.MAX_RENDERS // 2 + 1
    assert plates <= jobs.MAX_PLATES and objects <= jobs.MAX_OBJECTS
    # Every plate draws every object, so nothing but the cap stops the layout.
    drawn: dict[tuple[int | None, int | None], list[tuple[str, str, trimesh.Trimesh]]] = {
        (p, o): [TRAY] for p in range(1, plates + 1) for o in range(1, objects + 1)
    }
    drawn[(None, None)] = [TRAY]
    with pytest.raises(OpenSCADError, match=f"{plates} plates of {objects} objects"):
        await _object_layout(tmp_path, drawn, plates=plates, objects=objects)


async def test_a_layouts_objects_survive_saving_it(tmp_path: Path) -> None:
    layout, _, _ = await _object_layout(
        tmp_path, {(None, None): [TRAY, LID], (None, 1): [TRAY], (None, 2): [LID]}, objects=2
    )
    layout.save(tmp_path / "layout.json")

    loaded = PlateLayout.load(tmp_path / "layout.json")

    assert loaded.plates[0].objects == (1, 2)
    # One object is written as before, with no object numbers at all.
    single = json.loads((tmp_path / "layout.json").read_text())["plates"][0]["parts"]
    assert "object" not in single[0] and single[1]["object"] == 2


async def _render_painted(
    paths: DataPaths, sabotage: Callable[[Path], None] | None = None
) -> tuple[JobResult, tuple[str, ...]]:
    """A one-box mesh model with a painted part, rendered through `render_job` with the
    OpenSCAD calls faked; ``sabotage`` damages the model's directory first."""
    model_dir = paths.model_dir("demo")
    model_dir.mkdir(parents=True)
    mesh, codes = painted_box()
    read = [
        ReadObject("Box", 1, [ColourPart(0, "Body", ORANGE, mesh, Paint(codes, (ORANGE, GREEN)))])
    ]
    scene = written(model_dir, read)
    world = rendered(model_dir, scene, ORANGE)
    if sabotage is not None:
        sabotage(model_dir)

    async def render(*args: object, **kwargs: object) -> object:
        out = args[3]
        assert isinstance(out, Path)
        write_openscad_3mf(out, [("Body", f"{ORANGE}00", world)])
        return mock.Mock(
            log_tail=[],
            missing_files=(),
            diagnostics=(),
            diagnostics_dropped=0,
            notes=(),
            plates=None,
            objects=None,
        )

    async def cached_schema(*args: object, **kwargs: object) -> CustomizerSchema:
        return CustomizerSchema()

    async def render_solids(*args: object, **kwargs: object) -> SolidRender:
        colours, work_dir = args[3], args[4]
        assert isinstance(colours, list) and isinstance(work_dir, Path)
        meshes = {}
        for index, colour in enumerate(colours, start=1):
            path = solid_file(work_dir, index)
            write_openscad_3mf(path, [("Body", f"{colour}00", world)])
            found = solid_mesh(path)
            assert found is not None
            meshes[colour] = found
        return SolidRender(meshes=meshes)

    with (
        mock.patch.object(jobs, "render_3mf", render),
        mock.patch.object(jobs, "cached_schema", cached_schema),
        mock.patch.object(jobs, "render_solids", render_solids),
    ):
        result, _ = await jobs.render_job(
            _job("p"), config=CONFIG, paths=paths, assets=AssetStore(paths.assets)
        )
    return result, codes


async def test_a_mesh_models_painting_reaches_its_3mf(paths: DataPaths) -> None:
    """Spec 2026-10-11 §7: the solid of a painted part's colour carries the part's codes
    into model.3mf, through the layout that `finish_piece` and an output re-read; the
    colours the painting uses get filaments."""
    result, codes = await _render_painted(paths)
    assert result.colors == [ORANGE, GREEN]
    assert [(p.colour, p.extruder) for p in result.parts] == [(ORANGE, 1), (GREEN, 2)]
    [obj] = read_objects((paths.root / result.model_3mf).read_bytes(), ReadBudget())
    [part] = obj.parts
    assert part.paint is not None and part.paint.codes == codes
    layout = PlateLayout.load(paths.cache / "render-job" / "p" / LAYOUT_NAME)
    [loaded] = layout.plates[0].parts
    assert loaded.paint == Paint(codes, (ORANGE, GREEN))
    assert layout.sources[0][0].paint == "paint-1-1.json"


def _delete_meshes(model_dir: Path) -> None:
    for file in model_dir.rglob("*.3mf"):
        file.unlink()


def _garble_meshes(model_dir: Path) -> None:
    for file in model_dir.rglob("*.3mf"):
        file.write_bytes(b"not a zip")


def _break_scene(model_dir: Path) -> None:
    (model_dir / SCENE_NAME).write_text('{"objects": 3', encoding="utf-8")


@pytest.mark.parametrize(
    "sabotage",
    [_delete_meshes, _garble_meshes, _break_scene],
    ids=["no-mesh-file", "garbled-mesh", "bad-scene"],
)
async def test_an_unreadable_painting_costs_the_painting_not_the_render(
    paths: DataPaths, sabotage: Callable[[Path], None]
) -> None:
    result, _ = await _render_painted(paths, sabotage)
    assert result.colors == [ORANGE]
    assert jobs.PAINT_NOT_CARRIED in result.warnings
    layout = PlateLayout.load(paths.cache / "render-job" / "p" / LAYOUT_NAME)
    assert layout.plates[0].parts[0].paint is None
    assert layout.sources[0][0].paint is None


def test_a_non_mesh_layout_comes_back_identical_with_no_warnings(tmp_path: Path) -> None:
    mesh = trimesh.creation.box((1, 1, 1))
    part = ColourPart(1, "Color 1", "#FF0000", mesh)
    layout = PlateLayout(
        [single_plate([part])],
        ["#FF0000"],
        [],
        BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        [(PartSource(RAW_RENDER_NAME),)],
    )
    painted = jobs.paint_layout(layout, tmp_path / "model.scad", {}, tmp_path)
    assert painted is layout
    assert painted.warnings == []


def test_a_layout_part_without_paint_is_saved_as_before(tmp_path: Path) -> None:
    """Regression guard: no "paint" key unless a part carries paint, and a layout
    written before still loads."""
    mesh = trimesh.creation.box((1, 1, 1))
    raw = tmp_path / RAW_RENDER_NAME
    write_openscad_3mf(raw, [("Color 1", "#FF000000", mesh)])
    part = ColourPart(1, "Color 1", "#FF0000", mesh)
    layout = PlateLayout(
        [single_plate([part])],
        ["#FF0000"],
        [],
        BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
        [(PartSource(RAW_RENDER_NAME),)],
    )
    layout.save(tmp_path / LAYOUT_NAME)
    entry = json.loads((tmp_path / LAYOUT_NAME).read_text())["plates"][0]["parts"][0]
    assert "paint" not in entry
    assert PlateLayout.load(tmp_path / LAYOUT_NAME).plates[0].parts[0].paint is None


class _WatchedET:
    """`xml.etree.ElementTree` with its `fromstring` replaced."""

    def __init__(self, fromstring: Callable[..., ET.Element]) -> None:
        self.fromstring = fromstring

    def __getattr__(self, name: str) -> object:
        return getattr(ET, name)


@pytest.mark.parametrize(("drawn", "plates", "hollow"), STAGE_CASES)
async def test_the_solids_stage_splits_and_writes_its_meshes_off_the_event_loop(
    paths: DataPaths,
    drawn: dict[int, list[tuple[str, str, trimesh.Trimesh]]],
    plates: int | None,
    hollow: frozenset[str],
) -> None:
    """#3788: the worker's loop runs every workflow task and every activity, and a
    split held it long enough to time workflow tasks out at 10 s. Each mesh parse
    and the preview's GLB run in a thread."""
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("// stand-in\n", encoding="utf-8")
    params: dict[str, ParamValue] = {}
    assets = AssetStore(paths.assets)
    work = paths.root / "blob"
    loop_thread = threading.get_ident()
    on_loop: list[str] = []
    ran: list[str] = []

    def watched[**P, T](name: str, fn: Callable[P, T]) -> Callable[P, T]:
        def call(*args: P.args, **kwargs: P.kwargs) -> T:
            ran.append(name)
            if threading.get_ident() == loop_thread:
                on_loop.append(name)
            return fn(*args, **kwargs)

        return call

    with (
        _stage_patches(_stage_openscad(drawn, plates, hollow)),
        # At the parse itself, not at `jobs`' name for it: a split reached through any
        # other module (`solids.solid_mesh`, `PlateLayout.load`) is held to it too.
        mock.patch.object(split_module, "ET", _WatchedET(watched("split", ET.fromstring))),
        mock.patch.object(jobs, "write_glb", watched("write_glb", write_glb)),
    ):
        prepared, config = await prepare_source(
            "demo", None, config=CONFIG, paths=paths, history=None, fetcher=None
        )
        output = await render_main(
            prepared, params, work, config=config, assets=assets, checkouts=None, holder="piece"
        )
        await render_solids_stage(
            prepared,
            params,
            work,
            output,
            config=config,
            assets=assets,
            checkouts=None,
            holder="piece",
        )
    # Both were reached, so an empty `on_loop` is not a guard that never ran.
    assert {"split", "write_glb"} <= set(ran)
    assert on_loop == []
