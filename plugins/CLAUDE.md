# Claude plugin — notes for agents

These notes cover `plugins/`; paths are repo-relative; the root `CLAUDE.md` has the repo-wide rules.

## Layout
- `plugins/scadbuddy/` — ScadBuddy's Claude plugin (#299): skills (`authoring`,
  `customize`, `print`), subagents, and a `.mcp.json` for external installs; listed by
  the root `.claude-plugin/marketplace.json`. Every skill cites its sources, which
  `.github/scripts/lint-plugin.sh` checks; `claude plugin validate plugins/scadbuddy` is
  the authoritative manifest check. The agent loads `agent/plugins/scadbuddy/` instead
  (#896): its own `plugin.json` (no `userConfig`, no `.mcp.json`) with `skills/` and
  `agents/` symlinked here, so edit the files here; that query gets the `Skill` and
  `Agent` tools (`src/harness/ownPlugin.ts`). Every plugin change needs a new `version`
  (Claude Code keeps installs on the one they have): the `PostToolUse` hook in
  `.claude/settings.json` (`.github/scripts/plugin-edited.sh`) patch-bumps it on a
  branch's first plugin edit, mirrors it into the agent copy and runs the checks.

## See also

- The agent's copy of the plugin: see `agent/CLAUDE.md`.
