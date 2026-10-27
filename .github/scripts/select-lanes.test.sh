#!/usr/bin/env bash
# Tests for select-lanes.sh — which path-selected CI lanes run on this change.
# Both directions matter: a lane's change must select it, and an unrelated one (a
# near-miss path included) must not.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
fail=0
# expect "<backend> <agent_durable> <rust> <image>" path...
expect() {
  local want="$1"; shift
  local got
  got=$(printf '%s\n' "$@" | "$here/select-lanes.sh" | cut -d= -f2 | paste -sd' ')
  [[ "$got" == "$want" ]] || { echo "FAIL: $* -> $got, want $want"; fail=1; }
}
expect "true false false true"  backend/scadbuddy/main.py
expect "true false false true"  models/keychain/model.scad
expect "true false false true"  test-fixtures/makerworld/manifest.json
expect "true false false true"  e2e-stack/compose.yaml
expect "true false true true"   workers/rust/vectors/slice_plate.json
expect "true false false true"  frontend/page-csp.txt
expect "true false false true"  THIRD_PARTY_NOTICES.md
expect "true false false true"  .github/scripts/merge-test-durations.sh
expect "true true false true"   backend/uv.lock
expect "false true false false" agent-durable/scadbuddy_durable/worker.py
expect "false true false false" agent/src/secrets.ts
expect "false true false false" plugins/scadbuddy/skills/print/SKILL.md
expect "false true false false" .github/scripts/harness-pin.test.sh
expect "false false true false" workers/rust/crates/sb-slicer/src/lib.rs
expect "false false false true" frontend/src/main.tsx
expect "false false false true" tools/go/bambuddy-fs/main.go
expect "false false false true" go.work
expect "false false false true" vendor/modules.txt
expect "false false false true" .github/scripts/bambuddy-fs-live.sh
expect "true true true true"    Dockerfile
expect "true true true true"    .dockerignore
expect "true true true true"    .github/workflows/ci.yml
expect "true true true true"    .github/scripts/select-lanes.sh
expect "true true true true"    .github/scripts/changed-paths.sh
expect "false false false false" README.md
expect "false false false false" docs/superpowers/specs/2026-09-22-scadbuddy-design.md
expect "false false false false" .github/workflows/build-image.yml
expect "false false false false" backendx/foo.py
expect "false false false false" workers/rustacean.md
expect "false false false true" frontend/page-csp.txt.bak
expect "true false true true"   backend/x.py workers/rust/Cargo.toml
got=$("$here/select-lanes.sh" all | paste -sd' ')
[[ "$got" == "backend=true agent_durable=true rust=true image=true" ]] || { echo "FAIL: all -> $got"; fail=1; }
exit "$fail"
