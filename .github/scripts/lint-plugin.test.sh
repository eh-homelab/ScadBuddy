#!/usr/bin/env bash
# Tests for lint-plugin.sh — the CI check on ScadBuddy's Claude plugin (#299).
# Each case builds a small repo tree in a temp directory, breaks one thing, and
# asserts the lint both fails and names the problem. The good tree must pass,
# or every failure below could be the lint failing on everything.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/lint-plugin.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

pass=0
fail=0

check() {
  local name="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    printf 'ok   %s\n' "$name"
    pass=$((pass + 1))
  else
    printf 'FAIL %s\n       expected: %s\n       actual:   %s\n' "$name" "$expected" "$actual"
    fail=$((fail + 1))
  fi
}

# good <dir>: a minimal tree that passes.
good() {
  local r="$1"
  rm -rf "$r"
  mkdir -p "$r/.claude-plugin" "$r/plugins/demo/.claude-plugin" \
    "$r/plugins/demo/skills/one" "$r/plugins/demo/agents"
  cat >"$r/.claude-plugin/marketplace.json" <<'EOF'
{"name": "demo", "owner": {"name": "someone"},
 "plugins": [{"name": "demo", "source": "./plugins/demo"}]}
EOF
  cat >"$r/plugins/demo/.claude-plugin/plugin.json" <<'EOF'
{"name": "demo", "version": "0.1.0", "description": "A demo plugin"}
EOF
  printf '{"mcpServers": {}}\n' >"$r/plugins/demo/.mcp.json"
  cat >"$r/plugins/demo/skills/one/SKILL.md" <<'EOF'
---
name: one
description: Does one thing.
---

Per `docs/spec.md` §3, do the thing.
EOF
  cat >"$r/plugins/demo/agents/helper.md" <<'EOF'
---
name: helper
description: Helps.
---

Help.
EOF
}

# run <dir>: "<exit>:<the lint's problem lines, '|'-joined>" so a test asserts
# on both the verdict and the reason at once.
run() {
  local out status
  set +e
  out="$("$script" "$1" 2>&1)"
  status=$?
  set -e
  printf '%s:%s' "$status" "$(printf '%s\n' "$out" | grep -E '^[^ ]+: ' | grep -v '^usage:' | paste -sd'|' -)"
}

r="$tmp/repo"

good "$r"
check 'a complete plugin passes' '0:' "$(run "$r")"

good "$r"
rm "$r/.claude-plugin/marketplace.json"
check 'a missing marketplace.json fails' \
  '1:.claude-plugin/marketplace.json: missing' "$(run "$r")"

good "$r"
printf '{"name": "demo",\n' >"$r/.claude-plugin/marketplace.json"
check 'a marketplace.json that is not JSON fails' \
  '1:.claude-plugin/marketplace.json: not a JSON object' "$(run "$r")"

good "$r"
printf '{"name": "demo", "plugins": [{"name": "demo", "source": "./plugins/demo"}]}\n' \
  >"$r/.claude-plugin/marketplace.json"
check 'a marketplace without an owner fails' \
  "1:.claude-plugin/marketplace.json: no 'owner.name'" "$(run "$r")"

good "$r"
printf '{"name": "demo", "owner": {"name": "x"}, "plugins": []}\n' \
  >"$r/.claude-plugin/marketplace.json"
check 'a marketplace with no plugins fails' \
  "1:.claude-plugin/marketplace.json: 'plugins' must be a non-empty array" "$(run "$r")"

good "$r"
printf '{"name": "demo", "owner": {"name": "x"}, "plugins": [{"name": "demo"}]}\n' \
  >"$r/.claude-plugin/marketplace.json"
check 'a plugin entry without a source fails' \
  "1:.claude-plugin/marketplace.json: plugins[0] has no 'source'" "$(run "$r")"

good "$r"
printf '{"name": "demo", "owner": {"name": "x"}, "plugins": [{"name": "demo", "source": "./plugins/gone"}]}\n' \
  >"$r/.claude-plugin/marketplace.json"
