# CI and deploy

A map of the GitHub Actions workflows and of the path an image takes to prod. The rules
(runners, caches, the buildx mirror, pins, required checks) are in the root
[`CLAUDE.md`, "CI and caching rules"](../../CLAUDE.md#ci-and-caching-rules) and
["PR conventions"](../../CLAUDE.md#pr-conventions); the operator's view of a deploy is
in [`README.md`, "Deploying"](../../README.md#deploying). This page does not repeat them.

All line numbers are for `main` at the commit that adds this page.

## Images

One `Dockerfile` builds every image. `build-image.yml` publishes five of its stages to
GHCR, amd64 only:

| Dockerfile stage | Image | Entry | Job |
|---|---|---|---|
| `runtime` (`Dockerfile:943`) | `ghcr.io/eh-homelab/scadbuddy` | `tini` then `uvicorn --factory scadbuddy.main:create_app` (`Dockerfile:961`, `:987`); the same image runs the Python workers, the storefront and the intake worker with another command ([processes.md](processes.md)) | `build` (`.github/workflows/build-image.yml:53`) |
| `agent` (`Dockerfile:233`) | `ghcr.io/eh-homelab/scadbuddy-agent` | `node --import ./dist/telemetry.js dist/main.js` (`Dockerfile:289`) | `agent` (`build-image.yml:216`) |
| `sandbox` (`Dockerfile:307`) | `ghcr.io/eh-homelab/scadbuddy-sandbox` | `node dist/sandbox/main.js` (`Dockerfile:366`), role `exec` or `egress` from `SCADBUDDY_SANDBOX_ROLE` (`Dockerfile:300-306`) | `sandbox` (`build-image.yml:432`) |
| `agent-durable` (`Dockerfile:373`) | `ghcr.io/eh-homelab/scadbuddy-agent-durable` | `python -m scadbuddy_durable` (`Dockerfile:435`) | `agent-durable` (`build-image.yml:328`) |
| `slicer` (`Dockerfile:551`) | `ghcr.io/eh-homelab/scadbuddy-slicer` | `scadbuddy-slicer` (`Dockerfile:604`) | `slicer` (`build-image.yml:543`) |

`openscad-lsp` (`Dockerfile:623`) and `libraries` (`Dockerfile:685`) are internal stages
copied into `app`; `test` (`Dockerfile:863`) is the CI image. Only `build` gates the
workflow: the other four jobs are `continue-on-error` (for example `build-image.yml:220`).

The Go CLIs are published by `build-go-image.yml`, one image per `tools/go/*` module with
a `main.go` (`build-go-image.yml:46`, `:133`): today only `scadbuddy-bambuddy-fs`. Nothing
deploys it ([`tools/go/CLAUDE.md`](../../tools/go/CLAUDE.md)).

## How an image reaches prod

```mermaid
sequenceDiagram
    participant Main as push to main
    participant BI as build-image.yml
    participant GHCR
    participant DM as deploy-main.yml
    participant DR as deploy.reusable.yml
    participant CL as eh-homelab/clusters
    participant Argo as Argo CD
    Main->>BI: push (or v* tag)
    BI->>GHCR: 5 images, tags main, sha-<7>, version
    BI-->>DM: workflow_run completed (main only)
    DM->>GHCR: digest of scadbuddy:sha-<7>
    DM->>DR: digest, version, revision
    DR->>GHCR: agent, agent-durable, slicer, sandbox digests
    DR->>CL: PR on deploy/scadbuddy, image@sha256 pins, auto-merge armed
    CL->>Argo: merge (after clusters' own checks)
    Argo->>Argo: sync the scadbuddy Application
```

1. `build-image.yml` runs on a push to `main`, a `v*` tag or a dispatch, never on a PR
   (`build-image.yml:36-40`). Every job tags `main`, `sha-<7>`, `{{version}}` and
   `{{major}}.{{minor}}` (`build-image.yml:101-105`).
2. `deploy-main.yml` runs on the `workflow_run` of a successful `main` build
   (`.github/workflows/deploy-main.yml:25-29`, `:45-48`), resolves the API image's digest
   (`:67-77`) and calls the reusable deploy (`:82-89`). Disabling this workflow switches
   continuous deploy off ([README](../../README.md#switching-continuous-deploy-off)).
3. `deploy-release.yml` runs when a release is published (`deploy-release.yml:19-21`),
   waits for the tag's image (`:73-108`), calls the same reusable deploy (`:113-120`) and
   records the deploy in the release notes (`:143-184`).
4. `deploy.reusable.yml` is the one deploy mechanism. It checks out
   `eh-homelab/clusters` (`deploy.reusable.yml:109`) with the runner App's token and
   rewrites each image line to `image@sha256:digest`, plus the version, revision and
   source annotations (`:567-573`), in the API manifest (required) and, where they exist,
   the render, print, projects, storefront, intake, agent-worker, agent-durable, slicer
   and sandbox manifests (`:108-150`, `:784-890`). It also moves the Grafana dashboards'
   `?ref=` pin (`:700-767`). It fails on any `ghcr.io/eh-homelab/scadbuddy*` reference
   it did not pin (`:892-919`), then opens or replaces the PR on the fixed branch
   `deploy/scadbuddy` and arms auto-merge (`:931-1052`). Runs share one concurrency
   group that is never cancelled (`:160-175`).
5. Once the PR merges, Argo CD syncs the `scadbuddy` Application. A workflow in
   eh-homelab/clusters then checks `/healthz` for the pinned revision. Nothing in this
   repository talks to the cluster (`deploy.reusable.yml:61-66`).

How to cut a release, read a deploy or deploy by hand:
[README "Cutting a release"](../../README.md#cutting-a-release),
["Reading a deploy"](../../README.md#reading-a-deploy),
["Manual fallback"](../../README.md#manual-fallback).

## CI lanes (`ci.yml`)

`ci.yml` runs on PRs, the merge queue, pushes to `main`, a weekly schedule and dispatch
(`.github/workflows/ci.yml:48-67`). The required check is its `summary` job, **CI
Summary**, which needs every job except `lfs-fixtures` and `dispatch-review`
(`ci.yml:2410-2414`). Lane selection (`layout`,
`select-lanes.sh`, `go-select`) is described in
[`CLAUDE.md`](../../CLAUDE.md#ci-and-caching-rules).

| Lane | Jobs (`ci.yml`) | What it covers | Area notes |
|---|---|---|---|
| layout | `layout` (`:95`) | reads the changed paths and picks the lanes | root `CLAUDE.md` |
| lint | `lint` (`:195`) | actionlint, hadolint, shellcheck, the `lint-*.sh` scripts and every `.github/scripts/*.test.sh`, including the deploy workflow's shell tests (`:313`, `:318`) | root `CLAUDE.md` "Workflow/Dockerfile lint" |
| frontend | `frontend` (`:435`), `frontend-e2e` (`:524`), `frontend-e2e-split` (`:639`) | typecheck, vitest, Playwright shards | [`frontend/CLAUDE.md`](../../frontend/CLAUDE.md) |
| agent | `agent` (`:690`) | the agent's tests; builds the `agent` (`:793`) and `sandbox` (`:842`) targets | [`agent/CLAUDE.md`](../../agent/CLAUDE.md) |
| agent_durable | `agent-durable` (`:879`) | builds the `agent-durable` target (`:955`) | [`agent-durable/CLAUDE.md`](../../agent-durable/CLAUDE.md) |
| image | `image` (`:988`) | builds `test` and `runtime`, backend checks, a container smoke test, the real-backend e2e | [`backend/CLAUDE.md`](../../backend/CLAUDE.md) |
| backend | `lfs-fixtures` (`:1318`), `backend-pytest` (`:1350`), `backend-pytest-split` (`:1536`) | pytest in groups against Postgres and Temporal | [`backend/CLAUDE.md`](../../backend/CLAUDE.md) |
| models | `models` (`:1640`) | `verify-models.sh` over the changed templates on the `base` stage | [`models/CLAUDE.md`](../../models/CLAUDE.md) |
| go | `go-select` (`:1784`), `go` (`:1807`, calls `go-module-ci.yml`) | gofmt, vet, build, `test -race`, vendor drift, CLI images | [`tools/go/CLAUDE.md`](../../tools/go/CLAUDE.md) |
| rust | `rust` (`:1819`), `slicer-image` (`:1896`) | the Rust workspace and the `slicer` target | [`workers/rust/CLAUDE.md`](../../workers/rust/CLAUDE.md) |
| freshness | `freshness` (`:1995`), `go-client` (`:2382`) | the generated API files ([data.md](data.md#generated-api-files)) and the Go client | root `CLAUDE.md` "Generated API files" |
| review | `dispatch-review` (`:2573`) | dispatches `claude-code-review.yml`, which posts the `claude-review` status | root `CLAUDE.md` "PR conventions" |

Outside `ci.yml`:

- `hot-path-e2e.yml` builds `runtime`, `test` and `slicer` and runs the real stack
  (`e2e-stack/run.sh --mode both`, then `--auth`) on PRs touching `e2e-stack/**` or
  `backend/**`, weekly and on dispatch (`.github/workflows/hot-path-e2e.yml:29-40`,
  `:194-200`). It is not part of CI Summary. See
  [`e2e-stack/CLAUDE.md`](../../e2e-stack/CLAUDE.md).
- `merge-queue-review.yml` carries the `claude-review` verdict onto a merge-queue commit.
- `ai-evals.yml` runs the agent's live evals on dispatch only ([`docs/ai/evals.md`](../ai/evals.md)).

## Scheduled and bot workflows

| Workflow | Trigger | Does |
|---|---|---|
| `openscad-bump.yml` | weekly | opens a PR moving the OpenSCAD nightly (never auto-merged) |
| `postgres-pin.yml`, `rust-toolchain-pin.yml`, `harness-pin.yml` | weekly | check pinned digests and SHAs against upstream |
| `link-check.yml` | weekly, dispatch | lychee over `docs/`, `README.md`, every `CLAUDE.md` and `SKILL.md` |
| `ghcr-cleanup.yml` | monthly | prunes untagged versions of the `scadbuddy` package only (`ghcr-cleanup.yml:63`) |
| `release-drafter.yml` | push to main, PRs | drafts the release notes and labels PRs by title |
| `claude.yml`, `issue-intake.yml`, `pr-follow-up-issues.yml` | comments, issues, a label | the `@claude` bot, issue triage, follow-up issues |
| `vendor-autofix.yml` | Dependabot gomod PRs | regenerates `vendor/` |
| `cancel-pr-workflows.yml` | PR closed | cancels its runs |

## Observability

- Six Grafana dashboards live in [`deploy/grafana/`](../../deploy/grafana/) as ConfigMaps
  (`deploy/grafana/kustomization.yaml:29-77`); see
  [`deploy/CLAUDE.md`](../../deploy/CLAUDE.md). `lint-dashboard.sh` checks them in the
  `lint` job.
- Traces: the API, the Python workers, the agent and the slicer export OTLP when the
  standard `OTEL_*` variables are set; browser spans are relayed through the API.
  See [README "Tracing"](../../README.md#tracing-988).
