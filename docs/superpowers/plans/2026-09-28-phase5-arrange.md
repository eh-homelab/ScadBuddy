# Phase 5: Arrange — objects and plates are separate. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every output records the objects it is made of, and any set of objects from one or more outputs can be laid out again onto plates — by fewest plates, fewest filament swaps, single-colour plates or kept-together groups — for a real printer and a real spool plan, in seconds and with no re-render (epic #428; folds #314).

**Architecture:** Phase 4's `write_output` gains an object **manifest** (per object: Part ref, bbox, footprint, colours, count, provenance) that the output keeps as `manifest.json`, and a saved output holds `blob_refs` on its Parts so they outlive the job. Phase 4's shelf packer is replaced behind the same `pack` activity by `workflows/arrange.py`: a first-fit-decreasing MaxRects packer with 90° rotation that groups copies by filament signature (spools under a `filament_plan`, else colours) per goal, accepts a plate only if `render/plate.py`'s own `fit_problem` passes with the prime tower that plate needs, then orders plates to minimise swaps. The writer honours rotation and a pinned filament order so a plan's slot numbers survive a re-arrange. `Arrange` is a workflow on a `render_jobs` row with `kind = 'arrange'`, submitted through the same insert → start → reconcile path as a render (`RenderService.arrange`, `POST /outputs/arrange`). The History page gains an Arrange dialog (#314's build list) and the Print dialog a "Re-arrange for these spools" step.

**Tech Stack:** Python 3.12, `temporalio` 1.33.0, pydantic v2, FastAPI, psycopg 3, trimesh, numpy, pytest; React 19 + vitest + msw.

**Spec:** `docs/superpowers/specs/2026-09-27-template-pipelines-design.md`: §7 (objects and layout are separate; Arrange is a workflow), §3.2 (`kind`), §3.3 (insert → start → reconcile), §3.4 (the `Arrange` row in the workflow table; "A pipeline's `ctx.pack` calls Arrange's packing activity directly"), §5.2 (`pack`'s `goal` and `filament_plan`), §8.4, §10 (`GET /outputs/{id}` plus `manifest`; `POST /outputs/arrange`), §11 item 5, §12 (#314, #81, #83, #313). Epic **#428**.

**Base:** `main` after phases 1–4 (#424–#427) have merged. Names consumed verbatim from phase 4 (`docs/superpowers/plans/2026-09-28-phase4-template-pipelines.md`):
- `scadbuddy/template.py`: `Part(piece_key, file, bbox, colours, notes, plates, local)`, `Blob`.
- `workflows/models.py`: `PlateSize(key, width, depth)`, `PackItem(part, count)`, `Placed(piece_key, x, y)`, `LayoutPlate(items)`, `Layout(plates, own)`, `PackRequest(items, plate, goal)`, `OutputRequest(job_id, index, slug, layout, parts, name, bom, files, plate_model, record)`, `OutputRef`, `Projection.outputs`, `Projection.blob_keys`.
- `render/job_models.py`: `BomEntry`, `OutputRecord`, `PipelineOutput(name, result, bom, files, blob_keys, record)`, `Job.outputs`.
- `workflows/packing.py`: `GAP_MM`, `PackError`, `shelf_pack`, `explicit_plate`.
- `workflows/outputs.py`: `output_key`, `build_output`, `_write_plates`, `_refuse`.
- `workflows/pipeline_activities.py`: `pack_layout`, `plate_size`, `PipelineActivities.pack`, `.write_output`.
- `workflows/ctx.py`: `Ctx.pack(items, *, goal, filament_plan)`, `Ctx.plate_of`.
- `library/outputs.py`: `OutputStore.create(job, *, name, public_url, inputs, index, files_dir)`, `BOM_NAME`, `RECORD_NAME`, `FILES_DIR`, `OutputStore.bom/record/files`; `api/outputs.py`: `CreateOutputRequest.index`, `OutputDetail.bom/record/files`; `api/jobs.py`: `JobOutputSummary`, `JobStatus.outputs`, `_job_status`; `render/inputs.py`: `inputs_key`.
- Tests: `tests/test_packing_and_outputs.py` `_part`, `_deps`, `_render`, `_record`, `PLATE`; `tests/support/pipelines.py` `FakeWorld`, `run_job`, `a_job`.
- Phase 1: `TemplatePipeline`, `RenderPiece`, `RenderPreview`, `SHORT`, `RETRY`, `PROJECT_RETRY`, `_openscad_timeout`, `_failure_of` (`workflows/pipelines.py`); `render_worker` (`workflows/client.py`); `RenderService` (`render/submit.py`, `_start`, `reconcile_once`); `JobProjection`, `workflow_id_for`; `tests/support/temporal.py` `temporal_client()`; `tests/test_submit.py` fixture `projection`.
- Phase 3: `BlobRefs.add/drop_holder/referenced`, `sweep_blobs`, `LocalBlobStore`, `BlobStore.fetch/exists`.
- Unchanged baseline: `render/plate.py` `plate_for`, `fit_problem`, `PlateGeometry`, `PRIME_TOWER_SIDE`; `render/bambu3mf.py` `write_plates_3mf`, `PlateParts.tower`, `plates_of`; `bambuddy/filaments.py` `FilamentPlan(slots: list[SlotChoice], force_colour_match)`, `SlotChoice(slot_id, spool_id)`; `bambuddy/client.py` `client_for`, `BambuddyClient.printer`.

## Global Constraints

- "Rendering produces **objects**; plates are a **layout** over objects. `replate_3mf` … already re-lays out a finished 3MF for another printer without re-rendering; this makes that the rule" (§7).
- "Every output stores an **object manifest**: per object, its Part ref, bbox, footprint, colour slots, count, provenance (template, revision, inputs, BOM entry)" (§7).
- "A **Layout** is `[{plate: int, objects: [{part, at: (x, y, rot)}]}]` plus the plate geometry it was packed for. The 3MF is written *from* manifest + layout" (§7). Phase 4's `Layout`/`Placed` carry this; `Placed` gains `rot`.
- "`Arrange` is a workflow (a `render_jobs` row with `kind = 'arrange'`, §3.4) taking objects from one or more outputs (#314's build list), a filament assignment — a `FilamentPlan`, one chosen spool per slot … — and a `goal`: `fewest_plates` | `fewest_swaps` | `by_colour` (single-colour plates skip the prime tower) | `keep_together` groups. It produces a new layout → 3MF in seconds, with no re-render" (§7).
- "`ctx.pack(goal=…, filament_plan=…)` in a pipeline awaits the same packing activity with the same parameters" (§7, §5.2).
- "Heuristic, not a solver: group by colour signature, first-fit-decreasing 2D packing against `plate.py`'s exclusion zones and prime-tower rules, then order plates to minimise swaps. `Goal` is pluggable" (§7).
- "`Arrange` is submitted through the same insert → start → reconcile path as a render (§3.3), so `GET /jobs/{id}` works for it unchanged; the row's `kind` says which workflow `render-<job_id>` runs" (§3.4).
- "`POST /outputs/arrange` `{objects: [{output_id, part, count}], goal, printer_id, filament_plan?}` → a `render_jobs` row with `kind = 'arrange'`, polled with `GET /jobs/{id}` like any render; its output is the new layout's 3MF" (§10). "`GET /outputs/{id}`: plus … `manifest`" (§10).
- Backend gates per task: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest` green, after `uv run --frozen ruff format .`: the plan's code blocks are not wrapped to the 100-column limit, and `ruff format` wraps them (every E501 in them is a formatting one). `requires_temporal` tests use the `temporal` CLI dev server; `requires_postgres` tests need `SCADBUDDY_TEST_DATABASE_URL`. Frontend: `pnpm lint && pnpm typecheck && pnpm test && pnpm build`. Agent: the same four in `agent/`.
- Generated API files (#492) are never committed. After an API model or route change (Tasks 1, 5), run `cd frontend && pnpm gen:api`, then `cd agent && pnpm gen:api`, before either package's typecheck.
- Every `/api/v1` operation needs an agent tool or an `agent/src/tools/coverage.ts` `NOT_A_TOOL` entry (CLAUDE.md).
- No schema change is needed: `render_jobs.kind` (phase 1) and `blob_refs` (phase 1) exist. If one becomes needed, it is a NEW file `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`.
- Conventional-commit titles. Every PR body says `Part of #428`, the last `Fixes #428` and `Fixes #314`. No docstrings or comments on unchanged code.

## Review Focus

1. **A plate packed for a real printer** (the dual-extruder H2C, whose usable area is the intersection of both extruders' reach; the X1C, with its filament-cutter corner) must be writable: `write_plates_3mf` places each plate with `place_on_plate` and refuses one it cannot fit with its prime tower. Pinned in Task 2 by `test_every_packed_plate_places_on_the_real_printer`.
2. **An `arrange` row the reconciler restarts** (its `start_workflow` failed at submit) must run `Arrange`, never `TemplatePipeline`, which would read its inputs as template inputs. Pinned in Task 4 by `test_the_reconciler_starts_an_arrange_row_as_arrange`.
3. **An output saved weeks ago**, whose job the TTL pruned: its Parts must survive the blob sweep so it can still be arranged, and deleting the output must release them. Pinned in Task 1 by `test_a_saved_output_keeps_its_parts_after_the_job_is_pruned`.
4. **A filament plan made for an output** and then re-arranged: slot N of the new 3MF must be the same colour as slot N of the output the plan was chosen against, or the chosen spools print the wrong colours. Pinned in Task 3 by `test_an_arranged_output_keeps_the_filament_order_it_was_planned_against`.
5. **An output saved before manifests, or a part it does not have, or a count of zero everywhere:** a clear 409/422 before any job exists, never a job that fails on a worker later. Pinned in Task 5 by `test_an_output_without_a_manifest_is_refused_up_front` and `test_a_part_not_in_the_output_is_refused`.

---

## File Structure

Backend, created:
- `backend/scadbuddy/workflows/arrange.py`: `GOALS`, `geometry_of`, `signature_of`, `part_of`, `arrange`, `order_plates`. The packer. Pure; imported by the `pack` activity only.
- `backend/tests/test_arrange_packing.py`, `backend/tests/test_arrange_outputs.py`, `backend/tests/test_arrange_workflow.py`, `backend/tests/api/test_arrange_api.py`, `backend/tests/support/arrange.py` (`saved_output`).

Backend, modified:
- `render/job_models.py`: `ManifestObject`; `PipelineOutput.manifest`.
- `workflows/models.py`: `SlotPlan`; `Placed.rot`; `PackItem.group`; `PackRequest.filament_plan`, `.colours`; `OutputRequest.colours`, `.provenance`; `ArrangeInputs`; `ARRANGE_VERSION`.
- `workflows/packing.py`: `shelf_pack` deleted; `explicit_plate` takes quarter turns.
- `workflows/outputs.py`: `manifest_of`, `placement_matrix`; `build_output` fills `manifest`; `_write_plates` rotates and pins colours.
- `workflows/pipeline_activities.py`: `pack_layout` calls `arrange`.
- `workflows/ctx.py`: `Ctx.pack` passes `filament_plan`, `colours` and groups.
- `workflows/pipelines.py`: `Arrange`.
- `workflows/client.py`: `render_worker` registers `Arrange`.
- `render/inputs.py`: `arrange_key`.
- `render/submit.py`: `RenderService.arrange`; `_start` runs the row's kind.
- `library/outputs.py`: `MANIFEST_NAME`, `ARRANGED_NAME`, `OUTPUT_HOLDER`, `hold_parts`, `release_parts`, `OutputStore.manifest`, `OutputStore.arranged_from`; `create(..., arranged_from=)`.
- `api/outputs.py`: `ArrangeObject`, `ArrangeRequest`, `arrange_inputs`, `POST /outputs/arrange`; `OutputDetail.manifest`, `.arranged_from`; create/delete hold and release Parts.

Frontend: `src/api/client.ts`, `src/api/types.ts`, `src/lib/arrange.ts` (new), `src/components/ArrangeDialog.tsx` (new), `src/components/ArrangeDialog.test.tsx` (new), `src/components/PrintPicker.tsx`, `src/components/PrintPicker.test.tsx`, `src/pages/HistoryPage.tsx`, `src/mocks/handlers.ts`, `src/mocks/fixtures.ts`.

Agent and docs: `agent/src/tools/coverage.ts`; `plugins/scadbuddy/skills/authoring/SKILL.md`; `CLAUDE.md`.

## Task order and parallelism

- Tasks 1 and 2 are independent.
- Task 3 needs 2 (`Placed.rot`, `PackRequest.colours`).
- Task 4 needs 1, 2 and 3.
- Task 5 needs 4.
- Task 6 needs 2.
- Task 7 needs 5.

Suggested PRs: 1) Tasks 1–3 (manifests and the packer; every Generate keeps working); 2) Tasks 4–6 (the workflow, the API, `ctx.pack` goals); 3) Task 7 (`Fixes #428`, `Fixes #314`).

---

### Task 1: Manifests on outputs; a saved output holds its Parts

**Files:**
- Modify: `backend/scadbuddy/render/job_models.py`, `backend/scadbuddy/workflows/models.py`, `backend/scadbuddy/workflows/outputs.py`, `backend/scadbuddy/library/outputs.py`, `backend/scadbuddy/api/outputs.py`, `backend/scadbuddy/api/models.py`
- Create: `backend/tests/support/arrange.py`
- Test: `backend/tests/test_arrange_outputs.py`, `backend/tests/api/test_output_parts.py`

**Interfaces:**
- Consumes: phase 4's `OutputRequest`, `PipelineOutput`, `build_output`, `OutputStore.create`, `BomEntry`, `Part`, `Layout`, `LayoutPlate`, `Placed`; test helpers `_deps`, `_render`, `_record` (`tests/test_packing_and_outputs.py`); phase 3's `BlobRefs`, `sweep_blobs`.
- Produces:
  ```python
  # render/job_models.py
  class ManifestObject(BaseModel):
      part: str; file: str; slug: str; revision: str | None; bbox: BoundingBox
      footprint: tuple[float, float]; colours: list[str]; count: int (>= 1); plates: int = 1
      bom_piece: str | None = None; source_output: str | None = None; notes: list[str] = []
  PipelineOutput.manifest: list[ManifestObject] = []
  # workflows/models.py (defined here, additive to phase 4's model)
  OutputRequest.provenance: dict[str, ManifestObject] = {}
  # workflows/outputs.py
  def manifest_of(req: OutputRequest) -> list[ManifestObject]
  # library/outputs.py
  MANIFEST_NAME = "manifest.json"; ARRANGED_NAME = "arranged_from.json"; OUTPUT_HOLDER = "output"
  def hold_parts(refs: BlobRefs, output_id: str, manifest: Iterable[ManifestObject]) -> None
  def release_parts(refs: BlobRefs, output_id: str) -> None
  OutputStore.manifest(output_id) -> list[ManifestObject]
  OutputStore.arranged_from(output_id) -> list[str]
  OutputStore.create(..., arranged_from: Sequence[str] = ())
  OutputDetail.manifest: list[ManifestObject] = []; OutputDetail.arranged_from: list[str] = []
  # tests/support/arrange.py
  async def finished_job(tmp_path: Path, *, width: int = 12, count: int = 2, name: str = "two", job_id: str = "j1") -> tuple[DataPaths, Job, PipelineOutput]
  async def saved_output(tmp_path: Path, *, width: int = 12, count: int = 2, name: str = "two") -> tuple[DataPaths, OutputMeta, PipelineOutput]
  ```
  `POST /models/{slug}/outputs` holds a new output's Parts (`holder_kind = 'output'`); `DELETE /outputs/{id}` and `DELETE /models/{slug}` release them.

- [ ] **Step 1: The test helper**

`backend/tests/support/arrange.py`:

```python
"""A real output with a manifest, rendered by the fake openscad (phase 4's helpers)."""

from __future__ import annotations

from pathlib import Path

from temporalio.testing import ActivityEnvironment

from scadbuddy.core.paths import DataPaths
from scadbuddy.library.outputs import OutputMeta, OutputStore
from scadbuddy.render.job_models import BomEntry, Job, PipelineOutput, now
from scadbuddy.workflows.models import Layout, LayoutPlate, OutputRequest, Placed
from scadbuddy.workflows.pipeline_activities import PipelineActivities
from tests.test_packing_and_outputs import _deps, _record, _render


async def finished_job(
    tmp_path: Path, *, width: int = 12, count: int = 2, name: str = "two", job_id: str = "j1"
) -> tuple[DataPaths, Job, PipelineOutput]:
    """A done job for template `demo` whose one output lays out ``count`` copies of one
    Part. Its data directory is ``tmp_path / "data"``, which is also the API tests'
    `paths`, so an app built on those fixtures can save it. Once per ``tmp_path``:
    `_deps` creates the template."""
    deps, paths = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": width})
    layout = Layout(plates=[LayoutPlate(items=[
        Placed(piece_key=part.piece_key, x=i * (width + 10.0), y=0.0) for i in range(count)
    ])])
    req = OutputRequest(
        job_id=job_id, index=0, slug="demo", layout=layout, parts=[part], name=name,
        bom=[BomEntry(piece="wall", label="Wall", count=count, part=part.piece_key)],
        files={}, record=_record([part.piece_key]),
    )
    written = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    job = Job(id=job_id, slug="demo", state="done", created_at=now(), result=written.result,
              outputs=[written])
    return paths, job, written


async def saved_output(
    tmp_path: Path, *, width: int = 12, count: int = 2, name: str = "two"
) -> tuple[DataPaths, OutputMeta, PipelineOutput]:
    paths, job, written = await finished_job(tmp_path, width=width, count=count, name=name)
    meta = OutputStore(paths).create(job, name=name, index=0)
    return paths, meta, written
```

- [ ] **Step 2: Write the failing tests**

`backend/tests/test_arrange_outputs.py` (the last test also `requires_postgres`):

```python
"""Every output records its objects (spec 2026-09-27 §7), and keeps their Parts alive."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.library.outputs import OutputStore, hold_parts, release_parts
from scadbuddy.render.job_models import ManifestObject
from scadbuddy.store import sweep_blobs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs
from scadbuddy.workflows.models import Layout, OutputRequest
from scadbuddy.workflows.outputs import manifest_of
from scadbuddy.workflows.pipeline_activities import PipelineActivities
from tests.support.arrange import saved_output
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
    req = OutputRequest(job_id="j1", index=0, slug="demo", layout=Layout(own=part.piece_key),
                        parts=[part], name=None, bom=[], files={}, record=_record([part.piece_key]))
    out = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert [(m.part, m.count, m.plates) for m in out.manifest] == [(part.piece_key, 1, part.plates)]


async def test_provenance_from_an_earlier_output_is_carried(tmp_path: Path) -> None:
    deps, _ = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    earlier = ManifestObject(
        part=part.piece_key, file="model.scad", slug="dollhouse-kit", revision="abc",
        bbox=part.bbox, footprint=(1.0, 1.0), colours=part.colours, count=4, bom_piece="wall",
        source_output="o-old",
    )
    req = OutputRequest(job_id="j2", index=0, slug="demo", layout=Layout(own=part.piece_key),
                        parts=[part], name=None, bom=[], files={}, record=_record([part.piece_key]),
                        provenance={part.piece_key: earlier})
    [obj] = manifest_of(req)
    assert (obj.slug, obj.revision, obj.bom_piece, obj.source_output) == ("dollhouse-kit", "abc", "wall", "o-old")
    assert obj.count == 1  # the count is this layout's, never the source's


def test_an_output_saved_before_manifests_reads_as_none(tmp_path: Path) -> None:
    from scadbuddy.core.paths import DataPaths

    assert OutputStore(DataPaths(tmp_path)).manifest("never-written") == []


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
        hold_parts(refs, meta.id, written.manifest)
        refs.drop_holder("job", "j1")  # what JobProjection.prune does
        blobs = LocalBlobStore(paths.blobs)
        assert key not in sweep_blobs(blobs, refs, grace=0, now=10**12)
        assert blobs.exists(key)
        release_parts(refs, meta.id)
        assert key in sweep_blobs(blobs, refs, grace=0, now=10**12)
```

`backend/tests/api/test_output_parts.py` (the routes' side of Review Focus 3; every test here
builds the app, so it takes `pg_conninfo` and skips without Postgres, as `tests/api` does):

```python
"""A saved output holds its Parts; deleting it, or its model, lets them go (spec
2026-09-27 §7, Review Focus 3)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool

from scadbuddy.api.deps import STATE_ATTR, AppState, get_queue
from scadbuddy.library.outputs import OUTPUT_HOLDER
from scadbuddy.render.job_models import Job
from scadbuddy.render.job_store import JobNotFoundError
from scadbuddy.store.refs import BlobRefs
from tests.support.arrange import finished_job
from tests.support.store import store_pool

Pool = ConnectionPool[Connection[DictRow]]


class OneJob:
    """A queue that knows one finished job: all `require_job` (`queue.store.read`) and
    `_delete_model` (`queue.store.has_unfinished`) ask of it."""

    def __init__(self, job: Job) -> None:
        self.store = self
        self.job = job

    def read(self, job_id: str) -> Job:
        if job_id != self.job.id:
            raise JobNotFoundError(job_id)
        return self.job

    def has_unfinished(self, slug: str) -> bool:
        return False


@pytest.fixture
def pool(app: FastAPI, pg_conninfo: str) -> Iterator[Pool]:
    with store_pool(pg_conninfo) as opened:
        state: AppState = getattr(app.state, STATE_ATTR)
        state.refs = BlobRefs(opened)
        yield opened


def held(pool: Pool) -> set[tuple[str, str]]:
    """Every (Part, output) pair `blob_refs` holds for an output."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT key, holder_id FROM blob_refs WHERE holder_kind = %s", (OUTPUT_HOLDER,)
        ).fetchall()
    return {(row["key"], row["holder_id"]) for row in rows}


def finished(app: FastAPI, tmp_path: Path) -> tuple[str, list[str]]:
    """A done job the app's queue answers for: its id and its output's Parts."""
    _, job, written = asyncio.run(finished_job(tmp_path))
    app.dependency_overrides[get_queue] = lambda: OneJob(job)
    return job.id, [m.part for m in written.manifest]


def save(client: TestClient, job_id: str) -> str:
    response = client.post("/api/v1/models/demo/outputs", json={"job_id": job_id})
    assert response.status_code == 201, response.text
    output_id: str = response.json()["id"]
    return output_id


def test_saving_an_output_holds_each_of_its_parts(
    client: TestClient, app: FastAPI, pool: Pool, tmp_path: Path
) -> None:
    job_id, parts = finished(app, tmp_path)
    output_id = save(client, job_id)
    assert held(pool) == {(part, output_id) for part in parts}
    detail = client.get(f"/api/v1/outputs/{output_id}").json()
    assert [m["part"] for m in detail["manifest"]] == parts
    assert detail["arranged_from"] == []


def test_deleting_an_output_releases_its_parts(
    client: TestClient, app: FastAPI, pool: Pool, tmp_path: Path
) -> None:
    job_id, _ = finished(app, tmp_path)
    output_id = save(client, job_id)
    assert client.delete(f"/api/v1/outputs/{output_id}").status_code == 204
    assert held(pool) == set()


def test_deleting_the_model_releases_every_output_s_parts(
    client: TestClient, app: FastAPI, pool: Pool, tmp_path: Path
) -> None:
    job_id, _ = finished(app, tmp_path)
    first, second = save(client, job_id), save(client, job_id)
    assert {holder for _, holder in held(pool)} == {first, second}
    assert client.delete("/api/v1/models/demo").status_code == 204
    assert held(pool) == set()
```

(The tests do not take the `model` fixture: `finished_job` creates template `demo` in the
same data directory, and `_deps` refuses to create it twice.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_arrange_outputs.py -q`
Expected: collection error `ImportError: cannot import name 'hold_parts' from 'scadbuddy.library.outputs'`. `SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/api/test_output_parts.py -q` fails the same way on `OUTPUT_HOLDER`.

- [ ] **Step 4: `ManifestObject`**

In `render/job_models.py`, before `PipelineOutput`:

```python
class ManifestObject(BaseModel):
    """One object of an output (spec 2026-09-27 §7): what Arrange lays out again.

    ``part`` is the Part's store key (its `piece_key`), held by the output's
    `blob_refs` so it outlives the job that rendered it.
    """

    part: str
    file: str
    slug: str
    revision: str | None
    bbox: BoundingBox
    #: Width and depth on the plate, mm: the rectangle the packer places.
    footprint: tuple[float, float]
    #: The part's colours, `#RRGGBB`, in its own slot order.
    colours: list[str]
    #: Copies of this object in this output's layout.
    count: int = Field(ge=1)
    #: More than one: the part lays out its own plates (base spec §6.4) and is only
    #: ever written alone.
    plates: int = 1
    #: The BOM entry naming this Part, when the pipeline wrote one.
    bom_piece: str | None = None
    #: The output an arranged object came from.
    source_output: str | None = None
    notes: list[str] = Field(default_factory=list)
```

and add to `PipelineOutput`: `manifest: list[ManifestObject] = Field(default_factory=list)`.

In `workflows/models.py`, add to `OutputRequest` (import `ManifestObject` beside the other `render.job_models` names):

```python
    #: Arrange's objects keep where they came from; keyed by part.
    provenance: dict[str, ManifestObject] = Field(default_factory=dict)
```

- [ ] **Step 5: `manifest_of` and `build_output`**

In `workflows/outputs.py`:

```python
from collections import Counter

from scadbuddy.render.job_models import ManifestObject


def manifest_of(req: OutputRequest) -> list[ManifestObject]:
    """The output's objects, from the layout it is written from (spec §7)."""
    if req.layout.own is not None:
        counts = Counter([req.layout.own])
    else:
        counts = Counter(p.piece_key for plate in req.layout.plates for p in plate.items)
    named = {entry.part: entry.piece for entry in req.bom if entry.part}
    objects: list[ManifestObject] = []
    for part in req.parts:
        if not counts[part.piece_key]:
            continue
        earlier = req.provenance.get(part.piece_key)
        objects.append(ManifestObject(
            part=part.piece_key,
            file=part.file,
            slug=earlier.slug if earlier else req.slug,
            revision=earlier.revision if earlier else req.record.revision,
            bbox=part.bbox,
            footprint=(part.bbox.size[0], part.bbox.size[1]),
            colours=list(part.colours),
            count=counts[part.piece_key],
            plates=part.plates,
            bom_piece=named.get(part.piece_key) or (earlier.bom_piece if earlier else None),
            source_output=earlier.source_output if earlier else None,
            notes=list(part.notes),
        ))
    return objects
```

In `build_output`'s final `return PipelineOutput(...)`, add `manifest=manifest_of(req)`.

- [ ] **Step 6: The output keeps its manifest and holds its Parts**

In `library/outputs.py`:

```python
MANIFEST_NAME = "manifest.json"
ARRANGED_NAME = "arranged_from.json"
#: `blob_refs.holder_kind` for a saved output: its Parts live as long as it does.
OUTPUT_HOLDER = "output"


def hold_parts(refs: BlobRefs, output_id: str, manifest: Iterable[ManifestObject]) -> None:
    for obj in manifest:
        refs.add(obj.part, OUTPUT_HOLDER, output_id)


def release_parts(refs: BlobRefs, output_id: str) -> None:
    refs.drop_holder(OUTPUT_HOLDER, output_id)
```

`OutputStore.create` gains `arranged_from: Sequence[str] = ()`. Beside phase 4's `BOM_NAME`/`RECORD_NAME` writes (inside `if chosen is not None:`):

```python
            if chosen.manifest:
                (directory / MANIFEST_NAME).write_text(
                    json.dumps([m.model_dump(mode="json") for m in chosen.manifest]), encoding="utf-8"
                )
        if arranged_from:
            (directory / ARRANGED_NAME).write_text(json.dumps(list(arranged_from)), encoding="utf-8")
```

Readers beside `bom`:

```python
    def manifest(self, output_id: str) -> list[ManifestObject]:
        """Empty for an output saved before manifests (phase 5): it cannot be arranged."""
        try:
            path = self.directory(output_id) / MANIFEST_NAME
        except OutputNotFoundError:
            return []
        if not path.is_file():
            return []
        return [ManifestObject.model_validate(m) for m in json.loads(path.read_text(encoding="utf-8"))]

    def arranged_from(self, output_id: str) -> list[str]:
        try:
            path = self.directory(output_id) / ARRANGED_NAME
        except OutputNotFoundError:
            return []
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
```

(If phase 4's `directory(output_id)` returns a path without raising for an unknown id, drop the two `try` blocks; the `is_file` checks already answer `[]`.)

In `api/outputs.py`, import `hold_parts` and `release_parts` beside the other
`scadbuddy.library.outputs` names, `ManifestObject` from `scadbuddy.render.job_models`, and
`StateDep` from `scadbuddy.api.deps`. `OutputDetail` gains:

```python
    #: The output's objects (spec 2026-09-27 §7); empty for one saved before phase 5,
    #: which therefore cannot be arranged.
    manifest: list[ManifestObject] = Field(default_factory=list)
    #: For an arranged output, the outputs its objects came from (Task 5).
    arranged_from: list[str] = Field(default_factory=list)
```

and `_detail(store, meta, library_files)` passes them in the `OutputDetail(...)` it builds,
beside the `bom`/`record`/`files` phase 4 reads there:

```python
        manifest=store.manifest(meta.id),
        arranged_from=store.arranged_from(meta.id),
```

`create_output` (an `async def` since phase 3, which runs `outputs.create(...)` through
`asyncio.to_thread`) takes `state: StateDep` and, right after that call:

```python
    if state.refs is not None:
        # The Parts outlive the job that rendered them: Arrange reads them later (§7).
        manifest = await asyncio.to_thread(outputs.manifest, meta.id)
        await asyncio.to_thread(hold_parts, state.refs, meta.id, manifest)
```

`delete_output` takes `state: StateDep` and, after `await uploads.delete_outputs([output_id])`:

```python
    if state.refs is not None:
        await asyncio.to_thread(release_parts, state.refs, output_id)
```

In `api/models.py`, import `release_parts` beside `OutputStore` (`from scadbuddy.library.outputs
import OutputStore, release_parts`) and `StateDep` from `scadbuddy.api.deps`. `delete_model`
takes `state: StateDep`; `_delete_model` removes every output of the model through
`catalogue.delete` and never passes through `delete_output`, so its outputs' holds are released
here, after the `uploads.delete_outputs(output_ids)` block and before the `emit`:

```python
    # Their Parts go with them, or no sweep ever removes them (blob_refs, spec §7). Best
    # effort, like the upload records above: the model is gone either way.
    refs = state.refs
    if output_ids and refs is not None:
        try:
            for output_id in output_ids:
                await asyncio.to_thread(release_parts, refs, output_id)
        except psycopg.Error:
            logger.exception(
                "could not release a deleted model's output Parts", extra={"slug": slug}
            )
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/test_arrange_outputs.py tests/test_packing_and_outputs.py tests/api -q -k "output or arrange"`
Expected: all pass.

- [ ] **Step 8: Gates, generated files, commit**

Run `cd backend && uv run --frozen ruff format .`, then the backend gates; then `cd frontend && pnpm gen:api && pnpm typecheck`, `cd agent && pnpm gen:api && pnpm typecheck`.

```bash
git add backend/scadbuddy/render/job_models.py backend/scadbuddy/workflows/models.py backend/scadbuddy/workflows/outputs.py \
  backend/scadbuddy/library/outputs.py backend/scadbuddy/api/outputs.py backend/scadbuddy/api/models.py \
  backend/tests/support/arrange.py backend/tests/test_arrange_outputs.py backend/tests/api/test_output_parts.py
git commit -m "feat(outputs): every output records its objects and holds their Parts (#428)"
```

---

### Task 2: The Arrange packer

**Files:**
- Create: `backend/scadbuddy/workflows/arrange.py`
- Modify: `backend/scadbuddy/workflows/models.py`, `backend/scadbuddy/workflows/packing.py`, `backend/scadbuddy/workflows/pipeline_activities.py`, `backend/tests/test_packing_and_outputs.py`
- Test: `backend/tests/test_arrange_packing.py`

**Interfaces:**
- Consumes: `Part`, `PlateSize`, `PackItem`, `Placed`, `LayoutPlate`, `Layout`, `PackRequest`, `GAP_MM`, `PackError`, `pack_layout` (phase 4); `plate_for`, `fit_problem`, `PlateGeometry` (`render/plate.py`); `ManifestObject` (Task 1).
- Produces:
  ```python
  # workflows/models.py (additive to phase 4's models; defined here)
  class SlotPlan(BaseModel): slots: dict[int, int] = {}      # 1-based filament slot -> spool id
      @classmethod def of(cls, plan: object) -> SlotPlan | None   # a FilamentPlan, its JSON, a SlotPlan, or None
  Placed.rot: float = 0.0                                     # degrees, a multiple of 90
  PackItem.group: str | None = None
  PackRequest.filament_plan: SlotPlan | None = None; PackRequest.colours: list[str] = []
  PackRequest.allow_own: bool = True                           # False: always plates (Arrange)
  # workflows/arrange.py
  GOALS: tuple[str, ...] = ("fewest_plates", "fewest_swaps", "by_colour", "keep_together")
  def geometry_of(size: PlateSize) -> PlateGeometry
  def signature_of(part: Part, plan: SlotPlan | None, colours: Sequence[str]) -> frozenset[str]
  def part_of(obj: ManifestObject) -> Part
  def arrange(items: Sequence[PackItem], plate: PlateSize, *, goal: str = "fewest_plates", plan: SlotPlan | None = None, colours: Sequence[str] = (), allow_own: bool = True) -> Layout
  def order_plates(signatures: Sequence[frozenset[str]]) -> list[int]
  # workflows/packing.py
  shelf_pack  (deleted)
  explicit_plate(parts, at)  accepts rot in {0, 90, 180, 270}
  ```
  `pack_layout(req)` now calls `arrange(req.items, req.plate, goal=req.goal, plan=req.filament_plan, colours=req.colours, allow_own=req.allow_own)`; an unknown goal is a `PackError` naming the goals. With `allow_own=False`, one object with one copy is still packed onto a plate and written by `_write_plates`, never served as the piece's own result (`Layout.own`), so a pinned filament order always applies (Review Focus 4).

`SlotPlan` exists because `bambuddy.filaments` imports the Bambuddy client and must not enter the workflow sandbox; `SlotPlan.of(FilamentPlan)` is the bridge.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_arrange_packing.py`:

```python
"""The Arrange packer (spec 2026-09-27 §7): goals, rotation, real plates."""

from __future__ import annotations

import itertools

import pytest

from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.plate import fit_problem, plate_for
from scadbuddy.template import Part
from scadbuddy.workflows.arrange import GOALS, arrange, geometry_of, order_plates, signature_of
from scadbuddy.workflows.models import Layout, PackItem, PlateSize, SlotPlan
from scadbuddy.workflows.packing import GAP_MM, PackError, explicit_plate

DEFAULT = PlateSize(key="default", width=256.0, depth=256.0)


def plate(model: str) -> PlateSize:
    g = plate_for(model)
    return PlateSize(key=g.key, width=g.usable.width, depth=g.usable.depth)


def part(key: str, w: float, d: float, *colours: str, h: float = 5.0, plates: int = 1) -> Part:
    return Part(
        piece_key=key, file="model.scad", colours=list(colours or ("#FF0000",)), plates=plates,
        bbox=BoundingBox(min=(0, 0, 0), max=(w, d, h), size=(w, d, h)),
    )


def footprints(layout: Layout, parts: dict[str, Part]) -> list[list[tuple[float, float, float, float]]]:
    out = []
    for p in layout.plates:
        rects = []
        for placed in p.items:
            w, d = parts[placed.piece_key].bbox.size[:2]
            if placed.rot % 180:
                w, d = d, w
            rects.append((placed.x, placed.y, placed.x + w, placed.y + d))
        out.append(rects)
    return out


def assert_no_overlap(rects: list[tuple[float, float, float, float]]) -> None:
    for a, b in itertools.combinations(rects, 2):
        assert a[2] + GAP_MM <= b[0] + 1e-6 or b[2] + GAP_MM <= a[0] + 1e-6 \
            or a[3] + GAP_MM <= b[1] + 1e-6 or b[3] + GAP_MM <= a[1] + 1e-6, (a, b)


def test_one_part_alone_keeps_its_own_plates() -> None:
    assert arrange([PackItem(part=part("a", 10, 10, plates=3))], DEFAULT) == Layout(own="a")


def test_arrange_packs_even_one_object() -> None:
    # Arrange passes allow_own=False: the piece as rendered would skip the writer, and
    # with it the pinned filament order (Review Focus 4).
    layout = arrange([PackItem(part=part("a", 10, 10))], DEFAULT, allow_own=False)
    assert layout.own is None
    assert [[p.piece_key for p in plate.items] for plate in layout.plates] == [["a"]]


def test_copies_never_overlap_and_fill_a_plate_before_starting_another() -> None:
    a = part("a", 60, 40)
    layout = arrange([PackItem(part=a, count=12)], DEFAULT)
    assert len(layout.plates) == 1
    assert_no_overlap(footprints(layout, {"a": a})[0])


def test_a_long_part_is_turned_to_share_a_plate() -> None:
    # 240 x 60 beside 150 x 200 fits one 256 mm plate only turned (60 x 240 in the strip
    # right of the square); unturned it needs a second plate.
    long, square = part("long", 240, 60), part("sq", 150, 200)
    layout = arrange([PackItem(part=long), PackItem(part=square)], DEFAULT)
    assert len(layout.plates) == 1
    assert {p.piece_key: p.rot for p in layout.plates[0].items}["long"] == 90.0
    assert_no_overlap(footprints(layout, {"long": long, "sq": square})[0])


def test_a_part_no_turn_fits_is_refused_with_the_plate_saying_why() -> None:
    with pytest.raises(PackError, match="larger than the plate"):
        arrange([PackItem(part=part("a", 300, 10)), PackItem(part=part("b", 1, 1))], DEFAULT)


def test_a_multi_plate_part_cannot_share() -> None:
    with pytest.raises(PackError, match="its own 2 plates"):
        arrange([PackItem(part=part("a", 10, 10, plates=2)), PackItem(part=part("b", 1, 1))], DEFAULT)


def test_an_unknown_goal_names_the_goals() -> None:
    with pytest.raises(PackError, match="fewest_swaps"):
        arrange([PackItem(part=part("a", 1, 1), count=2)], DEFAULT, goal="prettiest")


@pytest.mark.parametrize("model", ["H2C", "X1C", "A1 mini"])
@pytest.mark.parametrize("goal", GOALS)
def test_every_packed_plate_places_on_the_real_printer(model: str, goal: str) -> None:
    size = plate(model)
    geometry = geometry_of(size)
    parts = {
        "red": part("red", 70, 50, "#FF0000", h=20),
        "two": part("two", 90, 35, "#FF0000", "#FFFFFF", h=10),
        "white": part("white", 30, 110, "#FFFFFF", h=15),
    }
    items = [PackItem(part=parts["red"], count=5, group="a"), PackItem(part=parts["two"], count=4, group="b"),
             PackItem(part=parts["white"], count=6, group="a" if goal != "keep_together" else "c")]
    layout = arrange(items, size, goal=goal)
    placed = sum(len(p.items) for p in layout.plates)
    assert placed == 15
    for rects, lp in zip(footprints(layout, parts), layout.plates, strict=True):
        assert_no_overlap(rects)
        colours = {c for i in lp.items for c in parts[i.piece_key].colours}
        block = (max(r[2] for r in rects) - min(r[0] for r in rects),
                 max(r[3] for r in rects) - min(r[1] for r in rects),
                 max(parts[i.piece_key].bbox.size[2] for i in lp.items))
        assert fit_problem(block, geometry, tower=len(colours) > 1) is None


def test_by_colour_gives_single_colour_plates_that_need_no_tower() -> None:
    red, white = part("red", 50, 50, "#FF0000"), part("white", 50, 50, "#FFFFFF")
    layout = arrange([PackItem(part=red, count=3), PackItem(part=white, count=3)], DEFAULT, goal="by_colour")
    per_plate = [{i.piece_key for i in p.items} for p in layout.plates]
    assert per_plate == [{"red"}, {"white"}]
    mixed = arrange([PackItem(part=red, count=3), PackItem(part=white, count=3)], DEFAULT)
    assert len(mixed.plates) == 1


def test_fewest_swaps_lets_a_subset_ride_on_a_plate_that_already_has_its_filament() -> None:
    both = part("both", 60, 60, "#FF0000", "#FFFFFF")
    red = part("red", 20, 20, "#FF0000")
    blue = part("blue", 20, 20, "#0000FF")
    layout = arrange([PackItem(part=both, count=2), PackItem(part=red, count=2), PackItem(part=blue)],
                     DEFAULT, goal="fewest_swaps")
    per_plate = [sorted({i.piece_key for i in p.items}) for p in layout.plates]
    assert per_plate == [["both", "red"], ["blue"]]


def test_a_plan_makes_two_colours_on_one_spool_one_filament() -> None:
    a, b = part("a", 10, 10, "#FF0000"), part("b", 10, 10, "#FE0000")
    colours = ["#FF0000", "#FE0000"]
    assert signature_of(a, None, colours) != signature_of(b, None, colours)
    plan = SlotPlan(slots={1: 7, 2: 7})
    assert signature_of(a, plan, colours) == signature_of(b, plan, colours) == frozenset({"spool:7"})
    layout = arrange([PackItem(part=a), PackItem(part=b)], DEFAULT, goal="by_colour", plan=plan, colours=colours)
    assert len(layout.plates) == 1


def test_keep_together_keeps_a_group_on_one_plate_or_says_it_cannot() -> None:
    a, b = part("a", 100, 100), part("b", 100, 100)
    layout = arrange([PackItem(part=a, group="left"), PackItem(part=b, group="right")], DEFAULT, goal="keep_together")
    assert [[i.piece_key for i in p.items] for p in layout.plates] == [["a"], ["b"]]
    with pytest.raises(PackError, match="group 'big' does not fit on one plate"):
        arrange([PackItem(part=part("c", 200, 200), count=2, group="big")], DEFAULT, goal="keep_together")


def test_plates_are_ordered_so_each_needs_the_fewest_new_filaments() -> None:
    r, w, b, rw = (frozenset(s) for s in (["r"], ["w"], ["b"], ["r", "w"]))
    # rw first (most filaments); r and w each add none after it, the earlier wins; then b, w
    assert order_plates([r, b, rw, w]) == [2, 0, 1, 3]


def test_the_same_request_packs_the_same_way() -> None:
    items = [PackItem(part=part(k, 10 + i * 7, 30, "#FF0000" if i % 2 else "#00FF00"), count=3)
             for i, k in enumerate("abcdef")]
    assert arrange(items, DEFAULT, goal="fewest_swaps") == arrange(items, DEFAULT, goal="fewest_swaps")


def test_an_explicit_plate_takes_quarter_turns() -> None:
    placed = explicit_plate([part("a", 10, 20)], [(5.0, 6.0, 90.0)])
    assert placed.items[0].rot == 90.0
    with pytest.raises(PackError, match="quarter turns"):
        explicit_plate([part("a", 10, 20)], [(0.0, 0.0, 45.0)])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_arrange_packing.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'scadbuddy.workflows.arrange'`.

- [ ] **Step 3: The payload additions**

In `workflows/models.py`:

```python
class SlotPlan(BaseModel):
    """The print-flow spec's `FilamentPlan` as a workflow payload: one spool per 1-based
    filament slot. `bambuddy.filaments` imports the client, which must not enter the
    workflow sandbox; `of` is the bridge."""

    slots: dict[int, int] = Field(default_factory=dict)

    @classmethod
    def of(cls, plan: object) -> SlotPlan | None:
        if plan is None or isinstance(plan, SlotPlan):
            return plan
        data = plan.model_dump() if isinstance(plan, BaseModel) else plan
        if not isinstance(data, Mapping):
            raise ValueError("filament_plan must be a FilamentPlan or {slots: [{slot_id, spool_id}]}")
        slots = data.get("slots", [])
        if isinstance(slots, Mapping):
            return cls(slots={int(k): int(v) for k, v in slots.items()})
        return cls(slots={int(s["slot_id"]): int(s["spool_id"]) for s in slots})
```

Add `rot: float = 0.0` to `Placed`, `group: str | None = None` to `PackItem`, and to `PackRequest`:

```python
    filament_plan: SlotPlan | None = None
    #: The filament order slot numbers refer to (`#RRGGBB`); the writer keeps it.
    colours: list[str] = Field(default_factory=list)
```

and, after `colours`:

```python
    #: False (Arrange): always pack onto plates, even one object with one copy, so the
    #: writer applies `colours`. True keeps phase 4's shortcut for a pipeline's lone part.
    allow_own: bool = True
```

(Import `Mapping` from `collections.abc`.)

- [ ] **Step 4: `workflows/arrange.py`**

```python
"""Arrange's packer (spec 2026-09-27 §7): objects onto plates, for a goal.

Heuristic, not a solver. Copies are grouped by filament signature (their spools under a
plan, else their colours), placed first-fit-decreasing into MaxRects free space with a
quarter turn when that fits better, and a plate only takes a copy if `render/plate.py`'s
`fit_problem` still passes for the plate's block with the prime tower that plate needs
(more than one colour). That is the same check `write_plates_3mf` makes, so what is
packed is always writable. Plates are then ordered so each needs the fewest filaments
the previous did not. A goal is two functions: which plates a copy may join, and
whether plates are reordered.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from scadbuddy.render.job_models import ManifestObject
from scadbuddy.render.plate import PlateGeometry, fit_problem, plate_for
from scadbuddy.template import Part
from scadbuddy.workflows.models import Layout, LayoutPlate, PackItem, Placed, PlateSize, SlotPlan
from scadbuddy.workflows.packing import GAP_MM, PackError

GOALS: tuple[str, ...] = ("fewest_plates", "fewest_swaps", "by_colour", "keep_together")
_EPS = 1e-6


def geometry_of(size: PlateSize) -> PlateGeometry:
    return plate_for(None if size.key == "default" else size.key)


def signature_of(part: Part, plan: SlotPlan | None, colours: Sequence[str]) -> frozenset[str]:
    """The filaments ``part`` needs: its spools under ``plan``, else its colours."""
    order = [c.upper() for c in colours]
    needs: set[str] = set()
    for colour in part.colours:
        slot = order.index(colour.upper()) + 1 if colour.upper() in order else None
        spool = plan.slots.get(slot) if plan is not None and slot is not None else None
        needs.add(f"spool:{spool}" if spool is not None else f"colour:{colour.upper()}")
    return frozenset(needs)


def part_of(obj: ManifestObject) -> Part:
    return Part(piece_key=obj.part, file=obj.file, bbox=obj.bbox, colours=list(obj.colours),
                notes=list(obj.notes), plates=obj.plates)


@dataclass(frozen=True)
class _Copy:
    part: Part
    group: str | None
    signature: frozenset[str]
    order: int

    @property
    def size(self) -> tuple[float, float]:
        return self.part.bbox.size[0], self.part.bbox.size[1]


@dataclass(frozen=True)
class _Free:
    x: float
    y: float
    w: float
    d: float

    def contains(self, other: _Free) -> bool:
        return (self.x <= other.x + _EPS and self.y <= other.y + _EPS
                and self.x + self.w >= other.x + other.w - _EPS
                and self.y + self.d >= other.y + other.d - _EPS)


@dataclass
class _Sheet:
    width: float
    depth: float
    group: str | None
    signature: frozenset[str]
    free: list[_Free] = field(default_factory=list)
    placed: list[tuple[_Copy, float, float, float]] = field(default_factory=list)  # copy, x, y, rot
    colours: set[str] = field(default_factory=set)
    height: float = 0.0
    extent: tuple[float, float] = (0.0, 0.0)

    def __post_init__(self) -> None:
        # The gap trails every part, so the sheet is one gap larger than the plate.
        self.free = [_Free(0.0, 0.0, self.width + GAP_MM, self.depth + GAP_MM)]

    def spot(self, w: float, d: float) -> _Free | None:
        """Best-short-side-fit: the free rectangle leaving the least on its tighter side."""
        best: tuple[float, float, float, _Free] | None = None
        for free in self.free:
            if w + GAP_MM <= free.w + _EPS and d + GAP_MM <= free.d + _EPS:
                score = (min(free.w - w, free.d - d), free.y, free.x)
                if best is None or score < best[:3]:
                    best = (*score, free)
        return best[3] if best else None

    def occupy(self, used: _Free) -> None:
        pieces: list[_Free] = []
        for free in self.free:
            if (used.x >= free.x + free.w - _EPS or used.x + used.w <= free.x + _EPS
                    or used.y >= free.y + free.d - _EPS or used.y + used.d <= free.y + _EPS):
                pieces.append(free)
                continue
            if used.x > free.x + _EPS:
                pieces.append(_Free(free.x, free.y, used.x - free.x, free.d))
            if used.x + used.w < free.x + free.w - _EPS:
                pieces.append(_Free(used.x + used.w, free.y, free.x + free.w - used.x - used.w, free.d))
            if used.y > free.y + _EPS:
                pieces.append(_Free(free.x, free.y, free.w, used.y - free.y))
            if used.y + used.d < free.y + free.d - _EPS:
                pieces.append(_Free(free.x, used.y + used.d, free.w, free.y + free.d - used.y - used.d))
        self.free = [p for i, p in enumerate(pieces)
                     if not any(j != i and q.contains(p) and (q != p or j < i) for j, q in enumerate(pieces))]


def _try(sheet: _Sheet, copy: _Copy, geometry: PlateGeometry) -> bool:
    """Place ``copy`` on ``sheet`` if a turn of it fits and the plate stays writable."""
    w, d = copy.size
    turns = [(w, d, 0.0)] + ([(d, w, 90.0)] if abs(w - d) > _EPS else [])
    options = [(spot, tw, td, rot) for tw, td, rot in turns if (spot := sheet.spot(tw, td)) is not None]
    options.sort(key=lambda o: (max(sheet.extent[0], o[0].x + o[1]) * max(sheet.extent[1], o[0].y + o[2]), o[3]))
    colours = sheet.colours | {c.upper() for c in copy.part.colours}
    height = max(sheet.height, copy.part.bbox.size[2])
    for spot, tw, td, rot in options:
        extent = (max(sheet.extent[0], spot.x + tw), max(sheet.extent[1], spot.y + td))
        if fit_problem((extent[0], extent[1], height), geometry, tower=len(colours) > 1) is not None:
            continue
        sheet.occupy(_Free(spot.x, spot.y, tw + GAP_MM, td + GAP_MM))
        sheet.placed.append((copy, spot.x, spot.y, rot))
        sheet.colours, sheet.height, sheet.extent = colours, height, extent
        return True
    return False


def _joins(goal: str) -> Callable[[_Sheet, _Copy], bool]:
    if goal == "fewest_plates":
        return lambda sheet, copy: True
    if goal == "by_colour":
        return lambda sheet, copy: sheet.signature == copy.signature
    if goal == "fewest_swaps":
        return lambda sheet, copy: copy.signature <= sheet.signature
    return lambda sheet, copy: sheet.group == copy.group


def order_plates(signatures: Sequence[frozenset[str]]) -> list[int]:
    """Greedy: start with the plate needing the most filaments, then always the plate
    that adds the fewest the previous one did not have (ties: the earlier plate)."""
    left = list(range(len(signatures)))
    if not left:
        return []
    current = min(left, key=lambda i: (-len(signatures[i]), i))
    ordered = [current]
    left.remove(current)
    while left:
        prev = signatures[current]
        current = min(left, key=lambda i: (len(signatures[i] - prev), -len(signatures[i] & prev), i))
        ordered.append(current)
        left.remove(current)
    return ordered


def arrange(
    items: Sequence[PackItem],
    plate: PlateSize,
    *,
    goal: str = "fewest_plates",
    plan: SlotPlan | None = None,
    colours: Sequence[str] = (),
    allow_own: bool = True,
) -> Layout:
    if goal not in GOALS:
        raise PackError(f"pack goal {goal!r} is not one of {', '.join(GOALS)}")
    lone = len(items) == 1 and items[0].count == 1
    if allow_own and lone and (goal == "fewest_plates" or items[0].part.plates > 1):
        return Layout(own=items[0].part.piece_key)
    for item in items:
        if item.part.plates > 1:
            raise PackError(
                f"{item.part.file} lays out its own {item.part.plates} plates; pack it on its own"
            )
    geometry = geometry_of(plate)
    order = list(colours) or list(dict.fromkeys(c for item in items for c in item.part.colours))
    copies = [
        _Copy(item.part, item.group if goal == "keep_together" else None,
              signature_of(item.part, plan, order), n)
        for n, item in enumerate(i for i in items for _ in range(i.count))
    ]
    copies.sort(key=lambda c: (-len(c.signature) if goal == "fewest_swaps" else 0,
                               -(c.size[0] * c.size[1]), -max(c.size), c.order))
    joins = _joins(goal)
    sheets: list[_Sheet] = []
    for copy in copies:
        if any(joins(s, copy) and _try(s, copy, geometry) for s in sheets):
            continue
        sheet = _Sheet(geometry.usable.width, geometry.usable.depth, copy.group, copy.signature)
        if not _try(sheet, copy, geometry):
            w, d = copy.size
            why = fit_problem((w, d, copy.part.bbox.size[2]), geometry,
                              tower=len(copy.part.colours) > 1) or "no turn of it fits"
            raise PackError(
                f"{copy.part.file} ({w:.0f} x {d:.0f} mm) is larger than the plate "
                f"({plate.width:.0f} x {plate.depth:.0f} mm): {why}"
            )
        sheets.append(sheet)
    if goal == "keep_together":
        seen: dict[str | None, int] = {}
        for sheet in sheets:
            seen[sheet.group] = seen.get(sheet.group, 0) + 1
        for group, count in seen.items():
            if group is not None and count > 1:
                raise PackError(f"group {group!r} does not fit on one plate")
    indices = (order_plates([s.signature for s in sheets]) if goal in ("fewest_swaps", "by_colour")
               else list(range(len(sheets))))
    return Layout(plates=[
        LayoutPlate(items=[
            Placed(piece_key=c.part.piece_key, x=round(x, 3), y=round(y, 3), rot=rot)
            for c, x, y, rot in sorted(sheets[i].placed, key=lambda p: p[0].order)
        ])
        for i in indices
    ])
```

For `fewest_swaps`, a sheet takes the signature of the copy that opened it; copies are sorted widest signature first, so a subset (red) joins a superset's plate (red+white) and adds no swap.

- [ ] **Step 5: `packing.py` and `pack_layout`**

In `workflows/packing.py`, delete `shelf_pack` (Arrange replaces it, §7) and change `explicit_plate`'s rotation check and its `Placed(...)`:

```python
    if any(rot % 90 for _, _, rot in at):
        raise PackError("plate_of rotates in quarter turns: 0, 90, 180 or 270")
    return LayoutPlate(items=[
        Placed(piece_key=p.piece_key, x=x, y=y, rot=float(rot) % 360)
        for p, (x, y, rot) in zip(parts, at, strict=True)
    ])
```

In `workflows/pipeline_activities.py`:

```python
from scadbuddy.workflows.arrange import arrange


def pack_layout(req: PackRequest) -> Layout:
    try:
        return arrange(
            req.items, req.plate, goal=req.goal, plan=req.filament_plan, colours=req.colours,
            allow_own=req.allow_own,
        )
    except PackError as error:
        raise ApplicationError(str(error), type="PackError", non_retryable=True) from None
```

In `tests/test_packing_and_outputs.py` (phase 4), six tests exercise the packers: five call
`shelf_pack` and one calls `explicit_plate`. Delete `test_parts_are_packed_in_rows_without_overlap`
and `test_a_full_plate_starts_another` (they pin shelf coordinates;
`test_copies_never_overlap…` and `test_every_packed_plate…` replace them). The four that remain
become, with the import line changed to
`from scadbuddy.workflows.packing import GAP_MM, PackError, explicit_plate` plus
`from scadbuddy.workflows.arrange import arrange` (drop `GAP_MM` if nothing else in the module
uses it):

```python
def test_one_part_alone_keeps_its_own_plates() -> None:
    assert arrange([PackItem(part=_part("a", 10, 10, plates=3))], PLATE) == Layout(own="a")


def test_a_part_larger_than_the_plate_is_refused() -> None:
    with pytest.raises(PackError, match="larger than the plate"):
        arrange([PackItem(part=_part("a", 300, 10)), PackItem(part=_part("b", 1, 1))], PLATE)


def test_a_multi_plate_part_cannot_share() -> None:
    with pytest.raises(PackError, match="its own 2 plates"):
        arrange([PackItem(part=_part("a", 10, 10, plates=2)), PackItem(part=_part("b", 1, 1))], PLATE)


def test_an_explicit_plate_places_where_told() -> None:
    plate = explicit_plate([_part("a", 10, 10)], [(20.0, 30.0, 0.0)])
    assert plate.items[0].x == 20.0 and plate.items[0].y == 30.0
    with pytest.raises(PackError, match="quarter turns"):
        explicit_plate([_part("a", 10, 10)], [(0.0, 0.0, 45.0)])
```

In phase 4's `tests/test_template_pipeline.py`, the third case of
`test_a_restricted_call_fails_the_job_with_its_line` packs nothing with `goal='fewest_swaps'`,
which Arrange now accepts (an empty layout). Change it to a goal no phase knows; the pack
activity's `PackError` still fails the job at line 2:

```python
    ("async def run(ctx, inputs):\n    await ctx.pack([], goal='prettiest')\n",
     "pipeline/pipeline.py:2: "),
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/test_arrange_packing.py tests/test_packing_and_outputs.py tests/test_template_pipeline.py -q`
Expected: all pass (the parametrised real-plate test runs 12 cases).

- [ ] **Step 7: Gates and commit**

Run `cd backend && uv run --frozen ruff format .`, then the backend gates (Global Constraints).

```bash
git add backend/scadbuddy/workflows/ backend/tests/test_arrange_packing.py backend/tests/test_packing_and_outputs.py backend/tests/test_template_pipeline.py
git commit -m "feat(arrange): goal-driven packer with quarter turns, checked against the real plate (#428)"
```

---

### Task 3: The writer turns parts and keeps the filament order

**Files:**
- Modify: `backend/scadbuddy/workflows/models.py`, `backend/scadbuddy/workflows/outputs.py`
- Test: `backend/tests/test_arrange_outputs.py` (append)

**Interfaces:**
- Consumes: `Placed.rot`, `PackRequest.colours` (Task 2); phase 4's `_write_plates`, `OutputRequest`.
- Produces:
  ```python
  OutputRequest.colours: list[str] = []            # defined here, additive to phase 4's model
  def placement_matrix(box: BoundingBox, x: float, y: float, rot: float) -> np.ndarray   # workflows/outputs.py
  ```
  With `req.colours` set, the output's filament list starts with exactly those colours, in that order; colours no listed one covers are appended after them.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_arrange_outputs.py`:

```python
import json
import zipfile

import numpy as np

from scadbuddy.render.bambu3mf import PROJECT_SETTINGS_NAME
from scadbuddy.render.glb import BoundingBox
from scadbuddy.workflows.models import LayoutPlate, Placed
from scadbuddy.workflows.outputs import placement_matrix


def test_a_quarter_turn_lands_the_turned_box_where_it_was_placed() -> None:
    box = BoundingBox(min=(-5, -10, 2), max=(5, 10, 7), size=(10, 20, 5))
    corners = np.array([[x, y, z, 1] for x in (-5, 5) for y in (-10, 10) for z in (2, 7)]).T
    moved = (placement_matrix(box, 30.0, 40.0, 90.0) @ corners)[:3]
    assert np.allclose(moved.min(axis=1), (30, 40, 0))
    assert np.allclose(moved.max(axis=1), (50, 50, 5))  # 20 wide, 10 deep once turned
    still = (placement_matrix(box, 30.0, 40.0, 0.0) @ corners)[:3]
    assert np.allclose(still.min(axis=1), (30, 40, 0)) and np.allclose(still.max(axis=1), (40, 60, 5))


async def test_an_arranged_output_keeps_the_filament_order_it_was_planned_against(tmp_path: Path) -> None:
    deps, paths = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    own = part.colours[0]
    planned = ["#123456", own]  # the output the plan was made for had own colour in slot 2
    req = OutputRequest(
        job_id="j3", index=0, slug="demo",
        layout=Layout(plates=[LayoutPlate(items=[Placed(piece_key=part.piece_key, x=0, y=0, rot=90.0),
                                                 Placed(piece_key=part.piece_key, x=40, y=0)])]),
        parts=[part], name=None, bom=[], files={}, record=_record([part.piece_key]), colours=planned,
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
    layout = pack_layout(PackRequest(
        items=[PackItem(part=part)], plate=PlateSize(key="default", width=256.0, depth=256.0),
        colours=planned, allow_own=False,
    ))
    assert layout.own is None
    req = OutputRequest(
        job_id="j4", index=0, slug="demo", layout=layout, parts=[part], name=None, bom=[],
        files={}, record=_record([part.piece_key]), colours=planned,
    )
    out = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert out.result.colors[:2] == planned
    with zipfile.ZipFile(paths.root / out.result.model_3mf) as archive:
        settings = json.loads(archive.read(PROJECT_SETTINGS_NAME))
    assert [c.upper() for c in settings["filament_colour"][:2]] == planned
```

(`bambu3mf.PROJECT_SETTINGS_NAME` holds JSON with the colour list under `"filament_colour"`.
Add `PackItem`, `PackRequest`, `PlateSize` to the `scadbuddy.workflows.models` import and
`from scadbuddy.workflows.pipeline_activities import pack_layout` beside `PipelineActivities`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_arrange_outputs.py -q -k "quarter or filament_order"`
Expected: `ImportError: cannot import name 'placement_matrix'` (and, run alone, the `one_object_arranged_alone` case fails on `PackRequest` having no field `allow_own` until Task 2 lands; Task 2 comes first, so in order it fails on the import).

- [ ] **Step 3: Implement**

In `workflows/models.py`, add to `OutputRequest`:

```python
    #: The filament order to keep (slot N = colours[N-1]); Arrange passes the order the
    #: plan was chosen against. Empty: first appearance, as phase 4 wrote it.
    colours: list[str] = Field(default_factory=list)
```

In `workflows/outputs.py`:

```python
import math

import numpy as np

from scadbuddy.render.glb import BoundingBox


def placement_matrix(box: BoundingBox, x: float, y: float, rot: float) -> np.ndarray:
    """Turn a part ``rot`` degrees about Z, then move it so its turned box's low corner
    is at ``(x, y)`` and it sits on the plate (z = 0)."""
    turn = trimesh.transformations.rotation_matrix(math.radians(rot), (0, 0, 1))
    corners = np.array([[bx, by, 0.0, 1.0] for bx in (box.min[0], box.max[0]) for by in (box.min[1], box.max[1])]).T
    turned = turn @ corners
    move = trimesh.transformations.translation_matrix(
        (x - float(turned[0].min()), y - float(turned[1].min()), -float(box.min[2]))
    )
    return move @ turn
```

In `_write_plates`, colours are matched case-insensitively by upper-casing both sides: the
planned order and every part's colour. `render/split.py` and `render/solids.py` already write
`#RRGGBB` upper-case, so a phase 4 output is unchanged; a plan or a caller that spells a colour
in lower case still lands on the same slot. Replace `colours: list[str] = []` with:

```python
    colours: list[str] = [c.upper() for c in req.colours]
```

and replace the placement and inner loop (phase 4's lines from `box = parts[placed.piece_key].bbox`
through `colours.append(part.colour)`) with:

```python
            matrix = placement_matrix(parts[placed.piece_key].bbox, placed.x, placed.y, placed.rot)
            for part in layouts[placed.piece_key].plates[0].parts:
                mesh = part.mesh.copy()
                mesh.apply_transform(matrix)
                colour = part.colour.upper()
                by_colour.setdefault(colour, []).append(mesh)
                names.setdefault(colour, part.name)
                if colour not in colours:
                    colours.append(colour)
```

The rest of `_write_plates` (`ordered = sorted(by_colour, key=colours.index)` onwards) is
unchanged: it reads `by_colour`, `names` and `colours`, all keyed by the upper-cased colour.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/test_arrange_outputs.py tests/test_packing_and_outputs.py -q`
Expected: all pass; phase 4's outputs (no `rot`, no `colours`) are unchanged.

- [ ] **Step 5: Gates and commit**

Run `cd backend && uv run --frozen ruff format .`, then the backend gates (Global Constraints).

```bash
git add backend/scadbuddy/workflows/ backend/tests/test_arrange_outputs.py
git commit -m "feat(arrange): the writer turns parts and keeps a planned filament order (#428)"
```

---

### Task 4: The `Arrange` workflow, and a row runs its own kind

**Files:**
- Modify: `backend/scadbuddy/workflows/models.py`, `backend/scadbuddy/workflows/pipelines.py`, `backend/scadbuddy/workflows/client.py`, `backend/scadbuddy/render/inputs.py`, `backend/scadbuddy/render/submit.py`
- Test: `backend/tests/test_arrange_workflow.py` (`requires_temporal`; the service tests also `requires_postgres`)

**Interfaces:**
- Consumes: `PackRequest`, `SlotPlan`, `OutputRequest.colours/.provenance` (Tasks 1–3); `ManifestObject` (Task 1); phase 1's `SHORT`, `RETRY`, `PROJECT_RETRY`, `_openscad_timeout`, `_failure_of`, `render_worker`, `RenderService._start`, `reconcile_once`, `workflow_id_for`; phase 4's `Projection.outputs/.blob_keys`, `OutputRecord`, `PipelineOutput`, `FakeWorld`.
- Produces:
  ```python
  # workflows/models.py
  ARRANGE_VERSION = "arrange"
  class ArrangeInputs(BaseModel):
      items: list[PackItem]; goal: str = "fewest_plates"; plate: PlateSize; plate_model: str | None = None
      filament_plan: SlotPlan | None = None; colours: list[str] = []; name: str | None = None
      provenance: dict[str, ManifestObject] = {}; sources: list[str] = []
  # workflows/pipelines.py
  @workflow.defn(name="Arrange") class Arrange: async def run(self, job: Job) -> None
  # render/inputs.py
  def arrange_key(slug: str, inputs: Mapping[str, Any]) -> str
  # render/submit.py
  async def RenderService.arrange(self, slug: str, inputs: ArrangeInputs) -> Job
  ```
  `RenderService._start` starts workflow `"Arrange"` for a `kind == "arrange"` row and `TemplatePipeline.run` otherwise; the reconciler calls `_start`, so it follows.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_arrange_workflow.py`:

```python
"""Arrange runs as a workflow on its own render_jobs row (spec 2026-09-27 §3.4, §7)."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.worker import Worker

from scadbuddy.core.config import Config
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.render.job_models import Job, now
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.submit import RenderService
from scadbuddy.workflows.models import ArrangeInputs, PackItem, PlateSize
from scadbuddy.workflows.pipelines import Arrange
from tests.support.pipelines import FakeWorld
from tests.support.temporal import temporal_client
from tests.test_arrange_packing import part
from tests.test_submit import projection  # noqa: F401  (the fixture)

PLATE = PlateSize(key="default", width=256.0, depth=256.0)


def _inputs(goal: str = "fewest_plates") -> ArrangeInputs:
    return ArrangeInputs(
        items=[PackItem(part=part("a", 40, 40), count=3), PackItem(part=part("b", 30, 30, "#FFFFFF"))],
        goal=goal, plate=PLATE, colours=["#FF0000", "#FFFFFF"], name="together", sources=["o1"],
    )


def _job(inputs: ArrangeInputs) -> Job:
    return Job(id=uuid.uuid4().hex, slug="demo", kind="arrange",
               inputs=inputs.model_dump(mode="json"), created_at=now())


async def _run(world: FakeWorld, job: Job) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(client, task_queue=queue, workflows=[Arrange], activities=world.activities()):
            await client.execute_workflow(Arrange.run, job, id=f"render-{job.id}", task_queue=queue)


@pytest.mark.requires_temporal
async def test_an_arrange_job_packs_and_writes_one_output_with_no_render() -> None:
    world = FakeWorld(None)
    job = _job(_inputs())
    await _run(world, job)
    assert world.pieces == []  # nothing rendered
    [req] = world.outputs
    assert req.colours == ["#FF0000", "#FFFFFF"] and req.name == "together"
    assert sum(len(p.items) for p in req.layout.plates) == 4
    assert req.record.pipeline_version == "arrange" and req.record.plate_key == "default"
    done = world.projections[-1]
    assert done.state == "done" and len(done.outputs) == 1
    assert {"a", "b"} <= set(done.blob_keys)


@pytest.mark.requires_temporal
async def test_an_arrange_that_cannot_pack_fails_with_the_reason() -> None:
    world = FakeWorld(None)
    inputs = _inputs()
    inputs.items.append(PackItem(part=part("huge", 400, 10)))
    await _run(world, _job(inputs))
    failed = world.projections[-1]
    assert failed.state == "failed" and failed.failure is not None
    assert "larger than the plate" in failed.failure.error
    assert world.outputs == []


@pytest.mark.requires_postgres
async def test_the_reconciler_starts_an_arrange_row_as_arrange(
    projection: JobProjection, tmp_path: Path  # noqa: F811
) -> None:
    client = MagicMock()
    client.start_workflow = AsyncMock(side_effect=RuntimeError("temporal is down"))
    svc = RenderService(projection=projection, client=client, task_queue="q",
                        config=Config(data_dir=tmp_path), paths=DataPaths(tmp_path),
                        metrics=Metrics(), reconcile_after=0.0)
    job = await svc.arrange("demo", _inputs())
    assert client.start_workflow.await_args.args[0] == "Arrange"
    client.start_workflow.side_effect = None
    client.start_workflow.reset_mock()
    assert await svc.reconcile_once() == 1
    assert client.start_workflow.await_args.args[0] == "Arrange"
    assert client.start_workflow.await_args.kwargs["id"] == f"render-{job.id}"


@pytest.mark.requires_postgres
async def test_an_identical_arrange_coalesces(projection: JobProjection, tmp_path: Path) -> None:  # noqa: F811
    client = MagicMock()
    client.start_workflow = AsyncMock()
    svc = RenderService(projection=projection, client=client, task_queue="q",
                        config=Config(data_dir=tmp_path), paths=DataPaths(tmp_path), metrics=Metrics())
    first = await svc.arrange("demo", _inputs())
    second = await svc.arrange("demo", _inputs())
    third = await svc.arrange("demo", _inputs("by_colour"))
    assert first.id == second.id != third.id
    assert first.kind == "arrange"
    assert client.start_workflow.await_count == 2
```

If phase 4's `FakeWorld` needs its workflows run through a particular worker set-up (a `workflow_runner`), build the `Worker` in `_run` exactly as `tests/support/pipelines.run_job` does, with `workflows=[Arrange]`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/test_arrange_workflow.py -q`
Expected: `ImportError: cannot import name 'ArrangeInputs'`.

- [ ] **Step 3: `ArrangeInputs` and `arrange_key`**

In `workflows/models.py`:

```python
#: `pipeline_version` of an arranged output's record: no template code ran.
ARRANGE_VERSION = "arrange"


class ArrangeInputs(BaseModel):
    """An arrange job's `render_jobs.inputs` (spec §7): objects, goal, plate and plan."""

    items: list[PackItem]
    goal: str = "fewest_plates"
    plate: PlateSize
    #: The printer model the 3MF is laid out for (`plate_for`); None is the default plate.
    plate_model: str | None = None
    filament_plan: SlotPlan | None = None
    colours: list[str] = Field(default_factory=list)
    name: str | None = None
    provenance: dict[str, ManifestObject] = Field(default_factory=dict)
    #: The outputs the objects came from.
    sources: list[str] = Field(default_factory=list)
```

In `render/inputs.py`, beside `inputs_key`:

```python
def arrange_key(slug: str, inputs: Mapping[str, Any]) -> str:
    """An arrange job's key: identical requests coalesce like renders (§3.3)."""
    raw = json.dumps(["arrange", slug, dict(inputs)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: The workflow**

In `workflows/pipelines.py` (import `ArrangeInputs`, `ARRANGE_VERSION`, `PackRequest`, `Layout`, `OutputRequest` from `workflows.models` and `OutputRecord`, `PipelineOutput` from `render.job_models` inside the module's existing `imports_passed_through` block):

```python
@workflow.defn(name="Arrange")
class Arrange:
    """Objects from saved outputs onto plates for a goal, then one 3MF (spec §7). No
    piece is rendered: the Parts are in the store already."""

    @workflow.run
    async def run(self, job: Job) -> None:
        inputs = ArrangeInputs.model_validate(job.inputs)

        async def project(**fields: object) -> None:
            await workflow.execute_activity(
                "project",
                Projection.model_validate({"job_id": job.id, "slug": job.slug,
                                           "pipeline_version": ARRANGE_VERSION, **fields}),
                start_to_close_timeout=SHORT,
                retry_policy=PROJECT_RETRY,
            )

        steps = [StepInfo(name="arrange", state="running", done=0, total=2)]
        try:
            await project(state="running", steps=steps)
            layout = await workflow.execute_activity(
                "pack",
                # Always plates, never the piece as rendered: only the writer applies
                # the pinned colour order (Review Focus 4).
                PackRequest(items=inputs.items, plate=inputs.plate, goal=inputs.goal,
                            filament_plan=inputs.filament_plan, colours=inputs.colours,
                            allow_own=False),
                result_type=Layout,
                start_to_close_timeout=SHORT,
                retry_policy=RETRY,
            )
            steps[0].done = 1
            await project(steps=steps)
            parts = list({item.part.piece_key: item.part for item in inputs.items}.values())
            keys = [p.piece_key for p in parts]
            written = await workflow.execute_activity(
                "write_output",
                OutputRequest(
                    job_id=job.id, index=0, slug=job.slug, layout=layout, parts=parts,
                    name=inputs.name, bom=[], files={}, plate_model=inputs.plate_model,
                    colours=inputs.colours, provenance=inputs.provenance,
                    record=OutputRecord(revision=None, ui_api=None, pipeline_api=0,
                                        pipeline_version=ARRANGE_VERSION, inputs_v=0,
                                        plate_key=inputs.plate.key, parts=keys),
                ),
                result_type=PipelineOutput,
                start_to_close_timeout=_openscad_timeout(),
                retry_policy=RETRY,
            )
            steps[0].state, steps[0].done = "done", 2
            await project(state="done", result=written.result, outputs=[written],
                          blob_keys=list(dict.fromkeys([*keys, *written.blob_keys])), steps=steps)
        except (asyncio.CancelledError, ActivityError) as error:
            if is_cancelled_exception(error) and workflow.cancellation_reason() is not None:
                await project(state="cancelled", failure=Failure(error="cancelled"), steps=steps)
                raise
            if not isinstance(error, ActivityError):
                raise
            steps[0].state = "failed"
            cause = error.cause
            message = cause.message if isinstance(cause, ApplicationError) else str(error)
            await project(state="failed", failure=Failure(error=message), steps=steps)
```

(`ApplicationError`, `ActivityError`, `is_cancelled_exception`, `Failure`, `StepInfo` are imported by `TemplatePipeline` already.) In `workflows/client.py`'s `render_worker`, add `Arrange` to `workflows=[…]`.

- [ ] **Step 5: `RenderService.arrange` and the kind-aware start**

In `render/submit.py` (import `ArrangeInputs` and `arrange_key`):

```python
    async def arrange(self, slug: str, inputs: ArrangeInputs) -> Job:
        """Insert an `arrange` row (or coalesce onto an identical one), then start it —
        the same insert → start → reconcile path as a render (spec §3.3, §3.4)."""
        payload = inputs.model_dump(mode="json")
        job = Job(id=uuid.uuid4().hex, slug=slug, kind="arrange", inputs=payload, created_at=now())
        try:
            submitted = await asyncio.to_thread(
                self.store.submit, job, arrange_key(slug, payload),
                max_pending=self.config.render_queue_max,
            )
        except QueueFullError as error:
            self.metrics.render_rejected.inc()
            raise QueueFullError(error.depth, self.retry_after()) from None
        if submitted.coalesced:
            self.metrics.render_coalesced.inc()
            return submitted.job
        self.metrics.render_submitted.inc()
        try:
            await self._start(submitted.job)
        except Exception:
            logger.exception("could not start an arrange's workflow; the reconciler will",
                             extra={"job_id": submitted.job.id})
            self.metrics.store_errors.labels("start_workflow").inc()
        return submitted.job
```

In `_start`, start the row's own workflow:

```python
        assert self.client is not None
        if job.kind == "arrange":
            await self.client.start_workflow(
                "Arrange", job, id=workflow_id_for(job.id), task_queue=self.task_queue,
                id_conflict_policy=conflict, memo=self._memo(),
            )
            return
        await self.client.start_workflow(TemplatePipeline.run, job, …)   # unchanged
```

`JobProjection.submit` already writes `job.kind` (phase 1), and `stale_pending` returns whole rows, so the reconciler hands `_start` the kind.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/test_arrange_workflow.py tests/test_submit.py tests/test_template_pipeline.py -q`
Expected: all pass.

- [ ] **Step 7: Gates and commit**

Run `cd backend && uv run --frozen ruff format .`, then the backend gates (Global Constraints).

```bash
git add backend/scadbuddy/workflows/ backend/scadbuddy/render/inputs.py backend/scadbuddy/render/submit.py backend/tests/test_arrange_workflow.py
git commit -m "feat(arrange): the Arrange workflow on a kind='arrange' row, started and reconciled by kind (#428)"
```

---

### Task 5: `POST /outputs/arrange`; arranged outputs are saved like any other

**Files:**
- Modify: `backend/scadbuddy/api/outputs.py`, `agent/src/tools/coverage.ts`
- Test: `backend/tests/api/test_arrange_api.py`, `backend/tests/api/test_output_parts.py` (append)

**Interfaces:**
- Consumes: `ArrangeInputs`, `RenderService.arrange` (Task 4); `part_of`, `GOALS` (Task 2); `OutputStore.manifest`, `hold_parts`, `OutputDetail.manifest`, `finished_job`, `saved_output` (Task 1); `SlotPlan.of`; phase 4's `_job_status`, `CreateOutputRequest.index`, `plate_size`; `FilamentPlan`; `client_for`, `BambuddyClient.printer` (which declares `Scope.READ_STATUS`); `StoredSettings.printer_id` and `.default_plate`; `plate_for`.
- Produces:
  ```python
  class ArrangeObject(BaseModel): output_id: str; part: str; count: int (0..500); group: str | None = None
  MAX_ARRANGE_COPIES = 2000
  class ArrangeRequest(BaseModel): objects: list[ArrangeObject] (1..200, total count <= MAX_ARRANGE_COPIES, else 422); goal: Literal[*GOALS] = "fewest_plates"; printer_id: int | None = None
      filament_plan: FilamentPlan | None = None; colours: list[str] | None = None; name: str | None (<= 200)
  def arrange_inputs(outputs: OutputStore, body: ArrangeRequest, *, plate_model: str | None) -> tuple[str, ArrangeInputs]   # (slug, inputs); raises ApiError
  POST /api/v1/outputs/arrange  ArrangeRequest -> 202 JobStatus
  ```
  Saving an arrange job's output (`POST /models/{slug}/outputs {job_id}`) stores `inputs = {}` and `arranged_from = inputs.sources`, and the History page reads `arranged_from` to hide "Open the customizer".

- [ ] **Step 1: Write the failing tests**

`backend/tests/api/test_arrange_api.py`:

```python
"""POST /outputs/arrange (spec 2026-09-27 §10)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scadbuddy.api.outputs import ArrangeObject, ArrangeRequest, arrange_inputs
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.models import SlotChoice
from scadbuddy.core.problems import ApiError
from scadbuddy.library.outputs import OutputStore
from tests.support.arrange import saved_output


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
    assert raised.value.status == 409 and "generate it again" in raised.value.detail


async def test_nothing_to_place_is_refused(tmp_path: Path) -> None:
    paths, meta, written = await saved_output(tmp_path)
    body = ArrangeRequest(objects=[ArrangeObject(output_id=meta.id, part=written.manifest[0].part, count=0)])
    with pytest.raises(ApiError) as raised:
        arrange_inputs(OutputStore(paths), body, plate_model=None)
    assert raised.value.status == 422 and "nothing to arrange" in raised.value.detail


async def test_a_printer_model_sets_the_plate(tmp_path: Path) -> None:
    paths, meta, written = await saved_output(tmp_path)
    body = ArrangeRequest(objects=[ArrangeObject(output_id=meta.id, part=written.manifest[0].part, count=2)])
    _, inputs = arrange_inputs(OutputStore(paths), body, plate_model="H2C")
    assert inputs.plate.key == "Bambu Lab H2C" and inputs.plate_model == "H2C"


def test_an_unknown_output_is_a_404(client: TestClient) -> None:
    response = client.post("/api/v1/outputs/arrange", json={"objects": [{"output_id": "missing", "part": "p", "count": 1}]})
    assert response.status_code == 404


def test_an_unknown_goal_is_a_422(client: TestClient) -> None:
    response = client.post("/api/v1/outputs/arrange",
                           json={"objects": [{"output_id": "o", "part": "p", "count": 1}], "goal": "prettiest"})
    assert response.status_code == 422


def test_more_than_2000_copies_is_a_422(client: TestClient) -> None:
    objects = [{"output_id": "o", "part": f"p{i}", "count": 500} for i in range(5)]
    response = client.post("/api/v1/outputs/arrange", json={"objects": objects})
    assert response.status_code == 422
    assert "2500 copies" in response.text and "2000" in response.text


def test_2000_copies_is_allowed() -> None:
    objects = [ArrangeObject(output_id="o", part=f"p{i}", count=500) for i in range(4)]
    assert sum(o.count for o in ArrangeRequest(objects=objects).objects) == 2000


class Arranger:
    """A queue whose `arrange` records what the route resolved, as RenderService would
    insert it."""

    def __init__(self) -> None:
        self.inputs: list[ArrangeInputs] = []

    async def arrange(self, slug: str, inputs: ArrangeInputs) -> Job:
        self.inputs.append(inputs)
        return Job(id="arr-1", slug=slug, state="pending", created_at=now(), kind="arrange",
                   inputs=inputs.model_dump(mode="json"))


def test_with_no_printer_the_plate_falls_back_to_the_stored_default(
    client: TestClient, app: FastAPI, tmp_path: Path
) -> None:
    _, meta, written = asyncio.run(saved_output(tmp_path))
    arranger = Arranger()
    app.dependency_overrides[get_queue] = lambda: arranger
    assert client.put("/api/v1/settings", json={"default_plate": "H2C"}).status_code == 200
    response = client.post("/api/v1/outputs/arrange", json={
        "objects": [{"output_id": meta.id, "part": written.manifest[0].part, "count": 2}],
    })
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "pending"
    [inputs] = arranger.inputs
    assert inputs.plate_model == "H2C" and inputs.plate.key == "Bambu Lab H2C"
```

(`plate_for("H2C").key` is `"Bambu Lab H2C"`: `PlateGeometry.key` is the profile's model name,
checked against the baseline. Add to the module's imports: `asyncio`, `FastAPI` from `fastapi`,
`get_queue` from `scadbuddy.api.deps`, `Job`, `now` from `scadbuddy.render.job_models`,
`ArrangeInputs` from `scadbuddy.workflows.models`. The settings `PUT` answers 200 as
`tests/api/test_plates.py` uses it; the fixture settings have no `printer_id`, so the route
never calls Bambuddy.)

Append to `backend/tests/api/test_output_parts.py` (Task 1), the arranged output's side of
Review Focus 3:

```python
def test_saving_an_arrange_job_records_its_sources_and_holds_its_parts(
    client: TestClient, app: FastAPI, pool: Pool, tmp_path: Path
) -> None:
    _, job, written = asyncio.run(finished_job(tmp_path, job_id="arr-1"))
    sources = ["a" * 32, "c" * 32]
    inputs = ArrangeInputs(
        items=[PackItem(part=part_of(written.manifest[0]), count=2)],
        plate=PlateSize(key="default", width=256.0, depth=256.0), sources=sources,
    )
    arranged = job.model_copy(update={"kind": "arrange", "inputs": inputs.model_dump(mode="json")})
    app.dependency_overrides[get_queue] = lambda: OneJob(arranged)
    output_id = save(client, "arr-1")
    detail = client.get(f"/api/v1/outputs/{output_id}").json()
    assert detail["arranged_from"] == sources
    assert held(pool) == {(m.part, output_id) for m in written.manifest}
```

(Imports: `ArrangeInputs`, `PackItem`, `PlateSize` from `scadbuddy.workflows.models`, `part_of`
from `scadbuddy.workflows.arrange`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/api/test_arrange_api.py -q`
Expected: `ImportError: cannot import name 'ArrangeObject'`.

- [ ] **Step 3: Implement**

In `api/outputs.py`:

```python
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.workflows.arrange import GOALS, part_of
from scadbuddy.workflows.models import ArrangeInputs, PackItem, SlotPlan
from scadbuddy.workflows.pipeline_activities import plate_size

Goal = Literal["fewest_plates", "fewest_swaps", "by_colour", "keep_together"]
assert GOALS == get_args(Goal)
#: The most copies one arrange places, summed over its objects.
MAX_ARRANGE_COPIES = 2000


class ArrangeObject(BaseModel):
    output_id: str
    #: A `manifest` entry's `part`.
    part: str
    #: Copies to place; 0 leaves the object out.
    count: int = Field(ge=0, le=500)
    #: With `goal = keep_together`: objects sharing a group share a plate.
    group: str | None = Field(default=None, max_length=100)


class ArrangeRequest(BaseModel):
    objects: list[ArrangeObject] = Field(min_length=1, max_length=200)
    goal: Goal = "fewest_plates"
    #: The printer whose plate to pack for; omitted means the configured one, and with
    #: none configured the default plate.
    printer_id: int | None = None
    filament_plan: FilamentPlan | None = None
    #: The filament order the plan's slots refer to; omitted means the first object's
    #: output's colours, then any colour the others add.
    colours: list[str] | None = None
    name: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _copies_within_the_cap(self) -> ArrangeRequest:
        # The packer checks every candidate spot against the plate; this keeps a request
        # well inside the pack activity's SHORT timeout.
        total = sum(o.count for o in self.objects)
        if total > MAX_ARRANGE_COPIES:
            raise ValueError(
                f"{total} copies is more than one arrange places ({MAX_ARRANGE_COPIES})"
            )
        return self


def arrange_inputs(
    outputs: OutputStore, body: ArrangeRequest, *, plate_model: str | None
) -> tuple[str, ArrangeInputs]:
    """Resolve the objects against the outputs' manifests, so every refusal happens here
    rather than on a worker (spec §10)."""
    manifests: dict[str, dict[str, ManifestObject]] = {}
    items: list[PackItem] = []
    provenance: dict[str, ManifestObject] = {}
    colours: list[str] = list(body.colours or [])
    slug: str | None = None
    for obj in body.objects:
        meta = require_output(outputs, obj.output_id)
        slug = slug or meta.slug
        if obj.output_id not in manifests:
            manifest = outputs.manifest(obj.output_id)
            if not manifest:
                raise ApiError(
                    status.HTTP_409_CONFLICT,
                    f"output {obj.output_id} was saved before outputs recorded their objects;"
                    " generate it again to arrange it",
                )
            manifests[obj.output_id] = {m.part: m for m in manifest}
            if body.colours is None:
                colours += [c for c in (meta.colors or []) if c not in colours]
        entry = manifests[obj.output_id].get(obj.part)
        if entry is None:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT,
                           f"output {obj.output_id} has no object {obj.part}")
        if obj.count == 0:
            continue
        items.append(PackItem(part=part_of(entry), count=obj.count, group=obj.group))
        provenance.setdefault(entry.part, entry.model_copy(
            update={"source_output": entry.source_output or obj.output_id}))
        if body.colours is None:
            colours += [c for c in entry.colours if c not in colours]
    if not items or slug is None:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "nothing to arrange: every count is 0")
    return slug, ArrangeInputs(
        items=items, goal=body.goal, plate=plate_size(plate_model), plate_model=plate_model,
        filament_plan=SlotPlan.of(body.filament_plan), colours=colours, name=body.name,
        provenance=provenance, sources=list(dict.fromkeys(o.output_id for o in body.objects)),
    )


@router.post(
    "/outputs/arrange",
    response_model=JobStatus,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Arrange objects onto plates",
    description="Lay out objects from saved outputs again for a goal, printer and spool plan"
    " (spec §7). No re-render. Poll the job with GET /jobs/{id}, then save it as an output.",
)
async def arrange_outputs(
    body: ArrangeRequest, outputs: OutputsDep, queue: QueueDep, store: SettingsStoreDep
) -> JobStatus:
    stored = await asyncio.to_thread(store.load)
    printer_id = body.printer_id if body.printer_id is not None else stored.printer_id
    # No printer: the plate the preview falls back to (Settings), else the default plate.
    plate_model = stored.default_plate
    if printer_id is not None:
        # `printer()` declares Scope.READ_STATUS, and the client's `_send` maps a refusal
        # through bambuddy/errors.py, so a key without it gets a 403 naming the scope.
        async with client_for(stored) as client:
            plate_model = (await client.printer(printer_id)).model
    slug, inputs = await asyncio.to_thread(arrange_inputs, outputs, body, plate_model=plate_model)
    arrange = getattr(queue, "arrange", None)
    if arrange is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "Arrange runs on Temporal; set SCADBUDDY_TEMPORAL_ADDRESS")
    job = await arrange(slug, inputs)
    return _job_status(job, None)
