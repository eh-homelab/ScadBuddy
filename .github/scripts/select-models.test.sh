#!/usr/bin/env bash
# Tests for select-models.sh — which templates' verify.sh the `models` job runs
# (#229). Both directions matter: a changed template must be selected, and an
# unrelated change must not pull the whole catalogue in.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/select-models.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/models/alpha" "$tmp/models/beta" "$tmp/models/no-verify"
touch "$tmp/models/alpha/verify.sh" "$tmp/models/beta/verify.sh" "$tmp/models/no-verify/model.scad"
# gamma has a pipeline its verify.sh runs; no-verify has one but nothing checks it.
mkdir -p "$tmp/models/gamma/pipeline" "$tmp/models/no-verify/pipeline"
touch "$tmp/models/gamma/verify.sh" "$tmp/models/gamma/pipeline/verify-inputs.json"

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

# "<exit>:<slugs, space-joined>" so a test asserts on both at once.
run() {
  local out status
  set +e
  out="$(printf '%s' "$2" | MODELS_DIR="$tmp/models" "$script" "$1" 2>/dev/null)"
  status=$?
  set -e
  printf '%s:%s' "$status" "$(printf '%s' "$out" | tr '\n' ' ' | sed 's/ $//')"
}

# MODELS_DIR is an absolute path in these tests, so changed paths carry it too.
m="$tmp/models"

check 'all selects every template with a verify.sh' \
  '0:alpha beta gamma' "$(run all '')"

check 'a change inside one template selects only it' \
  '0:alpha' "$(run changed "$m/alpha/model.scad")"

check 'a change deep inside a template selects it' \
  '0:beta' "$(run changed "$m/beta/assets/x/thumbnail.png")"

check 'two templates, reported once each and sorted' \
  '0:alpha beta' "$(run changed "$m/beta/README.md
$m/alpha/model.scad
$m/alpha/verify.sh")"

check 'no model touched selects nothing, and does not fail' \
  '0:' "$(run changed 'backend/scadbuddy/main.py
frontend/src/App.tsx')"

check 'an empty change list selects nothing, and does not fail' \
  '0:' "$(run changed '')"

check 'a template without verify.sh is not selected' \
  '0:' "$(run changed "$m/no-verify/model.scad")"

check 'a template the PR deleted is not selected' \
  '0:' "$(run changed "$m/gone/verify.sh")"

check 'a file directly under models/ selects nothing' \
  '0:' "$(run changed "$m/README.md")"

check 'a Dockerfile change runs every template' \
  '0:alpha beta gamma' "$(run changed 'Dockerfile')"

check 'a ci.yml change runs every template' \
  '0:alpha beta gamma' "$(run changed '.github/workflows/ci.yml')"

check 'a change to the selector itself runs every template' \
  '0:alpha beta gamma' "$(run changed '.github/scripts/select-models.sh')"

check 'a change to the runner runs every template' \
  '0:alpha beta gamma' "$(run changed '.github/scripts/verify-models.sh')"

check 'tooling is matched on the whole path, not a substring' \
  '0:' "$(run changed 'docs/Dockerfile.md')"

check 'a change to the pipeline engine also runs every template with a pipeline' \
  '0:alpha gamma' "$(run changed "backend/scadbuddy/workflows/pipelines.py
$m/alpha/model.scad")"

check 'a render change runs the pipeline templates' \
  '0:gamma' "$(run changed 'backend/scadbuddy/render/split.py')"

check 'a backend lockfile change runs the pipeline templates' \
  '0:gamma' "$(run changed 'backend/uv.lock')"

check 'any other backend change still selects nothing' \
  '0:' "$(run changed 'backend/scadbuddy/api/models.py')"

check 'an engine prefix matches from the start of the path only' \
  '0:' "$(run changed 'docs/backend/scadbuddy/workflows/x.md')"

check 'a selection with a pipeline template builds the pipeline image' \
  '0:gamma' "$(run pipelines 'alpha
gamma')"

check 'a selection without one does not' \
  '0:' "$(run pipelines 'alpha
beta')"

check 'an unknown mode is a usage error' \
  '2:' "$(run bogus '')"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
