#!/usr/bin/env bash
# Tests for select-go.sh — whether the `go` job checks the Go workspace on this
# change (#2686). Both directions matter: a Go change must be selected, and an
# unrelated one (a near-miss path included) must not be.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
fail=0
expect() { local want="$1"; shift; got=$(printf '%s\n' "$@" | "$here/select-go.sh"); [[ "$got" == "$want" ]] || { echo "FAIL: $* -> $got, want $want"; fail=1; }; }
expect true  tools/go/bambuddy-fs/main.go
expect true  backend/scadbuddy/migrations/20261102T0645Z_service_credentials.sql
expect true  backend/scadbuddy/migrations_agent/20260928T0107Z_sessions.sql
expect false backend/scadbuddy/migrationsx/a.sql
# core's sessions tests read the browser options' shared vectors.
expect true  workers/playwright-mcp/browser-vectors.json
expect false workers/playwright-mcp/options.mjs
expect true  backend/tests/fixtures/event-stream-vectors.json
expect false backend/tests/fixtures/event-stream-vectors.json.bak
expect true  go.work
expect true  go.work.sum
expect true  vendor/modules.txt
expect true  scripts/generate-go-client.sh
expect true  scripts/generate-go-client.jq
expect true  .github/scripts/generate-go-client-jq.test.sh
expect true  .github/workflows/go-module-ci.yml
expect true  tools/go/bambuddy-fs/Dockerfile
expect true  tools/go/fleetd/main.go
expect true  .github/workflows/ci.yml
expect true  .github/scripts/select-go.sh
expect true  .github/scripts/select-go.test.sh
expect true  .github/scripts/changed-paths.sh
expect true  agent/test/fixtures/rate-limit-reset-vectors.json
expect true  backend/tests/fixtures/plugin-seal-vectors.json
expect true  backend/tests/fixtures/auth-vectors.json
expect true  backend/tests/fixtures/secret-vectors.json
expect true  backend/tests/fixtures/pending-input-vectors.json
expect true  backend/tests/fixtures/response-hash-vectors.json
expect true  backend/tests/fixtures/payload-vectors.json
expect true  backend/tests/fixtures/subject-vectors.json
expect true  backend/tests/fixtures/approval-hash-vectors.json
expect false backend/tests/fixtures/hosted-plugin-hash-vectors.json
expect false agent/test/fixtures/secret-vectors.json
expect false agent/src/harness/credentialErrors.ts
expect false Dockerfile
expect false backend/scadbuddy/api/library_print.py
expect false README.md
expect false toolsy/go.mod
expect false go.workfoo
expect false .github/workflows/build-image.yml
expect false .github/scripts/select-models.sh
expect false .github/scripts/select-lanes.sh
exit "$fail"
