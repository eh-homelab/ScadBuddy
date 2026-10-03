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
