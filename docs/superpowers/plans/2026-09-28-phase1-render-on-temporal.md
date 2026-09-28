# Phase 1 — Render on Temporal, `render_jobs` as a projection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every render runs as a Temporal workflow on a separate worker process; `render_jobs` stops being a queue and becomes the projection the API reads and the workflow writes in place; nothing template-visible changes.

**Architecture:** The API inserts (or coalesces) a `render_jobs` row, starts workflow `render-<job_id>` with `USE_EXISTING`, and a reconciler re-starts any `pending` row that never got its workflow. `TemplatePipeline` (the default pipeline, the only one in phase 1) runs a child `RenderPiece` keyed by `piece_key` with `ABANDON`, whose four activities are today's `render_job` stages operating on a shared work directory under the new local blob store. Every state change is a `project` activity guarded by state order; job events reach the API's in-process bus through `pg_notify` and one listener connection. The worker is `python -m scadbuddy.worker` in the same image, one replica sharing the API's volume in phase 1.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3 + psycopg_pool, `temporalio==1.33.*` (pydantic data converter, workflow sandbox, `WorkerDeploymentConfig`), Postgres 17, the Temporal CLI dev server for tests (`temporalio.testing.WorkflowEnvironment.start_local(dev_server_existing_path=…)`).

**Spec:** `docs/superpowers/specs/2026-09-27-template-pipelines-design.md` — §3 (execution), §6.2 (`local` backend, phase-1 scope), §8.3 (worker versioning), §8.4 (record), §9 (`bambuddy_render_api_key`), §11 phase 1, §3.6 (to-verify list). Epic: #424 (parent #423).

## Global Constraints

- Python `>=3.12,<3.13`; `mypy --strict` over `scadbuddy` and `tests`; `ruff` with the repo's `select` list; line length 100. Every task ends with `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy` green in `backend/`.
- `temporalio` pinned `>=1.33.0,<1.34` in `backend/pyproject.toml`; `uv lock` after adding it and commit `uv.lock`.
- `SCADBUDDY_DATABASE_URL` and `SCADBUDDY_TEMPORAL_ADDRESS` are **required** (spec §3.1); the file-backed `render/job_store.py` is deleted (§3.1).
- `render_jobs` keeps the name `state`; values `pending | running | done | failed | cancelled` (§3.2). Job JSON on the wire is unchanged apart from the new `cancelled` value and `steps`.
- Workflow ids: `render-<job_id>`; child ids `piece-<piece_key>` where `piece_key = sha256(slug, revision, file, canonical params)`; `render_key` stays the job key (§3.4).
- Activity `start_to_close` for openscad activities is **derived**: `render_timeout + 60 s`; no separate setting (§3.4). The activity's cancellation path kills the openscad **process group** (`start_new_session=True`, `os.killpg`) — a new requirement (§3.4).
- Metric names in `core/metrics.py` are unchanged; `scadbuddy_render_jobs_running` is derived from the projection (§3.2).
- Every `docker run` in a `verify.sh` keeps its `--label` (CLAUDE.md); not touched here.
- Generated files are regenerated after the API model change, in the CLAUDE.md order: `export_openapi` → `frontend pnpm gen:api` → `msw init` → `agent pnpm gen:api`.
- Tests never need a live Temporal server unless one is offered: `SCADBUDDY_TEST_TEMPORAL_ADDRESS` (CI) or a `temporal` CLI on `PATH` (`start_local`); otherwise `requires_temporal` skips, like `requires_postgres`.
- Deviation from spec §6.2, stated here so it is not silent: phase 1's `BlobStore` is **directory-shaped** (`dir_for(key)`, refs, sweep), because `JobResult.model_3mf` / `preview_glb` are relative paths every output and route reads; the byte-stream `put/get/stat/list` interface of §6.2 arrives with phase 3 (#426) when Bambuddy needs it. Also: `bambuddy_render_api_key` lands as a Settings field and a `/healthz` flag only; the Settings-page warning is frontend work deferred to #426, when render workers first hold a key.

## Review Focus

The five inputs or conditions the spec implies but no task's tests would otherwise exercise, most likely to bite first. Each has a test pinned to the task that owns the code.

1. **A submit whose `start_workflow` fails** (Temporal unreachable for one call) must still answer 202 with a row in `pending`, and the reconciler must start it within 5 s once Temporal is back — Task 7, `test_a_submit_whose_workflow_start_fails_is_started_by_the_reconciler`.
2. **A worker killed between `openscad_render` and `render_solids`** must resume on a fresh worker from the next activity, not re-run the main render — Task 5, `test_a_piece_resumes_from_the_activity_it_was_on`.
3. **Two jobs sharing one piece, one superseded** — cancelling the first `TemplatePipeline` must not cancel the running `RenderPiece`, and the second job must still get `done` — Task 5, `test_cancelling_one_parent_leaves_a_shared_piece_running`.
4. **A projection write from a retried activity arriving after a later state** (`running` after `done`) must be a no-op, never a regression — Task 2, `test_a_late_running_projection_never_moves_a_settled_job_back`.
5. **Boot with `data/jobs/*.json` present** (an upgrade from today) must import settled jobs once, keep outputs' `job_id` links resolvable, remove the directory, and not import twice on the next boot — Task 7, `test_settled_file_jobs_are_imported_once_and_the_directory_removed`.

---

## File Structure

