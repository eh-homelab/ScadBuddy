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
uv run --frozen pytest
```

Tests marked `requires_openscad` / `requires_git` skip when the binary is not on
PATH. The backend will not start without `SCADBUDDY_DATABASE_URL` (#401: the settings
live only in Postgres), so every test that builds the app takes the `pg_conninfo`
fixture (a throwaway schema), and it and the `requires_postgres` tests (the render
queue, template media's `template_media`) skip unless `SCADBUDDY_TEST_DATABASE_URL`
points at a Postgres they can create schemas in, e.g.
`docker run -d -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=scadbuddy_test -p 5432:5432
postgres:17` and `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test`.
Without it most of `tests/api` skips. CI runs them against a `postgres:17` service
container. A `Settings` for an app that never starts uses `tests.conftest.UNUSED_DATABASE_URL`.
Tests marked `requires_temporal` skip unless `SCADBUDDY_TEST_TEMPORAL_ADDRESS` names a
running Temporal (e.g. `temporal server start-dev`) or a `temporal` CLI is on `PATH`
(`SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` can point at one; the test image ships
`/usr/local/bin/temporal`), from which the tests start their own dev server.
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
docker build --target agent -t scadbuddy-agent:dev .   # asserts CLAUDE_CODE_VERSION
```

Tests never call Anthropic. `test/run.test.ts` runs the bundled Claude Code binary
against a local fake Anthropic endpoint; `test/pg.test.ts` needs
`SCADBUDDY_TEST_DATABASE_URL` (e.g. `docker run -d -e POSTGRES_PASSWORD=postgres
-e POSTGRES_DB=scadbuddy_test -p 5432:5432 postgres:17`, then
`SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test pnpm test`).
Evals (`agent/evals/`, `docs/ai/evals.md`): `test/evals.test.ts` replays each scenario
against the fake endpoint in `pnpm test`; `pnpm evals` runs them live with the
credential saved in Settings (or `SCADBUDDY_EVAL_ANTHROPIC_API_KEY`, CI only; the
manual `ai-evals.yml` workflow) and skips cleanly without one.