```

(Import `_job_status`, `JobStatus` from `api/jobs.py`, `QueueDep` from `api/deps.py`, `get_args` from `typing`, `model_validator` from `pydantic`, `ManifestObject` from `render.job_models`. Declare the route above `GET /outputs/{output_id}` is not needed — the methods differ — but keep it before any `POST /outputs/{output_id}` route in the file so FastAPI never matches `arrange` as an id.)

In `create_output`, before the `outputs.create(...)` call phase 3 moved into
`asyncio.to_thread`:

```python
    # An arranged output has no template inputs to reopen; it records its sources (§7).
    arranged = job.kind == "arrange"
    sources = ArrangeInputs.model_validate(job.inputs).sources if arranged else []
```

and in that call, phase 2's `inputs=` argument becomes `inputs={} if arranged else job.inputs`
and a new keyword `arranged_from=sources` is added; every other argument (`name`, `public_url`,
phase 4's `index` and `files_dir`) stays as it is. Task 1's `hold_parts` lines after the call then
hold the arranged output's Parts (`test_saving_an_arrange_job_records_its_sources_and_holds_its_parts`).

In `agent/src/tools/coverage.ts`, add a `NOT_A_TOOL` entry for `POST /api/v1/outputs/arrange` in the form the file's other entries use, with the reason: "Arrange needs objects, a printer and spools chosen in the History or Print dialog; the agent's print tools do not pick spools yet."

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/api -q`
Expected: all pass, including `test_arrange_api.py` and `test_output_parts.py`.

