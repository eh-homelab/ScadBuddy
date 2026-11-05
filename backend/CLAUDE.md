# Backend — notes for agents

These notes cover `backend/`; paths are repo-relative; the root CLAUDE.md has the repo-wide rules (migrations, CI, PR conventions, OpenSCAD facts).

- Migrations: see root "Migrations (#491)". Generated API files: see root "Generated API files".
- Real `openscad` only in the image, and the verified OpenSCAD facts: root "Verified OpenSCAD facts".
- The frontend side of orders and activity: frontend/CLAUDE.md. The agent's side (tools): agent/CLAUDE.md.
- The native hot path's real-stack e2e (the recorder pass): e2e-stack/CLAUDE.md. The slicer worker: workers/rust/CLAUDE.md.

## Database access

The rule is in the root CLAUDE.md, "Database access (#5295)". Today the API connects as the
database owner and runs SQL on the agent's `ai_*` tables. The render, print and projects
workers each connect as a role of their own, with the grant lists in
`render/worker_role.py` (`RENDER_GRANTS`) and `auth/caller_grants.py` (`PRINT_GRANTS`,
`PROJECTS_GRANTS`, and `AGENT_GRANTS` for the agent). Template `pipeline/` Python runs in
the render worker's pod, and openscad and `openscad-lsp` run in the API's pod.

Under the rule the API becomes the CRUD service: the only process with SQL access, and
nothing untrusted in its pod. Each worker becomes an HTTP client of it, with a service
credential, and loses its database secret, role and grant list (#5298). The `ai_*` reads
become routes (#5299). Every `/api/v1/internal/*` route requires a service credential, and
the API moves to `enforce` (#5297). Template Python and openscad move to a pod without a
database secret (#5296). Never add a worker table grant, a cross-service table read or a
new database client; add a CRUD route that requires a service credential instead.

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
  and reads settings only through the `render_settings` view; that role is today's state,
  superseded by #5295, which moves the worker's SQL behind CRUD routes, #5298). The legacy in-process queue, its file and Postgres stores
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
  first read and its analysis, and the Prints page's queue and history panels ask
  Bambuddy nothing on these backends (#4601, #4629; the panels read it only when opened).
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
  AUTO_UPGRADE, one execution per distinct request: `copy_id`), routes
  `api/library_copy.py` (`POST`/`GET /print/library-copies`, and `/print/bambuddy-library`,
  Bambuddy's library browsed as a source on `native`). A file whose SHA-256 the target
  holds is skipped (Bambuddy's `file_hash` and the native rows' `sha256` compare
  directly), each selected folder's path is kept, nothing at the target is deleted or
  overwritten. The bytes go into Bambuddy through `send.upload_raw`, the one raw upload
  path (`ensure_raw_copy` uses it too), and into the native library through
  `NativeLibrary.upload` (`StoreFullError` is a failed row naming the 507). Agent
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
  times its quantity (`api/orders.py` `item_price`, the browser's `components/orders/price.ts`),
  with `pending_minor` and `unpriced_items`; accepting still needs every quote accepted.
  Job cost phase 1 (#5326, spec `2026-11-05-job-cost-design.md`, plan
  `2026-11-05-job-cost-phase-1.md`): `order_time_entries` and `order_cost_entries`, written
  only through `Lifecycle.add_time_entry`/`remove_time_entry`/`add_cost_entry`/`remove_cost_entry`
  in every order status, ended and on hold included (`time.added`, `cost.removed`, … on the
  audit line); a time entry copies `pricing_labour_rate_per_hour` (null outside the shop's
  currency), and a cost entry's `source` makes a retried writer write once. `orders/costs.py`
  `order_costs(..., CostRates)` computes on read each attempt's filament and machine cost
  (`estimated`/`unknown` when a figure is not measured), each item's labour (entries, else
  a default), other costs, `revenue_minor`, margin and `margin_state`, and the order's sums
  with its shipments' cost and every charge but tax. Settings `pricing_labour_rate_per_hour`,
  `pricing_usd_rate`, `orders_thin_margin_percent` (live). Routes `POST`/`DELETE
  /orders/{id}/time-entries` and `…/cost-entries`; agent `order_time_add`, `order_cost_add`
  (write); the order page's `components/orders/OrderCosts.tsx` and `CostEntryForms.tsx`.
  Until #5280 every attempt is `production`.
  Printing an item (#4719, spec §5.4 "As built"): the `order_item_print` kind
  (`POST /orders/{id}/items/{item}/print`, `library` queue, check `orders/printing.py`
  `print_check`, `requote_needed` when the accepted slice quote's file is gone or changed)
  starts the item's output's print through `api/printing.py` `accept_run`, never a path of
  its own, its `request_id` from the operation's key. The item is claimed first, under the
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
  Bambuddy (phase 5, #4253, spec §8): `orders/bambuddy.py` is the one file in `orders/`
  that imports `bambuddy.client` (`tests/test_orders_bambuddy_seam.py`), reached through
  `orders/integrations.py` `OrderIntegrations` (`OrdersComponent.integrations`, chosen for
  the running `bambuddy_url`; `NotLinked` without one); the quote's Bambuddy slice calls
  live there too. `POST /orders/price-book/import-spools` (`orders/spool_import.py`) makes
  or updates filament records from the spools, on click only (`PriceBook.spool_import`).
