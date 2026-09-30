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
- **Measured in #349** on `@playwright/mcp` 0.0.82, Claude Code 2.1.283 (SDK 0.3.283)
  and `chromium_headless_shell-1246`, by `agent/test/headlessBrowser.server.test.ts`
  (the server over stdio), `agent/test/headlessBrowser.e2e.test.ts` (the real SDK and
  CLI against the fake endpoint) and a run inside the `agent` image
  (`docs/ai/headless-browser.md`, "Measured"):
  - A local plugin's MCP server is named `plugin:<plugin>:<server>` and its tools
    `mcp__plugin_<plugin>_<server>__<tool>` (here `mcp__plugin_playwright_playwright__`).
    With `strictMcpConfig: true` Claude Code starts **no** plugin MCP server (the
    plugin is listed, no server or tool appears); with it off and `settingSources: []`,
    a `.mcp.json` in the cwd is still not loaded.
  - `disallowedTools` removes a plugin server's tools from the init message and the
    request; a call by name gets `No such tool available`. The server offers 25 core
    tools, `browser_run_code_unsafe`, `browser_evaluate`, `browser_file_upload` and
    `browser_drop` among them.
  - `network.allowedOrigins` blocks navigations, subresources and page `fetch` to other
    origins (`net::ERR_BLOCKED_BY_CLIENT`; the other origin receives nothing). A
    **redirect** off the origin is followed: the tool usually returns an error, but the
    other origin has received the request. Chromium follows a 3xx handed to it by a route
    handler, and every further hop, without calling the handler again; and after an
    aborted navigation every later fulfilled one fails. So the harness adds a redirect
    guard (`browser.initPage`) that makes each request with `maxRedirects: 0`, refuses
    off-origin hops (navigations with a 403 page), and turns a same-origin 3xx on a GET
    navigation into a new navigation; with it an off-origin redirect and an on→off chain
    reach nothing.
  - `browser.contextOptions.extraHTTPHeaders` from the config file reaches every request
    of the isolated context, and a page `fetch` that sets the same header gets the
    context's value, not its own.
  - `--isolated`: two servers do not share storage. Claude Code starts a plugin stdio
    server per process, i.e. per query, so a session gets a fresh browser every turn.
  - Files: unnamed output goes to `outputDir`; a named one resolves against the
    server's cwd (Claude Code's cwd); `../x` and absolute paths outside those roots are
    refused ("outside allowed roots").
  - Claude Code starts a plugin's stdio server with its own environment (so with the
    credential); `/usr/bin/env -i` in the server's `command` leaves only what is listed.
  - With `browserName: "chromium"` and no `channel`, the server launches Playwright's
    `chromium-headless-shell` and sets `chromiumSandbox: false` on Linux
    (`--no-sandbox`) unless asked. With `chromiumSandbox: true` it starts under
    `--security-opt seccomp=unconfined` and fails under Docker's default seccomp profile
    ("Chromium sandboxing failed!", user namespaces); the agent probes and asks for it
    where it works. It runs as uid 10001 with a read-only root, `/tmp` and the state
    directory on tmpfs, and no network, and reports WebGL available. Its profile goes
    under `TMPDIR`, whose path must stay short (the `SingletonSocket` Unix-socket
    limit): under the session directory the launch fails. Installed with
    `install-browser --with-deps --only-shell chromium`, the layer is 603 MB
    uncompressed (268 MB of browser); full Chromium is 740 MB.
  - How the backend matches a marked outward request to an approval (built in #349,
    after #258 merged): the model asks for one exact method and path with the outward
    tool `mcp__scadbuddy_browser__authorize_request`, which parks for approval like any
    outward call; the approved call writes a one-shot row to `ai_headless_grants`
    (session, turn, approval, method, path, two-minute expiry). The backend reads the
    shared database (`GRANT_SQL` in `backend/scadbuddy/api/agent_actor.py`) and uses a
    grant once, only while its turn is the session's live turn and its approval is
    approved and consumed. Tested against the agent's schema
    (`agent/test/headlessGrants.pg.test.ts`) and in a real session turn with Chromium
    (`agent/test/headlessBrowser.session.e2e.test.ts`).

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
- *Read 2026-09-28, for #268.* Bambuddy 1.2.5.5 has a push socket, `WS /api/v1/ws`. It
  takes a token minted by `POST /api/v1/auth/ws-token`, and an API key with
  `can_read_status` may mint one (bambuddy `v1.2.5.5`
  `backend/app/api/routes/websocket.py`, `backend/app/api/routes/auth.py`
  `mint_websocket_token`). It cannot stand in for reading a print's progress:
  - nothing is broadcast for slice jobs;
  - completion arrives as `print_complete` per *printer*, not per queue item;
  - `pipeline_run_updated` goes through `broadcast_to_user(run.created_by)`
    (`backend/app/core/websocket.py` `send_*`, `backend/app/api/routes/pipeline_runs.py`).

  So the print watcher polls with back-off (§7).

### 3.2 To verify (each item names who verifies it)

| Item | Where it matters | Verified by |
|---|---|---|
| Bambuddy 1.2.5.5 routes for the print archive (with outcome fields) and any stats endpoint, read off its `openapi.json` with respx recordings | #284, #264 | #251 |
| ~~Whether the #241 Postgres (CloudNativePG in eh-homelab/clusters) needs anything for `LISTEN/NOTIFY` across replicas~~ **Answered in #264:** only that every service connects to the primary. `LISTEN` and `NOTIFY` are refused on a hot standby ("`LISTEN`, `NOTIFY`" are among the commands not allowed, <https://www.postgresql.org/docs/current/hot-standby.html>), and CloudNativePG's `-rw` service "Points to the primary instance of the cluster" (<https://cloudnative-pg.io/docs/devel/service_management>), so `SCADBUDDY_DATABASE_URL` must name the `-rw` service, never `-ro` or `-r`. Any number of agent and backend replicas can then listen: a NOTIFY reaches every listening session (<https://www.postgresql.org/docs/current/sql-notify.html>). Measured on `postgres:17` by `agent/test/eventBus.pg.test.ts` | §7 | #264 |
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

The ingress routes `/mcp`, `/api/v1/ai/*` and `/.well-known/oauth-protected-resource`
(with its sub-paths; `oidc` mode's metadata, #262) to the agent container, and everything
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

As wired (#255, `agent/src/tools/harness.ts`): every session's queries get the harness
projection, bound to the session owner's principal, and a `tierOf` that maps
`mcp__scadbuddy__<name>` to each tool's `risk` for the permission seam (§8.1). An
outward call that the seam approved runs at once, because the harness projection tells
`runTool` it is past the gate (`gate: 'harness'`). Only `/mcp` calls take the
prepare/confirm path of §8.2. Measured on SDK 0.3.283: its in-process server validates
arguments with its own bundled zod 4.4.3, which refused any call that left out a
`.default()` field of our zod 4.6.5 ("expected nonoptional"). The harness projection
therefore offers such top-level fields as optional, with the same default in the JSON
Schema, and the tool's own schema applies the default (`agent/src/tools/projections.ts`
`sdkShape`; `agent/test/harnessWiring.test.ts`).

Tools are **task-shaped**, not one per route. For example, `render_model` submits a
render and streams progress until it settles, and `print_output` fills any omitted
choice the way the print dialog opens, then slices and queues behind a single approval.
(It wrapped eligibility → send → run until the spool-first print flow, #335, removed the
pipeline and eligibility routes; see `2026-09-27-spool-first-print-design.md` §7.)

As built for authoring (#252, `docs/ai/authoring.md`): `apply_patch` sends a unified
diff or search/replace edits against a `base` revision to
`POST /api/v1/models/{slug}/source/patch`, which answers 409 with the `current`
revision when the model has moved on, checked again under the history's write lock
(`update_source` takes the same optional `base`). Every backend call a tool makes
names the principal it runs as and, in a harness session, the session
(`agent/src/tools/authorship.ts`); the backend authors any commit that call makes as
"ScadBuddy agent" with both as git trailers (`backend/scadbuddy/core/authorship.py`),
and `list_versions` reports them as `agent`. `checkpoint` is a `read` tool that
answers the current revision to `restore_version` to; nothing new is stored.

### 5.2 Browser tools

The SDK runs tools in the service process ([custom tools][sdk-tools]), so a browser tool
is an ordinary registry tool whose handler forwards the call over the paired tab's
WebSocket and awaits the result, with a timeout. If no tab is paired it returns an error
("no browser attached"). The tab reports which handlers are live on each route change.
Unavailable handlers return an error instead of disappearing, so the session's tool list
stays stable. Details are in #254.

As built (#254; tab side PR #339, agent side its follow-up; `docs/ai/browser-bridge.md`):
every tool of the tab's catalogue (`frontend/src/agent/catalog.ts`) is a registry tool
`browser_<name>` (`agent/src/tools/browser.ts`), plus `browser_status` and
`browser_pair`. They are in `ALL_TOOLS`, so both projections serve them and the two lists
stay equal: the harness gets them bound to the turn's session, `/mcp` callers reach the
tab they paired. A tool is never below the tab's tier, and anything that moves or changes
the tab is at least `write` (`navigate` and `open_model` are raised from the tab's
`read`); `browser_open_print_dialog` is `outward`, so it is gated like any outward tool,
and the dialog's confirmation stays user-only in the tab. The tab opens
`GET /api/v1/ai/bridge` (`agent/src/routes/bridge.ts`, same gate as the chat socket) while
the assistant is available, says `hello` with its tab id (128 random bits per page load,
in memory only), route and live tools, and `state` on every change; the agent sends
`call`, the tab answers `result` from `AgentBridge.call()`. A call waits 30 s, or a
tool's own `timeout_ms` plus 10 s; with no tab it answers "no browser attached: …" with
the reason. Tabs are held per process: a call reaches a tab whose socket is on the same
replica, and otherwise says it is not connected (follow-up). `screenshot()` stays out,
as PR #339 decided.

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
  message. Bumping the version re-runs the #349 measurements in §3.1. As built
  (`agent/src/harness/headlessBrowser.ts`): the image carries the upstream manifest
  (pinned commit in `agent/plugins/playwright/README.md`), and the harness writes a
  copy per session whose `.mcp.json` starts the server under `/usr/bin/env -i`, so the
  credential in Claude Code's environment never reaches it. Queries with the plugin
  set `strictMcpConfig: false`, without which no plugin server starts (§3.1).
- **Locked to ScadBuddy's own origin.** The server starts with `--headless` and a
  `--config` file that sets `network.allowedOrigins` to the backend's origin
  (`SCADBUDDY_BACKEND_URL`, which serves the SPA) and nothing else. The README says the
  allow-list is not a security boundary and ignores redirects (§3.1), so it is not the
  only guard: page JavaScript coverage is in §3.2, and the approval rule below does not
  depend on it. `--allow-unrestricted-file-access` is never passed; `outputDir` is the
  session's scratch directory; `--caps` is left empty and `--no-webmcp` is set. The
  harness also refuses, before the server sees the call, any tool `url` off the origin
  and any `filename` with a directory part. Redirects off the origin would be followed
  (§3.1), so a redirect guard on every page refuses them, and the backend serves none.
- **Tools the model can't see.** `browser_run_code_unsafe` is RCE-equivalent in the
  agent container (§3.1), which D7 rules out, and `browser_evaluate`,
  `browser_file_upload` and `browser_drop` reach page JavaScript or the filesystem. All
  four go in `disallowedTools`, which removes a tool from the request
  ([permissions][sdk-permissions]), with `browser_install` (it downloads a browser);
  the exact names are in §3.1.
- **Isolated context per session.** `--isolated`, no `--user-data-dir`, no
  `--storage-state`: the profile stays in memory and dies with the session (§3.1). One
  server per query (§3.1), so two sessions never share cookies or a page, and each
  turn starts a fresh browser.
- **Off unless enabled.** A harness setting in the database (D4, §9), off by default,
  enables it; changing it is a settings write, so it needs approval (§8.3). With AI off
  it is hidden with the rest of the assistant. As built: `ai_settings` key
  `headless_browser_enabled`, set by `PUT /api/v1/ai/settings/headless-browser` behind
  the UI guard that stands for the browser user's approval (like the credential and
  plugin writes), from a user-only switch in Settings that is hidden when the agent or
  its database is not there.
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
  it removes is the headless path around §8.2. #349 measured that the header reaches
  every request and survives page JavaScript (§3.1), and built both halves
  (`AgentActorGate`: default deny for every non-safe route not on an allowlist of
  read/write routes, which the agent's tests keep equal to the tool registry's; and a
  one-shot grant, written only by the approved outward tool
  `mcp__scadbuddy_browser__authorize_request` for one exact method and path, used once
  while its turn is live, §3.1).

### 5.4 Resources

The `scadbuddy://` resources in #264 (models, sources, schemas, history, jobs, outputs,
print progress, Bambuddy printers, queue, inventory, history and stats, libraries,
settings, sessions, and the browser snapshot) use the same principal and tier checks as
tools.

Decided while building #264 (`agent/src/resources/`; MCP
[resources][mcp-resources], protocol 2025-11-25 as `@modelcontextprotocol/sdk` 1.30.1
implements it):

- **Every resource is backed by a `read` tool of the registry**
  (`catalog.ts` `RESOURCES`): reading `scadbuddy://models/{slug}/source` runs
  `get_source`. So there is one typed backend client, one argument validation and one
  redaction path, and the openapi coverage check needs no resource entries.
- **Tiers.** A resource needs its tool's tier (`read`), raised to `write` for
  `scadbuddy://settings`. `resources/list` and `resources/templates/list` leave out
  what the caller may not read; `resources/read` and `resources/subscribe` refuse it.
- **URIs** are RFC 6570 level-1 templates, one path segment per variable,
  percent-encoded (`builtin:x` is `builtin%3Ax`); subscriptions and notifications use
  that canonical spelling whichever one the client sent.
- **Errors** follow the resources page: `-32002` for an unknown URI or a backend 404,
  `-32602` for an argument the tool refuses, `-32603` otherwise.
- **Binary** content is a base64 `blob`; above the tools' inline cap (8 MiB,
  `tools/binary.ts`) the content is the same JSON note, with `application/json`.
