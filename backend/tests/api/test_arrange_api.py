"""POST /outputs/arrange (spec 2026-09-27 §10)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import respx
import trimesh
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState, get_render
from scadbuddy.api.outputs import (
    LIBRARY_FILE_NOT_ARRANGEABLE,
    NEEDS_BACKFILL_PROBLEM,
    ArrangeObject,
    ArrangeRequest,
    arrange_inputs,
)
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import SlotChoice
from scadbuddy.bambuddy.uploads import LibraryCopy
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import META_NAME, OutputMeta, OutputStore
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.job_models import Job, now
from scadbuddy.render.jobs import LAYOUT_NAME
from scadbuddy.render.split import ColourPart
from scadbuddy.tools.export_openapi import export
from scadbuddy.workflows.models import ArrangeInputs
from tests.api.test_print_library import library_file
from tests.api.test_send import configure
from tests.support.arrange import copied_output, saved_output

#: A well-formed output id no output has: each test fails for its own reason, not the id's.
UNKNOWN = "0" * 32


def _rewrite_manifest(paths: DataPaths, meta: OutputMeta, **update: object) -> None:
    """Change every manifest entry of a saved output on disk."""
    path = OutputStore(paths).directory(meta.id) / "manifest.json"
    entries = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps([{**e, **update} for e in entries]), encoding="utf-8")


async def test_objects_become_pack_items_with_their_provenance(tmp_path: Path) -> None:
    paths, meta, written = await saved_output(tmp_path, count=3)
    key = written.manifest[0].part
    body = ArrangeRequest(
        objects=[ArrangeObject(output_id=meta.id, part=key, count=5, group="g")],
        goal="by_colour",
        filament_plan=FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=9)]),
        name="more",
    )
    slug, inputs = arrange_inputs(OutputStore(paths), body, plate_model=None)
    assert slug == "demo"
    [item] = inputs.items
    assert (item.part.piece_key, item.count, item.group) == (key, 5, "g")
    assert inputs.goal == "by_colour" and inputs.plate.key == "default"
    assert inputs.filament_plan is not None and inputs.filament_plan.slots == {1: 9}
    assert inputs.colours == written.result.colors  # the source's filament order by default
    assert inputs.provenance[key].source_output == meta.id
    assert inputs.sources == [meta.id] and inputs.name == "more"


async def test_a_part_not_in_the_output_is_refused(tmp_path: Path) -> None:
    paths, meta, _ = await saved_output(tmp_path)
    body = ArrangeRequest(objects=[ArrangeObject(output_id=meta.id, part="nope", count=1)])
    with pytest.raises(ApiError) as raised:
        arrange_inputs(OutputStore(paths), body, plate_model=None)
    assert raised.value.status == 422 and "has no object nope" in raised.value.detail


async def test_an_output_without_a_manifest_is_refused_up_front(tmp_path: Path) -> None:
    paths, meta, _ = await saved_output(tmp_path)
    (OutputStore(paths).directory(meta.id) / "manifest.json").unlink()
    body = ArrangeRequest(objects=[ArrangeObject(output_id=meta.id, part="x", count=1)])
    with pytest.raises(ApiError) as raised:
        arrange_inputs(OutputStore(paths), body, plate_model=None)
    assert raised.value.status == 409 and raised.value.type == NEEDS_BACKFILL_PROBLEM
    assert raised.value.extensions == {"code": "needs_backfill", "output_ids": [meta.id]}


async def test_nothing_to_place_is_refused(tmp_path: Path) -> None:
    paths, meta, written = await saved_output(tmp_path)
    body = ArrangeRequest(
        objects=[ArrangeObject(output_id=meta.id, part=written.manifest[0].part, count=0)]
    )
    with pytest.raises(ApiError) as raised:
        arrange_inputs(OutputStore(paths), body, plate_model=None)
    assert raised.value.status == 422 and "nothing to arrange" in raised.value.detail


async def test_an_object_with_its_own_plates_is_refused_by_name(tmp_path: Path) -> None:
    """The packer refuses a multi-plate Part when it shares (allow_own=False): say so
    here, naming it, rather than in a job that fails on a worker."""
    paths, meta, written = await saved_output(tmp_path)
    _rewrite_manifest(paths, meta, plates=3)
    key = written.manifest[0].part
    body = ArrangeRequest(objects=[ArrangeObject(output_id=meta.id, part=key, count=1)])
    with pytest.raises(ApiError) as raised:
        arrange_inputs(OutputStore(paths), body, plate_model=None)
    assert raised.value.status == 422
    assert f"object {key}" in raised.value.detail and "model.scad" in raised.value.detail
    assert "its own 3 plates" in raised.value.detail


async def test_a_planned_slot_no_part_uses_keeps_its_place(tmp_path: Path) -> None:
    """An arranged output's `colors` can name a slot no part uses (Task 3), so `colors`
    and `parts` differ in length: the default order is the slots', by position, and
    never paired with `parts` by index."""
    paths, meta, written = await saved_output(tmp_path)
    own = written.result.colors[0]
    store = OutputStore(paths)
    record = store.directory(meta.id) / META_NAME
    data = json.loads(record.read_text(encoding="utf-8"))
    data["colors"] = ["#123456", own]
    data["parts"] = [{**data["parts"][0], "extruder": 2}]
    record.write_text(json.dumps(data), encoding="utf-8")
    body = ArrangeRequest(
        objects=[ArrangeObject(output_id=meta.id, part=written.manifest[0].part, count=1)]
    )
    _, inputs = arrange_inputs(store, body, plate_model=None)
    assert inputs.colours == ["#123456", own]


async def test_a_printer_model_sets_the_plate(tmp_path: Path) -> None:
    paths, meta, written = await saved_output(tmp_path)
    body = ArrangeRequest(
        objects=[ArrangeObject(output_id=meta.id, part=written.manifest[0].part, count=2)]
    )
    _, inputs = arrange_inputs(OutputStore(paths), body, plate_model="H2C")
    assert inputs.plate.key == "Bambu Lab H2C" and inputs.plate_model == "H2C"


def test_an_unknown_output_is_a_404(client: TestClient) -> None:
    response = client.post(
        "/api/v1/outputs/arrange",
        json={"objects": [{"output_id": UNKNOWN, "part": "p", "count": 1}]},
    )
    assert response.status_code == 404


def test_an_unknown_goal_is_a_422(client: TestClient) -> None:
    response = client.post(
        "/api/v1/outputs/arrange",
        json={"objects": [{"output_id": UNKNOWN, "part": "p", "count": 1}], "goal": "prettiest"},
    )
    assert response.status_code == 422


def test_more_than_2000_copies_is_a_422(client: TestClient) -> None:
    objects = [{"output_id": UNKNOWN, "part": f"p{i}", "count": 500} for i in range(5)]
    response = client.post("/api/v1/outputs/arrange", json={"objects": objects})
    assert response.status_code == 422
    assert "2500 copies" in response.text and "2000" in response.text


def test_2000_copies_is_allowed() -> None:
    objects = [ArrangeObject(output_id=UNKNOWN, part=f"p{i}", count=500) for i in range(4)]
    assert sum(o.count or 0 for o in ArrangeRequest(objects=objects).objects) == 2000


class Arranger:
    """A render service whose `arrange` records what the route resolved, as
    RenderService would insert it."""

    def __init__(self) -> None:
        self.inputs: list[ArrangeInputs] = []

    async def arrange(self, slug: str, inputs: ArrangeInputs) -> Job:
        self.inputs.append(inputs)
        return Job(
            id="arr-1",
            slug=slug,
            state="pending",
            created_at=now(),
            kind="arrange",
            inputs=inputs.model_dump(mode="json"),
        )


def test_with_no_printer_the_plate_falls_back_to_the_stored_default(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    _, meta, written = asyncio.run(saved_output(tmp_path))
    arranger = Arranger()
    app.dependency_overrides[get_render] = lambda: arranger
    assert client.put("/api/v1/settings", json={"default_plate": "H2C"}).status_code == 200
    response = client.post(
        "/api/v1/outputs/arrange",
        json={"objects": [{"output_id": meta.id, "part": written.manifest[0].part, "count": 2}]},
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "pending"
    [inputs] = arranger.inputs
    assert inputs.plate_model == "H2C" and inputs.plate.key == "Bambu Lab H2C"


def test_an_arrange_too_large_to_start_is_a_413_before_any_job(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    """As a render's submit: Temporal refuses an input that large outright, so the
    request is refused before a row exists that could never start."""
    paths, meta, written = asyncio.run(saved_output(tmp_path))
    _rewrite_manifest(paths, meta, notes=["x" * (1024 * 1024)])
    state: AppState = getattr(app.state, STATE_ATTR)
    before = state.render.store.counts()
    response = client.post(
        "/api/v1/outputs/arrange",
        json={"objects": [{"output_id": meta.id, "part": written.manifest[0].part, "count": 1}]},
    )
    assert response.status_code == 413, response.text
    assert "the most a job can carry is" in response.text
    assert state.render.store.counts() == before


def test_an_output_id_that_is_not_an_id_is_a_422_before_any_job(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    """The id names a directory to look up: "*" would glob to whichever output comes
    first and record it in the arranged output's `arranged_from`."""
    _, _, written = asyncio.run(saved_output(tmp_path))
    state: AppState = getattr(app.state, STATE_ATTR)
    before = state.render.store.counts()
    response = client.post(
        "/api/v1/outputs/arrange",
        json={"objects": [{"output_id": "*", "part": written.manifest[0].part, "count": 1}]},
    )
    assert response.status_code == 422, response.text
    assert state.render.store.counts() == before


