# The ScadBuddy Claude plugin

ScadBuddy's skills and subagents, packaged as a Claude plugin that you can install in
your own Claude Code (issue #299, merged in PR #336; spec §10 of
[`docs/superpowers/specs/2026-09-27-ai-integration-design.md`](../superpowers/specs/2026-09-27-ai-integration-design.md)).
The plugin's own README, [`plugins/scadbuddy/README.md`](../../plugins/scadbuddy/README.md),
is the primary reference. This page is the user-facing summary, and says what works
today.

> **Status.** The skills and subagents load. The MCP server the plugin connects to,
> `<your ScadBuddy>/mcp`, is **not on `main`** (open PR #368). Until it ships, the
> plugin's tools have nothing to connect to. The plugin README says so too
> ("The `/mcp` endpoint and its tools are built in issues #251 and #261").

## What is in it

| Component | Name in Claude Code | Purpose |
|---|---|---|
| [`skills/authoring/SKILL.md`](../../plugins/scadbuddy/skills/authoring/SKILL.md) | `/scadbuddy:authoring` | Writing ScadBuddy `.scad` templates |
| [`skills/customize/SKILL.md`](../../plugins/scadbuddy/skills/customize/SKILL.md) | `/scadbuddy:customize` | Driving the customizer |
| [`skills/print/SKILL.md`](../../plugins/scadbuddy/skills/print/SKILL.md) | `/scadbuddy:print` | The print flow through Bambuddy |
| [`agents/model-author.md`](../../plugins/scadbuddy/agents/model-author.md) | `scadbuddy:model-author` | The edit, render and inspect loop |
| [`agents/print-analyst.md`](../../plugins/scadbuddy/agents/print-analyst.md) | `scadbuddy:print-analyst` | Print-analyzer sessions (#284). It never sends or prints. |
| [`.mcp.json`](../../plugins/scadbuddy/.mcp.json) | server `plugin:scadbuddy:scadbuddy` | ScadBuddy's `/mcp`, for external installs |

The component table and namespacing are from the plugin README, which cites
[Add components to a plugin](https://code.claude.com/docs/en/plugins/components).

## Install

The repository root holds the marketplace file
[`.claude-plugin/marketplace.json`](../../.claude-plugin/marketplace.json). The
marketplace is named `scadbuddy` and lists one plugin, `scadbuddy`, with source
`./plugins/scadbuddy` (see the
[marketplace reference](https://code.claude.com/docs/en/plugins/marketplace-reference)).
In Claude Code:

```text
/plugin marketplace add eh-homelab/ScadBuddy
/plugin install scadbuddy@scadbuddy
```

The part before `@` is the plugin and the part after is the marketplace (plugin README,
"Install in Claude Code").

## Configure

When the plugin is enabled, Claude Code asks for the two `userConfig` values that
[`.claude-plugin/plugin.json`](../../plugins/scadbuddy/.claude-plugin/plugin.json)
declares:

| Key | Required | Enter |
|---|---|---|
| `scadbuddy_url` | yes | Your ScadBuddy's HTTPS base URL, **with no trailing slash**, for example `https://scadbuddy.example.org`. The server URL is `${user_config.scadbuddy_url}/mcp` ([`.mcp.json`](../../plugins/scadbuddy/.mcp.json)). |
| `scadbuddy_token` | no; `sensitive` | A bearer token minted in ScadBuddy Settings, sent as `Authorization: Bearer …`. Leave it empty only if the operator set the MCP auth mode to `disabled`. |

What the spec says the server will do (spec §8.2–§8.4; this is **not built on `main`**):

- `/mcp` refuses plain HTTP, except on loopback;
- `bearer` is the default auth mode, and a request without a valid token gets `401`;
- outward actions (send, print, delete, settings writes) always need a human approval
  in the ScadBuddy UI, whatever the token allows.

Token minting in Settings is also part of open PR #368.

In Claude Code the tools will appear as `mcp__plugin_scadbuddy_scadbuddy__<tool>`
(plugin README, citing [plugin components](https://code.claude.com/docs/en/plugins/components)).
The subagents' `tools` field lists both `mcp__scadbuddy` and
`mcp__plugin_scadbuddy_scadbuddy`, so the same files work inside ScadBuddy's own harness
and in an external install.

## Inside ScadBuddy (planned)

The spec (§10) has the agent service load this directory by path, through the Agent SDK
`plugins: [{ type: "local", path }]` option
([Agent SDK plugins](https://code.claude.com/docs/en/agent-sdk/plugins)). The harness can
do this: `pluginPaths` in `runHarness()`
([`agent/src/harness/run.ts`](../../agent/src/harness/run.ts)) vets and passes local
plugins, and the vetting test accepts `plugins/scadbuddy` (PR #379, row 8). But
`main.ts` passes no plugin path yet. Vetting is described in
[security.md](security.md#plugin-vetting).

## Citations in skills

Every `SKILL.md` must cite at least one source: a URL, or a repository path with a `§`
or "section" reference on the same line. [`.github/scripts/lint-plugin.sh`](../../.github/scripts/lint-plugin.sh)
enforces this, and the CI `lint` job runs it
([`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)). The rule comes from spec
D10. The same script checks:

- that the marketplace and `plugin.json` are valid JSON with their required fields;
- that each `./` source names a directory holding `.claude-plugin/plugin.json`;
- that each skill and agent has `name`/`description` frontmatter.

Checks to run locally (plugin README, "Checking it"):

```bash
.github/scripts/lint-plugin.sh
.github/scripts/lint-plugin.test.sh
claude plugin validate plugins/scadbuddy   # needs the Claude Code CLI; not in CI
claude plugin validate .
```

## Versioning

The plugin README says `version` in `plugin.json` follows the app release and matches
`backend/pyproject.toml` and `frontend/package.json`. Both are `0.1.0` today.

**Discrepancy found while writing this page:** `plugin.json` declares
`"license": "MIT"`, but the repository switched to Apache-2.0 in #301
([`LICENSE`](../../LICENSE)). Spec §2 "Licensing" also still says "ScadBuddy stays MIT".
Neither is changed here.
