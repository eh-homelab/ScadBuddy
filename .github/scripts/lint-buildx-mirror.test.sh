#!/usr/bin/env bash
# lint-buildx-mirror.sh passes on this checkout and fails on a buildx step that
# pulls BuildKit from Docker Hub, or names the mirror only in a folded `>` block,
# or whose daemon pulls Docker Hub images without the mirror (#4012).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

"$here/lint-buildx-mirror.sh" "$root" >/dev/null

mkdir -p "$work/.github/workflows"
w="$work/.github/workflows/w.yml"
# Each buildx step's `uses:` line ends with its name, so a check finds its line.
cat >"$w" <<'YML'
jobs:
  ok:
    steps:
      - uses: docker/setup-buildx-action@v4 # ok
        if: ${{ true }}
        with:
          driver-opts: image=mirror.gcr.io/moby/buildkit:buildx-stable-1
          buildkitd-config-inline: |
            [registry."docker.io"]
              mirrors = ["mirror.gcr.io"]
      - run: echo after
  block:
    steps:
      - uses: docker/setup-buildx-action@v4 # block
        with:
          driver-opts: |-2 # two options
            network=host
            image=mirror.gcr.io/moby/buildkit:buildx-stable-1
          buildkitd-config-inline: |- # mirror, #4012
            [registry."ghcr.io"]
              http = false
            [registry."docker.io"] # Docker Hub
              mirrors = [ "mirror.gcr.io" ]
  wrongblock:
    steps:
      - uses: docker/setup-buildx-action@v4 # wrongblock
        with:
          driver-opts: |
            network=host
          version: image=mirror.gcr.io/moby/buildkit:buildx-stable-1
          buildkitd-config-inline: |
            [registry."docker.io"]
              mirrors = ["mirror.gcr.io"]
  bare:
    steps:
      - uses: docker/setup-buildx-action@v4 # bare
        # image=mirror.gcr.io/moby/buildkit:buildx-stable-1 in a comment is no setting
      - with:
          driver-opts: image=mirror.gcr.io/moby/buildkit:buildx-stable-1
          buildkitd-config-inline: |
            [registry."docker.io"]
              mirrors = ["mirror.gcr.io"]
        run: echo the next step names it, not this one
  folded:
    steps:
      - uses: docker/setup-buildx-action@v4 # folded
        with:
          driver-opts: >
            image=mirror.gcr.io/moby/buildkit:buildx-stable-1
          buildkitd-config-inline: |
            [registry."docker.io"]
              mirrors = ["mirror.gcr.io"]
  noconfig:
    steps:
      - uses: docker/setup-buildx-action@v4 # noconfig
        with:
          driver-opts: image=mirror.gcr.io/moby/buildkit:buildx-stable-1
  othertable:
    steps:
      - uses: docker/setup-buildx-action@v4 # othertable
        with:
          driver-opts: image=mirror.gcr.io/moby/buildkit:buildx-stable-1
          buildkitd-config-inline: |
            [registry."docker.io"]
            [registry."ghcr.io"]
              mirrors = ["mirror.gcr.io"]
YML
at() { grep -n "# $1\$" "$w" | cut -d: -f1; }
if out="$("$here/lint-buildx-mirror.sh" "$work" 2>&1)"; then
  echo "FAIL: a bare buildx step passed"; exit 1
fi
flagged() { grep -q "w.yml:$(at "$1"): setup-buildx-action without $2" <<<"$out"; }
flagged wrongblock driver-opts || { echo "FAIL: a block naming no mirror passed: $out"; exit 1; }
flagged bare driver-opts || { echo "FAIL: wrong step named: $out"; exit 1; }
# A folded block joins its options onto one line, so it is not read (#4046).
flagged folded driver-opts || { echo "FAIL: a folded driver-opts block passed: $out"; exit 1; }
flagged noconfig "a buildkitd" || { echo "FAIL: a daemon without the registry mirror passed: $out"; exit 1; }
flagged othertable "a buildkitd" || { echo "FAIL: a mirror for another registry passed: $out"; exit 1; }
for ok in ok block; do
  if grep -q "w.yml:$(at "$ok"):" <<<"$out"; then echo "FAIL: the mirrored step $ok was flagged: $out"; exit 1; fi
done
for step in wrongblock folded noconfig othertable; do
  if [ "$(grep -c "w.yml:$(at "$step"):" <<<"$out")" != 1 ]; then
    echo "FAIL: $step was flagged for more than its one fault: $out"; exit 1
  fi
done
echo "ok: lint-buildx-mirror"