def test_the_needs_backfill_refusal_is_typed_in_the_spec(tmp_path: Path) -> None:
    """#902: the frontend's generated client types the 409's body, not a hand copy."""
    schema = json.loads(export(tmp_path / "openapi.json").read_text(encoding="utf-8"))
    conflict = schema["paths"]["/api/v1/outputs/arrange"]["post"]["responses"]["409"]
    body = conflict["content"]["application/problem+json"]["schema"]
    assert body == {"$ref": "#/components/schemas/NeedsBackfillProblem"}
    problem = schema["components"]["schemas"]["NeedsBackfillProblem"]
    assert {"type", "title", "status", "detail", "code", "output_ids"} <= set(problem["required"])
    assert problem["properties"]["code"]["const"] == "needs_backfill"


#: Output ids for the copies a test makes of its one rendered output (#1864).
SECOND = "b" * 32
THIRD = "c" * 32


async def test_the_result_is_filed_under_the_chosen_template(tmp_path: Path) -> None:
    """#1864: outputs of two templates arrange together; the result is the first
    object's template unless `slug` names another of theirs."""
    paths, meta, written = await saved_output(tmp_path)
    other = copied_output(paths, meta, slug="other", output_id=SECOND)
    key = written.manifest[0].part
    objects = [
        ArrangeObject(output_id=meta.id, part=key, count=1),
        ArrangeObject(output_id=other.id, part=key, count=1),
    ]
    store = OutputStore(paths)
    assert arrange_inputs(store, ArrangeRequest(objects=objects), plate_model=None)[0] == "demo"
    chosen = ArrangeRequest(objects=objects, slug="other")
    slug, inputs = arrange_inputs(store, chosen, plate_model=None)
    assert slug == "other" and inputs.sources == [meta.id, other.id]
    with pytest.raises(ApiError) as raised:
        arrange_inputs(store, ArrangeRequest(objects=objects, slug="third"), plate_model=None)
    assert raised.value.status == 422 and "third" in raised.value.detail


