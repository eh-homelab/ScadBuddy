# Phase 4: Template pipelines. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A template can define what one Generate does. `pipeline/pipeline.py` runs as a Temporal workflow over `ctx` primitives. It can render any of the template's files, run its own Python from `pipeline/activities.py`, pack the parts onto plates and write one or more outputs with a BOM and extra files. `dollhouse-kit` generates a whole house (epic #427).

**Architecture:** `TemplatePipeline` stops hard-coding one piece.
1. It calls a `load_pipeline` activity. The activity returns the template's pipeline source, or the built-in default, at the job's pinned revision, so the source is recorded in the workflow history.
2. It compiles that source with the file name `pipeline/pipeline.py` and `exec`s it inside the workflow sandbox.
3. It awaits `run(ctx, inputs)`.

Each `ctx` primitive leaves the sandbox as an activity or a child workflow:
- `render` is a `RenderPiece` child, shared by start-then-signal, with outcomes now keyed by `piece_key`.
- `pack` is a shelf-packing activity (Arrange replaces it in phase 5).
- `output` builds a multi-plate 3MF in an output blob.
- `activity` runs a template function in a subprocess in its own process group.
- `progress` projects steps.

Outputs ride on the job (`Job.outputs`), and Generate saves each one with its BOM, files and reproducibility record. `migrate(inputs, from_version)` runs as an activity in the same subprocess, behind `POST /models/{slug}/inputs/migrate`.

**Tech Stack:** Python 3.12, `temporalio` 1.33.0 (workflow sandbox, `temporalio.testing`), pydantic v2, FastAPI, psycopg 3, trimesh, pytest; React 19 + vitest + msw.

