#!/usr/bin/env bash
#
# Lint ScadBuddy's Claude plugin (#299) and the marketplace that lists it.
# Used by the `lint` job in ci.yml; runs the same locally:
#
#   .github/scripts/lint-plugin.sh [repo-root]
#
# `claude plugin validate` is the authoritative manifest check
# (https://code.claude.com/docs/en/plugins/manifest-reference), but it needs the
# Claude Code CLI. This covers what CI can check without it, plus the repo's own
# rule that every skill cites its sources (AI spec
# docs/superpowers/specs/2026-09-27-ai-integration-design.md §2, D10):
#
#   - .claude-plugin/marketplace.json is JSON with `name`, `owner.name` and a
#     non-empty `plugins` array; every entry has `name` and `source`, and a
#     `./` source names a directory holding .claude-plugin/plugin.json
#   - every plugins/*/.claude-plugin/plugin.json is JSON with a kebab-case
#     `name`, a `version` and a `description`
#   - every plugins/*/.mcp.json present is JSON
#   - every plugins/*/skills/*/SKILL.md and plugins/*/agents/*.md opens with
#     YAML frontmatter carrying a non-empty `name` and `description`
#   - every SKILL.md cites at least one source: a URL, or a repository file
#     path with a `§` or "section" reference on the same line
#   - agent/plugins/scadbuddy, the harness's copy, when present: the same
#     `version`, no `userConfig` or .mcp.json, and `skills` and `agents` as
#     links to plugins/scadbuddy's
#
# Every problem is printed (one per line, prefixed with the file); the exit
# status is 1 if there was any, 2 on a usage error.
set -euo pipefail

if [ "$#" -gt 1 ]; then
  echo "usage: $0 [repo-root]" >&2
  exit 2
fi
root="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
if [ ! -d "$root" ]; then
  echo "usage: $0 [repo-root]: $root is not a directory" >&2
  exit 2
fi

errors=0
err() {
  printf '%s: %s\n' "$1" "$2"
  errors=$((errors + 1))
}

# rel <path>: the path relative to the root, for messages.
rel() { printf '%s' "${1#"$root"/}"; }

# json_ok <file>: true when the file parses as a JSON object.
json_ok() { jq -e 'type == "object"' "$1" >/dev/null 2>&1; }

# json_str <file> <jq path>: true when the path is a non-empty string.
json_str() { jq -e "($2 | type) == \"string\" and ($2 | length) > 0" "$1" >/dev/null 2>&1; }

# frontmatter <file>: print the YAML frontmatter block (without the fences), or
# nothing when the file does not open with one. The opening `---` must be the
# first line, and a closing `---` must follow.
frontmatter() {
  awk 'NR == 1 { if ($0 != "---") exit; next }
       $0 == "---" { closed = 1; exit }
       { buf = buf $0 "\n" }
       END { if (closed) printf "%s", buf }' "$1"
}

# fm_value <frontmatter> <key>: the value of a top-level `key: value` line,
# stripped of surrounding quotes and whitespace.
fm_value() {
  printf '%s' "$1" | awk -v key="$2" '
    index($0, key ":") == 1 {
      v = substr($0, length(key) + 2)
      gsub(/^[ \t]+|[ \t]+$/, "", v)
      if (v ~ /^".*"$/ || v ~ /^'\''.*'\''$/) v = substr(v, 2, length(v) - 2)
      print v
      exit
    }'
}

check_frontmatter() {
  local file="$1" fm key
  fm="$(frontmatter "$file")"
  if [ -z "$fm" ]; then
    err "$(rel "$file")" "no YAML frontmatter (the file must open with a '---' block)"
    return
  fi
  for key in name description; do
    if [ -z "$(fm_value "$fm" "$key")" ]; then
      err "$(rel "$file")" "frontmatter has no '$key'"
    fi
  done
}

# A citation: a URL, or a repository file path followed on the same line by a
# section reference (`§3`, "section").
url_re='https?://[^[:space:]]+'
path_re='[A-Za-z0-9_.-]*[A-Za-z0-9_-]\.(md|py|ts|tsx|js|json|sh|scad|ya?ml|toml)'
check_citation() {
  local file="$1"
  if grep -Eq "$url_re" "$file"; then return; fi
  if grep -E "$path_re" "$file" | grep -Eiq '§|section'; then return; fi
  err "$(rel "$file")" "cites no source (add a URL, or a repository path with its § or section)"
}

# ── marketplace ──────────────────────────────────────────────────────────────
market="$root/.claude-plugin/marketplace.json"
if [ ! -f "$market" ]; then
  err ".claude-plugin/marketplace.json" "missing"
elif ! json_ok "$market"; then
  err "$(rel "$market")" "not a JSON object"
