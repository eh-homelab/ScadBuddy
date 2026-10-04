# Tracing, dashboard and its pin (PR #5 of #988) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ScadBuddy's Grafana dashboard lives in this repo as `deploy/grafana/`
(a kustomize directory that generates a sidecar-loaded ConfigMap in
`cattle-dashboards`), CI lints it with a pinned kustomize, and every deploy PR moves
the clusters overlay's `?ref=` for it to the same revision as the image digest, when
clusters has that line.

**Architecture:** `deploy/grafana/scadbuddy.json` (uid `scadbuddy`) is a small
hand-written dashboard: Prometheus panels over the existing `core/metrics.py` series
and Tempo TraceQL tables, every datasource a dashboard variable (`DS_PROMETHEUS`,
`DS_TEMPO` defaulting to uid `tempo`). `.github/scripts/lint-dashboard.sh` checks it
(JSON, uid, datasource variables, every `scadbuddy_*` series against `metrics.py`,
every span name against the backend source, the kustomize build) and has its own
`.test.sh`. The `edit` step of `deploy.reusable.yml` gains `pin_dashboard`, which
counts the anchored resource line in `clusters/prod/scadbuddy/kustomization.yaml`
and either skips with a notice (not configured), rewrites it (one), or fails (a near
miss, or more than one); `deploy-pin.test.sh` runs the step's own script against
fixtures for every outcome.

**Tech Stack:** Grafana dashboard JSON (schemaVersion 42, Grafana 12.3 as in
clusters' bambuddy dashboard), kustomize v5.6.0 (what ArgoCD's repo-server runs),
bash, jq, mikefarah yq v4, shellcheck, actionlint, GitHub Actions on `ubuntu-latest`.

**Spec:** `docs/superpowers/specs/2026-10-01-distributed-tracing-design.md` (§7 is
this PR; §3 for service names, §5.1/§5.4/§6 for span names and attributes, §5.2's
"trace panels filter on server-side services", §9 row 5). Rows 1–4 have their own
plans; row 1 (`docs/superpowers/plans/2026-10-02-tracing-backend-core.md`) is the
backend this dashboard queries.

## Global Constraints

