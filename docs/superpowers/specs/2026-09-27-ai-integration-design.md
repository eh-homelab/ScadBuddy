# AI integration: harness, tools, sessions, auth

Design for epic #249 (spec issue #250). Written 2026-09-27. It settles what the
epic's implementation stories build on: the harness, how tools are defined once and
served twice, where state lives, how MCP clients authenticate, and what a human must
approve. Those stories are the epic's sub-issues: #251–#259, #261, #262, #264, #265
(the realtime story, with its children #266–#270), #284, #297, #299, #300 and
#349. Numbers between them that aren't listed belong to unrelated work.

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
| D4 | **All AI state in the #241 Postgres database; configured only in Settings** | One durable store shared by replicas; no AI-*configuration* env vars (infrastructure bootstrap variables still reach the agent container, §9) | Env-var configuration; `data/settings.json` (not shareable, no transactions) |
| D5 | **MCP: Streamable HTTP only, over HTTPS** | One endpoint, streaming progress and resource notifications, resumable | stdio and legacy HTTP+SSE |
| D6 | **MCP auth modes `bearer` (default), `disabled`, later `oidc`** | Bearer now, OIDC per the MCP authorization spec later (#262), and an explicit off switch for trusted LANs | Hard-requiring auth; forking the code path per mode |
| D7 | **Least privilege: `tools: []`** | The harness sees only ScadBuddy tools and allowlisted plugin tools. No shell, no file access, no web | Leaving Claude Code's built-in tools available |
| D8 | **Bambuddy is served by ScadBuddy itself** | Keeps the key server-side, scope-aware errors, tiers, approvals and the audit log | Third-party Bambuddy MCP servers |
| D9 | **Plugins are Claude plugins, fetched and pinned** | The SDK loads plugins by local path only (§3.1) | Auto-updating plugins; stdio plugin servers |
| D10 | **Citations are required** | Suggested settings, analyzers, agent edits and docs carry their sources; unsourced claims are labelled judgement and are never auto-applied | — |
| D11 | **Headless browser: the official `playwright` Claude plugin, pinned and vendored, beside the #254 bridge** (§5.3, #349) | Sessions with no user tab (#300 sessions started over `/mcp` or by another agent, visual checks while authoring for #252/#253, #259 evals) still need to see and drive the UI. The plugin is only an MCP server around `@playwright/mcp` (§3.1) | Replacing the #254 bridge (a headless browser can't reach the user's tab inside the Bambuddy iframe); running the plugin's own `.mcp.json` as shipped (`npx @playwright/mcp@latest` fetches an unpinned package at runtime) |

### Licensing

The Agent SDK is used under Anthropic's Commercial Terms ("Use of the Claude Agent
SDK is governed by Anthropic's Commercial Terms of Service", [overview][sdk-overview]).
ScadBuddy is Apache-2.0 (`LICENSE`; it switched from MIT in #301). Nothing from the
SDK is vendored into the repo; it is an npm
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
  `getSessionMessages`, `renameSession` and `tagSession` exist. With a store, "The
  store lookup key derives from the working directory, so resume from a `cwd` matching
  the original run's" (§6 therefore gives every session a stable service-owned `cwd`).
  [Sessions][sdk-sessions]
- Measured in PR #319 on `@anthropic-ai/claude-agent-sdk` 0.3.283: the bundled Claude
  Code binary prints `2.1.283 (Claude Code)` on stdout for `--version` and exits 0, so
  the image build asserts `CLAUDE_CODE_VERSION` the way the Dockerfile asserts
  `OPENSCAD_VERSION`. In that SDK's `sdk.d.ts`, `settingSources: []` means "disable
  filesystem settings (SDK isolation mode)", so it loads nothing from the host (§4.4).
- Read and measured in #300 on SDK 0.3.283 (`sdk.d.ts`, `export declare type
  SessionStore`, marked `@alpha`): `append(key, entries)` and `load(key)` are required;
  `listSessions?(projectKey)`, `listSessionSummaries?(projectKey)`, `delete?(key)` and
  `listSubkeys?({projectKey, sessionId})` are optional. `SessionKey` is
  `{ projectKey, sessionId, subpath? }` (projectKey "Default: sanitized cwd"; no option
  sets it). `append` "SHOULD treat `uuid` as an idempotency key"; `load` returns `null`
  for a key "never written", and entries must be "deep-equal to what was appended". The
  `forkSession()` function and `getSessionMessages()` take the same `sessionStore`. The
  query option `sessionId` gives a new session a caller-chosen UUID. Measured against the
  local fake endpoint (`agent/test/sessions.e2e.test.ts`): a session resumes on a fresh
  `CLAUDE_CONFIG_DIR` with a different `cwd` when the store's lookups ignore projectKey;
  the last transcript entries (`last-prompt`, `cost-state`) are appended after the
  `result` message and before the iterator ends; a resumed query's `total_cost_usd`
  includes the earlier turns. The adapter is `agent/src/sessions/store.ts`.
- Read and measured in #297 on SDK 0.3.283 (`agent/test/plugins.e2e.test.ts`, the real
  bundled CLI against a local `@modelcontextprotocol/sdk` 1.30.1 Streamable HTTP server):
  the SDK passes every non-SDK MCP server to Claude Code as `--mcp-config <json>` on its
  argv (`sdk.mjs`), so a header value written there would be on the command line; a
  header written as `${VAR}` with the value in the query's `env` is expanded by Claude
  Code and reaches the server, and the argv holds only the reference. A configured
  `{ type: 'http' }` server named `my-memory` gives tools `mcp__my-memory__<tool>` and
  reports `{ name, status: 'connected', source: 'dynamic' }` in the init message;
  `disallowedTools: ['mcp__my-memory__forget']` removes that tool from the request;
  `alwaysLoad: true` ("never deferred behind tool search ... blocks startup until the
  server is connected (capped at the standard 5s connect timeout)", `sdk.d.ts`) puts the
  tools in the first turn. The permission seam applies to them as to in-process tools: a
  `read` tool runs, an unlisted one is denied as needing approval and never reaches the
  server.
- Read and measured in the #464 review, on the bundled Claude Code 2.1.283:
  - **Tool-name normalisation.** Claude Code names an MCP tool
    `mcp__${vn(server)}__${vn(tool)}`, where `vn(s) = s.replace(/[^a-zA-Z0-9_-]/g, "_")`
    (read in the CLI bundle's `Pa()`/`vn()`; confirmed by a probe). `files.list` and
    `files_list` therefore collide on one name, and a name with a space or a dot cannot be
    matched literally. The registry now tiers only names in that alphabet, maps disabled
    names through `vn`, and hides colliding tools (`agent/src/plugins/registry.ts`
    `harnessToolName`, `agent/src/plugins/forwarder.ts`). This is measured end to end in
    `agent/test/plugins.e2e.test.ts`: `files.delete` is disabled as
    `mcp__my-memory__files_delete`, the colliding pair is never offered, and a call to
    the colliding name never reaches the server.
  - **Redirects and OAuth discovery.** Claude Code's own MCP client follows 30x redirects
    and a `WWW-Authenticate` `resource_metadata` URL, and sends the configured header
    there too. The probe showed a 307 to another origin receiving every request with the
    header, and `resource_metadata="http://169.254.169.254/…"` receiving a GET with it.
    Claude Code is therefore never given a plugin's URL or secret. A loopback forwarder
    in the agent (`agent/src/plugins/forwarder.ts`):
    - connects to the egress-checked address;
    - refuses redirects;
    - turns a 401 into a 502;
    - adds the header itself.

    The e2e test measures that a redirecting plugin is not `connected` and that the
    redirect target and metadata URL are never contacted. That supersedes the `${VAR}`
    header mechanism above.
- "Unless previously approved, Anthropic does not allow third party developers to
  offer claude.ai login or rate limits for their products, including agents built on
  the Claude Agent SDK." The SDK "runs the Claude Code binary". [Overview][sdk-overview]
- Gateways: Anthropic "doesn't support routing Claude Code to non-Claude models
  through any gateway"; `ANTHROPIC_BASE_URL` points Claude Code at a gateway, and a
  gateway credential replaces subscription login. [LLM gateways][gateways]
- `disallowedTools` removes a tool: "The `Bash` tool definition is removed from the
  request. Claude does not see the tool and cannot attempt it." Tool-name globs work in
  deny rules. An allow rule only pre-approves; "Auto-approved tools never reach
  `canUseTool`". [Permissions][sdk-permissions]
- Read and measured in #258 on SDK 0.3.283 (moved up from §3.2): **`canUseTool` can
  park a tool call on an asynchronous human decision, with no deadline of its own.**
  `sdk.d.ts` on `CanUseTool`: "permission prompts have no park deadline"; the
  `dialogExpiry` setting (default 5 minutes) is for a dialog "forwarded to a remote
  client", and "Local-only permission prompts (no remote client) are unaffected".
  Measured against the local fake endpoint (`agent/test/approvals.sdk.test.ts`): a call
  parked for 5 s with that remote deadline forced down to 1 s
  (`CLAUDE_CODE_USER_DIALOG_TIMEOUT_MS=1000`), and once for 5.5 minutes with the
  default left in place, still waited; the model was sent nothing meanwhile; on
  approval the tool ran with the `updatedInput` the callback returned, and on denial
  the callback's `message` reached the model as the tool's error result. A
  `PreToolUse` hook answering `ask` hands the call to `canUseTool` ("With a permission
  prompt surface (stdio/SDK canUseTool), the 'ask' path surfaces via a
  can_use_tool control_request", `sdk.d.ts`). Aborting the query while a call is
  parked fails that call ("Tool permission request failed: AbortError: Tool
  permission stream closed before response received"); Claude Code may still send
  that to the model and yield a `result` before it exits
  (`agent/test/approvals.e2e.test.ts`), and the tool does not run. #258 therefore
  parks (§8.2), and uses deny-then-resume only for an approval whose turn is gone
  after a restart.

**Playwright plugin and `@playwright/mcp`** (#349; the `@playwright/mcp` items were read
from the **0.0.82** npm tarball's `README.md`, the `latest` dist-tag on 2026-09-27,
which is byte-identical to `README.md` on `main` of microsoft/playwright-mcp):

- The official plugin is only an MCP server. [`.mcp.json`][pw-plugin-mcp] is
  `{"playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}}`, and
  [`plugin.json`][pw-plugin-json] names the author `Microsoft`.
- 0.0.82 depends on `playwright` and `playwright-core` `1.64.0-alpha-1789764292000`
  and is Apache-2.0 ([npm][pw-npm], the tarball's `package.json`).
- Flags and config keys ([README, Configuration][pw-readme]; each flag also has a
  `PLAYWRIGHT_MCP_*` env var):
  - `--headless`: "run browser in headless mode, headed by default".
  - `--isolated` / `browser.isolated`: "keep the browser profile in memory, do not save
    it to disk". In isolated mode "Every time you ask MCP to close the browser, the
    session is closed and all the storage state for this session is lost."
  - `--allowed-origins` / `network.allowedOrigins`, `--blocked-origins` /
    `network.blockedOrigins`: origins "to allow the browser to request. Default is to
    allow all." Both are documented as "Important: *does not* serve as a security
    boundary and *does not* affect redirects." The blocklist wins over the allowlist.
  - `--output-dir` / `outputDir`: "for automatically named output files, for example a
    screenshot taken without an explicit file name. Files with an explicit name are
    resolved against the workspace root instead".
  - File access is restricted by default: "By default access to file system is
    restricted to workspace root directories (or cwd if no roots are configured) only,
    and navigation to file:// URLs is blocked." `--allow-unrestricted-file-access`
    lifts it, and the schema calls the restriction "a convenience defense … not a
    secure boundary".
  - `browser.contextOptions` is a `playwright.BrowserContextOptions` ("Context options
    for the browser context"), and `--init-script` is "evaluated in every page before
    any of the page's scripts".
  - `--caps` enables extra tool groups (`vision`, `pdf`, `devtools`; the tool list also
    marks `config`, `network`, `storage` and `testing` as opt-in). `--no-webmcp` turns off
    tools that a page registers through WebMCP.
- The core tool set includes `browser_run_code_unsafe`: "executes arbitrary JavaScript
  in the Playwright server process and is RCE-equivalent". It also includes
  `browser_evaluate` (JavaScript in the page), `browser_file_upload` and `browser_drop`
  (both take absolute file paths). [README, Tools][pw-readme]
- Playwright's `extraHTTPHeaders` context option: "An object containing additional HTTP
  headers to be sent with every request. Defaults to none." [Playwright
  `browser.newContext`][pw-extra-headers]

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
| Bambuddy 1.2.5.5 routes for the print archive (with outcome fields) and any stats endpoint, read off its `openapi.json` with respx recordings | #284, #264 | #251 |
| Whether the #241 Postgres (CloudNativePG in eh-homelab/clusters) needs anything for `LISTEN/NOTIFY` across replicas | §7 | #264 |
| Bambu Studio's hand-off mechanism for "Open in Bambu Studio" | #284 | #284 |
| Which keys `filament_overrides` accepts on `PrintQueueItemCreate` (can it carry nozzle temperature and fan?) | §11 | #284 |
| Whether Bambuddy's `/local-presets/` can create a process preset that inherits from a base preset plus a diff | §11 | #284 |
| Whether a 3MF that claims `Application: BambuStudio-…` has its `project_settings.config` override the pipeline's process preset (main spec §3 measured the preset winning before the claim was made) | §11 | #284 |
| Which process preset each "level of detail" choice maps to | §11 | #284 |
| Whether `network.allowedOrigins` in the pinned `@playwright/mcp` blocks requests made by page JavaScript (`fetch`, XHR, WebSocket, workers) as well as navigations and subresources. The README says only "the browser … request" and that the list "*does not* serve as a security boundary and *does not* affect redirects" (§3.1); measure it with a page that fetches another origin | §5.3 | #349 |
| Whether a navigation to an origin not on the allow-list is refused with an error the tool returns, and what a redirect off the origin does | §5.3 | #349 |
| Whether `browser.contextOptions.extraHTTPHeaders` in the config file reaches the isolated context `@playwright/mcp` creates, is sent on every request the page makes, and can't be removed or overridden by page JavaScript (`browser_evaluate`, a `fetch` with its own headers) | §5.3, §8.2 | #349 |
| Whether `--isolated` gives each MCP client connection (one per session) its own context, and whether the stdio server the plugin runs is one process per session in the SDK | §5.3 | #349 |
| The tool names the SDK gives a plugin's MCP server (the prefix `disallowedTools` must match), and that deny rules remove `browser_run_code_unsafe`, `browser_evaluate`, `browser_file_upload` and `browser_drop` from a plugin server as they do for a configured one | §5.3 | #349 |
| Whether `outputDir` plus the default workspace-root file restriction keeps every write (screenshots named by the model, `filename` arguments) inside the session's scratch directory | §5.3 | #349 |
| How the backend learns that an outward request carrying the agent-actor marker belongs to an approved, unconsumed outward action (a lookup in the shared database, or a call to the agent service), and that it can consume it exactly once | §5.3, §8.2 | #349 |
| The Chromium build that `playwright` 1.64 needs on the `agent` image's base, how it is installed at build time (`playwright install --with-deps chromium` or a distro package), whether it runs as the non-root user under a read-only root filesystem without `--no-sandbox`, and the image-size cost | §5.3, §4.4 | #349 |

## 4. Architecture

```
browser (SPA, maybe inside the Bambuddy iframe)
  │  https / wss (one origin)
  ▼
ingress ──/api/v1/*, /api/v1/ws, /──▶ backend (Python, uvicorn)  ◀── Bambuddy (httpx, key server-side)
   │                                     ▲        │
   │                                     │        │ pg_notify
   │                                     │        ▼
   │                                     │   Postgres (#241)
   │                                     │        │
   │                                     │        │ LISTEN
   │                                     │        ▼
   ├──/mcp, /api/v1/ai/* ─────────────▶ agent (Node 24, Agent SDK)
   │                                     │ openapi-fetch → backend on localhost
external MCP clients ───────────────────┘

(There is no direct backend→agent event channel: events go backend → Postgres → agent, §7.
 The backend and the agent each `LISTEN` on their own connection and serve their own
 sockets: the UI's `/api/v1/ws` from the backend, the agent's own under `/api/v1/ai/*`.)
```

### 4.1 Process layout: sidecar (recommended)

The agent service is a separate `agent` image target, deployed as a **second container
in the ScadBuddy pod**. This keeps one process per container, independent restarts and
resource limits, and needs no supervisor under `tini`. The two containers share the
pod network, so the agent calls the backend on `localhost` (§4.3).

For a single-container setup, the alternative is a supervisor running both processes
in one image. That is not recommended, and is left out unless someone needs it.

### 4.2 Routing: at the ingress (recommended)

The ingress routes `/mcp` and `/api/v1/ai/*` to the agent container, and everything
else, including the UI's realtime socket `/api/v1/ws`, to the backend. `/api/v1/ai/*`
is a sub-path of the backend's `/api/v1/*`, so **the agent's paths must take precedence**:
longest-prefix match, or explicit rule priority. A "first rule starting with `/api/v1/`"
setup would silently send the agent's routes to the backend. The ingress manifest gets a
test request per agent path. Routing at the ingress avoids a
uvicorn passthrough, which risks buffering SSE.

`/api/v1/ws` lives in the **backend** (decided while building #266, superseding the
first draft, which put it in the agent). It serves the UI's domain events (jobs, models,
outputs, prints, libraries, fonts, settings) straight from the backend's event bus
(`backend/scadbuddy/core/events.py`), so realtime does not depend on the AI container.
The agent's own streams (sessions, approvals, the browser bridge, #254) are served by
the agent under `/api/v1/ai/*`. Both are independent consumers of the same `NOTIFY`
channel (§7).

**Consequence:** with the agent container down, AI is hidden and nothing else changes:
realtime updates, renders and printing are unaffected.

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
render and streams progress until it settles, and `print_output` fills any omitted
choice the way the print dialog opens, then slices and queues behind a single approval.
(It wrapped eligibility → send → run until the spool-first print flow, #335, removed the
pipeline and eligibility routes; see `2026-09-27-spool-first-print-design.md` §7.)

### 5.2 Browser tools

The SDK runs tools in the service process ([custom tools][sdk-tools]), so a browser tool
is an ordinary registry tool whose handler forwards the call over the paired tab's
WebSocket and awaits the result, with a timeout. If no tab is paired it returns an error
("no browser attached"). The tab reports which handlers are live on each route change.
Unavailable handlers return an error instead of disappearing, so the session's tool list
stays stable. Details are in #254.

### 5.3 Headless browser (#349)

The #254 bridge acts **in the user's own tab**, inside the Bambuddy iframe, live in
front of them. Some sessions have no tab: sessions started over `/mcp` or by another
agent (#300), visual checks while authoring (render, then screenshot the preview or
plate, for #252/#253), and evals that drive the UI end to end (#259). For those the
harness gets a **headless Chromium in the agent container** through the official
`playwright` plugin (D11). The bridge stays the only way to touch the user's tab; the
headless browser never sees it.

- **Pinned and vendored.** `@playwright/mcp` is an exact dependency of `agent/`, and a
  matching Chromium is installed at image build (§3.2). Nothing runs `npx …@latest`: the
  image carries its own copy of the plugin whose `.mcp.json` starts the vendored
  `cli.js`, and the harness loads it by local path, as for every plugin (§3.1,
  [Plugins][sdk-plugins]). The loader checks `plugins` and `plugin_errors` in the init
  message. Bumping the version re-runs every item in the §3.2 rows for §5.3.
- **Locked to ScadBuddy's own origin.** The server starts with `--headless` and a
  `--config` file that sets `network.allowedOrigins` to the backend's origin
  (`SCADBUDDY_BACKEND_URL`, which serves the SPA) and nothing else. The README says the
  allow-list is not a security boundary and ignores redirects (§3.1), so it is not the
  only guard: page JavaScript coverage is in §3.2, and the approval rule below does not
  depend on it. `--allow-unrestricted-file-access` is never passed; `outputDir` is the
  session's scratch directory; `--caps` is left empty and `--no-webmcp` is set.
- **Tools the model can't see.** `browser_run_code_unsafe` is RCE-equivalent in the
  agent container (§3.1), which D7 rules out, and `browser_evaluate`,
  `browser_file_upload` and `browser_drop` reach page JavaScript or the filesystem. All
  four go in `disallowedTools`, which removes a tool from the request
  ([permissions][sdk-permissions]); the exact names are in §3.2.
- **Isolated context per session.** `--isolated`, no `--user-data-dir`, no
  `--storage-state`: the profile stays in memory and dies with the session (§3.1). One
  server per session (§3.2), so two sessions never share cookies or a page.
- **Off unless enabled.** A harness setting in the database (D4, §9), off by default,
  enables it; changing it is a settings write, so it needs approval (§8.3). With AI off
  it is hidden with the rest of the assistant.
- **Tiers.** The plugin's tools get an explicit tier map, so the §8.1 default of
  `outward` for unknown plugin tools never applies to them. Navigating, snapshots,
  screenshots, console and network listings, waiting and tab listing are `read`.
  Clicking, typing, filling, selecting, pressing keys and closing the browser are
  `write`. Tool calls appear in the action feed at those tiers.
- **Approvals cannot be bypassed.** Clicking *Send* or *Print* in the headless UI is a
  `write`-tier click, but the request it makes is outward, so the backend enforces
  §8.2 itself. Every request from the headless context carries an **agent-actor marker**:
  an `extraHTTPHeaders` entry set through `browser.contextOptions` (§3.1) naming the
  session. Backend middleware refuses every outward route (send, print, delete, settings
  and credential writes) that carries the marker, with `403`, **unless** an approved,
  unconsumed outward action for that session authorises that request; the backend
  consumes it once. The marker is not authentication: a request without it is exactly
  as trusted as today (§4.3, §8.3), and forging one can only get a request refused. What
  it removes is the headless path around §8.2. The mechanism is **not yet verified**:
  whether the header reaches every request and survives page JavaScript, and how the
  backend looks up the approval, are in §3.2, and #349 must prove them with the
  negative test in §13 before the setting can be turned on.

### 5.4 Resources

The `scadbuddy://` resources in #264 (models, sources, schemas, history, jobs, outputs,
print progress, Bambuddy printers, queue, inventory, history and stats, libraries,
settings, sessions, and the browser snapshot) use the same principal and tier checks as
tools.

## 6. Sessions (#300)

- **Storage.** A Postgres `SessionStore` adapter (§3.1) mirrors SDK transcripts, so any
  replica can resume a session. A metadata table `ai_sessions` holds the id, owner
  principal, origin (`chat` / `mcp` / `analyzer` / `hook`), scope (model, output, job),
  status (`running` / `waiting_input` / `waiting_approval` / `idle` / `done` /
  `failed`), title and tags, cost, and timestamps.
- **One writer at a time.** Sends to a session are serialized; a send while a turn is
  running gets a clear error. Watchers are unlimited.
- **Handoff.** Ownership moves between principals explicitly. The browser user can see
  every session, with a "controlled by …" badge.
- **Event log redaction.** Each session's panel events are stored in `ai_session_events`
  and replayed to every watcher, so tool payloads are scrubbed before they are stored
  (`agent/src/sessions/sdkEvents.ts` `scrubForLog`): the turn's credential is redacted
  from every string, arguments named like secrets are blanked, `tool.call` inputs are
  cut to a preview above 4 KB, and `tool.result` summaries are capped at 500 characters.
  Full payloads stay only in the SDK transcript, which watchers never receive. Before
  #251 and #258 wire in real outward tools, the registry must let a tool declare
  secret-bearing arguments under other names, and the scrubber must honour them.
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

Two independent consumers `LISTEN` on the channel, each on its own connection
(Postgres delivers a `NOTIFY` to every listening session:
<https://www.postgresql.org/docs/current/sql-notify.html>):

- **The backend** fans events out to the UI's WebSocket `/api/v1/ws` (#266,
  `backend/scadbuddy/api/realtime.py`), which replaces both polling loops (§3.1).
- **The agent service** fans them out to MCP resource subscriptions
  (`notifications/resources/updated`, #264), plugin event hooks (#297), and its own
  sockets under `/api/v1/ai/*`.

The event log used for MCP `Last-Event-ID` resumption belongs to the bus (#264). The
UI socket does not replay: on every (re)subscribe the server confirms with
`subscribed` only once it is listening, and the client re-reads then, so a reconnect
cannot leave a gap.

Print progress comes from **one server-side watcher per active print** (#268), not from
one poll per open dialog. #270 moves #241's render workers from interval polling to the
same `NOTIFY`, with a long fallback poll.

**The database is required** (decided while building #266; tracked in #401). The
polling code stays only as the fallback for a socket that cannot connect (#266's
"live updates unavailable").

## 8. Authorization and approvals

### 8.1 Principals and tiers

Every call, whether a tool, a resource read, a subscription or a session operation,
resolves to a principal through `authenticate(request) → principal { id, tiers }`:

| Principal | Source | Max tier |
|---|---|---|
| browser user | the UI's own chat or session | `outward` (approves in the UI) |
| bearer token | `/mcp` in `bearer` mode, minted in Settings, stored hashed | per token |
| OIDC subject | `/mcp` in `oidc` mode (#262), scopes `scadbuddy:read|write|outward` | per scope |
| `anonymous` | `/mcp` in `disabled` mode | **full access (`outward`) by default**; an operator can lower the cap in Settings |
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
  The callback parks the call until the decision (§3.1, #258). Approvals live in
  `ai_approvals`, so a pending one survives a restart; approving one whose turn is gone
  resumes the session with a turn that repeats the call. The approval is bound to that
  turn and used once, by a call with the same tool and input hash; if the session cannot
  resume, the approval is voided and the session is told. Resuming one of several such
  approvals of a session cancels the others. A decision binds to the input hash (an
  HMAC under a key derived from the key-encryption key); a changed input needs a new
  approval. Only the browser user decides, or another principal with a per-token grant
  (§6), and never for its own calls or sessions. Interrupt, handoff and a new turn
  cancel a pending approval and void an approved one that was not used yet, as does
  the end of the turn it belongs to; one that nobody decides expires
  (`approval_expiry_seconds` in `ai_settings`). The code is
  `agent/src/approvals/service.ts`.
- **External MCP clients:** a two-step `prepare` (returns a pending action id and a
  human-readable summary) then `confirm`, where the confirm completes only after the UI
  approval. The prepare is a pending row in `ai_approvals` with no session. It records
  the MCP principal, the tool, the input hash and the scrubbed summary, and its id is the
  pending action id. The UI decides it like any other approval, and self-approval is
  refused. `confirm_action` takes the id and the same arguments, because only the hash
  is stored. It answers "pending" until a decision. It runs the call only when a single
  `UPDATE` marks the row used, and that `UPDATE` requires the row to be approved,
  unused, not voided and within its usable window, with the same principal and the same
  input hash. So the call runs at most once, and a replay, another principal or a
  changed input is refused. The code is `agent/src/approvals/mcp.ts` and
  `agent/src/tools/approvals.ts`.
- **Headless browser (#349):** the backend refuses outward routes on requests that carry
  the agent-actor marker unless an approved outward action authorises them (§5.3). Until
  the §3.2 items for that mechanism are verified, the headless browser stays off.

### 8.3 MCP auth modes

The mode is a database setting, changed in Settings, and changing it counts as a
settings write, so it needs approval.

- **`bearer` (default).** `Authorization: Bearer <token>`. Unauthenticated requests get
  `401` with a `WWW-Authenticate: Bearer` header.
- **`disabled`.** No credential, but still HTTPS only (§8.4). Calls run as `anonymous`
  with **full access** by default: choosing `disabled` is the operator's explicit decision
  to trust the network, so it isn't second-guessed with a lower cap. The cap stays
  configurable in Settings for operators who want `read` or `write` only. Outward
  actions still need a human approval in the UI (§8.2); that rule is independent of auth.
  A persistent warning banner shows, and the audit log records the client IP. D7's least
  privilege applies to the harness's built-in tools, not to what an operator chooses to
  grant callers.
- **`oidc` (#262).** `/mcp` becomes an OAuth 2.1 resource server per the
  [MCP authorization spec][mcp-auth]: protected-resource metadata, and JWTs validated
  against the IdP's JWKS. Bearer tokens keep working alongside it. The mode can't be
  switched to `oidc` until a discovery test against the issuer passes.

**Stated plainly:** ScadBuddy's UI has no login of its own. Until it does, anyone who
can reach Settings can change the MCP auth mode, mint tokens or approve actions. MCP
auth protects against stray MCP clients, not against someone already on the network
that reaches the UI. Putting the UI behind OIDC is a separate issue.

### 8.4 Transport rules

- HTTPS only, **in every auth mode including `disabled`** (D5): `X-Forwarded-Proto` is
  trusted from the ingress only, and plain HTTP gets `403` naming the HTTPS URL. The
  only exception is loopback, for local development and tests. `disabled` mode removes
  the credential requirement, not the transport requirement.
- An `Origin` check on `/mcp`, the agent's sockets and the backend's `/api/v1/ws` prevents
  DNS rebinding.
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
| Stray or hostile MCP client on the LAN | `bearer` is the default; `disabled` is an explicit operator choice to trust the network (full access, lowerable cap); outward actions always need a human approval; HTTPS in every mode; audit log; rate limits on agent-triggered renders |
| Credential leakage | Credentials encrypted at rest (§9), never returned by any route, passed per query, redacted in logs and audit |
| Malicious or changed plugin | Fetched at a pinned commit; reviewed part by part before enabling; command hooks refused; unknown tools default to `outward`; re-pinning shows a diff |
| Runaway agent | `maxTurns`, per-session budget, render rate limits, interrupt from any watcher |
| Headless browser used to click past an approval, or to reach other origins or files (#349) | Agent-actor marker refused on outward routes without an approved action; origin allow-list plus the backend check (the allow-list alone is not a boundary, §3.1); `browser_run_code_unsafe`, `browser_evaluate` and the file tools disallowed; isolated context per session; off by default (§5.3) |

## 9. Persistence and credentials

All AI state lives in the #241 database, in `ai_*` tables owned and migrated by the
agent service:

- Claude credentials (encrypted);
- MCP auth mode, tokens (hashed), and OIDC configuration;
- plugins: source, pinned commit, enabled parts, endpoint credentials (encrypted), and
  tier map;
- sessions (§6), MCP subscriptions, and the resumability event log;
- approvals of outward actions (§8.2, `ai_approvals`);
- the audit log.

There are **no AI-*configuration* env vars**: providers, credentials, auth mode,
tokens and plugins are all configured in Settings. Three **infrastructure bootstrap**
variables still reach the agent container, because Settings itself needs them to exist:
`SCADBUDDY_DATABASE_URL` (shared with #241), the backend URL (`SCADBUDDY_BACKEND_URL`,
named in PR #319), and the key-encryption key file below. The post-merge hardening of
#255 adds three more of the same kind, which Settings cannot hold because they decide
who may write to Settings or open its secrets: `SCADBUDDY_PUBLIC_URL` (shared with the
backend; the origin allowlist of §8.4), `SCADBUDDY_AGENT_TRUSTED_PROXIES` (the peers
whose `X-Forwarded-*` are believed, §8.4), and `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE`
(the old key during a rotation).

**Encryption at rest (recommended):** envelope encryption.

- Each secret is sealed with AES-256-GCM under a random per-row data key. The data key
  is sealed under a **key-encryption key read from a file**
  (`SCADBUDDY_SECRET_KEY_FILE`, mounted from a Kubernetes Secret in eh-homelab/clusters).
- That file is infrastructure, like the database URL; it is not AI configuration.
- Rotating it re-wraps the data keys only.
- Without the file, Settings refuses to save credentials and says why.

**The database is required** (#401). Until #401 lands, a ScadBuddy with
`SCADBUDDY_DATABASE_URL` unset still starts: AI features are disabled, and Settings
explains that they need the database.

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
   #349 headless browser follows #255 and #251 (it needs plugin loading and the tier and
   approval plumbing).
4. #254 browser bridge, #256 assistant panel, #300 sessions, #299 ScadBuddy plugin.
5. #252 authoring, #253 dependencies, #258 safety (grows with each tool), #267–#270
   realtime conversions.
6. #284 analyzers (after the print-workflow ("easy print") work, #84, §11), #297 plugins, #262 OIDC, #257 voice,
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
    refusal;
  - the Playwright plugin (#349) loads from its pinned local path, its tools register
    at the tiers in §5.3, and the disallowed tools are absent from the request.
- **backend (pytest):** event publishing per mutation route with a test listener; the
  new endpoints (#252, #253, #284) as usual; Bambuddy routes with respx recordings; the
  agent-actor marker middleware (#349): each outward route with the marker and no
  approved action gets `403`, with one gets through once, and without the marker behaves
  as today.
- **e2e (Playwright):** scripted sessions against the fake endpoint, as listed in #259.
- **Container e2e (#349), against the `agent` image:** the agent opens the customizer,
  sets a parameter and screenshots the preview; clicking *Print* in the headless browser
  without an approval is refused by the backend; navigating to an origin not on the
  allow-list is blocked; a page `fetch` to another origin is measured and recorded in
  §3.1 (§3.2); two sessions don't share storage.
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
[pw-plugin-mcp]: https://github.com/anthropics/claude-plugins-official/blob/main/external_plugins/playwright/.mcp.json
[pw-plugin-json]: https://github.com/anthropics/claude-plugins-official/blob/main/external_plugins/playwright/.claude-plugin/plugin.json
[pw-npm]: https://www.npmjs.com/package/@playwright/mcp/v/0.0.82
[pw-readme]: https://github.com/microsoft/playwright-mcp/blob/main/README.md
[pw-extra-headers]: https://playwright.dev/docs/api/class-browser#browser-new-context-option-extra-http-headers
