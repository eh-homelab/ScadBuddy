"""Every output records its objects (spec 2026-09-27 §7), and keeps their Parts alive."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh
from temporalio.testing import ActivityEnvironment

from scadbuddy.bambuddy.library_objects import piece_key, publish_library_pieces
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.outputs import OutputStore, hold_parts, release_parts
from scadbuddy.render.bambu3mf import PROJECT_SETTINGS_NAME, write_bambu_3mf
from scadbuddy.render.glb import BoundingBox, bounding_box
from scadbuddy.render.job_models import ManifestObject
from scadbuddy.render.objects3mf import read_objects
from scadbuddy.render.split import ColourPart
from scadbuddy.store import sweep_blobs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from scadbuddy.workflows.arrange import part_of
from scadbuddy.workflows.models import (
    Layout,
    LayoutPlate,
    OutputRequest,
    PackItem,
    PackRequest,
    Placed,
    PlateSize,
)
from scadbuddy.workflows.outputs import manifest_of, placement_matrix
from scadbuddy.workflows.pipeline_activities import PipelineActivities, pack_layout
from tests.support.arrange import finished_job, saved_output
from tests.test_packing_and_outputs import _deps, _record, _render


async def test_a_packed_output_lists_each_object_once_with_its_count(tmp_path: Path) -> None:
    paths, meta, written = await saved_output(tmp_path, count=3)
    [obj] = written.manifest
    assert obj.count == 3
    assert obj.slug == "demo" and obj.file == "model.scad" and obj.revision is None
    assert obj.bom_piece == "wall"
    assert obj.footprint == (obj.bbox.size[0], obj.bbox.size[1])
    assert OutputStore(paths).manifest(meta.id) == written.manifest


async def test_an_output_of_one_piece_as_rendered_has_a_manifest_too(tmp_path: Path) -> None:
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
    assert [(m.part, m.count, m.plates) for m in out.manifest] == [(part.piece_key, 1, part.plates)]


async def test_provenance_from_an_earlier_output_is_carried(tmp_path: Path) -> None:
    deps, _ = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    earlier = ManifestObject(
        part=part.piece_key,
        file="model.scad",
        slug="dollhouse-kit",
        revision="abc",
        bbox=part.bbox,
        footprint=(1.0, 1.0),
        colours=part.colours,
        count=4,
        bom_piece="wall",
        source_output="o-old",
    )
    req = OutputRequest(
        job_id="j2",
        index=0,
        slug="demo",
        layout=Layout(own=part.piece_key),
        parts=[part],
        name=None,
        bom=[],
        files={},
        record=_record([part.piece_key]),
        provenance={part.piece_key: earlier},
    )
    [obj] = manifest_of(req)
    assert (obj.slug, obj.revision, obj.bom_piece, obj.source_output) == (
        "dollhouse-kit",
        "abc",
        "wall",
        "o-old",
    )
    assert obj.count == 1  # the count is this layout's, never the source's


def test_an_unknown_output_reads_as_none(tmp_path: Path) -> None:
    assert OutputStore(DataPaths(tmp_path)).manifest("never-written") == []


async def test_an_output_saved_before_manifests_reads_as_none(tmp_path: Path) -> None:
    """A job without pipeline outputs (one saved before phase 5) writes no manifest.json."""
    paths, job, _ = await finished_job(tmp_path)
    legacy = job.model_copy(update={"outputs": []})
    store = OutputStore(paths)
    meta = store.create(legacy, name="old")
    assert not (store.directory(meta.id) / "manifest.json").exists()
    assert store.manifest(meta.id) == []
    assert store.arranged_from(meta.id) == []


@pytest.mark.requires_postgres
async def test_a_saved_output_keeps_its_parts_after_the_job_is_pruned(
    tmp_path: Path, pg_conninfo: str
) -> None:
    from tests.support.store import store_pool

    paths, meta, written = await saved_output(tmp_path)
    key = written.manifest[0].part
    with store_pool(pg_conninfo) as pool:
        refs = BlobRefs(pool)
        refs.add(key, "job", "j1")
        hold_parts(refs, meta.id, written.manifest, meta.slug)
        refs.drop_holder("job", "j1")  # what JobProjection.prune does
        blobs = LocalBlobStore(paths.blobs)
        assert key not in sweep_blobs(blobs, refs, grace=0, now=10**12)
        assert blobs.exists(key)
        release_parts(refs, meta.id)
        assert key in sweep_blobs(blobs, refs, grace=0, now=10**12)


def test_a_quarter_turn_lands_the_turned_box_where_it_was_placed() -> None:
    box = BoundingBox(min=(-5, -10, 2), max=(5, 10, 7), size=(10, 20, 5))
    corners = np.array([[x, y, z, 1] for x in (-5, 5) for y in (-10, 10) for z in (2, 7)]).T
    moved = (placement_matrix(box, 30.0, 40.0, 90.0) @ corners)[:3]
    assert np.allclose(moved.min(axis=1), (30, 40, 0))
    assert np.allclose(moved.max(axis=1), (50, 50, 5))  # 20 wide, 10 deep once turned
    still = (placement_matrix(box, 30.0, 40.0, 0.0) @ corners)[:3]
    assert np.allclose(still.min(axis=1), (30, 40, 0)) and np.allclose(
        still.max(axis=1), (40, 60, 5)
    )


async def test_an_arranged_output_keeps_the_filament_order_it_was_planned_against(
    tmp_path: Path,
) -> None:
    deps, paths = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    own = part.colours[0]
    planned = ["#123456", own]  # the output the plan was made for had own colour in slot 2
    req = OutputRequest(
        job_id="j3",
        index=0,
        slug="demo",
        layout=Layout(
            plates=[
                LayoutPlate(
                    items=[
                        Placed(piece_key=part.piece_key, x=0, y=0, rot=90.0),
                        Placed(piece_key=part.piece_key, x=40, y=0),
                    ]
                )
            ]
        ),
        parts=[part],
        name=None,
        bom=[],
        files={},
        record=_record([part.piece_key]),
        colours=planned,
    )
    out = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert out.result.colors[:2] == [c.upper() for c in planned]
    with zipfile.ZipFile(paths.root / out.result.model_3mf) as archive:
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    assert [c.upper() for c in settings["filament_colour"][:2]] == [c.upper() for c in planned]


async def test_one_object_arranged_alone_still_takes_the_planned_order(tmp_path: Path) -> None:
    # Review Focus 4 through the pack step: one object, one copy, as the Arrange workflow
    # packs it (allow_own=False), then written with the colours the plan was made for.
    deps, paths = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    planned = ["#123456", part.colours[0].upper()]
    layout = pack_layout(
        PackRequest(
            items=[PackItem(part=part)],
            plate=PlateSize(key="default", width=256.0, depth=256.0),
            colours=planned,
            allow_own=False,
        )
    )
    assert layout.own is None
    req = OutputRequest(
        job_id="j4",
        index=0,
        slug="demo",
        layout=layout,
        parts=[part],
        name=None,
        bom=[],
        files={},
        record=_record([part.piece_key]),
        colours=planned,
    )
    out = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert out.result.colors[:2] == planned
    with zipfile.ZipFile(paths.root / out.result.model_3mf) as archive:
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    assert [c.upper() for c in settings["filament_colour"][:2]] == planned


async def test_a_colour_planned_twice_in_two_cases_takes_one_slot(tmp_path: Path) -> None:
    deps, paths = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    own = part.colours[0].upper()
    req = OutputRequest(
        job_id="j5",
        index=0,
        slug="demo",
        layout=Layout(plates=[LayoutPlate(items=[Placed(piece_key=part.piece_key, x=0, y=0)])]),
        parts=[part],
        name=None,
        bom=[],
        files={},
        record=_record([part.piece_key]),
        colours=["#abcdef", "#ABCDEF", own.lower()],
    )
    out = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert out.result.colors == ["#ABCDEF", own]
    with zipfile.ZipFile(paths.root / out.result.model_3mf) as archive:
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    assert [c.upper() for c in settings["filament_colour"]] == ["#ABCDEF", own]


async def test_a_library_files_object_is_placed_beside_a_rendered_part(tmp_path: Path) -> None:
    """#1863: an object read from a library 3MF is a piece like a render's, which the
    writer places and the manifest records with the file it came from."""
    deps, paths = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    red = trimesh.creation.box(extents=(10, 10, 4))
    blue = trimesh.creation.box(extents=(6, 6, 9))
    out = tmp_path / "plain.3mf"
    write_bambu_3mf(
        [ColourPart(1, "Red", "#FF0000", red), ColourPart(2, "Blue", "#0000FF", blue)],
        out,
        thumbnails=None,
    )
    read = read_objects(out.read_bytes())
    box = bounding_box(read[0].parts)
    entry = ManifestObject(
        part=piece_key("ab" * 32, 0),
        file=read[0].name,
        slug="demo",
        revision=None,
        bbox=box,
        footprint=(box.size[0], box.size[1]),
        colours=["#FF0000", "#0000FF"],
        count=1,
        library_file_id=88,
    )
    await publish_library_pieces(deps.blobs, [entry], read)
    library = part_of(entry)
    layout = pack_layout(
        PackRequest(
            items=[PackItem(part=part), PackItem(part=library, count=2)],
            plate=PlateSize(key="default", width=256.0, depth=256.0),
            allow_own=False,
        )
    )
    req = OutputRequest(
        job_id="j6",
        index=0,
        slug="demo",
        layout=layout,
        parts=[part, library],
        name=None,
        bom=[],
        files={},
        record=_record([part.piece_key, library.piece_key]),
        provenance={entry.part: entry},
    )
    written = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert {"#FF0000", "#0000FF"} <= set(written.result.colors)
    [_, obj] = written.manifest
    assert obj.part == entry.part and obj.count == 2 and obj.library_file_id == 88
    with zipfile.ZipFile(paths.root / written.result.model_3mf) as archive:
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    assert {"#FF0000", "#0000FF"} <= {c.upper() for c in settings["filament_colour"]}