- [ ] **Step 5: Gates, generated files, commit**

Run `cd backend && uv run --frozen ruff format .`, then the backend gates; `cd frontend && pnpm gen:api && pnpm typecheck`; `cd agent && pnpm gen:api && pnpm lint && pnpm typecheck && pnpm test` (the coverage test passes with the entry).

```bash
git add backend/scadbuddy/api/outputs.py backend/tests/api/test_arrange_api.py backend/tests/api/test_output_parts.py \
  agent/src/tools/coverage.ts
git commit -m "feat(api): POST /outputs/arrange; arranged outputs keep where their objects came from (#428)"
```

---

### Task 6: `ctx.pack` takes goals, a filament plan and groups

**Files:**
- Modify: `backend/scadbuddy/workflows/ctx.py`, `plugins/scadbuddy/skills/authoring/SKILL.md`, `CLAUDE.md`
- Test: `backend/tests/test_template_pipeline.py` (append)

**Interfaces:**
- Consumes: `SlotPlan.of`, `PackItem.group`, `PackRequest.filament_plan/.colours`, `GOALS` (Task 2); phase 4's `Ctx.pack`, `FakeWorld`, `run_job`, `a_job`.
- Produces: `Ctx.pack(items: Sequence[Part | tuple[Part, int] | tuple[Part, int, str]], *, goal: str = "fewest_plates", filament_plan: object | None = None, colours: Sequence[str] = ()) -> Layout`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_template_pipeline.py` (phase 4's module; it already imports `FakeWorld`, `run_job`, `a_job` and `temporal_client`):

```python
GOAL_PIPELINE = """
async def run(ctx, inputs):
    red = await ctx.render("model.scad", width=10)
    white = await ctx.render("model.scad", width=20)
    layout = await ctx.pack([(red, 2, "left"), (white, 1, "right")], goal="keep_together",
                            filament_plan={"slots": [{"slot_id": 1, "spool_id": 4}]})
    await ctx.output(plates=layout, name="grouped")
"""


