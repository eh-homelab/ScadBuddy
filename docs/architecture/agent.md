# The agent

The assistant's service: a TypeScript package (`agent/`) that runs Claude Code through
the Claude Agent SDK, gives it ScadBuddy's tools, gates outward actions behind a person's
approval, and serves `/api/v1/ai/*` and `/mcp`. Commands, tests and folder layout are in
[`agent/CLAUDE.md`](../../agent/CLAUDE.md). The operator and security docs are in
[`docs/ai/`](../ai/README.md):
[operating](../ai/operating.md), [security](../ai/security.md),
[sessions](../ai/agent-sessions.md), [browser bridge](../ai/browser-bridge.md),
[MCP resources](../ai/mcp-resources.md), [Claude plugin](../ai/claude-plugin.md),
[evals](../ai/evals.md). This page maps the code and links to them.

Paths below are under `agent/src/` unless they say otherwise. Line numbers are for `main`
at `67d7ff6` (2026-11-21).

## Processes and roles

One image (`Dockerfile:237`, stage `agent`) runs `node dist/main.js` on port 8081
(`Dockerfile:288-293`). `SCADBUDDY_AGENT_ROLE` (`config.ts:106-107`) picks what it runs
(`runtime/role.ts:27-35`):

| Role | Where | Serves |
|---|---|---|
| `api` | the sidecar in the API pod | HTTP and WebSocket on 8081 (`/api/v1/ai/*`, `/mcp`); its own host queue `agent-tools.<identity>` for browser-tab calls and pinned turn tools (`main.ts:424`, `:1234-1266`) |
| `worker` | the `scadbuddy-agent-worker` Deployment | only `/healthz` and `/readyz` over HTTP (`runtime/health.ts:41`, `:55`); the shared Temporal queues `agent-tools` and `agent-sessions` and its host queues (`main.ts:1154-1266`); it runs the turns |
| `all` (default) | development | both |

A second image, `sandbox` (`Dockerfile:311`), runs one pod per session for sandboxed
`Bash` and `PowerShell` (role `exec`) and the pods' CONNECT proxy (role `egress`)
(`sandbox/main.ts:13-19`). The worker creates those pods through the Kubernetes API
(`sandbox/pods.ts:192`) when all five `SCADBUDDY_SANDBOX_*` settings are present
(`config.ts:258-289`).

Inside the process: a loopback credential gateway (`gateway/server.ts:272-295`), a
loopback plugin forwarder (`plugins/forwarder.ts:404-432`), and one Claude Code child
process per query, spawned by the SDK (`harness/run.ts:636`).

The variables each role reads are the list `ENV_VARS` (`config.ts:113-138`); see
[operating.md §1](../ai/operating.md) for what they mean.

## Trust zone

