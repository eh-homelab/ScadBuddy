"""#3637: the same object arranged twice, one copy in other colours, on one plate."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
import trimesh
from temporalio.testing import ActivityEnvironment

from scadbuddy.api.outputs import ArrangeObject, ArrangeRequest, arrange_inputs
from scadbuddy.bambuddy.library_objects import LibraryObjects, piece_key, publish_library_pieces
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputStore
from scadbuddy.render.bambu3mf import PROJECT_SETTINGS_NAME, write_bambu_3mf
from scadbuddy.render.glb import BoundingBox, bounding_box
from scadbuddy.render.job_models import ManifestObject
from scadbuddy.render.objects3mf import read_objects
from scadbuddy.render.split import ColourPart
from scadbuddy.template import Part
from scadbuddy.workflows.arrange import arrange, part_of
from scadbuddy.workflows.models import (
    Layout,
    LayoutPlate,
    OutputRequest,
    PackItem,
    PackRequest,
    Placed,
    PlateSize,
)
from scadbuddy.workflows.outputs import _materials, manifest_of
from scadbuddy.workflows.pipeline_activities import PipelineActivities, pack_layout
from tests.support.arrange import saved_output
from tests.test_objects3mf import at, bambu_project
from tests.test_packing_and_outputs import _deps, _record

GREEN, BLACK, MAGENTA = "#2E8B57", "#000000", "#FF00FF"
PLATE = PlateSize(key="default", width=256.0, depth=256.0)


def _part(key: str = "lib-a", colours: list[str] | None = None) -> Part:
    return Part(
        piece_key=key,
        file="creeper",
        bbox=BoundingBox(min=(0, 0, 0), max=(30, 20, 5), size=(30, 20, 5)),
        colours=colours or [GREEN, BLACK],
    )


def test_a_recoloured_copy_prints_in_its_own_colours() -> None:
    item = PackItem(part=_part(), recolour={GREEN.lower(): MAGENTA})
    assert item.colours() == [MAGENTA, BLACK]
    # Two colours swapped to one are one filament.
    assert PackItem(part=_part(), recolour={GREEN: BLACK}).colours() == [BLACK]


def test_both_colourings_share_one_plate_and_each_copy_keeps_its_own() -> None:
    part = _part()
    layout = arrange(
        [PackItem(part=part), PackItem(part=part, recolour={GREEN: MAGENTA})],
        PLATE,
        allow_own=False,
    )
    [plate] = layout.plates
    assert [p.recolour for p in plate.items] == [{}, {GREEN: MAGENTA}]
    assert {p.piece_key for p in plate.items} == {part.piece_key}


def test_by_colour_tells_the_two_colourings_apart() -> None:
    part = _part()
    layout = arrange(
        [PackItem(part=part), PackItem(part=part, recolour={GREEN: MAGENTA})],
        PLATE,
        goal="by_colour",
    )
    assert [[p.recolour for p in plate.items] for plate in layout.plates] == [
        [{}],
        [{GREEN: MAGENTA}],
    ]


def _library_entry(deps_blobs_key: str, box: BoundingBox, colours: list[str]) -> ManifestObject:
    return ManifestObject(
        part=piece_key(deps_blobs_key * 32, 0),
        file="creeper",
        slug="",
        revision=None,
        bbox=box,
        footprint=(box.size[0], box.size[1]),
        colours=colours,
        materials={c: "PLA" for c in colours},
        count=1,
        library_file_id=42,
    )


async def test_the_written_file_has_a_slot_for_the_new_colour(tmp_path: Path) -> None:
    deps, paths = _deps(tmp_path)
    out = tmp_path / "creeper.3mf"
    write_bambu_3mf(
        [
            ColourPart(1, "Body", GREEN, trimesh.creation.box(extents=(20, 10, 4))),
            ColourPart(2, "Face", BLACK, trimesh.creation.box(extents=(6, 6, 9))),
        ],
        out,
        thumbnails=None,
    )
    read = read_objects(out.read_bytes())
    entry = _library_entry("ab", bounding_box(read[0].parts), [GREEN, BLACK])
    await publish_library_pieces(deps.blobs, [entry], read)
    part = part_of(entry)
    swap = {GREEN: MAGENTA}
    layout = pack_layout(
        PackRequest(
            items=[PackItem(part=part), PackItem(part=part, recolour=swap)],
            plate=PLATE,
            colours=[GREEN, BLACK, MAGENTA],
            allow_own=False,
        )
    )
    assert len(layout.plates) == 1
    req = OutputRequest(
        job_id="r1",
        index=0,
        slug="demo",
        layout=layout,
        parts=[part],
        name=None,
        bom=[],
        files={},
        record=_record([part.piece_key]),
        provenance={entry.part: entry},
        colours=[GREEN, BLACK, MAGENTA],
    )
    written = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert written.result.colors == [GREEN, BLACK, MAGENTA]
    assert {(p.colour, p.extruder) for p in written.result.parts} == {
        (GREEN, 1),
        (BLACK, 2),
        (MAGENTA, 3),
    }
    with zipfile.ZipFile(paths.root / written.result.model_3mf) as archive:
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    assert [c.upper() for c in settings["filament_colour"]] == [GREEN, BLACK, MAGENTA]
    # Magenta replaces a PLA green, so it is PLA too.
    assert settings["filament_type"] == ["PLA", "PLA", "PLA"]
    # Arranged again, each copy keeps its colours: one entry per colouring.
    assert sorted(((m.count, m.recolour) for m in written.manifest), key=lambda e: len(e[1])) == [
        (1, {}),
        (1, swap),
    ]


async def test_a_painted_copy_is_painted_in_its_new_colour(tmp_path: Path) -> None:
    deps, paths = _deps(tmp_path)
    body = trimesh.creation.box(extents=(20, 10, 5))
    payload = bambu_project(
        items=[at(50, 50)],
        parts={1: (body, "normal_part", 1)},
        triangle_extra=' paint_color="8"',
    )
    read = read_objects(payload)
    entry = _library_entry("cd", bounding_box(read[0].parts), ["#FF0000", "#00FF00"])
    await publish_library_pieces(deps.blobs, [entry], read)
    part = part_of(entry)
    req = OutputRequest(
        job_id="r2",
        index=0,
        slug="demo",
        layout=Layout(
            plates=[
                LayoutPlate(
                    items=[
                        Placed(piece_key=part.piece_key, x=0, y=0, recolour={"#00FF00": MAGENTA})
                    ]
                )
            ]
        ),
        parts=[part],
        name=None,
        bom=[],
        files={},
        record=_record([part.piece_key]),
        provenance={entry.part: entry},
    )
    written = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert "#00FF00" not in written.result.colors and MAGENTA in written.result.colors
    [again] = read_objects((paths.root / written.result.model_3mf).read_bytes())
    [painted] = again.parts
    assert painted.colour == "#FF0000"
    assert painted.paint is not None and painted.paint.used() == [MAGENTA]


def test_a_swap_the_sources_disagree_on_states_no_material() -> None:
    part = _part()
    entry = _library_entry("ef", part.bbox, [GREEN, BLACK]).model_copy(
        update={"part": part.piece_key, "materials": {GREEN: "PLA", BLACK: "PETG"}}
    )
    req = OutputRequest(
        job_id="r3",
        index=0,
        slug="demo",
        layout=Layout(
            plates=[
                LayoutPlate(
                    items=[
                        Placed(piece_key=part.piece_key, x=0, y=0, recolour={GREEN: MAGENTA}),
                        Placed(piece_key=part.piece_key, x=40, y=0, recolour={BLACK: MAGENTA}),
                    ]
                )
            ]
        ),
        parts=[part],
        name=None,
        bom=[],
        files={},
        record=_record([]),
        provenance={part.piece_key: entry},
    )
    assert _materials(req) == {GREEN: "PLA", BLACK: "PETG"}
    assert [m.recolour for m in manifest_of(req)] == [{GREEN: MAGENTA}, {BLACK: MAGENTA}]


def _library(file_id: int = 42) -> dict[int, LibraryObjects]:
    part = _part()
    entry = _library_entry("aa", part.bbox, [GREEN, BLACK]).model_copy(
        update={"library_file_id": file_id}
    )
    return {file_id: LibraryObjects(file_id=file_id, filename="creepers.3mf", objects=[entry])}


def test_a_library_file_twice_once_recoloured_is_two_items(tmp_path: Path) -> None:
    library = _library()
    [entry] = library[42].objects
    body = ArrangeRequest(
        objects=[
            ArrangeObject(library_file_id=42, part=entry.part, count=1),
            ArrangeObject(
                library_file_id=42, part=entry.part, count=1, recolour={GREEN.lower(): MAGENTA}
            ),
        ],
        slug="demo",
    )
    _, inputs = arrange_inputs(
        OutputStore(DataPaths(tmp_path)), body, plate_model=None, library=library
    )
    assert [(i.count, i.recolour) for i in inputs.items] == [(1, {}), (1, {GREEN: MAGENTA})]
    assert inputs.colours == [GREEN, BLACK, MAGENTA]


def test_a_colour_the_object_does_not_print_in_is_refused(tmp_path: Path) -> None:
    library = _library()
    [entry] = library[42].objects
    body = ArrangeRequest(
        objects=[ArrangeObject(library_file_id=42, part=entry.part, recolour={"#123456": MAGENTA})],
        slug="demo",
    )
    with pytest.raises(ApiError) as raised:
        arrange_inputs(OutputStore(DataPaths(tmp_path)), body, plate_model=None, library=library)
    assert raised.value.status == 422
    assert "does not print in #123456" in raised.value.detail


def test_a_swap_to_itself_is_no_swap() -> None:
    obj = ArrangeObject(library_file_id=1, recolour={GREEN.lower(): GREEN, BLACK: MAGENTA})
    assert obj.recolour == {BLACK: MAGENTA}


async def test_an_arranged_outputs_colourings_are_chosen_by_their_colours(tmp_path: Path) -> None:
    """An output holding one part twice, once recoloured: naming the part without
    colours takes both as they are; naming the colours takes that one."""
    paths, meta, written = await saved_output(tmp_path, count=1)
    [entry] = written.manifest
    own = entry.colours[0].upper()
    path = OutputStore(paths).directory(meta.id) / "manifest.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(
        json.dumps([*entries, {**entries[0], "count": 2, "recolour": {own: MAGENTA}}]),
        encoding="utf-8",
    )
    store = OutputStore(paths)
    both = ArrangeRequest(objects=[ArrangeObject(output_id=meta.id, part=entry.part)])
    _, inputs = arrange_inputs(store, both, plate_model=None)
    assert [(i.count, i.recolour) for i in inputs.items] == [(1, {}), (2, {own: MAGENTA})]
    assert MAGENTA in inputs.colours
    one = ArrangeRequest(
        objects=[ArrangeObject(output_id=meta.id, part=entry.part, recolour={own: MAGENTA})]
    )
    _, inputs = arrange_inputs(store, one, plate_model=None)
    assert [(i.count, i.recolour) for i in inputs.items] == [(2, {own: MAGENTA})]
    plain = ArrangeRequest(objects=[ArrangeObject(output_id=meta.id, part=entry.part, recolour={})])
    _, inputs = arrange_inputs(store, plain, plate_model=None)
    assert [(i.count, i.recolour) for i in inputs.items] == [(1, {})]