@pytest.mark.requires_temporal
async def test_a_pipeline_packs_for_a_goal_with_a_plan_and_groups() -> None:
    world = FakeWorld(GOAL_PIPELINE)
    async with temporal_client() as client:
        await run_job(world, a_job(params={}), client=client)
    [out] = world.outputs
    assert len(out.layout.plates) == 2
    assert [len(p.items) for p in out.layout.plates] == [2, 1]
    assert world.projections[-1].state == "done"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && uv run --frozen pytest tests/test_template_pipeline.py -q -k goal_with_a_plan`
Expected: the job fails with `filament_plan arrives with Arrange (phase 5)` in its error.

- [ ] **Step 3: Implement**

Replace phase 4's `Ctx.pack` in `workflows/ctx.py`:

```python
    async def pack(
        self,
        items: Sequence[Part | tuple[Part, int] | tuple[Part, int, str]],
        *,
        goal: str = "fewest_plates",
        filament_plan: object | None = None,
        colours: Sequence[str] = (),
    ) -> Layout:
        """Arrange's packing activity (spec §5.2, §7): the same goals and plan the Print
        dialog's Arrange uses. A 3-tuple names a `keep_together` group."""
        packed = [
            PackItem(part=i) if isinstance(i, Part)
            else PackItem(part=i[0], count=i[1], group=i[2] if len(i) > 2 else None)
            for i in items
        ]
        req = PackRequest(items=packed, plate=self.plate, goal=goal,
                          filament_plan=SlotPlan.of(filament_plan), colours=list(colours))
        # `activity_call` returns Any; binding it keeps mypy --strict's no-any-return quiet.
        layout: Layout = await self._host.activity_call("pack", req, result_type=Layout)
        return layout
