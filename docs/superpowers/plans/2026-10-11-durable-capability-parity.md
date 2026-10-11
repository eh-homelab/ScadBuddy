# Durable capability parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Whatever the user configured for the assistant (a plugin, plugin package, skill,
subagent, hook, MCP server, built-in tool, the headless browser, `http_request`, memory,
a tier or disable override, the model) works in every session mode and on every surface
that runs the assistant, or the user is told, in the place they configured it, exactly why not.

**Architecture:** One turn assembly, owned by the TypeScript agent, for both modes. A
classic turn hands it to Claude Code in-process as today. A durable turn gets the same
assembly through a `turn_tools` activity on `agent-tools`: every tool the classic turn
would get from a remote plugin, a plugin package's HTTP MCP server, `http_request`, the
headless browser and its grant server is declared to the durable workflow as one more
durable tool (an `activity_as_tool` on `agent-tools`), run by the agent service through the
same code the classic turn uses, parked at the same gate by its effective tier, audited
and logged the same way. Files (package skills, agents, hooks) reach the durable runner
through the payload volume both sidecars already mount. Nothing is ported to Python
except what only the segment runner can do (model, credential choice, plugin dirs).

**Tech Stack:** TypeScript (agent, Hono, Claude Agent SDK, Temporal TS SDK), Python 3.12
(agent-durable, temporalio, `temporalio-claude-agent-sdk` pinned), Postgres.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §6
(§6.3b is rewritten by Phase 9 to describe parity instead of the restriction).

## Why (the audit, 2026-10-11)

Durable became the default mode (`ai_settings.session_mode`, plan 5d Ruling 1) while a
durable turn skipped everything `SessionManager.runTurn` assembles
(`agent/src/sessions/manager.ts:1189-1199` vs `:1509-1738`). Prod sessions after
2026-10-10 22:05 UTC had no Playwright, no plugins, no packages, no `http_request`, no
memory, the engine's default model. Spec §6.3b called plugins in durable "its own later
issue"; none was filed, and the mode picker never said so. The full list, each closed by a
phase below:

