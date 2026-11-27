#!/usr/bin/env bash
# Prints which path-selected CI lanes this change needs, one `<lane>=true|false`
# line each, from the changed paths on stdin (changed-paths.sh). With `all`, every
# lane is true: a push, the schedule, a dispatch, or a diff that cannot be read.
#
#   backend        the backend's pytest shards and their LFS fixtures
#   agent_durable  the `agent-durable` job
#   rust           the `rust` and `slicer-image` jobs
#   image          the app image: build, ruff and mypy, container smoke, real-backend
#                  e2e and the bambuddy-fs live mount; every backend change, plus the
#                  frontend and Go trees that run against it; and the
#                  `profile-fetch-image`, `camera-image` and `cups-image` jobs (#6625,
#                  #7328, deploy/cups/)
#   android        the `android` job: the phone app's build (app/, #7905). It selects
#                  on its own wiring only, not on the Dockerfile, which it never reads.
#   docs_only      every changed path is a Markdown file under docs/, or a CLAUDE.md
#                  or AGENTS.md in any directory but a vendor/ tree, and none is a doc a
#                  test reads (`read_by_tests`): the lint, frontend, agent, models,
#                  freshness and Go jobs skip, and only the `markdown` job checks the
#                  change. False with `all` and on an empty diff.
#
# Those docs paths select no lane, so `backend/CLAUDE.md` runs no backend job.
# A path under a vendor/ directory is never docs: a vendored module's CLAUDE.md or
# AGENTS.md is module content, and the Go job's vendor drift check must see it.
#
# Each lane also selects on the shared build inputs (Dockerfile, .dockerignore) and
# on its own wiring (ci.yml, this script, changed-paths.sh), as select-go.sh does.
set -euo pipefail

lanes=(backend agent_durable rust image android)
if [ "${1:-}" = all ]; then
  for lane in "${lanes[@]}"; do echo "$lane=true"; done
  echo "docs_only=false"
  exit 0
fi

wiring='Dockerfile$|\.dockerignore$|\.github/workflows/ci\.yml$|\.github/scripts/(select-lanes|changed-paths)(\.test)?\.sh$'
backend="^(backend/|models/|deploy/camera/|deploy/grafana/|deploy/security/|test-fixtures/|e2e-stack/|workers/rust/vectors/|workers/playwright-mcp/browser-vectors\.json$|frontend/page-csp\.txt$|THIRD_PARTY_NOTICES\.md$|\.gitattributes$|\.github/scripts/merge-test-durations\.sh$|$wiring)"
# agent-durable's tests read agent/ (fixtures, vectors) and the agent's migrations in
# backend/scadbuddy/migrations_agent/ (#5299). The job also checks the backend's harness pin.
agent_durable="^(agent-durable/|agent/|backend/scadbuddy/migrations_agent/|backend/(pyproject\.toml|uv\.lock)$|\.github/scripts/(uv-git-pin|harness-pin)(\.test)?\.sh$|$wiring)"
rust="^(workers/rust/|$wiring)"
android="^(app/|\.github/workflows/ci\.yml$|\.github/scripts/(select-lanes|changed-paths)(\.test)?\.sh$)"
image_extra='^(frontend/|deploy/cups/|scripts/gen-api-request-defaults\.mjs$|tools/go/|go\.work(\.sum)?$|vendor/|\.github/scripts/(bambuddy-fs-live|fonts-probe)\.sh$)'

docs='^(docs/.+\.md|(.+/)?(CLAUDE|AGENTS)\.md)$'
# A doc a test reads is code, not docs-only: `agent/test/evalScoring.test.ts` resolves
# the design spec's §6.2 from disk (#7792), and the Go proof tests in
# `tools/go/core/internal/inventory/modeldata/` read `docs/inventory-model/README.md` (#6949).
# A new test that reads a doc adds its path here.
read_by_tests='^docs/superpowers/specs/2026-09-22-scadbuddy-design\.md$|^docs/inventory-model/README\.md$'
paths=$(cat)
code=$(grep -vE "$docs" <<<"$paths" || true; grep -E "(^|/)vendor/|$read_by_tests" <<<"$paths" || true)
pick() { if grep -qE "$1" <<<"$code"; then echo true; else echo false; fi; }

b=$(pick "$backend")
echo "backend=$b"
echo "agent_durable=$(pick "$agent_durable")"
echo "rust=$(pick "$rust")"
if [ "$b" = true ]; then echo "image=true"; else echo "image=$(pick "$image_extra")"; fi
echo "android=$(pick "$android")"
if [ -n "$paths" ] && [ -z "$code" ]; then
  echo "docs_only=true"
else
  echo "docs_only=false"
fi