```

(`SlotPlan` joins the `workflows.models` imports. Phase 4's `FakeWorld` pack fake calls `pack_layout`, which Task 2 routed to `arrange`, so the test sees the real packer.)

In `plugins/scadbuddy/skills/authoring/SKILL.md`, in the `ctx` section phase 4 wrote, replace the `pack` paragraph with:

```markdown
`await ctx.pack(items, goal=…, filament_plan=…)` lays parts out on plates. `items` are
`part`, `(part, count)` or `(part, count, group)`. `goal` is `fewest_plates` (default),
`fewest_swaps` (a part rides on a plate that already has its filaments), `by_colour`
(single-colour plates, which need no prime tower) or `keep_together` (each group on one
plate, or the pack fails). `filament_plan` is `{"slots": [{"slot_id", "spool_id"}]}`:
colours on one spool count as one filament. Every plate is checked against the printer's
plate and prime tower before it is written. The Print dialog can re-arrange the output
later without re-rendering, so pack for the common case.
Sources: `backend/scadbuddy/workflows/arrange.py`, `backend/scadbuddy/workflows/ctx.py`,
`docs/superpowers/specs/2026-09-27-template-pipelines-design.md` §7.
```

In `CLAUDE.md`'s Layout, after the `render/` bullet: "`backend/scadbuddy/workflows/arrange.py` — Arrange's packer (spec 2026-09-27 §7): goals, quarter turns, filament signatures, every plate checked with `plate.fit_problem`; `Arrange` in `workflows/pipelines.py` runs a `kind='arrange'` row (`POST /outputs/arrange`)."

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/test_template_pipeline.py -q && bash .github/scripts/lint-plugin.sh && claude plugin validate plugins/scadbuddy`
Expected: all pass; the linter finds the section's sources.

