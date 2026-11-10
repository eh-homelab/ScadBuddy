# Agent service — notes for agents

These notes cover `agent/`; paths are repo-relative; the root CLAUDE.md has the repo-wide rules (migrations, CI, PR conventions, generated API files).

## Database access

The rule is in the root CLAUDE.md, "Database access (#5295)". Today the agent sidecar and
the agent workers connect as the database owner (`SCADBUDDY_DATABASE_URL`), keep the
`ai_*` tables, and run SQL on backend tables (`events`, `workflow_runs`,
`workflow_run_decisions`, `flow_grants`, `flow_grant_uses`;
`backend/scadbuddy/auth/caller_grants.py` `AGENT_GRANTS`). The workspace files' `blob_refs`
holds go through CRUD routes (#5299, below), no longer a trigger. `src/db/grants.ts`
grants the API's role its `ai_*` reads and writes, and revokes what it once granted the
projects worker's role `scadbuddy_projects` (#5298; the worker holds no database).
Claude Code, and any plugin package approved with `allow_refused`, runs in the same pod
as the owner URL and the KEK (`docs/ai/security.md`, "Plugin packages").

Under the rule the agent holds no database secret. Its data (sessions, transcripts,
approvals, credentials, payload keys) goes behind CRUD routes, the cross-service reads
become routes, and Claude Code and plugin code run where no database secret exists
(#5299). Its 8081 routes move to `enforce` (#5297). Never add a backend table read, a new
`ai_*` grant or a trigger on another service's table; add a CRUD route that requires a
service credential instead.

The agent's service credential (`src/api/serviceCredential.ts`, wired in `main.ts`): with
`SCADBUDDY_SERVICE_TOKEN_FILE` or `SCADBUDDY_SERVICE_BOOTSTRAP_FILE` (both is a `ConfigError`)
`ExchangeCredential` exchanges that proof at the backend's `POST /api/v1/auth/service-token`
for a 15-minute credential, renews it at half-life and after a 401, and falls back to the view
on a 404 while it has a database. A failed exchange is tried again after 5 s doubling to 60 s,
but a refused connection (the backend not up yet, `src/api/backendUp.ts` `backendNotUp`, #5868)
is tried again at the next call and logged once as "waiting for the backend". Without a proof `ServiceCredential` reads the sealed one from
`service_credentials_agent`, as before (#5494).

The agent's own CRUD routes (`/api/v1/internal/crud/agent/`, class `crud.agent`, #5330): the
holds of the workspace files' blobs (`workspace-blob-refs`, #5299; see the workspace under
"Layout"), and the assistant's spend on an order. A session's successful `order_item_revision_add` (or
`_after_acceptance`) is an `ai_session_order_costs` row (`src/sessions/orderCosts.ts`
`OrderCosts`, a `TouchedSink` beside `SessionResources`); each turn's end
(`temporal/session/finishActivity.ts`, a retry too, not awaited: it never holds up the next
turn) reports the session's `cost_usd` since its last report, split equally between its
orders still to report, to `POST …/crud/agent/orders/{id}/assistant-cost` (5 s each, 15 s
for the turn's reports in all, one report per session at a time across replicas, by an
advisory lock on a `database.locks` connection, never the stores' pool, and none while every
lock connection is out; `sweep` at start and hourly reports what a session's last turn left,
20 sessions at most, the ones tried longest ago, `tried_at`, first; a share under one minor
unit is closed out with nothing carried once its session is over: `done`, `failed`, or `idle`
with no session activity, `updated_at`, for an hour, `IDLE_OVER_AFTER`; if it works again,
that spend folds into its next turn's share, split equally across the orders that turn revises). `record` waits at most `RECORD_WAIT_MS` for its row (#1076). Only the
route's own 400 or 422 (a path or body it cannot take), 404 (the order gone: an item gone
falls back to the order) or 409 (a currency no rate reaches, a visitor's unsubmitted cart) drops
that order's report, its share in no entry. Anything else (401 or 403 while there
is no live credential, 429, a 5xx, a transport failure, a non-CRUD answer) keeps it with the
share it sent (`attempted_usd`), sent again as it was at the next turn's end, so a lost
answer loses no spend; an answer with no entry (under one minor unit) or the deadline keeps
it too.

## Commands

Agent service (`agent/`, Node 24, pnpm via corepack; the `agent` CI job):

```bash
cd agent
corepack enable
pnpm install --frozen-lockfile
pnpm lint && pnpm typecheck && pnpm test && pnpm build
docker build --target agent -t scadbuddy-agent:dev .   # checks the bundled Claude Code binary
```

Tests never call Anthropic. `test/run.test.ts` runs the bundled Claude Code binary
against a local fake Anthropic endpoint; `test/pg.test.ts` needs
`SCADBUDDY_TEST_DATABASE_URL` (e.g. `docker run -d -e POSTGRES_PASSWORD=postgres
-e POSTGRES_DB=scadbuddy_test -p 5432:5432 postgres:18`, then
`SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test pnpm test`).
With it set, `pnpm test`'s globalSetup migrates a template database once and each
Postgres test copies it (`test/support/postgres.ts`, #2018), so the test role needs
`CREATEDB`, as the backend's does: without it the whole suite fails at setup, not
only the Postgres tests.
Evals (`agent/evals/`, `docs/ai/evals.md`): each scenario is a session on the one runtime,
an AgentSession that an `api` process starts and a `worker` process runs (`evals/durable.ts`
`startEvalStack`: Temporal, a throwaway Postgres, both roles in-process; plan 2026-10-13
Step 6.6). `test/evals.durable.e2e.test.ts` replays each scenario against the fake endpoint
in `pnpm test` (it skips without Temporal or Postgres); `pnpm evals` runs them live with the
credential saved in Settings (or `SCADBUDDY_EVAL_ANTHROPIC_API_KEY`, CI only; the
manual `ai-evals.yml` workflow) and skips cleanly without one. The render scenario
runs live only against a real backend (`SCADBUDDY_EVAL_BACKEND_URL`, the image; it
skips without one), and its render is checked `verify.sh`-style on the saved output's
3MF; replies with suggestions are scored on their citations (`evals/citations.ts`,
#1924).
`.github/scripts/lint-one-runtime.sh` (the `lint` job) fails on a second turn path: a session
mode branch in `agent/src`, the SDK's `query` imported outside `harness/run.ts` and
`harness/testConnection.ts`, or `agent-durable/` importing or depending on the Claude Agent
SDK or naming `DurableSession` (plan Step 8.1).

`agent/test/fixtures/secret-vectors.json` is written by `agent/test/secretVectors.test.ts`
(`UPDATE_SECRET_VECTORS=1`) and opened by `agent-durable/tests`: a change to
`agent/src/secrets.ts`'s format or `credentials.ts`'s AAD needs new vectors, and the port must open them.
`agent/test/fixtures/payload-vectors.json` does the same for the payload codec
(`agent/test/payloadCodec.test.ts`, `UPDATE_PAYLOAD_VECTORS=1`; `agent-durable/tests/test_codec.py`
opens every one and seals it again byte for byte).


## Layout

- `agent/` — the AI agent service (#261), TypeScript on the Claude Agent SDK, shipped
  as the Dockerfile's `agent` target and run as a sidecar container of the API pod (role
  `api`) and as the `scadbuddy-agent-worker` Deployment (role `worker`, clusters#3014). `src/config.ts`
  reads only infrastructure variables (`ENV_VARS`): `SCADBUDDY_DATABASE_URL`,
  `SCADBUDDY_BACKEND_URL`, `SCADBUDDY_SECRET_KEY_FILE`,
  `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` (rotation), `SCADBUDDY_PUBLIC_URL` (the same
  variable the backend reads; the one origin allowed to write),
  `SCADBUDDY_AGENT_TRUSTED_PROXIES` (CIDRs whose `X-Forwarded-*` are believed) and the
  backend's `SCADBUDDY_TEMPORAL_ADDRESS`, `_NAMESPACE` and `_SEARCH_ATTRIBUTES`. No AI
  env vars; AI settings live in the database.
  `SCADBUDDY_AGENT_ROLE` (#2726 Step 6.1; `all` by default, `api`, `worker`) picks what
  `main.ts` starts (`src/runtime/role.ts` `roleParts`): `api` is the HTTP half and polls
  only its own `agent-tools.<hostname>` (browser calls for its tabs), so it runs no
  `AgentSession` turn; `worker` runs the turn workers and serves only
  `src/runtime/health.ts` (`/healthz`, `/readyz`). `test/agentRole.temporal.test.ts` runs
  one of each on one dev server; a change to what a turn reaches in-process must keep it.
  One runtime (#2726, spec 2026-10-13, plan `docs/superpowers/plans/2026-10-13-unified-agent-runtime.md`):
  every session is an `AgentSession` workflow, `session-<id>` on `agent-sessions`
  (`src/temporal/session/workflow.ts`, deployment `scadbuddy-agent`, AUTO_UPGRADE: a change goes
  behind `patched()`, and `test/fixtures/agent_session_histories/` replays). Each turn is its
  `session_turn` activity on the session host queue `agent-sessions.<hostname>` that
  `acquire_worker` reserved a warm place on (`affinity.ts`, §5.2), run by
  `sessions/turnHost.ts` over `turnCore.ts` and `turnRunner.ts` (the turn's assembly) on the
  replica's warm Claude Code process (`sessions/warm.ts` `WarmSessions`, sized by the
  `session-runtime` settings, `runtimeSettings.ts`; a process is recycled when its fingerprint,
  transcript tail or budget changes). A park holds the process for `park_hold_seconds`, then
  closes it and the activity returns `released`; once every held call is decided, the turn is
  placed again with a `parked` continuation that runs each approved call once (§5.4). A turn
  lost with its pod is placed again with a `restarted` continuation, which resumes with
  `RESTARTED_PROMPT` once its message is in the transcript (§5.5). Every tool call
  of a turn on Temporal is a `ToolCall` workflow of its own, `session-<id>-call-<tool_use_id>`
  (`temporal/toolCall.ts`, `tools/activityProjection.ts`, plan Steps 9.2 and 9.3; the
  `tools_as_activities` switch is gone, Step 9.9), started with `requestEagerStart` on the turn's
  replica's own agent-tools worker connection while it polls, once its build has been made current (AgentWorker
  `connection`, #5865; it stays so when a later build is promoted, and Temporal grants an eager start
  to such a superseded build, whose worker then runs the call: measured on a dev server, Server
  1.32.0, `toolCallLatency.temporal.test.ts` `SUPERSEDED`; the call's span attribute `scadbuddy.eager` says
  `started`, `declined`, `failed` or `fallback`): the first workflow task comes back in the start's answer
  without waiting on matching, and the activity is dispatched eagerly to the same worker when it is
  on agent-tools and the worker has a free slot (a pinned server's call goes to `agent-tools.<hostname>`
  through matching). Both need the server's `system.enableEagerWorkflowStart` and
  `system.enableActivityEagerExecution`, on by default at Temporal v1.32.1
  ([constants.go](https://github.com/temporalio/temporal/blob/v1.32.1/common/dynamicconfig/constants.go))
  and not overridden in prod's `scadbuddy-temporal-v2-dynamicconfig` (read 2026-11-10);
  `test/toolCallLatency.temporal.test.ts`
  times each phase around the activity (`TOOL_CALL_BENCH_REPORT=1` prints them, `TOOL_CALL_BENCH_ASSERT=1`
  fails a median overhead over 1 s) and checks how each call was started; without Temporal the calls run in the turn's
  process. A session's working directory is `<SCADBUDDY_SESSION_DIR>/<id>/` (see the workspace
  below); a turn whose transcript last ran in another directory (a fork's first) is told so in
  its prompt (`sessions/directoryNote.ts`, #5867). Spans: `agent.turn` (with `scadbuddy.continuation` on a continued placement),
  `agent.turn.spawn` (`warm.outcome`), `agent.turn.release`, the turn's span of each call with
  `scadbuddy.eager` (above), and each ToolCall's
  `agent.tool/<name>` with `scadbuddy.task_queue`, which `deploy/grafana/scadbuddy-agent.json`
  charts.
  `src/app.ts` is the Hono server (`/healthz` and `/mcp`). Every other route group is a
  `src/routes/<name>.ts` that exports `route` (`routes/module.ts`) and is found without
  an edit to `app.ts`: among them `credentials.ts` (`/api/v1/ai/credentials`),
  `status.ts` (`/api/v1/ai/status`), and the assistant's WebSocket `/api/v1/ai/chat` and
  `/api/v1/ai/sessions` in `chat.ts` and `sessions.ts`, backed by `SessionManager`. A
  dependency only that group needs is declared in its own file, by augmenting `AppDeps`
  (#508); one several groups share (`sessions`, `upgradeWebSocket`) stays in `app.ts`.
  A route module imports only types from `app.ts` (`routes/index.ts` loads them while
  `app.ts` loads). Every route that must know "is this the UI's origin"
  (credential writes, `/mcp`, and the chat socket and session routes under `/api/v1/ai/*`) uses the one allowlist in
  `src/http/origins.ts`, never an `Origin == Host` comparison (DNS rebinding makes
  those equal). `src/harness/options.ts` builds every query's SDK options
  (`tools: []`, `settingSources: []`) and `src/harness/run.ts` runs every `query()` on
  top of it (never the credential in Claude Code's env: each run leases a token on the
  loopback credential gateway, `src/gateway/server.ts` and `tokens.ts`, plan Step 9.4, which
  adds the credential and meters the spend, so the env carries only `ANTHROPIC_BASE_URL` and
  that token; `maxTurns`, `maxBudgetUsd`,
  abort, the tier seam in `src/harness/permissions.ts` as both `canUseTool` and a
  `PreToolUse` hook; outward calls in a session PARK in `canUseTool` on the session
  workflow's gate until a human decides (`sessions/turnHost.ts`, the Update `open_input`;
  its `ai_pending_input` row is the request's record, plan Step 9.7), #258; `/mcp`'s outward
  calls are `src/approvals/service.ts`'s `ai_approvals` rows, run by `confirm_action`; outside
  a session they are denied as "needs approval"; the built-in `AskUserQuestion`, given
  only to sessions the browser user owns, parks there too until the user answers in the
  panel, via `src/harness/questions.ts` and the session workflow's gate (an `ai_pending_input`
  entry, the request's record, plan Step 9.7), #940, and never outlives its turn; the agent's
  `request_user_attention` tool, #815, `src/harness/attention.ts`, parks on the same gate as a
  `wait_for_user` entry, with a timer that never answers: `proceed` returns `timed_out`, `wait` and
  `stop` end the turn; its `done` reason waits for nothing and outlives its turn, an `ai_questions`
  row (`src/gate/questions.ts`), shown beside the badge's waiting count (not in it) until dismissed, at most
  `DONE_POSTS_PER_TURN` a turn, carrying `src/gate/doneSummary.ts`'s record of what the turn
  touched, unattended actions first; `GET /api/v1/ai/pending-input`, `src/routes/pendingInput.ts`, is
  the one read of every parked call, approvals and answers, that the badge counts, and
  `POST /api/v1/ai/pending-input/{request_id}` the one respond route the panel answers
  any of them through, refusing a stale id, a resolved entry or a body of the wrong kind;
  a browser_* call that finds no tab in such a session parks the same way as a
  `tab_disconnected` request, resolved `reconnected` when the bridge sees the session's
  tab again, `sessions/manager.ts` `waitForTab`, `bridge/hub.ts` `onSessionTab`; in an
  AgentSession turn both park as the workflow's `wait_for_user` entry, `sessions/turnHost.ts`,
  and the hub's process ends a tab wait with the Update `tab_reconnected`, #5003);
  `src/api/backend.ts` is the `openapi-fetch` client over the generated
  `src/api/schema.d.ts`.
  Tracing (#988): `src/telemetry.ts` is the `node --import` entry (Dockerfile `CMD`,
  `pnpm start`) that registers the OTel ESM hook, then `src/telemetry/setup.ts` starts
  the SDK (standard `OTEL_*` variables only; incoming HTTP only). `main.ts` imports
  only the SDK-free `src/telemetry/runtime.ts`, so with `OTEL_SDK_DISABLED=true` no SDK
  package is loaded (#1351).
  `src/telemetry/scrub.ts` strips exception messages, query strings and user agents
  before export; `src/telemetry/turn.ts` (`TurnTrace`) ends a turn's spans at every
  park and opens `agent.turn.resume` under the decision (`ai_approvals.traceparent`,
  `decision_traceparent`). Only `api/backend.ts`'s middleware injects `traceparent`;
  never add trace context to another outgoing call. Tests share one provider
  (`test/support/tracing.ts` `testTracing`).
  `src/tools/` is the tool registry (#251): one `defineTool`
  per tool, projected in-process for the harness and over `/mcp` (`src/mcp/http.ts`,
  auth in `src/auth/`); every `/api/v1` operation needs a tool or a
  `src/tools/coverage.ts` entry, or `test/coverage.test.ts` fails.
  - Database: the agent owns the `ai_*` tables (today's state; #5295 moves them behind
    CRUD routes and takes the agent's database secret away, #5299). Schema changes are new files in
    `src/db/migrations/` (see "Migrations" below; `src/db/migrations.ts` applies them at
    start under advisory lock "SCADAGNT" with `lock_timeout`/`statement_timeout`,
    ledger `ai_migrations` with a sha256 per file: an edited merged file stops the
    service at start; separate from the backend's `scadbuddy_migrations`). Secrets are
    envelope-encrypted with `src/secrets.ts` under the KEK in
    `SCADBUDDY_SECRET_KEY_FILE` (32 random bytes, base64; spec §9); the AAD binds each
    value to its row and to the columns that say where it is sent (for the credential:
    `kind` and `base_url`). Comparable tokens are stored hashed instead.
  - Temporal (#1055, spec 2026-10-01 §6.3): with `SCADBUDDY_TEMPORAL_ADDRESS` (and the
    database) `src/temporal/worker.ts` runs one worker on `agent-tools`, a version of
    `scadbuddy-agent` with `agent-sessions` when the build has an id (#2726 Phase 11; the
    host queues stay unversioned, `agent/test/temporal.versioning.temporal.test.ts`):
    every `ALL_TOOLS` entry as an activity under its name (`toolActivities.ts`,
    `runToolWithOutcome` with `gate: 'workflow'`, only for a `session-<id>` workflow (as the
    session's owner with its turn's tiers; `mode` is not read, since every session is an
    AgentSession from plan Step 6.3), or a flow run's `flow-<id>` (`flowRuns.ts`, #1057: as the
    run's starter, in no session; a gated call needs the backend's `workflow_run_decisions`
    approval; no browser tools)), a turn's other tools (#2320, plan Step 9.3,
    `turnTools.ts`, built by `turnServers.ts`: remote plugins
    through the forwarder, packages' http MCP servers and `http_request`): `listTurnTools` lists them per turn as
    `<server>__<tool>`, `turn_tool_tier` tiers a call from its input before it may park,
    and each call is the worker's `default` activity `turn:<server>__<tool>`, which builds
    the servers again and checks tier, principal and approval as it runs (a `flow-<id>`
    workflow's call too: as the run's starter, approved only by `workflow_run_decisions`,
    with no browser servers; a server whose
    state lives on the pod, `http_request` and the browser, is `pinned`: its calls run on
    `agent-tools.<hostname>`, which each agent also polls); and `AgentOperation` (`workflows.ts`, bundled by
    `pnpm build` into `dist/temporal/workflow-bundle.js`), the §4.2 command shape for
    the agent's commands, recorded in `ai_operations` (`src/operations/`). A change to
    `AgentOperation` goes behind `patched()`; `test/fixtures/agent_operation_histories/`
    replays. Temporal tests (`test/*temporal*.test.ts`)
    skip unless `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` names a Temporal CLI (or
    `temporal` is on `PATH`); the `agent` CI job installs the Dockerfile's pinned one.
    The `@temporalio/*` packages are pinned exactly, all one version.
  - The tool-call gate (#1056 phase 5b, spec 2026-10-01 §6.6, plan
    `docs/superpowers/plans/2026-10-08-durable-phase-5b-tool-call-gate.md`) is
    `src/gate/`: request ids (`ids.ts`: `approval:`, `question:`, `durable:<session>:<run>:<tool_use_id>`),
    the `respond` validator shared with agent-durable (`validate.ts`; `role.ts`;
    `test/fixtures/pending-input-vectors.json` pins the two languages together),
    the entry shapes and the projection read (`projection.ts`, `ai_pending_input`), the
    record's writers (`store.ts`: `ai_pending_input.gate` is an entry's record and
    `decideInput` decides it in one transaction, then the Signal `input_decided` wakes the
    workflow, which also re-reads it through `gate.read_input`, plan Step 9.7), the Temporal
    client of a session's gate handlers (`durable.ts` `DurableGate`), the `done` summaries and
    attention limits (`questions.ts`, `doneSummary.ts`, `waiting.ts`; `gate/classic.ts` and
    `questions/` were folded in, #5110) and the orphan sweep (`sweep.ts`). The workflow's half is
    `temporal/session/gate.ts` (`SessionGate`) and `gateActivities.ts`. `routes/pendingInput.ts` serves
    both reads and `respond`. `input.requested` / `input.resolved` are logged and never
    sent on the chat socket (`routes/chat.ts` `sentToPanel`).
  - Durable payloads (#1056 phase 5c, spec §6.5): `src/temporal/payloadCodec.ts` seals
    every payload of a `session-<uuid>` / `flow-<uuid>` workflow under that subject's
    data key (`ai_payload_keys`, sealed under the KEK, re-wrapped at start on rotation);
    the agent-tools worker and the service's Temporal client carry it when the KEK is
    mounted. `src/sessions/forget.ts` `forgetSubject` (key row, then the workflow, then
    the rows) is the one way a durable subject is deleted; operators run
    `node dist/forget-subject.js session-<uuid>`. A fork reads its parent's transcript
    through the cut (`ai_sessions.fork_parent_id`, `fork_cut`; `src/sessions/store.ts`,
    plan Step 6.5), so forgetting a parent first copies each fork's range into it under
    the fork's key (`copyForkRanges`, `inherited` rows), and refuses without the KEK. A gated tool's activity runs only
    with an `approved` `ai_input_responses` row for its own request id.
  - DurableSession, the retired Python turn (plan 2026-10-13 Steps 7.2 and 8.1, #4772): its
    open sessions were moved to AgentSession on 2026-11-01 (their conversations in
    `ai_session_entries`). A row still `runtime = durable` closed before that and has no
    transcript: a send to it is refused `closed` (`sessions/durable.ts`), and a fork of it is
    an AgentSession with the panel's history and no model context (`manager.ts` `fork`).
  - The workspace (#2329, `docs/ai/operating.md` §13): `src/workspace/store.ts`
    `SessionFiles` over `ai_session_files`, keyed by `workspace` (`session:<id>`, gone
    with its session, copied by a fork; or `principal:<kind>:<id>` outside a session,
    swept 24 h after its last write) and a checked `path`; caps
    `workspace_file_max_bytes`/`workspace_max_bytes` and `WORKSPACE_MAX_FILES` (5,000). Reached as
    `ToolServices.workspace` (so in-process, `/mcp` and the tool activities alike),
    with `workspaceOf(ctx)`; the `workspace_*` tools are `src/tools/workspace.ts`, and
    `http_request`'s `save_to_workspace` gets the session's from `sessions/manager.ts`.
    Never a file on the pod; a session's working directory, `<SCADBUDDY_SESSION_DIR>/<id>/`
    (each worker's own capped emptyDir, Step 9.5; not the NAS share, whose backup cannot leave
    the plaintext copies out), is a copy of it that each turn's placement fills and each park
    and its end write back by sha256 (`src/workspace/directory.ts`). The bytes (#4789) are a blob of the backend's content store,
    sealed under a per-file key wrapped under the session's data key (`src/workspace/sealing.ts`,
    `blobs.ts`; backend `api/internal_workspace.py` mints the URLs; the `blob_refs` holder
    `(ai_session_file, <workspace>/<path>)` goes through the agent's CRUD routes, backend
    `api/crud_agent.py` `…/crud/agent/workspace-blob-refs`, #5299: `SessionFiles` holds before
    the row that names a blob and releases after the row is gone, and `reconcile`, on the
    sweeper's tick hourly, releases what cascades, `forgetSubject` and crashes left and holds
    a row no hold names; no trigger;
    `test/workspaceBlobs.backend.e2e.test.ts` runs it against the real API on moto,
    `backend/tests/support/workspace_backend.py`). Only on `store_backend` local do they stay
    in `data`; on any other backend a write that cannot be sealed and stored is refused
    `unavailable`, never kept in plaintext. Uploads take `workspace_path` + `workspace_sha256` instead of
    inline content (`src/tools/workspaceUploads.ts`: binary streamed by
    `SessionFiles.chunks` into the multipart route, text decoded as UTF-8).
    The panel's Files (part 3) are `routes/sessionFiles.ts`
    (list, download as an attachment, multipart upload, delete; the session routes'
    guards, writes owner-only, each write a `workspace.file` event) and the caps' route
    `routes/workspaceSettings.ts`; the frontend is `components/assistant/SessionFiles.tsx`
    with msw `src/mocks/features/sessionFiles.ts` (component tests stub the upload:
    jsdom's `File` cannot pass undici's multipart parser). An `/mcp` client puts a file
    into its principal workspace with `PUT /mcp/workspace/<path>` (#4550,
    `src/mcp/uploads.ts`, mounted by `mountMcp` behind `/mcp`'s gates and auth; `write`
    tier, `disabled` mode refused), raw bytes answered with `{name, mediaType, size, sha256}`.
  - Plugins given to the harness are vetted by `src/harness/plugins.ts`: anything that
    starts a process (command hooks, stdio MCP servers, LSP servers, monitors) or runs
    plugin code in Claude Code (a hooks file's `modules`, on by default since Claude
    Code 2.1.287, and `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS` no longer turns it off) is
    refused, because it would inherit the credential env. A hooks file may set only
    `$schema`, `description` and `hooks` besides, so a new loader key is refused too.
    The one way past it is a plugin package approved with `allow_refused` (store.ts,
    column `allow_refused`, bound to that pin's approval): it loads as it is through
    `allowedPluginPaths`, unchecked, with the built-ins it names (`builtinTools`, each
    call `outward`; the session's own, `SESSION_BUILTINS`, are never among them and keep
    their item's tier) and skill shell injection on (with `Bash` offered, Claude Code
    2.1.287 runs an injection without asking the seam). A name that is not a safe path segment
    (`SAFE_NAME_RE`; a reserved name is allowable), paths outside the package, symlinks,
    the hash and the egress check stay fatal (`vet.ts` `fatal`).
  - Remote MCP plugins (#297) live in `ai_plugins` (`src/plugins/registry.ts`, routes
    `src/routes/plugins.ts` under `/api/v1/ai/plugins`). Claude Code never gets a
    plugin's URL or secret: it gets `http://127.0.0.1:<port>/p/<token>` on the loopback
    forwarder (`src/plugins/forwarder.ts`), which pins the checked address, refuses
    redirects and 401/OAuth discovery, and adds the header (Claude Code's own MCP client
    follows both with the header). A plugin with no header may be signed in with OAuth
    instead (#780, `src/plugins/oauth.ts`, `oauthStore.ts`, `ai_plugin_oauth`): only
    Settings starts discovery (`POST …/:name/oauth/start`, then the browser's
    `/api/v1/ai/plugin-oauth/authorize` and `/callback`, `routes/pluginOAuth.ts`), the
    tokens stay sealed in the agent, and the forwarder asks the plugin's `oauth` token
    source for each request's header; a refused refresh marks the grant for "Sign in again". Claude Code renames tool-name characters outside
    `[A-Za-z0-9_-]` to `_` (`harnessToolName`); only such names take a tier, and
    colliding tools are hidden. Unlisted plugin tools are `outward`.
  - Customer messages (#5660, spec `2026-11-08-order-notify-customer-design.md`):
    `src/orders/messaging.ts` `pluginMessaging` opens the backend setting
    `orders_customer_messaging`'s source (a remote plugin, or a package's http MCP server,
    both through the forwarder) and calls its tool; an `McpError` is `failed`, a timeout, a
    closed connection or another error is `unknown`. `order_notify_customer` (`read`) composes
    and answers `channel` and `send_ready`; `order_messages_list` (`read`) lists the order's
    messages, whose ids `resend_of` takes; `order_notify_customer_send` (`outward`) parks the
    exact message as `approved`, claims an `order_messages` row through the backend's
    `POST /api/v1/orders/{id}/messages` (`Idempotency-Key`, the call's `idempotencyKey`),
    sends, then posts the outcome. A replayed claim sends again only when the binding maps an
    `idempotency_key` argument; otherwise a fresh `sending` row is answered as in flight (no
    outcome posted) and one that reads `unknown` is recorded `unknown`, never resent by itself.
    `routes/messagingBinding.ts` checks a binding for Settings → Orders and sends a test
    message (a person's session only, kept in the audit log as `messaging.test_send`).
    `routes/orderMessages.ts` is the order page's "Send again": a preview answers the message
    composed now and its digest, and the send takes only the template, `resend_of` and the
    digest, composes again and refuses `message_changed` on a difference (a person's session
    only; `tools/orders.ts` `previewPersonSend`, `personSend`).
  - Plugin packages (#297; skills, agents, hooks from git or a marketplace entry) live
    in `ai_plugin_packages` (`src/plugins/packages/`, routes
    `src/routes/pluginPackages.ts`). Postgres holds the pin (commit + content hash);
    `<state dir>/plugins/` is only a cache, re-hashed before every load and
    re-fetched from the pin. Install stores the pin unapproved; approving needs the
    exact commit and hash. `vet.ts` adds rules on top of `harness/plugins.ts`. Tests
    use local git repos (`test/support/gitRepo.ts`). Their MCP servers' http ones go
    through the forwarder (`plugins/packages/mcp.ts`), and on Temporal each call is a
    `turn:<server>__<tool>` ToolCall.
  - Claude Code's own built-ins (plan 2026-10-13 Step 9.6): every session offers `Read`,
    `Glob`, `Grep` and the task list (`TaskCreate`, `TaskGet`, `TaskList`, `TaskUpdate`) at
    `read`, and `Write`, `Edit` and `NotebookEdit` at `write`, native in its Claude Code
    process, each an item of the access source `builtin:claude-code`
    (`src/access/nativeBuiltins.ts`; `resolver.ts` `native`, `live.ts`). Their paths are
    confined to the session's working directory by the seam (`src/harness/confine.ts`, an
    `InputGuard` in both canUseTool and the PreToolUse hook: `..`, absolute paths and links
    out are refused, and so is any path, Glob pattern or Grep glob starting with `~`, which
    Claude Code expands to HOME after the seam for Glob and Grep, measured on 2.1.310; with
    no working directory the file tools are not offered). `Read` and `Grep` may also reach the
    session's own `tool-results` directory in the Claude Code config dir, where Claude Code
    saves an oversized tool result (#5542, `projectDirName`). `Bash` and `PowerShell` are `off`: Claude Code's sandbox cannot run in the
    worker pod (no bwrap, and `unshare --mount` is refused under AppArmor; measured
    2026-11-01), so a sandbox pod (#779) comes first. An allowed package's `builtinTools`
    only switch on one that is `off` by default, at its package tier (`outward` by default),
    never one a person switched off (the source, or the item's row); they no longer decide
    what exists. The task list is `$CLAUDE_CONFIG_DIR/tasks/<session id>/` (measured
    on SDK 0.3.310), linked to the session directory's `_claude/tasks/`
    (`stateDirs.ts` `linkTaskList`), so it moves with the workspace.
    **Breaking for package authors:** the `builtin__<Name>` file and task tools of the
    retired durable turn, and their `/workspace` paths, are gone; a skill names Claude
    Code's own `Read`, `Write` and the rest, with paths in the session directory (its cwd).
    The agent still provides `WebFetch` and `WebSearch` (egress-checked, decision Q2) and the
    background ones in place of Claude Code's (`src/builtins/server.ts`, in-process
    `builtin` server, `mcp__builtin__<Name>`), in every turn, not only one whose package names
    them (#2726 R2): each is an item of `builtin:claude-code` too (`nativeBuiltins.ts`
    `SESSION_BUILTINS`, `turnBuiltins`), `WebFetch` and `WebSearch` at `outward`, `CronList` at
    `read`, the rest at `write`; a session a flow run started or forked, a Workflow agent's chat,
    a session one of those started or forked through `sessions_*`, and a fork of any of them
    (`ai_sessions.background_tools` false, `ClaimedSession.backgroundTools`) get no background one;
    nor does a turn a flow run, or such a session, sent (`ai_sessions.turn_flow`), nor any turn
    on an agent with no `deps.background`. A
    `Monitor`'s `ws` source is `outward`, and its `command` source runs a shell where Bash runs,
    so a turn gives it a command queue only while `Bash` is on, at Bash's tier if stricter
    (`monitorAccess`, `sessionTierOf`). `CronCreate`, `CronDelete`,
    `CronList`, `ScheduleWakeup`, `Monitor`, `Workflow` and `TaskStop` are the session's own
    (`src/background/`: `ai_background_tasks`; a recurring cron is a Temporal Schedule
    `bgtask-<session>-<id>` whose `BackgroundFire` sends the prompt when the session is idle
    (`background/temporal.ts`, plan Step 9.8, its older jobs converted at start by
    `convertCronTasks`), and a wakeup or a one-shot cron one `BackgroundTask` workflow on
    `agent-tools`; a Monitor's source
    runs where the session's Bash runs, the replica's own queue, its events in
    `ai_background_events`; a Workflow's
    script runs in QuickJS (`workflowRunner.ts`, never Node's `vm`), each `agent()` a child chat
    (`workflowAgents.ts`, a chat with no background built-in of its own), its results journalled in `ai_workflow_calls`; every turn that has them
    gets them as the in-process `builtin` server in place of Claude Code's, which would end
    with its process; the panel's list and cancel are `routes/backgroundTasks.ts`).
  - Tests never call Anthropic: `test/support/fakeAnthropic.ts` is a local Messages API
    (streaming SSE) that the real SDK and bundled CLI are pointed at as a gateway
    (`test/run.test.ts`). Postgres tests (`test/pg.test.ts`) skip unless
    `SCADBUDDY_TEST_DATABASE_URL` is set, as in the backend; the `agent` CI job sets it. The design is
  `docs/superpowers/specs/2026-09-27-ai-integration-design.md` (issue #250).
  The 09-22 design spec's "No database" statement (`2026-09-22-scadbuddy-design.md`
  §4, "Architecture") describes the backend container; the
  AI spec (#250, PR #303) adds Postgres (#241) for the system as a whole, and the
  09-27 template-pipelines spec makes Postgres and Temporal required.

## See also

- The vector fixtures are opened by `agent-durable/CLAUDE.md`. Generated API files: root CLAUDE.md.
- Backend contracts (the flows tools endpoint, orders, activity): `backend/CLAUDE.md`. Plugin source: `plugins/CLAUDE.md`.
- Migrations rule: root CLAUDE.md, "Migrations (#491)".