The agent has no SQL client and no KEK
([#5299](https://github.com/eh-homelab/ScadBuddy/issues/5299) step 12): nothing in
`agent/src` loads a Postgres driver (`agent/test/retiredTables.test.ts` fails on one), it
ignores `SCADBUDDY_DATABASE_URL`, `SCADBUDDY_SECRET_KEY_FILE` and
`SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` with one warning (`config.ts` `IGNORED_VARS`), and every
store is the API's CRUD routes. The API seals and opens what is sealed, and releases the two
keys the agent once derived from its own KEK (area `keys`). It opens no LISTEN connection: its
NOTIFY channels arrive over the API's change stream (`events/changeStream.ts`). It still runs
untrusted code (Claude Code's tools, plugin code) with its service credential and its Temporal
client. The API applies the agent's migrations (#5299 D6); the agent waits for no ledger
([data.md](data.md#the-two-migration-ledgers)). The state, step by step, is in
[trust-boundaries.md](../security/trust-boundaries.md#who-talks-to-the-database-today).

## How it reaches the CRUD API

- A backend client generated from `openapi.json` (`api/backend.ts`), with a `Bearer`
  service credential on every call. The credential comes only from exchanging the pod's
  projected token or a bootstrap file at `POST /api/v1/auth/service-token`
  (`api/serviceCredential.ts` `ExchangeCredential`); with no proof the agent refuses to start.
- Its data is `/api/v1/internal/crud/agent/*` (`api/crudRead.ts`). `CrudAreas` reads the
  API's `…/areas` list with back-off until every wanted area is listed, `keys` among them
  (`api/crudAreas.ts`), and the agent is not ready until then, nor until the API has
  released the input-hash key (`api/agentKeys.ts` `InputHashKey`, `api/dataHealth.ts`):
  `/mcp` and the data routes answer 503, and no Temporal worker polls.
- Every backend operation needs a tool or an entry in `tools/coverage.ts`, which a test
  checks (`test/coverage.test.ts`).

## Public surface

### HTTP and WebSocket (role `api`)

Routes are auto-registered from every `routes/*.ts` that exports `route`
(`routes/index.ts:17-31`, `app.ts:242`). Every response carries
`X-ScadBuddy-Service: agent` (`app.ts:164`).

| Path | What | Code |
|---|---|---|
| `GET /healthz` | health; the worker also has `/readyz` | `app.ts:195`, `runtime/health.ts:41` |
| `/mcp` | Streamable HTTP MCP server with `ALL_TOOLS` | `mcp/http.ts:451` |
| `GET /api/v1/ai/chat` (WebSocket) | the assistant panel | `routes/chat.ts:76` |
| `GET /api/v1/ai/bridge` (WebSocket) | the browser bridge | `routes/bridge.ts:34` |
| `/api/v1/ai/sessions…` | list, start, send, interrupt, fork, hand off, events, files | `routes/sessions.ts:240`, `routes/sessionFiles.ts:138` |
| `/api/v1/ai/pending-input…`, `/approvals…` | the gate's open questions and approvals | `routes/pendingInput.ts:561`, `routes/approvals.ts:85-109` |
| `/api/v1/ai/credentials…` | Claude credentials | `routes/credentials.ts:207-412` |
| `/api/v1/ai/settings/*`, `/capabilities/*` | model, limits, workspace, tool access | `routes/model.ts:37`, `routes/capabilities.ts:141-180` |
| `/api/v1/ai/plugins…`, `/plugin-packages…`, `/mcp-tokens…` | plugins, packages, tokens for external MCP clients | `routes/plugins.ts:161`, `routes/pluginPackages.ts:120`, `routes/mcpTokens.ts:116` |
| `/api/v1/ai/plugin-feeds…` | the marketplace feed for outside Claude Code (its `marketplace.json` and zips on `/mcp`'s auth, on the LAN host and the public URL), and Settings' read and writes of it | `routes/pluginFeeds.ts:159-272` |
| `/api/v1/ai/notify/*`, `/push/*`, `/audit` | notifications and the audit log | `routes/notifyChannels.ts:24`, `routes/push.ts:29`, `routes/audit.ts:68` |
| `/api/v1/ai/orders/*` | customer messages through a bound plugin | `routes/orderMessages.ts:85`, `routes/messagingBinding.ts:63` |

The ingress sends these paths to port 8081 ahead of the backend
([README "The agent sidecar"](../../README.md#the-agent-sidecar-ai-261)).

### Tools

`ALL_TOOLS` (`tools/index.ts:46-84`) is about 400 tools in 37 groups: catalogue, customizer,
outputs, arrange, history, files, workspace, libraries, print, prints, farm, filament,
printers, spools, orders (68), flows, sessions, browser and more. They are served on
`/mcp` (`tools/projections.ts:195-210`) and given to each turn as the in-process MCP server
`scadbuddy` (`tools/projections.ts:90-114`). Each tool calls the backend API; the agent
never calls Bambuddy or a printer itself (`tools/makerworld.ts:6-8`). Turns also get:

- `scadbuddy_http` (`http_request`, `harness/httpRequest.ts:79-81`) and
  `scadbuddy_questions` (`ask_user`, `harness/questions.ts:34-37`);
- the `builtin` server: WebFetch, WebSearch, Cron, Monitor, Workflow and, with the
  sandbox, `Bash` and `PowerShell` (`builtins/server.ts:36`, `builtins/sandboxShell.ts:88`);
- `scadbuddy://` resources (`resources/catalog.ts:88-203`; [mcp-resources.md](../ai/mcp-resources.md)).

Which of these a session may use is the access model (`access/model.ts:22-27`), set in
Settings ([user guide "Tool access"](../user-guide.md#tool-access)).

### Temporal

| Workflow | Queue | Code |
|---|---|---|
| `AgentSession`, id `session-<id>` | `agent-sessions` (deployment `scadbuddy-agent`) | `temporal/session/workflow.ts:321`, `temporal/session/names.ts:5-8` |
| `ToolCall` | `agent-tools` | `temporal/toolCall.ts:13` |
| `BridgeCall`, id `bridge-<uuid>` | the caller's `agent-tools.<identity>` | `temporal/bridgeCall.ts:12`, `bridge/relay.ts:109` |
| `AgentOperation` | `agent-tools` | `temporal/workflows.ts:77` |
| `BackgroundTask`, `BackgroundFire` | `agent-tools` | `temporal/backgroundTask.ts:55`, `:112` |

The activities on `agent-tools` are every tool under its own name, plus the gate, turn,
flow-session and operation activities (`main.ts:1161-1190`). `session_turn` runs on the
host queue `agent-sessions.<host>` of the replica that holds the session's warm Claude
Code process. The backend's flow workflows call the agent's flow activities
(`temporal/flowRuns.ts:6-9`; [workflows.md](workflows.md#flows)).

## A chat turn

```mermaid
sequenceDiagram
    participant UI as Browser panel
    participant API as agent (role api)
    participant T as Temporal
    participant W as agent (role worker)
    participant CC as Claude Code
    participant GW as credential gateway
    participant BE as ScadBuddy API
    UI->>API: WS /api/v1/ai/chat message
    API->>T: update-with-start AgentSession session-<id>
    T->>W: acquire_worker (agent-tools)
    T->>W: session_turn (agent-sessions.<host>)
    W->>CC: query() with tools, hooks, canUseTool
    CC->>GW: /v1/messages with a leased token
    GW->>GW: add the real credential, meter spend
    CC->>W: tool call
    W->>T: ToolCall workflow (or open_input if outward)
    T->>W: tool activity
    W->>BE: backend route with service credential
    API-->>W: change stream GET …/crud/agent/changes (SSE)
    API-->>UI: stream
```

1. A message reaches `SessionManager`, which does update-with-start of `AgentSession`
   (`sessions/durable.ts:560-569`).
2. The workflow acquires a worker and runs `session_turn` on its home queue
   (`temporal/session/workflow.ts:596-627`).
3. `runHarness` builds the query options: no settings sources, an explicit environment,
   and the gateway URL and token (`harness/options.ts:55-75`, `harness/run.ts:510-528`).
   Claude Code never holds the credential: the gateway leases a token per run and adds
   the real header itself (`gateway/server.ts:21-50`).
4. A tool call with a `tool_use_id` becomes a `ToolCall` workflow
   (`tools/projections.ts:136-147`, `main.ts:1110-1115`).

## The gate

`decide()` (`harness/permissions.ts:145-161`) gives every tool a tier: `read` and `write`
run, `outward` (and any unknown tool) needs a person's approval. It is installed as both
`canUseTool` and a PreToolUse hook (`harness/permissions.ts:310`, `:366`). In a session an
outward call opens a pending input on the session's workflow; the person's answer is an
`ai_input_responses` row written by the API through area `gate` (`gate/crudStore.ts:5-13`,
`main.ts:286`), and a signal. A flow's grant can approve instead; that check runs on the API
(`gate/store.ts:481-492`). On `/mcp`, outward calls become `ai_approvals` rows that
`confirm_action` runs (`approvals/service.ts:80-97`). What needs approval, from the
person's side: [user guide "What needs your approval"](../user-guide.md#what-needs-your-approval).

## Browser bridge

A ScadBuddy tab paired over `/api/v1/ai/bridge` lets the assistant drive the UI (the
`browser_*` tools). `TabHub` holds the sockets (`bridge/hub.ts:19-22`, `main.ts:486-496`).
A call to a tab connected to another replica is a `BridgeCall` workflow whose `bridge.tab`
activity runs on the owner's host queue (`bridge/relay.ts:30`, `:67-109`). Protocol and
security: [browser-bridge.md](../ai/browser-bridge.md).

## Workspace, plugins, credentials

- **Workspace.** A session's files are sealed blobs in the backend's blob store. The API
  seals, stores and opens them (area `workspace`, `workspace/crudStore.ts`
  `CrudSessionFiles`); the agent sends and gets plaintext only. The working directory is
  under `SCADBUDDY_SESSION_DIR` (`config.ts`).
- **Plugins.** Remote MCP plugins (`ai_plugins`) through the loopback forwarder; packages
  (`ai_plugin_packages`) pinned to a git commit (fetched with a stripped git environment,
  `plugins/packages/git.ts:1-29`), to an archive or npm file's sha256 (`pin_kind` `bytes`,
  loaded from the API's mirror, `plugins/packages/mirror.ts`), or to a hosted plugin's
  version (`pin_kind` `version`, `plugins/packages/hosted.ts`). A hosted plugin is a draft
  of files kept by the API (`ai_hosted_plugins`, published as immutable versions). A
  marketplace feed (`ai_plugin_feeds`, `ai_plugin_feed_packages`) lists approved pins to
  outside Claude Code (`plugins/feed.ts`). [User guide "Plugins"](../user-guide.md#plugins).
- **Claude credentials.** Kinds `anthropic_api_key`, `claude_oauth_token` and `gateway`
  (`credentials.ts`), each sealed with a per-row key under the API's KEK. The API seals and
  opens them, and the agent reads them through area `credentials` (`CrudCredentials`).
  The pool is shared with the fleet's login rotator (#7983): kind `claude_login` (a whole
  Claude Code `claudeAiOauth` login, its account in clear columns, with a machine lease) and
  column `scope` (`agent`, `fleet` or `both`; a `claude_login` is `fleet` only). The API's
  reveal answers the agent only rows of scope `agent` or `both`, in its SQL; the agent's own
  `agentUses` check is a second, defensive one. The agent's caller cannot change a
  `claude_login` or a `fleet` row in any way: a create, save, delete, rename, reset or event
  on one is a 409 from the API, a reorder names the agent's rows only (fleet rows keep their
  priorities), and a call with no id (the deprecated single-credential routes) takes the first
  row the agent uses. Settings shows fleet rows read-only.

## Tests and evals

`agent/test/` (vitest; `*.pg.test.ts` need Postgres, `*temporal*` a Temporal dev server)
and `agent/evals/` (`pnpm evals`). See [`agent/CLAUDE.md`](../../agent/CLAUDE.md) and
[evals.md](../ai/evals.md).

## agent-durable

`agent-durable/` is a Python image that today serves only `/healthz`
(`agent-durable/src/scadbuddy_durable/__main__.py:25-37`) and runs no Temporal worker
(`agent-durable/src/scadbuddy_durable/worker.py:21-24`). It keeps tested Python ports of
the agent's codec, gate store and credential store. See
[`agent-durable/CLAUDE.md`](../../agent-durable/CLAUDE.md) and [processes.md](processes.md#agent-durable).
