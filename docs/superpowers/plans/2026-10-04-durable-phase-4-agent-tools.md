# Phase 4: every `/api/v1` tool as a Temporal activity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The agent service runs a TypeScript `@temporalio/worker` on the `agent-tools` task
queue with one activity per tool of `ALL_TOOLS`, each running `runToolWithOutcome` as `/mcp`
does; a build step exports `ALL_TOOLS` as JSON for the phase 5 Python worker; and installing
or re-pinning a plugin package (a git fetch) becomes a command in the shape of §4.2 (#1055).

**Architecture:**
- `src/tools/manifest.ts` lists `ALL_TOOLS` through the `/mcp` projection itself
  (`createExternalServer` + an in-memory MCP client), so the exported `input_schema` is exactly
  what `/mcp` lists. `src/export-tools.ts` writes `[{name, description, input_schema, tier}]`;
  `pnpm build` writes `dist/tools.json`, `pnpm gen:tools <out>` anywhere. Generated, never
  committed (#492's rule).
- `src/temporal/toolActivities.ts`: `toolActivities(tools, deps)` maps each tool name to an
  activity. The activity reads its workflow ID (`session-<id>`), loads that session's owner
  from `ai_sessions`, and runs `runToolWithOutcome` with the owner's harness principal,
  `session`, the session-bound browser, `lookup`, `gate: 'workflow'`, progress as heartbeats
  and the activity's cancellation signal. A success returns the result's content blocks; any
  other outcome throws a non-retryable `ApplicationError` (type `ToolError`) with the text the
  model would see. Every call is written to the audit log as the harness writes it.
- `src/temporal/worker.ts`: `AgentWorker` connects (`NativeConnection`) in the background,
  retrying, and runs one `Worker` on `agent-tools` with the tool activities, the operation
  activities and the `AgentOperation` workflow (a prebuilt bundle, `dist/temporal/workflow-bundle.js`).
  Unversioned; `/healthz` reports it; shut down first on SIGTERM.
- Commands in the agent: the table `ai_operations` (the agent's copy of the backend's
  `operations`, since the agent owns its `ai_*` tables), `GET /api/v1/ai/operations/{id}`, the
  `AgentOperation` workflow (check → insert → run → finish, the backend `Operation`'s order and
  answers) and `startCommand` (update-with-start, `USE_EXISTING`,
  `ALLOW_DUPLICATE_FAILED_ONLY`, the 10 s deadline). Its two kinds are
  `plugin_package_install` and `plugin_package_repin`; `POST /plugin-packages` and
  `POST /plugin-packages/:name/repin` run them, answering as before within the deadline and
  202 with the operation past it. The frontend follows such a 202 through
  `/api/v1/ai/operations/{id}`.

**Tech Stack:** Node 24, TypeScript 6, `@temporalio/{client,worker,workflow,activity,common}`
1.24.0 and `@temporalio/testing` 1.24.0, postgres.js, Hono, vitest; the Temporal CLI dev
server for integration tests.

**Spec:** `docs/superpowers/specs/2026-10-01-durable-printing-agents-flows-design.md` §6.3,
§4.2 (the command shape), §4.3 (`agent-tools` row: "tool calls, … plugin package
install/approve (a git fetch)"), §6.5 ("Phase 4 starts by confirming that the TypeScript SDK
exposes the same serialization context to a codec"), §8 (`agent-tools` activities use
`@temporalio/testing`), §10 phase 4. Stacked on `feat/1060-print-deployment`.

## Rulings

1. **§6.5's gate holds.** `@temporalio/common` 1.24.0 `PayloadCodec.encode/decode(payloads,
   context?: SerializationContext)`; `ActivitySerializationContext` carries `workflowId` (and
   `WorkflowSerializationContext` `workflowId`), marked `@experimental`. Task 4's integration
   test pins it: a codec on the worker sees `{type: 'activity', workflowId: 'session-<id>'}`
   when it decodes a tool activity's arguments. The codec itself (`ai_payload_keys`,
   encryption) ships with durable sessions in phase 5: until then no `session-*` workflow
   exists, so nothing reaches the worker that the codec would encrypt.
2. **The manifest is generated, not committed.** `pnpm build` writes `dist/tools.json`
   (phase 5's `agent-durable` stage copies it from `agent-build`); `pnpm gen:tools <path>`
   writes it elsewhere. It is produced from the source at every build, so no freshness check
   is needed; a test asserts it equals the `/mcp` listing (names, descriptions, schemas) with
   `tier` = each tool's `risk`.
3. **Activity input and result.** The input is the one dict the Python plugin passes
   (`activity_as_tool`: `execute_activity(fn, call.input, activity_id="tool-<tool_use_id>")`,
   checked at ai-integrations `b1cf3848`). The result is the `CallToolResult`'s `content` (MCP
   blocks, any JSON value is accepted as `ToolOutcome.content`). A result that is not `ok`
   (an error result, a tier refusal, invalid arguments) throws
   `ApplicationError.nonRetryable(text, 'ToolError')`, which the plugin turns into
   `ToolOutcome(is_error=True)`: retrying would repeat a call that already ran. Only
   infrastructure errors (the session lookup failing) are retryable.
4. **Who the call runs as.** Only a workflow ID `session-<uuid>` whose `ai_sessions` row exists
   is served; anything else (a `flow-*` before phase 6, an unknown ID) is refused
   non-retryably. The principal is `harnessPrincipal(owner)` and `session` the row's id, as
   `harnessTools().mcpServers` sets them for a classic turn without turn tiers.
5. **Approval belongs to the workflow.** `ToolContext.gate` gains `'workflow'`: a call that
   reaches the activity has passed the session workflow's `needs_approval` (§6.4, phase 5), so
   `runToolWithOutcome` does not prepare a second approval. `runJudgedByResult` gates only when
   `gate` is unset. Reaching `agent-tools` takes the namespace's credentials, which only
   ScadBuddy's own processes hold.
6. **Audit.** Each activity writes one `tool_call` row, surface `harness`, with `sessionId`,
   `toolUseId` (the activity ID without `tool-`), tier, hash, summary and outcome, as
   `audit/turn.ts` writes the harness's.
7. **Progress and cancellation.** `progress` heartbeats its arguments; a 10 s heartbeat timer
   runs for the whole call, so a cancel reaches the activity (TypeScript SDK: cancellation is
   delivered on heartbeat) and aborts `ctx.signal` (`Context.current().cancellationSignal`).
8. **Configuration.** Infrastructure variables only (`config.ts`): `SCADBUDDY_TEMPORAL_ADDRESS`
   and `SCADBUDDY_TEMPORAL_NAMESPACE` (default `scadbuddy`), the backend's names, and
   `SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES` (the backend's switch, until the clusters change
   registers them). The task queue is fixed, `agent-tools` (§4.3), like the listening port.
   Unset address → no worker and no commands: `/healthz` says `temporal: "not configured"` and
   install/re-pin answer 503, as without a database. The CI container check runs without it.
9. **Unversioned worker, patched workflow.** The worker serves activities and one short
   workflow (`AgentOperation`, a fetch bounded by its run timeout). It is not a worker
   deployment: an activity has nothing to pin, and the agent pod restarts with `Recreate`
   inside 30 s, where a drain cannot wait. `AgentOperation` changes go behind
   `patched()`, and a recorded history (`test/fixtures/agent_operation_histories/`) is replayed
   in CI, as `PrintRunWorkflow`'s are. Shutdown: `worker.shutdown()` with a 10 s
   `shutdownGraceTime`, before the turns drain.
10. **The workflow is bundled at build.** `pnpm build` runs `bundleWorkflowCode` over
    `dist/temporal/workflows.js` into `dist/temporal/workflow-bundle.js` (the SDK's production
    path); tests pass `workflowsPath` to the source.
11. **The record is `ai_operations`.** The backend's `operations` is the backend's table; the
    agent owns its `ai_*` tables (CLAUDE.md). Same columns, same unique
    `(workflow_id, workflow_run_id)`, same `operation_key` (sha256 of kind, subject, canonical
    body, `Idempotency-Key`). Finished rows older than 7 days (the namespace's 168 h) are
    pruned hourly. `GET /api/v1/ai/operations/{id}` answers one, with the UI read guard; a
    `running` row whose execution is closed or gone is finished there as lost
    (the backend's `OPERATION_LOST` wording).
12. **Install and re-pin are both commands.** §10 names the install; §4.1 classifies by effect,
    and a re-pin is the same fetch. Approve, enable, discard and delete are one Postgres
    statement each (and an eviction from the cache) and stay requests (§4.1). Both kinds are
    `done`: 201 (install) / 200 (re-pin) with today's body inside the deadline, 202 with the
    operation past it. Check (no fetch): the source or ref validates (400), the package exists
    for a re-pin (404). Run: the fetch, vet and pin under the existing concurrency cap,
    `run_attempts` 1, run timeout 5 min. Refusals keep today's statuses and bodies (422 with
    `problems`, 409, 400) as the operation's problem with `extensions`.
13. **Without `Idempotency-Key`** each request is its own command (the backend's rule).

## Global Constraints

- Migrations: a NEW file `agent/src/db/migrations/<UTC yyyymmddThhmmZ>_operations.sql`; never
  edit a merged one.
- Follow the Temporal TypeScript SDK as documented; stop and report on any deviation.
- `@temporalio/*` pinned exactly (1.24.0), all the same version; Node 24 unchanged.
- `config.ts` reads only infrastructure variables; `ENV_VARS` lists every one.
- Never commit `backend/openapi.json`, a `schema.d.ts`, or `dist/tools.json`.
- Commits: conventional, naming `(#1055)`, ending with a blank line and
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Tests never call Anthropic. Temporal tests skip unless `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER`
  names a Temporal CLI (or `temporal` is on `PATH`); Postgres tests skip without
  `SCADBUDDY_TEST_DATABASE_URL` (locally `…:55432/scadbuddy_test_impl1055`).

## Review Focus

1. A tool activity runs with exactly the classic harness's context: the session owner's
   principal and tiers, `session` (commit trailers, `touched`), the session-bound browser,
   `lookup` for `confirm_action`; and a workflow that is not a known session is refused
   before any tool runs.
2. `gate: 'workflow'` skips the prepare step only for calls that came through an activity;
   `/mcp` (gate unset) still prepares outward calls, and the harness is unchanged.
3. Errors: a tool's error result is a non-retryable failure (never retried into a second
   effect), while a database blip during the session lookup is retried.
4. The install command keeps today's answers (201 body, 400/404/409/422 bodies with
   `problems`) inside the deadline; a repeat with the same `Idempotency-Key` after it finished
   answers from `ai_operations` and fetches nothing; a refusal before the record lets the same
   key start again.
5. Shutdown order: the worker stops polling and finishes (or cancels after 10 s) its running
   activities before the pool closes, within the pod's 30 s.

---

### Task 1: Dependencies and configuration

**Files:** `agent/package.json`, `agent/pnpm-lock.yaml`, `agent/pnpm-workspace.yaml` (if a new
dependency has a build script), `agent/src/config.ts`, `agent/test/config.test.ts`.

- [ ] Failing test: `loadConfig` reads `SCADBUDDY_TEMPORAL_ADDRESS` (blank → undefined, a value
  with whitespace inside → `ConfigError`), `SCADBUDDY_TEMPORAL_NAMESPACE` (default
  `scadbuddy`), `SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES` (`true`/`1` → true, else false); and
  `ENV_VARS` lists them.
- [ ] Run, expect FAIL; implement; PASS.
- [ ] `pnpm add -E @temporalio/client@1.24.0 @temporalio/worker@1.24.0 @temporalio/workflow@1.24.0
  @temporalio/activity@1.24.0 @temporalio/common@1.24.0` and `pnpm add -DE @temporalio/testing@1.24.0`;
  resolve any `allowBuilds` prompt in `pnpm-workspace.yaml` with a comment saying why.
- [ ] Commit `build(agent): pin the Temporal TypeScript SDK and read the Temporal address (#1055)`.

### Task 2: The tool manifest

**Files:** Create `agent/src/tools/manifest.ts`, `agent/src/export-tools.ts`,
`agent/test/manifest.test.ts`; modify `agent/package.json` (`build` writes `dist/tools.json`;
`gen:tools`).

**Interfaces:** `ToolManifestEntry = {name, description, input_schema, tier}`;
`toolManifest(tools?: readonly Tool[]): Promise<ToolManifestEntry[]>` (sorted by name).

- [ ] Failing test: the manifest's names are `ALL_TOOLS`', each `tier` is the tool's `risk`,
  and `{name, description, inputSchema}` equal the `/mcp` listing exactly; `node
  dist/export-tools.js <tmp>` is not run in vitest (the build covers it), the CLI's
  `writeManifest(path)` is.
- [ ] Implement via `createExternalServer(tools, inert services)` + `InMemoryTransport` +
  `Client.listTools()`.
- [ ] Commit `feat(agent): export ALL_TOOLS as a manifest for the durable worker (#1055)`.

### Task 3: Tool activities

**Files:** Create `agent/src/temporal/toolActivities.ts`, `agent/test/toolActivities.test.ts`;
modify `agent/src/tools/registry.ts` (`gate?: 'harness' | 'workflow'`, gate only when unset).

**Interfaces:** `ToolActivityDeps = {services: ToolServices; sessions: SessionOwners; audit?:
AuditLog; heartbeatMs?: number}`; `SessionOwners = {ownerOf(id): Promise<Owner | undefined>}`
(`PgSessionOwners(sql)` reads `ai_sessions`); `toolActivities(tools, deps):
Record<string, (input: unknown) => Promise<unknown>>`; `sessionOf(workflowId)`.

- [ ] Failing tests (`MockActivityEnvironment` with `info.workflowExecution.workflowId` and
  `activityId`): runs the tool as the owner and returns its content; the backend call carries
  the session's authorship header; `touched` records; an error result / a tier refusal /
  invalid arguments throw a non-retryable `ApplicationError` `ToolError`; an outward tool runs
  (no pending action) with `gate: 'workflow'` while `runToolWithOutcome` without a gate still
  prepares one; an unknown session and a non-`session-` workflow are refused non-retryably and
  run nothing; a failing owner lookup throws a retryable error; a browser tool with no tab
  fails at once ("no browser attached"); an audit row per call; cancellation aborts the signal.
- [ ] Implement; PASS; `pnpm exec vitest run test/toolActivities.test.ts test/projections.test.ts
  test/mcp.test.ts`.
- [ ] Commit `feat(agent): every tool as an activity on agent-tools (#1055)`.

### Task 4: The worker in the agent service

**Files:** Create `agent/src/temporal/worker.ts`, `agent/src/temporal/names.ts`,
`agent/test/support/temporal.ts`, `agent/test/temporal.worker.test.ts`,
`agent/test/support/toolWorkflows.ts`; modify `agent/src/main.ts`, `agent/src/app.ts`
(`/healthz` `temporal`), `agent/test/app.test.ts`.

**Interfaces:** `AgentWorker.start({address, namespace, activities, workflows, codec?})`,
`.state(): 'connecting' | 'running' | 'failed'`, `.stop()`; `TASK_QUEUE = 'agent-tools'`.

- [ ] Failing integration test (skips without a CLI): a test workflow on another queue calls
  `get_settings` on `agent-tools` with workflow ID `session-<id>`, gets the content; a codec
  spy on the worker sees `{type: 'activity', workflowId: 'session-<id>'}` (ruling 1).
- [ ] `/healthz` test: `temporal` is `not configured` / `connecting` / `ok` / `unavailable`.
- [ ] Implement; wire `main.ts` (start after the services exist; stop first in `stop()`).
- [ ] Commit `feat(agent): run the agent-tools worker in the agent service (#1055)`.

### Task 5: `ai_operations`

**Files:** Create `agent/src/db/migrations/<stamp>_operations.sql`,
`agent/src/operations/store.ts`, `agent/test/operations.pg.test.ts`.

**Interfaces:** `Operation = {id, kind, subject, status, request, result, error, created_at,
finished_at, repeated?}`; `OperationError = {status, title, detail, type?, extensions?}`;
`OperationStore(sql)`: `insert(...)` (ON CONFLICT on the execution, returns the row),
`find(key)`, `get(id)`, `finish(id, {result} | {error})` (only a running row), `prune(olderThan)`;
`operationKey(kind, subject, body, requestId)`.

- [ ] Failing pg tests: insert twice for one execution returns one row; finish is guarded;
  find by key returns the newest; prune leaves running rows.
- [ ] Implement; PASS. Commit `feat(agent): ai_operations, the agent's command record (#1055)`.

### Task 6: `AgentOperation` and its kinds

**Files:** Create `agent/src/temporal/workflows.ts`, `agent/src/temporal/operationActivities.ts`,
`agent/src/operations/kinds.ts`, `agent/src/operations/packageKinds.ts`,
`agent/scripts/bundle-workflows.mjs`, `agent/test/agentOperation.temporal.test.ts`,
`agent/test/fixtures/agent_operation_histories/install.json`; modify `agent/package.json`
(build bundles), `agent/src/main.ts`.

- [ ] Failing integration tests (Temporal + Postgres): a refusal answers and fails the
  execution with no row; success inserts, runs and finishes, the Update answers with the row;
  a run failure is a recorded failed operation and the execution completes; a cancel before
  the run records the cancelled problem; the replay of the recorded history passes.
- [ ] Implement the workflow (check `start_to_close` 8 s with 3 attempts; insert/finish retried
  forever; run with the kind's attempts and timeout, heartbeat 30 s) and the activities.
- [ ] Commit `feat(agent): AgentOperation, the command shape on agent-tools (#1055)`.

### Task 7: Install and re-pin as commands

**Files:** Create `agent/src/operations/command.ts`, `agent/src/routes/operations.ts`,
`agent/test/operations.routes.test.ts`; modify `agent/src/routes/pluginPackages.ts`,
`agent/test/pluginPackages.pg.test.ts`, `agent/src/main.ts`.

- [ ] Failing tests: the routes answer through `commands` (a fake) with 201/200, 202 + the
  operation, the recorded problem with `problems`; 503 without commands; `GET
  /api/v1/ai/operations/:id` 200/404 and the read guard; the pg route test runs install and
  re-pin through the real workflow when Temporal is available.
- [ ] Implement `startCommand` (record first; update-with-start; still-accepting 503 with
  `Retry-After: 2`; Temporal unavailable 503; already-closed answered from the record).
- [ ] Commit `feat(agent): plugin package install and re-pin are commands (#1055)`.

### Task 8: The frontend follows the agent's operations

**Files:** `frontend/src/api/client.ts` (`command` takes the follow path), `frontend/src/api/aiPlugins.ts`,
`frontend/src/mocks/aiPlugins.ts`, `frontend/src/components/settings/PluginPackages.test.tsx` or
`frontend/src/api/aiPlugins.test.ts`.

- [ ] Failing test: install sends an `Idempotency-Key`, and a 202 is followed through
  `/api/v1/ai/operations/{id}` to the package, or to its 422 `problems`.
- [ ] Implement; `pnpm lint && pnpm typecheck` and the touched tests.
- [ ] Commit `feat(frontend): plugin package install follows the agent's operation (#1055)`.

### Task 9: CI and image

**Files:** `.github/workflows/ci.yml` (agent job: the pinned Temporal CLI, sha256-checked, and
`SCADBUDDY_TEST_TEMPORAL_DEV_SERVER`), `Dockerfile` (comment only if needed).

- [ ] Add the step; `actionlint`; `docker build --target agent` if time allows.
- [ ] Commit `ci(agent): run the agent's Temporal tests on the pinned dev server (#1055)`.

### Task 10: Docs

**Files:** spec §10 phase 4 "As built", `CLAUDE.md` (agent section), `README.md` (the agent
container's `SCADBUDDY_TEMPORAL_ADDRESS`).

- [ ] Commit `docs: the agent-tools worker and the tool manifest (#1055)`.

## Clusters changes (for the lead, eh-homelab/clusters `applications/scadbuddy/`)

- The `agent` container: `SCADBUDDY_TEMPORAL_ADDRESS` and `SCADBUDDY_TEMPORAL_NAMESPACE` as the
  API container has them (and `SCADBUDDY_TEMPORAL_SEARCH_ATTRIBUTES` once registered).
- Its NetworkPolicy egress must allow the Temporal frontend (7233), as the API's does.
