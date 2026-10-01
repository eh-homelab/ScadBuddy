"""Packing and pipeline outputs (spec 2026-09-27 §5.2, §5.3, §8.4)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import BomEntry, OutputRecord
from scadbuddy.render.schema import ParamValue
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.template import Blob, Part
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps, _scope
from scadbuddy.workflows.arrange import arrange
from scadbuddy.workflows.models import (
    Layout,
    OutputRequest,
    PackItem,
    PackRequest,
    PieceRequest,
    PlateSize,
    piece_key,
)
from scadbuddy.workflows.packing import PackError, explicit_plate
from scadbuddy.workflows.pipeline_activities import PipelineActivities
from tests.support.openscad import install_fake_openscad

PLATE = PlateSize(key="default", width=256.0, depth=256.0)


def _part(key: str, w: float, d: float, *, plates: int = 1) -> Part:
    return Part(
        piece_key=key,
        file="model.scad",
        colours=["#FF0000"],
        plates=plates,
        bbox=BoundingBox(min=(-w / 2, -d / 2, 0), max=(w / 2, d / 2, 5), size=(w, d, 5)),
    )


def test_one_part_alone_keeps_its_own_plates() -> None:
    assert arrange([PackItem(part=_part("a", 10, 10, plates=3))], PLATE) == Layout(own="a")


def test_a_part_larger_than_the_plate_is_refused() -> None:
    with pytest.raises(PackError, match="larger than the plate"):
        arrange([PackItem(part=_part("a", 300, 10)), PackItem(part=_part("b", 1, 1))], PLATE)


def test_a_multi_plate_part_cannot_share() -> None:
    with pytest.raises(PackError, match="its own 2 plates"):
        arrange(
            [PackItem(part=_part("a", 10, 10, plates=2)), PackItem(part=_part("b", 1, 1))], PLATE
        )


def test_an_explicit_plate_places_where_told() -> None:
    plate = explicit_plate([_part("a", 10, 10)], [(20.0, 30.0, 0.0)], plate=PLATE)
    assert plate.items[0].x == 20.0 and plate.items[0].y == 30.0
    with pytest.raises(PackError, match="quarter turns"):
        explicit_plate([_part("a", 10, 10)], [(0.0, 0.0, 45.0)], plate=PLATE)


@pytest.mark.parametrize("at", [(250.0, 0.0, 0.0), (0.0, 250.0, 0.0), (-1.0, 0.0, 0.0)])
def test_an_explicit_position_off_the_plate_is_refused(at: tuple[float, float, float]) -> None:
    with pytest.raises(PackError, match="off the plate"):
        explicit_plate([_part("a", 10, 10)], [at], plate=PLATE)


def test_a_turned_part_is_checked_by_its_turned_footprint() -> None:
    # 100 x 10 at x=200 fits the 256 mm plate turned (10 wide), not as it is.
    explicit_plate([_part("a", 100, 10)], [(200.0, 0.0, 90.0)], plate=PLATE)
    with pytest.raises(PackError, match="off the plate"):
        explicit_plate([_part("a", 100, 10)], [(200.0, 0.0, 0.0)], plate=PLATE)


def test_nothing_to_pack_is_refused_at_once() -> None:
    with pytest.raises(PackError, match="nothing to pack"):
        arrange([], PLATE)


class _Refs:
    """`BlobRefs.add` as the job's refs: what `write_output` holds, and for whom."""

    def __init__(self) -> None:
        self.added: list[tuple[str, str, str]] = []

    def add(self, key: str, holder_kind: str, holder_id: str) -> None:
        self.added.append((key, holder_kind, holder_id))


def _deps(tmp_path: Path) -> tuple[WorkerDeps, DataPaths]:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("cube();\n", encoding="utf-8")
    paths.model_meta("demo").write_text('{"name": "Demo"}', encoding="utf-8")
    (paths.model_dir("demo") / "parts").mkdir()
    (paths.model_dir("demo") / "parts" / "roof.scad").write_text("cube();\n", encoding="utf-8")
    deps = WorkerDeps(
        config=install_fake_openscad(tmp_path, paths),
        paths=paths,
        assets=AssetStore(paths.assets),
        blobs=LocalBlobStore(paths.blobs),
        refs=_Refs(),  # type: ignore[arg-type]
        projection=None,  # type: ignore[arg-type]
        revision="rev-1",
        openscad_version="OpenSCAD version 2026.09.28",
    )
    return deps, paths


def _request(file: str, params: dict[str, ParamValue]) -> PieceRequest:
    return PieceRequest(
        slug="demo",
        revision=None,
        file=file,
        params=params,
        piece_key=piece_key("demo", None, file, params),
    )


async def _render(deps: WorkerDeps, file: str, params: dict[str, ParamValue]) -> Part:
    acts, env = RenderActivities(deps), ActivityEnvironment()
    req = _request(file, params)
    prepared = await env.run(acts.prepare, req)
    main = await env.run(acts.render_main, req, prepared)
    await env.run(acts.render_solids, req, prepared, main)
    return Part.of(req, await env.run(acts.finish_piece, req, prepared, main))


def _record(parts: list[str]) -> OutputRecord:
    return OutputRecord(
        revision=None,
        ui_api=None,
        pipeline_api=1,
        pipeline_version="default",
        inputs_v=0,
        plate_key="default",
        parts=parts,
    )


async def test_a_piece_renders_another_file_of_the_template(tmp_path: Path) -> None:
    deps, _ = _deps(tmp_path)
    roof = await _render(deps, "parts/roof.scad", {"width": 3})
    assert roof.file == "parts/roof.scad"
    req = _request("parts/roof.scad", {"width": 3})
    prepared = await ActivityEnvironment().run(RenderActivities(deps).prepare, req)
    assert prepared.scad.endswith("parts/roof.scad")
    assert _scope(req, prepared).title == "Demo"  # the template's folder, not parts/