- [ ] **Step 5: Gates and commit**

Run `cd backend && uv run --frozen ruff format .`, then the backend gates (Global Constraints).

```bash
git add backend/scadbuddy/workflows/ctx.py backend/tests/test_template_pipeline.py plugins/scadbuddy/skills/authoring/SKILL.md CLAUDE.md
git commit -m "feat(pipelines): ctx.pack takes Arrange's goals, a filament plan and groups (#428)"
```

---

### Task 7: Frontend — the Arrange dialog (#314) and re-arranging in the Print dialog

**Files:**
- Create: `frontend/src/lib/arrange.ts`, `frontend/src/components/ArrangeDialog.tsx`, `frontend/src/components/ArrangeDialog.test.tsx`
- Modify: `frontend/src/api/client.ts`, `frontend/src/api/types.ts`, `frontend/src/components/PrintPicker.tsx`, `frontend/src/components/PrintPicker.test.tsx`, `frontend/src/pages/HistoryPage.tsx`, `frontend/src/pages/HistoryPage.test.tsx`, `frontend/src/mocks/handlers.ts`, `frontend/src/mocks/fixtures.ts`

**Interfaces:**
- Consumes: the generated `ArrangeRequest`, `ManifestObject`, `JobStatus` (as `Job`), `Output` (`OutputDetail`) types (Tasks 1, 5); `api.getJob(jobId)`, `api.createOutput(slug, jobId, name?)`, `ApiError.detail`; `JobStatus.status` (never `state`) and `JobStatus.plates` (`PlateInfo[]`: every plate of the written file, empty when there is only one; `render/jobs.py` `result_plates`).
- Produces:
  ```ts
  // api/types.ts
  export type ArrangeRequest = components['schemas']['ArrangeRequest']
  export type ManifestObject = components['schemas']['ManifestObject']
  // api/client.ts
  api.arrangeOutputs(body: ArrangeRequest): Promise<Job>
  // lib/arrange.ts
  export type ArrangeGoal = NonNullable<ArrangeRequest['goal']>
  export const GOAL_LABELS: Record<ArrangeGoal, string>
  export interface Arranged { output: Output; plates: number }
  export function arrangedNote(plates: number): string          // "Arranged onto 1 plate." / "… 2 plates."
  export async function runArrange(slug: string, body: ArrangeRequest, opts?: { pollMs?: number; onProgress?: (message: string) => void }): Promise<Arranged>
  // components/ArrangeDialog.tsx
  export function ArrangeDialog(props: { open: boolean; slug: string; outputs: Output[]; onClose: () => void; onArranged: (arranged: Arranged) => void }): JSX.Element
  // mocks/handlers.ts
  export function lastArrangeRequest(): ArrangeRequest | null
  ```
  The plate count comes from the finished job, not from `Output.plates`: that field is `OutputMeta.plates: list[PlateSend]`, the plates of the output's last *send*, and it is empty on a new output.

- [ ] **Step 1: Types, client, mocks**

`src/api/types.ts`, beside `export type Output = Schemas['OutputDetail']`, in the file's own form:

```ts
export type ArrangeRequest = Schemas['ArrangeRequest']
export type ManifestObject = Schemas['ManifestObject']
```

`src/api/client.ts`: add `ArrangeRequest` to the file's `import type { … } from './types'` list, and beside `createOutput`:

```ts
  /** spec 2026-09-27 §7 — objects from saved outputs onto plates again; poll the job. */
  arrangeOutputs: (body: ArrangeRequest) =>
    request<Job>('/outputs/arrange', { method: 'POST', body: JSON.stringify(body) }),
```

`src/mocks/fixtures.ts`: give `outputs[0]` (`Reagan`, colours `['#1B6CA8', '#E8532F']`) a manifest, after its `colors` line:

```ts
    manifest: [
      {
        part: 'piece-wall',
        file: 'model.scad',
        slug: 'name-keychain',
        revision: null,
        bbox: { min: [0, 0, 0], max: [60, 20, 5], size: [60, 20, 5] },
        footprint: [60, 20],
        colours: ['#1B6CA8', '#E8532F'],
        count: 2,
        plates: 1,
        bom_piece: 'wall',
        source_output: null,
        notes: [],
      },
    ],
    arranged_from: [],
```

`outputs[1]` and `outputs[2]` stay without one: they stand for outputs saved before this phase.

`src/mocks/handlers.ts`. Add `ArrangeRequest` and `ManifestObject` to the file's `import type { … } from '../api/types'` list. In `state`, after `jobs: new Map<string, Job>(),`:

```ts
  /** spec 2026-09-27 §7 — what each arrange job was built from, read when it is saved. */
  arranged: new Map<string, { sources: string[]; manifest: ManifestObject[] }>(),
  /** The last `POST /outputs/arrange` body, for tests to read back. */
  lastArrange: null as ArrangeRequest | null,
```

In `resetMockState`, after `state.jobs.clear()`:

```ts
  state.arranged.clear()
  state.lastArrange = null
```

After `setCatalogueOffline`:

```ts
/** The body of the last `POST /outputs/arrange`, or null when none was sent. */
export function lastArrangeRequest(): ArrangeRequest | null {
  return state.lastArrange
}
```

In `handlers`, before `http.get(\`${base}/jobs/:id\`, …)`:

