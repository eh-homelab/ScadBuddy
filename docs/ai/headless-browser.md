# The headless browser

The agent harness can drive ScadBuddy's own UI in a **headless Chromium inside the
agent container**, through the official `playwright` Claude plugin (issue
[#349](https://github.com/eh-homelab/ScadBuddy/issues/349), spec D11 and §5.3). It is
for sessions that have no user tab: sessions started over `/mcp` or by another agent
(#300), visual checks while authoring (#252, #253), and evals (#259). The
[browser bridge](browser-bridge.md) is still the only way to act in the user's own tab;
the headless browser never sees that tab.

It is **off by default**. Settings has a switch for it ("AI headless browser"), which
stores `headless_browser_enabled` through `PUT /api/v1/ai/settings/headless-browser`;
from the next turn on, every session turn gets the browser (`main.ts` gives the
`SessionManager` its `headlessBrowser` dependency).

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
| The setting, and its switch in Settings | [`agent/src/routes/headlessBrowser.ts`](../../agent/src/routes/headlessBrowser.ts) (`GET`/`PUT /api/v1/ai/settings/headless-browser`, `uiReadProblem`/`uiRequestProblem` guards); [`frontend/src/components/HeadlessBrowserSetting.tsx`](../../frontend/src/components/HeadlessBrowserSetting.tsx) |
| The backend's refusal of outward requests from it | [`backend/scadbuddy/api/agent_actor.py`](../../backend/scadbuddy/api/agent_actor.py) `AgentActorGate` |
| The one way past that refusal: an approved, one-shot grant | [`agent/src/harness/headlessGrants.ts`](../../agent/src/harness/headlessGrants.ts) (`mcp__scadbuddy_browser__authorize_request`), table `ai_headless_grants` ([migration](../../agent/src/db/migrations/20260928T0812Z_headless_grants.sql)), `GRANT_SQL` in `agent_actor.py` |

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
   the redirect half is measured (below), so:
3. **The redirect guard** (`redirectGuardSource()`, loaded on every page through the
   server's `browser.initPage`) routes every request itself: another origin is refused;
   a same-origin request is made with `maxRedirects: 0`, a 3xx off the origin is
   refused, a same-origin 3xx on a GET navigation becomes a new navigation (which the
   guard sees again), and any other 3xx is refused. No request and no redirect hop
   leaves the origin, whatever sits in front of the backend (measured, below).
4. **The backend's agent-actor gate.** Every request from the headless context carries
   `X-ScadBuddy-Agent-Session`. `AgentActorGate` lets such a request through for
   `GET`/`HEAD`/`OPTIONS`, and for the non-safe routes in `AGENT_ALLOWED_WRITES` (the
   read/write tools' routes that no outward tool shares; `agent/test/agentActor.test.ts`
   derives the same list from the tool registry and fails on drift). Everything else,
   i.e. send, print, delete, settings writes, library pins from a URL, and any route
   added later, gets `403 Needs approval`, unless a grant authorises exactly that
   request (next section).

## Approving one outward request

Spec §5.3: a marked outward request passes only when "an approved, unconsumed outward
action for that session authorises that request; the backend consumes it once". As
built:

1. The model clicks *Print* in the headless UI; the page's `POST
   /api/v1/print/outputs/<id>/run` gets `403`, whose detail names the next step.
2. The model calls **`mcp__scadbuddy_browser__authorize_request`** with that request's
   `method` and exact `path` (`browser_network_requests` shows them). The tool is
   `outward` tier (`browserTierOf`), so it **parks for a human approval** in the
   ScadBuddy UI like any outward call (#258, `approvals/service.ts`), bound to that
   exact input by its HMAC.
3. Once approved, the tool's handler (`recordGrant()`) finds the approval it ran under
   (same session and turn, same tool, same input hash, approved and consumed) and
   writes one row to `ai_headless_grants`: session, turn, approval, method, path, and
   an expiry two minutes out (`GRANT_TTL_SECONDS`).
4. The model clicks again. The backend's `GRANT_SQL` lets the request through only if a
   grant matches the marker's session, the method and the exact path, is unused and
   unexpired, its turn is still the session's live turn (`ai_sessions.turn_id`, lease
   not expired), and its approval is approved and consumed; it marks the grant used in
   the same statement. A third click is refused again.

So an interrupt, a handoff, a new turn or the end of the turn voids an unused grant.
Everything else fails closed: no database URL on the backend, the database down, or no
`ai_*` tables all mean `403`. The backend reads the agent's tables through its own
`SCADBUDDY_DATABASE_URL` (the shared #241 database; spec §3.2 named "a lookup in the
shared database" as one option). `agent/test/headlessGrants.pg.test.ts` runs the
backend's `GRANT_SQL` against the agent's real schema, and
`agent/test/headlessBrowser.session.e2e.test.ts` runs the whole flow in a real session
turn: real SDK and Claude Code, real Chromium, approvals in Postgres, and a stand-in UI
whose outward route answers with `GRANT_SQL` (refused, parked, approved, exactly one
request through, refused again).

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
  harness refuses it before that). **The allow-list alone follows a redirect off the
  origin**: the tool usually reports an interrupted navigation, but the other origin has
  already received the request, marker included. With the redirect guard (Guards, 3)
  it is refused and the other origin receives nothing, a same-origin redirect still
  lands, and a chain that stays on the origin for one hop and then leaves it is refused
  at the second hop. Two things measured on the way, which shaped the guard: handing a
  same-origin 3xx to the browser is not safe, since Chromium then follows it and every
  further hop without calling the route handler again; and a navigation must be refused
  with a page, not an abort, since after an aborted navigation the tab sits on
  `chrome-error://` and every later fulfilled navigation fails.
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
  available, sends the marker, and blocks off-origin navigation and an off-origin
  redirect.
- **Sandbox**: with `browserName` set and no `channel`, the server leaves
  `chromiumSandbox` false on Linux (its `validateBrowserConfig`), i.e. `--no-sandbox`,
  unless the config asks for it. See "Sandbox" below.
- **Image size**: the Chromium layer (headless shell plus its Debian libraries) is
  **603 MB** uncompressed, of which `/opt/pw-browsers` is 268 MB and the rest the
  libraries `--with-deps` installs; full Chromium would be 740 MB. `@playwright/mcp`
  and `playwright`/`playwright-core` add about 19 MB to `node_modules`.

## Sandbox

Chromium's sandbox needs unprivileged user namespaces. Measured in the `agent` image
(uid 10001, read-only root, `chromium_headless_shell-1246`): under Docker's default
seccomp profile it fails ("Chromium sandboxing failed!"), because that profile only
lets a process with `CAP_SYS_ADMIN` create namespaces; with
`--security-opt seccomp=unconfined` it starts and the renderer runs without
`--no-sandbox`. The agent probes this once, on the first turn that uses the browser
(`agent/src/harness/headlessSandbox.ts`), asks for the sandbox (`chromiumSandbox: true`)
when it works, and otherwise logs a warning and runs with `--no-sandbox`. To get the
sandbox in Kubernetes, the agent container needs a seccomp profile that allows
`clone`/`unshare` with `CLONE_NEWUSER` (`securityContext.seccompProfile.type:
Unconfined`, or a `Localhost` profile derived from the runtime default with those
allowed; [Kubernetes seccomp](https://kubernetes.io/docs/tutorials/security/seccomp/),
[Docker's default profile](https://docs.docker.com/engine/security/seccomp/)), and a node
with `user.max_user_namespaces` above 0 (and, on Ubuntu 23.10+, AppArmor not restricting
unprivileged user namespaces). No capability and no privileged container is needed.
`RuntimeDefault` keeps it off. Tracked in #543.

## Redirects: guarded in the browser, and none from the backend

The redirect guard (Guards, 3) is what stops an off-origin redirect, including one
added by an ingress, auth proxy or CDN in front of `SCADBUDDY_BACKEND_URL`. The backend
also serves none of its own, as defence in depth: `backend/tests/api/test_no_open_redirect.py` sends paths
shaped to provoke a redirect (`//evil.example/`, `/%2F%2Fevil.example/`, trailing
slashes, the SPA's directories) to the app with a built SPA mounted and asserts every
`Location` stays on the origin, and it fails when any backend module starts building a
redirect by hand until that redirect is reviewed. A proxy in front of the backend
that redirects off the origin (a login bounce, a canonical-host redirect) no longer
leaks anything, but it does make the headless browser useless there: its pages are
refused. In the pod layout of spec §4.1 `SCADBUDDY_BACKEND_URL` is
`http://127.0.0.1:<backend port>`, the backend alone, with no proxy in between.

## Status and what is not done

- **Chromium's sandbox depends on the pod** (see "Sandbox"): off under a
  `RuntimeDefault` seccomp profile.
- **Not tested against the real SPA and backend in one container run.** The pieces
  are tested: the gate and `GRANT_SQL` against Postgres (pytest and the agent's pg
  test), the marker on a page's own `fetch` (server test), and a real session turn
  driving a stand-in UI through the whole approve-and-click flow (session e2e test).
- **The model has to find the request's path itself** (the `403` detail and
  `browser_network_requests`); an approval card shows that method and path.

## Bumping `@playwright/mcp`

Change `agent/package.json` and `PLAYWRIGHT_MCP_VERSION` together (a unit test checks
they match), rebuild the image (the Chromium revision follows `playwright-core`), and
run the headless tests: the server test fails if the tool list changed, and the e2e
test re-measures loading, naming, the guards and the marker.
