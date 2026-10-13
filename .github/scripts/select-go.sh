#!/usr/bin/env bash
# Prints true when any changed path on stdin is Go's (spec 2026-10-13 §7), or
# is the `go` job's own wiring (ci.yml, this script), as select-models.sh does.
set -euo pipefail
if grep -qE '^(go\.work(\.sum)?$|vendor/|tools/go/|scripts/generate-go-client\.sh$|\.github/workflows/(ci|go-module-ci)\.yml$|\.github/scripts/select-go(\.test)?\.sh$)'; then
  echo true
else
  echo false
fi
