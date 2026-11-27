#!/usr/bin/env bash
# Prints true when any changed path on stdin is Go's (spec 2026-10-13 §7), or
# is the `go` job's own wiring (ci.yml, this script, changed-paths.sh), as select-models.sh does.
# It also matches `docs/inventory-model/` and the migrations: Go tests read them.
# claudecreds' test reads the agent's rate-limit-reset-vectors.json (#7986); core's read the
# backend's event-stream-vectors.json (#7768), auth-vectors.json (#8038),
# plugin-seal-vectors.json (#8048) and blob-upload-vectors.json (#7958).
set -euo pipefail
if grep -qE '^(go\.work(\.sum)?$|vendor/|tools/go/|backend/scadbuddy/migrations(_agent)?/|backend/tests/fixtures/(event-stream|auth|plugin-seal|blob-upload)-vectors\.json$|docs/inventory-model/|scripts/generate-go-client\.(sh|jq)$|\.github/scripts/generate-go-client-jq\.test\.sh$|\.github/workflows/(ci|go-module-ci)\.yml$|\.github/scripts/select-go(\.test)?\.sh$|\.github/scripts/changed-paths\.sh$|agent/test/fixtures/rate-limit-reset-vectors\.json$)'; then
  echo true
else
  echo false
fi
