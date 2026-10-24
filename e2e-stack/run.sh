#!/usr/bin/env bash
#
# The native hot path against a real stack (#3818 tier A, plan 2026-10-18 Task 13):
# ScadBuddy with its own store, library and slicer, a fake LAN printer and a Bambuddy
# that fails every call (e2e-stack/compose.yaml), driven by
# frontend/e2e/real-hot-path.spec.ts.
#
#   run.sh [--mode recorder|unset|both] [--keep] [--no-build]
#
#   --mode recorder   bambuddy_url is the recorder; the spec asserts it saw nothing (AC1)
#   --mode unset      no bambuddy_url (AC2)
#   --mode both       one, then the other, each on a fresh stack (the default)
#   --keep            leave the (last) stack up afterwards, for a look or a rerun
#   --no-build        use the images already built
#
# It builds three images from the Dockerfile (unless --no-build): `runtime` as
# SB_HOTPATH_APP_IMAGE, `slicer` as SB_HOTPATH_SLICER_IMAGE and `test` as
# SB_HOTPATH_TOOLS_IMAGE (the Temporal dev server and moto), plus the two fakes. Tags of
# its own by default, so a shared machine's other builds keep theirs. BUILDX_* or
# DOCKER_BUILDKIT settings in the environment apply to the builds; the CI workflow
# builds with its own cache instead and passes --no-build.
#
# It needs docker with compose v2, curl, python3, and the frontend's dependencies
# installed (`pnpm install --frozen-lockfile` and `pnpm exec playwright install
# chromium` in frontend/). Teardown is `docker compose -p sb-hotpath down -v`; it never
# stops anything else. To tear down a stack left by --keep:
#
#   SB_HOTPATH_KEK_FILE=/dev/null docker compose -p sb-hotpath -f e2e-stack/compose.yaml down -v
set -euo pipefail

mode=both
keep=0
build=1
while [ $# -gt 0 ]; do
  case "$1" in
    --mode)
      mode="${2:-}"
      shift 2
      ;;
    --keep)
      keep=1
      shift
      ;;
    --no-build)
      build=0
      shift
      ;;
    *)
      echo "usage: $0 [--mode recorder|unset|both] [--keep] [--no-build]" >&2
      exit 2
      ;;
  esac
done
case "$mode" in
  recorder) modes=(recorder) ;;
  unset) modes=(unset) ;;
  both) modes=(recorder unset) ;;
  *)
    echo "--mode is recorder, unset or both, not '${mode}'" >&2
    exit 2
    ;;
esac

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/.." && pwd)"
export SB_HOTPATH_APP_IMAGE="${SB_HOTPATH_APP_IMAGE:-scadbuddy:hot-path-app}"
export SB_HOTPATH_SLICER_IMAGE="${SB_HOTPATH_SLICER_IMAGE:-scadbuddy-slicer:hot-path}"
export SB_HOTPATH_TOOLS_IMAGE="${SB_HOTPATH_TOOLS_IMAGE:-scadbuddy:hot-path-test}"
compose=(docker compose -p sb-hotpath -f "$here/compose.yaml")

state="$(mktemp -d)"
export SB_HOTPATH_KEK_FILE="$state/kek"
python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())' >"$SB_HOTPATH_KEK_FILE"
# Read by the app's uid 10001 through the bind mount compose makes of it.
chmod 0644 "$SB_HOTPATH_KEK_FILE"

up=0
down() {
  if [ "$up" = 1 ]; then
    "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    up=0
  fi
}
finish() {
  status=$?
  # A failed spec of the stack still up has saved its logs beside its results; anything
  # else that failed on it (a build, `up`, the seed) shows them here.
  if [ "$status" -ne 0 ] && [ "$up" = 1 ] && [ "${logs_saved:-0}" = 0 ]; then
    "${compose[@]}" ps -a || true
    "${compose[@]}" logs --tail 300 app slicer printer bambuddy >"$state/stack.log" 2>&1 || true
    echo "--- the stack's last logs ($state/stack.log)"
    tail -n 400 "$state/stack.log" || true
  fi
  if [ "$keep" = 0 ]; then
    down
    rm -rf "$state"
  else
    echo "kept: project sb-hotpath; the key is $SB_HOTPATH_KEK_FILE"
  fi
  exit "$status"
}
trap finish EXIT

if [ "$build" = 1 ]; then
  docker build --target runtime -t "$SB_HOTPATH_APP_IMAGE" "$repo"
  docker build --target slicer -t "$SB_HOTPATH_SLICER_IMAGE" "$repo"
  docker build --target test -t "$SB_HOTPATH_TOOLS_IMAGE" "$repo"
fi
"${compose[@]}" build printer bambuddy
# The pulled images (Postgres), by `docker pull`: compose's own pull of a tag@digest
# reference on mirror.gcr.io answered "not found" where `docker pull` of the same one
# worked (compose v5.0.2, 2026-10-24).
for image in $("${compose[@]}" config --images); do
  case "$image" in
    mirror.gcr.io/*) docker pull --quiet "$image" >/dev/null ;;
  esac
done

results=()
failed=0
for index in "${!modes[@]}"; do
  current="${modes[$index]}"
  echo "=== hot path, bambuddy_url ${current}"
  down
  up=1
  # Per mode: this stack's logs are not saved yet, whatever an earlier mode's did.
  logs_saved=0
  "${compose[@]}" up -d --wait --wait-timeout 300
  "$here/seed.sh" "$current" >/dev/null

  # Each mode runs whatever the one before it came to; the run fails at the end if any did.
  if (
    cd "$repo/frontend"
    E2E_BASE_URL=http://127.0.0.1:18080 E2E_HOT_PATH=1 E2E_HOT_PATH_MODE="$current" \
      E2E_PRINTER_URL=http://127.0.0.1:18081 E2E_RECORDER_URL=http://127.0.0.1:18000 \
      pnpm exec playwright test e2e/real-hot-path.spec.ts --output "test-results/hot-path-${current}"
  ); then
    results+=("${current}: passed")
    # Nothing failed on this stack: an earlier mode's failure is no reason to dump it.
    logs_saved=1
  else
    results+=("${current}: FAILED")
    failed=1
    # Beside the Playwright results, which the workflow uploads.
    mkdir -p "$repo/frontend/test-results/hot-path-${current}"
    "${compose[@]}" logs app slicer printer bambuddy \
      >"$repo/frontend/test-results/hot-path-${current}/stack.log" 2>&1 || true
    logs_saved=1
    echo "--- recorded by the recorder:"
    curl --silent http://127.0.0.1:18000/__recorded || true
    echo
  fi
  if [ "$index" -lt $((${#modes[@]} - 1)) ]; then
    down
  fi
done
printf '%s\n' "${results[@]}"
[ "$failed" = 0 ]
