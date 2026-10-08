#!/usr/bin/env bash
# Usage: agent-durable-pin.sh [project dir, default agent-durable]
#
# Fails when agent-durable's git-pinned temporalio/ai-integrations commit no
# longer resolves (spec 2026-10-01 §6.2: a commit only a draft PR references can
# vanish). `uv lock --check` reads only the lockfile and passes even for a
# commit that is gone, so this also fetches every locked source with no cache,
# into a throwaway environment that installs nothing from the project itself.
set -euo pipefail
project="${1:-agent-durable}"
venv="$(mktemp -d)"
trap 'rm -rf -- "$venv"' EXIT
cd "$project"
uv lock --check
UV_NO_CACHE=1 UV_PROJECT_ENVIRONMENT="$venv/.venv" uv sync --frozen --no-dev --no-install-project
