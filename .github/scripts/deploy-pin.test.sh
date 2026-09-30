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
export MANIFEST=applications/scadbuddy/scadbuddy.yaml
export MANIFEST_WORKER=applications/scadbuddy/scadbuddy-render.yaml
export VERSION=sha-2222222
DIGEST="sha256:$(printf '2%.0s' {1..64})"
REVISION="$(printf '2%.0s' {1..40})"
AGENT_DIGEST="sha256:$(printf '4%.0s' {1..64})"
export DIGEST REVISION AGENT_DIGEST
export SOURCE_URL="https://github.com/eh-homelab/ScadBuddy/commit/$REVISION"
old="sha-1111111@sha256:$(printf '1%.0s' {1..64})"
agent="$AGENT_IMAGE:sha-3333333@sha256:$(printf '3%.0s' {1..64})"
pinned="$IMAGE:$VERSION@$DIGEST"
agent_pinned="$AGENT_IMAGE:$VERSION@$AGENT_DIGEST"
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

if [ "$failures" -gt 0 ]; then
  echo "$failures case(s) failed" >&2
  exit 1
fi
echo "deploy-pin: all cases passed"
