#!/usr/bin/env bash
# Tests for plugin-edited.sh, the Claude Code hook that bumps a plugin's version
# on its first change in a branch and then lints it. Each case builds a git repo
# with a plugin and its agent copy, commits it as the base, edits a file, and
# runs the hook on that edit.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
hook="$here/plugin-edited.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
export PLUGIN_EDITED_BASE=base PLUGIN_EDITED_SKIP_VALIDATE=1

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

r="$tmp/repo"
repo() {
  rm -rf "$r"
  mkdir -p "$r/.github/scripts" "$r/.claude-plugin" "$r/plugins/scadbuddy/.claude-plugin" \
    "$r/plugins/scadbuddy/skills/one" "$r/plugins/scadbuddy/agents" "$r/agent/plugins/scadbuddy/.claude-plugin"
  cp "$here/lint-plugin.sh" "$r/.github/scripts/"
  printf '{"name": "m", "owner": {"name": "o"}, "plugins": [{"name": "scadbuddy", "source": "./plugins/scadbuddy"}]}\n' \
    >"$r/.claude-plugin/marketplace.json"
  for m in plugins/scadbuddy agent/plugins/scadbuddy; do
    printf '{"name": "scadbuddy", "version": "0.1.0", "description": "d"}\n' >"$r/$m/.claude-plugin/plugin.json"
  done
  printf -- '---\nname: one\ndescription: d\n---\n\nSee https://example.org.\n' >"$r/plugins/scadbuddy/skills/one/SKILL.md"
  ln -s ../../../plugins/scadbuddy/skills "$r/agent/plugins/scadbuddy/skills"
  ln -s ../../../plugins/scadbuddy/agents "$r/agent/plugins/scadbuddy/agents"
  git -C "$r" init -q
  git -C "$r" -c user.name=t -c user.email=t@t add -A
  git -C "$r" -c user.name=t -c user.email=t@t commit -qm base
  git -C "$r" branch -q base
}

# edited <path>: run the hook as Claude Code does after an Edit of <path>.
# Prints "<exit>|<versions of both manifests>".
edited() {
  local status
  set +e
  jq -n --arg f "$r/$1" '{tool_name: "Edit", tool_input: {file_path: $f}}' | "$hook" >"$tmp/out" 2>"$tmp/err"
  status=$?
  set -e
  printf '%s|%s %s' "$status" \
    "$(jq -r .version "$r/plugins/scadbuddy/.claude-plugin/plugin.json")" \
    "$(jq -r .version "$r/agent/plugins/scadbuddy/.claude-plugin/plugin.json")"
}

repo
echo more >>"$r/plugins/scadbuddy/skills/one/SKILL.md"
check 'the first change bumps both manifests' '0|0.1.1 0.1.1' "$(edited plugins/scadbuddy/skills/one/SKILL.md)"
check 'and says so to Claude' 'yes' "$(grep -q 'bumped plugins/scadbuddy' "$tmp/out" && echo yes)"
check 'a second change does not bump again' '0|0.1.1 0.1.1' "$(edited plugins/scadbuddy/skills/one/SKILL.md)"

repo
echo more >>"$r/plugins/scadbuddy/skills/one/SKILL.md"
check 'an edit through the agent copy link bumps too' '0|0.1.1 0.1.1' \
  "$(edited agent/plugins/scadbuddy/skills/one/SKILL.md)"

repo
echo more >>"$r/plugins/scadbuddy/skills/one/SKILL.md"
ln -s "$r" "$tmp/linked"
check 'an edit through a symlinked ancestor of the repo bumps too' '0|0.1.1 0.1.1' \
  "$(edited ../linked/plugins/scadbuddy/skills/one/SKILL.md)"
rm "$tmp/linked"

repo
jq '.version = "0.3.0"' "$r/plugins/scadbuddy/.claude-plugin/plugin.json" >"$tmp/j" && mv "$tmp/j" "$r/plugins/scadbuddy/.claude-plugin/plugin.json"
check 'a version set by hand is kept and mirrored' '0|0.3.0 0.3.0' "$(edited plugins/scadbuddy/.claude-plugin/plugin.json)"

repo
check 'an edit outside the plugins does nothing' '0|0.1.0 0.1.0' "$(edited README.md)"
check 'and prints nothing' '' "$(cat "$tmp/out" "$tmp/err")"

repo
check 'a marketplace edit lints without bumping' '0|0.1.0 0.1.0' "$(edited .claude-plugin/marketplace.json)"

repo
printf -- '---\nname: one\ndescription: d\n---\n\nNothing cited.\n' >"$r/plugins/scadbuddy/skills/one/SKILL.md"
check 'a lint failure exits 2' '2|0.1.1 0.1.1' "$(edited plugins/scadbuddy/skills/one/SKILL.md)"
check 'with the problem on stderr' 'yes' "$(grep -q 'cites no source' "$tmp/err" && echo yes)"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
