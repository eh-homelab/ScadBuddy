# ScadBuddy AI integration

This folder documents the AI features that are **merged on `main`** as of 2026-09-28.
The design is in the AI spec,
[`docs/superpowers/specs/2026-09-27-ai-integration-design.md`](../superpowers/specs/2026-09-27-ai-integration-design.md)
("the spec" below). Sections are cited as §N. These pages cover only what the code
does today, and every claim names the file (and function) it was read from. Where the
code and the spec disagree, the code is what runs, and the page says so.

Part of issue [#259](https://github.com/eh-homelab/ScadBuddy/issues/259) (docs half).

## Pages

| Page | For | What it covers |
|---|---|---|
| [operating.md](operating.md) | Operators | Deploying the agent sidecar, its environment variables, the key-encryption key and its rotation, the credential routes, `/healthz`, the origin allowlist and trusted proxies |
| [security.md](security.md) | Reviewers, operators | The threat model as built: risk tiers, the permission seam, envelope encryption, DNS-rebinding defence, egress checks, stderr redaction, plugin vetting, event-log scrubbing, known limitations and open §3.2 items |
| [mcp-resources.md](mcp-resources.md) | MCP client authors, operators | The `scadbuddy://` resources on `/mcp`, subscriptions and their notifications, and the agent's LISTEN on the event bus (#264) |
| [browser-bridge.md](browser-bridge.md) | Contributors | The tab-side agent tools, `data-agent-user-only`, and the WebMCP opt-in |
| [headless-browser.md](headless-browser.md) | Contributors, reviewers | The headless Chromium for sessions with no tab (#349): the pinned Playwright plugin, its tiers and guards, the backend's agent-actor gate, and what was measured |
| [dependencies.md](dependencies.md) | Contributors, MCP client authors | The library and font tools, include/use resolution, and the missing-font refusals (#253) |
| [claude-plugin.md](claude-plugin.md) | Users of Claude Code | Installing the ScadBuddy Claude plugin from this repository's marketplace |
| [evals.md](evals.md) | Contributors | The eval harness (`agent/evals/`, scripted in CI, live with `pnpm evals`), the AI test and e2e coverage, and what is still planned (#259) |

## What the AI integration is today

The spec's end state (§1) is an agent service in the ScadBuddy pod that external MCP
clients reach over `/mcp`, plus an in-app assistant. What is merged is the foundation
for that; **no AI feature is user-visible in a production build yet**:

- The assistant panel, and Settings → "MCP access tokens" (#251), are hidden outside
  the msw-mocked build. `useAiAvailability()` in
  [`frontend/src/agent/chat/availability.ts`](../../frontend/src/agent/chat/availability.ts)
  returns `available: true` only when `VITE_MOCK_API === '1'`, and
  `loadChatTransportFactory()` in
  [`frontend/src/agent/chat/transport.ts`](../../frontend/src/agent/chat/transport.ts)
  returns `null` otherwise (both carry a TODO for #255/#261/#266).
- The agent sidecar is not deployed: README "The agent sidecar" says "Nothing deploys
  it yet", and there are no ingress routes for `/mcp` or `/api/v1/ai/*` yet
  ([`README.md`](../../README.md)).
- `/mcp` and the tool registry are on `main` (#368), but no Settings route mints MCP
  tokens yet (#251), and nothing starts a harness session over HTTP yet (#266, #300).
  When a session does run, its queries get ScadBuddy's tools in-process, with their
  tiers and approvals, but no plugin
  ([security.md](security.md#risk-tiers-and-the-permission-seam)).

### Merged pieces

| PR | What it added | Where |
|---|---|---|
| [#319](https://github.com/eh-homelab/ScadBuddy/pull/319) | Agent service scaffold on the Claude Agent SDK (Hono, `/healthz`, least-privilege query options, pinned Claude Code version check) | `agent/src/app.ts`, `agent/src/config.ts`, `agent/src/harness/options.ts`, `agent/src/check-cli-version.ts`, `Dockerfile` (`agent` stage) |
| [#354](https://github.com/eh-homelab/ScadBuddy/pull/354) | Harness runner, encrypted Claude credential, `ai_*` migrations, credential routes | `agent/src/harness/run.ts`, `agent/src/secrets.ts`, `agent/src/credentials.ts`, `agent/src/routes/credentials.ts`, `agent/src/db/migrations.ts` |
| [#379](https://github.com/eh-homelab/ScadBuddy/pull/379) | Hardening: origin allowlist, AAD binding, key rotation, bounded health, egress check, stderr redaction, plugin vetting | `agent/src/http/origins.ts`, `agent/src/routes/guard.ts`, `agent/src/http/egress.ts`, `agent/src/harness/redactLines.ts`, `agent/src/harness/plugins.ts` |
| [#377](https://github.com/eh-homelab/ScadBuddy/pull/377) | Durable sessions: Postgres `SessionStore`, session manager, panel-protocol event log with scrubbing. Built in `main.ts` since #471; no HTTP route starts a session yet (PR #377 body, "HTTP routes") | `agent/src/sessions/` |
| [#368](https://github.com/eh-homelab/ScadBuddy/pull/368) | The tool registry, projected in-process for the harness and over `/mcp` (Streamable HTTP), with the auth modes (spec §5.1, §8.3) | `agent/src/tools/`, `agent/src/mcp/http.ts`, `agent/src/auth/` |
| [#471](https://github.com/eh-homelab/ScadBuddy/pull/471) | Approvals of outward tool calls in Postgres (`ai_approvals`): parked session calls, orphans after a restart, decision routes (spec §8.2) | `agent/src/approvals/service.ts`, `agent/src/routes/approvals.ts` |
| [#464](https://github.com/eh-homelab/ScadBuddy/pull/464) | Registered remote MCP plugins (`ai_plugins`) behind a loopback forwarder (spec §10). Not yet covered by these pages | `agent/src/plugins/`, `agent/src/routes/plugins.ts` |
| [#374](https://github.com/eh-homelab/ScadBuddy/pull/374) | The backend's Postgres `LISTEN/NOTIFY` event bus with a replay log (spec §7). Not yet covered by these pages | `backend/scadbuddy/core/pg_events.py`, `backend/scadbuddy/core/pg_listener.py` |
| [#461](https://github.com/eh-homelab/ScadBuddy/pull/461) | Print analyzers: cited rules, fixers and scoped decisions (spec §11). Not yet covered by these pages | `backend/scadbuddy/analyzers/`, `backend/scadbuddy/api/analyzers.py` |
| [#387](https://github.com/eh-homelab/ScadBuddy/pull/387) | Voice input and spoken replies in the assistant panel. Not yet covered by these pages | `frontend/src/agent/chat/voice.ts`, `frontend/src/components/assistant/` |
| [#526](https://github.com/eh-homelab/ScadBuddy/pull/526) | The harness wired up: the registry's in-process server and tiers for every session (outward calls park at the approval gate), and the MCP auth mode read from `ai_settings` (#255, #258, #300) | `agent/src/tools/harness.ts`, `agent/src/tools/projections.ts`, `agent/src/auth/authenticate.ts` |
| [#320](https://github.com/eh-homelab/ScadBuddy/pull/320) | Mesh geometry analysis for print analyzers: `GET /api/v1/outputs/{output_id}/geometry` | `backend/scadbuddy/render/geometry.py`, `backend/scadbuddy/api/outputs.py` |
| [#321](https://github.com/eh-homelab/ScadBuddy/pull/321) | Typed in-process event bus, published from every mutation (ids only, never content) | `backend/scadbuddy/core/events.py` |
| [#324](https://github.com/eh-homelab/ScadBuddy/pull/324) | Render diagnostics (`GET /api/v1/models/{slug}/diagnostics`), multi-view previews (`/jobs/{job_id}/views/{view}.png`, `/outputs/{output_id}/views/{view}.png`), library re-pin and remove (`PATCH`/`DELETE /models/{slug}/libraries/{name}`) | `backend/scadbuddy/api/jobs.py`, `backend/scadbuddy/render/diagnostics.py`, `backend/scadbuddy/api/libraries.py` |
| [#348](https://github.com/eh-homelab/ScadBuddy/pull/348) | Render workers wake on Postgres `NOTIFY scadbuddy_render_queue`, with a long fallback poll | `backend/scadbuddy/render/pg_store.py` |
| [#406](https://github.com/eh-homelab/ScadBuddy/pull/406) | The UI's realtime socket `/api/v1/ws`, served by the backend from the event bus (spec §4.2) | `backend/scadbuddy/api/realtime.py`, `frontend/src/lib/realtime.ts` |
| [#336](https://github.com/eh-homelab/ScadBuddy/pull/336) | The ScadBuddy Claude plugin and the repository marketplace | `plugins/scadbuddy/`, `.claude-plugin/marketplace.json`, `.github/scripts/lint-plugin.sh` |
| [#339](https://github.com/eh-homelab/ScadBuddy/pull/339) | In-browser agent bridge: semantic tools, snapshot, user-only confirmations, WebMCP opt-in | `frontend/src/agent/` |
| [#340](https://github.com/eh-homelab/ScadBuddy/pull/340) | Assistant panel: chat stream, action feed, approvals, sessions (against the scripted mock agent) | `frontend/src/components/assistant/`, `frontend/src/agent/chat/` |
| [#363](https://github.com/eh-homelab/ScadBuddy/pull/363) | Spec only: the headless Playwright browser for the harness (spec §5.3, D11). Nothing is implemented | spec §5.3, §3.2 |

## Coming (open PRs, not documented here)

These are not on `main`. Their docs belong in their own PRs or a follow-up to this one.

- [#501](https://github.com/eh-homelab/ScadBuddy/pull/501): MCP bearer tokens in Postgres (`ai_mcp_tokens`). Until it lands, `main.ts` wires a token store that verifies nothing, so `bearer` mode answers `401` to every `/mcp` request.
- [#504](https://github.com/eh-homelab/ScadBuddy/pull/504): MCP resources and subscriptions over the event bus (spec §5.4, §7).

## Citation rule

Every claim here names its source: a repository path and function, a spec section, or
an external URL. This follows the spec's citation rule (spec header, and D10 in §2).
Anything that could not be confirmed from those sources is marked **unverified**.
