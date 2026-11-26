#!/usr/bin/env bash
# Tests for select-lanes.sh — which path-selected CI lanes run on this change.
# Both directions matter: a lane's change must select it, and an unrelated one (a
# near-miss path included) must not.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
fail=0
# expect "<backend> <agent_durable> <rust> <image> <docs_only>" path...
expect() {
  local want="$1"; shift
  local got
  got=$(printf '%s\n' "$@" | "$here/select-lanes.sh" | cut -d= -f2 | paste -sd' ')
  [[ "$got" == "$want" ]] || { echo "FAIL: $* -> $got, want $want"; fail=1; }
}
expect "true false false true false" backend/scadbuddy/main.py
expect "true false false true false" models/keychain/model.scad
expect "true false false true false" test-fixtures/makerworld/manifest.json
expect "true false false true false" test-fixtures/prime-tower/tower-choice.json
expect "true false false true false" e2e-stack/compose.yaml
expect "true false false true false" deploy/security/seccomp/profile-fetch.json
expect "true false false true false" deploy/security/apparmor/scadbuddy-intake-sandbox
expect "true false false true false" deploy/camera/mediamtx-relay.yml
expect "true false false true false" backend/scadbuddy/camera/recorder.py
expect "false false false false true" deploy/CLAUDE.md
expect "true false true true false" workers/rust/vectors/slice_plate.json
expect "true false false true false" frontend/page-csp.txt
expect "true false false true false" workers/playwright-mcp/browser-vectors.json
expect "false false false false false" workers/playwright-mcp/options.mjs
expect "true false false true false" THIRD_PARTY_NOTICES.md
expect "true false false true false" .github/scripts/merge-test-durations.sh
expect "true true false true false" backend/uv.lock
expect "true true false true false" backend/scadbuddy/migrations_agent/20261114T1609Z_session_file_bytes.sql
expect "true false false true false" backend/scadbuddy/migrations/20260928T0840Z_settings.sql
expect "false true false false false" agent-durable/scadbuddy_durable/worker.py
expect "false true false false false" agent/src/secrets.ts
expect "false false false false false" plugins/scadbuddy/skills/print/SKILL.md
expect "false true false false false" .github/scripts/uv-git-pin.sh
expect "false true false false false" .github/scripts/harness-pin.test.sh
expect "false false true false false" workers/rust/crates/sb-slicer/src/lib.rs
expect "false false false true false" frontend/src/main.tsx
expect "false false false true false" deploy/cups/start.sh
expect "false false false true false" scripts/gen-api-request-defaults.mjs
expect "false false false false false" scripts/generate-go-client.sh
expect "false false false true false" tools/go/bambuddy-fs/main.go
expect "true false false true false" tools/go/core/internal/bus/testdata/history/entity_property.changed.json
expect "false false false true false" tools/go/core/internal/bus/in.go
expect "false false false true false" go.work
expect "false false false true false" vendor/modules.txt
expect "false false false true false" .github/scripts/bambuddy-fs-live.sh
expect "true true true true false" Dockerfile
expect "true true true true false" .dockerignore
expect "true true true true false" .github/workflows/ci.yml
expect "true true true true false" .github/scripts/select-lanes.sh
expect "true true true true false" .github/scripts/changed-paths.sh
expect "false false false false false" README.md
# The design spec is the exception: agent/test/evalScoring.test.ts reads its §6.2, so a
# change to it is not docs-only and the agent job runs (#7792).
expect "false false false false false" docs/superpowers/specs/2026-09-22-scadbuddy-design.md
expect "false false false false true" docs/superpowers/specs/2026-09-27-template-pipelines-design.md
expect "false false false false false" .github/workflows/build-image.yml
expect "false false false false false" backendx/foo.py
expect "false false false false false" workers/rustacean.md
expect "false false false true false" frontend/page-csp.txt.bak
expect "true false true true false" backend/x.py workers/rust/Cargo.toml
expect "false false false false true" docs/ai/operating.md docs/architecture/ci-and-deploy.md
expect "false false false false false" docs/superpowers/specs/2026-09-22-scadbuddy-design.md README.md
expect "false false false false false" docs/architecture/diagram.png
expect "false false false false false" docs.md
expect "true false false true false" backend/docs/notes.md
# CLAUDE.md and AGENTS.md are docs in every directory, and select no lane.
expect "false false false false true" CLAUDE.md
expect "false false false false true" AGENTS.md
expect "false false false false true" backend/CLAUDE.md
expect "false false false false true" workers/rust/CLAUDE.md agent-durable/AGENTS.md models/CLAUDE.md docs/ai/operating.md
expect "false false false true false" backend/CLAUDE.md frontend/src/main.tsx
expect "true false false true false" frontend/CLAUDE.md backend/scadbuddy/main.py
expect "false false false false false" CLAUDE.md README.md
expect "true false false true false" backend/NOTCLAUDE.md
expect "true false false true false" backend/CLAUDE.md.bak
expect "true false false true false" backend/CLAUDE.md/x.py
expect "true false false true false" backend/CLAUDExmd
# A vendored module's CLAUDE.md or AGENTS.md is code: the Go vendor drift check runs.
expect "false false false true false" vendor/go.opentelemetry.io/otel/AGENTS.md
expect "false false false true false" tools/go/scadbuddy-client/vendor/go.opentelemetry.io/otel/CLAUDE.md
expect "false false false true false" CLAUDE.md tools/go/scadbuddy-client/vendor/go.opentelemetry.io/otel/AGENTS.md
got=$(printf '' | "$here/select-lanes.sh" | paste -sd' ')
[[ "$got" == "backend=false agent_durable=false rust=false image=false docs_only=false" ]] || { echo "FAIL: empty -> $got"; fail=1; }
got=$("$here/select-lanes.sh" all | paste -sd' ')
[[ "$got" == "backend=true agent_durable=true rust=true image=true docs_only=false" ]] || { echo "FAIL: all -> $got"; fail=1; }
# #7891: each path `read_by_tests` names is re-included in ci.yml's push `paths`, so a
# push to main that changes only that doc still runs CI. The check runs one way: ci.yml
# also re-includes paths that `read_by_tests` does not name (`**/vendor/**`).
read_by_tests=$(sed -n "s/^read_by_tests='\(.*\)'$/\1/p" "$here/select-lanes.sh")
[[ -n "$read_by_tests" ]] || { echo "FAIL: no read_by_tests in select-lanes.sh"; fail=1; }
IFS='|' read -ra patterns <<<"$read_by_tests"
for pattern in "${patterns[@]}"; do
  path=$(sed -e 's/^\^//' -e 's/\$$//' -e 's/\\//g' <<<"$pattern")
  # Each entry must be a literal path; a regex could not be matched to a ci.yml line.
  if [[ "$(sed -e 's/^\^//' -e 's/\$$//' -e 's/\\.//g' <<<"$pattern")" =~ [][*+?(){}^$.\\] ]]; then
    echo "FAIL: read_by_tests entry $pattern is not a literal path"; fail=1; continue
  fi
  grep -qxF -- "      - \"$path\"" "$here/../workflows/ci.yml" \
    || { echo "FAIL: ci.yml push paths do not re-include $path"; fail=1; }
done
exit "$fail"