check 'a relative source that is not a plugin fails' \
  "1:.claude-plugin/marketplace.json: plugins[0].source './plugins/gone' has no .claude-plugin/plugin.json" \
  "$(run "$r")"

good "$r"
printf '{"name": "demo", "owner": {"name": "x"}, "plugins": [{"name": "demo", "source": {"source": "github", "repo": "o/r"}}]}\n' \
  >"$r/.claude-plugin/marketplace.json"
check 'an object source is accepted without a local directory' '0:' "$(run "$r")"

good "$r"
printf '{"version": "0.1.0", "description": "d"}\n' >"$r/plugins/demo/.claude-plugin/plugin.json"
check 'a plugin.json without a name fails' \
  "1:plugins/demo/.claude-plugin/plugin.json: 'name' must be a kebab-case string" "$(run "$r")"

good "$r"
printf '{"name": "Demo Plugin", "version": "0.1.0", "description": "d"}\n' \
  >"$r/plugins/demo/.claude-plugin/plugin.json"
check 'a plugin name that is not kebab-case fails' \
  "1:plugins/demo/.claude-plugin/plugin.json: 'name' must be a kebab-case string" "$(run "$r")"

good "$r"
printf '{"name": "demo", "description": "d"}\n' >"$r/plugins/demo/.claude-plugin/plugin.json"
check 'a plugin.json without a version fails' \
  "1:plugins/demo/.claude-plugin/plugin.json: no 'version'" "$(run "$r")"

good "$r"
printf '{"mcpServers": \n' >"$r/plugins/demo/.mcp.json"
check 'an .mcp.json that is not JSON fails' \
  '1:plugins/demo/.mcp.json: not a JSON object' "$(run "$r")"

good "$r"
printf 'Per docs/spec.md §3, do the thing.\n' >"$r/plugins/demo/skills/one/SKILL.md"
check 'a SKILL.md with no frontmatter fails' \
  "1:plugins/demo/skills/one/SKILL.md: no YAML frontmatter (the file must open with a '---' block)" \
  "$(run "$r")"

good "$r"
printf -- '---\nname: one\ndescription: d\n\nPer docs/spec.md §3.\n' >"$r/plugins/demo/skills/one/SKILL.md"
check 'an unclosed frontmatter block fails' \
  "1:plugins/demo/skills/one/SKILL.md: no YAML frontmatter (the file must open with a '---' block)" \
  "$(run "$r")"

good "$r"
printf -- '---\nname: one\n---\n\nPer docs/spec.md §3.\n' >"$r/plugins/demo/skills/one/SKILL.md"
check 'a SKILL.md without a description fails' \
  "1:plugins/demo/skills/one/SKILL.md: frontmatter has no 'description'" "$(run "$r")"

good "$r"
printf -- '---\nname: ""\ndescription: d\n---\n\nPer docs/spec.md §3.\n' >"$r/plugins/demo/skills/one/SKILL.md"
check 'an empty quoted name fails' \
  "1:plugins/demo/skills/one/SKILL.md: frontmatter has no 'name'" "$(run "$r")"

good "$r"
printf -- '---\nname: one\ndescription: d\n---\n\nJust do the thing.\n' >"$r/plugins/demo/skills/one/SKILL.md"
check 'a SKILL.md that cites nothing fails' \
  '1:plugins/demo/skills/one/SKILL.md: cites no source (add a URL, or a repository path with its § or section)' \
  "$(run "$r")"

good "$r"
printf -- '---\nname: one\ndescription: d\n---\n\nSee docs/spec.md for more.\n' >"$r/plugins/demo/skills/one/SKILL.md"
check 'a bare path without a section is not a citation' \
  '1:plugins/demo/skills/one/SKILL.md: cites no source (add a URL, or a repository path with its § or section)' \
  "$(run "$r")"

