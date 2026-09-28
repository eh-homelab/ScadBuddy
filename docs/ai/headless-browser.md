# The headless browser

The agent harness can drive ScadBuddy's own UI in a **headless Chromium inside the
agent container**, through the official `playwright` Claude plugin (issue
[#349](https://github.com/eh-homelab/ScadBuddy/issues/349), spec D11 and §5.3). It is
for sessions that have no user tab: sessions started over `/mcp` or by another agent
(#300), visual checks while authoring (#252, #253), and evals (#259). The
[browser bridge](browser-bridge.md) is still the only way to act in the user's own tab;
the headless browser never sees that tab.

It is **off by default**, and nothing in `main.ts` turns it on yet (the session manager
is not wired into the service; see "Status" below).

## Pieces

| What | Where |
|---|---|
| The pinned server, `@playwright/mcp` **0.0.82** (Apache-2.0, [npm](https://www.npmjs.com/package/@playwright/mcp/v/0.0.82), [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp)) | exact dependency in [`agent/package.json`](../../agent/package.json) |
| The official plugin's manifest, vendored byte for byte from [`anthropics/claude-plugins-official` at `fa59bc9`](https://github.com/anthropics/claude-plugins-official/tree/fa59bc9037741ecfa131aa27938272605710d7b2/external_plugins/playwright) (Apache-2.0, the repository's root `LICENSE`) | [`agent/plugins/playwright/`](../../agent/plugins/playwright/README.md) |
| Per-session plugin copy, server config, tier map, disallowed tools, input guard | [`agent/src/harness/headlessBrowser.ts`](../../agent/src/harness/headlessBrowser.ts) |
| Loading it into a query | `buildHarness()` in [`agent/src/harness/run.ts`](../../agent/src/harness/run.ts) (`HarnessRun.headlessBrowser`) |
| Turning it on per turn | `runTurn()` in [`agent/src/sessions/manager.ts`](../../agent/src/sessions/manager.ts): `deps.headlessBrowser` **and** the `ai_settings` key `headless_browser_enabled` = `true` |
| Its directory | `sessionBrowserDir()` in [`agent/src/harness/stateDirs.ts`](../../agent/src/harness/stateDirs.ts): `<state dir>/browser/<session id>` |
| Chromium | the Dockerfile's `agent` stage: `install-browser --with-deps --only-shell chromium`, `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers` |
| The backend's refusal of outward requests from it | [`backend/scadbuddy/api/agent_actor.py`](../../backend/scadbuddy/api/agent_actor.py) `AgentActorGate` |

## How a turn gets it

The upstream plugin's `.mcp.json` is `{"playwright": {"command": "npx", "args":
["@playwright/mcp@latest"]}}`, which downloads an unpinned package at runtime. Spec D11
rejects that, so it is not vendored. Instead, for each turn with the browser on,
`materializeHeadlessBrowser()` writes into the session's browser directory:

- `plugin/.claude-plugin/plugin.json`, the vendored manifest;
- `plugin/.mcp.json`, one stdio server that runs
  `/usr/bin/env -i HOME=… TMPDIR=… PLAYWRIGHT_BROWSERS_PATH=… node <pinned cli.js>
  --config playwright-mcp.json --headless --isolated --no-webmcp --block-service-workers`;
- `playwright-mcp.json`, the server config:
  - `browser.browserName: chromium`, `isolated: true`, `launchOptions.headless: true`;
  - `contextOptions.extraHTTPHeaders: { "X-ScadBuddy-Agent-Session": <session id> }`
    (the agent-actor marker);
  - `acceptDownloads: false`, `serviceWorkers: "block"`;
  - `network.allowedOrigins: [<origin of SCADBUDDY_BACKEND_URL>]`;
  - `outputDir: <browser dir>/output`, `allowUnrestrictedFileAccess: false`,
    `webmcp: false`, no `capabilities` (the core tool set only).

`assertHeadlessPlugin()` then checks the written `.mcp.json` (one server, under
`env -i`, the pinned `cli.js`, the required flags, none of the forbidden ones) and
`buildHarness()` loads it as `plugins: [{ type: "local", path }]`, sets
`disallowedTools` and, for that query only, `strictMcpConfig: false` (see below).

Why `env -i`: Claude Code starts a plugin's MCP server with its own environment, which
holds the Claude credential (`run.ts` `credentialEnv`). That is why
[`plugins.ts`](../../agent/src/harness/plugins.ts) refuses every stdio server in other
plugins. This one is built by the harness, not read from a plugin, and `env -i` means
the server and Chromium start with `HOME`, `TMPDIR` and `PLAYWRIGHT_BROWSERS_PATH` only
(measured: `/proc/<pid>/environ` in the tests and in the image).

## Tools and tiers

The server's 25 core tools, minus five, each with an explicit tier
(`BROWSER_TOOL_TIERS`), so the spec §8.1 default of `outward` never applies to them:

| Tier | Tools |
|---|---|
| `read` | `browser_navigate`, `browser_navigate_back`, `browser_snapshot`, `browser_take_screenshot`, `browser_console_messages`, `browser_network_requests`, `browser_network_request`, `browser_wait_for`, `browser_find` |
| `write` | `browser_click`, `browser_type`, `browser_fill_form`, `browser_select_option`, `browser_press_key`, `browser_hover`, `browser_drag`, `browser_handle_dialog`, `browser_resize`, `browser_emulate_media`, `browser_close`, `browser_tabs` |
| removed (`disallowedTools`) | `browser_run_code_unsafe` (RCE-equivalent in the agent container), `browser_evaluate` (page JavaScript), `browser_file_upload` and `browser_drop` (absolute file paths), `browser_install` (downloads a browser) |

`browser_tabs` lists tabs but also opens them at a URL, so it is `write` as a whole.
A tool a later server version adds is not in the map, so it is `outward` and denied
until someone reviews it. The action feed shows the same tiers (`SdkEventMapper` is
given the browser tiers in `manager.ts`).

The SDK names them `mcp__plugin_playwright_playwright__<tool>` (`TOOL_PREFIX`,
measured on Claude Code 2.1.283).

## Guards

In order, from the model outwards:

1. **The input guard** (`browserInputProblem()`, run in both the `PreToolUse` hook and
   `canUseTool`, see `permissions.ts` `InputGuard`) denies, at any tier:
   - a `url` (`browser_navigate`, `browser_tabs`) whose origin is not the backend's,
     compared with `normaliseOrigin()` from
     [`agent/src/http/origins.ts`](../../agent/src/http/origins.ts). `file:`,
     `data:`, `javascript:`, other ports, hosts and schemes, and
     `http://backend@evil.example/` are all refused;
   - a `filename` that is not a plain file name (no directory part, no leading dot).
2. **The server's allow-list** (`network.allowedOrigins`): a navigation, subresource or
   page `fetch` to another origin fails with `net::ERR_BLOCKED_BY_CLIENT`. The README
   says it "does not serve as a security boundary and does not affect redirects", and
   the redirect half is measured (below).
3. **The backend's agent-actor gate.** Every request from the headless context carries
   `X-ScadBuddy-Agent-Session`. `AgentActorGate` lets such a request through for
   `GET`/`HEAD`/`OPTIONS`, and for the non-safe routes in `AGENT_ALLOWED_WRITES` (the
   read/write tools' routes that no outward tool shares; `agent/test/agentActor.test.ts`
   derives the same list from the tool registry and fails on drift). Everything else,
   i.e. send, print, delete, settings writes, library pins from a URL, and any route
   added later, gets `403 Needs approval`. Spec §5.3 lets an approved, unconsumed
   outward action authorise one such request; approvals are #258 (open PR #471), so
   today the refusal is unconditional.

## Measured (the tests, and the image)

On `@playwright/mcp` 0.0.82 with Claude Code 2.1.283. The tests are
[`agent/test/headlessBrowser.server.test.ts`](../../agent/test/headlessBrowser.server.test.ts)
(the server over stdio, no model) and
[`agent/test/headlessBrowser.e2e.test.ts`](../../agent/test/headlessBrowser.e2e.test.ts)
(the real SDK and Claude Code, a scripted model on the local fake Anthropic endpoint,
a real Chromium); both skip without a Chromium. ci.yml's agent job installs the pinned
headless shell before the tests.

- **Plugin MCP servers need `strictMcpConfig: false`.** With it `true`, the init message
  lists the plugin but starts no server and no tool appears. The harness turns it off
  only for queries with the browser. `settingSources: []` still keeps settings-file MCP
  configs out: the e2e test plants a `.mcp.json` in the session's cwd and asserts only
  `plugin:playwright:playwright` is connected.
- **`disallowedTools` removes plugin tools** exactly as it removes built-ins: the four
  (five) are absent from the init message and the API request, and a model that names
  one gets `No such tool available`.
- **The marker reaches every request**, including a page `fetch` that sets the same
  header itself: the context's value arrives, not the page's.
- **Page JavaScript cannot reach another origin**: a `fetch` to it never arrives.
- **A direct navigation off the origin** fails with `net::ERR_BLOCKED_BY_CLIENT` (and the
  harness refuses it before that). **A redirect off the origin is followed**: the
  tool usually reports an interrupted navigation, but the other origin has already
  received the request, marker included. ScadBuddy's origin must therefore not serve
  open redirects.
- **Isolation**: two servers (two sessions) do not share `localStorage`. A server lives
  as long as its Claude Code process, which is **one query**, so a session's second turn
  starts with a fresh browser (and has to navigate again).
- **Files**: unnamed output goes to `outputDir`; a named one to the server's workspace
  root, which is the session's cwd (Claude Code starts the server there). The server
  refuses `../x` and absolute paths outside its roots ("File access denied … outside
  allowed roots"); the harness refuses them first. The browser directory (config,
  plugin copy) is outside the cwd, so no named file can overwrite them.
- **`TMPDIR` must be short.** Chromium's profile goes under it and its `SingletonSocket`
  is a Unix socket (path limit about 107 bytes); under the session directory the launch
  fails with "Target page, context or browser has been closed". It is the service's own
  `TMPDIR` (`/tmp` in the image).
- **In the image** (`docker build --target agent`, then run as uid 10001 with
  `--read-only --network none --tmpfs /tmp --tmpfs /var/lib/scadbuddy-agent`):
  `chromium_headless_shell-1246` launches, types, clicks, screenshots, reports WebGL
  available, sends the marker, and blocks off-origin navigation.
- **Sandbox**: with `browserName` set and no `channel`, the server leaves
  `chromiumSandbox` false on Linux (its `validateBrowserConfig`), so Chromium runs with
  `--no-sandbox`, which is Playwright's default for Chromium. Enabling it needs user
  namespaces in the pod; not measured.
- **Image size**: the Chromium layer (headless shell plus its Debian libraries) is
  **603 MB** uncompressed, of which `/opt/pw-browsers` is 268 MB and the rest the
  libraries `--with-deps` installs; full Chromium would be 740 MB. `@playwright/mcp`
  and `playwright`/`playwright-core` add about 19 MB to `node_modules`.

## Status and what is not done

- **Not reachable in production yet.** `SessionManager` is not constructed in
  `main.ts`, so nothing passes `deps.headlessBrowser`; and there is no route to set
  `headless_browser_enabled` (changing it is a settings write, which needs an approval,
  spec §5.3, §8.3).
- **Approvals (#258, PR #471).** When they land, `AgentActorGate` must look up an
  approved, unconsumed outward action for the marker's session and consume it once
  (spec §5.3). Until then it refuses, which is the safe direction.
- **Redirects** are not blocked by anything but the absence of open redirects on the
  backend's origin.
- **Not tested end to end against the real SPA and backend in one container run.** The
  pieces are tested separately: the backend gate against the real app (pytest), the
  marker on a page's own `fetch` (server test), and the harness driving a stand-in UI
  (e2e test).

## Bumping `@playwright/mcp`

Change `agent/package.json` and `PLAYWRIGHT_MCP_VERSION` together (a unit test checks
they match), rebuild the image (the Chromium revision follows `playwright-core`), and
run the headless tests: the server test fails if the tool list changed, and the e2e
test re-measures loading, naming, the guards and the marker.
