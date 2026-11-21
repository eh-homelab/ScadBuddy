# Frontend

`frontend/` is a React and Vite app with two entries: the back office (the app people
use inside Bambuddy) and the public storefront. Commands, Playwright and the folder
layout are in [`frontend/CLAUDE.md`](../../frontend/CLAUDE.md). Paths below are under
`frontend/` unless they say otherwise; line numbers are for `main` at the commit that adds
this page.

## Two entries, one build

| Entry | HTML | Boot | Router | API base |
|---|---|---|---|---|
| Back office | `index.html` | `src/main.tsx` | `src/App.tsx` | `/api/v1` (`src/api/client.ts:333`) |
| Storefront | `storefront.html` | `src/storefront/main.tsx` | `src/storefront/App.tsx:86-97` | `/api/store/v1` (`src/storefront/api/client.ts:67`) |

The API image serves the back-office bundle; the storefront edge serves only the
storefront's closure, which the build manifest lists (`vite.config.ts:93-100`). A lint rule
(`eslint.config.js:19-36`) and `scripts/check-storefront-bundle.mjs` keep back-office code
and `/api/v1/` out of the storefront bundle.

`src/main.tsx` installs the CSRF request header (`:13`), the stale-chunk reload (`:14`),
the msw worker when `VITE_MOCK_API==='1'` (`:15-21`) and the agent bridge (`:23`), renders
the router (`:28-34`), and loads tracing after the first paint (`:50-56`).

## Back-office routes

Every route sits under `AppShell` (`src/App.tsx:77`); every page but the catalogue and
Activity (both imported eagerly, `src/App.tsx:6-7`) is a lazily loaded chunk
(`src/App.tsx:16-57`). The sidebar lists Models, Prints, Library,
Orders, Storefront, Workflows, Activity and Settings (`src/components/AppShell.tsx:93-100`).

| Path | Page | Feature doc |
|---|---|---|
| `/` | `CataloguePage` | [catalogue](../features/catalogue-and-models.md) |
| `/new`, `/m/:slug/source`, `/m/:slug/source/:file` | `NewModelPage`, `EditSourcePage`, `EditSourceFilePage` (Monaco) | [source editing](../features/source-editing.md) |
| `/m/:slug` | `CustomizePage` | [customizer](../features/customizer-and-parameters.md) |
| `/m/:slug/history`, `/m/:slug/versions` | `HistoryPage`, `VersionsPage` | [versions and history](../features/versions-and-history.md) |
| `/m/:slug/prints`, `/prints`, `/prints/:archiveId`, `/prints/queue/:itemId` | `TemplatePrintsPage`, `PrintsPage`, `PrintDetailPage`, `QueueItemPage` | [print history](../features/print-history-and-watcher.md) |
| `/edit/:outputId` | `EditPage` | [Arrange](../features/arrange-and-plates.md) |
| `/library/*` | `LibraryPage` | [print library](../features/print-library.md) |
| `/orders`, `/orders/profit`, `/orders/parts`, `/orders/:id`, `/customers`, `/customers/:id` | orders, profit, parts, customers | [orders](../features/orders-and-quotes.md) |
| `/storefront`, `/storefront/listings/:id` | `StorefrontPage`, `StorefrontListingPage` | [storefront](../features/storefront-and-payments.md) |
| `/workflows`, `/workflows/flows/:id`, `/workflows/runs/:id` | `WorkflowsPage`, `FlowPage`, `WorkflowRunPage` | [flows](../features/flows.md) |
| `/activity` | `ActivityPage` | [activity](../features/activity.md) |
| `/settings/*` | `SettingsPage`, areas in `src/pages/settings/areas.ts:17-26` | [settings and auth](../features/settings-and-sign-in.md) |
| `*` | redirect to `/` | |

Settings sections are found by an eager glob of `src/pages/settings/sections/*.tsx`
(`src/pages/settings/registry.ts:58`); each declares its area, title and order.

The storefront's routes (`src/storefront/App.tsx:86-97`) are `/` (listings), `/l/:id`
(customize), `/draft` (cart), `/request`, `/print-my-model`, `/o` (order), `/pay`,
`/account` and `/account/sign-in`.

## How API types flow

