#!/usr/bin/env bash
# agent-durable-pin.sh, pointed at backend/, must fail when the pinned
# temporal-agent-harness commit is gone (plan 2026-10-09-durable-phase-6-flows.md
# Ruling 1), which `uv lock --check` alone never notices: it reads only the
# lockfile. Needs uv and the network.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

cp "$root/backend/pyproject.toml" "$root/backend/uv.lock" "$work/"
sha="$(grep -o 'temporal-agent-harness@[0-9a-f]\{40\}' "$work/pyproject.toml" | cut -d@ -f2)"
sed -i "s/$sha/deadbeefdeadbeefdeadbeefdeadbeefdeadbeef/g" "$work/pyproject.toml" "$work/uv.lock"

if out="$("$here/agent-durable-pin.sh" "$work" 2>&1)"; then
  echo "FAIL: a vanished pin passed the check"; exit 1
fi
# The failure must be the missing commit, not (say) no network.
if ! grep -q "failed to fetch commit \`deadbeef" <<<"$out"; then
  echo "FAIL: the check failed, but not on the vanished commit:"; echo "$out"; exit 1
fi
"$here/agent-durable-pin.sh" "$root/backend" >/dev/null
echo "ok: a vanished harness pin fails, the real pin passes"
