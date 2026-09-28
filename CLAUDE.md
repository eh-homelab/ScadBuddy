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
PATH. Tests marked `requires_postgres` (the render queue's Postgres store) skip
unless `SCADBUDDY_TEST_DATABASE_URL` points at a Postgres they can create schemas in;
CI runs them against a `postgres:17` service container. The only place a real `openscad` exists is the image:
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
  (`render_job` ties the steps together; job queue).
- `backend/scadbuddy/bambuddy/` — httpx client (`client.py`), send/print routes
  (`send.py`, `dispatch.py`, `pipelines.py`, `filaments.py`, `projects.py`), scope-aware
  error mapping (`errors.py`).
- `backend/scadbuddy/library/` — catalogue, outputs, git-backed model history
  (`history.py`), fonts (`fonts.py`, `googlefonts.py`), per-template presets
  (`presets.py`: saved ones under `data/presets/`, outside git so a save never moves a
  template's revision; shipped read-only ones in a template's `presets.json`).
- `backend/scadbuddy/api/` — FastAPI routes under `/api/v1`; `core/` — config/settings
  (every env var is `SCADBUDDY_<FIELD>`, see `core/settings.py`).
- `frontend/src/` — React 19 + Vite; `src/mocks/` is the msw API used by vitest and
  the mocked e2e run.
- `agent/` — the AI agent service (#261), TypeScript on the Claude Agent SDK, shipped
  as the Dockerfile's `agent` target and run as a sidecar container. `src/config.ts`
  reads only infrastructure variables (`ENV_VARS`): `SCADBUDDY_DATABASE_URL`,
  `SCADBUDDY_BACKEND_URL`, `SCADBUDDY_SECRET_KEY_FILE`,
  `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` (rotation), `SCADBUDDY_PUBLIC_URL` (the same
  variable the backend reads; the one origin allowed to write) and
  `SCADBUDDY_AGENT_TRUSTED_PROXIES` (CIDRs whose `X-Forwarded-*` are believed). No AI
  env vars; AI settings live in the database.
  `src/app.ts` is the Hono server (`/healthz`, plus `src/routes/credentials.ts` for
  `/api/v1/ai/credentials`). Every route that must know "is this the UI's origin"
  (credential writes and `/mcp` now; the agent's own sockets under `/api/v1/ai/*` later) uses the one allowlist in
  `src/http/origins.ts`, never an `Origin == Host` comparison (DNS rebinding makes
  those equal). `src/harness/options.ts` builds every query's SDK options
  (`tools: []`, `settingSources: []`) and `src/harness/run.ts` runs every `query()` on
  top of it (credential via the per-query `env` only, `maxTurns`, `maxBudgetUsd`,
  abort, the tier seam in `src/harness/permissions.ts` as both `canUseTool` and a
  `PreToolUse` hook; outward → denied as "needs approval" until #258);
  `src/api/backend.ts` is the `openapi-fetch` client over the generated
  `src/api/schema.d.ts`. `src/tools/` is the tool registry (#251): one `defineTool`
  per tool, projected in-process for the harness and over `/mcp` (`src/mcp/http.ts`,
  auth in `src/auth/`); every `/api/v1` operation needs a tool or a
  `src/tools/coverage.ts` entry, or `test/coverage.test.ts` fails.
  - Database: the agent owns the `ai_*` tables. Schema changes are appended to
    `src/db/migrations.ts` (numbered by position, never edited once merged, applied at
    start under advisory lock "SCADAGNT" with `lock_timeout`/`statement_timeout`,
    ledger `ai_migrations` with a sha256 per entry: an edited merged entry stops the
    service at start; separate from the backend's `scadbuddy_migrations`). Secrets are
    envelope-encrypted with `src/secrets.ts` under the KEK in
    `SCADBUDDY_SECRET_KEY_FILE` (32 random bytes, base64; spec §9); the AAD binds each
    value to its row and to the columns that say where it is sent (for the credential:
    `kind` and `base_url`). Comparable tokens are stored hashed instead.
  - Plugins given to the harness are vetted by `src/harness/plugins.ts`: anything that
    starts a process (command hooks, stdio MCP servers, LSP servers, monitors) is
    refused, because it would inherit the credential env.
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
  the authoritative manifest check.

## Verified OpenSCAD facts (do not re-derive; re-measure if the base image moves)

- Base image `openscad/openscad:dev` is a rolling nightly. The Dockerfile asserts
  `OPENSCAD_VERSION` (currently 2026.09.23) and fails the build on drift. When it
  fires, re-verify spec §3 against the new build and bump it in the same commit.
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
- Bambu Studio only reads `project_settings.config` if the 3MF claims
  `Application: BambuStudio-…`, and then segfaults unless five options are present
  (spec §3). Keep them.

## Bambuddy iframe facts

- Bambuddy has no plugin system. ScadBuddy is added as an External Link with
  `open_in_new_tab=false`, which Bambuddy renders in a sandboxed iframe at
  `/external/{id}` with `sandbox="allow-scripts allow-same-origin allow-forms
  allow-popups allow-popups-to-escape-sandbox"` (verified in the 1.2.5.5 bundle).
- Consequences in `frontend/src/lib/embed.ts`: downloads are fetched as a blob and
  opened with `target=_blank`; deep links to Bambuddy use `window.open(..., '_blank')`
  when embedded.
- Full screen (`frontend/src/lib/useFullscreen.ts`): a cross-origin iframe gets the
  Fullscreen API only with `allow="fullscreen"`, which Bambuddy is not known to set;
  where it is refused (`document.fullscreenEnabled` is false, or the request is
  rejected) the full-screen view covers the frame instead.
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