**Spec:** `docs/superpowers/specs/2026-09-27-template-pipelines-design.md`: §3.4 (`TemplatePipeline.run` steps 1–2), §3.6, §5, §8.2–8.4, §9, §10, §11 item 4, §12. Take §3.4/§3.6 from the amended text (PR #577). Epic **#427** (the brief said #426, which is the store epic; see "Disagreements").

**Base:** `main` after phase 1 (#424), phase 1's PR4 (#546), phase 2 (#425) and phase 3 (#426) have merged. Phase 3's plan is `docs/superpowers/plans/2026-09-28-phase3-blob-store.md` (PR #590); the phase 3 names below are not assumed from it but read from phase 3's implementation, the Tasks 1–9 chain at f9223552 (the store the phase 3 PRs carry). The phase 1 names below were checked at `wt-service` 69306836. PR4 (#546) is assumed to deliver exactly these, and this plan relies on each:
- the legacy queue removed; `AppState.queue` renamed `AppState.render: RenderService`, and `QueueDep` renamed `RenderDep = Annotated[RenderService, Depends(get_render)]`;
- `RenderService.client: Client` **non-optional** (so this plan's new `RenderService` methods carry no `assert self.client is not None`; the `assert`s at 69306836 go with PR4);
- a Temporal-backed `tests/api/conftest.py` `client` fixture with an in-process worker (`temporal_worker_inprocess=True`, the fake 3MF openscad `tests.conftest.fake_3mf_openscad`), replacing today's `temporal_address=""` fixture; `tests/api/test_temporal_path.py` shows the shape.

These names are fixed from those phases:
- Phase 1: `TemplatePipeline`, `RenderPiece`, `PieceRequest`, `PieceResult`, `PieceOutcome`, `Projection`, `Failure`, `piece_key`, `RETRY`, `PROJECT_RETRY`, `SHORT`, `_openscad_timeout`, `_waiter_recheck`, `_failure_of` (`workflows/pipelines.py`, `workflows/models.py`); `RenderActivities`, `WorkerDeps`, `_heartbeating`, `_failure`, `PIECE_NAME`, `_read_piece` (`workflows/activities.py`); `render_worker(client, task_queue, activities, *, build_id, max_concurrent_activities, graceful_shutdown_timeout)`; `build_worker_deps` (`worker.py`); `JobProjection` (`render/projection.py`); `RenderService` (`render/submit.py`: `submit`, `reconcile_once`, `_start`, `_memo`, `_reconcile_forever`, `store`, `config`, `client`, `task_queue`); `tests/support/temporal.py` `temporal_client()`; `tests/conftest.py` `fake_3mf_openscad(directory) -> str` (one blue 10 × 10 × 2 box per 3MF; its `.param` has one parameter, `width`).
- Phase 2: `render/inputs.py` (`normalize_inputs`, `legacy_inputs`, `InputsError`); `UiDeclaration`, `ModelMeta.ui`, `ModelMeta.ui_error`, `ModelRecord.ui_error` (`library/catalogue.py`); `OutputStore.create(job, *, name, public_url, inputs)`, `OutputStore.inputs`, `CreateOutputRequest.inputs`, `OutputDetail.inputs`; `RenderService.submit(slug, params, *, model_version, supersedes, inputs)`; `frontend/src/lib/saveOutput.ts`; `frontend/src/lib/inputs.ts`; `models/dollhouse-kit/ui/pieces.js` (`housePieces`, `clampHouse`, `pieceParams`, `DEFAULT_HOUSE`, `LIMITS`); `inputs.house`.
- Phase 3 (#426, plan PR #590; read at f9223552):
  - `store/__init__.py`: the `BlobStore` protocol keeps phase 1's sync `dir_for`, `exists`, `remove`, `keys`, `touched_at` (nothing is renamed; `exists` reads the index on the bambuddy backend, so call it from a thread) and adds async `fetch(key) -> bool`, `checkout(key) -> str | None`, `checkout_fresh(key) -> str | None`, `publish(key, *, scope: BlobScope) -> None`, `indexed_sha(key) -> str | None`, `publish_fresh(key, *, scope: BlobScope, expected: str | None) -> None`, and `PieceStateLostError`. Phase 1's `sweep_blobs` stays there for the local backend.
  - `store/local.py`: phase 1's `LocalBlobStore(root)`, whose phase 3 methods keep phase 1's behaviour (`fetch` is `exists`; `publish`/`publish_fresh` do nothing), and `LocalContentBackend`. This plan's tests build `LocalBlobStore` directly, so `fetch`/`publish` calls in them are no-ops on the shared directory.
  - `store/refs.py`: phase 1's `BlobRefs` (`add`, `drop_holder`, `referenced`).
  - `store/content_models.py`: `BlobScope(slug=None, title=None, folder: "work" | "output" = "work", project_id=None)`, `BlobKind`, `BlobRef`, `BlobStat`; `BlobScope` is also importable from `store/content.py`, which is where this plan imports it.
  - `store/content.py`: `template_title(model_dir, fallback)`, `ContentStore`, `sweep_content(content, refs, *, grace)` (the swept kinds are `piece` and `snapshot`; an output blob is a `piece`-kind blob, refed on `done` by Task 4 Step 7).
  - `store/cache.py`: `CachedBlobStore` (the bambuddy backend's per-worker cache: `publish` swaps against the directory's marker, `publish_fresh` against `expected`) and `materialize_result(blobs, result)`.
  - `store/fonts.py`: `model_dir(scad, file)` (the template directory from a piece's source path), `wanted_families`, `FontMirror`; `store/snapshots.py`: `SnapshotStore`; `store/assets.py`: `RemoteAssets`.
  - `store/factory.py`: `StoreBundle` and `build_store`. `AppState.store` is the bundle and `AppState.blobs` is `state.store.blobs`, set in the lifespan.
  - `workflows/activities.py`: `WorkerDeps` gains `snapshots`, `fonts_mirror`, `remote_assets` (all default None, so this plan's `WorkerDeps(...)` test constructions need not pass them); `_scope(req, prepared)`; the stages continue a piece with `_checkout` → `publish_fresh`.
  - `workflows/pipelines.py`: `TRANSFER`, `HEARTBEAT`, `CACHED_TIMEOUT`, `FINISH_TIMEOUT`, `PREPARE_TIMEOUT`, `_main_timeout()`, `_solids_timeout()` beside phase 1's `SHORT` and `_openscad_timeout()`; `_waiter_recheck` sums every stage's bound, so a stage this plan adds to `RenderPiece` goes into that sum too.
  - `worker.py`: `build_worker_deps(settings)` returns `(WorkerDeps, StoreBundle)`; `worker_deps_from_state(state)` takes the store's parts from `state.store`.

## Global Constraints

- `model.json` gains `"pipeline": {"module": "pipeline/pipeline.py", "api": 1}` (§5.1).
- "`pipeline.py` must be deterministic. Temporal's workflow sandbox enforces the import and I/O rules; ScadBuddy reports a violation as the job's `error` with the pipeline file and line" (§5.2).
- "The source text becomes part of the workflow history, so a replay always runs the code the job started with. Its sha256 is projected as `render_jobs.pipeline_version` (`default` for the built-in pipeline)" (§3.4).
- "The source is compiled with the template file's name so tracebacks carry `pipeline/pipeline.py:<line>`; a sandbox violation (the SDK's `RestrictedWorkflowAccessError` …) is wrapped into the job's `error` together with that location" (§3.4).
- "`render` is a child `RenderPiece` … `Part` is a reference … Reading mesh bytes happens in activities, never in the workflow" (§5.2).
- `activity(name, …)` runs `pipeline/activities.py:<name>` through one generic `run_template_activity` on the `render` queue. "Arguments and results are JSON plus `Blob` references"; "Heartbeat is automatic every 5 s; `start_to_close` defaults to `SCADBUDDY_RENDER_TIMEOUT + 60 s`, overridable per call up to `SCADBUDDY_TEMPLATE_ACTIVITY_MAX_TIMEOUT` (default 30 min)"; "runs the template function in a subprocess it kills on cancellation" (§5.2). "start their child with `start_new_session=True` and kill the **process group** (`os.killpg`)" (§3.4).
- "A pipeline may call `output` more than once (one 3MF per storey)." "`bom` is structured, not a file: `[{piece, label, count, plates: [int], part: PartRef}]`, stored on the output and rendered by the host as a table" (§5.2).
- "Template activities import what the worker image ships (`numpy`, `lxml`, `Pillow`, the stdlib, and `scadbuddy.render.*` as a public surface documented in the authoring skill). Nothing is installed per template" (§5.2).
- Default pipeline: `part = await ctx.render("model.scad", **inputs["params"])`, then `await ctx.output(plates=await ctx.pack([part]), name=inputs.get("name"))`. It is "today's behaviour, including base spec §6.4's `plates = N` echo handling inside `pack`. Every Generate, on every template, goes through `TemplatePipeline`; there is one code path" (§5.3).
- "A template declares `INPUTS_VERSION` in `pipeline.py` and stamps it as `inputs.v`. It may define `migrate(inputs, from_version) -> inputs`. … the host calls the pipeline's `migrate` (an activity, so it may read the snapshot) before handing inputs to the UI. A failed migration shows the raw inputs read-only with the error" (§8.2).
- "`pipeline.api`: the host supports the current major and the previous one; an unsupported major … the job fails at `load_pipeline` with a message naming the majors" (§8.1).
- "Every output stores: template revision, `ui.api`, `pipeline.api`, `pipeline_version`, `inputs.v`, the worker image digest (`SCADBUDDY_REVISION`), the `openscad --version` string, the plate geometry key, and the store refs of every Part it was built from" (§8.4).
- "Decided: template code is **not sandboxed**" (§9). Containment is by reach. The template subprocess gets the allowlisted environment (`core/fontconfig.env_for`), never the worker's.
- `GET /outputs/{id}`: "plus `inputs`, `bom`, `manifest`, `record` (§8.4), `files`" (§10). `manifest` is phase 5.
- Code in this plan is laid out for reading, not to ruff's 100 columns. Run `uv run --frozen ruff format .` after pasting; it wraps calls, but not a string literal, so split any literal `ruff check` still reports as E501 with implicit concatenation. mypy runs strict (`warn_return_any`): bind an `Any` activity result to a typed local before returning it, as `Ctx.pack` does.
- Backend gates per task: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest` green. `requires_temporal` tests use the `temporal` CLI dev server. `requires_postgres` tests need `SCADBUDDY_TEST_DATABASE_URL`. Frontend: `pnpm lint && pnpm typecheck && pnpm test && pnpm build`. Agent: the same four in `agent/`.
- Generated API files (#492) are never committed. After an API model or route change (Tasks 2, 6, 7), run `cd frontend && pnpm gen:api`, then `cd agent && pnpm gen:api`, before either package's typecheck.
- Every `/api/v1` operation needs an agent tool or an `agent/src/tools/coverage.ts` `NOT_A_TOOL` entry (CLAUDE.md).
- Schema changes are a NEW file `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one (CLAUDE.md "Migrations").
- Every `docker run` in a `verify.sh` carries `--label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}"` on the same line (CLAUDE.md).
- Conventional-commit titles. Every PR body says `Part of #427`, except the last, which says `Fixes #427`. No docstrings or comments on unchanged code.

## Review Focus

1. **A template parameter named `file`** (or any other name `ctx.render` itself uses) must reach the render through the default pipeline, never collide. `ctx.render(file, /, **params)` is positional-only. Pinned in Task 4 by `test_a_parameter_named_file_reaches_the_render`.
2. **A pipeline that touches a restricted API, raises, writes no output, or never yields** must fail the job: the first three with `pipeline/pipeline.py:<line>` in `error`, the last within `pipeline_timeout`. It must never leave the workflow task retrying forever with the row `running`. Pinned in Task 4 by `test_a_restricted_call_fails_the_job_with_its_line` and `test_a_pipeline_that_writes_nothing_fails`, and in Task 5 by `test_a_pipeline_that_never_yields_times_out` and `test_a_timed_out_pipeline_is_failed_by_the_reconciler`.
3. **A worker restart mid-house, after `pipeline.py` was edited:** the job finishes with the source it started with, and finished pieces are not rendered again. Pinned in Task 4 by `test_an_edit_mid_run_does_not_change_the_running_job`.
4. **A template activity that hangs, or spawns children, when its job is cancelled:** every process in its group is gone when the activity returns. Pinned in Task 5 by `test_cancelling_kills_the_template_process_group`.
5. **Reopening an output whose inputs are newer than the template, or whose `migrate` raises:** the raw inputs are shown read-only with the error, never silently dropped or replaced by defaults. Pinned in Task 6 by `test_inputs_newer_than_the_template_are_refused` and in Task 8 by `shows raw inputs read-only when migration fails`.

---

## File Structure

Backend, created:
- `backend/scadbuddy/workflows/sandbox.py`: `PIPELINE_MODULE_NAME`, `PipelineContractError`, `load_pipeline_module`, `pipeline_error`. Compiles and runs pipeline source inside the sandbox.
- `backend/scadbuddy/library/pipelines.py`: `PIPELINE_API_SUPPORTED`, `DEFAULT_PIPELINE_SOURCE`, `DEFAULT_PIPELINE_FILE`, `pipeline_version_of`, `inputs_version_of`. Pure, with no Temporal import.
- `backend/scadbuddy/template.py`: the public surface a template activity imports (`Blob`, `Part`, `emit`). The workflow payloads reuse `Blob` and `Part`.
- `backend/scadbuddy/workflows/packing.py`: `GAP_MM`, `PackError`, `shelf_pack`, `explicit_plate`. Pure.
- `backend/scadbuddy/workflows/outputs.py`: `output_key`, `build_output`. Builds one pipeline output's blob.
- `backend/scadbuddy/workflows/template_process.py`: `TemplateError`, `MAX_RESULT_BYTES`, `run_template`. The subprocess, its process group and its result.
- `backend/scadbuddy/workflows/template_runner.py`: `python -m scadbuddy.workflows.template_runner`, the child side.
- `backend/scadbuddy/workflows/ctx.py`: `Ctx`, `PieceFailedError`. The object a pipeline gets.
- `backend/scadbuddy/workflows/pipeline_activities.py`: `PipelineActivities` (`load_pipeline`, `pack`, `write_output`, `run_template_activity`, `migrate_inputs`).
- `backend/scadbuddy/workflows/verify_pipeline.py`: `python -m scadbuddy.workflows.verify_pipeline`, for `verify.sh`.
- `backend/scadbuddy/migrations/<ts>_render_jobs_outputs.sql`: `render_jobs.outputs`.
- `backend/tests/support/pipelines.py`: `FakeWorld`, fake activities by name, `run_job`.
- `backend/tests/support/pipeline_probe.py`: `ExecProbe`, the sandbox measurement workflow.
- Tests: `backend/tests/test_pipeline_sandbox.py`, `test_pipeline_declaration.py`, `test_packing_and_outputs.py`, `test_template_pipeline.py`, `test_template_activities.py`, `test_migrate_inputs.py`, `test_dollhouse_pipeline.py`, and `backend/tests/api/test_pipeline_api.py`.

Backend, modified:
- `workflows/models.py`: `PieceOutcome.piece_key`, `Projection.outputs`, `Projection.blob_keys`, and new payloads (`PackItem`, `Placed`, `LayoutPlate`, `Layout`, `PlateSize`, `LoadRequest`, `LoadedPipeline`, `PackRequest`, `OutputRequest`, `OutputRef`, `TemplateCall`, `MigrateRequest`, `MigrateResult`).
- `render/job_models.py`: `BomEntry`, `OutputRecord`, `PipelineOutput`, `Job.outputs`.
- `workflows/pipelines.py`: `TemplatePipeline` runs pipelines; outcomes keyed by piece; `MigrateInputs`.
- `workflows/activities.py`: `WorkerDeps.revision`, `.openscad_version`, `.template_python`; `prepare` renders `req.file` and validates params; `project` copies outputs and refs every blob key.
- `workflows/client.py`: `render_worker` registers `PipelineActivities` and `MigrateInputs`.
- `render/projection.py`: the `outputs` column.
- `library/catalogue.py`: `PipelineDeclaration`, `ModelMeta.pipeline`, `ModelMeta.pipeline_error`, `ModelRecord.pipeline_error`, `ModelRecord.inputs_version`.
- `render/runner.py`: `params_problem` (the message half of `api/params.py` `require_valid_params`, beside `build_defines`).
- `render/inputs.py`: `inputs_key`.
- `render/submit.py`: whole-inputs job key; the `template_activity_max_timeout` memo; `migrate_inputs`.
- `core/config.py`, `core/settings.py`: `template_activity_max_timeout`.
- `api/jobs.py`: `JobStatus.outputs`; no API-side param validation for pipeline templates.
- `library/outputs.py`, `api/outputs.py`: `index`, `bom.json`, `record.json`, `files/`.
- `api/models.py`: `POST /models/{slug}/inputs/migrate`.
- `worker.py`: fills the new `WorkerDeps` fields.

Frontend: `src/api/client.ts`, `src/lib/saveOutput.ts`, `src/lib/useMigratedInputs.ts` (new), `src/components/BomTable.tsx` (new), `src/components/RawInputs.tsx` (new), `src/pages/HistoryPage.tsx`, `src/pages/CustomizePage.tsx`, `src/mocks/handlers.ts`, `src/mocks/fixtures.ts`.

Templates, agent, docs, CI: `models/dollhouse-kit/pipeline/pipeline.py`, `pipeline/activities.py` and `pipeline/verify-inputs.json` (new); `models/dollhouse-kit/model.json`, `verify.sh` and `README.md`; `agent/src/tools/coverage.ts`; `plugins/scadbuddy/skills/authoring/SKILL.md`; `.github/workflows/ci.yml` (`models` job); `CLAUDE.md`; and the spec's §3.6.

## Task order and parallelism

- Task 1 comes first: every later task relies on what it measures.
- Tasks 2 and 3 are independent of each other.
- Task 4 needs 2 and 3.
- Task 5 needs 4.
- Task 6 needs 5 (the runner).
- Task 7 needs 4 and 6.
- Task 8 needs 7.
- Task 9 needs 5.
- Task 10 needs 9.

Suggested PRs:
1. Tasks 1–4: the pipeline runtime, where the default pipeline keeps every template's behaviour.
2. Tasks 5–6: template activities and migrate.
3. Tasks 7–8: API and frontend.
4. Tasks 9–10: the dollhouse house, verify and the skill (`Fixes #427`).

---

### Task 1: Measure the sandbox: `exec`'d source, line numbers, restricted access

§3.6 leaves two facts to measure against `temporalio` 1.33.0:
- the exception type and message on a restricted call or import;
- whether a traceback through `exec`'d source compiled with a file name carries its line.

A third is this plan's own: Task 10's `verify_pipeline` starts a local dev server with `temporalio.testing.WorkflowEnvironment` and runs `TemplatePipeline` on it with the pydantic data converter. The base spec's rule is "measure, don't guess", so the shape of that API in 1.33.0 is measured here, before any task relies on it (Step 2's `test_the_testing_api_verify_pipeline_uses`), not discovered in Task 10.

This task writes the two functions everything else uses, pins those facts in tests, and records them in the spec.

**Files:**
- Create: `backend/scadbuddy/workflows/sandbox.py`, `backend/tests/support/pipeline_probe.py`, `backend/tests/test_pipeline_sandbox.py`
- Modify: `docs/superpowers/specs/2026-09-27-template-pipelines-design.md` (§3.6, the "Still to verify" paragraph)

**Interfaces:**
- Produces:
  ```python
  PIPELINE_MODULE_NAME = "scadbuddy_pipeline"
  class PipelineContractError(Exception)
  def load_pipeline_module(source: str, filename: str) -> dict[str, Any]   # compile + exec; raises SyntaxError, PipelineContractError, or what the module's top level raises
  def pipeline_error(error: BaseException, filename: str) -> str          # "<filename>:<line>: <Type>: <message>", or "<filename>: …" with no frame in it
  ```

- [ ] **Step 1: Write the probe workflow**

`backend/tests/support/pipeline_probe.py`:

```python
"""A workflow that runs pipeline source the way `TemplatePipeline` does, and reports
what came out: the measurement §3.6 asks for."""

from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from scadbuddy.workflows.sandbox import load_pipeline_module, pipeline_error

FILE = "pipeline/pipeline.py"


@workflow.defn(name="ExecProbe")
class ExecProbe:
    @workflow.run
    async def run(self, source: str) -> str:
        try:
            namespace = load_pipeline_module(source, FILE)
            return f"ok|{await namespace['run'](None, {})}"
        except Exception as error:
            kind = f"{type(error).__module__}.{type(error).__name__}"
            return f"{kind}|{pipeline_error(error, FILE)}"
```

- [ ] **Step 2: Write the failing tests**

`backend/tests/test_pipeline_sandbox.py`:

```python
"""Pipeline source in the workflow sandbox (spec 2026-09-27 §3.4 step 2, §3.6)."""

from __future__ import annotations

import inspect
import shutil
import uuid

import pytest
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from scadbuddy.workflows.sandbox import PipelineContractError, load_pipeline_module, pipeline_error
from tests.support.pipeline_probe import ExecProbe
from tests.support.temporal import TEST_TEMPORAL_DEV_SERVER, temporal_client

pytestmark = pytest.mark.requires_temporal


async def _probe(source: str) -> str:
    async with temporal_client() as client:
        queue = f"probe-{uuid.uuid4().hex[:8]}"
        async with Worker(client, task_queue=queue, workflows=[ExecProbe]):
            return await client.execute_workflow(
                ExecProbe.run, source, id=f"probe-{uuid.uuid4().hex}", task_queue=queue
            )


async def test_source_runs_and_returns() -> None:
    assert await _probe("async def run(ctx, inputs):\n    return 41 + 1\n") == "ok|42"


async def test_a_runtime_error_names_its_line() -> None:
    result = await _probe("async def run(ctx, inputs):\n    x = 1\n    return x / 0\n")
    assert result == "builtins.ZeroDivisionError|pipeline/pipeline.py:3: ZeroDivisionError: division by zero"


async def test_a_restricted_call_is_catchable_and_names_its_line() -> None:
    result = await _probe(
        "import datetime\n\nasync def run(ctx, inputs):\n    return datetime.datetime.now()\n"
    )
    kind, message = result.split("|", 1)
    assert kind.endswith("RestrictedWorkflowAccessError")
    assert message.startswith("pipeline/pipeline.py:4: RestrictedWorkflowAccessError: ")
    assert "datetime.datetime.now" in message


async def test_a_restricted_call_at_module_level_names_its_line() -> None:
    result = await _probe("x = 1\nopen('/etc/hostname')\n\nasync def run(ctx, inputs):\n    return 1\n")
    kind, message = result.split("|", 1)
    assert kind.endswith("RestrictedWorkflowAccessError")
    assert message.startswith("pipeline/pipeline.py:2: ")


async def test_a_syntax_error_names_its_line() -> None:
    result = await _probe("async def run(ctx, inputs)\n    return 1\n")
    assert result.split("|", 1)[1].startswith("pipeline/pipeline.py:1: SyntaxError: ")


def test_source_without_run_breaks_the_contract() -> None:
    with pytest.raises(PipelineContractError, match="defines no run"):
        load_pipeline_module("INPUTS_VERSION = 1\n", "pipeline/pipeline.py")


def test_an_error_with_no_pipeline_frame_names_only_the_file() -> None:
    assert pipeline_error(ValueError("x"), "pipeline/pipeline.py") == (
        "pipeline/pipeline.py: ValueError: x"
    )


async def test_the_testing_api_verify_pipeline_uses() -> None:
    """Task 10's `verify_pipeline` in 1.33.0: `start_local` takes an existing dev-server
    binary and a data converter, returns an environment whose client carries that
    converter and runs a workflow, and is shut down with `shutdown()`."""
    params = inspect.signature(WorkflowEnvironment.start_local).parameters
    assert {"dev_server_existing_path", "data_converter"} <= set(params)
    binary = TEST_TEMPORAL_DEV_SERVER or shutil.which("temporal")
    if binary is None:
        pytest.skip("no temporal CLI to start a local dev server with")
    env = await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, data_converter=pydantic_data_converter
    )
    try:
        assert env.client.data_converter is pydantic_data_converter
        queue = f"probe-{uuid.uuid4().hex[:8]}"
        async with Worker(env.client, task_queue=queue, workflows=[ExecProbe]):
            result = await env.client.execute_workflow(
                ExecProbe.run,
                "async def run(ctx, inputs):\n    return 1\n",
                id=f"probe-{uuid.uuid4().hex}",
                task_queue=queue,
            )
        assert result == "ok|1"
    finally:
        await env.shutdown()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_pipeline_sandbox.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'scadbuddy.workflows.sandbox'`.

- [ ] **Step 4: Implement `workflows/sandbox.py`**

```python
"""Pipeline source run inside the workflow sandbox (spec 2026-09-27 §3.4 step 2).

Compiled under the template file's own name, so a traceback frame carries
``pipeline/pipeline.py`` and its line. Nothing here reads a file: the source
came out of `load_pipeline`, and so out of the workflow history."""

from __future__ import annotations

from typing import Any

PIPELINE_MODULE_NAME = "scadbuddy_pipeline"


class PipelineContractError(Exception):
    """The source ran, but is not a pipeline (§5.2)."""


def load_pipeline_module(source: str, filename: str) -> dict[str, Any]:
    # dont_inherit: this module's `from __future__` flags are not the template's.
    code = compile(source, filename, "exec", dont_inherit=True)
    namespace: dict[str, Any] = {"__name__": PIPELINE_MODULE_NAME, "__file__": filename}
    exec(code, namespace)
    if not callable(namespace.get("run")):
        raise PipelineContractError(f"{filename} defines no run(ctx, inputs)")
    return namespace


def pipeline_error(error: BaseException, filename: str) -> str:
    """The job's `error`: where in the template it happened, then what. The innermost
    frame in ``filename`` wins; walking frames never reads a file (no linecache)."""
    line: int | None = None
    if isinstance(error, SyntaxError) and error.filename == filename:
        line = error.lineno
    tb = error.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == filename:
            line = tb.tb_lineno
        tb = tb.tb_next
    where = filename if line is None else f"{filename}:{line}"
    message = error.msg if isinstance(error, SyntaxError) else str(error)
    return f"{where}: {type(error).__name__}: {message}"
```

If `ruff check` reports `S102` on the `exec` line, add `# noqa: S102` to it; `exec` is the mechanism the spec asks for.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/test_pipeline_sandbox.py -q`
Expected: `8 passed`.

If `test_a_restricted_call_at_module_level_names_its_line` fails because `open` is not restricted in 1.33.0, change the source to `import datetime\ndatetime.date.today()\n…`, keeping the call on line 2, and note the change in Step 6. If `test_a_restricted_call_is_catchable_and_names_its_line` returns a result that does not start with `…RestrictedWorkflowAccessError`, stop: §3.4's error mapping has no footing, and the controller must decide. If `test_the_testing_api_verify_pipeline_uses` fails, stop too and report what 1.33.0 offers instead: Task 10 is written against exactly this shape (`env = await WorkflowEnvironment.start_local(dev_server_existing_path=…, data_converter=…)`, `env.client`, `await env.shutdown()`), which is also how phase 1's `tests/support/temporal.py` starts its dev server.

- [ ] **Step 6: Record the measurement in the spec**

Print the two restricted-access messages:

```bash
cd backend && uv run --frozen python - <<'PY'
import asyncio
from tests.test_pipeline_sandbox import _probe
for src in ("import datetime\n\nasync def run(ctx, inputs):\n    return datetime.datetime.now()\n",
            "x = 1\nopen('/etc/hostname')\n\nasync def run(ctx, inputs):\n    return 1\n"):
    print(asyncio.run(_probe(src)))
PY
```

In §3.6, replace the sentence starting "Still to verify when their phases land: the sandbox exception type and message shape…" with the text below. Paste the printed messages in place of the two quoted examples, and keep the worker-versioning clause that follows:

```markdown
- Pipeline source (phase 4, measured in `backend/tests/test_pipeline_sandbox.py`):
  `compile(source, "pipeline/pipeline.py", "exec")` then `exec` inside the sandbox
  works, and a traceback frame's `co_filename`/`tb_lineno` name the template file and
  line (a runtime error, a restricted call at module level or inside `run`, and a
  `SyntaxError` via its `lineno`). A restricted call raises
  `temporalio.worker.workflow_sandbox._restrictions.RestrictedWorkflowAccessError`,
  catchable inside the workflow, message e.g. "<paste the datetime message>"; at
  module level: "<paste the open message>". Walking `tb_next` needs no `linecache`
  (no file read inside the sandbox).
```

- [ ] **Step 7: Gates and commit**

Run the backend gates (Global Constraints). Expected: all green.

```bash
git add backend/scadbuddy/workflows/sandbox.py backend/tests/support/pipeline_probe.py \
  backend/tests/test_pipeline_sandbox.py docs/superpowers/specs/2026-09-27-template-pipelines-design.md
git commit -m "feat(workflows): run pipeline source in the sandbox; measure §3.6's open items (#427)"
```

---

### Task 2: `pipeline` in `model.json`, and the `load_pipeline` activity

**Files:**
- Create: `backend/scadbuddy/library/pipelines.py`, `backend/scadbuddy/workflows/pipeline_activities.py`, `backend/tests/test_pipeline_declaration.py`
- Modify: `backend/scadbuddy/library/catalogue.py` (`PipelineDeclaration`, `ModelMeta.pipeline`, `ModelMeta.pipeline_error`, `ModelMeta.pipeline_raw`, `ModelRecord.pipeline_error`, `ModelRecord.inputs_version`, `Catalogue.record`, `Catalogue.create`)
- Modify: `backend/scadbuddy/workflows/models.py` (`PlateSize`, `LoadRequest`, `LoadedPipeline`)
- Modify: `agent/` nothing; the fields are additive on an existing response.

**Interfaces:**
- Consumes: phase 2's `UiDeclaration` validator pattern; `resolve_source(slug, revision, *, paths, history, fetcher)` (`render/jobs.py`); `plate_for(model: str | None) -> PlateGeometry` (`render/plate.py`).
- Produces:
  ```python
  # library/pipelines.py
  PIPELINE_API_SUPPORTED: tuple[int, ...] = (1,)
  DEFAULT_PIPELINE_FILE = "<default pipeline>"
  DEFAULT_PIPELINE_SOURCE: str
  def pipeline_version_of(source: str) -> str          # sha256 hex
  def inputs_version_of(source: str) -> int            # top-level `INPUTS_VERSION = <int>`, else 0; never executes
  # library/catalogue.py
  PIPELINE_MODULE_PATTERN = r"^pipeline/[A-Za-z0-9_]+\.py$"
  class PipelineDeclaration(BaseModel): module: str; api: int (>= 1)
  ModelMeta.pipeline: PipelineDeclaration | None; ModelMeta.pipeline_error: str | None (excluded from dumps); ModelMeta.pipeline_raw: Any (excluded; `Catalogue.create` writes it back as "pipeline")
  ModelRecord.pipeline_error: str | None; ModelRecord.inputs_version: int = 0
  # workflows/models.py
  class PlateSize(BaseModel): key: str; width: float; depth: float
  class LoadRequest(BaseModel): slug: str; revision: str | None
  class LoadedPipeline(BaseModel): source: str; file: str; api: int; version: str; inputs_version: int; ui_api: int | None = None; plate: PlateSize
  # workflows/pipeline_activities.py
  class PipelineActivities:
      def __init__(self, deps: WorkerDeps) -> None
      def all(self) -> Sequence[Callable[..., Any]]
      @activity.defn(name="load_pipeline") async def load_pipeline(self, req: LoadRequest) -> LoadedPipeline
  ```
  An unsupported `api` raises `ApplicationError(type="PipelineApiError", non_retryable=True)`. So do a missing module file and an unreadable `pipeline` declaration.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_pipeline_declaration.py`:

```python
"""model.json's `pipeline` and load_pipeline (spec 2026-09-27 §5.1, §3.4 step 1, §8.1)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.catalogue import ModelMeta
from scadbuddy.library.pipelines import (
    DEFAULT_PIPELINE_FILE,
    DEFAULT_PIPELINE_SOURCE,
    inputs_version_of,
    pipeline_version_of,
)
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.models import LoadRequest
from scadbuddy.workflows.pipeline_activities import PipelineActivities

PIPELINE = "INPUTS_VERSION = 3\n\nasync def run(ctx, inputs):\n    return None\n"


def _template(tmp_path: Path, meta: dict[str, object], files: dict[str, str]) -> DataPaths:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("cube();\n", encoding="utf-8")
    paths.model_meta("demo").write_text(json.dumps({"name": "Demo", **meta}), encoding="utf-8")
    for name, body in files.items():
        target = paths.model_dir("demo") / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    return paths


def _acts(paths: DataPaths) -> PipelineActivities:
    deps = WorkerDeps(
        config=Config(data_dir=paths.root),
        paths=paths,
        assets=AssetStore(paths.assets),
        blobs=LocalBlobStore(paths.blobs),
        refs=None,  # type: ignore[arg-type]
        projection=None,  # type: ignore[arg-type]
    )
    return PipelineActivities(deps)


def test_inputs_version_is_read_without_running_the_source() -> None:
    assert inputs_version_of(PIPELINE) == 3
    assert inputs_version_of("import os\nos.system('false')\n") == 0
    assert inputs_version_of("INPUTS_VERSION = 'two'\n") == 0
    assert inputs_version_of("def (:\n") == 0


def test_a_malformed_pipeline_costs_only_the_pipeline() -> None:
    meta = ModelMeta.model_validate({"name": "Demo", "pipeline": {"module": "../x.py", "api": 1}})
    assert meta.pipeline is None
    assert meta.pipeline_error is not None and "module" in meta.pipeline_error
    dumped = meta.model_dump()
    assert "pipeline_error" not in dumped and "pipeline_raw" not in dumped
    assert dumped["pipeline"] is None  # the API never shows a malformed declaration
    assert meta.pipeline_raw == {"module": "../x.py", "api": 1}


def test_writing_model_json_keeps_a_malformed_pipeline(tmp_path: Path) -> None:
    from scadbuddy.library.catalogue import Catalogue, ModelPatch
    from scadbuddy.render.solids import WRAPPER_PREFIX

    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    catalogue = Catalogue(paths, wrapper_prefix=WRAPPER_PREFIX)
    bad = {"module": "../x.py", "api": 1}
    catalogue.create("demo", "cube();\n", ModelMeta.model_validate({"name": "Demo", "pipeline": bad}))
    assert json.loads(paths.model_meta("demo").read_text())["pipeline"] == bad
    catalogue.update("demo", ModelPatch(name="Renamed"))
    assert json.loads(paths.model_meta("demo").read_text())["pipeline"] == bad


def test_the_model_record_schema_keeps_its_properties() -> None:
    from scadbuddy.library.catalogue import ModelRecord

    properties = ModelRecord.model_json_schema(mode="serialization")["properties"]
    assert {"name", "pipeline", "pipeline_error", "inputs_version"} <= set(properties)


async def test_no_pipeline_loads_the_default(tmp_path: Path) -> None:
    loaded = await ActivityEnvironment().run(
        _acts(_template(tmp_path, {}, {})).load_pipeline, LoadRequest(slug="demo", revision=None)
    )
    assert loaded.source == DEFAULT_PIPELINE_SOURCE
    assert loaded.file == DEFAULT_PIPELINE_FILE
    assert loaded.version == "default"
    assert loaded.plate.key == "default"


async def test_a_declared_pipeline_loads_its_source_and_sha(tmp_path: Path) -> None:
    paths = _template(
        tmp_path,
        {"pipeline": {"module": "pipeline/pipeline.py", "api": 1}, "ui": {"module": "ui/index.js", "api": 1}},
        {"pipeline/pipeline.py": PIPELINE},
    )
    loaded = await ActivityEnvironment().run(
        _acts(paths).load_pipeline, LoadRequest(slug="demo", revision=None)
    )
    assert loaded.source == PIPELINE
    assert loaded.file == "pipeline/pipeline.py"
    assert loaded.version == hashlib.sha256(PIPELINE.encode()).hexdigest() == pipeline_version_of(PIPELINE)
    assert (loaded.api, loaded.inputs_version, loaded.ui_api) == (1, 3, 1)


@pytest.mark.parametrize(
    ("meta", "files", "message"),
    [
        ({"pipeline": {"module": "pipeline/pipeline.py", "api": 7}}, {"pipeline/pipeline.py": PIPELINE}, "supports majors 1"),
        ({"pipeline": {"module": "pipeline/pipeline.py", "api": 1}}, {}, "pipeline/pipeline.py is missing"),
        ({"pipeline": {"module": "../x.py", "api": 1}}, {}, "module"),
    ],
)
async def test_an_unusable_pipeline_fails_the_job_at_load(
    tmp_path: Path, meta: dict[str, object], files: dict[str, str], message: str
) -> None:
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(
            _acts(_template(tmp_path, meta, files)).load_pipeline, LoadRequest(slug="demo", revision=None)
        )
    assert raised.value.type == "PipelineApiError"
    assert raised.value.non_retryable
    assert message in raised.value.message
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_pipeline_declaration.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'scadbuddy.library.pipelines'`.

- [ ] **Step 3: Implement `library/pipelines.py`**

```python
"""What a template's pipeline is, read without running it (spec 2026-09-27 §5, §8)."""

from __future__ import annotations

import ast
import hashlib

#: `pipeline.api` majors this host runs: the current one and, once there is one, the
#: previous (§8.1).
PIPELINE_API_SUPPORTED: tuple[int, ...] = (1,)

#: The file name the built-in pipeline's frames carry.
DEFAULT_PIPELINE_FILE = "<default pipeline>"

#: §5.3: today's behaviour, including §6.4's `plates = N` inside `pack`.
DEFAULT_PIPELINE_SOURCE = '''\
async def run(ctx, inputs):
    part = await ctx.render("model.scad", **inputs.get("params", {}))
    await ctx.output(plates=await ctx.pack([part]), name=inputs.get("name"))
'''


def pipeline_version_of(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def inputs_version_of(source: str) -> int:
    """The top-level ``INPUTS_VERSION = <int>`` (§8.2), found by parsing: the API
    process never runs template code (§9)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "INPUTS_VERSION" for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is int
        ):
            return node.value.value
    return 0
```

- [ ] **Step 4: Declare `pipeline` in the catalogue**

In `library/catalogue.py`, beside phase 2's `UI_MODULE_PATTERN` and `UiDeclaration`:

```python
PIPELINE_MODULE_PATTERN = r"^pipeline/[A-Za-z0-9_]+\.py$"


class PipelineDeclaration(BaseModel):
    """``model.json``'s ``pipeline`` (spec 2026-09-27 §5.1)."""

    module: str = Field(pattern=PIPELINE_MODULE_PATTERN, max_length=200)
    #: The pipeline-API major (§8.1). The worker decides whether it can run it.
    api: int = Field(ge=1)
```

In `ModelMeta`, after phase 2's `ui_error`:

```python
    #: The template's own pipeline (#427), or None for the default (§5.3).
    pipeline: PipelineDeclaration | None = None
    #: Why a ``pipeline`` on disk could not be read; never written back.
    pipeline_error: str | None = Field(default=None, exclude=True)
    #: The unreadable declaration as written: `Catalogue.create` writes it back to
    #: model.json, so the author fixes it and the host never deletes it.
    pipeline_raw: Any = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _readable_pipeline(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("pipeline") is None:
            return data
        try:
            PipelineDeclaration.model_validate(data["pipeline"])
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'pipeline'}: {e['msg']}" for e in error.errors()
            )
            return {**data, "pipeline": None, "pipeline_raw": data["pipeline"], "pipeline_error": f"pipeline: {problems}"}
        return data
```

No model serializer on `ModelMeta`: `ModelRecord(ModelMeta)` is the response model of `GET /models` and `GET /models/{slug}`, and a wrap serializer returning `dict[str, Any]` would become its serialization-mode JSON schema, flattening `ModelRecord` in `openapi.json` and the generated `schema.d.ts`. `test_the_model_record_schema_keeps_its_properties` pins that; after this task, `uv run --frozen python -m scadbuddy.tools.export_openapi` must still list `ModelRecord`'s properties (check it before `pnpm gen:api`).

Put the raw value back where `model.json` is written from a `ModelMeta`. At 69306836 that is one site. `Catalogue.create` (`library/catalogue.py:693`) writes `self.write_raw_meta(slug, meta.model_dump(exclude={"media"}))`; make it:

```python
            written = meta.model_dump(exclude={"media"})
            if meta.pipeline is None and meta.pipeline_raw is not None:
                written["pipeline"] = meta.pipeline_raw  # the author's, to fix; never dropped
            self.write_raw_meta(slug, written)
```

The metadata PATCH (`Catalogue.update`, `library/catalogue.py:821–827`) already reads the raw dict (`read_raw_meta`), updates it and writes it back with `write_raw_meta`, so it keeps a malformed `pipeline` with no change; the duplicate path (`library/catalogue.py:776–777`) copies the raw dict too. `test_writing_model_json_keeps_a_malformed_pipeline` pins `create` and `update`; the duplicate path copies the raw dict and is not exercised by that test. (If `Catalogue.create` needs the media store by then, build it as `tests/test_media_store.py:159` does.)

Phase 2's `_readable_ui` returns `"ui": None`, so `create` drops a malformed `ui` the same way. Phase 2 declined fixing it (its self-review, "Declined (review M10)"), because upload could not carry `ui/` files; this plan leaves `ui` as phase 2 decided.

Add `pipeline_error: str | None = None` and `inputs_version: int = 0` to `ModelRecord`. In `Catalogue.record`, where phase 2 sets `ui_error=meta.ui_error`, add `pipeline_error=meta.pipeline_error` and `inputs_version=self._inputs_version(slug, meta)` with:

```python
    def _inputs_version(self, slug: str, meta: ModelMeta) -> int:
        if meta.pipeline is None:
            return 0
        path = self.paths.model_dir(slug) / meta.pipeline.module
        try:
            return inputs_version_of(path.read_text(encoding="utf-8"))
        except OSError:
            return 0
```

(`from scadbuddy.library.pipelines import inputs_version_of`. If `Catalogue` names its `DataPaths` attribute differently, use that name.)

- [ ] **Step 5: Add the payloads and the activity**

In `workflows/models.py`:

```python
class PlateSize(BaseModel):
    """`ctx.plate` (§5.2): the bed a pipeline packs for, by its geometry key."""

    key: str
    width: float
    depth: float


class LoadRequest(BaseModel):
    slug: str
    revision: str | None


class LoadedPipeline(BaseModel):
    """What `load_pipeline` returns: recorded in the history, so a replay runs this
    source whatever the template holds by then (§3.4, §8.3)."""

    source: str
    file: str
    api: int
    #: sha256 of ``source``, or "default" (§3.2).
    version: str
    inputs_version: int
    ui_api: int | None = None
    plate: PlateSize
```

`backend/scadbuddy/workflows/pipeline_activities.py`:

```python
"""The activities behind a pipeline's `ctx` (spec 2026-09-27 §5.2), beside the render
stages in `activities.py`."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.library.catalogue import ModelMeta
from scadbuddy.library.pipelines import (
    DEFAULT_PIPELINE_FILE,
    DEFAULT_PIPELINE_SOURCE,
    PIPELINE_API_SUPPORTED,
    inputs_version_of,
    pipeline_version_of,
)
from scadbuddy.render.jobs import resolve_source
from scadbuddy.render.plate import plate_for
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.models import LoadedPipeline, LoadRequest, PlateSize


def _refuse(message: str) -> ApplicationError:
    return ApplicationError(message, type="PipelineApiError", non_retryable=True)


def plate_size(model: str | None) -> PlateSize:
    plate = plate_for(model)
    return PlateSize(key=plate.key, width=plate.usable.width, depth=plate.usable.depth)


class PipelineActivities:
    def __init__(self, deps: WorkerDeps) -> None:
        self.deps = deps

    def all(self) -> Sequence[Callable[..., Any]]:
        return [self.load_pipeline]

    async def model_dir(self, slug: str, revision: str | None) -> Path:
        """The template's directory at ``revision``: the live one, or its export."""
        d = self.deps
        source = await resolve_source(slug, revision, paths=d.paths, history=d.history, fetcher=d.fetcher)
        return source.scad.parent

    @activity.defn(name="load_pipeline")
    async def load_pipeline(self, req: LoadRequest) -> LoadedPipeline:
        directory = await self.model_dir(req.slug, req.revision)
        raw = json.loads(await asyncio.to_thread((directory / "model.json").read_text, "utf-8"))
        meta = ModelMeta.model_validate(raw)
        ui_api = meta.ui.api if meta.ui is not None else None
        plate = plate_size(None)
        if meta.pipeline_error is not None:
            raise _refuse(meta.pipeline_error)
        if meta.pipeline is None:
            return LoadedPipeline(
                source=DEFAULT_PIPELINE_SOURCE, file=DEFAULT_PIPELINE_FILE, api=1,
                version="default", inputs_version=0, ui_api=ui_api, plate=plate,
            )
        if meta.pipeline.api not in PIPELINE_API_SUPPORTED:
            majors = ", ".join(str(m) for m in PIPELINE_API_SUPPORTED)
            raise _refuse(
                f"{meta.pipeline.module} declares pipeline api {meta.pipeline.api}; "
                f"this ScadBuddy supports majors {majors}"
            )
        path = directory / meta.pipeline.module
        try:
            source = await asyncio.to_thread(path.read_text, "utf-8")
        except FileNotFoundError:
            raise _refuse(f"{meta.pipeline.module} is missing from the template") from None
        return LoadedPipeline(
            source=source, file=meta.pipeline.module, api=meta.pipeline.api,
            version=pipeline_version_of(source), inputs_version=inputs_version_of(source),
            ui_api=ui_api, plate=plate,
        )
```

`plate_size(None)` is the default plate. §5.2's "selected/default printer's bed" has no printer at Generate time (see Disagreements).

- [ ] **Step 6: Run the tests, the gates, and commit**

Run: `cd backend && uv run --frozen pytest tests/test_pipeline_declaration.py tests/api -q -k "pipeline or model"`
Expected: all pass.

Then run the backend gates, then `cd frontend && pnpm gen:api && pnpm typecheck`, then `cd agent && pnpm gen:api && pnpm typecheck`. Expected: green, because the new fields are only additions.

```bash
git add backend/scadbuddy/library/pipelines.py backend/scadbuddy/library/catalogue.py \
  backend/scadbuddy/workflows/models.py backend/scadbuddy/workflows/pipeline_activities.py \
  backend/tests/test_pipeline_declaration.py
git commit -m "feat(pipelines): pipeline in model.json and the load_pipeline activity (#427)"
```

---

### Task 3: Parts, packing, outputs; a piece renders any file and checks its params

**Files:**
- Create: `backend/scadbuddy/template.py`, `backend/scadbuddy/workflows/packing.py`, `backend/scadbuddy/workflows/outputs.py`, `backend/tests/test_packing_and_outputs.py`
- Create: `backend/tests/support/openscad.py` (`install_fake_openscad`, shared with `tests/test_activities.py`)
- Modify: `backend/scadbuddy/render/job_models.py` (`BomEntry`, `OutputRecord`, `PipelineOutput`, `Job.outputs`)
- Modify: `backend/scadbuddy/workflows/models.py` (`PackItem`, `Placed`, `LayoutPlate`, `Layout`, `PackRequest`, `OutputRequest`, `OutputRef`; re-export `Blob`, `Part`)
- Modify: `backend/scadbuddy/workflows/activities.py` (`WorkerDeps.revision`, `.openscad_version`, `.template_python`; `prepare` renders `req.file` and validates)
- Modify: `backend/scadbuddy/workflows/pipeline_activities.py` (`pack`, `write_output`)
- Modify: `backend/scadbuddy/render/runner.py` (`params_problem`), `backend/scadbuddy/api/params.py` (calls it)
- Modify: `backend/tests/test_activities.py` (import the fake from `tests/support/openscad.py`)

**Interfaces:**
- Consumes: `LoadedPipeline`, `PlateSize`, `plate_size`, `PipelineActivities` (Task 2); phase 1's `PieceRequest.file`, `PIECE_NAME`, `_read_piece`; the render helpers `PlateLayout`, `LAYOUT_NAME`, `MODEL_NAME`, `PREVIEW_NAME`, `plates_thumbnails`, `result_parts`, `result_plates` (`render/jobs.py`, the names `finish_piece_stage` uses); `write_plates_3mf`, `PlateParts` (`render/bambu3mf.py`); `write_glb`, `bounding_box`, `BoundingBox` (`render/glb.py`); `ColourPart` (`render/split.py`); phase 3's `BlobStore.fetch`/`publish`, `BlobScope`.
- Produces:
  ```python
  # template.py
  class Blob(BaseModel): kind: Literal["blob"] = "blob"; key: str; path: str; local: str | None (excluded)
      def read_bytes(self) -> bytes
  class Part(BaseModel): kind: Literal["part"] = "part"; piece_key: str; file: str; bbox: BoundingBox; colours: list[str]; notes: list[str] = []; plates: int = 1; local: str | None (excluded)
      def meshes(self) -> dict[str, trimesh.Trimesh]
      @classmethod def of(cls, req: PieceRequest, piece: PieceResult) -> Part
  def emit(name: str, data: bytes | str) -> Blob
  # render/job_models.py
  class BomEntry(BaseModel): piece: str; label: str; count: int (>= 1); plates: list[int] = []; part: str | None = None
  class OutputRecord(BaseModel): revision: str | None; ui_api: int | None; pipeline_api: int; pipeline_version: str; inputs_v: int; image_revision: str = ""; openscad_version: str = ""; plate_key: str; parts: list[str]
  FILE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"   # render/job_models.py: an output file's name; library/outputs.py reads it too
  class PipelineOutput(BaseModel): name: str | None; result: JobResult; bom: list[BomEntry] = []; files: list[str] = []; files_key: str | None = None; blob_keys: list[str]; record: OutputRecord
  Job.outputs: list[PipelineOutput] = []
  # workflows/models.py
  class PackItem(BaseModel): part: Part; count: int = 1
  class Placed(BaseModel): piece_key: str; x: float; y: float
  class LayoutPlate(BaseModel): items: list[Placed]
  class Layout(BaseModel): plates: list[LayoutPlate] = []; own: str | None = None
  class PackRequest(BaseModel): items: list[PackItem]; plate: PlateSize; goal: str = "fewest_plates"
  class OutputRequest(BaseModel): job_id: str; index: int; slug: str; layout: Layout; parts: list[Part]; name: str | None; bom: list[BomEntry]; files: dict[str, str | Blob]; plate_model: str | None = None; record: OutputRecord
  class OutputRef(BaseModel): index: int; name: str | None
  # workflows/packing.py
  GAP_MM = 5.0
  class PackError(ValueError)
  def shelf_pack(items: Sequence[PackItem], plate: PlateSize) -> Layout
  def explicit_plate(parts: Sequence[Part], at: Sequence[tuple[float, float, float]]) -> LayoutPlate
  # workflows/outputs.py
  def output_key(job_id: str, index: int) -> str        # "output-<job_id>-<index>"
  async def build_output(req: OutputRequest, deps: WorkerDeps, *, model_dir: Path) -> PipelineOutput
  # render/runner.py (beside build_defines: the worker must not import the API)
  def params_problem(schema: CustomizerSchema, params: Mapping[str, ParamValue]) -> str | None
  # activities
  def pack_layout(req: PackRequest) -> Layout            # pipeline_activities.py; raises ApplicationError(type="PackError", non_retryable=True)
  PipelineActivities.pack(req: PackRequest) -> Layout            @activity.defn(name="pack"), = pack_layout(req)
  PipelineActivities.write_output(req: OutputRequest) -> PipelineOutput   @activity.defn(name="write_output")
  WorkerDeps.revision: str = ""; WorkerDeps.openscad_version: str = ""; WorkerDeps.template_python: str = sys.executable
  ```
  In `prepare`, a bad `req.file` or bad params raise `ApplicationError(type="ParameterError", non_retryable=True, details=[Failure])`.

- [ ] **Step 1: A shared fake-openscad `Config`**

`tests/test_activities.py`'s `_config(tmp_path, paths)` is one line over `tests/conftest.py`'s `fake_3mf_openscad`. Make it shared. Create `backend/tests/support/openscad.py`:

```python
"""A `Config` whose openscad is `tests.conftest`'s fake: one blue box per 3MF, and a
`.param` with one parameter, `width`."""

from __future__ import annotations

from pathlib import Path

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from tests.conftest import fake_3mf_openscad


def install_fake_openscad(tmp_path: Path, paths: DataPaths) -> Config:
    return Config(openscad=fake_3mf_openscad(tmp_path / "bin"), data_dir=paths.root)
```

In `tests/test_activities.py`, delete `_config`, import `install_fake_openscad` from `tests.support.openscad`, and replace `_config(` with `install_fake_openscad(`.

Run: `cd backend && uv run --frozen pytest tests/test_activities.py -q`
Expected: the same pass count as before.

- [ ] **Step 2: Write the failing tests**

`backend/tests/test_packing_and_outputs.py`:

```python
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
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.template import Blob, Part
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps, _scope
from scadbuddy.workflows.models import (
    Layout,
    LayoutPlate,
    OutputRequest,
    PackItem,
    PackRequest,
    PieceRequest,
    PlateSize,
    piece_key,
)
from scadbuddy.workflows.packing import GAP_MM, PackError, explicit_plate, shelf_pack
from scadbuddy.workflows.pipeline_activities import PipelineActivities
from tests.support.openscad import install_fake_openscad

PLATE = PlateSize(key="default", width=256.0, depth=256.0)


def _part(key: str, w: float, d: float, *, plates: int = 1) -> Part:
    return Part(
        piece_key=key, file="model.scad", colours=["#FF0000"], plates=plates,
        bbox=BoundingBox(min=(-w / 2, -d / 2, 0), max=(w / 2, d / 2, 5), size=(w, d, 5)),
    )


def test_one_part_alone_keeps_its_own_plates() -> None:
    assert shelf_pack([PackItem(part=_part("a", 10, 10, plates=3))], PLATE) == Layout(own="a")


def test_parts_are_packed_in_rows_without_overlap() -> None:
    layout = shelf_pack([PackItem(part=_part("a", 100, 40), count=3)], PLATE)
    assert len(layout.plates) == 1
    xs = [(p.x, p.y) for p in layout.plates[0].items]
    assert xs == [(0.0, 0.0), (100 + GAP_MM, 0.0), (0.0, 40 + GAP_MM)]


def test_a_full_plate_starts_another() -> None:
    layout = shelf_pack([PackItem(part=_part("a", 200, 200), count=3)], PLATE)
    assert [len(p.items) for p in layout.plates] == [1, 1, 1]


def test_a_part_larger_than_the_plate_is_refused() -> None:
    with pytest.raises(PackError, match="larger than the plate"):
        shelf_pack([PackItem(part=_part("a", 300, 10)), PackItem(part=_part("b", 1, 1))], PLATE)


def test_a_multi_plate_part_cannot_share() -> None:
    with pytest.raises(PackError, match="its own 2 plates"):
        shelf_pack([PackItem(part=_part("a", 10, 10, plates=2)), PackItem(part=_part("b", 1, 1))], PLATE)


def test_an_explicit_plate_places_where_told() -> None:
    plate = explicit_plate([_part("a", 10, 10)], [(20.0, 30.0, 0.0)])
    assert plate.items[0].x == 20.0 and plate.items[0].y == 30.0
    with pytest.raises(PackError, match="rotation"):
        explicit_plate([_part("a", 10, 10)], [(0.0, 0.0, 90.0)])


def _deps(tmp_path: Path) -> tuple[WorkerDeps, DataPaths]:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("cube();\n", encoding="utf-8")
    paths.model_meta("demo").write_text('{"name": "Demo"}', encoding="utf-8")
    (paths.model_dir("demo") / "parts").mkdir()
    (paths.model_dir("demo") / "parts" / "roof.scad").write_text("cube();\n", encoding="utf-8")
    deps = WorkerDeps(
        config=install_fake_openscad(tmp_path, paths), paths=paths, assets=AssetStore(paths.assets),
        blobs=LocalBlobStore(paths.blobs), refs=None, projection=None,  # type: ignore[arg-type]
        revision="rev-1", openscad_version="OpenSCAD version 2026.09.28",
    )
    return deps, paths


async def _render(deps: WorkerDeps, file: str, params: dict[str, int]) -> Part:
    acts, env = RenderActivities(deps), ActivityEnvironment()
    req = PieceRequest(slug="demo", revision=None, file=file, params=params,
                       piece_key=piece_key("demo", None, file, params))
    prepared = await env.run(acts.prepare, req)
    main = await env.run(acts.render_main, req, prepared)
    await env.run(acts.render_solids, req, prepared, main)
    return Part.of(req, await env.run(acts.finish_piece, req, prepared, main))


def _record(parts: list[str]) -> OutputRecord:
    return OutputRecord(revision=None, ui_api=None, pipeline_api=1, pipeline_version="default",
                        inputs_v=0, plate_key="default", parts=parts)


async def test_a_piece_renders_another_file_of_the_template(tmp_path: Path) -> None:
    deps, _ = _deps(tmp_path)
    roof = await _render(deps, "parts/roof.scad", {"width": 3})
    assert roof.file == "parts/roof.scad"
    req = PieceRequest(slug="demo", revision=None, file="parts/roof.scad", params={"width": 3}, piece_key="k")
    prepared = await ActivityEnvironment().run(RenderActivities(deps).prepare, req)
    assert prepared.scad.endswith("parts/roof.scad")
    assert _scope(req, prepared).title == "Demo"  # the template's folder, not parts/


@pytest.mark.parametrize(("file", "params", "message"), [
    ("../escape.scad", {}, "not a file of the template"),
    ("model.scad", {"nope": 1}, "unknown parameters: nope"),
])
async def test_a_bad_file_or_parameter_fails_the_piece(
    tmp_path: Path, file: str, params: dict[str, int], message: str
) -> None:
    deps, _ = _deps(tmp_path)
    req = PieceRequest(slug="demo", revision=None, file=file, params=params, piece_key="k")
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(RenderActivities(deps).prepare, req)
    assert raised.value.type == "ParameterError" and raised.value.non_retryable
    assert message in raised.value.message


async def test_the_default_output_is_the_piece_as_rendered(tmp_path: Path) -> None:
    deps, _ = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    req = OutputRequest(job_id="j1", index=0, slug="demo", layout=Layout(own=part.piece_key),
                        parts=[part], name=None, bom=[], files={}, record=_record([part.piece_key]))
    out = await ActivityEnvironment().run(PipelineActivities(deps).write_output, req)
    assert out.blob_keys == [part.piece_key] and out.files_key is None
    assert out.result.model_3mf.startswith(f"blobs/{part.piece_key}/")
    assert out.record.image_revision == "rev-1"
    assert out.record.openscad_version == "OpenSCAD version 2026.09.28"


async def test_a_packed_output_writes_every_plate_bom_and_files(tmp_path: Path) -> None:
    deps, paths = _deps(tmp_path)
    a = await _render(deps, "model.scad", {"width": 12})
    b = await _render(deps, "parts/roof.scad", {"width": 4})
    acts, env = PipelineActivities(deps), ActivityEnvironment()
    layout = await env.run(acts.pack, PackRequest(items=[PackItem(part=a, count=2), PackItem(part=b)], plate=PLATE))
    out_dir = deps.blobs.dir_for("act-x")
    (out_dir / "guide.svg").write_text("<svg/>", encoding="utf-8")
    req = OutputRequest(
        job_id="j1", index=1, slug="demo", layout=layout, parts=[a, b], name="two",
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


async def _write_file_named(tmp_path: Path, name: str) -> ApplicationError:
    deps, _ = _deps(tmp_path)
    part = await _render(deps, "model.scad", {"width": 12})
    req = OutputRequest(job_id="j1", index=0, slug="demo", layout=Layout(own=part.piece_key),
                        parts=[part], name=None, bom=[], files={name: "x"}, record=_record([]))
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_packing_and_outputs.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'scadbuddy.template'`.

- [ ] **Step 4: Job models: BOM, record, pipeline output**

In `render/job_models.py`, after `JobResult`:

```python
class BomEntry(BaseModel):
    """One line of an output's bill of materials (spec §5.2): structured, shown as a table."""

    piece: str
    label: str
    count: int = Field(ge=1)
    #: 1-based plates the piece is on, when the pipeline says.
    plates: list[int] = Field(default_factory=list)
    #: The piece's `piece_key` (a PartRef).
    part: str | None = None


class OutputRecord(BaseModel):
    """What reproduces an output (§8.4)."""

    revision: str | None
    ui_api: int | None
    pipeline_api: int
    pipeline_version: str
    inputs_v: int
    #: `SCADBUDDY_REVISION` of the worker image that wrote it.
    image_revision: str = ""
    openscad_version: str = ""
    plate_key: str
    #: The store refs (piece keys) of every Part it was built from.
    parts: list[str]


#: An extra output file's name: plain, no path, no leading dot.
FILE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"


class PipelineOutput(BaseModel):
    """One `ctx.output` (§5.2): Generate saves each as an output."""

    name: str | None
    result: JobResult
    bom: list[BomEntry] = Field(default_factory=list)
    #: Extra files, by name, under ``files/`` in the blob ``files_key``.
    files: list[str] = Field(default_factory=list)
    #: The blob holding ``files/`` (always `output_key(job_id, index)`); None without files.
    files_key: str | None = None
    #: The blobs this output reads; the job holds a ref on each.
    blob_keys: list[str]
    record: OutputRecord
```

In `Job`, after `steps`: `outputs: list[PipelineOutput] = Field(default_factory=list)`.

- [ ] **Step 5: `scadbuddy/template.py`**

```python
"""What a template's `pipeline/activities.py` may import (spec 2026-09-27 §5.2): the
references a pipeline passes around, and `emit` for files. The workflow payloads are
these same classes. Heavy imports stay inside the methods: the workflow sandbox passes
this module through and must not load trimesh for a reference."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from scadbuddy.render.glb import BoundingBox

if TYPE_CHECKING:
    import trimesh

    from scadbuddy.workflows.models import PieceRequest, PieceResult


class Blob(BaseModel):
    """A file in the store: ``path`` inside the blob ``key``."""

    kind: Literal["blob"] = "blob"
    key: str
    path: str
    #: Where the file is on this machine, filled in for a template activity only.
    local: str | None = Field(default=None, exclude=True)

    def read_bytes(self) -> bytes:
        if self.local is None:
            raise RuntimeError("a Blob is readable inside a template activity only")
        return Path(self.local).read_bytes()


class Part(BaseModel):
    """What `ctx.render` returns (§5.2): a reference to a rendered piece, never meshes."""

    kind: Literal["part"] = "part"
    piece_key: str
    file: str
    bbox: BoundingBox
    colours: list[str]
    notes: list[str] = Field(default_factory=list)
    #: Plates the piece laid out for itself (`echo(plates = N)`, base spec §6.4).
    plates: int = 1
    local: str | None = Field(default=None, exclude=True)

    @classmethod
    def of(cls, req: PieceRequest, piece: PieceResult) -> Part:
        result = piece.result
        return cls(
            piece_key=req.piece_key, file=req.file, bbox=result.bbox_mm, colours=list(result.colors),
            notes=list(result.notes), plates=max(1, len(result.plates)),
        )

    def meshes(self) -> dict[str, trimesh.Trimesh]:
        """The first plate's closed part per colour, read from the piece's layout."""
        from scadbuddy.render.jobs import LAYOUT_NAME, PlateLayout

        if self.local is None:
            raise RuntimeError("a Part's meshes are readable inside a template activity only")
        layout = PlateLayout.load(Path(self.local) / LAYOUT_NAME)
        return {part.colour: part.mesh for part in layout.plates[0].parts}


def emit(name: str, data: bytes | str) -> Blob:
    """Write a file a template activity returns; ``name`` is a plain file name."""
    out = Path(os.environ["SCADBUDDY_TEMPLATE_OUT"])
    if "/" in name or name.startswith(".") or not name:
        raise ValueError(f"not a plain file name: {name!r}")
    target = out / name
    if isinstance(data, str):
        target.write_text(data, encoding="utf-8")
    else:
        target.write_bytes(data)
    return Blob(key=os.environ["SCADBUDDY_TEMPLATE_OUT_KEY"], path=name)
```

- [ ] **Step 6: Workflow payloads**

In `workflows/models.py`:

```python
from scadbuddy.render.job_models import BomEntry, OutputRecord
from scadbuddy.template import Blob, Part


class PackItem(BaseModel):
    part: Part
    count: int = Field(default=1, ge=1)


class Placed(BaseModel):
    """Where one copy of a piece goes: its box's min corner, relative to the plate's
    content (the writer then centres the plate as it does today)."""

    piece_key: str
    x: float
    y: float


class LayoutPlate(BaseModel):
    items: list[Placed]


class Layout(BaseModel):
    """What `pack`/`plate_of` yield (§5.2). ``own``: one part alone, on the plates it
    laid out itself, written exactly as it rendered (§5.3)."""

    plates: list[LayoutPlate] = Field(default_factory=list)
    own: str | None = None


class PackRequest(BaseModel):
    items: list[PackItem]
    plate: PlateSize
    goal: str = "fewest_plates"


class OutputRequest(BaseModel):
    job_id: str
    index: int
    slug: str
    layout: Layout
    parts: list[Part]
    name: str | None
    bom: list[BomEntry]
    files: dict[str, str | Blob]
    plate_model: str | None = None
    record: OutputRecord


class OutputRef(BaseModel):
    index: int
    name: str | None
```

Add `"Blob"` and `"Part"` to the module's `__all__` if it has one.

- [ ] **Step 7: `workflows/packing.py`**

```python
"""Plates from parts, by footprint (spec 2026-09-27 §5.2 `pack`, goal
``fewest_plates``). Pure: runs in an activity and in `plate_of` in the workflow.
Phase 5's Arrange replaces `shelf_pack`."""

from __future__ import annotations

from collections.abc import Sequence

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from scadbuddy.template import Part
    from scadbuddy.workflows.models import Layout, LayoutPlate, PackItem, Placed, PlateSize

#: Space between neighbouring parts, mm.
GAP_MM = 5.0


class PackError(ValueError):
    pass


def shelf_pack(items: Sequence[PackItem], plate: PlateSize) -> Layout:
    if len(items) == 1 and items[0].count == 1:
        return Layout(own=items[0].part.piece_key)
    for item in items:
        if item.part.plates > 1:
            raise PackError(
                f"{item.part.file} lays out its own {item.part.plates} plates; pack it on its own"
            )
    copies = sorted(
        (item.part for item in items for _ in range(item.count)),
        key=lambda p: (-p.bbox.size[1], -p.bbox.size[0], p.piece_key),
    )
    plates: list[LayoutPlate] = []
    current: list[Placed] = []
    x = y = row = 0.0
    for part in copies:
        w, d = part.bbox.size[0], part.bbox.size[1]
        if w > plate.width or d > plate.depth:
            raise PackError(
                f"{part.file} ({w:.0f} x {d:.0f} mm) is larger than the plate "
                f"({plate.width:.0f} x {plate.depth:.0f} mm)"
            )
        if x + w > plate.width:
            x, y, row = 0.0, y + row + GAP_MM, 0.0
        if y + d > plate.depth:
            plates.append(LayoutPlate(items=current))
            current, x, y, row = [], 0.0, 0.0, 0.0
        current.append(Placed(piece_key=part.piece_key, x=x, y=y))
        x += w + GAP_MM
        row = max(row, d)
    if current:
        plates.append(LayoutPlate(items=current))
    return Layout(plates=plates)


def explicit_plate(parts: Sequence[Part], at: Sequence[tuple[float, float, float]]) -> LayoutPlate:
    """`plate_of(items, at=…)`: the pipeline's own placement. Rotation arrives with
    Arrange (phase 5)."""
    if len(parts) != len(at):
        raise PackError(f"{len(parts)} parts but {len(at)} positions")
    if any(rot for _, _, rot in at):
        raise PackError("rotation in plate_of arrives with Arrange (phase 5); pass 0")
    return LayoutPlate(items=[Placed(piece_key=p.piece_key, x=x, y=y) for p, (x, y, _) in zip(parts, at, strict=True)])
```

- [ ] **Step 8: `workflows/outputs.py`**

```python
"""One pipeline output's blob (spec 2026-09-27 §5.2 `output`): the multi-plate 3MF
from the pieces' own solids, placed as the layout says, plus the extra files."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

import trimesh
from temporalio.exceptions import ApplicationError

from scadbuddy.render.bambu3mf import PlateParts, write_plates_3mf
from scadbuddy.render.glb import bounding_box, write_glb
from scadbuddy.render.job_models import FILE_NAME_PATTERN, JobResult, PipelineOutput
from scadbuddy.render.jobs import (
    LAYOUT_NAME,
    MODEL_NAME,
    PREVIEW_NAME,
    PlateLayout,
    plates_thumbnails,
    result_parts,
    result_plates,
)
from scadbuddy.render.plate import plate_for
from scadbuddy.render.split import ColourPart
from scadbuddy.store.content import BlobScope, template_title
from scadbuddy.template import Blob
from scadbuddy.workflows.activities import PIECE_NAME, WorkerDeps, _read_piece
from scadbuddy.workflows.models import OutputRequest

_RESERVED = {MODEL_NAME, PREVIEW_NAME, LAYOUT_NAME, PIECE_NAME}


def output_key(job_id: str, index: int) -> str:
    return f"output-{job_id}-{index}"


def _refuse(message: str) -> ApplicationError:
    return ApplicationError(message, type="OutputError", non_retryable=True)


async def build_output(req: OutputRequest, deps: WorkerDeps, *, model_dir: Path) -> PipelineOutput:
    """``model_dir`` is the template's directory at the job's revision: it names the
    store folder, as `_scope` does for a piece."""
    blobs = deps.blobs
    record = req.record.model_copy(
        update={"image_revision": deps.revision, "openscad_version": deps.openscad_version}
    )
    for name in req.files:
        if not re.match(FILE_NAME_PATTERN, name):
            raise _refuse(f"output file name {name!r}: use letters, digits, '.', '_' or '-'")
        if name in _RESERVED:
            raise _refuse(f"output file name {name!r} is reserved for the output's own files")
    key = output_key(req.job_id, req.index)
    if req.layout.own is not None:
        await blobs.fetch(req.layout.own)
        piece = await asyncio.to_thread(_read_piece, blobs.dir_for(req.layout.own) / PIECE_NAME)
        if piece is None:
            raise _refuse(f"piece {req.layout.own} has no finished render")
        result, keys = piece.result, [req.layout.own]
    else:
        result, keys = await _write_plates(req, deps, key), [key]
    if req.files:
        files_dir = blobs.dir_for(key) / "files"
        files_dir.mkdir(exist_ok=True)
        for name, value in req.files.items():
            if isinstance(value, Blob):
                await blobs.fetch(value.key)
                source = (blobs.dir_for(value.key) / value.path).resolve()
                if not source.is_relative_to(blobs.dir_for(value.key).resolve()) or not source.is_file():
                    raise _refuse(f"{name}: {value.path} is not a file of blob {value.key}")
                await asyncio.to_thread(shutil.copyfile, source, files_dir / name)
            else:
                await asyncio.to_thread((files_dir / name).write_text, value, "utf-8")
        if key not in keys:
            keys.append(key)
    if key in keys:
        # Internal, like a piece: the default `folder="work"` (phase 3 sweeps it). The
        # file a person sees is Generate's save (Task 7), not this blob.
        await blobs.publish(key, scope=BlobScope(slug=req.slug, title=template_title(model_dir, req.slug)))
    return PipelineOutput(
        name=req.name, result=result, bom=req.bom, files=sorted(req.files),
        files_key=key if req.files else None, blob_keys=keys, record=record,
    )


async def _write_plates(req: OutputRequest, deps: WorkerDeps, key: str) -> JobResult:
    blobs = deps.blobs
    parts = {p.piece_key: p for p in req.parts}
    layouts: dict[str, PlateLayout] = {}
    colours: list[str] = []
    plates: list[PlateParts] = []
    for plate in req.layout.plates:
        by_colour: dict[str, list[trimesh.Trimesh]] = {}
        names: dict[str, str] = {}
        for placed in plate.items:
            if placed.piece_key not in layouts:
                await blobs.fetch(placed.piece_key)
                layouts[placed.piece_key] = await asyncio.to_thread(
                    PlateLayout.load, blobs.dir_for(placed.piece_key) / LAYOUT_NAME
                )
            box = parts[placed.piece_key].bbox
            offset = (placed.x - box.min[0], placed.y - box.min[1], -box.min[2])
            for part in layouts[placed.piece_key].plates[0].parts:
                mesh = part.mesh.copy()
                mesh.apply_translation(offset)
                by_colour.setdefault(part.colour, []).append(mesh)
                names.setdefault(part.colour, part.name)
                if part.colour not in colours:
                    colours.append(part.colour)
        ordered = sorted(by_colour, key=colours.index)
        plates.append(PlateParts(
            tuple(ColourPart(colours.index(c), names[c], c, trimesh.util.concatenate(by_colour[c])) for c in ordered),
            tuple(colours.index(c) + 1 for c in ordered),
        ))
    everything = [part for plate in plates for part in plate.parts]
    layout = PlateLayout(plates, colours, [], bounding_box(everything), [])
    thumbnails, warnings = await plates_thumbnails(
        [plate.parts for plate in plates], config=deps.config, executor=deps.thumbnail_executor
    )
    work = blobs.dir_for(key)
    await asyncio.to_thread(
        write_plates_3mf, plates, colours, work / MODEL_NAME, thumbnails=thumbnails,
        model_name=req.slug, plate=plate_for(req.plate_model),
    )
    await asyncio.to_thread(write_glb, list(plates[0].parts), work / PREVIEW_NAME)
    root = deps.paths.root
    return JobResult(
        model_3mf=str((work / MODEL_NAME).relative_to(root)),
        preview_glb=str((work / PREVIEW_NAME).relative_to(root)),
        source_version=req.record.revision or "",
        parts=result_parts(layout), bbox_mm=layout.bbox, colors=colours, warnings=warnings,
        plates=result_plates(layout), notes=[note for p in req.parts for note in p.notes],
    )
```

`model_name=req.slug` must match `finish_piece_stage`'s `slug.removeprefix(BUILTIN_PREFIX)`. Import `BUILTIN_PREFIX` from the module `render/jobs.py` imports it from, and use the same expression.

- [ ] **Step 9: `prepare` renders `req.file` and checks its parameters**

In `render/runner.py`, beside `build_defines`, add the message half of `api/params.py`'s `require_valid_params`. `api/params.py` imports it; the worker must not import the API package:

```python
def params_problem(schema: CustomizerSchema, params: Mapping[str, ParamValue]) -> str | None:
    """What is wrong with ``params`` for ``schema``, or None (#432)."""
    unknown = sorted(set(params) - {p.name for p in schema.parameters})
    if unknown:
        return f"unknown parameters: {', '.join(unknown)}"
    try:
        build_defines(schema, params)
    except (UnknownParameterError, ParameterValueError) as error:
        return str(error)
    return None
```

Keep `require_valid_params`'s existing 422 bodies, including `parameters=unknown` for unknown names. It calls `params_problem` only for the `build_defines` half. Run `cd backend && uv run --frozen pytest tests/api -q -k param`; it must stay green. If `build_defines` raises a `ParameterValueError` that `require_valid_params` handles elsewhere, catch it in `params_problem` as shown.

In `workflows/activities.py`, extend `WorkerDeps`:

```python
    #: `SCADBUDDY_REVISION` and `openscad --version` of this worker, for the record (§8.4).
    revision: str = ""
    openscad_version: str = ""
    #: The interpreter template activities run under: this worker's own (§5.2).
    template_python: str = sys.executable
```

In `RenderActivities.prepare`, after `prepared, _ = await prepare_source(...)`:

```python
        scad = prepared.scad
        if req.file != "model.scad":
            root = prepared.scad.parent.resolve()
            scad = (root / req.file).resolve()
            if not scad.is_relative_to(root) or scad.suffix != ".scad" or not scad.is_file():
                raise _parameter_error(f"{req.file} is not a file of the template")
            prepared = replace(
                prepared,
                scad=scad,
                schema_cache=prepared.schema_cache.with_name(
                    f"{prepared.schema_cache.stem}.{hashlib.sha256(req.file.encode()).hexdigest()[:12]}"
                    f"{prepared.schema_cache.suffix}"
                ),
            )
        config = self._config_of(prepared)
        async with library_lease(d.checkouts, f"piece:{req.piece_key}", prepared.library_path):
            schema = await cached_schema(prepared.scad, prepared.schema_cache, config=config)
        problem = params_problem(schema, req.params)
        if problem is not None:
            raise _parameter_error(problem)
```

Imports: `hashlib`, `dataclasses.replace`, `cached_schema` (from where `render/jobs.py` takes it) and `params_problem` (`render/runner.py`). `_config_of(prepared: Prepared) -> Config` is `replace(self.deps.config, library_path=prepared.library_path)`. `_parameter_error(message)` is `ApplicationError(message, Failure(error=message), type="ParameterError", non_retryable=True)`. `Prepared` is a frozen dataclass, so `dataclasses.replace` applies. Also:
- widen phase 1's `_failure_of` in `pipelines.py` to read the `Failure` detail of every type that carries one: `FAILURE_TYPES = ("OpenSCADError", "ParameterError", "TemplateActivityError")` (a module constant; `cause.type in FAILURE_TYPES` replaces `cause.type == "OpenSCADError"`), so a template activity's log tail (Task 5) reaches the job too;
- make its fallback use `cause.message` for any `ApplicationError` in the chain:

```python
    cause = error
    while cause is not None:
        if isinstance(cause, ApplicationError):
            return Failure(error=cause.message)
        cause = cause.__cause__
    return Failure(error=f"{type(error).__name__}: {error}")
```

(This goes after the existing detail loop.)

Phase 3's `_scope(req, prepared)` names the store folder from `Path(prepared.scad).parent`, which is now `parts/` for `parts/roof.scad`. Point it at the template root with phase 3's `model_dir(scad, file)` (`store/fonts.py`, the helper `prepare` already uses to find the template's font names):

```python
def _scope(req: PieceRequest, prepared: PrepareResult) -> BlobScope:
    """Where the piece's blob goes: its template's folder, named by `model.json`."""
    root = model_dir(Path(prepared.scad), req.file)
    return BlobScope(slug=req.slug, title=template_title(root, req.slug))
```

(`model_dir` is already imported into `workflows/activities.py` by phase 3; add it to the `scadbuddy.store.fonts` import if not.)

`test_a_piece_renders_another_file_of_the_template` pins it (`_scope(...).title == "Demo"`, the `name` in the test template's `model.json`).

- [ ] **Step 10: The `pack` and `write_output` activities**

`pipeline_activities.py` now also imports `_heartbeating` from `scadbuddy.workflows.activities`, and `build_output` from `scadbuddy.workflows.outputs`.

At module level in `pipeline_activities.py` (the test fakes call it too):

```python
def pack_layout(req: PackRequest) -> Layout:
    if req.goal != "fewest_plates":
        raise ApplicationError(
            f"pack goal {req.goal!r} arrives with Arrange (phase 5); use 'fewest_plates'",
            type="PackError", non_retryable=True,
        )
    try:
        return shelf_pack(req.items, req.plate)
    except PackError as error:
        raise ApplicationError(str(error), type="PackError", non_retryable=True) from None
```

In `PipelineActivities`:

```python
    def all(self) -> Sequence[Callable[..., Any]]:
        return [self.load_pipeline, self.pack, self.write_output]

    @activity.defn(name="pack")
    async def pack(self, req: PackRequest) -> Layout:
        return pack_layout(req)

    @activity.defn(name="write_output")
    async def write_output(self, req: OutputRequest) -> PipelineOutput:
        """Meshes, thumbnails and a many-plate 3MF can outlast a short timeout: heartbeat,
        as the openscad stages do (the workflow gives it `_openscad_timeout()`)."""
        model_dir = await self.model_dir(req.slug, req.record.revision)
        return await _heartbeating(asyncio.create_task(build_output(req, self.deps, model_dir=model_dir)))
```

- [ ] **Step 11: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/test_packing_and_outputs.py tests/test_activities.py tests/test_workflows.py -q`
Expected: all pass.

- [ ] **Step 12: Gates and commit**

Run the backend gates.

```bash
git add backend/scadbuddy/template.py backend/scadbuddy/workflows/ backend/scadbuddy/render/job_models.py \
  backend/scadbuddy/api/params.py backend/scadbuddy/render/runner.py backend/tests/support/openscad.py backend/tests/test_activities.py \
  backend/tests/test_packing_and_outputs.py
git commit -m "feat(workflows): parts, pack and write_output; a piece renders any file and checks its params (#427)"
```

---

### Task 4: `ctx` and `TemplatePipeline` running the pipeline

**Files:**
- Create: `backend/scadbuddy/workflows/ctx.py`, `backend/tests/support/pipelines.py`, `backend/tests/test_template_pipeline.py`
- Create: `backend/scadbuddy/migrations/<ts>_render_jobs_outputs.sql` (`date -u +%Y%m%dT%H%MZ` when you create it)
- Modify: `backend/scadbuddy/workflows/models.py` (`PieceOutcome.piece_key`, `Projection.outputs`, `Projection.blob_keys`)
- Modify: `backend/scadbuddy/workflows/pipelines.py` (`TemplatePipeline`, `RenderPiece._tell_waiting`)
- Modify: `backend/scadbuddy/workflows/activities.py` (`project`)
- Modify: `backend/scadbuddy/render/projection.py` (`PROJECTION_COLUMNS`, `finish`)
- Modify: `backend/scadbuddy/workflows/client.py` (`render_worker` registers `PipelineActivities`)
- Modify: `backend/scadbuddy/worker.py` (`_poll` fills `revision`, `openscad_version`), `backend/scadbuddy/render/runner.py` (`probe_openscad_version` moved in), `backend/scadbuddy/api/deps.py` and `backend/scadbuddy/main.py` (import it from there)

**Interfaces:**
- Consumes: `load_pipeline_module`, `pipeline_error`, `PipelineContractError` (Task 1); `LoadedPipeline`, `LoadRequest`, `PipelineActivities` (Task 2); `Part`, `Blob`, `PackItem`, `PackRequest`, `Layout`, `LayoutPlate`, `OutputRequest`, `OutputRef`, `BomEntry`, `OutputRecord`, `PipelineOutput`, `explicit_plate`, `PackError` (Task 3).
- Produces:
  ```python
  # workflows/ctx.py
  class PieceFailedError(Exception): file: str; failure: Failure
  class Ctx:
      inputs_version: int; plate: PlateSize
      async def render(self, file: str, /, **params: ParamValue) -> Part
      async def pack(self, items: Sequence[Part | tuple[Part, int]], *, goal: str = "fewest_plates", filament_plan: object | None = None) -> Layout
      def plate_of(self, items: Sequence[Part], *, at: Sequence[tuple[float, float, float]] | None = None) -> LayoutPlate
      async def output(self, *, plates: Layout | Sequence[LayoutPlate], name: str | None = None, bom: Sequence[BomEntry | Mapping[str, Any]] | None = None, files: Mapping[str, str | bytes | Blob | Mapping[str, Any]] | None = None) -> OutputRef
      def progress(self, message: str, *, done: int | None = None, total: int | None = None) -> None
      async def activity(self, name: str, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any   # Task 5 fills it in
      # host-side, read by TemplatePipeline:
      outputs: list[PipelineOutput]; blob_keys: list[str]; log_tail: list[str]; steps: list[StepInfo]
  PieceOutcome.piece_key: str = ""
  Projection.outputs: list[PipelineOutput] = []; Projection.blob_keys: list[str] = []
  TemplatePipeline.piece(self, req: PieceRequest) -> Awaitable[PieceOutcome]   # the start-then-signal share, per piece
  # tests/support/pipelines.py
  class FakeWorld: source: str | None; pieces: list[PieceRequest]; projections: list[Projection]; outputs: list[OutputRequest]; calls: list[TemplateCall]
      def activities(self) -> list[Callable[..., Any]]
  async def run_job(world: FakeWorld, job: Job, *, client: Client) -> None
  def a_job(**inputs: Any) -> Job
  def activity_named(name: str, fn: Callable[..., Any]) -> Callable[..., Any]
  ```

- [ ] **Step 1: Test support: a world of fake activities**

`backend/tests/support/pipelines.py`:

```python
"""Fake activities, registered by name, for `TemplatePipeline` tests: a piece is "rendered"
by `cached_piece` from its params (width `w`, depth `d`), `pack` is the real one."""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker

from scadbuddy.library.pipelines import DEFAULT_PIPELINE_FILE, DEFAULT_PIPELINE_SOURCE, pipeline_version_of
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import Job, JobResult, PipelineOutput
from scadbuddy.render.projection import workflow_id_for
from scadbuddy.workflows.models import (
    Layout,
    LoadedPipeline,
    LoadRequest,
    OutputRequest,
    PackRequest,
    PieceRequest,
    PieceResult,
    PlateSize,
    Projection,
    TemplateCall,
)
from scadbuddy.workflows.pipeline_activities import pack_layout
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline


def a_job(**inputs: Any) -> Job:
    params = inputs.get("params", {})
    return Job(id=uuid.uuid4().hex, slug="demo", params=params, inputs={"v": 0, **inputs, "params": params},
               created_at=datetime.now(UTC))


class FakeWorld:
    def __init__(self, source: str | None = None, *, fail: str | None = None,
                 activities_py: Path | None = None) -> None:
        self.source = source
        self.fail = fail  # a file whose render fails
        self.activities_py = activities_py
        self.pieces: list[PieceRequest] = []
        self.projections: list[Projection] = []
        self.outputs: list[OutputRequest] = []
        self.calls: list[TemplateCall] = []

    def activities(self) -> list[Callable[..., Any]]:
        return [self.load_pipeline, self.cached_piece, self.pack, self.write_output, self.project,
                self.run_template_activity]

    @activity.defn(name="load_pipeline")
    async def load_pipeline(self, req: LoadRequest) -> LoadedPipeline:
        source = self.source or DEFAULT_PIPELINE_SOURCE
        return LoadedPipeline(
            source=source, file="pipeline/pipeline.py" if self.source else DEFAULT_PIPELINE_FILE,
            api=1, version=pipeline_version_of(source) if self.source else "default",
            inputs_version=1 if self.source else 0,
            plate=PlateSize(key="default", width=256, depth=256),
        )

    @activity.defn(name="cached_piece")
    async def cached_piece(self, req: PieceRequest) -> PieceResult | None:
        self.pieces.append(req)
        if req.file == self.fail:
            from temporalio.exceptions import ApplicationError

            from scadbuddy.workflows.models import Failure
            raise ApplicationError("openscad exited with 1", Failure(error="openscad exited with 1",
                                   log_tail=["ERROR: boom"]), type="OpenSCADError", non_retryable=True)
        w, d = float(req.params.get("w", 10)), float(req.params.get("d", 10))
        return PieceResult(result=JobResult(
            model_3mf=f"blobs/{req.piece_key}/model.3mf", preview_glb=f"blobs/{req.piece_key}/preview.glb",
            parts=[], colors=["#FF0000"], bbox_mm=BoundingBox(min=(0, 0, 0), max=(w, d, 5), size=(w, d, 5)),
        ), log_tail=[f"rendered {req.file}"])

    @activity.defn(name="pack")
    async def pack(self, req: PackRequest) -> Layout:
        return pack_layout(req)

    @activity.defn(name="write_output")
    async def write_output(self, req: OutputRequest) -> PipelineOutput:
        self.outputs.append(req)
        first = req.layout.own or req.layout.plates[0].items[0].piece_key
        return PipelineOutput(
            name=req.name, bom=req.bom, files=sorted(req.files), record=req.record,
            blob_keys=[first] if req.layout.own else [f"output-{req.job_id}-{req.index}"],
            result=JobResult(model_3mf=f"blobs/{first}/model.3mf", preview_glb=f"blobs/{first}/preview.glb",
                             parts=[], bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1))),
        )

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> None:
        self.projections.append(projection)

    @activity.defn(name="run_template_activity")
    async def run_template_activity(self, call: TemplateCall) -> Any:
        """In-process stand-in for the subprocess runner (Task 5 tests the real one)."""
        self.calls.append(call)
        assert self.activities_py is not None
        spec = importlib.util.spec_from_file_location("template_activities", self.activities_py)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, call.name)(*call.args, **call.kwargs)

    def final(self) -> Projection:
        return self.projections[-1]


def activity_named(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    return activity.defn(name=name)(fn)


async def run_job(world: FakeWorld, job: Job, *, client: Client) -> None:
    queue = f"t-{uuid.uuid4().hex[:8]}"
    async with Worker(client, task_queue=queue, workflows=[TemplatePipeline, RenderPiece],
                      activities=world.activities()):
        await client.execute_workflow(TemplatePipeline.run, job, id=workflow_id_for(job.id), task_queue=queue)
```

`TemplateCall` is defined in Task 5. Until then, keep `run_template_activity` out of `activities()`, and drop its import and the method. Task 5 Step 1 adds them back.

- [ ] **Step 2: Write the failing tests**

`backend/tests/test_template_pipeline.py`:

```python
"""TemplatePipeline over a pipeline's source (spec 2026-09-27 §3.4, §5.2, §5.3)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from temporalio.worker import Worker

from scadbuddy.render.projection import workflow_id_for
from scadbuddy.workflows.models import PieceRequest, PieceResult, piece_key
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline
from tests.support.pipelines import FakeWorld, a_job, activity_named, run_job
from tests.support.temporal import temporal_client

pytestmark = pytest.mark.requires_temporal

THREE = """\
INPUTS_VERSION = 1
import asyncio

async def run(ctx, inputs):
    a, b, c = await asyncio.gather(
        ctx.render("model.scad", piece="wall", w=100, d=10),
        ctx.render("model.scad", piece="wall", w=100, d=10),
        ctx.render("parts/roof.scad", w=inputs["span"], d=50),
    )
    ctx.progress("Rendered 3 of 3", done=3, total=3)
    await ctx.output(plates=await ctx.pack([(a, 4), c]), name="house",
                     bom=[{"piece": "wall", "label": "Wall", "count": 4, "part": a.piece_key}],
                     files={"notes.txt": "hello"})
"""


async def test_the_default_pipeline_renders_one_piece_and_writes_it_as_rendered() -> None:
    world, job = FakeWorld(), a_job(params={"w": 12})
    async with temporal_client() as client:
        await run_job(world, job, client=client)
    assert [p.params for p in world.pieces] == [{"w": 12}]
    assert world.outputs[0].layout.own == world.pieces[0].piece_key
    final = world.final()
    assert final.state == "done"
    assert final.pipeline_version == "default"
    assert final.result is not None and final.outputs[0].result == final.result
    assert set(final.blob_keys) == {world.pieces[0].piece_key}
    assert final.log_tail == ["rendered model.scad"]


async def test_a_parameter_named_file_reaches_the_render() -> None:
    world, job = FakeWorld(), a_job(params={"file": "x", "name": "y", "w": 3})
    async with temporal_client() as client:
        await run_job(world, job, client=client)
    assert world.pieces[0].file == "model.scad"
    assert world.pieces[0].params == {"file": "x", "name": "y", "w": 3}
    assert world.final().state == "done"


async def test_identical_renders_share_one_piece_and_files_reach_the_output() -> None:
    world, job = FakeWorld(THREE), a_job(span=120)
    async with temporal_client() as client:
        await run_job(world, job, client=client)
    keys = {p.piece_key for p in world.pieces}
    assert len(keys) == 2 and len(world.pieces) == 2  # the identical walls rendered once
    assert piece_key("demo", None, "parts/roof.scad", {"w": 120, "d": 50}) in keys
    out = world.outputs[0]
    assert out.name == "house" and out.files == {"notes.txt": "hello"}
    assert sum(len(p.items) for p in out.layout.plates) == 5
    assert out.record.pipeline_version == world.final().pipeline_version != "default"
    assert out.record.inputs_v == 0 and sorted(out.record.parts) == sorted(keys)
    assert world.final().state == "done"
    assert any(p.steps and p.steps[0].name == "Rendered 3 of 3" for p in world.projections if p.state is None)


async def test_a_failed_piece_fails_the_job_with_its_log() -> None:
    world, job = FakeWorld(THREE, fail="parts/roof.scad"), a_job(span=120)
    async with temporal_client() as client:
        await run_job(world, job, client=client)
    final = world.final()
    assert final.state == "failed"
    assert final.failure is not None
    assert final.failure.error == "parts/roof.scad: openscad exited with 1"
    assert final.failure.log_tail == ["ERROR: boom"]


@pytest.mark.parametrize(("source", "message"), [
    ("import datetime\n\nasync def run(ctx, inputs):\n    datetime.datetime.now()\n",
     "pipeline/pipeline.py:4: RestrictedWorkflowAccessError: "),
    ("async def run(ctx, inputs):\n    raise ValueError('no rooms')\n",
     "pipeline/pipeline.py:2: ValueError: no rooms"),
    ("async def run(ctx, inputs):\n    await ctx.pack([], goal='fewest_swaps')\n",
     "pipeline/pipeline.py:2: "),
])
async def test_a_restricted_call_fails_the_job_with_its_line(source: str, message: str) -> None:
    world = FakeWorld(source)
    async with temporal_client() as client:
        await asyncio.wait_for(run_job(world, a_job(), client=client), timeout=60)
    final = world.final()
    assert final.state == "failed"
    assert final.failure is not None and final.failure.error.startswith(message)


async def test_a_pipeline_that_writes_nothing_fails() -> None:
    world = FakeWorld("async def run(ctx, inputs):\n    return None\n")
    async with temporal_client() as client:
        await run_job(world, a_job(), client=client)
    assert world.final().failure is not None
    assert world.final().failure.error == "pipeline/pipeline.py: the pipeline wrote no output"


async def test_an_edit_mid_run_does_not_change_the_running_job() -> None:
    gate = asyncio.Event()
    slow = """\
async def run(ctx, inputs):
    a = await ctx.render("model.scad", w=1)
    b = await ctx.render("model.scad", w=2)
    await ctx.output(plates=await ctx.pack([a, b]), name="first source")
"""
    world, job = FakeWorld(slow), a_job()
    original = world.cached_piece
    started = asyncio.Event()

    # Annotated: temporalio converts the argument by its type hint (a bare `req` is a dict).
    async def held(req: PieceRequest) -> PieceResult | None:
        if req.params == {"w": 2} and not gate.is_set():
            started.set()  # before waiting: `original` records the piece only afterwards
            await gate.wait()
        return await original(req)

    world.cached_piece = activity_named("cached_piece", held)  # type: ignore[method-assign]
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(client, task_queue=queue, workflows=[TemplatePipeline, RenderPiece],
                          activities=world.activities(), max_cached_workflows=0):
            handle = await client.start_workflow(TemplatePipeline.run, job, id=workflow_id_for(job.id),
                                                 task_queue=queue)
            async with asyncio.timeout(30):
                await started.wait()
        # The worker is gone mid-house (its held attempt is cancelled at shutdown).
        # The template's pipeline changes on disk.
        world.source = "async def run(ctx, inputs):\n    raise RuntimeError('the new source ran')\n"
        gate.set()
        async with Worker(client, task_queue=queue, workflows=[TemplatePipeline, RenderPiece],
                          activities=world.activities()):
            await asyncio.wait_for(handle.result(), timeout=60)
    assert world.final().state == "done"
    assert world.outputs[-1].name == "first source"
    assert [p.params for p in world.pieces].count({"w": 1}) == 1
```

`activity_named` is in `tests/support/pipelines.py` (Step 1). Add `PieceRequest` and `PieceResult` to the test's `scadbuddy.workflows.models` import. The first worker's `w=2` attempt is cancelled at shutdown before it records anything, and the retry on the second worker records it once. The last assertion pins that the finished `w=1` piece is not rendered again.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_template_pipeline.py -q`
Expected: FAIL for every test but one. `test_the_default_pipeline…` fails on `world.outputs` being empty, because phase 1's `TemplatePipeline` never calls `load_pipeline` or `write_output`; the others fail the same way or time out at `wait_for`. `test_a_parameter_named_file_reaches_the_render` passes against phase 1 too: it is a regression pin for the new `ctx.render(file, /, **params)`, not a red test.

- [ ] **Step 4: Models: outcomes keyed by piece, outputs on the projection**

In `workflows/models.py`, add `piece_key: str = ""` to `PieceOutcome`. Add to `Projection`:

```python
    #: Every `ctx.output`, in order (§5.2); ``result`` is the first one's.
    outputs: list[PipelineOutput] = Field(default_factory=list)
    #: Every blob the job reads (pieces and outputs); `project` refs each on done.
    blob_keys: list[str] = Field(default_factory=list)
```

In `RenderPiece.run` (`pipelines.py`), build both outcomes with `piece_key=req.piece_key`.

- [ ] **Step 5: `workflows/ctx.py`**

```python
"""The one object a pipeline gets (spec 2026-09-27 §5.2). Everything it does leaves
the sandbox as an activity or a child workflow; nothing here does I/O."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from scadbuddy.render.job_models import BomEntry, Job, OutputRecord, PipelineOutput, StepInfo
    from scadbuddy.render.schema import ParamValue
    from scadbuddy.template import Blob, Part
    from scadbuddy.workflows.models import (
        Failure,
        Layout,
        LayoutPlate,
        LoadedPipeline,
        OutputRef,
        OutputRequest,
        PackItem,
        PackRequest,
        PieceRequest,
        PlateSize,
        piece_key,
    )
    from scadbuddy.workflows.packing import explicit_plate

if TYPE_CHECKING:
    from scadbuddy.workflows.pipelines import TemplatePipeline


class PieceFailedError(Exception):
    """A `ctx.render` whose piece failed: the job fails with the piece's log unless
    the pipeline catches it."""

    def __init__(self, file: str, failure: Failure) -> None:
        super().__init__(f"{file}: {failure.error}")
        self.file = file
        self.failure = failure


class Ctx:
    def __init__(self, host: TemplatePipeline, job: Job, loaded: LoadedPipeline, inputs: Mapping[str, Any]) -> None:
        self._host = host
        self._job = job
        self._loaded = loaded
        self._inputs_v = int(inputs.get("v", 0))
        self.inputs_version = loaded.inputs_version
        self.plate: PlateSize = loaded.plate
        self.outputs: list[PipelineOutput] = []
        self.blob_keys: list[str] = []
        self.log_tail: list[str] = []
        self.steps: list[StepInfo] = [StepInfo(name="render", state="running", done=0, total=None)]
        self._rendered: list[str] = []

    async def render(self, file: str, /, **params: ParamValue) -> Part:
        job = self._job
        key = piece_key(job.slug, job.model_version, file, params)
        req = PieceRequest(slug=job.slug, revision=job.model_version, file=file, params=dict(params), piece_key=key)
        outcome = await self._host.piece(req)
        if outcome.result is None:
            raise PieceFailedError(file, outcome.failure or Failure(error="the piece failed"))
        if key not in self._rendered:
            self._rendered.append(key)
            self.blob_keys.append(key)
        self.log_tail = list(outcome.result.log_tail)
        return Part.of(req, outcome.result)

    async def pack(self, items: Sequence[Part | tuple[Part, int]], *, goal: str = "fewest_plates",
                   filament_plan: object | None = None) -> Layout:
        if filament_plan is not None:
            raise ValueError("filament_plan arrives with Arrange (phase 5)")
        packed = [PackItem(part=i[0], count=i[1]) if isinstance(i, tuple) else PackItem(part=i) for i in items]
        layout: Layout = await self._host.activity_call(
            "pack", PackRequest(items=packed, plate=self.plate, goal=goal), result_type=Layout
        )
        return layout

    def plate_of(self, items: Sequence[Part], *, at: Sequence[tuple[float, float, float]] | None = None) -> LayoutPlate:
        if at is None:
            at = [(0.0, 0.0, 0.0)] * len(items) if len(items) == 1 else None
        if at is None:
            raise ValueError(
                "plate_of needs at=[(x, y, rot), …] for more than one part;"
                " use pack for automatic placement"
            )
        return explicit_plate(items, at)

    async def output(self, *, plates: Layout | Sequence[LayoutPlate], name: str | None = None,
                     bom: Sequence[BomEntry | Mapping[str, Any]] | None = None,
                     files: Mapping[str, str | bytes | Blob | Mapping[str, Any]] | None = None) -> OutputRef:
        layout = plates if isinstance(plates, Layout) else Layout(plates=list(plates))
        used = [layout.own] if layout.own else list(dict.fromkeys(p.piece_key for pl in layout.plates for p in pl.items))
        parts = [self._host.part_of(key) for key in used]
        index = len(self.outputs)
        req = OutputRequest(
            job_id=self._job.id, index=index, slug=self._job.slug, layout=layout, parts=parts, name=name,
            bom=[b if isinstance(b, BomEntry) else BomEntry.model_validate(b) for b in bom or []],
            files={k: _file(k, v) for k, v in (files or {}).items()},
            record=OutputRecord(
                revision=self._job.model_version, ui_api=self._loaded.ui_api, pipeline_api=self._loaded.api,
                pipeline_version=self._loaded.version, inputs_v=self._inputs_v, plate_key=self.plate.key,
                parts=used,
            ),
        )
        written: PipelineOutput = await self._host.activity_call("write_output", req, result_type=PipelineOutput)
        self.outputs.append(written)
        self.blob_keys.extend(k for k in written.blob_keys if k not in self.blob_keys)
        return OutputRef(index=index, name=name)

    def progress(self, message: str, *, done: int | None = None, total: int | None = None) -> None:
        self.steps = [StepInfo(name=message, state="running", done=done, total=total)]
        self._host.project_later(steps=self.steps)


def _file(name: str, value: str | bytes | Blob | Mapping[str, Any]) -> str | Blob:
    if isinstance(value, Blob | str):
        return value
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(
                f"{name}: binary files come from a template activity as a Blob"
                " (scadbuddy.template.emit)"
            ) from None
    return Blob.model_validate(value)
```

- [ ] **Step 6: `TemplatePipeline` runs the source**

Replace phase 1's `TemplatePipeline` in `workflows/pipelines.py`. `RenderPiece` stays; its outcomes now carry `piece_key`. Add these imports inside the existing passthrough block: `LoadedPipeline`, `LoadRequest`, and `from scadbuddy.template import Part`. Outside it, add `from scadbuddy.workflows.ctx import Ctx, PieceFailedError` and `from scadbuddy.workflows.sandbox import load_pipeline_module, pipeline_error, pipeline_error_at`, the latter two also inside `imports_passed_through` so the sandbox reuses them.

```python
@workflow.defn(name="TemplatePipeline")
class TemplatePipeline:
    def __init__(self) -> None:
        self._outcomes: dict[str, PieceOutcome] = {}
        self._parts: dict[str, Part] = {}
        self._job: Job | None = None

    @workflow.signal
    def piece_finished(self, outcome: PieceOutcome) -> None:
        self._outcomes[outcome.piece_key] = outcome

    def part_of(self, key: str) -> Part:
        return self._parts[key]

    async def activity_call(self, name: str, arg: object, *, result_type: type[Any]) -> Any:
        # `write_output` loads meshes, renders every plate's thumbnails (bounded by the
        # render timeout) and writes the 3MF: it gets the openscad bound and heartbeats.
        long = name == "write_output"
        return await workflow.execute_activity(
            name,
            arg,
            result_type=result_type,
            start_to_close_timeout=_openscad_timeout() if long else SHORT,
            heartbeat_timeout=timedelta(seconds=30) if long else None,
            retry_policy=RETRY,
        )

    async def _project(self, **fields: object) -> None:
        assert self._job is not None
        await workflow.execute_activity(
            "project",
            Projection.model_validate({"job_id": self._job.id, "slug": self._job.slug, **fields}),
            start_to_close_timeout=SHORT,
            retry_policy=PROJECT_RETRY,
        )

    def project_later(self, **fields: object) -> None:
        """`ctx.progress` is synchronous (§5.2): the write goes out without waiting.
        A late one after the job settles is a no-op (`set_steps` guards the state)."""
        assert self._job is not None
        workflow.start_activity(
            "project",
            Projection.model_validate({"job_id": self._job.id, "slug": self._job.slug, **fields}),
            start_to_close_timeout=SHORT,
            retry_policy=PROJECT_RETRY,
        )

    @workflow.run
    async def run(self, job: Job) -> None:
        self._job = job
        ctx: Ctx | None = None
        steps = [StepInfo(name="render", state="running", done=0, total=None)]
        try:
            await self._project(state="running")
            await self._project(steps=steps)
            loaded: LoadedPipeline = await workflow.execute_activity(
                "load_pipeline", LoadRequest(slug=job.slug, revision=job.model_version),
                result_type=LoadedPipeline, start_to_close_timeout=SHORT, retry_policy=RETRY,
            )
            inputs = job.inputs or {"params": job.params, "v": 0}
            ctx = Ctx(self, job, loaded, inputs)
            failure = await self._run_pipeline(ctx, loaded, inputs)
            steps = ctx.steps
            if failure is None and not ctx.outputs:
                failure = Failure(error=f"{loaded.file}: the pipeline wrote no output")
            if failure is not None:
                steps = [s.model_copy(update={"state": "failed"}) for s in steps]
                await self._project(state="failed", failure=failure, steps=steps, pipeline_version=loaded.version)
                return
            steps = [s.model_copy(update={"state": "done"}) for s in steps]
            await self._project(
                state="done", result=ctx.outputs[0].result, outputs=ctx.outputs, log_tail=ctx.log_tail,
                steps=steps, blob_keys=ctx.blob_keys, pipeline_version=loaded.version,
            )
        except (asyncio.CancelledError, ActivityError, ChildWorkflowError) as error:
            if is_cancelled_exception(error) and workflow.cancellation_reason() is not None:
                await self._project(state="cancelled", failure=Failure(error="cancelled"), steps=steps)
                raise
            if isinstance(error, asyncio.CancelledError):
                raise
            await self._project(state="failed", failure=_failure_of(error), steps=steps)

    async def _run_pipeline(self, ctx: Ctx, loaded: LoadedPipeline, inputs: dict[str, Any]) -> Failure | None:
        """Run the template's `run(ctx, inputs)`. What it raises becomes the job's error with
        its file and line (§3.4 step 2); cancellation passes through untouched."""
        try:
            namespace = load_pipeline_module(loaded.source, loaded.file)
            await namespace["run"](ctx, inputs)
        except PieceFailedError as error:
            return error.failure.model_copy(update={"error": str(error)})
        except (ActivityError, ChildWorkflowError) as error:
            if is_cancelled_exception(error):
                raise
            failure = _failure_of(error)
            return failure.model_copy(update={"error": pipeline_error_at(error, loaded.file, failure.error)})
        except Exception as error:
            return Failure(error=pipeline_error(error, loaded.file))
        return None

    async def piece(self, req: PieceRequest) -> PieceOutcome:
        # Identical renders in one pipeline share one task, so one child (§3.4).
        if req.piece_key not in self._pieces:
            self._pieces[req.piece_key] = asyncio.create_task(self._piece(req))
        outcome = await self._pieces[req.piece_key]
        if outcome.result is not None:
            self._parts[req.piece_key] = Part.of(req, outcome.result)
        return outcome

    async def _settle_progress(self) -> None:
        """Let every progress write land before the final one, so it cannot overtake it."""
        for handle in self._progress:
            with contextlib.suppress(ActivityError):
                await handle
```

In `__init__`, also set `self._pieces: dict[str, asyncio.Task[PieceOutcome]] = {}` and `self._progress: list[workflow.ActivityHandle[Any]] = []`. `project_later` appends the handle `workflow.start_activity` returns to `self._progress`. In `run`, call `await self._settle_progress()` right before the `failed` projection and before the `done` projection.

`pipeline_error_at(error, filename, message)` goes in `workflows/sandbox.py` (Task 1's module, with its test added to `tests/test_pipeline_sandbox.py`). It is `pipeline_error` with the message replaced:

```python
def pipeline_error_at(error: BaseException, filename: str, message: str) -> str:
    where = pipeline_error(error, filename).split(": ", 1)[0]
    return f"{where}: {message}"
```

Test to append in `tests/test_pipeline_sandbox.py`:

```python
def test_pipeline_error_at_keeps_the_location() -> None:
    from scadbuddy.workflows.sandbox import pipeline_error_at
    assert pipeline_error_at(ValueError("x"), "pipeline/pipeline.py", "pack goal nope") == (
        "pipeline/pipeline.py: pack goal nope"
    )
```

Make phase 1's `_piece(req)` per-piece. The two uses of the single `_outcome` become `self._outcomes.get(req.piece_key)`: `if self._outcomes.get(req.piece_key) is not None: return self._outcomes[req.piece_key]`, and the wait becomes `await workflow.wait_condition(lambda: req.piece_key in self._outcomes, timeout=_waiter_recheck())`. The `memo` and ABANDON arguments stay exactly as phase 1 has them.

The workflow task must never fail on a template's exception. That is why `_run_pipeline` catches `Exception`, which includes `RestrictedWorkflowAccessError` (Task 1 measured that it is catchable). `asyncio.CancelledError` is a `BaseException` and passes through.

- [ ] **Step 7: The projection stores outputs; `project` refs every blob**

Create the migration (`backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_render_jobs_outputs.sql`):

```sql
-- Pipeline outputs on the job (spec 2026-09-27 §5.2): Generate saves each one.
ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS outputs jsonb NOT NULL DEFAULT '[]'::jsonb;
```

In `render/projection.py`:
- add `"outputs"` to `PROJECTION_COLUMNS`;
- in `finish`'s first `UPDATE`, add `outputs = %s` with `Jsonb([o.model_dump(mode="json") for o in job.outputs])` in the matching position.

In `RenderActivities.project` (`workflows/activities.py`), after the `projection.result` copy, add `job.outputs = projection.outputs`. Replace the single-key ref with:

```python
        if projection.state == "done":
            keys = list(dict.fromkeys([*projection.blob_keys, *([projection.blob_key] if projection.blob_key else [])]))
            for key in keys:
                await asyncio.to_thread(self.deps.refs.add, key, "job", job.id)
```

This still runs before `finish`, as phase 1 has it. Add to `tests/test_activities.py`'s `test_project_done_copies_the_result_and_refs_the_blob`: pass `blob_keys=["piece-key", "output-x-0"]`, then assert both are in `refs.referenced()`.

- [ ] **Step 8: Register the pipeline activities; the worker fills the record fields**

In `workflows/client.py`, give `render_worker` a keyword `pipeline: PipelineActivities` and pass `activities=[*activities.all(), *pipeline.all()]`. In `worker.py`'s `_poll`, pass `pipeline=PipelineActivities(deps)`. Then:
- move `probe_openscad_version` (and its `VERSION_TIMEOUT`) from `api/deps.py:352` to `render/runner.py`, beside `run_openscad`, unchanged in body; `render/runner.py` gains the `import shutil`, `import logging` and `logger = logging.getLogger(__name__)` the probe needs, and `api/deps.py` drops its now-unused `import shutil` (ruff F401 catches both); `api/deps.py` and `main.py:18` import it from there. `worker.py` imports `api.deps` only under `TYPE_CHECKING` (the RenderService review's M4), and must keep it that way;
- at the top of `_poll`, set `deps.revision = settings.revision` and `deps.openscad_version = await probe_openscad_version(deps.config) or ""`, importing it from `scadbuddy.render.runner`. `_poll` runs for both the worker process and the in-process worker (`worker_deps_from_state`), so this covers both; do not set them in `build_worker_deps`.

Update the phase 1 end-to-end test `tests/test_activities.py::test_a_job_renders_end_to_end_on_the_render_worker` to pass `pipeline=PipelineActivities(deps)`.

- [ ] **Step 9: Run the tests to verify they pass**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/test_template_pipeline.py tests/test_workflows.py tests/test_activities.py tests/test_pipeline_sandbox.py tests/test_projection.py -q`
Expected: all pass. `test_a_job_renders_end_to_end_on_the_render_worker` now runs the default pipeline over the real activities: the job reaches `done` with one output whose result is the piece's.

`tests/test_workflows.py` was written for phase 1's single-piece workflow. Its `FakeActivities` gains three methods, `load_pipeline`, `pack` and `write_output`, each delegating to a `FakeWorld()` instance it holds (`FakeWorld().load_pipeline`, `pack_layout`, `FakeWorld.write_output`'s body), and its `_worker` registers them. Update the assertions that read `Projection.result` only where they now also see `outputs`; the behaviour they pin is unchanged.

Three more suites run the new workflow and must stay green:
- `tests/test_submit.py` builds its worker with `tests/test_workflows.py`'s `_worker`/`FakeActivities`, so the change above covers it;
- `tests/test_worker.py` and `tests/api/test_temporal_path.py` run the real worker through `_poll`, which now registers `PipelineActivities` (Step 8).

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/test_submit.py tests/test_worker.py tests/api/test_temporal_path.py -q`
Expected: all pass.

- [ ] **Step 10: Gates and commit**

Run the backend gates.

```bash
git add backend/scadbuddy/workflows/ backend/scadbuddy/render/projection.py backend/scadbuddy/worker.py \
  backend/scadbuddy/migrations/ backend/tests/support/pipelines.py backend/tests/test_template_pipeline.py \
  backend/tests/test_workflows.py backend/tests/test_activities.py backend/tests/test_pipeline_sandbox.py
git commit -m "feat(workflows): TemplatePipeline runs the template's pipeline over ctx (#427)"
```

---

### Task 5: Template activities in their own process group

**Files:**
- Create: `backend/scadbuddy/workflows/template_process.py`, `backend/scadbuddy/workflows/template_runner.py`, `backend/tests/test_template_activities.py`
- Modify: `backend/scadbuddy/workflows/models.py` (`TemplateCall`), `workflows/ctx.py` (`Ctx.activity`), `workflows/pipeline_activities.py` (`run_template_activity`), `core/config.py` and `core/settings.py` (`template_activity_max_timeout`, `pipeline_timeout`), `render/submit.py` (the memo, `execution_timeout`, `settle_timed_out`), `render/projection.py` (`stale_running`), `tests/support/pipelines.py` (the `run_template_activity` fake returns), `tests/test_template_pipeline.py`, `tests/test_submit.py`

**Interfaces:**
- Consumes: `Blob`, `Part` (Task 3); `_heartbeating` (phase 1); `env_for(data_dir)` (`core/fontconfig.py`); `LOG_TAIL_LINES` (`render/runner.py`); `PipelineActivities.model_dir` (Task 2).
- Produces:
  ```python
  # workflows/models.py
  class TemplateCall(BaseModel): slug: str; revision: str | None; name: str; args: list[Any] = []; kwargs: dict[str, Any] = {}; timeout_s: float
  # workflows/template_process.py
  class TemplateError(RuntimeError): log_tail: list[str]; retryable: bool
  MAX_RESULT_BYTES = 1 << 20
  async def run_template(model_dir: Path, request: dict[str, Any], *, out: Path, out_key: str, python: str, data_dir: Path, timeout: float) -> Any
  def template_out_key(call: TemplateCall, source_sha: str) -> str   # "act-" + 40 hex; source_sha = sha256 of activities.py
  # template_runner: python -m scadbuddy.workflows.template_runner <model_dir> <result.json>; stdin {"mode": "call"|"migrate", ...}
  # activities
  PipelineActivities.run_template_activity(call: TemplateCall) -> Any   @activity.defn(name="run_template_activity")
  Ctx.activity(name, *args, timeout: float | None = None, **kwargs) -> Any
  Config.template_activity_max_timeout: float = 1800.0   # SCADBUDDY_TEMPLATE_ACTIVITY_MAX_TIMEOUT
  PIPELINE_TIMEOUT_FACTOR = 4; Config.pipeline_timeout -> float   # 4 x the ceiling: TemplatePipeline's execution_timeout
  JobProjection.stale_running(older_than: float) -> list[Job]
  async def RenderService.settle_timed_out(self) -> int      # called by the reconciler loop
  ```
  A template exception raises `ApplicationError(type="TemplateActivityError", non_retryable=True)` with the message `pipeline/activities.py:<line>: <Type>: <message>`. A crash or kill of the process is retryable.

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_template_activities.py`:

```python
"""pipeline/activities.py in a subprocess (spec 2026-09-27 §5.2, §3.4, §9)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.template import Blob
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.models import TemplateCall
from scadbuddy.workflows.pipeline_activities import PipelineActivities

ACTIVITIES = '''\
import os, subprocess, time
from scadbuddy.template import emit

def add(a, b):
    return {"sum": a + b}

async def later(x):
    return x * 2

def environment():
    return sorted(os.environ)

def guide(rooms):
    return emit("guide.svg", f"<svg>{rooms}</svg>")

def read(blob):
    return blob.read_bytes().decode()

def boom():
    x = 1
    raise ValueError("no rooms")

def huge():
    return "x" * (2 << 20)

def spawn(pid_file):
    child = subprocess.Popen(["sleep", "300"])
    with open(pid_file, "w") as f:
        f.write(f"{os.getpid()} {child.pid}")
    time.sleep(300)

def leave_child():
    subprocess.Popen(["sleep", "300"])
    return "done"
'''


def _world(tmp_path: Path) -> tuple[PipelineActivities, DataPaths]:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("cube();\n", encoding="utf-8")
    paths.model_meta("demo").write_text('{"name": "Demo"}', encoding="utf-8")
    (paths.model_dir("demo") / "pipeline").mkdir()
    (paths.model_dir("demo") / "pipeline" / "activities.py").write_text(ACTIVITIES, encoding="utf-8")
    deps = WorkerDeps(config=Config(data_dir=paths.root), paths=paths, assets=AssetStore(paths.assets),
                      blobs=LocalBlobStore(paths.blobs), refs=None, projection=None,  # type: ignore[arg-type]
                      template_python=sys.executable)
    return PipelineActivities(deps), paths


def _call(name: str, *args: object, timeout_s: float = 60, **kwargs: object) -> TemplateCall:
    return TemplateCall(slug="demo", revision=None, name=name, args=list(args), kwargs=dict(kwargs), timeout_s=timeout_s)


async def test_a_function_runs_with_json_in_and_out(tmp_path: Path) -> None:
    acts, _ = _world(tmp_path)
    env = ActivityEnvironment()
    assert await env.run(acts.run_template_activity, _call("add", 2, b=3)) == {"sum": 5}
    assert await env.run(acts.run_template_activity, _call("later", 21)) == 42


async def test_the_process_sees_no_worker_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCADBUDDY_DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("SCADBUDDY_SECRET_KEY_FILE", "/run/secret")
    acts, _ = _world(tmp_path)
    names = await ActivityEnvironment().run(acts.run_template_activity, _call("environment"))
    assert not [n for n in names if n.startswith("SCADBUDDY_") and not n.startswith("SCADBUDDY_TEMPLATE_")]


async def test_an_emitted_file_comes_back_as_a_blob_and_can_be_read_again(tmp_path: Path) -> None:
    acts, paths = _world(tmp_path)
    env = ActivityEnvironment()
    blob = Blob.model_validate(await env.run(acts.run_template_activity, _call("guide", 2)))
    assert (paths.blobs / blob.key / "guide.svg").read_text() == "<svg>2</svg>"
    assert await env.run(acts.run_template_activity, _call("read", blob.model_dump())) == "<svg>2</svg>"


@pytest.mark.parametrize(("name", "message"), [
    ("boom", "pipeline/activities.py:21: ValueError: no rooms"),
    ("missing", "pipeline/activities.py has no function 'missing'"),
    ("_private", "pipeline/activities.py has no function '_private'"),
    ("huge", "return scadbuddy.template.emit"),
])
async def test_a_template_error_is_non_retryable_and_names_its_line(tmp_path: Path, name: str, message: str) -> None:
    acts, _ = _world(tmp_path)
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(acts.run_template_activity, _call(name))
    assert raised.value.type == "TemplateActivityError" and raised.value.non_retryable
    assert message in raised.value.message


def _dead(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0]
    except FileNotFoundError:
        return True
    return state == "Z"


async def test_cancelling_kills_the_template_process_group(tmp_path: Path) -> None:
    acts, _ = _world(tmp_path)
    pid_file = tmp_path / "pids"
    env = ActivityEnvironment()
    task = asyncio.create_task(env.run(acts.run_template_activity, _call("spawn", str(pid_file))))
    deadline = time.monotonic() + 30
    while not pid_file.is_file() or not pid_file.read_text():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)
    pids = [int(p) for p in pid_file.read_text().split()]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    deadline = time.monotonic() + 5
    while not all(_dead(pid) for pid in pids):
        assert time.monotonic() < deadline, f"still alive: {pids}"
        await asyncio.sleep(0.05)


async def test_a_timed_out_function_is_killed_and_refused(tmp_path: Path) -> None:
    acts, _ = _world(tmp_path)
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(acts.run_template_activity, _call("spawn", str(tmp_path / "p"), timeout_s=1))
    assert "timed out after 1s" in raised.value.message
```

`boom`'s `raise` is line 21 of `ACTIVITIES`. If you edit that source, recount the lines.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_template_activities.py -q`
Expected: collection error `ImportError: cannot import name 'TemplateCall'`.

- [ ] **Step 3: The payload, the setting and the memo**

In `workflows/models.py`:

```python
class TemplateCall(BaseModel):
    """`ctx.activity(name, …)` (§5.2): JSON arguments, `Blob`/`Part` as their dicts."""

    slug: str
    revision: str | None
    name: str
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float
```

In `core/config.py`'s `Config`, add `template_activity_max_timeout: float = 1800.0` next to `render_timeout`. In `core/settings.py`, add the field `template_activity_max_timeout: float = 1800.0` (so env `SCADBUDDY_TEMPLATE_ACTIVITY_MAX_TIMEOUT`) and pass it through `to_config` the way `render_timeout` is passed. In `render/submit.py`, wherever the workflow start sets `memo={"activity_timeout": …}`, add `"template_activity_max_timeout": self.config.template_activity_max_timeout` to the dict `RenderService._memo()` returns (both `_start` and `render_preview` use it).

- [ ] **Step 4: The child side, `workflows/template_runner.py`**

```python
"""`python -m scadbuddy.workflows.template_runner <model_dir> <result.json>`: one template
function (or `migrate`) in its own process (spec 2026-09-27 §5.2, §8.2, §9). Reads the
request as JSON on stdin; writes {"ok": value} or {"error": …, "line": …} to result.json."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import BaseModel

from scadbuddy.template import Blob, Part


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if isinstance(value, dict):
        if value.get("kind") == "blob":
            return Blob.model_validate(value)
        if value.get("kind") == "part":
            return Part.model_validate(value)
        return {k: _decode(v) for k, v in value.items()}
    return value


def _encode(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, bytes):
        raise TypeError("return scadbuddy.template.emit(name, data) for bytes")
    return value


def _line(error: BaseException, path: Path) -> int | None:
    line = None
    tb = error.__traceback__
    while tb is not None:
        if Path(tb.tb_frame.f_code.co_filename) == path:
            line = tb.tb_lineno
        tb = tb.tb_next
    return line


def _migrate(module: ModuleType, inputs: dict[str, Any]) -> dict[str, Any]:
    current = int(getattr(module, "INPUTS_VERSION", 0))
    version = int(inputs.get("v", 0))
    if version > current:
        raise ValueError(f"these inputs are v{version}; the template's INPUTS_VERSION is {current}")
    if version == current:
        return inputs
    migrate = getattr(module, "migrate", None)
    if not callable(migrate):
        raise LookupError(
            f"inputs are v{version} and pipeline.py defines no migrate(inputs, from_version)"
        )
    migrated = dict(migrate(dict(inputs), version))
    migrated["v"] = current
    return migrated


def main(argv: list[str]) -> int:
    model_dir, result_path = Path(argv[1]), Path(argv[2])
    request = json.loads(sys.stdin.read())
    migrate = request["mode"] == "migrate"
    path = model_dir / "pipeline" / ("pipeline.py" if migrate else "activities.py")
    sys.path.insert(0, str(path.parent))
    try:
        if migrate:
            value: Any = _migrate(_load(path, "scadbuddy_pipeline"), request["inputs"])
        else:
            name = request["name"]
            fn = getattr(_load(path, "scadbuddy_template_activities"), name, None)
            if name.startswith("_") or not callable(fn):
                raise LookupError(f"pipeline/activities.py has no function {name!r}")
            value = fn(*_decode(request["args"]), **_decode(request["kwargs"]))
            if inspect.isawaitable(value):
                value = asyncio.run(_await(value))
        reply: dict[str, Any] = {"ok": _encode(value)}
    except Exception as error:
        reply = {"error": f"{type(error).__name__}: {error}", "line": _line(error, path),
                 "file": f"pipeline/{path.name}"}
    result_path.write_text(json.dumps(reply), encoding="utf-8")
    return 0


async def _await(value: Any) -> Any:
    return await value


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

An `emit` inside the function writes to `SCADBUDDY_TEMPLATE_OUT`, which the parent sets.

- [ ] **Step 5: The parent side, `workflows/template_process.py`**

```python
"""Run a template function in a subprocess, in its own process group, killed with every
child it spawned on cancellation or timeout (spec 2026-09-27 §3.4, §5.2)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import tempfile
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any

from scadbuddy.core.fontconfig import env_for
from scadbuddy.render.runner import LOG_TAIL_LINES

MAX_RESULT_BYTES = 1 << 20


class TemplateError(RuntimeError):
    def __init__(self, message: str, log_tail: list[str], *, retryable: bool = False) -> None:
        super().__init__(message)
        self.log_tail = log_tail
        self.retryable = retryable


def template_out_key(call: Any, source_sha: str) -> str:
    """Where a call's emitted files go. ``source_sha`` is `activities.py`'s own sha256,
    so a live template (revision None) edited between two calls never reuses the
    first call's directory; identical calls on one source write identical bytes."""
    raw = json.dumps(
        [call.slug, call.revision, source_sha, call.name, call.args, call.kwargs], sort_keys=True, default=str
    )
    return "act-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _kill_group(process: asyncio.subprocess.Process) -> None:
    # Under start_new_session the group id is the child's pid. Not os.getpgid(pid):
    # once the leader is reaped that lookup fails while its children still run.
    # Accepted edge: after a normal exit with the group already empty, the pid could
    # be reused as another new session's group id before this runs; a surviving
    # grandchild keeps the id taken, which is the case this kill exists for.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


async def _drain(stream: asyncio.StreamReader, tail: deque[str]) -> None:
    async for line in stream:
        tail.append(line.decode("utf-8", errors="replace").rstrip("\n"))


async def run_template(model_dir: Path, request: dict[str, Any], *, out: Path, out_key: str, python: str,
                       data_dir: Path, timeout: float) -> Any:
    with tempfile.TemporaryDirectory(prefix="scadbuddy-template-") as scratch:
        result_path = Path(scratch) / "result.json"
        # The allowlist openscad gets (#281), not the worker's environment (§9).
        env = env_for(data_dir)
        env.update({"SCADBUDDY_TEMPLATE_OUT": str(out), "SCADBUDDY_TEMPLATE_OUT_KEY": out_key,
                    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"})
        process = await asyncio.create_subprocess_exec(
            python, "-m", "scadbuddy.workflows.template_runner", str(model_dir), str(result_path),
            cwd=model_dir, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=env, start_new_session=True,
        )
        assert process.stdin is not None and process.stdout is not None
        with suppress(BrokenPipeError, ConnectionResetError):
            process.stdin.write(json.dumps(request).encode())
            await process.stdin.drain()
            process.stdin.close()
        lines: deque[str] = deque(maxlen=LOG_TAIL_LINES)
        # Not communicate(): a grandchild holding stdout would keep it waiting for EOF.
        drain = asyncio.create_task(_drain(process.stdout, lines))
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except TimeoutError:
            _kill_group(process)
            await process.wait()
            drain.cancel()
            name = request.get("name")
            raise TemplateError(
                f"pipeline/activities.py:{name} timed out after {timeout:g}s", list(lines)
            ) from None
        except asyncio.CancelledError:
            _kill_group(process)
            await process.wait()
            drain.cancel()
            raise
        # The function returned; whatever it left running goes with its group, which
        # also closes the pipe a grandchild held.
        _kill_group(process)
        with suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(drain), 5)
        drain.cancel()
        tail = list(lines)
        if process.returncode != 0 or not result_path.is_file():
            raise TemplateError(f"the template process exited with {process.returncode}", tail, retryable=True)
        if result_path.stat().st_size > MAX_RESULT_BYTES:
            raise TemplateError(
                "the template function returned more than 1 MiB;"
                " return scadbuddy.template.emit(name, data) instead",
                tail,
            )
        reply = json.loads(result_path.read_text(encoding="utf-8"))
        if "error" in reply:
            where = reply["file"] if reply.get("line") is None else f"{reply['file']}:{reply['line']}"
            raise TemplateError(f"{where}: {reply['error']}", tail)
        return reply["ok"]
```

`env_for(data_dir)` builds its environment from an allowlist, so the child cannot see `SCADBUDDY_DATABASE_URL` or the key file. `PATH` passes the allowlist, so the test's `subprocess.Popen(["sleep", …])` resolves.

Add a test beside the cancellation one, pinning that a function which returns while a child it spawned still holds stdout does not wait for that child:

```python
async def test_a_returned_function_does_not_wait_for_its_children(tmp_path: Path) -> None:
    acts, _ = _world(tmp_path)
    started = time.monotonic()
    assert await ActivityEnvironment().run(acts.run_template_activity, _call("leave_child")) == "done"
    assert time.monotonic() - started < 20
```

(`leave_child` is the last function in `ACTIVITIES`, below every line the other tests count.)

- [ ] **Step 6: The activity and `ctx.activity`**

In `PipelineActivities` (add `self.run_template_activity` to `all()`):

```python
    async def _localize(self, value: Any) -> Any:
        """Give a template activity the local path of every `Blob`/`Part` it is passed."""
        blobs = self.deps.blobs
        if isinstance(value, list):
            return [await self._localize(v) for v in value]
        if isinstance(value, dict):
            if value.get("kind") == "blob":
                await blobs.fetch(value["key"])
                root = blobs.dir_for(value["key"]).resolve()
                path = (root / value["path"]).resolve()
                if not path.is_relative_to(root):
                    raise ApplicationError(f"blob path {value['path']!r} leaves its blob",
                                           type="TemplateActivityError", non_retryable=True)
                return {**value, "local": str(path)}
            if value.get("kind") == "part":
                await blobs.fetch(value["piece_key"])
                return {**value, "local": str(blobs.dir_for(value["piece_key"]))}
            return {k: await self._localize(v) for k, v in value.items()}
        return value

    @activity.defn(name="run_template_activity")
    async def run_template_activity(self, call: TemplateCall) -> Any:
        d = self.deps
        model_dir = await self.model_dir(call.slug, call.revision)
        source = model_dir / "pipeline" / "activities.py"  # hashlib imported at the top
        source_sha = hashlib.sha256(await asyncio.to_thread(source.read_bytes)).hexdigest() if source.is_file() else ""
        out_key = template_out_key(call, source_sha)
        out = d.blobs.dir_for(out_key)
        request = {"mode": "call", "name": call.name, "args": await self._localize(call.args),
                   "kwargs": await self._localize(call.kwargs)}
        work = asyncio.create_task(run_template(
            model_dir, request, out=out, out_key=out_key, python=d.template_python,
            data_dir=d.config.data_dir, timeout=call.timeout_s,
        ))
        try:
            value = await _heartbeating(work)
        except TemplateError as error:
            raise ApplicationError(str(error), Failure(error=str(error), log_tail=error.log_tail),
                                   type="TemplateActivityError", non_retryable=not error.retryable) from None
        if any(out.iterdir()):
            await d.blobs.publish(out_key, scope=BlobScope(slug=call.slug))
        return value
```

In `Ctx` (`workflows/ctx.py`):

```python
    async def activity(self, name: str, *args: Any, timeout: float | None = None, **kwargs: Any) -> Any:
        default = workflow.memo_value("activity_timeout", default=180.0, type_hint=float)
        ceiling = workflow.memo_value("template_activity_max_timeout", default=1800.0, type_hint=float)
        seconds = min(timeout if timeout is not None else default, ceiling)
        call = TemplateCall(slug=self._job.slug, revision=self._job.model_version, name=name,
                            args=[_json(a) for a in args], kwargs={k: _json(v) for k, v in kwargs.items()},
                            timeout_s=seconds)
        return await workflow.execute_activity(
            "run_template_activity", call, start_to_close_timeout=timedelta(seconds=seconds + 30),
            heartbeat_timeout=timedelta(seconds=30), retry_policy=RETRY,
        )
```

Also add to `ctx.py`:

```python
def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    return value
```

It needs `TemplateCall` added to `ctx.py`'s passthrough import from `workflows/models.py`, `from datetime import timedelta`, `from pydantic import BaseModel`, and `RETRY` imported from `scadbuddy.workflows.pipelines` inside the function to avoid the import cycle (or move `RETRY`/`SHORT` into `workflows/models.py` and import them in both). `start_to_close` is the subprocess timeout plus 30 s, so the subprocess's own kill always fires first.

Also restore `run_template_activity` in `tests/support/pipelines.py` (Task 4 Step 1 dropped it). Then add to `tests/test_template_pipeline.py`:

```python
async def test_ctx_activity_passes_parts_and_returns_json(tmp_path) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "activities.py"
    source.write_text("def count(parts, n):\n    return {'n': len(parts) * n}\n")
    world = FakeWorld("""\
async def run(ctx, inputs):
    a = await ctx.render("model.scad", w=1)
    got = await ctx.activity("count", [a, a], n=3)
    await ctx.output(plates=await ctx.pack([a]), name=str(got["n"]))
""", activities_py=source)
    async with temporal_client() as client:
        await run_job(world, a_job(), client=client)
    assert world.outputs[0].name == "6"
    assert world.calls[0].args[0][0]["kind"] == "part"
```

- [ ] **Step 7: Bound a pipeline's run**

A `pipeline.py` that computes without yielding (a runaway loop in `run`) trips the SDK's deadlock detection ("Potential deadlock detected": a workflow task must yield within 2 s). The SDK then retries that workflow task forever, and the row stays `running`. Nothing else bounds it, so bound the workflow and settle what the bound ends.

Tests first. Append to `backend/tests/test_template_pipeline.py`:

```python
from datetime import timedelta

from temporalio.client import WorkflowFailureError
from temporalio.exceptions import TimeoutError as WorkflowTimeoutError

#: Bounded CPU work that never yields: about a minute in CPython, 30x the SDK's 2 s
#: deadlock detector, so every workflow task fails and only the execution timeout
#: ends the run. Bounded, so no thread spins for the rest of the session. Should a
#: machine ever finish it early, the pipeline raises: the job fails, never `done`.
NEVER_YIELDS = (
    "async def run(ctx, inputs):\n"
    "    total = sum(i * i for i in range(10**9))\n"
    "    raise RuntimeError(f'finished early: {total}')\n"
)


async def test_a_pipeline_that_never_yields_times_out() -> None:
    world, job = FakeWorld(NEVER_YIELDS), a_job()
    timed_out = False
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        async with Worker(client, task_queue=queue, workflows=[TemplatePipeline, RenderPiece],
                          activities=world.activities()):
            try:
                await asyncio.wait_for(
                    client.execute_workflow(
                        TemplatePipeline.run, job, id=workflow_id_for(job.id), task_queue=queue,
                        execution_timeout=timedelta(seconds=8),
                    ),
                    timeout=60,
                )
            except WorkflowFailureError as error:
                timed_out = isinstance(error.cause, WorkflowTimeoutError)
    settled = [p.state for p in world.projections if p.state in ("done", "failed")]
    # Either the timeout ended it (the expected path) or the pipeline failed the job
    # itself; never `done`.
    assert "done" not in settled
    assert timed_out or settled == ["failed"]
```

And to `backend/tests/test_submit.py` (`requires_postgres`, `requires_temporal`, like its neighbours):

```python
class _ProjectingWorld(FakeWorld):
    """FakeWorld, but `project` writes the row, as `ProjectingActivities` does: without
    it the row never leaves `pending` and `stale_running` never sees it."""

    def __init__(self, source: str, deps: WorkerDeps) -> None:
        super().__init__(source)
        self._real = RenderActivities(deps)

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> None:
        await super().project(projection)
        await self._real.project(projection)


async def test_a_timed_out_pipeline_is_failed_by_the_reconciler(
    make_service: ServiceFactory, deps: WorkerDeps, projection: JobProjection
) -> None:
    world = _ProjectingWorld(NEVER_YIELDS, deps)
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        service = make_service(client, queue, config=replace(deps.config, template_activity_max_timeout=2.0))
        assert service.config.pipeline_timeout == 8.0
        async with Worker(client, task_queue=queue, workflows=[TemplatePipeline, RenderPiece],
                          activities=world.activities()):
            job = await service.submit("demo", {}, model_version=None, supersedes=None,
                                       inputs={"params": {}, "v": 0})
            async with asyncio.timeout(60):
                while (await asyncio.to_thread(projection.read, job.id)).state not in ("done", "failed"):
                    await service.settle_timed_out()
                    await asyncio.sleep(1)
    stored = await asyncio.to_thread(projection.read, job.id)
    assert stored.state == "failed"
    # The reconciler's message; the only other way to fail is the pipeline's own
    # raise, should a machine ever finish the loop inside 8 s (N3).
    assert stored.error == "the pipeline did not finish within 8s" or (
        stored.error or ""
    ).startswith("pipeline/pipeline.py:")
```

`tests/test_submit.py` needs `activity`, `Worker`, `TemplatePipeline`, `RenderPiece`, `RenderActivities`, `Projection`, `dataclasses.replace`, `FakeWorld` (`tests.support.pipelines`) and `NEVER_YIELDS` (`tests.test_template_pipeline`) imported where it does not already. Its `make_service` fixture (`tests/test_submit.py:92–103` at 69306836) passes `config=deps.config` itself, so a `config=` through `**kwargs` would be a duplicate keyword. Give its inner `make` a `config: Config | None = None` keyword and pass `config=config or deps.config`:

```python
    def make(client: Client, task_queue: str, *, config: Config | None = None, **kwargs: Any) -> RenderService:
        return RenderService(
            projection=projection,
            client=client,
            task_queue=task_queue,
            config=config or deps.config,
            paths=deps.paths,
            metrics=Metrics(),
            **kwargs,
        )
```

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/test_template_pipeline.py -k never_yields tests/test_submit.py -k timed_out -q`
Expected: `test_a_timed_out_pipeline_is_failed_by_the_reconciler` fails with `AttributeError: 'Config' object has no attribute 'pipeline_timeout'` (Step 3 already added `template_activity_max_timeout`). `test_a_pipeline_that_never_yields_times_out` already passes (it sets the timeout itself): it pins the SDK behaviour the service relies on.

Implement:

- `core/config.py`: next to `template_activity_max_timeout`,

  ```python
  #: A pipeline's whole run (§5.2): this many of the longest template activity. Bounds
  #: a `pipeline.py` that never yields, which the SDK would otherwise retry forever.
  PIPELINE_TIMEOUT_FACTOR = 4

      @property
      def pipeline_timeout(self) -> float:
          return PIPELINE_TIMEOUT_FACTOR * self.template_activity_max_timeout
  ```

- `render/submit.py` `_start` (used by `submit` and by `reconcile_once`): pass `execution_timeout=timedelta(seconds=self.config.pipeline_timeout)` to `start_workflow`.
- `render/projection.py`:

  ```python
      def stale_running(self, older_than: float) -> list[Job]:
          with self._pool.connection() as conn:
              rows = conn.execute(
                  "SELECT * FROM render_jobs WHERE state = 'running'"
                  " AND started_at < now() - make_interval(secs => %s) ORDER BY started_at",
                  (older_than,),
              ).fetchall()
          return [_job(row) for row in rows]
  ```

- `render/submit.py`:

  ```python
      async def settle_timed_out(self) -> int:
          """Fail every running row whose workflow ended without settling it: timed out
          (a pipeline that never yields), terminated, or failed. Returns how many."""
          timeout = self.config.pipeline_timeout
          stale = await asyncio.to_thread(self.store.stale_running, timeout)
          settled = 0
          for job in stale:
              try:
                  status = (await self.client.get_workflow_handle(workflow_id_for(job.id)).describe()).status
              except RPCError:
                  status = None  # gone from the server's retention
              if status == WorkflowExecutionStatus.RUNNING:
                  continue
              job.state = "failed"
              job.error = (
                  f"the pipeline did not finish within {timeout:g}s"
                  if status == WorkflowExecutionStatus.TIMED_OUT
                  else "the job's workflow ended without settling it"
              )
              if await asyncio.to_thread(self.store.finish, job):
                  settled += 1
          return settled
  ```

  (`from temporalio.client import WorkflowExecutionStatus`.) In `_reconcile_forever`, after `await self.reconcile_once()`, call `await self.settle_timed_out()` inside the same `try`.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/test_template_activities.py tests/test_template_pipeline.py -q`
Expected: all pass.

- [ ] **Step 9: Gates and commit**

```bash
git add backend/scadbuddy/workflows/ backend/scadbuddy/core/config.py backend/scadbuddy/core/settings.py \
  backend/scadbuddy/render/submit.py backend/tests/test_template_activities.py backend/tests/support/pipelines.py \
  backend/tests/test_template_pipeline.py
git commit -m "feat(workflows): template activities in their own process group (#427)"
```

---

### Task 6: Inputs versioning and `migrate`

**Files:**
- Create: `backend/tests/test_migrate_inputs.py`
- Modify: `backend/scadbuddy/workflows/models.py` (`MigrateRequest`, `MigrateResult`), `workflows/pipeline_activities.py` (`migrate_inputs`), `workflows/pipelines.py` (`MigrateInputs`), `workflows/client.py` (register `MigrateInputs`), `render/submit.py` (`RenderService.migrate_inputs`), `api/models.py` (the route), `agent/src/tools/coverage.ts`
- Test: `backend/tests/api/test_pipeline_api.py` (new; the migrate route)

**Interfaces:**
- Consumes: `template_runner`'s `migrate` mode and `run_template` (Task 5); `PipelineActivities.model_dir` (Task 2); phase 1's `RenderService` (its Temporal client and task queue, as `render_preview` uses them).
- Produces:
  ```python
  class MigrateRequest(BaseModel): slug: str; revision: str | None; inputs: dict[str, Any]
  class MigrateResult(BaseModel): inputs: dict[str, Any]; from_version: int; to_version: int
  PipelineActivities.migrate_inputs(req: MigrateRequest) -> MigrateResult     @activity.defn(name="migrate_inputs")
  MigrateInputs.run(req: MigrateRequest) -> MigrateResult                      @workflow.defn(name="MigrateInputs"), id "migrate-<uuid>"
  async def RenderService.migrate_inputs(self, slug: str, inputs: Mapping[str, Any], *, version: str | None) -> MigrateResult   # raises InputsError with the template's message
  POST /api/v1/models/{slug}/inputs/migrate  {inputs, version?} -> 200 MigrateResult | 422 {detail}
  ```

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_migrate_inputs.py`:

```python
"""migrate(inputs, from_version) (spec 2026-09-27 §8.2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows.activities import WorkerDeps
from scadbuddy.workflows.models import MigrateRequest
from scadbuddy.workflows.pipeline_activities import PipelineActivities

PIPELINE = """\
INPUTS_VERSION = 2

def migrate(inputs, from_version):
    if from_version == 0:
        inputs = {**inputs, "house": {"cols": inputs["params"].get("cols", 1)}}
    if from_version <= 1:
        inputs = {**inputs, "pitch": 35}
    return inputs

async def run(ctx, inputs):
    pass
"""


def _acts(tmp_path: Path, source: str) -> PipelineActivities:
    paths = DataPaths(tmp_path / "data")
    paths.ensure()
    paths.model_dir("demo").mkdir(parents=True)
    paths.model_source("demo").write_text("cube();\n", encoding="utf-8")
    paths.model_meta("demo").write_text(
        '{"name": "Demo", "pipeline": {"module": "pipeline/pipeline.py", "api": 1}}', encoding="utf-8"
    )
    (paths.model_dir("demo") / "pipeline").mkdir()
    (paths.model_dir("demo") / "pipeline" / "pipeline.py").write_text(source, encoding="utf-8")
    return PipelineActivities(WorkerDeps(
        config=Config(data_dir=paths.root), paths=paths, assets=AssetStore(paths.assets),
        blobs=LocalBlobStore(paths.blobs), refs=None, projection=None,  # type: ignore[arg-type]
        template_python=sys.executable,
    ))


async def test_old_inputs_are_migrated_and_stamped(tmp_path: Path) -> None:
    result = await ActivityEnvironment().run(
        _acts(tmp_path, PIPELINE).migrate_inputs,
        MigrateRequest(slug="demo", revision=None, inputs={"params": {"cols": 3}, "v": 0}),
    )
    assert (result.from_version, result.to_version) == (0, 2)
    assert result.inputs == {"params": {"cols": 3}, "house": {"cols": 3}, "pitch": 35, "v": 2}


async def test_current_inputs_come_back_unchanged(tmp_path: Path) -> None:
    inputs = {"params": {}, "v": 2, "x": 1}
    result = await ActivityEnvironment().run(
        _acts(tmp_path, PIPELINE).migrate_inputs, MigrateRequest(slug="demo", revision=None, inputs=inputs)
    )
    assert result.inputs == inputs


@pytest.mark.parametrize(("source", "inputs", "message"), [
    (PIPELINE, {"params": {}, "v": 5}, "these inputs are v5; the template's INPUTS_VERSION is 2"),
    ("INPUTS_VERSION = 1\n\nasync def run(ctx, inputs):\n    pass\n", {"params": {}, "v": 0},
     "defines no migrate"),
    ("INPUTS_VERSION = 1\n\ndef migrate(inputs, v):\n    return inputs['nope']\n", {"params": {}, "v": 0},
     "pipeline/pipeline.py:4: KeyError"),
])
async def test_inputs_newer_than_the_template_are_refused(
    tmp_path: Path, source: str, inputs: dict[str, object], message: str
) -> None:
    with pytest.raises(ApplicationError) as raised:
        await ActivityEnvironment().run(
            _acts(tmp_path, source).migrate_inputs, MigrateRequest(slug="demo", revision=None, inputs=inputs)
        )
    assert raised.value.type == "MigrateError" and raised.value.non_retryable
    assert message in raised.value.message
```

`backend/tests/api/test_pipeline_api.py` starts with the route test. It uses the API fixtures phase 1 made Temporal-backed; mark it the way `tests/api/test_jobs.py` marks its render tests:

```python
"""The pipeline API (spec 2026-09-27 §8.2, §10)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from scadbuddy.core.paths import DataPaths

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal]

PIPELINE = (
    "INPUTS_VERSION = 1\n\ndef migrate(inputs, v):\n    return {**inputs, 'house': {}}\n\n"
    "async def run(ctx, inputs):\n    part = await ctx.render('model.scad', **inputs['params'])\n"
    "    await ctx.output(plates=await ctx.pack([part]), bom=[{'piece': 'p', 'label': 'P', 'count': 1}],"
    " files={'a.txt': 'hi'})\n"
)


def with_pipeline(paths: DataPaths, slug: str, source: str = PIPELINE) -> None:
    directory = paths.model_dir(slug)
    (directory / "pipeline").mkdir(exist_ok=True)
    (directory / "pipeline" / "pipeline.py").write_text(source, encoding="utf-8")
    meta = json.loads(paths.model_meta(slug).read_text(encoding="utf-8"))
    meta["pipeline"] = {"module": "pipeline/pipeline.py", "api": 1}
    paths.model_meta(slug).write_text(json.dumps(meta), encoding="utf-8")


def test_migrate_upgrades_old_inputs(client: TestClient, model: str, paths: DataPaths) -> None:
    with_pipeline(paths, model)
    response = client.post(f"/api/v1/models/{model}/inputs/migrate", json={"inputs": {"params": {}, "v": 0}})
    assert response.status_code == 200, response.text
    assert response.json() == {"inputs": {"params": {}, "house": {}, "v": 1}, "from_version": 0, "to_version": 1}


def test_migrate_refuses_newer_inputs_with_the_reason(client: TestClient, model: str, paths: DataPaths) -> None:
    with_pipeline(paths, model)
    response = client.post(f"/api/v1/models/{model}/inputs/migrate", json={"inputs": {"params": {}, "v": 9}})
    assert response.status_code == 422
    assert "these inputs are v9" in response.json()["detail"]


def test_the_record_carries_the_inputs_version(client: TestClient, model: str, paths: DataPaths) -> None:
    with_pipeline(paths, model)
    assert client.get(f"/api/v1/models/{model}").json()["inputs_version"] == 1
```

The API test app must have a render worker polling whose openscad writes a real 3MF: PR4's Temporal-backed `tests/api/conftest.py` `client` fixture (Base), whose in-process worker runs through `_poll` and so already registers `PipelineActivities` (Task 4 Step 8).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_migrate_inputs.py -q`
Expected: collection error `ImportError: cannot import name 'MigrateRequest'`.

- [ ] **Step 3: Payloads, activity, workflow**

In `workflows/models.py`:

```python
class MigrateRequest(BaseModel):
    slug: str
    revision: str | None
    inputs: dict[str, Any]


class MigrateResult(BaseModel):
    inputs: dict[str, Any]
    from_version: int
    to_version: int
```

In `PipelineActivities` (add it to `all()`):

```python
    @activity.defn(name="migrate_inputs")
    async def migrate_inputs(self, req: MigrateRequest) -> MigrateResult:
        """The template's `migrate`, in the template process (§8.2): it runs template code,
        so it runs here on the worker, never in the API (§9)."""
        d = self.deps
        model_dir = await self.model_dir(req.slug, req.revision)
        with tempfile.TemporaryDirectory(prefix="scadbuddy-migrate-") as out:
            try:
                migrated = await run_template(
                    model_dir, {"mode": "migrate", "inputs": req.inputs}, out=Path(out), out_key="",
                    python=d.template_python, data_dir=d.config.data_dir, timeout=MIGRATE_SECONDS,
                )
            except TemplateError as error:
                raise ApplicationError(str(error), type="MigrateError", non_retryable=True) from None
        return MigrateResult(inputs=migrated, from_version=int(req.inputs.get("v", 0)), to_version=int(migrated.get("v", 0)))
```

`MIGRATE_SECONDS = 30.0` is a module constant: shorter than the activity's `SHORT` (60 s) `start_to_close`, so the subprocess's own kill fires first, as `Ctx.activity`'s +30 s does. `import tempfile`.

In `workflows/pipelines.py`:

```python
@workflow.defn(name="MigrateInputs")
class MigrateInputs:
    @workflow.run
    async def run(self, req: MigrateRequest) -> MigrateResult:
        return await workflow.execute_activity(
            "migrate_inputs", req, result_type=MigrateResult, start_to_close_timeout=SHORT,
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
```

Add `MigrateInputs` to `render_worker`'s `workflows=[…]`.

- [ ] **Step 4: `RenderService.migrate_inputs` and the route**

In `render/submit.py`, in the style of `render_preview`, and with the same client and task-queue attributes that method uses:

```python
    async def migrate_inputs(self, slug: str, inputs: Mapping[str, Any], *, version: str | None) -> MigrateResult:
        try:
            return await self.client.execute_workflow(
                MigrateInputs.run,
                MigrateRequest(slug=slug, revision=version, inputs=dict(inputs)),
                id=f"migrate-{uuid.uuid4().hex}",
                task_queue=self.task_queue,
                execution_timeout=timedelta(seconds=120),
            )
        except WorkflowFailureError as error:
            cause = error.cause
            while cause is not None and not isinstance(cause, ApplicationError):
                cause = cause.__cause__
            raise InputsError(cause.message if cause is not None else str(error)) from None
```

In `api/models.py`:

```python
class MigrateInputsRequest(BaseModel):
    inputs: dict[str, Any]
    version: str | None = None


@router.post("/models/{slug}/inputs/migrate", response_model=MigrateResult, summary="Migrate saved inputs")
async def migrate_inputs(
    slug: SlugPath, body: MigrateInputsRequest, render: RenderDep, catalogue: CatalogueDep
) -> MigrateResult:
    """Bring saved inputs up to the template's `INPUTS_VERSION` (§8.2)."""
    require_model(catalogue, slug)  # 404 for an unknown template, as `get_model` does
    try:
        return await render.migrate_inputs(slug, body.inputs, version=body.version)
    except InputsError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
```

`SlugPath`, `CatalogueDep` and `require_model` are what `api/models.py`'s `get_model` already uses; `RenderDep` is PR4's (Base).

In `agent/src/tools/coverage.ts`, add a `NOT_A_TOOL` entry for `POST /api/v1/models/{slug}/inputs/migrate`, with the reason "the host migrates inputs as it opens a preset or output; the agent reads inputs already migrated".

- [ ] **Step 5: Run the tests, gates, generated files, commit**

Run: `cd backend && uv run --frozen pytest tests/test_migrate_inputs.py tests/api/test_pipeline_api.py -q`
Expected: all pass.

Then run the backend gates, then `cd frontend && pnpm gen:api && pnpm typecheck`, then `cd agent && pnpm gen:api && pnpm typecheck && pnpm test`. Expected: green, and the agent's coverage test passes.

```bash
git add backend/scadbuddy/ backend/tests/test_migrate_inputs.py backend/tests/api/ agent/src/tools/coverage.ts
git commit -m "feat(pipelines): INPUTS_VERSION and migrate as an activity, behind POST inputs/migrate (#427)"
```

---

### Task 7: Pipeline jobs and outputs through the API

**Files:**
- Modify: `backend/scadbuddy/render/inputs.py` (`inputs_key`), `render/submit.py` (`submit(..., whole_inputs=)`), `api/jobs.py` (`render_model`, `JobStatus.outputs`, `JobOutputSummary`), `library/outputs.py` (`BOM_NAME`, `RECORD_NAME`, `FILES_DIR`, `OutputStore.create(..., index=, files_dir=)`, `OutputStore.bom`, `.record`, `.files`, `.file_path`), `api/outputs.py` (`CreateOutputRequest.index`, `OutputDetail.bom/record/files`, `GET /outputs/{id}/files/{name}`), `agent/src/tools/coverage.ts`
- Test: `backend/tests/api/test_pipeline_api.py` (append)

**Interfaces:**
- Consumes: `Job.outputs`, `PipelineOutput`, `BomEntry`, `OutputRecord`, `FILE_NAME_PATTERN` (Task 3, all in `render/job_models.py`); phase 3's `materialize_result`; phase 2's `normalize_inputs`, `render_key`.
- Produces:
  ```python
  def inputs_key(slug: str, inputs: Mapping[str, Any], model_version: str | None) -> str   # render/inputs.py
  RenderService.submit(..., inputs=..., whole_inputs: bool = False)
  class JobOutputSummary(BaseModel): index: int; name: str | None; bom: list[BomEntry]; files: list[str]
  JobStatus.outputs: list[JobOutputSummary] = []
  CreateOutputRequest.index: int = 0
  OutputDetail.bom: list[BomEntry] = []; OutputDetail.record: OutputRecord | None = None; OutputDetail.files: list[str] = []
  GET /api/v1/outputs/{output_id}/files/{name} -> the file (application/octet-stream, or image/svg+xml for .svg with the UI_FILE_HEADERS of phase 2)
  OutputStore.create(job, *, name=None, public_url=None, inputs=None, index: int = 0, files_dir: Path | None = None) -> OutputMeta
  ```

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/api/test_pipeline_api.py`:

```python
import time


def _done(client: TestClient, model: str, inputs: dict[str, object]) -> dict[str, object]:
    accepted = client.post(f"/api/v1/models/{model}/render", json={"inputs": inputs})
    assert accepted.status_code == 202, accepted.text
    url = accepted.json()["status_url"]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        job = client.get(url).json()
        if job["status"] in {"done", "failed"}:
            assert job["status"] == "done", job
            return dict(job)
        time.sleep(0.05)
    raise AssertionError("the render did not finish")


def test_inputs_beyond_params_make_a_different_pipeline_job(
    client: TestClient, model: str, paths: DataPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coalescing joins only a *pending* row, so hold every row pending: refuse the
    workflow starts, as `tests/api/test_temporal_path.py` does."""
    with_pipeline(paths, model)
    service = getattr(client.app.state, STATE_ATTR).render

    async def unavailable(*_: object, **__: object) -> None:
        raise RuntimeError("temporal is down")

    def submit(inputs: dict[str, object]) -> str:
        accepted = client.post(f"/api/v1/models/{model}/render", json={"inputs": inputs})
        assert accepted.status_code == 202, accepted.text
        return str(accepted.json()["job_id"])

    with monkeypatch.context() as patched:
        patched.setattr(service.client, "start_workflow", unavailable)
        one = submit({"params": {}, "v": 1, "house": {"cols": 1}})
        same = submit({"params": {}, "v": 1, "house": {"cols": 1}})
        other = submit({"params": {}, "v": 1, "house": {"cols": 2}})
    assert same == one  # coalescing is on: the test can fail
    assert other != one


def test_a_pipeline_template_takes_params_its_model_scad_lacks(client: TestClient, model: str, paths: DataPaths) -> None:
    with_pipeline(paths, model, PIPELINE.replace("**inputs['params']", ""))
    job = _done(client, model, {"params": {"not_in_model_scad": 1}, "v": 1})
    assert job["outputs"][0]["bom"][0]["piece"] == "p"


def test_an_output_saves_its_bom_record_and_files(client: TestClient, model: str, paths: DataPaths) -> None:
    with_pipeline(paths, model)
    job = _done(client, model, {"params": {}, "v": 1})
    created = client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job["id"], "index": 0})
    assert created.status_code in (200, 201), created.text
    detail = client.get(f"/api/v1/outputs/{created.json()['id']}").json()
    assert detail["bom"] == [{"piece": "p", "label": "P", "count": 1, "plates": [], "part": None}]
    assert detail["files"] == ["a.txt"]
    assert detail["record"]["pipeline_api"] == 1 and len(detail["record"]["pipeline_version"]) == 64
    assert detail["record"]["inputs_v"] == 1
    body = client.get(f"/api/v1/outputs/{created.json()['id']}/files/a.txt")
    assert body.status_code == 200 and body.text == "hi"
    assert client.get(f"/api/v1/outputs/{created.json()['id']}/files/..%2Fmeta.json").status_code == 404


def test_an_output_index_the_job_does_not_have_is_refused(client: TestClient, model: str, paths: DataPaths) -> None:
    with_pipeline(paths, model)
    job = _done(client, model, {"params": {}, "v": 1})
    assert client.post(f"/api/v1/models/{model}/outputs", json={"job_id": job["id"], "index": 3}).status_code == 422
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/api/test_pipeline_api.py -q`
Expected: the four new tests fail. `other == one`, because the two pending rows coalesce on `params`. The `render` call returns 422 for the unknown parameter. `outputs`, `bom` and `index` are missing.

`STATE_ATTR` comes from where `tests/api/test_temporal_path.py` imports it; add `import pytest` beside the file's imports.

- [ ] **Step 3: The job key and validation for pipeline templates**

In `render/inputs.py`:

```python
def inputs_key(slug: str, inputs: Mapping[str, Any], model_version: str | None) -> str:
    """The job key of a pipeline template (§3.4): its whole inputs, which the pipeline
    reads, not only ``params``."""
    raw = json.dumps(["inputs", slug, model_version, dict(inputs)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

In `RenderService.submit`, add the keyword `whole_inputs: bool = False`. Where it computes `render_key(slug, params, model_version)`, use `inputs_key(slug, inputs, model_version) if whole_inputs else render_key(...)`. In `api/jobs.py` `render_model`:
- read the template's meta, as it already does for the schema;
- when `meta.pipeline is not None`, skip `require_valid_params`, because each piece checks its own parameters (Task 3), and pass `whole_inputs=True`;
- otherwise leave it unchanged.

- [ ] **Step 4: Outputs on the job status**

In `api/jobs.py`:

```python
class JobOutputSummary(BaseModel):
    index: int
    name: str | None
    bom: list[BomEntry] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
```

Add `outputs: list[JobOutputSummary] = Field(default_factory=list)` to `JobStatus`, and fill it in `_job_status` as `[JobOutputSummary(index=i, name=o.name, bom=o.bom, files=o.files) for i, o in enumerate(job.outputs)]`.

- [ ] **Step 5: Outputs store the BOM, the record and the files**

In `library/outputs.py`, add `BOM_NAME = "bom.json"`, `RECORD_NAME = "record.json"` and `FILES_DIR = "files"`. `OutputStore.create` gains `index: int = 0` and `files_dir: Path | None = None`. At its top:

```python
        chosen = job.outputs[index] if job.outputs else None
        if job.outputs and not 0 <= index < len(job.outputs):
            raise IndexError(index)
        result = chosen.result if chosen is not None else job.result
```

Use `result` where the method reads `job.result` today (the two `copyfile` lines and the metadata). After the output directory is written:

```python
        if chosen is not None:  # files_dir is the caller's dir_for(chosen.files_key) / "files"
            (directory / BOM_NAME).write_text(json.dumps([b.model_dump(mode="json") for b in chosen.bom]), encoding="utf-8")
            (directory / RECORD_NAME).write_text(chosen.record.model_dump_json(), encoding="utf-8")
            if files_dir is not None and chosen.files:
                shutil.copytree(files_dir, directory / FILES_DIR, dirs_exist_ok=True)
```

Add readers:

```python
    def bom(self, output_id: str) -> list[BomEntry]:
        path = self.directory(output_id) / BOM_NAME
        return [BomEntry.model_validate(e) for e in json.loads(path.read_text(encoding="utf-8"))] if path.is_file() else []

    def record(self, output_id: str) -> OutputRecord | None:
        path = self.directory(output_id) / RECORD_NAME
        return OutputRecord.model_validate_json(path.read_text(encoding="utf-8")) if path.is_file() else None

    def files(self, output_id: str) -> list[str]:
        directory = self.directory(output_id) / FILES_DIR
        return sorted(p.name for p in directory.iterdir() if p.is_file()) if directory.is_dir() else []

    def file_path(self, output_id: str, name: str) -> Path:
        if not re.match(FILE_NAME_PATTERN, name):
            raise OutputNotFoundError(name)
        path = self.directory(output_id) / FILES_DIR / name
        if not path.is_file():
            raise OutputNotFoundError(name)
        return path
```

In `api/outputs.py`:
- `CreateOutputRequest` gains `index: int = Field(default=0, ge=0)`.
- The create route checks `job.outputs and body.index >= len(job.outputs)`, or `not job.outputs and body.index != 0`, and answers 422 `f"job {job.id} has no output {body.index}"`.
- Phase 3's route materializes `job.result` before `create`. Materialize the chosen result instead (`job.outputs[body.index].result` when there are outputs).
- For a chosen output with files, `key = chosen.files_key` (Task 3; always `output_key(job.id, index)`, never a piece), `await state.blobs.fetch(key)` and pass `files_dir=state.blobs.dir_for(key) / "files"`.
- Pass `index=body.index`.

`OutputDetail` gains `bom`, `record` and `files`, filled from the three readers in `_detail`. Add the route:

```python
@router.get("/outputs/{output_id}/files/{name}")
def output_file(output_id: str, name: str, outputs: OutputsDep) -> FileResponse:
    """An extra file a pipeline wrote (§10): served as a download, never as a page."""
    try:
        path = outputs.file_path(output_id, name)
    except OutputNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no file {name!r} on output {output_id}") from None
    return FileResponse(path, filename=name, headers=UI_FILE_HEADERS,
                        media_type="image/svg+xml" if name.endswith(".svg") else "application/octet-stream")
```

`UI_FILE_HEADERS` is phase 2's (`api/template_ui.py`). Use the dependency name `api/outputs.py` already uses for `OutputStore`. Add a `NOT_A_TOOL` entry in `agent/src/tools/coverage.ts` for `GET /api/v1/outputs/{output_id}/files/{name}`, reason "binary download; the agent reads `bom` and `files` from GET /outputs/{id}".

- [ ] **Step 6: Run the tests, gates, generated files, commit**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/api -q`
Expected: all pass, including phase 2's output tests: a job without `outputs` still saves `job.result` at index 0.

Then run the backend gates, `cd frontend && pnpm gen:api && pnpm typecheck && pnpm test`, and `cd agent && pnpm gen:api && pnpm typecheck && pnpm test`.

```bash
git add backend/ agent/src/tools/coverage.ts
git commit -m "feat(api): pipeline jobs key on their inputs; outputs keep BOM, record and files (#427)"
```

---

### Task 8: Frontend: save every output, the BOM table, migrated inputs

**Files:**
- Create: `frontend/src/components/BomTable.tsx`, `frontend/src/components/BomTable.test.tsx`, `frontend/src/components/RawInputs.tsx`, `frontend/src/lib/useMigratedInputs.ts`, `frontend/src/lib/useMigratedInputs.test.tsx`
- Modify: `frontend/src/api/client.ts` (`migrateInputs`, `outputFileUrl`), `frontend/src/lib/saveOutput.ts`, `frontend/src/pages/HistoryPage.tsx`, `frontend/src/pages/CustomizePage.tsx`, `frontend/src/mocks/handlers.ts`, `frontend/src/mocks/fixtures.ts`

**Interfaces:**
- Consumes: the generated `schema.d.ts` types `JobOutputSummary`, `BomEntry`, `OutputRecord`, `MigrateResult` (Task 7 and Task 6); phase 2's `saveOutput.ts` and `api.createOutput`; `ModelRecord.inputs_version`.
- Produces:
  ```ts
  // CHANGED phase 2 signature: a trailing index (phase 2 plan Task 5 records the same line)
  api.createOutput(slug: string, jobId: string, name?: string, inputs?: JsonObject, index?: number)
  api.migrateInputs(slug: string, inputs: JsonObject, version?: string): Promise<MigrateResult>
  api.outputFileUrl(outputId: string, name: string): string
  export function BomTable(props: { bom: BomEntry[] }): JSX.Element | null
  export function RawInputs(props: { inputs: JsonObject; error: string }): JSX.Element
  export type MigrateOutcome = { kind: 'ready'; inputs: JsonObject } | { kind: 'failed'; inputs: JsonObject; error: string }
  export async function migrateIfOld(slug: string, inputs: JsonObject, current: number, version?: string): Promise<MigrateOutcome>
  ```

- [ ] **Step 1: Write the failing tests**

`frontend/src/components/BomTable.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { BomTable } from './BomTable'

describe('BomTable', () => {
  it('lists every piece with its count and plates', () => {
    render(<BomTable bom={[
      { piece: 'wall', label: 'Wall', count: 8, plates: [1, 2], part: 'k1' },
      { piece: 'floor', label: 'Floor tile', count: 2, plates: [], part: null },
    ]} />)
    const rows = screen.getAllByRole('row')
    expect(rows).toHaveLength(3)
    expect(rows[1]).toHaveTextContent('Wall')
    expect(rows[1]).toHaveTextContent('8')
    expect(rows[1]).toHaveTextContent('1, 2')
  })

  it('renders nothing for an empty bill', () => {
    const { container } = render(<BomTable bom={[]} />)
    expect(container).toBeEmptyDOMElement()
  })
})
```

`frontend/src/lib/useMigratedInputs.test.tsx`:

```tsx
import { http, HttpResponse } from 'msw'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { RawInputs } from '../components/RawInputs'
import { migrateIfOld } from './useMigratedInputs'

describe('migrateIfOld', () => {
  it('leaves current inputs alone without a request', async () => {
    const outcome = await migrateIfOld('demo', { params: {}, v: 2 }, 2)
    expect(outcome).toEqual({ kind: 'ready', inputs: { params: {}, v: 2 } })
  })

  it('migrates old inputs through the API', async () => {
    server.use(http.post('/api/v1/models/demo/inputs/migrate', () =>
      HttpResponse.json({ inputs: { params: {}, v: 2, house: {} }, from_version: 0, to_version: 2 })))
    const outcome = await migrateIfOld('demo', { params: {}, v: 0 }, 2)
    expect(outcome).toEqual({ kind: 'ready', inputs: { params: {}, v: 2, house: {} } })
  })

  it('shows raw inputs read-only when migration fails', async () => {
    server.use(http.post('/api/v1/models/demo/inputs/migrate', () =>
      HttpResponse.json({ detail: 'these inputs are v5; the template\'s INPUTS_VERSION is 2' }, { status: 422 })))
    const outcome = await migrateIfOld('demo', { params: {}, v: 5 }, 2)
    expect(outcome.kind).toBe('failed')
    if (outcome.kind !== 'failed') return
    render(<RawInputs inputs={outcome.inputs} error={outcome.error} />)
    expect(screen.getByText(/these inputs are v5/)).toBeInTheDocument()
    expect(screen.getByRole('textbox')).toHaveAttribute('readonly')
    expect((screen.getByRole('textbox') as HTMLTextAreaElement).value).toContain('"v": 5')
  })
})
```

An inputs `v` newer than the template also goes to the API. The API refuses it, and that refusal is the error the user sees. If the msw server export in `src/mocks/` has another name, import that one.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/components/BomTable.test.tsx src/lib/useMigratedInputs.test.tsx`
Expected: FAIL, with modules not found.

- [ ] **Step 3: Implement**

`frontend/src/components/BomTable.tsx`:

```tsx
import type { components } from '../api/schema'

type BomEntry = components['schemas']['BomEntry']

export function BomTable({ bom }: { bom: BomEntry[] }) {
  if (bom.length === 0) return null
  return (
    <table className="bom-table">
      <thead><tr><th>Piece</th><th>Count</th><th>Plates</th></tr></thead>
      <tbody>
        {bom.map((entry) => (
          <tr key={entry.piece}>
            <td>{entry.label}</td>
            <td>{entry.count}</td>
            <td>{(entry.plates ?? []).join(', ')}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
```

`frontend/src/components/RawInputs.tsx`:

```tsx
import type { JsonObject } from '../lib/inputs'

export function RawInputs({ inputs, error }: { inputs: JsonObject; error: string }) {
  return (
    <div role="alert" className="raw-inputs">
      <p>These saved inputs could not be brought up to this template version: {error}</p>
      <textarea readOnly rows={12} value={JSON.stringify(inputs, null, 2)} aria-label="Saved inputs" />
    </div>
  )
}
```

`frontend/src/lib/useMigratedInputs.ts`:

```ts
import { api } from '../api/client'
import type { JsonObject } from './inputs'

export type MigrateOutcome =
  | { kind: 'ready'; inputs: JsonObject }
  | { kind: 'failed'; inputs: JsonObject; error: string }

/** Saved inputs, brought up to the template's INPUTS_VERSION (spec §8.2). */
export async function migrateIfOld(slug: string, inputs: JsonObject, current: number, version?: string): Promise<MigrateOutcome> {
  const v = typeof inputs.v === 'number' ? inputs.v : 0
  if (v === current) return { kind: 'ready', inputs }
  try {
    const result = await api.migrateInputs(slug, inputs, version)
    return { kind: 'ready', inputs: result.inputs as JsonObject }
  } catch (error) {
    return { kind: 'failed', inputs, error: error instanceof Error ? error.message : String(error) }
  }
}
```

In `api/client.ts`, give phase 2's `createOutput(slug, jobId, name?, inputs?)` a trailing `index?: number`, sent as `index` in the body only when defined (so every existing call is unchanged). Then add these beside it, using its request helper, whose `ApiError` carries `detail` as `message`:
- `migrateInputs(slug, inputs, version)` posts `{ inputs, version }` to `/models/${slug}/inputs/migrate`;
- `outputFileUrl(id, name)` returns `` `/api/v1/outputs/${id}/files/${encodeURIComponent(name)}` ``.

In `lib/saveOutput.ts`, after phase 2's `createOutput` for the job, create the remaining outputs of a job whose `outputs.length > 1`: `for (let index = 1; index < job.outputs.length; index++) await api.createOutput(slug, job.id, undefined, inputs, index)`. The promise still resolves with the first output's id.

In `pages/HistoryPage.tsx`, where an output's detail shows its name and downloads, render `<BomTable bom={output.bom ?? []} />`. Below it, render one link per `output.files` entry with `href={api.outputFileUrl(output.id, name)}` and `download`.

In `pages/CustomizePage.tsx`, phase 2 applies an output's or a preset's `inputs` when it opens one (`joinInputs`). Before applying them, `await migrateIfOld(slug, inputs, model.inputs_version, version)`:
- on `ready`, apply the migrated inputs;
- on `failed`, keep the current state and render `<RawInputs inputs={…} error={…} />` above the panel until the user loads something else.

Extend `mocks/handlers.ts` and `mocks/fixtures.ts`: a default `migrate` handler that echoes `{inputs, from_version: v, to_version: v}`, `inputs_version: 0` on model fixtures, and `bom: [], files: [], record: null` on output fixtures.

- [ ] **Step 4: Run the tests and gates**

Run: `cd frontend && pnpm gen:api && pnpm lint && pnpm typecheck && pnpm test && pnpm build`
Expected: green.

- [ ] **Step 5: Commit**

```bash
git add frontend/src
git commit -m "feat(frontend): save every pipeline output, show its BOM and files, migrate old inputs (#427)"
```

---

### Task 9: `dollhouse-kit` builds a whole house

The house is phase 2's `inputs.house` (`cols`, `rows`, `storeys`, `windows`) with the style in `inputs.params`. The counting rules are phase 2's `housePieces`, ported to Python so the pipeline can run them in the sandbox.

**Files:**
- Create: `models/dollhouse-kit/pipeline/pipeline.py`, `models/dollhouse-kit/pipeline/activities.py`, `backend/tests/test_dollhouse_pipeline.py`
- Modify: `models/dollhouse-kit/model.json` (`pipeline`), `models/dollhouse-kit/README.md` (one paragraph)

**Interfaces:**
- Consumes: `ctx.render/activity/pack/output/progress` (Tasks 4–5); `FakeWorld`, `run_job`, `a_job` (Task 4).
- Produces (`pipeline/pipeline.py`): `INPUTS_VERSION = 1`, `DEFAULT_HOUSE`, `LIMITS`, `clamp_house(house) -> dict`, `house_pieces(house) -> list[dict]` (entries `{id, piece, course, count, label}`, phase 2's `housePieces` ids: `wall:lower`, `wall:upper`, then each piece's own name; `corner_post` one entry of 8 per storey; `course` set only on walls), `piece_params(entry, style) -> dict`, `migrate(inputs, from_version)`, `async run(ctx, inputs)`. `pipeline/activities.py`: `assembly_guide(house, entries) -> str` (SVG).

- [ ] **Step 1: Write the failing tests**

`backend/tests/test_dollhouse_pipeline.py`:

```python
"""dollhouse-kit's whole-house pipeline (spec 2026-09-27 §5.4; epic #427 "Done when")."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from tests.support.pipelines import FakeWorld, a_job, run_job
from tests.support.temporal import temporal_client

TEMPLATE = Path(__file__).resolve().parents[2] / "models" / "dollhouse-kit"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dollhouse_pipeline", TEMPLATE / "pipeline" / "pipeline.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_labels_match_the_designer() -> None:
    labels = {e["id"]: e["label"] for e in _module().house_pieces({"cols": 1, "rows": 1, "storeys": 2, "windows": 1})}
    assert labels["wall:lower"] == "Wall, lower course"
    assert labels["wall_door_lower"] == "Door wall, lower course"
    assert labels["connectors"] == "Connectors (keys, pegs, hinge pins)"


def test_a_two_by_one_house_counts_like_the_designer() -> None:
    counts = {e["id"]: e["count"] for e in _module().house_pieces({"cols": 2, "rows": 1, "storeys": 1, "windows": 2})}
    assert counts == {
        "wall_door_lower": 1, "wall:lower": 5, "wall_door_upper": 1, "wall_window": 2, "wall:upper": 3,
        "corner_post": 8, "floor_tile": 2, "roof_panel": 2,
        "door_leaf_lower": 1, "door_leaf_upper": 1, "connectors": 1,
    }


@pytest.mark.parametrize(("value", "expected"), [(None, 1), ("", 1), ("3", 3), ("x", 2)])
def test_a_null_or_empty_value_counts_as_zero_like_the_designer(value: object, expected: int) -> None:
    # clampHouse: Number(null) and Number("") are 0 (clamped to 1); Number("x") is NaN (the default).
    assert _module().clamp_house({"cols": value})["cols"] == expected


def test_the_house_is_clamped_to_the_designer_limits() -> None:
    assert _module().clamp_house({"cols": 9, "rows": 0, "storeys": 2, "windows": -1}) == {
        "cols": 4, "rows": 1, "storeys": 2, "windows": 0,
    }


def test_v0_inputs_migrate_to_a_default_house() -> None:
    m = _module()
    assert m.migrate({"params": {"wallpaper": "stars"}, "v": 0}, 0) == {
        "params": {"wallpaper": "stars"}, "v": 0, "house": m.DEFAULT_HOUSE,
    }


@pytest.mark.requires_temporal
async def test_a_house_is_one_output_with_a_bom_and_a_guide() -> None:
    world = FakeWorld((TEMPLATE / "pipeline" / "pipeline.py").read_text(), activities_py=TEMPLATE / "pipeline" / "activities.py")
    job = a_job(params={"wallpaper": "stripes"}, house={"cols": 2, "rows": 1, "storeys": 1, "windows": 2}, v=1)
    async with temporal_client() as client:
        await run_job(world, job, client=client)
    assert world.final().state == "done", world.final().failure
    assert len({p.piece_key for p in world.pieces}) == 11
    [out] = world.outputs
    assert sum(b.count for b in out.bom) == 27
    assert "assembly.svg" in out.files
    assert sum(len(p.items) for p in out.layout.plates) == 27


@pytest.mark.requires_temporal
async def test_changing_wallpaper_rerenders_walls_but_not_floors() -> None:
    source = (TEMPLATE / "pipeline" / "pipeline.py").read_text()
    keys: dict[str, dict[str, str]] = {}
    for wallpaper in ("stripes", "stars"):
        world = FakeWorld(source, activities_py=TEMPLATE / "pipeline" / "activities.py")
        job = a_job(params={"wallpaper": wallpaper}, house={"cols": 1, "rows": 1, "storeys": 1, "windows": 1}, v=1)
        async with temporal_client() as client:
            await run_job(world, job, client=client)
        keys[wallpaper] = {
            p.params["piece"] + (f":{p.params['course']}" if "course" in p.params else ""): p.piece_key
            for p in world.pieces
        }
    before, after = keys["stripes"], keys["stars"]
    assert before["wall:lower"] != after["wall:lower"]
    assert before["floor_tile"] == after["floor_tile"]
    assert before["roof_panel"] == after["roof_panel"]
```

The fake `cached_piece` sizes a piece from `w`/`d` params, which a real piece does not have. All 27 copies are 10 × 10 mm, so they pack onto one plate, which is enough for the counts.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_dollhouse_pipeline.py -q`
Expected: FAIL, `FileNotFoundError` for `pipeline/pipeline.py`.

- [ ] **Step 3: Write `models/dollhouse-kit/pipeline/pipeline.py`**

```python
"""dollhouse-kit: the whole house in one Generate (spec 2026-09-27 §5.4).

Deterministic: no I/O, no clock, no randomness. Every render goes through ctx. The
counting rules are the designer's (`ui/pieces.js` `housePieces`); keep them in step."""

import asyncio

INPUTS_VERSION = 1

LIMITS = {"cols": (1, 4), "rows": (1, 3), "storeys": (1, 3), "windows": (0, 14)}
DEFAULT_HOUSE = {"cols": 2, "rows": 1, "storeys": 1, "windows": 2}

LABELS = {
    "wall": "Wall", "wall_window": "Wall with window", "wall_door_lower": "Door wall, lower course",
    "wall_door_upper": "Door wall, upper course", "door_leaf_lower": "Door leaf, lower half",
    "door_leaf_upper": "Door leaf, upper half", "corner_post": "Corner post", "floor_tile": "Floor tile",
    "roof_panel": "Roof panel", "stairs_lower": "Stairs, lower half", "stairs_upper": "Stairs, upper half",
    "railing": "Railing", "connectors": "Connectors (keys, pegs, hinge pins)",
}
#: A piece's key holds only what shapes it (§5.4: new wallpaper re-renders walls, not floors).
WALL_STYLE = {"exterior", "texture_size", "wallpaper", "pattern_repeats", "wainscoting", "wainscot_height",
              "baseboard", "crown_moulding"}
WINDOW = {"window_style", "window_width", "window_height", "window_sill", "window_panes_x", "window_panes_y",
          "glass", "shutters", "flower_box", "trim"}
DOOR = {"door_style", "door_width", "door_height", "door_panels"}
WALLS = {"wall", "wall_window", "wall_door_lower", "wall_door_upper", "corner_post"}
SHAPE = {"piece", "course", "width_units", "depth_units"}


def clamp_house(house):
    """`clampHouse`: a value that is not a number takes the default; the rest is clamped.
    As JS `Number(null)` and `Number("")` are 0, a null or empty value is 0 (then
    clamped to the minimum); a missing key is `undefined`, NaN, the default."""
    out = {}
    for key, (low, high) in LIMITS.items():
        raw = house.get(key, "missing") if isinstance(house, dict) else "missing"
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            raw = 0
        try:
            value = int(float(raw))
        except (TypeError, ValueError, OverflowError):
            out[key] = DEFAULT_HOUSE[key]
            continue
        out[key] = min(high, max(low, value))
    return out


def house_pieces(house):
    """`housePieces`, rule for rule: ids `wall:lower`/`wall:upper`, every other piece by its
    own name; `corner_post` is one entry of 8 per storey; count-0 entries are left out."""
    h = clamp_house(house)
    perimeter = 2 * (h["cols"] + h["rows"])
    entries = {}

    def add(piece, course, count):
        if count <= 0:
            return
        ident = f"wall:{course}" if piece == "wall" else piece
        entry = entries.setdefault(ident, {
            "id": ident, "piece": piece, "course": course if piece == "wall" else None, "count": 0,
            "label": f"{LABELS['wall']}, {course} course" if piece == "wall" else LABELS[piece],
        })
        entry["count"] += count

    for storey in range(h["storeys"]):
        ground = storey == 0
        openings = perimeter - 1 if ground else perimeter
        glazed = min(h["windows"], openings)
        if ground:
            add("wall_door_lower", None, 1)
            add("wall_door_upper", None, 1)
        add("wall", "lower", openings)
        add("wall_window", None, glazed)
        add("wall", "upper", openings - glazed)
        add("corner_post", None, 8)
        add("floor_tile", None, h["cols"] * h["rows"])
    add("roof_panel", None, h["cols"] * h["rows"])
    add("stairs_lower", None, h["storeys"] - 1)
    add("stairs_upper", None, h["storeys"] - 1)
    add("railing", None, h["storeys"] - 1)
    add("door_leaf_lower", None, 1)
    add("door_leaf_upper", None, 1)
    add("connectors", None, 1)
    return list(entries.values())


def piece_params(entry, style):
    """`pieceParams` (piece, course only for a wall, one module), plus the style that
    shapes this piece."""
    piece = entry["piece"]
    keep = {k: v for k, v in style.items() if k not in SHAPE}
    if piece not in WALLS:
        keep = {k: v for k, v in keep.items() if k not in WALL_STYLE}
    if piece != "wall_window":
        keep = {k: v for k, v in keep.items() if k not in WINDOW}
    if not piece.startswith(("wall_door", "door_leaf")):
        keep = {k: v for k, v in keep.items() if k not in DOOR}
    params = {**keep, "piece": piece, "width_units": 1, "depth_units": 1}
    if entry["course"]:
        params["course"] = entry["course"]
    return params


def migrate(inputs, from_version):
    if from_version == 0:
        return {**inputs, "house": clamp_house(inputs.get("house") or DEFAULT_HOUSE)}
    return inputs


async def run(ctx, inputs):
    house = clamp_house(inputs.get("house") or DEFAULT_HOUSE)
    style = dict(inputs.get("params", {}))
    entries = house_pieces(house)
    done = 0

    async def one(entry):
        nonlocal done
        part = await ctx.render("model.scad", **piece_params(entry, style))
        done += 1
        ctx.progress(f"Rendered {entry['label']}", done=done, total=len(entries))
        return part

    parts = await asyncio.gather(*(one(e) for e in entries))
    guide = await ctx.activity("assembly_guide", house, entries)
    layout = await ctx.pack([(part, e["count"]) for e, part in zip(entries, parts)])
    bom = [{"piece": e["id"], "label": e["label"], "count": e["count"], "part": p.piece_key}
           for e, p in zip(entries, parts)]
    storeys = house["storeys"]
    name = f"{house['cols']} x {house['rows']} house, {storeys} storey{'s' if storeys > 1 else ''}"
    await ctx.output(plates=layout, name=name, bom=bom, files={"assembly.svg": guide})
```

`house_pieces` and `clamp_house` follow phase 2's `ui/pieces.js` (`housePieces`, `clampHouse`, `LABELS`) rule for rule, and the ids, counts and labels are pinned against it by the tests above. A change to either file changes both. The loop order differs (the ground storey adds both door walls first, as the JS does), which changes nothing but entry order.

- [ ] **Step 4: Write `models/dollhouse-kit/pipeline/activities.py`**

```python
"""dollhouse-kit's template activities (spec 2026-09-27 §5.2): plain Python run in its
own process on the render worker."""

from xml.sax.saxutils import escape


def assembly_guide(house, entries):
    """An SVG checklist: the footprint grid, then every piece with its count."""
    cell, pad = 40, 20
    width = max(house["cols"] * cell + 2 * pad, 360)
    grid_h = house["rows"] * cell
    lines = [f'<rect x="{pad + c * cell}" y="{pad + r * cell}" width="{cell}" height="{cell}" '
             f'fill="none" stroke="#333"/>' for r in range(house["rows"]) for c in range(house["cols"])]
    y = pad + grid_h + 30
    for entry in entries:
        lines.append(f'<text x="{pad}" y="{y}" font-family="sans-serif" font-size="14">'
                     f'{entry["count"]} x {escape(entry["label"])}</text>')
        y += 20
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{y + pad}">'
            f'<text x="{pad}" y="14" font-family="sans-serif" font-size="14">'
            f'{house["storeys"]} storey(s), {house["cols"]} x {house["rows"]} modules</text>'
            + "".join(lines) + "</svg>")
```

- [ ] **Step 5: Declare it, and say so in the README**

In `models/dollhouse-kit/model.json`, add `"pipeline": {"module": "pipeline/pipeline.py", "api": 1}` beside phase 2's `ui`. Add this paragraph to `README.md`:

"Generate builds the whole house in one go (`pipeline/pipeline.py`). Every piece the designer lists is rendered once and packed onto as many plates as it takes. The result is one 3MF with a bill of materials and an `assembly.svg`. Changing wallpaper re-renders the walls; the floors and the roof come straight from the store."

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/test_dollhouse_pipeline.py -q`
Expected: `10 passed`.

- [ ] **Step 7: Commit**

```bash
git add models/dollhouse-kit backend/tests/test_dollhouse_pipeline.py
git commit -m "feat(models): dollhouse-kit generates a whole house through its pipeline (#427)"
```

---

### Task 10: `verify.sh` runs pipelines; the authoring skill

**Files:**
- Create: `backend/scadbuddy/workflows/verify_pipeline.py`, `models/dollhouse-kit/pipeline/verify-inputs.json`, `backend/tests/test_verify_pipeline.py`
- Modify: `models/dollhouse-kit/verify.sh`, `.github/workflows/ci.yml` (`models` job), `plugins/scadbuddy/skills/authoring/SKILL.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: `RenderActivities`, `PipelineActivities`, `WorkerDeps`, `TemplatePipeline`, `RenderPiece` (Tasks 1–5); `install_fake_openscad` (Task 3, for the test only); the `WorkflowEnvironment.start_local` shape Task 1 measured (`test_the_testing_api_verify_pipeline_uses`).
- Produces: `python -m scadbuddy.workflows.verify_pipeline <template_dir> --inputs <file.json>`. The inputs file holds a JSON list of inputs objects. The command exits 0 when every case ends `done` with each output's 3MF a readable zip, and 1 otherwise, printing one line per case. `async def verify(template: Path, cases: list[dict], *, config: Config) -> list[str]` returns the failures.

- [ ] **Step 1: Write the failing test**

`backend/tests/test_verify_pipeline.py`:

```python
"""verify.sh's pipeline check (spec 2026-09-27 §5.5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scadbuddy.core.paths import DataPaths
from scadbuddy.workflows.verify_pipeline import verify
from tests.support.openscad import install_fake_openscad

pytestmark = pytest.mark.requires_temporal

SOURCE = """\
async def run(ctx, inputs):
    a = await ctx.render("model.scad", width=inputs["params"]["width"])
    await ctx.output(plates=await ctx.pack([(a, inputs["n"])]), name="n")
"""


def _template(tmp_path: Path, source: str) -> Path:
    template = tmp_path / "demo"
    (template / "pipeline").mkdir(parents=True)
    (template / "model.scad").write_text("cube();\n")
    (template / "pipeline" / "pipeline.py").write_text(source)
    (template / "model.json").write_text(json.dumps({"name": "Demo", "pipeline": {"module": "pipeline/pipeline.py", "api": 1}}))
    return template


async def test_every_case_that_renders_passes(tmp_path: Path) -> None:
    config = install_fake_openscad(tmp_path, DataPaths(tmp_path / "unused"))
    failures = await verify(_template(tmp_path, SOURCE), [{"params": {"width": 3}, "n": 1}, {"params": {"width": 3}, "n": 2}], config=config)
    assert failures == []


async def test_a_failing_case_is_reported(tmp_path: Path) -> None:
    config = install_fake_openscad(tmp_path, DataPaths(tmp_path / "unused"))
    failures = await verify(_template(tmp_path, SOURCE), [{"params": {"width": 3}}], config=config)
    assert len(failures) == 1 and "pipeline/pipeline.py:3: KeyError" in failures[0]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && uv run --frozen pytest tests/test_verify_pipeline.py -q`
Expected: `ModuleNotFoundError: No module named 'scadbuddy.workflows.verify_pipeline'`.

- [ ] **Step 3: Implement `workflows/verify_pipeline.py`**

```python
"""`python -m scadbuddy.workflows.verify_pipeline <template> --inputs cases.json`: run a
template's pipeline on a local Temporal dev server with the local store (spec §5.5), for
`verify.sh`. No Postgres: the job row is a list in memory."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import uuid
import zipfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.render.job_models import Job
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.client import pydantic_data_converter  # re-exported from temporalio.contrib.pydantic
from scadbuddy.workflows.models import Projection
from scadbuddy.workflows.pipeline_activities import PipelineActivities
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline


async def verify(template: Path, cases: list[dict[str, Any]], *, config: Config) -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="scadbuddy-verify-") as root:
        paths = DataPaths(Path(root))
        paths.ensure()
        slug = template.name
        shutil.copytree(template, paths.model_dir(slug))
        deps = WorkerDeps(config=replace(config, data_dir=paths.root), paths=paths, assets=AssetStore(paths.assets),
                          blobs=LocalBlobStore(paths.blobs), refs=None, projection=None)  # type: ignore[arg-type]
        finals: dict[str, Projection] = {}

        @activity.defn(name="project")
        async def project(projection: Projection) -> None:
            if projection.state is not None:
                finals[projection.job_id] = projection

        render = RenderActivities(deps)
        acts = [a for a in render.all() if a != render.project] + [project, *PipelineActivities(deps).all()]
        # The pinned `temporal` CLI's dev server, as tests/support/temporal.py starts it:
        # never a download at run time (the test image carries the binary).
        binary = os.environ.get("SCADBUDDY_TEST_TEMPORAL_DEV_SERVER") or shutil.which("temporal")
        if binary is None:
            raise SystemExit(
                "no temporal CLI: set SCADBUDDY_TEST_TEMPORAL_DEV_SERVER or put temporal on PATH"
            )
        # The form Task 1 measured against 1.33.0 (`test_the_testing_api_verify_pipeline_uses`).
        env = await WorkflowEnvironment.start_local(
            dev_server_existing_path=binary, data_converter=pydantic_data_converter
        )
        try:
            async with Worker(env.client, task_queue="verify", workflows=[TemplatePipeline, RenderPiece], activities=acts):
                for number, inputs in enumerate(cases, start=1):
                    job = Job(id=uuid.uuid4().hex, slug=slug, params=inputs.get("params", {}), inputs=inputs,
                              created_at=datetime.now(UTC))
                    await env.client.execute_workflow(TemplatePipeline.run, job, id=f"render-{job.id}", task_queue="verify")
                    final = finals.get(job.id)
                    if final is None or final.state != "done":
                        error = final.failure.error if final and final.failure else "no final state"
                        failures.append(f"case {number}: {error}")
                        continue
                    for output in final.outputs:
                        try:
                            zipfile.ZipFile(paths.root / output.result.model_3mf).testzip()
                        except (OSError, zipfile.BadZipFile) as error:
                            failures.append(f"case {number}: {output.name}: {error}")
        finally:
            await env.shutdown()
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m scadbuddy.workflows.verify_pipeline")
    parser.add_argument("template", type=Path)
    parser.add_argument("--inputs", type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.inputs.read_text(encoding="utf-8"))
    failures = asyncio.run(verify(args.template.resolve(), cases, config=Config(data_dir=Path("/unused"))))
    for line in failures:
        print(f"FAIL {line}")
    print(f"{len(cases) - len(failures)} of {len(cases)} pipeline cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
```

`Config()`'s default `openscad` is the binary on `PATH`, which is right in the `test` image. The test passes the fake. Check that `render.all()` returns bound methods, so that `a != render.project` compares correctly, and that `verify`'s KeyError message carries `pipeline/pipeline.py:3`. The dev-server calls are the shape Task 1 pinned in `test_the_testing_api_verify_pipeline_uses`; nothing about them is left to find out here. The pipelines wait on real openscad activities, not timers, so time skipping would buy nothing; the Dockerfile's `test` stage already sets `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` to its pinned CLI.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && uv run --frozen pytest tests/test_verify_pipeline.py -q`
Expected: `2 passed`, with no network access (the dev server is the local `temporal` CLI).

- [ ] **Step 5: dollhouse-kit's `verify.sh` runs its pipeline**

`models/dollhouse-kit/pipeline/verify-inputs.json`:

```json
[
  {"v": 1, "params": {"wallpaper": "stripes"}, "house": {"cols": 1, "rows": 1, "storeys": 1, "windows": 1}},
  {"v": 1, "params": {"wallpaper": "stars", "exterior": "brick"}, "house": {"cols": 2, "rows": 1, "storeys": 2, "windows": 3}}
]
```

Append to `models/dollhouse-kit/verify.sh`, before its final summary:

```bash
# The whole-house pipeline (spec 2026-09-27 §5.5), in the image that has the backend:
# the OpenSCAD image has no Python. CI sets SCADBUDDY_PIPELINE_IMAGE to the test image.
PIPELINE_IMAGE="${SCADBUDDY_PIPELINE_IMAGE:-}"
if [ -n "$PIPELINE_IMAGE" ]; then
    echo "==> pipeline: $(jq length pipeline/verify-inputs.json) house cases in $PIPELINE_IMAGE"
    docker run --rm --label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}" -v "$PWD:/template/dollhouse-kit:ro" "$PIPELINE_IMAGE" uv run --frozen python -m scadbuddy.workflows.verify_pipeline /template/dollhouse-kit --inputs /template/dollhouse-kit/pipeline/verify-inputs.json
else
    echo "==> SCADBUDDY_PIPELINE_IMAGE unset: pipeline check skipped (docker build --target test -t scadbuddy:test . and set it to run)"
fi
```

If `jq` is not already used in the script, replace `$(jq length …)` with plain text. In `.github/workflows/ci.yml`'s `models` job, beside the step that builds `scadbuddy-verify:ci`, add a build of `--target test` tagged `scadbuddy:test`, with the same buildx cache settings that step uses. Set `SCADBUDDY_PIPELINE_IMAGE: scadbuddy:test` in the environment of the step that runs `verify-models.sh`.

Run:
- `shellcheck models/dollhouse-kit/verify.sh && bash .github/scripts/lint-verify-labels.sh`. Expected: clean.
- `actionlint`. Expected: clean.

- [ ] **Step 6: The authoring skill and CLAUDE.md**

Add to `plugins/scadbuddy/skills/authoring/SKILL.md`, after section 8, and renumber section 9 ("Things to treat as untrusted") to 10:

```markdown
## 9. Pipelines: `pipeline/pipeline.py` and `pipeline/activities.py`

Sources: `docs/superpowers/specs/2026-09-27-template-pipelines-design.md` §5 (the
contract), §8.2 (inputs versions), §9 (trust); `backend/scadbuddy/workflows/ctx.py`
(`Ctx`); `backend/scadbuddy/template.py` (`Blob`, `Part`, `emit`);
`models/dollhouse-kit/pipeline/` (a worked example).

A template with no `pipeline` renders `model.scad` with its parameters, as always.
Declare one in `model.json`: `"pipeline": {"module": "pipeline/pipeline.py", "api": 1}`.

`pipeline.py` defines `async def run(ctx, inputs)` and runs inside Temporal's
workflow sandbox, so it must be deterministic: no files, no network, no clock, no
randomness. A forbidden call fails the job with `pipeline/pipeline.py:<line>` in
its error. Two more rules the sandbox does not catch for you:

- **Yield within 2 s.** Between two `await ctx.…` calls, `run` must not compute
  for more than 2 seconds; Temporal then reports a deadlock and retries forever,
  and only the pipeline's overall bound (4 × `SCADBUDDY_TEMPLATE_ACTIVITY_MAX_TIMEOUT`)
  fails the job. Put heavy computation in `activities.py`.
- **Never iterate a `set` to decide what to call.** String hashing differs between
  worker processes, so a set's order does too; a replay on another worker would
  issue the calls in another order and fail. Use a list, `dict`, or `sorted(...)`.
  Sets for membership tests are fine.

Everything else goes through `ctx`:

| Call | Does | Returns |
| --- | --- | --- |
| `await ctx.render(file, **params)` | renders any `.scad` of the template; identical calls render once | `Part` (a reference: `bbox`, `colours`, `notes`, `plates`) |
| `await ctx.activity(name, *args, timeout=None, **kwargs)` | runs `pipeline/activities.py:<name>` in its own process | its JSON result |
| `await ctx.pack([part, (part, count)])` | packs parts onto plates (goal `fewest_plates`) | layout |
| `ctx.plate_of(parts, at=[(x, y, 0)])` | one plate, placed by you | plate |
| `await ctx.output(plates=…, name=…, bom=[…], files={…})` | writes a 3MF; call it again for another | `OutputRef` |
| `ctx.progress(message, done=, total=)` | the job's progress line | — |

One part packed alone keeps the plates it laid out itself (`echo(plates = N)`, section 5).
`bom` entries are `{"piece", "label", "count", "plates": [..], "part": part.piece_key}`.
`files` values are text, or a `Blob` returned by an activity.

`activities.py` is plain Python on the render worker, with what the image ships
(numpy, lxml, Pillow, the stdlib, `scadbuddy.render.*`, `scadbuddy.template`).
Nothing is installed per template. Arguments and results are JSON. A `Part`
argument gives `part.meshes()`, and a `Blob` argument gives `blob.read_bytes()`.
Return `scadbuddy.template.emit(name, data)` for a file. The function runs in its
own process group, with no ScadBuddy secrets in its environment, and is killed with
its children on cancellation or timeout (default the render timeout plus 60 s, at
most `SCADBUDDY_TEMPLATE_ACTIVITY_MAX_TIMEOUT`). An exception fails the job with
`pipeline/activities.py:<line>`.

Inputs versions: set `INPUTS_VERSION = n` in `pipeline.py` and have your UI stamp
`inputs.v = n`. When you change the inputs' shape, raise it, and define
`migrate(inputs, from_version) -> inputs`. It runs when an older preset or output
opens. If it raises, the user sees the raw inputs, read-only, with your error.

`verify.sh`: list inputs cases in `pipeline/verify-inputs.json`, and run
`python -m scadbuddy.workflows.verify_pipeline <template> --inputs …` in the
`test` image, as `models/dollhouse-kit/verify.sh` does.
```

In `CLAUDE.md`'s Layout section, add after the `backend/scadbuddy/render/` bullet:

```markdown
- `backend/scadbuddy/workflows/` — Temporal: `pipelines.py` (`TemplatePipeline` runs a
  template's `pipeline/pipeline.py`, or the built-in default, `exec`'d in the workflow
  sandbox; `RenderPiece`; `MigrateInputs`), `ctx.py` (the `ctx` a pipeline gets),
  `activities.py` (render stages, `project`), `pipeline_activities.py` (`load_pipeline`,
  `pack`, `write_output`, `run_template_activity`, `migrate_inputs`),
  `template_process.py`/`template_runner.py` (template Python in its own process group,
  env allowlisted), `verify_pipeline.py` (for `verify.sh`). `scadbuddy/template.py` is
  the surface a template's `activities.py` imports.
```

Run: `bash .github/scripts/lint-plugin.sh && claude plugin validate plugins/scadbuddy`
Expected: clean. If the `claude` CLI is not installed, run only `lint-plugin.sh`.

- [ ] **Step 7: Gates and commit**

Run the backend gates, then `docker build --target test -t scadbuddy:test . && SCADBUDDY_PIPELINE_IMAGE=scadbuddy:test bash models/dollhouse-kit/verify.sh`.
Expected: the existing checks pass, and then `2 of 2 pipeline cases passed`.

```bash
git add backend/scadbuddy/workflows/verify_pipeline.py backend/tests/test_verify_pipeline.py \
  models/dollhouse-kit .github/workflows/ci.yml plugins/scadbuddy/skills/authoring/SKILL.md CLAUDE.md
git commit -m "feat(models): verify.sh runs template pipelines; authoring skill covers pipelines (#427)"
```

---

## Disagreements between the spec, the brief and the code (resolved here)

1. **Epic number.** The brief named #426 as the pipelines epic. #426 is phase 3 (the store), #427 is phase 4 (template pipelines), and #428 is phase 5 (Arrange). This plan is #427.
2. **`ctx.pack` is Arrange's activity (§5.2), but Arrange is phase 5.** Phase 4 ships a shelf packer that implements only the `fewest_plates` goal:
   - any other goal, or a `filament_plan`, fails with a message naming phase 5;
   - a part packed alone keeps its own `plates = N` layout (§5.3);
   - rotation in `plate_of` is refused.
3. **`ctx.output` "records the output row" (§5.2).** In the code, the preview render is automatic, and Generate saves a finished job as an output (phase 2, Disagreement 3). Pipeline outputs therefore ride on the job (`Job.outputs`, a new `render_jobs.outputs` column), and Generate saves each one (`CreateOutputRequest.index`).
4. **The dollhouse inputs and roof.** §5.4 shows a `rooms` grid, `parts/roof.scad` and a `split_to_fit` activity. Phase 2 built the designer on `inputs.house = {cols, rows, storeys, windows}`, and `model.scad` already has a `roof_panel` piece. The pipeline keeps phase 2's shape and pieces. Its template activity is an assembly guide; there is no roof split. `parts/<file>.scad` rendering is still implemented and tested (Task 3). The piece list is phase 2's `housePieces` exactly (ids `wall:lower`/`wall:upper`, `corner_post` as one entry of 8 per storey at `model.scad`'s default course), so the BOM matches what the designer shows.
5. **`piece_finished` held one outcome** (phase 1 `_outcome`). A pipeline waits on many pieces at once, so outcomes are keyed by `PieceOutcome.piece_key`. This field is additive, and phase-1 workflows stay PINNED to their build.
6. **Job key.** Phase 2 kept `render_key` over `params`. For a pipeline template, keys outside `params` change the result, so such jobs key on the whole inputs (`inputs_key`, Task 7). Templates without a pipeline are unchanged.
7. **`require_valid_params`.** Phase 2, Disagreement 5, deferred its move here. For a pipeline template the API no longer checks `inputs.params` against `model.scad`. Every piece checks its own file's parameters in `prepare`, which fails with today's message.
8. **URL-import confirmation (§9).** Imports are still single-file (phase 2, Disagreement 8), so no import can bring `pipeline/`. This plan defers it again, to the change that makes imports multi-file.
9. **"`verify.sh` can run a template's pipeline against `temporalio.testing` … in the `base` image" (§5.5).** Two deviations. The base image has no Python (CLAUDE.md), so the pipeline check runs in the Dockerfile's `test` image (`SCADBUDDY_PIPELINE_IMAGE`) and the rendering checks stay in `base`. And it uses `WorkflowEnvironment.start_local` over the image's pinned `temporal` CLI (`SCADBUDDY_TEST_TEMPORAL_DEV_SERVER`), not `start_time_skipping`, which downloads an unpinned Java test server at run time and gains nothing when the waits are real activities. The spec's §5.5 note should say "a local Temporal dev server (the pinned `temporal` CLI)"; the controller amends the spec.
10. **`files: dict[str, bytes | Blob]` (§5.2).** Payloads are JSON, so `bytes` must be UTF-8 text. Binary files come from a template activity as a `Blob` (`scadbuddy.template.emit`).
11. **`ctx.plate` "the selected/default printer's bed (#81)".** Generate has no printer selected, so phase 4 uses the default plate (`plate_for(None)`). `PlateSize.key` still lands on the record.
12. **`ctx.render(self, file, **params)`.** This plan makes `file` positional-only, so that a template parameter called `file` (or `name`) still reaches the render (Review Focus 1).
13. **`run_template_activity(slug, revision, name, args)`.** This plan uses one `TemplateCall` payload (plus `kwargs` and `timeout_s`), because the SDK passes one pydantic argument most simply.
14. **`manifest` on `GET /outputs/{id}` (§10)** is phase 5 (§11 item 5) and is not added here.

## Self-review notes

- **Spec coverage.**
  - §3.4 step 1 (`load_pipeline`, source in history, sha as `pipeline_version`) → Tasks 2 and 4 (`test_an_edit_mid_run…`).
  - §3.4 step 2 (sandbox, file:line, `RestrictedWorkflowAccessError`) → Tasks 1 and 4.
  - §3.6's two open items → Task 1 measures them and edits the spec.
  - §5.1 declaration → Task 2.
  - §5.2:
    - `render` → Tasks 3–4;
    - `activity`, process group, timeouts, env → Task 5;
    - `pack`/`plate_of` → Tasks 3–4;
    - `output`, `bom`, `files`, more than one output → Tasks 3, 4 and 7;
    - `progress` → Task 4;
    - `inputs_version`/`plate` → Tasks 2 and 4;
    - the public surface for template activities → Task 5 (`scadbuddy/template.py`) and Task 10 (the skill).
  - §5.3 default pipeline, one code path → Task 4 (`test_the_default_pipeline…` and phase 1's end-to-end test over the real activities).
  - §5.4 → Task 9 (see Disagreement 4).
  - §5.5 → Task 10.
  - §8.1 pipeline majors → Task 2.
  - §8.2 → Tasks 5 (runner mode), 6 (activity, workflow, API) and 8 (UI fallback).
  - §8.3 → Task 4.
  - §8.4 → Tasks 3, 4 and 7 (`OutputRecord`, saved as `record.json`).
  - §9 (env allowlist, process group, template code only on the worker; `migrate` also on the worker) → Tasks 5 and 6. The URL-import confirmation is deferred (Disagreement 8).
  - §10 (`bom`, `record`, `files` on outputs; the migrate route) → Tasks 6–7. `manifest` is deferred (Disagreement 14).
  - §12: #289 → Task 3 (`Layout.own`); #252 → Task 10 (the skill); #174 → deferred.
  - The epic's "Done when":
    - 2-room house → one multi-plate 3MF plus SVG plus BOM (Task 9);
    - wallpaper → walls only (Task 9);
    - a killed worker costs one piece, and an edit mid-run does not matter (Task 4);
    - an old preset opens through `migrate` (Tasks 6 and 8).
- **§6 and §7.** §6 (the store): output blobs are published as internal `Work/` blobs (default `folder="work"`, `title=template_title(...)`; the user-facing file is Generate's save) and template-activity files under `BlobScope(slug=…)` through phase 3's `publish`; every piece and output a job reads is refed on `done` (Task 4 Step 7), so the §6.2 grace sweep leaves them alone. Nothing else in §6 is phase 4's. §7 (Arrange) is phase 5; phase 4's `pack` is its declared stand-in (Disagreement 2), with the same `PackRequest` shape Arrange's packing activity can take over.
- **Placeholder scan.** No TBD or TODO. Some steps edit phase 1/2/3 code this plan cannot quote verbatim: `render_model`, the output create route, `RenderService`'s client attributes, `CustomizePage`'s inputs-apply site, the `models` CI job. Those steps name the exact site and give the replacement code. Task 1 Step 6 pastes measured strings, which is the point of that task.
- **Type consistency.**
  - `Part`/`Blob` are defined once, in `scadbuddy/template.py`, and re-exported by `workflows/models.py`.
  - `Layout.own`/`Layout.plates`/`Placed` are the same in `packing.py`, `outputs.py`, `ctx.py` and the fakes.
  - `PipelineOutput.blob_keys` is a list everywhere, and `Projection.blob_keys` is consumed by `project`.
  - `OutputRecord` fields match between `Ctx.output` and `build_output`.
  - `TemplateCall.timeout_s` is set in `Ctx.activity` and read in `run_template_activity`.
  - `run_template(..., data_dir=, timeout=)` has the same keywords in Tasks 5 and 6.
  - `MigrateRequest`/`MigrateResult` are the same in the activity, the workflow, `RenderService` and the route.
  - `FakeWorld(source, fail=, activities_py=)` is the same in Tasks 4, 5 and 9.
  - `install_fake_openscad(tmp_path, paths)` is the same in Tasks 3 and 10.
- **Review Focus.** All five are pinned:
  1. Task 4, `test_a_parameter_named_file_reaches_the_render`.
  2. Task 4, `test_a_restricted_call_fails_the_job_with_its_line` and `test_a_pipeline_that_writes_nothing_fails`; Task 5, `test_a_pipeline_that_never_yields_times_out` and `test_a_timed_out_pipeline_is_failed_by_the_reconciler`.
  3. Task 4, `test_an_edit_mid_run_does_not_change_the_running_job`.
  4. Task 5, `test_cancelling_kills_the_template_process_group`.
  5. Task 6, `test_inputs_newer_than_the_template_are_refused`, and Task 8, `shows raw inputs read-only when migration fails`.
- **Known risks for the implementer.**
  - Task 1 may find `open` unrestricted, and Step 5 says what to do then.
  - `render_worker`'s new `pipeline=` keyword touches phase 1's worker and its end-to-end test (Task 4 Step 8).
  - Phase 1's `tests/test_workflows.py` fakes need the three new activities (Task 4 Step 9).

## Revision 1 (review)

Review: `.superpowers/sdd/2026-09-28-phase1-render-on-temporal/plan-phase4-review.md`. Phase 1 names re-checked at `wt-service` 69306836.

- **B1 fixed.** `test_an_edit_mid_run_does_not_change_the_running_job`: `held` is typed (`PieceRequest -> PieceResult | None`, so temporalio converts the argument), a `started` event is set before it waits on the gate, and the loop is `asyncio.timeout(30)` around `started.wait()`.
- **B2 fixed.** `PipelineOutput.files_key: str | None`, set by `build_output` to `output_key(job_id, index)` whenever there are files (for `layout.own` too), and read by Task 7's create route; the `files` docstring names `files_key`. Task 3's output tests assert it.
- **B3 fixed.** Task 7 posts to `/api/v1/models/{model}/outputs`. Task 8 calls phase 2's positional `createOutput(slug, jobId, name?, inputs?, index?)` with the new trailing `index` (Interfaces marks it a change to phase 2's signature). The phase 2 plan's Task 5 Interfaces gains the same line, and its Disagreements gain item 15.
- **I1 fixed.** Base names PR4 (#546) and exactly the deliverables this plan assumes: legacy queue removed, `queue`→`render`, `RenderDep`, `RenderService.client` non-optional (so the new `RenderService` methods have no `assert self.client is not None`), and a Temporal-backed `tests/api` `client` fixture with an in-process worker. Task 6's route uses `SlugPath`/`CatalogueDep`/`require_model` as `get_model` does.
- **I2 fixed.** Output blobs are published with the default `folder="work"` and `title=template_title(model_dir, slug)`; `build_output` takes `model_dir` (the `write_output` activity resolves it). The user-facing file stays Generate's save.
- **I3 fixed.** `write_output` runs with `_openscad_timeout()` and a 30 s heartbeat timeout, and the activity runs `build_output` under `_heartbeating`.
- **I4 fixed.** `Config.pipeline_timeout` (4 × `template_activity_max_timeout`) is the `execution_timeout` of every `TemplatePipeline` start (`_start`, used by `submit` and `reconcile_once`). `RenderService.settle_timed_out` (via `JobProjection.stale_running`) fails a running row whose workflow timed out, terminated or failed; the reconciler loop calls it. Tests: `test_a_pipeline_that_never_yields_times_out`, `test_a_timed_out_pipeline_is_failed_by_the_reconciler` (bounded CPU work, so no thread spins forever). The skill says "yield within 2 s". Review Focus 2 names them.
- **I5 fixed.** `test_inputs_beyond_params_make_a_different_pipeline_job` holds the rows pending by patching `service.client.start_workflow` (as `test_temporal_path.py` does), asserts identical inputs coalesce (so the test can fail), and that a different `house` gets a different id. Step 2's expected failure is corrected.
- **I6 fixed.** `house_pieces`/`clamp_house`/`LABELS` follow phase 2's `housePieces` rule for rule: `wall:lower`/`wall:upper`, `wall_window` with no course, `corner_post` one entry of 8 per storey. 11 distinct pieces, 27 copies for 2 × 1. A labels test is added, and the deferral to the implementer is gone.
- **I7 fixed.** `verify_pipeline` uses `WorkflowEnvironment.start_local(dev_server_existing_path=SCADBUDDY_TEST_TEMPORAL_DEV_SERVER or which("temporal"))`, as `tests/support/temporal.py` does. Disagreement 9 states the text the spec's §5.5 note should carry; the spec itself is not edited here.
- **M1 fixed.** `install_fake_openscad` is the one line over `tests.conftest.fake_3mf_openscad`.
- **M2 fixed.** `Ctx.pack` and `Ctx.output` bind typed locals; the unused `DEFAULT_PIPELINE_FILE` import is gone. The string literals over 100 columns are split. A Global Constraint says to run `ruff format` and split any literal still over.
- **M3 fixed.** Task 4 Step 9 names `tests/test_submit.py` (through `tests/test_workflows.py`'s `FakeActivities`, which gains the three activities by delegating to `FakeWorld`/`pack_layout`), `tests/test_worker.py` and `tests/api/test_temporal_path.py`, and runs them.
- **M4 fixed.** `revision` and `openscad_version` are set at the top of `_poll`, which covers the worker process and the in-process worker.
- **M5 fixed.** `_kill_group` is `os.killpg(process.pid, SIGKILL)`. `run_template` reads stdout with a drain task and waits on `process.wait()`, not `communicate()`, so a grandchild holding the pipe cannot hold the call; `test_a_returned_function_does_not_wait_for_its_children` pins it.
- **M6 fixed.** `_failure_of` reads the `Failure` detail of every type in `FAILURE_TYPES` (`OpenSCADError`, `ParameterError`, `TemplateActivityError`).
- **M7 fixed.** `migrate_inputs` gives the subprocess `MIGRATE_SECONDS = 30` under the activity's 60 s.
- **M8 fixed.** The skill says never to iterate a `set` to decide what to call.
- **M9 fixed** (superseded by N2 below: no serializer). `ModelMeta` keeps the unreadable declaration in `pipeline_raw`, and the `model.json` write sites re-insert it, so a rewrite keeps it; the test asserts it. Phase 2's `_readable_ui` has the same loss (it returns `"ui": None`); phase 2 declined that fix (its self-review, "Declined (review M10)"), and this plan leaves `ui` as phase 2 decided.
- **M10 fixed.** Task 4 Step 3 says `test_a_parameter_named_file_reaches_the_render` passes against phase 1 and is a regression pin.
- **M11 fixed.** `template_out_key` folds in the sha256 of `activities.py`, so a live template edited between calls never reuses the earlier call's directory; identical calls on one source write identical bytes.
- **M12 fixed.** Task 3 points phase 3's `_scope` at the template root (`Path(prepared.scad).parents[len(Path(req.file).parts) - 1]`), pinned in `test_a_piece_renders_another_file_of_the_template`.

## Revision 2 (re-review)

Re-review: `.superpowers/sdd/2026-09-28-phase1-render-on-temporal/plan-phase4-rereview.md` (all 22 earlier findings addressed; six new).

- **N1 fixed.** `test_a_timed_out_pipeline_is_failed_by_the_reconciler` registers `_ProjectingWorld`: FakeWorld with `project` also calling `RenderActivities(deps).project`, as `ProjectingActivities` does, so the row reaches `running` and `stale_running` sees it. It uses `tests/test_submit.py`'s real `make_service`/`deps`/`projection` fixtures (69306836, lines 65–103); `make` gains a `config: Config | None = None` override (it passed `config=deps.config` itself, so `**kwargs` would have duplicated it), given `replace(deps.config, template_activity_max_timeout=2.0)`. It asserts `failed` with "the pipeline did not finish within 8s".
- **N2 fixed.** No model serializer on `ModelMeta` (it would flatten `ModelRecord`'s response schema). `pipeline_raw` is re-inserted where `model.json` is written from a `ModelMeta`: `Catalogue.create` (`library/catalogue.py:693`). The metadata PATCH (`Catalogue.update`, 821–827) and the duplicate (776–777) already write the raw dict, and the test pins `create` and `update` (the duplicate path copies the raw dict). `test_the_model_record_schema_keeps_its_properties` pins the schema, and Task 2 says `export_openapi` must still list `ModelRecord`'s properties.
- **N3 fixed.** `NEVER_YIELDS` is `sum(i * i for i in range(10**9))` (about a minute, 30× the 2 s detector; bounded), then `raise`, so an early finish fails the job. Both tests accept either failure path (the timeout, or a `failed` row), never `done`.
- **N4 accepted and noted.** `_kill_group` already suppresses `ProcessLookupError`; its comment now states the pid-reuse edge after a normal exit with an empty group. There is no restructuring.
- **N5 fixed.** `probe_openscad_version` moves to `render/runner.py` beside `run_openscad`; `api/deps.py`, `main.py` and `worker.py` import it from there; `worker.py` keeps `api.deps` under `TYPE_CHECKING` only.
- **N6 fixed.** `clamp_house` maps `None` and `""` to 0 before `float`, as `clampHouse` does (`Number(null)`/`Number("")` are 0, so both clamp to the minimum), while a missing key or a non-number takes the default. A test case per value: `None`→1, `""`→1, `"3"`→3, `"x"`→2.
- R1–R3 (re-review 2) fixed: the probe's imports named; the M9 line points at N2; the duplicate path's coverage stated precisely.

## Revision 3 (merge-gate review of cfb84654)

- **Reserved names have their own test.** `test_an_output_file_name_that_could_escape_is_refused` now lists only names that could leave `files/` (`../x`, `.hidden`, `a/b`, `/etc/passwd`). The new `test_an_output_file_name_the_output_itself_uses_is_refused` covers the names the output directory already uses (`model.3mf`, `preview.glb`, `layout.json`, `piece.json`). `build_output` refuses the two cases with different messages ("use letters, digits…" and "is reserved…"), and each test asserts its own.
- **temporalio's testing API is measured in Task 1.** `verify_pipeline`'s use of `WorkflowEnvironment.start_local(dev_server_existing_path=…, data_converter=…)`, `env.client` and `env.shutdown()` is pinned against 1.33.0 by `test_the_testing_api_verify_pipeline_uses`, in Task 1's measurement pass, before any task relies on it (the base spec's "measure, don't guess"). Task 1 now expects `8 passed` and stops if the shape differs. Task 10 uses exactly that form (`try/finally: await env.shutdown()`, as `tests/support/temporal.py` does), and its "if it is not an async context manager" hedge is gone.
- **The Base section names phase 3's plan (PR #590) and its real interfaces.** It lists module paths and names as phase 3 built them (read at f9223552), not as this plan assumed them:
  - `exists` is kept, not renamed to `fetch`;
  - `BlobScope` lives in `store/content_models.py` and is re-exported by `content.py`;
  - `LocalBlobStore`, `BlobRefs` and `sweep_blobs` are phase 1's;
  - it adds `checkout`/`publish_fresh`, `model_dir`, `StoreBundle`/`AppState.store`, the new timeouts, and `build_worker_deps` returning `(WorkerDeps, StoreBundle)`.

  Task 3's `_scope` fix now uses phase 3's `model_dir(scad, file)` rather than repeating its expression.

