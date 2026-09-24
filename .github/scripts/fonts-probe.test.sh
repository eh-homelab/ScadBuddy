#!/usr/bin/env bash
#
# Branch coverage for fonts-probe.sh, which decides whether the install-on-demand
# font test runs — a decision that sits behind the required `CI Summary` check,
# where a wrong verdict is invisible: it skips a test and stays green. The rule
# it has to keep is narrow and easy to get subtly wrong, and has been got wrong
# several times: ONLY an unreachable dependency skips, and everything this
# application does wrong reds.
#
# No docker and no network. `PROBE_EXEC` points the script at the stub below,
# which answers for the whole container, so every branch is reachable — including
# the ones a healthy CI run never takes.
#
# Runs in ci.yml's `lint` job.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
probe="$here/fonts-probe.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The stub stands in for `docker exec <container>`: argv is the command that
# would have run inside it. Each case is driven by a STUB_* variable.
cat > "$work/stub" <<'STUB'
#!/usr/bin/env bash
set -uo pipefail
case "$1" in
  python)
    if [ -n "${STUB_PYTHON_FAILS:-}" ]; then exit 1; fi
    printf '%s\n%s\n%s\n' "${STUB_BUDGET-70}" \
      "${STUB_METADATA_URL-https://fonts.google.com/metadata/fonts}" \
      "${STUB_REPO_URL-https://raw.githubusercontent.com/google/fonts/main}"
    ;;
  curl)
    url="${*: -1}"
    case "$url" in
      *metadata/fonts*)
        printf '%s' "${STUB_METADATA_STATUS-200}"
        ;;
      *raw.githubusercontent.com*)
        printf '%s' "${STUB_REPO_STATUS-404}"
        ;;
      *api/v1/fonts/catalogue*)
        # -w '%{http_code}' appends the status to the body.
        printf '%s%s' "${STUB_ROUTE_BODY-\{\"fonts\":[\{\"family\":\"Pacifico\"\}]\}}" \
          "${STUB_ROUTE_STATUS-200}"
        ;;
      *)
        echo "stub: unexpected url $url" >&2
        exit 99
        ;;
    esac
    ;;
  *)
    echo "stub: unexpected command $1" >&2
    exit 99
    ;;
esac
STUB
chmod +x "$work/stub"

failures=0
ran=0

# run <name> <expected offline value> <expected substring or -> [STUB_VAR=value ...]
run() {
  local name="$1" expect_offline="$2" expect_text="$3"
  shift 3
  ran=$((ran + 1))

  local out rc=0
  out="$(env -i PATH="$PATH" HOME="$HOME" PROBE_EXEC="$work/stub" "$@" bash "$probe" 2>&1)" || rc=$?

  local got
  got="$(printf '%s\n' "$out" | sed -n 's/^offline=//p' | tail -1)"

  if [ "$rc" -ne 0 ]; then
    echo "FAIL  ${name}: exited ${rc}"
    printf '%s\n' "$out" | sed 's/^/        /'
    failures=$((failures + 1))
    return
  fi
  if [ "$got" != "$expect_offline" ]; then
    echo "FAIL  ${name}: expected offline='${expect_offline}', got '${got}'"
    printf '%s\n' "$out" | sed 's/^/        /'
    failures=$((failures + 1))
    return
  fi
  if [ "$expect_text" != "-" ] && ! printf '%s' "$out" | grep -qF "$expect_text"; then
    echo "FAIL  ${name}: output did not mention '${expect_text}'"
    printf '%s\n' "$out" | sed 's/^/        /'
    failures=$((failures + 1))
    return
  fi
  if [ "$expect_text" = "-" ] && printf '%s' "$out" | grep -q '::warning::'; then
    echo "FAIL  ${name}: expected no warning, got one"
    printf '%s\n' "$out" | sed 's/^/        /'
    failures=$((failures + 1))
    return
  fi
  echo "ok    ${name}"
}

# ── A healthy run says nothing and skips nothing ─────────────────────────────
run "healthy: both upstreams answer, route serves Pacifico" "" "-"

# ── Only an unreachable DEPENDENCY may skip ──────────────────────────────────
run "catalogue upstream unreachable (000)" "1" "did not answer from the container (status 000)" \
  STUB_METADATA_STATUS=000
run "catalogue upstream 5xx" "1" "did not answer from the container (status 503)" \
  STUB_METADATA_STATUS=503
run "download host unreachable (000)" "1" "where the font files are downloaded from" \
  STUB_REPO_STATUS=000
run "download host 5xx" "1" "where the font files are downloaded from" \
  STUB_REPO_STATUS=500
run "download host 404 is still reachable" "" "-" \
  STUB_REPO_STATUS=404
# A rate-limited CDN is the dependency refusing us, not this repo failing: the
# install would hit the same wall, so reding here would blame the wrong party.
run "download host rate-limited (429)" "1" "where the font files are downloaded from" \
  STUB_REPO_STATUS=429
run "download host forbidden (403)" "1" "where the font files are downloaded from" \
  STUB_REPO_STATUS=403
run "download host unauthorized (401)" "1" "where the font files are downloaded from" \
  STUB_REPO_STATUS=401
run "download host request timeout (408)" "1" "where the font files are downloaded from" \
  STUB_REPO_STATUS=408
run "catalogue upstream rate-limited (429)" "1" "did not answer from the container (status 429)" \
  STUB_METADATA_STATUS=429
run "catalogue upstream 404 is still reachable" "" "-" \
  STUB_METADATA_STATUS=404

# ── Everything that is OURS must RED, never skip ─────────────────────────────
# The one that keeps being got wrong: the route maps a schema drift and an
# empty catalogue to 503 as well as a dead socket, so a 503 from US while the
# upstream answers is a bug, not an outage.
run "route 503 while the upstream answers (schema drift)" "" "will RUN and is expected to fail" \
  STUB_ROUTE_STATUS=503
run "route 500 (unhandled exception)" "" "That is ScadBuddy failing, not an outage" \
  STUB_ROUTE_STATUS=500
run "route never answers (backend wedged)" "" "will RUN and is expected to fail" \
  STUB_ROUTE_STATUS=000 STUB_ROUTE_BODY=
run "route answers 200 without the family" "" "does not name Pacifico" \
  STUB_ROUTE_BODY='{"fonts":[]}'

# ── The container read failing must be loud, not silent ──────────────────────
run "constants unreadable: falls back and says so" "" "using its own copies" \
  STUB_PYTHON_FAILS=1
run "constants unreadable does not mask an outage" "1" "using its own copies" \
  STUB_PYTHON_FAILS=1 STUB_METADATA_STATUS=000
run "non-numeric budget is rejected" "" "using its own copies" \
  STUB_BUDGET=soon

echo
if [ "$failures" -ne 0 ]; then
  echo "${failures} of ${ran} fonts-probe cases failed"
  exit 1
fi
echo "all ${ran} fonts-probe cases passed"
