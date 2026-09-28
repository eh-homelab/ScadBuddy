# The ScadBuddy Claude plugin

ScadBuddy's skills and subagents, packaged as a Claude plugin that you can install in
your own Claude Code (issue #299, merged in PR #336; spec §10 of
[`docs/superpowers/specs/2026-09-27-ai-integration-design.md`](../superpowers/specs/2026-09-27-ai-integration-design.md)).
The plugin's own README, [`plugins/scadbuddy/README.md`](../../plugins/scadbuddy/README.md),
is the primary reference. This page is the user-facing summary, and says what works
today.

> **Status.** The skills and subagents load in your Claude Code. ScadBuddy's own
> harness does not load them ([Inside ScadBuddy](#inside-scadbuddy-not-loaded)). The MCP server the plugin connects to,
> `<your ScadBuddy>/mcp`, is on `main` (#368), but nothing deploys the agent sidecar or
> routes `/mcp` to it yet, and tokens cannot be minted in Settings yet (#251).

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

What the server does (spec §8.2–§8.4; [`agent/src/mcp/http.ts`](../../agent/src/mcp/http.ts)):

- `/mcp` refuses plain HTTP, except on loopback;
- `bearer` is the default auth mode, and a request without a valid token gets `401`.
  The mode is an `ai_settings` key ([operating.md](operating.md#10-mcp-auth-mode));
- outward actions (send, print, delete, settings writes) always need a human approval
  in the ScadBuddy UI, whatever the token allows. The tool returns a
  `pending_action_id`; once the user approves it, `confirm_action` runs it, once
  ([security.md](security.md#mcp-prepareconfirm-on-the-approval-store)).

Token minting in Settings is not built yet (#251).

In Claude Code the tools will appear as `mcp__plugin_scadbuddy_scadbuddy__<tool>`
(plugin README, citing [plugin components](https://code.claude.com/docs/en/plugins/components)).
The subagents' `tools` field lists both `mcp__scadbuddy` and
`mcp__plugin_scadbuddy_scadbuddy`, so the same files work inside ScadBuddy's own harness
and in an external install.

## Inside ScadBuddy (not loaded)

The spec (§10) has the agent service load this directory by path, through the Agent SDK
`plugins: [{ type: "local", path }]` option
([Agent SDK plugins](https://code.claude.com/docs/en/agent-sdk/plugins)). The harness can
do this: `pluginPaths` in `runHarness()`
([`agent/src/harness/run.ts`](../../agent/src/harness/run.ts)) vets and passes local
plugins. But `main.ts` does not pass this one, on purpose (it does load the headless
browser's vendored plugin when that is enabled, see
[headless-browser.md](headless-browser.md), and any approved plugin packages): every query runs with
`tools: []` ([`agent/src/harness/options.ts`](../../agent/src/harness/options.ts)), so
there is no `Skill` or `Agent` tool, and the plugin's skills and subagents would be
listed but never usable. The harness's own tools reach the model directly as
`mcp__scadbuddy__<tool>` ([`agent/src/tools/harness.ts`](../../agent/src/tools/harness.ts));
[`agent/test/harnessWiring.test.ts`](../../agent/test/harnessWiring.test.ts) asserts that,
with no other plugin enabled, they are the only tools offered.

Vetting is described in [security.md](security.md#plugin-vetting).

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

The plugin's `license` is `Apache-2.0`, the repository's license since #301
([`LICENSE`](../../LICENSE)).