else
  json_str "$market" '.name' || err "$(rel "$market")" "no 'name'"
  json_str "$market" '.owner.name' || err "$(rel "$market")" "no 'owner.name'"
  if ! jq -e '(.plugins | type) == "array" and (.plugins | length) > 0' "$market" >/dev/null; then
    err "$(rel "$market")" "'plugins' must be a non-empty array"
  else
    count="$(jq '.plugins | length' "$market")"
    for ((i = 0; i < count; i++)); do
      json_str "$market" ".plugins[$i].name" || err "$(rel "$market")" "plugins[$i] has no 'name'"
      source="$(jq -r ".plugins[$i].source | if type == \"string\" then . else \"\" end" "$market")"
      if ! jq -e ".plugins[$i] | has(\"source\")" "$market" >/dev/null; then
        err "$(rel "$market")" "plugins[$i] has no 'source'"
      elif [[ "$source" == ./* ]]; then
        if [[ "$source" == *..* ]]; then
          err "$(rel "$market")" "plugins[$i].source '$source' must not contain '..'"
        elif [ ! -f "$root/${source#./}/.claude-plugin/plugin.json" ]; then
          err "$(rel "$market")" "plugins[$i].source '$source' has no .claude-plugin/plugin.json"
        fi
      fi
    done
  fi
fi

# ── plugins ──────────────────────────────────────────────────────────────────
shopt -s nullglob
plugins=("$root"/plugins/*/)
if [ "${#plugins[@]}" -eq 0 ]; then
  err "plugins/" "no plugin directories"
fi
for dir in "${plugins[@]}"; do
  dir="${dir%/}"
  manifest="$dir/.claude-plugin/plugin.json"
  if [ ! -f "$manifest" ]; then
    err "$(rel "$manifest")" "missing"
  elif ! json_ok "$manifest"; then
    err "$(rel "$manifest")" "not a JSON object"
  else
    if ! jq -e '(.name | type) == "string" and (.name | test("^[a-z0-9]+(-[a-z0-9]+)*$"))' \
      "$manifest" >/dev/null; then
      err "$(rel "$manifest")" "'name' must be a kebab-case string"
    fi
    json_str "$manifest" '.version' || err "$(rel "$manifest")" "no 'version'"
    json_str "$manifest" '.description' || err "$(rel "$manifest")" "no 'description'"
  fi

  if [ -f "$dir/.mcp.json" ] && ! json_ok "$dir/.mcp.json"; then
    err "$(rel "$dir/.mcp.json")" "not a JSON object"
  fi

  skills=("$dir"/skills/*/)
  for skill in "${skills[@]}"; do
    file="${skill%/}/SKILL.md"
    if [ ! -f "$file" ]; then
      err "$(rel "${skill%/}")" "skill directory has no SKILL.md"
      continue
    fi
    check_frontmatter "$file"
    check_citation "$file"
  done

  for agent in "$dir"/agents/*.md; do
    check_frontmatter "$agent"
  done
done

# ── the agent's copy (#896) ──────────────────────────────────────────────────
# agent/plugins/scadbuddy is plugins/scadbuddy as the harness loads it: its own
# manifest, at the same version, without userConfig or .mcp.json, and `skills`
# and `agents` as links to the shared directories (agent/src/harness/ownPlugin.ts).
own="$root/agent/plugins/scadbuddy"
if [ -d "$own" ]; then
  own_manifest="$own/.claude-plugin/plugin.json"
  shared_manifest="$root/plugins/scadbuddy/.claude-plugin/plugin.json"
  if ! json_ok "$own_manifest"; then
    err "$(rel "$own_manifest")" "missing or not a JSON object"
  else
    jq -e '.name == "scadbuddy"' "$own_manifest" >/dev/null || err "$(rel "$own_manifest")" "'name' must be 'scadbuddy'"
    jq -e 'has("userConfig") | not' "$own_manifest" >/dev/null ||
      err "$(rel "$own_manifest")" "must not have 'userConfig' (the harness refuses it)"
    if json_ok "$shared_manifest" &&
      [ "$(jq -r .version "$own_manifest")" != "$(jq -r .version "$shared_manifest")" ]; then
      err "$(rel "$own_manifest")" "'version' must match $(rel "$shared_manifest")"
    fi
  fi
  [ ! -e "$own/.mcp.json" ] || err "$(rel "$own/.mcp.json")" "must not exist (the harness serves the tools in-process)"
  for part in skills agents; do
    if [ ! -L "$own/$part" ] || [ "$(readlink "$own/$part")" != "../../../plugins/scadbuddy/$part" ]; then
      err "$(rel "$own/$part")" "must be a symlink to ../../../plugins/scadbuddy/$part"
    elif [ ! -d "$own/$part" ]; then
      err "$(rel "$own/$part")" "does not resolve to a directory"
    fi
  done
fi

if [ "$errors" -gt 0 ]; then
  printf '\n%d problem(s) in the plugin\n' "$errors"
  exit 1
fi
echo "plugin lint: ok"
