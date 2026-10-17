#!/usr/bin/env bash
# lint-buildx-mirror.sh passes on this checkout and fails on a buildx step that
# pulls BuildKit from Docker Hub.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

"$here/lint-buildx-mirror.sh" "$root" >/dev/null

mkdir -p "$work/.github/workflows"
cat >"$work/.github/workflows/w.yml" <<'YML'
jobs:
  ok:
    steps:
      - uses: docker/setup-buildx-action@v4
        if: ${{ true }}
        with:
          driver-opts: image=mirror.gcr.io/moby/buildkit:buildx-stable-1
      - run: echo after
  block:
    steps:
      - uses: docker/setup-buildx-action@v4
        with:
          driver-opts: |
            image=mirror.gcr.io/moby/buildkit:buildx-stable-1
  bare:
    steps:
      - uses: docker/setup-buildx-action@v4
        # image=mirror.gcr.io/moby/buildkit:buildx-stable-1 in a comment is no setting
      - with:
          driver-opts: image=mirror.gcr.io/moby/buildkit:buildx-stable-1
        run: echo the next step names it, not this one
YML
if out="$("$here/lint-buildx-mirror.sh" "$work" 2>&1)"; then
  echo "FAIL: a bare buildx step passed"; exit 1
fi
grep -q "w.yml:11:" <<<"$out" || { echo "FAIL: a block-scalar step passed: $out"; exit 1; }
grep -q "w.yml:17:" <<<"$out" || { echo "FAIL: wrong step named: $out"; exit 1; }
if grep -q "w.yml:4:" <<<"$out"; then echo "FAIL: the mirrored step was flagged: $out"; exit 1; fi
echo "ok: lint-buildx-mirror"
