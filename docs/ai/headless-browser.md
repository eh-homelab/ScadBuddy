# A browser for the assistant: Playwright MCP as a plugin

ScadBuddy no longer ships a browser of its own. The built-in headless browser (#349: a
vendored `playwright` plugin, `@playwright/mcp` and Chromium in the agent image, the
`headless_browser_enabled` setting, the backend's agent-actor gate and its one-shot
grants) was removed. A session that needs a browser it can drive, apart from the
user's own tab, gets one from **Microsoft's Playwright MCP server, run as Microsoft
ships it and added like any other remote MCP plugin**.

The [browser bridge](browser-bridge.md) is unchanged: it is still the only way to act
in the user's own tab (`mcp__scadbuddy__browser_*`).

## Adding it

The server is deployed beside ScadBuddy, not by it, **in its own pod**:
eh-homelab/clusters#2174 runs `mcr.microsoft.com/playwright/mcp` as its own Deployment
and Service, with its Streamable HTTP endpoint at
`http://playwright-mcp.bambuddy:8931/mcp`. Then, as an admin, in **Settings → Plugins →
Add endpoint**:

- **Name** `playwright`. Its tools reach a classic turn as `mcp__playwright__browser_*`
  and a durable turn as `playwright__browser_*`.
- **URL** `http://playwright-mcp.bambuddy:8931/mcp`. A plugin URL must be `https`,
  except to loopback or a private address (an in-cluster Service), where plain `http`
  is accepted.

**Never run it as a sidecar** in the pod of the backend or the agent, or anywhere else
that shares their network. A browser on that pod's loopback reaches the backend
(`:8080`) and the agent (`:8081`) with no sign-in, so a page it opens could take
ScadBuddy's own outward actions (print, send to Bambuddy) with no approval.

Nothing else in ScadBuddy knows about it: its tools are tiered, disabled and approved
exactly as any plugin's (`agent/src/plugins/registry.ts`).

## Tiers

A plugin tool with no tier of its own is **`outward`**: every call parks for a human's
approval until it is tiered. So once the plugin is added, set its tiers in Settings →
Plugins. A starting point:

| Tier | Tools |
|---|---|
| `read` | `browser_snapshot`, `browser_take_screenshot`, `browser_console_messages`, `browser_network_requests`, `browser_wait_for` |
| `write` | `browser_resize`, `browser_close` |
| `outward` (the default) | `browser_navigate`, `browser_navigate_back`, `browser_tabs`, `browser_click`, `browser_type`, `browser_fill_form`, `browser_select_option`, `browser_press_key`, `browser_hover`, `browser_drag`, `browser_handle_dialog` |
| `outward`, or disabled | `browser_evaluate` and `browser_run_code_unsafe` (they run JavaScript), `browser_file_upload`, anything the server adds later |

Navigation and input stay `outward`: they decide where the browser goes and what it
does there, and nothing in ScadBuddy tells the browser's requests from a person's. A
navigation's own tier is then decided by the page it opens (next section), so leave
`browser_navigate` at `outward`.
Lower them to `write` only when the server **cannot reach ScadBuddy itself** (next
section). `browser_tabs` both lists and opens tabs, so it cannot be `read` as a whole.

## Where it may go: the allow and deny lists (#982)

