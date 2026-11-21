# The AI assistant

## What it does

A chat panel in ScadBuddy where Claude can read and change models, render, arrange,
print, run orders and flows with ScadBuddy's own tools, drive a paired browser tab, keep
files in a session workspace and use plugins. Outward actions (printing, sending, paying)
wait for a person's approval. Sessions can be forked, handed off, archived and given a
budget. User guide: [Assistant](../user-guide.md#assistant) and its sections
([the panel](../user-guide.md#the-assistant-panel),
[what needs your approval](../user-guide.md#what-needs-your-approval),
[pairing a browser tab](../user-guide.md#pairing-a-browser-tab),
[tool access](../user-guide.md#tool-access), [plugins](../user-guide.md#plugins)).

## How it works

The panel's WebSocket reaches the agent sidecar, which runs each turn as an
`AgentSession` workflow on the agent worker, where Claude Code runs through the Claude
Agent SDK with a loopback credential gateway. Tool calls become `ToolCall` workflows
whose activities call the API. The full map, with a sequence diagram, is in
[agent.md](../architecture/agent.md); the operator docs are in [`docs/ai/`](../ai/README.md).

## Code

- UI: `frontend/src/components/assistant/AssistantPanel.tsx:22`, `AssistantChat.tsx:154`,
  `SessionSwitcher.tsx:69`; chat transport `frontend/src/agent/chat/socketTransport.ts:37`;
  bridge `frontend/src/agent/link.ts:32`.
- Agent: `agent/src/routes/chat.ts:76`, `agent/src/sessions/durable.ts:403-412`,
  `agent/src/temporal/session/workflow.ts:214`, `agent/src/harness/run.ts:636`,
  `agent/src/harness/permissions.ts:145-161`, `agent/src/tools/index.ts:46-84`.

## Data

- `ai_*` tables (sessions, entries, events, pending input, approvals, credentials,
  settings, plugins, workspace files); session workspace bytes as sealed blobs
  (`workspace-<sha256>`).

## Tests

- Agent: `agent/test/` (vitest, Postgres and Temporal suites), `agent/evals/`
  ([evals.md](../ai/evals.md)).
- e2e: `frontend/e2e/assistant.spec.ts`, `assistant-skills.spec.ts`, `agent-bridge.spec.ts`,
  `agent-link.spec.ts`, `session-files.spec.ts`, `voice.spec.ts`, `ai-audit.spec.ts`,
  `tools-and-plugins.spec.ts`, `plugin-packages.spec.ts`, `real-agent.spec.ts`.

## Limits and known issues

- The agent still holds a database secret
  ([#5299](https://github.com/eh-homelab/ScadBuddy/issues/5299)); its 8081 routes are not
  authenticated ([#5297](https://github.com/eh-homelab/ScadBuddy/issues/5297)).
- Sandboxed `Bash` and `PowerShell` for every session
  ([#5864](https://github.com/eh-homelab/ScadBuddy/issues/5864)); each tool call spends about
  3 s in Temporal overhead ([#5865](https://github.com/eh-homelab/ScadBuddy/issues/5865));
  `BridgeCall` workflow tasks time out
  ([#5939](https://github.com/eh-homelab/ScadBuddy/issues/5939)).