The generated-files rule is in the root
[`CLAUDE.md` "Generated API files"](../../CLAUDE.md#generated-api-files); the pipeline is
drawn in [data.md](data.md#generated-api-files).

- `scripts/gen-api.mjs` exports the backend's spec (or reads `SCADBUDDY_OPENAPI_JSON`)
  and runs `openapi-typescript` into `src/api/schema.d.ts`, then does the same for the
  storefront spec into `src/storefront/api/schema.d.ts` (`scripts/gen-api.mjs:16-33`).
- `src/api/types.ts` holds only aliases into `components['schemas']` (`:1-21`).
- `src/api/client.ts` is the back-office client: `request` and friends (`:423-460`),
  RFC 9457 problems as `ApiError` (`:365-384`), and `command()`, which posts an
  operation with an `Idempotency-Key` (`:886-912`).
- The agent's `/api/v1/ai/*` routes are not in the backend's OpenAPI. Their types are
  hand-written (for example `src/api/aiCredential.ts`, `src/api/aiPlugins.ts`), and the
  chat protocol is checked with zod (`src/agent/chat/protocol.ts:44`, `:582`).

## Realtime and the assistant

| Channel | Path | Code |
|---|---|---|
| Backend events | WebSocket `/api/v1/ws` | `src/lib/realtime.ts:8-15` |
| Assistant chat | WebSocket `/api/v1/ai/chat` | `src/agent/chat/socketTransport.ts:37`, `:66` |
| Browser bridge | WebSocket `/api/v1/ai/bridge` | `src/agent/link.ts:32`, mounted by `src/components/AgentLink.tsx:35` |
| Pending approvals | SSE `/api/v1/ai/pending-input/changes`, plus a 15 s poll | `src/lib/pendingChanges.ts:9`, `src/agent/attention.ts:24-26` |
| Source editor LSP | WebSocket `/api/v1/models/{slug}/lsp` | `src/lib/languageClient.ts:34-36` |

The ingress sends `/api/v1/ai/*` to the agent sidecar and the rest to the API; the dev
server's proxy does the same (`vite.config.ts:43-49`). The assistant panel is
`src/components/assistant/AssistantPanel.tsx:22` and `AssistantChat.tsx:154`; the tools a
paired tab offers the assistant are in `src/agent/`. See [agent.md](agent.md) and
[features/ai-assistant.md](../features/ai-assistant.md).

## Inside Bambuddy's iframe

The sandbox constraints (downloads through a popup, `window.open` for deep links, the
full-screen fallback) are in the root
[`CLAUDE.md` "Bambuddy iframe facts"](../../CLAUDE.md#bambuddy-iframe-facts). The code:

- `src/lib/embed.ts`: `isEmbedded` (`:7`), `downloadBlob` (`:46`), `openExternal` (`:127`).
- `src/lib/useFullscreen.ts:36`: the Fullscreen API or the covering view.
- `src/lib/bambuddyLinks.ts:145`: deep links into Bambuddy.
- `src/components/AppShell.tsx:226-237`: notifications are off when embedded.
- `e2e/bambuddyFrame.ts` and `e2e/downloads.spec.ts`: a replica of the frame.

## Template-owned UIs

A template can ship its own interface: `model.json` declares
`ui: {module, slot: "panel"|"page", api}`
(`backend/scadbuddy/library/catalogue.py:264-275`; `src/template-ui/types.ts:14-18`).
The design is in the
[template-pipelines spec §4](../superpowers/specs/2026-09-27-template-pipelines-design.md).

`TemplateUi` (`src/template-ui/TemplateUi.tsx:63`) is "not sandboxed, style-isolated"
(`:62`): it refuses an unsupported `api` major (`:84-87`), attaches an open shadow root
and adopts the app's styles (`:94-95`), builds the host API (`src/template-ui/host.ts:85`),
`import()`s the module from the backend's versioned UI route (`:108`,
`src/api/client.ts:1815-1819`) and calls its `mount` within 15 s (`:17`, `:117-125`). The
`<sb-param>`, `<sb-preview>` and `<sb-generate>` elements render the app's own widgets
through `createPortal` (`:164-175`). When a UI fails, the customizer falls back to the
generated form (`src/pages/CustomizePage.tsx:1428-1438`).

## Browser tracing

`src/lib/tracing.ts:93` starts a tracer (`service.name` `scadbuddy-web`) that drops polls,
propagates trace context same-origin only, scrubs spans (`src/lib/traceScrub.ts:113`) and
exports to the API's relay `/telemetry/v1/traces` (`src/lib/relayExporter.ts:7`, `:51`).
User actions are spans named by `traceAction` (`src/lib/traceAction.ts:68`). See
[README "Tracing"](../../README.md#tracing-988).

## Tests

- Vitest (jsdom), colocated `*.test.ts(x)` files, msw handlers in `src/mocks/` (one
  module per feature under `src/mocks/features/`, `src/mocks/features.ts:38-40`).
- Playwright against the mocked production bundle by default, or a real stack with
  `E2E_BASE_URL` (`playwright.config.ts:13-53`). The specs in `e2e/` are grouped by
  feature in each [feature doc](../features/README.md).
