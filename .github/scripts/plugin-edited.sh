#!/usr/bin/env bash
#
# Claude Code PostToolUse hook (.claude/settings.json): after an edit under
# plugins/, agent/plugins/scadbuddy/ or .claude-plugin/, bump the plugin's
# version if this branch has not yet, then lint and validate.
#
# Why bump: "Setting [version] keeps users on that version until you change
# it" (https://code.claude.com/docs/en/plugins-reference, "Fields"), so a change
# shipped at the same version never reaches an install. The version is bumped
# once per branch: a patch bump when it still equals the merge base's, and left
# alone once it differs (bumped already, or by hand). plugins/scadbuddy's bump
# is mirrored into agent/plugins/scadbuddy, which lint-plugin.sh holds to the
# same version.
#
# Reads the hook's JSON on stdin. Exit 0 with a JSON `additionalContext` when it
# bumped something; exit 2 with the problems on stderr (shown to Claude) when
# the lint or `claude plugin validate` fails; exit 0 silently otherwise.
#
#   PLUGIN_EDITED_BASE   the ref to compare against (default: origin/main)
#   PLUGIN_EDITED_SKIP_VALIDATE=1   skip `claude plugin validate` (tests)
set -euo pipefail

input="$(cat)"
file="$(jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' <<<"$input")"
[ -n "$file" ] || exit 0

dir="$(dirname "$file")"
while [ ! -d "$dir" ]; do dir="$(dirname "$dir")"; done
root="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)" || exit 0
case "$file" in
  /*) rel="${file#"$root"/}" ;;
  *) rel="$file" ;;
esac

plugin=""
case "$rel" in
  agent/plugins/scadbuddy/*) plugin=scadbuddy ;;
  plugins/*/*)
    plugin="${rel#plugins/}"
    plugin="${plugin%%/*}"
    ;;
  .claude-plugin/*) ;;
  *) exit 0 ;;
esac

notes=()

# bump <manifest path relative to root>: patch-bump when it equals the base's.
bump() {
  local manifest="$1" base_ref merge_base base current next
  [ -f "$root/$manifest" ] || return 1
  base_ref="${PLUGIN_EDITED_BASE:-origin/main}"
  merge_base="$(git -C "$root" merge-base HEAD "$base_ref" 2>/dev/null)" || return 1
  base="$(git -C "$root" show "$merge_base:$manifest" 2>/dev/null | jq -r '.version // empty')" || return 1
  current="$(jq -r '.version // empty' "$root/$manifest")"
  [ -n "$base" ] && [ "$current" = "$base" ] || return 1
  if [[ "$current" =~ ^(.*[^0-9])?([0-9]+)$ ]]; then
    next="${BASH_REMATCH[1]}$((BASH_REMATCH[2] + 1))"
  else
    return 1
  fi
  set_version "$manifest" "$next"
  notes+=("bumped $manifest from $current to $next (first plugin change on this branch)")
}

# set_version <manifest> <version>: rewrites only the `"version": "…"` line, so
# the rest of the file keeps its formatting.
set_version() {
  local manifest="$1" version="$2"
  sed -i -E "0,/\"version\"[[:space:]]*:[[:space:]]*\"[^\"]*\"/s//\"version\": \"$version\"/" "$root/$manifest"
}

if [ -n "$plugin" ]; then
  manifest="plugins/$plugin/.claude-plugin/plugin.json"
  bump "$manifest" || true
  own="agent/plugins/scadbuddy/.claude-plugin/plugin.json"
  if [ "$plugin" = scadbuddy ] && [ -f "$root/$own" ] && [ -f "$root/$manifest" ]; then
    version="$(jq -r .version "$root/$manifest")"
    if [ "$(jq -r .version "$root/$own")" != "$version" ]; then
      set_version "$own" "$version"
      notes+=("set $own to $version to match $manifest")
    fi
  fi
fi

problems=""
if ! out="$("$root/.github/scripts/lint-plugin.sh" "$root" 2>&1)"; then
  problems+="$out"$'\n'
fi
if [ "${PLUGIN_EDITED_SKIP_VALIDATE:-}" != 1 ] && command -v claude >/dev/null; then
  targets=(.)
  [ -z "$plugin" ] || targets+=("plugins/$plugin")
  for target in "${targets[@]}"; do
    if ! out="$(cd "$root" && claude plugin validate "$target" 2>&1)"; then
      problems+="claude plugin validate $target:"$'\n'"$out"$'\n'
    fi
  done
fi

if [ -n "$problems" ]; then
  {
    [ "${#notes[@]}" -eq 0 ] || printf '%s\n' "${notes[@]}"
    printf 'The plugin checks failed after editing %s:\n%s' "$rel" "$problems"
  } >&2
  exit 2
fi
if [ "${#notes[@]}" -gt 0 ]; then
  jq -n --arg c "$(printf '%s\n' "${notes[@]}")" \
    '{hookSpecificOutput: {hookEventName: "PostToolUse", additionalContext: $c}}'
fi
exit 0
