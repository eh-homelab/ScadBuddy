# ScadBuddy — notes for agents

Self-hosted OpenSCAD customizer that sends multi-colour 3MFs to Bambuddy. The design,
and the measured facts it rests on, are in
`docs/superpowers/specs/2026-09-22-scadbuddy-design.md` (§3 is the verified-facts list);
the print dialog is `docs/superpowers/specs/2026-09-24-print-flow-design.md`; template-owned
UIs and pipelines on Temporal, the blob store and Arrange are
`docs/superpowers/specs/2026-09-27-template-pipelines-design.md`.
Deployment is described in `README.md` ("Deploying").

## Where the notes are

This file holds the repo-wide rules. Each area keeps its commands and layout in a
CLAUDE.md of its own, which Claude Code reads when it works on files there:

- `backend/CLAUDE.md`: backend commands and tests (Postgres, Temporal, LFS fixtures, xdist, pytest-split), and the layout of `backend/scadbuddy/` (render, meshmodel, workflows, flows, operations, slicing, printers and the hot path, store, worker, bambuddy, library, tracing, components, activity, orders).
- `frontend/CLAUDE.md`: frontend commands, Playwright e2e, and the layout of `frontend/src/` (mocks, tracing, template-owned UIs).
- `agent/CLAUDE.md`: the agent service's commands, tests and evals, the vector fixtures, and its layout (runtime, routes, harness, tools, database, Temporal, gate, payloads, workspace, plugins, built-ins).
- `agent-durable/CLAUDE.md`: the Python agent parts' commands and layout.
- `tools/go/CLAUDE.md`: the Go workspace's commands, the Go client generator, the toolchain pin, and the modules.
- `workers/rust/CLAUDE.md`: the Rust workers' commands, toolchain and crates.
- `e2e-stack/CLAUDE.md`: the real-stack e2e runs (login and native hot path) and the stack's layout.
- `models/CLAUDE.md`: the template checks (`verify.sh`, the `models` job) and the bundled models.
- `deploy/CLAUDE.md`: the Grafana dashboards.
- `plugins/CLAUDE.md`: ScadBuddy's Claude plugin.

## Database access (#5295)