```ts
  // spec 2026-09-27 §7 / §10 — Arrange refuses what the API refuses, then finishes at
  // once. Its plates are what the writer reports: `plates` lists every plate of the
  // new file and is empty when there is one. The mock's packer: more than four copies
  // take a second plate.
  http.post(`${base}/outputs/arrange`, async ({ request }) => {
    const body = (await request.json()) as ArrangeRequest
    state.lastArrange = body
    const manifest: ManifestObject[] = []
    for (const object of body.objects) {
      const source = state.outputs.find((o) => o.id === object.output_id)
      if (!source) return problem(404, 'Not Found', `no output with id '${object.output_id}'`)
      if ((source.manifest ?? []).length === 0) {
        return problem(
          409,
          'Conflict',
          `output ${source.id} was saved before outputs recorded their objects; generate it again to arrange it`,
        )
      }
      const entry = (source.manifest ?? []).find((m) => m.part === object.part)
      if (!entry) {
        return problem(422, 'Unprocessable Content', `output ${source.id} has no object ${object.part}`)
      }
      if (object.count > 0) {
        manifest.push({ ...entry, count: object.count, source_output: entry.source_output ?? source.id })
      }
    }
    if (manifest.length === 0) {
      return problem(422, 'Unprocessable Content', 'nothing to arrange: every count is 0')
    }
    const first = state.outputs.find((o) => o.id === body.objects[0]?.output_id)
    if (!first?.bbox_mm) return problem(409, 'Conflict', 'the output has no dimensions')
    const colors = body.colours ?? first.colors ?? []
    const copies = manifest.reduce((sum, m) => sum + m.count, 0)
    const bbox = first.bbox_mm
    const jobId = nextHexId()
    const job: Job = {
      id: jobId,
      slug: first.slug,
      status: 'done',
      created_at: new Date().toISOString(),
      finished_at: new Date().toISOString(),
      params: {},
      log_tail: [],
      bbox_mm: bbox,
      colors,
      plates: copies > 4 ? [1, 2].map((index) => ({ index, bbox_mm: bbox, colors })) : [],
    }
    state.jobs.set(jobId, job)
    state.arranged.set(jobId, {
      sources: [...new Set(body.objects.map((o) => o.output_id))],
      manifest,
    })
    return HttpResponse.json(job, { status: 202 })
  }),
```

In the `POST /models/:slug/outputs` handler, in the `const output: Output = { … }` literal, after `warnings: [],`:

```ts
      // An arranged output keeps its objects and where they came from (§7); a render's
      // output has no manifest in the mock.
      manifest: state.arranged.get(job.id)?.manifest ?? [],
      arranged_from: state.arranged.get(job.id)?.sources ?? [],
```

- [ ] **Step 2: Write the failing tests**

`frontend/src/components/ArrangeDialog.test.tsx`:

```tsx
import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { Output } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import { lastArrangeRequest } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { ArrangeDialog } from './ArrangeDialog'

const first = fixtures.outputs[0] as Output
const second: Output = { ...first, id: 'o-2', name: 'second', manifest: [] }

describe('ArrangeDialog', () => {
  it('lists every object with its count and sends the build list', async () => {
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open slug="name-keychain" outputs={[first]} onClose={vi.fn()} onArranged={onArranged} />,
    )
    const count = screen.getByLabelText('Copies of wall — Reagan')
    expect(count).toHaveValue(2)
    await user.clear(count)
    await user.type(count, '5')
    await user.selectOptions(screen.getByLabelText('Goal'), 'by_colour')
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() => expect(onArranged).toHaveBeenCalledOnce())
    expect(onArranged).toHaveBeenCalledWith({
      output: expect.objectContaining({ arranged_from: [first.id] }),
      plates: 2,
    })
    expect(lastArrangeRequest()).toMatchObject({
      goal: 'by_colour',
      objects: [{ output_id: first.id, part: 'piece-wall', count: 5 }],
    })
  })

  it('says which outputs cannot be arranged', () => {
    renderPage(
      <ArrangeDialog open slug="name-keychain" outputs={[second]} onClose={vi.fn()} onArranged={vi.fn()} />,
    )
    expect(screen.getByRole('status')).toHaveTextContent(
      'second was saved before Arrange; generate it again to arrange it.',
    )
    expect(screen.getByRole('button', { name: 'Arrange' })).toBeDisabled()
  })

  it('shows why an arrange failed', async () => {
    const created_at = '2026-09-28T12:00:00Z'
    server.use(
      http.post('/api/v1/outputs/arrange', () =>
        HttpResponse.json(
          { id: 'arrange-fail', slug: 'name-keychain', status: 'pending', created_at },
          { status: 202 },
        ),
      ),
      http.get('/api/v1/jobs/arrange-fail', () =>
        HttpResponse.json({
          id: 'arrange-fail',
          slug: 'name-keychain',
          status: 'failed',
          created_at,
          error: "group 'big' does not fit on one plate",
        }),
      ),
    )
    const onArranged = vi.fn()
    const { user } = renderPage(
      <ArrangeDialog open slug="name-keychain" outputs={[first]} onClose={vi.fn()} onArranged={onArranged} />,
    )
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    expect(await screen.findByRole('alert')).toHaveTextContent("group 'big' does not fit on one plate")
    expect(onArranged).not.toHaveBeenCalled()
  })
})
```

Append to `frontend/src/components/PrintPicker.test.tsx`, inside its `describe`. It uses the module-level `const output = fixtures.outputs[0] as Output`, `renderPicker` and `loaded`, and adds `lastArrangeRequest` to the existing `import { resetMockState } from '../mocks/handlers'`:

```tsx
  it('re-arranges for the chosen spools and switches to the new output', async () => {
    const { user } = renderPicker()
    await loaded()
    await user.selectOptions(screen.getByLabelText('Arrange for'), 'fewest_swaps')
    await user.click(screen.getByRole('button', { name: 'Re-arrange for these spools' }))
    // Two copies: the mock writes one plate, and the job's empty `plates` says so.
    expect(await screen.findByText('Arranged onto 1 plate.')).toBeInTheDocument()
    const sent = lastArrangeRequest()
    expect(sent).toMatchObject({
      goal: 'fewest_swaps',
      colours: output.colors,
      objects: [{ output_id: output.id, part: 'piece-wall', count: 2 }],
      name: 'Reagan (arranged)',
    })
    expect(sent?.filament_plan?.slots.length).toBeGreaterThan(0)
    // The dialog now reads the new output: its plates are asked for by the new id.
    await screen.findByTestId('filament-slot-1')
  })

  it('offers no re-arrange for an output saved before manifests', async () => {
    renderPage(
      <PrintPicker
        open
        slug="name-keychain"
        output={{ ...output, manifest: [], library_files: [], pipeline_run_id: undefined }}
        onClose={vi.fn()}
        onRan={vi.fn()}
      />,
    )
    await loaded()
    expect(screen.queryByLabelText('Arrange for')).not.toBeInTheDocument()
  })
```

Append to `frontend/src/pages/HistoryPage.test.tsx`, inside its `describe` (add `import * as fixtures from '../mocks/fixtures'` beside the existing `bbox` import):

```tsx
  it('arranges the outputs ticked on the page', async () => {
    const { user } = render()
    const reagan = await row('Reagan')
    expect(screen.getByRole('button', { name: 'Arrange selected (0)' })).toBeDisabled()
    await user.click(within(reagan).getByRole('checkbox', { name: 'Select Reagan' }))
    await user.click(screen.getByRole('button', { name: 'Arrange selected (1)' }))
    expect(await screen.findByLabelText('Copies of wall — Reagan')).toHaveValue(2)
    await user.click(screen.getByRole('button', { name: 'Arrange' }))
    await waitFor(() =>
      expect(screen.queryByLabelText('Copies of wall — Reagan')).not.toBeInTheDocument(),
    )
    // The list reloads with the arranged output, and the selection is cleared.
    expect(await screen.findByText('Arranged from 1 output')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Arrange selected (0)' })).toBeDisabled()
  })

  it('offers no Edit on an arranged output', async () => {
    server.use(
      http.get('/api/v1/models/name-keychain/outputs', () =>
        HttpResponse.json([
          {
            ...fixtures.outputs[0],
            id: 'f'.repeat(32),
            name: 'Batch',
            params: {},
            arranged_from: ['a'.repeat(32), 'c'.repeat(32)],
          },
        ]),
      ),
    )
    render()
    const batch = await row('Batch')
    expect(within(batch).queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    expect(within(batch).getByText('Arranged from 2 outputs')).toBeInTheDocument()
    expect(within(batch).getByRole('button', { name: 'Send again' })).toBeInTheDocument()
  })
```

