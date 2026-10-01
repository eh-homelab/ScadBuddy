"""POST /outputs/arrange (spec 2026-09-27 §10)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.deps import STATE_ATTR, AppState, get_render
from scadbuddy.api.outputs import (
    NEEDS_BACKFILL_PROBLEM,
    ArrangeObject,
    ArrangeRequest,
    arrange_inputs,
)
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import SlotChoice
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import META_NAME, OutputMeta, OutputStore
from scadbuddy.render.job_models import Job, now
from scadbuddy.workflows.models import ArrangeInputs
from tests.support.arrange import saved_output

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
    assert sum(o.count for o in ArrangeRequest(objects=objects).objects) == 2000


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
