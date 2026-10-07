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
#   - every target is TraceQL (queryType `traceql`) or PromQL (has `expr`);
#     anything else, a Tempo `traceqlSearch` target included, is rejected as an
#     unsupported target kind, since the checks below would not see it
#   - every `scadbuddy_*` series a query reads is one backend/scadbuddy/core/
#     metrics.py exposes, by its constructor: a Gauge as its name, a Counter as
#     `<name>_total`, a Histogram as `<name>_bucket`/`_sum`/`_count`, an Info as
#     `<name>_info` (prometheus_client's exposition names), so a `_total` on a
#     gauge or a bare counter is an error, not an empty panel
#   - every label a PromQL target's `by (...)`/`without (...)` or `{{label}}` legend
#     names is one a series in that query declares in its constructor's label list
#     (`le` comes with a `_bucket`), or one the scrape adds (namespace, pod,
#     instance, job)
#   - every TraceQL query names exactly one ScadBuddy `resource.service.name`
#     (spec §3; a query spanning services is rejected, so each span name is
#     checked against the service that says it), and every span `name="…"` in
#     it is one that service emits; `name=~`, `name!~` and `name!=` are
#     rejected, since this lint cannot check them. Emitted means, for
#     scadbuddy-worker, `render.<RenderStage>` or a literal passed to
#     `span(`/`detached_span(` under backend/scadbuddy/render/ (not submit.py),
#     workflows/ or worker.py; for scadbuddy-api, such a literal anywhere else
#     in backend/scadbuddy (render/submit.py included: render.submit,
#     render.reconcile). `openscad.export` is allowed on both, since the API
#     runs openscad too (schema and parameter checks). The in-process worker
#     mode (a dev setting) would put worker spans on the API; it is not modelled.
#     For
#     scadbuddy-agent and scadbuddy-web, a literal passed to a tracer call
#     (`startSpan(`, `startActiveSpan(`, `withSpan(`, `traceAction(`) or
#     assigned to a `*_SPAN` constant (the agent's `TURN_SPAN = 'agent.turn'`)
#     in a non-test source file under agent/src or frontend/src: files named
#     `*.test.*` and the `test`, `mocks` and `e2e` directories are excluded,
#     so a name that only a fixture or log line quotes does not count. That
#     search runs once the service's tracing module (agent/src/telemetry/setup.ts,
#     frontend/src/lib/tracing.ts) exists, and is noted as unchecked until then
#   - `kustomize build deploy/grafana` succeeds and yields exactly one
#     ConfigMap, `scadbuddy-dashboard` in `cattle-dashboards`, labelled
#     `grafana_dashboard: "1"`, whose `scadbuddy.json` is the file above
#
# $KUSTOMIZE names the binary (default `kustomize` on PATH); CI points it at
# the pinned release its own step installed. Needs jq, mikefarah yq v4 and python3.
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
  if [ "$want" = UNSUPPORTED ]; then
    problem "$rel" "$where: unsupported target kind (queryType \"$ds\"); a target is TraceQL (queryType traceql) or PromQL (expr)"
    continue
  fi
  [ "$ds" = "\${$want}" ] || problem "$rel" "$where: must use \${$want}, not \"$ds\""