- **Completion** (`completion/complete`) offers slugs, and commits and output ids
  for a slug given in `context.arguments`.
- **Not built in #264**, for want of a backend route or event source on `main`: the
  Bambuddy printers, queue, inventory, history and stats resources (print watcher,
  #268), the browser snapshot (#254), `scadbuddy://docs/authoring` (#252), and
  sessions (#300).

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

The event log used for MCP `Last-Event-ID` resumption belongs to the bus (#264). As
built (`agent/src/events/pgListener.ts`), the agent LISTENs on a dedicated connection
and keeps its place in the backend's `events` log by `seq`; when that connection drops
and comes back, it replays the rows after its place, skipping event ids it has already
delivered, and when the gap is larger than 1000 events or the log cannot be read it
tells every subscriber to resync (each subscribed URI gets `resources/updated`, plus one
`list_changed`). Resource notifications are coalesced to at most one per URI per 250 ms
per session (`agent/src/resources/hub.ts`). The UI socket does not replay: on every (re)subscribe the server confirms with
`subscribed` only once it is listening, and the client re-reads then, so a reconnect
cannot leave a gap.

Print progress comes from **one server-side watcher per active print** (#268,
`backend/scadbuddy/bambuddy/watcher.py`), not from one poll per open dialog. It reads with
back-off (2 s while the print moves, up to 30 s while it doesn't), because Bambuddy's push
socket can't replace the read (§3.1). When each print started is kept in Postgres
(`print_watches`, `backend/scadbuddy/migrations/20260928T0718Z_print_watches.sql`; nothing on disk), so the watcher resumes recent
prints after a restart; a settled print is forgotten. A session advisory lock per print
means one replica follows each print. Reading a print's progress re-arms its watcher,
and an open dialog reads at least every 30 s while the socket is up, so it never waits
on a watcher that is not there. With Postgres, `print.*` events cross replicas on the
event bus. #270 moves #241's render workers from interval polling to the
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
  the agent-actor marker unless an approved outward action authorises them (§5.3): a
  grant that only an approved `mcp__scadbuddy_browser__authorize_request` call writes,
  used once (§3.1). The headless browser stays off by default.

### 8.3 MCP auth modes

The mode is a database setting, changed in Settings, and changing it counts as a
settings write, so it needs approval.

As built (#255): two `ai_settings` keys, `mcp_auth_mode` (`"bearer"` or `"disabled"`;
unset means `bearer`) and `mcp_anonymous_cap` (`"read"`, `"write"` or `"outward"`; unset
means `outward`). `oidc` is on while the OIDC configuration (`mcp_oidc`, #262) is
enabled, and then wins over `mcp_auth_mode`, even over `"disabled"`; a stored `"oidc"`
without it reads as `bearer`. They are read on every `/mcp` request, so a change
applies on every replica without a restart. An unknown value fails closed, to `bearer`
or a `read` cap, and a failed read serves `bearer` with no verifiable token. The agent
logs a warning while the mode is `disabled`, once per change of the settings (the
banner is the UI's). The code is `agent/src/auth/authenticate.ts` `mcpAuthSettings`.
Settings changes them through `GET`/`PUT /api/v1/ai/mcp/auth`
(`agent/src/routes/mcpAuthMode.ts`, #251), behind the interim gate for settings writes
(`routes/guard.ts`) until approvals cover settings writes. Both keys change in one
transaction, as a compare-and-set against the values the page showed (`409` otherwise).
`PUT` does not set `oidc`; while OIDC is enabled `GET` reports `oidc` with the stored mode
beside it. The UI confirms before allowing calls without a token.

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
  switched to `oidc` until a discovery test against the issuer passes. As built
  (`agent/src/auth/oidc.ts`), following the revision the pinned MCP SDK implements
  ([2025-11-25][mcp-auth-2025-11-25]; `@modelcontextprotocol/sdk` 1.30.x,
  `LATEST_PROTOCOL_VERSION`):
  - **Discovery.** Protected Resource Metadata ([RFC 9728][rfc9728]) at
    `/.well-known/oauth-protected-resource` and at the path-inserted
    `/.well-known/oauth-protected-resource/mcp` (§3.1), naming the configured issuer
    as the only authorization server. Every 401 carries
    `WWW-Authenticate: Bearer realm="scadbuddy", resource_metadata="…"` (RFC 9728
    §5.1). In `bearer` and `disabled` mode neither is served nor named.
  - **Resource and audience.** The resource URI is the origin of `SCADBUDDY_PUBLIC_URL`
    plus `/mcp`, never the request's `Host`. A token's `aud` must contain it
    ([RFC 8707][rfc8707]), or an operator-set audience for IdPs that cannot put a URL
    there. Without a public URL, JWTs are refused and OIDC can't be enabled.
  - **Validation.** Signature by a key of the issuer's JWKS; `alg` on an allowlist
    (default `RS256`, `ES256`; `none` and HMAC can never be allowed) checked before
    any key is fetched; `iss` equal to the configured issuer; `exp` required, `nbf` and
    `iat` checked, 30 s leeway; `sub` required; `typ`, when present, `at+jwt` or `JWT`
    ([RFC 9068][rfc9068]). Errors per [RFC 6750][rfc6750] §3.1: `invalid_token` → 401,
    `insufficient_scope` → 403 naming the scopes. An IdP that can't be reached is 503,
    not 401. The token is never passed on: tools call the Python API without it.
  - **Fetching.** Issuer metadata (RFC 8414 then OIDC Discovery, in the MCP spec's
    order) and the JWKS go through the egress check of #255 on the connection itself
    (the socket's DNS lookup returns only the checked addresses), https only (http for
    loopback), no redirects, 512 KiB, 5 s. Cached 10 minutes; an unknown `kid` forces
    a JWKS refetch at most every 30 s; a failure is remembered 30 s.
  - **Scopes to tiers.** `scadbuddy:read|write|outward` by default, renameable, read
    from `scope`, `scp`, and optionally one more claim (e.g. `groups`). The highest
    granted tier includes those below it, as for bearer tokens. The principal is
    `oidc:<sub>`.
  - **Config** lives in `ai_settings` under `mcp_oidc` (issuer, audience, client ID,
    scope map, extra claim, algorithms, enabled), edited through
    `/api/v1/ai/mcp/oidc` and the Settings section "MCP sign-in (OIDC)". Saving with
    `enabled: true` runs the discovery test first and saves nothing if it fails.

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

As built (#254; `agent/src/bridge/`, `docs/ai/browser-bridge.md`):

- **Chat sessions.** The panel names its tab on every chat-socket connection
  (`tab.bind`), and each session it starts or sends to (or attaches to while it has no
  connected tab) is paired with that tab, in memory (`TabHub.pairSession`). The last tab
  the user sent from wins.
- **External agents.** The token is a short code in the style of the device
  authorization grant ([RFC 8628 §3.3][rfc8628]): `browser_pair` stores a pending row in
  `ai_browser_pairings` (migration `20260929T1330Z_browser_pairings.sql`) and returns
  the code once; only its SHA-256 is kept. Every connected tab shows the request, naming
  the MCP token that asked, and the user types the code into the tab the agent should
  drive. Typing it, rather than only clicking Allow, ties the acceptance to the agent
  the user is talking to. The code is single-use, lives 5 minutes, and allows 5 tries;
  requests are capped per principal (3) and overall (20). The accepted pairing binds the
  principal to that tab, one tab per principal (a partial unique index), until the user
  disconnects it in the tab, 8 hours pass, or the tab reloads (a new tab id). The prompt
  is user-only, so a paired agent's own `click`/`fill` cannot accept another.
- **Replicas.** Pairing rows are shared through Postgres, and every replica re-reads the
  pending requests for its tabs every 3 s, so the prompt appears wherever the tab is
  connected; a call itself reaches only a tab on the caller's replica (§5.2).

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
- sessions (§6);
- ~~MCP subscriptions, and the resumability event log~~ (decided in #264: these live
  with the MCP session, in memory on the replica that holds it. An MCP session is
  in-memory state, so after a restart its id answers 404 and the transport spec
  requires the client to start a new session ("When a client receives HTTP 404 in
  response to a request containing an `MCP-Session-Id`, it MUST start a new session",
  [Streamable HTTP][mcp-transport]), which re-subscribes. A durable copy would replay
  into a session that no longer exists. What is durable is the backend's `events`
  table, which covers the agent's own LISTEN gaps, §7);
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
  As built (#526): the harness does not load it yet. Every query runs with `tools: []`
  (§4.4), which leaves no `Skill` or `Agent` tool, so its skills and subagents would be
  listed but unusable; exposing them needs those tools and a tier for them (§8.1)
  first (`agent/test/harnessWiring.test.ts`).
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
[mcp-auth-2025-11-25]: https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization
[rfc9728]: https://www.rfc-editor.org/rfc/rfc9728
[rfc8707]: https://www.rfc-editor.org/rfc/rfc8707
[rfc6750]: https://www.rfc-editor.org/rfc/rfc6750
[rfc9068]: https://www.rfc-editor.org/rfc/rfc9068
[rfc8628]: https://www.rfc-editor.org/rfc/rfc8628#section-3.3
[mcp-resources]: https://modelcontextprotocol.io/specification/2025-11-25/server/resources
[mcp-transport]: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#session-management
[a2a]: https://github.com/a2aproject
[pw-plugin-mcp]: https://github.com/anthropics/claude-plugins-official/blob/main/external_plugins/playwright/.mcp.json
[pw-plugin-json]: https://github.com/anthropics/claude-plugins-official/blob/main/external_plugins/playwright/.claude-plugin/plugin.json
[pw-npm]: https://www.npmjs.com/package/@playwright/mcp/v/0.0.82
[pw-readme]: https://github.com/microsoft/playwright-mcp/blob/main/README.md
[pw-extra-headers]: https://playwright.dev/docs/api/class-browser#browser-new-context-option-extra-http-headers
