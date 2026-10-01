#!/usr/bin/env bash
#
# Fail when a plugin's files changed since <base> but its `version` did not
# (#896). Setting `version` "keeps users on that version until you change it"
# (https://code.claude.com/docs/en/plugins-reference, "Fields"), so a change
# at the same version never reaches an install. plugin-edited.sh bumps it for
# edits made through Claude Code; this is the CI check for every other edit.
# Used by the `lint` job in ci.yml on pull requests:
#
#   .github/scripts/check-plugin-version.sh <base commit> [repo-root]
#
# A change under agent/plugins/scadbuddy counts as a change to
# plugins/scadbuddy (lint-plugin.sh holds the two at one version). A plugin
# new since <base> passes. Exit 1 with one line per plugin on failure, 2 on a
# usage error.
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: $0 <base commit> [repo-root]" >&2
  exit 2
fi
base="$1"
root="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
if ! git -C "$root" rev-parse --verify --quiet "$base^{commit}" >/dev/null; then
  echo "usage: $0 <base commit> [repo-root]: $base is not a commit" >&2
  exit 2
fi

changed="$(git -C "$root" diff --name-only "$base" HEAD -- plugins agent/plugins/scadbuddy)"
plugins="$(printf '%s\n' "$changed" | sed -nE 's#^(agent/)?plugins/([^/]+)/.*#\2#p' | sort -u)"

errors=0
for plugin in $plugins; do
  manifest="plugins/$plugin/.claude-plugin/plugin.json"
  [ -f "$root/$manifest" ] || continue
  before="$(git -C "$root" show "$base:$manifest" 2>/dev/null | jq -r '.version // empty' || true)"
  [ -n "$before" ] || continue
  now="$(jq -r '.version // empty' "$root/$manifest")"
  if [ "$now" = "$before" ]; then
    echo "$manifest: plugin files changed but 'version' is still $before; bump it"
    errors=$((errors + 1))
  fi
done

[ "$errors" -eq 0 ] || exit 1
echo "plugin versions: ok"