- Dashboard uid is `scadbuddy`; it never changes after the first deploy (§7).
- The ConfigMap is labelled `grafana_dashboard: "1"` and lands in `cattle-dashboards`, the namespace the rancher-monitoring sidecar watches (§7).
- Datasources are dashboard variables (Prometheus, and Tempo defaulting to uid `tempo` from clusters#1596), never hard-coded uids (§7).
- Panels that need Tempo's metrics-generator (TraceQL metrics, service graph) are left out (§7).
- Trace panels filter on server-side services (`scadbuddy-api`, `scadbuddy-worker`, `scadbuddy-agent`); only the Browser panel reads `scadbuddy-web` (§5.2, §7).
- Service names are exactly `scadbuddy-api`, `scadbuddy-worker`, `scadbuddy-agent`, `scadbuddy-web` (§3).
- Backend span names (row 1, verified in `backend/scadbuddy`): `render.<RenderStage>` for `source`, `render`, `split`, `solids`, `thumbnail`, `write` (`render/jobs.py` `_traced_stage`); `render.solid` (`scadbuddy.colour_index`); `openscad.export` (`scadbuddy.openscad.format`, `.backend`, `.exit_code`); `render.submit` (`scadbuddy.slug`, `scadbuddy.job_id`, `scadbuddy.coalesced`); `render.preview`; `render.reconcile`; `bambuddy.<METHOD>` CLIENT spans (`scadbuddy.bambuddy.scope`); `scadbuddy.failure_class` on errors.
- Metrics are read as `prometheus_client` exposes them: counters with `_total`, histograms with `_bucket`/`_sum`/`_count`; names must not move (§1 non-goals).
- The deploy anchor is exactly `^\s*- https://github\.com/eh-homelab/ScadBuddy//deploy/grafana\?ref=[0-9a-f]{40}$`; both counts use `n=$(grep -c… "$file" || true)` (§7).
- The `ref` is rewritten to the same `REVISION` stamped into the image, in the same deploy PR (§7).
- kustomize in CI is a pinned release whose published sha256 is checked before use; nothing relies on the runner image's kustomize (§7).
- Every job runs on `ubuntu-latest`; never a self-hosted pool. No new buildx cache scope (CLAUDE.md "CI and caching rules").
- `shellcheck .github/scripts/*.sh models/*/verify.sh` and actionlint (`-ignore create-github-app-token`) must stay clean (CLAUDE.md "Commands").
- Commits are conventional and end with `(#988)`; the PR body says `Refs #988` (the epic stays open for its other rows until all land).

## Review Focus

1. **No Tempo datasource yet** (clusters#1596 Phase 4 not done): the dashboard loads, the `namespace` variable and the Prometheus panels that read API or queue series work, and the trace tables are empty. Prometheus panels that read worker series (stage time, outcomes, submit to settled) stay empty until clusters#1596 Phase 5 widens the worker ServiceMonitor's keep list. Pinned by the lint rule "a PromQL target or query variable uses `${DS_PROMETHEUS}`, a TraceQL target `${DS_TEMPO}`" and its two cases in Task 1.
2. **Clusters adds the dashboard line after the last deploy** (with a placeholder SHA), then the same digest deploys again: the deploy PR moves only the ref, instead of answering "nothing to deploy". Test case 11 in Task 3.
3. **The line is commented out** in clusters (someone disabling the dashboard by hand): the deploy stops with an error naming the line, not a silent skip that leaves the old build's dashboard. Test case "commented out" in Task 3.
4. **The overlay file is gone or moved** in clusters: the deploy fails saying so, rather than reading a missing file as "not configured". Test case 19 in Task 3.
5. **A row 3 agent ships span names that differ from the spec** (`agent.turn`, `agent.approval`): the lint fails on the real repo once `agent/src/telemetry.ts` exists, instead of leaving empty agent panels. Test cases "agent span names absent from agent/src fail once it traces" / "… present … pass" in Task 1.

---

## File structure

| File | Responsibility |
|---|---|
| `deploy/grafana/kustomization.yaml` (new) | `configMapGenerator`: ConfigMap `scadbuddy-dashboard` in `cattle-dashboards`, label `grafana_dashboard: "1"`, no hash suffix |
| `deploy/grafana/scadbuddy.json` (new) | The dashboard: rows Renders, HTTP, Agent, Browser |
| `.github/scripts/lint-dashboard.sh` (new) | The dashboard lint, `[repo-root]`, `$KUSTOMIZE` |
| `.github/scripts/lint-dashboard.test.sh` (new) | Fixture cases for the lint, run on a copy of the real dashboard and backend |
| `.github/workflows/ci.yml` (modify, `lint` job) | Install pinned kustomize 5.6.0, run the lint's tests and the lint |
| `.github/workflows/deploy.reusable.yml` (modify) | `pin_dashboard` in the `edit` step, `dashboard` output, PR/commit text |
| `.github/scripts/deploy-pin.test.sh` (modify) | Overlay fixture, `rerun`, the dashboard cases |
| `README.md`, `CLAUDE.md` (modify) | Deploying, Tracing, the lint job and Layout |

Decisions settled here, with their reasons:

- **kustomize v5.6.0**, not the newest (v5.8.2, 2026-09-30): clusters'
  `clusters/prod/bambuddy/kustomization.yaml` records that ArgoCD's repo-server runs
  5.6.0, and the lint should build what production builds. Its sha256 (from the
  release's `checksums.txt`) is
  `54e4031ddc4e7fc59e408da29e7c646e8e57b8088c51b84b3df0864f47b5148f`; pinned in the
  workflow rather than fetched at run time from the same release (a pin also catches
  a replaced asset).
- **Namespace on the generator entry**, as clusters' bambuddy overlay does. Verified
  with 5.6.0: an overlay with the `unsetOnly` NamespaceTransformer keeps
  `cattle-dashboards`; one with a plain `namespace: bambuddy` rewrites it (which is
  why §7 asks clusters#1596 Phase 5 for the transformer).
- **The lint reads the backend's source**, not a running backend: the `lint` job
  has no uv or Postgres, and the `test` image does not contain `deploy/`, so a
  pytest could not see the dashboard. Span names are not constants in the backend
  (row 1 writes literals and `f"render.{name}"`), so the lint collects them: the
  `RenderStage` literal in `core/metrics.py` and every string literal passed to
  `span(` / `detached_span(` under `backend/scadbuddy`.
- **Agent and browser span names** are checked against `agent/src` / `frontend/src`
  only once those services' tracing modules exist (`agent/src/telemetry.ts`,
  `frontend/src/lib/tracing.ts`, the paths §5.3/§5.4 name); until then the lint
  prints a `::notice::` that they are unchecked. Row 5 does not wait for rows 3–4
  (§9), and the check turns strict by itself when they land.
- **Span names are matched exactly** (`name="…"`, never `name=~`), so the lint can
  check every one. Where §7 wants "tool count", the Agent panel uses the structural
  query `{…name="agent.turn"} > {resource.service.name="scadbuddy-agent"}`: each
  turn's row expands to its child spans (its tool calls). TraceQL search cannot put
  a count in a column without TraceQL metrics, which §7 excludes.
- **"Slowest and failed first"** is two tables beside the render metrics: *Slowest
  renders* (the §7 query verbatim, table sorted by Duration descending) and *Failed
  renders* (the same with `&& status=error`, selecting `scadbuddy.failure_class`).
- **A missing overlay file is an error**, not "not configured": §7 says the file
  exists; if clusters moves it, a deploy that skipped would leave the dashboard
  stale with nothing but a notice.
- **`namespace` variable** (from `scadbuddy_build_info`, default `bambuddy`): the
  same filter clusters' `app-alerts.yaml` uses, so a second deployment never mixes in.

---

### Task 1: The dashboard, its kustomization and its lint

**Files:**
- Create: `deploy/grafana/kustomization.yaml`
- Create: `deploy/grafana/scadbuddy.json`
- Create: `.github/scripts/lint-dashboard.sh`
- Test: `.github/scripts/lint-dashboard.test.sh`

**Interfaces:**
- Consumes: `backend/scadbuddy/core/metrics.py` (`RenderStage = Literal[...]` on one
  line, metric names as `"scadbuddy_…"` literals); span literals in
  `backend/scadbuddy/**/*.py`.
- Produces (Task 2 runs them): `.github/scripts/lint-dashboard.sh [repo-root]`, exit 0
  clean / 1 problems / 2 usage, problems printed as `deploy/grafana/<file>: <text>`,
  `::notice::` for unchecked services; reads `$KUSTOMIZE` (default `kustomize`).
  `.github/scripts/lint-dashboard.test.sh`, exit 0 when every case passes. ConfigMap
  `scadbuddy-dashboard` / namespace `cattle-dashboards` (Task 4 documents them).

- [ ] **Step 1: Get kustomize 5.6.0 locally (the version CI will pin)**

```bash
mkdir -p /tmp/kustomize-5.6.0 && cd /tmp/kustomize-5.6.0
curl -fsSLo k.tgz "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv5.6.0/kustomize_v5.6.0_linux_amd64.tar.gz"
echo "54e4031ddc4e7fc59e408da29e7c646e8e57b8088c51b84b3df0864f47b5148f  k.tgz" | sha256sum -c -
tar -xzf k.tgz kustomize && ./kustomize version
export KUSTOMIZE=/tmp/kustomize-5.6.0/kustomize
```

Expected: `k.tgz: OK`, then `v5.6.0`. Keep `KUSTOMIZE` exported for every later step
(the commands below spell it out anyway). jq, mikefarah yq v4 and shellcheck must be
on PATH (`yq --version` prints `mikefarah`).

- [ ] **Step 2: Write the failing test**

Create `.github/scripts/lint-dashboard.test.sh` (then `chmod +x` it):

```bash
#!/usr/bin/env bash
# Tests for lint-dashboard.sh, the CI check on deploy/grafana/ (#988, spec
# 2026-10-01 §7). Each case copies this repo's real dashboard and backend
# sources into a temp tree, breaks one thing, and asserts the lint both fails
# and names the problem. The untouched copy must pass, or every failure below
# could be the lint failing on everything; that case is also the check that
# the real dashboard queries only series and spans the real backend has.
#
# Runs in ci.yml's `lint` job after the pinned kustomize is installed
# ($KUSTOMIZE, as for the lint itself). Needs jq and mikefarah yq v4.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
script="$here/lint-dashboard.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0

check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    printf 'ok   %s\n' "$name"
    pass=$((pass + 1))
  else
    printf 'FAIL %s\n       expected: %s\n       actual:   %s\n' "$name" "$expected" "$actual"
    fail=$((fail + 1))
  fi
}

# One pristine copy, made once; good() resets the case tree from it.
mkdir -p "$tmp/pristine/deploy" "$tmp/pristine/backend"
cp -R "$repo/deploy/grafana" "$tmp/pristine/deploy/"
cp -R "$repo/backend/scadbuddy" "$tmp/pristine/backend/"
r="$tmp/repo"
good() {
  rm -rf "$r"
  cp -R "$tmp/pristine" "$r"
}

dash="$r/deploy/grafana/scadbuddy.json"
# edit <jq filter>: rewrite the case tree's dashboard.
edit() {
  jq "$1" "$dash" > "$dash.new" && mv "$dash.new" "$dash"
}
# The id of the first panel whose target runs a TraceQL query naming $1.
tempo_panel() {
  jq -r --arg s "$1" '[.panels[] | select(any(.targets[]?; .query? // "" | contains($s))) | .id][0]' "$dash"
}

# run: "<exit>:<problem lines, '|'-joined>", so a case asserts on both.
run() {
  local out status
  set +e
  out="$("$script" "$r" 2>&1)"
  status=$?
  set -e
  printf '%s:%s' "$status" "$(printf '%s\n' "$out" | grep -E '^deploy/grafana/' | paste -sd'|' -)"
}

f=deploy/grafana/scadbuddy.json
k=deploy/grafana/kustomization.yaml

good
check 'the real dashboard passes against the real backend' '0:' "$(run)"

good
printf '{"uid": "scadbuddy",' > "$dash"
check 'invalid JSON fails' "1:$f: missing or not valid JSON" "$(run)"

good
edit '.uid = "scadbuddy-v2"'
check 'another uid fails' "1:$f: uid must be \"scadbuddy\", not \"scadbuddy-v2\"" "$(run)"

good
edit '.panels[2].id = .panels[1].id'
id=$(jq -r '.panels[1].id' "$dash")
check 'a duplicated panel id fails' "1:$f: panel id $id is used more than once" "$(run)"

good
edit '(.panels[] | select(.id == 2) | .datasource.uid) = "prometheus"'
check 'a hard-coded panel datasource fails' \
  "1:$f: panel 2 (Queue): datasource must be a datasource variable (\${DS_PROMETHEUS} or \${DS_TEMPO}), not \"prometheus\"" \
  "$(run)"

good
# shellcheck disable=SC2016 # a literal ${DS_LOKI}, for jq
edit '(.panels[] | select(.id == 2) | .datasource.uid) = "${DS_LOKI}"'
check 'an undeclared datasource variable fails' \
  "1:$f: panel 2 (Queue): datasource must be a datasource variable (\${DS_PROMETHEUS} or \${DS_TEMPO}), not \"\${DS_LOKI}\"" \
  "$(run)"

good
# shellcheck disable=SC2016 # a literal ${DS_TEMPO}, for jq
edit '(.panels[] | select(.id == 2) | .targets[0].datasource.uid) = "${DS_TEMPO}"'
check 'a PromQL target on the Tempo datasource fails' \
  "1:$f: panel 2 target A: must use \${DS_PROMETHEUS}, not \"\${DS_TEMPO}\"" \
  "$(run)"

good
# shellcheck disable=SC2016 # a literal ${DS_TEMPO}, for jq
edit '(.templating.list[] | select(.name == "namespace") | .datasource.uid) = "${DS_TEMPO}"'
check 'a query variable on the Tempo datasource fails' \
  "1:$f: variable namespace: must use \${DS_PROMETHEUS}, not \"\${DS_TEMPO}\"" \
  "$(run)"

good
edit '(.templating.list[] | select(.name == "DS_TEMPO") | .current.value) = "P214B5B846CF3925F"'
check 'a Tempo default other than uid tempo fails' \
  "1:$f: DS_TEMPO must default to uid \"tempo\" (clusters#1596), not \"P214B5B846CF3925F\"" \
  "$(run)"

good
edit '(.panels[] | select(.id == 2) | .targets[0].expr) = "max(scadbuddy_render_queue_lenght)"'
check 'a series the backend does not declare fails' \
  "1:$f: scadbuddy_render_queue_lenght is not a metric backend/scadbuddy/core/metrics.py declares" \
  "$(run)"

good
p=$(tempo_panel 'render.render')
edit "(.panels[] | select(.id == $p) | .targets[0].query) = \"{resource.service.name=\\\"scadbuddy-worker\\\" && name=\\\"render.openscad\\\"}\""
check 'a span name the backend never emits fails' \
  "1:$f: panel $p target A: scadbuddy-worker emits no span named \"render.openscad\"" \
  "$(run)"

good
p=$(tempo_panel 'render.render')
edit "(.panels[] | select(.id == $p) | .targets[0].query) = \"{name=\\\"render.render\\\"}\""
check 'a TraceQL query without a service filter fails' \
  "1:$f: panel $p target A: a TraceQL query must filter on resource.service.name" \
  "$(run)"

good
p=$(tempo_panel 'render.render')
edit "(.panels[] | select(.id == $p) | .targets[0].query) = \"{resource.service.name=\\\"scadbuddy-render\\\"}\""
check 'an unknown service.name fails' \
  "1:$f: panel $p target A: \"scadbuddy-render\" is not a ScadBuddy service.name" \
  "$(run)"

good
p=$(tempo_panel 'render.render')
edit "(.panels[] | select(.id == $p) | .targets[0].query) = \"{resource.service.name=\\\"scadbuddy-worker\\\" && name=~\\\"render.*\\\"}\""
check 'a span-name regex fails' \
  "1:$f: panel $p target A: match span names exactly (name=\"…\"), so this lint can check them" \
  "$(run)"

# The agent's span names are checked once its tracing module exists (row 3).
good
mkdir -p "$r/agent/src"
: > "$r/agent/src/telemetry.ts"
turn=$(tempo_panel 'agent.turn')
approval=$(tempo_panel 'agent.approval')
check 'agent span names absent from agent/src fail once it traces' \
  "1:$f: panel $turn target A: scadbuddy-agent emits no span named \"agent.turn\"|$f: panel $approval target A: scadbuddy-agent emits no span named \"agent.approval\"" \
  "$(run)"

good
mkdir -p "$r/agent/src"
: > "$r/agent/src/telemetry.ts"
printf 'export const TURN = "agent.turn";\nexport const APPROVAL = "agent.approval";\n' > "$r/agent/src/spans.ts"
check 'agent span names present in agent/src pass' '0:' "$(run)"

good
out="$("$script" "$r" 2>&1)" || true
check 'agent span names are unchecked, with a notice, before the agent traces' \
  'yes' "$(grep -q '^::notice::scadbuddy-agent has no tracing module' <<< "$out" && echo yes || echo no)"

good
yq -i '.configMapGenerator[0].options.labels = {}' "$r/$k"
check 'a ConfigMap without the sidecar label fails' \
  "1:$k: must build exactly one ConfigMap scadbuddy-dashboard in cattle-dashboards labelled grafana_dashboard: \"1\" (got 1 document(s): ConfigMap scadbuddy-dashboard cattle-dashboards )" \
  "$(run)"

good
yq -i 'del(.configMapGenerator[0].namespace)' "$r/$k"
check 'a ConfigMap outside cattle-dashboards fails' \
  "1:$k: must build exactly one ConfigMap scadbuddy-dashboard in cattle-dashboards labelled grafana_dashboard: \"1\" (got 1 document(s): ConfigMap scadbuddy-dashboard  1)" \
  "$(run)"

good
yq -i '.resources = ["missing.yaml"]' "$r/$k"
check 'a kustomization that does not build fails' '1' "$(run | cut -d: -f1)"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
```

- [ ] **Step 3: Run it to verify it fails**

Run: `chmod +x .github/scripts/lint-dashboard.test.sh && KUSTOMIZE=/tmp/kustomize-5.6.0/kustomize .github/scripts/lint-dashboard.test.sh`
Expected: exits non-zero at once with `cp: cannot stat '…/deploy/grafana': No such file or directory` (neither the dashboard nor the lint exists yet).

- [ ] **Step 4: Write the kustomization**

Create `deploy/grafana/kustomization.yaml`:

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

# ScadBuddy's Grafana dashboard (spec 2026-10-01 §7, #988), as a ConfigMap the
# rancher-monitoring Grafana's dashboard sidecar loads: it watches
# `cattle-dashboards` for the `grafana_dashboard: "1"` label.
#
# eh-homelab/clusters' clusters/prod/scadbuddy overlay pulls this directory in
# as a remote resource pinned to a full commit SHA, and deploy.reusable.yml
# moves that `ref` in the same deploy PR as the image digest, so the dashboard
# shown is the one written for the build that is serving.
#
# The namespace is set on the generator, not with a top-level `namespace:`
# field, as in clusters/prod/bambuddy. The overlay has to namespace its own
# resources with an `unsetOnly` NamespaceTransformer, not a plain `namespace:`
# field, or this ConfigMap is rewritten into the overlay's namespace, where the
# sidecar never looks (clusters#1596 Phase 5).
#
# No hash suffix: the sidecar keys on the dashboard's uid, not the ConfigMap's
# name, and a stable name lets ArgoCD update it in place.
configMapGenerator:
  - name: scadbuddy-dashboard
    namespace: cattle-dashboards
    options:
      disableNameSuffixHash: true
      labels:
        grafana_dashboard: "1"
    files:
      - scadbuddy.json
```

- [ ] **Step 5: Write the dashboard**

Create `deploy/grafana/scadbuddy.json`. Panel ids are unique and the order matters to
the test (panel id 2 is the "Queue" timeseries the test edits):

```json
{
  "uid": "scadbuddy",
  "title": "ScadBuddy",
  "description": "Renders, HTTP, the agent and the browser: Prometheus metrics beside Tempo trace searches. Deployed from eh-homelab/ScadBuddy deploy/grafana at the serving build's revision (spec 2026-10-01 §7).",
  "tags": ["scadbuddy"],
  "editable": false,
  "graphTooltip": 1,
  "schemaVersion": 42,
  "time": { "from": "now-6h", "to": "now" },
  "refresh": "1m",
  "links": [],
  "annotations": { "list": [] },
  "templating": {
    "list": [
      {
        "name": "DS_PROMETHEUS",
        "label": "Prometheus",
        "type": "datasource",
        "query": "prometheus",
        "current": { "text": "Prometheus", "value": "prometheus" },
        "hide": 0,
        "refresh": 1,
        "regex": "",
        "options": []
      },
      {
        "name": "DS_TEMPO",
        "label": "Tempo",
        "description": "Trace panels need a Tempo datasource (clusters#1596 Phase 4); until one exists they show no data and the metric panels are unaffected.",
        "type": "datasource",
        "query": "tempo",
        "current": { "text": "Tempo", "value": "tempo" },
        "hide": 0,
        "refresh": 1,
        "regex": "",
        "options": []
      },
      {
        "name": "namespace",
        "label": "Namespace",
        "type": "query",
        "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
        "definition": "label_values(scadbuddy_build_info, namespace)",
        "query": {
          "query": "label_values(scadbuddy_build_info, namespace)",
          "refId": "namespace"
        },
        "current": { "text": "bambuddy", "value": "bambuddy" },
        "includeAll": false,
        "hide": 0,
        "refresh": 2,
        "regex": "",
        "options": []
      }
    ]
  },
  "panels": [
    {
      "id": 1,
      "type": "row",
      "title": "Renders",
      "collapsed": false,
      "gridPos": { "x": 0, "y": 0, "w": 24, "h": 1 },
      "panels": []
    },
    {
      "id": 2,
      "type": "timeseries",
      "title": "Queue",
      "description": "Jobs waiting and running (read from the queue on every scrape, so the max across pods), against SCADBUDDY_RENDER_QUEUE_DEPTH_SLO.",
      "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
      "gridPos": { "x": 0, "y": 1, "w": 8, "h": 8 },
      "fieldConfig": { "defaults": { "unit": "short", "min": 0 }, "overrides": [] },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "expr": "max(scadbuddy_render_queue_depth{namespace=\"$namespace\"})",
          "legendFormat": "waiting"
        },
        {
          "refId": "B",
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "expr": "max(scadbuddy_render_jobs_running{namespace=\"$namespace\"})",
          "legendFormat": "running"
        },
        {
          "refId": "C",
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "expr": "max(scadbuddy_render_queue_depth_slo{namespace=\"$namespace\"}) > 0",
          "legendFormat": "depth SLO"
        }
      ]
    },
    {
      "id": 3,
      "type": "timeseries",
      "title": "Wait for a worker",
      "description": "Submit until a worker took the job: p50 and p95, and the oldest job still waiting.",
      "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
      "gridPos": { "x": 8, "y": 1, "w": 8, "h": 8 },
      "fieldConfig": { "defaults": { "unit": "s", "min": 0 }, "overrides": [] },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "expr": "histogram_quantile(0.5, sum by (le) (rate(scadbuddy_render_queue_wait_seconds_bucket{namespace=\"$namespace\"}[$__rate_interval])))",
          "legendFormat": "p50"
        },
        {
          "refId": "B",
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "expr": "histogram_quantile(0.95, sum by (le) (rate(scadbuddy_render_queue_wait_seconds_bucket{namespace=\"$namespace\"}[$__rate_interval])))",
          "legendFormat": "p95"
        },
        {
          "refId": "C",
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "expr": "max(scadbuddy_render_queue_oldest_seconds{namespace=\"$namespace\"})",
          "legendFormat": "oldest waiting"
        }
      ]
    },
    {
      "id": 4,
      "type": "timeseries",
      "title": "Submit to settled (p95) against the SLO",
      "description": "What the user waits for, by outcome, against SCADBUDDY_RENDER_LATENCY_SLO.",
      "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
      "gridPos": { "x": 16, "y": 1, "w": 8, "h": 8 },
      "fieldConfig": { "defaults": { "unit": "s", "min": 0 }, "overrides": [] },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "expr": "histogram_quantile(0.95, sum by (le, outcome) (rate(scadbuddy_render_job_latency_seconds_bucket{namespace=\"$namespace\"}[$__rate_interval])))",
          "legendFormat": "p95 {{outcome}}"
        },
        {
          "refId": "B",
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "expr": "max(scadbuddy_render_latency_slo_seconds{namespace=\"$namespace\"}) > 0",
          "legendFormat": "SLO"
        }
      ]
    },
    {
      "id": 5,
      "type": "timeseries",
      "title": "Outcomes",
      "description": "Render jobs leaving the queue, by outcome, and submits refused because the queue was full.",
      "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
      "gridPos": { "x": 0, "y": 9, "w": 8, "h": 8 },
      "fieldConfig": { "defaults": { "unit": "ops", "min": 0 }, "overrides": [] },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "expr": "sum by (outcome) (rate(scadbuddy_render_jobs_finished_total{namespace=\"$namespace\"}[$__rate_interval]))",
          "legendFormat": "{{outcome}}"
        },
        {
          "refId": "B",
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "expr": "sum(rate(scadbuddy_render_jobs_rejected_total{namespace=\"$namespace\"}[$__rate_interval]))",
          "legendFormat": "rejected (queue full)"
        }
      ]
    },
    {
      "id": 6,
      "type": "timeseries",
      "title": "Stage time (p95)",
      "description": "Time in each render stage; render.<stage> spans in the traces below carry the same names.",
      "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
      "gridPos": { "x": 8, "y": 9, "w": 8, "h": 8 },
      "fieldConfig": { "defaults": { "unit": "s", "min": 0 }, "overrides": [] },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "expr": "histogram_quantile(0.95, sum by (le, stage) (rate(scadbuddy_render_stage_seconds_bucket{namespace=\"$namespace\"}[$__rate_interval])))",
          "legendFormat": "{{stage}}"
        }
      ]
    },
    {
      "id": 7,
      "type": "table",
      "title": "Slowest renders",
      "description": "render.render spans on the worker, slowest first. Needs the Tempo datasource (clusters#1596 Phase 4).",
      "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
      "gridPos": { "x": 16, "y": 9, "w": 8, "h": 8 },
      "options": { "sortBy": [{ "displayName": "Duration", "desc": true }] },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
          "queryType": "traceql",
          "query": "{resource.service.name=\"scadbuddy-worker\" && name=\"render.render\"}",
          "limit": 20,
          "spss": 3,
          "tableType": "traces"
        }
      ]
    },
    {
      "id": 8,
      "type": "table",
      "title": "Failed renders",
      "description": "render.render spans that ended in ERROR (the job settled failed); scadbuddy.failure_class names the cause. Needs the Tempo datasource (clusters#1596 Phase 4).",
      "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
      "gridPos": { "x": 0, "y": 17, "w": 24, "h": 8 },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
          "queryType": "traceql",
          "query": "{resource.service.name=\"scadbuddy-worker\" && name=\"render.render\" && status=error} | select(span.scadbuddy.failure_class)",
          "limit": 20,
          "spss": 3,
          "tableType": "traces"
        }
      ]
    },
    {
      "id": 9,
      "type": "row",
      "title": "HTTP",
      "collapsed": false,
      "gridPos": { "x": 0, "y": 25, "w": 24, "h": 1 },
      "panels": []
    },
    {
      "id": 10,
      "type": "timeseries",
      "title": "Requests by route",
      "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
      "gridPos": { "x": 0, "y": 26, "w": 8, "h": 8 },
      "fieldConfig": { "defaults": { "unit": "reqps", "min": 0 }, "overrides": [] },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "expr": "sum by (method, route) (rate(scadbuddy_http_requests_total{namespace=\"$namespace\"}[$__rate_interval]))",
          "legendFormat": "{{method}} {{route}}"
        }
      ]
    },
    {
      "id": 11,
      "type": "timeseries",
      "title": "Duration by route (p95)",
      "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
      "gridPos": { "x": 8, "y": 26, "w": 8, "h": 8 },
      "fieldConfig": { "defaults": { "unit": "s", "min": 0 }, "overrides": [] },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "${DS_PROMETHEUS}" },
          "expr": "histogram_quantile(0.95, sum by (le, method, route) (rate(scadbuddy_http_request_duration_seconds_bucket{namespace=\"$namespace\"}[$__rate_interval])))",
          "legendFormat": "{{method}} {{route}}"
        }
      ]
    },
    {
      "id": 12,
      "type": "table",
      "title": "Slowest API traces",
      "description": "Server spans on the API, slowest first. Needs the Tempo datasource (clusters#1596 Phase 4).",
      "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
      "gridPos": { "x": 16, "y": 26, "w": 8, "h": 8 },
      "options": { "sortBy": [{ "displayName": "Duration", "desc": true }] },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
          "queryType": "traceql",
          "query": "{resource.service.name=\"scadbuddy-api\" && kind=server}",
          "limit": 20,
          "spss": 3,
          "tableType": "traces"
        }
      ]
    },
    {
      "id": 13,
      "type": "row",
      "title": "Agent",
      "collapsed": false,
      "gridPos": { "x": 0, "y": 34, "w": 24, "h": 1 },
      "panels": []
    },
    {
      "id": 14,
      "type": "table",
      "title": "Turns and their tool calls",
      "description": "Recent agent.turn spans; expand a row for its tool calls (the turn's child spans) and their count. A parked turn ends with scadbuddy.outcome=parked. Needs the Tempo datasource (clusters#1596 Phase 4).",
      "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
      "gridPos": { "x": 0, "y": 35, "w": 8, "h": 8 },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
          "queryType": "traceql",
          "query": "{resource.service.name=\"scadbuddy-agent\" && name=\"agent.turn\"} > {resource.service.name=\"scadbuddy-agent\"}",
          "limit": 20,
          "spss": 20,
          "tableType": "traces"
        }
      ]
    },
    {
      "id": 15,
      "type": "table",
      "title": "Approvals",
      "description": "agent.approval spans: each human decision on a parked call, linked to the call it decided. Needs the Tempo datasource (clusters#1596 Phase 4).",
      "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
      "gridPos": { "x": 8, "y": 35, "w": 8, "h": 8 },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
          "queryType": "traceql",
          "query": "{resource.service.name=\"scadbuddy-agent\" && name=\"agent.approval\"} | select(span.scadbuddy.outcome)",
          "limit": 20,
          "spss": 3,
          "tableType": "traces"
        }
      ]
    },
    {
      "id": 16,
      "type": "table",
      "title": "Agent errors",
      "description": "Agent spans that ended in ERROR. Needs the Tempo datasource (clusters#1596 Phase 4).",
      "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
      "gridPos": { "x": 16, "y": 35, "w": 8, "h": 8 },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
          "queryType": "traceql",
          "query": "{resource.service.name=\"scadbuddy-agent\" && status=error}",
          "limit": 20,
          "spss": 3,
          "tableType": "traces"
        }
      ]
    },
    {
      "id": 17,
      "type": "row",
      "title": "Browser",
      "collapsed": false,
      "gridPos": { "x": 0, "y": 43, "w": 24, "h": 1 },
      "panels": []
    },
    {
      "id": 18,
      "type": "table",
      "title": "Browser actions",
      "description": "Recent traces that start in the browser; Name is the root span, the action (Generate, Print, Send, page load). Browser spans are untrusted input (spec §5.2): every other panel filters on server-side services. Needs the Tempo datasource (clusters#1596 Phase 4).",
      "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
      "gridPos": { "x": 0, "y": 44, "w": 24, "h": 8 },
      "options": { "sortBy": [{ "displayName": "Name", "desc": false }] },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "tempo", "uid": "${DS_TEMPO}" },
          "queryType": "traceql",
          "query": "{resource.service.name=\"scadbuddy-web\"}",
          "limit": 50,
          "spss": 3,
          "tableType": "traces"
        }
      ]
    }
  ]
}
```

Notes on the queries, so a reviewer can check them against the code:
- Queue gauges are read from the queue on every scrape (`max`, not `sum`, as clusters'
  `app-alerts.yaml` does); SLO gauges are `> 0` so an unset SLO draws nothing.
- Counters carry `_total` and histograms `_bucket` on the wire.
- `render.render` is the openscad stage on the worker (`timed_stage("render")`), so
  *Slowest renders* is §7's query verbatim. A one-process dev run
  (`SCADBUDDY_TEMPORAL_WORKER_INPROCESS`) reports as `scadbuddy-api` and leaves it
  empty; production runs the worker apart.

- [ ] **Step 6: Write the lint**

Create `.github/scripts/lint-dashboard.sh` (then `chmod +x` it):

```bash
#!/usr/bin/env bash
#
# Lint ScadBuddy's Grafana dashboard, deploy/grafana/ (spec
# docs/superpowers/specs/2026-10-01-distributed-tracing-design.md §7, #988).
# Used by the `lint` job in ci.yml; runs the same locally:
#
#   KUSTOMIZE=/path/to/kustomize .github/scripts/lint-dashboard.sh [repo-root]
#
# Checks, each problem printed as `<file>: <problem>`:
#
#   - scadbuddy.json is JSON, its uid is `scadbuddy`, and panel ids are unique
#   - it declares the datasource variables DS_PROMETHEUS (type prometheus) and
#     DS_TEMPO (type tempo, defaulting to uid `tempo`, clusters#1596 Phase 4)
#   - every panel, every target and every query variable names its datasource
#     by one of those variables, never by a hard-coded uid; a TraceQL target
#     uses ${DS_TEMPO}, and a PromQL target or a query variable
#     ${DS_PROMETHEUS}, so a missing Tempo empties the trace panels only
#   - every `scadbuddy_*` series a query reads is declared in
#     backend/scadbuddy/core/metrics.py (a histogram's `_bucket`/`_sum`/`_count`
#     and a counter's `_total` are prometheus_client's exposition suffixes)
#   - every TraceQL query filters on a ScadBuddy `resource.service.name`
#     (spec §3), and every span `name="…"` in it is one that service emits:
#     for scadbuddy-api and scadbuddy-worker, `render.<RenderStage>` or a
#     literal passed to `span(`/`detached_span(` in backend/scadbuddy; for
#     scadbuddy-agent and scadbuddy-web, a quoted literal in agent/src or
#     frontend/src, checked once that service's tracing module
#     (agent/src/telemetry.ts, frontend/src/lib/tracing.ts) exists and noted
#     as unchecked until then
#   - `kustomize build deploy/grafana` succeeds and yields exactly one
#     ConfigMap, `scadbuddy-dashboard` in `cattle-dashboards`, labelled
#     `grafana_dashboard: "1"`, whose `scadbuddy.json` is the file above
#
# $KUSTOMIZE names the binary (default `kustomize` on PATH); CI points it at
# the pinned release its own step installed. Needs jq and mikefarah yq v4.
# Exit status 1 if there was any problem, 2 on a usage error.
set -euo pipefail

