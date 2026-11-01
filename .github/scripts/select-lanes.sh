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
#                  frontend and Go trees that run against it
#
# Each lane also selects on the shared build inputs (Dockerfile, .dockerignore) and
# on its own wiring (ci.yml, this script, changed-paths.sh), as select-go.sh does.
set -euo pipefail

lanes=(backend agent_durable rust image)
if [ "${1:-}" = all ]; then
  for lane in "${lanes[@]}"; do echo "$lane=true"; done
  exit 0
fi

wiring='Dockerfile$|\.dockerignore$|\.github/workflows/ci\.yml$|\.github/scripts/(select-lanes|changed-paths)(\.test)?\.sh$'
backend="^(backend/|models/|test-fixtures/|e2e-stack/|workers/rust/vectors/|frontend/page-csp\.txt$|THIRD_PARTY_NOTICES\.md$|\.gitattributes$|\.github/scripts/merge-test-durations\.sh$|$wiring)"
# agent-durable's tests read agent/ (migrations, fixtures, vectors). The job also checks
# the backend's harness pin.
agent_durable="^(agent-durable/|agent/|backend/(pyproject\.toml|uv\.lock)$|\.github/scripts/(uv-git-pin|harness-pin)(\.test)?\.sh$|$wiring)"
rust="^(workers/rust/|$wiring)"
image_extra='^(frontend/|tools/go/|go\.work(\.sum)?$|vendor/|\.github/scripts/(bambuddy-fs-live|fonts-probe)\.sh$)'

paths=$(cat)
pick() { if grep -qE "$1" <<<"$paths"; then echo true; else echo false; fi; }

b=$(pick "$backend")
echo "backend=$b"
echo "agent_durable=$(pick "$agent_durable")"
echo "rust=$(pick "$rust")"
if [ "$b" = true ]; then echo "image=true"; else echo "image=$(pick "$image_extra")"; fi
