# Durable phase 5d: Mode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A person (or an MCP caller) picks whether a new assistant session is classic or durable, the operator picks the default in Settings → Assistant, and the panel shows which mode a session runs in.

**Architecture:** One PR, stacked on #1988 (5c PR 3). The agent service takes `mode` on its three create paths and reports it in every session view; a new route group stores the `session_mode` setting; the panel gets a picker in the composer, a Durable badge in the header and a Settings section.

**Tech Stack:** TypeScript (Hono, zod, postgres.js, vitest) in `agent/`; React 19, msw, vitest in `frontend/`.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §6.1. Sub-plan table: `2026-10-08-durable-phase-5a-agent-durable-foundation.md` (row 5d). Consumes "What 5d and 5e need from 5c" in `2026-10-09-durable-phase-5c-durable-session.md`. Issue #1056.

## Rulings (where the spec is ambiguous, or the owner decided otherwise; the spec governs the rest)

- **Ruling 1, the default is `durable`** (the owner's decision, overriding §6.1's "default `classic`" and 5c Ruling 13). An unset or unreadable `session_mode` means `durable`; only a stored `classic` means classic.
- **Ruling 2, the fallback.** A start whose mode came from the setting (the caller named none) and that finds no durable dispatch (no Temporal client, no KEK: `SessionManager.durableTurns` unset) runs classic, and says so. A caller that named `durable` is refused `unavailable` (503) as in 5c, and nothing is inserted. The setting counts as "the default" whether it is unset or stored as `durable`: either way the caller did not ask.
- **Ruling 2b, "can run durable" includes a worker.** With the default `durable`, an agent service that has Temporal and the KEK (production already has both, for `agent-tools`) but no `agent-durable` worker (5e ships it) would start every new chat as a workflow no one runs, and each send would be refused after 10 s. So `DurableTurns.unready()` also asks Temporal whether any worker polls the `agent` queue (`DescribeTaskQueue`, workflow type, 3 s deadline, answer reused 15 s). No poller, or no answer, is the same as no Temporal: the default falls back, an explicit `durable` is refused `unavailable`.
- **Ruling 3, saying which mode ran.** `session.started` gains `mode` and, after a fallback, `modeFallback` (why: the 5c refusal's text). `POST /sessions` and `sessions_start` answer `session.mode` and, after a fallback, top-level `mode_fallback`. The panel shows the reason as a note under the header of that session, and the Durable badge only on a durable one. The reason is in the event log, so a replay shows it too.
- **Ruling 4, `mode` everywhere a session is described.** `SessionView.mode` (routes, `sessions_*` tools), `SessionSummary.mode` (`sessions.snapshot`). The frontend's schemas take it as optional (an older agent sends none: read as classic).
- **Ruling 5, an existing session refuses `mode`.** The chat socket's `user.message` with both `sessionId` and `mode` is refused `invalid`, as §6.1 says. `POST /sessions/:id/messages` and `sessions_send` have no `mode` field at all (zod strict / unknown key), so there is nothing more to refuse.
- **Ruling 6, the route group.** `GET`/`PUT /api/v1/ai/settings/session-mode`, `src/routes/sessionMode.ts`, guarded and audited as `imageSettings.ts`. `GET` answers `{ mode, durable_available, durable_unavailable_reason? }`: whether this agent service can start a durable session now (`SessionManager.durableUnready`, Ruling 2b), and why not, so Settings can say that the default will fall back. `PUT` takes `{ mode }` and stores it whether or not durable is available (an operator may set it before Temporal is up). Both are in `AGENT_ROUTES` with a reason: they are Settings-only, and an agent chooses its own session's mode through `sessions_start`.
- **Ruling 7, the composer's picker.** A `<details>` named "Session mode" (not "Advanced": the panel header already has an Advanced switch, #1488, and two controls of one name would be ambiguous to a screen reader and to the bridge). One select: Default (with the stored default when known), Classic, Durable. Shown only while no session is open. "Default" sends no `mode`, so the fallback applies; a picked mode is sent and never falls back. The choice is remembered in `localStorage` `scadbuddy.assistant.mode` (try/catch); Default removes the key.
- **Ruling 7b, Fork on a durable session.** The header's Fork is disabled on a durable session, with a title saying why, since the agent refuses it (5c Ruling 13).
- **Ruling 7c, the mock agent's default is classic.** `createMockAgentTransport` takes `defaultMode` (default `classic`) and `durableAvailable` (default `true`): its scripts are classic sessions that fork. Tests of the mode pass `defaultMode: 'durable'`, the agent's real default. The msw Settings mock (`mocks/features/sessionMode.ts`) answers `durable`, as the agent does.
- **Ruling 8, `wait_for_user`'s options.** 5c Ruling 14 left `stop`/`wait` to 5d. Not in this PR: it is the gate's contract, not the mode switch, and needs its own spec reading. Left open in "What 5e needs".

## Task 1: the manager

**Files:** `agent/src/sessions/manager.ts`, `agent/src/sessions/protocol.ts`, `agent/test/sessions.durable.temporal.test.ts`.

- [x] Test: with no setting, `start` without `mode` and with no `durableTurns` inserts a classic session, returns `modeFallback`, and its `session.started` carries `mode: 'classic'` and `modeFallback`. With `classic` stored, classic and no fallback. With `mode: 'durable'`, refused `unavailable`, nothing inserted.
- [x] `modeSetting()` reads `classic` only when stored so; `start` returns `{ session, turn?, modeFallback? }`; `session.started` gains `mode` and `modeFallback?`; `fork` writes `mode` on its `session.started`. `SessionSummary` gains `mode`.

## Task 2: the create paths and views

**Files:** `agent/src/routes/sessions.ts`, `agent/src/routes/chat.ts`, `agent/src/sessions/clientProtocol.ts`, `agent/src/tools/sessions.ts`, their tests.

- [x] Tests: `POST /sessions` with `mode` passes it; without, answers `mode_fallback` after a fallback; a bad `mode` is 400. `SessionView` carries `mode`. The socket passes `mode` on a new chat and refuses it with a `sessionId`. `sessions_start` passes `mode` and answers `mode_fallback`.
- [x] Implement.

## Task 3: the setting's routes

**Files:** `agent/src/routes/sessionMode.ts` (new), `agent/test/sessionModeRoutes.test.ts` (new), `agent/src/tools/coverage.ts`.

- [x] Tests as `imageSettingsRoutes.test.ts`: default `durable`, round trip, refused bodies, guards, 503 without a database, `durable_available`.
- [x] Implement; `AGENT_ROUTES` entries.

## Task 4: the frontend

**Files:** `frontend/src/agent/chat/protocol.ts`, `state.ts`, `useAgentChat.ts`, `components/assistant/AssistantChat.tsx`, `badges.tsx`, `components/SessionModeSetting.tsx` (new), `pages/SettingsPage.tsx`, `api/client.ts`, `api/types.ts`, `mocks/features/sessionMode.ts` (new), tests.

- [x] Tests: the picker sends `mode` only when picked, remembers it, and is gone once a session is open; the header shows Durable on a durable session and the fallback note; Settings shows and saves the default and says when durable is unavailable.
- [x] Implement.

## Task 5: docs

- [x] `docs/ai/operating.md`: the setting, the default and the fallback.

## Ledger

| # | Decision | Where |
|---|----------|-------|
| 1 | Default `durable` (owner) | Ruling 1 |
| 2 | Fallback only from the default; explicit durable refused | Ruling 2 |
| 3 | `modeFallback` / `mode_fallback` tell the user | Ruling 3 |
| 4 | Picker is "Session mode", not "Advanced" | Ruling 7 |
| 5 | `wait_for_user` stop/wait deferred | Ruling 8 |
| 6 | "Can run durable" includes a polling `agent-durable` worker | Ruling 2b |
| 7 | Fork disabled on a durable session | Ruling 7b |
| 8 | Mock agent's sessions default classic | Ruling 7c |

## What 5e needs from 5d

- Nothing new in the image. New sessions become durable by default the moment an `agent-durable` worker polls the `agent` queue (Ruling 2b); until then they run classic and say why.
- Still open from 5c: nothing reaps a durable session whose row says `running` after its workflow ended without `finish_turn`; `wait_for_user` is `proceed`-only (Ruling 8).
