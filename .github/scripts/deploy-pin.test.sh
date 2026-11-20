#!/usr/bin/env bash
#
# Cases for the pin step in deploy.reusable.yml (step id `edit`), which rewrites
# ScadBuddy's image line in the clusters manifests. It runs the step's own
# `run:` script, read out of the workflow with yq, so the test cannot drift from
# the code it covers. No network and no clusters checkout: each case is a
# scratch git repo holding fixture manifests.
#
# The case that matters most is a second image in the same pod whose name shares
# the prefix: the agent sidecar, ghcr.io/eh-homelab/scadbuddy-agent (#249). The
# old pattern `scadbuddy[^[:space:]]*` matched it too, so every deploy failed
# with "expected exactly one image line, found 2" (#720). Since #738 the step
# pins the agent too, to the same version at its own digest, and a manifest
# without the agent's line fails.
#
# The dashboards' vendored copy in clusters/prod/scadbuddy/grafana/ (#988,
# spec 2026-10-01 §7, eh-homelab/clusters#3734) is optional: an overlay without
# it deploys the images alone, a copy is replaced with deploy/grafana at the
# revision and its .revision moved, and an overlay that still names the old
# remote resource (`deploy/grafana?ref=`) fails the deploy. The fetch step
# (step id `grafana`) runs against a stub gh.
#
# Runs in ci.yml's `lint` job. Needs mikefarah yq v4 (as the step does).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workflow="$here/../workflows/deploy.reusable.yml"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

yq '.jobs[].steps[] | select(.id == "edit") | .run' "$workflow" > "$work/edit.sh"
[ -s "$work/edit.sh" ] || { echo "FAIL: no step with id 'edit' in $workflow" >&2; exit 1; }

export IMAGE=ghcr.io/eh-homelab/scadbuddy
export AGENT_IMAGE=ghcr.io/eh-homelab/scadbuddy-agent
export AGENT_DURABLE_IMAGE=ghcr.io/eh-homelab/scadbuddy-agent-durable
export SLICER_IMAGE=ghcr.io/eh-homelab/scadbuddy-slicer
export SANDBOX_IMAGE=ghcr.io/eh-homelab/scadbuddy-sandbox
export PROFILE_FETCH_IMAGE=ghcr.io/eh-homelab/scadbuddy-profile-fetch
export MANIFEST=applications/scadbuddy/scadbuddy.yaml
export MANIFEST_WORKER=applications/scadbuddy/scadbuddy-render.yaml
export MANIFEST_PRINT=applications/scadbuddy/scadbuddy-print.yaml
export MANIFEST_PROJECTS=applications/scadbuddy/scadbuddy-projects.yaml
export MANIFEST_AGENT_DURABLE=applications/scadbuddy/scadbuddy-agent-durable.yaml
export MANIFEST_SLICER=applications/scadbuddy/scadbuddy-slicer.yaml
export MANIFEST_STOREFRONT=applications/scadbuddy/scadbuddy-storefront.yaml
export MANIFEST_INTAKE=applications/scadbuddy/scadbuddy-intake.yaml
export MANIFEST_PROFILE_FETCH=applications/scadbuddy/scadbuddy-profile-fetch.yaml
export KUSTOMIZATION=clusters/prod/scadbuddy/kustomization.yaml
export DASHBOARD_DIR=clusters/prod/scadbuddy/grafana
# What the step before it found: deploy/grafana exists at REVISION (its files
# are in $DASHBOARD_SOURCE, set with the dashboard cases).
export DASHBOARD_AT_REVISION=true
export VERSION=sha-2222222
DIGEST="sha256:$(printf '2%.0s' {1..64})"
REVISION="$(printf '2%.0s' {1..40})"
AGENT_DIGEST="sha256:$(printf '4%.0s' {1..64})"
AGENT_DURABLE_DIGEST="sha256:$(printf '5%.0s' {1..64})"
SLICER_DIGEST="sha256:$(printf '7%.0s' {1..64})"
SANDBOX_DIGEST="sha256:$(printf '9%.0s' {1..64})"
PROFILE_FETCH_DIGEST="sha256:$(printf 'b%.0s' {1..64})"
export DIGEST REVISION AGENT_DIGEST AGENT_DURABLE_DIGEST SLICER_DIGEST SANDBOX_DIGEST PROFILE_FETCH_DIGEST
export SOURCE_URL="https://github.com/eh-homelab/ScadBuddy/commit/$REVISION"
old="sha-1111111@sha256:$(printf '1%.0s' {1..64})"
agent="$AGENT_IMAGE:sha-3333333@sha256:$(printf '3%.0s' {1..64})"
pinned="$IMAGE:$VERSION@$DIGEST"
agent_pinned="$AGENT_IMAGE:$VERSION@$AGENT_DIGEST"
durable="$AGENT_DURABLE_IMAGE:sha-3333333@sha256:$(printf '6%.0s' {1..64})"
durable_pinned="$AGENT_DURABLE_IMAGE:$VERSION@$AGENT_DURABLE_DIGEST"
slicer_old="$SLICER_IMAGE:sha-3333333@sha256:$(printf '8%.0s' {1..64})"
slicer_pinned="$SLICER_IMAGE:$VERSION@$SLICER_DIGEST"
sandbox_old="$SANDBOX_IMAGE:sha-3333333@sha256:$(printf 'a%.0s' {1..64})"
sandbox_pinned="$SANDBOX_IMAGE:$VERSION@$SANDBOX_DIGEST"
pf_old="$PROFILE_FETCH_IMAGE:sha-3333333@sha256:$(printf 'c%.0s' {1..64})"
pf_pinned="$PROFILE_FETCH_IMAGE:$VERSION@$PROFILE_FETCH_DIGEST"
failures=0

fail() {
  echo "FAIL: $1" >&2
  failures=$((failures + 1))
}

# `deployment <name> <image-line>...` prints a Deployment carrying the pipeline's
# annotations, with one container per image line given.
deployment() {
  local name=$1; shift
  cat <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $name
  annotations:
    scadbuddy.eh-homelab.io/version: "sha-1111111"
    scadbuddy.eh-homelab.io/revision: "$(printf '1%.0s' {1..40})"
    scadbuddy.eh-homelab.io/source: "https://github.com/eh-homelab/ScadBuddy/commit/1"
spec:
  template:
    spec:
      containers:
EOF
  local i=0 image
  for image in "$@"; do
    i=$((i + 1))
    cat <<EOF
        - name: c$i
          image: $image
          imagePullPolicy: IfNotPresent
EOF
  done
}

# `overlay [line]...` prints the clusters overlay's kustomization with the given
# extra resource lines; with none, it is an overlay with no vendored
# dashboards. `run` writes $overlay, which a case sets before calling it.
overlay() {
  printf '%s\n' '---' 'kind: Kustomization' 'namespace: bambuddy' 'resources:' \
    '  - ../../../applications/scadbuddy' "$@"
}
# The overlay the vendored dashboards need: no top-level `namespace:`, which
# would move the ConfigMaps out of cattle-dashboards, but a NamespaceTransformer
# with unsetOnly (clusters#1596 Phase 5).
overlay_ns() {
  printf '%s\n' '---' 'kind: Kustomization' 'resources:' \
    '  - ../../../applications/scadbuddy' "$@" 'transformers:' '  - |-' \
    '    apiVersion: builtin' '    kind: NamespaceTransformer' \
    '    metadata:' '      name: ns' '    namespace: bambuddy' "    unsetOnly: ${UNSET_ONLY:-true}"
}
overlay=$(overlay)
old_ref="$(printf '1%.0s' {1..40})"

