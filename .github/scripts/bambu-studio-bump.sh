#!/usr/bin/env bash
#
# Moves the Dockerfile's pinned Bambu Studio release to the newest stable one
# (#8325), for the weekly `bambu-studio-bump` workflow to open as one PR.
# Dependabot cannot watch a release asset, so this does.
#
# The slicer stages pin the Ubuntu 24.04 AppImage by version, asset name, REST API
# asset id (the fallback URL, #5706) and sha256 (the global ARGs at the top of the
# Dockerfile). This script moves all four together. It only proposes the bump: the
# PR's own CI (the slicer image's `--check-cli`, the profile tests, the hot path's
# e2e) is the re-measurement a human reads before merging.
#
# Usage: bambu-studio-bump.sh [repo-root]
# Prints `bump=true|false`, and for a bump `version=`, `asset=`, `asset_id=`,
# `sha256=` and `sha256_from=` (`digest` or `download`), as lines a workflow can
# append to $GITHUB_OUTPUT. Exits non-zero, having changed nothing, when the
# release list cannot be read or the new release has no usable asset.
#
# GH_TOKEN, when set, is sent with the API call.
# Test seam (bambu-studio-bump.test.sh):
#   BAMBU_STUDIO_RELEASES_URL  the release listing (default: the real one)
set -euo pipefail

root="${1:-.}"
dockerfile="$root/Dockerfile"
releases_url="${BAMBU_STUDIO_RELEASES_URL:-https://api.github.com/repos/bambulab/BambuStudio/releases?per_page=30}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

arg() {
  sed -n "s/^ARG $1=//p" "$dockerfile" | head -n 1
}

current="$(arg BAMBU_STUDIO_VERSION)"
if [[ ! "$current" =~ ^[0-9]{2}\.[0-9]{2}\.[0-9]{2}\.[0-9]{2}$ ]]; then
  echo "bambu-studio-bump: no 'ARG BAMBU_STUDIO_VERSION=<NN.NN.NN.NN>' in $dockerfile" >&2
  exit 1
fi
for name in BAMBU_STUDIO_ASSET BAMBU_STUDIO_ASSET_ID BAMBU_STUDIO_SHA256_AMD64; do
  if [ -z "$(arg "$name")" ]; then
    echo "bambu-studio-bump: no 'ARG $name=' in $dockerfile" >&2
    exit 1
  fi
done

headers=(-H "Accept: application/vnd.github+json")
if [ -n "${GH_TOKEN:-}" ]; then
  headers+=(-H "Authorization: Bearer ${GH_TOKEN}")
fi
curl -fsSL --retry 3 --retry-all-errors "${headers[@]}" -o "$work/releases.json" "$releases_url"

# The newest by version, not by listing order or the "latest" mark: only published,
# non-prerelease releases tagged exactly vNN.NN.NN.NN count.
version="$(jq -r '.[] | select(.draft == false and .prerelease == false) | .tag_name
  | select(test("^v[0-9]{2}\\.[0-9]{2}\\.[0-9]{2}\\.[0-9]{2}$")) | ltrimstr("v")' \
  "$work/releases.json" | sort -V | tail -n 1)"
if [ -z "$version" ]; then
  echo "bambu-studio-bump: no stable vNN.NN.NN.NN release in the listing" >&2
  exit 1
fi

# Equal or older is no bump.
if [ "$(printf '%s\n%s\n' "$current" "$version" | sort -V | tail -n 1)" = "$current" ]; then
  echo "bump=false"
  exit 0
fi

# The Ubuntu 24.04 AppImage. Its name has changed between releases
# (`BambuStudio_ubuntu24.04-v02.08.02.61-…`, `BambuStudio_ubu24-v02.09.02.62-…`, with
# `BambuStudio_ubu22_v02.09.01.70-…` beside one), so both spellings and either
# separator match; exactly one asset must.
jq --arg tag "v$version" --arg version "$version" '
  .[] | select(.tag_name == $tag) | .assets[]
  | select(.name | test("^BambuStudio_ubu(ntu)?24(\\.04)?[-_]v" + ($version | gsub("\\."; "\\.")) + "-[0-9]+\\.AppImage$"))
  | {name, id, digest, url: .browser_download_url}' "$work/releases.json" \
  | jq -s . > "$work/assets.json"
count="$(jq length "$work/assets.json")"
if [ "$count" -ne 1 ]; then
  echo "bambu-studio-bump: v$version has $count Ubuntu 24.04 AppImage assets, not one" >&2
  exit 1
fi
asset="$(jq -r '.[0].name' "$work/assets.json")"
asset_id="$(jq -r '.[0].id' "$work/assets.json")"
digest="$(jq -r '.[0].digest // ""' "$work/assets.json")"
url="$(jq -r '.[0].url' "$work/assets.json")"
if [[ ! "$asset_id" =~ ^[0-9]+$ ]]; then
  echo "bambu-studio-bump: $asset has no numeric asset id ('$asset_id')" >&2
  exit 1
fi

# GitHub lists a sha256 digest for each asset uploaded since mid-2025; with none,
# the asset is downloaded and hashed.
if [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  sha256="${digest#sha256:}"
  sha256_from=digest
else
  curl -fsSL --retry 3 --retry-all-errors -o "$work/asset" "$url"
  sha256="$(sha256sum "$work/asset" | cut -d ' ' -f 1)"
  sha256_from=download
fi

sed -i \
  -e "s|^ARG BAMBU_STUDIO_VERSION=.*\$|ARG BAMBU_STUDIO_VERSION=$version|" \
  -e "s|^ARG BAMBU_STUDIO_ASSET=.*\$|ARG BAMBU_STUDIO_ASSET=$asset|" \
  -e "s|^ARG BAMBU_STUDIO_ASSET_ID=.*\$|ARG BAMBU_STUDIO_ASSET_ID=$asset_id|" \
  -e "s|^ARG BAMBU_STUDIO_SHA256_AMD64=.*\$|ARG BAMBU_STUDIO_SHA256_AMD64=$sha256|" \
  "$dockerfile"

echo "bump=true"
echo "version=$version"
echo "asset=$asset"
echo "asset_id=$asset_id"
echo "sha256=$sha256"
echo "sha256_from=$sha256_from"
