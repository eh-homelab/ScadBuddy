# ScadBuddy — notes for agents

Self-hosted OpenSCAD customizer that sends multi-colour 3MFs to Bambuddy. The design,
and the measured facts it rests on, are in
`docs/superpowers/specs/2026-09-22-scadbuddy-design.md` (§3 is the verified-facts list);
the print dialog is `docs/superpowers/specs/2026-09-24-print-flow-design.md`.
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
PATH. The only place a real `openscad` exists is the image:
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

Generated files (the `freshness` job regenerates them on PRs and pushes a fix; run
them yourself when you change an API model or route, in this order):

```bash
cd backend && uv run --frozen python -m scadbuddy.tools.export_openapi   # backend/openapi.json
cd frontend && pnpm gen:api                                             # src/api/schema.d.ts
cd frontend && pnpm exec msw init public --save                         # public/mockServiceWorker.js
```

`gen:api` reads the exported spec, so export first. `--save` is required on `msw init`
(without it the CLI prompts and dies with no TTY).

Workflow/Dockerfile lint (the `lint` job): actionlint, hadolint with `.hadolint.yaml`,
`shellcheck .github/scripts/*.sh`, and the `.github/scripts/*.test.sh` suites.

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
  (`history.py`), fonts (`fonts.py`, `googlefonts.py`).
- `backend/scadbuddy/api/` — FastAPI routes under `/api/v1`; `core/` — config/settings
  (every env var is `SCADBUDDY_<FIELD>`, see `core/settings.py`).
- `frontend/src/` — React 19 + Vite; `src/mocks/` is the msw API used by vitest and
  the mocked e2e run.
- `models/` — bundled example models (`models/<name>/verify.sh`).

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
- The API key never reaches the browser; every Bambuddy call is server-side. Each
  client call declares its scope (`bambuddy/errors.py` `Scope`) so a 401/403 names it.

## CI and caching rules

- Every job runs on `ubuntu-latest`. The repo is public; never move a job onto the
  self-hosted `clusters-runner*` pools (fork PRs would run code on the LAN).
- **Never use the buildx `type=gha` cache on a self-hosted pool** — it fails the build
  there. It is used on the hosted runners in `ci.yml` and `build-image.yml`.
- Node major is pinned in both the Dockerfile and `ci.yml` (`24`); change them
  together, LTS (even) majors only. `frontend/pnpm-workspace.yaml` must be copied into
  the Docker build (it holds `allowBuilds`).

## PR conventions

- Conventional-commit titles (`feat(scope):`, `fix(scope):`, `docs:`, `ci:` …);
  Release Drafter labels and groups PRs by title. Body links the issue: `Fixes #N`.
- Required checks on `main`: **`CI Summary`** and **`claude-review`** (the ruleset
  lives in eh-homelab/clusters, so renaming either job breaks the gate silently).
- `claude-review` is a merge gate: the review runs after CI, then a classifier passes
  only when every finding in the review for *this* commit is fixed or tracked in an
  open `pr-feedback` issue for the PR. Adding the `claude-make-follow-up-issues` label
  to the PR files those `pr-feedback` issues automatically.
- The freshness job may push a `chore: regenerate committed generated files` commit to
  your branch; pull before pushing again.

## Known flakes

- Frontend vitest tests can time out when the machine is under heavy load (e.g.
  several builds or agents at once). Re-run before debugging; a failure that repeats
  on an idle machine is real.