# `run <api-manifest> [worker-manifest] [print-manifest] [projects-manifest]`: a fresh repo, then the
# step. Output lands in $work/log, and the manifests stay in $work/repo for the checks.
# With $durable_manifest set, it is written as agent-durable's own Deployment.
# With $extra_path set, $extra_content is written there (relative to the repo).
# With $vendored set, clusters' copy of the dashboards is written (vendor_dir).
run() {
  rm -rf "$work/repo" && mkdir -p "$work/repo/applications/scadbuddy" "$work/repo/$(dirname "$KUSTOMIZATION")"
  printf '%s\n' "$1" > "$work/repo/$MANIFEST"
  [ $# -lt 2 ] || printf '%s\n' "$2" > "$work/repo/$MANIFEST_WORKER"
  [ $# -lt 3 ] || printf '%s\n' "$3" > "$work/repo/$MANIFEST_PRINT"
  [ $# -lt 4 ] || printf '%s\n' "$4" > "$work/repo/$MANIFEST_PROJECTS"
  [ -z "${durable_manifest:-}" ] || printf '%s\n' "$durable_manifest" > "$work/repo/$MANIFEST_AGENT_DURABLE"
  [ -z "${slicer_manifest:-}" ] || printf '%s\n' "$slicer_manifest" > "$work/repo/$MANIFEST_SLICER"
  [ -z "${pf_manifest:-}" ] || printf '%s\n' "$pf_manifest" > "$work/repo/$MANIFEST_PROFILE_FETCH"
  [ -z "${extra_path:-}" ] || printf '%s\n' "$extra_content" > "$work/repo/$extra_path"
  [ -z "$overlay" ] || printf '%s\n' "$overlay" > "$work/repo/$KUSTOMIZATION"
  [ -z "${vendored:-}" ] || vendor_dir
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

has_line() { grep -qF "image: $1" "$work/repo/$2"; }

# 1. The API alone: the agent's line is missing, which fails (#738). clusters
#    runs the agent in the API's pod, so a missing line is a dropped sidecar.
if run "$(deployment scadbuddy "$IMAGE:$old")"; then
  fail "api only: step passed, expected an error"
else
  grep -q 'expected exactly one agent image line, found 0' "$work/log" \
    || fail "api only: wrong error: $(grep '::error' "$work/log")"
fi

# 2. The agent sidecar beside it: both pinned, each to its own digest.
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  has_line "$pinned" "$MANIFEST" || fail "with agent: api image not pinned"
  has_line "$agent_pinned" "$MANIFEST" || fail "with agent: agent image not pinned"
else
  fail "with agent: step failed: $(grep '::error' "$work/log")"
fi

# 3. A bare-digest pin (no tag) still counts as this image, and as the agent.
if run "$(deployment scadbuddy "$IMAGE@sha256:$(printf '1%.0s' {1..64})" "$AGENT_IMAGE@sha256:$(printf '3%.0s' {1..64})")"; then
  has_line "$agent_pinned" "$MANIFEST" || fail "bare digest: agent image not pinned"
  has_line "$pinned" "$MANIFEST" || fail "bare digest: image not pinned"
else
  fail "bare digest: step failed: $(grep '::error' "$work/log")"
fi

# 4. Two lines of THIS image stay an error: the guard is not weakened.
if run "$(deployment scadbuddy "$IMAGE:$old" "$IMAGE:$old")"; then
  fail "two api lines: step passed, expected an error"
else
  grep -q 'expected exactly one image line, found 2' "$work/log" \
    || fail "two api lines: wrong error: $(grep '::error' "$work/log")"
fi

# 5. The render worker's file, when present, is pinned too; it has no agent.
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")" "$(deployment scadbuddy-render "$IMAGE:$old")"; then
  has_line "$pinned" "$MANIFEST_WORKER" || fail "worker: image not pinned"
  has_line "$agent_pinned" "$MANIFEST" || fail "worker: agent image not pinned"
else
  fail "worker: step failed: $(grep '::error' "$work/log")"
fi

# 5b. The print worker's file (#1060), when present, is pinned too; it has no agent.
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")" "$(deployment scadbuddy-render "$IMAGE:$old")" "$(deployment scadbuddy-print "$IMAGE:$old")"; then
  has_line "$pinned" "$MANIFEST_PRINT" || fail "print worker: image not pinned"
  has_line "$pinned" "$MANIFEST_WORKER" || fail "print worker: render worker not pinned"
  grep -q "$MANIFEST_PRINT" "$work/out" || fail "print worker: not among the changed files"
else
  fail "print worker: step failed: $(grep '::error' "$work/log")"
fi

# 5c. The flow runs' worker's file (#1057), when present, is pinned too; it has no agent.
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")" "$(deployment scadbuddy-render "$IMAGE:$old")" "$(deployment scadbuddy-print "$IMAGE:$old")" "$(deployment scadbuddy-projects "$IMAGE:$old")"; then
  has_line "$pinned" "$MANIFEST_PROJECTS" || fail "projects worker: image not pinned"
  has_line "$pinned" "$MANIFEST_PRINT" || fail "projects worker: print worker not pinned"
  has_line "$pinned" "$MANIFEST_WORKER" || fail "projects worker: render worker not pinned"
  grep -q "$MANIFEST_PROJECTS" "$work/out" || fail "projects worker: not among the changed files"
else
  fail "projects worker: step failed: $(grep '::error' "$work/log")"
fi

# 5d. The storefront's file (#5284), when present, is pinned too; it has no agent.
extra_path=$MANIFEST_STOREFRONT
extra_content=$(deployment scadbuddy-storefront "$IMAGE:$old")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")" "$(deployment scadbuddy-render "$IMAGE:$old")"; then
  has_line "$pinned" "$MANIFEST_STOREFRONT" || fail "storefront: image not pinned"
  grep -q "$MANIFEST_STOREFRONT" "$work/out" || fail "storefront: not among the changed files"
else
  fail "storefront: step failed: $(grep '::error' "$work/log")"
fi
extra_path='' extra_content=''

# 5e. Without the storefront's file, the deploy pins the rest and says so.
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")" "$(deployment scadbuddy-render "$IMAGE:$old")"; then
  grep -q "${MANIFEST_STOREFRONT} is not in clusters yet" "$work/log" || fail "no storefront: no notice"
else
  fail "no storefront: step failed: $(grep '::error' "$work/log")"
fi

# 5f. The intake worker's file (#6133), when present, is pinned too; it has no agent.
extra_path=$MANIFEST_INTAKE
extra_content=$(deployment scadbuddy-intake "$IMAGE:$old")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")" "$(deployment scadbuddy-render "$IMAGE:$old")"; then
  has_line "$pinned" "$MANIFEST_INTAKE" || fail "intake: image not pinned"
  grep -q "$MANIFEST_INTAKE" "$work/out" || fail "intake: not among the changed files"
else
  fail "intake: step failed: $(grep '::error' "$work/log")"
fi
extra_path='' extra_content=''

# 5g. Without the intake worker's file, the deploy pins the rest and says so.
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")" "$(deployment scadbuddy-render "$IMAGE:$old")"; then
  grep -q "${MANIFEST_INTAKE} is not in clusters yet" "$work/log" || fail "no intake: no notice"
else
  fail "no intake: step failed: $(grep '::error' "$work/log")"
fi

# 6. A bare image with no tag or digest is REJECTED, on purpose: clusters pins
#    by digest, and a ref with neither is a hand edit this pipeline should not
#    silently adopt. It fails loudly rather than rewriting an unexpected line.
if run "$(deployment scadbuddy "$IMAGE")"; then
  fail "bare image: step passed, expected an error"
else
  grep -q 'expected exactly one image line, found 0' "$work/log" \
    || fail "bare image: wrong error: $(grep '::error' "$work/log")"
fi

# 7. Two agent lines are an error, like two API lines.
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent" "$agent")"; then
  fail "two agent lines: step passed, expected an error"
else
  grep -q 'expected exactly one agent image line, found 2' "$work/log" \
    || fail "two agent lines: wrong error: $(grep '::error' "$work/log")"
fi

# 7b. agent-durable (#1056) is optional: without its line the API and agent
#     deploy alone (case 2), and the step says so.
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  grep -q '^durable=none$' "$work/out" || fail "no durable: durable output is not none"
else
  fail "no durable: step failed: $(grep '::error' "$work/log")"
fi

# 7c. With its line, it is pinned to its own digest, and the agent's pattern
#     (a prefix of it) leaves it alone.
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent" "$durable")"; then
  has_line "$pinned" "$MANIFEST" || fail "with durable: api image not pinned"
  has_line "$agent_pinned" "$MANIFEST" || fail "with durable: agent image not pinned"
  has_line "$durable_pinned" "$MANIFEST" || fail "with durable: durable image not pinned"
  grep -q '^durable=pinned$' "$work/out" || fail "with durable: durable output is not pinned"
else
  fail "with durable: step failed: $(grep '::error' "$work/log")"
fi

# 7d. With its line but no published image (a build from before 5e): an error,
#     never a stale durable pin beside a new agent.
if AGENT_DURABLE_DIGEST="" run "$(deployment scadbuddy "$IMAGE:$old" "$agent" "$durable")"; then
  fail "durable, no image: step passed, expected an error"
else
  grep -q 'no image was published' "$work/log" \
    || fail "durable, no image: wrong error: $(grep '::error' "$work/log")"
fi

# 7e. A durable image line with no tag or digest is a near miss, not "not run".
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent" "$AGENT_DURABLE_IMAGE")"; then
  fail "bare durable: step passed, expected an error"
else
  grep -q 'has an image line for' "$work/log" \
    || fail "bare durable: wrong error: $(grep '::error' "$work/log")"
fi

# 7f. Two durable lines are an error.
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent" "$durable" "$durable")"; then
  fail "two durable lines: step passed, expected an error"
else
  grep -q 'expected exactly one agent-durable image line, found 2' "$work/log" \
    || fail "two durable lines: wrong error: $(grep '::error' "$work/log")"
fi

# 7g. In its own Deployment (scadbuddy-agent-durable.yaml), it is pinned there,
#     and the file is among the changed ones.
durable_manifest=$(printf '%s\n' '---' 'apiVersion: apps/v1' 'kind: Deployment' \
  'metadata:' '  name: scadbuddy-agent-durable' 'spec:' '  template:' '    spec:' \
  '      containers:' '        - name: agent-durable' "          image: $durable")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  has_line "$pinned" "$MANIFEST" || fail "durable deployment: api image not pinned"
  has_line "$durable_pinned" "$MANIFEST_AGENT_DURABLE" || fail "durable deployment: durable image not pinned"
  grep -q '^durable=pinned$' "$work/out" || fail "durable deployment: durable output is not pinned"
  grep -q "$MANIFEST_AGENT_DURABLE" "$work/out" || fail "durable deployment: not among the changed files"
else
  fail "durable deployment: step failed: $(grep '::error' "$work/log")"
fi

# 7h. Its own file AND a sidecar line in the API's manifest: a half-done move.
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent" "$durable")"; then
  fail "durable in both: step passed, expected an error"
else
  grep -q 'it belongs in one of them' "$work/log" \
    || fail "durable in both: wrong error: $(grep '::error' "$work/log")"
fi

# 7i. Its own file with no durable image line is an error, not "not run".
durable_manifest=$(printf '%s\n' '---' 'kind: Deployment' 'metadata:' '  name: scadbuddy-agent-durable')
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  fail "empty durable deployment: step passed, expected an error"
else
  grep -q "has no image line for $AGENT_DURABLE_IMAGE" "$work/log" \
    || fail "empty durable deployment: wrong error: $(grep '::error' "$work/log")"
fi
durable_manifest=

# 8. The slicer worker (#3822) has its own file, optional until clusters has it.
#    Absent: not pinned, output none. Present: pinned to this build's digest.
#    A build with no slicer image keeps the pin and warns (the one image whose
#    absence is not an error). A file with no slicer line is an error.
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  grep -q '^slicer=none$' "$work/out" || fail "no slicer file: slicer output is not none"
else
  fail "no slicer file: step failed: $(grep '::error' "$work/log")"
fi
slicer_manifest=$(deployment scadbuddy-slicer "$slicer_old")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  has_line "$pinned" "$MANIFEST" || fail "slicer: api image not pinned"
  has_line "$slicer_pinned" "$MANIFEST_SLICER" || fail "slicer: slicer image not pinned"
  grep -q '^slicer=pinned$' "$work/out" || fail "slicer: slicer output is not pinned"
  grep -q "$MANIFEST_SLICER" "$work/out" || fail "slicer: not among the changed files"
else
  fail "slicer: step failed: $(grep '::error' "$work/log")"
fi
if SLICER_DIGEST="" run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  has_line "$pinned" "$MANIFEST" || fail "slicer, no image: api image not pinned"
  has_line "$slicer_old" "$MANIFEST_SLICER" || fail "slicer, no image: old pin was not kept"
  grep -q '^slicer=kept$' "$work/out" || fail "slicer, no image: slicer output is not kept"
  grep -q '::warning.*keeping the slicer pin' "$work/log" || fail "slicer, no image: no warning: $(cat "$work/log")"
  grep -q "$MANIFEST_SLICER" "$work/out" && fail "slicer, no image: kept file listed as changed"
else
  fail "slicer, no image: step failed: $(grep '::error' "$work/log")"
fi
slicer_manifest=$(deployment scadbuddy-slicer "$slicer_old" "$slicer_old")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  fail "two slicer lines: step passed, expected an error"
else
  grep -q 'expected exactly one slicer image line, found 2' "$work/log" \
    || fail "two slicer lines: wrong error: $(grep '::error' "$work/log")"
fi
slicer_manifest=$(deployment scadbuddy-slicer "ghcr.io/eh-homelab/other:1")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  fail "slicer file without its line: step passed, expected an error"
else
  grep -q 'expected exactly one slicer image line, found 0' "$work/log" \
    || fail "slicer file without its line: wrong error: $(grep '::error' "$work/log")"
fi
slicer_manifest=

# 8a. The profile-fetch worker (#6625), pinned exactly as the slicer is: its own
#     file, optional until clusters has it; a build with no image keeps the pin.
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  grep -q '^profile_fetch=none$' "$work/out" || fail "no profile-fetch file: output is not none"
else
  fail "no profile-fetch file: step failed: $(grep '::error' "$work/log")"
fi
pf_manifest=$(deployment scadbuddy-profile-fetch "$pf_old")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  has_line "$pinned" "$MANIFEST" || fail "profile-fetch: api image not pinned"
  has_line "$pf_pinned" "$MANIFEST_PROFILE_FETCH" || fail "profile-fetch: image not pinned"
  grep -q '^profile_fetch=pinned$' "$work/out" || fail "profile-fetch: output is not pinned"
  grep -q "$MANIFEST_PROFILE_FETCH" "$work/out" || fail "profile-fetch: not among the changed files"
else
  fail "profile-fetch: step failed: $(grep '::error' "$work/log")"
fi
if PROFILE_FETCH_DIGEST="" run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  has_line "$pinned" "$MANIFEST" || fail "profile-fetch, no image: api image not pinned"
  has_line "$pf_old" "$MANIFEST_PROFILE_FETCH" || fail "profile-fetch, no image: old pin was not kept"
  grep -q '^profile_fetch=kept$' "$work/out" || fail "profile-fetch, no image: output is not kept"
  grep -q '::warning.*keeping the profile-fetch pin' "$work/log" || fail "profile-fetch, no image: no warning: $(cat "$work/log")"
  grep -q "$MANIFEST_PROFILE_FETCH" "$work/out" && fail "profile-fetch, no image: kept file listed as changed"
else
  fail "profile-fetch, no image: step failed: $(grep '::error' "$work/log")"
fi
pf_manifest=$(deployment scadbuddy-profile-fetch "$pf_old" "$pf_old")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  fail "two profile-fetch lines: step passed, expected an error"
else
  grep -q 'expected exactly one profile-fetch image line, found 2' "$work/log" \
    || fail "two profile-fetch lines: wrong error: $(grep '::error' "$work/log")"
fi
pf_manifest=$(deployment scadbuddy-profile-fetch "ghcr.io/eh-homelab/other:1")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  fail "profile-fetch file without its line: step passed, expected an error"
else
  grep -q 'expected exactly one profile-fetch image line, found 0' "$work/log" \
    || fail "profile-fetch file without its line: wrong error: $(grep '::error' "$work/log")"
fi
pf_manifest=

# 8b. Every other file that runs the agent is pinned too, found by the image
#     (#5037): the agent's worker Deployment, scadbuddy-agent-worker.yaml.
worker_path=applications/scadbuddy/scadbuddy-agent-worker.yaml
extra_path=$worker_path
extra_content=$(deployment scadbuddy-agent-worker "$agent")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  has_line "$agent_pinned" "$worker_path" || fail "agent worker: agent image not pinned"
  has_line "$agent_pinned" "$MANIFEST" || fail "agent worker: sidecar not pinned"
  grep -q "^files=.*$worker_path" "$work/out" || fail "agent worker: not among the changed files"
  rerun || fail "agent worker: rerun failed: $(grep '::error' "$work/log")"
  grep -qx 'unchanged=true' "$work/out" || fail "agent worker: rerun expected unchanged=true"
else
  fail "agent worker: step failed: $(grep '::error' "$work/log")"
fi
# ...in the overlay's directory too, which the step also scans.
overlay_worker=clusters/prod/scadbuddy/agent-worker.yaml
extra_path=$overlay_worker
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  has_line "$agent_pinned" "$overlay_worker" || fail "overlay agent worker: agent image not pinned"
  grep -q "^files=.*$overlay_worker" "$work/out" || fail "overlay agent worker: not among the changed files"
else
  fail "overlay agent worker: step failed: $(grep '::error' "$work/log")"
fi
extra_path=$worker_path
# A file that is not YAML fails the deploy, closed: what it runs cannot be checked.
extra_content=$'a: [unclosed'
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  fail "unparseable file: step passed, expected an error"
else
  grep -q "file=$worker_path::could not parse it" "$work/log" \
    || fail "unparseable file: wrong error: $(grep '::error' "$work/log")"
fi
# ...with exactly one line, as in the API's manifest.
extra_content=$(deployment scadbuddy-agent-worker "$agent" "$agent")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  fail "agent worker, two lines: step passed, expected an error"
else
  grep -q "file=$worker_path::expected exactly one agent image line, found 2" "$work/log" \
    || fail "agent worker, two lines: wrong error: $(grep '::error' "$work/log")"
fi
# ...and a line the pattern misses (quoted) is an error, not a skipped file.
extra_content=$(deployment scadbuddy-agent-worker "\"$agent\"")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  fail "agent worker, quoted: step passed, expected an error"
else
  grep -q "file=$worker_path::expected exactly one agent image line, found 0" "$work/log" \
    || fail "agent worker, quoted: wrong error: $(grep '::error' "$work/log")"
fi

# 8c. Nothing stays behind (#5037): a ScadBuddy image the step does not move,
#     in any file under the manifests' or the overlay's directory, fails it.
stale_fails() { # name path content expected-error
  local name=$1
  extra_path=$2
  extra_content=$3
  if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
    fail "$name: step passed, expected an error"
  else
    grep -qF "$4" "$work/log" || fail "$name: wrong error: $(grep '::error' "$work/log")"
  fi
}
not_moved="which this deploy does not move to $VERSION"
stale_fails "unknown worker of the api image" applications/scadbuddy/scadbuddy-new.yaml \
  "$(deployment scadbuddy-new "$IMAGE:$old")" "file=applications/scadbuddy/scadbuddy-new.yaml::names $IMAGE:$old, $not_moved"
stale_fails "unknown sibling image" applications/scadbuddy/scadbuddy-other.yaml \
  "$(deployment scadbuddy-other "$IMAGE-other:sha-1111111")" "names $IMAGE-other:sha-1111111, $not_moved"
stale_fails "agent-durable outside its files" applications/scadbuddy/scadbuddy-extra.yaml \
  "$(deployment scadbuddy-extra "$durable")" "names $durable, $not_moved"
stale_fails "slicer outside its file" applications/scadbuddy/scadbuddy-extra.yaml \
  "$(deployment scadbuddy-extra "$slicer_old")" "names $slicer_old, $not_moved"
stale_fails "profile-fetch outside its file" applications/scadbuddy/scadbuddy-extra.yaml \
  "$(deployment scadbuddy-extra "$pf_old")" "names $pf_old, $not_moved"
stale_fails "an overlay images: entry" clusters/prod/scadbuddy/images.yaml \
  "$(printf '%s\n' 'images:' "  - name: $IMAGE" "    newName: $IMAGE:sha-1111111")" "names $IMAGE:sha-1111111, $not_moved"
stale_fails "an alias of an old pin" applications/scadbuddy/scadbuddy-alias.yaml \
  "$(printf '%s\n' "x: &old $IMAGE:$old" 'y: *old')" "names $IMAGE:$old, $not_moved"
# A comment that quotes an image line, and another eh-homelab image, are not pins.
extra_path=applications/scadbuddy/temporal.yaml
extra_content=$(printf '%s\n' "# image: $IMAGE:$old" "image: ghcr.io/eh-homelab/temporal-server:1.32.1")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  has_line "$pinned" "$MANIFEST" || fail "comment and other image: api image not pinned"
else
  fail "comment and other image: step failed: $(grep '::error' "$work/log")"
fi
extra_path=
extra_content=

# 8d. The sessions' shell sandbox (#5864): every image: or value: line that names it, in any
#     scanned file (an overlay subdirectory too), moves to this build; a build with no sandbox
#     image keeps them and warns; a mention on any other kind of line fails.
sandbox_file() { # image-ref value-ref
  printf '%s\n' 'apiVersion: apps/v1' 'kind: Deployment' 'metadata:' '  name: sandbox-egress' 'spec:' '  template:' '    spec:' \
    '      containers:' '        - name: egress' "          image: $1" '          env:' '            - name: SCADBUDDY_SANDBOX_IMAGE' \
    "              value: $2"
}
extra_path=clusters/prod/scadbuddy/sandbox-egress.yaml
extra_content=$(sandbox_file "$sandbox_old" "$sandbox_old")
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  grep -qF "image: $sandbox_pinned" "$work/repo/$extra_path" || fail "sandbox: image line not pinned"
  grep -qF "value: $sandbox_pinned" "$work/repo/$extra_path" || fail "sandbox: value line not pinned"
  grep -q '^sandbox=pinned$' "$work/out" || fail "sandbox: output is not pinned"
  grep -q "$extra_path" "$work/out" || fail "sandbox: not among the changed files"
else
  fail "sandbox: step failed: $(grep '::error' "$work/log")"
fi
if SANDBOX_DIGEST="" run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  grep -qF "image: $sandbox_old" "$work/repo/$extra_path" || fail "sandbox, no image: old pin was not kept"
  grep -q '^sandbox=kept$' "$work/out" || fail "sandbox, no image: output is not kept"
  grep -q '::warning.*keeping the sandbox pins' "$work/log" || fail "sandbox, no image: no warning: $(cat "$work/log")"
else
  fail "sandbox, no image: step failed: $(grep '::error' "$work/log")"
fi
extra_content=$(printf '%s\n' "$(sandbox_file "$sandbox_old" "$sandbox_old")" '          args: ["--image", "'"$sandbox_old"'"]')
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  fail "sandbox on another kind of line: step passed, expected an error"
else
  grep -q "names $SANDBOX_IMAGE 3 time(s), but 2 of them" "$work/log" \
    || fail "sandbox on another kind of line: wrong error: $(grep '::error' "$work/log")"
fi
extra_path=
extra_content=
if run "$(deployment scadbuddy "$IMAGE:$old" "$agent")"; then
  grep -q '^sandbox=none$' "$work/out" || fail "no sandbox: output is not none"
else
  fail "no sandbox: step failed: $(grep '::error' "$work/log")"
fi

# The vendored dashboards (eh-homelab/clusters#3734). $work/src stands for
# deploy/grafana at REVISION as the fetch step leaves it; `vendored` (set by a
# case) makes `run` write clusters' copy at an old revision, with a dashboard
# the new revision dropped.
api_and_agent="$(deployment scadbuddy "$IMAGE:$old" "$agent")"
src_files() { # dir
  mkdir -p "$1"
  printf '%s\n' 'configMapGenerator:' '  - name: new' > "$1/kustomization.yaml"
  printf '%s\n' '{"uid": "scadbuddy", "v": 2}' > "$1/scadbuddy.json"
  printf '%s\n' '{"uid": "scadbuddy-added", "v": 2}' > "$1/scadbuddy-added.json"
}
rm -rf "$work/src" && src_files "$work/src"
export DASHBOARD_SOURCE="$work/src"
vendor_dir() { # writes clusters' copy into $work/repo
  local d="$work/repo/$DASHBOARD_DIR"
  mkdir -p "$d"
  printf '%s\n' 'configMapGenerator:' '  - name: old' > "$d/kustomization.yaml"
  printf '%s\n' '{"uid": "scadbuddy", "v": 1}' > "$d/scadbuddy.json"
  printf '%s\n' '{"uid": "scadbuddy-dropped", "v": 1}' > "$d/scadbuddy-dropped.json"
  printf '%s\n' "$old_ref" > "$d/.revision"
}
grafana_entry="  - grafana/"
vendored=
# `run_vendored [overlay]`: `run` with clusters' copy written before the commit.
run_vendored() {
  overlay=${1:-$(overlay_ns "$grafana_entry")}
  vendored=1
  run "$api_and_agent"
  local status=$?
  vendored=
  return "$status"
}
# `dashboard_fails <name> <expected-error> <overlay>`: the step must fail with that error.
dashboard_fails() {
  local name=$1 expected=$2
  if run_vendored "$3"; then
    fail "$name: step passed, expected an error"
  else
    grep -qF "$expected" "$work/log" || fail "$name: wrong error: $(grep '::error' "$work/log")"
  fi
}
same_as_src() { # name
  local f
  for f in "$work/src"/*; do
    cmp -s "$f" "$work/repo/$DASHBOARD_DIR/${f##*/}" || fail "$1: ${f##*/} is not byte-identical to the source"
  done
}

# 8. Not vendored: today's overlay with no grafana/ and no remote line. A
#    notice, the images pinned, the overlay untouched and not among the files.
overlay=$(overlay)
if run "$api_and_agent"; then
  grep -q "::notice::${DASHBOARD_DIR} is not in clusters" "$work/log" || fail "no dashboard: no notice"
  has_line "$pinned" "$MANIFEST" || fail "no dashboard: api image not pinned"
  (cd "$work/repo" && git diff --quiet -- "$KUSTOMIZATION") || fail "no dashboard: overlay changed"
  grep -qx 'dashboard=none' "$work/out" || fail "no dashboard: dashboard output is not none"
  grep -q "^files=.*\(kustomization\|grafana\)" "$work/out" && fail "no dashboard: overlay listed in files"
else
  fail "no dashboard: step failed: $(grep '::error' "$work/log")"
fi

# 9. A refresh: the copy's kustomization.yaml and *.json are replaced with the
#    revision's, byte for byte, a dashboard the revision dropped is gone, the
#    directory holds those files and .revision and nothing else, and .revision
#    is the revision and a newline. Every changed path is among the files, the
#    overlay is not (clusters' deploy-PR classifier allows only grafana/ to
#    change for the dashboards), and the PR step's `git add` of them stages the
#    whole change, the deletion included.
if run_vendored; then
  d="$work/repo/$DASHBOARD_DIR"
  same_as_src "refresh"
  [ ! -e "$d/scadbuddy-dropped.json" ] || fail "refresh: a dashboard the revision dropped survived"
  [ "$(cd "$d" && find . -mindepth 1 -maxdepth 1 -printf '%f\n' | sort)" = "$( (cd "$work/src" && find . -mindepth 1 -maxdepth 1 -printf '%f\n'; echo .revision) | sort)" ] \
    || fail "refresh: the copy holds more or less than deploy/grafana's files and .revision: $(ls -A "$d")"
  [ "$(od -An -v -c "$d/.revision" | tr -d ' \n')" = "${REVISION}\\n" ] \
    || fail "refresh: .revision is not exactly the revision and a newline: $(od -c "$d/.revision")"
  grep -qx 'dashboard=vendored' "$work/out" || fail "refresh: dashboard output is not vendored"
  for f in scadbuddy.json scadbuddy-added.json scadbuddy-dropped.json kustomization.yaml .revision; do
    grep -q "^files=.*$DASHBOARD_DIR/$f" "$work/out" || fail "refresh: $f not among the files"
  done
  grep -q "^files=.*$KUSTOMIZATION" "$work/out" && fail "refresh: the overlay listed in files"
  (cd "$work/repo" && git diff --quiet -- "$KUSTOMIZATION") || fail "refresh: the overlay changed"
  has_line "$pinned" "$MANIFEST" || fail "refresh: api image not pinned"
  (
    cd "$work/repo"
    read -ra files <<< "$(sed -n 's/^files=//p' "$work/out")"
    git add -- "${files[@]}"
    git diff --quiet -- "$DASHBOARD_DIR"
  ) || fail "refresh: git add of the files left part of the change unstaged"
  # The same deploy again, once clusters merged it: nothing to deploy.
  rerun || fail "refresh: rerun failed: $(grep '::error' "$work/log")"
  grep -qx 'unchanged=true' "$work/out" || fail "refresh: rerun expected unchanged=true"
  # Only the dashboards behind: the deploy PR moves just them.
  printf '%s\n' '{"uid": "scadbuddy", "v": 3}' > "$work/src/scadbuddy.json"
  rerun || fail "dashboards behind: step failed: $(grep '::error' "$work/log")"
  grep -qx 'unchanged=false' "$work/out" || fail "dashboards behind: expected unchanged=false"
  grep -qx "files=$DASHBOARD_DIR/scadbuddy.json" "$work/out" || fail "dashboards behind: expected only the dashboard in files: $(grep '^files=' "$work/out")"
  rm -rf "$work/src" && src_files "$work/src"
else
  fail "refresh: step failed: $(grep '::error' "$work/log")"
fi

# 9b. A rollback to a revision without deploy/grafana: the images move, the
#     copy stays as it is, with a warning.
if DASHBOARD_AT_REVISION=false run_vendored; then
  [ "$(cat "$work/repo/$DASHBOARD_DIR/.revision")" = "$old_ref" ] || fail "rollback: .revision moved"
  [ -f "$work/repo/$DASHBOARD_DIR/scadbuddy-dropped.json" ] || fail "rollback: the copy changed"
  (cd "$work/repo" && git diff --quiet -- "$DASHBOARD_DIR") || fail "rollback: the copy changed"
  grep -q '::warning .*has no deploy/grafana' "$work/log" || fail "rollback: no warning"
  grep -qx 'dashboard=kept' "$work/out" || fail "rollback: dashboard output is not kept"
  has_line "$pinned" "$MANIFEST" || fail "rollback: api image not pinned"
else
  fail "rollback: step failed: $(grep '::error' "$work/log")"
fi

# 10. The fetch step could not tell (#1179): a vendored copy is an error, since
#     it may be refreshed only from the revision's files, but an overlay with
#     no copy deploys the images as before.
if DASHBOARD_AT_REVISION=unknown run_vendored; then
  fail "presence unknown with a copy: step passed, expected an error"
else
  grep -qF "could not tell whether ${REVISION} has deploy/grafana" "$work/log" \
    || fail "presence unknown with a copy: wrong error: $(grep '::error' "$work/log")"
fi
overlay=$(overlay)
if DASHBOARD_AT_REVISION=unknown run "$api_and_agent"; then
  grep -qx 'dashboard=none' "$work/out" || fail "presence unknown without a copy: dashboard not none"
  has_line "$pinned" "$MANIFEST" || fail "presence unknown without a copy: api image not pinned"
else
  fail "presence unknown without a copy: step failed: $(grep '::error' "$work/log")"
fi

# 11. The old remote line, in any form, with or without the copy: an error
#     naming clusters#3734, never a silent images-only deploy.
old_line="  - https://github.com/eh-homelab/ScadBuddy//deploy/grafana?ref=${old_ref}"
remote="still names eh-homelab/ScadBuddy//deploy/grafana"
dashboard_fails "remote line beside the copy" "$remote" "$(overlay_ns "$grafana_entry" "$old_line")"
dashboard_fails "remote line with a timeout" "$remote" "$(overlay_ns "${old_line}&timeout=180s")"
dashboard_fails "remote .git form, lower case" "$remote" \
  "$(overlay_ns "  - https://github.com/eh-homelab/scadbuddy.git//deploy/grafana?ref=main")"
# A large overlay with the remote line near the top still fails: the check must
# not read grep's early exit (SIGPIPE to its writer, 141 under pipefail) as no match.
mapfile -t padding < <(for i in $(seq 6000); do printf '  - padding-resource-%05d.yaml\n' "$i"; done)
dashboard_fails "remote line atop a large overlay" "$remote" "$(overlay_ns "$old_line" "$grafana_entry" "${padding[@]}")"
# A comment that names the old source is not the remote line.
if run_vendored "$(overlay_ns "  # A copy of https://github.com/eh-homelab/ScadBuddy//deploy/grafana" "$grafana_entry  # was eh-homelab/ScadBuddy//deploy/grafana?ref=x")"; then
  grep -qx 'dashboard=vendored' "$work/out" || fail "comment naming the remote: not vendored"
else
  fail "comment naming the remote: step failed: $(grep '::error' "$work/log")"
fi
overlay=$(overlay_ns "$old_line")
if run "$api_and_agent"; then
  fail "remote line, no copy: step passed, expected an error"
else
  grep -qF "eh-homelab/clusters#3734" "$work/log" || fail "remote line, no copy: error does not name clusters#3734: $(grep '::error' "$work/log")"
fi

# 12. The revision's deploy/grafana holds something that is not
#     kustomization.yaml or `[a-z0-9-]+.json`: rejected, the copy untouched.
for bad in README.md Scadbuddy.json scadbuddy_api.json; do
  printf 'x\n' > "$work/src/$bad"
  dashboard_fails "bad name $bad" "deploy/grafana/$bad at ${REVISION} is not kustomization.yaml" "$(overlay_ns "$grafana_entry")"
  [ -f "$work/repo/$DASHBOARD_DIR/scadbuddy-dropped.json" ] || fail "bad name $bad: the copy changed"
  rm -f "$work/src/$bad"
done
mkdir "$work/src/panels"
dashboard_fails "subdirectory" "deploy/grafana/panels at ${REVISION} is not a regular file" "$(overlay_ns "$grafana_entry")"
rmdir "$work/src/panels"

# 12b. clusters' copy holds something other than deploy/grafana's files and
#      .revision (a README, a subdirectory): an error, never left beside the
#      refreshed copy.
for extra in NOTES.txt panels; do
  overlay=$(overlay_ns "$grafana_entry")
  rm -rf "$work/repo" && mkdir -p "$work/repo/applications/scadbuddy" "$work/repo/$(dirname "$KUSTOMIZATION")"
  printf '%s\n' "$api_and_agent" > "$work/repo/$MANIFEST"
  printf '%s\n' "$overlay" > "$work/repo/$KUSTOMIZATION"
  vendor_dir
  if [ "$extra" = panels ]; then
    mkdir "$work/repo/$DASHBOARD_DIR/panels" && printf 'x\n' > "$work/repo/$DASHBOARD_DIR/panels/a.json"
  else
    printf 'notes\n' > "$work/repo/$DASHBOARD_DIR/$extra"
  fi
  (cd "$work/repo" && git init -q)
  if rerun; then
    fail "extra $extra in the copy: step passed, expected an error"
  else
    grep -qF "$DASHBOARD_DIR/$extra::is not kustomization.yaml, a [a-z0-9-]+.json dashboard or .revision" "$work/log" \
      || fail "extra $extra in the copy: wrong error: $(grep '::error' "$work/log")"
  fi
done

# 13. A grafana/ that is not the vendored copy, or one the overlay does not list.
rm_revision() { rm "$work/repo/$DASHBOARD_DIR/.revision"; }
overlay=$(overlay_ns "$grafana_entry")
rm -rf "$work/repo" && mkdir -p "$work/repo/applications/scadbuddy" "$work/repo/$(dirname "$KUSTOMIZATION")"
printf '%s\n' "$api_and_agent" > "$work/repo/$MANIFEST"
printf '%s\n' "$overlay" > "$work/repo/$KUSTOMIZATION"
vendor_dir && rm_revision
(cd "$work/repo" && git init -q)
if rerun; then
  fail "no .revision: step passed, expected an error"
else
  grep -qF "has no .revision" "$work/log" || fail "no .revision: wrong error: $(grep '::error' "$work/log")"
fi
dashboard_fails "copy not listed" "lists grafana/ (or grafana, ./grafana/) 0 time(s)" "$(overlay_ns)"
dashboard_fails "copy listed twice" "lists grafana/ (or grafana, ./grafana/) 2 time(s)" "$(overlay_ns "$grafana_entry" "  - grafana")"
# `grafana`, `./grafana/` and `./grafana` are the same directory.
for entry in grafana ./grafana/ ./grafana; do
  if run_vendored "$(overlay_ns "  - $entry")"; then
    grep -qx 'dashboard=vendored' "$work/out" || fail "listed as $entry: not vendored"
  else
    fail "listed as $entry: step failed: $(grep '::error' "$work/log")"
  fi
done

# 14. The namespace guard (clusters#1596 Phase 5): the vendored ConfigMaps
#     keep cattle-dashboards only through an unsetOnly NamespaceTransformer.
dashboard_fails "plain namespace" "top-level namespace:" "$(overlay "$grafana_entry")"
dashboard_fails "transformer without unsetOnly" "NamespaceTransformer with unsetOnly: true" \
  "$(UNSET_ONLY=false overlay_ns "$grafana_entry")"
# ...as a map, and as the string "true", which does not count.
ns_map() {
  printf '%s\n' '---' 'kind: Kustomization' 'resources:' '  - ../../../applications/scadbuddy' "$grafana_entry" \
    'transformers:' '  - apiVersion: builtin' '    kind: NamespaceTransformer' '    metadata:' '      name: ns' \
    '    namespace: bambuddy' "    unsetOnly: $1"
}
if run_vendored "$(ns_map true)"; then
  grep -qx 'dashboard=vendored' "$work/out" || fail "map transformer: not vendored"
else
  fail "map transformer: step failed: $(grep '::error' "$work/log")"
fi
dashboard_fails "string \"true\"" "the string \"true\" does not count" "$(ns_map '"true"')"
# ...as a file the overlay names.
overlay=$(printf '%s\n' '---' 'kind: Kustomization' 'resources:' '  - ../../../applications/scadbuddy' "$grafana_entry" \
  'transformers:' '  - ns.yaml')
for ns_file_unset in true false; do
  rm -rf "$work/repo"
  mkdir -p "$work/repo/applications/scadbuddy" "$work/repo/$(dirname "$KUSTOMIZATION")"
  printf '%s\n' "$api_and_agent" > "$work/repo/$MANIFEST"
  printf '%s\n' "$overlay" > "$work/repo/$KUSTOMIZATION"
  ns_map "$ns_file_unset" | yq '.transformers[0]' > "$work/repo/$(dirname "$KUSTOMIZATION")/ns.yaml"
  vendor_dir
  (cd "$work/repo" && git init -q)
  if rerun; then
    if [ "$ns_file_unset" = true ]; then
      grep -qx 'dashboard=vendored' "$work/out" || fail "file transformer: not vendored"
    else
      fail "file transformer unsetOnly: false: step passed, expected an error"
    fi
  elif [ "$ns_file_unset" = true ]; then
    fail "file transformer: step failed: $(grep '::error' "$work/log")"
  fi
done
# ...and in a directory the overlay names: kustomize reads the transformer
# configs from that directory's kustomization `resources` (#1210), here one
# level down, beside a file that is not one.
overlay=$(printf '%s\n' '---' 'kind: Kustomization' 'resources:' '  - ../../../applications/scadbuddy' "$grafana_entry" \
  'transformers:' '  - ns')
for ns_dir_unset in true false; do
  rm -rf "$work/repo"
  ns_dir="$work/repo/$(dirname "$KUSTOMIZATION")/ns"
  mkdir -p "$work/repo/applications/scadbuddy" "$ns_dir/inner"
  printf '%s\n' "$api_and_agent" > "$work/repo/$MANIFEST"
  printf '%s\n' "$overlay" > "$work/repo/$KUSTOMIZATION"
  printf '%s\n' 'resources:' '  - labels.yaml' '  - inner' > "$ns_dir/kustomization.yaml"
  printf '%s\n' 'apiVersion: builtin' 'kind: LabelTransformer' 'metadata:' '  name: l' > "$ns_dir/labels.yaml"
  printf '%s\n' 'resources:' '  - ns.yaml' > "$ns_dir/inner/kustomization.yaml"
  ns_map "$ns_dir_unset" | yq '.transformers[0]' > "$ns_dir/inner/ns.yaml"
  vendor_dir
  (cd "$work/repo" && git init -q)
  if rerun; then
    if [ "$ns_dir_unset" = true ]; then
      grep -qx 'dashboard=vendored' "$work/out" || fail "directory transformer: not vendored"
    else
      fail "directory transformer unsetOnly: false: step passed, expected an error"
    fi
  elif [ "$ns_dir_unset" = true ]; then
    fail "directory transformer: step failed: $(grep '::error' "$work/log")"
  else
    grep -qF "NamespaceTransformer with unsetOnly: true" "$work/log" \
      || fail "directory transformer unsetOnly: false: wrong error: $(grep '::error' "$work/log")"
  fi
done

# 15. No overlay at all: an error, not a silent skip. clusters moved it, and
#     this run cannot tell whether the new one vendors the dashboards.
overlay=""
if run "$api_and_agent"; then
  fail "no overlay: step passed, expected an error"
else
  grep -qF "$KUSTOMIZATION is not in clusters" "$work/log" || fail "no overlay: wrong error: $(grep '::error' "$work/log")"
fi

# 20-25. The fetch step (step id `grafana`, #1179), with a stub `gh` that
#        answers from $work/gh-answers one line per call (`ok`, or an error
#        message; the last line repeats), and a `sleep` that returns at once.
#        On `ok`, a directory listing prints $GH_FILES/listing.json and a raw
#        file read prints $GH_FILES/<name>. Only a 404 of kustomization.yaml is
#        "absent"; any other error is retried, and one that persists, or a
#        file that does not hash to its listed blob, is `unknown`, which fails
#        the deploy only if clusters vendors the dashboards (10).
yq '.jobs[].steps[] | select(.id == "grafana") | .run' "$workflow" > "$work/grafana.sh"
[ -s "$work/grafana.sh" ] || { echo "FAIL: no step with id 'grafana' in $workflow" >&2; exit 1; }
mkdir -p "$work/bin"
cat > "$work/bin/gh" <<'STUB'
#!/usr/bin/env bash
n=$(( $(cat "$GH_CALLS" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$GH_CALLS"
answer=$(sed -n "${n}p" "$GH_ANSWERS")
[ -n "$answer" ] || answer=$(tail -n 1 "$GH_ANSWERS")
if [ "$answer" != ok ]; then
  echo "$answer" >&2
  exit 1
fi
raw=false path=
for a in "$@"; do
  case "$a" in
    *vnd.github.raw*) raw=true ;;
    repos/*) path=${a%%\?*} ;;
  esac
done
if [ "$raw" = true ]; then
  cat "$GH_FILES/${path##*/}"
elif [ "${path##*/}" = grafana ]; then
  cat "$GH_FILES/listing.json"
fi
STUB
printf '#!/bin/sh\n' > "$work/bin/sleep"
chmod +x "$work/bin/gh" "$work/bin/sleep"
# The listing GitHub gives for $work/src (blob shas from git hash-object),
# plus any extra entries passed as `<type>:<name>`.
listing() {
  local f
  {
    for f in "$work/src"/*; do
      jq -n --arg n "${f##*/}" --arg s "$(git hash-object "$f")" '{type: "file", name: $n, sha: $s}'
    done
    for f in "$@"; do
      jq -n --arg t "${f%%:*}" --arg n "${f#*:}" '{type: $t, name: $n, sha: "0000000000000000000000000000000000000000"}'
    done
  } | jq -s '.' > "$work/src-listing.json"
}
mkdir -p "$work/rt"
# `presence <answer>...`: run the step; "<exit>:<present>:<gh calls>".
presence() {
  local status=0
  printf '%s\n' "$@" > "$work/gh-answers"
  rm -f "$work/gh-calls" && : > "$work/out"
  rm -rf "$work/gh-files" && cp -R "$work/src" "$work/gh-files" && cp "$work/src-listing.json" "$work/gh-files/listing.json"
  PATH="$work/bin:$PATH" GH_ANSWERS="$work/gh-answers" GH_CALLS="$work/gh-calls" GH_FILES="$work/gh-files" \
    GITHUB_OUTPUT="$work/out" RUNNER_TEMP="$work/rt" GITHUB_REPOSITORY=eh-homelab/ScadBuddy \
    bash -e "$work/grafana.sh" > "$work/log" 2>&1 || status=$?
  printf '%s:%s:%s' "$status" "$(sed -n 's/^present=//p' "$work/out")" "$(cat "$work/gh-calls")"
}
fetched="$work/rt/grafana-at-revision"
not_found='gh: Not Found (HTTP 404)'
server_error='gh: Internal Server Error (HTTP 500)'
listing
# kustomization.yaml, the listing, then each of the three files.
got=$(presence ok)
[ "$got" = 0:true:5 ] || fail "fetch: present: got $got, expected 0:true:5: $(cat "$work/log")"
grep -qx "dir=$fetched" "$work/out" || fail "fetch: no dir output"
diff -r "$work/src" "$fetched" > /dev/null || fail "fetch: the fetched files differ from the source"
got=$(presence "$not_found")
[ "$got" = 0:false:1 ] || fail "fetch: 404: got $got, expected 0:false:1"
got=$(presence "$server_error" "$server_error" ok)
[ "$got" = 0:true:7 ] || fail "fetch: 500 then ok: got $got, expected 0:true:7"
got=$(presence "$server_error")
[ "$got" = 0:unknown:5 ] || fail "fetch: 500 every time: got $got, expected 0:unknown:5"
grep -qF '::warning::could not tell whether' "$work/log" || fail "fetch: 500 every time: no warning: $(cat "$work/log")"
# A file read that keeps failing is `unknown` too, not a partial copy.
got=$(presence ok ok ok "$server_error")
[ "$got" = 0:unknown:8 ] || fail "fetch: file read 500: got $got, expected 0:unknown:8"
# A file that does not hash to its listed blob: `unknown`.
cp "$work/src-listing.json" "$work/listing-good.json"
jq '(.[] | select(.name == "scadbuddy.json") | .sha) = "1111111111111111111111111111111111111111"' "$work/listing-good.json" > "$work/src-listing.json"
got=$(presence ok)
[ "$got" = 0:unknown:5 ] || fail "fetch: hash mismatch: got $got, expected 0:unknown:5"
grep -qF 'does not hash to its blob' "$work/log" || fail "fetch: hash mismatch: no warning: $(cat "$work/log")"
# A subdirectory and a bad name are not fetched; they reach the edit step as an
# empty directory and an empty file, and the edit step rejects them (12).
listing dir:panels file:README.md
got=$(presence ok)
[ "$got" = 0:true:5 ] || fail "fetch: odd entries: got $got, expected 0:true:5"
[ -d "$fetched/panels" ] || fail "fetch: the subdirectory did not reach the edit step"
[ -f "$fetched/README.md" ] || fail "fetch: the bad name did not reach the edit step"
if DASHBOARD_SOURCE="$fetched" run_vendored; then
  fail "fetch then edit, odd entries: step passed, expected an error"
else
  grep -qE "deploy/grafana/(panels|README\.md) at ${REVISION} is not" "$work/log" \
    || fail "fetch then edit, odd entries: wrong error: $(grep '::error' "$work/log")"
fi
# A name with a tab is refused up front, by name, before any file is read.
listing "file:$(printf 'a\tb.json')"
got=$(presence ok)
[ "$got" = 0:unknown:2 ] || fail "fetch: tab in a name: got $got, expected 0:unknown:2"
grep -qF 'with a tab, newline or slash' "$work/log" || fail "fetch: tab in a name: no warning: $(cat "$work/log")"
# An entry without a blob sha: `unknown`, with that reason, before any file is read.
listing
jq 'map(del(.sha))' "$work/src-listing.json" > "$work/listing-nosha.json" && mv "$work/listing-nosha.json" "$work/src-listing.json"
got=$(presence ok)
[ "$got" = 0:unknown:2 ] || fail "fetch: no sha: got $got, expected 0:unknown:2"
grep -qF 'without a 40-hex blob sha' "$work/log" || fail "fetch: no sha: no warning: $(cat "$work/log")"
# A listing at the contents API's 1,000-entry cap may be cut short: `unknown`.
jq -n '[range(1000) | {type: "file", name: "d\(.).json", sha: "0000000000000000000000000000000000000000"}]' > "$work/src-listing.json"
got=$(presence ok)
[ "$got" = 0:unknown:2 ] || fail "fetch: 1,000 entries: got $got, expected 0:unknown:2"
grep -qF "the contents API's cap" "$work/log" || fail "fetch: 1,000 entries: no warning: $(cat "$work/log")"
# ...and a clean fetch feeds a refresh end to end.
listing
presence ok > /dev/null
if DASHBOARD_SOURCE="$fetched" run_vendored; then
  same_as_src "fetch then edit"
else
  fail "fetch then edit: step failed: $(grep '::error' "$work/log")"
fi

if [ "$failures" -gt 0 ]; then
  echo "$failures case(s) failed" >&2
  exit 1
fi
echo "deploy-pin: all cases passed"
