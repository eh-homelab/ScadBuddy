# AI integration: harness, tools, sessions, auth

Design for epic #249 (spec issue #250). Written 2026-09-27. It settles what the
implementation stories (#251–#300) build on: the harness, how tools are defined once
and served twice, where state lives, how MCP clients authenticate, and what a human
must approve.

**Citation rule** (epic principle): every external claim below links its source, and
every fact about this repository names the file it was read from. §3 separates facts
**read on 2026-09-27** from facts that are **still to verify**; nothing in "to verify"
may be relied on until a PR moves it up with evidence, as §3 of the main design spec
does for OpenSCAD.

## 1. What this adds

ScadBuddy gains an agent service in the same pod. Through it:

- **External agents** such as Claude Code, Claude Desktop, or any MCP client use every
  ScadBuddy capability over `/mcp`, and can start, watch, steer and hand off sessions
  (#251, #264, #300).
- **The in-app assistant** chats, drives the user's open tab through semantic browser
  tools, authors and iterates on models, and runs print analyzers (#252–#256, #284).
- **Plugins** add capabilities ScadBuddy does not own, such as long-term memory
  (#297). ScadBuddy's own capabilities ship as a Claude plugin that users can also
  install in their own Claude Code (#299).

The Python backend stays the render and API backend and gains **no LLM code**. It
gains an event bus (§7) and a few endpoints the tools need (#252, #253, #284).

## 2. Decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| D1 | **Harness: Claude Agent SDK, TypeScript** | Loads Claude plugins natively; has sessions with resume/fork and a pluggable `SessionStore`, in-process custom tools, permission callbacks and hooks (§3.1) | Vercel AI SDK loop and Mastra: both multi-provider, neither loads Claude plugins |
| D2 | **Claude only, to start** | The harness supports nothing else (§3.1). Credentials: an Anthropic API key, or a gateway base URL plus credential | claude.ai subscription login (not allowed, §3.1); non-Claude models through a gateway (not supported, §3.1) |
| D3 | **One tool registry, two projections** | A tool is defined once and served in-process to the harness and over `/mcp` to external agents, so the surfaces cannot drift | Deriving tools mechanically from `openapi.json` (route-shaped rather than task-shaped; no risk tiers) |
| D4 | **All AI state in the #241 Postgres database; configured only in Settings** | One durable store shared by replicas; no AI env vars | Env-var configuration; `data/settings.json` (not shareable, no transactions) |
| D5 | **MCP: Streamable HTTP only, over HTTPS** | One endpoint, streaming progress and resource notifications, resumable | stdio and legacy HTTP+SSE |
| D6 | **MCP auth modes `bearer` (default), `disabled`, later `oidc`** | Bearer now, OIDC per the MCP authorization spec later (#262), and an explicit off switch for trusted LANs | Hard-requiring auth; forking the code path per mode |
| D7 | **Least privilege: `tools: []`** | The harness sees only ScadBuddy tools and allowlisted plugin tools. No shell, no file access, no web | Leaving Claude Code's built-in tools available |
| D8 | **Bambuddy is served by ScadBuddy itself** | Keeps the key server-side, scope-aware errors, tiers, approvals and the audit log | Third-party Bambuddy MCP servers |
| D9 | **Plugins are Claude plugins, fetched and pinned** | The SDK loads plugins by local path only (§3.1) | Auto-updating plugins; stdio plugin servers |
| D10 | **Citations are required** | Suggested settings, analyzers, agent edits and docs carry their sources; unsourced claims are labelled judgement and are never auto-applied | — |

### Licensing

The Agent SDK is used under Anthropic's Commercial Terms ("Use of the Claude Agent
SDK is governed by Anthropic's Commercial Terms of Service", [overview][sdk-overview]).
ScadBuddy stays MIT. Nothing from the SDK is vendored into the repo; it is an npm
dependency of `agent/`.

## 3. Facts this design rests on

### 3.1 Read on 2026-09-27

**Claude Agent SDK** (all quotes from the linked pages):

- Plugins load by local path: "The `type` field must be `"local"`, the only value the
  SDK accepts." A plugin can hold skills, agents, hooks and MCP servers (`.mcp.json`).
  Plugin skills are namespaced `/plugin-name:skill-name`. A missing path is skipped
  silently, so loaders must check the init message's `plugins` list and
  `plugin_errors`. [Plugins][sdk-plugins]
- Custom tools are an in-process MCP server: `tool()` with a Zod schema, wrapped in
  `createSdkMcpServer`, named `mcp__{server}__{tool}`. `tools: []` means "All
  built-ins are removed. Claude can only use your MCP tools." Tool annotations are
  "metadata, not enforcement". Tool search is on by default for SDK MCP tools.
  [Custom tools][sdk-tools]
- Sessions are written to disk as JSONL under `$CLAUDE_CONFIG_DIR/projects/`. They
  support `resume` (by id) and `forkSession`. "Session files are local to the machine
  that created them"; to resume elsewhere, "Attach a `sessionStore` / `session_store`
  adapter so the SDK mirrors transcripts to your own backend". `listSessions`,
  `getSessionMessages`, `renameSession` and `tagSession` exist. [Sessions][sdk-sessions]
- "Unless previously approved, Anthropic does not allow third party developers to
  offer claude.ai login or rate limits for their products, including agents built on
  the Claude Agent SDK." The SDK "runs the Claude Code binary". [Overview][sdk-overview]
- Gateways: Anthropic "doesn't support routing Claude Code to non-Claude models
  through any gateway"; `ANTHROPIC_BASE_URL` points Claude Code at a gateway, and a
  gateway credential replaces subscription login. [LLM gateways][gateways]

**This repository** (read from the files named):

- Only two polling loops remain in the frontend: `frontend/src/lib/useRenderJob.ts`
  (`POLL_MS = 400`) and `frontend/src/lib/usePrintProgress.ts` (`POLL_MS = 2000`).
  There is no server event stream. The only WebSockets are the LSP bridges in
  `backend/scadbuddy/api/lsp.py`.
- The Bambuddy client (`backend/scadbuddy/bambuddy/client.py`) already calls
  `/inventory/spools`, `/printers/{id}/inventory-remain`,
  `/library/files/{id}/filament-requirements`, `/projects/{id}/add-archives`, the
  queue, slicer pipelines and pipeline runs. It does **not** yet read the print
  archive as history, or any stats route.
- Settings persist to `data/settings.json` through `library/settings_store.py`, and
  `core/settings.py` maps every field to `SCADBUDDY_<FIELD>`.
- PR #241 (open) adds `SCADBUDDY_DATABASE_URL`, `render/pg_store.py` and a Postgres
  render queue that falls back to files when the URL is unset. Its workers wake on
  `render_poll_interval`.
- TLS is terminated in front of the app (`README.md` checks
  `https://scadbuddy.internal.nullreference.io/healthz`).

### 3.2 To verify (each item names who verifies it)

| Item | Where it matters | Verified by |
|---|---|---|
| The `SessionStore` interface in the pinned TypeScript SDK version, and whether the store is keyed by `cwd` | §6 | #300 |
| How the bundled Claude Code CLI reports its version, so the build can assert it the way the Dockerfile asserts `OPENSCAD_VERSION` | §4.4 | #261 |
| Which `settingSources` value loads nothing from the host | §4.4 | #255 |
| Whether `canUseTool` can pause for an asynchronous human decision without holding the query open indefinitely (or whether a `PreToolUse` hook must deny, and the session resume after approval) | §8 | #255, #258 |
| Bambuddy 1.2.5.5 routes for the print archive (with outcome fields) and any stats endpoint, read off its `openapi.json` with respx recordings | #284, #264 | #251 |
| Whether the #241 Postgres (CloudNativePG in eh-homelab/clusters) needs anything for `LISTEN/NOTIFY` across replicas | §7 | #264 |
| Bambu Studio's hand-off mechanism for "Open in Bambu Studio" | #284 | #284 |
| Which keys `filament_overrides` accepts on `PrintQueueItemCreate` (can it carry nozzle temperature and fan?) | §11 | #284 |
| Whether Bambuddy's `/local-presets/` can create a process preset that inherits from a base preset plus a diff | §11 | #284 |
| Whether a 3MF that claims `Application: BambuStudio-…` has its `project_settings.config` override the pipeline's process preset (main spec §3 measured the preset winning before the claim was made) | §11 | #284 |
| Which process preset each "level of detail" choice maps to | §11 | #284 |

## 4. Architecture

```
browser (SPA, maybe inside the Bambuddy iframe)
  │  https / wss (one origin)
  ▼
ingress ──/api/v1/*, /──────────────▶ backend (Python, uvicorn)  ◀── Bambuddy (httpx, key server-side)
   │                                     ▲   │ NOTIFY
   ├──/mcp, /api/v1/ai/*, /api/v1/ws ─▶ agent (Node 24, Agent SDK) ──LISTEN── Postgres (#241)
   │                                     │ openapi-fetch → backend on localhost
external MCP clients ───────────────────┘
```

### 4.1 Process layout: sidecar (recommended)

The agent service is a separate `agent` image target, deployed as a **second container
in the ScadBuddy pod**. This keeps one process per container, independent restarts and
resource limits, and needs no supervisor under `tini`. The two containers share the
pod network, so the agent calls the backend on `localhost` (§4.3).

For a single-container setup, the alternative is a supervisor running both processes
in one image. That is not recommended, and is left out unless someone needs it.

### 4.2 Routing: at the ingress (recommended)

The ingress routes `/mcp`, `/api/v1/ai/*` and the realtime socket `/api/v1/ws` to the
agent container, and everything else to the backend. Routing at the ingress avoids a
uvicorn passthrough, which risks buffering SSE. `/api/v1/ws` lives in the agent service
because it already holds the database listener (§7) and the browser-bridge pairing
(#254).

**Consequence:** with the agent container down, the UI's realtime updates fall back to
polling (#266) and AI is hidden. Renders and printing are unaffected.

### 4.3 Agent → backend

The agent calls the backend over the pod-local `localhost` port using `openapi-fetch`,
with types generated from `backend/openapi.json`. The freshness chain becomes: export
openapi → `frontend gen:api` → `agent gen:api`. The backend has no auth today and this
adds none. The trust boundary is the pod, and the backend port is not exposed beyond
the Service it already has.

### 4.4 Harness hardening

- The SDK and bundled CLI versions are pinned and asserted at image build (§3.2).
- `CLAUDE_CONFIG_DIR` points to a service-owned directory. `settingSources` is limited
  so nothing is read from a host `~/.claude`. `tools: []` is set on every query.
- The container runs as non-root, with a read-only root filesystem except for the
  config directory, a scratch `cwd` and the plugin cache.
- Credentials are passed per query through the SDK's `env` option and never through the
  container environment.
- Tests point `ANTHROPIC_BASE_URL` at a local fake Anthropic-format server (the seam in
  [LLM gateways][gateways]), so CI never calls Anthropic.

### 4.5 HTTP framework: Hono (recommended)

Hono is small, built on web-standard `Request`/`Response`, handles streaming responses
directly, and hosts both the MCP Streamable HTTP handler and the chat and WebSocket
endpoints. Fastify is the alternative. The choice is local to `agent/`.

## 5. Tools

### 5.1 Registry

One definition per tool:

```ts
defineTool({
  name: "render_model",
  description: "…",
  input: z.object({ slug: z.string(), params: z.record(z.unknown()) }),
  risk: "write",                   // read | write | outward
  bambuddyScope: undefined,        // e.g. "library:write" for Bambuddy-touching tools
  readOnly: false,                 // → readOnlyHint for batching (never used for gating)
  handler: async (args, ctx) => …, // calls the backend via openapi-fetch; ctx.principal, ctx.session
})
```

Two projections are generated from the registry:

1. **Harness:** `createSdkMcpServer({ name: "scadbuddy", tools })`, giving tools named
   `mcp__scadbuddy__render_model`.
2. **External:** `@modelcontextprotocol/sdk` server at `/mcp`, with the same names,
   schemas and handlers.

A test asserts both lists are identical, apart from browser-only tools. A CI check fails
when an operation in `backend/openapi.json` has neither a tool nor an explicit allowlist
entry.

Tools are **task-shaped**, not one per route. For example, `render_model` submits a
render and streams progress until it settles, and `print_output` wraps
eligibility → send → run behind a single approval.

### 5.2 Browser tools

The SDK runs tools in the service process ([custom tools][sdk-tools]), so a browser tool
is an ordinary registry tool whose handler forwards the call over the paired tab's
WebSocket and awaits the result, with a timeout. If no tab is paired it returns an error
("no browser attached"). The tab reports which handlers are live on each route change.
Unavailable handlers return an error instead of disappearing, so the session's tool list
stays stable. Details are in #254.

### 5.3 Resources

The `scadbuddy://` resources in #264 (models, sources, schemas, history, jobs, outputs,
print progress, Bambuddy printers, queue, inventory, history and stats, libraries,
settings, sessions, and the browser snapshot) use the same principal and tier checks as
tools.

## 6. Sessions (#300)

- **Storage.** A Postgres `SessionStore` adapter (§3.2) mirrors SDK transcripts, so any
  replica can resume a session. A metadata table `ai_sessions` holds the id, owner
  principal, origin (`chat` / `mcp` / `analyzer` / `hook`), scope (model, output, job),
  status (`running` / `waiting_input` / `waiting_approval` / `idle` / `done` /
  `failed`), title and tags, cost, and timestamps.
- **One writer at a time.** Sends to a session are serialized; a send while a turn is
  running gets a clear error. Watchers are unlimited.
- **Handoff.** Ownership moves between principals explicitly. The browser user can see
  every session, with a "controlled by …" badge.
- **Agent-to-agent.** Over `/mcp`: `sessions.list/start/send/get/fork/interrupt/approve/deny/handoff`.
  Approvals of outward actions by another agent are off by default and need a per-token
  grant.
- **A2A: deferred.** Once the MCP path works, the same model can be exposed through the
  [A2A protocol][a2a] if an agent needs it. That is not planned for now.

## 7. Events

The Python backend gets a small typed event bus. Every state change publishes
`pg_notify('scadbuddy_events', …)` with a small payload: kind plus ids, and never
content. Kinds:

`job.*`, `model.created|updated|deleted`, `source.changed`, `version.committed`,
`upstream.available`, `output.created`, `print.progress`, `print.settled`,
`printer.status`, `inventory.changed`, `library.changed`, `font.installed`,
`settings.changed`, and from the agent side `session.*` and `analyzer.decision`.

The agent service `LISTEN`s and fans events out to:

- MCP resource subscriptions (`notifications/resources/updated`, #264);
- the UI's WebSocket (#266), which replaces both polling loops (§3.1);
- plugin event hooks (#297);
- the event log used for MCP `Last-Event-ID` resumption.

Print progress comes from **one server-side watcher per active print** (#268), not from
one poll per open dialog. #270 moves #241's render workers from interval polling to the
same `NOTIFY`, with a long fallback poll.

**Without a database**, AI is disabled (§9) and the UI keeps today's polling. The
polling code stays as the fallback path.

## 8. Authorization and approvals

### 8.1 Principals and tiers

Every call, whether a tool, a resource read, a subscription or a session operation,
resolves to a principal through `authenticate(request) → principal { id, tiers }`:

| Principal | Source | Max tier |
|---|---|---|
| browser user | the UI's own chat or session | `outward` (approves in the UI) |
| bearer token | `/mcp` in `bearer` mode, minted in Settings, stored hashed | per token |
| OIDC subject | `/mcp` in `oidc` mode (#262), scopes `scadbuddy:read|write|outward` | per scope |
| `anonymous` | `/mcp` in `disabled` mode | configurable, `write` by default |
| flow | analyzer (#284) or plugin hook (#297) session | the skill's declared `permissions` |

Tiers: `read`, `write` (reversible through history), and `outward` (send, print,
delete, settings or credential writes). A plugin tool ScadBuddy doesn't recognise
defaults to `outward`.

### 8.2 Approval gate

Outward tools **always** need a human approval in the ScadBuddy UI, in every auth mode,
including `disabled`. Where it is enforced:

- **Harness:** the SDK permission callback and a `PreToolUse` hook
  ([permissions][sdk-permissions], [hooks][sdk-hooks]). The session goes to
  `waiting_approval`, the UI shows a confirmation card, and the decision resumes it.
  Whether the callback can wait on an asynchronous human decision, or the hook must deny
  and the session be resumed afterwards, is in §3.2.
- **External MCP clients:** a two-step `prepare` (returns a pending action id and a
  human-readable summary) then `confirm`, where the confirm completes only after the UI
  approval.

### 8.3 MCP auth modes

The mode is a database setting, changed in Settings, and changing it counts as a
settings write, so it needs approval.

- **`bearer` (default).** `Authorization: Bearer <token>`. Unauthenticated requests get
  `401` with a `WWW-Authenticate: Bearer` header.
- **`disabled`.** No credential. Calls run as `anonymous` with a tier cap; a persistent
  warning banner shows; the audit log records the client IP.
- **`oidc` (#262).** `/mcp` becomes an OAuth 2.1 resource server per the
  [MCP authorization spec][mcp-auth]: protected-resource metadata, and JWTs validated
  against the IdP's JWKS. Bearer tokens keep working alongside it. The mode can't be
  switched to `oidc` until a discovery test against the issuer passes.

**Stated plainly:** ScadBuddy's UI has no login of its own. Until it does, anyone who
can reach Settings can change the MCP auth mode, mint tokens or approve actions. MCP
auth protects against stray MCP clients, not against someone already on the network
that reaches the UI. Putting the UI behind OIDC is a separate issue.

### 8.4 Transport rules

- HTTPS only: `X-Forwarded-Proto` is trusted from the ingress only, and plain HTTP gets
  `403` in `bearer` and `oidc` modes, except from loopback.
- An `Origin` check on `/mcp` and `/api/v1/ws` prevents DNS rebinding.
- Nothing in the path may buffer SSE. A test asserts that events arrive before the
  response completes.

### 8.5 Browser pairing

The browser user's own chat sessions pair with their tab automatically. An external
agent needs a pairing token that the user accepts **in the tab**, in every auth mode:
driving someone's open tab is more invasive than calling tools, so `disabled` mode does
not skip pairing.

### 8.6 Threat model (summary)

| Threat | Mitigation |
|---|---|
| Prompt injection via model READMEs, upstream sources, library code, plugin output, Bambuddy data | Tool results wrap such content as untrusted; outward actions always need a human approval; `tools: []` means injected text can't reach a shell or the filesystem |
| Stray or hostile MCP client on the LAN | `bearer` default, tier caps, audit log, rate limits on agent-triggered renders |
| Credential leakage | Credentials encrypted at rest (§9), never returned by any route, passed per query, redacted in logs and audit |
| Malicious or changed plugin | Fetched at a pinned commit; reviewed part by part before enabling; command hooks refused; unknown tools default to `outward`; re-pinning shows a diff |
| Runaway agent | `maxTurns`, per-session budget, render rate limits, interrupt from any watcher |

## 9. Persistence and credentials

All AI state lives in the #241 database, in `ai_*` tables owned and migrated by the
agent service:

- Claude credentials (encrypted);
- MCP auth mode, tokens (hashed), and OIDC configuration;
- plugins: source, pinned commit, enabled parts, endpoint credentials (encrypted), and
  tier map;
- sessions (§6), MCP subscriptions, and the resumability event log;
- the audit log.

There are **no AI env vars**. The only variables the agent reads are
`SCADBUDDY_DATABASE_URL` (shared with #241), the backend URL, and the key-encryption
key file below.

**Encryption at rest (recommended):** envelope encryption.

- Each secret is sealed with AES-256-GCM under a random per-row data key. The data key
  is sealed under a **key-encryption key read from a file**
  (`SCADBUDDY_SECRET_KEY_FILE`, mounted from a Kubernetes Secret in eh-homelab/clusters).
- That file is infrastructure, like the database URL; it is not AI configuration.
- Rotating it re-wraps the data keys only.
- Without the file, Settings refuses to save credentials and says why.

**No database** (`SCADBUDDY_DATABASE_URL` unset): AI features are disabled, and Settings
explains that they need the database. The rest of ScadBuddy works as today (§7).

## 10. Plugins (#297, #299)

- **ScadBuddy's own plugin** (`plugins/scadbuddy/`) holds skills (authoring,
  customizing, printing, analyzers), subagents (`model-author`, `print-analyst`), hooks,
  and a `.mcp.json` for external installs. It is baked into the image and loaded by path.
  A marketplace file at the repo root lets users install it in their own Claude Code.
- **User plugins** are Claude plugins from a git URL, fetched into the data volume at a
  pinned commit. They are reviewed before enabling; their MCP servers must be Streamable
  HTTPS, with credentials in Settings. Command hooks are refused, because the harness has
  no shell. ScadBuddy adds **event hooks** that start a session running a plugin skill
  when a bus event fires (§7).
- The first user plugin is Hindsight memory, connected to the user's own endpoint.
  Learned findings cite the events behind them and are never auto-applied.

## 11. Print analyzers (#284)

Analyzers are skills in the ScadBuddy plugin, or in user and template scope folders.
They run in the print flow as sessions with `flow` principals (§8.1). #284 holds the
design: scopes, `checks.yaml`, CEL conditions, diagnostics and fixers, and citations.

**Base profile.** The "easy print" work is the print-workflow epic **#84**. The base
analyzers diff against is the resolved print request it already builds
(`2026-09-24-print-flow-design.md` §1):

- the pipeline's printer, process and filament presets, and bed type;
- the per-slot filament plan (#87);
- the plate (#83);
- the print options (#88).

The level-of-detail choice selects the process preset (§3.2).

**Where an accepted diff lands.** ScadBuddy stores "never its own slicing settings"
(#84), and a pipeline run carries only `source_library_file_id` /
`source_archive_id` / `copies` / `force` (print-flow spec §2). So a diff is expressed
as Bambuddy objects, and accepting one forces the slice-then-queue route that #88
already escalates to:

1. **Filament-level settings** go in `filament_overrides` on the queue item.
2. **Process-level settings** become a **derived local preset**: the base process
   preset plus the diff, created through `/local-presets/` and tagged with the
   analyzers and scope that produced it.
3. **3MF `project_settings.config`** only if §3.2 shows it beats the pipeline's
   preset. Main spec §3 measured the preset winning.

Each of these is in §3.2 until verified.

## 12. Order of work

1. This spec (#250).
2. #241 merges: it is the database everything here needs.
3. #261 agent service scaffold → #255 harness and credentials → #251 tool registry and
   `/mcp` → #264 resources and event bus, alongside #266 WebSocket gateway.
4. #254 browser bridge, #256 assistant panel, #300 sessions, #299 ScadBuddy plugin.
5. #252 authoring, #253 dependencies, #258 safety (grows with each tool), #267–#270
   realtime conversions.
6. #284 analyzers (after the dynamic profile work), #297 plugins, #262 OIDC, #257 voice,
   #259 evals and docs throughout.

## 13. Test strategy

- **agent/ (vitest):**
  - the fake Anthropic-format endpoint via `ANTHROPIC_BASE_URL`;
  - registry projection equality and the openapi coverage check;
  - auth per mode;
  - approval gates;
  - `tools: []` enforcement;
  - the plugin loader (init message `plugins` and `plugin_errors`);
  - the `SessionStore` against Postgres;
  - the MCP Streamable HTTP client for streaming, resume, `Origin` and plain-HTTP
    refusal.
- **backend (pytest):** event publishing per mutation route with a test listener; the
  new endpoints (#252, #253, #284) as usual; Bambuddy routes with respx recordings.
- **e2e (Playwright):** scripted sessions against the fake endpoint, as listed in #259.
- **Live evals:** opt-in, never in required CI. Scored on task success and on citation
  quality.

[sdk-overview]: https://code.claude.com/docs/en/agent-sdk/overview
[sdk-plugins]: https://code.claude.com/docs/en/agent-sdk/plugins
[sdk-tools]: https://code.claude.com/docs/en/agent-sdk/custom-tools
[sdk-sessions]: https://code.claude.com/docs/en/agent-sdk/sessions
[sdk-permissions]: https://code.claude.com/docs/en/agent-sdk/permissions
[sdk-hooks]: https://code.claude.com/docs/en/agent-sdk/hooks
[gateways]: https://code.claude.com/docs/en/llm-gateway
[mcp-auth]: https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
[a2a]: https://github.com/a2aproject
