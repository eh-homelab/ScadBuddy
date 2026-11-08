#!/usr/bin/env bash
# Tests release-asset-pins.sh against a stub curl and a made-up repo root.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
script="$here/release-asset-pins.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# The stub serves "asset <id>" for any asset URL, or fails for id 404.
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
[[ "$(grep -c '^ok:' "$work/out")" == 5 ]] || fail "five pins checked: $(cat "$work/out")"

make_root "$work/drift" 10 "$(sha 99)"
if RELEASE_ASSET_CURL="$work/curl" "$script" "$work/drift" >"$work/out"; then
  fail "a stale sha256 should fail"
fi
grep -q 'Bambu Studio: asset 10' "$work/out" || fail "names the drifted pin: $(cat "$work/out")"

make_root "$work/gone" 404 "$(sha 404)"
if RELEASE_ASSET_CURL="$work/curl" "$script" "$work/gone" >"$work/out"; then
  fail "an asset that cannot be fetched should fail"
fi

make_root "$work/missing" "" ""
if RELEASE_ASSET_CURL="$work/curl" "$script" "$work/missing" >"$work/out"; then
  fail "a missing pin should fail"
fi
grep -q 'no pinned asset id' "$work/out" || fail "says the pin is missing: $(cat "$work/out")"

echo "release-asset-pins: all tests passed"
