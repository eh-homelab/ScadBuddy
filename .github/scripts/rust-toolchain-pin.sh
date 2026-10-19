#!/usr/bin/env bash
# rust-toolchain-pin.sh [--check] [ci.yml]: the dtolnay/rust-toolchain SHA ci.yml pins
# against the action's master. The action has no release tags, so Dependabot never moves
# this pin (#4099); the weekly rust-toolchain-pin.yml is the reminder. Prints both SHAs;
# with --check, exits 1 when they differ, naming the line to change. Needs gh.
set -euo pipefail
check=0
if [ "${1:-}" = --check ]; then check=1; shift; fi
ci="${1:-$(cd "$(dirname "$0")/../.." && pwd)/.github/workflows/ci.yml}"

line="$(grep -n 'dtolnay/rust-toolchain@' "$ci" | head -n 1)" || {
  echo "no dtolnay/rust-toolchain@ pin in $ci" >&2; exit 2
}
lineno="${line%%:*}"
pinned="$(grep -o 'dtolnay/rust-toolchain@[0-9a-f]\{40\}' <<<"$line" | cut -d@ -f2)" || {
  echo "$ci:$lineno: the pin is not a full commit SHA" >&2; exit 2
}
master="$(gh api repos/dtolnay/rust-toolchain/commits/master --jq .sha)"

echo "pinned: $pinned ($ci:$lineno)"
echo "master: $master"
if [ "$check" = 1 ] && [ "$pinned" != "$master" ]; then
  echo "$ci:$lineno: dtolnay/rust-toolchain is pinned to $pinned; master is $master. Move the pin (and its 'master as of' date) in a reviewed PR." >&2
  exit 1
fi
