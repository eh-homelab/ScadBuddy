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
# The dashboard's `ref=` in clusters/prod/scadbuddy/kustomization.yaml (#988,
# spec 2026-10-01 §7) is optional: an overlay that never mentions
# deploy/grafana deploys the images alone, one exact line is moved to the
# revision, and a near miss or a second line fails the deploy.
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
export KUSTOMIZATION=clusters/prod/scadbuddy/kustomization.yaml
export DASHBOARD_RESOURCE=https://github.com/eh-homelab/ScadBuddy//deploy/grafana
# What the step before it found: deploy/grafana exists at REVISION.
export DASHBOARD_AT_REVISION=true
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

# `overlay [line]...` prints the clusters overlay's kustomization with the given
# extra resource lines; with none, it is today's overlay, which has no
# dashboard pin. `run` writes $overlay, which a case sets before calling it.
overlay() {
  printf '%s\n' '---' 'kind: Kustomization' 'namespace: bambuddy' 'resources:' \
    '  - ../../../applications/scadbuddy' "$@"
}
# The overlay a dashboard line needs: no top-level `namespace:`, which would
# move the ConfigMap out of cattle-dashboards, but a NamespaceTransformer with
# unsetOnly (clusters#1596 Phase 5).
overlay_ns() {
  printf '%s\n' '---' 'kind: Kustomization' 'resources:' \
    '  - ../../../applications/scadbuddy' "$@" 'transformers:' '  - |-' \
    '    apiVersion: builtin' '    kind: NamespaceTransformer' \
    '    metadata:' '      name: ns' '    namespace: bambuddy' "    unsetOnly: ${UNSET_ONLY:-true}"
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
overlay=$(overlay_ns "${dashboard_line}${old_ref}")
if run "$api_and_agent"; then
  grep -qxF "${dashboard_line}${REVISION}" "$work/repo/$KUSTOMIZATION" || fail "dashboard: ref not moved"
  grep -qF "$old_ref" "$work/repo/$KUSTOMIZATION" && fail "dashboard: old ref survived"
  grep -qx 'dashboard=pinned' "$work/out" || fail "dashboard: dashboard output is not pinned"
  grep -q "^files=.*$KUSTOMIZATION" "$work/out" || fail "dashboard: overlay not listed in files"
  has_line "$pinned" "$MANIFEST" || fail "dashboard: api image not pinned"
else
  fail "dashboard: step failed: $(grep '::error' "$work/log")"
fi

# 9b. A revision without deploy/grafana: the pin does not move, the deploy fails.
overlay=$(overlay_ns "${dashboard_line}${old_ref}")
if DASHBOARD_AT_REVISION=false run "$api_and_agent"; then
  fail "dashboard missing at revision: step passed, expected an error"
else
  grep -qF "has no deploy/grafana/kustomization.yaml" "$work/log" \
    || fail "dashboard missing at revision: wrong error: $(grep '::error' "$work/log")"
fi

# 10. The same deploy again, once clusters merged the first: nothing to deploy.
overlay=$(overlay_ns "${dashboard_line}${old_ref}")
if run "$api_and_agent" && rerun; then
  grep -qx 'unchanged=true' "$work/out" || fail "dashboard current: expected unchanged=true"
else
  fail "dashboard current: step failed: $(grep '::error' "$work/log")"
fi

# 11. Only the dashboard is behind (clusters added the line after the last
#     deploy): the deploy PR moves just the ref.
overlay=$(overlay_ns)
if run "$api_and_agent" \
  && yq -i ".resources += [\"$DASHBOARD_RESOURCE?ref=$old_ref\"]" "$work/repo/$KUSTOMIZATION" \
  && rerun; then
  grep -qx 'unchanged=false' "$work/out" || fail "dashboard behind: expected unchanged=false"
  grep -qx "files=$KUSTOMIZATION" "$work/out" || fail "dashboard behind: expected only the overlay in files"
else
  fail "dashboard behind: step failed: $(grep '::error' "$work/log")"
fi

# 11b. The same line under transformers: instead of resources: is a mention,
#      not the pin.
overlay=$(printf '%s\n' '---' 'kind: Kustomization' 'resources:' '  - ../../../applications/scadbuddy' \
  'transformers:' "${dashboard_line}${old_ref}")
if run "$api_and_agent"; then
  fail "line under transformers: step passed, expected an error"
else
  grep -qF "$near_miss" "$work/log" || fail "line under transformers: wrong error: $(grep '::error' "$work/log")"
fi

# 11c. The NamespaceTransformer as a map, and as a file the overlay names.
ns_map() {
  printf '%s\n' '---' 'kind: Kustomization' 'resources:' '  - ../../../applications/scadbuddy' "${dashboard_line}${old_ref}" \
    'transformers:' '  - apiVersion: builtin' '    kind: NamespaceTransformer' '    metadata:' '      name: ns' \
    '    namespace: bambuddy' "    unsetOnly: $1"
}
overlay=$(ns_map true)
if run "$api_and_agent"; then
  grep -qx 'dashboard=pinned' "$work/out" || fail "map transformer: not pinned"
else
  fail "map transformer: step failed: $(grep '::error' "$work/log")"
fi
overlay=$(ns_map '"true"')
if run "$api_and_agent"; then
  fail "string \"true\": step passed, expected an error"
else
  grep -qF "the string \"true\" does not count" "$work/log" || fail "string true: wrong error: $(grep '::error' "$work/log")"
fi
overlay=$(printf '%s\n' '---' 'kind: Kustomization' 'resources:' '  - ../../../applications/scadbuddy' "${dashboard_line}${old_ref}" \
  'transformers:' '  - ns.yaml')
for ns_file_unset in true false; do
  rm -rf "$work/repo"
  mkdir -p "$work/repo/applications/scadbuddy" "$work/repo/$(dirname "$KUSTOMIZATION")"
  printf '%s\n' "$api_and_agent" > "$work/repo/$MANIFEST"
  printf '%s\n' "$overlay" > "$work/repo/$KUSTOMIZATION"
  ns_map "$ns_file_unset" | yq '.transformers[0]' > "$work/repo/$(dirname "$KUSTOMIZATION")/ns.yaml"
  (cd "$work/repo" && git init -q)
  if rerun; then
    if [ "$ns_file_unset" = true ]; then
      grep -qx 'dashboard=pinned' "$work/out" || fail "file transformer: not pinned"
    else
      fail "file transformer unsetOnly: false: step passed, expected an error"
    fi
  elif [ "$ns_file_unset" = true ]; then
    fail "file transformer: step failed: $(grep '::error' "$work/log")"
  fi
done

# 12-17. Near misses are errors, never "not configured".
dashboard_fails "short sha" "$near_miss" "${dashboard_line}1111111"
dashboard_fails "branch ref" "$near_miss" "${dashboard_line}main"
dashboard_fails "trailing comment" "$near_miss" "${dashboard_line}${old_ref} # dashboard"
dashboard_fails "lower case" "$near_miss" "  - https://github.com/eh-homelab/scadbuddy//deploy/grafana?ref=${old_ref}"
dashboard_fails "commented out" "$near_miss" "  # - $DASHBOARD_RESOURCE?ref=${old_ref}"
dashboard_fails "extra space" "$near_miss" "  -  $DASHBOARD_RESOURCE?ref=${old_ref}"
dashboard_fails ".git form" "$near_miss" "  - https://github.com/eh-homelab/ScadBuddy.git//deploy/grafana?ref=${old_ref}"
# ...and beside an exact line they are errors too: a second, unpinned copy.
dashboard_fails "exact line plus .git near miss" "times but only 1 line is exactly" \
  "${dashboard_line}${old_ref}" "  - https://github.com/eh-homelab/ScadBuddy.git//deploy/grafana?ref=main"
dashboard_fails "exact line plus commented-out line" "times but only 1 line is exactly" \
  "${dashboard_line}${old_ref}" "  # - $DASHBOARD_RESOURCE?ref=${old_ref}"

# 17b. The line beside a plain namespace: the ConfigMap would leave
#      cattle-dashboards and the sidecar never see it: an error.
dashboard_fails "plain namespace" "top-level namespace:" "${dashboard_line}${old_ref}"
overlay=$(UNSET_ONLY=false overlay_ns "${dashboard_line}${old_ref}")
if run "$api_and_agent"; then
  fail "transformer without unsetOnly: step passed, expected an error"
else
  grep -qF "NamespaceTransformer with unsetOnly: true" "$work/log" || fail "no unsetOnly: wrong error: $(grep '::error' "$work/log")"
fi

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

if [ "$failures" -gt 0 ]; then
  echo "$failures case(s) failed" >&2
  exit 1
fi
echo "deploy-pin: all cases passed"
