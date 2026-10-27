#!/usr/bin/env bash
# postgres-pin.sh against fixture workflows and a stub curl serving a fake registry
# (#4729): a digest pin passes, a bare tag or two different digests fail naming the
# line, and --check fails on a blob the mirror lacks, on a manifest it cannot read and
# on a moved tag. No network.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

major="$(sed -n 's/^major=\([0-9]*\).*/\1/p' "$here/postgres-pin.sh")"  # the script's own
[ -n "$major" ] || { echo "FAIL: no major= line in postgres-pin.sh"; exit 1; }
pin="sha256:$(printf 'a%.0s' {1..64})"
other="sha256:$(printf 'b%.0s' {1..64})"
service() {  # service <file> <image>
  printf 'jobs:\n  test:\n    services:\n      postgres:\n        image: %s\n' "$2" >"$1"
}

fails() {  # fails <what> <expected text> <args...>
  local what="$1" want="$2" out
  shift 2
  if out="$(PATH="$work/bin:$PATH" "$here/postgres-pin.sh" "$@" 2>&1)"; then
    echo "FAIL: $what passed"; exit 1
  fi
  if ! grep -qF -- "$want" <<<"$out"; then
    echo "FAIL: $what failed without saying '$want':"; echo "$out"; exit 1
  fi
}

service "$work/ci.yml" "mirror.gcr.io/library/postgres:$major@$pin"
"$here/postgres-pin.sh" "$work/ci.yml" >/dev/null

service "$work/bare.yml" "mirror.gcr.io/library/postgres:$major"
fails "a bare tag" "$work/bare.yml:5:" "$work/ci.yml" "$work/bare.yml"

service "$work/hub.yml" "postgres:$major@$pin"
fails "a Docker Hub image" "$work/hub.yml:5:" "$work/hub.yml"

# Another image whose name merely contains postgres is not the service's.
printf 'jobs:\n  test:\n    services:\n      exporter:\n        image: quay.io/prometheuscommunity/postgres-exporter:v0.15.0\n' >"$work/exporter.yml"
"$here/postgres-pin.sh" "$work/ci.yml" "$work/exporter.yml" >/dev/null

service "$work/split.yml" "mirror.gcr.io/library/postgres:$major@$other"
fails "two digests" "$work/split.yml:5:" "$work/ci.yml" "$work/split.yml"

# The fake registry: one file per manifest or blob under $work/registry, the tag's
# digest in manifests/$major.digest. The stub reads only the URL and -I / -D.
reg="$work/registry"
mkdir -p "$work/bin" "$reg/manifests" "$reg/blobs"
cat >"$work/bin/curl" <<STUB
#!/usr/bin/env bash
url=''; head=0; headers=0
for arg in "\$@"; do
  case "\$arg" in
    http*) url="\$arg" ;;
    -sSIL) head=1 ;;
    -D) headers=1 ;;
  esac
done
path="$reg/\${url#https://registry.test/v2/library/postgres/}"
if [ "\$head" = 1 ]; then
  if [ -e "\$path" ]; then printf 200; else printf 404; fi
elif [ "\$headers" = 1 ]; then
  printf 'HTTP/2 200\r\nDocker-Content-Digest: %s\r\n\r\n' "\$(cat "$reg/manifests/$major.digest")"
else
  cat "\$path" || exit 22
fi
STUB
chmod +x "$work/bin/curl"
export POSTGRES_PIN_REGISTRY=https://registry.test

image() {  # image <index digest> <amd64 manifest digest> <blob...>
  local index="$1" amd64="$2" blob
  shift 2
  printf '{"manifests":[{"digest":"sha256:arm","platform":{"os":"linux","architecture":"arm64"}},{"digest":"%s","platform":{"os":"linux","architecture":"amd64"}}]}' \
    "$amd64" >"$reg/manifests/$index"
  printf '{"config":{"digest":"%s"},"layers":[' "$1" >"$reg/manifests/$amd64"
  printf '{"digest":"%s"}' "${@:2}" | sed 's/}{/},{/g' >>"$reg/manifests/$amd64"
  printf ']}' >>"$reg/manifests/$amd64"
  for blob in "$@"; do touch "$reg/blobs/$blob"; done
}

image "$pin" sha256:amd64-a sha256:config-a sha256:layer-1 sha256:layer-2
echo "$pin" >"$reg/manifests/$major.digest"
PATH="$work/bin:$PATH" "$here/postgres-pin.sh" --check "$work/ci.yml" >/dev/null

rm "$reg/blobs/sha256:layer-2"
fails "a blob the mirror lacks" "sha256:layer-2 (HTTP 404)" --check "$work/ci.yml"
touch "$reg/blobs/sha256:layer-2"

# A manifest the mirror will not serve is an error, never "every blob is there".
mv "$reg/manifests/sha256:amd64-a" "$work/amd64-a"
fails "an amd64 manifest that cannot be read" "could not read the linux/amd64 manifest sha256:amd64-a" --check "$work/ci.yml"
mv "$work/amd64-a" "$reg/manifests/sha256:amd64-a"
mv "$reg/manifests/$pin" "$work/index"
fails "an index that cannot be read" "could not read the index $pin" --check "$work/ci.yml"
mv "$work/index" "$reg/manifests/$pin"

: >"$reg/manifests/$major.digest"
fails "a tag the mirror gives no digest for" "the mirror gave no digest for postgres:$major" --check "$work/ci.yml"
printf '{"manifests":[{"digest":"sha256:arm","platform":{"os":"linux","architecture":"arm64"}}]}' >"$reg/manifests/$other"
echo "$other" >"$reg/manifests/$major.digest"
fails "a moved tag with no linux/amd64 image" "$other (no linux/amd64 manifest)" --check "$work/ci.yml"

image "$other" sha256:amd64-b sha256:config-b sha256:layer-1 sha256:layer-3
echo "$other" >"$reg/manifests/$major.digest"
fails "a moved tag" "moved to $other and the mirror holds all of its blobs" --check "$work/ci.yml"

rm "$reg/blobs/sha256:layer-3"
fails "a moved tag the mirror lacks a blob of" "keep the pin" --check "$work/ci.yml"

echo "ok: a digest pin passes, a bare tag or split digests fail, --check catches a missing blob and a moved tag"
