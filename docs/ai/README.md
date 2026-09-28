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
| [browser-bridge.md](browser-bridge.md) | Contributors | The tab-side agent tools, `data-agent-user-only`, and the WebMCP opt-in |
| [claude-plugin.md](claude-plugin.md) | Users of Claude Code | Installing the ScadBuddy Claude plugin from this repository's marketplace |
| [evals.md](evals.md) | Contributors | **Plan only**: the test and eval half of #259 |

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
- There is no `/mcp` endpoint and no tool registry on `main` (open PR #368).

### Merged pieces

| PR | What it added | Where |
|---|---|---|
| [#319](https://github.com/eh-homelab/ScadBuddy/pull/319) | Agent service scaffold on the Claude Agent SDK (Hono, `/healthz`, least-privilege query options, pinned Claude Code version check) | `agent/src/app.ts`, `agent/src/config.ts`, `agent/src/harness/options.ts`, `agent/src/check-cli-version.ts`, `Dockerfile` (`agent` stage) |
| [#354](https://github.com/eh-homelab/ScadBuddy/pull/354) | Harness runner, encrypted Claude credential, `ai_*` migrations, credential routes | `agent/src/harness/run.ts`, `agent/src/secrets.ts`, `agent/src/credentials.ts`, `agent/src/routes/credentials.ts`, `agent/src/db/migrations.ts` |
| [#379](https://github.com/eh-homelab/ScadBuddy/pull/379) | Hardening: origin allowlist, AAD binding, key rotation, bounded health, egress check, stderr redaction, plugin vetting | `agent/src/http/origins.ts`, `agent/src/routes/guard.ts`, `agent/src/http/egress.ts`, `agent/src/harness/redactLines.ts`, `agent/src/harness/plugins.ts` |
| [#377](https://github.com/eh-homelab/ScadBuddy/pull/377) | Durable sessions: Postgres `SessionStore`, session manager, panel-protocol event log with scrubbing. Not wired into `main.ts` and no HTTP routes yet (PR #377 body, "HTTP routes") | `agent/src/sessions/` |
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

- [#368](https://github.com/eh-homelab/ScadBuddy/pull/368): tool registry and the `/mcp` endpoint with its auth modes (spec §5.1, §8.3).
- [#374](https://github.com/eh-homelab/ScadBuddy/pull/374): the Postgres `NOTIFY` event bus (the `PgNotifyEventBus` that `backend/scadbuddy/core/events.py` describes as "not built yet").
- [#387](https://github.com/eh-homelab/ScadBuddy/pull/387): voice.
- [#461](https://github.com/eh-homelab/ScadBuddy/pull/461): print analyzers (spec §11).
- [#464](https://github.com/eh-homelab/ScadBuddy/pull/464): user plugins (spec §10).
- [#471](https://github.com/eh-homelab/ScadBuddy/pull/471): approvals (spec §8.2).

## Citation rule

Every claim here names its source: a repository path and function, a spec section, or
an external URL. This follows the spec's citation rule (spec header, and D10 in §2).
Anything that could not be confirmed from those sources is marked **unverified**.
