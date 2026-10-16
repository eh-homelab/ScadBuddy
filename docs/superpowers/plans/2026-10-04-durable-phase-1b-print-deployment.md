# Phase 1b: the `scadbuddy-print` Deployment and a volume-free print source — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The `bambuddy` queue (print runs, `FollowPrint`, the Bambuddy operation kinds) runs
as its own process, `python -m scadbuddy.worker --queue bambuddy`, versioned as the worker
deployment `scadbuddy-print` and drained like `scadbuddy-render`, with no `scadbuddy-data`
mount. The API stops serving that queue unless an in-process setting says so (#1060).

**Architecture:**
- Everything a `bambuddy` activity read from the data volume goes through one port,
  `OutputReader` (`bambuddy/output_reader.py`): the output's record (`get`), its stored
  `model.3mf` (`model_3mf`) and what is named from the model's files (`naming`: the project
  file stem and the template's `print_settings`). `LocalOutputs` reads the volume (the API,
  and the in-process worker); `RemoteOutputs` reads the API's cluster-internal routes
  (`api/internal.py`, `GET /api/v1/internal/outputs/{id}`, `…/model.3mf`, `…/naming`, hidden
  from the OpenAPI schema).
- The per-plate record a print writes (`OutputStore.record_send`, `meta.json`) moves to
  Postgres: `output_last_prints` (new migration), written by `OutputPrintStore`, overlaid by
  `OutputStore.get`/`list_for` on whatever an older `meta.json` still says.
- Output delete's Bambuddy part becomes a step on the `bambuddy` queue: an `OperationKind` may
  name a `prelude`, another kind whose run the `Operation` workflow executes first on that
  kind's task queue (`execute_activity(..., task_queue=...)`, behind `workflow.patched`). The
  library kind `output_delete` names `output_inbox_delete`, a `bambuddy` kind, when the request
  asks for the inbox copies to go.