(`render` returns `renderPage`'s result, which carries `user`, as `PrintPicker.test.tsx` uses it.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd frontend && pnpm gen:api && pnpm test -- ArrangeDialog PrintPicker HistoryPage`
Expected: `ArrangeDialog.test.tsx` fails with `Failed to resolve import "./ArrangeDialog"`; the new `PrintPicker` case fails on `Unable to find a label with the text of: Arrange for`; the `HistoryPage` cases fail on `Unable to find role="button" and name "Arrange selected (0)"` and on the `Edit` button still being there.

- [ ] **Step 4: `lib/arrange.ts`**

```ts
import { api } from '../api/client'
import type { ArrangeRequest, Output } from '../api/types'

export type ArrangeGoal = NonNullable<ArrangeRequest['goal']>

export const GOAL_LABELS: Record<ArrangeGoal, string> = {
  fewest_plates: 'Fewest plates',
  fewest_swaps: 'Fewest filament swaps',
  by_colour: 'One colour per plate (no prime tower)',
  keep_together: 'Keep groups together',
}

/** A saved arrange, and how many plates its file has. */
export interface Arranged {
  output: Output
  plates: number
}

export function arrangedNote(plates: number): string {
  return `Arranged onto ${plates} ${plates === 1 ? 'plate' : 'plates'}.`
}

/**
 * spec 2026-09-27 §7 — lay objects out again, wait for the job, save its output. The
 * plate count is the job's: `plates` lists every plate of the new file and is empty
 * when there is only one. A failed or cancelled job rejects with the job's own error.
 */
export async function runArrange(
  slug: string,
  body: ArrangeRequest,
  opts: { pollMs?: number; onProgress?: (message: string) => void } = {},
): Promise<Arranged> {
  const started = await api.arrangeOutputs(body)
  const pollMs = opts.pollMs ?? 1000
  for (;;) {
    const job = await api.getJob(started.id)
    if (job.status === 'done') {
      const output = await api.createOutput(slug, job.id, body.name ?? undefined)
      return { output, plates: Math.max(1, (job.plates ?? []).length) }
    }
    if (job.status === 'failed' || job.status === 'cancelled') {
      throw new Error(job.error ?? `The arrange was ${job.status}.`)
    }
    opts.onProgress?.(job.status === 'running' ? 'Arranging…' : 'Waiting for a worker…')
    await new Promise((resolve) => setTimeout(resolve, pollMs))
  }
}
```

- [ ] **Step 5: `ArrangeDialog.tsx`**

```tsx
import { useState } from 'react'
import { ApiError } from '../api/client'
import type { ArrangeRequest, Output } from '../api/types'
import { GOAL_LABELS, runArrange, type ArrangeGoal, type Arranged } from '../lib/arrange'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'

type Props = {
  open: boolean
  slug: string
  outputs: Output[]
  onClose: () => void
  onArranged: (arranged: Arranged) => void
}

/**
 * #314 — objects from several outputs onto shared plates (spec 2026-09-27 §7). Each
 * object of each selected output is a row with its copies; Arrange lays them out for
 * the goal on the configured printer's plate, with no re-render, and saves the result.
 */
export function ArrangeDialog({ open, slug, outputs, onClose, onArranged }: Props) {
  const rows = outputs.flatMap((output) =>
    (output.manifest ?? []).map((object) => ({ output, object, key: `${output.id}:${object.part}` })),
  )
  const unusable = outputs.filter((output) => (output.manifest ?? []).length === 0)
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [goal, setGoal] = useState<ArrangeGoal>('fewest_plates')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const countOf = (key: string, fallback: number) => counts[key] ?? fallback

  async function submit() {
    setBusy(true)
    setError(null)
    const body: ArrangeRequest = {
      objects: rows.map(({ output, object, key }) => ({
        output_id: output.id,
        part: object.part,
        count: countOf(key, object.count),
      })),
      goal,
      name: name.trim() || null,
    }
    try {
      onArranged(await runArrange(slug, body, { onProgress: setProgress }))
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : (cause as Error).message)
    } finally {
      setBusy(false)
      setProgress(null)
    }
  }

  return (
    <Dialog open={open} title="Arrange" onClose={onClose}>
      <div className="flex flex-col gap-3">
        {unusable.length > 0 && (
          <p role="status" className="text-[12px] text-muted">
            {unusable.map((o) => o.name ?? o.id).join(', ')} was saved before Arrange; generate it
            again to arrange it.
          </p>
        )}
        <ul className="flex flex-col gap-1.5">
          {rows.map(({ output, object, key }) => {
            const label = `${object.bom_piece ?? object.file} — ${output.name ?? output.id}`
            return (
              <li key={key} className="flex items-center justify-between gap-2 text-[13px]">
                <span>{label}</span>
                <input
                  type="number"
                  min={0}
                  max={500}
                  aria-label={`Copies of ${label}`}
                  value={countOf(key, object.count)}
                  onChange={(event) =>
                    setCounts({ ...counts, [key]: Math.max(0, Number(event.target.value) || 0) })
                  }
                  className="sb-field sb-num w-20"
                />
              </li>
            )
          })}
        </ul>
        <label htmlFor="arrange-goal" className="text-[12px] text-muted">
          Goal
        </label>
        <select
          id="arrange-goal"
          value={goal}
          onChange={(event) => setGoal(event.target.value as ArrangeGoal)}
          className="sb-field"
        >
          {Object.entries(GOAL_LABELS).map(([value, text]) => (
            <option key={value} value={value}>
              {text}
            </option>
          ))}
        </select>
        <label htmlFor="arrange-name" className="text-[12px] text-muted">
          Name
        </label>
        <input
          id="arrange-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          className="sb-field"
        />
        {progress && <p className="text-[12px] text-faint">{progress}</p>}
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
        <Button onClick={() => void submit()} disabled={busy || rows.length === 0}>
          Arrange
        </Button>
      </div>
    </Dialog>
  )
}
```

`Dialog({ open, title, onClose })` and `Button` are the baseline's (`components/ui/`); `text-warn` is the class the Print dialog's `runError` line uses. The label `Goal` names the `select` through `htmlFor`, so the test's `getByLabelText('Goal')` finds it without an `aria-label`. The server caps the total copies at 2000 (Task 5); a larger sum comes back as the 422's detail in the `alert`.

- [ ] **Step 6: The History page picks outputs to arrange**

In `src/pages/HistoryPage.tsx`, import the dialog:

```tsx
import { ArrangeDialog } from '../components/ArrangeDialog'
```

In `HistoryPage`, after `const [confirmFor, setConfirmFor] = useState<Output | undefined>(undefined)`:

```tsx
  /** §7 — outputs ticked for the next Arrange (#314). */
  const [picked, setPicked] = useState<string[]>([])
  const [arranging, setArranging] = useState(false)
  const togglePicked = (id: string) =>
    setPicked((current) => (current.includes(id) ? current.filter((p) => p !== id) : [...current, id]))
```

Replace the list block `{!loading && schema && outputsState.data && outputsState.data.length > 0 && ( <ul data-testid="outputs" …> … </ul> )}` with:

```tsx
        {!loading && schema && outputsState.data && outputsState.data.length > 0 && (
          <>
            <div className="mb-2 flex justify-end">
              <Button size="sm" disabled={picked.length === 0} onClick={() => setArranging(true)}>
                Arrange selected ({picked.length})
              </Button>
            </div>
            <ul data-testid="outputs" aria-label="Generated outputs" className="space-y-2">
              {outputsState.data.map((output) => (
                <OutputRow
                  key={output.id}
                  output={output}
                  schema={schema}
                  deleting={deleting === output.id}
                  picked={picked.includes(output.id)}
                  onPick={() => togglePicked(output.id)}
                  onEdit={() =>
                    void navigate(editPath(output.id), {
                      state: { editTarget: editTargetFor(output) } satisfies EditNavigationState,
                    })
                  }
                  onSend={() => setSendFor(output)}
                  onDelete={() => requestDelete(output)}
                  bambuddyUrl={bambuddyUrl}
                />
              ))}
            </ul>
          </>
        )}
```

After the `<SendDialog … />` element:

```tsx
      <ArrangeDialog
        open={arranging}
        slug={slug}
        outputs={(outputsState.data ?? []).filter((o) => picked.includes(o.id))}
        onClose={() => setArranging(false)}
        onArranged={() => {
          setArranging(false)
          setPicked([])
          outputsState.reload()
        }}
      />
```

In `OutputRow`, the props gain `picked` and `onPick`:

```tsx
function OutputRow({
  output,
  schema,
  deleting,
  picked,
  onPick,
  onEdit,
  onSend,
  onDelete,
  bambuddyUrl,
}: {
  output: Output
  schema: CustomizerSchema
  deleting: boolean
  /** §7 — ticked for the next Arrange. */
  picked: boolean
  onPick: () => void
  onEdit: () => void
  onSend: () => void
  onDelete: () => void
  bambuddyUrl: string | undefined
}) {
  const diff = diffFromDefaults(schema, output.params ?? {})
  const unit = useDisplayUnit()
  /** An arranged output has no template inputs to reopen in the customizer. */
  const arrangedFrom = (output.arranged_from ?? []).length
```

First in the header row (`<div className="flex items-center gap-2.5">`), before `<ColorStrip …/>`:

```tsx
            <input
              type="checkbox"
              aria-label={`Select ${output.name ?? shortId(output.id)}`}
              checked={picked}
              onChange={onPick}
              className="accent-[var(--sb-accent)]"
            />
```

In the actions (`<div className="flex shrink-0 items-center gap-2">`), replace the `Edit` button with:

```tsx
          {arrangedFrom > 0 ? (
            <span className="text-[12px] text-muted">
              {`Arranged from ${arrangedFrom} ${arrangedFrom === 1 ? 'output' : 'outputs'}`}
            </span>
          ) : (
            <Button size="sm" onClick={onEdit}>
              Edit
            </Button>
          )}
```

- [ ] **Step 7: The Print dialog re-arranges for the spools it shows**

In `src/components/PrintPicker.tsx`, add the imports:

```tsx
import { arrangedNote, GOAL_LABELS, runArrange, type ArrangeGoal } from '../lib/arrange'
```

At the top of `PrintPicker`'s body, before `const [choices, setChoices] = …`:

```tsx
  /** §7 — the output being printed: the one passed in, or what Re-arrange made of it. */
  const [target, setTarget] = useState<Output | undefined>(output)
  useEffect(() => setTarget(output), [output])
  const [arrangeGoal, setArrangeGoal] = useState<ArrangeGoal>('fewest_swaps')
  const [arranging, setArranging] = useState(false)
  const [arrangeNote, setArrangeNote] = useState<string | null>(null)
```

The component reads the `output` prop in exactly one place, `const outputId = output?.id` (line 117 at the baseline); every effect, the choices and plates reads, the filament reads, `attachToProject`, `runPrint` and the plate thumbnails go through `outputId` (lines 122–660). So the whole switch to the re-arranged output is:

```tsx
  const outputId = target?.id
```

A new `outputId` runs the existing reset effect ("One output's plates and overrides do not survive a change of output") and reloads the choices, plates and filaments, exactly as opening the dialog on that output would. The filament plan is reseeded from the new output's choices; because `colours` pins the filament order (Task 3), slot N still means the same colour, so the auto-match lands on the same spools.

After `const printerId = choices?.printer_id ?? null`, add:

```tsx
  async function rearrange() {
    if (!target) return
    setArranging(true)
    setArrangeNote(null)
    try {
      const next = await runArrange(slug, {
        objects: (target.manifest ?? []).map((object) => ({
          output_id: target.id,
          part: object.part,
          count: object.count,
        })),
        goal: arrangeGoal,
        printer_id: printerId,
        filament_plan: { slots: plan, force_colour_match: false },
        colours: target.colors ?? [],
        name: target.name ? `${target.name} (arranged)` : null,
      })
      setTarget(next.output)
      setArrangeNote(arrangedNote(next.plates))
    } catch (cause) {
      setArrangeNote(cause instanceof ApiError ? cause.detail : (cause as Error).message)
    } finally {
      setArranging(false)
    }
  }
```

Right after the `{filaments && ( <FilamentPicker … /> )}` block:

```tsx
              {(target?.manifest ?? []).length > 0 && (
                <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
                  <legend className="px-1 text-[13px] text-ink">Arrange</legend>
                  <label htmlFor="arrange-for" className="text-[12px] text-muted">
                    Arrange for
                  </label>
                  <select
                    id="arrange-for"
                    value={arrangeGoal}
                    onChange={(event) => setArrangeGoal(event.target.value as ArrangeGoal)}
                    className="sb-field mt-1.5"
                  >
                    {Object.entries(GOAL_LABELS)
                      .filter(([value]) => value !== 'keep_together')
                      .map(([value, text]) => (
                        <option key={value} value={value}>
                          {text}
                        </option>
                      ))}
                  </select>
                  <Button
                    size="sm"
                    className="mt-1.5"
                    disabled={arranging}
                    onClick={() => void rearrange()}
                  >
                    Re-arrange for these spools
                  </Button>
                  {arrangeNote && (
                    <p aria-live="polite" className="mt-1.5 text-[12px] text-muted">
                      {arrangeNote}
                    </p>
                  )}
                </fieldset>
              )}
```

`plan` is the dialog's `SlotChoice[]` state and `printerId` its chosen printer; `colours: target.colors` pins slot order so the plan still means the same spools on the new file. `keep_together` is left out here: groups are named in the History page's dialog, not per spool. The note is `aria-live`, not `role="status"`, so the existing tests that look up the progress panel's status are not disturbed. The arranged output has a manifest (the writer records it, Task 1), so the fieldset stays and can re-arrange again.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `cd frontend && pnpm gen:api && pnpm lint && pnpm typecheck && pnpm test && pnpm build`
Expected: all pass, including the existing `PrintPicker` and `HistoryPage` tests. `outputs[0]` now has a manifest, so every `PrintPicker` test that opens on it also shows the Arrange fieldset; none of them queries a `select` or button by a name the fieldset shares (`Arrange for`, `Re-arrange for these spools`). If the frontend tests time out under load, re-run on an idle machine before debugging (CLAUDE.md, "Known flakes").

- [ ] **Step 9: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): Arrange dialog for several outputs, and re-arrange for the chosen spools (#428, #314)"
```

---

## Disagreements between the spec, the brief and the code (resolved here)

1. **"§3.7 or wherever Arrange lives."** §3.7 is the Temporal test list. Arrange lives in §7, with its workflow row in §3.4 and its route in §10.
2. **`Layout` shape (§7).** §7 writes `[{plate: int, objects: [{part, at: (x, y, rot)}]}]`. Phase 4 shipped `Layout(plates: [LayoutPlate(items: [Placed(piece_key, x, y)])], own)`; this plan keeps it and adds `Placed.rot` rather than a second layout type. "Plus the plate geometry it was packed for" is the `PlateSize`/`plate_model` the output's record already carries (`OutputRecord.plate_key`).
3. **`FilamentPlan` in a workflow payload.** `bambuddy/filaments.py` imports the Bambuddy client, which must not enter the workflow sandbox. `SlotPlan` (slot → spool) carries it; `SlotPlan.of(FilamentPlan)` is the bridge.
4. **Slot numbers across a re-arrange.** §7 is silent. A plan names spools by 1-based slot, and a new 3MF could order its filaments differently; `colours` pins the order (Review Focus 4).
5. **Where the output's Parts live.** Phase 4 refs Parts only by their job, which the TTL prunes. §7's "no re-render" needs them later, so a saved output holds them (`OUTPUT_HOLDER`, Review Focus 3).
6. **Outputs saved before this phase** have no manifest. They cannot be arranged without a re-render; the API says so (409), and the dialog lists them.
7. **Foreign objects (#313).** §7 names the limit ("Arrange can place them but not split them by colour"). Placing a Bambuddy library file needs its meshes read from a 3MF ScadBuddy did not write; that is #313's change and is not in this plan.
8. **`POST /outputs/arrange` response.** §10 says "a `render_jobs` row … polled with `GET /jobs/{id}`". The route answers 202 with that row's `JobStatus`; saving the result is the ordinary `POST /models/{slug}/outputs {job_id}`, so an arranged output is an output like any other. Its slug is the first object's output's.
9. **The shelf packer.** Phase 4's `shelf_pack` is deleted; its two coordinate-pinning tests go with it (Task 2 Step 5).

## Self-review notes

- **Spec coverage.**
  - §7 "objects; plates are a layout", manifest per object (Part ref, bbox, footprint, colour slots, count, provenance incl. BOM entry) → Task 1. "The 3MF is written from manifest + layout" → Tasks 1 and 3 (the writer reads Parts and the layout; rotation, colour order).
  - §7 Arrange workflow, `kind='arrange'`, build list from several outputs, `FilamentPlan`, goals, "in seconds, with no re-render" → Tasks 2, 4 and 5; #314's build list → Task 7.
  - §7 heuristic (group by signature, FFD 2D, exclusion zones and prime tower, order to minimise swaps; pluggable `Goal`) → Task 2 (`_joins`, `order_plates`, `fit_problem`).
  - §7 foreign objects → deferred (Disagreement 7).
  - §3.3/§3.4 insert → start → reconcile, `kind` picks the workflow → Task 4.
  - §5.2 `ctx.pack(goal, filament_plan)` → Task 6.
  - §8.4 record on arranged outputs → Task 4 (`OutputRecord` with `pipeline_version="arrange"`, `plate_key`, `parts`).
  - §10 `manifest` on `GET /outputs/{id}`, `POST /outputs/arrange` → Tasks 1 and 5.
  - §12 #314 → Tasks 5 and 7; #81 (plate follows printer) → Task 5 (`printer_id` → `plate_for`); #83 → Task 7 (the new output's plates in the Print dialog); #313 → deferred.
- **Placeholder scan.** No TBD or TODO. Steps that edit phase 1/4 code this plan cannot quote verbatim (`OutputStore.directory`'s behaviour on an unknown id, phase 3/4's final argument list of `outputs.create` in the create route, `FakeWorld`'s worker set-up, the `NOT_A_TOOL` entry form) name the exact site and show the code to write there. `Dialog`'s props, the `filament_colour` key, the `default_plate` setting, `Scope.READ_STATUS` on `client.printer` and `plate_for("H2C").key` were checked against the baseline and are no longer hedged.
- **Type consistency.** `ManifestObject` is defined once (`render/job_models.py`) and used by `OutputRequest.provenance`, `ArrangeInputs.provenance`, `OutputStore.manifest`, `OutputDetail.manifest`, `part_of`. `SlotPlan.slots: dict[int, int]` is the same in `signature_of`, `PackRequest`, `ArrangeInputs`, `SlotPlan.of`. `Placed.rot` is read by `placement_matrix` and written by `arrange` and `explicit_plate`. `OutputRequest.colours` (Task 3) is set by `Arrange` (Task 4) from `ArrangeInputs.colours` (Task 5). `RenderService.arrange(slug, inputs)` has the same signature in Tasks 4 and 5. `GOALS` equals the route's `Goal` literal (asserted at import).
- **Review Focus.** All five pinned: (1) Task 2 `test_every_packed_plate_places_on_the_real_printer`; (2) Task 4 `test_the_reconciler_starts_an_arrange_row_as_arrange`; (3) Task 1 `test_a_saved_output_keeps_its_parts_after_the_job_is_pruned`, with the routes in `tests/api/test_output_parts.py` (create holds, output delete and model delete release, an arrange save holds and records `arranged_from`); (4) Task 3 `test_an_arranged_output_keeps_the_filament_order_it_was_planned_against` and `test_one_object_arranged_alone_still_takes_the_planned_order` (Arrange never takes the `own` shortcut, Task 2 `test_arrange_packs_even_one_object`); (5) Task 5 `test_an_output_without_a_manifest_is_refused_up_front` and `test_a_part_not_in_the_output_is_refused`.
- **Known risks for the implementer.** The packer calls `fit_problem` once per candidate spot; 200 copies on a busy plate is a few thousand calls of pure arithmetic, well inside the `pack` activity's `SHORT` timeout, but a much larger build list would want the check only on the final candidate. The total copies per request are capped at 2000 (`MAX_ARRANGE_COPIES`, a 422), which keeps that well inside `SHORT`. `test_a_long_part_is_turned_to_share_a_plate` uses the geometry the review traced through this packer (240 x 60 beside 150 x 200: one plate turned, two unturned), so deleting the quarter turn from `_try` fails it.

## Revision 1 (review)

The review is `.superpowers/sdd/2026-09-28-phase1-render-on-temporal/plan-phase5-review.md`.
The baseline is `wt-service` at 69306836, and phase 4's names were re-checked against its
revision-2 Interfaces blocks. Every finding is fixed; none is declined.

**Blocking**
- **B1: fixed.** `runArrange` reads `job.status` (`JobStatus` has no `state`), and every mock and test answers `status`. The progress text that read `job.steps` is gone, since `JobStatus` has no `steps` in this phase. Progress now says "Waiting for a worker…" or "Arranging…" from `status`.
- **B2: fixed.** `ArrangeDialog.test.tsx` uses `fixtures.outputs[0] as Output`. The `PrintPicker` case uses the module-level `output`, with `renderPicker` and `loaded`. Step 1 says `outputs[0]`, and its manifest colours are that fixture's own (`#1B6CA8`, `#E8532F`).

**Important**
- **I1: fixed**, with the reviewer's verified geometry: `long` 240×60, `sq` 150×200, `rot == 90.0` for `long`, one plate. The "Known risks" note about widening the plate is replaced.
- **I2: fixed.** Arrange never takes the `own` shortcut.
  - `arrange(..., allow_own=True)` and `PackRequest.allow_own` are new; `pack_layout` passes the flag through, and the `Arrange` workflow sends `allow_own=False`. A pipeline's lone part keeps phase 4's shortcut.
  - New tests: `test_arrange_packs_even_one_object` (Task 2), and `test_one_object_arranged_alone_still_takes_the_planned_order` (Task 3). The Task 3 test packs one object and one copy through `pack_layout`, writes it with a `colours` pin, and asserts the 3MF's `filament_colour` order.
- **I3: fixed.**
  - `tests/api/test_output_parts.py` covers four cases: creating an output adds a `blob_refs` row per manifest part; deleting the output leaves none; saving an arrange job gives `arranged_from == sources` and holds its Parts; deleting the model releases every output's rows.
  - `delete_model` calls `release_parts` for each id beside `uploads.delete_outputs(output_ids)`, best effort like that block.
  - The create and delete route changes and the arrange branch of the create route are code now, not prose.
- **I4: fixed.**
  - `runArrange` returns `{ output, plates }`. The plate count comes from the finished job: `max(1, job.plates.length)`, because `plates` is empty for a one-plate file (`result_plates`).
  - The note is `arrangedNote(n)`, with singular and plural.
  - The mock `POST /outputs/arrange` builds a realistic done job: it refuses as the API does, and puts two plates in `plates` for more than four copies and an empty list otherwise.
  - The tests cover both counts: "Arranged onto 1 plate." in `PrintPicker`, and `plates: 2` in `ArrangeDialog`.
- **I5: fixed.** `layout: Layout = await self._host.activity_call(...)`, then `return layout`.

**Minor**
- **M1: fixed.**
  - Task 2 Step 5 shows the four remaining P4 tests and the changed `test_template_pipeline.py` case, and gives the real count: six packer tests, two deleted, four left.
  - Task 7 Step 7 names the only read of the `output` prop, `const outputId = output?.id` at line 117; every other read goes through `outputId`. It shows the one-line change.
  - The `GET /jobs/:id` mock is unchanged. The arrange handler registers its job in `state.jobs`, and the create-output mock reads `state.arranged`.
- **M2: fixed.** `hold_parts`, `release_parts`, `outputs.manifest` and `store.load` run through `asyncio.to_thread` in the async routes.
- **M3: fixed.**
  - With no `printer_id`, the route uses `StoredSettings.default_plate`, the plate the preview falls back to. Test: `test_with_no_printer_the_plate_falls_back_to_the_stored_default`.
  - `client.printer` already declares `Scope.READ_STATUS`, and `_send` maps failures through `bambuddy/errors.py` (`map_response`/`map_transport`). That was verified at the baseline, `client.py:181-189`. The route now says so; it needs no further mapping.
- **M4: fixed, by a decision.** `_write_plates` matches colours case-insensitively by upper-casing both the planned order and each part's colour. `split.py` and `solids.py` already emit upper-case, so phase 4 outputs are unchanged. The full replaced loop is shown.
- **M5: fixed.** Two `HistoryPage` tests cover the Task 7 wiring:
  - the select checkbox and "Arrange selected (n)", from disabled at 0 through 1, then the dialog, then reload and cleared;
  - an arranged output shows "Arranged from 2 outputs" and no Edit button.
- **M6: fixed.** The Global Constraints and every gates step say to run `uv run --frozen ruff format .` before the gates.
- **M7: fixed.** `ArrangeRequest` caps the sum of counts at `MAX_ARRANGE_COPIES = 2000` with a `model_validator`, which returns 422. Tests: `test_more_than_2000_copies_is_a_422` and `test_2000_copies_is_allowed`.

**Also changed**
- Task 7 now carries full code, to the standard of Tasks 1–6:
  - types, client, fixtures and every mock edit;
  - `lib/arrange.ts` and `ArrangeDialog.tsx`;
  - the `HistoryPage` changes: state, list block, `OutputRow` props, checkbox and Edit swap;
  - the `PrintPicker` changes: target, `rearrange`, fieldset.
- The hedges the review flagged (the `filament_colour` key, `plate.key`, `Dialog`'s props, `text-danger`) are replaced with the checked facts.
