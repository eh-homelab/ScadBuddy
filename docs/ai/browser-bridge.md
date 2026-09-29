# The browser bridge

The browser tools of issue #254. They let an agent act *in the user's own tab*,
including inside the Bambuddy iframe, through the same state and `onChange` paths that
user input takes. The tab's side merged in PR #339; the agent's side (the `browser_*`
tools, the tab's socket to the agent, and pairing) is the follow-up PR for #254. The design is in spec §5.2 and §8.5 of
[`docs/superpowers/specs/2026-09-27-ai-integration-design.md`](../superpowers/specs/2026-09-27-ai-integration-design.md).
The code is in [`frontend/src/agent/`](../../frontend/src/agent/).

Sessions that have **no** user tab use a different tool: a headless Chromium in the
agent container, driven through the Playwright plugin (#349,
[headless-browser.md](headless-browser.md)). The two do not overlap: the headless
browser loads ScadBuddy's UI on its own and never sees the user's tab, and the bridge
stays the only way to act in that tab. The headless browser does not honour
`data-agent-user-only` (it clicks like a user would); what it cannot do is complete an
outward request, which the backend refuses for it (`AgentActorGate`).

> **Status.** The bridge has three callers: the agent service, over the tab's socket
> (below, [The agent's side](#the-agents-side-browser_-tools)); WebMCP, which is opt-in
> ([WebMCP opt-in](#webmcp-opt-in)); and `window.__scadbuddyBridge`, which exists in dev
> and mocked-e2e builds only (`installAgentBridge()`,
> [`frontend/src/agent/index.ts`](../../frontend/src/agent/index.ts)). The tab opens its
> socket only while the assistant is available (`useAiAvailability()`), so a production
> tab is driven only once the agent sidecar is deployed.

## Pieces

| File | Role |
|---|---|
| [`catalog.ts`](../../frontend/src/agent/catalog.ts) | `TOOLS`: every tool declared once, with its description, zod input schema, risk tier and scope. It is loaded lazily, so zod stays out of the entry chunk. |
| [`bridge.ts`](../../frontend/src/agent/bridge.ts) | `AgentBridge`: `register()`, `listTools()`, `call()` and `snapshot()`. `call()` validates arguments against the schema and runs the **newest** mounted handler. It always answers a typed result and never throws. |
| [`useAgentHandlers.ts`](../../frontend/src/agent/useAgentHandlers.ts) | `useAgentHandlers()`: a page registers its handlers for as long as it is mounted. |
| [`global.ts`](../../frontend/src/agent/global.ts) | The global tools (`navigate`, `snapshot`, and the `click`/`fill` fallbacks). |
| [`snapshot.ts`](../../frontend/src/agent/snapshot.ts) | `takeSnapshot()`: the route, dialogs, form values, errors, page state and interactive elements. |
| [`dom.ts`](../../frontend/src/agent/dom.ts) | Role and accessible-name lookup, `USER_ONLY` and `isUserOnly()`, and `setControlValue()`. |
| [`highlight.ts`](../../frontend/src/agent/highlight.ts) | `touch()`: scrolls to and briefly outlines what the agent touched. The pulse is dropped under `prefers-reduced-motion`. |
| [`webmcp.ts`](../../frontend/src/agent/webmcp.ts), [`webmcpPreference.ts`](../../frontend/src/agent/webmcpPreference.ts) | WebMCP registration and its per-browser opt-in. |
| [`types.ts`](../../frontend/src/agent/types.ts) | `Risk`, `Scope`, `ErrorCode`, `CallResult` and `AgentToolError`. |
| [`link.ts`](../../frontend/src/agent/link.ts), [`linkProtocol.ts`](../../frontend/src/agent/linkProtocol.ts) | `createTabLink()`: the tab's socket to the agent (`/api/v1/ai/bridge`) and its wire protocol. It runs each call through `bridge.call()`. |
| [`tabId.ts`](../../frontend/src/agent/tabId.ts) | `TAB_ID`: 128 random bits per page load, in memory only. |
| [`AgentLink.tsx`](../../frontend/src/components/AgentLink.tsx), [`PairingPrompt.tsx`](../../frontend/src/components/PairingPrompt.tsx) | The shell holds the link while the assistant is available and shows the pairing prompt. |
| agent [`bridge/hub.ts`](../../agent/src/bridge/hub.ts), [`bridge/protocol.ts`](../../agent/src/bridge/protocol.ts), [`routes/bridge.ts`](../../agent/src/routes/bridge.ts) | The agent's side: the connected tabs (`TabHub`), which tab a call goes to, the timeout, and the socket route. |
| agent [`bridge/pairings.ts`](../../agent/src/bridge/pairings.ts) | Pairing requests and pairings, in Postgres (`ai_browser_pairings`). |
| agent [`tools/browser.ts`](../../agent/src/tools/browser.ts) | The `browser_*` tools, in the registry of both projections. |

## The tools

From `TOOLS` in [`catalog.ts`](../../frontend/src/agent/catalog.ts). The tier follows
spec §8.1.

| Scope (where it is live) | Tool | Tier |
|---|---|---|
| global (any page) | `navigate`, `snapshot` | read |
| | `click`, `fill` (fallbacks) | write |
| catalogue (`/`) | `search`, `open_model` | read |
| customize (`/m/<slug>`) | `get_params`, `render` | read |
| | `set_param`, `set_params`, `reset_param`, `generate`, `select_plate` | write |
| | `open_print_dialog` | **outward** |
| source (`/m/<slug>/source`, `/new`) | `get_editor_text`, `get_problems` | read |
| | `replace_range` (not saved) | write |
| settings (`/settings`) | `get_form`, `test_connection` | read |
| | `set_field` (not saved; `SETTINGS_FIELDS` only, and never the API key) | write |

Notes, each from the tool's own entry:

- `open_print_dialog` only opens the Print or Send dialog. "The user reviews it and
  presses the confirmation themselves."
- `test_connection` is `read` although it reaches Bambuddy. `POST /settings/test` only
  runs `GET /printers/` with the stored settings, and the handler refuses while the form
  has unsaved changes (comment in `catalog.ts`; `test_settings` in
  `backend/scadbuddy/api/settings.py`).

**Stable tool list** (spec §5.2): the catalogue is the same on every route. A tool whose
page is not mounted answers `unavailable` instead of disappearing (`call()` in
`bridge.ts`). The error codes are `unknown_tool`, `unavailable`, `invalid_args`,
`refused`, `timeout` and `failed` (`ErrorCode`, `types.ts`).

## `data-agent-user-only`

Outward actions (spec §8.1: send, print, delete, settings or credential writes) must
be confirmed by a human (§8.2). In the tab, their confirmation controls carry the
`data-agent-user-only` attribute, spread from `USER_ONLY` in
[`dom.ts`](../../frontend/src/agent/dom.ts).

- `click` refuses any element inside a `[data-agent-user-only]` ancestor, with the code
  `refused`. The message says only the user can press it (`USER_ONLY_MESSAGE` in
  [`global.ts`](../../frontend/src/agent/global.ts)).
- `fill` refuses user-only fields and every `type="password"` input: "Credentials are
  for the user to type; an agent never fills them."
- `snapshot` never reads a password back. It reports `(typed, hidden)` or an empty
  string (`valueOf()` in `snapshot.ts`).
- The fallbacks search only inside an open modal dialog while one is up, as a user could
  only reach what is in it (`activeDialog()` and `findByRole()` in `dom.ts`).

Controls marked user-only on `main` (`grep -rn "{...USER_ONLY}" frontend/src`):

| Where | Control |
|---|---|
| [`SendDialog.tsx`](../../frontend/src/components/SendDialog.tsx) | Send confirmation |
| [`PrintPicker.tsx`](../../frontend/src/components/PrintPicker.tsx) | Print confirmation |
| [`ProjectPicker.tsx`](../../frontend/src/components/ProjectPicker.tsx) | Create project (in Bambuddy) |
| [`PresetPicker.tsx`](../../frontend/src/components/PresetPicker.tsx) | Delete preset |
| [`DeleteModelButton.tsx`](../../frontend/src/components/DeleteModelButton.tsx) | Delete model |
| [`HistoryPage.tsx`](../../frontend/src/pages/HistoryPage.tsx) | Delete output |
| [`SettingsPage.tsx`](../../frontend/src/pages/SettingsPage.tsx) | Test connection button, WebMCP toggle, Add to Bambuddy sidebar, Save changes |

**When you add an outward confirmation**, spread `{...USER_ONLY}` on it, or on a wrapper
around it. `isUserOnly()` uses `closest()`, so a wrapper covers everything inside.

This is a guard against the agent's own fallback tools, not a security boundary against
arbitrary script in the page. Any script running in the page can click anything. That
is why `window.__scadbuddyBridge` is compiled out of production bundles
(`installAgentBridge()`).

## WebMCP opt-in

[WebMCP](https://github.com/webmachinelearning/webmcp) lets an agent built into the
browser call a page's tools. `connectWebMcp()` in
[`webmcp.ts`](../../frontend/src/agent/webmcp.ts) works as follows.

- **API shape.** It feature-detects `document.modelContext.registerTool`, following the
  explainer's "Imperative Tool Registration" and the
  [draft spec](https://github.com/webmachinelearning/webmcp/blob/main/index.bs) as read
  on 2026-09-27. Issue #254 named `navigator.modelContext`; the current explainer puts it
  on `document`. Where the API is missing, WebMCP support does nothing.
- **Off by default.** Nothing is registered until the user turns on "Let this browser's
  built-in agent use ScadBuddy tools (WebMCP)" on the Settings page. The setting is per
  browser: `localStorage` key `scadbuddy.webmcp` = `on` (`WEBMCP_STORAGE_KEY`,
  [`webmcpPreference.ts`](../../frontend/src/agent/webmcpPreference.ts)). Storage that
  throws (private mode, a sandboxed iframe) reads as off. The reason (spec §8.5): an
  outside agent must pair before it drives a tab, and a browser's own agent is external
  to ScadBuddy.
- **What it registers.** Only the tools that are **live** on the current route. They are
  re-registered on every route change, and all are unregistered, by aborting their
  signal, when the toggle is turned off. Each call goes through `bridge.call()`, the same
  validation and error path as every other caller.
- **Annotations.** `readOnlyHint` is set for `read` tools and `consequentialHint` for
  `outward` tools.
- **The toggle is user-only**, so the `click` fallback cannot turn it on (PR #339
  commit "make WebMCP registration opt-in").
- **Inside Bambuddy's iframe it is expected to fail.** `registerTool` rejects with
  `NotAllowedError` in a cross-origin iframe without `allow="tools"`. Bambuddy's iframe
  sets `sandbox` and no `allow` ([`CLAUDE.md`](../../CLAUDE.md), "Bambuddy iframe
  facts"), so the rejection is ignored. This is from the `webmcp.ts` header, and has
  **not been measured** in a browser that ships WebMCP.

## The agent's side: `browser_*` tools

Spec §5.2: "The SDK runs tools in the service process ([custom
tools](https://code.claude.com/docs/en/agent-sdk/custom-tools)), so a browser tool is
an ordinary registry tool whose handler forwards the call over the paired tab's
WebSocket and awaits the result, with a timeout." That is
[`agent/src/tools/browser.ts`](../../agent/src/tools/browser.ts): one `browser_<name>`
tool per tool in `TOOLS`, plus two of its own. They are in `ALL_TOOLS`
([`tools/index.ts`](../../agent/src/tools/index.ts)), so both projections serve them:
in-process as `mcp__scadbuddy__browser_*`, and over `/mcp`. There are no browser-only
tools, so the two lists stay equal (`test/projections.test.ts`).

| Tool | Tier | Tab tool |
|---|---|---|
| `browser_status` | read | (the agent's own: attached or not, why not, the route and the live tools) |
| `browser_pair` | write | (the agent's own: ask the user to pair, [Pairing](#pairing-spec-85)) |
| `browser_snapshot`, `browser_get_params`, `browser_render`, `browser_get_editor_text`, `browser_get_problems`, `browser_get_form`, `browser_search`, `browser_test_connection` | read | same tier |
| `browser_navigate`, `browser_open_model` | **write** | `read` in the tab |
| `browser_click`, `browser_fill`, `browser_set_param`, `browser_set_params`, `browser_reset_param`, `browser_generate`, `browser_select_plate`, `browser_replace_range`, `browser_set_field` | write | same tier |
| `browser_open_print_dialog` | **outward** (gated) | same tier |

- **Tiers.** A `browser_*` tool is never below the tab's tier. Anything that moves or
  changes the user's tab is at least `write`, so `navigate` and `open_model` are raised
  from the tab's `read` (they change no data, but they change what the user is looking
  at). `open_print_dialog` is `outward` in both, so it stops at the approval gate of
  §8.2 like every outward tool: in a session it parks for the user's approval, and over
  `/mcp` it answers `pending_approval` until `confirm_action`. Even then it only opens
  the dialog. The Print and Send confirmations stay user-only in the tab, so the same
  human presses them, exactly as in the UI.
- **Arguments** are the tab's, with the same defaults. `test/browserTools.test.ts` loads
  the tab's own `catalog.ts` and fails if a tool, an argument or a default drifts, or a
  tier drops below the tab's.
- **Timeouts.** A call waits 30 s for the tab (`CALL_TIMEOUT_MS`,
  [`bridge/hub.ts`](../../agent/src/bridge/hub.ts)). A tool with its own `timeout_ms`
  (`render`, `generate`, `get_problems`) waits that long plus 10 s
  (`ROUND_TRIP_MARGIN_MS`). A call that times out says the tab "may still finish, so
  take a snapshot before trying again". A cancelled call (the MCP request's signal)
  stops waiting.
- **Errors.** The hub's own answers are ScadBuddy's words, returned as they are: `no
  browser attached: …` (with why: no tab for this session, no pairing for this caller,
  the paired tab is not connected, or no database for pairing), a timeout, a tab that
  disconnected mid-call, or more than 8 calls waiting on one tab (`MAX_CALLS_PER_TAB`).
  The tab's own errors (`unavailable`, `invalid_args`, `refused`, `failed`) come back
  as `the tab answered <tool> with <code>: …`, with the tab's message in the
  untrusted-data envelope (#258), because it can quote the page.
- **Results** are what `bridge.call()` returned, as JSON, in the untrusted-data
  envelope with the source "the user's open ScadBuddy tab". A result over 200 000
  characters is answered by the tab as a `failed` error that asks for less
  (`MAX_RESULT_CHARS`, [`link.ts`](../../frontend/src/agent/link.ts)), because the
  agent's sockets take frames up to 256 KiB (`main.ts`).
- **Not here:** `screenshot()`, which PR #339 left out of the tab on purpose; and the
  headless browser (#349, [headless-browser.md](headless-browser.md)), a separate
  Chromium whose Playwright tools are `mcp__plugin_playwright_playwright__browser_*`.

## The tab's socket

`GET /api/v1/ai/bridge` ([`agent/src/routes/bridge.ts`](../../agent/src/routes/bridge.ts)),
a WebSocket carrying one JSON object per text frame, version 1
([`agent/src/bridge/protocol.ts`](../../agent/src/bridge/protocol.ts) and the tab's
[`linkProtocol.ts`](../../frontend/src/agent/linkProtocol.ts)).

| From | Frame | Meaning |
|---|---|---|
| tab | `hello {tabId, route, live}` | First frame. Nothing else is taken before it. |
| tab | `state {route, live}` | The route or the live tools changed (issue #254: "The tab reports its live handlers on each route change"). |
| tab | `result {id, outcome}` | The answer to a `call`: `CallResult`, never a throw. |
| tab | `pairing.accept {id, code}`, `pairing.deny {id}`, `pairing.end {id}` | The user's answers in the pairing prompt. |
| agent | `call {id, tool, args}` | Run one tool of `TOOLS`. |
| agent | `pairings {pending, paired}` | The requests waiting (every tab sees them) and this tab's own pairings. Sent when they change. |
| agent | `pairing.result {id, ok, message}` | How an accept went ("That is not the code. 4 tries left."). |

- **Who may connect.** The upgrade passes the chat socket's gate (`uiRequestProblem`,
  [`routes/guard.ts`](../../agent/src/routes/guard.ts)): HTTPS through the trusted
  ingress, and an `Origin` on the UI's allowlist (spec §8.4). A socket that passes is
  the browser user's tab.
- **When the tab connects.** [`AgentLink.tsx`](../../frontend/src/components/AgentLink.tsx)
  holds the link while `useAiAvailability()` says the assistant is available, whether or
  not the chat panel is open, and closes it when it is not. The mocked build links only
  when a Playwright test sets `window.__scadbuddyTabLink`.
- **Reconnects.** The link reconnects with back-off (0.5 s doubling to 15 s) with the
  same tab id, so a pairing survives a dropped socket. A second socket with the same id
  replaces the first (close code 4000); calls waiting on the old one fail as
  "disconnected".
- **Tab id.** 128 random bits from `crypto.getRandomValues`, made once per page load and
  kept in memory only ([`tabId.ts`](../../frontend/src/agent/tabId.ts)).
  `sessionStorage` would survive a reload, but a duplicated tab copies it and would
  answer for the original. So a reload is a new tab, and an agent that paired by code
  must pair again.
- **Keep-alive.** The bridge socket is on the same `ws` server as the chat socket, so
  the 25 s ping of `startHeartbeat` (`routes/chat.ts`) covers it
  ([operating.md §1.1](operating.md#11-routing-the-uis-origin-to-the-agent)).

## Pairing (spec §8.5)

"The browser user's own chat sessions pair with their tab automatically. An external
agent needs a pairing token that the user accepts **in the tab**, in every auth mode."

- **Chat sessions.** The panel names its tab on every connection of the chat socket
  (`tab.bind`, sent first by `useAgentChat`). From then on, each session the panel
  starts or sends a message to is paired with that tab, and so is one it attaches to
  that has no connected tab yet (`ChatConnection` in
  [`routes/chat.ts`](../../agent/src/routes/chat.ts), `TabHub.pairSession`). The last
  tab the user sent from wins. Each turn's harness tools are bound to the session
  (`tools/harness.ts`), so a turn drives only its own session's tab. These pairings are
  in memory: they follow live sockets, which are this process's anyway.
- **External agents (MCP).** `browser_pair` stores a pending request and returns a code
  like `K7QP-2M9X`, once ([`bridge/pairings.ts`](../../agent/src/bridge/pairings.ts)).
  Every connected tab shows the request, naming the MCP token that asked
  ([`PairingPrompt.tsx`](../../frontend/src/components/PairingPrompt.tsx)). The user
  types the code into the tab the agent should drive and presses Allow. Typing the
  code, not just clicking, shows that the user is pairing the agent they are talking
  to, as in the device authorization grant
  ([RFC 8628 §3.3](https://www.rfc-editor.org/rfc/rfc8628#section-3.3)). That pairs the
  principal with that tab; its `browser_*` calls, over `/mcp` or from a session it
  owns, go there.
- **The code.** 8 symbols from a 30-letter alphabet without look-alikes (about 39 bits),
  from `crypto.randomInt`. Only its SHA-256 is stored, like the MCP bearer tokens. It
  can be accepted once, for 5 minutes (`REQUEST_TTL_MS`), and 5 wrong codes deny the
  request (`MAX_ATTEMPTS`). A principal may have 3 requests waiting and everyone 20
  (`MAX_PENDING_PER_PRINCIPAL`, `MAX_PENDING`), so a LAN client cannot fill the prompt.
- **The pairing.** One tab per principal: accepting a new pairing ends the old one in
  the same transaction, and a partial unique index enforces it
  ([`20260929T1330Z_browser_pairings.sql`](../../agent/src/db/migrations/20260929T1330Z_browser_pairings.sql)).
  It lasts until the user presses Disconnect in that tab, 8 hours pass
  (`PAIRED_TTL_MS`), or the tab reloads (a new tab id). Rows a day past their use are
  deleted on the next request.
- **Only the user.** The prompt is user-only (`{...USER_ONLY}` on the whole section), so
  a paired agent's `click` and `fill` cannot accept another agent, deny one, or keep
  itself paired. `disabled` MCP auth mode does not skip pairing.
- **The browser user** never pairs by code: `browser_pair` refuses in the user's own
  chat, which is already paired.

## Replicas

The tabs are held by the process their socket reached. A turn runs where its chat socket
started it, and `/mcp` sessions are already per replica (`mcp/http.ts`), so a call
reaches the tab only when the tab's bridge socket is on the same replica. Otherwise it
answers "the paired ScadBuddy tab is not connected (… or is connected to another agent
replica)". Pairing requests reach every tab whatever the replica: each replica re-reads
them from Postgres every 3 s (`PAIRINGS_POLL_MS`). Routing a call to another replica's
tab is follow-up work; one replica, or an ingress that keeps a browser on one replica,
has no gap.

## Inside Bambuddy's iframe

`e2e/agent-link.spec.ts` runs the same calls and the pairing prompt in the replica of
Bambuddy's frame that `e2e/downloads.spec.ts` uses (`sandbox="allow-scripts
allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"`, a different
host). The socket opens and the tools work there, and the prompt takes the code. The
tab id needs no storage, so a sandbox that refuses storage changes nothing. The socket
URL is the frame's own origin; that the frame's `Origin` passes the agent's allowlist
follows from `allow-same-origin`, as for the chat socket
([`CLAUDE.md`](../../CLAUDE.md), "Bambuddy iframe facts"), and is **not measured**
inside a live Bambuddy: in these tests Playwright routes the socket, so no `Origin`
reaches an agent.

## Testing

- Unit tests: [`bridge.test.tsx`](../../frontend/src/agent/bridge.test.tsx),
  [`global.test.tsx`](../../frontend/src/agent/global.test.tsx) and
  [`webmcp.test.tsx`](../../frontend/src/agent/webmcp.test.tsx).
- e2e: [`frontend/e2e/agent-bridge.spec.ts`](../../frontend/e2e/agent-bridge.spec.ts)
  drives `window.__scadbuddyBridge` in the msw-mocked bundle.
- The link and the prompt: [`link.test.ts`](../../frontend/src/agent/link.test.ts),
  [`PairingPrompt.test.tsx`](../../frontend/src/components/PairingPrompt.test.tsx) and
  [`AgentLink.test.tsx`](../../frontend/src/components/AgentLink.test.tsx).
- The agent's side without a socket:
  [`agent/test/browserTools.test.ts`](../../agent/test/browserTools.test.ts) (parity
  with `catalog.ts`, tiers, forwarding, "no browser attached", the timeout, a dropped
  or replaced tab, the harness and `/mcp` projections, `tab.bind`, and pairing by code).
  The Postgres store: [`bridgePairings.pg.test.ts`](../../agent/test/bridgePairings.pg.test.ts).
- The round trip in Node:
  [`agent/test/bridge.e2e.test.ts`](../../agent/test/bridge.e2e.test.ts) runs the tab's
  own `bridge.ts` and `link.ts` over a real socket against the agent's real server: an
  MCP client pairs by code and drives the tab, and a chat turn's
  `mcp__scadbuddy__browser_set_param` (the real SDK against the fake Anthropic
  endpoint) runs the tab's handler and hands its answer to the model.
- The DOM, in a real browser:
  [`frontend/e2e/agent-link.spec.ts`](../../frontend/e2e/agent-link.spec.ts)
  (Playwright plays the agent over the socket, at the top level and in the iframe) and
  [`frontend/e2e/agent-link.real.spec.ts`](../../frontend/e2e/agent-link.real.spec.ts)
  (a real agent service, `E2E_AGENT_BRIDGE_URL`; skipped without one, so not in CI):
  `/mcp` → agent → tab socket → DOM and back, at the top level and in the iframe.