good "$r"
printf -- '---\nname: one\ndescription: d\n---\n\nSee CLAUDE.md, section "Facts".\n' >"$r/plugins/demo/skills/one/SKILL.md"
check 'a path with a named section is a citation' '0:' "$(run "$r")"

good "$r"
printf -- '---\nname: one\ndescription: d\n---\n\nSee https://example.org/doc.\n' >"$r/plugins/demo/skills/one/SKILL.md"
check 'a URL is a citation' '0:' "$(run "$r")"

good "$r"
rm "$r/plugins/demo/skills/one/SKILL.md"
check 'a skill directory without SKILL.md fails' \
  '1:plugins/demo/skills/one: skill directory has no SKILL.md' "$(run "$r")"

good "$r"
printf -- '---\ndescription: Helps.\n---\n' >"$r/plugins/demo/agents/helper.md"
check 'an agent without a name fails' \
  "1:plugins/demo/agents/helper.md: frontmatter has no 'name'" "$(run "$r")"

good "$r"
printf -- '---\nname: one\n---\n\nNothing cited.\n' >"$r/plugins/demo/skills/one/SKILL.md"
check 'every problem is reported, not only the first' \
  "1:plugins/demo/skills/one/SKILL.md: frontmatter has no 'description'|plugins/demo/skills/one/SKILL.md: cites no source (add a URL, or a repository path with its § or section)" \
  "$(run "$r")"

# own <dir>: the good tree plus a plugins/scadbuddy and its agent copy (#896).
own() {
  local r="$1"
  good "$r"
  cp -r "$r/plugins/demo" "$r/plugins/scadbuddy"
  printf '{"name": "scadbuddy", "version": "0.2.0", "description": "d"}\n' \
    >"$r/plugins/scadbuddy/.claude-plugin/plugin.json"
  mkdir -p "$r/agent/plugins/scadbuddy/.claude-plugin"
  printf '{"name": "scadbuddy", "version": "0.2.0", "description": "d"}\n' \
    >"$r/agent/plugins/scadbuddy/.claude-plugin/plugin.json"
  ln -s ../../../plugins/scadbuddy/skills "$r/agent/plugins/scadbuddy/skills"
  ln -s ../../../plugins/scadbuddy/agents "$r/agent/plugins/scadbuddy/agents"
}

own "$r"
check 'an agent copy in step passes' '0:' "$(run "$r")"

own "$r"
printf '{"name": "scadbuddy", "version": "0.1.0", "description": "d"}\n' \
  >"$r/agent/plugins/scadbuddy/.claude-plugin/plugin.json"
check 'an agent copy at another version fails' \
  "1:agent/plugins/scadbuddy/.claude-plugin/plugin.json: 'version' must match plugins/scadbuddy/.claude-plugin/plugin.json" \
  "$(run "$r")"

own "$r"
printf '{"name": "scadbuddy", "version": "0.2.0", "description": "d", "userConfig": {}}\n' \
  >"$r/agent/plugins/scadbuddy/.claude-plugin/plugin.json"
check 'an agent copy with userConfig fails' \
  "1:agent/plugins/scadbuddy/.claude-plugin/plugin.json: must not have 'userConfig' (the harness refuses it)" \
  "$(run "$r")"

own "$r"
printf '{}\n' >"$r/agent/plugins/scadbuddy/.mcp.json"
check 'an agent copy with an .mcp.json fails' \
  '1:agent/plugins/scadbuddy/.mcp.json: must not exist (the harness serves the tools in-process)' "$(run "$r")"

own "$r"
rm "$r/agent/plugins/scadbuddy/skills"
cp -r "$r/plugins/scadbuddy/skills" "$r/agent/plugins/scadbuddy/skills"
check 'an agent copy whose skills are copied, not linked, fails' \
  '1:agent/plugins/scadbuddy/skills: must be a symlink to ../../../plugins/scadbuddy/skills' "$(run "$r")"

check 'a root that is not a directory is a usage error' '2:' "$(run "$tmp/nope")"

check 'the repository plugin passes' '0:' "$(run "$here/../..")"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
