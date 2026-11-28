#!/usr/bin/env bash
#
# Branch coverage for bambu-studio-bump.sh. No network: the release listing and
# the asset are local files.
#
# Runs in ci.yml's `lint` job.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bump="$here/bambu-studio-bump.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

old_sha="$(printf 'a%.0s' {1..64})"
new_sha="$(printf 'b%.0s' {1..64})"
failures=0

fail() {
  echo "FAIL: $1" >&2
  failures=$((failures + 1))
}

# A repo root with the four lines the script edits and a neighbour it must not.
setup() {
  rm -rf "$work/repo"
  mkdir -p "$work/repo"
  cat > "$work/repo/Dockerfile" <<EOF
ARG BOSL2_REF=v2.0.761
ARG BAMBU_STUDIO_VERSION=02.08.02.61
ARG BAMBU_STUDIO_ASSET=BambuStudio_ubuntu24.04-v02.08.02.61-20260820225108.AppImage
ARG BAMBU_STUDIO_ASSET_ID=523226841
ARG BAMBU_STUDIO_SHA256_AMD64=$old_sha
ARG BAMBU_STUDIO_VERSION
EOF
}

# `release <tag> <prerelease> <draft> <asset json>...` prints one release object.
release() {
  local tag="$1" pre="$2" draft="$3"
  shift 3
  local IFS=,
  printf '{"tag_name":"%s","prerelease":%s,"draft":%s,"assets":[%s]}' "$tag" "$pre" "$draft" "$*"
}

# `asset <name> <id> [digest]` prints one asset object; its download is a local file.
asset() {
  local digest=null
  if [ -n "${3:-}" ]; then digest="\"$3\""; fi
  printf '{"name":"%s","id":%s,"digest":%s,"browser_download_url":"file://%s/asset.bin"}' \
    "$1" "$2" "$digest" "$work"
}

# `listing <release json>...` writes the listing.
listing() {
  local IFS=,
  printf '[%s]' "$*" > "$work/releases.json"
}

run() {
  BAMBU_STUDIO_RELEASES_URL="file://$work/releases.json" "$bump" "$work/repo"
}

new_asset="BambuStudio_ubu24-v02.09.02.62-20261124164959.AppImage"
listing_new() {
  listing \
    "$(release v02.10.00.10 true false "$(asset BambuStudio_ubu24-v02.10.00.10-1.AppImage 9 "sha256:$new_sha")")" \
    "$(release v02.09.02.62 false false \
      "$(asset BambuStudio_ubu22-v02.09.02.62-20261124164959.AppImage 787354854 "sha256:$old_sha")" \
      "$(asset "$new_asset" 787358056 "$1")" \
      "$(asset Bambu_Studio_mac-v02.09.02.62-20261124164959.dmg 787323840 "sha256:$old_sha")")" \
    "$(release v02.08.02.61 false false "$(asset BambuStudio_ubuntu24.04-v02.08.02.61-20260820225108.AppImage 523226841 "sha256:$old_sha")")"
}

# 1. A newer stable release with a digest: all four ARGs move together; the
#    prerelease is skipped.
setup
listing_new "sha256:$new_sha"
out="$(run)" || fail "bump exited non-zero"
grep -qx "bump=true" <<<"$out" || fail "bump: expected bump=true, got: $out"
grep -qx "version=02.09.02.62" <<<"$out" || fail "bump: version"
grep -qx "sha256_from=digest" <<<"$out" || fail "bump: sha256_from"
grep -qx "ARG BAMBU_STUDIO_VERSION=02.09.02.62" "$work/repo/Dockerfile" || fail "bump: version ARG"
grep -qx "ARG BAMBU_STUDIO_ASSET=$new_asset" "$work/repo/Dockerfile" || fail "bump: asset ARG"
grep -qx "ARG BAMBU_STUDIO_ASSET_ID=787358056" "$work/repo/Dockerfile" || fail "bump: asset id ARG"
grep -qx "ARG BAMBU_STUDIO_SHA256_AMD64=$new_sha" "$work/repo/Dockerfile" || fail "bump: sha256 ARG"
grep -qx "ARG BOSL2_REF=v2.0.761" "$work/repo/Dockerfile" || fail "bump: touched another ARG"
grep -qx "ARG BAMBU_STUDIO_VERSION" "$work/repo/Dockerfile" || fail "bump: touched the stage's bare ARG"