Every navigation a session's browser is asked to make is judged by two lists the user
keeps in **Settings → Assistant → AI headless browser** (`ai_settings` key
`browser_origins`, routes `agent/src/routes/browserOrigins.ts`, the rules
`agent/src/browser/origins.ts`). It applies to any remote plugin's `browser_*` call that
names a `url` (Playwright MCP's `browser_navigate`), in classic and durable turns alike
(`agent/src/browser/guard.ts`):

| The page's origin | What happens |
|---|---|
| matches a **deny** rule | refused, even if someone would approve it; the refusal names the rule. Deny wins over allow. |
| matches an **allow** rule, or was approved **for this chat** | opens without asking, whatever the tool's tier. |
| on neither list | an outward call: it parks for the user. The card shows the full URL and offers **Allow once**, **Allow for this chat** (the origin is recorded for the session, `ai_browser_session_origins`) and **Always allow** (the origin is added to the allow list, a settings write the user makes from the card). **Deny** refuses it. |

Rules are one per line: a host (`makerworld.com`, https on its default port only on the
allow list), `*.example.com` (its subdomains, never itself), an exact origin
(`http://printer.lan:8080`), or an address range (`10.0.0.0/8`, for IP-literal URLs). A
host or wildcard on the deny list matches every scheme and port. Hosts match whole
labels, so `makerworld.com` never lets `makerworld.com.evil.example` through. A URL is
parsed once, with the WHATWG parser, and judged only when it is already written as that
parser (and the browser) writes it: whitespace, control characters, backslashes, user
names, upper-case or percent-encoded hosts, numeric addresses and other schemes are
refused, with the form to send instead, so the guard and the browser never read one URL
two ways. The
defaults allow a few model sites and references (MakerWorld, Printables, Thingiverse,
GitHub, OpenSCAD, the OpenSCAD wikibook) and deny loopback, link-local and cloud
metadata addresses, and the `internal`, `svc` and `cluster.local` names and their subdomains. A LAN address
is not denied by default: it asks.

**Pages the browser reaches on its own.** A redirect, a link it clicks, or a new tab
can take the browser somewhere no navigation named. Playwright MCP reports the page it is
on in each result (`- Page URL: …`); a result whose page is denied, or on neither list
and not approved in the session, is replaced before the model reads it (and in the
transcript) by a note saying where the browser is, so the page's content never reaches
the model. Each result is judged only by what it says itself: nothing about the
browser's page is remembered between calls, so an error, a timeout, a tab switch, a popup
or a back or forward navigation never leaves a stale page trusted. Playwright MCP names
the page only when the tool shows a snapshot or a tab changed since its last result
(measured in the `playwright-core` that `@playwright/mcp` 0.0.82 ships,
`Response._build`), so a result that names none is withheld: in practice
`browser_take_screenshot`, `browser_console_messages` and `browser_network_requests` reach
the model only when they report the page, and the agent reads a page with
`browser_snapshot`, which always names it. `browser_close`, `browser_resize` and
`browser_install` show no page and pass. When more than one tab is open, Playwright MCP
lists them all with their titles (`### Open tabs`), so every tab in the list must be
readable, or the result is withheld until the agent closes the others; a tab line that
cannot be read unambiguously (a `](` or a parenthesis in it) withholds it too. A blank tab
(`about:blank`) is read only while it is the browser's one tab: a page in another tab can
write into a tab it opened. A browser tool's error is screened the same way:
the forwarder passes it to Claude Code as the tool's result, since Claude Code hands an
MCP error to the model through `PostToolUseFailure`, which no hook can replace (measured,
`agent/test/browserOrigins.e2e.test.ts`). Both turns screen the same text, the whole
result (`browser/origins.ts` `screenedText`). A check or screen that fails or takes longer than 20 s refuses the call or
withholds the result; a deny rule that no longer parses refuses every page; a stored list
that cannot be read leaves no allow list (everything asks) and the default deny list. The
allow list lowers only `browser_navigate`'s tier: another browser tool that names a `url`
can be refused or made to ask by it, never allowed. A classic turn does that with a `PostToolUse` hook's `updatedMCPToolOutput`
(measured on Claude Code 2.1.296, `agent/test/browserOrigins.e2e.test.ts`), a durable
turn in its call activity (`agent/src/temporal/turnTools.ts`). To read such a page the
agent navigates to it, which asks. A navigation approved once is recorded as `once`, so
the pages of that origin stay readable in the chat while the next navigation there asks
again. A flow run has no chat to remember it in: only the approved call's own result is
shown.

Settings lists the origins approved in each chat, with **Revoke**. An approval or a
revocation is written only to a chat the user controls and that is not archived, as the
chat's other writes are. Page content still
arrives as untrusted data: the forwarder wraps every plugin result in the envelope
(`agent/src/plugins/forwarder.ts`).

**The lists are not the network boundary.** The browser runs in its own pod
(Microsoft's server, run as shipped), and the agent reaches it only over MCP: it cannot
route or abort the browser's own requests (`context.route`, CDP `Fetch`), so it never
sees a redirect hop, a subframe, a popup's `window.open`, or a page's fetches as they
happen. What the agent enforces is what the model asks for and what the model reads: a
navigation is refused or parked before it is sent, and every result is withheld unless
the pages and tabs Playwright MCP reports in it are readable, judged deny-by-default
(an unknown scheme, a URL not written as the browser writes it, or a result whose
structure cannot be read unambiguously is withheld). What the browser can reach, and so
what a page can make it request, is decided by the egress policy of the pod it runs in
(next section). Playwright MCP's own `--allowed-origins` / `--blocked-origins` run inside
the browser but do not apply to redirects (measured on `@playwright/mcp` 0.0.82), so they
are not that boundary either.

The lists judge names, not addresses: a permitted name that resolves to a private
address is not caught by them. A page that moves itself while Playwright MCP is writing
one result, after it captured the snapshot and before it read the page's address, is not
seen: the result names where the page landed. Nor do they judge a page's subresources and frames: an
allowed page's iframe from elsewhere shows in its snapshot. The boundary for that stays the network the server runs
in (next section).

## What the server can reach

The browser runs where the server runs and reaches whatever that network position
reaches. ScadBuddy no longer adds a marker to its requests or refuses them on the
backend, so the boundary is the network where it is deployed, not its own
`--allowed-origins` / `--blocked-origins`: those do not apply to redirects (measured on
`@playwright/mcp` 0.0.82), which is also why the backend still serves no off-origin
redirect (`backend/tests/api/test_no_open_redirect.py`).

eh-homelab/clusters#2174's deployment is that boundary: its pod's egress policy allows
DNS and the public internet only (no 10/8, 172.16/12, 192.168/16, 100.64/10, loopback
or link-local), so it reaches no pod, Service or LAN host, ScadBuddy included. Its
ingress policy admits only ScadBuddy's pods. ScadBuddy's public URL is behind SSO,
which the browser has no way through. Only with all of that in place is `write` for
navigation and input reasonable.

## One browser per MCP session

Playwright MCP with `--isolated` gives each MCP session a fresh browser context.

- **Classic turn**: one Claude Code process per turn, which connects to each plugin once
  and keeps that MCP session for the turn. A page opened by `browser_navigate` is
  still there for the next call in the same turn, and gone in the next turn.
- **Durable turn**: the session keeps one MCP session per plugin across its calls and
  its turns (#2581), so a page opened by `browser_navigate` is still there for the
  next call, and the next turn, as it would be for a person using Playwright MCP from
  Claude Code.

How a durable session keeps it (`agent/src/temporal/sessionClients.ts`):

- The agent replica that lists a turn's tools holds one connected MCP client per
  (durable session, plugin, plugin config). The turn's listing and every call use it.
  It has its own forwarder route, so the route outlives the activity that opened it.
- Every remote plugin's calls are pinned to that replica's own task queue
  (`agent-tools.<host>`), the mechanism `http_request` already uses. The next turn is
  listed on that queue first, so the pin carries over. If that replica does not pick
  the listing up within 5 s, it is gone: the turn is listed anywhere, and its plugins
  start new MCP sessions there. The browser state is lost; nothing fails.
- A client is ended with the session `DELETE` (`terminateSession`), then closed:
  when the durable session ends (`turn_tools_close`, run on the pinned replica), after
  10 minutes unused (`CLIENT_IDLE_MS`), when the plugin's config changes or it is
  disabled or removed, when the replica holds more than 64 (`MAX_KEPT_CLIENTS`, least
  recently used first), and when the agent stops. A Playwright MCP browser context
  therefore lives as long as the durable session uses it, and no longer.
- If the server no longer knows the session (a 404 on its id, after a Playwright MCP
  restart for example), or the request could not be sent at all, the call never ran:
  the agent opens a new MCP session and runs it once more. Its result starts with a
  note that the MCP session was reset, so earlier state (the open page) is gone. Any
  other broken connection may have run the call, so it is never retried: the call
  fails, saying the session was reset and the call not retried, and the next call
  starts a new MCP session.
- A flow run's calls, and a tier check on a replica that holds no client for the
  session, use a client for that one use and end it with its `DELETE`.
