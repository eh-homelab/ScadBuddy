"""Every output records its objects (spec 2026-09-27 §7), and keeps their Parts alive."""

from __future__ import annotations

import gc
import json
import weakref
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import trimesh
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.bambuddy.library_objects import piece_key, publish_library_pieces
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.outputs import OutputStore, hold_parts, release_parts
from scadbuddy.render.bambu3mf import PROJECT_SETTINGS_NAME, laid_out_plates, write_bambu_3mf
from scadbuddy.render.glb import BoundingBox, bounding_box
from scadbuddy.render.job_models import ManifestObject
from scadbuddy.render.jobs import PlateLayout, plates_thumbnails
from scadbuddy.render.objects3mf import read_objects
from scadbuddy.render.split import ColourPart
from scadbuddy.store import sweep_blobs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from scadbuddy.workflows import outputs
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
from tests.test_objects3mf import at, bambu_project
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


async def test_a_painted_library_object_keeps_its_painting_in_the_output(tmp_path: Path) -> None:
    """#1965: a part painted in Bambu Studio is placed whole, its faces in their order,
    and its painting renumbered to the output's filaments: here green moves from
    extruder 2 to 1 and red, the part's own, from 1 to 3."""
    deps, paths = _deps(tmp_path)
    body = trimesh.creation.box(extents=(20, 10, 5))
    payload = bambu_project(
        items=[at(50, 50)],
        parts={1: (body, "normal_part", 1)},
        triangle_extra=' paint_color="8"',
    )
    read = read_objects(payload)
    box = bounding_box(read[0].parts)
    entry = ManifestObject(
        part=piece_key("cd" * 32, 0),
        file=read[0].name,
        slug="demo",
        revision=None,
        bbox=box,
        footprint=(box.size[0], box.size[1]),
        colours=["#FF0000", "#00FF00"],
        count=1,
        library_file_id=89,
    )
    await publish_library_pieces(deps.blobs, [entry], read)
    library = part_of(entry)
    planned = ["#00FF00", "#0000AA", "#FF0000"]
    req = OutputRequest(
        job_id="j7",
        index=0,
        slug="demo",
        layout=Layout(
            plates=[LayoutPlate(items=[Placed(piece_key=library.piece_key, x=0, y=0, rot=90.0)])]
        ),
        parts=[library],
        name=None,
        bom=[],
        files={},
        record=_record([library.piece_key]),
        provenance={entry.part: entry},
        colours=planned,
    )
    written = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert written.result.colors == planned
    # Green prints only where the part is painted, and the print dialog still asks for it.
    assert [(p.colour, p.extruder) for p in written.result.parts] == [
        ("#00FF00", 1),
        ("#FF0000", 3),
    ]
    model = paths.root / written.result.model_3mf
    with zipfile.ZipFile(model) as archive:
        objects = [n for n in archive.namelist() if n.startswith("3D/Objects/")]
        [painted] = [n for n in objects if b"paint_color" in archive.read(n)]
        text = archive.read(painted).decode()
        settings = archive.read("Metadata/model_settings.config").decode()
    assert text.count('paint_color="4"') == len(body.faces)
    assert 'paint_color="8"' not in text
    assert '<metadata key="extruder" value="3"/>' in settings
    # The output reads back as it was painted: a red part painted green.
    [again] = read_objects(model.read_bytes())
    [part] = again.parts
    assert part.colour == "#FF0000"
    assert part.paint is not None and part.paint.used() == ["#00FF00"]


async def test_a_negative_part_is_written_for_the_slicer_to_cut(tmp_path: Path) -> None:
    """A library object's negative part goes into the output as Bambu Studio's
    ``negative_part``, on no filament of its own, and nothing a person sees counts it:
    the colours, the parts, the box and the mesh checks are the body's."""
    from scadbuddy.render.geometry import analyze_3mf
    from tests.test_objects3mf import _cut_project

    deps, paths = _deps(tmp_path)
    read = read_objects(_cut_project())
    box = bounding_box(read[0].parts)
    entry = ManifestObject(
        part=piece_key("ef" * 32, 0),
        file=read[0].name,
        slug="demo",
        revision=None,
        bbox=box,
        footprint=(box.size[0], box.size[1]),
        colours=["#FF0000"],
        count=1,
        library_file_id=90,
    )
    await publish_library_pieces(deps.blobs, [entry], read)
    library = part_of(entry)
    req = OutputRequest(
        job_id="j8",
        index=0,
        slug="demo",
        layout=Layout(
            plates=[LayoutPlate(items=[Placed(piece_key=library.piece_key, x=0, y=0, rot=0.0)])]
        ),
        parts=[library],
        name=None,
        bom=[],
        files={},
        record=_record([library.piece_key]),
        provenance={entry.part: entry},
    )
    written = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert written.result.colors == ["#FF0000"]
    assert [p.colour for p in written.result.parts] == ["#FF0000"]
    assert tuple(round(v, 3) for v in written.result.bbox_mm.size) == (20, 10, 5)
    model = paths.root / written.result.model_3mf
    with zipfile.ZipFile(model) as archive:
        settings = archive.read("Metadata/model_settings.config").decode()
    assert settings.count('subtype="negative_part"') == 1
    assert settings.count('subtype="normal_part"') == 1
    analysis = analyze_3mf(model)
    assert len(analysis.parts) == 1
    # Arranged again, the output still carries the cut.
    [again] = read_objects(model.read_bytes())
    assert [p.negative for p in again.parts] == [False, True]