| # | Gap | Phase |
|---|---|---|
| A12 | Durable ignores Settings → Model | 1 |
| A10 | Durable ignores the own-plugin switch | 1 |
| A13 | Durable tries only the first credential | 1 |
| A14/B5 | A durable turn's and a flow call's sender tiers are dropped (read-only) | 1 |
| C1 | Classic: own plugin off removes `Skill`/`Agent` for every package | 1 |
| A8/A9 | Raised tiers refused, disabled tools offered | PR #2311 (base of this stack) |
| A1, A4 (tools) | Remote MCP plugins (incl. Hindsight's tools) | 2 |
| A6 | `http_request` | 2 |
| A5 | Headless browser and request grants | 3 |
| A2 | Plugin packages (skills, agents, HTTP MCP servers, hooks, refused-allowed built-ins) | 4 |
| A3, C3 | Subagents in durable; subagents' tool lists | 5 |
| A4 (hooks) | Hindsight recall/retain in durable | 6 |
| A11 | Tool-result images in durable | 7 |
| B3, B6 | Flows `tool()` reaches the turn's plugin tools and `http_request`; disabled filtered up front | 8 |
| E | Evals run durable too, with the session system append | 9 |
| docs/UI | Spec §6.3b, plan 5c Rulings 4/15, operating.md, user-guide, headless-browser.md, Settings copy | 9 |

Not gaps (kept, with the reason stated where the user meets them): `/mcp` serves only
ScadBuddy's own tools (an external client brings its own); `ask_session()` refuses classic
sessions (a flow joins only a workflow); browser and question tools in flows (no tab, no
user in the loop).

## Global Constraints

- Every outward call parks for a human in both modes; a tier is resolved when the call runs
  (`effectiveTool`, #2088), never from the declaration.
- The credential never reaches a tool server, a plugin, or a file: plugin headers are added by
  the forwarder, never given to Claude Code.
- `DurableSession` changes go behind `workflow.patched(...)`; `AgentOperation` unchanged.
- Agent migrations: a NEW file `agent/src/db/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`.
- No new deployment objects: the payload volume (`/var/lib/scadbuddy-payloads`) is the only
  shared filesystem between `agent` and `agent-durable`.
- Docs cite their sources; measured facts say where and how (#259).

## Review Focus

1. A tool raised to outward between declaration and call: parks, never runs unapproved (both modes).
2. A plugin disabled mid-session: the next durable turn no longer declares its tools; a call already parked is refused at run time.
3. A worker restart mid-tool for a plugin tool: an outward plugin call runs at most once.
4. The headless browser's pod is gone: the call fails with a reason, the next turn starts a fresh browser.
5. Two credentials, the first refused: the durable segment retries on the second, not the same one.

---

## Phase 1 — settings durable ignores (PR 1, stacked on #2311)

### Task 1.1: Durable segments use Settings → Model and the own-plugin switch

**Files:**
- Modify: `agent-durable/src/scadbuddy_durable/session/runner.py` (`run`, settings read)
- Test: `agent-durable/tests/test_runner.py`

**Interfaces:**
- Produces: `ScadBuddyRunner._turn_settings() -> TurnSettings(model: str | None, own_plugin: bool)`; `make_runner(..., model=...)`.

- [ ] Test: with `ai_settings.model = "claude-sonnet-x"` the runner factory receives `model="claude-sonnet-x"`; with it unset, no `model`; with `scadbuddy_plugin_enabled = false`, `extra` has no `plugins`.
- [ ] Run, see it fail.
- [ ] Implement: one `SELECT key, value FROM ai_settings WHERE key = ANY(...)` for `model`, `scadbuddy_plugin_enabled`, and the long edge; `model` only when a non-empty string (manager.ts `SETTING_MODEL`); own plugin on unless stored `false` (`plugins/packages/builtins.ts ownPluginEnabled`).
- [ ] Run tests, ruff, mypy. Commit `fix(durable): use the model and own-plugin settings (#<issue>)`.

### Task 1.2: Durable credential fallback

**Files:**
- Create: `agent-durable/src/scadbuddy_durable/session/fallback.py`
- Modify: `runner.py` (`run`)
- Test: `agent-durable/tests/test_fallback.py`

**Interfaces:**
- Produces: `credential_failure(text: str) -> Literal["auth","quota","rate",None]` (the classes of `agent/src/harness/credentialErrors.ts classifyFailure` that are the credential's); `ScadBuddyRunner.run` tries each usable credential in priority order at most once per segment attempt.

- [ ] Test: first credential's segment ends `is_error` with a 401 text → the runner runs the same segment again with the second credential's env and returns its output; a non-credential failure is not retried; costs of both attempts are added.
- [ ] Implement by porting `classifyFailure`'s patterns verbatim (cite file:line), and the cooldown write `ai_credentials` uses (`credentials.ts` health columns) so classic and durable agree on a cooled key.
- [ ] Tests, lint, commit `fix(durable): fall back to the next credential like a classic turn`.

### Task 1.3: A durable turn runs with its sender's tiers

**Files:**
- Create: `agent/src/db/migrations/<ts>_ai_sessions_turn_tiers.sql` (`ALTER TABLE ai_sessions ADD COLUMN turn_tiers text[]`)
- Modify: `agent/src/sessions/durable.ts` (`DurableTurns.send` writes `turn_tiers` in the claim), `agent/src/sessions/manager.ts:1189-1199` (pass `options.tiers`), `agent/src/temporal/toolActivities.ts` (`durableSession` returns `tiers`; principal from them), `SessionLookup.ownerOf` → `turnOf`.
- Test: `agent/test/toolActivities.pg.test.ts`, `agent/test/durableTurns.pg.test.ts`

- [ ] Test: an MCP token with `read,write` starts a durable session and sends; a write tool's activity runs (today: refused as read-only). A browser turn keeps every tier. A turn with no tiers keeps `harnessPrincipal(owner)`.
- [ ] Implement; `turn_tiers` set NULL for browser turns; principal = `{...harnessPrincipal(owner), tiers: turn_tiers}` when set (intersected with `TIERS`).
- [ ] Flows (B5): `flows.startedBy` returns the starter's tiers recorded at start (`workflow_runs.started_by_tiers`, backend migration) and `flowCaller` uses them. Test a token-started run's write `tool()`.
- [ ] Tests, lint, commit `fix(agent): durable turns and flow calls run with their sender's tiers`.

### Task 1.4: Classic: `Skill`/`Agent` follow any loaded skills or agents

**Files:** Modify `agent/src/harness/run.ts:517-529`; Test `agent/test/run.test.ts`.

- [ ] Test: own plugin off, one vetted package with a skill → `tools` contains `Skill`; with an agent → `Agent`.
- [ ] Implement: offer `Skill` when any loaded plugin (own, package, allowed) has `skills/` or `commands/`, `Agent` when any has `agents/`; record which in `PackagesForRun`.
- [ ] Commit `fix(agent): a package's skills and agents load without ScadBuddy's own plugin`.

## Phase 2 — remote plugins and `http_request` in durable (PR 2)

Design: `agent/src/turn/assembly.ts` extracts from `runTurn` the parts both modes need:
`assembleTools({session, principal, settings}) -> TurnToolset` = `{ entries: ExtraToolEntry[], problems: string[], secrets: string[] }`
where `ExtraToolEntry = {name, description, input_schema, tier, server, tool, hitl}` names a
tool as `<server>__<tool>` (sanitised as Claude Code does, `harnessToolName`). Classic keeps
handing Claude Code the servers; durable gets the entries:

- TS: `turn_tools` activity (agent-tools) → entries for remote plugins (forwarder `tools/list`,
  disabled stripped), `http_request`/`http_response_read` when enabled. `extra_tool` activity
  runs one call: resolves the entry again (enabled? tier now?), gate check as `runAsActivity`,
  calls the plugin through the forwarder (same pinned address, header, untrusted wrapping)
  or the in-process http server, audits, returns text.
- Python: `DurableSession._run_turn` under `workflow.patched("turn-tools-1")` executes
  `turn_tools`, adds/removes `DurableTool`s in the agent's tool dict (needs_approval from
  tier, `_ONCE` for outward), passes `plugin_problems` to `segment_context` and logs them as
  `plugin_unavailable`. The system append describes the plugin tools' names.
- Tests: pg test with a fake MCP HTTP server as a remote plugin: listed, read tool runs, outward
  parks then runs once after approval, disabled not declared, plugin removed mid-session.

## Phase 3 — headless browser in durable (PR 3)

- TS hosts `@playwright/mcp` per durable session (stdio client, same launch as
  `headlessBrowser.ts`, same deny list, tiers, input guard, origin approvals, grant server).
- Affinity: each agent-tools worker also polls `agent-tools.<hostname>`; `turn_tools` returns
  browser entries with that queue; the browser lives until the session idles
  (`BROWSER_IDLE_MS`) or the pod goes. A call whose queue has no poller fails at
  `schedule_to_close` with "the headless browser was on a replica that is gone; it starts
  again next turn".
- `systemAppend(true)` when the browser is on.

## Phase 4 — plugin packages in durable (PR 4)

- TS `turn_tools` materialises each enabled pin into `/var/lib/scadbuddy-payloads/plugins/<hash>/`
  (re-hashed, as `loadPackagesForRun`), returns `plugin_dirs`, `allowed_plugin_dirs`, package
  HTTP MCP servers as entries (served like remote plugins), and package-named built-ins.
- Python runner loads the dirs as `plugins`; package built-ins go into `builtin_tools`, and
  into `tool_activities` + `tool_approvals` (outward, as classic tiers them).

## Phase 5 — subagents (PR 5)

- The plugin denies a durable tool to a subagent (`MAIN_AGENT_ONLY`). Durable subagents get
  the agent's loopback `scadbuddy` HTTP MCP server (pod-local, HMAC token per segment from the
  KEK) with the session principal's read and write tools; an outward call from a subagent is
  denied with "ask the main agent to do this; it parks for approval there". `Agent` and the
  agent definitions load in durable.
- Classic and durable: subagent definitions drop the `tools:` allowlist so they inherit the
  turn's tools (same gate).

## Phase 6 — Hindsight in durable (PR 6)

- `memory_recall` (TS activity, turn start) returns the `<hindsight_memories>` block the
  classic `UserPromptSubmit` hook adds; appended to the prompt. `memory_retain` (turn end,
  before `finish_turn`) retains the turn's transcript. Supersedes #825.

## Phase 7 — tool images in durable (PR 7)

- `toolActivities` stores a result's images in `ai_session_blobs` and returns their names; the
  runner's images server gains `view_tool_images(names)`. `DURABLE_IMAGE_NOTE` names the tool.

## Phase 8 — flows (PR 8)

- `tool(name, …)` accepts plugin and `http_request` entries through `turn_tools`/`extra_tool`
  with the run's starter principal; disabled tools refused before the call is made.

## Phase 9 — evals, docs, UI (PR 9)

- `agent/evals/runner.ts` runs each scenario in both modes; system append included.
- Rewrite spec §6.3b and plan 5c Rulings 4/15 to describe parity; `docs/ai/operating.md`,
  `docs/user-guide.md`, `docs/ai/headless-browser.md`, `SessionModeSetting`/`SessionModePicker`
  copy (only real differences: forking).
- Restore `session_mode = durable` in prod once Phases 1–7 are deployed.
