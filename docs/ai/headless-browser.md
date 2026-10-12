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

The server is deployed beside ScadBuddy, not by it: eh-homelab/clusters#2174 runs
`mcr.microsoft.com/playwright/mcp` with its Streamable HTTP endpoint at `/mcp`. Then,
as an admin, in **Settings → Plugins → Add endpoint**:

- **Name** `playwright`. Its tools reach a classic turn as `mcp__playwright__browser_*`
  and a durable turn as `playwright__browser_*`.
- **URL** the server's `/mcp` endpoint. A plugin URL must be `https`, except on
  loopback (`127.0.0.1`, `::1`, `localhost`), where plain `http` is accepted.

Nothing else in ScadBuddy knows about it: its tools are tiered, disabled and approved
exactly as any plugin's (`agent/src/plugins/registry.ts`).

## Tiers

A plugin tool with no tier of its own is **`outward`**: every call parks for a human's
approval until it is tiered. So once the plugin is added, set its tiers in Settings →
Plugins. A starting point:

| Tier | Tools |
|---|---|
| `read` | `browser_snapshot`, `browser_take_screenshot`, `browser_console_messages`, `browser_network_requests`, `browser_wait_for` |
| `write` | `browser_navigate`, `browser_navigate_back`, `browser_click`, `browser_type`, `browser_fill_form`, `browser_select_option`, `browser_press_key`, `browser_hover`, `browser_drag`, `browser_handle_dialog`, `browser_resize`, `browser_close`, `browser_tabs` |
| `outward`, or disabled | `browser_evaluate` and `browser_run_code_unsafe` (they run JavaScript), `browser_file_upload`, anything the server adds later |

`browser_tabs` both lists and opens tabs, so it cannot be `read` as a whole. Navigation
is a `write` here because it decides where the browser goes; leave it `outward` if
every new site should be approved.

## What the server can reach

The browser runs where the server runs and reaches whatever that network position
reaches. ScadBuddy no longer adds a marker to its requests or refuses them on the
backend, so limit it where it is deployed: the server's own `--allowed-origins` /
`--blocked-origins`, and a network policy. Its origin lists do not apply to redirects
(measured on `@playwright/mcp` 0.0.82), which is why the backend still serves no
off-origin redirect (`backend/tests/api/test_no_open_redirect.py`).

## One browser per MCP session

Playwright MCP with `--isolated` gives each MCP session a fresh browser context.

- **Classic turn**: one Claude Code process per turn, which connects to each plugin once
  and keeps that MCP session for the turn. A page opened by `browser_navigate` is
  still there for the next call in the same turn, and gone in the next turn.
- **Durable turn**: each turn-tool call is its own activity, which connects to the
  plugin afresh (`agent/src/temporal/turnTools.ts`, `withClient`), so each call gets
  a new MCP session. A page opened by one call is not there for the next.
