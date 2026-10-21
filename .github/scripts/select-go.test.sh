#!/usr/bin/env bash
# Tests for select-go.sh — whether the `go` job checks the Go workspace on this
# change (#2686). Both directions matter: a Go change must be selected, and an
# unrelated one (a near-miss path included) must not be.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
fail=0
expect() { local want="$1"; shift; got=$(printf '%s\n' "$@" | "$here/select-go.sh"); [[ "$got" == "$want" ]] || { echo "FAIL: $* -> $got, want $want"; fail=1; }; }
expect true  tools/go/bambuddy-fs/main.go
expect true  go.work
expect true  go.work.sum
expect true  vendor/modules.txt
expect true  scripts/generate-go-client.sh
expect true  scripts/generate-go-client.jq
expect true  .github/scripts/generate-go-client-jq.test.sh
expect true  .github/workflows/go-module-ci.yml
expect true  tools/go/bambuddy-fs/Dockerfile
expect true  .github/workflows/ci.yml
expect true  .github/scripts/select-go.sh
expect true  .github/scripts/select-go.test.sh
expect false Dockerfile
expect false backend/scadbuddy/api/library_print.py
expect false README.md
expect false toolsy/go.mod
expect false go.workfoo
expect false .github/workflows/build-image.yml
expect false .github/scripts/select-models.sh
exit "$fail"
