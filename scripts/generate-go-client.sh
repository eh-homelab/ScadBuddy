#!/usr/bin/env bash
# Generates the Go ScadBuddy client (tools/go/scadbuddy-client/client) from the
# backend's OpenAPI spec with Kiota's pinned image, then vendors (spec 2026-10-13 §6).
# Adapted from WonderForgeLabs/forge scripts/generate-kiota-clients.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE="$ROOT/tools/go/scadbuddy-client"
OUT="$MODULE/client"
NAMESPACE="github.com/eh-homelab/ScadBuddy/tools/go/scadbuddy-client/client"
KIOTA_IMAGE="mcr.microsoft.com/openapi/kiota:1.36.1@sha256:077073656898de0f8d3339bad496f154d603c30c0cdcbcbc5feacad2ad988d9d"
SPEC_DIR="$ROOT/tools/go/.spec"   # under the /work mount; git-ignored

check=false
spec=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) check=true; shift ;;
    --spec) spec="$2"; shift 2 ;;
    *) echo "usage: $0 [--check] [--spec <openapi.json>]" >&2; exit 2 ;;
  esac
done

mkdir -p "$SPEC_DIR"
trap 'rm -rf "$SPEC_DIR"' EXIT
if [[ -z "$spec" ]]; then
  (cd "$ROOT/backend" && uv run --frozen python -m scadbuddy.tools.export_openapi "$SPEC_DIR/raw.json" >/dev/null)
else
  cp "$spec" "$SPEC_DIR/raw.json"
fi

# Two generator workarounds, both leaving the wire format alone:
#  - Kiota's doc comments depend on hash order when a $ref has sibling descriptions;
#    dropping them makes generation reproducible (forge #1043).
#  - A default on an anyOf/oneOf property (PrintChoices.tier: Tier | None = "standard")
#    makes Kiota's Go emit `m.SetTier(&"standard")` on a union wrapper, which does not
#    compile; the default is only a hint to a client that never reads it.
jq 'walk(if type == "object" and has("$ref") and (has("description") or has("summary")) then del(.description, .summary) else . end)
    | walk(if type == "object" and (has("anyOf") or has("oneOf")) then del(.default) else . end)' \
  "$SPEC_DIR/raw.json" > "$SPEC_DIR/openapi.json"

# Delete first: a file the generator no longer emits must show up as a deletion.
rm -rf "$OUT"
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$ROOT:/work" "$KIOTA_IMAGE" \
  generate -d /work/tools/go/.spec/openapi.json -l Go -o /work/tools/go/scadbuddy-client/client \
  -n "$NAMESPACE" \
  --include-path '/api/v1/print/library' \
  --include-path '/api/v1/print/library/**' \
  --include-path '/api/v1/operations/**' \
  --clean-output >/dev/null

rm -f "$OUT/.kiota.log"
# The hash covers the whole spec; keeping it would churn on every unrelated API change.
jq 'del(.descriptionHash)' "$OUT/kiota-lock.json" > "$OUT/kiota-lock.json.tmp"
mv "$OUT/kiota-lock.json.tmp" "$OUT/kiota-lock.json"

gofmt -w "$OUT"
# The client's own vendor/ first, in module mode, then the workspace's. No `go work
# sync`: it raises each module's requires to the workspace's build list, which would
# move the client's go.mod after its vendor/modules.txt was written (inconsistent
# vendoring in the module-mode build) and edit bambuddy-fs's go.mod on a client-only
# change. Each module keeps its own requires; the workspace takes the highest.
(cd "$MODULE" && GOWORK=off go mod tidy && GOWORK=off go mod vendor)
(cd "$ROOT" && go work vendor)

if [[ "$check" == "true" ]]; then
  # Against the index, not HEAD: staged files are not drift, anything the run changed or added since is.
  drift=$(cd "$ROOT" && git ls-files --modified --others --deleted --exclude-standard -- tools/go/scadbuddy-client vendor go.work go.work.sum)
  if [[ -n "$drift" ]]; then
    echo "The Go client is stale. Run scripts/generate-go-client.sh and commit:" >&2
    echo "$drift" >&2
    exit 1
  fi
fi
