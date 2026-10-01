#!/usr/bin/env bash
#
# Decide which bundled templates' `models/<slug>/verify.sh` the `models` job in
# ci.yml runs (#229). Prints one slug per line on stdout, sorted, and the
# reason for the choice on stderr.
#
#   select-models.sh all              every models/<slug>/ that has a verify.sh
#   select-models.sh changed < FILES  the templates a PR touches; FILES is the
#                                     PR's changed paths, one per line
#   select-models.sh pipelines < SLUGS
#                                     of a selection, the templates whose
#                                     verify.sh runs their pipeline (they have a
#                                     pipeline/verify-inputs.json): the `models`
#                                     job builds the image that check needs only
#                                     when there is one
#
# The rules, in order:
#
#   1. A change to the verify TOOLING runs everything. The image the scripts
#      render in is the Dockerfile's `base` stage (OpenSCAD plus the font
#      packages), so a Dockerfile edit — an OPENSCAD_VERSION bump above all —
#      can move every template's geometry at once. ci.yml and the two scripts
#      that select and run the checks are tooling for the same reason: a PR
#      that edits how templates are checked has to show that every one of them
#      still passes under the new rules.
#   2. Otherwise a template runs when any path under models/<slug>/ changed and
#      models/<slug>/verify.sh exists in this tree. A template deleted by the PR
#      has no verify.sh left and drops out on its own.
#   3. A change to the pipeline engine (backend/scadbuddy/workflows/,
#      backend/scadbuddy/render/, backend/scadbuddy/template.py, or the backend
#      lockfile) also selects every template that has a pipeline/: their verify.sh
#      runs that engine with real OpenSCAD (spec 2026-09-27 §5.5).
#   4. Nothing else selects anything. No model reaches outside its own directory
#      (`use <../...>` appears in none of them), so any other backend or frontend
#      change cannot move a template's geometry.
#
# Deciding WHEN to call `changed` (pull_request, and only when the diff could be
# computed) is the caller's job; on any doubt it calls `all`. A selector that
# fails open to "everything" costs minutes; one that fails open to "nothing"
# turns a broken template into a green check.
#
# Its branches are covered by select-models.test.sh, which runs in the `lint`
# job.
set -euo pipefail

MODELS_DIR="${MODELS_DIR:-models}"

TOOLING=(
  Dockerfile
  .github/workflows/ci.yml
  .github/scripts/select-models.sh
  .github/scripts/verify-models.sh
)

ENGINE=(
  backend/scadbuddy/workflows/
  backend/scadbuddy/render/
  backend/scadbuddy/template.py
  backend/uv.lock
)

pipeline_models() {
  local f
  for f in "$MODELS_DIR"/*/pipeline; do
    if [ -d "$f" ] && [ -f "$(dirname "$f")/verify.sh" ]; then
      basename "$(dirname "$f")"
    fi
  done
}

all_models() {
  local f
  for f in "$MODELS_DIR"/*/verify.sh; do
    [ -f "$f" ] || continue
    basename "$(dirname "$f")"
  done | sort
}

mode="${1:-}"
case "$mode" in
  all)
    echo "select-models: all templates" >&2
    all_models
    ;;
  changed)
    changed="$(cat)"
    for t in "${TOOLING[@]}"; do
      if printf '%s\n' "$changed" | grep -qxF -- "$t"; then
        echo "select-models: $t changed, so every template runs" >&2
        all_models
        exit 0
      fi
    done
    # `sed -n .../p`, not grep: a PR that touches no model matches nothing,
    # and a no-match grep exits 1, which `set -e` turns into an abort on
    # exactly the path most PRs take. sed exits 0 either way.
    slugs="$(printf '%s\n' "$changed" \
      | sed -n "s|^${MODELS_DIR}/\([^/][^/]*\)/.*|\1|p" \
      | sort -u)"
    for e in "${ENGINE[@]}"; do
      # A prefix for a directory, the whole path for a file. awk, not grep -q: a
      # PR that touches none of them matches nothing, which must not be an error.
      if [ -n "$(printf '%s\n' "$changed" | awk -v e="$e" 'index($0, e) == 1' | head -n 1)" ]; then
        echo "select-models: $e changed, so every template with a pipeline runs" >&2
        slugs="$(printf '%s\n%s\n' "$slugs" "$(pipeline_models)" | sed '/^$/d' | sort -u)"
        break
      fi
    done
    selected=0
    for s in $slugs; do
      if [ -f "$MODELS_DIR/$s/verify.sh" ]; then
        echo "$s"
        selected=$((selected + 1))
      elif [ -d "$MODELS_DIR/$s" ]; then
        echo "::warning::models/$s changed but has no verify.sh, so nothing checks its geometry" >&2
      fi
    done
    echo "select-models: $selected changed template(s)" >&2
    ;;
  pipelines)
    # `|| [ -n "$s" ]`: a last line without a newline is still a slug.
    while IFS= read -r s || [ -n "$s" ]; do
      if [ -n "$s" ] && [ -f "$MODELS_DIR/$s/pipeline/verify-inputs.json" ]; then
        echo "$s"
      fi
    done | sort -u
    ;;
  *)
    echo "usage: $0 all | changed < changed-files | pipelines < slugs" >&2
    exit 2
    ;;
esac