if [ "$#" -gt 1 ]; then
  echo "usage: $0 [repo-root]" >&2
  exit 2
fi
root="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
if [ ! -d "$root" ]; then
  echo "usage: $0 [repo-root]: $root is not a directory" >&2
  exit 2
fi
kustomize="${KUSTOMIZE:-kustomize}"

rel=deploy/grafana/scadbuddy.json
dash="$root/$rel"
metrics_py="$root/backend/scadbuddy/core/metrics.py"
problems=0
problem() {
  printf '%s: %s\n' "$1" "$2"
  problems=$((problems + 1))
}

if ! jq -e . "$dash" > /dev/null 2>&1; then
  problem "$rel" "missing or not valid JSON"
  exit 1
fi

# Every panel, a row's collapsed children included.
panels='def panels: .panels[]? | (., .panels[]?);'

uid=$(jq -r '.uid // ""' "$dash")
[ "$uid" = scadbuddy ] || problem "$rel" "uid must be \"scadbuddy\", not \"$uid\""

while read -r id; do
  problem "$rel" "panel id $id is used more than once"
done < <(jq -r "$panels"' [panels | .id] | group_by(.) | map(select(length > 1) | .[0]) | .[]' "$dash")

# The datasource variables, and the two the dashboard must have.
ds_var() { # name -> "<plugin type>\t<default uid>", or nothing
  jq -r --arg n "$1" '.templating.list[]? | select(.type == "datasource" and .name == $n)
    | "\(.query)\t\(.current.value // "")"' "$dash"
}
IFS=$'\t' read -r prom_type _ < <(ds_var DS_PROMETHEUS; echo) || true
[ "$prom_type" = prometheus ] \
  || problem "$rel" "needs a datasource variable DS_PROMETHEUS of type prometheus"