- `worker.py` gains `--queue {render,bambuddy}`. The bambuddy worker builds its deps from
  Postgres and the API URL only (`build_print_deps`), registers `PrintRun`, `Operation` and
  `FollowPrint` plus the follow queue's activity under `WorkerDeploymentVersion("scadbuddy-print",
  revision)`, makes itself current and drains on SIGTERM. `FollowPrint` declares
  `AUTO_UPGRADE`: it lives as long as the print, so it must not hold a drain.

**Tech Stack:** Python 3.12, temporalio 1.33 (worker deployments, `workflow.patched`,
cross-queue activities), FastAPI, httpx, psycopg, pytest with the Temporal dev server.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §4.3
(queue table, `bambuddy` row: "Bambuddy part of output delete"), §5.3 (`print_upload`), §5.5
(the `scadbuddy-print` worker), §10 phase 1 "As built" (#1060 named) and 3e's note (output delete
is one `library` kind; #1060 splits it). Stacked on `feat/1054-previews` (3f).

## Rulings

1. **The 3MF comes from the API, not the blob store.** `state.store` (the `StoreBundle`) is
   built, but an output's `model.3mf` is not in it: `output_create` copies the job's piece onto
   the volume (`data/models/<slug>/outputs/<id>/`), and the piece is swept once no job
   references it. The worker reads the stored bytes from the API's internal route. Moving
   outputs into the store is outside #1060.
2. **The existing `GET /outputs/{id}/model.3mf` is not reused**: it lays the file out for the
   default printer and its presets (`download_3mf`), not the bytes `OutputSource` replates
   itself. A variant is added (§5.5 allows it), under `/api/v1/internal/`, with
   `include_in_schema=False`: it is not a client API, so no frontend client, no agent tool and
   no `coverage.ts` entry. The API has no auth of its own today; these routes expose nothing
   the public output routes do not.
3. **The per-plate record moves to Postgres** (`output_last_prints`, keyed by output id), not
   behind a route. A worker holds the database already (print runs, uploads, links), new
   durable state goes in Postgres, and a write through the API would make a print's record
   depend on the API answering. Old `meta.json` files are not rewritten: a row overrides the
   file's last-print fields, and an output with no row reads as before. `record_send`'s
   "omitted fields are left alone" survives as `COALESCE` on the nullable ids. The output's
   delete removes the row (best effort, like its uploads and links).
4. **The prelude is a kind, run as the step's activity on the other kind's queue.** The
   library worker cannot hold the Bambuddy key (§4.3), and a second operation from the route
   would split one request into two records. `Operation` runs `op.<prelude>.run` with
   `task_queue=` that kind's queue, after the record and before the kind's own run; a failure
   there is the operation's failure, and the files stay (today's rule: a Bambuddy delete that
   fails stops before the record goes). `OperationInput.prelude` is absent on every history
   recorded before this change, and the branch is guarded by `workflow.patched("op-prelude")`
   as the brief requires. A plain delete (`delete_inbox_copies=false`) names no prelude, so it
   never waits on the print worker.
5. **Versioning: `PrintRun` and `Operation` are PINNED, `FollowPrint` is AUTO_UPGRADE.** "Drained
   like `scadbuddy-render`" holds for runs with a bounded life: a print run lasts its slices
   plus `REPEAT_WINDOW` (10 min). A follow lasts the print (hours), so pinning it would hold
   every drain to its bound; it is AUTO_UPGRADE and keeps the `workflow.patched`
   discipline its docstring already states. (As built: an unversioned worker refuses a
   workflow that declares a versioning behavior, "versioning behavior cannot be specified
   without deployment options", so the versioned worker registers `VersionedFollowPrint`,
   a subclass declaring AUTO_UPGRADE under the same workflow name, and the in-process one
   `FollowPrint`. The drain's visibility count skips `WorkflowType="FollowPrint"`, which
   stays attributed to the old build until its next workflow task.)
   The follow queue's worker carries the same deployment version, so an activity scheduled
   there is routed within the version. The drain bound is `PRINT_DRAIN_TIMEOUT` =
   `REPEAT_WINDOW + 2 × DEFAULT_SLICE_TIMEOUT + 120` s (1920 s).
6. **The in-process print worker stays unversioned.** It runs only with
   `SCADBUDDY_TEMPORAL_WORKER_INPROCESS` (dev, `tests/api`) or the new
   `SCADBUDDY_TEMPORAL_PRINT_WORKER_INPROCESS` (a deployment that has not applied the
   `scadbuddy-print` manifest yet). It is then the only poller of its queue, so a version buys
   nothing, and making one current would delay every api test's first print. It keeps its
   restart-on-failure loop (review #1061) and reads through `LocalOutputs`.
7. **The API keeps the passes that need only Postgres and a client**: the lost-run/operation
   reconcile (`_end_lost_runs_until`) and the `print_watches` hand-off (`resume_followed`) run
   in the API whether or not it serves the queue.
8. **Transport errors to the API are left retryable.** `RemoteOutputs` maps an API *answer*
   (a problem document) to `ApiError` (or `OutputNotFoundError` for a 404 on the record), so
   refusals read as before; a connect error or 5xx without a problem propagates as an ordinary
   exception, which the activity's retry policy retries (a `print_plan` mid-run survives an
   API restart).
9. **Events from the worker go out on its own `PgNotifyEventBus`** (started, so its outbox
   drains), as the render worker's projection does. `ProgressObserver`'s dedupe is per
   process: the API's progress route and the worker's follow each keep their own last-seen,
   so a print may publish one extra `print.progress`; `print.settled` hooks are idempotent
   already (`Follower.on_settled`).

## Global Constraints

- Migrations: a NEW file `backend/scadbuddy/migrations/<UTC yyyymmddThhmmZ>_output_last_prints.sql`;
  never edit a merged one.
- `PrintRunWorkflow` is not changed (its replay fixtures in `tests/fixtures/print_run_histories/`
  are never re-recorded). `OperationWorkflow`'s change goes behind `workflow.patched`.
- Follow the Temporal Python SDK as documented; stop and report on any deviation.
- Every env var is `SCADBUDDY_<FIELD>`; a new `Settings` field is in `BOOTSTRAP_FIELDS` or
  `APPLIES` (`tests/test_settings_coverage.py`).
- Never commit `backend/openapi.json` or a `schema.d.ts`.
- Commits: conventional, naming `(#1060)`, ending with a blank line and
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Tests: Postgres `postgresql://postgres:postgres@127.0.0.1:55432/scadbuddy_test_impl1060`,
  Temporal dev server `/home/elan/.local/bin/temporal`; `tests/api/` paths together.

## Review Focus

1. An output deleted between a print's check and its upload or record: the worker's
   `RemoteOutputs` sees the API's 404 and the run fails with today's "has no 3MF to send" /
   "no output with id" problem, never an unexpected failure.
2. The API restarting while a print worker uploads: a connection error on `model_3mf` is
   retried by the activity, not turned into a non-retryable refusal.
3. An output printed before this release (last print only in `meta.json`) and printed again
   after: the progress route and the attach-to-project kind read the new row, and an output
   never printed again keeps showing its old print.
4. `DELETE /outputs/{id}?delete_inbox_copies=true` while the print worker is down: the
   operation waits (202 past the deadline) and the files stay until the inbox step ran; a delete
   without inbox copies finishes with no print worker at all.
5. A rolling deploy of `scadbuddy-print`: the old pod drains its pinned print runs, while every
   `FollowPrint` moves to the new build instead of holding the drain for hours.

---

### Task 1: The last print in Postgres

**Files:**
- Create: `backend/scadbuddy/migrations/<stamp>_output_last_prints.sql`
- Create: `backend/scadbuddy/library/output_prints.py`
- Modify: `backend/scadbuddy/library/outputs.py` (`OutputStore.__init__`, `get`, `list_for`,
  `_oldest_first` unchanged, `record_send` removed)
- Modify: `backend/scadbuddy/bambuddy/print_source.py` (`OutputSource.prints`, `record`)
- Modify: `backend/scadbuddy/api/deps.py` (`OutputStore(paths, prints=OutputPrintStore(pool))`)
- Modify: `backend/scadbuddy/library/output_operations.py` (`delete_run` forgets the row)
- Modify: `backend/scadbuddy/workflows/print_activities.py` (`PrintDeps.prints`)
- Test: `backend/tests/test_output_prints.py`

**Interfaces:**
- Produces: `OutputPrintStore(pool)` with `record(output_id, *, queue_item_id, slice_job_id,
  project_id, plates) -> None` (sync; `print_route` is always `"slice_queue"`),
  `for_outputs(ids: Sequence[str]) -> dict[str, LastPrint]`, `delete(output_id) -> None`;
  `LastPrint` (pydantic: `queue_item_id: int | None`, `print_route: PrintRoute`,
  `slice_job_id: int | None`, `project_id: int | None`, `plates: list[PlateSend]`);
  `OutputStore(paths, prints: OutputPrintStore | None = None)`.

- [ ] **Step 1: Write the failing test** (`requires_postgres`, the `pg_conninfo` fixture):

```python
def test_a_recorded_print_overrides_the_files_last_print(tmp_path, pool):
    prints = OutputPrintStore(pool)
    store = OutputStore(DataPaths(root=tmp_path), prints=prints)
    meta = make_output(store, queue_item_id=1, project_id=7)  # helper writes a meta.json
    prints.record(meta.id, queue_item_id=5, slice_job_id=9, project_id=None,
                  plates=[PlateSend(plate_id=1, queue_item_id=5, slice_job_id=9)])
    got = store.get(meta.id)
    assert (got.queue_item_id, got.slice_job_id, got.print_route) == (5, 9, "slice_queue")
    assert got.project_id == 7  # omitted: left as it was
    assert [p.queue_item_id for p in got.plates] == [5]
    assert store.list_for(meta.slug)[0].queue_item_id == 5

def test_an_output_with_no_row_reads_its_file(tmp_path, pool): ...
def test_a_second_record_keeps_the_project_and_replaces_the_plates(tmp_path, pool): ...
def test_delete_forgets_the_row(tmp_path, pool): ...
```

- [ ] **Step 2: Run, expect ImportError** (`uv run --frozen pytest -q tests/test_output_prints.py`).
- [ ] **Step 3: Implement.** Migration:

```sql
-- #1060: the last print of an output (`library.output_prints`), which the print worker
-- writes without the data volume. Overrides the last-print fields of an older meta.json.
CREATE TABLE output_last_prints (
    output_id     text PRIMARY KEY,
    queue_item_id bigint,
    print_route   text NOT NULL CHECK (print_route IN ('slice_queue')),
    slice_job_id  bigint,
    project_id    bigint,
    plates        jsonb NOT NULL DEFAULT '[]',
    recorded_at   timestamptz NOT NULL DEFAULT now()
);
```

`record` is one `INSERT … ON CONFLICT (output_id) DO UPDATE SET queue_item_id =
COALESCE(EXCLUDED.queue_item_id, t.queue_item_id)` (same for `slice_job_id`, `project_id`),
`print_route`, `plates`, `recorded_at = now()`. `OutputStore.get` / `list_for` apply
`_with_last_print(meta, row)`: `meta.model_copy(update=…)` with the row's non-null fields and its
`plates`. `OutputSource.record` calls `await asyncio.to_thread(self.prints.record, …)` per queue
item (as `record_send` did). `delete_run` calls `prints.delete` in its own best-effort `try`.
- [ ] **Step 4: Run the new test and `tests/bambuddy/test_print_source.py
  tests/test_print_workflow.py`, expect PASS.**
- [ ] **Step 5: Commit** `feat(outputs): an output's last print is recorded in Postgres (#1060)`.

### Task 2: `OutputReader`, and the print side reads through it

**Files:**
- Create: `backend/scadbuddy/bambuddy/output_reader.py`
- Modify: `backend/scadbuddy/library/outputs.py` (`OutputStore.model_3mf`)
- Modify: `backend/scadbuddy/render/bambu3mf.py` (`plates_of` takes `Path | IO[bytes]`)
- Modify: `backend/scadbuddy/bambuddy/send.py` (`_read_3mf` → `await files.model_3mf`; params
  typed `OutputFiles`), `print_source.py`, `project_file.py`, `print_run.py`
- Modify: `backend/scadbuddy/workflows/print_activities.py` (`PrintDeps.outputs: OutputReader`,
  no `catalogue`), `bambuddy/operations.py` (`bambuddy_kinds_over`), `bambuddy/follow.py`
  (`Follower.outputs: OutputReader`), `api/deps.py`, `main.py`
- Test: `backend/tests/bambuddy/test_output_reader.py`, existing print/follow/operation tests

**Interfaces:**
- Produces:

```python
class OutputFiles(Protocol):
    async def model_3mf(self, output_id: str) -> bytes | None: ...   # None: no 3MF

class OutputNaming(BaseModel):
    stem: str
    print_settings: dict[str, str] = {}
    #: The model.json's refusal (`InvalidModelMetaError`), when it has one.
    invalid_meta: str | None = None

class OutputReader(OutputFiles, Protocol):
    async def get(self, output_id: str) -> OutputMeta: ...           # OutputNotFoundError
    async def naming(self, meta: OutputMeta) -> OutputNaming: ...

@dataclass
class LocalOutputs:  # OutputReader over OutputStore + Catalogue, each call in a thread
    outputs: OutputStore
    catalogue: Catalogue

async def require(reader: OutputReader, output_id: str) -> OutputMeta  # ApiError 404
```

- `bambuddy_kinds_over(*, settings_store, outputs: OutputReader, uploads, links, archive_cache)
  -> list[OperationKind]`; `bambuddy_kinds(core, components)` calls it with
  `LocalOutputs(core.outputs, core.catalogue)`.

- [ ] **Step 1: Failing tests:** `LocalOutputs.get` raises `OutputNotFoundError` for an unknown id;
  `model_3mf` returns the stored bytes and `None` without a 3MF; `naming` returns the stem and
  the template's `print_settings`, and `invalid_meta` (no raise) for a model.json that refuses
  them; `plates_of(io.BytesIO(...))` lists the same plates as the path.
- [ ] **Step 2: Run, expect ImportError.**
- [ ] **Step 3: Implement.** `print_check` uses `naming = await outputs.naming(meta)`; an
  `invalid_meta` refuses with the 409 "Invalid Model Metadata" it answers today; the stem is
  used only when a project is chosen (as today). `project_file_check` uses `naming.stem`.
  `print_succeed` uses `await outputs.get`. `Follower.follow` awaits `self.outputs.get`.
- [ ] **Step 4: Run** `tests/bambuddy/test_output_reader.py tests/bambuddy/test_follow.py
  tests/bambuddy/test_print_source.py tests/test_print_workflow.py tests/test_follow_workflow.py`
  and `tests/api/test_project_file.py tests/api/test_print_links_api.py tests/api/test_printing*.py`.
- [ ] **Step 5: Commit** `refactor(print): the bambuddy queue reads outputs through OutputReader (#1060)`.

### Task 3: The internal routes and `RemoteOutputs`

**Files:**
- Create: `backend/scadbuddy/api/internal.py` (discovered by `_api_router`)
- Modify: `backend/scadbuddy/bambuddy/output_reader.py` (`RemoteOutputs`)
- Test: `backend/tests/api/test_internal_outputs.py`

**Interfaces:**
- `GET /api/v1/internal/outputs/{id}` → `OutputMeta` (404 problem when gone);
  `GET /api/v1/internal/outputs/{id}/model.3mf` → the stored bytes (404 problem without one);
  `GET /api/v1/internal/outputs/{id}/naming` → `OutputNaming`. All `include_in_schema=False`,
  all read through `LocalOutputs` built from the app's state.
- `RemoteOutputs(base_url: str, *, client: httpx.AsyncClient | None = None)`, an `OutputReader`
  with `aclose()`. A 404 on `get` is `OutputNotFoundError`; a 404 on `model_3mf` is `None`; any
  other problem answer is `ApiError(status, detail, title=, type_=)`; a transport error or a
  non-problem 5xx is raised as is (ruling 8).

- [ ] **Step 1: Failing tests** in `tests/api` (`client`, `app` fixtures): the three routes answer
  as above for a saved output and 404 for none; the routes are absent from `app.openapi()`;
  `RemoteOutputs` over `httpx.ASGITransport(app)` returns the same meta, bytes and naming as
  `LocalOutputs`, `OutputNotFoundError` for an unknown id, `None` for a missing 3MF.
- [ ] **Step 2: Run, expect 404s / ImportError.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(api): internal output routes the print worker reads (#1060)`.

### Task 4: Output delete's inbox copies on the `bambuddy` queue

**Files:**
- Modify: `backend/scadbuddy/operations/kinds.py` (`OperationKind.prelude`, `needs_prelude`)
- Modify: `backend/scadbuddy/workflows/operation_models.py` (`PreludeStep`,
  `OperationInput.prelude`), `workflows/operation.py`, `api/operations.py` (`_run_operation`)
- Modify: `backend/scadbuddy/bambuddy/operations.py` (`output_inbox_delete`),
  `library/output_operations.py` (`output_delete` names it; its run drops the Bambuddy part)
- Test: `backend/tests/test_operation_workflow.py`, `backend/tests/api/test_outputs*.py`

**Interfaces:**

```python
class PreludeStep(BaseModel):
    kind: str          # its run is `op.<kind>.run`
    task_queue: str
    run_attempts: int = 1

class OperationInput(BaseModel):
    ...
    prelude: PreludeStep | None = None

PRELUDE_PATCH = "op-prelude"
```

`OperationKind(..., prelude: str | None = None, needs_prelude: Callable[[dict[str, Any]], bool]
= _always)`.

- [ ] **Step 1: Failing tests:** an `Operation` with a prelude on a second task queue runs the
  prelude's activity there before the run, with the run's request; a prelude that fails ends the
  operation `failed` with its problem and the run never starts; `OperationInput` without a
  prelude records no patch marker (the history has no `MarkerRecorded`). API: a delete with
  `delete_inbox_copies=true` still deletes the inbox copy (existing test), and `run_operation`
  passes `prelude=None` for `delete_inbox_copies=false`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** In `OperationWorkflow._effect`, before the run:

```python
if input.prelude is not None and workflow.patched(PRELUDE_PATCH):
    try:
        await workflow.execute_activity(
            run_activity(input.prelude.kind),
            RunOp(request=input.request, checked={}, author=input.author),
            result_type=dict,
            task_queue=input.prelude.task_queue,
            start_to_close_timeout=RUN_TIMEOUT,
            heartbeat_timeout=RUN_HEARTBEAT,
            retry_policy=RetryPolicy(maximum_attempts=input.prelude.run_attempts, ...),
        )
    except (ActivityError, ApplicationError, asyncio.CancelledError) as error:
        return <the same FinishOp the run's except builds>
```

`output_inbox_delete` (bambuddy, `run_attempts=3`: a 404 counts as deleted, so it is
idempotent): `meta = await require(outputs, id)`; if `await uploads.for_output(meta.id)`, load
settings and `delete_inbox_copies(client, uploads, meta, settings)`. `output_delete` sets
`prelude="output_inbox_delete", needs_prelude=lambda r: bool(r["delete_inbox_copies"])`.
- [ ] **Step 4: Run** `tests/test_operation_workflow.py` and the api output tests.
- [ ] **Step 5: Commit** `feat(outputs): output delete's Bambuddy inbox step runs on the bambuddy queue (#1060)`.

### Task 5: `python -m scadbuddy.worker --queue bambuddy`

**Files:**
- Modify: `backend/scadbuddy/core/settings.py` (`api_internal_url`,
  `temporal_print_worker_inprocess`, both bootstrap)
- Modify: `backend/scadbuddy/workflows/client.py` (`PRINT_DEPLOYMENT_NAME = "scadbuddy-print"`;
  `make_current` / `is_current` / `drained` take `deployment_name=DEPLOYMENT_NAME`;
  `bambuddy_worker` / `follow_worker` take `deployment: WorkerDeploymentVersion | None = None`)
- Modify: `backend/scadbuddy/workflows/follow.py` (`FollowPrint` AUTO_UPGRADE)
- Modify: `backend/scadbuddy/worker.py` (`--queue`, `build_print_deps`, `run_print_worker`,
  health for the queue it serves)
- Modify: `backend/scadbuddy/main.py` (print worker only in-process when asked; lost-run
  reconcile and `resume_followed` always)
- Test: `backend/tests/test_print_worker.py`, `tests/test_print_worker_task.py`

**Interfaces:**
- `build_print_deps(settings) -> PrintWorkerDeps` (dataclass: `settings_store`, `events`,
  `outputs: RemoteOutputs`, `activities: list[Callable]`, `follow_print`, `metrics`, `aclose()`),
  never touching `settings.data_dir`.
- `run_print_worker(settings, *, stop, health_port, client)`; `main()` parses `--queue`.
- `PRINT_DRAIN_TIMEOUT = (REPEAT_WINDOW + 2 * DEFAULT_SLICE_TIMEOUT).total_seconds() + 120`.

- [ ] **Step 1: Failing tests:** `Settings` without `SCADBUDDY_API_INTERNAL_URL` refuses
  `--queue bambuddy` with a message naming it; `build_print_deps` with `data_dir` pointing at a
  path that does not exist leaves it absent; the print worker on the dev server makes
  `scadbuddy-print:<revision>` current, serves `/healthz` with the bambuddy queue, runs an
  `Operation` started on an unversioned queue whose prelude is on the print queue, and stops on
  `stop`; the API without either in-process flag starts no print worker but still reconciles
  lost runs.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** `run_print_worker` mirrors `run_worker`/`_poll`: `bambuddy_worker` and
  `follow_worker` with `WorkerDeploymentConfig(version=WorkerDeploymentVersion(
  "scadbuddy-print", settings.revision), use_worker_versioning=True,
  default_versioning_behavior=PINNED)`, `make_current_until_polled` with
  `deployment_name=PRINT_DEPLOYMENT_NAME`, `_drain(... timeout=PRINT_DRAIN_TIMEOUT)`.
- [ ] **Step 4: Run** the new tests, `tests/test_worker.py tests/test_print_worker_task.py
  tests/test_follow_workflow.py tests/test_print_replay.py tests/test_settings_coverage.py`.
- [ ] **Step 5: Commit** `feat(worker): python -m scadbuddy.worker --queue bambuddy, the scadbuddy-print deployment (#1060)`.

### Task 6: Docs

**Files:** `README.md` ("Bambuddy writes on the `bambuddy` queue" → the print worker),
`CLAUDE.md` (Layout: `worker.py`, `bambuddy/output_reader.py`, `api/internal.py`), the spec §10
phase 1 "As built (1b)" note.

- [ ] Write, then commit `docs: the scadbuddy-print worker and its volume-free source (#1060)`.

---

## Clusters changes (for the lead to apply in eh-homelab/clusters `applications/scadbuddy/`)

Apply together with the image that contains #1060, as one ArgoCD sync, with the API's rollout
`Recreate` for this release (the API stops polling `bambuddy`; a new versioned poller replaces
it). Until applied, set `SCADBUDDY_TEMPORAL_PRINT_WORKER_INPROCESS=true` on the API to keep
prints working.

1. **New file `scadbuddy-print.yaml`**: a Deployment `scadbuddy-print`, one replica, the same
   image line and the three `scadbuddy.eh-homelab.io/*` annotations as `scadbuddy-render.yaml`
   (`deploy.reusable.yml` pins `scadbuddy-print.yaml` too once the file exists, #1060),
   and:

```yaml
spec:
  replicas: 1
  strategy: {type: RollingUpdate, rollingUpdate: {maxSurge: 1, maxUnavailable: 0}}
  template:
    spec:
      terminationGracePeriodSeconds: 2000   # PRINT_DRAIN_TIMEOUT 1920 + SDK 30 + slack
      containers:
        - name: print
          image: <same pin as scadbuddy.yaml>
          args: ["python", "-m", "scadbuddy.worker", "--queue", "bambuddy"]
          ports: [{name: health, containerPort: 9090}]
          readinessProbe: {httpGet: {path: /healthz, port: 9090}}
          livenessProbe: {httpGet: {path: /healthz, port: 9090}, periodSeconds: 30}
          env:
            - {name: SCADBUDDY_DATABASE_URL, valueFrom: {secretKeyRef: <as scadbuddy.yaml>}}
            - {name: SCADBUDDY_TEMPORAL_ADDRESS, value: <as scadbuddy.yaml>}
            - {name: SCADBUDDY_TEMPORAL_NAMESPACE, value: scadbuddy}
            - {name: SCADBUDDY_TEMPORAL_TASK_QUEUE_BAMBUDDY, value: bambuddy}
            - {name: SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES, value: "true"}   # after step 3
            - {name: SCADBUDDY_API_INTERNAL_URL, value: "http://<the API Service>:8080"}   # cluster-internal, never the ingress
            - {name: SCADBUDDY_BAMBUDDY_URL, value: <as scadbuddy.yaml>}
            - {name: SCADBUDDY_BAMBUDDY_API_KEY, valueFrom: {secretKeyRef: <the full key, as scadbuddy.yaml>}}
          # No volumes: no scadbuddy-data, no emptyDir needed (nothing is written to disk).
```

   `SCADBUDDY_REVISION` is stamped in the image. The Bambuddy URL/key env only seed a database
   that has never been saved; the worker reads the stored settings on every use, as the API does.
2. **`scadbuddy.yaml`** (the API): no new variable; make sure it does **not** set
   `SCADBUDDY_TEMPORAL_PRINT_WORKER_INPROCESS` once step 1 is live. A NetworkPolicy, if the
   namespace has one, must let `scadbuddy-print` reach the API Service on 8080, Postgres,
   Temporal's frontend and Bambuddy.
3. **Search Attributes** on the `scadbuddy` namespace (the Temporal operator's namespace
   resource, or a one-off `temporal operator search-attribute create`):
   `ScadbuddyKind` Keyword, `ScadbuddySubject` Keyword, `ScadbuddyStatus` Keyword,
   `ScadbuddyMayHaveQueued` Bool. Then set `SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES=true` on the API
   and `scadbuddy-print` (it decides at start whether workflows upsert them).
4. **Dynamic config**: `matching.maxTaskQueuesInDeploymentVersion` needs no change (two queues,
   `bambuddy` and `bambuddy-follow`, join `scadbuddy-print`).
