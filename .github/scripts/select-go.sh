#!/usr/bin/env bash
# Prints true when any changed path on stdin is Go's (spec 2026-10-13 §7).
set -euo pipefail
if grep -qE '^(go\.work(\.sum)?|vendor/|tools/go/|scripts/generate-go-client\.sh$|\.github/workflows/go-module-ci\.yml$)'; then
  echo true
else
  echo false
fi