async def test_no_copy_is_packed_where_a_neighbours_negative_part_reaches(tmp_path: Path) -> None:
    """An arranged plate is one object to the slicer, which cuts a negative part out of
    all of it: a cut reaching past its body (x 15 to 35 on a 20 mm body) widens the
    footprint, so the next copy is packed beyond it, not inside it."""
    from scadbuddy.render.glb import footprint_box

    deps, _ = _deps(tmp_path)
    cutter = trimesh.creation.box(extents=(20, 4, 20))
    cutter.apply_translation((25, 5, 2))
    body = trimesh.creation.box(extents=(20, 10, 5))
    body.apply_translation((10, 5, 2.5))
    read = read_objects(
        bambu_project(
            items=[at(0, 0)],
            parts={1: (body, "normal_part", None), 2: (cutter, "negative_part", 1)},
        )
    )
    box = footprint_box(read[0].parts)
    assert round(box.size[0], 3) == 35 and round(box.size[2], 3) == 5
    entry = ManifestObject(
        part=piece_key("ab" * 32, 0),
        file=read[0].name,
        slug="demo",
        revision=None,
        bbox=box,
        footprint=(box.size[0], box.size[1]),
        colours=["#FF0000"],
        count=2,
        library_file_id=91,
    )
    await publish_library_pieces(deps.blobs, [entry], read)
    library = part_of(entry)
    layout = pack_layout(
        PackRequest(
            items=[PackItem(part=library, count=2)],
            plate=PlateSize(key="default", width=256.0, depth=256.0),
            allow_own=False,
        )
    )
    [plate] = layout.plates
    first, second = plate.items
    apart_x = abs(first.x - second.x) >= 35 and first.rot == second.rot == 0
    apart_y = abs(first.y - second.y) >= min(box.size[1], box.size[0])
    assert apart_x or apart_y


