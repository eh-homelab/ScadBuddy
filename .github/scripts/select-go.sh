#!/usr/bin/env bash
# Prints true when any changed path on stdin is Go's (spec 2026-10-13 §7), or
# is the `go` job's own wiring (ci.yml, this script, changed-paths.sh), as select-models.sh does.
# A file outside the Go trees that a Go test reads is Go's too: core's sessions tests read
# workers/playwright-mcp/browser-vectors.json, and claudecreds' test reads the agent's
# rate-limit-reset-vectors.json (#7986).
set -euo pipefail
if grep -qE '^(go\.work(\.sum)?$|vendor/|tools/go/|backend/scadbuddy/migrations(_agent)?/|workers/playwright-mcp/browser-vectors\.json$|scripts/generate-go-client\.(sh|jq)$|\.github/scripts/generate-go-client-jq\.test\.sh$|\.github/workflows/(ci|go-module-ci)\.yml$|\.github/scripts/select-go(\.test)?\.sh$|\.github/scripts/changed-paths\.sh$|agent/test/fixtures/rate-limit-reset-vectors\.json$)'; then
  echo true
else
  echo false
fi
