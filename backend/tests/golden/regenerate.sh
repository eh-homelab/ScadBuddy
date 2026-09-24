#!/usr/bin/env bash
# Regenerate the model goldens in this directory.
#
# They have to be produced inside the image: a golden rendered against a
# different OpenSCAD build, or on a host without fonts-lobstertwo, records
# measurements this repo never asserts anywhere else.
#
# The container writes to its own copy of the tree and the files are copied back
# out, rather than bind-mounting this directory: the test stage runs as uid
# 10001, so a mounted host directory would be unwritable.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
IMAGE="${SCADBUDDY_TEST_IMAGE:-scadbuddy:golden}"
CONTAINER="scadbuddy-golden-$$"
GOLDEN=/app/backend/tests/golden

cd "$ROOT"

echo "==> building $IMAGE (--target test)"
docker build --target test -t "$IMAGE" .

echo "==> rendering"
trap 'docker rm -f "$CONTAINER" >/dev/null 2>&1 || true' EXIT
docker run --name "$CONTAINER" -w /app/backend \
    -e SCADBUDDY_UPDATE_GOLDEN=1 "$IMAGE" \
    uv run --frozen --no-sync pytest tests/test_golden_models.py -q

echo "==> copying the goldens back"
# `docker exec` is not an option: the run above has already exited. `docker cp`
# of `<dir>/.` merges the container's copy over this directory, which is a no-op
# for any golden the run did not rewrite.
docker cp "${CONTAINER}:${GOLDEN}/." backend/tests/golden/

echo "==> done; review the diff before committing"
git -C "$ROOT" status --short backend/tests/golden