async def test_an_object_without_a_part_is_every_object_of_its_output(tmp_path: Path) -> None:
    """The agent names a source, not its manifest: `part` omitted places every object,
    each at its own count unless `count` says otherwise."""
    paths, meta, written = await saved_output(tmp_path, count=3)
    key = written.manifest[0].part
    store = OutputStore(paths)
    _, inputs = arrange_inputs(
        store, ArrangeRequest(objects=[ArrangeObject(output_id=meta.id)]), plate_model=None
    )
    assert [(i.part.piece_key, i.count) for i in inputs.items] == [(key, 3)]
    _, inputs = arrange_inputs(
        store,
        ArrangeRequest(objects=[ArrangeObject(output_id=meta.id, count=5)]),
        plate_model=None,
    )
    assert [i.count for i in inputs.items] == [5]


async def test_the_copy_cap_holds_once_omitted_counts_are_read(tmp_path: Path) -> None:
    paths, meta, _ = await saved_output(tmp_path, count=3)
    _rewrite_manifest(paths, meta, count=500)
    body = ArrangeRequest(objects=[ArrangeObject(output_id=meta.id)] * 5)
    with pytest.raises(ApiError) as raised:
        arrange_inputs(OutputStore(paths), body, plate_model=None)
    assert raised.value.status == 422 and "2500 copies" in raised.value.detail


