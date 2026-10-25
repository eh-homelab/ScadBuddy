"""#3637: the same object arranged twice, one copy in other colours, on one plate."""

from __future__ import annotations

import json
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import trimesh
from temporalio.testing import ActivityEnvironment

from scadbuddy.api import jobs as jobs_api
from scadbuddy.api.jobs import _job_status
from scadbuddy.api.outputs import ArrangeObject, ArrangeRequest, arrange_inputs
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.library_objects import LibraryObjects, piece_key, publish_library_pieces
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputStore
from scadbuddy.render.bambu3mf import PROJECT_SETTINGS_NAME, write_bambu_3mf
from scadbuddy.render.glb import BoundingBox, bounding_box
from scadbuddy.render.job_models import Job, ManifestObject, now
from scadbuddy.render.objects3mf import read_objects
from scadbuddy.render.split import ColourPart
from scadbuddy.template import Part
from scadbuddy.workflows.arrange import arrange, part_of
from scadbuddy.workflows.models import (
    ArrangeInputs,
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


def test_a_swap_pair_swaps_both_ways_at_once() -> None:
    item = PackItem(part=_part(), recolour={GREEN: BLACK, BLACK: GREEN})
    assert item.colours() == [BLACK, GREEN]
    part = _part()
    entry = _library_entry("ef", part.bbox, [GREEN, BLACK]).model_copy(
        update={"part": part.piece_key, "materials": {GREEN: "PLA", BLACK: "PETG"}}
    )
    req = OutputRequest(
        job_id="r4",
        index=0,
        slug="demo",
        layout=Layout(
            plates=[
                LayoutPlate(
                    items=[
                        Placed(
                            piece_key=part.piece_key,
                            x=0,
                            y=0,
                            recolour={GREEN: BLACK, BLACK: GREEN},
                        )
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
    # Each colour prints in the other's place, in the other's material: no conflict.
    assert _materials(req) == {BLACK: "PLA", GREEN: "PETG"}
    [manifest] = manifest_of(req)
    assert manifest.recolour == {GREEN: BLACK, BLACK: GREEN}


async def test_a_swap_pair_is_written_swapped(tmp_path: Path) -> None:
    deps, paths = _deps(tmp_path)
    out = tmp_path / "pair.3mf"
    write_bambu_3mf(
        [
            ColourPart(1, "Body", GREEN, trimesh.creation.box(extents=(20, 10, 4))),
            ColourPart(2, "Face", BLACK, trimesh.creation.box(extents=(6, 6, 9))),
        ],
        out,
        thumbnails=None,
    )
    read = read_objects(out.read_bytes())
    entry = _library_entry("ba", bounding_box(read[0].parts), [GREEN, BLACK])
    await publish_library_pieces(deps.blobs, [entry], read)
    part = part_of(entry)
    req = OutputRequest(
        job_id="r5",
        index=0,
        slug="demo",
        layout=Layout(
            plates=[
                LayoutPlate(
                    items=[
                        Placed(
                            piece_key=part.piece_key,
                            x=0,
                            y=0,
                            recolour={GREEN: BLACK, BLACK: GREEN},
                        )
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
        colours=[GREEN, BLACK],
    )
    written = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    # The body (green in the file, the 20 mm box) now prints black, the face green.
    [again] = read_objects((paths.root / written.result.model_3mf).read_bytes())
    by_width = {round(float(p.mesh.extents.max())): p.colour for p in again.parts}
    assert by_width == {20: BLACK, 9: GREEN}


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


@pytest.mark.parametrize("mirrored", [False, True])
async def test_a_painted_copy_is_painted_in_its_new_colour(tmp_path: Path, mirrored: bool) -> None:
    """A mirrored painting stays mirrored through the swap (#4015, #4075)."""
    deps, paths = _deps(tmp_path)
    body = trimesh.creation.box(extents=(20, 10, 5))
    payload = bambu_project(
        items=["-1 0 0 0 1 0 0 0 1 50 50 0" if mirrored else at(50, 50)],
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
    assert painted.paint.reversed is mirrored


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
    entries = manifest_of(req)
    assert [m.recolour for m in entries] == [{GREEN: MAGENTA}, {BLACK: MAGENTA}]
    # Each copy says why the file states no material for magenta.
    assert all(any(MAGENTA in note for note in m.notes) for m in entries)


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


async def test_a_colour_every_copy_swaps_away_takes_no_slot(tmp_path: Path) -> None:
    paths, meta, written = await saved_output(tmp_path, count=1)
    [entry] = written.manifest
    own = entry.colours[0].upper()
    body = ArrangeRequest(
        objects=[ArrangeObject(output_id=meta.id, part=entry.part, recolour={own: MAGENTA})]
    )
    _, inputs = arrange_inputs(OutputStore(paths), body, plate_model=None)
    assert MAGENTA in inputs.colours and own not in [c.upper() for c in inputs.colours]


GOLD = "#E4BD68"


def _with_a_gold_base(paths: DataPaths, output_id: str) -> None:
    """The output also holds a gold part, first in its colour list: the menorah's base."""
    directory = OutputStore(paths).directory(output_id)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    gold = {**manifest[0], "part": "gold-base", "colours": [GOLD]}
    (directory / "manifest.json").write_text(json.dumps([gold, *manifest]), encoding="utf-8")
    stored = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    stored["colors"] = [GOLD, *stored["colors"]]
    (directory / "meta.json").write_text(json.dumps(stored), encoding="utf-8")


async def test_a_subset_takes_only_the_colours_its_parts_print_in(tmp_path: Path) -> None:
    """#4636: the menorah's candles arranged without its gold base kept all ten of the
    source's colours, gold first, so the order item had a slot no plate prints and its
    slice quote refused it. A subset's colours are those its copies print in, in the
    source's order."""
    paths, meta, written = await saved_output(tmp_path, count=1)
    [entry] = written.manifest
    own = entry.colours[0].upper()
    _with_a_gold_base(paths, meta.id)
    store = OutputStore(paths)
    subset = ArrangeRequest(objects=[ArrangeObject(output_id=meta.id, part=entry.part)])
    _, inputs = arrange_inputs(store, subset, plate_model=None)
    assert [c.upper() for c in inputs.colours] == [own]
    whole = ArrangeRequest(objects=[ArrangeObject(output_id=meta.id)])
    _, inputs = arrange_inputs(store, whole, plate_model=None)
    assert [c.upper() for c in inputs.colours] == [GOLD, own]


async def test_a_subsets_plan_keeps_each_spool_on_its_colour(tmp_path: Path) -> None:
    """#4636: with no colours given, a plan names slots in the source's colour order, so
    a slot the subset drops takes its spool with it and the rest follow their colours."""
    paths, meta, written = await saved_output(tmp_path, count=1)
    [entry] = written.manifest
    _with_a_gold_base(paths, meta.id)
    body = ArrangeRequest(
        objects=[ArrangeObject(output_id=meta.id, part=entry.part)],
        filament_plan=FilamentPlan.model_validate(
            {"slots": [{"slot_id": 1, "spool_id": 7}, {"slot_id": 2, "spool_id": 9}]}
        ),
    )
    _, inputs = arrange_inputs(OutputStore(paths), body, plate_model=None)
    assert inputs.filament_plan is not None
    assert inputs.filament_plan.slots == {1: 9}
    # The dropped slot's spool is said, not lost without a word (#4684 review).
    assert inputs.notes == [
        f"filament_plan slot 1 (spool 7) is {GOLD}, which no copy prints in: that spool is not used"
    ]
    assert inputs.notes == _job_status(_arrange_job(inputs), None).warnings


def _arrange_job(inputs: ArrangeInputs) -> Job:
    return Job(
        id="j",
        slug="demo",
        kind="arrange",
        inputs=inputs.model_dump(mode="json"),
        created_at=now(),
    )


async def test_a_plan_with_a_recolour_follows_the_colours_left(tmp_path: Path) -> None:
    """#4684 review: a recolour and a plan with no colours given. The source's colour is
    swapped away (#3637), so its slot and its spool go, and the new colour's slot,
    numbered after it in the source's order, becomes slot 1."""
    paths, meta, written = await saved_output(tmp_path, count=1)
    [entry] = written.manifest
    own = entry.colours[0].upper()
    body = ArrangeRequest(
        objects=[ArrangeObject(output_id=meta.id, part=entry.part, recolour={own: MAGENTA})],
        filament_plan=FilamentPlan.model_validate(
            {"slots": [{"slot_id": 1, "spool_id": 7}, {"slot_id": 2, "spool_id": 9}]}
        ),
    )
    _, inputs = arrange_inputs(OutputStore(paths), body, plate_model=None)
    assert inputs.colours == [MAGENTA]
    assert inputs.filament_plan is not None
    assert inputs.filament_plan.slots == {1: 9}
    assert inputs.notes == [
        f"filament_plan slot 1 (spool 7) is {own}, which no copy prints in: that spool is not used"
    ]


async def test_a_planned_slot_past_every_colour_is_said(tmp_path: Path) -> None:
    """#4702 review: a plan slot past the colour list names no colour; its spool goes
    unused, and a note says so rather than dropping it without a word."""
    paths, meta, written = await saved_output(tmp_path, count=1)
    [entry] = written.manifest
    body = ArrangeRequest(
        objects=[ArrangeObject(output_id=meta.id, part=entry.part)],
        filament_plan=FilamentPlan.model_validate(
            {
                "slots": [
                    {"slot_id": 0, "spool_id": 4},
                    {"slot_id": 1, "spool_id": 7},
                    {"slot_id": 5, "spool_id": 9},
                ]
            }
        ),
    )
    _, inputs = arrange_inputs(OutputStore(paths), body, plate_model=None)
    assert inputs.filament_plan is not None
    assert inputs.filament_plan.slots == {1: 7}
    # Below the list as above it (#4704 review): slot 0 names no colour either.
    assert inputs.notes == [
        "filament_plan slot 0 (spool 4) names no colour: that spool is not used",
        "filament_plan slot 5 (spool 9) names no colour: that spool is not used",
    ]


def test_a_jobs_status_reads_the_notes_field_with_its_own_type() -> None:
    """`api/jobs.py` reads `ArrangeInputs.notes` alone, by name and as `list[str]`."""
    assert jobs_api._NOTES_FIELD in ArrangeInputs.model_fields
    assert ArrangeInputs.model_fields[jobs_api._NOTES_FIELD].annotation == list[str]


@pytest.fixture(autouse=True)
def _no_logged_jobs() -> Iterator[None]:
    """Each test starts with no job's unreadable notes logged (`api/jobs.py` keeps the
    record per process), so an id one test logs never decides another's (#4709 review)."""
    jobs_api._UNREADABLE_LOGGED.clear()
    yield
    jobs_api._UNREADABLE_LOGGED.clear()


def _unreadable(job_id: str) -> Job:
    return Job(
        id=job_id, slug="demo", kind="arrange", inputs={"notes": "not a list"}, created_at=now()
    )


def test_a_job_forgotten_from_the_record_logs_again(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#4709 review: the record is bounded; the oldest job is forgotten first, and a
    forgotten job's unreadable notes are logged again."""
    monkeypatch.setattr(jobs_api, "_UNREADABLE_LOGGED_MAX", 1)
    with caplog.at_level("WARNING", logger="scadbuddy.api.jobs"):
        for job_id in ("first", "second", "first"):
            _job_status(_unreadable(job_id), None)
    logged = [getattr(r, "job_id", None) for r in caplog.records if r.name == "scadbuddy.api.jobs"]
    assert logged == ["first", "second", "first"]
    assert list(jobs_api._UNREADABLE_LOGGED) == ["first"]


def test_an_arrange_rows_unreadable_notes_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    """#4704 review: notes a row holds in a shape this build cannot read are not said,
    and the drift is logged with the job's id rather than lost without a trace."""
    job_id = uuid.uuid4().hex
    job = Job(
        id=job_id, slug="demo", kind="arrange", inputs={"notes": "not a list"}, created_at=now()
    )
    with caplog.at_level("WARNING", logger="scadbuddy.api.jobs"):
        assert _job_status(job, None).warnings is None
        # Polled again: logged once per job, not on every poll (#4707 review).
        assert _job_status(job, None).warnings is None
    [record] = [r for r in caplog.records if r.name == "scadbuddy.api.jobs"]
    assert record.getMessage() == "an arrange job's notes are unreadable"
    assert getattr(record, "job_id", None) == job_id


def test_a_rows_notes_are_said_though_its_other_fields_are_unreadable() -> None:
    """#4707 review: only `notes` is read, so a row whose items this build cannot read
    as an arrange's still says its notes."""
    job = Job(
        id="j10",
        slug="demo",
        kind="arrange",
        inputs={"items": "not a list", "notes": ["slot 5 names no colour"]},
        created_at=now(),
    )
    assert _job_status(job, None).warnings == ["slot 5 names no colour"]
