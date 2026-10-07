#!/usr/bin/env bash
# Tests for check-plugin-version.sh: a plugin whose files changed since the
# base must have a new `version`. Each case commits a base tree, changes it,
# commits again, and runs the check against the base.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/check-plugin-version.sh"

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

r="$tmp/repo"
g() { git -C "$r" -c user.name=t -c user.email=t@t "$@"; }
repo() {
  rm -rf "$r"
  mkdir -p "$r/plugins/scadbuddy/.claude-plugin" "$r/plugins/scadbuddy/skills/one" \
    "$r/agent/plugins/scadbuddy/.claude-plugin" "$r/agent-durable/plugin/.claude-plugin"
  for m in plugins/scadbuddy agent/plugins/scadbuddy agent-durable/plugin; do
    printf '{"name": "scadbuddy", "version": "0.1.0"}\n' >"$r/$m/.claude-plugin/plugin.json"
  done
  printf 'one\n' >"$r/plugins/scadbuddy/skills/one/SKILL.md"
  printf 'readme\n' >"$r/README.md"
  g init -q
  g add -A
  g commit -qm base
  g branch -q base
}
commit() { g add -A && g commit -qm change; }
version() {
  sed -i -E "s/\"0\\.1\\.0\"/\"$1\"/" "$r/plugins/scadbuddy/.claude-plugin/plugin.json"
}
# run: "<exit>:<output lines, '|'-joined>"
run() {
  local out status
  set +e
  out="$("$script" base "$r" 2>&1)"
  status=$?
  set -e
  printf '%s:%s' "$status" "$(printf '%s\n' "$out" | paste -sd'|' -)"
}

repo
echo two >>"$r/README.md" && commit
check 'a change outside the plugins passes' '0:plugin versions: ok' "$(run)"

repo
echo two >>"$r/plugins/scadbuddy/skills/one/SKILL.md" && commit
check 'a skill change without a bump fails' \
  "1:plugins/scadbuddy/.claude-plugin/plugin.json: plugin files changed but 'version' is still 0.1.0; bump it" "$(run)"

repo
echo two >>"$r/plugins/scadbuddy/skills/one/SKILL.md" && version 0.1.1 && commit
check 'a skill change with a bump passes' '0:plugin versions: ok' "$(run)"

repo
printf '{"name": "scadbuddy", "version": "0.1.0", "description": "x"}\n' \
  >"$r/agent/plugins/scadbuddy/.claude-plugin/plugin.json" && commit
check "a change to the agent copy counts as the plugin's" \
  "1:plugins/scadbuddy/.claude-plugin/plugin.json: plugin files changed but 'version' is still 0.1.0; bump it" "$(run)"

repo
printf '{"name": "scadbuddy", "version": "0.1.0", "description": "x"}\n' \
  >"$r/agent-durable/plugin/.claude-plugin/plugin.json" && commit
check "a change to agent-durable's copy counts as the plugin's" \
  "1:plugins/scadbuddy/.claude-plugin/plugin.json: plugin files changed but 'version' is still 0.1.0; bump it" "$(run)"

repo
mkdir -p "$r/plugins/fresh/.claude-plugin"
printf '{"name": "fresh", "version": "0.1.0"}\n' >"$r/plugins/fresh/.claude-plugin/plugin.json" && commit
check 'a plugin new since the base passes' '0:plugin versions: ok' "$(run)"

check 'a base that is not a commit is a usage error' \
  "2:usage: $script <base commit> [repo-root]: nope is not a commit" \
  "$(set +e; out="$("$script" nope "$r" 2>&1)"; printf '%s:%s' "$?" "$out")"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