def test_an_object_names_one_source(client: TestClient) -> None:
    for source in ({}, {"output_id": UNKNOWN, "library_file_id": 7}):
        response = client.post(
            "/api/v1/outputs/arrange", json={"objects": [{**source, "part": "p", "count": 1}]}
        )
        assert response.status_code == 422, response.text


def test_two_templates_and_a_library_file_arrange_together(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    """#1864: a library file ScadBuddy uploaded arranges through the output it is a copy
    of (`output_bambuddy_uploads`), beside outputs of two templates."""
    paths, meta, written = asyncio.run(saved_output(tmp_path))
    other = copied_output(paths, meta, slug="other", output_id=SECOND)
    uploaded = copied_output(paths, meta, slug="demo", output_id=THIRD)
    state: AppState = getattr(app.state, STATE_ATTR)
    asyncio.run(
        state.uploads.record(uploaded.id, LibraryCopy(id=77, folder_id=None, target_key="H2D"))
    )
    arranger = Arranger()
    app.dependency_overrides[get_render] = lambda: arranger
    key = written.manifest[0].part
    response = client.post(
        "/api/v1/outputs/arrange",
        json={
            "objects": [
                {"output_id": meta.id, "part": key, "count": 1},
                {"output_id": other.id, "part": key, "count": 2},
                {"library_file_id": 77},
            ],
            "slug": "other",
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["slug"] == "other"
    [inputs] = arranger.inputs
    assert inputs.sources == [meta.id, other.id, uploaded.id]
    assert [i.count for i in inputs.items] == [1, 2, 2]


def test_a_library_file_whose_output_was_deleted_is_a_404_naming_the_file(
    client: TestClient, app: FastAPI
) -> None:
    state: AppState = getattr(app.state, STATE_ATTR)
    asyncio.run(state.uploads.record(UNKNOWN, LibraryCopy(id=78, folder_id=None, target_key="k")))
    response = client.post("/api/v1/outputs/arrange", json={"objects": [{"library_file_id": 78}]})
    assert response.status_code == 404, response.text
    assert "library file 78" in response.json()["detail"]


def two_colour_3mf(tmp_path: Path) -> bytes:
    """A two-colour project, as Bambuddy's library holds one ScadBuddy did not make."""
    red = trimesh.creation.box(extents=(10, 10, 4))
    blue = trimesh.creation.box(extents=(6, 6, 9))
    out = tmp_path / "plain.3mf"
    write_bambu_3mf(
        [ColourPart(1, "Red", "#FF0000", red), ColourPart(2, "Blue", "#0000FF", blue)],
        out,
        thumbnails=None,
        model_name="plain",
    )
    return out.read_bytes()


@respx.mock
def test_a_plain_library_file_arranges_beside_an_output(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    """#1863: a file no output records is read from its 3MF, its objects stored as
    pieces the job places and holds."""
    configure(client)
    _, meta, written = asyncio.run(saved_output(tmp_path))
    library_file(88, content=two_colour_3mf(tmp_path))
    arranger = Arranger()
    app.dependency_overrides[get_render] = lambda: arranger
    response = client.post(
        "/api/v1/outputs/arrange",
        json={
            "objects": [
                {"output_id": meta.id, "part": written.manifest[0].part, "count": 1},
                {"library_file_id": 88, "count": 3},
            ]
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["slug"] == "demo"
    [inputs] = arranger.inputs
    [_, item] = inputs.items
    key = item.part.piece_key
    assert key.startswith("lib1-") and item.count == 3
    assert item.part.colours == ["#FF0000", "#0000FF"]
    assert "#FF0000" in inputs.colours and "#0000FF" in inputs.colours
    assert inputs.provenance[key].library_file_id == 88
    assert inputs.provenance[key].slug == "demo"
    assert inputs.sources == [meta.id]
    state: AppState = getattr(app.state, STATE_ATTR)
    assert (state.store.blobs.dir_for(key) / LAYOUT_NAME).is_file()


@respx.mock
def test_a_library_file_with_part_omitted_keeps_its_own_count(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    configure(client)
    asyncio.run(saved_output(tmp_path))
    library_file(88, content=two_colour_3mf(tmp_path))
    listed = client.get("/api/v1/print/library/88/objects")
    assert listed.status_code == 200, listed.text
    [obj] = listed.json()["objects"]
    assert obj["count"] == 1 and obj["colours"] == ["#FF0000", "#0000FF"]
    assert obj["part"].startswith("lib1-") and obj["name"] == "plain"
    assert [round(v) for v in obj["size"]] == [10, 10, 9]
    arranger = Arranger()
    app.dependency_overrides[get_render] = lambda: arranger
    response = client.post(
        "/api/v1/outputs/arrange",
        json={"objects": [{"library_file_id": 88, "part": obj["part"]}], "slug": "demo"},
    )
    assert response.status_code == 202, response.text
    [inputs] = arranger.inputs
    assert [(i.part.piece_key, i.count) for i in inputs.items] == [(obj["part"], 1)]
    unknown = client.post(
        "/api/v1/outputs/arrange",
        json={"objects": [{"library_file_id": 88, "part": "nope"}], "slug": "demo"},
    )
    assert unknown.status_code == 422 and "has no object nope" in unknown.json()["detail"]


@respx.mock
def test_library_files_alone_are_filed_under_a_template_named(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    configure(client)
    asyncio.run(saved_output(tmp_path))
    library_file(88, content=two_colour_3mf(tmp_path))
    arranger = Arranger()
    app.dependency_overrides[get_render] = lambda: arranger
    objects = [{"library_file_id": 88}]
    unnamed = client.post("/api/v1/outputs/arrange", json={"objects": objects})
    assert unnamed.status_code == 422 and "slug" in unnamed.json()["detail"]
    unknown = client.post("/api/v1/outputs/arrange", json={"objects": objects, "slug": "nope"})
    assert unknown.status_code == 404, unknown.text
    named = client.post("/api/v1/outputs/arrange", json={"objects": objects, "slug": "demo"})
    assert named.status_code == 202, named.text
    assert named.json()["slug"] == "demo"


@respx.mock
def test_a_library_file_that_cannot_be_read_is_refused_saying_why(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    """A sliced file, and one that is no 3MF, are refused by a code the UI and the
    agent tell apart, naming every such file and why."""
    configure(client)
    _, meta, written = asyncio.run(saved_output(tmp_path))
    library_file(88, file_type="gcode.3mf")
    library_file(89, content=b"not a zip")
    arranger = Arranger()
    app.dependency_overrides[get_render] = lambda: arranger
    response = client.post(
        "/api/v1/outputs/arrange",
        json={
            "objects": [
                {"output_id": meta.id, "part": written.manifest[0].part, "count": 1},
                {"library_file_id": 88},
                {"library_file_id": 89, "part": "x", "count": 1},
            ]
        },
    )
    assert response.status_code == 422, response.text
    problem = response.json()
    assert problem["code"] == LIBRARY_FILE_NOT_ARRANGEABLE
    assert problem["library_file_ids"] == [88, 89]
    assert "file-88.gcode.3mf: it is sliced already" in problem["detail"]
    assert "file-89.3mf: the file is not a 3MF archive" in problem["detail"]
    assert arranger.inputs == []
    listed = client.get("/api/v1/print/library/88/objects")
    assert listed.status_code == 422
    assert listed.json()["code"] == LIBRARY_FILE_NOT_ARRANGEABLE
