# ScadBuddy — notes for agents

Self-hosted OpenSCAD customizer that sends multi-colour 3MFs to Bambuddy. The design,
and the measured facts it rests on, are in
`docs/superpowers/specs/2026-09-22-scadbuddy-design.md` (§3 is the verified-facts list);
the print dialog is `docs/superpowers/specs/2026-09-24-print-flow-design.md`; template-owned
UIs and pipelines on Temporal, the blob store and Arrange are
`docs/superpowers/specs/2026-09-27-template-pipelines-design.md`.
Deployment is described in `README.md` ("Deploying").

## Commands (what CI runs)

Backend (`backend/`, Python 3.12, uv). CI runs these inside the Dockerfile's `test`
image with `--no-sync`; locally drop that flag:

```bash
cd backend
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy              # strict; files = scadbuddy, tests
uv run --frozen pytest -n auto  # pytest-xdist; drop -n to run serially
```

Tests marked `requires_openscad` / `requires_git` skip when the binary is not on
PATH. The backend will not start without `SCADBUDDY_DATABASE_URL` (#401: the settings
live only in Postgres), so every test that builds the app takes the `pg_conninfo`
fixture (a throwaway schema), and it and the `requires_postgres` tests (the render
queue, template media's `template_media`) skip unless `SCADBUDDY_TEST_DATABASE_URL`
points at a Postgres they can create schemas in, e.g.
`docker run -d -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=scadbuddy_test -p 5432:5432
postgres:18` and `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test`.
Without it most of `tests/api` skips. CI runs them against a `postgres:18` service
container. A `Settings` for an app that never starts uses `tests.conftest.UNUSED_DATABASE_URL`.
Tests marked `requires_fixtures` (#2146) use the real MakerWorld 3MFs in
`test-fixtures/makerworld/` (Git LFS, ~290 MB; `git lfs pull --include "test-fixtures/**"`;
`manifest.json` says what each covers) and skip, naming every missing file, without
them; with `SCADBUDDY_TEST_FIXTURES` set (a directory holding `makerworld/`, as CI sets
it) they fail instead. With git-lfs installed, a plain clone downloads all of them:
clone with `GIT_LFS_SKIP_SMUDGE=1 git clone …` (or set
`git config lfs.fetchexclude 'test-fixtures/**'` in a clone) and pull them only when
you run those tests (#2489). CI's `lfs-fixtures` job pulls them once into the Actions
cache and each shard mounts them at `/app/test-fixtures`; a shard whose restore misses
pulls them itself (#2488). Add or change a fixture only on a
branch of this repo: a fork's LFS objects live in the fork's store, which that job's
`git lfs pull` cannot reach.
Tests marked `requires_temporal` skip unless `SCADBUDDY_TEST_TEMPORAL_ADDRESS` names a
running Temporal (e.g. `temporal server start-dev`) or a `temporal` CLI is on `PATH`
(`SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` can point at one; the test image ships
`/usr/local/bin/temporal`), from which the tests start their own dev server.
That CLI is pinned by version and amd64 sha256 in the Dockerfile's `test` stage
(`TEMPORAL_CLI_VERSION`, `TEMPORAL_CLI_SHA256_AMD64`), in `ci.yml`'s two "Install the
Temporal CLI" steps; move all three together.
Under pytest-xdist each worker makes its schemas in a database of its own
(`<test db>_gw<N>`, created and dropped by `tests/conftest.py::_pg_database_url`),
because advisory locks and NOTIFY channels are per database: workers sharing one would
serialise every `migrate` and hear each other's events. So the test role needs
`CREATEDB` (the `postgres` superuser above has it). Each worker also starts its own
Temporal dev server.
CI runs the suite in six `backend-pytest` jobs (#1167), each `--splits 6 --group N`
(pytest-split) by the timings in `backend/.test_durations`; `backend-pytest-split`
checks that the six selections add up to the whole collection and that each
shard ran what it selected. A test missing from
the file is placed by the average, so a stale file only unbalances the shards. The
timings must be CI's own (a file stored on a faster machine left one shard ten minutes
early): each shard stores the tests it ran (`--store-durations --clean-durations
--durations-path=/results/durations.json`, a copy of the committed file it also splits
by) and `backend-pytest-split` merges them (`.github/scripts/merge-test-durations.sh`)
into the `backend-test-durations` artifact. To refresh, download that artifact from a
green run on main, unzip it into `backend/` and commit; the file is in `.dockerignore`,
so a refresh leaves the image's layers alone. Never pass `--durations-path <path>` as
two words: pytest takes the path for its rootdir and drops `pyproject.toml`'s settings.
Mixing `tests/` and `tests/api/` paths in one pytest command is fine two at a time,
but an api module after a non-api module that itself follows an api module loses
`tests/api/conftest.py`: `uv run --frozen pytest tests/api/test_health.py
tests/test_config.py tests/api/test_jobs.py` errors at setup of `test_jobs.py`'s tests
with `fixture 'client' not found`. Put the `tests/api/` paths together.
Renders run on Temporal, and `SCADBUDDY_TEMPORAL_ADDRESS` is required (#546), like
`SCADBUDDY_DATABASE_URL`; `python -m scadbuddy.worker` is the worker (or
`SCADBUDDY_TEMPORAL_WORKER_INPROCESS=true` for a one-process dev run). A `Settings` for
an app whose renders never run uses `tests.conftest.UNUSED_TEMPORAL_ADDRESS`.
`tests/api` renders on Temporal too, and skips without one: one dev server per session
(`tests/api/conftest.py::temporal_address`), and each test's app runs its own in-process
worker on a task queue of its own (the api `settings` fixture), because every test has
its own data directory and schema. The render is the real pipeline behind the fake
openscad (`FAKE_3MF`, `FAKE_STDERR` in `fake-env.json`; `width=999` fails). Every
test's queue registers with one worker-deployment version, so the dev server raises
`matching.maxTaskQueuesInDeploymentVersion` past Temporal's default of 100 (past it,
renders never start); a server named by `SCADBUDDY_TEST_TEMPORAL_ADDRESS` needs the same.
The fixture terminates the workflows a test leaves open, since an abandoned piece
would be joined by the next test that renders it. The dev server's store is a SQLite
file (on `/dev/shm` when it has room): in memory it was lost mid-session under load.
Backend schema changes are new files in `backend/scadbuddy/migrations/`
(`<yyyymmdd>T<hhmm>Z_<slug>.sql`, UTC; never edit a merged one); the settings tables are
`20260928T0840Z_settings.sql`. The only place a real `openscad` exists is the image:
`docker build --target test -t scadbuddy:test . && docker run --rm scadbuddy:test`.

Frontend (`frontend/`, Node 24, pnpm via corepack from `packageManager`):

```bash
cd frontend
corepack enable
pnpm install --frozen-lockfile
pnpm lint && pnpm typecheck && pnpm test && pnpm build
pnpm exec playwright test         # msw-mocked e2e against `pnpm preview` of the bundle
```

CI runs the lint, typecheck, unit tests and build in the `frontend` job, and Playwright
in three `frontend-e2e` jobs of its own (`--shard=N/3`, one worker each);
`frontend-e2e-split` checks that the three selections add up to the whole `--list` and
that each shard ran what it selected, as `backend-pytest-split` does.
`e2e-stack/run.sh [--mode recorder|unset|both] [--keep] [--no-build]` is the real-stack
e2e of the native hot path (docker with compose v2; it builds the runtime, slicer and test
images; weekly and on dispatch in `hot-path-e2e.yml`'s `stack` job, never on PRs);
`e2e/real-hot-path.spec.ts` skips without what it sets.
`e2e/real-backend.spec.ts` skips unless `E2E_BASE_URL` points at a running container.
`e2e/real-agent.spec.ts` also needs `E2E_AGENT=1`: a stack whose one origin routes
`/api/v1/ai/*` to a real agent with a credential pointed at a fake Anthropic endpoint
(its header lists the script `E2E_AGENT_SCRIPTED=1` expects; `pnpm preview` routes the
same way, `vite.config.ts`).

Agent service (`agent/`, Node 24, pnpm via corepack; the `agent` CI job):

```bash
cd agent
corepack enable
pnpm install --frozen-lockfile
pnpm lint && pnpm typecheck && pnpm test && pnpm build
docker build --target agent -t scadbuddy-agent:dev .   # checks the bundled Claude Code binary
```

Tests never call Anthropic. `test/run.test.ts` runs the bundled Claude Code binary
against a local fake Anthropic endpoint; `test/pg.test.ts` needs
`SCADBUDDY_TEST_DATABASE_URL` (e.g. `docker run -d -e POSTGRES_PASSWORD=postgres
-e POSTGRES_DB=scadbuddy_test -p 5432:5432 postgres:18`, then
`SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test pnpm test`).
With it set, `pnpm test`'s globalSetup migrates a template database once and each
Postgres test copies it (`test/support/postgres.ts`, #2018), so the test role needs
`CREATEDB`, as the backend's does: without it the whole suite fails at setup, not
only the Postgres tests.
Evals (`agent/evals/`, `docs/ai/evals.md`): each scenario is a session on the one runtime,
an AgentSession that an `api` process starts and a `worker` process runs (`evals/durable.ts`
`startEvalStack`: Temporal, a throwaway Postgres, both roles in-process; plan 2026-10-13
Step 6.6). `test/evals.durable.e2e.test.ts` replays each scenario against the fake endpoint
in `pnpm test` (it skips without Temporal or Postgres); `pnpm evals` runs them live with the
credential saved in Settings (or `SCADBUDDY_EVAL_ANTHROPIC_API_KEY`, CI only; the
manual `ai-evals.yml` workflow) and skips cleanly without one. The render scenario
runs live only against a real backend (`SCADBUDDY_EVAL_BACKEND_URL`, the image; it
skips without one), and its render is checked `verify.sh`-style on the saved output's
3MF; replies with suggestions are scored on their citations (`evals/citations.ts`,
#1924).
`.github/scripts/lint-one-runtime.sh` (the `lint` job) fails on a second turn path: a session
mode branch in `agent/src`, the SDK's `query` imported outside `harness/run.ts` and
`harness/testConnection.ts`, or `agent-durable/` importing or depending on the Claude Agent
SDK or naming `DurableSession` (plan Step 8.1).

The Python agent parts (`agent-durable/`, Python 3.14; the `agent-durable` CI job, #1056):

```bash
cd agent-durable
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest            # requires_postgres tests need SCADBUDDY_TEST_DATABASE_URL
docker build --target agent-durable -t scadbuddy-agent-durable:dev .
```

It needs uv 0.9.0 or later, which `[tool.uv] required-version` enforces. The job also runs
`.github/scripts/harness-pin.test.sh`: `uv-git-pin.sh` on the backend's temporal-agent-harness
pin, which must fail on a vanished commit (`uv lock --check` alone cannot tell).

`agent/test/fixtures/secret-vectors.json` is written by `agent/test/secretVectors.test.ts`
(`UPDATE_SECRET_VECTORS=1`) and opened by `agent-durable/tests`: a change to
`agent/src/secrets.ts`'s format or `credentials.ts`'s AAD needs new vectors, and the port must open them.
`agent/test/fixtures/payload-vectors.json` does the same for the payload codec
(`agent/test/payloadCodec.test.ts`, `UPDATE_PAYLOAD_VECTORS=1`; `agent-durable/tests/test_codec.py`
opens every one and seals it again byte for byte).

Generated API files (#492): `backend/openapi.json`, `frontend/src/api/schema.d.ts` and
`agent/src/api/schema.d.ts` are gitignored and never committed. In frontend and agent,
`pnpm gen:api` (`scripts/gen-api.mjs`) exports the spec with uv, then writes the
client. `typecheck`, `test` and (in the agent) `build` run it first, so both packages need
uv and the backend tree. With `SCADBUDDY_OPENAPI_JSON` set, it reads that spec and skips
the export. That's how the Dockerfile's `frontend` and `agent-build` stages use the spec
from its `api-spec` stage. A frontend vitest run outside `pnpm test` (`pnpm exec vitest
<file>`) needs `pnpm gen:api` first, or `SCADBUDDY_OPENAPI_JSON` set, because some unit
tests read bounds from the exported spec (`components/settings/Printers.test.tsx`
`specAccessCodeMax`, #4502). The `freshness` job no longer commits anything. It checks
that two exports are byte-identical, checks the committed msw worker, and posts the API
diff against main as one PR comment, edited in place. The msw worker
(`public/mockServiceWorker.js`) stays committed; regenerate it after an msw bump:

```bash
cd frontend && pnpm exec msw init public --save   # --save, or it prompts and dies with no TTY
```

The Go client `tools/go/scadbuddy-client/client` is the one exception: committed (a
vendored Go module must build offline), regenerated by `scripts/generate-go-client.sh`
(Kiota's pinned image; it deletes the folder first), checked and auto-fixed by the
`freshness` job.

Go tools (`tools/go/`, one `go.work` at the root, dependencies vendored in `vendor/`):

```bash
go work vendor                                   # after a go.mod change
scripts/generate-go-client.sh [--check]          # the ScadBuddy client; needs docker, uv, jq
cd tools/go/<m> && go vet ./... && go test -race ./...
docker build -f tools/go/bambuddy-fs/Dockerfile -t bambuddy-fs:dev .   # repo root as context
```

The generator vendors the client in module mode (`GOWORK=off go mod tidy && go mod
vendor`, for consumers outside the workspace) and then runs `go work vendor`; it never
runs `go work sync`, which would move a lagging client's `go.mod` past its own
`vendor/modules.txt` and edit bambuddy-fs's `go.mod` on a client-only change (#2686).
bambuddy-fs's mount tests need `/dev/fuse` and `fusermount3` (they skip without the
device). The Go toolchain is pinned in six places, moved together: `go.work`'s
`toolchain`, the golang tag in `tools/go/bambuddy-fs/Dockerfile`, and `GOTOOLCHAIN` in
`go-module-ci.yml`, in `ci.yml`'s two freshness steps and in its `go-client` job.

Rust workers (`workers/rust/`, one Cargo workspace; the `rust` CI job):

```bash
cd workers/rust
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
```

`rust-toolchain.toml` pins the toolchain (rustup installs it). The Temporal Core SDK's
build needs `protoc` on `PATH` (`apt-get install protobuf-compiler`). `Cargo.lock` is
committed; CI runs clippy and the tests `--locked`, with the same Temporal CLI as the
`agent` job in `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER`.

Workflow/Dockerfile lint (the `lint` job): actionlint, hadolint with `.hadolint.yaml`,
`shellcheck .github/scripts/*.sh models/*/verify.sh`, `lint-verify-labels.sh`,
`lint-dashboard.sh` (the Grafana dashboard, #988; it needs `KUSTOMIZE` pointing at
kustomize v5.6.0, which the job downloads and checks by sha256 because ArgoCD's
repo-server runs that version), `lint-base64-images.sh` (#2479: no image is built as
base64 in JSON or text outside its allowlisted Messages API boundaries; images move as
blobs and `{name, mediaType}` pointers), and the `.github/scripts/*.test.sh` suites. Every
`docker run` in a `verify.sh` must carry
`--label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}"` (Python:
`"--label", "scadbuddy-verify=" + os.environ.get("SCADBUDDY_VERIFY_LABEL", "local")`) on
the same line: `verify-models.sh` reaps a timed-out template's containers by it (#302).

Template checks (the `models` job): each `models/<slug>/verify.sh` the PR touches, or all
of them when the Dockerfile, `ci.yml` or the selector/runner scripts change, and always on
push to main and the weekly schedule. Run the same locally, in the Dockerfile's `base`
stage (OpenSCAD plus the image's fonts):

```bash
docker build --target base -t scadbuddy-verify:ci .
docker build --target test -t scadbuddy:test .   # for templates with a pipeline/ (#427)
SCADBUDDY_OPENSCAD_IMAGE=scadbuddy-verify:ci SCADBUDDY_FONTS_IMAGE=scadbuddy-verify:ci \
  SCADBUDDY_PIPELINE_IMAGE=scadbuddy:test \
  bash -c '.github/scripts/select-models.sh all | .github/scripts/verify-models.sh'
```

Without `SCADBUDDY_PIPELINE_IMAGE` a template's pipeline check prints "skipped".

## Layout

- `backend/scadbuddy/render/` — the render pipeline: `runner.py` (openscad invocation,
  `-D` building), `schema.py` (`.param` → customizer schema), `split.py` (3MF split by
  per-triangle material), `solids.py` (one closed solid per colour via a `color()`
  wrapper), `bambu3mf.py` (Bambu-style 3MF writer), `glb.py`, `thumbnail.py` (numpy
  rasteriser for plate cover images), `plate.py`/`plate_profiles.py`, `jobs.py`
  (the render stages the worker's activities run; `render_job` runs them in one
  process, which the pipeline tests use), `job_models.py` (`Job`, `render_key`,
  `QueueFullError`), `submit.py` (`RenderService`, what the routes type against as
  `RenderDep`: submit starts `render-<render_key>` with update-with-start; the
  workflow's first (local) activity inserts the row, identical requests join it as
  claims, and a supersede sends the old execution `release`, #1053; a withdrawal
  (`withdraw`, `POST /jobs/{id}/withdraw` with an `Idempotency-Key`, the agent's
  `withdraw_render`, the Customize page's Cancel) sends it `release` with `cancelled`,
  and the last claim withdrawn cancels the job, #4968; with `reuse_finished`
  (the render route, an order's quote) a `done` job of the key whose refs still hold its
  result answers at once, its `reused_at` keeping it from the prune,
  `JobProjection.reuse_done`, #4870; a caller whose work follows the run, as the output
  backfill's attach does, leaves it off), `projection.py` (`render_jobs` as a projection
  the workflow writes in place through the `project` activity), `pg_store.py` (the
  backend's migrations), `worker_role.py` (#601: `RENDER_GRANTS`, everything the
  render worker's own role `scadbuddy_render` may touch, granted by the API at start;
  a change to what the worker reads or writes changes it, and
  `tests/test_render_worker_role.py` renders as that role; the worker never migrates
  and reads settings only through the `render_settings` view). The legacy in-process queue, its file and Postgres stores
  and its `.renders/<key>` cache are gone (#546): the Temporal path's cache is the blob
  store's piece (`piece.json`), and nothing writes or prunes `models/<slug>/.renders/`
  any more (it stays hidden and git-ignored for volumes that still hold one).
- `backend/scadbuddy/meshmodel/` — mesh models (#2146, spec 2026-10-11): `scene.py`,
  `compile.py`, `edits.py`, `shared.py` (what `edits` shares with the render and geometry
  analysis, which must not import `edits`: `JointSpec`, `WALL`, `MeshOf`, `SceneEditError`,
  `transform_matrix`), `operations.py` (the `library` kinds), `paint.py` (paint
  transfer: a painted part's `.paint.json` codes carried onto its rendered solid by the
  render's `paint` stage, `render/jobs.py` `paint_layout`; a layout part names its carried
  codes file, `PartSource.paint`), `locate.py` (the scene object a weak joint is in).
  `render/geometry.py` `weak_joints` finds where a print snaps; `render/meshmatch.py`
  matches points and triangles in numpy (trimesh's proximity needs rtree, not installed).
- `backend/scadbuddy/workflows/arrange.py` — Arrange's packer (spec 2026-09-27 §7): goals,
  quarter turns, filament signatures, every plate checked with `plate.fit_problem`;
  `Arrange` in `workflows/pipelines.py` runs a `kind='arrange'` row (`POST /outputs/arrange`).
- `backend/scadbuddy/workflows/` — renders on Temporal (#424): `pipelines.py`
  (`TemplatePipeline`, its `RenderPiece` children, `RenderPreview`), `activities.py`
  (the render stages as activities, `WorkerDeps`), `client.py` (`connect`,
  `render_worker`, `print_worker`, `make_current`, `drained`), `models.py` (what crosses
  the history). Printing (#1052): `printing.py` (`PrintRunWorkflow`), `print_activities.py`
  (`PrintActivities`, `PrintDeps`), `print_models.py`; `commands.py` (`start_command`,
  update-with-start, the one way a route starts a command, spec 2026-10-01 §4.2). The
  `bambuddy` queue's worker runs inside the API process until #1060.
  Housekeeping (#1054): `housekeeping.py` (`Housekeeping`, `ensure_schedule`), the
  periodic sweeps as Temporal Schedules (every sweep, and `PRUNE_SWEEPS` on its own
  300 s one: the render prune, claims, lost slice starts and activity) on the `library`
  queue (`SCADBUDDY_TEMPORAL_TASK_QUEUE_LIBRARY`), whose worker also runs inside the API
  process (it holds the data volume). That worker is versioned (#4090): deployment
  `scadbuddy-library` (`client.library_deployment`, no default behavior), each API build
  made current at start (`main._make_library_current`, which also waits for the queue to
  route to it before the Schedules are set up), never drained. Every workflow on it is
  AUTO_UPGRADE through a `Versioned*` subclass (`main.LIBRARY_WORKFLOWS`; an unversioned
  worker cannot complete a workflow that declares a behavior), so each stays
  replay-compatible behind `patched`; a new one needs its own behavior or the worker
  refuses to build. Sweeps off (`asset_sweep_interval` 0) deletes the
  first, so only `PRUNE_SWEEPS` still run; the print dialog's background slices
  (`housekeeping_sweep_preview_slices`) are not among them. A new periodic pass is an
  activity in its `SWEEPS`, never a loop in the API. The preview backfill (#1054): `previews.py`
  (`PreviewBackfill`, `ensure_preview_schedule`), the pass over every model for missing
  default-render previews, on its own hourly Schedule on the same queue; the per-change
  requests stay in-process (`render/previews.py` `PreviewScheduler`).
  Generic commands (#1053): `operation.py` (`OperationWorkflow`: check, insert, run,
  finish; a kind its worker does not register fails the workflow task until a build that
  has it is current, #4139, told by `registered.py` `RegisteredActivities`),
  `operation_activities.py`, `operation_models.py`; `problems.py` (`problem_of`).
  Flows (#1057, plan `docs/superpowers/plans/2026-10-09-durable-phase-6-flows.md`):
  `project.py` (`ProjectWorkflow`, a model-free temporal-agent-harness agent that runs
  the script in Code Mode), `flow_tools.py` (the host functions, harness tools; no
  `from __future__ import annotations`), `flow_steps.py` (each host call's step on the
  row), `flow_entries.py` (a parked entry's timer, ended by the harness's own Update:
  a callback answered `timed out`, an approval denied at the run's approval timeout),
  `flow_routes.py` (`render`, `save_output`, `queue_print` and `arrange` call the API's
  own routes at `SCADBUDDY_API_INTERNAL_URL` with a key per call; not `print`, which
  Monty's builtin shadows); `tool(name, args)` takes one of the agent's own tools, its
  tier asked of the agent per call (`flow_tool_tier`, agent `temporal/flowTools.ts`; the
  API reads the same from the agent sidecar's loopback `GET /internal/flows/tools`,
  `flows/agent_tools.py`; never a file, #2704), or a
  `<server>__<tool>` turn tool (a remote plugin's, `scadbuddy_http__http_request`;
  #2320, behind `patched("flow-turn-tools-2320")`) that the agent's `turn_tool_tier`
  tiers and names the queue of, as the run's starter;
  `flow_activities.py`, `flow_models.py`,
  `projects_worker.py`, on the `projects` queue
  (`SCADBUDDY_TEMPORAL_TASK_QUEUE_PROJECTS`), versioned AUTO_UPGRADE in its own
  Deployment (#4146, `client.projects_deployment`, never pinned; unversioned in-process),
  so a run moves to each new build and a change must replay
  `tests/fixtures/project_workflow_histories/`. A run whose start names a host function
  (`FlowStart.host_functions`) its worker lacks waits after `flow_record` for a build
  that has it (`project.offers_host_functions`). A flow's payloads are sealed per run
  (`payload_codec.py`, `flows_client.py`), so flows need `SCADBUDDY_SECRET_KEY_FILE`.
- `backend/scadbuddy/flows/` — flow versions and runs (`store.py`: `workflow_definitions`,
  `workflow_runs`, the latter written only by the run's workflow), `typecheck.py` (a
  script's check at registration and at each start), `history.py` (a Reset's preview: the
  outward sends past a point, by activity id), `component.py` (`FLOWS`; runs the
  `projects` worker in-process with `SCADBUDDY_TEMPORAL_WORKER_INPROCESS` or
  `SCADBUDDY_TEMPORAL_PROJECTS_WORKER_INPROCESS`), `forget.py` (`DELETE
  /workflow-runs/{id}`: key, then workflow, then row), `sweep.py` (the housekeeping
  sweep for runs whose execution closed). Routes `api/flows.py`. An outward call's
  approval timeout resolves from the run, its flow, then `flow_approval_timeout_seconds`
  (0 is never).
- `backend/scadbuddy/flow_triggers/` — runs started on a schedule or an event (#2484,
  spec 2026-10-11 §12): `store.py` (`flow_triggers`, their standing grants and fires),
  `fire.py` (the `flow_trigger_fire` activity on the `library` queue), `events.py` (the
  event bus to `FlowTriggerFire`, one per trigger and event); the workflow and the
  Schedules are `workflows/flow_triggers.py`, routes `api/flow_triggers.py`. A fire reads
  everything from the trigger's row (the Schedule carries the trigger id only) and runs
  as its creator, with a run grant only from a standing grant a person approved; never
  take an author or a grant status from a body. A trigger pins a version; any edit
  returns its standing grant to requested, and an approval names the state it saw
  (`seen`); a fire is admitted, and a standing grant spent, under row locks.
- `backend/scadbuddy/operations/` — the `operations` record (`store.py`, the table
  `operations`), `kinds.py` (`OperationKind`: a kind's check, its effect, its
  attempts, and its queue) and `component.py` (`OPERATIONS`, `OperationsDep`). A
  feature registers its kinds by exporting `OPERATION_KINDS` (a `KindsBuild`) from its
  `scadbuddy/<feature>/operations.py`, found like components, never by editing a list.
  Each worker serves only its queue's kinds: the Bambuddy kinds are
  `bambuddy/operations.py` (queue `bambuddy`); `library/operations.py` (queue
  `library`, #1054) exports the library pins with a model's lifecycle
  and edits (`library/model_operations.py` `model_kinds`: create, import, patch,
  duplicate, delete, source, README, thumbnail, sibling files, restore and upstream; an
  upstream merge that would conflict is refused by the route, so its merged text never
  enters a history), a model's media (`media_operations.py`), outputs
  (`output_operations.py`: create, thumbnail, delete with its inbox copies until #1060
  splits the Bambuddy part out), assets (`asset_operations.py`: upload, fetch), font
  install (`font_operations.py`) and the preset writes that run openscad
  (`preset_operations.py`; a preset delete stays a plain route). Request bytes too large
  for a workflow payload (a create's source, thumbnail, README, a patch's presets, an
  import's or asset fetch's URL, an asset, an output's thumbnail, a media upload) go
  by claim check: `operations/claims.py` `ClaimStore`, under `cache/claims/`, named by
  sha256 so a re-send keeps its key. A media upload is streamed, never read into
  memory: `ClaimStore.hold_file` moves the streamed file in under the digest hashed
  while it streamed, and the run links it (`ClaimStore.link`) to a staging file of its
  own, so the claim stays for its release. `run_operation(..., claimed=)` releases them once
  the answer is final: only what its own `hold` created, unless a later put rewrote it
  or a running operation names the digest; the rest go to the
  `housekeeping_sweep_claims` sweep (on the prune Schedule, so sweeps off still sweeps
  them). `run_operation` refuses an inline request over `MAX_REQUEST_BYTES` (128 KB)
  with 413. A kind reads the state when it runs, never a route dependency, so a test
  replaces a store on the state (`state.libraries = store`). `api/operations.py`
  `run_operation` is how a route runs a kind (`Idempotency-Key` header; 202 with the
  operation past the deadline) and serves `GET /operations/{id}`. The browser's
  `command()` (`frontend/src/api/client.ts`) and the agent's (`agent/src/tools/command.ts`)
  send the
  key, re-send it after an answer that never arrived, and follow a 202.
  `render_key` coalesces identical *jobs*; `piece_key` dedupes identical *openscad
  renders* across jobs. Never swap them. A third key, `geometry_key`
  (`render/geometry_cache.py`, the `piece_geometry` table), is a mesh model's only: its
  `scene.json` with each colour reduced to which colours are equal, its meshes and its
  parameters, so a recolour edit (a new revision, a new piece key) keeps it. On a hit
  `RenderPiece` runs `reuse_geometry` (the rendered piece's files copied, `layout.json`
  and `preview.glb` recoloured) and then `finish_piece`, never openscad. A `.scad`
  template gets no geometry key: its colours can flow into anything the program
  computes, so only rendering it tells. Template pipelines (#427): `TemplatePipeline`
  runs a template's `pipeline/pipeline.py`, or the built-in default, `exec`'d in the
  workflow sandbox; `MigrateInputs`; `ctx.py` (the `ctx` a pipeline gets),
  `pipeline_activities.py` (`load_pipeline`, `pack`, `write_output`,
  `run_template_activity`, `migrate_inputs`), `template_process.py`/`template_runner.py`
  (template Python in its own process group, env allowlisted), `verify_pipeline.py`
  (for `verify.sh`). `scadbuddy/template.py` is the surface a template's
  `activities.py` imports.
- `backend/scadbuddy/slicing/` — the native slicer's Python side (#3822, spec 2026-10-16
  §5, plan `docs/superpowers/plans/2026-10-16-native-slicer-rust-worker.md`), on
  `slicer_backend` `native`: `models.py` (`slice_plate`'s payloads, pinned to the Rust
  worker by a vector file), `profiles.py` (Bambu Studio's tree in the API image,
  `/opt/slicer-profiles` from the Dockerfile's `slicer-dist`; `Profiles.version` keys the
  slices), `presets.py` (a `SlicePlan` built into machine, process and filament files,
  `native_preset_key`), `slice_info.py` (grams and G-code from the sliced 3MF),
  `transfer.py` and `transfer_token.py` (the worker's transfer URLs: presigned S3 GET or
  the internal blob routes, a token MACed under a KEK-derived key; never log one),
  `store.py` (`native_slices`; its blobs held in `blob_refs` while the row stands),
  `start.py` (`start_native_slice`: one execution per copy, subject, copy sha256 and
  presets, which the dialog and the run share; the lost-row repair), `preview.py` (the
  dialog's reads under `?slicer=native`) and `component.py` (`SLICING`).
  `workflows/slicing.py` is `SliceWorkflow` on the `library` queue (activities in
  `workflows/slicing_activities.py`, the run's `slice_run` and `print_sliced_upload`
  among them); the print worker only starts it. `api/internal_blobs.py` is the hidden
  `GET`/`PUT /api/v1/internal/blobs/{key}` the worker reads and writes through.
  `SliceWorkflow` histories replay from `tests/fixtures/slice_workflow_histories/`; a
  change goes behind `workflow.patched`. The worker is `workers/rust/`
  (`scadbuddy-slicer`, README "Slicer worker").
- `backend/scadbuddy/printers/hot_path.py` — the native hot path (#3818 tier A, plan
  `docs/superpowers/plans/2026-10-18-hot-path-without-bambuddy.md`): a print that
  touches Bambuddy nowhere. `hot_path_native(settings, path)` decides per run from the
  stored backends (`slicer_backend`, `library_backend` `native`, `store_backend` `s3` or
  `bambuddy`) and the printer row's resolved path; there is no switch of its own. A
  native run passes `client=None` (`bambuddy/client.py` `maybe_client`), reads printer
  state from the LAN report (`printers/reports.py`), and its filaments are the loaded
  trays plus the physical-spool overlay of cached provider records (`spools/`, refreshed
  off the path): Bambuddy's inventory and ScadBuddy's own (`native_spools`,
  `spools/native.py`, provider `native` in `spools/spool_providers.py`, routes
  `api/native_spools.py`, #4643; each write re-caches it at once). `printers/ams_mapping.py` computes `ams_mapping`/`ams_mapping2` from the
  plan's trays and `printers/nozzle_mapping.py` an H2C rack print's `nozzle_mapping`
  (measured values in the spec's §2); `printers/lan.py` is the FTPS upload and MQTT
  command. `slicing/catalogue.py` and `slicing/values.py` give a native run the image's
  presets and a process preset's values in Bambuddy's shapes, with Bambuddy's `local`
  presets from the kept copy `slicing/held_presets.py` (#4645: `held_local_presets`,
  refreshed on the spool providers' Schedule, read through `Profiles.held`; cloud user
  presets are not offered, D5). `print_library/copies.py` files the laid-out per-print copy in the
  native library, marked with its run's subject (`library_files.print_subject`, as is
  its slice); `print_library/sweep.py` `sweep_print_copies` (housekeeping
  `NATIVE_COPIES_SWEEP`, #4337) trashes a folder's superseded copies and what nothing
  records, as Bambuddy's inbox keeps its copies; a moved, restored or copied one is the
  person's (no subject). The Customize page, the dialog's
  first read and its analysis, and the Prints page's queue and history panels ask
  Bambuddy nothing on these backends (#4601, #4629; the panels read it only when opened).
  A LAN print's records (#4644, #4543): `print_succeed` writes its `output_last_prints`
  row (`print_route` `lan`), and `lan_print` saves its rack picks keyed by run and
  `subtask_name` (`rack_nozzle_*`'s `lan_run_id`, `lan_subtask`); the follow's read hooks
  (`Follower.on_read`: `printers/progress.py` `lan_last_print`, `rack/usage.py`
  `lan_settle_hook`) move the row and settle the picks, `lan_wait` each earlier copy's.
  A run of several plates or copies is its own queue (#4541, `workflows/printing.py`
  `_print_lan`): it succeeds at its first start, then `lan_wait` (`printers/print_activities.py`,
  over the watch's `printer_reports`) waits out each print before the next starts, and
  `print_lan_update` rewrites the result (`PrintRunResult.lan_waiting`, `lan_stopped`).
  A change to what a native run reads must keep `e2e-stack/`'s recorder pass at zero.
  A native run of several prints starts the later ones in `PrintLanSequence`
  (`print-lan-<run id>`, `workflows/printing.py` `LanSequenceWorkflow`); its controls
  (#4942, #4939) are `POST /print/runs/{id}/stop` (cancels the sequence, which records
  `lan_stopped`) and `…/plate-cleared` (the `plate_cleared` signal, for a run planned with
  the stored setting `lan_require_plate_clear`, behind `PLATE_CLEAR_PATCH`), shown from
  `PrintProgress.lan_run` and `PrinterNow.lan_run` by `components/print/LanRunControls.tsx`,
  and the agent's `stop_print_run` and `confirm_plate_clear`.
- `e2e-stack/` — the real-stack e2e of the hot path (#4602): `compose.yaml` (ScadBuddy on
  moto's S3, the Temporal dev server, the slicer worker, a fake LAN printer and a
  Bambuddy recorder that fails every call), `run.sh` (builds, brings up, seeds with
  `seed.sh`, runs `frontend/e2e/real-hot-path.spec.ts`, tears down by compose project),
  `fake-printer/` (MQTT and FTPS) and `bambuddy-recorder/`; their tests are
  `backend/tests/printers/test_fake_printer.py` and `backend/tests/test_bambuddy_recorder.py`.
  `.github/workflows/hot-path-e2e.yml` runs the stack (its `stack` job) weekly and on
  dispatch, never on a PR. Its `fakes` job (those two tests, ruff and mypy over the fakes)
  runs on a PR that touches `e2e-stack/**`, `backend/**` or the workflow, and is not in
  `CI Summary`. The test image carries mosquitto (`SCADBUDDY_TEST_MQTT`) and both fakes
  (#4541), so the fake-printer tests (`tests/test_print_native_workflow.py`,
  `tests/api/test_hot_path_native.py` and the fakes' own) run in every backend-pytest shard;
  locally they skip without a `mosquitto` on `PATH` or `SCADBUDDY_TEST_MQTT`.
- `backend/scadbuddy/store/` — the blob store. Phase 1: the directory-shaped `BlobStore`
  Protocol and `LocalBlobStore` (`local.py`, a piece in `data/blobs/<piece_key>/`),
  `BlobRefs` (`refs.py`, the `blob_refs` table that keeps a blob alive) and `sweep_blobs`
  (the grace-period sweep). Phase 3 (#426, spec §6): `content.py`/`content_models.py`
  (`ContentStore`, content-addressed keys over a backend), `index.py` (the Postgres index
  with CAS), `archive.py` (a directory as one object), `cache.py` (`CachedBlobStore`, the
  worker's bounded local copy), `bambuddy.py` (`BambuddyContentBackend`, Bambuddy's
  library as the backend, `verify_bambuddy.py` its check), `locks.py`, `fonts.py`
  (`FontMirror`), `snapshots.py` (revision snapshots for workers) and `assets.py`
  (`RemoteAssets`, uploads reaching workers). `factory.py` builds the `StoreBundle` that
  wires them, reading `store_backend` (a stored setting) at start, through `backends.py`
  (`build_backend`, and `UnrecordedSweeper`, the sweep of objects an upload left without a
  row). `s3.py` is `S3ContentBackend` (`store_backend` `s3`, multipart above 64 MiB).
  `migrate.py` and `workflows/store_migration.py` copy blobs between backends in two
  phases (copy and record, then flip `store_backend` and restart; `api/store.py`
  `/store/migrate`).

  `python -m scadbuddy.store.verify_bambuddy` re-measures §6.3; it has not yet been
  run against a live Bambuddy (`tests/bambuddy/recordings/README.md`).
- `backend/scadbuddy/worker.py` — `python -m scadbuddy.worker`: the render worker,
  `/healthz` and `/metrics` on 9090; makes its build current at start and drains its
  pinned workflows on SIGTERM. `run_inprocess_worker` is the API's
  `SCADBUDDY_TEMPORAL_WORKER_INPROCESS` mode (no drain). `--queue bambuddy`
  (`run_print_worker`, #1060) is the print worker, deployment `scadbuddy-print`: no data
  volume, Postgres and `SCADBUDDY_API_INTERNAL_URL` only (`build_print_deps`); every
  workflow on it AUTO_UPGRADE (`print_deployment`'s default, #2066; `FollowPrint` names
  it itself as `VersionedFollowPrint`, since an unversioned worker refuses a versioning
  behavior), so an `Operation` moves to the current build, which #4139's deferral
  needs. The API serves `bambuddy` itself, unversioned, only with
  `SCADBUDDY_TEMPORAL_WORKER_INPROCESS` or `SCADBUDDY_TEMPORAL_PRINT_WORKER_INPROCESS`.
  `--queue projects` (`run_projects_worker`, #1057) serves flow runs, deployment
  `scadbuddy-projects` (#4146; `/readyz` once the queue routes to it, as the print
  worker's): Postgres and the KEK only, and it refuses to start without the KEK.
- `backend/scadbuddy/bambuddy/` — httpx client (`client.py`), send/print routes
  (`send.py`, `dispatch.py`, `print_run.py`, `filaments.py`, `projects.py`), scope-aware
  error mapping (`errors.py`). Everything on the `bambuddy` queue reads outputs through
  `output_reader.py`'s `OutputReader` (#1060): `LocalOutputs` on the volume,
  `RemoteOutputs` through the API's hidden `/api/v1/internal/outputs/…` routes
  (`api/internal.py`). An output's last print is `output_last_prints` in Postgres
  (`library/output_prints.py`), laid over an older `meta.json`'s. A kind may name a
  `prelude`, another kind run first on its own queue: `output_delete` (library) names
  `output_inbox_delete` (bambuddy). How a print ended is `print_outcomes`
  (`outcomes.py`, #2665): the follow's settled read (`Follower.record_outcome`, every
  path) records it once against the run that queued or started it and publishes
  `print.outcome` in that transaction; `print.finished`/`print.failed` triggers fire on
  it, never on the per-process `print.settled`.
- `backend/scadbuddy/core/gitrepo.py` — `GitRepo`, the hermetic git core (#2503) under
  model history (`library/history.py` `ModelHistory`) and flow scripts
  (`flows/repository.py` `FlowHistory`: `data/flows/<name>/flow.py`, one commit per
  version; `flows/backfill.py` is the start-time backfill and reconciler;
  `flows/history.py` is the Reset preview, not git). Both repositories are shared by
  every replica over NFS, so each writer holds a Postgres lock that carries the last HEAD
  (`library/repo_lock.py`: `models_repo_head`, and `flows_coordinator`'s `flows_repo_head`).
- `backend/scadbuddy/library/` — catalogue, outputs, git-backed model history
  (`history.py`), fonts (`fonts.py`, `googlefonts.py`), per-template presets
  (`presets.py`: saved ones in Postgres, the `saved_presets` table (#332), outside git so
  a save never moves a template's revision; a template's own read-only ones in the `presets` list of its
  `model.json`, with a legacy `presets.json` still read), uploads for `// file`
  parameters (`assets.py`: the bytes under `data/assets/`, the metadata, last use and
  usage in the `assets` table (#591); a blob with no row is an orphan the sweep removes).
- `backend/scadbuddy/editor/` — the language server's guards (#95): `nonet.py`, the
  launcher every openscad-lsp start goes through (`nonet.command`; a seccomp filter, no
  network sockets), and `component.py`, the per-client session cap (on only with
  `SCADBUDDY_TRUSTED_PROXIES` set). The bridge itself is `library/lsp.py`.
- `backend/scadbuddy/api/` — FastAPI routes under `/api/v1`; `core/` — config/settings
  (every env var is `SCADBUDDY_<FIELD>`, see `core/settings.py`).
- `backend/scadbuddy/core/tracing.py` — OpenTelemetry (#988): the provider from the
  standard `OTEL_*` variables, the sampler (parentless `CLIENT` spans dropped), and the
  helpers every traced file uses (`span`, `detached_span` for a span exited in another
  task such as a stream, `current_traceparent`, `link_to`, `use_traceparent`).
  `core/trace_scrub.py` strips exception messages and status
  descriptions before anything is exported; never record a parameter value, a log
  line or anything Bambuddy returns. Tests share one provider (`tests/conftest.py`,
  fixture `spans`); the Bambuddy client injects no trace headers.
  The browser relay (`POST /telemetry/v1/traces`, route `api/telemetry.py`, feature
  `scadbuddy/telemetry/`): `admission.py` (same-origin checks and the rate limits; the
  client by `core/proxies.py` and `SCADBUDDY_TRUSTED_PROXIES`, the agent's rules),
  `payload.py` (rebuilds, caps and scrubs the page's spans), `forwarder.py` (the queue
  and the one uninstrumented httpx client; never retries), `target.py` (the collector URL
  and headers from the `OTEL_*` variables, `None` unless `traces_export_enabled()`).
- A new backend service is a `Component` (`core/components.py`) in a `component.py`
  beside its feature (`scadbuddy/<feature>/component.py`, discovered), never a new
  `AppState` field; routes read it through `api/components.py` `component_dep` (#508).
- Activity (principal jobs, spec `2026-10-09-principal-jobs-design.md`): the `activity`
  table is one row per piece of background work, written by the sources (render, operation,
  print run, agent operation) through `ActivityIndex.upsert_in` (`backend/scadbuddy/activity/`:
  `index.py`, `kinds.py` the registry, `models.py`, `component.py` `ACTIVITY`), under a
  savepoint so a failed write never fails the work. A feature registers kinds by exporting
  `ACTIVITY_KINDS` from its `scadbuddy/<feature>/activity.py` (found like components;
  `agent.*` keys are the agent's). A route attributes the job it starts with
  `attribute_request(activity.index, "<source>:<id>")` (author headers, else `browser`), as
  `render_model`, `post_run` and `arrange_outputs` do. `api/activity.py` serves
  `/activity`, `/activity/facets` and `/activity/{id}`, and the realtime `activity` topic;
  both read everything, as the browser does. The `housekeeping_activity` sweep (last in
  `SWEEPS`, behind `workflow.patched("housekeeping-activity")`) repairs, then prunes, on the
  prune Schedule (settings `activity_ttl_seconds`, `activity_repair_window_seconds`,
  `activity_follow`). The agent: `src/activity/kinds.ts` (its kinds) and `tools/activity.ts`
  (`activity_list`, `activity_get`, `activity_watch`), the tier boundary: a caller without
  `outward` sees only its own principal's. Frontend: `pages/ActivityPage.tsx`,
  `components/activity/` (table, filters, drawer, `ActivityBell`) and
  `mocks/features/activity.ts`.
- `backend/scadbuddy/orders/` — orders phase 1 (#4249, epic #4248, spec
  `2026-10-18-orders-design.md`, plan `2026-10-19-orders-phase-1-customers-and-orders.md`):
  customers, orders and line items as ScadBuddy's own records. `component.py` `ORDERS`
  (`OrdersComponent`: `store` over a pool of its own opened on first use, `lifecycle` over the
  core's bus; `OrdersDep`), `models.py` (the wire and row shapes; money is integer minor
  units, `*_minor`), `store.py` (`OrderStore`, plain CRUD per table; `transaction()` and
  `lock_order`; a customer's contacts and addresses replaced whole but kept by id, so an open
  order's address pointer survives an edit), `numbers.py` (`ORD-<yyyy>-<n>` from the
  `order_numbers` counter row under `FOR UPDATE` in the insert's transaction: gapless, never a
  sequence), `reference.py` (`resolve_reference`: the slug exists, the commit is expanded to
  its full id and must have touched the slug's directory, the inputs normalise against that
  commit's schema; an output's commit, libraries and colours when made from one; a deleted
  template is `TemplateDeletedError`), `lifecycle.py` (`Lifecycle`: every transition opens a
  transaction, takes the order's row lock, checks, writes, derives the status past `accepted`
  with the pure `derive` (`in_production` from an attempt, `ready`, `shipped` once shipments
  cover every fulfilled item), writes the audit row (`order_events`, one per status change)
  and publishes after commit; a refusal is `LifecycleError(slug, items, shipments)`, which
  `api/orders.py` maps to a 409 problem `https://scadbuddy.dev/problems/orders/<slug>`; on
  hold every transition that advances the order is `order_on_hold`, never release, a cancel,
  a payment or a plain edit), `actor.py` (`created_by` and an audit row's `by` from the
  author header, never a body). The bus events are `OrderChanged` (`order.changed`) and
  `OrderItemChanged` (`order_item.changed`), ids and the new status only, on the realtime
  topics `orders` and `order:<id>`. Every item's `inputs` join `referenced_asset_ids`, and
  `main.sweep_assets` skips the pass when they cannot be read. Routes `api/customers.py`,
  `api/orders.py` (plain reads and writes; the slice quote is phase 2). Agent tools
  `agent/src/tools/orders.ts` (`read` lists and gets, `write` records the shop keeps,
  `outward` what commits it to the customer: accept, a quote's acceptance, fulfil, ship,
  close, cancel). Frontend: `pages/OrdersPage.tsx`, `OrderPage.tsx`, `CustomersPage.tsx`,
  `CustomerPage.tsx` over `components/records/` (`DataTable`, `FilterBar`, `RecordDrawer`,
  extracted from the Activity components, which wrap them) and `components/orders/`; the
  Customize page's "Add to order…" and an output's "Order this" (`AddToOrderDialog`); mocks
  `src/mocks/features/orders/`; `lib/ordersQuery.ts` keeps the list's filters in the URL.
  Phase 2, pricing (#4250, plan `2026-10-22-orders-phase-2-pricing.md`): the price book is
  `filaments` rows (`pricebook.py` `PriceBookReader`, the store's `*_filament` methods) and
  the stored settings `pricing_*` and `rough_estimate_factor` (`live`), behind `GET`/`PUT
  /orders/price-book` and `…/price-book/filaments` (an archived filament comes back, its id
  kept, through `…/{id}/unarchive`, 409 `filament_exists` when its identity was taken since,
  #4977); `currency.py` is the one table of
  minor-unit exponents (ISO 4217), published as `minor_exponent` on every order and on the
  book (the browser's `lib/money.ts` takes it as an argument and never asks `Intl` for the
  digits), and `orders_currency` refuses a code with no minor unit. `pricing.py` is the
  arithmetic on `Decimal` (one rounding per line, `to_major`'s difference a `rounding`
  line). A slice quote is the `order_item_quote` kind of `operations.py` on the `library`
  queue (`POST /orders/{id}/items/{item}/quote`, subject `order_item:<id>`, 30-minute
  `QUOTE_TIMEOUT`), its steps `quote.py`: the check writes nothing; the run writes the
  body's filaments onto the item (`update_item`'s `filaments`, a repin only when a slot
  changes), renders and saves an output through `output_create`'s run when the item has
  none matching, slices every plate through `bambuddy/preview.py` `start_preview_plates`
  (the dialog's background slice, either slicer), keeps each plate's sliced file as the
  directory blob `quote-<sha256>` held by `blob_refs (order_item_quote, <quote id>)`
  before the quote's row exists, reads each plate with `read_sliced`, prices and records
  it (`Lifecycle.record_slice_quote`). The lifecycle drops the quote holders §3.4 releases
  after each commit, and `housekeeping_reap_quote_holds` (both Schedules, behind its patch)
  drops any holder older than `QUOTE_TIMEOUT` that no standing quote names. `GET
  /orders/estimate` is the rough estimate from every plate's part volumes, never a record.
  The agent's `order_item_quote` takes `manual` or `slice`, beside `price_book_get`,
  `price_book_set` and `order_estimate`. Frontend: the Settings page's Orders section
  (`components/orders/FilamentsEditor.tsx`), `PrintPicker`'s `quote` mode with
  `QuoteFilaments` ("Quote from a slice…" in `QuoteEditor`), and the estimate in
  `AddToOrderDialog`; mocks `src/mocks/features/orders/priceBook.ts`.
  Attachments (#4251, spec §6.2 "As built"): any file on an order or a line item,
  `order_attachments` (subject `order:<id>` / `order_item:<id>`, typed by its bytes in
  `orders/attachments.py` `sniff`; only `orders/models.py` `INLINE` types are served as
  themselves, the rest as downloads; the spec carries the type sets, which
  `attachmentMedia.test.ts` checks the browser's copy against), uploaded through the claim store and the `order_attachment_add` kind (the
  blob `attachment-<sha256>`, its `blob_refs` row written in the attachment's own
  transaction), routes `api/order_attachments.py`; an item's `delivered_attachment_id`
  (`PUT …/items/{item}/delivered`) is its picture. Frontend `components/orders/OrderAttachments.tsx`,
  `ItemPicture.tsx`; mocks `src/mocks/features/orders/attachments.ts`.
  Undo (spec §3.5 "Undo"): `Lifecycle.reopen_order` (a closed order back to what
  `derive` gives its rows; a cancelled one back to the status its cancel's audit row
  recorded, with the items it took) and `reopen_item` (a cancelled item back to what its
  quotes and attempts give it), each taking its quotes' sliced-file holds again (`_hold`);
  `POST /customers/{id}/unarchive`. Agent `order_reopen` (outward, as a cancel),
  `customer_upsert`'s `unarchive` and `price_book_set`'s filament `unarchive`. `Lifecycle.unfulfil_item` (#4952, `POST
  …/items/{item}/unfulfil`, agent `order_item_unfulfil`, outward) clears the latest
  accepted attempt's `accepted_at` (the row stays) and sets the item to what its latest
  attempt gives; refused for an item on a shipment (`order_item_shipped`) and on hold.
  The browser's row menu and drawer (`components/orders/Unfulfil.tsx`) confirm first.
  The stored setting `orders_allow_unfulfil` (#4953, live, default on) turns it off:
  the route passes it as `allowed` and the lifecycle answers `unfulfil_disabled`.
  `OrderTotals` counts an item with no accepted quote at its newest quote's unit price
  times its quantity (`api/orders.py` `item_price`, the browser's `components/orders/price.ts`),
  with `pending_minor` and `unpriced_items`; accepting still needs every quote accepted.
- `frontend/src/` — React 19 + Vite; `src/mocks/` is the msw API used by vitest and
  the mocked e2e run. A new feature's mocks go under `src/mocks/features/`, in
  `<feature>.ts` or a `<feature>/` folder. Every `.ts` file there except tests is picked
  up without editing `handlers.ts`, and must export `handlers` (and optionally
  `reset`) (#508).
- `frontend/src/lib/tracing.ts` — the browser's OpenTelemetry (#988), a lazy chunk
  `main.tsx` loads after the first paint; spans leave through `traceScrub.ts` and
  `relayExporter.ts` to the backend relay `/telemetry/v1/traces`, and stop for the
  page's life when it answers `X-ScadBuddy-Tracing: off` (`startTracing` then undoes
  itself, so no `traceparent` is sent; msw always answers off,
  `src/mocks/features/telemetry.ts`). A user action is `traceAction`
  (`lib/traceAction.ts`, entry chunk, API only): a request issued after an `await` joins
  the action's trace only inside its `within`. `traceparent` goes on same-origin
  requests only. The chunk loads through `loadOptionalChunk` (`lib/staleChunks.ts`), so
  a blocked one (an error naming `tracing-<hash>.js`, or Safari's naming no URL while it
  loads) does not trigger the stale-chunk reload; any other chunk's error still does.
- `frontend/src/template-ui/` — template-owned UIs (#425): `host.ts` (Host API v1 over the page's
  inputs), `TemplateUi.tsx` (loads `ui/<module>` with `import()`, mounts into a shadow root, and
  reports a failure through `onFailure`; the Customize page then falls back to the generated form
  with a banner), `elements.ts` (`sb-param`/`sb-preview`/`sb-generate`, rendered by portal). Inputs are `{params, v, …ui state}` (`backend/scadbuddy/render/inputs.py`);
  `backend/scadbuddy/api/template_ui.py` serves `ui/**` live and at `/versions/{commit}/`;
  `api/static.py` `PAGE_CSP` (mirrored in `frontend/page-csp.txt`) is the page's policy.
  Its one `script-src` hash is `frontend/index.html`'s inline entry-reload handler (#4873):
  a page whose entry `index-*.js` or CSS 404s mid-rollout reloads, up to 3 times per failed
  URL, for parse-time tags only (a lazy chunk's CSS is `staleChunks.ts`'s, #4874). Editing
  that script changes the hash; `src/entryReload.test.ts` names the new one.
- `agent/` — the AI agent service (#261), TypeScript on the Claude Agent SDK, shipped
  as the Dockerfile's `agent` target and run as a sidecar container. `src/config.ts`
  reads only infrastructure variables (`ENV_VARS`): `SCADBUDDY_DATABASE_URL`,
  `SCADBUDDY_BACKEND_URL`, `SCADBUDDY_SECRET_KEY_FILE`,
  `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` (rotation), `SCADBUDDY_PUBLIC_URL` (the same
  variable the backend reads; the one origin allowed to write),
  `SCADBUDDY_AGENT_TRUSTED_PROXIES` (CIDRs whose `X-Forwarded-*` are believed) and the
  backend's `SCADBUDDY_TEMPORAL_ADDRESS`, `_NAMESPACE` and `_SEARCH_ATTRIBUTES`. No AI
  env vars; AI settings live in the database.
  `SCADBUDDY_AGENT_ROLE` (#2726 Step 6.1; `all` by default, `api`, `worker`) picks what
  `main.ts` starts (`src/runtime/role.ts` `roleParts`): `api` is the HTTP half and polls
  only its own `agent-tools.<hostname>` (browser calls for its tabs), so it runs no
  `AgentSession` turn; `worker` runs the turn workers and serves only
  `src/runtime/health.ts` (`/healthz`, `/readyz`). `test/agentRole.temporal.test.ts` runs
  one of each on one dev server; a change to what a turn reaches in-process must keep it.
  `src/app.ts` is the Hono server (`/healthz` and `/mcp`). Every other route group is a
  `src/routes/<name>.ts` that exports `route` (`routes/module.ts`) and is found without
  an edit to `app.ts`: among them `credentials.ts` (`/api/v1/ai/credentials`),
  `status.ts` (`/api/v1/ai/status`), and the assistant's WebSocket `/api/v1/ai/chat` and
  `/api/v1/ai/sessions` in `chat.ts` and `sessions.ts`, backed by `SessionManager`. A
  dependency only that group needs is declared in its own file, by augmenting `AppDeps`
  (#508); one several groups share (`sessions`, `upgradeWebSocket`) stays in `app.ts`.
  A route module imports only types from `app.ts` (`routes/index.ts` loads them while
  `app.ts` loads). Every route that must know "is this the UI's origin"
  (credential writes, `/mcp`, and the chat socket and session routes under `/api/v1/ai/*`) uses the one allowlist in
  `src/http/origins.ts`, never an `Origin == Host` comparison (DNS rebinding makes
  those equal). `src/harness/options.ts` builds every query's SDK options
  (`tools: []`, `settingSources: []`) and `src/harness/run.ts` runs every `query()` on
  top of it (credential via the per-query `env` only, `maxTurns`, `maxBudgetUsd`,
  abort, the tier seam in `src/harness/permissions.ts` as both `canUseTool` and a
  `PreToolUse` hook; outward calls in a session PARK in `canUseTool` until a human
  decides, via `src/approvals/service.ts` and the `ai_approvals` table, #258; outside
  a session they are denied as "needs approval"; the built-in `AskUserQuestion`, given
  only to sessions the browser user owns, parks there too until the user answers in the
  panel, via `src/harness/questions.ts` and the session workflow's gate (an `ai_pending_input`
  entry, the request's record, plan Step 9.7), #940, and never outlives its turn; the agent's
  `request_user_attention` tool, #815, `src/harness/attention.ts`, parks on the same gate as a
  `wait_for_user` entry, with a timer that never answers: `proceed` returns `timed_out`, `wait` and
  `stop` end the turn; its `done` reason waits for nothing and outlives its turn, an `ai_questions`
  row (`src/gate/questions.ts`), shown beside the badge's waiting count (not in it) until dismissed, at most
  `DONE_POSTS_PER_TURN` a turn, carrying `src/gate/doneSummary.ts`'s record of what the turn
  touched, unattended actions first; `GET /api/v1/ai/pending-input`, `src/routes/pendingInput.ts`, is
  the one read of every parked call, approvals and answers, that the badge counts, and
  `POST /api/v1/ai/pending-input/{request_id}` the one respond route the panel answers
  any of them through, refusing a stale id, a resolved entry or a body of the wrong kind;
  a browser_* call that finds no tab in such a session parks the same way as a
  `tab_disconnected` request, resolved `reconnected` when the bridge sees the session's
  tab again, `sessions/manager.ts` `waitForTab`, `bridge/hub.ts` `onSessionTab`; in an
  AgentSession turn both park as the workflow's `wait_for_user` entry, `sessions/turnHost.ts`,
  and the hub's process ends a tab wait with the Update `tab_reconnected`, #5003);
  `src/api/backend.ts` is the `openapi-fetch` client over the generated
  `src/api/schema.d.ts`.
  Tracing (#988): `src/telemetry.ts` is the `node --import` entry (Dockerfile `CMD`,
  `pnpm start`) that registers the OTel ESM hook, then `src/telemetry/setup.ts` starts
  the SDK (standard `OTEL_*` variables only; incoming HTTP only). `main.ts` imports
  only the SDK-free `src/telemetry/runtime.ts`, so with `OTEL_SDK_DISABLED=true` no SDK
  package is loaded (#1351).
  `src/telemetry/scrub.ts` strips exception messages, query strings and user agents
  before export; `src/telemetry/turn.ts` (`TurnTrace`) ends a turn's spans at every
  park and opens `agent.turn.resume` under the decision (`ai_approvals.traceparent`,
  `decision_traceparent`). Only `api/backend.ts`'s middleware injects `traceparent`;
  never add trace context to another outgoing call. Tests share one provider
  (`test/support/tracing.ts` `testTracing`).
  `src/tools/` is the tool registry (#251): one `defineTool`
  per tool, projected in-process for the harness and over `/mcp` (`src/mcp/http.ts`,
  auth in `src/auth/`); every `/api/v1` operation needs a tool or a
  `src/tools/coverage.ts` entry, or `test/coverage.test.ts` fails.
  - Database: the agent owns the `ai_*` tables. Schema changes are new files in
    `src/db/migrations/` (see "Migrations" below; `src/db/migrations.ts` applies them at
    start under advisory lock "SCADAGNT" with `lock_timeout`/`statement_timeout`,
    ledger `ai_migrations` with a sha256 per file: an edited merged file stops the
    service at start; separate from the backend's `scadbuddy_migrations`). Secrets are
    envelope-encrypted with `src/secrets.ts` under the KEK in
    `SCADBUDDY_SECRET_KEY_FILE` (32 random bytes, base64; spec §9); the AAD binds each
    value to its row and to the columns that say where it is sent (for the credential:
    `kind` and `base_url`). Comparable tokens are stored hashed instead.
  - Temporal (#1055, spec 2026-10-01 §6.3): with `SCADBUDDY_TEMPORAL_ADDRESS` (and the
    database) `src/temporal/worker.ts` runs one unversioned worker on `agent-tools`:
    every `ALL_TOOLS` entry as an activity under its name (`toolActivities.ts`,
    `runToolWithOutcome` with `gate: 'workflow'`, only for a `session-<id>` workflow (as the
    session's owner with its turn's tiers; `mode` is not read, since every session is an
    AgentSession from plan Step 6.3), or a flow run's `flow-<id>` (`flowRuns.ts`, #1057: as the
    run's starter, in no session; a gated call needs the backend's `workflow_run_decisions`
    approval; no browser tools)), a durable turn's other tools (#2320,
    `turnTools.ts`, built by `turnServers.ts` as a classic turn builds them (its `builtins`
    and `listing` paths served only the retired Python turn and go with `turnPackages.ts` in
    plan Step 9.3): remote plugins
    through the forwarder and `http_request`): `turn_tools` lists them per turn as
    `<server>__<tool>`, `turn_tool_tier` tiers a call from its input before it may park,
    and each call is the worker's `default` activity `turn:<server>__<tool>`, which builds
    the servers again and checks tier, principal and approval as it runs (a `flow-<id>`
    workflow's call too: as the run's starter, approved only by `workflow_run_decisions`,
    with no browser servers; a server whose
    state lives on the pod, `http_request` and the browser, is `pinned`: its calls run on
    `agent-tools.<hostname>`, which each agent also polls); and `AgentOperation` (`workflows.ts`, bundled by
    `pnpm build` into `dist/temporal/workflow-bundle.js`), the §4.2 command shape for
    the agent's commands, recorded in `ai_operations` (`src/operations/`). A change to
    `AgentOperation` goes behind `patched()`; `test/fixtures/agent_operation_histories/`
    replays. Temporal tests (`test/*temporal*.test.ts`)
    skip unless `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` names a Temporal CLI (or
    `temporal` is on `PATH`); the `agent` CI job installs the Dockerfile's pinned one.
    The `@temporalio/*` packages are pinned exactly, all one version.
  - The tool-call gate (#1056 phase 5b, spec 2026-10-01 §6.6, plan
    `docs/superpowers/plans/2026-10-08-durable-phase-5b-tool-call-gate.md`) is
    `src/gate/`: request ids (`ids.ts`: `approval:`, `question:`, `durable:<session>:<run>:<tool_use_id>`),
    the `respond` validator shared with agent-durable (`validate.ts`; `role.ts`;
    `test/fixtures/pending-input-vectors.json` pins the two languages together),
    the classic entries (`classic.ts`), the durable projection read (`projection.ts`,
    `ai_pending_input`), the Temporal client of a session's gate handlers (`durable.ts`
    `DurableGate`) and the orphan sweep (`sweep.ts`). `routes/pendingInput.ts` serves
    both reads and `respond`. `input.requested` / `input.resolved` are logged in both modes and never
    sent on the chat socket (`routes/chat.ts` `sentToPanel`).
  - Durable payloads (#1056 phase 5c, spec §6.5): `src/temporal/payloadCodec.ts` seals
    every payload of a `session-<uuid>` / `flow-<uuid>` workflow under that subject's
    data key (`ai_payload_keys`, sealed under the KEK, re-wrapped at start on rotation);
    the agent-tools worker and the service's Temporal client carry it when the KEK is
    mounted. `src/sessions/forget.ts` `forgetSubject` (key row, then the workflow, then
    the rows) is the one way a durable subject is deleted; operators run
    `node dist/forget-subject.js session-<uuid>`. A fork reads its parent's transcript
    through the cut (`ai_sessions.fork_parent_id`, `fork_cut`; `src/sessions/store.ts`,
    plan Step 6.5), so forgetting a parent first copies each fork's range into it under
    the fork's key (`copyForkRanges`, `inherited` rows), and refuses without the KEK. A gated tool's activity runs only
    with an `approved` `ai_input_responses` row for its own request id.
  - DurableSession, the retired Python turn (plan 2026-10-13 Steps 7.2 and 8.1, #4772): its
    open sessions were moved to AgentSession on 2026-11-01 (their conversations in
    `ai_session_entries`). A row still `runtime = durable` closed before that and has no
    transcript: a send to it is refused `closed` (`sessions/durable.ts`), and a fork of it is
    an AgentSession with the panel's history and no model context (`manager.ts` `fork`).
  - The workspace (#2329, `docs/ai/operating.md` §13): `src/workspace/store.ts`
    `SessionFiles` over `ai_session_files`, keyed by `workspace` (`session:<id>`, gone
    with its session, copied by a fork; or `principal:<kind>:<id>` outside a session,
    swept 24 h after its last write) and a checked `path`; caps
    `workspace_file_max_bytes`/`workspace_max_bytes` and `WORKSPACE_MAX_FILES` (5,000). Reached as
    `ToolServices.workspace` (so in-process, `/mcp` and the durable activities alike),
    with `workspaceOf(ctx)`; the `workspace_*` tools are `src/tools/workspace.ts`, and
    `http_request`'s `save_to_workspace` gets the session's from `sessions/manager.ts`.
    Never a file on the pod; a session's working directory, `<SCADBUDDY_SESSION_DIR>/<id>/`
    (each worker's own capped emptyDir, Step 9.5; not the NAS share, whose backup cannot leave
    the plaintext copies out), is a copy of it that each turn's placement fills and each park
    and its end write back by sha256 (`src/workspace/directory.ts`). The bytes (#4789) are a blob of the backend's content store,
    sealed under a per-file key wrapped under the session's data key (`src/workspace/sealing.ts`,
    `blobs.ts`; backend `api/internal_workspace.py` mints the URLs; a trigger keeps the
    `blob_refs` holder with the row, which assumes one database and role for both services;
    `test/workspaceBlobs.backend.e2e.test.ts` runs it against the real API on moto,
    `backend/tests/support/workspace_backend.py`). Only on `store_backend` local do they stay
    in `data`; on any other backend a write that cannot be sealed and stored is refused
    `unavailable`, never kept in plaintext. Uploads take `workspace_path` + `workspace_sha256` instead of
    inline content (`src/tools/workspaceUploads.ts`: binary streamed by
    `SessionFiles.chunks` into the multipart route, text decoded as UTF-8).
    The panel's Files (part 3) are `routes/sessionFiles.ts`
    (list, download as an attachment, multipart upload, delete; the session routes'
    guards, writes owner-only, each write a `workspace.file` event) and the caps' route
    `routes/workspaceSettings.ts`; the frontend is `components/assistant/SessionFiles.tsx`
    with msw `src/mocks/features/sessionFiles.ts` (component tests stub the upload:
    jsdom's `File` cannot pass undici's multipart parser).
  - Plugins given to the harness are vetted by `src/harness/plugins.ts`: anything that
    starts a process (command hooks, stdio MCP servers, LSP servers, monitors) or runs
    plugin code in Claude Code (a hooks file's `modules`, on by default since Claude
    Code 2.1.287, and `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS` no longer turns it off) is
    refused, because it would inherit the credential env. A hooks file may set only
    `$schema`, `description` and `hooks` besides, so a new loader key is refused too.
    The one way past it is a plugin package approved with `allow_refused` (store.ts,
    column `allow_refused`, bound to that pin's approval): it loads as it is through
    `allowedPluginPaths`, unchecked, with the built-ins it names (`builtinTools`, each
    call `outward`) and skill shell injection on (with `Bash` offered, Claude Code
    2.1.287 runs an injection without asking the seam). A name that is not a safe path segment
    (`SAFE_NAME_RE`; a reserved name is allowable), paths outside the package, symlinks,
    the hash and the egress check stay fatal (`vet.ts` `fatal`).
  - Remote MCP plugins (#297) live in `ai_plugins` (`src/plugins/registry.ts`, routes
    `src/routes/plugins.ts` under `/api/v1/ai/plugins`). Claude Code never gets a
    plugin's URL or secret: it gets `http://127.0.0.1:<port>/p/<token>` on the loopback
    forwarder (`src/plugins/forwarder.ts`), which pins the checked address, refuses
    redirects and 401/OAuth discovery, and adds the header (Claude Code's own MCP client
    follows both with the header). Claude Code renames tool-name characters outside
    `[A-Za-z0-9_-]` to `_` (`harnessToolName`); only such names take a tier, and
    colliding tools are hidden. Unlisted plugin tools are `outward`.
  - Plugin packages (#297; skills, agents, hooks from git or a marketplace entry) live
    in `ai_plugin_packages` (`src/plugins/packages/`, routes
    `src/routes/pluginPackages.ts`). Postgres holds the pin (commit + content hash);
    `<state dir>/plugins/` is only a cache, re-hashed before every load and
    re-fetched from the pin. Install stores the pin unapproved; approving needs the
    exact commit and hash. `vet.ts` adds rules on top of `harness/plugins.ts`. Tests
    use local git repos (`test/support/gitRepo.ts`). Their MCP servers' http ones go
    through the forwarder (`plugins/packages/mcp.ts`); `temporal/turnPackages.ts` shares a
    package under `<payload dir>/plugins/<content hash>/` for the retired Python turn
    (Step 8.2 removes it). Of an allowed package's built-ins the engine
    runs `Bash`/`PowerShell` (each parked for approval); the agent provides the rest as
    the `builtin` turn tools (`src/builtins/`, #2393: the file tools on the session
    workspace under `/workspace`, WebFetch through the egress check, WebSearch and
    WebFetch's model work as one-shot queries on the session's credential, the task
    list as a workspace file), each outward, and the listing's `system_note` tells the
    model; `src/builtins/schemas.json` is Claude Code's own input schemas
    (`test/builtins.sdk.test.ts`, `UPDATE_BUILTIN_SCHEMAS=1`). `CronCreate`, `CronDelete`,
    `CronList`, `ScheduleWakeup`, `Monitor`, `Workflow` and `TaskStop` are the session's own in both modes
    (`src/background/`: `ai_background_tasks`, one `BackgroundTask` workflow per task on
    `agent-tools` that sends the prompt when due and the session is idle; a Monitor's source
    runs where the session's Bash runs, the replica's own queue, its events in
    `ai_background_events`; a Workflow's
    script runs in QuickJS (`workflowRunner.ts`, never Node's `vm`), each `agent()` a child chat
    (`workflowAgents.ts`), its results journalled in `ai_workflow_calls`; a classic turn
    gets them as the in-process `builtin` server in place of Claude Code's, which end
    with its process; the panel's list and cancel are `routes/backgroundTasks.ts`).
  - Tests never call Anthropic: `test/support/fakeAnthropic.ts` is a local Messages API
    (streaming SSE) that the real SDK and bundled CLI are pointed at as a gateway
    (`test/run.test.ts`). Postgres tests (`test/pg.test.ts`) skip unless
    `SCADBUDDY_TEST_DATABASE_URL` is set, as in the backend; the `agent` CI job sets it. The design is
  `docs/superpowers/specs/2026-09-27-ai-integration-design.md` (issue #250).
  The 09-22 design spec's "No database" statement (`2026-09-22-scadbuddy-design.md`
  §4, "Architecture") describes the backend container; the
  AI spec (#250, PR #303) adds Postgres (#241) for the system as a whole, and the
  09-27 template-pipelines spec makes Postgres and Temporal required.
- `agent-durable/` — ScadBuddy's Python agent parts (#1056; spec 2026-10-13 §14.4), shipped
  as the Dockerfile's `agent-durable` target, trusted like `agent`. It ran the Python turn
  (DurableSession on the `agent` queue) until plan Step 8.1 retired it: it now depends on no
  Claude Agent SDK, and `python -m scadbuddy_durable` serves `/healthz` on 8082 and runs no
  worker (`worker.py` `NO_WORKER`), until a Python workload exists (decision Q9; clusters
  keeps its Deployment at zero). What stays, tested against the agent's vector files:
  `secrets.py`/`credentials.py` (open `ai_credentials` as the agent does; only the agent
  writes a secret), `codec.py` and `payload_store.py` (the payload codec's Python half),
  and `gate/` (the gate's Python half: ids, the `respond` validator, `build_entry`, and the
  `open_input` / `resolve_input` writers of `ai_pending_input`, `ai_input_responses`, the
  session's events and the approval audit row, which `agent/test/gateStore.pg.test.ts`
  runs against the TypeScript store).
- `models/` — bundled example models (`models/<name>/verify.sh`).
- `deploy/grafana/` — the ScadBuddy Grafana dashboards (#988, tracing spec §7): the
  overview `scadbuddy.json` (uid `scadbuddy`; never change it) and one `<name>.json` per
  area (`scadbuddy-api`, `-web`, `-renders`, `-printing`, `-agent`), each with uid
  `<name>` and its own `configMapGenerator` ConfigMap `<name>-dashboard` in
  `cattle-dashboards` for the rancher-monitoring sidecar, datasources only as the
  `DS_PROMETHEUS`/`DS_TEMPO` variables. A new dashboard is a new file plus its
  generator entry. clusters will pull them in as a remote
  resource pinned to a full SHA, once clusters#1596 Phase 5 adds the line, and
  `deploy.reusable.yml` moves that `ref` with the image.
  A query may read only series `core/metrics.py` declares and span names the
  service emits (`lint-dashboard.sh` checks both, in every file); a span named at run
  time (a server span, `bambuddy.<op>`, Temporal's) is matched by attribute instead.
- `tools/go/` — Go modules in one workspace (#2686): `go.work` and the root `vendor/`
  at the repo root, so every build is offline. `scadbuddy-client/` is the
  Kiota-generated client (`client/`, committed, plus its own `vendor/` for module-mode
  consumers); `bambuddy-fs/` is the FUSE mount of Bambuddy's library over it
  (`internal/api` the client wrapper, `internal/fsys` the tree, cache, writes and the
  unsent queue, `internal/telemetry` OTel; README there). Its unsent writes are the
  only copy of a save, under the state directory, never the cache.
- `workers/rust/` — Rust Temporal workers and tools (spec 2026-10-16 §7):
  `crates/sb-slicer` the `slice_plate` activity, `crates/sb-transfer` GET/PUT of
  `TransferUrl`s (each body's sha256 reported, never checked: a `TransferUrl` carries
  none), `crates/sb-temporal` the worker bootstrap (slicer only),
  `bins/scadbuddy-slicer` the `slicer` queue's worker; `vectors/` pins payloads against
  the pydantic models (`UPDATE_SLICE_VECTORS=1` in `backend/tests/slicing/test_models.py`).
- `plugins/scadbuddy/` — ScadBuddy's Claude plugin (#299): skills (`authoring`,
  `customize`, `print`), subagents, and a `.mcp.json` for external installs; listed by
  the root `.claude-plugin/marketplace.json`. Every skill cites its sources, which
  `.github/scripts/lint-plugin.sh` checks; `claude plugin validate plugins/scadbuddy` is
  the authoritative manifest check. The agent loads `agent/plugins/scadbuddy/` instead
  (#896): its own `plugin.json` (no `userConfig`, no `.mcp.json`) with `skills/` and
  `agents/` symlinked here, so edit the files here; that query gets the `Skill` and
  `Agent` tools (`src/harness/ownPlugin.ts`). Every plugin change needs a new `version`
  (Claude Code keeps installs on the one they have): the `PostToolUse` hook in
  `.claude/settings.json` (`.github/scripts/plugin-edited.sh`) patch-bumps it on a
  branch's first plugin edit, mirrors it into the agent copy and runs the checks.

## Migrations (#491)

Both services keep one file per migration, named by UTC timestamp plus a slug:
`backend/scadbuddy/migrations/` (ledger `scadbuddy_migrations`, applied by
`render/pg_store.py` `migrate`) and `agent/src/db/migrations/` (ledger `ai_migrations`,
applied by `src/db/migrations.ts`). To add one, create a NEW file named
`$(date -u +%Y%m%dT%H%MZ)_<slug>.sql` (slug `[a-z0-9_]`) and edit nothing else. Never
edit, rename or remove a merged file; the agent checks each applied file's sha256 and
stops at start on a mismatch. At start every file not yet in the ledger is applied, in
timestamp order, under the service's advisory lock; that includes a file OLDER than ones
already applied (a branch that merged late), so a migration may depend only on files
already on main. The pre-#491 positional entries are frozen as `LEGACY_VERSIONS` in each
module; a ledger still keyed by position is rewritten to file ids once, and a positional
row main never had (a dev database that ran an unmerged branch's entry) stops the
service with `MigrationLedgerError` rather than being guessed at. The agent's files reach
the image because `pnpm build` copies them into `dist/db/migrations/`.

## Verified OpenSCAD facts (do not re-derive; re-measure if the base image moves)

- Base image is a pinned dated nightly, `openscad/openscad:dev.2026-10-26@sha256:…`
  (tag plus index digest; the only stable release, 2021.01, has no Manifold). The
  Dockerfile also asserts `OPENSCAD_VERSION` (currently 2026.10.26). Bump
  deliberately: re-verify spec §3 against the new build, then change the tag,
  digest and `OPENSCAD_VERSION` in the same commit. The weekly `OpenSCAD Bump`
  workflow (`openscad-bump.yml`) opens that PR when a newer nightly exists; its CI
  is the re-verification, and it is never auto-merged.
- **No Python in the base image.** The Dockerfile `apt install`s `python3` and uv
  provides 3.12. Do not switch to a Python base with OpenSCAD installed beside it —
  the facts below were measured on this exact image.
- `openscad -o model.param model.scad` exports the customizer schema as JSON.
  `// color` and `// font` annotations are *not* typed by OpenSCAD; ScadBuddy overlays
  them by scanning the source.
- `--backend=Manifold -o out.3mf` emits one object with `<basematerials>` and a
  per-triangle material index (`p1`). Ignore the `displaycolor` alpha byte (written
  as `00`).
- Splitting by `p1` gives *open* meshes wherever colours touch — fine for the preview
  only. Closed parts come from re-rendering once per colour with a wrapper that
  shadows the builtin `color` module (`render/solids.py`). `--enable=lazy-union`
  loses colour attribution; do not use it.
- `openscad --version` writes to stderr.
- Fonts: there is no family "Lobster" in the image, only "Lobster Two"; a missing
  family silently falls back to DejaVu and changes the geometry.
  Fonts named only inside a library checkout are not mirrored to render workers on
  the bambuddy store (`store/fonts.py` `wanted_families`); pass such a font as a
  parameter or name it in the template's own source.
- Bambu Studio only reads `project_settings.config` if the 3MF claims
  `Application: BambuStudio-…`, and then segfaults unless five options are present
  (spec §3). Keep them.

## Bambuddy iframe facts

- Bambuddy has no plugin system. ScadBuddy is added as an External Link with
  `open_in_new_tab=false`, which Bambuddy renders in a sandboxed iframe at
  `/external/{id}` with `sandbox="allow-scripts allow-same-origin allow-forms
  allow-popups allow-popups-to-escape-sandbox"` (verified in the 1.2.5.5 bundle).
- Downloads (`frontend/src/lib/embed.ts`): the sandbox has no `allow-downloads`, so
  Chromium silently drops a download started in the frame, `target=_blank` or not.
  When embedded, `downloadBlob` opens a blank popup first (it escapes the sandbox via
  `allow-popups-to-escape-sandbox` and is same-origin via `allow-same-origin`, so it can
  use the frame's blob URL), fetches the file as a blob, and clicks the download anchor
  in the popup. The popup is opened before the fetch, while the click still permits it.
  A blocked popup, or one closed before the file loaded, is an error the user sees
  (`DownloadBlockedError`, `DownloadWindowClosedError`), never a fallback to the
  frame's own anchor, which would fail silently.
  `e2e/downloads.spec.ts` checks this in a replica of the frame.
- Deep links to Bambuddy use `window.open(..., '_blank')` when embedded.
- Full screen (`frontend/src/lib/useFullscreen.ts`): a cross-origin iframe gets the
  Fullscreen API only with `allow="fullscreen"`, which Bambuddy is not known to set;
  where it is refused (`document.fullscreenEnabled` is false, or the request is
  rejected) the full-screen view covers the frame instead.
- The assistant (the agent's `/api/v1/ai/*`, including its WebSockets `/api/v1/ai/chat` and,
  for the browser bridge, `/api/v1/ai/bridge`)
  is reached on ScadBuddy's own origin: the ingress routes those paths to the agent
  sidecar (AI spec §4.2, `docs/ai/operating.md` §1.1). The sandbox's
  `allow-same-origin` is what keeps the frame's `Origin` ScadBuddy's own, and the agent's
  origin allowlist requires that. This is inferred from the sandbox attribute above and
  has not been exercised inside a live Bambuddy.
- The API key never reaches the browser; every Bambuddy call is server-side. Each
  client call declares its scope (`bambuddy/errors.py` `Scope`) so a 401/403 names it.

## CI and caching rules

- Every job runs on `ubuntu-latest`. The repo is public; never move a job onto the
  self-hosted `clusters-runner*` pools (fork PRs would run code on the LAN).
- **Never use the buildx `type=gha` cache on a self-hosted pool** — it fails the build
  there. It is used on the hosted runners in `ci.yml` and `build-image.yml`.
- Node major is pinned in both the Dockerfile and `ci.yml` (`24`); change them
  together, LTS (even) majors only. That covers the Dockerfile's `frontend` and three
  `agent*` stages and the `frontend`, `agent` and `freshness` jobs.
  `frontend/pnpm-workspace.yaml` and `agent/pnpm-workspace.yaml` must be copied into
  the Docker build (they hold `allowBuilds`; the agent's declines msw's install script).
- `@anthropic-ai/claude-agent-sdk` is pinned exactly in `agent/package.json`, and the
  lockfile pins the Claude Code binary it bundles. The image build checks that the
  binary reports the version the SDK declares (`agent/src/check-cli-version.ts`), so
  no second pin moves with a bump (#1540). A bump still changes the harness under
  every query: the `*.e2e`/`*.sdk` tests run the real binary, and a "measured on"
  note is re-dated only when what backs it was re-checked.
- The `agent` jobs in `ci.yml` and `build-image.yml` use the buildx `type=gha` cache
  with `scope=agent`, so they do not overwrite the backend image's cache index.
- Every `docker/setup-buildx-action` step sets `driver-opts:
  image=mirror.gcr.io/moby/buildkit:buildx-stable-1` (#3370): Docker Hub refuses some
  anonymous pulls from hosted runners, which failed jobs before any test ran. A new
  buildx step needs it too. The tag floats with buildx's own default; nothing would
  bump a digest pinned there. Each step also gives its daemon a `buildkitd-config-inline`
  `[registry."docker.io"] mirrors = ["mirror.gcr.io"]` (#4012), so the Dockerfiles'
  Docker Hub bases come through the mirror while the Dockerfiles keep their plain
  references for Dependabot and `openscad-bump.yml` (BuildKit falls back to Docker Hub
  for an image the mirror lacks; the pinned digests matched on both on 2026-10-19,
  `docker buildx imagetools inspect`). `lint-buildx-mirror.sh` checks both. The
  workflows' `postgres:18` service containers (the major prod's `scadbuddy-db` runs, #4760) are `mirror.gcr.io/library/postgres:18`;
  a service container has no such fallback, so a tag the mirror lacks fails the job.
  So they are pinned by digest, `mirror.gcr.io/library/postgres:18@sha256:…`, all one
  digest, as is `e2e-stack/compose.yaml`'s (#4729: the moving tag once reached a layer the mirror answered `unknown blob`
  for, and every backend job failed). `postgres-pin.sh` checks the pins in the `lint`
  job; with `--check`, the weekly `postgres-pin.yml` checks that the mirror holds every
  blob of the pin's amd64 image and fails when `18` has moved, saying whether the mirror
  holds the new digest's blobs yet. Move every pin together, only once it does.
- The repo's Actions cache has a ~10 GB ceiling, and it is full (9.9 GB on 2026-09-30,
  almost all `buildkit-blob`): GitHub evicts the least recently used entries. A new
  `type=gha` scope should use `mode=min` unless it needs its intermediate stages; if a
  scope's hit rate drops, move it to `type=registry` on GHCR, which works only on
  pushes and same-repo PRs, not forks (#743).
- No CI job builds arm64. `build-image.yml` (all three images) and `build-go-image.yml`
  publish `linux/amd64` only, and nothing in `build-image.yml` runs on PRs. arm64
  (QEMU-emulated, with a PR-time `openscad-lsp-arm64` check, #199) was dropped; the
  Dockerfile still handles `TARGETARCH=arm64`, so re-enabling it is `platforms` plus
  `docker/setup-qemu-action`.

- Go (#2686): `ci.yml`'s `go-select` (`.github/scripts/select-go.sh`, tested by
  `select-go.test.sh`) decides whether a change touched Go, and the `go` job always
  runs `go-module-ci.yml` with that answer (gofmt over the tree, then vet, build and
  `test -race` per module, the client's module-mode vendor build, the `go work vendor`
  drift check, and each CLI image built and run once). Both are in `CI Summary`'s
  `needs`, so `go` is required: a skip is a failure, a "no Go changes" run a pass. The
  `freshness` job also regenerates the Go client when the API or the client changed and
  pushes it to a same-repo PR with the `RUNNER_APP` token.
- Python and Rust lanes: `layout` reads the change's paths (`.github/scripts/changed-paths.sh`,
  shared with `go-select`) and `select-lanes.sh` (tested by `select-lanes.test.sh`) picks
  the lanes: `backend` (`lfs-fixtures`, `backend-pytest`), `agent_durable`, `rust`
  (`rust`, `slicer-image`) and `image` (the app image job: any backend change, plus
  `frontend/` and the Go trees its live mount runs). A lane job skips on a change that
  does not need it; `CI Summary` re-derives the lanes from its own checkout, so a skip
  where the lane was needed is a failure. A push, the schedule, a dispatch, the
  Dockerfile, `ci.yml` or the selector selects every lane. A new cross-tree read (a test
  that opens a file outside its tree) needs its path in that lane's pattern.
- `build-go-image.yml` publishes each `tools/go/*` CLI (every one with a `main.go`
  must have a Dockerfile) as `ghcr.io/eh-homelab/scadbuddy-<module>` on a push to main
  touching Go, a `v*` tag or a dispatch, for amd64 only: `sha-<7>` always,
  `latest` only on a push to main, the version on a tag; buildx `type=gha` cache
  `scope=go-<module>`, `mode=min`. The package must be public, as the app image's.
- `vendor-autofix.yml` (`pull_request_target`, Dependabot's own PRs on this repo only)
  regenerates the root and client `vendor/` after a gomod bump and pushes the commit with
  the `RUNNER_APP` token, which a GITHUB_TOKEN push could not do (it starts no CI); no
  step that runs `go` holds a token. A gomod PR without it fails the `go` job's drift
  check. Once it pushes, Dependabot stops rebasing that PR (`@dependabot recreate`
  starts over).
- The `rust` job pins `dtolnay/rust-toolchain` by commit (the toolchain itself is
  `rust-toolchain.toml`'s). The action has no release tags, so Dependabot never moves
  that SHA: `rust-toolchain-pin.yml` runs `.github/scripts/rust-toolchain-pin.sh --check`
  weekly over every workflow's pin, failing (the reminder) when master has moved past
  one, naming each line to change (#4099, #4264). Move it, and its `master as of` date, in a reviewed PR.

## PR conventions

- Conventional-commit titles (`feat(scope):`, `fix(scope):`, `docs:`, `ci:` …);
  Release Drafter labels and groups PRs by title. Body links the issue: `Fixes #N`.
- Agent-authored changes and docs cite their sources (#259): every external fact gets
  a link, every measured fact says where and how it was measured (spec §3).
  `lint-plugin.sh` checks it for plugin skills only; `link-check.yml` checks weekly that
  the links in `docs/`, `README.md`, this file and every `SKILL.md` still resolve.
- Required checks on `main`: **`CI Summary`** and **`claude-review`** (the ruleset
  lives in eh-homelab/clusters, so renaming either job breaks the gate silently).
- `claude-review` is a merge gate: the review runs after CI and sorts its findings into
  `## Blocking` and `## Non-blocking`; a classifier passes only when every Blocking
  finding in the review for *this* commit is fixed or tracked in an open `pr-feedback`
  issue for the PR. Non-blocking findings never gate. A person adding the
  `claude-make-follow-up-issues` label to the PR files every outstanding finding as
  `pr-feedback` issues, Non-blocking ones in groups of their own titled `(non-blocking)`;
  the label the review applies itself to a PR merged with Blocking findings outstanding
  (a `Bot` sender) files the Blocking ones only.
- When claude-code-action's workflow-validation guard skips the review (the PR's
  `claude-code-review.yml` differs from `main`'s), the gate passes **only if the PR
  itself edits that file**. A PR merely branched before `main` changed it fails closed
  (#487): merge `main` and re-dispatch the review.
- Never commit `backend/openapi.json` or either `schema.d.ts`. An API change shows up
  as the `freshness` job's diff comment on the PR, not in the PR's own diff.

## Known flakes

- Frontend vitest tests can time out when the machine is under heavy load (e.g.
  several builds or agents at once). Re-run before debugging; a failure that repeats
  on an idle machine is real.
