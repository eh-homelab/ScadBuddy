# Backend — notes for agents

These notes cover `backend/`; paths are repo-relative; the root CLAUDE.md has the repo-wide rules (migrations, CI, PR conventions, OpenSCAD facts).

- Migrations: see root "Migrations (#491)". Generated API files: see root "Generated API files".
- Real `openscad` only in the image, and the verified OpenSCAD facts: root "Verified OpenSCAD facts".
- The frontend side of orders and activity: frontend/CLAUDE.md. The agent's side (tools): agent/CLAUDE.md.
- The native hot path's real-stack e2e (the recorder pass): e2e-stack/CLAUDE.md. The slicer worker: workers/rust/CLAUDE.md.

## Database access

The rule is in the root CLAUDE.md, "Database access (#5295)". Today the API connects as the
database owner and runs SQL on the agent's `ai_*` tables. The print worker connects as a
role of its own, with the grant list in `auth/caller_grants.py` (`PRINT_GRANTS`, and
`AGENT_GRANTS` for the agent). The projects and render workers have no database client
(#5298): they reach their data through CRUD routes. The projects worker has no grant
list, and the API and the agent revoke what its old role `scadbuddy_projects` held at
every start (`auth/revoke.py`, `agent/src/db/grants.ts` `revokeRole`); `RENDER_GRANTS`
(`render/worker_role.py`) is empty until #5298 Task 22 revokes its role. The render
worker also holds no KEK and runs only on a remote store (plan D11). Template `pipeline/`
Python runs in the render worker's pod, and openscad and `openscad-lsp` run in the API's
pod.

Under the rule the API becomes the CRUD service: the only process with SQL access, and
nothing untrusted in its pod. Each worker becomes an HTTP client of it, with a service
credential, and loses its database secret, role and grant list (#5298). The `ai_*` reads
become routes (#5299). Every `/api/v1/internal/*` route requires a service credential, and
the API moves to `enforce` (#5297). Template Python and openscad move to a pod without a
database secret (#5296). Never add a worker table grant, a cross-service table read or a
new database client; add a CRUD route that requires a service credential instead.

A caller gets its service credential from `auth/service_credentials.py` `token_source`: with
`SCADBUDDY_SERVICE_TOKEN_FILE` or `SCADBUDDY_SERVICE_BOOTSTRAP_FILE` it exchanges that proof
at `POST /api/v1/auth/service-token` (`ExchangeReader`; route `api/service_token.py`, class
`exchange`; verifiers `auth/exchange.py`: TokenReview, bootstrap hashes) for a 15-minute
credential, falling back to its view on a 404 while it has a database URL; otherwise it
reads its sealed one from its view (`TokenReader`), as before (#5494). A caller with no database
(the projects and render workers, #5298) takes `exchange_source` instead: the exchange only, with no
view, and no proof refuses its start. `ExchangeReader`, `exchange_source` and `ServiceAuth`
live in `auth/service_auth.py`, which imports no Postgres code (`service_credentials.py`
re-exports them), so the storefront edge (`python -m scadbuddy.storefront`, #5282; it holds
no database secret and refuses to start with one) can use them. The API's own in-process callers use
`InProcessProvider` (`proof = inprocess`). A new caller-side client takes a `token_source`,
never a `TokenReader` of its own.

## Commands

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
(`TEMPORAL_CLI_VERSION`, `TEMPORAL_CLI_SHA256_AMD64`, with the arm64 digest and its REST
API asset id `TEMPORAL_CLI_ASSET_ARM64`, the arm64 fallback's URL, #5706), in `ci.yml`'s
two "Install the Temporal CLI" steps; move all three together.
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
  backend's migrations), `worker_role.py` (#601: `RENDER_GRANTS`, now empty, which the
  API grants the unused role `scadbuddy_render` at start until #5298 Task 22 revokes it,
  and `grant_role`, which every caller's list goes through; the render worker holds no
  database, #5298, `tests/test_render_worker_nodb.py`: it reads the store settings and
  writes its job rows, their events and activity rows, their blob refs, the piece geometry, the blob index, its uploads' `assets` rows (`library/assets.py` `RemoteAssetStore`, the files on its own volume) and its library leases, pin holds and install slots (`RemoteCheckoutLeases`, `RemoteInstallSlots`, under the API's `REMOVAL_LOCK`) only through the API's CRUD routes, `crud/render.py`, where the API also finds the Bambuddy store's folders for it). The legacy in-process queue, its file and Postgres stores
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
  event bus to `FlowTriggerFire`, one per trigger and event, an order event one per
  trigger and status change, `_change_fires`); the workflow and the
  Schedules are `workflows/flow_triggers.py`, routes `api/flow_triggers.py`. A fire reads
  everything from the trigger's row (the Schedule carries the trigger id only) and runs
  as its creator, with a run grant only from a standing grant a person approved; never
  take an author or a grant status from a body. A trigger pins a version; any edit
  returns its standing grant to requested (the one exception: the built-in flow's move,
  `orders/builtin_flow.py` `move_triggers`, #4673), and an approval names the state it saw
  (`seen`); a fire is admitted, and a standing grant spent, under row locks.
  The `orders` bound (#4252, orders spec §7.2): `GrantRule.orders` (ids, or `"any"`)
  bounds `queue_print` and `tool` by the order the call names (`CallFacts.order_id`: an
  order tool's `order_id`, a line item print's source); a run grant seeded for an order's
  event is `narrowed_to_order` (each rule keeps its index, which the standing grant's
  counters are kept by). `queue_print`'s source `{"order_id", "order_item_id"}` posts to
  `order_item_print` with the call's key and follows the attempt's run; a refusal before
  it starts is the outcome `refused` with `problem` (`requote_needed`), and
  `RouteRefusedError.problem` carries a route's problem `type`.
  Order events (#4252, orders spec §7.1): the kinds `order.changed` and
  `order_item.changed` fire once per row of `order_status_changes` (kept by a trigger on
  `orders` and `order_items`; keys `order:<id>:<status>:<change>` and
  `order_item:<id>:<status>:<change>`), never per event: an event reads the newest
  `CHANGES_HEARD` rows of its order or item (`events.py` `_change_fires`), so a plain edit,
  a hold or a release fires nothing, a return to a status fires again, and a change before
  the trigger's `live_since` (made, its event last edited, or last resumed) or already fired is
  skipped. An item
  attempt recorded with no status change fires nothing. `EventTiming.subject` `{kind: template|order|order_item, id?}` (a stored
  or sent `slug` reads as a template subject; no migration) and `match` (`status` only,
  values checked against `OrderStatus`/`ItemStatus`), both applied in
  `TriggerStore.event_triggers` before a fire starts; an order fire's `customer_id` is
  read from the order's row (`order_facts`), and an item event for an item gone since
  fires nothing. `trigger()` returns `OrderChanged` /
  `OrderItemChanged`.
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
  `start.py` (`start_native_slice`: one running execution per copy, subject, copy sha256
  and presets, which the dialog and the run share; the dialog's Reslice gives a
  completed slice a new row of its own, `reslice_of`, #4433; the lost-row repair), `preview.py` (the
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
  `bambuddy`) and the printer row's resolved path; there is no stored switch of its own.
  One request may choose (#5352, print-flow spec §2 "As built: the request picks the
  path"): `PrintRunRequest.path` `bambuddy` is Bambuddy's queue whatever the row says
  (`run_route`/`ensured_route` `asked`, no LAN report read; the printer must link
  Bambuddy), and `native` is refused when the run would not go over the LAN
  (`LAN_PATH_REFUSAL`). Every reader that takes the request passes it: the dialog's
  checks (`dialog_route` `asked`), the background and quote slices (`slice_route`
  `asked`, `SliceQuoteRequest.path`) and the analyzers (`AnalysisRequest.path`). A
  native run refuses a request whose own `options` ask a calibration value the LAN start
  cannot honour (`options.lan_calibration_refusal`, 422): it sends `flow_cali` false and no
  field for the other three, so `flow_cali` `"off"` and `bed_levelling`/`nozzle_offset_cali`
  `"auto"` pass (`LAN_CALIBRATIONS`), and any `vibration_cali` is refused. Its type is
  `LAN_CALIBRATION_PROBLEM`, with `bambuddy_linked`; for a caller that is not the page
  (`api/printing.py` `asked_by_api`; an order print's operation records it as `api`), on
  a row that links a Bambuddy printer, the print route adds `LAN_CALIBRATION_API_HINT`,
  which names `path` (#5602). An order print
  with `path` `bambuddy` of a quote sliced over the LAN slices again (orders spec §5.4). A
  native run passes `client=None` (`bambuddy/client.py` `maybe_client`), reads printer
  state from the LAN report (`printers/reports.py`), and its filaments are the loaded
  trays plus the physical-spool overlay of cached provider records (`spools/`, refreshed
  off the path): Bambuddy's inventory and ScadBuddy's own (`native_spools`,
  `spools/native.py`, provider `native` in `spools/spool_providers.py`, routes
  `api/native_spools.py`, #4643; each write re-caches it at once; a LAN start saves its
  slots' grams in `native_spool_uses`, taken off once the run settles with that start
  `done`, `native_use_hook`; the editor is Settings' `components/settings/NativeSpools.tsx`).
  `printers/ams_mapping.py` computes `ams_mapping`/`ams_mapping2` from the
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
  first read and its analysis, and the Prints page ask Bambuddy nothing on these
  backends (#4601, #4629); the Prints page reads Bambuddy's queue and history only after
  its "Show Bambuddy's queue and print history" button (#3745).
  A LAN print's records (#4644, #4543): `print_succeed` writes its `output_last_prints`
  row (`print_route` `lan`), and `lan_print` saves its rack picks keyed by run and
  `subtask_name` (`rack_nozzle_*`'s `lan_run_id`, `lan_subtask`); the follow's read hooks
  (`Follower.on_read`: `printers/progress.py` `lan_last_print`, `rack/usage.py`
  `lan_settle_hook`) move the row and settle the picks, `lan_wait` each earlier copy's;
  first, the printer's watch settles each start's picks as its report ends it, timed to
  that report (`printers/reports.py` `advance_print`, #5138, #5142). A LAN print keeps
  `output_last_prints.sent_to_bambuddy`, so the output still says "Send again" (#5143).
  A run of several plates or copies is its own queue (#4541, `workflows/printing.py`
  `_print_lan`): it succeeds at its first start, then `lan_wait` (`printers/print_activities.py`,
  over the watch's `printer_reports`) waits out each print before the next starts, and
  `print_lan_update` rewrites the result (`PrintRunResult.lan_waiting`, `lan_stopped`).
  A change to what a native run reads must keep `e2e-stack/`'s recorder pass at zero.
  Copies between the two libraries (#5355): `print_library/copy.py` (the plan's walk,
  the batch, the ``library`` queue activities), `copy_models.py`, `copy_store.py`
  (`library_copies`, `library_copy_files`), `workflows/library_copy.py` (`LibraryCopy`,
  AUTO_UPGRADE, one execution per distinct request: `copy_id`; its histories replay from
  `tests/fixtures/library_copy_histories/`, so a change goes behind `workflow.patched`), routes
  `api/library_copy.py` (`POST`/`GET /print/library-copies`, and `/print/bambuddy-library`,
  Bambuddy's library browsed as a source on `native`). A file whose SHA-256 the target
  holds is skipped (Bambuddy's `file_hash` and the native rows' `sha256` compare
  directly), each selected folder's path is kept, nothing at the target is deleted or
  overwritten. The bytes go into Bambuddy through `send.upload_raw`, the one raw upload
  path (`ensure_raw_copy` uses it too), and into the native library through
  `NativeLibrary.upload` (`StoreFullError` is a failed row naming the 507). Each file
  counts its own tries (`attempts`, limit `MAX_FILE_TRIES`: every try, whether the
  library answered, did not answer, answered 429 or the try never ended) and, apart, its
  5xx answers (`server_errors`, 507 aside, limit `FILE_ATTEMPTS`); any other answer fails
  the file at once, except a 401 or 403, which fails the whole copy (`CopyRefusedError`).
  A session advisory lock per target library and SHA-256 (`TargetLocks`) keeps two
  copies from putting the same bytes twice, and the status route matches the execution by the
  row's `run_id`; a row with no run id falls back to `close_time >= started_at +
  NO_RUN_ID_MARGIN` (5 min) (#5424). Agent
  `copy_library_files` and `get_library_copy` (`agent/src/tools/libraryCopy.ts`);
  `list_library`'s `source: bambuddy`; frontend `components/library/LibraryCopyDialog.tsx`.
  A native run of several prints starts the later ones in `PrintLanSequence`
  (`print-lan-<run id>`, `workflows/printing.py` `LanSequenceWorkflow`); its controls
  (#4942, #4939) are `POST /print/runs/{id}/stop` (cancels the sequence, which records
  `lan_stopped`) and `…/plate-cleared` (the `plate_cleared` signal, for a run planned with
  the stored setting `lan_require_plate_clear`, behind `PLATE_CLEAR_PATCH`), shown from
  `PrintProgress.lan_run` and `PrinterNow.lan_run` by `components/print/LanRunControls.tsx`,
  and the agent's `stop_print_run` and `confirm_plate_clear`.
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
  pinned workflows on SIGTERM. No database and no KEK (#5298, `NoDatabaseSettings`): it
  waits for its CRUD areas (`RENDER_CRUD_AREAS`), gets its credential from
  `exchange_source`, and refuses the local store (`LocalStoreRefusedError`, plan D11). `run_inprocess_worker` is the API's
  `SCADBUDDY_TEMPORAL_WORKER_INPROCESS` mode (no drain). `--queue bambuddy`
  (`run_print_worker`, #1060) is the print worker, deployment `scadbuddy-print`: no data
  volume, Postgres and `SCADBUDDY_API_INTERNAL_URL` only (`build_print_deps`); its
  settings, the Bambuddy key among them, come from the API's CRUD route
  (`crud/print.py` `RemoteSettingsStore`, a `SettingsReader`; route `api/crud_print.py`,
  every sealed field but `PRINT_SECRETS` null, #5298), never from its own SQL; its print
  runs, their outcomes, its operations and the events it publishes go through the same
  routes (`RemotePrintRunStore`, `RemoteOutcomes`, `crud/operations.py`
  `RemoteOperationStore`, `RemoteEvents`; the API writes each with its event and
  activity row, and logs a posted event once by its id, of `EVENT_KINDS` only), and it
  does not LISTEN, so it keeps no `RecentRuns`; every
  workflow on it AUTO_UPGRADE (`print_deployment`'s default, #2066; `FollowPrint` names
  it itself as `VersionedFollowPrint`, since an unversioned worker refuses a versioning
  behavior), so an `Operation` moves to the current build, which #4139's deferral
  needs. The API serves `bambuddy` itself, unversioned, only with
  `SCADBUDDY_TEMPORAL_WORKER_INPROCESS` or `SCADBUDDY_TEMPORAL_PRINT_WORKER_INPROCESS`.
  `--queue projects` (`run_projects_worker`, #1057) serves flow runs, deployment
  `scadbuddy-projects` (#4146; `/readyz` once the queue routes to it, as the print
  worker's): no database (#5298), the KEK, and a credential exchanged for
  `SCADBUDDY_SERVICE_TOKEN_FILE` or `SCADBUDDY_SERVICE_BOOTSTRAP_FILE`; it refuses to
  start without the KEK or a proof, and ignores `SCADBUDDY_DATABASE_URL` with a warning
  (`core/settings.py` `NoDatabaseSettings`).
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
  it, never on the per-process `print.settled`. Each run is settled on its own (#5613,
  orders spec §3.3): the subject's one follow also reads each older open run of the
  subject by that run (`Follower.read_run`, `progress.py` `run_reader`: a library file's
  run by its own sends, a LAN run by its printer's report; `PrintOutcomeStore.older_open`,
  within `OPEN_RUN_WINDOW`) and records it with `record_run`, and ends only once none is
  open. This lives in the activity: `FollowPrint`'s commands are unchanged, and its
  histories replay from `tests/fixtures/follow_print_histories/`, so a change to them
  goes behind `workflow.patched`.
- `backend/scadbuddy/core/gitrepo.py` — `GitRepo`, the hermetic git core (#2503) under
  model history (`library/history.py` `ModelHistory`) and flow scripts
  (`flows/repository.py` `FlowHistory`: `data/flows/<name>/flow.py`, one commit per
  version, each with an optional message and body; `<name>/flow.json` is the flow's
  display name and description, each change a commit of its own that no version or
  restore changes, except that restoring a deleted flow brings back the ones it had at
  the restored commit, in the version's own commit, `PATCH /workflows/names/{name}`, #3186; `flows/reconcile.py` indexes a commit whose row was never written;
  `flows/history.py` is the Reset preview, not git). Both repositories are shared by
  every replica over NFS, so each writer holds a Postgres lock that carries the last HEAD
  (`library/repo_lock.py`: `models_repo_head`, and `flows_coordinator`'s `flows_repo_head`).
  The off-box copy (#4974) is `git_remote/` (`push.py` `RemotePush`, `store.py`
  `git_remote_pushes`): `GitRepo.after_commit` asks for a push on a thread once the write
  lock is released, the `housekeeping_push_git_remote` sweep (prune Schedule too) retries,
  `GET /settings/git-remote` is the lag. `GitRepo.push` never forces, allows only the URL's
  protocol and hands the token (`git_remote_token`, a sealed secret; a save needs the KEK)
  to git through `CREDENTIAL_HELPER` from the child's env, never the argv.
- `backend/scadbuddy/library/` — catalogue, outputs, git-backed model history
  (`history.py`), fonts (`fonts.py`, `googlefonts.py`), per-template presets
  (`presets.py`: saved ones in Postgres, the `saved_presets` table (#332), outside git so
  a save never moves a template's revision; a template's own read-only ones in the `presets` list of its
  `model.json`, with a legacy `presets.json` still read), uploads for `// file`
  parameters (`assets.py`: the bytes under `data/assets/`, the metadata, last use and
  usage in the `assets` table (#591); a blob with no row is an orphan the sweep removes;
  a render worker's `RemoteAssetStore` keeps its copies and reads the rows through CRUD routes, #5298).
- `backend/scadbuddy/editor/` — the language server's guards (#95): `nonet.py`, the
  launcher every openscad-lsp start goes through (`nonet.command`; a seccomp filter, no
  network sockets), and `component.py`, the per-client session cap (on only with
  `SCADBUDDY_TRUSTED_PROXIES` set). The bridge itself is `library/lsp.py`.
- The storefront's bot check (#5283, storefront spec §5.8): Cloudflare Turnstile, on while
  `storefront_turnstile_site_key` is set. Only the CRUD service holds the secret
  (`storefront_turnstile_secret`, a `SECRET_FIELDS` member; a save needs the KEK, as
  `git_remote_token`'s does), and only it calls Siteverify (`orders/storefront/turnstile.py`
  `verify`; the Cloudflare facts are its docstring). The edge's `storefront/turnstile.py`
  `require_turnstile(request, action)` forwards the page's `X-Turnstile-Token` to `POST
  …/crud/storefront/turnstile/verify` (area `turnstile`) and refuses 403
  `bot_check_failed` or 503 `bot_check_unavailable`. It fails closed: a site key with no
  secret this process can open is `unavailable`, never a pass. A client (IPv4
  address, IPv6 /64) with `FAILED_CHECKS` (10) failed checks in 10 minutes, or any client
  of a prefix (IPv4 /24, IPv6 /48) with 100, is refused with no Siteverify call
  (`FailedChecks`; it never evicts a block, and when full of blocks it checks new clients
  without tracking them, never refusing them). A send with no token is refused but not
  counted: it costs no Siteverify call. A
  design request checks its upload allowance first, and only a passed check spends it.
  The edge asks for the verdict once, never retried, with 15 s to answer; no answer is
  503 and never counts as a failed check. While the edge's `SiteKey` read no key within
  `SITE_KEY_TTL` (30 s), the check passes with no call, so a newly set key takes effect
  within 30 s. A form route awaits it
  before it reads its body. Today only design requests (`request`) take it; the actions
  `submit`, `sign_up` and `sign_in` are kept for the submit, code request and sign-in
  routes still to come. The edge's page CSP (`storefront/static.py`) names
  `challenges.cloudflare.com` in `script-src` and `frame-src` only while a site key is set,
  and until the edge's first site-key read succeeds (`SiteKey.ever_read`).
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
- Postgres pools (#5226): every one is made by `core/pg_pool.py` (`row_pool`,
  `async_pool`: `min_size` 0, idle connections closed after a minute, the pool's name
  as `application_name`), never a `ConnectionPool(...)` of a feature's own. A feature's
  short statements on the API's role go on the shared `scadbuddy-rows` pool
  (`rows/component.py` `ROWS`). `tests/test_pg_pools.py` sums an API process's pools
  and fails on any new or bigger one; change it, and README "Running it", on purpose.
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
  both read everything, as the browser does. `/activity` with `subject` repeated (at most
  `MAX_SUBJECTS`, 200) reads each subject's newest `limit` rows in one query
  (`ActivityIndex.list_per_subject`, a keyset walk per subject of the expression index
  `activity_subject_key`), with `subject_cursors` to page one subject (#5596). The `housekeeping_activity` sweep (last in
  `SWEEPS`, behind `workflow.patched("housekeeping-activity")`) repairs, then prunes, on the
  prune Schedule (settings `activity_ttl_seconds`, `activity_repair_window_seconds`,
  `activity_follow`). Table size (#5599): the prune (`ActivityIndex._prune`) deletes a row
  `activity_ttl_seconds` after its `finished_at` (default `ACTIVITY_TTL_DEFAULT`, 30 days;
  the setting is at least one day, `library/settings_store.py`) and a placeholder after
  the repair window (default one day), every `PRUNE_INTERVAL` (300 s,
  `workflows/housekeeping.py` `PRUNE_SWEEPS`). So the table holds about 30 days of
  finished jobs plus the jobs still running: one row per job, never one per event. An
  operator who raises the TTL makes the table grow by the same factor. The migration
  `20261107T0625Z_activity_subject_key.sql` builds its index with a plain `CREATE INDEX`.
  That takes a `SHARE` lock: reads go on, but every insert, update and delete of
  `activity` waits until the index is built
  ([PostgreSQL docs](https://www.postgresql.org/docs/18/sql-createindex.html#SQL-CREATEINDEX-CONCURRENTLY)).
  `render/pg_store.py` `migrate` runs all pending files in one transaction, so the lock
  holds until they all commit, and `CONCURRENTLY` cannot run there. On a table
  bounded to 30 days the build is short. A new index on `activity` with a much larger
  TTL needs another plan; never edit a merged migration. The agent: `src/activity/kinds.ts` (its kinds) and `tools/activity.ts`
  (`activity_list`, `activity_get`, `activity_watch`), the tier boundary: a caller without
  `outward` sees only its own principal's. Frontend: `pages/ActivityPage.tsx`,
  `components/activity/` (table, filters, drawer, `ActivityBell`) and
  `mocks/features/activity.ts`.
- `backend/scadbuddy/orders/` — orders phase 1 (#4249, epic #4248, spec
  `2026-10-18-orders-design.md`, plan `2026-10-19-orders-phase-1-customers-and-orders.md`):
  customers, orders and line items as ScadBuddy's own records. `component.py` `ORDERS`
  (`OrdersComponent`: `store` over the shared `scadbuddy-rows` pool (`rows/component.py`),
  opened on first use, `lifecycle` over the core's bus; `OrdersDep`), `models.py` (the wire and row shapes; money is integer minor
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
  topics `orders` and `order:<id>`; a frame for a cart the draft sweep deleted also
  carries `deleted: true` (and `cart`, a flag flow triggers read, marks a storefront cart). Every item's `inputs` join `referenced_asset_ids`, and
  `main.sweep_assets` skips the pass when they cannot be read. Routes `api/customers.py`,
  `api/orders.py` (plain reads and writes; the slice quote is phase 2). Agent tools
  `agent/src/tools/orders.ts` (`read` lists and gets, `write` records the shop keeps,
  `outward` what commits it to the customer: accept, a quote's acceptance, fulfil, ship,
  close, cancel, a deposit, shipping without full payment). Frontend: `pages/OrdersPage.tsx`, `OrderPage.tsx`, `CustomersPage.tsx`,
  `CustomerPage.tsx` over `components/records/` (`DataTable`, `FilterBar`, `RecordDrawer`,
  extracted from the Activity components, which wrap them) and `components/orders/`; the
  Customize page's "Add to order…" and an output's "Order this" (`AddToOrderDialog`); mocks
  `src/mocks/features/orders/`; `lib/ordersQuery.ts` keeps the list's filters in the URL.
  Phase 4, flows (#4252): `builtin_flow.py` is the built-in `order-fulfilment` flow (its
  script, inputs `notify`, `send` and `settle_hours`), registered by
  `ensure_builtin_flow` at the component's start when the name never had a version
  (`FlowDefinitions.register_new`, `FlowExistsError`), and as a new version when the newest
  is still an unedited built-in one with another script (#4673, `_upgraded`:
  `FlowDefinitions.upgrade_builtin`, which writes only while the newest is the one it
  read, `expect_head`, and never over a delete), so a person's edit or delete stands.
  A trigger pins its version, so `move_triggers` then moves each trigger of an older
  built-in version to the newest (`TriggerStore.move_builtin`, under the row lock, its
  approved grant kept) only when the grant is in force and admits, by function and tool,
  every call the new script makes that its version did not (`script_calls`; `agent(`
  never); any other trigger gets `NEWER_NOTICE` in `flow_triggers.notice`, which an edit of
  its version clears, and only while it still runs the version read; it runs again after a
  person approves a built-in trigger's grant (`move_after_approval`);
  `SCADBUDDY_ORDERS_BUILTIN_FLOW` (a bootstrap field, on by default, off in the tests'
  settings) switches it. It prints each approved or failed item through `queue_print`
  with an `order_item_id` source; a refusal (`order_on_hold`, `requote_needed`, any
  other) or print choices it cannot read (no output, or one gone) asks a person to fix it and answer `print`, and an unanswered or skipped item
  is passed over, never the run's end. It never re-quotes: on an accepted order a new
  quote opens only after a person's revision after acceptance. A print run ends once
  its plates are queued, so it then reads `order_get` (`sleep` between reads, from
  `POLL_S` doubling to `POLL_MAX_S`, so a run's history stays bounded: it has no
  continue-as-new) until no printed item is `queued` or `printing`, for at most the
  input `settle_hours` (default 48; past it the run ends with a summary and no check);
  an item a failed print left `queued` anyway counts as printed. Only then does it ask
  the agent to record the attempts under a grant kept to the order, and with `notify`,
  once the re-read order is `ready`, call `order_notify_customer` (`read`: it composes the text and sends nothing),
  and with `send` too (#5660) and the answer's `send_ready`, `order_notify_customer_send` (`outward`,
  so the trigger's grant must name it); the result's `send` says `sent`, `skipped`, `refused` (the agent's refusal only),
  `unknown` (a failure that may have sent it) or `not_sent`, and none ends the run. `ensure_builtin_flow` runs as a task beside the
  component's start; it reads the versions first (a name with any registers nothing,
  unless the newest is an unedited older built-in), then runs `check_script` and registers nothing that fails it, and the version's commit names the built-in
  (`ScadBuddy-Flow-Builtin`), so the versions list and reconcile read its author as the
  build (`flows/reconcile.py` `created_by`).
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
  it (`Lifecycle.record_slice_quote`). The quote records its print setup (#4673, order operations spec §5): `choices`
  (`quote.py` `print_setup`: printer, path, `PrintChoices`, and the `prime_tower`, `supports` and
  `print_sequence` the body takes in `PrintRunRequest`'s shapes, which `operations.py`
  `preview_request` slices with) and `filament_plan`, null on a manual or archive quote.
  An item's print (`ItemPrintRequest`, `{}` included) takes every part of that setup and
  the spools it omits from the quote's (`printing.py` `QUOTED_SETUP`, `quoted_setup`;
  `operations.py` `filled`); one sent that differs is `requote_needed` with `reason`
  `setup_differs` and `differs` (`setup_differences`: not `path`, a printer name left
  null, or the spools; the dialog adds its hint only then), a quote's `native` path taken
  on a printer no longer on the LAN is `requote_needed` naming that
  (`quoted_path_refusal`), and a quote
  with no setup needs `choices` and `filament_plan` sent (422 `print_setup_missing`). The
  quote route's body (`ItemSliceQuoteRequest`) also takes `inputs` and `commit`: a design
  that differs from the item's is made a revision first (`api/orders.py` `_slice_revision`,
  through `Lifecycle.add_revision`, after acceptance for an accepted item on a
  `TAKES_ITEMS` order) in `run_operation`'s `before_start`, after the quote's check of
  the body alone (`quote_check` `body_only`: the filaments named, never the new design's
  slots, which the run judges after the render with the support slots, so a 422 writes
  nothing); the render's slots
  take the item's earlier filaments (`carried_filaments`, unkeyed), and a slot the new
  design adds that neither the item nor the body covers is refused after the revision is
  written; the quote is of it (`revision_id` in the operation's request;
  409 `revision_superseded` once another is current, the record's locked check
  included; a cart is refused `order_not_submitted` first). The page (#4673 PR 4): the
  row's "Slice…" and the drawer's print setups (`QuoteEditor`, `printSetups.ts`,
  `DesignInputs.tsx` for the inputs), each plate's image from the quote's sliced file
  (`GET …/quotes/{quote}/plates/{plate}/thumbnail`, `quote.py` `plate_image`). The lifecycle drops the quote holders §3.4 releases
  after each commit, and `housekeeping_reap_quote_holds` (both Schedules, behind its patch)
  drops any holder older than `QUOTE_TIMEOUT` that no standing quote names. `GET
  /orders/estimate` is the rough estimate from every plate's part volumes, never a record.
  The agent's `order_item_quote` takes `manual` or `slice`, beside `price_book_get`,
  `price_book_set` and `order_estimate`. Frontend: the Settings page's Orders section
  (`components/orders/FilamentsEditor.tsx`), `PrintPicker`'s `quote` mode with
  `QuoteFilaments` ("Slice…" in `QuoteEditor`), and the estimate in
  `AddToOrderDialog`; mocks `src/mocks/features/orders/priceBook.ts`.
  Attachments (#4251, spec §6.2 "As built"): any file on an order, a line item or a print attempt,
  `order_attachments` (subject `order:<id>` / `order_item:<id>` / `order_item_fulfilment:<id>`, an attempt's file listed
  under its item through `attempt_item_id`; `POST …/fulfilments/{f}/attachments`; typed by its bytes in
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
  times its quantity (`orders/totals.py` `item_price`, the browser's `components/orders/price.ts`),
  with `pending_minor` and `unpriced_items`; accepting still needs every quote accepted.
  With the order and its payment requests (`order_totals(..., order=, requests=)`, as `GET
  /orders/{id}` reads them) it also gives `deposit_due_minor` (the deposit less what is
  paid, 0 with none or a waived one) and `shipping_held` (`balance_unpaid`'s rule,
  `totals.balance_due` over `has_standing_balance`); `totals_of`, the lifecycle's read,
  leaves them at 0 and False.
  Deposits, the balance, auto tax and shipping options (#5281, storefront spec §8-§9,
  plan `2026-11-07-storefront-phase-2-payments-deposits.md`, migration
  `…_order_deposits_tax.sql`): `orders.deposit_minor`, `.deposit_waived_reason` and
  `.balance_waived_reason`; `order_charges.auto` (the one tax row ScadBuddy computes,
  `Lifecycle._retax`) and `.shipping_option_id`. `set_deposit` and `apply_deposit_policy`
  (`PUT /orders/{id}/deposit`, `POST …/deposit/policy`, gap 7; a policy rule skipped
  for the order's currency is named as `skipped` on the `deposit.set` row, and only such
  rules is 409 `deposit_policy_currency`) work while `draft` or
  `quoted`: they cancel the open and failed `deposit` requests and open one for the
  deposit less what is paid on the default provider (gap 4); a pending one refuses them
  (`payment_in_flight`). Until the deposit is paid (gap 3) or waived (`POST
  …/deposit/waive`, a reason of 1 to 500 characters), a revision is not sent and a test
  print is not claimed (`deposit_unpaid`). `accept_order` leaves an order that asked for
  money (a deposit, or a request not cancelled) with one open `balance` request for the
  rest, cancelling its other open or failed requests (gap 1), and is refused
  `payment_in_flight` while one is pending. While a `balance` request stands, the balance
  is above zero and no one waived it (`POST …/balance/waive`, once accepted, else
  `order_not_accepted`), a shipment, or a shipment edit that raises a quantity, is
  `balance_unpaid` (gap 2). The routes that cancel requests (accept, a deposit set or
  applied, an order's cancel, gap 5) run through `Payments.run_closing`, which closes each
  cancelled request's provider page after the commit and never a kept one's; accept and
  the two deposit calls then call `link_pending` to link the new request (an order's
  cancel opens none and does not). A storefront cart refuses (`order_not_submitted`) a
  deposit set or applied, both waivers and accept, and refuses its shipping option and its
  charges (`PATCH` with `charges`) to anyone but the visitor (`check_visitors_cart`): the
  back office never acts on a visitor's cart. The `housekeeping_sweep_storefront_drafts`
  sweep (both Schedules, behind its patch) deletes a cart idle past
  `storefront_draft_ttl_days` (`Lifecycle.sweep_drafts`: neither the order nor its visitor
  seen since; one transaction per cart, `FOR UPDATE SKIP LOCKED`, checked again under the
  order's and the visitor's locks (`claim_idle_draft`) before its attachments go through
  `delete_attachment`, then the order), then the visitors idle as long with no order and
  the `storefront_renders` rows past a day (`VisitorStore.sweep`), 200 of each a run. A cancelled order reopened past `quoted`
  that had asked for money gets its balance request again (`_open_balance(reopened=True)`),
  and an accepted order whose deposit paid the whole total opens one once its balance is
  above zero (`_regate_balance`, from `_retax`; never after the shop cancelled the balance
  request, and it cancels nothing). Every route whose lifecycle call reaches `_retax`
  links such a request after the commit, and only the requests that call opened, so an
  older one whose link failed waits for `POST …/link` (`Payments.run_linking` under
  `api/orders.py` `_run_linked` and the shipping-option route; `request_ids` and
  `link_opened` around the archive quote): adding items, an
  item's edit, delete, cancel and reopen, a revision and its answer, a manual or archive
  quote and its acceptance, a duplicate, the charges, and the order's reopen. The slice
  quote's run (an operation) does not: a request it opens is linked by the next linked
  call or `POST …/payment-requests/{id}/link`. A request's cancel takes an optional
  `reason` (1 to 500 characters), kept on its `payment_request.cancelled` audit row:
  cancelling the balance request lifts `balance_unpaid`, so the reason says why. The tax
  row is `orders_tax_percent` of the items plus, with `orders_tax_shipping`, the shipping
  charges, rounded half up; a typed `tax` charge removes it (gap 6). A charges replace
  (`PATCH` and `…/charges/after-acceptance`) writes the typed charges only: the auto row
  sent back as read (marked by `auto` or its `id`, whatever the amount) is dropped and
  the auto row is kept and recomputed (`Lifecycle._typed`); any other `tax` charge,
  whatever its label, is typed, and a charge's `shipping_option_id` is kept when
  sent. `OrdersComponent.rules` reads only its five settings (`SettingsStore.read`, one
  query, no secret unsealed), and `_retax` writes nothing at 0 percent with no auto row.
  `PUT …/shipping-option` (`set_shipping_option`, `{option_id}`, null
  for none) writes the order's one `shipping` charge from Settings → Orders'
  `storefront_shipping_options` while `draft` or `quoted`: an unknown id is 422 on
  `option_id`, an option that ships needs the shipping address
  (`shipping_address_required`), a pickup does not (gap 15), and `PATCH` does not clear
  the address while such an option stays (`_no_option_ships`, the same slug). A payment
  that reaches the deposit pays the open and failed `deposit` requests (#4959: a failed
  page can be approved again), a provider's as a manual one
  (`_close_deposit`), and their pages are closed after the commit. Routes in
  `api/order_payments.py`; agent `order_deposit_set` and `order_balance_waive` (outward),
  `order_deposit_waive` and `order_shipping_option_set` (write).
  An item added after acceptance (#5349, spec §3.5 "As built"): `POST
  …/items/after-acceptance` (`Lifecycle.add_item`/`add_request_item`/`add_custom_item`
  with `after_acceptance`, checked by `_takes_item`) on an `accepted`, `in_production` or
  `ready` order not on hold (`TAKES_ITEMS`); refused `order_not_accepted` before that,
  `item_locked` once shipped. The item starts unpriced (a custom one `quoted`, `accept:
  true` 422), its audit row is `item.added_after_acceptance`, and `derive` sets the status
  (`ready` goes back to `in_production`). Past `quoted` the lock is per item (`item_open`,
  `_open_for_item`, `quote.py` `quote_check`): an item with no accepted quote takes
  revisions, edits, quotes and an accept; an accepted one stays pinned (until #5350's
  revision after acceptance, below). A cancelled
  unpriced item reopens (alone, or with its cancelled order) as `draft` or `quoted` on
  such an order too; only a `shipped` one refuses it (`item_not_priced`). The two state
  sets are in `orders/models.py` and the spec (`Order.status`'s `x-open-for-edits`,
  `x-takes-items`), which `afterAcceptance.test.ts` checks the browser's copy against. Agent
  `order_item_add_after_acceptance` (outward; `order_item_add` stays write); frontend
  `components/orders/afterAcceptance.ts` (the rules, the confirm text) in `AddItemDialog`,
  `AddToOrderDialog`, the order page and its custom item row (#5541); the item drawer
  accepts a custom item's quoted price (`QuoteEditor` `acceptPending`).
  The charges changed after acceptance (#5351, spec §3.5 "As built"): `PUT
  …/charges/after-acceptance` (`Lifecycle.replace_charges_after_acceptance`, body
  `{charges}`) on a `TAKES_ITEMS` order not on hold; refused `order_not_accepted` before
  that, `order_not_editable` once shipped. Its audit row
  `charges.replaced_after_acceptance` holds the charges before and after. `PATCH` with
  charges past `quoted` names the route. Agent `order_charges_update_after_acceptance`
  (outward); frontend `ChargesEditor` with `addMode` and `chargesWarning`. A no-charge
  item is #5349's custom item at price 0; job cost shows its cost as a `loss`.
  An accepted item's new design (#5350, spec §3.5 "As built"): `POST
  …/items/{item}/revisions/after-acceptance` (`Lifecycle.add_revision` with
  `after_acceptance`, checked by `_revisable_after_acceptance`), the plain revision body,
  for an item with an accepted quote on a `TAKES_ITEMS` order not on hold; refused
  `order_not_accepted`, `item_locked`, `item_not_open`, `item_not_priced`, `item_custom`,
  `print_in_flight` and, once an attempt is accepted, `item_partly_fulfilled` (#5592). `_revise` keeps the accepted quote (the old price stands in the
  totals), sets `order_items.revised_after_acceptance`, puts a `REQUOTED` item back to
  `draft` (no print or fulfilment at the old quote) and writes
  `revision.added_after_acceptance` (with `accepted_quote_id`). While the mark stands the
  lock is `requote_open` (`_open_for_requote`, and `quote_check`): quotes, a quote's
  acceptance, a customer's answer and more revisions, never the quantity. `_accept_quote`
  clears the mark; `_reopened_status` gives such an item `quoted` or `draft`.
  `accept_fulfilment` refuses an attempt made before the accepted quote's revision
  (`fulfilment_stale`, `_before_priced_design`, #5582); the rule is `stale_attempt`,
  which `GET /orders/{id}` also gives each attempt as `stale` (`ItemAttempt`, #5592;
  the drawer's "Old design", `FulfilmentForm`). Agent
  `order_item_revision_add_after_acceptance` (outward; `order_item_revision_add` stays
  write); frontend `afterAcceptance.ts` `reviseMode`, `priceOpen`, `reviseWarning` in
  `RevisionsPanel` and `AddItemDialog` (`revise` with `mode`).
  Job cost phase 1 (#5326, spec `2026-11-05-job-cost-design.md`, plan
  `2026-11-05-job-cost-phase-1.md`): `order_time_entries` and `order_cost_entries`, written
  only through `Lifecycle.add_time_entry`/`remove_time_entry`/`add_cost_entry`/`remove_cost_entry`
  in every order status, ended and on hold included, except that adding one to a storefront cart not yet submitted is 409 `order_not_submitted` (`time.added`, `cost.removed`, … on the
  audit line); a time entry copies `pricing_labour_rate_per_hour` (null outside the shop's
  currency), and an entry's `source` (a timer's `timer:<item or order>:<started_at>`, a form's
  `manual:<uuid>`, a writer's id) makes a retry write once (200 with the first entry; a time
  entry's is unique per order whatever its kind, a cost entry's per order and kind). An item with entries is never deleted, only
  cancelled (`delete_item`, `item_has_entries`), and a cancelled order is charged nothing, its
  charges included (§5). `orders/costs.py`
  `order_costs(..., CostRates)` computes on read each attempt's filament and machine cost
  (`estimated`/`unknown` when a figure is not measured), each item's labour (entries, else
  a default), other costs, `revenue_minor`, margin and `margin_state`, and the order's sums
  with its shipments' cost and every charge but tax. Settings `pricing_labour_rate_per_hour`,
  `pricing_usd_rate`, `orders_thin_margin_percent` (live). Routes `POST`/`DELETE
  /orders/{id}/time-entries` and `…/cost-entries`; agent `order_time_add`, `order_cost_add`
  (write); the order page's `components/orders/OrderCosts.tsx` and `CostEntryForms.tsx`.
  Every attempt counts, test prints (#5280) too (`OrderStore.cost_rows`, every purpose).
  Job cost phase 5 (#5330): the assistant's spend arrives from the agent service on its own
  CRUD route, `POST /api/v1/internal/crud/agent/orders/{id}/assistant-cost` (`api/crud_agent.py`,
  class `crud.agent`, area `order-costs`; only the `agent` credential), a session's USD
  figure that `Lifecycle.add_assistant_cost` converts at `pricing_usd_rate` (`UsdRate`: 1
  for a USD order, the rate for the shop's currency, else 409 `currency_unconvertible`; a
  visitor's unsubmitted cart is 409 `order_not_submitted`; an item gone or not the order's
  puts the entry on the order) into
  one `assistant` entry, source `ai_session:<id>`, its note keeping the USD figure; a
  repeat returns the first entry, and a first report under one minor unit writes nothing
  (`entry: null`, which the agent keeps to report with more spend). The backend reads no `ai_*` table for it.
  Job cost phase 2 (#5327): `GET /orders` rows (`OrderRow`) carry `margin_minor`,
  `margin_percent`, `margin_state` and `margin_estimated`, and the list takes `margin_state`
  repeated. `costs.py` `order_margins` and the per-order read are one computation
  (`_order_costs`) over `OrderStore.cost_rows`, which reads every table once for all the
  orders given (one order for the page, a page for the list); never a second formula. A
  margin is not stored, so the filter computes each store page's margins until the page is
  full or `MAX_SCAN_PAGES` pages were read (`api/orders.py` `_order_page`, #5515): a page
  can be short or empty while a cursor remains. Frontend: the list's Margin column, its sort (the
  rows loaded, `components/orders/marginSort.ts`) and the "Loss or thin" chip
  (`lib/ordersQuery.ts` `margin=loss_thin`); agent `order_list`'s `margin_state`.
  Job cost phase 3 (#5328, §6.3): `GET /orders/history/unit-cost?slug=` (`orders/history.py`
  `unit_cost_history`, `OrderStore.fulfilled_items`): the slug's last 10 items `fulfilled`
  within 365 days of their last accepted attempt, on orders not cancelled, in one currency;
  each item's cost is `_order_costs`'s, never a second formula. It gives the unit cost, the
  main reason, the suggested price and, with `unit_price_minor`, `warning`. Advice only.
  Frontend `components/orders/QuoteWarning.tsx` in `QuoteEditor` (mock
  `mocks/features/orders/history.ts`); agent `order_item_quote` adds `price_warning`.
  Job cost phase 4 (#5329, §6.4): `GET /orders/reports/profit?from=&to=&group=model`
  (`orders/report.py` `profit_report`): the orders accepted in `[from, to)` (default the
  last 90 days), cancelled ones too, per currency (never summed across currencies) and per
  product (`product_of`: a model `slug`, else a `sku`, else `custom`, an empty SKU too;
  the list's `custom` filter matches `sku` null or `''`). Each row sums its
  items' figures from `_order_costs`, never a second formula. Waste is the cost of test and
  failed attempts. The `order` row is each order's read less its items' (entries, shipping
  cost, charges), over the orders with figures of their own; it has no list link. `total`
  is the orders' reads summed. It reads batches of `REPORT_BATCH`
  (`OrderStore.accepted_orders`, index `orders_accepted_at`) and stops at
  `MAX_REPORT_ORDERS` (`complete`, `read_from`). `GET /orders` takes `product`, `accepted_from`, `accepted_to` and
  `currency` (`OrderFilter`, `models.py` `Product`), which list a row's orders. Frontend
  `pages/ProfitPage.tsx` (`/orders/profit`, tab `components/orders/OrdersTabs.tsx`, links
  `components/orders/profit.ts`; mock `mocks/features/orders/report.ts`); agent
  `orders_profit_report` (read) and `order_list`'s new filters.
  Job cost phase 6, parts (#5543, §11): `orders/parts.py` (`PartStore`, the `parts`,
  `part_stock_moves`, `model_part_rules` and `order_item_parts` rows; a unit cost is a
  `numeric` rate, each cost entry rounded once), routes `api/parts.py` (`/parts…`,
  `GET /orders/{id}/parts`, `PUT /orders/{id}/items/{item}/parts`). `Lifecycle.add_item` copies
  the model's rule; `accept_fulfilment` making an item `fulfilled` takes its parts out of stock
  as `material` cost entries `part:<item>:<part>` (`parts.used`), `unfulfil_item` gives them
  back (`parts.returned`); a fulfilled item's parts are fixed (`item_parts_used`). The
  unit-cost history takes `item_id` (`parts_unit_minor`). Agent `part_list`, `part_stock_add`,
  `order_item_parts_set`.
  The machine rate (#4625, order operations spec 2026-11-07 §6): per registry printer,
  `orders/machine_rate.py` (`compute`, pure; `MachineRateStore` over
  `printer_cost_profiles`, `printer_wear_parts` and `printer_maintenance_costs`, each in
  `printers/store.py` `PRINTER_REFERENCES`; `MachineRates`, `OrdersComponent.machine_rates`).
  Each part per hour is rounded to 0.0001 and the rate is their sum. A printer linked to
  Bambuddy takes its tasks and intervals from `GET /maintenance/printers/{id}` (the seam's
  `maintenance_tasks`, `Read Status`; calendar and disabled tasks are listed, not counted)
  and keeps the counted tasks' intervals on its task rows (`interval_read_at`; every other
  row's is cleared), which price it when Bambuddy does not answer (`intervals` `stored`):
  the stored path counts only those rows (`compute`'s `read_tasks`; a task not read yet is
  `not_read`), so it gives the rate last read live. No profile is the book's
  `machine_rate_per_hour`. A merge of the three tables runs on the API only: the print
  worker's run makes or merges its printer row through `POST
  /api/v1/internal/crud/print/printers/ensure-bambuddy` (`crud/print.py`
  `RemotePrinterStore`, area `printer-ensure`, #5298 Task 29's route taken early). Routes
  `GET`/`PUT`/`DELETE /printers/{id}/machine-rate` (`api/machine_rates.py`). A slice quote
  is priced at the rate of the printer it sliced for (`QuoteRun.machine_rate`,
  `MachineRates.for_quote`: `printer`, else the printer linked to `printer_id`, else the
  default printer) and records `printer` and `machine_rate_breakdown`. A quote's
  `support_filaments` price each sliced slot that is no colour slot of the item as its own
  `Support, …` filament line (`support_check` in the quote's check). The money view
  prices an attempt with no quote's rate at its printer's stored rate, or the book's for a
  printer with no profile (by its kept `printer_serial` when that row was merged away,
  `CostRates.serial_rates`); an attempt naming no registry printer at the default
  printer's, else the book's (`CostRates.printer_rates`, `MachineRates.money_rates`).
  Printing an item (#4719, spec §5.4 "As built"): the `order_item_print` kind
  (`POST /orders/{id}/items/{item}/print`, `library` queue, check `orders/printing.py`
  `print_check`, `requote_needed` when the accepted slice quote's file is gone or changed)
  starts the item's output's print through `api/printing.py` `accept_run`, never a path of
  its own, its `request_id` from the operation's key. Its body's `path` and `options` are
  the print route's (#5352): with `path` `bambuddy` the print files under the item's
  project (`item_project_of` judges the route with `asked`) and the calibrations and
  timelapse reach the queue item. The item is claimed first, under the
  order's lock (`Lifecycle.claim_print`: the attempt keyed by `print_request_id`, the quote's
  file held by `(order_item_fulfilment, <id>)`, the item `queued`), so one print starts per
  item; then the run is attached (`attach_print`) or a refused start releases the claim
  (`release_print`); a retry re-checks a run-less claim before it starts (refused, it
  attaches the run its request made if one exists, else releases), a key's claim on
  another item is `print_request_conflict`, and a run-less claim's `PATCH` waits for its
  operation (`print_starting`). The item follows the run from
  `print_runs` and `print_outcomes` only (`Lifecycle.follow_print`, the component's
  `listen_for_prints`), never by asking a printer or Bambuddy. Agent `order_item_print`
  (outward); frontend `components/orders/PrintItem.tsx` over `PrintPicker`'s `itemPrint`.
  A library file item (#5544, spec §3.3 "As built"): `kind` `library_file`,
  `OrderItem.library_file` `{id, filename, library}` (columns `library_file_id`,
  `library_filename`, `library_source`, check `order_items_library_file`); no reference,
  output or revisions (`lifecycle.py` `NO_REVISIONS`, `has_design`). `POST …/items` and
  `…/items/after-acceptance` take `library_file_id` (any other source or kind is 422), read
  the file from the library that serves as the library print route does (`orders/bambuddy.py`
  `ServedLibrary`: 422 on `library_file_id` for one it lacks or does not print), then
  `Lifecycle.add_library_item`. The slice quote is `quote.py` `_library_quote`
  (`Slicer.start_library`; the first quote gives the item its slots, `library_colours`; the
  quote names no revision); the print starts `api/library_print.py` `start_library_run`
  (subject `library:<id>`; the check returns `library_file_id`). A file gone since is 409
  `library_file_gone` in the quote's check and run and the print's check. Its slots'
  filaments are written on the item (`OrderStore.set_library_colours`). Agent
  `order_item_add`'s `library_file_id`. Frontend: `AddItemDialog`'s "Library file" source
  (`components/orders/LibraryFilePicker.tsx` over `GET /print/library/search`), the items
  grid's file line, and `QuoteEditor` and `PrintItem` on a `library` print source; mock
  `mocks/features/orders/orders.ts` `addLibraryItem`.
  Recent renders (#4777, order operations spec 2026-11-07 §3.1): `GET /outputs` lists every
  model's saved outputs, newest first, as `OutputDetail`s, filtered by `from`/`to`, `slug`,
  `tag` (the model's catalogue tags), `q` (name, or the params that differ from the cached
  schema's defaults as `name=value`) and `ordered`; 50 a page by default. It reads the
  `output_index` table (`library/output_index.py` `OutputIndex`), which `OutputStore`
  writes on save (a failure is logged), rename and delete, and whose rows of a model go
  when its outputs directory does (`Catalogue._forget_cover` → `OutputStore.forget_indexed`:
  a model's delete, the orphan sweep and a reused slug); the API's start runs
  `OutputIndex.sync` in the background (adds a row for any output with none, drops rows
  whose output is gone, `main._index_outputs_logged`), and a listed row whose files are
  gone is dropped as it is read. `POST /orders/{id}/items/batch` and
  `…/items/after-acceptance/batch` (`ItemBatch`, at most `MAX_BATCH_ITEMS` 50) add
  `ItemCreate`s through `Lifecycle.add_items`, the one transaction every single add now
  uses (`NewModelItem`, `NewLibraryItem`, `NewCustomItem`, `NewRequestItem`); a body
  refused before the add names its place, `item_index`. Agent `list_recent_outputs`
  and `order_item_add`'s `items`; frontend `components/orders/RecentRendersPicker.tsx`
  ("Recent renders" in `AddItemDialog`), mocks `mocks/features/recentOutputs.ts` and
  `orders/orders.ts` `addItems`.
  Past prints (#4777, §3.2): `ItemCreate.archive_id` alone (any other source or `kind` is
  422) makes a Bambuddy archive an item (`api/orders.py` `_archive_item`): a `model` item of
  the output whose `print_links` row owns it, else a `library_file` item of the library file
  that owns it, else (an output or file gone since, or an output whose template was deleted,
  falls through too) a `library_file` item of
  a copy of its printed file (`/archives/{id}/download`) in the native library,
  `orders/past_prints.py` `copied_file`, one per archive (`archive_library_copies`, reused while
  the file is not trashed); 409 `archive_gone`, and `archive_copy_not_native` when a copy is
  needed and the Bambuddy library serves. The copy's download makes the spool directory
  first, and a pick that loses a race to record its copy trashes its own. The item keeps `origin_archive_ids` (the archive and
  its print run's other linked plates, `OrderStore.archive_run_plates`). Quote source
  `archive`: `POST …/items/{item}/quotes/archive` (`ArchiveQuoteRequest`: `archive_id`,
  default the item's first origin, one of its origins or printed by its output or library
  file, else 422; `filaments`; `filament_id` for every slot the print used), `quote_from_archive`: the slice quote's
  check, then every plate archive of the run (`print_sends` by `run_id`, `print_links` by
  queue item; 409 `archive_plates_missing` for a plate with none, `archive_not_succeeded` for
  a status other than `completed`, `archive_gone`), grams per slot from
  `extra_data.filament_slots` (slot id less one), the actual seconds else the estimate, each
  plate an `ArchivePlate` naming its archive, recorded by `Lifecycle.record_archive_quote`
  (the whole run is one unit: a run that printed several units, on one plate or as copies,
  overstates the unit cost: `ArchiveFacts` carries no count of the units a plate held)
  (no sliced file; printing it re-slices, as a manual quote). A library file item with no
  slots gets the slots the print used (`archive_colours`). Job cost reads an `archive`
  quote's plates as a slice quote's (`models.py` `MEASURED_SOURCES`). Agent
  `order_item_add`'s `archive_id`, `order_item_quote`'s `archive`; frontend
  `components/orders/PastPrintsPicker.tsx` ("Past prints" in `AddItemDialog`, over
  `GET /farm/archives`; status and name filter the listed page), `ArchiveQuote.tsx` ("Quote
  from the print" in `QuoteEditor`), the grid's "From past print" line; mocks
  `orders/pastPrints.ts`, `orders.ts` `addArchiveItem` and `priceBook.ts`.
  The items card's writes (#4955, §4.2): `POST …/items/{item}/duplicate`
  (`Lifecycle.duplicate_item`, body `{after_acceptance}`: the item's kind, current design,
  past print archives, quantity, title, notes, SKU and per-unit parts, added as `add_items`
  adds one; no quote copied but a custom item's own price; 409 `template_deleted` for a
  model item whose template is gone, checked by the route; audit `item.duplicated`) and `POST
  /orders/{id}/fulfil` (`Lifecycle.fulfil_items`, `FulfilLine`s: per item an attempt by hand
  of `quantity`, `succeeded`, recorded and accepted through the same `_insert_attempt` and
  `_accept_attempt` as the single routes, in one transaction; an item not `approved` or
  `failed`, or named twice, refuses all). Both refuse a visitor's cart (`order_not_submitted`).
  Customer messages (#5660, spec `2026-11-08-order-notify-customer-design.md` §7): the agent
  sends through a messaging plugin and the API keeps the record, `order_messages`
  (`orders/messages.py` `OrderMessages`, shapes `orders/message_models.py`, routes
  `api/order_messages.py`), each write under the order's lock with its `order_events` row
  (`message.sending`, `.sent`, `.failed`, `.unknown`, `.marked_sent`). The agent claims a
  `sending` row under its `Idempotency-Key` before it calls the plugin (a key seen before
  answers its row, 200), then posts the outcome, taken from `sending` or `unknown` only. One
  template is told once per status change (`episode`, the order's newest
  `order_status_changes` row): a second claim is `already_notified` unless it names
  `resend_of`; `GET …/messages/check` answers the same and writes nothing. A `sending` row
  older than `STALE_SENDING_MINUTES` reads `unknown`, is never sent again by itself and has
  no sweep; only a person marks it sent (`…/mark-sent`, `PERSON_ENDPOINTS`). The plugin
  binding is the stored setting `orders_customer_messaging` (`MessagingBinding`; no secret,
  a secret-looking `fixed_args` key is refused; an unreadable one loads as none).
  Bambuddy (phase 5, #4253, spec §8): `orders/bambuddy.py` is the one file in `orders/`
  that imports `bambuddy.client` (`tests/test_orders_bambuddy_seam.py`), reached through
  `orders/integrations.py` `OrderIntegrations` (`OrdersComponent.integrations`, chosen for
  the running `bambuddy_url`; `NotLinked` without one); the quote's Bambuddy slice calls
  live there too. `POST /orders/price-book/import-spools` (`orders/spool_import.py`) makes
  or updates filament records from the spools, on click only (`PriceBook.spool_import`).
- Storefront accounts (#5283, spec 2026-11-02 §5.2): `orders/storefront/accounts.py`, a
  passwordless sign-in on the caller's connection (migration `…_storefront_accounts.sql`).
  - `request_code` makes a sixteen-character code from `CODE_ALPHABET` (Crockford's
    base32 less `0` and `1`), shown `XXXX-XXXX-XXXX-XXXX`; `verify_code` reads it with
    case, spaces and hyphens ignored. It does the same for every address, whether or not
    an account exists (enumeration-safe). Only a salted sha256 is kept. A code dies only
    when used or after an hour (`CODE_TTL`); wrong guesses never spend it, and a correct
    live code signs in from any client. No address can be locked: requests are limited
    per address and client (3 an hour) and per client (10), wrong guesses per client (20),
    never per address, and a client is `core/proxies.py` `client_key` (IPv4 address,
    IPv6 /64). The mail cap is 20 live codes per address: past it `request_code` returns
    None, raises nothing, and the caller answers as usual and sends no mail. A full cap
    means 20 live codes in the owner's inbox, and a sign-in marks them all used, freeing
    it. `verify_code` always makes 20 compares, padding with a dummy digest. The bound is
    the code (about 10^-17 an hour per address; the arithmetic is in the docstring).
    `request_code` and `verify_code` each hold transaction locks on the client, then the
    address, so call them in a transaction. The code route, a later PR, is to take
    Turnstile (`sign_up`), and the verify route (`sign_in`), whenever a site key is set.
  - `verify_code` makes the account at the first sign-in. It links the one unarchived
    customer with that email contact, else makes a new customer. It joins a visitor of no
    account, rotates the session (the old row is deleted), and stores only the 256-bit
    token's sha256.
  - What an account sees is `orders_of_account`: its visitors' orders plus `attach_order`'s.
    The order page attaches an order whose link was opened while signed in, in its own
    transaction.
  - Old code and failure rows are pruned as new rows are written, and every expired
    session at each sign-in. There is no sweep.
- `backend/scadbuddy/payments/` — the payments seam (#4957, epic #4962, spec
  `2026-11-02-storefront-design.md`, plan `2026-11-07-storefront-phase-2-payments-deposits.md`):
  a payment request asks the customer for money through a provider's hosted page, or by
  hand with no provider. `models.py` (`PaymentRequest`, the storefront's shape with no
  `provider_ref`; `PaymentRequestView`, the back office's; `ProviderEvent`), `store.py`
  (SQL in the caller's transaction; `open_requests_for_order`), `provider.py` (the
  `PaymentProvider` protocol, `Verified`: `verify`'s account label and, when the account
  takes one currency, that currency, `BadSignatureError`, `ProviderError`), `registry.py` (the
  offered providers; the fake one only with the bootstrap
  `SCADBUDDY_PAYMENTS_FAKE_PROVIDER`), `fake.py` (`signed_event` for tests and dev),
  `stripe.py` (#4958: a request is a Checkout Session; `Stripe-Signature` checked by our
  own code, no SDK; the facts it rests on, each with its Stripe link, are its docstring),
  `paypal.py` (#4959: a request is an Orders v2 order to capture; `CHECKOUT.ORDER.APPROVED`
  is the `approved` event, and ScadBuddy captures it (`CapturingProvider.capture_request`):
  under the order lock the lifecycle moves an open or failed request of an order that
  still takes money to `pending` and commits, then `Payments._capture` calls PayPal with no
  lock held; a capture PayPal refused or declined puts the request back to `failed` in a
  second transaction (`Lifecycle.capture_failed`), and one whose outcome is unknown (no
  answer) leaves it `pending` and keeps the event's row, so PayPal's retry reads `replayed`
  and captures again under the same key, only for the approval that last moved it to `pending` (named on that audit row,
  `Lifecycle.approval_pending`) and
  never on an order ended since. When PayPal stops retrying, the
  `housekeeping_settle_payment_captures` sweep (both Schedules, behind its patch) calls
  `Payments.settle_captures`: the requests an approval moved to `pending`
  (`capture_pending_at`, set by `mark_capture_pending`, cleared once the request leaves
  `pending`) whose wait is over (`captures_due`: `CAPTURE_SETTLE_FIRST_MINUTES` after the
  approval or the last read, doubled per unknown read, at most a day; claimed `FOR UPDATE
  SKIP LOCKED`), read with no lock held (`CapturingProvider.read_capture`). A capture
  found is recorded through `record_provider_event` (keyed `capture:<id>:<kind>`, so a pending read and a later paid one are two events; a later webhook
  writes no second payment, matched by the order) and its closes run, and one that leaves
  the request `pending` (`currency_mismatch`, `ignored`, `unmatched`) counts as an
  unknown read; a pending one is read once a day (`CAPTURE_DAILY_ATTEMPTS`), not
  counted, and is `failed` (`capture_pending_unsettled`) once `capture_pending_at` (the
  latest approval, or the backfill's run) is `CAPTURE_PENDING_GIVE_UP_DAYS` (30) old; a request whose settling raises is logged and counted, and
  the batch (`CAPTURE_SETTLE_BATCH`, 4) goes on; an `APPROVED` order with no capture is captured again under the approval's
  key once `Lifecycle.recapture_key` says, under the order lock, that the order takes money
  (else `failed`, reason `order_not_taking_money`); a voided order, a refused capture, or
  `CAPTURE_GIVE_UP_ATTEMPTS` unknown reads (`capture_unsettled`) mark it `failed`. The capture's events record the money. Each
  webhook is checked by PayPal's verify-webhook-signature call with the raw body; the
  OAuth token is cached in memory, never stored; a cancel ends open and failed requests
  (`CAPTURABLE`) and marks the order's `custom_id` as defence in depth (a failed mark is
  tried again by the housekeeping sweep below, with a backoff); the facts, each with its
  PayPal link, are its docstring),
  `square.py` (#5666: a request is a quick-pay payment link and its reference is the link's
  Square order id, which each payment carries; `x-square-hmacsha256-signature` checked by our
  own code over the notification URL setting and the raw body; a cancel finds the order's
  link by listing the links and deletes it; a payment is `paid` under Square's event id each
  time it is told, and its fee comes from `processing_fee`; only an ACH payment's `PENDING`
  and `FAILED` move the request, a declined card leaves it open, and a payment or refund
  with no order is `other`; `verify` gives back the location's currency; the facts, each with its Square
  link, are its docstring),
  `venmo.py` (#4960: once the storefront's `/pay/<token>` page ships (#5283 phase 4,
  `PAY_PAGE_READY`, false until then), a USD PayPal request from a shop with the `venmo`
  setting on will link to it; until then no link is rewritten and a connect with `venmo`
  `on` is `VenmoPageMissingError`, 409 `venmo_page_missing`, while a connect that leaves
  it out takes `off` (`ProviderField.default`; `ProviderField.unavailable` makes the form
  show `on` disabled, with why). Only the token's sha256 is kept, in
  `payment_requests.pay_token_hash`, read while the request is open or failed (not
  pending), dropped when it is paid, cancelled or expired, or linked again
  without one; `venmo_checkout` is what the edge's CRUD route reads,
  `POST /api/v1/internal/crud/storefront/pay/checkout` (`api/crud_storefront.py`, area
  `pay`, the token in the body, a bare 404 for None); a Venmo-funded capture is
  `ProviderEvent.method` `venmo`, and one whose order PayPal does not answer is recorded
  with no method),
  `credentials.py` (`PaymentCredentials`: the shop's account per provider, the secret
  sealed with `core/secrets.py` under AAD `payment_credentials:<id>:<provider>`; without
  a KEK `connect` raises `KekMissingError`, which `api/payments.py` answers 409
  `kek_missing`), `service.py` (`Payments`, what the routes call; `connect` refuses an
  account whose `Verified.currency` is not the shop's `orders_currency`,
  `CurrencyMismatchError`, 409 `currency_mismatch`, #5666, and keeps it on the row,
  `payment_credentials.currency`, given back as `ProviderStatus.currency`; Settings →
  Payments warns when `orders_currency` moved away from it since, and the settings
  route does not refuse that move; a provider whose account takes one currency
  (`PaymentProvider.reports_currency`, Square's) with a row of no currency, connected
  before it was kept, is asked to reconnect; such a provider whose `verify` names no
  currency is refused, `CurrencyUnknownError`, 409 `currency_unknown`) and
  `component.py` (`PAYMENTS`, which reads `ORDERS`; `PaymentsDep`). Tables
  `payment_requests`, `payment_events` (one row per provider and event id, so a replayed
  webhook records one payment; no body column) and `payment_credentials`, plus
  `order_payments.provider` and `.payment_request_id` (migration `…_payments_seam.sql`),
  all on the orders pool. The lifecycle (`open_payment_request`, `cancel_payment_request`,
  `set_request_link`, `record_provider_event`, `add_payment(payment_request_id=…)`) writes
  the rows under the order's lock, with an audit line for each but `set_request_link` (the
  link is the provider's, not a person's decision). A named provider's credential row is
  held `FOR SHARE` while a request opens, so a disconnect waits and then refuses; a
  default provider no longer offered (`Lifecycle.offered_providers`) is no provider. A
  provider is called only after
  that commit (`Payments.link`), with the request id as its idempotency key, and a
  failure stays on the open request as `link_error` (`POST …/payment-requests/{id}/link`
  retries). A provider's `paid` event with a fee writes a `payment_fee` cost entry,
  `source` `payment:<payment id>`, in the payment's own transaction (job cost spec
  §4.4); a manual payment writes none. A `fee` event (#4958, Stripe's balance transaction made after the
  payment) writes the same entry later, once (`_late_fee`, under the order lock, on the
  unique `(order_id, kind, source)` index); one that arrives before its payment is
  `EventTooEarlyError`, which writes nothing and answers 503, so the provider sends it again,
  only while the request is open or pending and the event (`ProviderEvent.created_at`) is
  younger than `FEE_RETRY_WINDOW` (24 hours); after that it is recorded as `ignored`. A
  `paid` event for a request on which this provider already recorded its payment (same
  provider, request and reference, `Lifecycle._provider_payment`, whatever the request's
  `payment_id`) writes no money; with a fee it writes that payment's fee once (#5666:
  Square adds the fee in a later update). A `pending` event (a bank debit on its way)
  moves an open request to `pending`, and `failed` moves an open or pending one to
  `failed`; neither writes money, a pending request is never cancelled or closed by a
  manual payment, and refuses its order's cancel (`payment_in_flight`). A disconnect is
  refused while a request is pending or failed: a failed one holds its provider until it is
  cancelled (#4959; PayPal's page of a failed request can still be approved, and
  `ProviderStatus.reapprovable` says so, which the order page's note reads). A cancel (of
  the request or its order) ends a `failed` request as well as an open one, and asks its
  provider to close its page too (Stripe's `cancel_request` reads a completed session as
  closed). A `refund_failed`
  event (#5681; Stripe sends it for every `failed` or `canceled` Refund) names the
  refund's event id (`ProviderEvent.reverses`, kept on its `payment_events` row). When
  that refund was recorded, it writes a positive payment that cancels the refund's row,
  with the amount, provider, request and reference of that row, never the event's
  (`Lifecycle._reverse_refund`). When the refund was not told yet, the failure is kept
  at once as `ignored` (no payment, no retry, no `OrderChanged`), and the refund told after it is
  `ignored` (`payments.reversed_by_failure`), so the two cancel out in either order. A
  refund told without money is `ignored`; no request's status changes. `parse_webhook` is
  async and may call its provider once the signature holds; a failed call is
  `ProviderUnavailableError`, a 503 that records nothing, so the provider sends it again. Routes: `api/order_payments.py`
  (`/orders/{id}/payment-requests`, and the deposit, waiver and shipping-option routes of
  #5281 above), `api/payments.py` (`/payments/providers`; connect,
  disconnect and the default are `PERSON_ENDPOINTS`) and `api/payment_webhooks.py`
  (`POST /payments/webhooks/{provider}`, the one public back-office route: raw body at
  most 256 KiB, a bad signature 400 with no event or money written, the actor `provider`
  `payment:<id>`). A failed webhook keeps only a fixed category on its credential row
  (`last_webhook_error`: `signature`, `provider_unavailable` or `handler_error`, and
  `last_webhook_error_at`; #5684; a failure to write it is logged, never raised over the
  webhook's own), never a
  provider's text; the same category stamped within `WEBHOOK_ERROR_QUIET` (60 s) is not
  written again (one conditional UPDATE), so a flood of unsigned POSTs writes once a
  minute. The trade-off: a same-category failure skipped inside the window after a
  success began keeps the older stamp, so that success clears it; the next failure after
  the clear is written at once. An `EventTooEarlyError` (a late fee within `FEE_RETRY_WINDOW`) is a
  bounded wait, not a failure, and keeps none. The next success clears it only when the
  failure is older than that success's start; both times are the database's
  `clock_timestamp()` (`PaymentCredentials.now`), never a replica's clock. A reconnect
  clears it; Settings shows it
  (`components/settings/WebhookError.tsx`). No route, log, problem or audit row carries a secret.
  A provider's page to close (#5700): `store.set_request_status` is the one place that
  marks it, setting `payment_requests.close_pending_at` when a request with a
  `provider_ref` is cancelled, or paid by a payment that is not its own provider's (by
  hand, or another request's payment that cleared the balance); its own provider's
  payment clears the mark. After the commit, a cancel and a manual payment close the
  pages they marked (`Payments.cancel_request`, `cancel_order`, `add_payment`); a
  webhook's payment hands its closes to the route's `BackgroundTasks` (`handle_webhook`
  `after`), so they run once the answer is sent. `Payments._close_page` clears the mark once the
  provider confirms (`close_confirmed`) and counts a failed try (`close_failed`:
  `close_attempts`, `close_tried_at`), each only while the request still holds the
  reference it tried. The `housekeeping_retry_payment_closes` sweep (both Schedules, behind
  its patch) calls `Payments.retry_closes`: up to `CLOSE_RETRY_BATCH` marked pages whose
  wait is over (`closes_due`: `CLOSE_RETRY_FIRST_MINUTES` after the mark or the last try,
  doubled per failed try, at most a day), claimed `FOR UPDATE SKIP LOCKED` with
  `close_tried_at` stamped as they are taken, so the two Schedules' runs never try one
  page at once (a sweep that dies between the claim and `close_failed` leaves the page
  waiting one full backoff with no try counted, on purpose). The first try, at or after
  the `CLOSE_GIVE_UP_ATTEMPTS`th (10; the tenth comes about 66.5 hours, 2.8 days, after
  the cancel), that finds the provider not connected gives the page up
  (`close_abandoned_at`); any other failure is tried again at most a day apart. Connecting the provider again (`PaymentCredentials.connect`,
  `resume_closes`) starts its marked pages' tries over, given up or not, and a new or
  cleared mark (`set_request_status`) clears the give-up and the tries. It never runs the
  lifecycle again. The order page says "Close not yet confirmed" while the mark stands,
  and "Could not close; close it at <provider>" once given up, only while the mark stands
  (`PaymentRequests.tsx`). A page the provider made after its request was cancelled or
  paid by hand (`Payments.link`'s race) is kept with its `provider_ref`, no `hosted_url`,
  and the mark (`Lifecycle.set_request_link`, `store.set_request_link` `close_pending`),
  so the sweep retries it like any other. A late page whose reference another request
  holds names that request's live page: it is not kept, not marked and not closed.
  The shop's bank account through Plaid (#4961) sits beside the seam, not in it: Plaid
  opens no payment request, so it is not in `registry.py` and not a `payment_credentials`
  row. `payments/plaid.py` (the stateless Plaid calls: link token, public-token exchange,
  `/accounts/get` of the chosen account, `/item/remove`; keys in the `PLAID-CLIENT-ID` and
  `PLAID-SECRET` headers; the facts, each with its Plaid link, are its docstring),
  `payments/bank.py` (`BankLinks`, `PaymentsComponent.bank`: one `bank_links` row, the keys
  and the access token sealed under AAD `bank_links:<id>`, and in the clear only Plaid's
  ids and the account's name, type and mask; Plaid is called outside every transaction, and
  a link that lost a race, chose an account that is not checking or savings, or failed to
  store removes its Item at Plaid (the page sends Link's token with every chosen id, and
  `link` exchanges it before refusing none or several, `BankChoiceError`, so no Item Link
  made is left behind; Plaid not answering is `PlaidUnavailableError`, 503
  `plaid_unavailable`; a linked row with no access token is `AccessTokenMissingError`,
  whose unlink drops the link and keeps the keys, `TOKEN_MISSING`) (`bank_keys_changed` when the keys were replaced or
  forgotten meanwhile); new keys are refused while linked, `bank_linked`; two first saves
  at once meet on the `singleton` column and the second updates the first's row; every
  Postgres call of an async method runs in `asyncio.to_thread`; an unlink Plaid does not
  answer keeps the token, an unlink without the key file is 409 `kek_missing` and keeps
  the row (the file may come back), and one whose keys Plaid refuses
  (`plaid.KEYS_REFUSED`) drops the link here only and keeps the keys (`REMOVE_AT_PLAID`),
  while one whose keys are sealed under another key file deletes the row, keys and all
  (`KEYS_UNREADABLE`: remove the Item in the Plaid Dashboard, then save the keys again);
  either way `BankUnlinked.note` says what to do), routes `api/bank_link.py` (`/payments/bank`; every write in
  `PERSON_ENDPOINTS`), frontend `components/settings/BankAccount.tsx` and
  `lib/plaidLink.ts` (Link's script from `cdn.plaid.com`, which `api/static.py` `PAGE_CSP`
  allows with its frame and Plaid's two API hosts), mocks `src/mocks/features/bank.ts`.
  Tests replay `tests/payments/plaid_payloads/` (Plaid's sandbox examples) through respx.
  The deposit sync (`payments/deposits.py` `sync_deposits`) is the housekeeping sweep
  `housekeeping_sync_bank_deposits` (the sweep Schedule only, behind its patch, never a loop
  in the API): `/transactions/sync` from `bank_links.sync_cursor`, at most `MAX_PAGES` pages,
  the loop started again from its first cursor on a mutation during pagination (an update
  `MAX_PAGES` cut short keeps that first cursor in `sync_loop_cursor` for the next run); each
  open order's balance is read once per sync (`_Balances`); keys that will not open, or a
  link with no access token, set `sync_error` instead of failing every run; the cursor
  is stored only when the link and the cursor are still the ones it started from. Posted
  deposits on the linked account only, kept in `bank_deposits` with the bank's text masked
  (`mask`: every run of five or more digits, a run going on across single spaces or
  hyphens; an order number is a run too, and only the canonical `ORD-yyyy-n` of an
  order that exists is put back, `_existing`, with digits run on after it still masked; rows stored before that rule were re-masked by
  `20261109T1420Z_bank_deposits_remask.sql`, every order number and every run of four or more digits, counted as
  `mask` counts them, in `bank_deposits.description` and in the notes of the payment a confirmed
  deposit recorded; `order_events.detail` holds ids only) and suggested in `bank_deposit_matches`
  (`order_number`, or `amount`: the balance of an order past `draft`, made on or before the
  deposit's date); nothing is recorded. A Plaid failure keeps its `error_code` as
  `bank_links.sync_error`, which Settings shows. Unlink drops the Item's deposits but the
  recorded ones (`bank.py` `_drop_deposits`), which keep their warnings below, and every
  suggestion; nothing prunes a kept row while its payment stands.
  The order page (`api/order_bank_deposits.py`, `GET /orders/{id}/bank-deposits`; frontend
  `components/orders/DepositSuggestions.tsx`, mocks `src/mocks/features/orders/bankDeposits.ts`)
  lists an order's suggestions; "Record this deposit as payment" (`…/{tx}/record`,
  `deposits.record_deposit`) is `Lifecycle.add_payment` through `Payments.add_payment`,
  method `bank_transfer`, reference the transaction id, under the deposit's row lock, so a
  deposit is recorded once on any order (`deposit_recorded`; `deposit_currency` for another
  currency; `deposit_changed` when a sync changed its amount meanwhile); the lock is taken in the payment's own transaction (`add_payment`'s
  `on_insert`), so a confirmation holds one connection. "Not this order" (`…/{tx}/dismiss`)
  writes a `bank_deposit_dismissals` row (transaction id and order), kept apart from the
  Item's deposits, so a later sync, an unlink and a new link all keep it. Both are in
  `PERSON_ENDPOINTS`; a deposit already recorded (by `bank_transfer` and its reference) is
  never suggested again, after a new link too. A recorded deposit Plaid changed since is
  listed with `reason` `changed`, and one Plaid removed (its row kept, `removed_at`) with
  `removed`, each with `recorded_minor`, the sum of the order's `bank_transfer` payments
  whose reference is the transaction id, while that sum is not the deposit's amount (0 when
  removed), and only once the bank moved it (removed, or `changed_at`: set the first time a
  sync reads a new amount for a recorded deposit and kept, so a change back after the
  shop's correction warns again). Payments are never edited, so the page's warning asks the shop to
  record the difference with that reference, which clears it. A cancelled order takes no
  payment and shows no such warning; a removed row whose payment is gone is deleted by the
  next sync. A sync emits `order.bank_deposits` (`OrderBankDepositsChanged`,
  topic `order:<id>`) per order it suggested a deposit for, and per order of a recorded
  deposit whose amount it changed or that it removed, so an open order page reloads.
  Plaid's webhook (`POST /payments/bank/webhook`, in `PUBLIC_ENDPOINTS`; the link token
  names it from `public_url`, `PaymentsComponent._webhook_url`) is checked by its
  `Plaid-Verification` JWT before it is read (`plaid.webhook_key_id`, `verification_key`,
  `verify_webhook`: ES256 only, a `kid` shaped as a UUID (`KID_SHAPE`) before Plaid is
  asked, `iat` at most 5 minutes old and at most `WEBHOOK_MAX_SKEW_SECONDS` (60) ahead,
  the body's SHA-256 compared in constant time; `BankLinks.handle_webhook`, which reads the
  `bank_links` row only after the key caches and the bucket, so a forged `kid` costs no
  database read or unseal); a bad one is 400 and changes nothing, a key Plaid could not
  give, or a link whose keys or access token cannot be read, is 503 (Plaid sends it
  again). Only a key that verified a webhook is kept by `kid`
  (at most `MAX_WEBHOOK_KEYS`, least recently used dropped first), and asked for again
  `KEY_RECHECK_SECONDS` (a day) after Plaid gave it; a key with `expired_at` set verifies
  nothing and is kept with the unknown ones; a `kid` Plaid does not
  have (`INVALID_WEBHOOK_VERIFICATION_KEY_ID`) is 400 and goes in a cache of its own for
  `UNKNOWN_KEY_SECONDS`, so forged `kid`s never evict a verified key. A `kid` in neither
  cache takes a token from `_KEY_LOOKUPS` (`KeyLookups`): the sender's bucket first (the
  address `relay_client` trusts, never a raw `X-Forwarded-For`), then the process's
  (a burst of `KEY_LOOKUP_BURST`); a refusal is 429 with `Retry-After`, which Plaid honours
  ([webhook retries](https://plaid.com/docs/api/webhooks/#webhook-retries)). A webhook
  whose `kid` is already being looked up waits for that lookup and takes no token.
  `SYNC_UPDATES_AVAILABLE` runs `sync_deposits` after the answer (`BackgroundTasks`), the
  sync's cursor check keeping it apart from the sweep's; an `ITEM` `ERROR` with
  `ITEM_LOGIN_REQUIRED` (or a sync that gets that code) sets `bank_links.login_required_at`
  and Settings shows "Reconnect": the link token is then for Link's update mode (the
  Item's `access_token`, no `products`; a linked row with no token is 409
  `access_token_missing`, never a fresh Link), and `POST …/reconnected` (person) clears it,
  with a `sync_error` that was `ITEM_LOGIN_REQUIRED`, and syncs after the answer;
  `LOGIN_REPAIRED` clears both too, and any sync that reads Plaid clears it. A webhook for another Item is `ignored`.