Created:
- `backend/scadbuddy/workflows/__init__.py` — package marker; exports the workflow classes and `RENDER_TASK_QUEUE_DEFAULT`.
- `backend/scadbuddy/workflows/models.py` — pydantic payloads: `PieceRequest`, `PrepareResult`, `RenderMainResult`, `SolidsResult`, `PieceResult`, `Projection`; `piece_key()`.
- `backend/scadbuddy/workflows/activities.py` — `RenderActivities` (the four render activities + `project`), built from a `WorkerDeps`.
- `backend/scadbuddy/workflows/pipelines.py` — `TemplatePipeline` and `RenderPiece` workflow definitions.
- `backend/scadbuddy/workflows/client.py` — `connect(settings) -> Client` with the pydantic data converter; task-queue constants.
- `backend/scadbuddy/render/submit.py` — `RenderService`: insert/coalesce → start → reconcile; supersede/cancel; previews via `RenderPreview`.
- `backend/scadbuddy/render/projection.py` — `JobProjection` (the Postgres row store, replacing `PostgresJobStore`'s queue half), `JobEventListener`.
- `backend/scadbuddy/store/__init__.py`, `backend/scadbuddy/store/local.py`, `backend/scadbuddy/store/refs.py` — `BlobStore` protocol (directory-shaped for phase 1), `LocalBlobStore`, `BlobRefs`.
- `backend/scadbuddy/worker.py` — `python -m scadbuddy.worker`: worker deps, Temporal `Worker`, health/metrics HTTP, signal handling; `run_inprocess_worker()` for the API's dev/test mode.
- `backend/tests/support/temporal.py` — `temporal_env` helpers, `worker_thread()`.
- `backend/tests/test_projection.py`, `backend/tests/test_blob_store.py`, `backend/tests/test_workflows.py`, `backend/tests/test_submit.py`, `backend/tests/test_worker.py`.

Modified:
- `backend/pyproject.toml`, `backend/uv.lock` — `temporalio`.
- `backend/scadbuddy/core/settings.py`, `core/config.py` — Temporal settings; `database_url` required; `bambuddy_render_api_key`; `temporal_worker_inprocess`.
- `backend/scadbuddy/render/pg_store.py` — migration 4 appended (`MIGRATIONS` stays the one list); the `PostgresJobStore` class and `QueueListener` move to `projection.py` in their new shape (file kept for `MIGRATIONS`, `migrate`, `JOB_COLUMNS`).
- `backend/scadbuddy/render/job_models.py` — `JobState` gains `cancelled`; `Job` gains `kind`, `inputs`, `pipeline_version`, `steps`, `workflow_id`.
- `backend/scadbuddy/render/jobs.py` — `render_job` split into stage functions the activities call; `RenderQueue`, `_Background`, worker/reaper/heartbeat loops deleted.
- `backend/scadbuddy/render/runner.py` — `run_openscad` starts the child in a new session and kills the group.
- `backend/scadbuddy/render/previews.py` — `PreviewScheduler` takes a `PreviewRunner` callable instead of the queue.
- `backend/scadbuddy/api/jobs.py`, `api/models.py`, `api/metrics.py`, `api/deps.py`, `api/health.py`, `main.py` — the queue's consumers move to `RenderService` / `JobProjection`.
- `backend/scadbuddy/core/paths.py` — `blobs` dir; `job_work_dir` stays for the import step only.
- `backend/scadbuddy/core/metrics.py` — `store_info` label doc; `listener_*` re-described for the event listener.
- `Dockerfile` — Temporal CLI in the `test` stage; comment on the worker command.
- `.github/workflows/ci.yml` — nothing new is required (the CLI is in the image); the pytest step gains `-e SCADBUDDY_TEST_TEMPORAL_DEV_SERVER=/usr/local/bin/temporal`.
- `CLAUDE.md`, `README.md` — commands, required env, worker.
- `backend/openapi.json`, `frontend/src/api/schema.d.ts`, `frontend/public/mockServiceWorker.js`, `agent/src/api/schema.d.ts` — regenerated.

Deleted:
- `backend/scadbuddy/render/job_store.py`, `backend/tests/test_render_queue.py` (its cases move to `test_projection.py`, `test_submit.py`, `test_workflows.py`), `backend/tests/api/test_postgres_queue.py` (folded into `test_submit.py`).

---

### Task 1: Dependency, settings, config

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/scadbuddy/core/settings.py`
- Modify: `backend/scadbuddy/core/config.py`
- Test: `backend/tests/test_config.py`

**Interfaces:**
- Produces: `Settings.temporal_address: str`, `Settings.temporal_namespace: str = "scadbuddy"`, `Settings.temporal_task_queue_render: str = "render"`, `Settings.temporal_worker_inprocess: bool = False`, `Settings.bambuddy_render_api_key: str | None`, `Settings.database_url: str` (required). `Config.temporal_address`, `Config.temporal_namespace`, `Config.temporal_task_queue_render`, `Config.activity_timeout` property `= render_timeout + 60.0`. Constants `DEFAULT_TEMPORAL_NAMESPACE = "scadbuddy"`, `DEFAULT_TEMPORAL_TASK_QUEUE_RENDER = "render"`, `ACTIVITY_TIMEOUT_MARGIN = 60.0` in `core/config.py`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_config.py`:

```python
import pytest
from pydantic import ValidationError

from scadbuddy.core.config import ACTIVITY_TIMEOUT_MARGIN, Config
from scadbuddy.core.settings import Settings


def test_database_and_temporal_are_required() -> None:
    with pytest.raises(ValidationError) as missing:
        Settings(_env_file=None)
    names = {error["loc"][0] for error in missing.value.errors()}
    assert {"database_url", "temporal_address"} <= names


def test_temporal_settings_reach_the_config() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://x",
        temporal_address="temporal:7233",
        render_timeout=45.0,
    )
    config = settings.to_config()
    assert config.temporal_address == "temporal:7233"
    assert config.temporal_namespace == "scadbuddy"
    assert config.temporal_task_queue_render == "render"
    assert config.activity_timeout == 45.0 + ACTIVITY_TIMEOUT_MARGIN


def test_the_render_key_falls_back_to_the_full_key_and_says_so() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://x",
        temporal_address="t:7233",
        bambuddy_api_key="full",
    )
    assert settings.render_bambuddy_key() == ("full", True)
    with_own = settings.model_copy(update={"bambuddy_render_api_key": "narrow"})
    assert with_own.render_bambuddy_key() == ("narrow", False)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'ACTIVITY_TIMEOUT_MARGIN'`.

- [ ] **Step 3: Add the dependency**

In `backend/pyproject.toml` `dependencies`, add `"temporalio>=1.33.0,<1.34",` after `"pydantic-settings>=2.6",`. Then:

```bash
cd backend && uv lock && uv sync --frozen
```

- [ ] **Step 4: Config constants and fields**

In `backend/scadbuddy/core/config.py`, after `DEFAULT_DATABASE_POOL_SIZE = 10`:

```python
# Temporal (spec 2026-09-27 §3.1). The address is required — there is no queue
# without it — so it has no default; the namespace and task queue do.
DEFAULT_TEMPORAL_NAMESPACE = "scadbuddy"
DEFAULT_TEMPORAL_TASK_QUEUE_RENDER = "render"
# An openscad activity's start_to_close is derived from the one timeout an operator
# tunes (§3.4): the subprocess is killed at render_timeout, and Temporal gives up on
# the attempt this much later, so the two can never invert.
ACTIVITY_TIMEOUT_MARGIN = 60.0
```

In the `Config` dataclass add fields (after `database_pool_size` or the last render field):

```python
    temporal_address: str = ""
    temporal_namespace: str = DEFAULT_TEMPORAL_NAMESPACE
    temporal_task_queue_render: str = DEFAULT_TEMPORAL_TASK_QUEUE_RENDER

    @property
    def activity_timeout(self) -> float:
        """start_to_close for the openscad activities: `render_timeout` plus the margin."""
        return self.render_timeout + ACTIVITY_TIMEOUT_MARGIN
```

Remove `render_poll_interval`, `render_fallback_poll_interval`, `render_lease_timeout`, `render_max_attempts` from `Config`, from `Settings`, from `Settings.to_config`, from the `("SCADBUDDY_…", value)` validation list and from `from_env` — they belong to the deleted queue. Delete their `DEFAULT_*` constants and comments. (`mypy` will point at every remaining reference; the only ones are in `render/jobs.py`, which Task 7 deletes, so leave those for Task 7 and mark this step done when `tests/test_config.py` passes.)

- [ ] **Step 5: Settings fields**

In `backend/scadbuddy/core/settings.py`, replace the `database_url` block with:

```python
    # SCADBUDDY_DATABASE_URL: a libpq URL or DSN. Required since phase 1 of the
    # template-pipelines spec (§3.1): render_jobs is the projection the API reads.
    database_url: str
    database_pool_size: int = DEFAULT_DATABASE_POOL_SIZE

    # SCADBUDDY_TEMPORAL_ADDRESS: host:port of the Temporal frontend. Required.
    temporal_address: str
    temporal_namespace: str = DEFAULT_TEMPORAL_NAMESPACE
    temporal_task_queue_render: str = DEFAULT_TEMPORAL_TASK_QUEUE_RENDER
    # SCADBUDDY_TEMPORAL_WORKER_INPROCESS: run the render worker inside the API
    # process (one replica, dev and tests). Production runs `python -m
    # scadbuddy.worker` as its own Deployment and leaves this off.
    temporal_worker_inprocess: bool = False

    # SCADBUDDY_BAMBUDDY_RENDER_API_KEY: the Manage-Library-only key render workers
    # hold (spec §9). Unset, they fall back to `bambuddy_api_key` and /healthz says so.
    bambuddy_render_api_key: str | None = None
```

and add the method:

```python
    def render_bambuddy_key(self) -> tuple[str | None, bool]:
        """The key render workers use, and whether it is the full key by fallback."""
        if self.bambuddy_render_api_key:
            return self.bambuddy_render_api_key, False
        return self.bambuddy_api_key, True
```

Import the two new defaults, and pass `temporal_address=self.temporal_address, temporal_namespace=self.temporal_namespace, temporal_task_queue_render=self.temporal_task_queue_render` in `to_config`.

- [ ] **Step 6: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/test_config.py -q`
Expected: PASS. (`mypy` will fail on `render/jobs.py`'s removed config fields until Task 7; run `uv run --frozen ruff check . && uv run --frozen ruff format .` now.)

- [ ] **Step 7: Fix every other test that builds `Settings(...)` without the two required fields**

Run: `cd backend && grep -rn "Settings(" tests | grep -v "_env_file" | head`. Every `Settings(` in a test (notably `tests/api/conftest.py::settings`) must now pass `database_url` and `temporal_address`; use placeholders here (`database_url="postgresql://placeholder"`, `temporal_address="127.0.0.1:1"`) — Task 7 replaces them with the real fixtures. This keeps the suite importable in between.

- [ ] **Step 8: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/scadbuddy/core backend/tests
git commit -m "feat(render): temporalio dependency; Temporal and database settings are required (#424)"
```

---

### Task 2: Migration 4 and the `JobProjection`

**Files:**
- Modify: `backend/scadbuddy/render/pg_store.py` (append migration; keep `migrate`, `MIGRATIONS`, `JOB_COLUMNS`, `_job`; remove `PostgresJobStore`, `QueueListener`, `_notify`)
- Modify: `backend/scadbuddy/render/job_models.py`
- Create: `backend/scadbuddy/render/projection.py`
- Test: `backend/tests/test_projection.py` (`requires_postgres`)

**Interfaces:**
- Produces:
  ```python
  JobState = Literal["pending", "running", "done", "failed", "cancelled"]
  JobKind = Literal["render", "arrange"]          # job_models.JobKind (rename the events one? no — see below)
  class StepInfo(BaseModel): name: str; state: Literal["pending", "running", "done", "failed"]; done: int | None = None; total: int | None = None
  class Job(...): + kind: JobKind = "render"; inputs: dict[str, Any] = {}; pipeline_version: str = "default"; steps: list[StepInfo] = []; workflow_id: str | None = None

  class Submitted: job: Job; coalesced: bool; superseded: Job | None
  class JobProjection:
      def __init__(self, conninfo: str, *, pool_size: int = 10, connect_timeout: float = 30.0)
      def open(self) -> None; def close(self) -> None
      def submit(self, job: Job, key: str, *, supersedes: str | None = None, max_pending: int = 0) -> Submitted
      def release_claim(self, job_id: str, *, slug: str) -> Job | None      # → the job when its last claim went (state cancelled)
      def mark_started(self, job_id: str) -> Job | None                      # pending→running once; None if not pending
      def set_steps(self, job_id: str, steps: list[StepInfo]) -> None
      def finish(self, job: Job) -> bool                                     # running→done|failed|cancelled; False if not running
      def stale_pending(self, older_than: float) -> list[Job]                # pending rows with started_at IS NULL, created before now-older_than
      def read(self, job_id: str) -> Job; def list_jobs(self) -> list[Job]
      def has_unfinished(self, slug: str) -> bool; def latest_finished(self, slug: str) -> Job | None
      def counts(self) -> QueueCounts; def prune(self, ttl: float, *, now=None) -> list[str]; def delete(self, job_id: str) -> None
  EVENTS_CHANNEL = "scadbuddy_job_events"
  ```
  Every write that changes `state` also `pg_notify(EVENTS_CHANNEL, json.dumps({"kind": "job.<state or superseded>", "job_id", "slug"}))` **inside its transaction** (Task 3's listener reads it).
- The name clash: `core/events.py` already has `JobKind` (the event kinds). Name the job-table column type `JobTableKind` in `job_models.py` to avoid confusion.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_projection.py`:

```python
"""The render_jobs projection: rows the API reads and the workflow writes in place."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from scadbuddy.render.job_models import Job, JobResult, PartInfo, StepInfo
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.pg_store import MIGRATIONS
from scadbuddy.render.projection import EVENTS_CHANNEL, JobProjection, render_key

pytestmark = pytest.mark.requires_postgres


def _job(slug: str = "demo", **params: object) -> Job:
    return Job(id=uuid.uuid4().hex, slug=slug, params=dict(params), created_at=datetime.now(UTC))


def _result() -> JobResult:
    return JobResult(
        model_3mf="blobs/k/model.3mf",
        preview_glb="blobs/k/preview.glb",
        parts=[PartInfo(name="Color 1", colour="#FF0000", extruder=1, watertight=True)],
        bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1)),
    )


@pytest.fixture
def projection(pg_conninfo: str):
    store = JobProjection(pg_conninfo, pool_size=2)
    store.open()
    try:
        yield store
    finally:
        store.close()


def test_migration_four_reshapes_the_table(pg_conninfo: str, projection: JobProjection) -> None:
    with psycopg.connect(pg_conninfo) as conn:
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'render_jobs'"
            )
        }
        versions = [r[0] for r in conn.execute("SELECT version FROM scadbuddy_migrations ORDER BY 1")]
    assert {"workflow_id", "kind", "inputs", "pipeline_version", "steps"} <= columns
    assert "heartbeat_at" not in columns
    assert versions == list(range(1, len(MIGRATIONS) + 1))


def test_submit_inserts_pending_with_its_workflow_id(projection: JobProjection) -> None:
    job = _job(width=1)
    submitted = projection.submit(job, render_key("demo", {"width": 1}, None))
    assert not submitted.coalesced
    stored = projection.read(job.id)
    assert stored.state == "pending"
    assert stored.workflow_id == f"render-{job.id}"
    assert stored.inputs == {"params": {"width": 1}}


def test_an_identical_pending_submit_coalesces(projection: JobProjection) -> None:
    key = render_key("demo", {"width": 1}, None)
    first = projection.submit(_job(width=1), key)
    second = projection.submit(_job(width=1), key)
    assert second.coalesced and second.job.id == first.job.id
    assert projection.read(first.job.id).claims == 2


def test_releasing_the_last_claim_cancels(projection: JobProjection) -> None:
    key = render_key("demo", {"width": 1}, None)
    first = projection.submit(_job(width=1), key)
    projection.submit(_job(width=1), key)
    assert projection.release_claim(first.job.id, slug="demo") is None  # one claim left
    gone = projection.release_claim(first.job.id, slug="demo")
    assert gone is not None and gone.state == "cancelled"


def test_state_moves_forward_only(projection: JobProjection) -> None:
    job = projection.submit(_job(width=2), render_key("demo", {"width": 2}, None)).job
    assert projection.mark_started(job.id) is not None
    assert projection.mark_started(job.id) is None  # already running
    job.state, job.result, job.finished_at = "done", _result(), datetime.now(UTC)
    assert projection.finish(job)
    assert not projection.finish(job)  # already settled


def test_a_late_running_projection_never_moves_a_settled_job_back(
    projection: JobProjection,
) -> None:
    job = projection.submit(_job(width=3), render_key("demo", {"width": 3}, None)).job
    projection.mark_started(job.id)
    job.state, job.result, job.finished_at = "done", _result(), datetime.now(UTC)
    projection.finish(job)
    assert projection.mark_started(job.id) is None
    assert projection.read(job.id).state == "done"


def test_steps_are_stored_as_given(projection: JobProjection) -> None:
    job = projection.submit(_job(width=4), render_key("demo", {"width": 4}, None)).job
    projection.set_steps(job.id, [StepInfo(name="render", state="running")])
    assert projection.read(job.id).steps == [StepInfo(name="render", state="running")]


def test_stale_pending_is_what_the_reconciler_restarts(projection: JobProjection) -> None:
    job = projection.submit(_job(width=5), render_key("demo", {"width": 5}, None)).job
    assert projection.stale_pending(older_than=3600) == []
    assert [j.id for j in projection.stale_pending(older_than=0)] == [job.id]
    projection.mark_started(job.id)
    assert projection.stale_pending(older_than=0) == []


def test_every_state_change_notifies(pg_conninfo: str, projection: JobProjection) -> None:
    with psycopg.connect(pg_conninfo, autocommit=True) as listener:
        listener.execute(f"LISTEN {EVENTS_CHANNEL}")
        job = projection.submit(_job(width=6), render_key("demo", {"width": 6}, None)).job
        projection.mark_started(job.id)
        job.state, job.error, job.finished_at = "failed", "boom", datetime.now(UTC)
        projection.finish(job)
        kinds = [n.payload for n in listener.notifies(timeout=2, stop_after=3)]
    assert [__import__("json").loads(k)["kind"] for k in kinds] == [
        "job.pending",
        "job.running",
        "job.failed",
    ]


def test_prune_removes_settled_rows_only(projection: JobProjection) -> None:
    old = projection.submit(_job(width=7), render_key("demo", {"width": 7}, None)).job
    projection.mark_started(old.id)
    old.state, old.error, old.finished_at = "failed", "x", datetime.now(UTC) - timedelta(days=2)
    projection.finish(old)
    fresh = projection.submit(_job(width=8), render_key("demo", {"width": 8}, None)).job
    assert projection.prune(ttl=86400) == [old.id]
    assert projection.read(fresh.id).state == "pending"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/test_projection.py -q`
Expected: FAIL — `ModuleNotFoundError: scadbuddy.render.projection`.

- [ ] **Step 3: Job model changes**

In `backend/scadbuddy/render/job_models.py`:

```python
from typing import Any, Literal

JobState = Literal["pending", "running", "done", "failed", "cancelled"]
JobTableKind = Literal["render", "arrange"]
StepState = Literal["pending", "running", "done", "failed"]


class StepInfo(BaseModel):
    """One row of `render_jobs.steps`: what `ctx.progress` writes (spec §3.2)."""

    name: str
    state: StepState = "pending"
    done: int | None = None
    total: int | None = None
```

and on `Job`, after `params`:

```python
    #: Which workflow `render-<id>` runs (spec §3.4): a render, or (phase 5) an arrange.
    kind: JobTableKind = "render"
    #: Template-owned inputs (spec §4.3). For a params-only template, `{"params": …}`;
    #: `params` is kept beside it through phase 1 and dropped by phase 2's migration.
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: sha256 of the pipeline source `load_pipeline` recorded, or "default" (§3.2).
    pipeline_version: str = "default"
    steps: list[StepInfo] = Field(default_factory=list)
    workflow_id: str | None = None
    #: Submitters still waiting on this job (coalesced identical requests).
    claims: int = 1
```

Remove `_attempt` / `attempt` / `claimed` — Temporal owns attempts now.

- [ ] **Step 4: Migration 4**

Append to `MIGRATIONS` in `backend/scadbuddy/render/pg_store.py`:

```python
    # 4: the projection (spec 2026-09-27 §3.2). Temporal owns the wait list, leases
    # and retries, so the heartbeat goes; the workflow writes the row in place.
    """
    ALTER TABLE render_jobs DROP CONSTRAINT render_jobs_state_check;
    ALTER TABLE render_jobs ADD CONSTRAINT render_jobs_state_check
        CHECK (state IN ('pending', 'running', 'done', 'failed', 'cancelled'));
    DROP INDEX IF EXISTS render_jobs_running;
    ALTER TABLE render_jobs
        DROP COLUMN heartbeat_at,
        ADD COLUMN workflow_id text,
        ADD COLUMN kind text NOT NULL DEFAULT 'render' CHECK (kind IN ('render', 'arrange')),
        ADD COLUMN inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
        ADD COLUMN pipeline_version text NOT NULL DEFAULT 'default',
        ADD COLUMN steps jsonb NOT NULL DEFAULT '[]'::jsonb;
    UPDATE render_jobs SET workflow_id = 'render-' || id, inputs = jsonb_build_object('params', params);
    CREATE INDEX render_jobs_stale_pending ON render_jobs (created_at)
        WHERE state = 'pending' AND started_at IS NULL;
    CREATE TABLE blob_refs (
        key         text NOT NULL,
        holder_kind text NOT NULL,
        holder_id   text NOT NULL,
        PRIMARY KEY (key, holder_kind, holder_id)
    );
    CREATE INDEX blob_refs_key ON blob_refs (key);
    """,
```

Extend `JOB_COLUMNS` with `"kind", "inputs", "pipeline_version", "steps", "workflow_id", "claims"`. Delete `PostgresJobStore`, `QueueListener`, `_notify`, `QUEUE_CHANNEL`, `LISTENER_APPLICATION_NAME`, `TWIN_QUEUED_ERROR` from this file (they are replaced in the next step); keep the module docstring's migration paragraph and rewrite the first paragraph to say the table is a projection.

- [ ] **Step 5: `JobProjection`**

Create `backend/scadbuddy/render/projection.py`:

```python
"""`render_jobs` as a projection (spec 2026-09-27 §3.2).

The API inserts a row and starts the workflow named by it; the workflow's `project`
activity moves `state` forward in place. Every write is guarded by the state it
expects, so a retried activity cannot move a job backwards, and every state change
NOTIFYs `EVENTS_CHANNEL` in its own transaction so the API's bus hears it on commit.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from scadbuddy.render.job_models import Job, StepInfo, now
from scadbuddy.render.pg_store import JOB_COLUMNS, migrate
from scadbuddy.render.schema import ParamValue

logger = logging.getLogger(__name__)

EVENTS_CHANNEL = "scadbuddy_job_events"
SUPERSEDED_ERROR = "superseded by a newer render before it started"
CANCELLED_ERROR = "cancelled: every request for it was withdrawn"


class QueueFullError(Exception):
    def __init__(self, depth: int, retry_after: int = 1) -> None:
        super().__init__(
            f"{depth} renders are already waiting (SCADBUDDY_RENDER_QUEUE_MAX); "
            f"try again in {retry_after} s"
        )
        self.depth = depth
        self.retry_after = retry_after


class JobNotFoundError(LookupError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"no job with id {job_id!r}")


def render_key(slug: str, params: Mapping[str, ParamValue], model_version: str | None) -> str:
    """The JOB key: what makes two requests the same render (spec §3.4)."""
    raw = json.dumps([slug, model_version, dict(params)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def workflow_id_for(job_id: str) -> str:
    return f"render-{job_id}"


@dataclass(frozen=True)
class Submitted:
    job: Job
    coalesced: bool
    superseded: Job | None = None


@dataclass(frozen=True)
class QueueCounts:
    pending: int
    running: int
    oldest_pending: datetime | None


def _job(row: DictRow) -> Job:
    return Job.model_validate({column: row[column] for column in JOB_COLUMNS})


def _notify(conn: Connection[Any], kind: str, job_id: str, slug: str) -> None:
    payload = json.dumps({"kind": kind, "job_id": job_id, "slug": slug})
    conn.execute("SELECT pg_notify(%s, %s)", (EVENTS_CHANNEL, payload))


class JobProjection:
    backend = "postgres"

    def __init__(self, conninfo: str, *, pool_size: int = 10, connect_timeout: float = 30.0) -> None:
        self.conninfo = conninfo
        self.connect_timeout = connect_timeout
        self._pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            conninfo,
            min_size=1,
            max_size=pool_size,
            open=False,
            connection_class=Connection[DictRow],
            kwargs={"autocommit": True, "row_factory": dict_row},
            name="scadbuddy-jobs",
        )

    def open(self) -> None:
        self._pool.open(wait=True, timeout=self.connect_timeout)
        with self._pool.connection() as conn:
            applied = migrate(conn)
        if applied:
            logger.info("applied database migrations", extra={"versions": applied})

    def close(self) -> None:
        self._pool.close()

    # -- the API's writes -------------------------------------------------------

    def submit(
        self, job: Job, key: str, *, supersedes: str | None = None, max_pending: int = 0
    ) -> Submitted:
        superseded: Job | None = None
        with self._pool.connection() as conn, conn.transaction():
            if supersedes is not None:
                previous = conn.execute(
                    "SELECT * FROM render_jobs WHERE id = %s AND state = 'pending'"
                    " AND slug = %s FOR UPDATE",
                    (supersedes, job.slug),
                ).fetchone()
                if previous is not None and previous["render_key"] == key:
                    return Submitted(_job(previous), coalesced=True)
                if previous is not None:
                    superseded = self._release(conn, previous, error=SUPERSEDED_ERROR)
            if max_pending:
                twin = conn.execute(
                    "SELECT 1 FROM render_jobs WHERE state = 'pending' AND render_key = %s", (key,)
                ).fetchone()
                if twin is None:
                    counted = conn.execute(
                        "SELECT count(*) AS pending FROM render_jobs WHERE state = 'pending'"
                    ).fetchone()
                    assert counted is not None
                    if counted["pending"] >= max_pending:
                        raise QueueFullError(counted["pending"])
            row = conn.execute(
                "INSERT INTO render_jobs (id, slug, params, inputs, model_version, state,"
                " created_at, render_key, workflow_id, kind)"
                " VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s)"
                " ON CONFLICT (render_key) WHERE state = 'pending'"
                " DO UPDATE SET claims = render_jobs.claims + 1"
                " RETURNING *, (xmax = 0) AS inserted",
                (
                    job.id,
                    job.slug,
                    Jsonb(job.params),
                    Jsonb(job.inputs or {"params": job.params}),
                    job.model_version,
                    job.created_at,
                    key,
                    workflow_id_for(job.id),
                    job.kind,
                ),
            ).fetchone()
            assert row is not None
            if row["inserted"]:
                _notify(conn, "job.pending", row["id"], row["slug"])
        return Submitted(_job(row), coalesced=not row["inserted"], superseded=superseded)

    def _release(self, conn: Connection[Any], row: DictRow, *, error: str) -> Job | None:
        """Take one claim off a pending row; the last one cancels it. Returns the
        cancelled job, or None while claims remain."""
        if row["claims"] > 1:
            conn.execute("UPDATE render_jobs SET claims = claims - 1 WHERE id = %s", (row["id"],))
            return None
        dropped = conn.execute(
            "UPDATE render_jobs SET state = 'cancelled', claims = 0, finished_at = %s,"
            " error = %s WHERE id = %s RETURNING *",
            (now(), error, row["id"]),
        ).fetchone()
        assert dropped is not None
        _notify(conn, "job.superseded", row["id"], row["slug"])
        return _job(dropped)

    def release_claim(self, job_id: str, *, slug: str) -> Job | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT * FROM render_jobs WHERE id = %s AND slug = %s"
                " AND state IN ('pending', 'running') FOR UPDATE",
                (job_id, slug),
            ).fetchone()
            if row is None:
                return None
            return self._release(conn, row, error=CANCELLED_ERROR)

    # -- the workflow's writes (each guarded by the state it expects) -----------

    def mark_started(self, job_id: str) -> Job | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE render_jobs SET state = 'running', started_at = now(),"
                " attempts = attempts + 1 WHERE id = %s AND state = 'pending' RETURNING *",
                (job_id,),
            ).fetchone()
            if row is not None:
                _notify(conn, "job.running", row["id"], row["slug"])
        return _job(row) if row is not None else None

    def set_steps(self, job_id: str, steps: list[StepInfo]) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE render_jobs SET steps = %s WHERE id = %s AND state = 'running'",
                (Jsonb([step.model_dump(mode="json") for step in steps]), job_id),
            )

    def finish(self, job: Job) -> bool:
        assert job.state in ("done", "failed", "cancelled")
        with self._pool.connection() as conn, conn.transaction():
            cursor = conn.execute(
                "UPDATE render_jobs SET state = %s, finished_at = %s, log_tail = %s,"
                " error = %s, result = %s, diagnostics = %s, diagnostics_dropped = %s,"
                " warnings = %s, steps = %s, pipeline_version = %s"
                " WHERE id = %s AND state = 'running'",
                (
                    job.state,
                    job.finished_at or now(),
                    Jsonb(job.log_tail),
                    job.error,
                    Jsonb(job.result.model_dump(mode="json")) if job.result is not None else None,
                    Jsonb([d.model_dump(mode="json") for d in job.diagnostics]),
                    job.diagnostics_dropped,
                    Jsonb(job.warnings),
                    Jsonb([s.model_dump(mode="json") for s in job.steps]),
                    job.pipeline_version,
                    job.id,
                ),
            )
            landed = cursor.rowcount == 1
            if landed:
                _notify(conn, f"job.{job.state}", job.id, job.slug)
        return landed

    # -- reads ------------------------------------------------------------------

    def stale_pending(self, older_than: float) -> list[Job]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM render_jobs WHERE state = 'pending' AND started_at IS NULL"
                " AND created_at < now() - make_interval(secs => %s) ORDER BY created_at",
                (older_than,),
            ).fetchall()
        return [_job(row) for row in rows]

    def read(self, job_id: str) -> Job:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT * FROM render_jobs WHERE id = %s", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return _job(row)

    def list_jobs(self) -> list[Job]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT * FROM render_jobs ORDER BY created_at, id").fetchall()
        return [_job(row) for row in rows]

    def has_unfinished(self, slug: str) -> bool:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM render_jobs WHERE slug = %s"
                " AND state IN ('pending', 'running')) AS unfinished",
                (slug,),
            ).fetchone()
        return bool(row and row["unfinished"])

    def latest_finished(self, slug: str) -> Job | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM render_jobs WHERE slug = %s AND state IN ('done', 'failed')"
                " AND finished_at IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1",
                (slug,),
            ).fetchone()
        return _job(row) if row is not None else None

    def counts(self) -> QueueCounts:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FILTER (WHERE state = 'pending') AS pending,"
                " count(*) FILTER (WHERE state = 'running') AS running,"
                " min(created_at) FILTER (WHERE state = 'pending') AS oldest_pending"
                " FROM render_jobs WHERE state IN ('pending', 'running')"
            ).fetchone()
        assert row is not None
        return QueueCounts(row["pending"], row["running"], row["oldest_pending"])

    def prune(self, ttl: float, *, now_: datetime | None = None) -> list[str]:
        cutoff = (now_ or now()) - timedelta(seconds=ttl)
        with self._pool.connection() as conn, conn.transaction():
            rows = conn.execute(
                "DELETE FROM render_jobs WHERE state IN ('done', 'failed', 'cancelled')"
                " AND coalesce(finished_at, created_at) < %s RETURNING id",
                (cutoff,),
            ).fetchall()
            removed = [row["id"] for row in rows]
            if removed:
                conn.execute(
                    "DELETE FROM blob_refs WHERE holder_kind = 'job' AND holder_id = ANY(%s)",
                    (removed,),
                )
        return removed

    def delete(self, job_id: str) -> None:
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM render_jobs WHERE id = %s", (job_id,))
            conn.execute(
                "DELETE FROM blob_refs WHERE holder_kind = 'job' AND holder_id = %s", (job_id,)
            )
```

(`prune`'s keyword is `now_` to avoid shadowing the imported `now`; the test calls it positionally.) Note `prune` and `delete` no longer `rmtree` a work dir: blobs are swept by refs (Task 4).

- [ ] **Step 6: Run the tests**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/test_projection.py -q`
Expected: PASS (10 tests). `mypy` still fails on `render/jobs.py` imports of the removed `PostgresJobStore`; that is Task 7's.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/render/pg_store.py backend/scadbuddy/render/projection.py backend/scadbuddy/render/job_models.py backend/tests/test_projection.py
git commit -m "feat(render): render_jobs becomes a projection — migration 4, JobProjection, cancelled state (#424)"
```

---

### Task 3: Job events reach the API's bus

**Files:**
- Modify: `backend/scadbuddy/render/projection.py` (append `JobEventListener`)
- Test: `backend/tests/test_projection.py` (append; `requires_postgres`)

**Interfaces:**
- Produces: `class JobEventListener: def __init__(self, conninfo: str, *, events: EventBus, on_state: Callable[[bool], None] | None = None, check_interval: float = 30.0, connect_timeout: float = 30.0); async def run(self) -> None; connects: int`. Decodes each NOTIFY payload into `JobEvent(kind=…, job_id=…, slug=…)` and `emit(events, …)`. Reconnects with the same jittered back-off the old `QueueListener` had.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_projection.py`:

```python
import asyncio

from scadbuddy.core.events import InProcessEventBus, JobEvent
from scadbuddy.render.projection import JobEventListener


@pytest.mark.asyncio
async def test_the_listener_turns_notifies_into_job_events(
    pg_conninfo: str, projection: JobProjection
) -> None:
    bus = InProcessEventBus()
    listener = JobEventListener(pg_conninfo, events=bus, check_interval=0.2)
    task = asyncio.create_task(listener.run())
    try:
        async with bus.subscribe(kinds=["job.pending", "job.running"]) as sub:
            for _ in range(100):
                if listener.connects:
                    break
                await asyncio.sleep(0.02)
            job = await asyncio.to_thread(
                lambda: projection.submit(_job(width=9), render_key("demo", {"width": 9}, None)).job
            )
            await asyncio.to_thread(projection.mark_started, job.id)
            first = await asyncio.wait_for(sub.get(), 5)
            second = await asyncio.wait_for(sub.get(), 5)
        assert isinstance(first, JobEvent) and first.kind == "job.pending" and first.job_id == job.id
        assert isinstance(second, JobEvent) and second.kind == "job.running"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bus.aclose()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/test_projection.py -k listener -q`
Expected: FAIL — `ImportError: JobEventListener`.

- [ ] **Step 3: Implement the listener**

Append to `backend/scadbuddy/render/projection.py`:

```python
import asyncio
import random
from collections.abc import Callable

from psycopg import AsyncConnection

from scadbuddy.core.events import EventBus, JobEvent, JobKind, emit

LISTENER_APPLICATION_NAME = "scadbuddy-job-events"
_KINDS: frozenset[str] = frozenset(
    ("job.pending", "job.running", "job.done", "job.failed", "job.superseded", "job.cancelled")
)


class JobEventListener:
    """One LISTEN on `EVENTS_CHANNEL`, decoded onto this process's event bus.

    Realtime sockets (#266) subscribe to the in-process bus; the writes now happen in
    the worker process, so the database is what carries them across. A dropped
    connection is retried with capped, jittered back-off; an idle one is checked every
    ``check_interval`` because a half-open socket delivers nothing and raises nothing.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        events: EventBus,
        on_state: Callable[[bool], None] | None = None,
        check_interval: float = 30.0,
        connect_timeout: float = 30.0,
        backoff: float = 0.5,
        max_backoff: float = 30.0,
    ) -> None:
        self.conninfo = conninfo
        self.events = events
        self.on_state = on_state or (lambda _connected: None)
        self.check_interval = check_interval
        self.connect_timeout = connect_timeout
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.connects = 0

    async def run(self) -> None:
        delay = self.backoff
        while True:
            connects = self.connects
            try:
                await self._listen()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("job event listener disconnected", extra={"error": str(error)})
            if self.connects != connects:
                delay = self.backoff
            await asyncio.sleep(delay * random.uniform(0.5, 1.0))
            delay = min(delay * 2, self.max_backoff)

    async def _listen(self) -> None:
        conn = await AsyncConnection.connect(
            self.conninfo,
            autocommit=True,
            connect_timeout=max(1, round(self.connect_timeout)),
            application_name=LISTENER_APPLICATION_NAME,
        )
        async with conn:
            await conn.execute(f"LISTEN {EVENTS_CHANNEL}".encode())
            self.connects += 1
            self.on_state(True)
            try:
                while True:
                    async for notice in conn.notifies(timeout=self.check_interval):
                        self._deliver(notice.payload)
                    await conn.execute("SELECT 1")  # the idle check
            finally:
                self.on_state(False)

    def _deliver(self, payload: str) -> None:
        try:
            data = json.loads(payload)
            kind = data["kind"]
            if kind not in _KINDS:
                raise ValueError(kind)
            event = JobEvent(kind=_as_kind(kind), job_id=str(data["job_id"]), slug=str(data["slug"]))
        except (ValueError, KeyError, TypeError):
            logger.warning("ignored a malformed job event", extra={"payload": payload[:200]})
            return
        emit(self.events, event)


def _as_kind(kind: str) -> JobKind:
    # "job.cancelled" is new on the wire; the bus keeps its existing vocabulary and
    # reports a withdrawn job as superseded, which is what every subscriber handles.
    return "job.superseded" if kind == "job.cancelled" else kind  # type: ignore[return-value]
```

In `core/events.py`, extend `JobKind` with `"job.cancelled"` instead of the cast above if the frontend's realtime client can take a new kind without change — check `frontend/src/lib/realtime*.ts` for an exhaustive switch; if none, add the kind and delete `_as_kind`. Otherwise keep the mapping and this comment.

- [ ] **Step 4: Run the tests**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/test_projection.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/render/projection.py backend/scadbuddy/core/events.py backend/tests/test_projection.py
git commit -m "feat(render): job state changes reach the API bus through pg_notify (#424)"
```

---

### Task 4: The local blob store and its refs

**Files:**
- Create: `backend/scadbuddy/store/__init__.py`, `backend/scadbuddy/store/local.py`, `backend/scadbuddy/store/refs.py`
- Modify: `backend/scadbuddy/core/paths.py`
- Test: `backend/tests/test_blob_store.py` (refs part `requires_postgres`)

**Interfaces:**
- Produces:
  ```python
  class BlobStore(Protocol):                       # store/__init__.py
      backend: str
      def dir_for(self, key: str) -> Path           # where a piece writes/reads its files; created on demand
      def exists(self, key: str) -> bool
      def remove(self, key: str) -> None
      def keys(self) -> list[str]
      def touched_at(self, key: str) -> float       # mtime of the dir, for the sweep
  class LocalBlobStore(BlobStore): backend = "local"; __init__(self, root: Path)
  class BlobRefs:                                   # store/refs.py, Postgres table blob_refs
      def __init__(self, pool: ConnectionPool[Connection[DictRow]])
      def add(self, key: str, holder_kind: str, holder_id: str) -> None
      def drop_holder(self, holder_kind: str, holder_id: str) -> None
      def referenced(self) -> set[str]
  def sweep_blobs(store: BlobStore, refs: BlobRefs, *, grace: float, now: float | None = None) -> list[str]
  DataPaths.blobs -> Path  (root / "blobs")
  ```
  `JobProjection` exposes its pool as `projection.pool` so `BlobRefs` shares it.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_blob_store.py`:

```python
from __future__ import annotations

import time
from pathlib import Path

import pytest

from scadbuddy.store import sweep_blobs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.store.refs import BlobRefs


def test_dir_for_creates_and_finds_a_key(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    directory = store.dir_for("abc")
    assert directory.is_dir() and directory == tmp_path / "blobs" / "abc"
    assert store.exists("abc") and store.keys() == ["abc"]
    store.remove("abc")
    assert not store.exists("abc")


def test_keys_are_confined_to_the_root(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    with pytest.raises(ValueError):
        store.dir_for("../escape")


@pytest.mark.requires_postgres
def test_sweep_removes_only_unreferenced_blobs_past_grace(tmp_path: Path, pg_conninfo: str) -> None:
    from scadbuddy.render.projection import JobProjection

    projection = JobProjection(pg_conninfo, pool_size=2)
    projection.open()
    try:
        refs = BlobRefs(projection.pool)
        store = LocalBlobStore(tmp_path / "blobs")
        for key in ("kept", "fresh", "stale"):
            (store.dir_for(key) / "model.3mf").write_bytes(b"x")
        refs.add("kept", "job", "j1")
        old = time.time() - 7200
        import os

        os.utime(store.dir_for("stale"), (old, old))
        os.utime(store.dir_for("kept"), (old, old))
        assert sweep_blobs(store, refs, grace=3600) == ["stale"]
        assert store.exists("kept") and store.exists("fresh")
        refs.drop_holder("job", "j1")
        assert sweep_blobs(store, refs, grace=3600) == ["kept"]
    finally:
        projection.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_blob_store.py -q`
Expected: FAIL — `ModuleNotFoundError: scadbuddy.store`.

- [ ] **Step 3: Paths**

In `backend/scadbuddy/core/paths.py`, beside `jobs`:

```python
    @property
    def blobs(self) -> Path:
        """Rendered pieces, keyed by `piece_key` (spec §6.2's `local` backend)."""
        return self.root / "blobs"
```

and add `self.blobs` to the list `ensure()` creates.

- [ ] **Step 4: Implement**

`backend/scadbuddy/store/__init__.py`:

```python
"""The blob store (spec 2026-09-27 §6). Phase 1 ships the interface directory-shaped
over the data volume: a blob is the directory a piece rendered into, referenced by the
jobs (and later outputs) that need it, swept when nothing does. Phase 3 (#426) adds the
Bambuddy backend and the byte-stream calls."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol

from scadbuddy.store.refs import BlobRefs


class BlobStore(Protocol):
    backend: str

    def dir_for(self, key: str) -> Path: ...
    def exists(self, key: str) -> bool: ...
    def remove(self, key: str) -> None: ...
    def keys(self) -> list[str]: ...
    def touched_at(self, key: str) -> float: ...


def sweep_blobs(store: BlobStore, refs: BlobRefs, *, grace: float, now: float | None = None) -> list[str]:
    """Remove every blob nothing references that has not been touched for ``grace``."""
    cutoff = (now if now is not None else time.time()) - grace
    kept = refs.referenced()
    removed: list[str] = []
    for key in store.keys():
        if key in kept or store.touched_at(key) > cutoff:
            continue
        store.remove(key)
        removed.append(key)
    return removed


__all__ = ["BlobRefs", "BlobStore", "sweep_blobs"]
```

`backend/scadbuddy/store/local.py`:

```python
from __future__ import annotations

import re
import shutil
from pathlib import Path

_KEY = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class LocalBlobStore:
    backend = "local"

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        if not _KEY.match(key) or key in (".", ".."):
            raise ValueError(f"not a blob key: {key!r}")
        return self.root / key

    def dir_for(self, key: str) -> Path:
        path = self._path(key)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).is_dir()

    def remove(self, key: str) -> None:
        shutil.rmtree(self._path(key), ignore_errors=True)

    def keys(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def touched_at(self, key: str) -> float:
        return self._path(key).stat().st_mtime
```

`backend/scadbuddy/store/refs.py`:

```python
from __future__ import annotations

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool


class BlobRefs:
    """`blob_refs`: which holders (a job now; outputs and presets later) keep a blob."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    def add(self, key: str, holder_kind: str, holder_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO blob_refs (key, holder_kind, holder_id) VALUES (%s, %s, %s)"
                " ON CONFLICT DO NOTHING",
                (key, holder_kind, holder_id),
            )

    def drop_holder(self, holder_kind: str, holder_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM blob_refs WHERE holder_kind = %s AND holder_id = %s",
                (holder_kind, holder_id),
            )

    def referenced(self) -> set[str]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT DISTINCT key FROM blob_refs").fetchall()
        return {row["key"] for row in rows}
```

In `JobProjection` add `@property def pool(self) -> ConnectionPool[Connection[DictRow]]: return self._pool`.

- [ ] **Step 5: Run the tests**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/test_blob_store.py -q`
Expected: PASS (3).

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/store backend/scadbuddy/core/paths.py backend/scadbuddy/render/projection.py backend/tests/test_blob_store.py
git commit -m "feat(store): directory-shaped local blob store with Postgres refs and a grace sweep (#424)"
```

---

### Task 5: Workflows and activities

**Files:**
- Create: `backend/scadbuddy/workflows/__init__.py`, `models.py`, `activities.py`, `pipelines.py`, `client.py`
- Modify: `backend/scadbuddy/render/jobs.py` (extract stage functions; delete the queue)
- Modify: `backend/scadbuddy/render/runner.py` (`run_openscad` process group)
- Create: `backend/tests/support/__init__.py`, `backend/tests/support/temporal.py`
- Test: `backend/tests/test_workflows.py` (`requires_temporal`), `backend/tests/test_runner.py` (append)

**Interfaces:**
- Produces (`workflows/models.py`):
  ```python
  class PieceRequest(BaseModel): slug: str; revision: str | None; file: str = "model.scad"; params: dict[str, ParamValue]; piece_key: str
  class PrepareResult(BaseModel): version: str; scad: str; library_path: list[str]; schema_cache: str
  class RenderMainResult(BaseModel): plates: int; log_tail: list[str]; diagnostics: list[Diagnostic]; diagnostics_dropped: int; notes: list[str]; missing_files: list[str]
  class PieceResult(BaseModel): result: JobResult; log_tail: list[str]
  class Failure(BaseModel): error: str; log_tail: list[str] = []; diagnostics: list[Diagnostic] = []; diagnostics_dropped: int = 0; warnings: list[str] = []
  def piece_key(slug: str, revision: str | None, file: str, params: Mapping[str, ParamValue]) -> str
  ```
  Activities (`RenderActivities`, all `@activity.defn(name=...)`): `prepare(PieceRequest) -> PrepareResult`, `render_main(PieceRequest, PrepareResult) -> RenderMainResult`, `render_solids(PieceRequest, PrepareResult, RenderMainResult) -> None` (writes plate files into the blob dir), `finish_piece(PieceRequest, PrepareResult, RenderMainResult) -> PieceResult`, `project(Projection) -> None` where `Projection(BaseModel): job_id: str; slug: str; state: Literal["running","done","failed","cancelled"] | None; steps: list[StepInfo] | None; result: JobResult | None; failure: Failure | None; pipeline_version: str = "default"; blob_key: str | None`.
  Workflows (`workflows/pipelines.py`): `RenderPiece.run(PieceRequest) -> PieceResult` (raises `ApplicationError(type="OpenSCADError", non_retryable=True)` carrying a `Failure` on a render error); `TemplatePipeline.run(job: Job) -> None`.
  `workflows/client.py`: `async def connect(address: str, namespace: str) -> Client` (with `pydantic_data_converter`), `RENDER_TASK_QUEUE_DEFAULT = "render"`, `def render_worker(client, task_queue, activities: RenderActivities, *, build_id: str, max_concurrent_activities: int) -> Worker`.
- Consumes: `JobProjection` (Task 2), `LocalBlobStore`/`BlobRefs` (Task 4), `Config.activity_timeout` (Task 1).

- [ ] **Step 1: Extract the stages from `render_job`**

In `backend/scadbuddy/render/jobs.py`, replace `render_job` (lines 592–711) with four functions the activities call, each taking a `work: Path` (the blob dir) instead of `attempt_work_dir`:

```python
@dataclass(frozen=True)
class Prepared:
    scad: Path
    version: str
    library_path: tuple[Path, ...]
    schema_cache: Path


async def prepare_source(
    slug: str,
    revision: str | None,
    *,
    config: Config,
    paths: DataPaths,
    history: ModelHistory | None,
    fetcher: CheckoutFetcher | None,
) -> tuple[Prepared, Config]:
    source = await resolve_source(slug, revision, paths=paths, history=history, fetcher=fetcher)
    version = source.version
    config = source.configure(config)
    if version is None:
        version = await asyncio.to_thread(source_version, source.scad.parent)
    return Prepared(source.scad, version, tuple(source.library_path), source.schema_cache), config


async def render_main(
    prepared: Prepared, params: Mapping[str, ParamValue], work: Path, *, config: Config,
    assets: AssetStore, checkouts: CheckoutGate | None, holder: str,
) -> RenderOutput:
    """Today's "render" stage: the customizer schema, then the raw multi-material 3MF."""
    async with library_lease(checkouts, holder, prepared.library_path):
        schema = await cached_schema(prepared.scad, prepared.schema_cache, config=config)
        work.mkdir(parents=True, exist_ok=True)
        with staged_assets(schema, params, prepared.scad.parent, assets) as staged:
            try:
                return await render_3mf(prepared.scad, schema, staged, work / RAW_RENDER_NAME, config=config)
            except OpenSCADError as error:
                error.warnings = failed_render_warnings(error.missing_files, schema, params)
                raise


async def render_solids_stage(
    prepared: Prepared, params: Mapping[str, ParamValue], work: Path, plates: int, *,
    config: Config, assets: AssetStore, checkouts: CheckoutGate | None, holder: str,
) -> PlateLayout:
    """Today's "split" + "solids" stages: preview parts, GLB, then one closed solid per
    colour per plate. Writes `preview.glb` and returns the layout; `finish_piece`
    re-reads the layout's plate files, so this activity's output is on disk."""
    async with library_lease(checkouts, holder, prepared.library_path):
        schema = await cached_schema(prepared.scad, prepared.schema_cache, config=config)
        with staged_assets(schema, params, prepared.scad.parent, assets) as staged:
            preview_parts = extruder_order(split_by_material(work / RAW_RENDER_NAME), schema, staged)
            if not preview_parts:
                raise OpenSCADError("the render produced no geometry", [], warnings=failed_render_warnings([], schema, params))
            write_glb(preview_parts, work / PREVIEW_NAME)
            layout = await plate_layout(prepared.scad, schema, staged, preview_parts, plates, work, config=config)
    layout.save(work / LAYOUT_NAME)   # new: a small JSON of plate part files + colours + warnings
    return layout


async def finish_piece_stage(
    prepared: Prepared, params: Mapping[str, ParamValue], work: Path, output: RenderOutput, *,
    config: Config, paths: DataPaths, slug: str, thumbnail_executor: Executor | None,
) -> JobResult:
    """Today's "thumbnail" + "write" stages, from the layout the solids stage saved."""
    layout = PlateLayout.load(work / LAYOUT_NAME)
    schema = await cached_schema(prepared.scad, prepared.schema_cache, config=config)
    warnings = [*(MISSING_FILE_WARNING.format(name=n) for n in output.missing_files), *layout.warnings]
    thumbnails, thumbnail_warnings = await plates_thumbnails(
        [plate.parts for plate in layout.plates], config=config, executor=thumbnail_executor
    )
    warnings += thumbnail_warnings + unreadable_colour_warnings(schema, params)
    model_3mf = work / MODEL_NAME
    await asyncio.to_thread(
        write_plates_3mf, layout.plates, layout.colours, model_3mf,
        thumbnails=thumbnails, model_name=slug.removeprefix(BUILTIN_PREFIX),
    )
    box = bounding_box(read_glb(work / PREVIEW_NAME))
    return JobResult(
        model_3mf=str(model_3mf.relative_to(paths.root)),
        preview_glb=str((work / PREVIEW_NAME).relative_to(paths.root)),
        source_version=prepared.version, parts=result_parts(layout), bbox_mm=box,
        colors=list(layout.colours), warnings=warnings, plates=result_plates(layout),
        diagnostics=list(output.diagnostics), diagnostics_dropped=output.diagnostics_dropped,
        notes=list(output.notes),
    )
```

`PlateLayout` gains `save(path)` / `load(path)` (JSON of each plate's part file names, colours and warnings — `ColourPart` meshes are re-read from the files `plate_layout` already writes under `work`; check `plate_layout` for the file names it uses and keep them). `LAYOUT_NAME = "layout.json"` beside the other names. `RenderOutput` is `render_3mf`'s return type (check the name in `runner.py`; if it is a dataclass, add `to_model()`/`from_model()` to move it through `RenderMainResult`).

Delete `RenderQueue`, `_Background`, `RenderCallable`, `INITIAL_RENDER_ESTIMATE`, `RENDER_ESTIMATE_WEIGHT`, `attempt_work_dir`, and the imports of `job_store`, `deque`, `math`, `threading`, `uuid`. Keep `OUTCOME_EVENT_KINDS` and `RenderOutcome` (Task 7's submitter uses them).

- [ ] **Step 2: Process-group kill in the runner**

In `backend/scadbuddy/render/runner.py`, `run_openscad`: pass `start_new_session=True` to `create_subprocess_exec`, and replace both `process.kill()` calls with `_kill_group(process)`:

```python
def _kill_group(process: asyncio.subprocess.Process) -> None:
    """Kill the child and everything it spawned: a template activity's helper, or a
    fontconfig cache rebuild openscad forked (spec §3.4, a phase-1 requirement)."""
    with suppress(ProcessLookupError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
```

Append to `backend/tests/test_runner.py`:

```python
@pytest.mark.asyncio
async def test_a_timed_out_openscad_takes_its_children_with_it(tmp_path: Path) -> None:
    fake = tmp_path / "openscad"
    fake.write_text("#!/bin/sh\nsleep 30 &\necho $! > child.pid\nwait\n", encoding="utf-8")
    fake.chmod(0o755)
    config = Config(data_dir=tmp_path, openscad=str(fake), render_timeout=0.3)
    with pytest.raises(RenderTimeoutError):
        await run_openscad([], cwd=tmp_path, config=config)
    child = int((tmp_path / "child.pid").read_text())
    await asyncio.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
```

(Match `run_openscad`'s real signature from `runner.py:199`; the test above assumes `(args, *, cwd, config)` — adjust.)

- [ ] **Step 3: Write the failing workflow tests**

Create `backend/tests/support/__init__.py` (empty) and `backend/tests/support/temporal.py`:

```python
"""A Temporal for the tests: SCADBUDDY_TEST_TEMPORAL_ADDRESS (a running server, CI's
service or a dev server), else the `temporal` CLI's dev server started here
(SCADBUDDY_TEST_TEMPORAL_DEV_SERVER or `temporal` on PATH), else skip."""

from __future__ import annotations

import os
import shutil
import threading
import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import pytest
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from scadbuddy.workflows.client import pydantic_data_converter

TEST_TEMPORAL_ADDRESS_ENV = "SCADBUDDY_TEST_TEMPORAL_ADDRESS"
TEST_TEMPORAL_DEV_SERVER_ENV = "SCADBUDDY_TEST_TEMPORAL_DEV_SERVER"


def temporal_available() -> bool:
    return bool(
        os.environ.get(TEST_TEMPORAL_ADDRESS_ENV)
        or os.environ.get(TEST_TEMPORAL_DEV_SERVER_ENV)
        or shutil.which("temporal")
    )


@asynccontextmanager
async def temporal_client() -> AsyncIterator[Client]:
    address = os.environ.get(TEST_TEMPORAL_ADDRESS_ENV)
    if address:
        yield await Client.connect(address, data_converter=pydantic_data_converter)
        return
    binary = os.environ.get(TEST_TEMPORAL_DEV_SERVER_ENV) or shutil.which("temporal")
    env = await WorkflowEnvironment.start_local(
        dev_server_existing_path=binary, data_converter=pydantic_data_converter
    )
    try:
        yield env.client
    finally:
        await env.shutdown()


class WorkerThread:
    """Run a Temporal `Worker` on its own loop in a thread, for sync TestClient tests."""

    def __init__(self, make_worker) -> None:  # Callable[[], Awaitable[Worker]]
        self._make_worker = make_worker
        self._stop = asyncio.Event()
        self._thread = threading.Thread(target=self._run, name="temporal-worker", daemon=True)
        self._loop: asyncio.AbstractEventLoop | None = None

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        worker = await self._make_worker()
        async with worker:
            await self._stop.wait()

    def __enter__(self) -> WorkerThread:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=30)
```

Add to `backend/tests/conftest.py`:

```python
from tests.support.temporal import TEST_TEMPORAL_ADDRESS_ENV, temporal_available


@pytest.fixture(autouse=True)
def _skip_without_temporal(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("requires_temporal") and not temporal_available():
        pytest.skip(f"no Temporal: set {TEST_TEMPORAL_ADDRESS_ENV} or put `temporal` on PATH")
```

and register the marker in `pyproject.toml` `[tool.pytest.ini_options] markers` beside `requires_postgres` (check how the existing markers are declared and follow it).

Create `backend/tests/test_workflows.py`:

```python
"""TemplatePipeline and RenderPiece against a Temporal dev server, with the openscad
activities replaced by fakes that record their calls."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.worker import Worker

from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.job_models import Job, JobResult, PartInfo
from scadbuddy.workflows.models import (
    Failure, PieceRequest, PieceResult, PrepareResult, Projection, RenderMainResult, piece_key,
)
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline
from tests.support.temporal import temporal_client

pytestmark = [pytest.mark.requires_temporal, pytest.mark.asyncio]


class FakeActivities:
    """Same activity names as `RenderActivities`; records calls; `fail_main` makes the
    main render raise the way openscad does."""

    def __init__(self, *, fail_main: bool = False, block_solids: asyncio.Event | None = None) -> None:
        self.calls: list[str] = []
        self.projections: list[Projection] = []
        self.fail_main = fail_main
        self.block_solids = block_solids

    @activity.defn(name="prepare")
    async def prepare(self, req: PieceRequest) -> PrepareResult:
        self.calls.append("prepare")
        return PrepareResult(version="v1", scad=f"models/{req.slug}/model.scad", library_path=[], schema_cache="cache/x")

    @activity.defn(name="render_main")
    async def render_main(self, req: PieceRequest, prepared: PrepareResult) -> RenderMainResult:
        self.calls.append("render_main")
        if self.fail_main:
            from temporalio.exceptions import ApplicationError
            raise ApplicationError("openscad exited with 1", Failure(error="openscad exited with 1", log_tail=["ERROR: boom"]), type="OpenSCADError", non_retryable=True)
        return RenderMainResult(plates=1, log_tail=["fine"], diagnostics=[], diagnostics_dropped=0, notes=[], missing_files=[])

    @activity.defn(name="render_solids")
    async def render_solids(self, req: PieceRequest, prepared: PrepareResult, main: RenderMainResult) -> None:
        self.calls.append("render_solids")
        if self.block_solids is not None:
            await self.block_solids.wait()

    @activity.defn(name="finish_piece")
    async def finish_piece(self, req: PieceRequest, prepared: PrepareResult, main: RenderMainResult) -> PieceResult:
        self.calls.append("finish_piece")
        return PieceResult(
            result=JobResult(model_3mf=f"blobs/{req.piece_key}/model.3mf", preview_glb=f"blobs/{req.piece_key}/preview.glb",
                             parts=[PartInfo(name="Color 1", colour="#FF0000", extruder=1, watertight=True)],
                             bbox_mm=BoundingBox(min=(0, 0, 0), max=(1, 1, 1), size=(1, 1, 1))),
            log_tail=main.log_tail,
        )

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> None:
        self.projections.append(projection)


def _job(**params: int) -> Job:
    return Job(id=uuid.uuid4().hex, slug="demo", params=dict(params), inputs={"params": dict(params)}, created_at=datetime.now(UTC))


def _worker(client, queue: str, acts: FakeActivities) -> Worker:
    return Worker(client, task_queue=queue, workflows=[TemplatePipeline, RenderPiece],
                  activities=[acts.prepare, acts.render_main, acts.render_solids, acts.finish_piece, acts.project])


async def test_a_default_render_runs_the_four_stages_and_projects_done() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities()
        async with _worker(client, queue, acts):
            job = _job(width=1)
            await client.execute_workflow(TemplatePipeline.run, job, id=f"render-{job.id}", task_queue=queue)
        assert acts.calls == ["prepare", "render_main", "render_solids", "finish_piece"]
        states = [p.state for p in acts.projections if p.state]
        assert states == ["running", "done"]
        assert acts.projections[-1].result is not None
        assert acts.projections[-1].blob_key == piece_key("demo", None, "model.scad", {"width": 1})


async def test_an_openscad_failure_projects_failed_with_the_log_tail() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        acts = FakeActivities(fail_main=True)
        async with _worker(client, queue, acts):
            job = _job(width=999)
            await client.execute_workflow(TemplatePipeline.run, job, id=f"render-{job.id}", task_queue=queue)
        last = acts.projections[-1]
        assert last.state == "failed" and last.failure is not None
        assert last.failure.log_tail == ["ERROR: boom"]
        assert acts.calls == ["prepare", "render_main"]


async def test_identical_pieces_render_once_across_two_jobs() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(block_solids=gate)
        async with _worker(client, queue, acts):
            a, b = _job(width=2), _job(width=2)
            ha = await client.start_workflow(TemplatePipeline.run, a, id=f"render-{a.id}", task_queue=queue)
            hb = await client.start_workflow(TemplatePipeline.run, b, id=f"render-{b.id}", task_queue=queue)
            while "render_solids" not in acts.calls:
                await asyncio.sleep(0.05)
            gate.set()
            await asyncio.gather(ha.result(), hb.result())
        assert acts.calls.count("render_main") == 1
        assert [p.state for p in acts.projections if p.state == "done"] == ["done", "done"]


async def test_cancelling_one_parent_leaves_a_shared_piece_running() -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        acts = FakeActivities(block_solids=gate)
        async with _worker(client, queue, acts):
            a, b = _job(width=3), _job(width=3)
            ha = await client.start_workflow(TemplatePipeline.run, a, id=f"render-{a.id}", task_queue=queue)
            hb = await client.start_workflow(TemplatePipeline.run, b, id=f"render-{b.id}", task_queue=queue)
            while "render_solids" not in acts.calls:
                await asyncio.sleep(0.05)
            await ha.cancel()
            with pytest.raises(WorkflowFailureError):
                await ha.result()
            gate.set()
            await hb.result()
        assert acts.calls.count("render_main") == 1
        assert acts.calls.count("finish_piece") == 1
        cancelled = [p for p in acts.projections if p.job_id == a.id and p.state]
        assert cancelled[-1].state == "cancelled"


async def test_a_piece_resumes_from_the_activity_it_was_on() -> None:
    """Worker dies after render_main: a new worker finishes without re-running it."""
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        gate = asyncio.Event()
        first = FakeActivities(block_solids=gate)
        job = _job(width=4)
        handle = None
        async with _worker(client, queue, first):
            handle = await client.start_workflow(TemplatePipeline.run, job, id=f"render-{job.id}", task_queue=queue)
            while "render_solids" not in first.calls:
                await asyncio.sleep(0.05)
        # first worker gone mid-solids; a second one picks the activity up
        second = FakeActivities()
        async with _worker(client, queue, second):
            assert handle is not None
            await handle.result()
        assert "render_main" not in second.calls
        assert second.calls == ["render_solids", "finish_piece"]
```

- [ ] **Step 4: Run to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_workflows.py -q`
Expected: FAIL — `ModuleNotFoundError: scadbuddy.workflows`.

- [ ] **Step 5: Models**

`backend/scadbuddy/workflows/models.py`:

```python
"""Payloads between the workflows and their activities (spec 2026-09-27 §3.4)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field

from scadbuddy.render.diagnostics import Diagnostic
from scadbuddy.render.job_models import JobResult, StepInfo
from scadbuddy.render.schema import ParamValue


def piece_key(slug: str, revision: str | None, file: str, params: Mapping[str, ParamValue]) -> str:
    """One openscad invocation (§3.4): the slug is in it because a revision is the
    template's own last commit, which one commit can give to many templates."""
    raw = json.dumps([slug, revision, file, dict(params)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class PieceRequest(BaseModel):
    slug: str
    revision: str | None
    file: str = "model.scad"
    params: dict[str, ParamValue] = Field(default_factory=dict)
    piece_key: str


class PrepareResult(BaseModel):
    version: str
    scad: str
    library_path: list[str] = Field(default_factory=list)
    schema_cache: str


class RenderMainResult(BaseModel):
    plates: int = 1
    log_tail: list[str] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    diagnostics_dropped: int = 0
    notes: list[str] = Field(default_factory=list)
    missing_files: list[str] = Field(default_factory=list)


class PieceResult(BaseModel):
    result: JobResult
    log_tail: list[str] = Field(default_factory=list)


class Failure(BaseModel):
    error: str
    log_tail: list[str] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    diagnostics_dropped: int = 0
    warnings: list[str] = Field(default_factory=list)


class Projection(BaseModel):
    job_id: str
    slug: str
    state: Literal["running", "done", "failed", "cancelled"] | None = None
    steps: list[StepInfo] | None = None
    result: JobResult | None = None
    log_tail: list[str] = Field(default_factory=list)
    failure: Failure | None = None
    pipeline_version: str = "default"
    #: The piece the result lives in; `project` adds the job's blob ref (Task 4).
    blob_key: str | None = None
```

- [ ] **Step 6: Client helpers**

`backend/scadbuddy/workflows/client.py`:

```python
from __future__ import annotations

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker, WorkerDeploymentConfig, WorkerDeploymentVersion

from scadbuddy.workflows.activities import RenderActivities
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline

RENDER_TASK_QUEUE_DEFAULT = "render"
DEPLOYMENT_NAME = "scadbuddy-render"


async def connect(address: str, namespace: str) -> Client:
    return await Client.connect(address, namespace=namespace, data_converter=pydantic_data_converter)


def render_worker(
    client: Client,
    task_queue: str,
    activities: RenderActivities,
    *,
    build_id: str,
    max_concurrent_activities: int,
) -> Worker:
    """The render worker (spec §3.5): versioned by build id so a rolling deploy lets
    old workers drain the workflows they started."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[TemplatePipeline, RenderPiece],
        activities=activities.all(),
        max_concurrent_activities=max_concurrent_activities,
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(deployment_name=DEPLOYMENT_NAME, build_id=build_id),
            use_worker_versioning=True,
        ),
    )


__all__ = ["RENDER_TASK_QUEUE_DEFAULT", "connect", "pydantic_data_converter", "render_worker"]
```

If `use_worker_versioning=True` makes the dev-server test in Step 11 fail with an "unversioned task queue" error, the dev server needs versioning enabled: pass `dev_server_extra_args=["--dynamic-config-value", "system.enableDeploymentVersions=true"]` in `tests/support/temporal.py`'s `start_local`. Record the outcome in the spec's §3.6 list when this task lands.

- [ ] **Step 7: Activities**

`backend/scadbuddy/workflows/activities.py`:

```python
"""The render activities: today's `render_job` stages, each one an activity that
reads and writes the piece's directory in the blob store (spec §3.4)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from pathlib import Path

from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.core.config import Config
from scadbuddy.core.events import EventBus
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import CheckoutFetcher, CheckoutGate
from scadbuddy.render.job_models import Job
from scadbuddy.render.jobs import (
    Prepared, finish_piece_stage, prepare_source, render_main, render_solids_stage,
)
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.runner import OpenSCADError, RenderOutput
from scadbuddy.store import BlobRefs, BlobStore
from scadbuddy.workflows.models import (
    Failure, PieceRequest, PieceResult, PrepareResult, Projection, RenderMainResult,
)


@dataclass
class WorkerDeps:
    config: Config
    paths: DataPaths
    assets: AssetStore
    blobs: BlobStore
    refs: BlobRefs
    projection: JobProjection
    history: ModelHistory | None = None
    checkouts: CheckoutGate | None = None
    fetcher: CheckoutFetcher | None = None
    thumbnail_executor: Executor | None = None
    metrics: Metrics | None = None


def _failure(error: OpenSCADError) -> ApplicationError:
    return ApplicationError(
        str(error),
        Failure(
            error=str(error), log_tail=list(error.log_tail), diagnostics=list(error.diagnostics),
            diagnostics_dropped=error.diagnostics_dropped, warnings=list(error.warnings),
        ),
        type="OpenSCADError",
        non_retryable=True,
    )


async def _heartbeating[T](work: asyncio.Future[T] | asyncio.Task[T], every: float = 5.0) -> T:
    """Heartbeat while a long openscad run is on; cancellation kills it (the runner
    kills the process group on CancelledError)."""
    while True:
        done, _ = await asyncio.wait({work}, timeout=every)
        if done:
            return work.result()
        activity.heartbeat()


class RenderActivities:
    def __init__(self, deps: WorkerDeps) -> None:
        self.deps = deps

    def all(self) -> Sequence[object]:
        return [self.prepare, self.render_main, self.render_solids, self.finish_piece, self.project]

    def _prepared(self, result: PrepareResult) -> Prepared:
        return Prepared(Path(result.scad), result.version, tuple(Path(p) for p in result.library_path), Path(result.schema_cache))

    def _work(self, req: PieceRequest) -> Path:
        return self.deps.blobs.dir_for(req.piece_key)

    @activity.defn(name="prepare")
    async def prepare(self, req: PieceRequest) -> PrepareResult:
        d = self.deps
        prepared, _ = await prepare_source(req.slug, req.revision, config=d.config, paths=d.paths, history=d.history, fetcher=d.fetcher)
        return PrepareResult(version=prepared.version, scad=str(prepared.scad), library_path=[str(p) for p in prepared.library_path], schema_cache=str(prepared.schema_cache))

    @activity.defn(name="render_main")
    async def render_main(self, req: PieceRequest, prepared: PrepareResult) -> RenderMainResult:
        d = self.deps
        config = d.config  # `Prepared` carried the configured library path; re-apply:
        try:
            output: RenderOutput = await _heartbeating(asyncio.ensure_future(render_main(
                self._prepared(prepared), req.params, self._work(req), config=config,
                assets=d.assets, checkouts=d.checkouts, holder=f"piece:{req.piece_key}",
            )))
        except OpenSCADError as error:
            raise _failure(error) from None
        return RenderMainResult(plates=output.plates or 1, log_tail=list(output.log_tail), diagnostics=list(output.diagnostics),
                                diagnostics_dropped=output.diagnostics_dropped, notes=list(output.notes), missing_files=list(output.missing_files))

    @activity.defn(name="render_solids")
    async def render_solids(self, req: PieceRequest, prepared: PrepareResult, main: RenderMainResult) -> None:
        d = self.deps
        try:
            await _heartbeating(asyncio.ensure_future(render_solids_stage(
                self._prepared(prepared), req.params, self._work(req), main.plates, config=d.config,
                assets=d.assets, checkouts=d.checkouts, holder=f"piece:{req.piece_key}",
            )))
        except OpenSCADError as error:
            raise _failure(error) from None

    @activity.defn(name="finish_piece")
    async def finish_piece(self, req: PieceRequest, prepared: PrepareResult, main: RenderMainResult) -> PieceResult:
        d = self.deps
        output = RenderOutput.from_result(main)  # rebuild the fields finish needs
        result = await finish_piece_stage(self._prepared(prepared), req.params, self._work(req), output,
                                          config=d.config, paths=d.paths, slug=req.slug, thumbnail_executor=d.thumbnail_executor)
        return PieceResult(result=result, log_tail=main.log_tail)

    @activity.defn(name="project")
    async def project(self, projection: Projection) -> None:
        """Move the row forward; a no-op when the row is already past this state."""
        p = self.deps.projection
        if projection.state == "running":
            await asyncio.to_thread(p.mark_started, projection.job_id)
            return
        if projection.steps is not None and projection.state is None:
            await asyncio.to_thread(p.set_steps, projection.job_id, projection.steps)
            return
        job = await asyncio.to_thread(p.read, projection.job_id)
        if projection.state is None:
            return
        job.state = projection.state
        job.pipeline_version = projection.pipeline_version
        if projection.steps is not None:
            job.steps = projection.steps
        if projection.result is not None:
            job.result = projection.result
            job.log_tail = projection.log_tail
            job.warnings = list(projection.result.warnings)
            job.diagnostics = list(projection.result.diagnostics)
            job.diagnostics_dropped = projection.result.diagnostics_dropped
            if projection.blob_key:
                await asyncio.to_thread(self.deps.refs.add, projection.blob_key, "job", job.id)
        if projection.failure is not None:
            f = projection.failure
            job.error, job.log_tail, job.diagnostics = f.error, f.log_tail, f.diagnostics
            job.diagnostics_dropped, job.warnings = f.diagnostics_dropped, f.warnings
        await asyncio.to_thread(p.finish, job)
```

Adjust the two places marked with comments to the real `RenderOutput` type in `runner.py` (add `from_result(main: RenderMainResult)` there, or build the object inline).

- [ ] **Step 8: Workflows**

`backend/scadbuddy/workflows/pipelines.py`:

```python
"""TemplatePipeline and RenderPiece (spec 2026-09-27 §3.4). Phase 1 runs only the
built-in default pipeline: one piece, one plate layout, one output."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from scadbuddy.render.job_models import Job, StepInfo
    from scadbuddy.workflows.models import (
        Failure, PieceRequest, PieceResult, PrepareResult, Projection, RenderMainResult, piece_key,
    )

RETRY = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=2), backoff_coefficient=2.0)
SHORT = timedelta(seconds=60)


def _openscad_timeout() -> timedelta:
    # `activity_timeout` (render_timeout + margin) travels in the workflow memo, set
    # by the submitter, so the workflow stays deterministic across config changes.
    memo = workflow.memo_value("activity_timeout", default=180.0, type_hint=float)
    return timedelta(seconds=memo)


def _failure_of(error: BaseException) -> Failure | None:
    cause = error
    while cause is not None:
        if isinstance(cause, ApplicationError) and cause.type == "OpenSCADError" and cause.details:
            detail = cause.details[0]
            return detail if isinstance(detail, Failure) else Failure.model_validate(detail)
        cause = cause.__cause__
    return None


@workflow.defn(name="RenderPiece")
class RenderPiece:
    @workflow.run
    async def run(self, req: PieceRequest) -> PieceResult:
        prepared: PrepareResult = await workflow.execute_activity(
            "prepare", req, start_to_close_timeout=SHORT, retry_policy=RETRY
        )
        main: RenderMainResult = await workflow.execute_activity(
            "render_main", args=[req, prepared], start_to_close_timeout=_openscad_timeout(),
            heartbeat_timeout=timedelta(seconds=30), retry_policy=RETRY,
        )
        await workflow.execute_activity(
            "render_solids", args=[req, prepared, main], start_to_close_timeout=_openscad_timeout(),
            heartbeat_timeout=timedelta(seconds=30), retry_policy=RETRY,
        )
        result: PieceResult = await workflow.execute_activity(
            "finish_piece", args=[req, prepared, main], start_to_close_timeout=SHORT, retry_policy=RETRY
        )
        return result


@workflow.defn(name="TemplatePipeline")
class TemplatePipeline:
    @workflow.run
    async def run(self, job: Job) -> None:
        async def project(**fields: object) -> None:
            await workflow.execute_activity(
                "project", Projection(job_id=job.id, slug=job.slug, **fields),
                start_to_close_timeout=SHORT, retry_policy=RETRY,
            )

        await project(state="running")
        params = job.inputs.get("params", job.params) if job.inputs else job.params
        key = piece_key(job.slug, job.model_version, "model.scad", params)
        req = PieceRequest(slug=job.slug, revision=job.model_version, params=dict(params), piece_key=key)
        steps = [StepInfo(name="render", state="running", done=0, total=1)]
        await project(steps=steps)
        try:
            piece: PieceResult = await workflow.execute_child_workflow(
                RenderPiece.run, req, id=f"piece-{key}",
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
        except (ChildWorkflowError, ActivityError) as error:
            failure = _failure_of(error)
            if failure is None:
                failure = Failure(error=f"{type(error).__name__}: {error}")
            steps[0].state = "failed"
            await project(state="failed", failure=failure, steps=steps)
            return
        except workflow.CancelledError:  # noqa: F841 - a superseded/withdrawn job
            # The row was already moved to cancelled by the API (release_claim); the
            # projection here is idempotent for the case the API did not.
            await project(state="cancelled", failure=Failure(error="cancelled"), steps=steps)
            raise
        steps[0].state, steps[0].done = "done", 1
        await project(state="done", result=piece.result, log_tail=piece.log_tail, steps=steps, blob_key=key)
```

`workflow.CancelledError` is `asyncio.CancelledError` inside the sandbox; import it as `import asyncio` under `imports_passed_through` if the attribute does not exist in 1.33 — check `temporalio.workflow` for `CancelledError`; otherwise catch `asyncio.CancelledError`.

`backend/scadbuddy/workflows/__init__.py`:

```python
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline

__all__ = ["RenderPiece", "TemplatePipeline"]
```

- [ ] **Step 9: Run the workflow tests**

Run: `cd backend && uv run --frozen pytest tests/test_workflows.py -q` (with `temporal` on PATH: `brew install temporal` / the CLI release; or `SCADBUDDY_TEST_TEMPORAL_ADDRESS=127.0.0.1:7233` against `temporal server start-dev`).
Expected: PASS (5). If `execute_child_workflow` with `USE_EXISTING` in `test_identical_pieces_render_once_across_two_jobs` starts two children, the SDK's child-start conflict policy is not honoured in 1.33 — fall back to `workflow.start_child_workflow(...)` catching `WorkflowAlreadyStartedError` and then `workflow.get_external_workflow_handle(...)`; record the finding in the spec's §3.6 list.

- [ ] **Step 10: Run the runner test and lint**

Run: `cd backend && uv run --frozen pytest tests/test_runner.py -q && uv run --frozen ruff check . && uv run --frozen ruff format .`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add backend/scadbuddy/workflows backend/scadbuddy/render/jobs.py backend/scadbuddy/render/runner.py backend/tests/support backend/tests/test_workflows.py backend/tests/test_runner.py backend/tests/conftest.py backend/pyproject.toml
git commit -m "feat(render): TemplatePipeline and RenderPiece workflows over four render activities (#424)"
```

---

### Task 6: The worker process

**Files:**
- Create: `backend/scadbuddy/worker.py`
- Test: `backend/tests/test_worker.py` (`requires_postgres`, `requires_temporal`)
- Modify: `Dockerfile` (comment on the worker command; Temporal CLI in `test`)

**Interfaces:**
- Produces: `def build_worker_deps(settings: Settings) -> WorkerDeps`, `async def run_worker(settings: Settings, *, stop: asyncio.Event | None = None, health_port: int | None = 9090) -> None`, `async def run_inprocess_worker(settings, deps: WorkerDeps, client: Client, stop: asyncio.Event) -> None`, `if __name__ == "__main__": main()` handling SIGTERM → graceful `Worker.shutdown()` (drains in-flight activities up to `graceful_shutdown_timeout=render_timeout+margin`).
- The health/metrics server: `GET /healthz` → `{"ok": true, "build_id": …, "task_queue": …}`; `GET /metrics` → Prometheus exposition of the worker's registry (render stages/duration).

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_worker.py`:

```python
from __future__ import annotations

import asyncio
import socket
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from scadbuddy.core.settings import Settings
from scadbuddy.render.job_models import Job
from scadbuddy.render.projection import JobProjection, render_key
from scadbuddy.worker import build_worker_deps, run_worker
from scadbuddy.workflows.pipelines import TemplatePipeline
from tests.support.temporal import temporal_client

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal, pytest.mark.asyncio]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_the_worker_renders_a_job_end_to_end_with_the_fake_openscad(
    settings: Settings, model: str, pg_conninfo: str
) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        port = _free_port()
        cfg = settings.model_copy(update={"database_url": pg_conninfo, "temporal_task_queue_render": queue})
        stop = asyncio.Event()
        projection = JobProjection(pg_conninfo, pool_size=2)
        projection.open()
        task = asyncio.create_task(run_worker(cfg, stop=stop, health_port=port, client=client))
        try:
            async with httpx.AsyncClient() as http:
                for _ in range(100):
                    try:
                        if (await http.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200:
                            break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.05)
            job = Job(id=uuid.uuid4().hex, slug=model, params={"width": 12}, inputs={"params": {"width": 12}}, created_at=datetime.now(UTC))
            projection.submit(job, render_key(model, job.params, None))
            await client.execute_workflow(TemplatePipeline.run, job, id=f"render-{job.id}", task_queue=queue)
            stored = projection.read(job.id)
            assert stored.state in ("done", "failed"), stored.error
            metrics = (await httpx.AsyncClient().get(f"http://127.0.0.1:{port}/metrics")).text
            assert "scadbuddy_render_duration_seconds" in metrics
        finally:
            stop.set()
            await asyncio.wait_for(task, 30)
            projection.close()
```

(`settings`, `model` come from `tests/api/conftest.py` — move those two fixtures, `fake_openscad` and `data_dir`/`seed_dir`, up to `tests/conftest.py` so this test can use them; Task 7 needs that too.)

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && … uv run --frozen pytest tests/test_worker.py -q`
Expected: FAIL — `ModuleNotFoundError: scadbuddy.worker`.

- [ ] **Step 3: Implement the worker**

`backend/scadbuddy/worker.py`:

```python
"""`python -m scadbuddy.worker`: the render worker (spec 2026-09-27 §3.1).

Same image as the API, its own Deployment. Holds the data volume and the database
(the projection and blob refs) and nothing else — no Bambuddy key with Manage Queue,
no agent secret (§9). Phase 1 runs one replica beside the API on the same volume.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST
from temporalio.client import Client

from scadbuddy.core.config import ACTIVITY_TIMEOUT_MARGIN
from scadbuddy.core.logging import configure_logging
from scadbuddy.core.metrics import Metrics
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.settings import Settings
from scadbuddy.library.assets import AssetStore
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import CheckoutFetcher, CheckoutGate, LibraryStore
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.solids import WRAPPER_PREFIX
from scadbuddy.store import BlobRefs
from scadbuddy.store.local import LocalBlobStore
from scadbuddy.workflows.activities import RenderActivities, WorkerDeps
from scadbuddy.workflows.client import connect, render_worker

logger = logging.getLogger(__name__)
INSTALL_CONCURRENCY = 2


def build_worker_deps(settings: Settings) -> WorkerDeps:
    config = settings.to_config()
    paths = DataPaths(root=settings.data_dir)
    paths.ensure()
    history = ModelHistory(paths.models, wrapper_prefix=WRAPPER_PREFIX, timeout=config.git_timeout)
    checkouts = CheckoutGate()
    libraries = LibraryStore(paths, max_bytes=config.library_max_bytes)
    fetcher = CheckoutFetcher(libraries, asyncio.Semaphore(INSTALL_CONCURRENCY), checkouts)
    projection = JobProjection(settings.database_url, pool_size=settings.database_pool_size)
    projection.open()
    metrics = Metrics()
    metrics.build_info.labels(settings.version, settings.revision).set(1)
    return WorkerDeps(
        config=config,
        paths=paths,
        assets=AssetStore(paths.assets, max_total_bytes=config.asset_max_total_bytes, max_count=config.asset_max_count),
        blobs=LocalBlobStore(paths.blobs),
        refs=BlobRefs(projection.pool),
        projection=projection,
        history=history,
        checkouts=checkouts,
        fetcher=fetcher,
        thumbnail_executor=ThreadPoolExecutor(max_workers=config.render_concurrency, thread_name_prefix="thumbnail"),
        metrics=metrics,
    )


def _health_app(deps: WorkerDeps, settings: Settings) -> FastAPI:
    app = FastAPI(openapi_url=None)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True, "build_id": settings.revision, "task_queue": settings.temporal_task_queue_render})

    @app.get("/metrics")
    def metrics() -> Response:
        assert deps.metrics is not None
        return Response(deps.metrics.exposition(), media_type=CONTENT_TYPE_LATEST)

    return app


async def run_worker(
    settings: Settings,
    *,
    stop: asyncio.Event | None = None,
    health_port: int | None = 9090,
    client: Client | None = None,
) -> None:
    deps = build_worker_deps(settings)
    client = client or await connect(settings.temporal_address, settings.temporal_namespace)
    worker = render_worker(
        client, settings.temporal_task_queue_render, RenderActivities(deps),
        build_id=settings.revision, max_concurrent_activities=deps.config.render_concurrency,
    )
    worker.graceful_shutdown_timeout = timedelta(seconds=deps.config.render_timeout + ACTIVITY_TIMEOUT_MARGIN)  # noqa: E501
    stop = stop or asyncio.Event()
    server: uvicorn.Server | None = None
    tasks: list[asyncio.Task[None]] = []
    if health_port is not None:
        server = uvicorn.Server(uvicorn.Config(_health_app(deps, settings), host="0.0.0.0", port=health_port, log_level="warning"))
        tasks.append(asyncio.create_task(server.serve()))
    logger.info("render worker started", extra={"task_queue": settings.temporal_task_queue_render, "build_id": settings.revision})
    try:
        async with worker:
            await stop.wait()
    finally:
        if server is not None:
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        deps.projection.close()
        if deps.thumbnail_executor is not None:
            deps.thumbnail_executor.shutdown(wait=False, cancel_futures=True)


async def run_inprocess_worker(settings: Settings, deps: WorkerDeps, client: Client, stop: asyncio.Event) -> None:
    """The API's dev/test mode (`SCADBUDDY_TEMPORAL_WORKER_INPROCESS`): same worker,
    the API's own deps, no health server of its own."""
    worker = render_worker(client, settings.temporal_task_queue_render, RenderActivities(deps),
                           build_id=settings.revision, max_concurrent_activities=deps.config.render_concurrency)
    async with worker:
        await stop.wait()


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    stop = asyncio.Event()

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await run_worker(settings, stop=stop)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
```

`Worker.graceful_shutdown_timeout` is a constructor argument in 1.33 — move it into `render_worker(...)` as `graceful_shutdown_timeout=timedelta(...)` and drop the attribute assignment.

- [ ] **Step 4: Temporal CLI in the test image; worker command documented**

In `Dockerfile`, in the `test` stage (before `USER 10001:10001`), add a pinned download of the Temporal CLI (check the latest release at https://github.com/temporalio/cli/releases and pin version + sha256):

```dockerfile
# The Temporal CLI's dev server backs the `requires_temporal` tests
# (tests/support/temporal.py). Pinned by version and digest like every other download.
ARG TEMPORAL_CLI_VERSION=1.4.1
ARG TEMPORAL_CLI_SHA256=<fill from the release's checksums file>
RUN curl -fsSL "https://github.com/temporalio/cli/releases/download/v${TEMPORAL_CLI_VERSION}/temporal_cli_${TEMPORAL_CLI_VERSION}_linux_amd64.tar.gz" -o /tmp/temporal.tgz \
 && echo "${TEMPORAL_CLI_SHA256}  /tmp/temporal.tgz" | sha256sum -c - \
 && tar -xzf /tmp/temporal.tgz -C /usr/local/bin temporal && rm /tmp/temporal.tgz
ENV SCADBUDDY_TEST_TEMPORAL_DEV_SERVER=/usr/local/bin/temporal
```

Above the runtime `CMD`, add:

```dockerfile
# The render worker is the same image with a different command (spec 2026-09-27 §3.1):
#   python -m scadbuddy.worker
# It serves /healthz and /metrics on 9090. eh-homelab/clusters runs it as its own
# Deployment beside the API (one replica in phase 1, sharing /data).
```

hadolint: `curl` with `-f`, pinned versions, `rm` in the same layer — matches `.hadolint.yaml`'s existing rules; run `hadolint --config .hadolint.yaml Dockerfile`.

- [ ] **Step 5: Run the test**

Run: `cd backend && … uv run --frozen pytest tests/test_worker.py -q`
Expected: PASS. (Locally this needs Postgres, `temporal` on PATH and a `fake_openscad`; the `settings` fixture provides the fake.)

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/worker.py backend/tests/test_worker.py backend/tests/conftest.py backend/tests/api/conftest.py Dockerfile
git commit -m "feat(worker): python -m scadbuddy.worker — the render worker with health and metrics (#424)"
```

---

### Task 7: The API submits, reconciles, cancels; the queue is gone

**Files:**
- Create: `backend/scadbuddy/render/submit.py`
- Modify: `backend/scadbuddy/api/deps.py`, `api/jobs.py`, `api/models.py`, `api/metrics.py`, `api/health.py`, `main.py`, `render/previews.py`
- Delete: `backend/scadbuddy/render/job_store.py`, `backend/tests/test_render_queue.py`, `backend/tests/api/test_postgres_queue.py`
- Modify: `backend/tests/api/conftest.py`, `backend/tests/test_previews.py`, `backend/tests/test_jobs.py`
- Test: `backend/tests/test_submit.py` (`requires_postgres`, `requires_temporal`)

**Interfaces:**
- Produces:
  ```python
  class RenderService:                                 # render/submit.py
      def __init__(self, *, projection: JobProjection, client: Client, task_queue: str, config: Config, metrics: Metrics, events: EventBus, reconcile_after: float = 5.0, reconcile_interval: float = 5.0)
      async def start(self) -> None                    # starts the reconciler task
      async def aclose(self) -> None
      async def submit(self, slug: str, params: Mapping[str, ParamValue], *, model_version: str | None, supersedes: str | None) -> Job
      async def cancel(self, job_id: str, *, slug: str) -> Job | None        # release one claim; cancels the workflow on the last
      async def reconcile_once(self) -> int            # re-starts stale pending rows; returns how many
      async def render_preview(self, slug: str) -> bytes   # RenderPreview workflow (below)
      def refresh_metrics(self) -> None
  ```
  A fifth workflow `RenderPreview.run(slug) -> bytes` (id `preview-<slug>`, `USE_EXISTING`) with one activity `render_preview_png(slug) -> bytes` in `RenderActivities`, wrapping today's `render_preview` (the worker owns the openscad run; the API keeps the store write and the "still wanted" check). `PreviewScheduler.__init__` takes `runner: Callable[[str], Awaitable[bytes]]` in place of `queue` + `render`.
  `AppState`: `queue` → `render: RenderService`, plus `projection: JobProjection`, `temporal: Client`, `blobs: LocalBlobStore`, `refs: BlobRefs`. `QueueDep` → `RenderDep`; `get_queue` → `get_render`.
- Consumes: everything above.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_submit.py`:

```python
"""Submit → start → reconcile, supersede/cancel, previews — the API half (spec §3.3)."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from temporalio.client import Client

from scadbuddy.core.config import Config
from scadbuddy.core.events import InProcessEventBus
from scadbuddy.core.metrics import Metrics
from scadbuddy.render.projection import JobProjection
from scadbuddy.render.submit import RenderService
from scadbuddy.workflows.pipelines import RenderPiece, TemplatePipeline
from tests.support.temporal import temporal_client
from tests.test_workflows import FakeActivities, _worker

pytestmark = [pytest.mark.requires_postgres, pytest.mark.requires_temporal, pytest.mark.asyncio]


async def _service(client: Client, pg_conninfo: str, queue: str, **kw: object) -> RenderService:
    projection = JobProjection(pg_conninfo, pool_size=2)
    await asyncio.to_thread(projection.open)
    return RenderService(projection=projection, client=client, task_queue=queue,
                         config=Config(data_dir=__import__("pathlib").Path("/unused")),
                         metrics=Metrics(), events=InProcessEventBus(), reconcile_after=0.5, reconcile_interval=0.5, **kw)


async def test_submit_starts_the_workflow_named_by_the_row(pg_conninfo: str) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        svc = await _service(client, pg_conninfo, queue)
        acts = FakeActivities()
        async with _worker(client, queue, acts):
            job = await svc.submit("demo", {"width": 1}, model_version=None, supersedes=None)
            handle = client.get_workflow_handle(f"render-{job.id}")
            await handle.result()
        assert svc.projection.read(job.id).state == "done"
        await svc.aclose()


async def test_a_submit_whose_workflow_start_fails_is_started_by_the_reconciler(pg_conninfo: str) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        svc = await _service(client, pg_conninfo, queue)
        acts = FakeActivities()
        with patch.object(client, "start_workflow", AsyncMock(side_effect=RuntimeError("temporal down"))):
            job = await svc.submit("demo", {"width": 2}, model_version=None, supersedes=None)
        assert svc.projection.read(job.id).state == "pending"
        await svc.start()
        async with _worker(client, queue, acts):
            for _ in range(200):
                if svc.projection.read(job.id).state == "done":
                    break
                await asyncio.sleep(0.05)
        assert svc.projection.read(job.id).state == "done"
        await svc.aclose()


async def test_superseding_the_last_claim_cancels_the_workflow(pg_conninfo: str) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        svc = await _service(client, pg_conninfo, queue)
        gate = asyncio.Event()
        acts = FakeActivities(block_solids=gate)
        async with _worker(client, queue, acts):
            first = await svc.submit("demo", {"width": 3}, model_version=None, supersedes=None)
            while "render_solids" not in acts.calls:
                await asyncio.sleep(0.05)
            second = await svc.submit("demo", {"width": 4}, model_version=None, supersedes=first.id)
            for _ in range(200):
                if svc.projection.read(first.id).state == "cancelled":
                    break
                await asyncio.sleep(0.05)
            gate.set()
            await client.get_workflow_handle(f"render-{second.id}").result()
        assert svc.projection.read(first.id).state == "cancelled"
        assert svc.projection.read(second.id).state == "done"
        await svc.aclose()


async def test_an_identical_submit_coalesces_and_starts_nothing_new(pg_conninfo: str) -> None:
    async with temporal_client() as client:
        queue = f"t-{uuid.uuid4().hex[:8]}"
        svc = await _service(client, pg_conninfo, queue)
        acts = FakeActivities(block_solids=asyncio.Event())
        async with _worker(client, queue, acts):
            a = await svc.submit("demo", {"width": 5}, model_version=None, supersedes=None)
            b = await svc.submit("demo", {"width": 5}, model_version=None, supersedes=None)
        assert a.id == b.id
        assert svc.metrics.render_coalesced._value.get() == 1  # noqa: SLF001
        await svc.aclose()
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && … uv run --frozen pytest tests/test_submit.py -q`
Expected: FAIL — `ModuleNotFoundError: scadbuddy.render.submit`.

- [ ] **Step 3: `RenderService`**

`backend/scadbuddy/render/submit.py`:

```python
"""Submit → start → reconcile (spec 2026-09-27 §3.3)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.service import RPCError

from scadbuddy.core.config import Config
from scadbuddy.core.events import EventBus
from scadbuddy.core.metrics import Metrics
from scadbuddy.render.job_models import Job, now
from scadbuddy.render.projection import JobProjection, QueueFullError, render_key, workflow_id_for
from scadbuddy.render.schema import ParamValue
from scadbuddy.workflows.pipelines import RenderPreview, TemplatePipeline

logger = logging.getLogger(__name__)


class RenderService:
    def __init__(
        self,
        *,
        projection: JobProjection,
        client: Client,
        task_queue: str,
        config: Config,
        metrics: Metrics,
        events: EventBus,
        reconcile_after: float = 5.0,
        reconcile_interval: float = 5.0,
    ) -> None:
        self.projection = projection
        self.client = client
        self.task_queue = task_queue
        self.config = config
        self.metrics = metrics
        self.events = events
        self.reconcile_after = reconcile_after
        self.reconcile_interval = reconcile_interval
        self._reconciler: asyncio.Task[None] | None = None
        metrics.store_info.labels(projection.backend).set(1)
        metrics.queue_depth_slo.set(config.render_queue_depth_slo)
        metrics.queue_max.set(config.render_queue_max)
        metrics.latency_slo.set(config.render_latency_slo)
        metrics.workers.set(config.render_concurrency)

    async def start(self) -> None:
        await self.reconcile_once()
        self._reconciler = asyncio.create_task(self._reconcile_loop())

    async def aclose(self) -> None:
        if self._reconciler is not None:
            self._reconciler.cancel()
            await asyncio.gather(self._reconciler, return_exceptions=True)
            self._reconciler = None

    async def _start_workflow(self, job: Job) -> None:
        await self.client.start_workflow(
            TemplatePipeline.run,
            job,
            id=workflow_id_for(job.id),
            task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            memo={"activity_timeout": self.config.activity_timeout},
        )

    async def submit(
        self,
        slug: str,
        params: Mapping[str, ParamValue],
        *,
        model_version: str | None,
        supersedes: str | None,
    ) -> Job:
        job = Job(id=uuid.uuid4().hex, slug=slug, params=dict(params), inputs={"params": dict(params)},
                  model_version=model_version, created_at=now())
        try:
            submitted = await asyncio.to_thread(
                self.projection.submit, job, render_key(slug, params, model_version),
                supersedes=supersedes, max_pending=self.config.render_queue_max,
            )
        except QueueFullError as error:
            self.metrics.render_rejected.inc()
            raise QueueFullError(error.depth, self.retry_after()) from None
        if submitted.superseded is not None:
            await self._cancel_workflow(submitted.superseded)
        if submitted.coalesced:
            self.metrics.render_coalesced.inc()
            return submitted.job
        self.metrics.render_submitted.inc()
        try:
            await self._start_workflow(submitted.job)
        except Exception:  # the row is committed; the reconciler starts it (§3.3 step 3)
            logger.exception("could not start the render workflow; the reconciler will", extra={"job": job.id})
            self.metrics.store_errors.labels("start_workflow").inc()
        return submitted.job

    async def cancel(self, job_id: str, *, slug: str) -> Job | None:
        gone = await asyncio.to_thread(self.projection.release_claim, job_id, slug=slug)
        if gone is not None:
            await self._cancel_workflow(gone)
        return gone

    async def _cancel_workflow(self, job: Job) -> None:
        handle: WorkflowHandle[None, None] = self.client.get_workflow_handle(workflow_id_for(job.id))
        try:
            await handle.cancel()
        except RPCError as error:  # never started, or already closed: nothing to cancel
            logger.info("nothing to cancel for a withdrawn job", extra={"job": job.id, "error": str(error)})
        self.metrics.render_finished.labels("superseded").inc()

    async def reconcile_once(self) -> int:
        stale = await asyncio.to_thread(self.projection.stale_pending, self.reconcile_after)
        started = 0
        for job in stale:
            try:
                await self._start_workflow(job)
                started += 1
            except Exception:
                logger.exception("reconciler could not start a pending render", extra={"job": job.id})
                self.metrics.store_errors.labels("reconcile").inc()
        if started:
            logger.warning("reconciler started renders whose submit lost its start", extra={"count": started})
        return started

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reconcile_interval)
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reconciler pass failed")

    async def render_preview(self, slug: str) -> bytes:
        result: bytes = await self.client.execute_workflow(
            RenderPreview.run, slug, id=f"preview-{slug}", task_queue=self.task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            memo={"activity_timeout": self.config.activity_timeout},
        )
        return result

    def retry_after(self) -> int:
        return max(1, round(self.config.render_timeout))

    def refresh_metrics(self) -> None:
        try:
            counts = self.projection.counts()
        except Exception:
            logger.exception("could not read the render projection")
            self.metrics.store_up.set(0)
            self.metrics.store_errors.labels("read").inc()
            return
        self.metrics.store_up.set(1)
        self.metrics.queue_depth.set(counts.pending)
        self.metrics.running.set(counts.running)
        oldest = counts.oldest_pending
        self.metrics.oldest_pending.set(max(0.0, (now() - oldest).total_seconds()) if oldest else 0.0)
```

Add `RenderPreview` to `workflows/pipelines.py`:

```python
@workflow.defn(name="RenderPreview")
class RenderPreview:
    @workflow.run
    async def run(self, slug: str) -> bytes:
        png: bytes = await workflow.execute_activity(
            "render_preview_png", slug, start_to_close_timeout=_openscad_timeout(),
            heartbeat_timeout=timedelta(seconds=30), retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return png
```

and to `RenderActivities`:

```python
    @activity.defn(name="render_preview_png")
    async def render_preview_png(self, slug: str) -> bytes:
        d = self.deps
        return await _heartbeating(asyncio.ensure_future(render_preview(
            slug, config=d.config, paths=d.paths, history=d.history, assets=d.assets,
            executor=d.thumbnail_executor, checkouts=d.checkouts,
        )))
```

(register it in `all()` and in `render_worker`'s `workflows=[…, RenderPreview]`). In `render/previews.py`, `PreviewScheduler.__init__(self, catalogue, store, runner: Callable[[str], Awaitable[bytes]], *, timeout, debounce=…, interval=…)` and `_refresh` calls `await asyncio.wait_for(self.runner(slug), timeout=self.timeout)`.

- [ ] **Step 4: Wire the API**

`api/deps.py` `build_state`: replace the `store`/`queue`/`previews` block with

```python
    projection = JobProjection(settings.database_url, pool_size=settings.database_pool_size)
    blobs = LocalBlobStore(paths.blobs)
    # Opened in the lifespan: `projection.open()` migrates; the Temporal client connects there too.
    render = RenderService(projection=projection, client=_UNCONNECTED, task_queue=settings.temporal_task_queue_render,
                           config=config, metrics=metrics, events=events)
    previews = PreviewScheduler(catalogue, preview_store, render.render_preview, timeout=config.render_timeout * TIMEOUT_FACTOR)
```

Simpler than a sentinel: make `RenderService.client` assignable and set it in the lifespan after `connect(...)`. `AppState` fields: `render: RenderService`, `projection: JobProjection`, `blobs: LocalBlobStore`, `refs: BlobRefs | None` (built after `projection.open()`), `temporal: Client | None`. `get_render(state) -> RenderService`; `RenderDep = Annotated[RenderService, Depends(get_render)]`; `get_projection`/`ProjectionDep` likewise.

`api/jobs.py`: `require_job(projection, job_id)` reads `projection.read`; `render_model` calls `render.submit(...)`; `get_model_diagnostics` uses `projection.latest_finished`. `api/models.py:890`: `projection.has_unfinished(slug)`. `api/metrics.py`: `state.render.refresh_metrics()`. `main.py` `sweep_assets`: `state.projection.list_jobs()`. `api/health.py`: add `"render_key_fallback": settings.render_bambuddy_key()[1]` and `"temporal": {"address": …, "namespace": …, "task_queue": …}` to the healthz body.

`main.py` lifespan, replacing `await state.queue.start()` … `await state.queue.aclose()`:

```python
    await asyncio.to_thread(state.projection.open)
    state.refs = BlobRefs(state.projection.pool)
    await import_file_jobs(state)                       # Step 5
    state.temporal = await connect(state.settings.temporal_address, state.settings.temporal_namespace)
    state.render.client = state.temporal
    await asyncio.to_thread(state.projection.prune, state.config.job_ttl)
    await asyncio.to_thread(prune_revision_exports, state.paths, state.config.job_ttl)
    await state.render.start()
    listener = JobEventListener(state.settings.database_url, events=state.events,
                                on_state=lambda up: state.metrics.listener_connected.set(1 if up else 0))
    listener_task = asyncio.create_task(listener.run())
    worker_stop = asyncio.Event()
    worker_task: asyncio.Task[None] | None = None
    if state.settings.temporal_worker_inprocess:
        deps = worker_deps_from_state(state)             # WorkerDeps over the app's own objects
        worker_task = asyncio.create_task(run_inprocess_worker(state.settings, deps, state.temporal, worker_stop))
    try:
        ...existing body (sweeper, previews, log, yield)...
    finally:
        await state.previews.aclose()
        ...sweeper cancel...
        worker_stop.set()
        if worker_task is not None:
            await asyncio.wait_for(worker_task, state.config.activity_timeout)
        listener_task.cancel(); await asyncio.gather(listener_task, return_exceptions=True)
        await state.render.aclose()
        await asyncio.to_thread(state.projection.close)
        await state.events.aclose()
```

`worker_deps_from_state(state)` lives in `worker.py`: `WorkerDeps(config=state.config, paths=state.paths, assets=state.assets, blobs=state.blobs, refs=state.refs, projection=state.projection, history=state.history, checkouts=state.checkouts, fetcher=CheckoutFetcher(state.libraries, state.installs, state.checkouts), thumbnail_executor=ThreadPoolExecutor(...), metrics=state.metrics)`.

The blob sweep joins `_asset_sweeper`: after `_sweep_assets_logged`, `await asyncio.to_thread(sweep_blobs, state.blobs, state.refs, grace=state.config.asset_sweep_grace)`.

- [ ] **Step 5: Import of `data/jobs/*.json`, once**

In `main.py`:

```python
async def import_file_jobs(state: AppState) -> int:
    """An upgrade from the file-backed store (spec §3.1): settled jobs become rows so
    outputs keep their `job_id` links; the directory then goes, unfinished jobs with it."""
    jobs_dir = state.paths.jobs
    if not jobs_dir.is_dir():
        return 0
    imported = 0
    for path in sorted(jobs_dir.glob("*.json")):
        try:
            job = Job.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("skipped an unreadable job file", extra={"path": str(path)})
            continue
        if job.state in ("done", "failed"):
            await asyncio.to_thread(state.projection.import_settled, job)
            imported += 1
    await asyncio.to_thread(shutil.rmtree, jobs_dir, True)
    if imported:
        logger.info("imported settled jobs from the file store", extra={"count": imported})
    return imported
```

with `JobProjection.import_settled(job)`: `INSERT INTO render_jobs (…all JOB_COLUMNS…) VALUES (…) ON CONFLICT (id) DO NOTHING` (state as stored, `workflow_id = NULL`, `inputs = {"params": …}`). `paths.ensure()` must stop creating `jobs/` (remove `self.jobs` from its list) or the import runs every boot on an empty dir — harmless but noisy; remove it.

Test (append to `tests/test_submit.py`, `requires_postgres` only — no Temporal needed, patch `connect`):

```python
def test_settled_file_jobs_are_imported_once_and_the_directory_removed(settings, paths, pg_conninfo, model) -> None:
    from scadbuddy.core.paths import DataPaths
    from scadbuddy.main import create_app
    from fastapi.testclient import TestClient
    import json

    jobs = paths.root / "jobs"; jobs.mkdir()
    done = Job(id="a" * 32, slug=model, state="done", created_at=now(), finished_at=now(), params={})
    pending = Job(id="b" * 32, slug=model, state="pending", created_at=now(), params={})
    (jobs / "a.json").write_text(done.model_dump_json()); (jobs / "b.json").write_text(pending.model_dump_json())
    cfg = settings.model_copy(update={"database_url": pg_conninfo})
    with patch("scadbuddy.main.connect", AsyncMock()), TestClient(create_app(cfg)) as client:
        assert client.get(f"/api/v1/jobs/{'a' * 32}").status_code == 200
        assert client.get(f"/api/v1/jobs/{'b' * 32}").status_code == 404
    assert not jobs.exists()
    with patch("scadbuddy.main.connect", AsyncMock()), TestClient(create_app(cfg)):
        pass  # second boot: nothing to import, nothing fails
```

- [ ] **Step 6: Delete the queue and rewrite the test fixtures**

Delete `backend/scadbuddy/render/job_store.py`, `backend/tests/test_render_queue.py`, `backend/tests/api/test_postgres_queue.py`. In `render/jobs.py` remove the `job_store` imports; re-export `QueueFullError`, `JobNotFoundError`, `SUPERSEDED_ERROR`, `render_key` from `render/projection.py` where `api/jobs.py` and tests import them (update the imports instead of re-exporting).

`tests/api/conftest.py::app` becomes:

```python
@pytest.fixture
def app(settings: Settings, paths: DataPaths, pg_conninfo: str) -> Iterator[FastAPI]:
    """The real app on a real projection and Temporal, with the render activities
    replaced by a stub that writes plausible files. ``width: 999`` makes it fail."""
    queue = f"api-{uuid.uuid4().hex[:8]}"
    cfg = settings.model_copy(update={"database_url": pg_conninfo, "temporal_task_queue_render": queue})
    application = create_app(cfg)
    stub = StubRenderActivities(paths)  # prepare/render_main/render_solids no-ops; finish_piece → _fake_result; project → real JobProjection

    async def make_worker() -> Worker:
        async with temporal_client() as client:  # NOTE: keep the env alive for the worker's life
            ...
```

The worker must outlive the fixture's `with` — restructure: the fixture opens `temporal_client()` itself via `contextlib.AsyncExitStack` run on the `WorkerThread` loop; simplest is a module-level `WorkerThread` whose `_main` does `async with temporal_client() as client: worker = Worker(client, task_queue=queue, workflows=[TemplatePipeline, RenderPiece, RenderPreview], activities=stub.all()); async with worker: await stop.wait()`. The app's own `connect` is patched to return a client to the same server: expose the address the test env is on (`env.client.service_client.config.target_host`) through `tests/support/temporal.py` (`current_address()`), and set `temporal_address` on `cfg` to it. Mark the `app`/`client` fixtures' users `requires_postgres` + `requires_temporal` by applying `pytestmark` in `tests/api/conftest.py` via `pytest_collection_modifyitems` for every test under `tests/api` that requests `client` or `app`.

`StubRenderActivities` (in `tests/api/conftest.py`): the `project` activity is the real `RenderActivities.project` bound to a `WorkerDeps` over the test's `JobProjection`/`BlobRefs`/`LocalBlobStore`; `finish_piece` writes `_fake_result(paths, req)` into `blobs.dir_for(req.piece_key)` (adapt `_fake_result` to take the directory); `render_main` raises the `OpenSCADError` `ApplicationError` for `width == FAIL_WIDTH`.

`tests/test_previews.py`: `PreviewScheduler(catalogue, store, runner=stub_runner, timeout=…)` where `stub_runner` is `async def (slug) -> bytes`. `tests/test_jobs.py`: whatever tested `render_job` end-to-end now calls `prepare_source` → `render_main` → `render_solids_stage` → `finish_piece_stage` in sequence against the fake openscad, with `work = tmp_path / "blob"`.

- [ ] **Step 7: Run the whole backend suite**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest -q && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: PASS; `mypy` clean (the last references to the deleted config fields and `RenderQueue` are gone). Expected skips: none when both services are present.

- [ ] **Step 8: Commit**

```bash
git add -A backend
git commit -m "feat(render): submit → start → reconcile through RenderService; the render queue is gone (#424)"
```

---

### Task 8: Generated files, docs, CI, healthz

**Files:**
- Modify: `backend/openapi.json`, `frontend/src/api/schema.d.ts`, `frontend/public/mockServiceWorker.js`, `agent/src/api/schema.d.ts` (regenerated)
- Modify: `frontend/src/mocks/handlers.ts` (if a `status` union is exhaustively switched, add `cancelled`)
- Modify: `.github/workflows/ci.yml`, `CLAUDE.md`, `README.md`

- [ ] **Step 1: Regenerate**

```bash
cd backend && uv run --frozen python -m scadbuddy.tools.export_openapi
cd ../frontend && pnpm gen:api && pnpm exec msw init public --save
cd ../agent && pnpm gen:api
cd ../frontend && pnpm typecheck && pnpm test
cd ../agent && pnpm typecheck && pnpm test
```

Expected: `JobStatus.status` now includes `"cancelled"`; typecheck passes. If a frontend switch over `status` is exhaustive (`useRenderJob.ts`, `CustomizePage.tsx`), treat `cancelled` like `failed` with the job's `error` text; add a vitest case.

- [ ] **Step 2: CI**

In `.github/workflows/ci.yml`, the pytest `docker run` (line ~555) adds nothing for Temporal (the CLI is in the image, Task 6). Update the comment above `services:` to say the Postgres service backs the projection and the `requires_postgres`/`requires_temporal` tests. `actionlint` must pass.

- [ ] **Step 3: Docs**

`CLAUDE.md` "Commands" section: tests marked `requires_temporal` skip unless `SCADBUDDY_TEST_TEMPORAL_ADDRESS` is set or a `temporal` CLI is on `PATH` (`temporal server start-dev` locally, or the image's `/usr/local/bin/temporal`); both `SCADBUDDY_DATABASE_URL` and `SCADBUDDY_TEMPORAL_ADDRESS` are required to run the app; `SCADBUDDY_TEMPORAL_WORKER_INPROCESS=true` for a one-process dev run; the worker is `python -m scadbuddy.worker`. Layout: `render/submit.py`, `render/projection.py`, `workflows/`, `store/`, `worker.py`; `render_jobs` is a projection; `piece_key` vs `render_key`. Remove the "Postgres optional" wording in the AI-agent paragraph.

`README.md` "Deploying": a "Render worker" subsection — same image, command `python -m scadbuddy.worker`, port 9090 `/healthz` and `/metrics`, one replica in phase 1 sharing `/data` (RWO is fine on one node; note it), env it needs (`SCADBUDDY_DATABASE_URL`, `SCADBUDDY_TEMPORAL_ADDRESS`, `SCADBUDDY_TEMPORAL_NAMESPACE`, `SCADBUDDY_DATA_DIR`, `SCADBUDDY_REVISION` for the build id), and the `preStop`/`terminationGracePeriodSeconds ≥ render_timeout + 60` requirement (§3.5). Temporal itself: the Temporal operator + a CNPG database in `eh-homelab/clusters` (Task 9).

- [ ] **Step 4: Lint the workflow and docs; commit**

```bash
actionlint && shellcheck .github/scripts/*.sh
git add -A
git commit -m "docs(render): worker, Temporal and database requirements; regenerated API types (#424)"
```

---

### Task 9: Deployment handoff to `eh-homelab/clusters`

This repo cannot make the change; it files it precisely and hands it off. No code.

**Files:** none here. Output: an issue in `eh-homelab/clusters`, a fleet wall/announce.

- [ ] **Step 1: File the clusters issue**

```bash
gh issue create --repo eh-homelab/clusters --title "scadbuddy: Temporal (operator + CNPG) and the render worker Deployment (ScadBuddy #424)" --body-file - <<'EOF'
ScadBuddy phase 1 (eh-homelab/ScadBuddy#424, spec docs/superpowers/specs/2026-09-27-template-pipelines-design.md §3.1) moves rendering onto Temporal. What the cluster needs, all in `applications/scadbuddy/`:

1. **A `TemporalCluster`** (temporal-operator is already installed under `applications/temporal-operator/`): namespace `bambuddy`, persistence + visibility on **CNPG** — either two databases on the existing `scadbuddy-db` `Cluster` (`temporal`, `temporal_visibility`, via `managed.roles`/`bootstrap` or a `Database` CR) or a new small CNPG cluster `scadbuddy-temporal-db`. Frontend service name to expose: `scadbuddy-temporal-frontend.bambuddy:7233`. Temporal namespace `scadbuddy` (operator `TemporalNamespace` CR).
2. **The API Deployment** gains env: `SCADBUDDY_TEMPORAL_ADDRESS=scadbuddy-temporal-frontend.bambuddy:7233`, `SCADBUDDY_TEMPORAL_NAMESPACE=scadbuddy`. `SCADBUDDY_DATABASE_URL` is now required (already set).
3. **A render worker Deployment** `scadbuddy-render`: same image and tag as the API (the deploy workflow pins both), command `["python", "-m", "scadbuddy.worker"]`, **replicas 1**, mounts the same `/data` PVC as the API (phase 1 shares the volume; multi-worker waits for phase 3), env as above plus `SCADBUDDY_DATA_DIR`, `SCADBUDDY_REVISION` (build id), no Bambuddy key. Port 9090: readiness on `/healthz`, scrape `/metrics`. `terminationGracePeriodSeconds: 240` and a `preStop` that lets the worker drain (it exits on SIGTERM after in-flight activities, ≤ render_timeout+60 s).
4. The deploy workflow (`deploy.reusable.yml` in ScadBuddy rewrites the image line): the worker's image line must be rewritten together with the API's — please point out where the second image line should live so ScadBuddy's workflow can be updated in the same change.

Rollout order: 1 → 2+3 in one sync (the API refuses to start without Temporal). Alerts: `ScadBuddyRenderQueueNotInPostgres` still holds (`scadbuddy_render_store_info{backend="postgres"}`).
EOF
```

- [ ] **Step 2: Reach @it-guy**

Using the fleet skill: `fleet.sh announce "ScadBuddy #424 needs a TemporalCluster + render worker in clusters — issue <url>; @it-guy this is yours per Elan" --topics "clusters,scadbuddy,temporal"`, and if @it-guy's session thread is listed in Agent Sessions, `fleet.sh wall-reply <its thread url> "<same text>"`. Journal the handoff link on this session's thread.

- [ ] **Step 3: Record**

Comment on #424 with the clusters issue URL. Done when the clusters issue exists and is linked from #424.

---

## Self-review notes

- **Spec coverage.** §3.1 topology → Tasks 1, 6, 9. §3.2 projection → Task 2. §3.3 submit/reconcile/supersede → Task 7. §3.4 workflows, activities, `piece_key`, `ABANDON`, timeouts, process group → Task 5 (+ runner). §3.5 worker versioning → Task 5 (`render_worker`) and Task 9 (grace period). §3.6 to-verify → Tasks 5 and 6 record outcomes. §3.7 tests → every task; `WorkflowEnvironment` via `tests/support/temporal.py`. §6.2 `local` backend + refs + sweep → Task 4 (shape deviation stated in Global Constraints). §8.3 → Task 5. §8.4 record fields present on the row (`pipeline_version`, `model_version`, `source_version` in result; image digest and openscad version already on `/healthz` — the per-output record lands with outputs in phase 2/4 when outputs change; noted). §9 `bambuddy_render_api_key` → Task 1 + healthz flag (frontend warning deferred, stated). §11 phase 1 items → all above.
- **Type consistency.** `JobProjection.submit/release_claim/mark_started/set_steps/finish/stale_pending` names match between Tasks 2, 5 (`project`), 7. `Projection` fields match `project()`. `WorkerDeps` fields match `build_worker_deps` and `worker_deps_from_state`. `PreviewScheduler(runner=…)` matches Task 7's wiring and the test change.
- **Placeholders.** One deliberate fill-in: the Temporal CLI sha256 in Task 6 Step 4 must be copied from the release checksums at implementation time — it is a value, not a design gap. Two "check the SDK" branches (Task 5 Steps 6 and 9) name the exact fallback code.
- **Review Focus** — all five pinned: (1) Task 7 `test_a_submit_whose_workflow_start_fails…`; (2) Task 5 `test_a_piece_resumes…`; (3) Task 5 `test_cancelling_one_parent…`; (4) Task 2 `test_a_late_running_projection…`; (5) Task 7 `test_settled_file_jobs_are_imported_once…`.

## Rollout addendum (2026-09-28)

`main` moved while Tasks 1–2 were built; they were rebuilt on it. Three changes matter:

- **#498 migrations are files.** Schema changes are new files in `backend/scadbuddy/migrations/`
  (`<yyyymmdd>T<hhmm>Z_<slug>.sql`), never edits; `LEGACY_VERSIONS` is frozen. "Migration 4"
  is `20260928T0900Z_render_jobs_projection.sql`, and it is additive: `heartbeat_at` and the
  legacy queue stay until PR4.
- **#419/#264 job events go through `PgNotifyEventBus`.** `JobProjection` takes
  `events: TransactionalEvents` and publishes `job.*` with `publish_in(conn, …)` inside each
  write's transaction, as `PostgresJobStore._announce` does. **Task 3 is collapsed into Task 2**:
  there is no private `scadbuddy_job_events` channel and no second listener.
- **#496 generated API files are not committed.** Task 8's regeneration step is only
  `export_openapi` + frontend/agent `pnpm gen:api` locally for typecheck; nothing is committed.

**The flag.** The database is required since #467. `SCADBUDDY_TEMPORAL_ADDRESS` empty →
the legacy queue renders; set → Temporal. Each PR leaves `main` deployable without Temporal.

**PR sequence.** PR1 (as three PRs: this plan, Task 1, Task 2). PR2: Tasks 4 + 5. PR3:
Tasks 6 + 7 behind the flag, plus Task 8. PR4, after Temporal is deployed in
`eh-homelab/clusters`: `SCADBUDDY_TEMPORAL_ADDRESS` becomes required; the legacy queue,
`PostgresJobStore`, the poll/lease/attempt knobs and `heartbeat_at` are removed;
`tests/test_render_queue.py` is deleted.

**Open for later tasks.** The migration backfills `inputs` once; rows the legacy queue
inserts afterwards have `inputs = '{}'`, so phase 2's migration that drops `params` must
backfill again. While both paths exist (PR3), legacy pending rows have `workflow_id IS NULL`
and the projection's `ON CONFLICT` can coalesce onto them, and legacy `claim()` could take
projection-inserted rows: PR3/Task 7 must make the flag select exactly one path per deployment.
