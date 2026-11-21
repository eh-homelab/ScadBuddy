# Claude plugin and MCP

## What it does

Claude Code or Claude Desktop can use ScadBuddy's tools from outside the app: the
`/mcp` endpoint serves the same tools as the assistant, and the `scadbuddy` Claude plugin
adds skills (authoring, customize, print) and subagents on top. Outward calls from an MCP
client become approvals a person confirms in ScadBuddy. User guide:
[Connecting Claude Code or Claude Desktop](../user-guide.md#connecting-claude-code-or-claude-desktop).

## How it works

`/mcp` is a Streamable HTTP MCP server on the agent sidecar (the ingress routes it to
port 8081). It authenticates by an MCP token or OIDC. The plugin's `.mcp.json` points at
`<scadbuddy_url>/mcp` with a bearer token from the plugin's user config. The same plugin's
skills are copied into the agent image for the in-app assistant. See
[docs/ai/claude-plugin.md](../ai/claude-plugin.md) and
[docs/ai/mcp-resources.md](../ai/mcp-resources.md).

## Code

- Endpoint: `agent/src/mcp/http.ts:446`, tools `agent/src/tools/projections.ts:195-210`,
  auth `agent/src/auth/authenticate.ts:22`, tokens `agent/src/routes/mcpTokens.ts:116`,
  approvals `agent/src/approvals/service.ts:79-96`.
- Plugin: `plugins/scadbuddy/` (`.claude-plugin/plugin.json`, `skills/`, `agents/`,
  `.mcp.json`); [`plugins/CLAUDE.md`](../../plugins/CLAUDE.md). The agent image copies it at
  `Dockerfile:205-221`.
- UI: Settings → MCP (`frontend/src/pages/settings/sections/`, the `mcp-*` sections).

## Data

- Tables: `ai_mcp_tokens`, `ai_mcp_sessions`, `ai_approvals`.

## Tests

- Agent: `agent/test/` (`mcp*`, `approvals*`).
- e2e: `frontend/e2e/mcp-auth.spec.ts`, `mcp-tokens.spec.ts`.
- CI: `lint-plugin.sh` and `check-plugin-version.sh` in the `lint` job.

## Limits and known issues

- Replacing the post-deploy MCP test with a cheap smoke test:
  [#4017](https://github.com/eh-homelab/ScadBuddy/issues/4017).
