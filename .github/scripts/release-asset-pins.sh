#!/usr/bin/env bash
# The release downloads fall back to a REST API asset URL when the release URL fails
# (#5706). Each asset id is pinned beside its version and sha256 (the Dockerfile's ARGs,
# ci.yml's kustomize step), and the fallback runs only in an outage, so an id left behind
# by a bump would show up only then. This fetches each pinned id and checks that it still
# serves the pinned sha256. Weekly, from link-check.yml.
#
# Usage: release-asset-pins.sh [repo root]. GH_TOKEN, when set, is sent with each call.
# RELEASE_ASSET_CURL replaces curl (the test's stub).
set -euo pipefail

root="${1:-.}"
curl_bin="${RELEASE_ASSET_CURL:-curl}"

# The value of `ARG <name>=<value>` in the Dockerfile.
arg() {
  sed -n "s/^ARG $1=//p" "$root/Dockerfile" | head -n 1
}

# The value of `<name>: <value>` (quotes dropped) in ci.yml.
ci_env() {
  sed -n "s/^ *$1: *\"\{0,1\}\([^\"]*\)\"\{0,1\} *$/\1/p" "$root/.github/workflows/ci.yml" |
    head -n 1
}

failed=0
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# check <owner/repo> <asset id> <pinned sha256> <what>
check() {
  local repo="$1" id="$2" sha="$3" what="$4"
  if [[ -z "$id" || -z "$sha" ]]; then
    echo "::error::$what: no pinned asset id or sha256 found"
    failed=1
    return
  fi
  local headers=(-H "Accept: application/octet-stream")
  if [[ -n "${GH_TOKEN:-}" ]]; then
    headers+=(-H "Authorization: Bearer ${GH_TOKEN}")
  fi
  if ! "$curl_bin" -fsSL --retry 3 --retry-all-errors "${headers[@]}" -o "$work/asset" \
    "https://api.github.com/repos/$repo/releases/assets/$id"; then
    echo "::error::$what: asset $id of $repo could not be fetched"
    failed=1
    return
  fi
  local actual
  actual="$(sha256sum "$work/asset" | cut -d ' ' -f 1)"
  if [[ "$actual" != "$sha" ]]; then
    echo "::error::$what: asset $id of $repo serves sha256 $actual, not the pinned $sha"
    failed=1
    return
  fi
  echo "ok: $what (asset $id)"
}

check bambulab/BambuStudio "$(arg BAMBU_STUDIO_ASSET_ID)" \
  "$(arg BAMBU_STUDIO_SHA256_AMD64)" "Bambu Studio"
check Leathong/openscad-LSP "$(arg OPENSCAD_LSP_ASSET_AMD64)" \
  "$(arg OPENSCAD_LSP_SHA256_AMD64)" "openscad-lsp amd64"
check Leathong/openscad-LSP "$(arg OPENSCAD_LSP_ASSET_ARM64)" \
  "$(arg OPENSCAD_LSP_SHA256_ARM64)" "openscad-lsp arm64"
check temporalio/cli "$(arg TEMPORAL_CLI_ASSET_ARM64)" \
  "$(arg TEMPORAL_CLI_SHA256_ARM64)" "Temporal CLI arm64"
check kubernetes-sigs/kustomize "$(ci_env KUSTOMIZE_ASSET_ID)" \
  "$(ci_env KUSTOMIZE_SHA256)" "kustomize"

exit "$failed"