Only the CRUD service (today's API process) gets SQL access. No untrusted code can ever
reach the database. Every other process (the render, print and projects workers, the
agent services, agent-durable) is an HTTP client of the CRUD service, with a short-lived
service credential (PR #5211). The owner decided this on 2026-11-05.

- Never add a new database client, a read or write of another service's tables, or a
  cross grant. A role per caller in one database is not a boundary: a table owner's
  triggers, rules, defaults and builtin-shadowing functions run with the other role's
  privileges (the review of PR #5287, closed).
- Never run untrusted code (template Python, openscad, Claude Code tools, plugin code) in
  a pod that holds a database secret.
- New data that crosses services goes through a CRUD route that requires a service
  credential from its first PR.

Today's code still breaks the rule: seven processes hold a Postgres secret (six once
eh-homelab/clusters drops the projects worker's, which it no longer uses; the API and
the agent revoke its role `scadbuddy_projects` at every start, #5298), the agent
and the API share tables through grants, and template Python runs in the render worker's
pod. Do the work in this order: #5298 (the workers), #5299 (the agent), #5297 (authenticate
`/api/v1/internal/*`, then `enforce`), #5296 (untrusted code out of pods with a database
secret), then eh-homelab/clusters#3182 (the network). The evidence and the zones are in
[docs/security/trust-boundaries.md](docs/security/trust-boundaries.md).

## Commands (what CI runs)

Each area's commands are in its own CLAUDE.md (see "Where the notes are").

### Generated API files

Generated API files (#492): `backend/openapi.json`, `frontend/src/api/schema.d.ts` and
`agent/src/api/schema.d.ts` are gitignored and never committed. In frontend and agent,
`pnpm gen:api` (`scripts/gen-api.mjs`) exports the spec with uv, then writes the
client. `typecheck`, `test` and (in the agent) `build` run it first, so both packages need
uv and the backend tree. With `SCADBUDDY_OPENAPI_JSON` set, it reads that spec and skips
the export. That's how the Dockerfile's `frontend` and `agent-build` stages use the spec
from its `api-spec` stage. The frontend's `gen:api` also writes
`src/storefront/api/schema.d.ts` (gitignored) from the storefront edge's spec
(`export_openapi --storefront`, `backend/storefront-openapi.json`, gitignored), or from
`SCADBUDDY_STOREFRONT_OPENAPI_JSON` when it is set (#5282). A frontend vitest run outside `pnpm test` (`pnpm exec vitest
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

The Go client's generator and checks: `tools/go/CLAUDE.md`.

### Workflow/Dockerfile lint

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

The template checks and the `verify.sh` label rule's runner: `models/CLAUDE.md`.

## Layout

Each area's layout is in its own CLAUDE.md (see "Where the notes are").

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

The backend's migration files and its settings tables: `backend/CLAUDE.md`.

## Verified OpenSCAD facts (do not re-derive; re-measure if the base image moves)

- Base image is a pinned dated nightly, `openscad/openscad:dev.2026-11-09@sha256:…`
  (tag plus index digest; the only stable release, 2021.01, has no Manifold). The
  Dockerfile also asserts `OPENSCAD_VERSION` (currently 2026.11.09). Bump
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
  scope's hit rate drops, move it to `type=registry` on GHCR: writes to it work only on
  pushes and same-repo PRs, not forks (#743); reads of a public package work on fork
  PRs too. The slicer image's cache is there now
  (#4059): `ghcr.io/eh-homelab/scadbuddy-slicer:buildcache`, `mode=max`, so the
  `slicer-build` stage's cargo-chef dependency layer is kept. Only `build-image.yml`'s
  `slicer` job writes it; `ci.yml`'s `slicer-image` and `hot-path-e2e.yml` only read it,
  which works on fork PRs too because the package is public.
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

The Go and Rust commands and toolchain notes: `tools/go/CLAUDE.md`, `workers/rust/CLAUDE.md`.

## PR conventions

- Conventional-commit titles (`feat(scope):`, `fix(scope):`, `docs:`, `ci:` …);
  Release Drafter labels and groups PRs by title. Body links the issue: `Fixes #N`.
- Agent-authored changes and docs cite their sources (#259): every external fact gets
  a link, every measured fact says where and how it was measured (spec §3).
  `lint-plugin.sh` checks it for plugin skills only; `link-check.yml` checks weekly that
  the links in `docs/`, `README.md`, this file, every per-directory `CLAUDE.md` and every `SKILL.md` still resolve.
- Required checks on `main`: **`CI Summary`** and **`claude-review`** (the ruleset
  lives in eh-homelab/clusters, so renaming either job breaks the gate silently).
- `claude-review` is a merge gate: the review runs after CI and sorts its findings into
  `## Blocking` and `## Non-blocking`; a classifier passes only when every Blocking
  finding in the review for *this* commit is fixed or tracked in an open `pr-feedback`
  issue for the PR. Non-blocking findings never gate. A comment or doc that **this diff
  made false** (the PR changed a value and left text describing the old one) is Blocking
  when the correct text is unambiguous, listed with every copy grep finds in one finding
  and raised once; phrasing never blocks. Cosmetic details, phrasing nits, pre-existing
  problems the diff neither causes nor exposes, and checks the review could not run are
  left out of both sections (the last on one `Unverified:` line). Anything the diff
  causes is in scope wherever it surfaces, unchanged code included. A false statement
  the diff **adds** blocks too, not only one it leaves stale (#5304 merged a diagram that
  said "reads" for a grant that also writes, listed twice as Non-blocking). A concrete
  defect is never hedged down to Non-blocking with "if that matters" (#5303 and #5306
  did that to user-visible async races). The review reports every Blocking finding in
  one pass and marks one it missed earlier ("missed in the last review"). On #5565 a
  defect in the first commit surfaced only in round 2, and #5603 passed two reviews of
  one commit before a third blocked it. Ported from eh-homelab/clusters#2989 and #3055,
  where those stale comments had been listed as Non-blocking on almost every PR and
  never fixed after merge. A person adding the
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
