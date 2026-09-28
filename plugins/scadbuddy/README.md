# ScadBuddy Claude plugin

ScadBuddy's agent capabilities, packaged as a
[Claude plugin](https://code.claude.com/docs/en/plugins/manifest-reference): the
same skills and subagents the in-app assistant uses, installable in your own
Claude Code (issue #299;
`docs/superpowers/specs/2026-09-27-ai-integration-design.md` §10).

| Component | Name in Claude Code | What it's for |
|---|---|---|
| `skills/authoring/` | `/scadbuddy:authoring` | Writing `.scad` templates: customizer annotations, `// color` and `// font`, per-colour solids, the verified OpenSCAD facts, `verify.sh` |
| `skills/customize/` | `/scadbuddy:customize` | Driving the customizer: schema, presets, render, preview, plate fit, outputs |
| `skills/print/` | `/scadbuddy:print` | The print flow through Bambuddy: spools, nozzle, quality and plate, then slice-then-queue; approvals, progress |
| `agents/model-author.md` | `scadbuddy:model-author` | Subagent for the edit → render → inspect loop (#252) |
| `agents/print-analyst.md` | `scadbuddy:print-analyst` | Subagent that runs a print-analyzer session (#284) |
| `.mcp.json` | server `plugin:scadbuddy:scadbuddy` | ScadBuddy's `/mcp` endpoint, for installs outside ScadBuddy |

Plugin components are namespaced under the plugin name, so a skill is
`/scadbuddy:<skill>` and an agent is `scadbuddy:<agent>`
([Add components to a plugin](https://code.claude.com/docs/en/plugins/components)).

Every skill cites its sources: a repository path with its section, or a URL
(AI spec §2, D10). `.github/scripts/lint-plugin.sh` enforces this in CI.

## Install in Claude Code

The repository root holds a marketplace file, `.claude-plugin/marketplace.json`
([Marketplace reference](https://code.claude.com/docs/en/plugins/marketplace-reference)).
In Claude Code:

```text
/plugin marketplace add eh-homelab/ScadBuddy
/plugin install scadbuddy@scadbuddy
```

The first `scadbuddy` is the plugin, and the one after `@` is the marketplace.
When the plugin is enabled, Claude Code asks for the two values below.

## Connecting to your ScadBuddy (`.mcp.json` placeholders)

`.mcp.json` declares one Streamable HTTP server, `scadbuddy`. Its URL and bearer
token are placeholders, filled in from the plugin's
[`userConfig`](https://code.claude.com/docs/en/plugins/manifest-reference) in
`.claude-plugin/plugin.json`:

| Placeholder | `userConfig` key | What to enter |
|---|---|---|
| `${user_config.scadbuddy_url}` in `url` | `scadbuddy_url` (required) | Your ScadBuddy's HTTPS base URL, with **no trailing slash**, e.g. `https://scadbuddy.example.org`. The server URL becomes `<that>/mcp`. |
| `${user_config.scadbuddy_token}` in the `Authorization: Bearer …` header | `scadbuddy_token` (sensitive) | A bearer token minted in ScadBuddy **Settings**. Claude Code keeps it in secure storage, not in `settings.json`. |

- Use **HTTPS**. `/mcp` refuses plain HTTP in every auth mode, except on
  loopback (AI spec §8.4).
- In the default `bearer` auth mode, a request without a valid token gets a 401
  (AI spec §8.3). Leave the token empty only if the operator has set the mode to
  `disabled`.
- Sending, printing, deleting and settings writes always need a human approval
  in the ScadBuddy UI, whatever the token allows (AI spec §8.2).
- In Claude Code, the tools appear as `mcp__plugin_scadbuddy_scadbuddy__<tool>`
  ([MCP servers in plugins](https://code.claude.com/docs/en/plugins/components)).

The `/mcp` endpoint and its tools are built in issues #251 and #261. Until they
ship, the skills and agents load, but the server has nothing to connect to.

## Inside ScadBuddy

ScadBuddy's agent service loads this directory by path with the Claude Agent SDK
(`plugins: [{ type: "local", path }]`), and serves its tools in-process as the
`scadbuddy` server, so they are named `mcp__scadbuddy__<tool>` there
([Agent SDK plugins](https://code.claude.com/docs/en/agent-sdk/plugins);
AI spec §3.1 and §5.1). `.mcp.json` is only for external installs (issue #299).
Loading the plugin in the harness is issue #261 and #255.

The subagents' `tools` allow both server names, `mcp__scadbuddy` and
`mcp__plugin_scadbuddy_scadbuddy`, so the same file works in both places and
nothing else is reachable
([Subagents](https://code.claude.com/docs/en/sub-agents)).

## Versioning

The plugin version follows the app release (issue #299). `version` in
`.claude-plugin/plugin.json` matches `backend/pyproject.toml` and
`frontend/package.json`.

## Checking it

```bash
.github/scripts/lint-plugin.sh          # JSON, frontmatter, citations (CI: lint job)
.github/scripts/lint-plugin.test.sh     # the lint's own tests
claude plugin validate plugins/scadbuddy
claude plugin validate .                # the marketplace file
```

`claude plugin validate` is the authoritative manifest check
([Validate the manifest](https://code.claude.com/docs/en/plugins/manifest-reference)).
