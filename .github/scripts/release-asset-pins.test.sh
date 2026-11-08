#!/usr/bin/env bash
# Tests release-asset-pins.sh against a stub curl and a made-up repo root.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
script="$here/release-asset-pins.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The stub serves "asset <id>" for any asset URL, or fails for id 404, and
# "temporal" for temporal.download.
cat >"$work/curl" <<'EOF'
#!/usr/bin/env bash
out=""
url=""
while (($#)); do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -H) shift 2 ;;
    https://*) url="$1"; shift ;;
    *) shift ;;
  esac
done
if [[ "$url" == https://temporal.download/* ]]; then
  printf 'temporal' >"$out"
  exit 0
fi
id="${url##*/}"
[[ "$id" == 404 ]] && exit 22
printf 'asset %s' "$id" >"$out"
EOF
chmod +x "$work/curl"

sha() { printf 'asset %s' "$1" | sha256sum | cut -d ' ' -f 1; }

# make_root <dir> <bambu id> <bambu sha>: every other pin matches its stub asset.
make_root() {
  local dir="$1"
  mkdir -p "$dir/.github/workflows"
  cat >"$dir/Dockerfile" <<EOF
ARG BAMBU_STUDIO_ASSET_ID=$2
ARG BAMBU_STUDIO_SHA256_AMD64=$3
ARG OPENSCAD_LSP_ASSET_AMD64=11
ARG OPENSCAD_LSP_SHA256_AMD64=$(sha 11)
ARG OPENSCAD_LSP_ASSET_ARM64=12
ARG OPENSCAD_LSP_SHA256_ARM64=$(sha 12)
ARG TEMPORAL_CLI_ASSET_ARM64=13
ARG TEMPORAL_CLI_SHA256_ARM64=$(sha 13)
ARG TEMPORAL_CLI_VERSION=1.9.1
ARG TEMPORAL_CLI_SHA256_AMD64=$(printf 'temporal' | sha256sum | cut -d ' ' -f 1)
EOF
  cat >"$dir/.github/workflows/ci.yml" <<EOF
        env:
          KUSTOMIZE_SHA256: $(sha 14)
          KUSTOMIZE_ASSET_ID: "14"
EOF
}

fail() { echo "FAIL: $*"; exit 1; }

make_root "$work/good" 10 "$(sha 10)"
RELEASE_ASSET_CURL="$work/curl" "$script" "$work/good" >"$work/out" ||
  fail "matching pins should pass: $(cat "$work/out")"
[[ "$(grep -c '^ok:' "$work/out")" == 6 ]] || fail "six pins checked: $(cat "$work/out")"

make_root "$work/drift" 10 "$(sha 99)"
if RELEASE_ASSET_CURL="$work/curl" "$script" "$work/drift" >"$work/out"; then
  fail "a stale sha256 should fail"
fi
grep -q 'Bambu Studio: .*assets/10 serves' "$work/out" || fail "names the drifted pin: $(cat "$work/out")"

# temporal.download serving another archive than the pinned amd64 sha256.
make_root "$work/temporal" 10 "$(sha 10)"
sed -i "s/^ARG TEMPORAL_CLI_SHA256_AMD64=.*/ARG TEMPORAL_CLI_SHA256_AMD64=$(sha 98)/" "$work/temporal/Dockerfile"
if RELEASE_ASSET_CURL="$work/curl" "$script" "$work/temporal" >"$work/out"; then
  fail "a temporal.download archive that drifted should fail"
fi
grep -q 'Temporal CLI amd64 (temporal.download): https://temporal.download/.* serves sha256' "$work/out" ||
  fail "names the temporal.download drift: $(cat "$work/out")"

make_root "$work/gone" 404 "$(sha 404)"
if RELEASE_ASSET_CURL="$work/curl" "$script" "$work/gone" >"$work/out"; then
  fail "an asset that cannot be fetched should fail"
fi

make_root "$work/missing" "" ""
if RELEASE_ASSET_CURL="$work/curl" "$script" "$work/missing" >"$work/out"; then
  fail "a missing pin should fail"
fi
grep -q 'Bambu Studio: a pin was not found' "$work/out" || fail "says the pin is missing: $(cat "$work/out")"

# --resolve fetches nothing: a stub that always fails proves it.
printf '#!/bin/sh\nexit 99\n' >"$work/nocurl"
chmod +x "$work/nocurl"
RELEASE_ASSET_CURL="$work/nocurl" "$script" --resolve "$work/good" >"$work/out" ||
  fail "--resolve should pass without a fetch: $(cat "$work/out")"
[[ "$(grep -c '^found:' "$work/out")" == 6 ]] || fail "six pins found: $(cat "$work/out")"
if RELEASE_ASSET_CURL="$work/nocurl" "$script" --resolve "$work/missing" >"$work/out"; then
  fail "--resolve should fail on a missing pin"
fi

# The repo's own Dockerfile and ci.yml: every pin is found.
RELEASE_ASSET_CURL="$work/nocurl" "$script" --resolve "$here/../.." >"$work/out" ||
  fail "the repo's pins should resolve: $(cat "$work/out")"
[[ "$(grep -c '^found:' "$work/out")" == 6 ]] || fail "six of the repo's pins found: $(cat "$work/out")"

echo "release-asset-pins: all tests passed"
