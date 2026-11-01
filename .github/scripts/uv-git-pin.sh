#!/usr/bin/env bash
# Usage: uv-git-pin.sh <project dir>
# (run on backend/, for its temporal-agent-harness pin: harness-pin.yml)
#
# Fails when a uv project's git-pinned dependency commit no longer resolves (a
# commit only a draft PR references can vanish; spec 2026-10-01 §6.2). `uv lock --check` reads only the lockfile and passes even for a
# commit that is gone, so this also fetches every locked source with no cache,
# into a throwaway environment that installs nothing from the project itself.
set -euo pipefail
project="${1:?usage: uv-git-pin.sh <project dir>}"
venv="$(mktemp -d)"
trap 'rm -rf -- "$venv"' EXIT
cd "$project"
uv lock --check
UV_NO_CACHE=1 UV_PROJECT_ENVIRONMENT="$venv/.venv" uv sync --frozen --no-dev --no-install-project
