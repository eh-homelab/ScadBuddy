#!/usr/bin/env bash
#
# Run `models/<slug>/verify.sh` for each slug given on stdin (one per line, the
# output of select-models.sh), several at once, and fail if any of them fails.
# Used by the `models` job in ci.yml (#229); runs the same locally:
#
#   .github/scripts/select-models.sh all | .github/scripts/verify-models.sh
#
# Every verify.sh reads SCADBUDDY_OPENSCAD_IMAGE / SCADBUDDY_FONTS_IMAGE for the
# image it renders in; the caller sets both to one image so no script falls
# back to building its own from the rolling `openscad/openscad:dev`.
#
# Each template's output goes to its own log and is printed whole, in a
# collapsible group, once that template finishes — interleaved output from
# parallel renders is unreadable. A summary table goes to $GITHUB_STEP_SUMMARY
# when it is set.
set -euo pipefail

JOBS="${VERIFY_JOBS:-$(nproc)}"
# Per template. The slowest bundled template takes a few minutes; this is a
# hang guard, not a budget.
TIMEOUT="${VERIFY_TIMEOUT:-20m}"
LOG_DIR="${VERIFY_LOG_DIR:-$(mktemp -d)}"
mkdir -p "$LOG_DIR"

mapfile -t slugs < <(grep -v '^[[:space:]]*$' || true)
if [ "${#slugs[@]}" -eq 0 ]; then
  echo "No template selected; nothing to verify."
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "No template's \`verify.sh\` was selected for this change." >>"$GITHUB_STEP_SUMMARY"
  fi
  exit 0
fi

echo "Verifying ${#slugs[@]} template(s), $JOBS at a time: ${slugs[*]}"

# Force-remove every running container that template $1's verify.sh started.
#
# `timeout` kills verify.sh and its process group, which includes the `docker
# run` CLI blocked on a render — but killing the CLI does not stop the
# container: it lives on the daemon, and `--rm` only fires once IT exits. A
# hung render would keep burning the runner's cores under the templates still
# running beside it.
#
# Every `docker run` in a models/*/verify.sh carries
# `--label scadbuddy-verify=$SCADBUDDY_VERIFY_LABEL`, which run_one sets to the
# slug (#302; lint-verify-labels.sh fails CI on an unlabelled one). The label
# finds the containers that mount nothing, such as the `fc-list` font probe.
# The bind-mount match after it is a fallback for a container the label
# missed: every render bind-mounts the template's own directory (or one under
# it), and no two templates share one. The trailing `/` keeps `name-sign` from
# matching `name-sign-foo`.
reap_containers() {
  local slug="$1" dir="$PWD/models/$1" id src
  for id in $(docker ps -q --filter "label=scadbuddy-verify=$slug"); do
    echo "reaping container $id (label scadbuddy-verify=$slug)"
    docker rm -f "$id" >/dev/null 2>&1 || true
  done
  for id in $(docker ps -q); do
    while read -r src; do
      case "$src/" in
        "$dir"/*)
          echo "reaping container $id (mounts $src)"
          docker rm -f "$id" >/dev/null 2>&1 || true
          break
          ;;
      esac
    done < <(docker inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' "$id" 2>/dev/null || true)
  done
}

# One template: run it, record exit status and wall time next to its log.
run_one() {
  local slug="$1" start rc
  start=$(date +%s)
  rc=0
  # -k: SIGKILL a script that ignores the TERM, so the guard is a hard one.
  SCADBUDDY_VERIFY_LABEL="$slug" timeout -k 30s "$TIMEOUT" "models/$slug/verify.sh" \
    >"$LOG_DIR/$slug.log" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    reap_containers "$slug" >>"$LOG_DIR/$slug.log" 2>&1
  fi
  echo "$rc $(($(date +%s) - start))" >"$LOG_DIR/$slug.result"
  if [ "$rc" -eq 0 ]; then
    echo "PASS $slug"
  elif [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    echo "FAIL $slug (timed out after $TIMEOUT)"
  else
    echo "FAIL $slug (exit $rc)"
  fi
}
export -f reap_containers run_one
export LOG_DIR TIMEOUT

# `|| true`: run_one never fails, but xargs exits non-zero if a child is
# killed; the per-template .result files are what decide the verdict below.
# shellcheck disable=SC2016 # $1 is expanded by the child bash, on purpose.
printf '%s\n' "${slugs[@]}" | xargs -P "$JOBS" -I{} bash -c 'run_one "$1"' _ {} || true

failed=0
summary="| Template | Result | Time |"$'\n'"| --- | --- | --- |"
for slug in "${slugs[@]}"; do
  rc=missing secs='?'
  if [ -f "$LOG_DIR/$slug.result" ]; then read -r rc secs <"$LOG_DIR/$slug.result"; fi
  if [ "$rc" = 0 ]; then
    verdict=PASS
  else
    verdict="FAIL (exit $rc)"
    failed=$((failed + 1))
  fi
  echo "::group::$slug — $verdict in ${secs}s"
  cat "$LOG_DIR/$slug.log" 2>/dev/null || echo "(no log)"
  echo "::endgroup::"
  if [ "$verdict" != PASS ]; then
    echo "::error title=models/$slug/verify.sh::$verdict — expand the '$slug' group above for the failing cases"
  fi
  summary+=$'\n'"| \`$slug\` | $verdict | ${secs}s |"
done

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  printf '### Template verify.sh\n\n%s\n' "$summary" >>"$GITHUB_STEP_SUMMARY"
fi
printf '%s\n' "$summary"

if [ "$failed" -ne 0 ]; then
  echo "$failed of ${#slugs[@]} template(s) failed."
  exit 1
fi
echo "All ${#slugs[@]} template(s) passed."
