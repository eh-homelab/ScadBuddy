#!/usr/bin/env bash
# postgres-pin.sh [--check] [workflow.yml...]: the workflows' Postgres service containers
# (default: every .github/workflows/*.y*ml, and e2e-stack/compose.yaml's, which runs the
# same image) are pinned by digest (#4729). A service
# container pulls from mirror.gcr.io with no Docker Hub fallback (#3370), so when the
# moving `postgres:18` tag reached a layer the mirror did not yet hold, every job with
# the service failed before any test ran. A digest whose every blob the mirror holds
# cannot do that.
#
# Without --check (the `lint` job, no network): every `image:` of the postgres image is
# mirror.gcr.io/library/postgres:18@sha256:<64 hex>, and all of them the same digest.
# With --check (the weekly postgres-pin.yml): also that the mirror holds every blob of
# the pin's linux/amd64 image, and that `18` on the mirror still points at the pin. A
# moved tag exits 1, the reminder to move the pin in a reviewed PR, saying whether the
# mirror holds all of the new digest's blobs yet (move it only once it does).
# Needs curl and jq for --check; POSTGRES_PIN_REGISTRY overrides the registry (tests).
set -euo pipefail
check=0
if [ "${1:-}" = --check ]; then check=1; shift; fi
if [ "$#" = 0 ]; then
  root="$(cd "$(dirname "$0")/../.." && pwd)"
  set -- "$root"/.github/workflows/*.y*ml "$root"/e2e-stack/compose.yaml
fi

image='mirror.gcr.io/library/postgres:18'
lines="$(grep -HnE '^[[:space:]]*image:.*(library/postgres|[/[:space:]]postgres)[:@]' "$@")" || {
  echo "no postgres service image in $*" >&2; exit 2
}

bad=0
pin=''
while IFS= read -r line; do
  at="$(cut -d: -f1,2 <<<"$line")"  # file:lineno
  text="$(cut -d: -f3- <<<"$line")"
  if ! [[ "$text" =~ ^[[:space:]]*image:[[:space:]]*mirror\.gcr\.io/library/postgres:18@(sha256:[0-9a-f]{64})[[:space:]]*(#.*)?$ ]]; then
    echo "$at: the postgres service image is not $image@sha256:<digest> (#4729)" >&2
    bad=1
    continue
  fi
  digest="${BASH_REMATCH[1]}"
  echo "pinned: $digest ($at)"
  if [ -z "$pin" ]; then
    pin="$digest"
  elif [ "$digest" != "$pin" ]; then
    echo "$at: pinned to $digest; an earlier service is pinned to $pin. Pin them all to one digest." >&2
    bad=1
  fi
done <<<"$lines"
if [ "$bad" = 1 ]; then exit 1; fi
if [ "$check" = 0 ]; then exit 0; fi

registry="${POSTGRES_PIN_REGISTRY:-https://mirror.gcr.io}/v2/library/postgres"
index_types='application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json'
manifest_types='application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'

# missing <index digest>: prints each blob of its linux/amd64 image the mirror lacks.
# A manifest it cannot read exits 2: an empty answer must mean every blob was checked.
missing() {
  local index amd64 blobs blob code
  index="$(curl -fsS --retry 3 -H "Accept: $index_types" "$registry/manifests/$1")" || {
    echo "could not read the index $1 from the mirror" >&2; exit 2
  }
  amd64="$(jq -r '.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest' <<<"$index")" || {
    echo "the index $1 on the mirror is not an image index" >&2; exit 2
  }
  if [ -z "$amd64" ]; then echo "$1 (no linux/amd64 manifest)"; return; fi
  blobs="$(curl -fsS --retry 3 -H "Accept: $manifest_types" "$registry/manifests/$amd64" \
    | jq -r '.config.digest, .layers[].digest')" || {
    echo "could not read the linux/amd64 manifest $amd64 of $1 from the mirror" >&2; exit 2
  }
  if [ -z "$blobs" ]; then echo "the linux/amd64 manifest $amd64 of $1 names no blobs" >&2; exit 2; fi
  for blob in $blobs; do
    code="$(curl -sSIL --retry 3 -o /dev/null -w '%{http_code}' "$registry/blobs/$blob")" || true
    if [ "$code" != 200 ]; then echo "$blob (HTTP $code)"; fi
  done
}

lacking="$(missing "$pin")" || exit $?
if [ -n "$lacking" ]; then
  echo "the mirror lacks blobs of the pinned $pin, so every job with the postgres service fails:" >&2
  echo "$lacking" >&2
  exit 1
fi
echo "the mirror holds every blob of the pin's linux/amd64 image"

tag="$(curl -fsS --retry 3 -o /dev/null -D - -H "Accept: $index_types" "$registry/manifests/18" \
  | tr -d '\r' | awk 'tolower($1) == "docker-content-digest:" { print $2 }')" || {
  echo "could not read postgres:18 from the mirror" >&2; exit 2
}
if [ -z "$tag" ]; then echo "the mirror gave no digest for postgres:18" >&2; exit 2; fi
echo "postgres:18 on the mirror: $tag"
if [ "$tag" != "$pin" ]; then
  lacking="$(missing "$tag")" || exit $?
  if [ -n "$lacking" ]; then
    echo "postgres:18 moved to $tag, but the mirror lacks some of its blobs; keep the pin until it holds them:" >&2
    echo "$lacking" >&2
  else
    echo "postgres:18 moved to $tag and the mirror holds all of its blobs. Move every pin to it in a reviewed PR." >&2
  fi
  exit 1
fi
