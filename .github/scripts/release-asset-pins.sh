#!/usr/bin/env bash
# The release downloads fall back to a second URL when the release URL fails (#5706): a
# REST API asset URL, pinned by asset id beside each version and sha256 (the Dockerfile's
# ARGs, ci.yml's kustomize step), or, for the amd64 Temporal CLI, temporal.download. The
# fallback runs only in an outage, so an id left behind by a bump, or a moved archive
# path, would show up only then. This fetches each fallback and checks that it still
# serves the pinned sha256. Weekly, from link-check.yml.
#
# Usage: release-asset-pins.sh [--resolve] [repo root]. With --resolve it fetches
# nothing: it only checks that every pin is found in the files (the lint job runs it on
# each PR, so a renamed or re-indented pin fails there). GH_TOKEN, when set, is sent
# with each API call. RELEASE_ASSET_CURL replaces curl (the test's stub).
set -euo pipefail

resolve_only=0
if [[ "${1:-}" == "--resolve" ]]; then
  resolve_only=1
  shift
fi
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

# fetch_check <url> <pinned sha256> <what> [api]: "api" sends the API's headers.
fetch_check() {
  local url="$1" sha="$2" what="$3" api="${4:-}"
  local headers=()
  if [[ "$api" == api ]]; then
    headers+=(-H "Accept: application/octet-stream")
    if [[ -n "${GH_TOKEN:-}" ]]; then
      headers+=(-H "Authorization: Bearer ${GH_TOKEN}")
    fi
  fi
  if ! "$curl_bin" -fsSL --retry 3 --retry-all-errors "${headers[@]}" -o "$work/asset" "$url"; then
    echo "::error::$what: $url could not be fetched"
    failed=1
    return
  fi
  local actual
  actual="$(sha256sum "$work/asset" | cut -d ' ' -f 1)"
  if [[ "$actual" != "$sha" ]]; then
    echo "::error::$what: $url serves sha256 $actual, not the pinned $sha"
    failed=1
    return
  fi
  echo "ok: $what ($url)"
}

# pinned <what> <value>...: every value must be found.
pinned() {
  local what="$1"
  shift
  local value
  for value in "$@"; do
    if [[ -z "$value" ]]; then
      echo "::error::$what: a pin was not found"
      failed=1
      return 1
    fi
  done
  if ((resolve_only)); then
    echo "found: $what"
    return 1
  fi
}

# check <owner/repo> <asset id> <pinned sha256> <what>
check() {
  local repo="$1" id="$2" sha="$3" what="$4"
  pinned "$what" "$id" "$sha" || return 0
  fetch_check "https://api.github.com/repos/$repo/releases/assets/$id" "$sha" "$what" api
}

# check_temporal_download <version> <pinned amd64 sha256>
check_temporal_download() {
  local version="$1" sha="$2" what="Temporal CLI amd64 (temporal.download)"
  pinned "$what" "$version" "$sha" || return 0
  fetch_check "https://temporal.download/cli/archive/v$version?platform=linux&arch=amd64" \
    "$sha" "$what"
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
check_temporal_download "$(arg TEMPORAL_CLI_VERSION)" "$(arg TEMPORAL_CLI_SHA256_AMD64)"

exit "$failed"