# 2. No digest: the asset is downloaded and hashed.
setup
printf 'appimage bytes' > "$work/asset.bin"
listing_new ""
out="$(run)" || fail "no digest exited non-zero"
want="$(sha256sum "$work/asset.bin" | cut -d ' ' -f 1)"
grep -qx "sha256=$want" <<<"$out" || fail "no digest: sha256, got: $out"
grep -qx "sha256_from=download" <<<"$out" || fail "no digest: sha256_from"
grep -qx "ARG BAMBU_STUDIO_SHA256_AMD64=$want" "$work/repo/Dockerfile" || fail "no digest: sha256 ARG"

# 3. Already on the newest stable: nothing changes. A draft and a prerelease that
#    are newer do not count, nor does an oddly named tag.
setup
listing \
  "$(release v02.09.02.62 false true "$(asset "$new_asset" 1 "sha256:$new_sha")")" \
  "$(release v02.09.03.01 true false "$(asset "$new_asset" 1 "sha256:$new_sha")")" \
  "$(release v02.99.00.00-beta false false "$(asset "$new_asset" 1 "sha256:$new_sha")")" \
  "$(release v02.08.02.61 false false)" \
  "$(release v02.07.01.62 false false)"
cp "$work/repo/Dockerfile" "$work/before"
out="$(run)" || fail "up to date exited non-zero"
[ "$out" = "bump=false" ] || fail "up to date: got: $out"
cmp -s "$work/before" "$work/repo/Dockerfile" || fail "up to date: Dockerfile changed"

# 4. Newest by version, not by listing order.
setup
listing \
  "$(release v02.08.02.60 false false)" \
  "$(release v02.09.02.62 false false "$(asset "$new_asset" 787358056 "sha256:$new_sha")")" \
  "$(release v02.09.01.70 false false "$(asset BambuStudio_ubu24-v02.09.01.70-1.AppImage 7 "sha256:$old_sha")")"
out="$(run)" || fail "by version exited non-zero"
grep -qx "version=02.09.02.62" <<<"$out" || fail "by version: got: $out"

# 5. Failures change nothing and exit non-zero.
setup
cp "$work/repo/Dockerfile" "$work/before"
listing "$(release v02.09.02.62 false false "$(asset Bambu_Studio_mac-v02.09.02.62-1.dmg 1 "sha256:$new_sha")")"
if run >/dev/null 2>&1; then fail "no AppImage: exited zero"; fi
listing "$(release v02.09.02.62 false false \
  "$(asset BambuStudio_ubu24-v02.09.02.62-1.AppImage 1 "sha256:$new_sha")" \
  "$(asset BambuStudio_ubuntu24.04-v02.09.02.62-2.AppImage 2 "sha256:$new_sha")")"
if run >/dev/null 2>&1; then fail "two AppImages: exited zero"; fi
listing "$(release v02.08.02.61 true false)"
if run >/dev/null 2>&1; then fail "no stable release: exited zero"; fi
rm -f "$work/asset.bin"
listing_new ""
if run >/dev/null 2>&1; then fail "no digest, no download: exited zero"; fi
rm -f "$work/releases.json"
if run >/dev/null 2>&1; then fail "no listing: exited zero"; fi
cmp -s "$work/before" "$work/repo/Dockerfile" || fail "a failure changed the Dockerfile"

# 6. A Dockerfile without the pin is refused, not rewritten.
setup
sed -i 's|^ARG BAMBU_STUDIO_VERSION=.*|ARG BAMBU_STUDIO_VERSION=latest|' "$work/repo/Dockerfile"
listing_new "sha256:$new_sha"
if run >/dev/null 2>&1; then fail "unpinned: exited zero"; fi
setup
sed -i '/^ARG BAMBU_STUDIO_ASSET_ID=/d' "$work/repo/Dockerfile"
if run >/dev/null 2>&1; then fail "no asset id ARG: exited zero"; fi

if [ "$failures" -gt 0 ]; then
  echo "$failures failure(s)" >&2
  exit 1
fi
echo "bambu-studio-bump.sh: all cases pass"