Generated API files (#492): `backend/openapi.json`, `frontend/src/api/schema.d.ts` and
`agent/src/api/schema.d.ts` are gitignored and never committed. In frontend and agent,
`pnpm gen:api` (`scripts/gen-api.mjs`) exports the spec with uv, then writes the
client. `typecheck`, `test` and (in the agent) `build` run it first, so both packages need
uv and the backend tree. With `SCADBUDDY_OPENAPI_JSON` set, it reads that spec and skips
the export. That's how the Dockerfile's `frontend` and `agent-build` stages use the spec
from its `api-spec` stage. The `freshness` job no longer commits anything. It checks
that two exports are byte-identical, checks the committed msw worker, and posts the API
diff against main as one PR comment, edited in place. The msw worker
(`public/mockServiceWorker.js`) stays committed; regenerate it after an msw bump:

```bash
cd frontend && pnpm exec msw init public --save   # --save, or it prompts and dies with no TTY
```

Workflow/Dockerfile lint (the `lint` job): actionlint, hadolint with `.hadolint.yaml`,
`shellcheck .github/scripts/*.sh models/*/verify.sh`, `lint-verify-labels.sh`, and the
`.github/scripts/*.test.sh` suites. Every `docker run` in a `verify.sh` must carry
`--label "scadbuddy-verify=${SCADBUDDY_VERIFY_LABEL:-local}"` (Python:
`"--label", "scadbuddy-verify=" + os.environ.get("SCADBUDDY_VERIFY_LABEL", "local")`) on
the same line: `verify-models.sh` reaps a timed-out template's containers by it (#302).

Template checks (the `models` job): each `models/<slug>/verify.sh` the PR touches, or all
of them when the Dockerfile, `ci.yml` or the selector/runner scripts change, and always on
push to main and the weekly schedule. Run the same locally, in the Dockerfile's `base`
stage (OpenSCAD plus the image's fonts):

```bash
docker build --target base -t scadbuddy-verify:ci .
SCADBUDDY_OPENSCAD_IMAGE=scadbuddy-verify:ci SCADBUDDY_FONTS_IMAGE=scadbuddy-verify:ci \
  bash -c '.github/scripts/select-models.sh all | .github/scripts/verify-models.sh'
```

## Layout

- `backend/scadbuddy/render/` — the render pipeline: `runner.py` (openscad invocation,
  `-D` building), `schema.py` (`.param` → customizer schema), `split.py` (3MF split by
  per-triangle material), `solids.py` (one closed solid per colour via a `color()`
  wrapper), `bambu3mf.py` (Bambu-style 3MF writer), `glb.py`, `thumbnail.py` (numpy
  rasteriser for plate cover images), `plate.py`/`plate_profiles.py`, `jobs.py`
  (the render stages the worker's activities run; `render_job` runs them in one
  process, which the pipeline tests use), `job_models.py` (`Job`, `render_key`,
  `QueueFullError`), `submit.py` (`RenderService`, what the routes type against as
  `RenderDep`: submit inserts the row, starts the workflow, and a reconciler starts
  any pending row nothing picked up), `projection.py` (`render_jobs` as a projection
  the workflow writes in place through the `project` activity), `pg_store.py` (the
  backend's migrations). The legacy in-process queue, its file and Postgres stores
  and its `.renders/<key>` cache are gone (#546): the Temporal path's cache is the blob
  store's piece (`piece.json`), and nothing writes or prunes `models/<slug>/.renders/`
  any more (it stays hidden and git-ignored for volumes that still hold one).
- `backend/scadbuddy/workflows/` — renders on Temporal (#424): `pipelines.py`
  (`TemplatePipeline`, its `RenderPiece` children, `RenderPreview`), `activities.py`
  (the render stages as activities, `WorkerDeps`), `client.py` (`connect`,
  `render_worker`, `make_current`, `drained`), `models.py` (what crosses the history).
  `render_key` coalesces identical *jobs*; `piece_key` dedupes identical *openscad
  renders* across jobs. Never swap them.
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
  wires them, reading `store_backend` (a stored setting) at start.

  `python -m scadbuddy.store.verify_bambuddy` re-measures §6.3; it has not yet been
  run against a live Bambuddy (`tests/bambuddy/recordings/README.md`).
- `backend/scadbuddy/worker.py` — `python -m scadbuddy.worker`: the render worker,
  `/healthz` and `/metrics` on 9090; makes its build current at start and drains its
  pinned workflows on SIGTERM. `run_inprocess_worker` is the API's
  `SCADBUDDY_TEMPORAL_WORKER_INPROCESS` mode (no drain).
- `backend/scadbuddy/bambuddy/` — httpx client (`client.py`), send/print routes
  (`send.py`, `dispatch.py`, `print_run.py`, `filaments.py`, `projects.py`), scope-aware
  error mapping (`errors.py`).
- `backend/scadbuddy/library/` — catalogue, outputs, git-backed model history
  (`history.py`), fonts (`fonts.py`, `googlefonts.py`), per-template presets
  (`presets.py`: saved ones in Postgres, the `saved_presets` table (#332), outside git so
  a save never moves a template's revision; a template's own read-only ones in the `presets` list of its
  `model.json`, with a legacy `presets.json` still read), uploads for `// file`
  parameters (`assets.py`: the bytes under `data/assets/`, the metadata, last use and
  usage in the `assets` table (#591); a blob with no row is an orphan the sweep removes).
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
- `frontend/src/` — React 19 + Vite; `src/mocks/` is the msw API used by vitest and
  the mocked e2e run. A new feature's mocks go under `src/mocks/features/`, in
  `<feature>.ts` or a `<feature>/` folder. Every `.ts` file there except tests is picked
  up without editing `handlers.ts`, and must export `handlers` (and optionally
  `reset`) (#508).
- `frontend/src/lib/tracing.ts` — the browser's OpenTelemetry (#988), a lazy chunk
  `main.tsx` loads after the first paint; spans leave through `traceScrub.ts` and
  `relayExporter.ts` to the backend relay `/telemetry/v1/traces`, and stop for the
  page's life when it answers `X-ScadBuddy-Tracing: off` (`startTracing` then undoes itself, so
  no `traceparent` is sent; msw always answers off,
  `src/mocks/features/telemetry.ts`). A user action is `traceAction` (`lib/traceAction.ts`,
  entry chunk, API only): a request issued after an `await` joins the action's trace
  only inside its `within`. `traceparent` goes on same-origin requests only. The chunk loads through
  `loadOptionalChunk` (`lib/staleChunks.ts`), so a blocked one (an error naming `tracing-<hash>.js`) does not trigger the stale-chunk reload; any other chunk's error still does.
- `frontend/src/template-ui/` — template-owned UIs (#425): `host.ts` (Host API v1 over the page's
  inputs), `TemplateUi.tsx` (loads `ui/<module>` with `import()`, mounts into a shadow root, and
  reports a failure through `onFailure`; the Customize page then falls back to the generated form
  with a banner), `elements.ts` (`sb-param`/`sb-preview`/`sb-generate`, rendered by portal). Inputs are `{params, v, …ui state}` (`backend/scadbuddy/render/inputs.py`);
  `backend/scadbuddy/api/template_ui.py` serves `ui/**` live and at `/versions/{commit}/`;
  `api/static.py` `PAGE_CSP` (mirrored in `frontend/page-csp.txt`) is the page's policy.
- `agent/` — the AI agent service (#261), TypeScript on the Claude Agent SDK, shipped
  as the Dockerfile's `agent` target and run as a sidecar container. `src/config.ts`
  reads only infrastructure variables (`ENV_VARS`): `SCADBUDDY_DATABASE_URL`,
  `SCADBUDDY_BACKEND_URL`, `SCADBUDDY_SECRET_KEY_FILE`,
  `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` (rotation), `SCADBUDDY_PUBLIC_URL` (the same
  variable the backend reads; the one origin allowed to write) and
  `SCADBUDDY_AGENT_TRUSTED_PROXIES` (CIDRs whose `X-Forwarded-*` are believed). No AI
  env vars; AI settings live in the database.
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
  panel, via `src/harness/questions.ts`, `src/questions/service.ts` and `ai_questions`,
  #940, and never outlives its turn);
  `src/api/backend.ts` is the `openapi-fetch` client over the generated
  `src/api/schema.d.ts`. `src/tools/` is the tool registry (#251): one `defineTool`
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
  - Plugins given to the harness are vetted by `src/harness/plugins.ts`: anything that
    starts a process (command hooks, stdio MCP servers, LSP servers, monitors) is
    refused, because it would inherit the credential env.
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
    use local git repos (`test/support/gitRepo.ts`).
  - Tests never call Anthropic: `test/support/fakeAnthropic.ts` is a local Messages API
    (streaming SSE) that the real SDK and bundled CLI are pointed at as a gateway
    (`test/run.test.ts`). Postgres tests (`test/pg.test.ts`) skip unless
    `SCADBUDDY_TEST_DATABASE_URL` is set, as in the backend; the `agent` CI job sets it. The design is
  `docs/superpowers/specs/2026-09-27-ai-integration-design.md` (issue #250; on branch
  `claude/scad-buddy-ai-integration-pfn00c` until that spec merges).
  The 09-22 design spec's "No database" statement (`2026-09-22-scadbuddy-design.md`
  §4, "Architecture") describes the backend container; the
  AI spec (#250, PR #303) adds Postgres (#241) for the system as a whole, and the
  09-27 template-pipelines spec makes Postgres and Temporal required.
- `models/` — bundled example models (`models/<name>/verify.sh`).
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

- Base image is a pinned dated nightly, `openscad/openscad:dev.2026-09-28@sha256:…`
  (tag plus index digest; the only stable release, 2021.01, has no Manifold). The
  Dockerfile also asserts `OPENSCAD_VERSION` (currently 2026.09.28). Bump
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
  Dockerfile asserts the Claude Code binary it bundles (`CLAUDE_CODE_VERSION`,
  currently 2.1.283 for SDK 0.3.283). Bump both in the same commit.
- The `agent` jobs in `ci.yml` and `build-image.yml` use the buildx `type=gha` cache
  with `scope=agent`, so they do not overwrite the backend image's cache index.
- The repo's Actions cache has a ~10 GB ceiling, and it is full (9.9 GB on 2026-09-30,
  almost all `buildkit-blob`): GitHub evicts the least recently used entries. A new
  `type=gha` scope should use `mode=min` unless it needs its intermediate stages; if a
  scope's hit rate drops, move it to `type=registry` on GHCR, which works only on
  pushes and same-repo PRs, not forks (#743).
- `build-image.yml`'s `openscad-lsp-arm64` job (#199) is the one job there that runs on
  PRs (when the Dockerfile or that file changes): it builds the `openscad-lsp` stage for
  arm64 under QEMU and runs `openscad-lsp --version`, caching under
  `scope=openscad-lsp-arm64` with `mode=min`. It is not a required check. Making it one needs its
  `pull_request.paths` filter dropped first: a required check that never runs on a PR
  outside those paths leaves that PR waiting for it forever.

## PR conventions

- Conventional-commit titles (`feat(scope):`, `fix(scope):`, `docs:`, `ci:` …);
  Release Drafter labels and groups PRs by title. Body links the issue: `Fixes #N`.
- Required checks on `main`: **`CI Summary`** and **`claude-review`** (the ruleset
  lives in eh-homelab/clusters, so renaming either job breaks the gate silently).
- `claude-review` is a merge gate: the review runs after CI, then a classifier passes
  only when every finding in the review for *this* commit is fixed or tracked in an
  open `pr-feedback` issue for the PR. Adding the `claude-make-follow-up-issues` label
  to the PR files those `pr-feedback` issues automatically.
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
