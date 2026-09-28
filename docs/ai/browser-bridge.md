# The browser bridge

The tab's side of the browser tools (issue #254, merged in PR #339). It lets an agent
act *in the user's own tab*, including inside the Bambuddy iframe, through the same
state and `onChange` paths that user input takes. The design is in spec §5.2 and §8.5 of
[`docs/superpowers/specs/2026-09-27-ai-integration-design.md`](../superpowers/specs/2026-09-27-ai-integration-design.md).
The code is in [`frontend/src/agent/`](../../frontend/src/agent/).

> **Status.** On `main` the bridge has two callers: WebMCP, which is opt-in (below),
> and `window.__scadbuddyBridge`, which exists in dev and mocked-e2e builds only. The
> transport from the agent service to a paired tab (#266 and the pairing of spec §8.5)
> is **not built**, so no server-side agent drives a production tab yet
> (`installAgentBridge()`, [`frontend/src/agent/index.ts`](../../frontend/src/agent/index.ts)).

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

## Testing

- Unit tests: [`bridge.test.tsx`](../../frontend/src/agent/bridge.test.tsx),
  [`global.test.tsx`](../../frontend/src/agent/global.test.tsx) and
  [`webmcp.test.tsx`](../../frontend/src/agent/webmcp.test.tsx).
- e2e: [`frontend/e2e/agent-bridge.spec.ts`](../../frontend/e2e/agent-bridge.spec.ts)
  drives `window.__scadbuddyBridge` in the msw-mocked bundle.
