#!/usr/bin/env bash
#
# Moves the Dockerfile's pinned OpenSCAD base to the newest dated nightly
# (#508 item 9), for the weekly `openscad-bump` workflow to open as one PR.
#
# The base is pinned by tag and index digest (#507), so a new nightly no longer
# breaks every PR at once; it waits here instead. This script only proposes the
# bump: tag, digest and OPENSCAD_VERSION move together, and the PR's own CI
# (every template's verify.sh, the image's requires_openscad tests) is the §3
# re-verification a human reads before merging.
#
# Usage: openscad-bump.sh [repo-root]
# Prints `bump=true|false`, and for a bump `tag=`, `digest=`, `version=`, as
# lines a workflow can append to $GITHUB_OUTPUT. Exits non-zero, having changed
# nothing, when the tag list or the new image cannot be read.
#
# Test seams (openscad-bump.test.sh):
#   OPENSCAD_TAGS_URL      the Docker Hub tag listing (default: the real one)
#   OPENSCAD_VERSION_CMD   runs `openscad --version` in "$1" (default: docker run)
set -euo pipefail

root="${1:-.}"
dockerfile="$root/Dockerfile"
claude_md="$root/CLAUDE.md"
repo="openscad/openscad"
# Docker Hub's tag API; each result's `digest` is the multi-arch index digest,
# the one `docker buildx imagetools inspect` reports and the FROM line pins.
tags_url="${OPENSCAD_TAGS_URL:-https://hub.docker.com/v2/repositories/$repo/tags?name=dev.&ordering=last_updated&page_size=100}"

version_of() {
  if [ -n "${OPENSCAD_VERSION_CMD:-}" ]; then
    "$OPENSCAD_VERSION_CMD" "$1"
  else
    # --version writes to stderr (CLAUDE.md).
    docker run --rm "$1" openscad --version 2>&1
  fi
}

current="$(sed -n "s|^FROM $repo:\(dev\.[0-9-]*\)@sha256:[0-9a-f]* AS base\$|\1|p" "$dockerfile")"
if [ -z "$current" ]; then
  echo "openscad-bump: no pinned 'FROM $repo:dev.<date>@sha256:<digest> AS base' in $dockerfile" >&2
  exit 1
fi

listing="$(curl -fsSL --retry 3 "$tags_url")"
# The newest by NAME, not by last_updated: a dated tag re-pushed later must not
# look newer than the next day's. Only exact `dev.YYYY-MM-DD` names count.
newest="$(jq -r '.results[] | select(.name | test("^dev\\.[0-9]{4}-[0-9]{2}-[0-9]{2}$")) | "\(.name) \(.digest)"' <<<"$listing" | sort -r | head -n 1)"
if [ -z "$newest" ]; then
  echo "openscad-bump: no dev.<date> tag in the listing" >&2
  exit 1
fi
tag="${newest%% *}"
digest="${newest##* }"
if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "openscad-bump: $tag has no index digest ('$digest')" >&2
  exit 1
fi

# `sort` on the fixed-width date orders them; equal or older is no bump.
if [ "$(printf '%s\n%s\n' "$current" "$tag" | sort -r | head -n 1)" = "$current" ]; then
  echo "bump=false"
  exit 0
fi

reported="$(version_of "$repo:$tag@$digest")"
version="$(sed -n 's/^OpenSCAD version //p' <<<"$reported" | head -n 1)"
# Checked as strictly as the tag and digest: all three are written into the
# Dockerfile by sed and into a PR by the workflow.
if [[ ! "$version" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}(\.[0-9]+)?$ ]]; then
  echo "openscad-bump: $repo:$tag reported no version: $reported" >&2
  exit 1
fi

sed -i \
  -e "s|^FROM $repo:dev\.[0-9-]*@sha256:[0-9a-f]* AS base\$|FROM $repo:$tag@$digest AS base|" \
  -e "s|^ARG OPENSCAD_VERSION=.*\$|ARG OPENSCAD_VERSION=$version|" \
  "$dockerfile"
if [ -f "$claude_md" ]; then
  sed -i \
    -e "s|\`$repo:dev\.[0-9-]*@sha256:…\`|\`$repo:$tag@sha256:…\`|" \
    -e "s|\`OPENSCAD_VERSION\` (currently [0-9.]*)|\`OPENSCAD_VERSION\` (currently $version)|" \
    "$claude_md"
fi

echo "bump=true"
echo "tag=$tag"
echo "digest=$digest"
echo "version=$version"