async def test_each_pieces_meshes_are_let_go_after_its_last_plate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2573: an arrange of many library pieces was OOM-killed holding every piece's
    meshes until the output was written. A piece is let go once its last copy is
    placed: the first piece is gone while the second plate is laid out, and when the
    covers are drawn."""
    deps, _ = _deps(tmp_path)
    entries, reads = [], []
    for index, colour in enumerate(["#FF0000", "#0000FF"]):
        out = tmp_path / f"piece-{index}.3mf"
        mesh = trimesh.creation.box(extents=(10, 10, 4 + index))
        write_bambu_3mf([ColourPart(1, "Body", colour, mesh)], out, thumbnails=None)
        read = read_objects(out.read_bytes())
        box = bounding_box(read[0].parts)
        entries.append(
            ManifestObject(
                part=piece_key(f"{index}a" * 32, 0),
                file=read[0].name,
                slug="demo",
                revision=None,
                bbox=box,
                footprint=(box.size[0], box.size[1]),
                colours=[colour],
                count=1,
                library_file_id=90 + index,
            )
        )
        reads.append(read[0])
    await publish_library_pieces(deps.blobs, entries, reads)
    first, second = (part_of(entry) for entry in entries)

    loaded: dict[str, weakref.ref[PlateLayout]] = {}
    alive_at_load: list[list[str]] = []
    real_load = PlateLayout.load

    def load(path: Path) -> PlateLayout:
        gc.collect()
        alive_at_load.append([key for key, ref in loaded.items() if ref() is not None])
        layout = real_load(path)
        loaded[path.parent.name] = weakref.ref(layout)
        return layout

    alive_at_covers: list[str] = []
    real_covers = plates_thumbnails

    async def covers(*args: Any, **kwargs: Any) -> Any:
        gc.collect()
        alive_at_covers.extend(key for key, ref in loaded.items() if ref() is not None)
        return await real_covers(*args, **kwargs)

    monkeypatch.setattr(PlateLayout, "load", staticmethod(load))
    monkeypatch.setattr(outputs, "plates_thumbnails", covers)
    req = OutputRequest(
        job_id="j9",
        index=0,
        slug="demo",
        layout=Layout(
            plates=[
                LayoutPlate(items=[Placed(piece_key=first.piece_key, x=0, y=0)]),
                LayoutPlate(items=[Placed(piece_key=second.piece_key, x=0, y=0)]),
            ]
        ),
        parts=[first, second],
        name=None,
        bom=[],
        files={},
        record=_record([first.piece_key, second.piece_key]),
        provenance={entry.part: entry for entry in entries},
    )
    written = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)

    assert len(written.result.plates or []) == 2
    assert alive_at_load == [[], []]
    assert first.piece_key not in alive_at_covers


def _library_piece(read: list[Any], key: str, file_id: int, count: int) -> tuple[Any, Any]:
    box = bounding_box(read[0].parts)
    entry = ManifestObject(
        part=piece_key(key * 32, 0),
        file=read[0].name,
        slug="demo",
        revision=None,
        bbox=box,
        footprint=(box.size[0], box.size[1]),
        colours=["#FF0000"],
        count=count,
        library_file_id=file_id,
    )
    return entry, part_of(entry)


async def _objects_of_output(
    tmp_path: Path, read: list[Any], copies: int, key: str
) -> list[set[str]]:
    """Each object of the written plate, as the subtypes of its parts."""
    deps, paths = _deps(tmp_path)
    entry, library = _library_piece(read, key, 92, copies)
    await publish_library_pieces(deps.blobs, [entry], read)
    req = OutputRequest(
        job_id="j9",
        index=0,
        slug="demo",
        layout=Layout(
            plates=[
                LayoutPlate(
                    items=[
                        Placed(piece_key=library.piece_key, x=40.0 * n, y=0, rot=0.0)
                        for n in range(copies)
                    ]
                )
            ]
        ),
        parts=[library],
        name=None,
        bom=[],
        files={},
        record=_record([library.piece_key]),
        provenance={entry.part: entry},
    )
    written = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    with zipfile.ZipFile(paths.root / written.result.model_3mf) as archive:
        [plate] = laid_out_plates(archive)
        config = ET.fromstring(archive.read("Metadata/model_settings.config"))
    subtypes = {
        obj.get("id"): {part.get("subtype") or "" for part in obj.findall("part")}
        for obj in config.iter("object")
    }
    return [subtypes[assembly] for assembly in plate.assembly_ids]


async def test_each_copy_of_a_cut_piece_is_an_object_with_its_own_cut(tmp_path: Path) -> None:
    """#2874 review: a packed copy with a negative part is its own object, and its cut
    goes with it, not into its neighbour's."""
    from tests.test_objects3mf import _cut_project

    objects = await _objects_of_output(tmp_path, read_objects(_cut_project()), 2, "cd")

    assert objects == [{"normal_part", "negative_part"}] * 2


async def test_a_pieces_declared_objects_stay_apart_in_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2874 review: a piece whose template declares two objects keeps them two in a
    packed plate, rather than joined into one per colour."""
    left = trimesh.creation.box(extents=(10, 10, 4))
    right = trimesh.creation.box(extents=(10, 10, 4))
    right.apply_translation((20, 0, 0))
    read = read_objects(
        bambu_project(
            items=[at(0, 0)],
            parts={1: (left, "normal_part", 1), 2: (right, "normal_part", 2)},
        )
    )
    loaded = PlateLayout.load

    def declared(path: Path) -> PlateLayout:
        layout = loaded(path)
        first = layout.plates[0]
        assert len(first.parts) == 2
        layout.plates[0] = replace(first, objects=(1, 2))
        return layout

    monkeypatch.setattr(PlateLayout, "load", staticmethod(declared))

    objects = await _objects_of_output(tmp_path, read, 1, "ce")

    assert len(objects) == 2


async def test_a_layout_plate_with_nothing_to_print_is_refused(tmp_path: Path) -> None:
    """#2874 review: skipped, an empty plate would renumber every plate after it
    against the layout, so it is refused loudly instead."""
    from tests.test_objects3mf import _cut_project

    deps, _ = _deps(tmp_path)
    read = read_objects(_cut_project())
    entry, library = _library_piece(read, "cf", 93, 1)
    await publish_library_pieces(deps.blobs, [entry], read)
    req = OutputRequest(
        job_id="j10",
        index=0,
        slug="demo",
        layout=Layout(
            plates=[
                LayoutPlate(items=[Placed(piece_key=library.piece_key, x=0, y=0, rot=0.0)]),
                LayoutPlate(items=[]),
            ]
        ),
        parts=[library],
        name=None,
        bom=[],
        files={},
        record=_record([library.piece_key]),
        provenance={entry.part: entry},
    )
    with pytest.raises(ApplicationError, match="plate 2 of the layout has nothing to print"):
        await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