@pytest.mark.parametrize(
    ("file", "params", "message"),
    [
        ("../escape.scad", {}, "by its plain path inside the template"),
        # Inside the template, but not canonical: the template root is derived from the
        # string, so each would root the fonts scan and the store folder elsewhere.
        ("parts/../model.scad", {}, "by its plain path inside the template"),
        ("./model.scad", {}, "by its plain path inside the template"),
        ("parts//roof.scad", {}, "by its plain path inside the template"),
        ("ABSOLUTE", {}, "by its plain path inside the template"),
        ("model.scad", {"nope": 1}, "unknown parameters: nope"),
        ("model.scad", {"width": "wide"}, "expects a number"),
    ],
)
async def test_a_bad_file_or_parameter_fails_the_piece(
    tmp_path: Path, file: str, params: dict[str, ParamValue], message: str
) -> None:
    deps, paths = _deps(tmp_path)
    if file == "ABSOLUTE":  # an absolute path to the template's own model.scad
        file = str(paths.model_source("demo"))
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(RenderActivities(deps).prepare, _request(file, params))
    assert raised.value.type == "ParameterError" and raised.value.non_retryable
    assert message in raised.value.message


def _refs(deps: WorkerDeps) -> list[tuple[str, str, str]]:
    refs = deps.refs
    assert isinstance(refs, _Refs)
    return refs.added


async def test_the_default_output_is_the_piece_as_rendered(tmp_path: Path) -> None:
    deps, _ = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    req = OutputRequest(
        job_id="j1",
        index=0,
        slug="demo",
        layout=Layout(own=part.piece_key),
        parts=[part],
        name=None,
        bom=[],
        files={},
        record=_record([part.piece_key]),
    )
    out = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert out.blob_keys == [part.piece_key] and out.files_key is None
    assert out.result.model_3mf.startswith(f"blobs/{part.piece_key}/")
    assert out.record.image_revision == "rev-1"
    assert out.record.openscad_version == "OpenSCAD version 2026.09.28"
    # Held for the job as soon as it exists: a long pipeline must not lose it to a sweep.
    assert _refs(deps) == [(part.piece_key, "job", "j1")]


async def test_a_packed_output_writes_every_plate_bom_and_files(tmp_path: Path) -> None:
    deps, paths = _deps(tmp_path)
    a = await _render(deps, "model.scad", {"width": 12})
    b = await _render(deps, "parts/roof.scad", {"width": 4})
    acts, env = PipelineActivities(deps), ActivityEnvironment()
    layout = await env.run(
        acts.pack, PackRequest(items=[PackItem(part=a, count=2), PackItem(part=b)], plate=PLATE)
    )
    out_dir = deps.blobs.dir_for("act-x")
    (out_dir / "guide.svg").write_text("<svg/>", encoding="utf-8")
    req = OutputRequest(
        job_id="j1",
        index=1,
        slug="demo",
        layout=layout,
        parts=[a, b],
        name="two",
        bom=[BomEntry(piece="a", label="A", count=2, part=a.piece_key)],
        files={"notes.txt": "hello", "guide.svg": Blob(key="act-x", path="guide.svg")},
        record=_record([a.piece_key, b.piece_key]),
    )
    out = await env.run(acts.write_output, req)
    assert out.blob_keys == ["output-j1-1"] and out.files_key == "output-j1-1"
    assert sorted(out.files) == ["guide.svg", "notes.txt"]
    model = paths.root / out.result.model_3mf
    with zipfile.ZipFile(model) as archive:
        assert "3D/3dmodel.model" in archive.namelist()
    assert (paths.blobs / "output-j1-1" / "files" / "guide.svg").read_text() == "<svg/>"
    assert out.bom[0].count == 2
    assert _refs(deps) == [("output-j1-1", "job", "j1")]


async def _write_file_named(tmp_path: Path, name: str) -> ApplicationError:
    deps, _ = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    req = OutputRequest(
        job_id="j1",
        index=0,
        slug="demo",
        layout=Layout(own=part.piece_key),
        parts=[part],
        name=None,
        bom=[],
        files={name: "x"},
        record=_record([]),
    )
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert raised.value.type == "OutputError" and raised.value.non_retryable
    return raised.value


@pytest.mark.parametrize("name", ["../x", ".hidden", "a/b", "/etc/passwd"])
async def test_an_output_file_name_that_could_escape_is_refused(tmp_path: Path, name: str) -> None:
    error = await _write_file_named(tmp_path, name)
    assert "use letters, digits" in error.message


@pytest.mark.parametrize("name", ["model.3mf", "preview.glb", "layout.json", "piece.json"])
async def test_an_output_file_name_the_output_itself_uses_is_refused(
    tmp_path: Path, name: str
) -> None:
    """A safe name, but one of the output directory's own files: `files/` sits beside
    them, and a template file must never be mistaken for (or shadow) one."""
    error = await _write_file_named(tmp_path, name)
    assert "is reserved" in error.message


async def test_an_own_layout_must_name_one_of_the_parts(tmp_path: Path) -> None:
    deps, _ = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    stranger = piece_key("demo", None, "model.scad", {"width": 99})
    req = OutputRequest(
        job_id="j1",
        index=0,
        slug="demo",
        layout=Layout(own=stranger),
        parts=[part],
        name=None,
        bom=[],
        files={},
        record=_record([part.piece_key]),
    )
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert raised.value.type == "PackError" and raised.value.non_retryable
    assert _refs(deps) == []