IFS=$'\t' read -r tempo_type tempo_default < <(ds_var DS_TEMPO; echo) || true
if [ "$tempo_type" != tempo ]; then
  problem "$rel" "needs a datasource variable DS_TEMPO of type tempo"
elif [ "$tempo_default" != tempo ]; then
  problem "$rel" "DS_TEMPO must default to uid \"tempo\" (clusters#1596), not \"$tempo_default\""
fi
ds_vars=$(jq -r '[.templating.list[]? | select(.type == "datasource") | .name] | join(" ")' "$dash")

# Every panel (not a row), every target, every query variable.
while IFS=$'\t' read -r where ds; do
  if [[ "$ds" =~ ^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$ ]] && [[ " $ds_vars " == *" ${BASH_REMATCH[1]} "* ]]; then
    continue
  fi
  problem "$rel" "$where: datasource must be a datasource variable (\${DS_PROMETHEUS} or \${DS_TEMPO}), not \"$ds\""
done < <(jq -r "$panels"'
  [ (panels | select(.type != "row") | ["panel \(.id) (\(.title))", (.datasource.uid // "")]),
    (panels | select(.type != "row") | . as $p | .targets[]?
      | ["panel \($p.id) target \(.refId)", (.datasource.uid // "")]),
    (.templating.list[]? | select(.type == "query") | ["variable \(.name)", (.datasource.uid // "")]) ]
  | .[] | @tsv' "$dash")

# Only trace searches read Tempo: until clusters#1596 Phase 4 adds the
# datasource, a Tempo panel shows no data and nothing else is affected, so no
# PromQL target and no variable may sit on ${DS_TEMPO}.
while IFS=$'\t' read -r where want ds; do
  [ "$ds" = "\${$want}" ] || problem "$rel" "$where: must use \${$want}, not \"$ds\""
done < <(jq -r "$panels"'
  [ (panels | . as $p | .targets[]?
      | if .queryType == "traceql" then ["panel \($p.id) target \(.refId)", "DS_TEMPO", (.datasource.uid // "")]
        elif has("expr") then ["panel \($p.id) target \(.refId)", "DS_PROMETHEUS", (.datasource.uid // "")]
        else empty end),
    (.templating.list[]? | select(.type == "query") | ["variable \(.name)", "DS_PROMETHEUS", (.datasource.uid // "")]) ]
  | .[] | @tsv' "$dash")

# Prometheus series against the registry in core/metrics.py.
declared() { grep -qF "\"$1\"" "$metrics_py"; }
while read -r series; do
  [ -n "$series" ] || continue
  ok=false
  for name in "$series" "${series%_bucket}" "${series%_sum}" "${series%_count}" "${series%_total}"; do
    if declared "$name"; then ok=true; break; fi
  done
  $ok || problem "$rel" "$series is not a metric backend/scadbuddy/core/metrics.py declares"
done < <(jq -r "$panels"' [panels | .targets[]? | .expr // empty] + [.templating.list[]? | .query | objects | .query // empty] | .[]' "$dash" \
  | grep -oE 'scadbuddy_[a-z0-9_]+' | sort -u)

# The span names the backend emits: one per render stage (render/jobs.py's
# `render.{name}`), and every literal its code passes to span()/detached_span().
backend_names=$(
  {
    grep -E '^RenderStage = Literal\[' "$metrics_py" | grep -oE '"[a-z_]+"' | tr -d '"' | sed 's/^/render./'
    find "$root/backend/scadbuddy" -name '*.py' -exec cat {} + | tr '\n' ' ' \
      | grep -oE '(^|[^A-Za-z_])(detached_)?span\(\s*"[^"]+"' | grep -oE '"[^"]+"' | tr -d '"'
  } | sort -u
)
unchecked=""
emitted() { # service name -> 0 when that service emits a span of that name
  local service=$1 name=$2 src probe
  case "$service" in
    scadbuddy-api | scadbuddy-worker) grep -qxF "$name" <<< "$backend_names"; return ;;
    scadbuddy-agent) src="$root/agent/src" probe="$root/agent/src/telemetry.ts" ;;
    scadbuddy-web) src="$root/frontend/src" probe="$root/frontend/src/lib/tracing.ts" ;;
  esac
  if [ ! -f "$probe" ]; then
    [[ " $unchecked " == *" $service "* ]] || unchecked+=" $service"
    return 0
  fi
  grep -rqF -e "\"$name\"" -e "'$name'" -e "\`$name\`" "$src"
}

while IFS=$'\t' read -r where query; do
  mapfile -t services < <(grep -oE 'resource\.service\.name\s*=\s*"[^"]*"' <<< "$query" | sed -E 's/.*"(.*)"/\1/')
  if [ "${#services[@]}" -eq 0 ]; then
    problem "$rel" "$where: a TraceQL query must filter on resource.service.name"
    continue
  fi
  bad=false
  for service in "${services[@]}"; do
    case "$service" in
      scadbuddy-api | scadbuddy-worker | scadbuddy-agent | scadbuddy-web) ;;
      *) problem "$rel" "$where: \"$service\" is not a ScadBuddy service.name"; bad=true ;;
    esac
  done
  $bad && continue
  if grep -qE '(^|[^.A-Za-z_])name\s*(=~|!~)' <<< "$query"; then
    problem "$rel" "$where: match span names exactly (name=\"…\"), so this lint can check them"
  fi
  while read -r name; do
    [ -n "$name" ] || continue
    emitted "${services[0]}" "$name" \
      || problem "$rel" "$where: ${services[0]} emits no span named \"$name\""
  done < <(grep -oE '(^|[^.A-Za-z_])name\s*=\s*"[^"]*"' <<< "$query" | sed -E 's/.*"(.*)"/\1/')
done < <(jq -r "$panels"' panels | . as $p | .targets[]? | select(.queryType == "traceql")
  | ["panel \($p.id) target \(.refId)", .query] | @tsv' "$dash")
for service in $unchecked; do
  echo "::notice::$service has no tracing module in this tree yet; its span names in $rel are unchecked"
done

# What the sidecar will load.
kz_rel=deploy/grafana/kustomization.yaml
if ! command -v "$kustomize" > /dev/null 2>&1; then
  problem "$kz_rel" "kustomize not found (set KUSTOMIZE)"
elif ! built=$("$kustomize" build "$root/deploy/grafana" 2>&1); then
  problem "$kz_rel" "kustomize build failed: $(head -n 1 <<< "$built")"
else
  count=$(yq ea -N '[.] | length' - <<< "$built")
  meta=$(yq -r '[.kind, .metadata.name, .metadata.namespace, .metadata.labels.grafana_dashboard] | join(" ")' - <<< "$built")
  if [ "$count" != 1 ] || [ "$meta" != "ConfigMap scadbuddy-dashboard cattle-dashboards 1" ]; then
    problem "$kz_rel" "must build exactly one ConfigMap scadbuddy-dashboard in cattle-dashboards labelled grafana_dashboard: \"1\" (got $count document(s): $meta)"
  elif ! diff -q <(yq -r '.data["scadbuddy.json"]' - <<< "$built" | jq -S .) <(jq -S . "$dash") > /dev/null; then
    problem "$kz_rel" "the ConfigMap's scadbuddy.json is not $rel"
  fi
fi

[ "$problems" -eq 0 ] || exit 1
echo "lint-dashboard: ok"
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `chmod +x .github/scripts/lint-dashboard.sh && KUSTOMIZE=/tmp/kustomize-5.6.0/kustomize .github/scripts/lint-dashboard.test.sh`
Expected: 20 `ok` lines, then `20 passed, 0 failed`, exit 0.

- [ ] **Step 8: Run the lint on the repo, and shellcheck**

Run: `KUSTOMIZE=/tmp/kustomize-5.6.0/kustomize .github/scripts/lint-dashboard.sh && shellcheck .github/scripts/*.sh models/*/verify.sh`
Expected: `::notice::scadbuddy-agent has no tracing module in this tree yet; its span names in deploy/grafana/scadbuddy.json are unchecked` (only while row 3 is unmerged; if `agent/src/telemetry.ts` exists the notice is absent and the agent names are checked), then `lint-dashboard: ok`; shellcheck prints nothing.

If row 3 has merged and the lint reports `scadbuddy-agent emits no span named "…"`,
the agent's names differ from §5.4: fix the dashboard query to the name the agent
emits, and note the difference in the PR body (the spec is the authority; a mismatch
is a finding, not something to paper over).

- [ ] **Step 9: Commit**

```bash
git add deploy/grafana/kustomization.yaml deploy/grafana/scadbuddy.json \
  .github/scripts/lint-dashboard.sh .github/scripts/lint-dashboard.test.sh
git commit -m "feat(dashboard): ScadBuddy Grafana dashboard in deploy/grafana, with its lint (#988)"
```

---

### Task 2: The `lint` job runs the dashboard lint on a pinned kustomize

**Files:**
- Modify: `.github/workflows/ci.yml` (`lint` job, after the "Test the clusters deploy pin" step)
- Modify: `CLAUDE.md` ("Workflow/Dockerfile lint" paragraph, and "Layout")

**Interfaces:**
- Consumes: `.github/scripts/lint-dashboard.sh`, `.github/scripts/lint-dashboard.test.sh` (Task 1), both reading `$KUSTOMIZE`.
- Produces: `KUSTOMIZE` in `$GITHUB_ENV` for every later step of the `lint` job.

- [ ] **Step 1: Add the steps**

In `.github/workflows/ci.yml`, replace

```yaml
      - name: Test the clusters deploy pin
        run: .github/scripts/deploy-pin.test.sh
```

with

```yaml
      - name: Test the clusters deploy pin
        run: .github/scripts/deploy-pin.test.sh

      # deploy/grafana is built by kustomize in ArgoCD's repo-server, which
      # runs 5.6.0 (clusters/prod/bambuddy/kustomization.yaml says so), so the
      # lint builds it with that release, never whatever kustomize the runner
      # image happens to carry. The sha256 is the one kustomize published in
      # that release's checksums.txt, pinned here so a replaced asset fails.
      - name: Install kustomize
        env:
          KUSTOMIZE_VERSION: v5.6.0
          KUSTOMIZE_SHA256: 54e4031ddc4e7fc59e408da29e7c646e8e57b8088c51b84b3df0864f47b5148f
        run: |
          set -euo pipefail
          tarball="kustomize_${KUSTOMIZE_VERSION}_linux_amd64.tar.gz"
          curl -fsSL --retry 3 -o "$RUNNER_TEMP/$tarball" \
            "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/${tarball}"
          echo "${KUSTOMIZE_SHA256}  $RUNNER_TEMP/$tarball" | sha256sum -c -
          mkdir -p "$RUNNER_TEMP/kustomize"
          tar -xzf "$RUNNER_TEMP/$tarball" -C "$RUNNER_TEMP/kustomize" kustomize
          "$RUNNER_TEMP/kustomize/kustomize" version
          echo "KUSTOMIZE=$RUNNER_TEMP/kustomize/kustomize" >> "$GITHUB_ENV"

      # The dashboard (#988, spec 2026-10-01 §7): JSON, uid, datasource
      # variables, the series and span names its queries read, and the
      # ConfigMap kustomize builds from it. Its own tests first, so a
      # regression in the lint reads as a failing case.
      - name: Test the dashboard lint
        run: .github/scripts/lint-dashboard.test.sh

      - name: Lint the Grafana dashboard
        run: .github/scripts/lint-dashboard.sh
```

Hosted runner, no cache, no new action: `curl`, `sha256sum` and `tar` are on
`ubuntu-latest`, and `$GITHUB_ENV` makes the path explicit so the lint never picks up
the runner image's own kustomize.

- [ ] **Step 2: Check the workflow and run the install step as CI would**

```bash
actionlint -ignore create-github-app-token .github/workflows/ci.yml
rt=$(mktemp -d)
RUNNER_TEMP=$rt GITHUB_ENV=$rt/env KUSTOMIZE_VERSION=v5.6.0 \
  KUSTOMIZE_SHA256=54e4031ddc4e7fc59e408da29e7c646e8e57b8088c51b84b3df0864f47b5148f \
  bash -c "$(yq '.jobs.lint.steps[] | select(.name == "Install kustomize") | .run' .github/workflows/ci.yml)"
env "$(cat "$rt/env")" .github/scripts/lint-dashboard.test.sh | tail -1
env "$(cat "$rt/env")" .github/scripts/lint-dashboard.sh | tail -1
```

Expected: actionlint prints nothing; the step prints `…tar.gz: OK` and `v5.6.0`; then
`20 passed, 0 failed` and `lint-dashboard: ok`.

Then the negative: a wrong pin must stop the step.

```bash
RUNNER_TEMP=$rt GITHUB_ENV=$rt/env KUSTOMIZE_VERSION=v5.6.0 KUSTOMIZE_SHA256=$(printf '0%.0s' {1..64}) \
  bash -c "$(yq '.jobs.lint.steps[] | select(.name == "Install kustomize") | .run' .github/workflows/ci.yml)"; echo "exit $?"
```

Expected: `FAILED` from `sha256sum`, `exit 1`.

- [ ] **Step 3: CLAUDE.md**

In the paragraph that begins `Workflow/Dockerfile lint (the \`lint\` job)`, replace

```markdown
`shellcheck .github/scripts/*.sh models/*/verify.sh`, `lint-verify-labels.sh`, and the
`.github/scripts/*.test.sh` suites.
```

with

```markdown
`shellcheck .github/scripts/*.sh models/*/verify.sh`, `lint-verify-labels.sh`,
`lint-dashboard.sh` (the Grafana dashboard, #988; it needs `KUSTOMIZE` pointing at
kustomize v5.6.0, which the job downloads and checks by sha256 because ArgoCD's
repo-server runs that version), and the `.github/scripts/*.test.sh` suites.
```

Under "Layout", after the `models/` bullet, add:

```markdown
- `deploy/grafana/` — the ScadBuddy Grafana dashboard (#988, tracing spec §7): uid
  `scadbuddy` (never change it), a `configMapGenerator` ConfigMap in
  `cattle-dashboards` for the rancher-monitoring sidecar, datasources only as the
  `DS_PROMETHEUS`/`DS_TEMPO` variables. clusters pulls it in as a remote resource
  pinned to a full SHA, and `deploy.reusable.yml` moves that `ref` with the image.
  A query may read only series `core/metrics.py` declares and span names the
  service emits (`lint-dashboard.sh` checks both).
```

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml CLAUDE.md
git commit -m "ci(lint): lint the dashboard with a pinned, checksummed kustomize 5.6.0 (#988)"
```

---

### Task 3: The deploy moves the dashboard's `ref` with the image, when clusters pins it

**Files:**
- Modify: `.github/workflows/deploy.reusable.yml` (header comment, `env`, the `edit` step, the `pr` step)
- Test: `.github/scripts/deploy-pin.test.sh`

**Interfaces:**
- Consumes: the `edit` step's existing `expect_one`, `$REVISION` (validated as 40 hex by "Validate inputs"), `$GITHUB_OUTPUT`, and the `manifests` array whose diff decides `unchanged`/`files`.
- Produces: workflow env `KUSTOMIZATION=clusters/prod/scadbuddy/kustomization.yaml`, `DASHBOARD_RESOURCE=https://github.com/eh-homelab/ScadBuddy//deploy/grafana`; step output `steps.edit.outputs.dashboard` = `pinned` | `none`; error texts the test matches: `is not in clusters`, `mentions eh-homelab/ScadBuddy//deploy/grafana but no line is exactly`, `expected at most one dashboard line, found N`, notice `has no dashboard pin yet`.

- [ ] **Step 1: Write the failing tests**

Four edits to `.github/scripts/deploy-pin.test.sh`.

(a) In the header comment, replace

```bash
# without the agent's line fails.
#
```

with

```bash
# without the agent's line fails.
#
# The dashboard's `ref=` in clusters/prod/scadbuddy/kustomization.yaml (#988,
# spec 2026-10-01 §7) is optional: an overlay that never mentions
# deploy/grafana deploys the images alone, one exact line is moved to the
# revision, and a near miss or a second line fails the deploy.
#
```

(b) Replace

```bash
export MANIFEST_WORKER=applications/scadbuddy/scadbuddy-render.yaml
```

with

```bash
export MANIFEST_WORKER=applications/scadbuddy/scadbuddy-render.yaml
export KUSTOMIZATION=clusters/prod/scadbuddy/kustomization.yaml
export DASHBOARD_RESOURCE=https://github.com/eh-homelab/ScadBuddy//deploy/grafana
```

(c) Replace the whole `run()` function and its comment:

```bash
# `run <api-manifest> [worker-manifest]`: a fresh repo, then the step. Output
# lands in $work/log, and the manifests stay in $work/repo for the checks.
run() {
  rm -rf "$work/repo" && mkdir -p "$work/repo/applications/scadbuddy"
  printf '%s\n' "$1" > "$work/repo/$MANIFEST"
  [ $# -lt 2 ] || printf '%s\n' "$2" > "$work/repo/$MANIFEST_WORKER"
  (
    cd "$work/repo"
    git init -q && git add -A
    git -c user.email=t@t -c user.name=t commit -qm base
    GITHUB_OUTPUT="$work/out" bash -e "$work/edit.sh"
  ) > "$work/log" 2>&1
}
```

with

```bash
# `overlay [line]...` prints the clusters overlay's kustomization with the given
# extra resource lines; with none, it is today's overlay, which has no
# dashboard pin. `run` writes $overlay, which a case sets before calling it.
overlay() {
  printf '%s\n' '---' 'kind: Kustomization' 'namespace: bambuddy' 'resources:' \
    '  - ../../../applications/scadbuddy' "$@"
}
overlay=$(overlay)
old_ref="$(printf '1%.0s' {1..40})"
dashboard_line="  - $DASHBOARD_RESOURCE?ref="

# `run <api-manifest> [worker-manifest]`: a fresh repo, then the step. Output
# lands in $work/log, and the manifests stay in $work/repo for the checks.
run() {
  rm -rf "$work/repo" && mkdir -p "$work/repo/applications/scadbuddy" "$work/repo/$(dirname "$KUSTOMIZATION")"
  printf '%s\n' "$1" > "$work/repo/$MANIFEST"
  [ $# -lt 2 ] || printf '%s\n' "$2" > "$work/repo/$MANIFEST_WORKER"
  [ -z "$overlay" ] || printf '%s\n' "$overlay" > "$work/repo/$KUSTOMIZATION"
  (cd "$work/repo" && git init -q)
  rerun
}

# `rerun`: commit the repo as it stands (a clusters main that already merged
# what the last run wrote), then run the step on it again.
rerun() {
  (
    cd "$work/repo"
    git add -A
    git -c user.email=t@t -c user.name=t commit -qm base --allow-empty
    : > "$work/out"
    GITHUB_OUTPUT="$work/out" bash -e "$work/edit.sh"
  ) > "$work/log" 2>&1
}
```

The first seven cases are unchanged: they now run beside today's overlay (no
dashboard line), which is exactly "not configured".

(d) Insert the new cases immediately before the closing block
`if [ "$failures" -gt 0 ]; then`:

```bash
# `dashboard_fails <name> <expected-error> <overlay-line>...`: the API and agent
# beside an overlay carrying those lines; the step must fail with that error.
api_and_agent="$(deployment scadbuddy "$IMAGE:$old" "$agent")"
dashboard_fails() {
  local name=$1 expected=$2; shift 2
  overlay=$(overlay "$@")
  if run "$api_and_agent"; then
    fail "$name: step passed, expected an error"
  else
    grep -qF "$expected" "$work/log" || fail "$name: wrong error: $(grep '::error' "$work/log")"
  fi
}
near_miss="mentions eh-homelab/ScadBuddy//deploy/grafana but no line is exactly"

# 8. Not configured: today's overlay. A notice, the images pinned, the overlay
#    untouched and not among the files the PR names.
overlay=$(overlay)
if run "$api_and_agent"; then
  grep -q '::notice::.*has no dashboard pin yet' "$work/log" || fail "no dashboard: no notice"
  has_line "$pinned" "$MANIFEST" || fail "no dashboard: api image not pinned"
  (cd "$work/repo" && git diff --quiet -- "$KUSTOMIZATION") || fail "no dashboard: overlay changed"
  grep -qx 'dashboard=none' "$work/out" || fail "no dashboard: dashboard output is not none"
  grep -q "^files=.*kustomization" "$work/out" && fail "no dashboard: overlay listed in files"
else
  fail "no dashboard: step failed: $(grep '::error' "$work/log")"
fi

# 9. One exact line: moved to REVISION, and the overlay is among the files.
overlay=$(overlay "${dashboard_line}${old_ref}")
if run "$api_and_agent"; then
  grep -qxF "${dashboard_line}${REVISION}" "$work/repo/$KUSTOMIZATION" || fail "dashboard: ref not moved"
  grep -qF "$old_ref" "$work/repo/$KUSTOMIZATION" && fail "dashboard: old ref survived"
  grep -qx 'dashboard=pinned' "$work/out" || fail "dashboard: dashboard output is not pinned"
  grep -q "^files=.*$KUSTOMIZATION" "$work/out" || fail "dashboard: overlay not listed in files"
  has_line "$pinned" "$MANIFEST" || fail "dashboard: api image not pinned"
else
  fail "dashboard: step failed: $(grep '::error' "$work/log")"
fi

# 10. The same deploy again, once clusters merged the first: nothing to deploy.
overlay=$(overlay "${dashboard_line}${old_ref}")
if run "$api_and_agent" && rerun; then
  grep -qx 'unchanged=true' "$work/out" || fail "dashboard current: expected unchanged=true"
else
  fail "dashboard current: step failed: $(grep '::error' "$work/log")"
fi

# 11. Only the dashboard is behind (clusters added the line after the last
#     deploy): the deploy PR moves just the ref.
overlay=$(overlay)
if run "$api_and_agent" \
  && printf '%s\n' "${dashboard_line}${old_ref}" >> "$work/repo/$KUSTOMIZATION" \
  && rerun; then
  grep -qx 'unchanged=false' "$work/out" || fail "dashboard behind: expected unchanged=false"
  grep -qx "files=$KUSTOMIZATION" "$work/out" || fail "dashboard behind: expected only the overlay in files"
else
  fail "dashboard behind: step failed: $(grep '::error' "$work/log")"
fi

# 12-17. Near misses are errors, never "not configured".
dashboard_fails "short sha" "$near_miss" "${dashboard_line}1111111"
dashboard_fails "branch ref" "$near_miss" "${dashboard_line}main"
dashboard_fails "trailing comment" "$near_miss" "${dashboard_line}${old_ref} # dashboard"
dashboard_fails "lower case" "$near_miss" "  - https://github.com/eh-homelab/scadbuddy//deploy/grafana?ref=${old_ref}"
dashboard_fails "commented out" "$near_miss" "  # - $DASHBOARD_RESOURCE?ref=${old_ref}"
dashboard_fails "extra space" "$near_miss" "  -  $DASHBOARD_RESOURCE?ref=${old_ref}"

# 18. Two exact lines: an error, as for any other pinned line.
dashboard_fails "two dashboard lines" "expected at most one dashboard line, found 2" \
  "${dashboard_line}${old_ref}" "${dashboard_line}${old_ref}"

# 19. No overlay at all: an error, not a silent skip. clusters moved it, and
#     this run cannot tell whether the new one pins the dashboard.
overlay=""
if run "$api_and_agent"; then
  fail "no overlay: step passed, expected an error"
else
  grep -qF "$KUSTOMIZATION is not in clusters" "$work/log" || fail "no overlay: wrong error: $(grep '::error' "$work/log")"
fi
```

- [ ] **Step 2: Run them to verify they fail**

Run: `bash .github/scripts/deploy-pin.test.sh`
Expected: exit 1, ending with these 16 lines (the old step neither notices, nor
rewrites, nor refuses anything about the dashboard; cases 1–7 and 10 still pass):

```
FAIL: no dashboard: no notice
FAIL: no dashboard: dashboard output is not none
FAIL: dashboard: ref not moved
FAIL: dashboard: old ref survived
FAIL: dashboard: dashboard output is not pinned
FAIL: dashboard: overlay not listed in files
FAIL: dashboard behind: expected unchanged=false
FAIL: dashboard behind: expected only the overlay in files
FAIL: short sha: step passed, expected an error
FAIL: branch ref: step passed, expected an error
FAIL: trailing comment: step passed, expected an error
FAIL: lower case: step passed, expected an error
FAIL: commented out: step passed, expected an error
FAIL: extra space: step passed, expected an error
FAIL: two dashboard lines: step passed, expected an error
FAIL: no overlay: step passed, expected an error
16 case(s) failed
```

- [ ] **Step 3: Implement it in the workflow**

Five edits to `.github/workflows/deploy.reusable.yml`.

(a) Header comment, step 2: replace

```yaml
#      Digest-pinned on purpose — nodes pull ghcr.io through a Nexus proxy that
#      caches tag metadata for 24 h, so a tag alone can serve yesterday's image.
```

with

```yaml
#      Digest-pinned on purpose — nodes pull ghcr.io through a Nexus proxy that
#      caches tag metadata for 24 h, so a tag alone can serve yesterday's image.
#      And, once clusters#1596 Phase 5 adds it, the `ref=` of the remote
#      resource line in clusters/prod/scadbuddy/kustomization.yaml that pulls
#      in this repo's deploy/grafana dashboard (#988), to the same `revision`,
#      so the dashboard shown is the one written for the build that serves.
```

(b) `env:` — replace

```yaml
  AGENT_IMAGE: ghcr.io/eh-homelab/scadbuddy-agent
```

with

```yaml
  AGENT_IMAGE: ghcr.io/eh-homelab/scadbuddy-agent
  # The overlay that pulls in deploy/grafana as a remote kustomize resource,
  # and that resource without its `?ref=` (spec 2026-10-01 §7, #988).
  KUSTOMIZATION: clusters/prod/scadbuddy/kustomization.yaml
  DASHBOARD_RESOURCE: https://github.com/eh-homelab/ScadBuddy//deploy/grafana
```

(c) In the `edit` step, replace

```yaml
          manifests=("$MANIFEST")
          pin "$MANIFEST" scadbuddy
          pin_agent "$MANIFEST"
```

with

```yaml
          # The dashboard's `ref=` (#988, spec 2026-10-01 §7). Optional, unlike
          # the agent: until clusters#1596 Phase 5 adds the line, the overlay
          # does not mention deploy/grafana at all and the images deploy alone.
          # A line that mentions it but does not match the anchor exactly (a
          # short SHA, a branch, a trailing comment, other casing or spacing,
          # commented out) is an error, never "not configured": read that way,
          # the dashboard would stay pinned to an old build without a word.
          # Sets $dashboard to `pinned` when it rewrote the line; a global, not
          # a printed value, so its ::error lines reach the log instead of a
          # command substitution.
          dashboard=none
          pin_dashboard() { # kustomization
            local target=$1 n
            local line="^(\s*)- ${DASHBOARD_RESOURCE//./\\.}\?ref=[0-9a-f]{40}\$"
            [ -f "$target" ] || { echo "::error::${target} is not in clusters; cannot tell whether it pins the dashboard"; exit 1; }
            n=$(grep -cE "$line" "$target" || true)
            if [ "$n" -eq 0 ]; then
              if grep -qiF 'eh-homelab/ScadBuddy//deploy/grafana' "$target"; then
                echo "::error file=$target::mentions eh-homelab/ScadBuddy//deploy/grafana but no line is exactly '- ${DASHBOARD_RESOURCE}?ref=<40-hex revision>'"; exit 1
              fi
              echo "::notice::${target} has no dashboard pin yet (clusters#1596 Phase 5); pinning the images only"
              return 0
            fi
            [ "$n" -eq 1 ] || { echo "::error file=$target::expected at most one dashboard line, found $n"; exit 1; }
            sed -i -E "s|${line}|\1- ${DASHBOARD_RESOURCE}?ref=${REVISION}|" "$target"
            expect_one "$target" "^\s*- ${DASHBOARD_RESOURCE//./\\.}\?ref=${REVISION}\$" "pinned dashboard"
            dashboard=pinned
          }

          manifests=("$MANIFEST")
          pin "$MANIFEST" scadbuddy
          pin_agent "$MANIFEST"
          pin_dashboard "$KUSTOMIZATION"
          echo "dashboard=$dashboard" >> "$GITHUB_OUTPUT"
          if [ "$dashboard" = pinned ]; then
            manifests+=("$KUSTOMIZATION")
          fi
```

`$dashboard` is a global rather than the function's printed output: inside
`$(pin_dashboard …)` the `::error` lines would be captured instead of logged.
`$KUSTOMIZATION` joins `manifests` only when pinned, so the existing
`git diff`/`files`/`unchanged` logic covers it with no other change: a deploy where
only the ref moved opens a PR for the overlay alone, and one where nothing moved is
`unchanged`.

(d) In the `pr` step's `env:`, replace

```yaml
          FILES: ${{ steps.edit.outputs.files }}
```

with

```yaml
          FILES: ${{ steps.edit.outputs.files }}
          DASHBOARD: ${{ steps.edit.outputs.dashboard }}
```

(e) In the `pr` step's script, replace

```yaml
          for f in "${manifest_files[@]}"; do listed+="\`${f}\` "; done
```

with

```yaml
          for f in "${manifest_files[@]}"; do listed+="\`${f}\` "; done
          # The dashboard moves with the image, or the overlay has no pin yet.
          case "$DASHBOARD" in
            pinned) dashboard_note="deploy/grafana at ${REVISION}" ;;
            *)      dashboard_note="not pinned in clusters yet (clusters#1596 Phase 5)" ;;
          esac
```

then, in the commit message heredoc, replace

```yaml
          and ${AGENT_IMAGE}:${VERSION}@${AGENT_DIGEST}
          built from eh-homelab/ScadBuddy@${REVISION} (${KIND}),
```

with

```yaml
          and ${AGENT_IMAGE}:${VERSION}@${AGENT_DIGEST}
          (dashboard: ${dashboard_note})
          built from eh-homelab/ScadBuddy@${REVISION} (${KIND}),
```

and in the PR body heredoc, replace

```yaml
          - **Agent:** \`${AGENT_IMAGE}:${VERSION}@${AGENT_DIGEST}\`
```

with

```yaml
          - **Agent:** \`${AGENT_IMAGE}:${VERSION}@${AGENT_DIGEST}\`
          - **Dashboard:** ${dashboard_note}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `bash .github/scripts/deploy-pin.test.sh`
Expected: `deploy-pin: all cases passed`, exit 0.

- [ ] **Step 5: Lint**

Run: `actionlint -ignore create-github-app-token .github/workflows/deploy.reusable.yml && shellcheck .github/scripts/*.sh models/*/verify.sh`
Expected: no output (actionlint also shellchecks the `run:` blocks).

- [ ] **Step 6: Mutation check (do not commit it)**

Confirm the near-miss guard is what the tests pin: make the mention check
case-sensitive and the "lower case" case must fail.

```bash
cp .github/workflows/deploy.reusable.yml /tmp/deploy.reusable.yml.bak
sed -i "s|grep -qiF 'eh-homelab/ScadBuddy//deploy/grafana'|grep -qF 'eh-homelab/ScadBuddy//deploy/grafana'|" .github/workflows/deploy.reusable.yml
bash .github/scripts/deploy-pin.test.sh; echo "exit $?"
cp /tmp/deploy.reusable.yml.bak .github/workflows/deploy.reusable.yml
git diff --stat .github/workflows/deploy.reusable.yml
```

Expected: `FAIL: lower case: step passed, expected an error`, `1 case(s) failed`,
`exit 1`; after the restore, the diff stat shows only this task's change.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/deploy.reusable.yml .github/scripts/deploy-pin.test.sh
git commit -m "feat(deploy): move the dashboard's ref with the image digest when clusters pins it (#988)"
```

---

### Task 4: Documentation, and the PR's manual uid check

**Files:**
- Modify: `README.md` ("Deploying": the `deploy.reusable.yml` list, "Reading a deploy", "Manual fallback", "Tracing (#988)")

**Interfaces:**
- Consumes: names from Tasks 1–3 (`deploy/grafana/`, ConfigMap `scadbuddy-dashboard`, `cattle-dashboards`, `KUSTOMIZATION`, the anchor).
- Produces: nothing code depends on.

- [ ] **Step 1: README, the deploy list**

In "Deploying", in the list under "Both call `deploy.reusable.yml`, which:", replace
item 2's last sentence

```markdown
   clusters#1454 adds it, the run notes its absence and pins the API alone). Each
   file must have exactly one such image line and one of each annotation, before
   and after the rewrite, or the deploy stops;
```

with

```markdown
   clusters#1454 adds it, the run notes its absence and pins the API alone). Each
   file must have exactly one such image line and one of each annotation, before
   and after the rewrite, or the deploy stops. In the same PR it moves the
   dashboard's pin in `clusters/prod/scadbuddy/kustomization.yaml`, the line
   `- https://github.com/eh-homelab/ScadBuddy//deploy/grafana?ref=<40-hex SHA>`, to
   the same revision. That line is optional: an overlay that does not mention
   `eh-homelab/ScadBuddy//deploy/grafana` at all deploys the images alone with a
   notice; one that mentions it in any other form (a short SHA, a branch, a
   comment, other casing or spacing) or twice stops the deploy;
```

- [ ] **Step 2: README, reading a deploy and the manual fallback**

In "Reading a deploy", after the "**What is pinned:**" bullet, add:

```markdown
- **Which dashboard is live:** the `?ref=` on the `deploy/grafana` line of
  clusters' `clusters/prod/scadbuddy/kustomization.yaml`, which should equal the
  `revision` annotation.
```

In "Manual fallback", replace

```markdown
Editing the
image line and annotations in the clusters manifests (both, once the render
worker's exists) by hand and merging does
exactly what the pipeline does.
```

with

```markdown
Editing the
image line and annotations in the clusters manifests (both, once the render
worker's exists), and the dashboard line's `?ref=` (the full 40-character
revision), by hand and merging does exactly what the pipeline does.
```

- [ ] **Step 3: README, the dashboard under Tracing**

At the end of "### Tracing (#988)", add:

```markdown
The ScadBuddy dashboard (uid `scadbuddy`) is `deploy/grafana/`: a kustomize
directory whose `configMapGenerator` makes the ConfigMap `scadbuddy-dashboard`
in `cattle-dashboards`, labelled `grafana_dashboard: "1"`, which the
rancher-monitoring Grafana's sidecar loads. clusters' `clusters/prod/scadbuddy`
overlay lists it as a remote resource pinned to a full commit SHA, and the deploy
moves that pin with the image (above), so the dashboard shown is the one written
for the build that is serving. The overlay must namespace its own resources with
an `unsetOnly` NamespaceTransformer, not a plain `namespace:` field, or the
ConfigMap is moved out of `cattle-dashboards` and never loads (clusters#1596
Phase 5). Datasources are the variables `DS_PROMETHEUS` and `DS_TEMPO` (default
uid `tempo`); until clusters#1596 Phase 4 adds Tempo the trace tables are empty
and the metric panels are unaffected. CI's `lint` job checks the dashboard
(`.github/scripts/lint-dashboard.sh`): every series it reads must be declared in
`core/metrics.py`, and every span name must be one the service emits.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: the dashboard, its pin and how a deploy moves it (#988)"
```

- [ ] **Step 5: The PR description's manual check**

CI cannot see the live Grafana (§7: hosted runners cannot reach the cluster, and this
repo never runs on the LAN pools). When the PR is opened (title
`feat(dashboard): ScadBuddy dashboard and its deploy pin (#988)`), its body must
include, unticked, for whoever opens it to complete from the LAN:

```markdown
Refs #988 (row 5 of the tracing spec, §7).

- [ ] Listed the live dashboards (Grafana `GET /api/search?type=dash-db` from the LAN)
      and confirmed uid `scadbuddy` is not among them. A duplicated uid puts the whole
      sidecar provider into read-only mode, so this is checked once, before the first
      deploy that carries the dashboard line.

Clusters (clusters#1596 Phase 5) needs, before the dashboard deploys: the remote
resource line `- https://github.com/eh-homelab/ScadBuddy//deploy/grafana?ref=<40-hex SHA>`
in `clusters/prod/scadbuddy/kustomization.yaml`, and the `unsetOnly`
NamespaceTransformer in place of that overlay's plain `namespace: bambuddy`.
Until then every deploy notes "no dashboard pin yet" and pins the images only.
```

---

## After the last task

Run every check this PR touches, as the `lint` job will:

```bash
export KUSTOMIZE=/tmp/kustomize-5.6.0/kustomize
actionlint -ignore create-github-app-token
shellcheck .github/scripts/*.sh models/*/verify.sh
.github/scripts/lint-dashboard.test.sh
.github/scripts/lint-dashboard.sh
.github/scripts/deploy-pin.test.sh
```

Expected: no actionlint or shellcheck output; `20 passed, 0 failed`;
`lint-dashboard: ok`; `deploy-pin: all cases passed`. No backend, frontend or agent
code changes in this PR, so their suites are not affected.