done < <(jq -r "$panels"'
  [ (panels | . as $p | .targets[]?
      | if .queryType == "traceql" then ["panel \($p.id) target \(.refId)", "DS_TEMPO", (.datasource.uid // "")]
        elif has("expr") then ["panel \($p.id) target \(.refId)", "DS_PROMETHEUS", (.datasource.uid // "")]
        else ["panel \($p.id) target \(.refId)", "UNSUPPORTED", (.queryType // "")] end),
    (.templating.list[]? | select(.type == "query") | ["variable \(.name)", "DS_PROMETHEUS", (.datasource.uid // "")]) ]
  | .[] | @tsv' "$dash")

# Prometheus series against the registry in core/metrics.py: the names each
# declaration exposes, by its constructor, one `<series> <label>...` line each
# (the labels it declares, a list or tuple, positional or `labelnames=`). Read
# as Python rather than split on text, so how a declaration passes its registry
# cannot merge it with the next one (#1190, #1233).
exposed=$(python3 - "$metrics_py" <<'PY'
import ast, sys

suffixes = {
    "Gauge": lambda n: [n],
    "Counter": lambda n: [n.removesuffix("_total") + "_total"],
    "Histogram": lambda n: [n + "_bucket le", n + "_sum", n + "_count"],
    "Summary": lambda n: [n + "_sum", n + "_count"],
    "Info": lambda n: [n.removesuffix("_info") + "_info"],
}
for call in ast.walk(ast.parse(open(sys.argv[1]).read())):
    if not isinstance(call, ast.Call):
        continue
    kind = getattr(call.func, "id", getattr(call.func, "attr", None))
    if kind not in suffixes or not call.args:
        continue
    name = call.args[0]
    if not (isinstance(name, ast.Constant) and str(name.value).startswith("scadbuddy_")):
        continue
    labels = call.args[2] if len(call.args) > 2 else None
    labels = next((k.value for k in call.keywords if k.arg == "labelnames"), labels)
    names = []
    if isinstance(labels, (ast.List, ast.Tuple)):
        names = [e.value for e in labels.elts if isinstance(e, ast.Constant)]
    for series in suffixes[kind](name.value):
        series, *extra = series.split()
        print(" ".join([series, *names, *extra]))
PY
) || exposed=""
[ -n "$exposed" ] \
  || problem "backend/scadbuddy/core/metrics.py" "no scadbuddy_* metric declarations found"
series_labels() { # series -> its declared labels on one line; fails when undeclared
  awk -v s="$1" '$1 == s { $1 = ""; print; found = 1 } END { exit !found }' <<< "$exposed"
}
while read -r series; do
  [ -n "$series" ] || continue
  series_labels "$series" > /dev/null \
    || problem "$rel" "$series is not a metric backend/scadbuddy/core/metrics.py declares"
done < <(jq -r "$panels"' [panels | .targets[]? | .expr // empty] + [.templating.list[]? | .query | objects | .query // empty] | .[]' "$dash" \
  | grep -oE 'scadbuddy_[a-z0-9_]+' | sort -u)

# The labels a target's by()/without() grouping and {{label}} legend name must
# be ones a series in its query declares (`le` comes with a _bucket), or ones
# Prometheus adds when it scrapes (namespace, pod, instance, job): a misspelt
# label would collapse the panel into one unlabelled series without an error.
while IFS=$'\t' read -r where expr legend; do
  allowed=" namespace pod instance job "
  while read -r series; do
    [ -n "$series" ] || continue
    allowed+="$(series_labels "$series" || true) "
  done < <(grep -oE 'scadbuddy_[a-z0-9_]+' <<< "$expr" | sort -u)
  while read -r group; do
    [ -n "$group" ] || continue
    read -ra group_labels <<< "$(sed -E 's/^[a-z]+\s*\(//; s/\)$//; s/,/ /g' <<< "$group")"
    for label in "${group_labels[@]}"; do
      [[ "$allowed" == *" $label "* ]] \
        || problem "$rel" "$where: $group: \"$label\" is not a label of the series the query reads"
    done
  done < <(grep -oE '\b(by|without)\s*\([^)]*\)' <<< "$expr")
  while read -r ref; do
    [ -n "$ref" ] || continue
    label=$(tr -d '{} ' <<< "$ref")
    [[ "$allowed" == *" $label "* ]] \
      || problem "$rel" "$where: legend $ref: \"$label\" is not a label of the series the query reads"
  done < <(grep -oE '\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}' <<< "$legend")
done < <(jq -r "$panels"' panels | . as $p | .targets[]? | select(has("expr"))
  | ["panel \($p.id) target \(.refId)", .expr, (.legendFormat // "")] | @tsv' "$dash")

# The span names the backend emits: one per render stage (render/jobs.py's
# `render.{name}`), and every literal its code passes to span()/detached_span().
span_literals() { # python files... -> the literals passed to span()/detached_span()
  cat "$@" | tr '\n' ' ' \
    | grep -oE '(^|[^A-Za-z_])(detached_)?span\(\s*"[^"]+"' | grep -oE '"[^"]+"' | tr -d '"' || true
}
bk="$root/backend/scadbuddy"
# The worker runs the render pipeline: render/ (but for submit.py, which the API
# calls), workflows/ and worker.py.
mapfile -t worker_files < <(
  { find "$bk/render" "$bk/workflows" -name '*.py' ! -path "$bk/render/submit.py"; echo "$bk/worker.py"; } | sort
)
mapfile -t api_files < <(
  find "$bk" -name '*.py' ! -path "$bk/render/*" ! -path "$bk/workflows/*" ! -path "$bk/worker.py"
  echo "$bk/render/submit.py"
)
worker_names=$(
  {
    grep -E '^RenderStage = Literal\[' "$metrics_py" | grep -oE '"[a-z_]+"' | tr -d '"' | sed 's/^/render./' || true
    span_literals "${worker_files[@]}"
  } | sort -u
)
[ -n "$worker_names" ] \
  || problem "backend/scadbuddy" "scadbuddy-worker has no RenderStage and passes no literal to span()/detached_span()"
api_names=$(span_literals "${api_files[@]}" | sort -u)
[ -n "$api_names" ] \
  || problem "backend/scadbuddy" "scadbuddy-api passes no literal to span()/detached_span()"
# openscad.export is run_openscad's span, and the API calls run_openscad too
# (library/scad.py, api/params.py, api/models.py): both services emit it.
both_names="openscad.export"
# The names a TS tree passes to a tracer, outside its tests and fixtures (the
# header says which calls and files count).
ts_names() { # source dir
  find "$1" \( -name test -o -name mocks -o -name e2e \) -prune -o \
    -type f \( -name '*.ts' -o -name '*.tsx' \) ! -name '*.test.*' -print0 \
    | xargs -0 cat | tr '\n' ' ' \
    | grep -oE "((startSpan|startActiveSpan|withSpan|traceAction)\(\s*|\b[A-Z][A-Z0-9_]*_SPAN\s*(:\s*[A-Za-z]+\s*)?=\s*)['\"\`][^'\"\`]+['\"\`]" \
    | grep -oE "['\"\`][^'\"\`]+['\"\`]\$" | tr -d "'\"\`" | sort -u
}
# Each TS service's names, found once on first use.
declare -A ts_cache=()
unchecked=""
emitted() { # service name -> 0 when that service emits a span of that name
  local service=$1 name=$2 src probe
  case "$service" in
    scadbuddy-api) grep -qxF "$name" <<< "$api_names"$'\n'"$both_names"; return ;;
    scadbuddy-worker) grep -qxF "$name" <<< "$worker_names"$'\n'"$both_names"; return ;;
    scadbuddy-agent) src="$root/agent/src" probe="$root/agent/src/telemetry/setup.ts" ;;
    scadbuddy-web) src="$root/frontend/src" probe="$root/frontend/src/lib/tracing.ts" ;;
  esac
  if [ ! -f "$probe" ]; then
    [[ " $unchecked " == *" $service "* ]] || unchecked+=" $service"
    return 0
  fi
  [[ -v "ts_cache[$service]" ]] || ts_cache[$service]=$(ts_names "$src" || true)
  grep -qxF "$name" <<< "${ts_cache[$service]}"
}

while IFS=$'\t' read -r where query; do
  mapfile -t services < <(grep -oE 'resource\.service\.name\s*=\s*"[^"]*"' <<< "$query" | sed -E 's/.*"(.*)"/\1/' | sort -u)
  if [ "${#services[@]}" -eq 0 ]; then
    problem "$rel" "$where: a TraceQL query must filter on resource.service.name"
    continue
  fi
  if [ "${#services[@]}" -gt 1 ]; then
    problem "$rel" "$where: a TraceQL query must name one resource.service.name, not ${services[*]}"
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
  if grep -qE '(^|[^.A-Za-z_])name\s*(=~|!~|!=)' <<< "$query"; then
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
