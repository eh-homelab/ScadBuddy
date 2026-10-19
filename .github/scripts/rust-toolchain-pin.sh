#!/usr/bin/env bash
# rust-toolchain-pin.sh [--check] [workflow.yml...]: every dtolnay/rust-toolchain SHA the
# workflows pin (default: every .github/workflows/*.y*ml) against the action's master. The
# action has no release tags, so Dependabot never moves these pins (#4099); the weekly
# rust-toolchain-pin.yml is the reminder. Prints master and each pin; with --check, exits
# 1 when any differs, naming each line to change (#4264). A comment line naming the
# action is not a pin and is skipped. Needs gh.
set -euo pipefail
check=0
if [ "${1:-}" = --check ]; then check=1; shift; fi
if [ "$#" = 0 ]; then
  root="$(cd "$(dirname "$0")/../.." && pwd)"
  set -- "$root"/.github/workflows/*.y*ml
fi

lines="$(grep -Hn 'dtolnay/rust-toolchain@' "$@" | grep -vE '^[^:]+:[0-9]+:[[:space:]]*#')" || {
  echo "no dtolnay/rust-toolchain@ pin in $*" >&2; exit 2
}
master="$(gh api repos/dtolnay/rust-toolchain/commits/master --jq .sha)"
echo "master: $master"

stale=0
while IFS= read -r line; do
  at="$(cut -d: -f1,2 <<<"$line")"  # file:lineno
  pinned="$(grep -o 'dtolnay/rust-toolchain@[0-9a-f]\{40\}' <<<"$line" | cut -d@ -f2)" || {
    echo "$at: the pin is not a full commit SHA" >&2; exit 2
  }
  echo "pinned: $pinned ($at)"
  if [ "$pinned" != "$master" ]; then
    stale=1
    if [ "$check" = 1 ]; then
      echo "$at: dtolnay/rust-toolchain is pinned to $pinned; master is $master. Move the pin (and its 'master as of' date) in a reviewed PR." >&2
    fi
  fi
done <<<"$lines"

if [ "$check" = 1 ] && [ "$stale" = 1 ]; then exit 1; fi
